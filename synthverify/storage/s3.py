"""S3-compatible backend (MinIO by default) - **no new dependency**.

FC-1 and FC-2 decide the shape here: the AWS SDK is Apache-licensed so it would
pass FC-1, but it drags in botocore's transitive tree and an implicit assumption
that "S3-compatible" means AWS. So this is the whole protocol surface the product
needs - ``PUT``/``GET``/``HEAD``/``DELETE`` on one object - signed with AWS
Signature V4 from ``hmac`` + ``hashlib``, sent over ``httpx`` (already a
dependency for webhooks).

Point it at MinIO, SeaweedFS, R2, Ceph or AWS by setting ``SV_S3_ENDPOINT``;
nothing else changes. AC-INFRA-4 is the test that keeps that honest.

The transport is injectable so the parity test can run against an in-process
mock without a socket, and so an operator behind a proxy can pass their own.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from synthverify.storage.base import MediaNotFoundError, MediaStore, MediaStoreError

_PATH_SAFE = "-._~"  # everything else in a key is percent-encoded, per S3 SigV4
_DEFAULT_TIMEOUT = 30.0


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _encode_key(key: str) -> str:
    """Percent-encode an object key, leaving path separators intact."""
    return "/".join(quote(segment, safe=_PATH_SAFE) for segment in key.split("/"))


class S3Credentials:
    """Region + scope for SigV4, kept separate so signing is testable alone."""

    __slots__ = ("access_key", "secret_key")

    def __init__(self, access_key: str, secret_key: str) -> None:
        if not access_key or not secret_key:
            raise ValueError("S3 media store needs both an access key and a secret key")
        self.access_key = access_key
        self.secret_key = secret_key

    def authorization(self, *, method: str, uri: str, payload: bytes, host: str,
                      region: str, when: datetime) -> dict[str, str]:
        """SigV4 headers for one request. See AWS 'Signature Version 4' process."""
        amz_date = when.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        payload_hash = _sha256_hex(payload)
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join([method.upper(), uri, "", canonical_headers, signed_headers, payload_hash])
        scope = f"{date_stamp}/{region}/s3/aws4_request"
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode())]
        )
        key = _hmac(f"AWS4{self.secret_key}".encode(), date_stamp)
        key = _hmac(key, region)
        key = _hmac(key, "s3")
        key = _hmac(key, "aws4_request")
        signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        return {
            **headers,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }


class S3MediaStore(MediaStore):
    """Object storage over the S3 wire protocol."""

    backend = "s3"

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
        path_style: bool = True,
        prefix: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not endpoint or not bucket:
            raise ValueError("S3 media store needs an endpoint and a bucket")
        split = urlsplit(endpoint if "//" in endpoint else f"https://{endpoint}")
        self.scheme = split.scheme or "https"
        self.host = split.netloc or split.path
        self.bucket = bucket
        self.region = region
        self.path_style = path_style
        self.prefix = prefix.strip("/")
        self.timeout = timeout
        self._credentials = S3Credentials(access_key, secret_key)
        self._client = httpx.Client(transport=transport, timeout=timeout)

    # ------------------------------------------------------------------ write

    def put(self, digest: str, filename: str, data: bytes) -> str:
        content_key = self.key(digest, filename)
        response = self._request("PUT", content_key, payload=data)
        self._raise_for_status(response, "store", content_key)
        return self.location(content_key)

    # ------------------------------------------------------------------- read

    def get(self, location: str) -> bytes:
        content_key = self.key_for_location(location)
        response = self._request("GET", content_key)
        if response.status_code == 404:
            raise MediaNotFoundError(f"no media at {location}")
        self._raise_for_status(response, "read", content_key)
        return response.content

    def exists(self, location: str) -> bool:
        response = self._request("HEAD", self.key_for_location(location))
        if response.status_code == 404:
            return False
        self._raise_for_status(response, "stat", location)
        return True

    def delete(self, location: str) -> bool:
        # S3 answers 204 even when nothing was there, so "did anything go away?"
        # has to come from a HEAD. Retention sweeps pay the extra hop.
        existed = self.exists(location)
        response = self._request("DELETE", self.key_for_location(location))
        self._raise_for_status(response, "delete", location)
        return existed

    # ------------------------------------------------------- location <-> key
    #
    # Both directions speak the *content* key (no bucket, no configured prefix);
    # ``_object_key`` is applied only when a request is built, so a location and
    # its key round-trip exactly.

    def location(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._object_key(key)}"

    def key_for_location(self, location: str) -> str:
        head, _, tail = location.partition("://")
        if head == "s3":
            bucket, _, object_key = tail.partition("/")
            if bucket != self.bucket:
                raise MediaStoreError(
                    f"{location!r} names bucket {bucket!r}, but this store is configured "
                    f"for {self.bucket!r} - check SV_S3_BUCKET against the stored rows"
                )
        elif location.startswith("/"):
            raise MediaStoreError(
                f"{location!r} is a filesystem path, not an s3:// location: the row was written "
                "by the local backend. Copy the object in (aws s3 cp / mc mirror) or point "
                "SV_MEDIA_STORE back at local."
            )
        else:
            object_key = location
        if self.prefix and object_key.startswith(f"{self.prefix}/"):
            return object_key[len(self.prefix) + 1 :]
        return object_key

    # ----------------------------------------------------------------- extras

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"

    # ---------------------------------------------------------------- helpers

    def _object_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _request(self, method: str, content_key: str, payload: bytes = b"") -> httpx.Response:
        object_key = _encode_key(self._object_key(content_key))
        # The signed URI must be the request target minus the query, or the
        # signature will not match what the server re-computes.
        path = f"/{self.bucket}/{object_key}" if self.path_style else f"/{object_key}"
        url = f"{self.scheme}://{self.host}{path}"
        headers = self._credentials.authorization(
            method=method, uri=path, payload=payload, host=self.host,
            region=self.region, when=datetime.now(UTC),
        )
        headers["content-type"] = "application/octet-stream"
        try:
            return self._client.request(method, url, content=payload, headers=headers)
        except httpx.TransportError as exc:
            # An unreachable endpoint is an operational failure the API turns into
            # a 503, not an httpx leak into the response body.
            raise MediaStoreError(f"could not reach the media endpoint at {self.host}: {exc}") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response, verb: str, key: str) -> None:
        if response.status_code < 400:
            return
        detail = response.text[:200].replace("\n", " ")
        raise MediaStoreError(f"could not {verb} media at {key!r}: HTTP {response.status_code} {detail}")
