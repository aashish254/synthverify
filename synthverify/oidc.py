"""OIDC / JWT bearer authentication (REQ-IDAM-1, AC-IDAM-1).

A second credential type alongside static API keys. A bearer token is verified against
a JWKS the operator points at through ``SV_OIDC_JWKS_URL``; its claims are mapped onto
the existing ``admin``/``analyst``/``service`` role model rather than a parallel one,
so ``require_role``, ``require_scope`` and the ``visible_to`` tenancy rule keep working
unchanged for whichever credential type arrived.

Offline by construction. Nothing here reaches the network unless an operator has set
``SV_OIDC_ENABLED=true`` AND supplied a JWKS URL, so FC-4's socket-guard leg and the
sealed air-gap container exercise the same auth surface as a running install with OIDC
disabled - the OIDC branch is inert, not merely untested.

The bearer adapter mints ``BearerPrincipal`` rather than an ``ApiKey`` row because there
is no DB row to load: the credential is the token itself. ``BearerPrincipal`` duck-types
against ``ApiKey`` for every attribute ``auth.py`` reads (``role``, ``organisation``,
``platform_scope``, ``scopes``, ``key_id``, ``active``), so the same ``require_role``,
``require_scope`` and tenancy code paths answer a JWT exactly the way they answer a key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from synthverify.config import Settings, get_settings


class OidcError(Exception):
    """Every rejection reason the bearer adapter recognises, in the caller's words.

    Raised by :func:`authenticate_bearer`. ``auth.py`` turns one into a 401 with
    ``reason`` as the detail, so the shape is a stable vocabulary the tests assert on.
    """

    def __init__(self, reason: str, *, status_hint: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_hint = status_hint


@dataclass(frozen=True)
class BearerPrincipal:
    """A credential minted from a verified JWT.

    Carries every attribute :mod:`synthverify.auth` reads off an :class:`ApiKey`.
    ``key_id`` is the ``sub`` claim prefixed with ``oidc:`` so the audit ledger sees
    a bearer and a static key as different subjects even on the same organisation.
    ``last_used_at`` is None and there is no DB row to touch, which is why the
    throttled write in ``_authenticate`` must skip this shape.
    """

    role: str
    organisation: str
    key_id: str
    scopes: str | None = None
    platform_scope: bool = False
    active: bool = True
    subject: str = ""
    claims: dict[str, Any] = field(default_factory=dict)

    # Every ApiKey-shaped caller uses `.id` only in the last-used touch guard;
    # the sentinel None makes `is not None` on a row-loaded key evaluate truthy.
    id: int | None = None


# ----------------------------------------------------------------- JWKS fetch + cache

#: kid -> PyJWK. Per-URL so two configured issuers never share cache entries.
_JWKS_CACHE: dict[str, dict[str, PyJWK]] = {}
_JWKS_CACHE_AT: dict[str, float] = {}


def _fetch_jwks(url: str, *, timeout: float = 5.0) -> dict[str, PyJWK]:
    """Fetch the JWKS document and index its keys by ``kid``.

    Kept as a module-level function rather than a method so a test can monkeypatch
    one name and never opens a socket. Air-gap FC-4 stays green because
    :func:`authenticate_bearer` short-circuits before reaching here when
    ``oidc_enabled`` is false.
    """
    resp = httpx.get(url, timeout=timeout, headers={"Accept": "application/json"})
    resp.raise_for_status()
    payload = resp.json()
    keys: dict[str, PyJWK] = {}
    for entry in payload.get("keys", []):
        kid = entry.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = PyJWK(entry)
        except (jwt.InvalidKeyError, KeyError, ValueError):
            # A malformed JWK entry is not a reason to reject every token; skip it and
            # let a real kid miss against the surviving set. An IdP shipping a broken
            # key is a deployment problem, not an authentication one.
            continue
    return keys


def _jwks_for(url: str, refresh_seconds: int) -> dict[str, PyJWK]:
    """Return the cached JWKS for ``url``, refreshing when the TTL or a lookup misses."""
    cached = _JWKS_CACHE.get(url)
    cached_at = _JWKS_CACHE_AT.get(url, 0.0)
    now = time.monotonic()
    if cached is None or (now - cached_at) >= refresh_seconds:
        keys = _fetch_jwks(url)
        _JWKS_CACHE[url] = keys
        _JWKS_CACHE_AT[url] = now
        return keys
    return cached


def _jwks_lookup(url: str, kid: str, refresh_seconds: int) -> PyJWK | None:
    """Find the key for ``kid``, forcing one refresh on a miss inside the TTL.

    An IdP rotates keys; a token signed by the new key arrives before the next scheduled
    refresh does. Forcing one refetch on a cache miss lets rotation succeed without
    turning JWKS fetches into a per-request cost an attacker could hammer.
    """
    keys = _jwks_for(url, refresh_seconds)
    jwk = keys.get(kid)
    if jwk is not None:
        return jwk
    # One forced refresh, then re-check. Two network round-trips per rotation,
    # not per request.
    keys = _fetch_jwks(url)
    _JWKS_CACHE[url] = keys
    _JWKS_CACHE_AT[url] = time.monotonic()
    return keys.get(kid)


def clear_jwks_cache() -> None:
    """Drop every cached JWKS. Called by tests and by an operator rotating issuer URLs."""
    _JWKS_CACHE.clear()
    _JWKS_CACHE_AT.clear()


# ----------------------------------------------------------------- claim extraction


def _claim_by_path(claims: dict[str, Any], dotted: str) -> Any:
    """Read a possibly-dotted claim path, e.g. ``resource_access.analytics.roles``.

    Returns None on any missing hop. A claim named without a dot is just a top-level key.
    """
    current: Any = claims
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _pick_role(claims: dict[str, Any], role_claim: str) -> str | None:
    """Return one of ``admin``/``analyst``/``service`` the claims name, or None.

    A single string is used verbatim; a list is scanned in that priority order so a
    token that carries both ``admin`` and ``service`` becomes admin. Unknown values
    are ignored rather than rejected, because a Keycloak realm that also names a
    ``developer`` role is an operator's business, not the token's failure.
    """
    raw = _claim_by_path(claims, role_claim)
    if raw is None:
        return None
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        return candidate if candidate in {"admin", "analyst", "service"} else None
    if isinstance(raw, (list, tuple, set)):
        seen = {str(item).strip().lower() for item in raw}
        for name in ("admin", "analyst", "service"):
            if name in seen:
                return name
    return None


def _join_scopes(raw: Any) -> str | None:
    """Normalise a scopes claim (string with spaces, or list) into the CSV form ApiKey stores."""
    if raw is None:
        return None
    if isinstance(raw, str):
        cleaned = raw.replace(",", " ").split()
        if not cleaned:
            return None
        return ",".join(cleaned)
    if isinstance(raw, (list, tuple, set)):
        cleaned = [str(item).strip() for item in raw if str(item).strip()]
        if not cleaned:
            return None
        return ",".join(cleaned)
    return None


# ----------------------------------------------------------------- bearer entry point


def authenticate_bearer(token: str, *, settings: Settings | None = None) -> BearerPrincipal:
    """Verify a JWT against the configured JWKS and mint a :class:`BearerPrincipal`.

    AC-IDAM-1's shape: signature, issuer, audience, expiry and clock are enforced by
    PyJWT; algorithm allow-listing is enforced here so the ``alg`` field of the header
    cannot widen it (the classic ``none``/HMAC-confusion bypass). Roles map onto the
    existing three-role model, and a ``platform_scope`` claim is honoured only if the
    operator opted into that at the settings level.
    """
    s = settings or get_settings()
    if not s.oidc_enabled:
        raise OidcError("OIDC is not enabled on this deployment.")
    if not (s.oidc_issuer and s.oidc_audience and s.oidc_jwks_url):
        raise OidcError("OIDC is enabled but SV_OIDC_ISSUER / _AUDIENCE / _JWKS_URL are not all set.")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError as exc:
        raise OidcError(f"Malformed bearer token: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise OidcError("Bearer token header carries no kid; refusing without a key to try.")

    alg = header.get("alg", "")
    allowed = {a.strip() for a in s.oidc_allowed_algorithms.split(",") if a.strip()}
    if alg not in allowed:
        raise OidcError(
            f"Bearer token algorithm {alg!r} is not in SV_OIDC_ALLOWED_ALGORITHMS ({', '.join(sorted(allowed))})."
        )

    try:
        jwk = _jwks_lookup(s.oidc_jwks_url, kid, s.oidc_jwks_refresh_seconds)
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"Could not fetch the JWKS from SV_OIDC_JWKS_URL: {exc}") from exc
    if jwk is None:
        raise OidcError(f"Bearer kid {kid!r} is not present in the issuer's JWKS.")

    try:
        claims = jwt.decode(
            token,
            jwk.key,
            algorithms=sorted(allowed),
            audience=s.oidc_audience,
            issuer=s.oidc_issuer,
            leeway=s.oidc_clock_leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.InvalidAudienceError as exc:
        raise OidcError(f"Audience mismatch: {exc}") from exc
    except jwt.InvalidIssuerError as exc:
        raise OidcError(f"Issuer mismatch: {exc}") from exc
    except jwt.ExpiredSignatureError as exc:
        raise OidcError("Bearer token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise OidcError(f"Bearer token rejected: {exc}") from exc

    role = _pick_role(claims, s.oidc_role_claim)
    if role is None:
        raise OidcError(
            f"Bearer token carries no recognised role under claim {s.oidc_role_claim!r}."
        )

    organisation = _claim_by_path(claims, s.oidc_org_claim)
    if not isinstance(organisation, str) or not organisation.strip():
        organisation = s.oidc_default_org
    else:
        organisation = organisation.strip()

    scopes = _join_scopes(_claim_by_path(claims, s.oidc_scopes_claim))
    platform_scope = bool(
        s.oidc_allow_platform_scope_claim and claims.get("platform_scope") is True
    )

    subject = str(claims.get("sub", ""))
    key_id = f"oidc:{subject or 'anonymous'}"

    return BearerPrincipal(
        role=role,
        organisation=organisation,
        key_id=key_id,
        scopes=scopes,
        platform_scope=platform_scope,
        subject=subject,
        claims=claims,
    )
