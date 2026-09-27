# API Reference

Base URL: `http://localhost:8080` · Interactive docs: `/docs` (OpenAPI 3.1)

**Authentication** — all `/api/v1/**` endpoints require an API key:

```
X-API-Key: sv_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   or
Authorization: Bearer sv_live_…
```

Roles: `admin` > `analyst` > `service`. Errors use machine-readable `error`
codes (`unsupported_or_invalid_media`, `unknown_detector`, `validation_error`,
`http_error`) with HTTP semantics: `401` bad key, `403` wrong role or a tenant
key at the control plane, `404` not found **or** cross-tenant, `413` too large,
`422` invalid media or parameters, `429` rate-limited (`Retry-After` header).

**Tenancy** — a key's `organisation` decides whose data it may touch; its role decides which
endpoints it may call. The two are enforced separately and deliberately: a request for a job,
artifact or media row owned by another organisation answers `404` with the *same body* an unknown id
answers with, so no endpoint in this API is an oracle for whether an id exists elsewhere.
`GET /api/v1/jobs` filters by the caller's organisation, and ingest stamps the caller's organisation
on the asset and the job it creates - there is no field in the request that can choose otherwise.
Reach across organisations belongs to one property, `platform_scope`, which a key can only be given
by an existing platform admin alongside `role: admin`. It is not derivable from a role, and an
organisation *named* anything at all - including `*` - is just an organisation.

**Control plane** — every `/api/v1/admin/**` endpoint needs role `admin` **and** `platform_scope`.
There is no per-organisation administration surface in v1: newsrooms are served by `analyst` and
`service` keys, and webhooks, policy profiles and keys are registered by the operator.

**Tracing** — every response carries `X-Trace-Id`, `X-Request-ID`, `X-Process-Time-Ms` and a
`traceparent`. Send a spec-valid W3C `traceparent` (`00-<32 hex>-<16 hex>-01`) to join an existing trace;
one that fails validation — wrong length, uppercase hex, all-zero ids, the reserved `ff` version, more than
560 bytes — is discarded and a fresh id is minted, so a caller can never put a value into this system's
metrics labels, log lines or audit rows. The id you send back is the **hop's** span, not yours. The same id
appears in that request's `/metrics` exemplars, in its job's `trace_id`, in every audit row it writes, and
in the log line of whichever worker runs its job.

---

## Media

### `POST /api/v1/media/analyze` — synchronous verification

Multipart form:

| Field | Type | Notes |
|---|---|---|
| `file` | file | required; ≤ `SV_MAX_INLINE_BYTES` (default 8 MiB) |
| `requested_detectors` | JSON string | optional, e.g. `'["ela","metadata"]'` |

**200** — full XAI report:

```json
{
  "schema": "synthverify.report/v1",
  "verdict": {
    "risk_score": 0.900, "risk_tier": "CRITICAL",
    "confidence": 0.60, "detector_coverage": 0.8, "conclusive": true,
    "recommended_action": "BLOCK",
    "action_rationale": "A high-confidence detector self-reports…",
    "summary": "'render.png' (image) shows near-certain synthetic origin…"
  },
  "narrative": ["…", "…"],
  "detectors": [
    {"detector": "ela", "status": "ran", "score": 0.16, "confidence": 0.45,
     "findings": ["…"], "flags": [], "evidence": {"mean_ela": 0.84, "…": "…"},
     "runtime_ms": 21.4}
  ],
  "flags": ["AI_GENERATION_TAG", "STRUCTURED_RESIDUAL"],
  "flag_explanations": {"AI_GENERATION_TAG": "…"},
  "top_evidence": ["[metadata · contribution 0.72] PNG text chunks…", "…"],
  "policy": {"block_score": 0.85, "review_score": 0.5, "…": "…"},
  "policy_name": "org-a-strict",
  "artifacts": [{"detector": "ela", "name": "ela_heatmap.png", "path": "…"}],
  "media": {"filename": "render.png", "media_type": "image", "sha256": "…", "size_bytes": 15939},
  "pipeline": {"duration_ms": 32.1}
}
```

**422** — undecodable/unknown media, or unknown detector names.

### `POST /api/v1/media/ingest` — asynchronous verification (202)

| Field | Type | Notes |
|---|---|---|
| `file` | file | required; ≤ `SV_MAX_UPLOAD_BYTES` (default 64 MiB) |
| `priority` | int 1–9 | lower = sooner (default 5) |
| `requested_detectors` | JSON string | optional subset |
| `idempotency_key` | string | safe retries; same key+org returns the original job |
| `callback_url` | URL | per-job webhook override (reserved) |

**202** → `{"job_id": "…", "status": "queued", "links": {"self": "/api/v1/jobs/…"}}`

### `POST /api/v1/media/ingest/batch` — up to 20 files (202)

Returns per-file items; failures are itemized, successes carry `job_id`.

---

## Jobs

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/jobs/{job_id}?include_report=true` | full job + XAI report once completed |
| `GET /api/v1/jobs?status=&risk_tier=&media_type=&limit=&offset=` | paginated queue views, scoped to the caller's organisation (`total` counts only what the key may read) |
| `GET /api/v1/jobs/{job_id}/artifacts` | forensic artifacts recorded by the job: `{index, detector, name, url}` |
| `GET /api/v1/jobs/{job_id}/artifacts/{index}` | authenticated image bytes (e.g. the ELA heatmap PNG) for the viewer |
| `POST /api/v1/jobs/{id}/reanalyze?requested_detectors=["ela"]` | new job on the same media |
| `DELETE /api/v1/jobs/{id}` *(admin)* | cancel a queued job |

**Artifact access.** Artifact files are stored under `SV_ARTIFACTS_DIR` and are never publicly
reachable — both endpoints authenticate and are tenant-scoped: a key whose organisation does not own
the job reads `404`, whatever its role. The serving path is *re-derived* from the job's own media
digest rather than trusted from the stored `path` string: the filename must be a bare name carrying
this job's `sha256` prefix, and it must exist inside the artifacts directory. A tampered result blob
therefore cannot read arbitrary disk.

Five of the six rows above address one existing job, so all five go through the same lookup
(`_get_job_or_404`) - one enforcement point rather than a check copy-pasted per handler, which is how
`reanalyze` ended up with none.

Job states: `queued → running → completed | failed`. Completed bodies include
`risk_score`, `risk_tier`, `confidence`, `detector_coverage` and the full
`result` report.

**List rows are a different shape from detail rows, deliberately.** `GET /api/v1/jobs` omits
`result` — a queue page that re-serialised five reports per screen is the reason the serializer has
an `include_result` flag at all. The four verdict scalars above still appear because they are real
columns the filters run on, and `recommended_action` appears too even though nothing filters on it:
the queue table renders an Action column, so the one field a *row* has to show is read out of the
stored report by the serializer rather than left behind with the rest of it. A row for a job that has
not finished carries the key as `null`, so the shape never varies by status.

---

## Webhooks

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/admin/webhooks` *(admin)* | register `{url, events, description, organisation}` → signing secret shown **once** |
| `GET /api/v1/admin/webhooks` | list |
| `DELETE /api/v1/admin/webhooks/{id}` | disable |
| `POST /api/v1/admin/webhooks/{id}/test` | signed `test.ping` delivery |
| `GET /api/v1/admin/webhooks/{id}/deliveries` | delivery ledger (attempts, status, HTTP code, errors) |

Events: `job.completed`, `job.failed`, `*` (all).

**Delivery contract**

```
POST <your url>
X-SynthVerify-Event: job.completed
X-SynthVerify-Delivery: <delivery id>
X-SynthVerify-Signature: t=<unix>, v1=<hex hmac_sha256(secret, "<unix>.<body>")>
Content-Type: application/json

{"event": "job.completed", "api_version": "v1",
 "data": { …full job dict incl. result… }}
```

Respond `2xx` to acknowledge. Failures retry with exponential backoff
(2ⁿ seconds, max attempts configurable) and are visible in the ledger.

**Receiver-side verification (Python)**

```python
import hmac, hashlib
def verify(secret: str, sig_header: str, body: bytes) -> bool:
    t_part, v1_part = sig_header.split(", ")
    ts = t_part.split("=")[1]
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v1_part.split("=")[1], expected)
```

---

## Administration *(platform admin: role `admin` **and** `platform_scope`)*

Every row below is one router, so one dependency refuses a tenant's admin key with `403` before any
lookup happens - and the refusal is the same for a real id and a fabricated one, which is what keeps
the control plane from becoming an oracle either.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/admin/keys` | create key `{name, role, organisation, platform_scope?, rate_limit_rpm?}` → secret shown once; `platform_scope: true` is accepted only with `role: admin` |
| `GET /api/v1/admin/keys` / `DELETE /api/v1/admin/keys/{key_id}` | list (each record states its `organisation` and `platform_scope`) / revoke |
| `GET /api/v1/admin/stats` | jobs by status/tier/action, avg latency, queue depth, webhook health, uptime |
| `GET /api/v1/admin/policy` / `PUT /api/v1/admin/policy` | global routing thresholds (`review ≤ escalate ≤ block` enforced) |
| `GET /api/v1/admin/policy/profiles` | list all PolicyProfiles (org-scoped + global) |
| `POST /api/v1/admin/policy/profiles` | create `{name, organisation, description?, thresholds}` (409 on duplicate name) |
| `PUT /api/v1/admin/policy/profiles/{name}` / `DELETE …` | update thresholds (re-activates) / deactivate (audit-safe soft delete) |
| `GET /api/v1/admin/policy/effective?organisation=X` | which profile X's jobs resolve to: `{organisation, source, thresholds}` |
| `GET /api/v1/admin/audit?limit=&action=` | audit trail (newest first) |
| `GET /api/v1/admin/audit/verify` | hash-chain integrity: the full `VerifyReport` — `{verified, entries_checked, checkpoints_checked, break_at_seq, break_reason, head_hash, sealed_through, elapsed_ms, events_per_second}` (`null` for the three break fields when `verified` is true) |
| `GET /api/v1/admin/detectors` | registry: names, media types, weights, descriptions |
| `GET /api/v1/admin/retention/policies` | which organisations have opted into deletion: `{items, default_days}` — `default_days` is reported because this list reading "no row for org-x" means "keep forever" **only** when `SV_RETENTION_DEFAULT_DAYS` is unset |
| `PUT /api/v1/admin/retention/policies/{organisation}` | set `{media_ttl_days (1…36 500), note?}`; creates or updates, and `retention_policy.created`/`.updated` is ledgered either way |
| `DELETE /api/v1/admin/retention/policies/{organisation}` | remove the row (`404` if absent) and return `{…, falls_back_to}` — the TTL the organisation now inherits, which is `null` only if nothing deletes its media. Deleting a policy is not the same act as protecting data, so the response says which is which |
| `GET /api/v1/admin/retention/holds?active_only=&limit=` | the pins, newest first; `active_only=false` also returns released ones, because a hold is the record that a hold *was* in force |
| `POST /api/v1/admin/retention/holds` | `{resource_kind: media\|job, resource_ref, reason, organisation?}` → `201`. `resource_ref` must be exactly a 64-hex `sha256` or a 32-hex job id (`422` otherwise, before any lookup) and **must exist** (`404`): a hold naming a typoed digest would look like protection in the audit trail and provide none. A `media` pin is scoped to the **digest**, so it protects every organisation holding those bytes; a `job` pin freezes one verdict. The append is in the same transaction as the row |
| `DELETE /api/v1/admin/retention/holds/{hold_id}` | release: `active=false` + `released_at`, ledgered as `legal_hold.released`. Soft — hard-deleting the row would delete the evidence that a hold existed. `404` unknown, `409` already released |
| `POST /api/v1/admin/retention/sweep` | run one pass: `{dry_run?=true, organisation?, limit?}` → the full `SweepReport`. `dry_run` defaults to true because this endpoint takes evidence out of a forensic store, and the preview returns **the same plan object** a real pass consumes, so "what would this delete" and "what did this delete" cannot drift |

**Audit verify shape.** The three counts answer three different questions. `entries_checked` is how many
rows were **hashed**, which is how the checkpoint scheme stays auditable rather than trusted: sealing
every `SV_AUDIT_CHECKPOINT_EVERY` (5 000) events does not make the walk cheaper, so a verified ledger
reports its full row count whatever that setting is. `checkpoints_checked` is how many sealed ranges were
cross-checked against that walk, and `sealed_through` is the highest seq a seal covers — the honest
statement of *how far back is provable*, because rows appended after the last seal and then removed leave
no witness. `break_at_seq`/`break_reason` locate the failure instead of asserting it, and the reason names the
layer that fired — **seven shapes, in the order the walk tests them**: `chain break at seq=…: prev_hash
pointer mismatch` (a re-pointed predecessor), `entry hash mismatch at seq=…: content was altered` (a rewritten
row), `checkpoint at seq=… seals head X… but the chain reaches Y… (sealed range P + 1..S)` (a re-hashed tail,
contradicted by the seal that closes the range, and the range is named), `checkpoint chain break at seq=…: it
names prev_chain_hash X after Y` (an inserted, pruned or re-ordered seal), `checkpoint hash mismatch at seq=…:
the sealed record was altered` (an edit to a seal's own columns), `checkpoint at seq=… seals 5000 events in
range 45000 + 1..50000; the walk found 4999` (a deletion inside a sealed range that was then re-sealed — the
one check that survives a forger who recomputes both chains), and `N checkpoint(s) seal ranges the ledger no
longer reaches` (rows removed from the tail). The read streams ten columns a page at a time — measured twice
at 1 M events by `scripts/ledger_bench.py`: 16.4 s at 69.3 MiB on `postgres:16` (13.7 s on the earlier
run) and 11.4 s at 65.5 MiB on SQLite (7.4 s earlier), against **2 462 MiB** and **2 392 MiB** for the
read that materialises every row. The seconds move with machine load; the megabytes do not. **The walk *is*
the request, and it no longer runs on the event loop**: this handler was an `async def` over a synchronous
session, so those 11–16 s were spent blocking the loop (measured with a 2 s stub in place of the walk, a
concurrent `GET /healthz` returned after 1.63 s). `TODO.md` T51 made it a plain `def`, which dispatches it to
Starlette's thread pool — `/healthz` answers while the walk runs, and the request holds a worker thread
instead of the replica (`tests/test_event_loop.py` holds the ledger walk and probes the loop while it is held;
`README.md` §6 item 27 records why the same change covers all seven long-running endpoints). On a ledger past
a few hundred thousand events the recommended path is still the CLI — `synthverify audit-verify`
prints the same verdict, both counts and the head hash, and occupies nothing but its own process.

**Retention report shape.** `GET`/`PUT`/`DELETE` on policies and holds return the row; the sweep returns a
plan plus what was done to it. `plan`: `now`, `ttl_days` (organisation → days, only for organisations that
actually have a policy), `assets_selected`, `asset_ids`, `jobs_selected`, `media_digests`, `media_objects`
(the stored keys this pass may remove), `artifact_files`, `held` (`{asset_id, organisation, sha256, reason,
hold_id, job_id}` per blocked resource), `deferred`, `assets_scanned`. `counts` is what the pass
**decided** — the same dict the `retention.swept` ledger row carries, because the ledger is written inside
the transaction and a decision is all it can honestly record — and `outcomes` is what the storage half
**did** after the commit (`media_objects_removed` vs `media_objects_absent`, `artifact_files_removed` vs
`artifact_files_kept`). Rows and ledger commit first, bytes go second: a sweep that rolled back after
removing an object would leave rows pointing at evidence that no longer exists, which is the one state this
store must not reach. An object or a digest-named artifact file is removed only when **no** surviving row in
any organisation still names it.

**Per-org policy resolution.** Every analysis (async job or sync `/analyze`) picks its thresholds at
run time: the submitting organisation's most recent **active** PolicyProfile → the `global` profile
(managed via `PUT /admin/policy`) → `SV_BLOCK_SCORE`/`SV_MANUAL_REVIEW_SCORE` settings. The resolved
profile's name is recorded in the report as `policy_name`, so every verdict states which policy
produced it. Worker policy lookups never fail a job — on error they fall back to settings defaults.

---

## Operations *(unauthenticated)*

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness |
| `GET /readyz` | database + queue + limiter shape: `job_broker`, `durable_queue`, `embedded_workers`, `queue_depth` (read from the live broker, so a replica's topology is inspectable rather than inferred), plus `rate_limit_backend` and `rate_limit_degraded` — the second is true when a shared Valkey bucket was configured and the limiter is currently enforcing its **local** budget. Both are read from process state, so a readiness probe never contacts the limiter's backend |
| `GET /metrics` | Prometheus text format 0.0.4 by default; `Accept: application/openmetrics-text` returns OpenMetrics 1.0.0 **with exemplars** (`Vary: Accept`). Exemplars carry `trace_id`/`span_id`, so a series can be pointed at one real request; 0.0.4 has no syntax for them and so carries none. `synthverify_metrics_exemplars_dropped_total` counts the ones this build refused for an invalid label value. This endpoint needs no key, so no label here identifies a caller: the rate-limit rejection counter is labelled `authenticated="true"\|"false"` and never carries the limited subject, which would otherwise publish key ids — and, for an anonymous caller, client IPs — to anyone who can scrape |
| `GET /api/v1/meta` | version, limits, tiers, actions, flag glossary |
| `GET /dashboard/` | operator console (key stored in your browser only) |
