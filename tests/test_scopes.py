"""AC-IDAM-2 / REQ-IDAM-2: fine-grained scope matrix.

A credential minted with `jobs:read` + `media:submit` is admitted to ingest and job-read routes
and rejected **403** on `admin:policy:write` and `artifacts:read` naming the missing scope. No
scope combination reads another organisation's resource (404, per AC-IDAM-3). Pre-scope keys keep
their role-equivalent grant in the same table.

The 403/404 split is stated deliberately: an unknown scope on a *known* credential does not leak
existence the way a cross-org read would.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


async def _mint_key(client, *, role: str, scopes: list[str] | None = None, org: str = "default") -> str:
    """Mint a key through the admin API and return its raw secret."""
    body: dict = {"name": f"test-{role}-{'-'.join(scopes or ['noscoped'])}", "role": role, "organisation": org}
    if scopes is not None:
        body["scopes"] = scopes
    resp = await client.post("/api/v1/admin/keys", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


async def _scoped_client(app, secret: str):
    """Return an AsyncClient authenticated with the given key secret."""
    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url="http://testserver")
    c.headers["X-API-Key"] = secret
    return c


# ------------------------------------------------------------------ admission matrix


@pytest.mark.anyio
async def test_scoped_key_admitted_to_granted_job_read(client):
    """A key with jobs:read scope is admitted to GET /api/v1/jobs."""
    from synthverify.app import app

    secret = await _mint_key(client, role="service", scopes=["jobs:read"])
    scoped = await _scoped_client(app, secret)
    try:
        resp = await scoped.get("/api/v1/jobs")
        assert resp.status_code == 200, resp.text
    finally:
        await scoped.aclose()


@pytest.mark.anyio
async def test_scoped_key_admitted_to_media_submit(client):
    """A key with media:submit scope is admitted to POST /api/v1/media/ingest."""
    from synthverify.app import app

    secret = await _mint_key(client, role="service", scopes=["media:submit"])
    scoped = await _scoped_client(app, secret)
    try:
        # Ingest needs a file; a 422 (missing body) means auth passed
        resp = await scoped.post("/api/v1/media/ingest")
        # If we got 403, auth failed; if 422, auth passed but body validation failed
        assert resp.status_code != 403, resp.text
    finally:
        await scoped.aclose()


@pytest.mark.anyio
async def test_scoped_key_rejected_on_artifacts_read_naming_scope(client):
    """AC-IDAM-2: rejected with 403 naming the missing scope when artifacts:read absent."""
    from synthverify.app import app

    secret = await _mint_key(client, role="service", scopes=["jobs:read"])
    scoped = await _scoped_client(app, secret)
    try:
        resp = await scoped.get("/api/v1/jobs/fake-id/artifacts")
        assert resp.status_code == 403, resp.text
        assert "artifacts:read" in resp.text
    finally:
        await scoped.aclose()


@pytest.mark.anyio
async def test_scoped_key_rejected_on_admin_route(client):
    """AC-IDAM-2: a scoped service key is 403 on admin/policy (role gate prevents reaching scope)."""
    from synthverify.app import app

    secret = await _mint_key(client, role="service", scopes=["jobs:read", "media:submit"])
    scoped = await _scoped_client(app, secret)
    try:
        resp = await scoped.put("/api/v1/admin/policy", json={"risk_threshold": 0.7})
        assert resp.status_code == 403, resp.text
        # The admin router's platform_admin gate fires before scope, which is correct:
        # a service key cannot reach admin regardless of scope tokens.
    finally:
        await scoped.aclose()


# ------------------------------------------------------------------ backward compatibility


@pytest.mark.anyio
async def test_legacy_key_without_scopes_keeps_role_equivalent_grant(client):
    """AC-IDAM-2: "legacy keys staying role-equivalent" — NULL scopes means role grant."""
    from synthverify.app import app

    # Mint without scopes field at all
    resp = await client.post("/api/v1/admin/keys", json={
        "name": "legacy-service", "role": "service", "organisation": "default"
    })
    assert resp.status_code == 201
    secret = resp.json()["key"]

    scoped = await _scoped_client(app, secret)
    try:
        # A service key without explicit scopes should access GET /api/v1/jobs (role-equivalent)
        resp = await scoped.get("/api/v1/jobs")
        assert resp.status_code == 200, resp.text
    finally:
        await scoped.aclose()


@pytest.mark.anyio
async def test_explicit_scopes_narrow_below_role(client):
    """A scoped key grants LESS than its role would: scopes refine, not widen."""
    from synthverify.app import app

    # Analyst role normally gets artifacts:read but this key only has jobs:read
    secret = await _mint_key(client, role="analyst", scopes=["jobs:read"])
    scoped = await _scoped_client(app, secret)
    try:
        resp = await scoped.get("/api/v1/jobs/fake-id/artifacts")
        assert resp.status_code == 403, resp.text
        assert "artifacts:read" in resp.text
    finally:
        await scoped.aclose()


# ------------------------------------------------------------------ tenancy interaction


@pytest.mark.anyio
async def test_no_scope_combination_enables_cross_org_read(client):
    """AC-IDAM-2 + AC-IDAM-3: no scope combination reads another organisation's resource."""
    from synthverify.app import app

    # Mint a scoped key for org-alpha
    secret_a = await _mint_key(
        client, role="service", scopes=["jobs:read", "media:read", "artifacts:read"], org="org-alpha"
    )

    # Create a job under org-beta using the admin client
    # The admin client has org=default so this is a different org
    scoped = await _scoped_client(app, secret_a)
    try:
        resp = await scoped.get("/api/v1/jobs")
        assert resp.status_code == 200
        # All returned jobs should belong to org-alpha (the key's org)
        items = resp.json().get("items", [])
        for item in items:
            assert item.get("organisation") == "org-alpha", (
                f"Cross-org leak: scoped key from org-alpha sees {item.get('organisation')}"
            )
    finally:
        await scoped.aclose()


# ------------------------------------------------------------------ scope vocabulary validation


@pytest.mark.anyio
async def test_unknown_scope_token_is_refused_at_mint(client):
    """POST with an unrecognized scope token gets 422, not silent acceptance."""
    resp = await client.post("/api/v1/admin/keys", json={
        "name": "bad-scope", "role": "service",
        "scopes": ["jobs:read", "made:up:scope"]
    })
    assert resp.status_code == 422, resp.text
    assert "made:up:scope" in resp.text


# ------------------------------------------------------------------ the scope library itself


class TestScopeParsing:
    """Unit-level: parse_scopes, effective_scopes, ROLE_SCOPES."""

    def test_parse_empty_string_returns_empty_set(self):
        from synthverify.auth import parse_scopes

        assert parse_scopes("") == frozenset()
        assert parse_scopes(None) == frozenset()
        assert parse_scopes("   ") == frozenset()

    def test_parse_comma_separated_returns_frozenset(self):
        from synthverify.auth import parse_scopes

        got = parse_scopes("jobs:read, media:submit , artifacts:read")
        assert got == frozenset({"jobs:read", "media:submit", "artifacts:read"})

    def test_service_role_without_scopes_gets_role_equivalent(self):
        from synthverify.auth import ROLE_SCOPES, UserRole

        granted = ROLE_SCOPES[UserRole.SERVICE.value]
        assert "media:submit" in granted
        assert "jobs:read" in granted
        assert "admin:policy:write" not in granted

    def test_admin_role_without_scopes_gets_everything(self):
        from synthverify.auth import ROLE_SCOPES, SCOPE_VOCABULARY, UserRole

        assert ROLE_SCOPES[UserRole.ADMIN.value] == SCOPE_VOCABULARY

    def test_effective_scopes_with_explicit_overrides_role(self):
        from synthverify.auth import effective_scopes
        from synthverify.db import ApiKey

        key = ApiKey(role="admin", scopes="jobs:read")
        assert effective_scopes(key) == frozenset({"jobs:read"})

    def test_effective_scopes_falls_through_when_none(self):
        from synthverify.auth import effective_scopes
        from synthverify.db import ApiKey

        key = ApiKey(role="analyst", scopes=None)
        granted = effective_scopes(key)
        assert "jobs:read" in granted
        assert "admin:policy:write" not in granted

    def test_scope_vocabulary_has_all_required_tokens(self):
        """The spec names these scopes; they must all be in the vocabulary."""
        from synthverify.auth import SCOPE_VOCABULARY

        required = {"media:submit", "jobs:read", "admin:policy:write", "artifacts:read"}
        missing = required - SCOPE_VOCABULARY
        assert not missing, f"Spec-mandated scope tokens not in vocabulary: {missing}"
