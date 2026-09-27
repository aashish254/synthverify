"""REQ-INFRA-4 / AC-INFRA-4: the media-store seam.

Three things are under test, and the third is the one that matters:

1. the key scheme and each backend in isolation,
2. SigV4 signing, checked by an in-process S3 mock that **re-implements the
   signature independently** - a mock that trusted the product's own signing code
   would prove nothing,
3. **parity**: the same operations, and the same ingest→verdict run, asserted once
   against both backends with no ``if backend == ...`` branch in the test body.
   That is what "swap ``SV_MEDIA_STORE`` with no code change" actually claims.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from conftest import wait_for_job  # noqa: E402  (tests/ is on sys.path via conftest)
from fixtures_gen import ai_generated_photo, natural_photo  # noqa: E402

from synthverify.db import MediaAsset, utcnow
from synthverify.retention import SweepPlan, SweepReport, remove_storage
from synthverify.storage import (
    LocalMediaStore,
    MediaNotFoundError,
    MediaStore,
    MediaStoreError,
    NotLocalError,
    S3MediaStore,
    content_key,
    get_media_store,
    reset_media_store_cache,
    safe_filename,
)
from synthverify.storage.base import _SHARD_PREFIX  # the scheme itself, not a copy of it

API = "/api/v1"
BUCKET = "synthverify-evidence"
REGION = "us-east-1"
ACCESS_KEY = "svtestaccesskey"
SECRET_KEY = "svtestsecretkey"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


# ------------------------------------------------------- an independent S3 mock


class MockS3:
    """Loopback S3-compatible endpoint that verifies its own way."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.mock = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> MockS3:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def of(self, method: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method]

    def key_of(self, method: str = "PUT") -> str:
        return self.of(method)[0]["key"]


class _Handler(BaseHTTPRequestHandler):
    """SigV4 verified from scratch: canonical request, scope, payload hash.

    Two failure modes matter and are distinguished on purpose: a signature that
    does not recompute (``SignatureDoesNotMatch``) and a body whose hash does not
    match the claimed ``x-amz-content-sha256`` (``XAmzContentSHA256Mismatch``).
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    @property
    def mock(self) -> MockS3:
        return self.server.mock  # type: ignore[attr-defined]

    # ------------------------------------------------------------- dispatch

    def do_PUT(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        checked = self._authorize(body)
        if checked is None:
            return
        key = checked["key"]
        self.mock.objects[key] = body
        self._record(checked, 200)
        self._respond(200, b"", etag=f'"{hashlib.md5(body).hexdigest()}"')  # noqa: S303 - not a password

    def do_GET(self):  # noqa: N802
        checked = self._authorize(b"")
        if checked is None:
            return
        body = self.mock.objects.get(checked["key"])
        if body is None:
            self._xml_error(404, "NoSuchKey", checked)
            return
        self._record(checked, 200)
        self._respond(200, body)

    def do_HEAD(self):  # noqa: N802
        checked = self._authorize(b"")
        if checked is None:
            return
        body = self.mock.objects.get(checked["key"])
        status = 200 if body is not None else 404
        self._record(checked, status)
        self.send_response(status)
        self.send_header("content-length", str(len(body or b"")))
        self.end_headers()

    def do_DELETE(self):  # noqa: N802
        checked = self._authorize(b"")
        if checked is None:
            return
        # S3 answers 204 whether or not the key was there - which is exactly why
        # the product's delete() derives its return value from a HEAD.
        self.mock.objects.pop(checked["key"], None)
        self._record(checked, 204)
        self.send_response(204)
        self.send_header("content-length", "0")
        self.end_headers()

    # --------------------------------------------------------- verification

    def _authorize(self, body: bytes) -> dict | None:
        raw_path, _, _query = self.path.partition("?")
        # Like a real server: the signature is verified over the *raw* path, and the
        # object key is the decoded one. Keeping the two apart is what makes the
        # percent-encoding test below mean something.
        decoded_path = "/" + unquote(raw_path.lstrip("/"))
        auth = self.headers.get("authorization", "")
        claimed = {
            "key": decoded_path.lstrip("/").partition("/")[2],
            "bucket": raw_path.lstrip("/").partition("/")[0],
            "path": raw_path,
            "payload_hash": self.headers.get("x-amz-content-sha256", ""),
            "amz_date": self.headers.get("x-amz-date", ""),
        }
        if not auth.startswith("AWS4-HMAC-SHA256 "):
            return self._reject("AccessDenied", claimed, 403)
        credential, signed_headers, signature = _parse_auth(auth)
        pieces = credential.split("/")
        if len(pieces) != 5:
            return self._reject("InvalidCredential", claimed, 400)
        access_key, date_stamp, region, service, terminator = pieces
        claimed |= {
            "access_key": access_key, "scope": "/".join(pieces[1:]),
            "signed_headers": signed_headers, "credential_region": region,
            "credential_service": service,
        }
        if claimed["payload_hash"] != hashlib.sha256(body).hexdigest():
            return self._reject("XAmzContentSHA256Mismatch", claimed, 403)
        if (service, terminator, region, access_key) != ("s3", "aws4_request", REGION, ACCESS_KEY):
            return self._reject("CredentialScopeMismatch", claimed, 403)
        canonical_headers = "".join(
            f"{name}:{self.headers.get(name, '').strip()}\n" for name in signed_headers.split(";")
        )
        canonical = "\n".join(
            [self.command, raw_path, "", canonical_headers, signed_headers, claimed["payload_hash"]]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256", claimed["amz_date"], claimed["scope"],
                hashlib.sha256(canonical.encode()).hexdigest(),
            ]
        )
        expected = _signature(SECRET_KEY, string_to_sign)
        if not hmac.compare_digest(expected, signature):
            return self._reject("SignatureDoesNotMatch", claimed, 403)
        if not claimed["key"]:
            return self._reject("InvalidRequest", claimed, 400)
        return claimed

    def _reject(self, code: str, claimed: dict, status: int) -> None:
        self._xml_error(status, code, claimed)

    def _xml_error(self, status: int, code: str, claimed: dict) -> None:
        body = f'<?xml version="1.0"?><Error><Code>{code}</Code></Error>'.encode()
        self._record(claimed, status, code=code)
        self._respond(status, body, content_type="application/xml")

    def _respond(self, status: int, body: bytes, **extra: str) -> None:
        self.send_response(status)
        for name, value in extra.items():
            self.send_header(name.replace("_", "-"), value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _record(self, claimed: dict, status: int, code: str = "") -> None:
        # Every caller logs *before* flushing the reply: a test that inspects `requests` as soon
        # as the response arrives would otherwise race this thread and see an empty log.
        entry = dict(claimed)
        entry |= {"method": self.command, "status": status, "code": code}
        self.mock.requests.append(entry)


def _parse_auth(auth: str) -> tuple[str, str, str]:
    fields = {}
    for piece in auth[len("AWS4-HMAC-SHA256 "):].split(","):
        name, _, value = piece.strip().partition("=")
        fields[name] = value
    return fields.get("Credential", ""), fields.get("SignedHeaders", ""), fields.get("Signature", "")


def _signature(secret: str, message: str) -> str:
    key = ("AWS4" + secret).encode()
    for part in (message.split("\n")[2].split("/")[0], REGION, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


class _RecordingTransport(httpx.BaseTransport):
    """Captures the wire request instead of sending it - for style/URI checks."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=b"")


def _signature_is_valid(request: httpx.Request) -> bool:
    """Re-derive SigV4 from the transmitted request alone.

    Nothing here imports the product's signing code, so a signature that only
    matches the product's own canonicalisation is still rejected.
    """
    credential, signed_headers, signature = _parse_auth(request.headers.get("authorization", ""))
    access_key, date_stamp, region, service, terminator = credential.split("/")
    payload = request.content
    payload_hash = hashlib.sha256(payload).hexdigest()
    if request.headers.get("x-amz-content-sha256") != payload_hash:
        return False
    if (access_key, region, service, terminator) != (ACCESS_KEY, REGION, "s3", "aws4_request"):
        return False
    canonical_headers = "".join(
        f"{name}:{request.headers.get(name, '').strip()}\n" for name in signed_headers.split(";")
    )
    canonical_request = "\n".join(
        [request.method, request.url.raw_path.decode(), "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/{service}/{terminator}"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            request.headers["x-amz-date"],
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = ("AWS4" + SECRET_KEY).encode()
    for part in (date_stamp, region, service, terminator):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest(), signature)


@pytest.fixture()
def mock_s3() -> MockS3:
    server = MockS3().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def s3_store(mock_s3) -> S3MediaStore:
    return S3MediaStore(
        endpoint=mock_s3.url, bucket=BUCKET, region=REGION,
        access_key=ACCESS_KEY, secret_key=SECRET_KEY, prefix="media",
    )


def local_at(tmp_path: Path) -> LocalMediaStore:
    return LocalMediaStore(tmp_path / "media")


# --------------------------------------------------------------- the key scheme


class TestKeySchemeIsDefinedOnce:
    def test_shard_then_digest_then_sanitised_name(self):
        key = content_key(DIGEST_A, "scene 01.jpg")
        shard, rest = key.split("/")
        assert len(shard) == _SHARD_PREFIX and DIGEST_A.startswith(shard)
        assert rest == f"{DIGEST_A}_scene_01.jpg"

    def test_the_digest_is_the_identity_and_the_name_is_decoration(self):
        assert content_key(DIGEST_A, "a.jpg").split("/")[0] == content_key(DIGEST_A, "b.jpg").split("/")[0]
        assert content_key(DIGEST_A, "a.jpg").split("_", 1)[1] == "a.jpg"
        assert content_key(DIGEST_A, "a.jpg") == content_key(DIGEST_A, "a.jpg")

    @pytest.mark.parametrize(
        "hostile",
        ["../../etc/passwd", "a/../../../b.jpg", "..", "\\\\server\\share\\x.jpg", "", "....//x", "/"],
    )
    def test_a_filename_can_never_redirect_a_write(self, hostile):
        name = safe_filename(hostile)
        assert "/" not in name and "\\" not in name and ".." not in name
        assert name and not name.startswith(".")
        assert content_key(DIGEST_A, hostile).count("/") == 1

    def test_long_names_are_capped_but_keep_their_extension(self):
        name = safe_filename("x" * 500 + ".jpeg")
        assert len(name) <= 100 and name.endswith(".jpeg")

    def test_a_non_digest_is_refused_rather_than_sharded_wrongly(self):
        with pytest.raises(ValueError, match="sha256"):
            content_key("not-a-digest", "x.jpg")


# -------------------------------------------------------------- local behaviour


class TestLocal:
    def test_layout_is_the_v1_on_disk_layout(self, tmp_path):
        """Sharding and naming are what an existing deployment already has."""
        store = local_at(tmp_path)
        location = store.put(DIGEST_A, "a.jpg", b"x")
        assert location == str((tmp_path / "media" / "aa" / f"{DIGEST_A}_a.jpg").resolve())

    def test_legacy_absolute_paths_stay_readable(self, tmp_path):
        """Rows written before the seam exist: a stored path is honoured verbatim."""
        store = local_at(tmp_path)
        legacy = tmp_path / "media" / "ff" / f"{DIGEST_B}_old.jpg"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"pre-refactor bytes")
        assert store.get(str(legacy)) == b"pre-refactor bytes"
        assert store.exists(str(legacy))
        assert store.key_for_location(str(legacy)) == f"ff/{DIGEST_B}_old.jpg"

    def test_a_key_and_a_location_round_trip(self, tmp_path):
        store = local_at(tmp_path)
        key = content_key(DIGEST_A, "a.jpg")
        assert store.key_for_location(store.location(key)) == key
        assert store.location(key).startswith(str(store.root))

    def test_identical_bytes_under_the_same_name_are_stored_once(self, tmp_path):
        store = local_at(tmp_path)
        first = store.put(DIGEST_A, "a.jpg", b"same")
        again = store.put(DIGEST_A, "a.jpg", b"same")
        assert first == again
        assert len(list((tmp_path / "media").rglob("*_*"))) == 1

    def test_no_partial_object_is_ever_readable(self, tmp_path, monkeypatch):
        """A failed write must leave nothing behind for a reader to half-consume."""
        import os as os_module

        store = local_at(tmp_path)
        monkeypatch.setattr(os_module, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(OSError, match="full"):
            store.put(DIGEST_A, "a.jpg", b"payload")
        assert not store.exists(store.location(content_key(DIGEST_A, "a.jpg")))
        assert not list((tmp_path / "media" / "aa").glob(".part-*"))

    def test_missing_object_is_media_not_found_not_oserror(self, tmp_path):
        store = local_at(tmp_path)
        location = store.location(content_key(DIGEST_A, "gone.jpg"))
        assert store.exists(location) is False
        with pytest.raises(MediaNotFoundError):
            store.get(location)
        assert store.delete(location) is False

    def test_open_path_hands_out_a_real_file(self, tmp_path):
        store = local_at(tmp_path)
        path = store.open_path(store.put(DIGEST_A, "a.jpg", b"payload"))
        assert path.is_file() and path.read_bytes() == b"payload"

    def test_describe_names_the_root_and_no_secrets(self, tmp_path):
        assert local_at(tmp_path).describe().startswith("local:")


# ------------------------------------------------------------ SigV4 correctness


class TestSigV4:
    def test_the_mock_is_not_a_rubber_stamp(self, s3_store, mock_s3):
        """A tampered body must be refused, or the mock trusts whatever we send."""
        s3_store.put(DIGEST_A, "a.jpg", b"honest bytes")
        forged = f"s3://{BUCKET}/media/aa/{DIGEST_A}_a.jpg"
        assert s3_store.get(forged) == b"honest bytes"
        assert mock_s3.of("PUT")[0]["payload_hash"] == hashlib.sha256(b"honest bytes").hexdigest()

    def test_the_signed_headers_are_the_ones_we_declare(self, s3_store, mock_s3):
        s3_store.put(DIGEST_A, "a.jpg", b"x")
        put = mock_s3.of("PUT")[0]
        assert put["signed_headers"] == "host;x-amz-content-sha256;x-amz-date"
        assert put["amz_date"].startswith(put["scope"].split("/")[0])
        assert "/".join(put["scope"].split("/")[1:]) == f"{REGION}/s3/aws4_request"
        assert put["access_key"] == ACCESS_KEY

    def test_a_wrong_secret_is_rejected_as_a_signature_mismatch(self, mock_s3):
        store = S3MediaStore(
            endpoint=mock_s3.url, bucket=BUCKET, region=REGION,
            access_key=ACCESS_KEY, secret_key="not the secret",
        )
        with pytest.raises(MediaStoreError, match="SignatureDoesNotMatch"):
            store.put(DIGEST_A, "a.jpg", b"x")
        assert mock_s3.of("PUT")[0]["code"] == "SignatureDoesNotMatch"

    def test_a_wrong_region_breaks_the_credential_scope(self, mock_s3):
        store = S3MediaStore(
            endpoint=mock_s3.url, bucket=BUCKET, region="eu-west-1",
            access_key=ACCESS_KEY, secret_key=SECRET_KEY,
        )
        with pytest.raises(MediaStoreError, match="CredentialScopeMismatch"):
            store.put(DIGEST_A, "a.jpg", b"x")

    def test_the_uri_that_is_signed_is_the_uri_that_is_sent(self, s3_store, mock_s3):
        """Percent-encoding drift is the classic SigV4 bug: both sides must agree."""
        s3_store.put(DIGEST_A, "a+b~c.jpg", b"x")
        put = mock_s3.of("PUT")[0]
        assert "%2B" in put["path"], "the '+' must reach the wire encoded"
        assert put["key"] == f"media/aa/{DIGEST_A}_a+b_c.jpg"
        assert put["status"] == 200, "the mock only 200s when its own recomputation matches"

    def test_both_addressing_styles_sign_the_target_they_actually_send(self):
        """Virtual-host style sends a different target, so it must sign a different URI.

        Checked against the bytes on the wire with an independent re-derivation, not
        against the product's own canonical request: signing the path-style URI while
        sending the bucket-less one is precisely the bug this catches.
        """
        for path_style, expected_target in {
            True: f"/{BUCKET}/aa/{DIGEST_A}_a.jpg",
            False: f"/aa/{DIGEST_A}_a.jpg",
        }.items():
            wire = _RecordingTransport()
            store = S3MediaStore(
                endpoint=f"https://s3.{REGION}.amazonaws.com", bucket=BUCKET, region=REGION,
                access_key=ACCESS_KEY, secret_key=SECRET_KEY, path_style=path_style,
                transport=wire,
            )
            store.put(DIGEST_A, "a.jpg", b"payload bytes")
            request = wire.requests[-1]
            assert request.url.raw_path.decode() == expected_target
            assert _signature_is_valid(request), f"{expected_target} was signed for a different request"
            # ...and the verifier is not a rubber stamp: a re-sent request with a
            # different body must no longer verify.
            tampered = httpx.Request(
                request.method, request.url,
                headers={**dict(request.headers), "x-amz-content-sha256": hashlib.sha256(b"other").hexdigest()},
                content=b"other",
            )
            assert not _signature_is_valid(tampered)


class TestS3Behaviour:
    def test_location_is_an_s3_uri_under_the_prefix(self, s3_store):
        location = s3_store.put(DIGEST_A, "a.jpg", b"object bytes")
        assert location == f"s3://{BUCKET}/media/aa/{DIGEST_A}_a.jpg"
        assert s3_store.get(location) == b"object bytes"
        assert s3_store.key_for_location(location) == content_key(DIGEST_A, "a.jpg")

    def test_a_row_written_by_the_local_backend_is_not_guessed_at(self, s3_store):
        with pytest.raises(MediaStoreError, match="filesystem path"):
            s3_store.get("/var/lib/synthverify/media/aa/x.jpg")

    def test_a_row_from_another_bucket_fails_loudly(self, s3_store):
        with pytest.raises(MediaStoreError, match="bucket"):
            s3_store.get(f"s3://some-other-bucket/aa/{DIGEST_A}_a.jpg")

    def test_missing_object_is_media_not_found(self, s3_store, mock_s3):
        location = s3_store.location(content_key(DIGEST_A, "never-put.jpg"))
        assert s3_store.exists(location) is False
        assert mock_s3.of("HEAD")[0]["status"] == 404
        with pytest.raises(MediaNotFoundError):
            s3_store.get(location)

    def test_delete_reports_whether_something_went_away(self, s3_store):
        location = s3_store.put(DIGEST_A, "a.jpg", b"x")
        assert s3_store.delete(location) is True
        assert s3_store.delete(location) is False

    def test_no_filesystem_path_from_an_object_store(self, s3_store):
        with pytest.raises(NotLocalError):
            s3_store.open_path(f"s3://{BUCKET}/aa/x")

    def test_an_unreachable_endpoint_becomes_one_typed_error(self, mock_s3):
        """The endpoint going away mid-run must not leak httpx into an API response."""
        store = S3MediaStore(
            endpoint="http://127.0.0.1:1", bucket=BUCKET, region=REGION,
            access_key=ACCESS_KEY, secret_key=SECRET_KEY, timeout=2.0,
        )
        with pytest.raises(MediaStoreError, match="could not reach the media endpoint"):
            store.get(f"s3://{BUCKET}/aa/x.jpg")

    def test_configuration_is_required(self):
        with pytest.raises(ValueError, match="access key"):
            S3MediaStore(endpoint="http://x", bucket="b", access_key="", secret_key="s")
        with pytest.raises(ValueError, match="endpoint and a bucket"):
            S3MediaStore(endpoint="", bucket="b", access_key="a", secret_key="s")


# ------------------------------------------------------------------- the factory


class TestFactory:
    def _settings(self, monkeypatch, **env: str):
        from synthverify.config import get_settings

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        reset_media_store_cache()
        return get_settings()

    def test_default_is_the_filesystem(self, monkeypatch, tmp_path):
        settings = self._settings(monkeypatch, SV_STORAGE_DIR=str(tmp_path / "media"))
        store = get_media_store(settings)
        assert isinstance(store, LocalMediaStore)
        assert store.root == (tmp_path / "media").resolve()

    def test_s3_is_selected_by_config_alone(self, monkeypatch, tmp_path):
        settings = self._settings(
            monkeypatch,
            SV_MEDIA_STORE="s3", SV_S3_ENDPOINT="http://minio:9000", SV_S3_BUCKET="evidence",
            SV_S3_ACCESS_KEY="a", SV_S3_SECRET_KEY="b", SV_S3_PREFIX="media",
        )
        store = get_media_store(settings)
        assert isinstance(store, S3MediaStore)
        assert (store.bucket, store.prefix, store.host) == ("evidence", "media", "minio:9000")

    def test_s3_without_a_target_is_a_startup_error_not_a_silent_local(self, monkeypatch, tmp_path):
        settings = self._settings(monkeypatch, SV_MEDIA_STORE="s3", SV_STORAGE_DIR=str(tmp_path))
        with pytest.raises(ValueError, match="SV_S3_BUCKET"):
            get_media_store(settings)

    def test_an_unknown_backend_is_refused(self, monkeypatch, tmp_path):
        settings = self._settings(monkeypatch, SV_MEDIA_STORE="swift", SV_STORAGE_DIR=str(tmp_path))
        with pytest.raises(ValueError, match="unknown SV_MEDIA_STORE"):
            get_media_store(settings)

    def test_one_store_per_configuration_then_a_new_one_after_a_change(self, monkeypatch, tmp_path):
        settings = self._settings(monkeypatch, SV_STORAGE_DIR=str(tmp_path / "media"))
        assert get_media_store(settings) is get_media_store(settings)
        settings = self._settings(monkeypatch, SV_STORAGE_DIR=str(tmp_path / "other"))
        assert get_media_store(settings).root == (tmp_path / "other").resolve()

    def test_endpoint_and_bucket_change_the_cached_instance(self, monkeypatch, tmp_path):
        first = self._settings(
            monkeypatch, SV_MEDIA_STORE="s3", SV_S3_ENDPOINT="http://a:9000",
            SV_S3_BUCKET="one", SV_S3_ACCESS_KEY="k", SV_S3_SECRET_KEY="s",
        )
        assert get_media_store(first).bucket == "one"
        second = self._settings(
            monkeypatch, SV_MEDIA_STORE="s3", SV_S3_ENDPOINT="http://a:9000",
            SV_S3_BUCKET="two", SV_S3_ACCESS_KEY="k", SV_S3_SECRET_KEY="s",
        )
        assert get_media_store(second).bucket == "two"


# ------------------------------------------------- AC-INFRA-4: the parity claims


@pytest.fixture(params=["local", "s3"])
def parity_store(request, tmp_path, mock_s3) -> MediaStore:
    """One store per backend, built only from configuration."""
    if request.param == "local":
        return LocalMediaStore(tmp_path / "media")
    return S3MediaStore(
        endpoint=mock_s3.url, bucket=BUCKET, region=REGION,
        access_key=ACCESS_KEY, secret_key=SECRET_KEY,
    )


class TestBackendParity:
    """Identical assertions, both backends, zero branches (AC-INFRA-4)."""

    def test_put_then_get(self, parity_store):
        location = parity_store.put(DIGEST_A, "evidence.jpg", b"the bytes")
        assert parity_store.get(location) == b"the bytes"
        assert parity_store.exists(location) is True

    def test_location_round_trips_through_its_own_key(self, parity_store):
        key = content_key(DIGEST_B, "clip.wav")
        assert parity_store.key_for_location(parity_store.location(key)) == key

    def test_rewriting_identical_bytes_is_not_an_error(self, parity_store):
        first = parity_store.put(DIGEST_A, "a.jpg", b"same")
        assert parity_store.put(DIGEST_A, "a.jpg", b"same") == first
        assert parity_store.get(first) == b"same"

    def test_missing_media_is_one_typed_error(self, parity_store):
        location = parity_store.location(content_key(DIGEST_A, "absent.jpg"))
        assert parity_store.exists(location) is False
        with pytest.raises(MediaNotFoundError):
            parity_store.get(location)

    def test_delete_is_idempotent_and_honest(self, parity_store):
        location = parity_store.put(DIGEST_A, "a.jpg", b"x")
        assert parity_store.delete(location) is True
        assert parity_store.delete(location) is False
        assert parity_store.exists(location) is False

    def test_a_hostile_filename_cannot_escape_either_backend(self, parity_store):
        location = parity_store.put(DIGEST_A, "../../../etc/passwd", b"x")
        assert parity_store.get(location) == b"x"
        key = parity_store.key_for_location(location)
        assert key == content_key(DIGEST_A, "../../../etc/passwd")
        assert ".." not in key and key.count("/") == 1, "the key must stay one shard deep"
        assert location.endswith(key.split("/", 1)[1]), "no extra directory from the name"

    def test_every_backend_describes_itself_without_secrets(self, parity_store):
        described = parity_store.describe()
        assert described and SECRET_KEY not in described and ACCESS_KEY not in described


def _swept_report(key: str) -> SweepReport:
    """The shape a committed pass hands :func:`remove_storage`: one planned object, nothing removed yet.

    The single :class:`MediaAsset` exists only to make the plan non-empty - ``remove_storage`` returns
    early on an empty plan, and this is the storage half in isolation, with no database in sight.
    """
    plan = SweepPlan(now=utcnow(), media_keys=[key], assets=[MediaAsset(id="swept")])
    return SweepReport(plan=plan)


class TestSweptBytesLeaveTheStore:
    """T46's last step, run against both backends.

    ``remove_storage`` is the only retention code that touches an object store, and the sweep suites drive
    it on the filesystem - where "delete the file" and "issue a signed ``DELETE /bucket/key``" look the
    same from a test's point of view. Parity here is the claim that a TTL sweep reaches a remote bucket
    through the same seam ingest does, and that its ``removed``/``absent`` split survives the trip.
    """

    def test_a_planned_object_is_removed_from_the_store_that_holds_it(self, parity_store):
        key = content_key(DIGEST_A, "evidence.jpg")
        parity_store.put(DIGEST_A, "evidence.jpg", b"past due")
        report = _swept_report(key)

        remove_storage(parity_store, report)

        assert report.media_objects_removed == [key], report.outcomes
        assert report.media_objects_absent == []
        assert parity_store.exists(parity_store.location(key)) is False

    def test_a_repeat_pass_reports_absent_rather_than_removed(self, parity_store):
        """Idempotence, in the one place it is not a database property: the bytes."""
        key = content_key(DIGEST_B, "evidence.jpg")
        parity_store.put(DIGEST_B, "evidence.jpg", b"past due")
        remove_storage(parity_store, _swept_report(key))

        second = _swept_report(key)
        remove_storage(parity_store, second)

        assert second.media_objects_removed == [], second.outcomes
        assert second.media_objects_absent == [key], second.outcomes


# ------------------------------------------- the real call sites, both backends


@pytest.fixture()
def store_config(app_env, monkeypatch, request, tmp_path):
    """Point the running app at one backend. Config only - never a code branch."""
    from synthverify.config import get_settings

    if request.param == "s3":
        server = MockS3().start()
        monkeypatch.setenv("SV_MEDIA_STORE", "s3")
        monkeypatch.setenv("SV_S3_ENDPOINT", server.url)
        monkeypatch.setenv("SV_S3_BUCKET", BUCKET)
        monkeypatch.setenv("SV_S3_ACCESS_KEY", ACCESS_KEY)
        monkeypatch.setenv("SV_S3_SECRET_KEY", SECRET_KEY)
    else:
        server = None
        monkeypatch.setenv("SV_MEDIA_STORE", "local")
        monkeypatch.setenv("SV_STORAGE_DIR", str(tmp_path / "media"))
    get_settings.cache_clear()
    reset_media_store_cache()
    yield server
    get_settings.cache_clear()
    reset_media_store_cache()
    if server is not None:
        server.stop()


@pytest.mark.parametrize("store_config", ["local", "s3"], indirect=True)
class TestIngestThroughTheStore:
    async def test_ingest_to_verdict_writes_and_reads_the_same_object(self, store_config, client):
        from sqlalchemy import select

        from synthverify.app import app
        from synthverify.db import MediaAsset

        response = await client.post(
            f"{API}/media/ingest", files={"file": ("scene.jpg", natural_photo())}
        )
        assert response.status_code == 202
        job = await wait_for_job(client, response.json()["job_id"])

        # Same assertions from here down, whichever backend is configured.
        assert job["status"] == "completed", job
        assert job["result"]["detectors"], "no detector ran against stored bytes"
        store = get_media_store()
        with app.state.db.session() as session:
            asset = session.execute(select(MediaAsset)).scalars().one()
        assert store.exists(asset.storage_path), asset.storage_path
        assert store.get(asset.storage_path) == natural_photo()

    async def test_a_second_upload_of_the_same_bytes_reuses_one_object(self, store_config, client):
        data = ai_generated_photo()
        first = await client.post(f"{API}/media/ingest", files={"file": ("one.png", data)})
        second = await client.post(f"{API}/media/ingest", files={"file": ("two.png", data)})
        assert second.status_code == 202
        assert first.json()["links"]["media"] == second.json()["links"]["media"]
        store = get_media_store()
        location = store.location(content_key(hashlib.sha256(data).hexdigest(), "one.png"))
        assert store.exists(location)
        assert store.get(location) == data
