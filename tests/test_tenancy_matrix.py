"""`AC-IDAM-3` - tenant isolation proven over every operation the API exposes.

`REQ-IDAM-3` says isolation is "proven, not assumed", and the proof it asks for is a table: take an
organisation-B credential, aim it at organisation-A resources on *every* route, and assert `404`. The
status code is the point - `403` would be a plausible-looking answer that leaks the row's existence,
which turns id guessing into a directory of who verified what.

Three things make this more than a list of cases:

1. **The table is checked against the live route surface.** ``operations()`` reads the (method, path)
   pairs out of the OpenAPI schema the app itself publishes, so adding an endpoint without deciding
   its tenancy rule fails here instead of shipping unproven. A row naming a removed endpoint fails too.
2. **Every negative carries its oracle twin.** Each cross-organisation request is replayed against an
   id no one owns, and the two responses must be indistinguishable - same status, same detail text.
3. **The tenant credential is the harshest one in the model.** Tenant-resource cases run as an *admin*
   key bound to one organisation, because that is precisely the credential this suite exists to catch:
   ``role=admin`` used to short-circuit tenancy on the job routes, so a key minted for one newsroom
   could read every other one's jobs, artifacts and the media metadata those responses inline.

What is deliberately not claimed: that a tenant admin has a control plane. ``/api/v1/admin/**`` mints
keys for any organisation, points webhooks at any organisation and reads the whole ledger, so it is
gated on the platform scope and a tenant admin gets ``403`` there. That is the honest v1 shape -
newsrooms get analyst and service keys, and the platform key stays the operator's.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures_gen import natural_photo  # noqa: E402

API = "/api/v1"
ORG_A = "matrix-a"
ORG_B = "matrix-b"
#: a name a magic-string design would have honoured; here it is just another tenant
PLATFORM = "*"
UNKNOWN_JOB = "f" * 32
UNKNOWN_ID = "deadbeefcafe"

# ------------------------------------------------------------------ route surface


def operations() -> set[tuple[str, str]]:
    """Every ``(method, path)`` the app publishes, read from its own OpenAPI schema.

    Built from a fresh ``create_app()`` so no lifespan, database or worker is involved: this is the
    surface the process will serve, not a recording of one run.
    """
    from synthverify.app import create_app

    spec = create_app().openapi()
    ops: set[tuple[str, str]] = set()
    for path, item in spec["paths"].items():
        for method in item:
            if method in ("head", "options"):
                continue
            ops.add((method.upper(), path))
    return ops


# ------------------------------------------------------------------- tenancy table

PUBLIC = "public"  # no credential at all; must leak nothing
STAMPED = "caller-stamped"  # persists a row; only the caller's organisation may appear
EPHEMERAL = "no-row"  # computes a verdict and persists nothing belonging to a tenant
CROSS_ORG_404 = "cross-org-404"  # addresses an org-A tenant resource
COLLECTION = "org-scoped-collection"  # lists; must contain no org-A identifier
PLATFORM_ONLY = "platform-only"  # control plane; a tenant credential is refused before any lookup


@dataclass(frozen=True)
class Rule:
    kind: str
    credential: str  # which org-B credential the case runs as
    note: str


JOBS = f"{API}/jobs"
ADMIN = f"{API}/admin"

MATRIX: dict[tuple[str, str], Rule] = {
    # ---------------------------------------------------------------- public surface
    ("GET", "/healthz"): Rule(PUBLIC, "none", "liveness; version only"),
    ("GET", "/readyz"): Rule(PUBLIC, "none", "readiness; broker and limiter names, no tenant rows"),
    ("GET", "/metrics"): Rule(PUBLIC, "none", "scrape target; no subject, organisation or id labels"),
    ("GET", f"{API}/meta"): Rule(PUBLIC, "none", "limits and flag glossary; no configuration secrets"),
    # -------------------------------------------------------------------- ingest
    ("POST", f"{API}/media/ingest"): Rule(STAMPED, "b_analyst", "the form has no organisation field"),
    ("POST", f"{API}/media/ingest/batch"): Rule(STAMPED, "b_service", "one job per file, same stamping"),
    ("POST", f"{API}/media/analyze"): Rule(EPHEMERAL, "b_analyst", "inline verdict, no job row"),
    # ---------------------------------------------------------------------- jobs
    ("GET", JOBS): Rule(COLLECTION, "b_admin", "filtered by organisation, never by role"),
    ("GET", f"{JOBS}/{{job_id}}"): Rule(CROSS_ORG_404, "b_admin", "the admin bypass used to live here"),
    ("GET", f"{JOBS}/{{job_id}}/artifacts"): Rule(CROSS_ORG_404, "b_admin", "artifact manifest"),
    ("GET", f"{JOBS}/{{job_id}}/artifacts/{{index}}"): Rule(CROSS_ORG_404, "b_admin", "artifact bytes"),
    ("POST", f"{JOBS}/{{job_id}}/reanalyze"): Rule(
        CROSS_ORG_404, "b_admin", "a write: queued forensics on another tenant's media"
    ),
    ("DELETE", f"{JOBS}/{{job_id}}"): Rule(CROSS_ORG_404, "b_admin", "role passes, tenancy does not"),
    # ----------------------------------------------------------------- control plane
    ("POST", f"{ADMIN}/keys"): Rule(PLATFORM_ONLY, "b_admin", "minting into any org is platform work"),
    ("GET", f"{ADMIN}/keys"): Rule(PLATFORM_ONLY, "b_admin", "lists every org's credentials"),
    ("DELETE", f"{ADMIN}/keys/{{key_id}}"): Rule(PLATFORM_ONLY, "b_admin", "revokes any credential"),
    ("POST", f"{ADMIN}/webhooks"): Rule(PLATFORM_ONLY, "b_admin", "can redirect another org's callbacks"),
    ("GET", f"{ADMIN}/webhooks"): Rule(PLATFORM_ONLY, "b_admin", "every org's endpoints and secrets"),
    ("DELETE", f"{ADMIN}/webhooks/{{webhook_id}}"): Rule(PLATFORM_ONLY, "b_admin", "any org's endpoint"),
    ("GET", f"{ADMIN}/webhooks/{{webhook_id}}/deliveries"): Rule(
        PLATFORM_ONLY, "b_admin", "delivery bodies carry verdicts"
    ),
    ("POST", f"{ADMIN}/webhooks/{{webhook_id}}/test"): Rule(
        PLATFORM_ONLY, "b_admin", "makes the server send to a URL"
    ),
    ("GET", f"{ADMIN}/audit"): Rule(PLATFORM_ONLY, "b_admin", "the whole hash chain"),
    ("GET", f"{ADMIN}/audit/verify"): Rule(PLATFORM_ONLY, "b_admin", "chain state across orgs"),
    ("GET", f"{ADMIN}/stats"): Rule(PLATFORM_ONLY, "b_admin", "counts by media type and tier"),
    ("GET", f"{ADMIN}/policy"): Rule(PLATFORM_ONLY, "b_admin", "deployment thresholds"),
    ("PUT", f"{ADMIN}/policy"): Rule(PLATFORM_ONLY, "b_admin", "changes thresholds for every org"),
    ("GET", f"{ADMIN}/policy/profiles"): Rule(PLATFORM_ONLY, "b_admin", "one profile per org"),
    ("POST", f"{ADMIN}/policy/profiles"): Rule(PLATFORM_ONLY, "b_admin", "creates an org profile"),
    ("PUT", f"{ADMIN}/policy/profiles/{{name}}"): Rule(PLATFORM_ONLY, "b_admin", "edits one"),
    ("DELETE", f"{ADMIN}/policy/profiles/{{name}}"): Rule(PLATFORM_ONLY, "b_admin", "removes one"),
    ("GET", f"{ADMIN}/policy/effective"): Rule(
        PLATFORM_ONLY, "b_admin", "resolves any named org's policy; org is a query parameter"
    ),
    ("GET", f"{ADMIN}/detectors"): Rule(PLATFORM_ONLY, "b_admin", "detector registry"),
    ("GET", f"{ADMIN}/retention/policies"): Rule(
        PLATFORM_ONLY, "b_admin", "which orgs opt out of keeping media forever"
    ),
    ("PUT", f"{ADMIN}/retention/policies/{{organisation}}"): Rule(
        PLATFORM_ONLY, "b_admin", "sets a TTL for any org"
    ),
    ("DELETE", f"{ADMIN}/retention/policies/{{organisation}}"): Rule(
        PLATFORM_ONLY, "b_admin", "changes any org's deletion policy"
    ),
    ("GET", f"{ADMIN}/retention/holds"): Rule(PLATFORM_ONLY, "b_admin", "every org's pins and reasons"),
    ("POST", f"{ADMIN}/retention/holds"): Rule(
        PLATFORM_ONLY, "b_admin", "pins any resource, and pins are audit evidence"
    ),
    ("DELETE", f"{ADMIN}/retention/holds/{{hold_id}}"): Rule(
        PLATFORM_ONLY, "b_admin", "releases another org's pin"
    ),
    ("POST", f"{ADMIN}/retention/sweep"): Rule(
        PLATFORM_ONLY, "b_admin", "deletes media; no tenant credential may reach it at all"
    ),
}


def _kind(kind: str) -> list[tuple[str, str]]:
    return sorted(op for op, rule in MATRIX.items() if rule.kind == kind)


def _label(op: tuple[str, str]) -> str:
    return f"{op[0]} {op[1]}"


def _op_for(label: str) -> tuple[str, str]:
    return next(op for op in MATRIX if _label(op) == label)


# ------------------------------------------------------------------ request shapers

#: a stored job id, webhook id, key id and profile name exist in org A, so each path parameter has
#: an org-A value to substitute and an unowned value for the oracle twin.
_A_VALUES = {
    "{job_id}": "a_job_id",
    "{index}": "0",
    "{key_id}": "a_key_id",
    "{webhook_id}": "a_webhook_id",
    "{name}": "a_profile",
    "{organisation}": "a_org",
    "{hold_id}": "a_hold_id",
}
_GHOST_VALUES = {
    "{job_id}": UNKNOWN_JOB,
    "{index}": "0",
    "{key_id}": UNKNOWN_ID,
    "{webhook_id}": UNKNOWN_ID,
    "{name}": "nobody-names-this",
    "{organisation}": "org-nobody-registered",
    "{hold_id}": UNKNOWN_JOB,
}


def _shape(path: str, values: dict[str, str], t: dict) -> str:
    """Substitute real org-A values for a path's parameters, or say which one was missing.

    An unsubstituted ``{job_id}`` would turn every case below into a request for a row that does not
    exist, which passes for the wrong reason - so the leftover placeholder is an assertion, not a
    typo to be discovered later.
    """
    out = path
    for token, source in values.items():
        out = out.replace(token, str(t[source]) if source in t else source)
    assert "{" not in out, f"{path} was not fully substituted: {out}"
    return out


def _url(op: tuple[str, str], t: dict) -> str:
    return _shape(op[1], _A_VALUES, t)


def _ghost_url(op: tuple[str, str]) -> str:
    path = op[1]
    for token, value in _GHOST_VALUES.items():
        path = path.replace(token, value)
    return path


# ------------------------------------------------------------------------ fixtures


def _headers(secret: str) -> dict[str, str]:
    return {"X-API-Key": secret}


async def _mint(client, *, name: str, role: str, organisation: str, platform_scope: bool = False) -> dict:
    resp = await client.post(
        f"{API}/admin/keys",
        json={
            "name": name,
            "role": role,
            "organisation": organisation,
            "platform_scope": platform_scope,
        },
    )
    assert resp.status_code == 201, resp.text
    record = resp.json()["record"]
    record["key"] = resp.json()["key"]
    return record


async def _wait_for_job(client, job_id: str, secret: str, timeout: float = 30.0) -> dict:
    """Poll *as the owning credential*, so a tenancy filter cannot make this hang."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await client.get(f"{API}/jobs/{job_id}", headers=_headers(secret))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in ("completed", "failed"):
            return body
        await asyncio.sleep(0.1)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s")


async def _count_jobs(client) -> int:
    by_status = (await client.get(f"{API}/admin/stats")).json()["jobs_by_status"]
    return sum(by_status.values())


@pytest.fixture()
async def tenants(client):
    """Two organisations with a completed job each, plus four org-B credentials.

    Seeded through the public API rather than by inserting rows, because a seed that bypassed the
    routes would not exercise the code that stamps ``organisation`` on the way in - which is the
    half of isolation that has no filter to hide behind.
    """
    a_key = await _mint(client, name="a-analyst", role="analyst", organisation=ORG_A)
    b_analyst = await _mint(client, name="b-analyst", role="analyst", organisation=ORG_B)
    b_service = await _mint(client, name="b-service", role="service", organisation=ORG_B)
    b_admin = await _mint(client, name="b-admin", role="admin", organisation=ORG_B)
    payload = natural_photo()

    a_job = (
        await client.post(
            f"{API}/media/ingest",
            headers=_headers(a_key["key"]),
            files={"file": ("org-a-evidence.jpg", payload)},
        )
    ).json()["job_id"]
    b_job = (
        await client.post(
            f"{API}/media/ingest",
            headers=_headers(b_analyst["key"]),
            files={"file": ("org-b-evidence.jpg", payload)},
        )
    ).json()["job_id"]
    a = await _wait_for_job(client, a_job, a_key["key"])
    b = await _wait_for_job(client, b_job, b_analyst["key"])

    webhook = await client.post(
        f"{API}/admin/webhooks",
        json={"url": "http://127.0.0.1:9/sink", "organisation": ORG_A, "description": "org-a"},
    )
    assert webhook.status_code == 201, webhook.text
    profile = await client.post(
        f"{API}/admin/policy/profiles",
        json={
            "name": "org-a-strict",
            "organisation": ORG_A,
            "thresholds": {
                "block_score": 0.9,
                "escalate_score": 0.7,
                "review_score": 0.4,
                "low_confidence": 0.3,
                "min_coverage": 0.5,
            },
        },
    )
    assert profile.status_code == 201, profile.text

    # A retention policy and a legal hold, both naming org A. Neither is swept here (the TTL is a
    # decade, and no case reaches the sweep with a tenant credential), but both have to be real rows
    # so the retention routes below are probed at an address that exists rather than at a placeholder.
    policy = await client.put(
        f"{API}/admin/retention/policies/{ORG_A}",
        json={"media_ttl_days": 3650, "note": "matrix seed"},
    )
    assert policy.status_code == 200, policy.text
    hold = await client.post(
        f"{API}/admin/retention/holds",
        json={
            "resource_kind": "media",
            "resource_ref": a["media"]["sha256"],
            "reason": "matrix seed hold",
            "organisation": ORG_A,
        },
    )
    assert hold.status_code == 201, hold.text

    return {
        "client": client,
        "a_key_id": a_key["key_id"],
        "a_secret": a_key["key"],
        "b_analyst": b_analyst["key"],
        "b_service": b_service["key"],
        "b_admin": b_admin["key"],
        "a_job_id": a["job_id"],
        "a_job": a,
        "b_job_id": b["job_id"],
        "b_job": b,
        "a_media_id": a["media"]["id"],
        "a_webhook_id": str(webhook.json()["id"]),
        "a_profile": "org-a-strict",
        "a_org": ORG_A,
        "a_hold_id": hold.json()["id"],
        "payload": payload,
    }


# ------------------------------------------------------------------ the surface gate


def test_table_covers_every_exposed_operation():
    """An endpoint with no decided tenancy rule is a finding, not an oversight to trust."""
    exposed, covered = operations(), set(MATRIX)
    assert covered == exposed, f"undecided or stale: {sorted(covered ^ exposed)}"


def _template(detail, id_value: str):
    """A route echoing the id its caller supplied is not a leak; the shape around it can be."""
    return detail.replace(id_value, "<id>") if isinstance(detail, str) else detail


@pytest.mark.parametrize("label", [_label(o) for o in _kind(CROSS_ORG_404)])
async def test_cross_org_resource_is_404_not_403(tenants, label):
    """`AC-IDAM-3`: org-B at an org-A resource answers exactly like a row that does not exist."""
    op = _op_for(label)
    secret = tenants[MATRIX[op].credential]
    client = tenants["client"]

    real = await client.request(op[0], _url(op, tenants), headers=_headers(secret))
    ghost = await client.request(op[0], _ghost_url(op), headers=_headers(secret))

    assert real.status_code == 404, f"{label} -> {real.status_code}: {real.text[:200]}"
    assert ghost.status_code == 404, f"unknown id on {label} -> {ghost.status_code}"
    assert _template(real.json()["detail"], tenants["a_job_id"]) == _template(
        ghost.json()["detail"], UNKNOWN_JOB
    ), "the two answers differ: existence leaks"
    # the app's error envelope is `{error, detail}`; anything more is a partial row being returned
    assert set(real.json()) == {"error", "detail"}, real.text[:200]


@pytest.mark.parametrize("label", [_label(o) for o in _kind(PLATFORM_ONLY)])
async def test_control_plane_refuses_a_tenant_credential(tenants, label):
    """A tenant admin gets the same refusal for a real row as for a fabricated one."""
    op = _op_for(label)
    secret = tenants[MATRIX[op].credential]
    client = tenants["client"]

    real = await client.request(op[0], _url(op, tenants), headers=_headers(secret))
    ghost = await client.request(op[0], _ghost_url(op), headers=_headers(secret))

    assert real.status_code == 403, f"{label} -> {real.status_code}: {real.text[:200]}"
    assert ghost.status_code == 403
    assert tenants["a_job_id"] not in real.text


async def test_collection_route_holds_no_other_org(tenants):
    label = _label(("GET", JOBS))
    client, secret = tenants["client"], tenants["b_admin"]

    resp = await client.get(JOBS, params={"limit": 200}, headers=_headers(secret))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1, f"org-B's queue held {body['total']} jobs"
    assert body["items"][0]["job_id"] == tenants["b_job_id"]
    for marker in (tenants["a_job_id"], tenants["a_media_id"], ORG_A):
        assert marker not in resp.text, f"{marker} readable on {label}"


@pytest.mark.parametrize("label", [_label(o) for o in _kind(STAMPED)])
async def test_ingest_stamps_only_the_callers_organisation(tenants, label):
    """Whatever the response says, the row that lands carries the caller's org and nothing else."""
    op = _op_for(label)
    client, secret = tenants["client"], tenants[MATRIX[op].credential]
    files = (
        [("files", ("x.jpg", tenants["payload"])), ("files", ("y.jpg", tenants["payload"]))]
        if "batch" in op[1]
        else {"file": ("x.jpg", tenants["payload"])}
    )
    before = await _count_jobs(client)
    resp = await client.request(op[0], op[1], headers=_headers(secret), files=files)
    assert resp.status_code in (200, 202), resp.text
    payload = resp.json()
    ids = (
        [payload["job_id"]]
        if "job_id" in payload
        else [i["job_id"] for i in payload["items"] if "job_id" in i]
    )
    assert ids, payload
    assert await _count_jobs(client) == before + len(ids)

    seen = await client.get(JOBS, params={"limit": 200}, headers=_headers(client.headers["X-API-Key"]))
    owned = {i["job_id"]: i["organisation"] for i in seen.json()["items"] if i["job_id"] in ids}
    assert set(owned) == set(ids), f"{label} created rows the platform listing cannot see"
    assert set(owned.values()) == {ORG_B}, f"{label} wrote outside the caller's org: {owned}"


async def test_analyze_persists_no_tenant_row(tenants):
    op = ("POST", f"{API}/media/analyze")
    client, secret = tenants["client"], tenants["b_analyst"]
    before = await _count_jobs(client)
    resp = await client.post(op[1], headers=_headers(secret), files={"file": ("x.jpg", tenants["payload"])})
    assert resp.status_code == 200, resp.text
    assert resp.json()["media"]["filename"] == "x.jpg"
    assert await _count_jobs(client) == before, "/analyze started writing job rows"


async def test_idempotency_key_is_not_a_cross_org_handle(tenants):
    """Reusing org-A's idempotency key must not hand back org-A's job."""
    client = tenants["client"]
    key = f"shared-{uuid.uuid4().hex[:8]}"
    first = await client.post(
        f"{API}/media/ingest",
        headers=_headers(tenants["a_secret"]),
        files={"file": ("a.jpg", tenants["payload"])},
        data={"idempotency_key": key},
    )
    second = await client.post(
        f"{API}/media/ingest",
        headers=_headers(tenants["b_analyst"]),
        files={"file": ("b.jpg", tenants["payload"])},
        data={"idempotency_key": key},
    )
    assert first.json()["job_id"] != second.json()["job_id"]
    assert second.json().get("deduplicated") is not True


async def test_content_dedup_does_not_share_rows_across_orgs(tenants):
    """Identical bytes in two organisations are two asset rows.

    Keying dedup on ``sha256`` alone made the second tenant's job inline the *first* tenant's media
    object - ``Job.to_dict`` embeds ``media.to_dict()``, so org-B got org-A's asset id, filename and
    upload time. The bytes are still stored once, because the object store keys on the digest.
    """
    assert tenants["a_job"]["media"]["filename"] == "org-a-evidence.jpg"
    b = tenants["b_job"]
    assert b["media"]["id"] != tenants["a_media_id"], "org-B's job points at org-A's asset row"
    assert b["media"]["filename"] == "org-b-evidence.jpg", b["media"]

    from sqlalchemy import func, select

    from synthverify.db import MediaAsset

    session = tenants["client"]._transport.app.state.db.session()
    try:
        rows = session.execute(
            select(MediaAsset.organisation, func.count()).group_by(MediaAsset.organisation)
        ).all()
    finally:
        session.close()
    assert dict(rows) == {ORG_A: 1, ORG_B: 1}, rows


# ------------------------------------------------------------------- public surface


@pytest.mark.parametrize("label", [_label(o) for o in _kind(PUBLIC)])
async def test_public_endpoints_carry_no_tenant_data(tenants, label):
    op = _op_for(label)
    resp = await tenants["client"].request(op[0], op[1], headers={"X-API-Key": ""})
    assert resp.status_code == 200, f"{label} -> {resp.status_code} {resp.text[:200]}"
    for marker in (
        tenants["a_job_id"],
        tenants["b_job_id"],
        tenants["a_media_id"],
        tenants["a_key_id"],
        ORG_A,
        ORG_B,
        "org-a-evidence",
    ):
        assert marker not in resp.text, f"{marker} readable on {label} with no credential"
    assert "subject=" not in resp.text, f"{label} published a per-subject metric label"


async def test_dashboard_shell_carries_no_credential_and_no_tenant_data(tenants):
    """The operator console is a static mount, so OpenAPI - and therefore the table above - cannot see it.

    What it must still never do is ship a key of its own. Every call the shell makes is authenticated
    by whatever the operator pasted into it, which is the only reason a tenant's console shows a
    tenant's queue; a baked-in secret would put the platform credential in every visitor's page.
    """
    from conftest import ADMIN_SECRET

    resp = await tenants["client"].get("/dashboard/")
    assert resp.status_code == 200, resp.text
    for marker in (ADMIN_SECRET, tenants["a_job_id"], tenants["a_key_id"], ORG_A, ORG_B):
        assert marker not in resp.text, f"{marker} shipped inside the console shell"
    # the `sv_live_…` in the input's placeholder is a hint, not a key; a real one is 32 hex after it
    assert re.search(r"sv_live_[0-9a-f]{32}", resp.text) is None, "a live key shipped in the shell"
    # ... and it does authenticate, from the operator's own session rather than from nowhere
    assert "X-API-Key" in resp.text, "the shell stopped sending the pasted key"


async def test_rate_limiter_exposes_no_identifiers_and_no_unbounded_label(app_env, monkeypatch):
    """A 429 must not publish who was limited, nor let a caller mint label values.

    The counter used to carry the first twelve characters of the limiter's subject: a key id when
    authenticated, a **client IP** when not. ``/metrics`` is unauthenticated, so that was a
    tenant-identifier disclosure *and* an externally growable series count.
    """
    from conftest import ADMIN_SECRET
    from httpx import ASGITransport, AsyncClient

    from synthverify.app import app
    from synthverify.config import get_settings

    monkeypatch.setenv("SV_RATE_LIMIT_RPM", "60")
    monkeypatch.setenv("SV_RATE_LIMIT_BURST", "2")
    get_settings.cache_clear()

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            minted = await c.post(
                f"{API}/admin/keys",
                headers=_headers(ADMIN_SECRET),
                json={"name": "throttled", "role": "analyst", "organisation": ORG_A},
            )
            assert minted.status_code == 201, minted.text
            key = minted.json()["key"]
            codes = [(await c.get(JOBS, headers=_headers(key))).status_code for _ in range(6)]
            assert 429 in codes, codes
            scrape = (await c.get("/metrics")).text
    get_settings.cache_clear()

    assert "synthverify_rate_limited_total" in scrape
    assert 'authenticated="true"' in scrape, scrape[:400]
    for marker in ("subject=", ORG_A, "127.0.0.1"):
        assert marker not in scrape


# -------------------------------------------------------- the platform scope itself


async def test_tenant_admin_reaches_no_platform_surface(tenants):
    client, secret = tenants["client"], tenants["b_admin"]
    for path in (f"{ADMIN}/audit", f"{ADMIN}/stats", f"{ADMIN}/keys", f"{ADMIN}/detectors"):
        resp = await client.get(path, headers=_headers(secret))
        assert resp.status_code == 403, f"{path} -> {resp.status_code}"
        assert "platform control plane" in resp.json()["detail"]
    # ... and the bootstrap key still does, so this is a scope test not a broken route.
    assert (await client.get(f"{ADMIN}/keys")).status_code == 200


async def test_platform_scope_needs_the_admin_role(tenants):
    """An unattended credential must never be the one that reads every tenant.

    A ``service`` key is what a newsroom's CMS holds, so a scope that lets it sweep the whole
    deployment would leak through the weakest secret in the system.
    """
    resp = await tenants["client"].post(
        f"{ADMIN}/keys", json={"name": "sneaky", "role": "service", "platform_scope": True}
    )
    assert resp.status_code == 422, resp.text
    assert "platform_scope" in resp.text


async def test_an_organisation_name_grants_nothing(tenants):
    """Cross-organisation reach is a flag, never something a tenant can be *called*.

    The alternative - reserving one organisation value as the platform scope - is what this test
    rules out: an org whose name happened to equal it would own the deployment, and the name is set
    by whoever registers the tenant.
    """
    client = tenants["client"]
    record = await _mint(client, name="called-star", role="admin", organisation=PLATFORM)
    assert record["platform_scope"] is False
    # an ordinary tenant by any other name: 404 at org A's job, 403 at the control plane
    resp = await client.get(f"{API}/jobs/{tenants['a_job_id']}", headers=_headers(record["key"]))
    assert resp.status_code == 404, resp.status_code
    assert (await client.get(f"{ADMIN}/keys", headers=_headers(record["key"]))).status_code == 403
    # ... and registering a webhook for it is legal, because the label carries no privilege
    webhook = await client.post(
        f"{ADMIN}/webhooks", json={"url": "http://127.0.0.1:9/sink", "organisation": PLATFORM}
    )
    assert webhook.status_code == 201, webhook.text


async def test_a_minted_platform_key_reads_across_orgs(tenants):
    """The scope is what grants reach, so a second one minted over HTTP must behave identically."""
    client = tenants["client"]
    record = await _mint(
        client, name="second-platform-admin", role="admin", organisation=ORG_A, platform_scope=True
    )
    assert record["platform_scope"] is True
    both = await client.get(JOBS, params={"limit": 200}, headers=_headers(record["key"]))
    ids = {i["job_id"] for i in both.json()["items"]}
    assert {tenants["a_job_id"], tenants["b_job_id"]} <= ids, ids
    assert (await client.get(f"{ADMIN}/keys", headers=_headers(record["key"]))).status_code == 200
