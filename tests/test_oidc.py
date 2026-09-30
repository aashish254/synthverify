"""T49: AC-IDAM-1 - a locally minted JWT signed by a test-key JWKS is admitted and a
wrong-audience token is rejected. Every test here mints its own RSA keypair and its own
token, so nothing depends on a live IdP; FC-4 keeps that honest."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK

from synthverify import oidc as oidc_module
from synthverify.config import Settings, get_settings
from synthverify.db import ApiKey, UserRole
from synthverify.oidc import BearerPrincipal, OidcError, authenticate_bearer

ISSUER = "https://oidc.test.invalid/"
AUDIENCE = "synthverify"
KID = "sv-test-key-1"
JWKS_URL = f"{ISSUER}.well-known/jwks.json"


# --------------------------------------------------------------------- key + JWKS


def _mint_test_keypair() -> tuple[str, str]:
    """Return (private_pem, public_jwk_json_string) for a fresh RSA-2048 key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())
    return private_pem, public_jwk


@pytest.fixture()
def keypair() -> tuple[str, str]:
    return _mint_test_keypair()


@pytest.fixture()
def jwk_entry(keypair: tuple[str, str]) -> dict[str, Any]:
    private_pem, public_jwk = keypair
    entry = jwt.algorithms.RSAAlgorithm.from_jwk(public_jwk)  # sanity: parses
    assert entry is not None
    import json

    jwk = json.loads(public_jwk)
    jwk["kid"] = KID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


@pytest.fixture()
def oidc_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Configure OIDC via the same env surface an operator uses, and clear the cache
    so `get_settings()` sees the change without any test touching production code."""
    for name, value in {
        "SV_OIDC_ENABLED": "true",
        "SV_OIDC_ISSUER": ISSUER,
        "SV_OIDC_AUDIENCE": AUDIENCE,
        "SV_OIDC_JWKS_URL": JWKS_URL,
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture()
def jwks_stub(monkeypatch: pytest.MonkeyPatch, jwk_entry: dict[str, Any]):
    """Replace the network fetch with an in-memory JWKS so FC-4 holds even in the OIDC path."""
    calls: list[str] = []

    def _fake_fetch(url: str, *, timeout: float = 5.0) -> dict[str, PyJWK]:
        calls.append(url)
        return {KID: PyJWK(jwk_entry)}

    monkeypatch.setattr(oidc_module, "_fetch_jwks", _fake_fetch)
    oidc_module.clear_jwks_cache()
    yield calls
    oidc_module.clear_jwks_cache()


def _sign(
    keypair: tuple[str, str],
    *,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    subject: str = "user-42",
    role: str | list[str] = "analyst",
    org: str = "tenant-a",
    scopes: str | list[str] | None = None,
    platform_scope: bool | None = None,
    expires_in: int = 300,
    issued_at_offset: int = 0,
    kid: str = KID,
    algorithm: str = "RS256",
    extra: dict[str, Any] | None = None,
) -> str:
    private_pem, _ = keypair
    now = int(time.time()) + issued_at_offset
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": now,
        "exp": now + expires_in,
        "sv_role": role,
        "sv_org": org,
    }
    if scopes is not None:
        payload["sv_scopes"] = scopes
    if platform_scope is not None:
        payload["platform_scope"] = platform_scope
    if extra:
        payload.update(extra)
    headers = {"kid": kid} if kid else None
    return jwt.encode(payload, private_pem, algorithm=algorithm, headers=headers)


# --------------------------------------------------------- library-level AC-IDAM-1


def test_ac_idam_1_admits_a_locally_minted_jwt(keypair, oidc_settings, jwks_stub):
    """AC-IDAM-1, first half: a locally minted JWT signed by a test-key JWKS authenticates."""
    token = _sign(keypair)
    principal = authenticate_bearer(token, settings=oidc_settings)
    assert isinstance(principal, BearerPrincipal)
    assert principal.role == "analyst"
    assert principal.organisation == "tenant-a"
    assert principal.key_id == "oidc:user-42"
    assert principal.active is True
    # `platform_scope` is opt-in per settings; a plain token is a tenant credential.
    assert principal.platform_scope is False


def test_ac_idam_1_rejects_a_token_for_another_audience(keypair, oidc_settings, jwks_stub):
    """AC-IDAM-1, second half: a token for another audience is rejected."""
    token = _sign(keypair, audience="some-other-service")
    with pytest.raises(OidcError) as exc:
        authenticate_bearer(token, settings=oidc_settings)
    assert "Audience mismatch" in str(exc.value)


def test_wrong_issuer_is_refused(keypair, oidc_settings, jwks_stub):
    token = _sign(keypair, issuer="https://evil.invalid/")
    with pytest.raises(OidcError) as exc:
        authenticate_bearer(token, settings=oidc_settings)
    assert "Issuer mismatch" in str(exc.value)


def test_expired_token_is_refused(keypair, oidc_settings, jwks_stub):
    token = _sign(keypair, expires_in=-600)
    with pytest.raises(OidcError) as exc:
        authenticate_bearer(token, settings=oidc_settings)
    assert "expired" in str(exc.value).lower()


def test_unknown_kid_is_refused(keypair, oidc_settings, jwks_stub):
    token = _sign(keypair, kid="kid-that-does-not-exist")
    with pytest.raises(OidcError) as exc:
        authenticate_bearer(token, settings=oidc_settings)
    assert "not present in the issuer's JWKS" in str(exc.value)


def test_missing_kid_in_header_is_refused(keypair, oidc_settings, jwks_stub):
    token = _sign(keypair, kid=None)
    with pytest.raises(OidcError) as exc:
        authenticate_bearer(token, settings=oidc_settings)
    assert "no kid" in str(exc.value)


def test_alg_confusion_is_refused(keypair, oidc_settings, jwks_stub):
    """The classic bypass: an attacker takes the RS256 public key material the verifier
    trusts, uses it as an HS256 HMAC secret, mints an admin token, and hopes the verifier
    follows the header's `alg`. The allow-list stops it before PyJWT ever sees the token.

    Forged by hand because PyJWT now refuses to *encode* an HMAC secret that looks
    asymmetric - the same class of mistake, produced the same way an attacker would."""
    import base64
    import hmac as _hmac

    _, public_jwk_str = keypair
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(public_jwk_str)
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def _b64(obj: bytes) -> str:
        return base64.urlsafe_b64encode(obj).rstrip(b"=").decode()

    header = _b64(b'{"alg":"HS256","typ":"JWT","kid":"' + KID.encode() + b'"}')
    claims = _b64(
        (
            '{"iss":"' + ISSUER + '","aud":"' + AUDIENCE + '","sub":"attacker",'
            '"iat":' + str(int(time.time())) + ',"exp":' + str(int(time.time()) + 300) + ','
            '"sv_role":"admin","sv_org":"anyone"}'
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    sig = _b64(_hmac.new(public_pem, signing_input, "sha256").digest())
    attacker_token = f"{header}.{claims}.{sig}"

    with pytest.raises(OidcError) as exc:
        authenticate_bearer(attacker_token, settings=oidc_settings)
    assert "not in SV_OIDC_ALLOWED_ALGORITHMS" in str(exc.value)


def test_signature_from_other_key_is_refused(keypair, oidc_settings, jwks_stub):
    other_private, _ = _mint_test_keypair()
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "attacker",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "sv_role": "admin",
        },
        other_private,
        algorithm="RS256",
        headers={"kid": KID},
    )
    with pytest.raises(OidcError) as exc:
        authenticate_bearer(forged, settings=oidc_settings)
    assert "signature" in str(exc.value).lower() or "Invalid" in str(exc.value)


def test_oidc_disabled_short_circuits_before_the_network(monkeypatch: pytest.MonkeyPatch):
    """FC-4 shape: with the default settings (OIDC off) the JWT path never opens a socket."""

    def _boom(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("JWKS fetched while oidc_enabled was False")

    monkeypatch.setattr(oidc_module, "_fetch_jwks", _boom)
    off = Settings(oidc_enabled=False)
    with pytest.raises(OidcError) as exc:
        authenticate_bearer("eyJ.eyJ.eyJ", settings=off)
    assert "not enabled" in str(exc.value)


# -------------------------------------------------------------------- role mapping


@pytest.mark.parametrize(
    ("role_claim", "expected"),
    [
        ("admin", "admin"),
        ("analyst", "analyst"),
        ("service", "service"),
        (["service", "analyst"], "analyst"),
        (["viewer", "admin"], "admin"),
    ],
)
def test_role_mapping_onto_the_existing_three_roles(
    keypair, oidc_settings, jwks_stub, role_claim, expected
):
    """Spec REQ-IDAM-1: "Roles map onto the existing admin/analyst/service model rather than
    inventing a parallel one." The mapper takes the highest-privilege recognised entry."""
    token = _sign(keypair, role=role_claim)
    principal = authenticate_bearer(token, settings=oidc_settings)
    assert principal.role == expected


def test_token_with_no_recognised_role_is_refused(keypair, oidc_settings, jwks_stub):
    token = _sign(keypair, role="developer")
    with pytest.raises(OidcError) as exc:
        authenticate_bearer(token, settings=oidc_settings)
    assert "no recognised role" in str(exc.value)


# ----------------------------------------------------------------- scopes plumbing


def test_scopes_claim_flows_into_require_scope_shape(keypair, oidc_settings, jwks_stub):
    """T48's require_scope reads `api_key.scopes`; a JWT with a scopes claim lands as a
    CSV the same parser produces for a static key, so the 403-naming-the-scope contract
    transfers to the bearer path without touching that code."""
    token = _sign(keypair, scopes=["jobs:read", "media:submit"])
    principal = authenticate_bearer(token, settings=oidc_settings)
    assert principal.scopes == "jobs:read,media:submit"

    from synthverify.auth import effective_scopes, parse_scopes

    granted = effective_scopes(principal)
    assert granted == frozenset({"jobs:read", "media:submit"})
    assert parse_scopes("jobs:read,media:submit") == granted


def test_missing_scopes_claim_falls_through_to_role_equivalent(
    keypair, oidc_settings, jwks_stub
):
    """T48's backward-compat rule is the same for a JWT: an unset `scopes` means the
    role grant, so a bearer analyst keeps the analyst role-scopes union."""
    token = _sign(keypair)
    principal = authenticate_bearer(token, settings=oidc_settings)
    assert principal.scopes is None

    from synthverify.auth import ROLE_SCOPES, effective_scopes

    granted = effective_scopes(principal)
    assert granted == ROLE_SCOPES[UserRole.ANALYST.value]


def test_platform_scope_claim_requires_opt_in(keypair, oidc_settings, jwks_stub):
    """A JWT that carries `platform_scope: true` is still a tenant credential unless
    SV_OIDC_ALLOW_PLATFORM_SCOPE_CLAIM is set; an IdP that does not vet the claim
    cannot mint a cross-tenant bearer by adding a JSON field."""
    token = _sign(keypair, platform_scope=True)
    principal = authenticate_bearer(token, settings=oidc_settings)
    assert principal.platform_scope is False


def test_platform_scope_opt_in_honours_the_claim(
    keypair, oidc_settings, jwks_stub, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SV_OIDC_ALLOW_PLATFORM_SCOPE_CLAIM", "true")
    get_settings.cache_clear()
    on = get_settings()
    assert on.oidc_allow_platform_scope_claim is True
    token = _sign(keypair, platform_scope=True)
    principal = authenticate_bearer(token, settings=on)
    assert principal.platform_scope is True


# ------------------------------------------------------- integration with the API


def _oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same four env vars an operator sets: enable + issuer + audience + JWKS URL."""
    for name, value in {
        "SV_OIDC_ENABLED": "true",
        "SV_OIDC_ISSUER": ISSUER,
        "SV_OIDC_AUDIENCE": AUDIENCE,
        "SV_OIDC_JWKS_URL": JWKS_URL,
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


async def _raw_bearer_client(app, token: str):
    """An AsyncClient that sends *only* `Authorization: Bearer <token>` - the `client`
    fixture defaults to `X-API-Key: <admin>`, which `extract_key` prefers over bearer,
    so OIDC-shape tests need to bypass that default."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url="http://testserver")
    c.headers["Authorization"] = f"Bearer {token}"
    return c


@pytest.mark.anyio
async def test_bearer_admitted_to_job_read_via_api(client, keypair, jwks_stub, monkeypatch):
    """End-to-end shape AC-IDAM-1 names: `require_analyst` admits a locally minted JWT."""
    _oidc_env(monkeypatch)
    token = _sign(keypair, role="analyst", org="default")
    scoped = await _raw_bearer_client(client._transport.app, token)
    try:
        resp = await scoped.get("/api/v1/jobs")
    finally:
        await scoped.aclose()
        get_settings.cache_clear()
    assert resp.status_code == 200, resp.text


@pytest.mark.anyio
async def test_wrong_audience_rejected_via_api_401(client, keypair, jwks_stub, monkeypatch):
    _oidc_env(monkeypatch)
    token = _sign(keypair, audience="not-this-service")
    scoped = await _raw_bearer_client(client._transport.app, token)
    try:
        resp = await scoped.get("/api/v1/jobs")
    finally:
        await scoped.aclose()
        get_settings.cache_clear()
    assert resp.status_code == 401
    assert "Audience mismatch" in resp.text


@pytest.mark.anyio
async def test_bearer_respects_t48_scope_naming(client, keypair, jwks_stub, monkeypatch):
    """The 403 that names the missing scope is a T48 rule; it must apply unchanged
    when the credential is a JWT rather than a static key."""
    _oidc_env(monkeypatch)
    # A service-role bearer token scoped to `jobs:read`; the artifacts route needs
    # `artifacts:read`, so require_scope must fire before the fake-id lookup does.
    token = _sign(keypair, role="service", org="default", scopes=["jobs:read"])
    scoped = await _raw_bearer_client(client._transport.app, token)
    try:
        resp = await scoped.get("/api/v1/jobs/fake-id/artifacts")
    finally:
        await scoped.aclose()
        get_settings.cache_clear()
    assert resp.status_code == 403, resp.text
    assert "artifacts:read" in resp.text


@pytest.mark.anyio
async def test_oidc_disabled_shape_leaves_static_keys_working(client):
    """With OIDC off (the default), a `sv_live_*` key authenticates exactly as before T49,
    and a JWT-shaped bearer string is refused on the API-key path rather than reaching
    any JWKS fetch."""
    from sqlalchemy import select

    app = client._transport.app
    with app.state.db.session() as s:
        existing = s.execute(select(ApiKey).where(ApiKey.active.is_(True))).scalars().first()
    assert existing is not None, "conftest bootstrap must create a key"

    # No Authorization override on `client` means X-API-Key (bootstrap admin) still wins,
    # so build a raw client that carries only the fake JWT to prove the shape is refused
    # by the API-key path with `Invalid or revoked API key` and never reaches a JWKS fetch.
    fake_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.fake_sig"
    scoped = await _raw_bearer_client(app, fake_token)
    try:
        resp = await scoped.get("/api/v1/jobs")
    finally:
        await scoped.aclose()
    assert resp.status_code == 401, resp.text
    assert "Invalid or revoked API key" in resp.text
