# Security Model

## Identity & access

* **API keys** — format `sv_live_<32 hex>`; only the SHA-256 hash is stored, so
  a database leak does not leak usable credentials. The raw secret is shown
  exactly once at creation. Revocation is immediate and audited.
* **Roles** — `admin` (keys, webhooks, policy, audit), `analyst` (read jobs,
  run verification), `service` (machine submission). Enforced server-side per
  endpoint.
* **Tenancy** — a key's `organisation` decides *whose data* it may read, and the
  role decides only *which endpoints* it may call. A cross-organisation read,
  update or delete of a job, artifact or asset answers `404`, the same as an id
  that does not exist, so the API is not an existence oracle. `POST
  /api/v1/jobs/{id}/reanalyze` is covered by the same single check at the shared
  lookup, because it is a write to someone else's evidence otherwise.
  Cross-organisation reach belongs to `ApiKey.platform_scope` alone - a column,
  not a role and not an organisation name, so no tenant can be *called* into
  owning the deployment.
* **Control plane** — `/api/v1/admin/**` mints keys for any organisation, points
  webhooks at any organisation and reads the whole ledger, so it requires role
  `admin` **and** `platform_scope`. There is deliberately no per-organisation
  admin surface in v1: a newsroom gets `analyst`/`service` keys, and the
  platform key stays with the operator. Minting a second platform key is `POST
  /api/v1/admin/keys` with `platform_scope: true`, which the API accepts only
  alongside `role: admin` - a `service`-role platform key would be an unattended
  full-tenant reader.
* **Bootstrap key** — set `SV_BOOTSTRAP_ADMIN_KEY` in production; otherwise a
  random admin key is generated once, written `0600` to
  `data/bootstrap_admin_key.txt` and audit-logged. It is the one credential
  created with `platform_scope`, and migration `0005` (brought in by `alembic upgrade head`, now at
  `0007`) gives the
  same flag to an existing install's bootstrap key so an operator upgrading is
  not locked out of the plane that mints keys.
## Input handling (forensic services process hostile files)

* Media type is decided by **magic bytes**, never by filename/extension.
* PIL decompression-bomb guard (`MAX_IMAGE_PIXELS` capped at 64 MP).
* Strict upload limits: 64 MiB (async) / 8 MiB (sync); size checked before any
  decode.
* Every decode is wrapped: undecodable or corrupt content yields a clean 422,
  never a worker crash. Detector exceptions are converted to `ERROR` results —
  one hostile file cannot take down the fleet.
* Webhook URLs cannot target cloud-metadata endpoints implicitly — deploy
  egress policies per your environment; deliveries never follow redirects.

## Integrity & non-repudiation

* **Audit hash chain** — every security-relevant event (key creation/revocation,
  ingestion, completion, failures, policy changes, webhook outcomes) commits to
  the previous entry: `entry_hash = SHA256(canonical_json(entry, prev_hash))`.
  `GET /api/v1/admin/audit/verify` (and `synthverify audit-verify`) recompute
  the full chain; any silent edit breaks it with the offending sequence number.
* **Checkpoint seals** (`REQ-IDAM-4`, migration `0007`) — every
  `SV_AUDIT_CHECKPOINT_EVERY` events (5 000 by default) a row in `audit_checkpoints` records that
  seq, the `entry_hash` at it, **how many events its range held**, and the previous seal's own digest,
  so the seals form a chain of their own on top of the event chain. Verification hashes **every**
  event row regardless - a seal is a second fixed point to compare against, never a licence to skip
  work, and `entries_checked` in the report is what proves it. Five independent checks run per
  sealed row (entry digest and pointer, sealed head against chain head, previous-seal pointer, the
  seal's own digest, the recorded range size against the walk) plus a tail guard that a seal past the
  end of the data cannot be ignored; `scripts/ledger_e2e.py` removes each one in turn and requires
  the suite to notice. `sealed_through` answers the question a chain inside someone else's database
  cannot otherwise answer: how far back is provable.
* **What the chain does *not* protect, stated rather than assumed** — the ledger lives in a database
  an attacker with a write credential can edit. Measured, on 1 000 000 events, on both dialects, by
  `scripts/ledger_bench.py`: editing a row is caught at that row; editing **and** re-hashing the tail
  is caught by the seal that closes the range; deleting a row **and** re-pointing the seal is caught,
  because the seal also recorded how many rows its range held and the walk finds four where five were
  sealed. What is **not** caught is a forger who re-links the tail, re-points every seal *and*
  rewrites the recorded counts - fully privileged history rewrite, which no in-database commitment
  can detect, because the attacker holds every copy of the evidence. That case is not glossed over:
  the bench asserts it returns `verified=True` on every run and **fails if it ever stops being true**,
  so the code cannot drift better than this paragraph while nobody is looking. Closing it needs a
  seal digest anchored where the writer has no credential (an external append-only copy, or a
  signature over the head published out of band), which is out of scope for v1.
* **Retention is audited in both directions** — creating a legal hold
  (`legal_hold.created`) and every pass that deletes evidence
  (`retention.swept`) are ledger entries, so "who pinned this, who deleted this,
  under which TTL, and what was skipped" is one query. The hold entry exists
  because `AC-INFRA-5` asks for it: a pin that is only a flag in a table is not
  evidence that the pin was in force.
* **Signed webhooks** — `X-SynthVerify-Signature: t=<unix>, v1=HMAC-SHA256(secret,
  "<t>.<body>")`. Receivers must verify the HMAC and may enforce timestamp
  freshness (reject `|now - t| > 300 s`).
* **Idempotency** — client-supplied keys make retried ingest safe; duplicates
  return the original job rather than double-processing.

## Abuse prevention

* Per-key token-bucket rate limiting (RPM + burst) with `429` + `Retry-After`.
* Queue depth cap (`SV_QUEUE_MAX_SIZE`); job timeout; per-key RPM override for
  partner tiers.
* Job cancellation is admin-only and audited.
* `/metrics` is **unauthenticated**, so nothing in it may identify a caller: the
  rejection counter is labelled `authenticated="true"|"false"` and never carries
  the limiter's subject. A per-subject label would have published every tenant's
  `key_id` to anyone who could scrape, and for an unauthenticated caller the
  subject is a client IP - which makes the series count attacker-controlled.

## Data protection

* Media is stored content-addressed under `SV_STORAGE_DIR`; reports and
  artifacts under `SV_ARTIFACTS_DIR` — run these on encrypted volumes and
  define a retention policy per your jurisdiction.
* **Retention (REQ-INFRA-5) is per-organisation, opt-in and reference-counted.**
  The TTL is a row in `retention_policies`, written through the platform-admin
  control plane; **no row means the organisation's media is never swept**, and
  `SV_RETENTION_DEFAULT_DAYS` is unset by default, so an install that configures
  nothing loses nothing. Deletion then has three guards. (1) *Capability*: every
  route — including `POST /admin/retention/sweep` — needs role `admin` **and**
  `platform_scope`, so a tenant's own admin key cannot purge the store, and the
  sweep's `dry_run` defaults to **true** (`synthverify retention-sweep` likewise
  previews unless given `--apply`). (2) *Reference count*: storage is
  content-addressed, so an object — or a heatmap file named from the media
  digest — is removed only when no surviving row in any organisation still names
  it; two tenants over one object survive each other's TTL. (3) *Order*: the rows
  and the ledger entry commit **before** any byte is removed. A sweep that
  rolled back after deleting storage would leave rows pointing at evidence that
  no longer exists, which is the one state a forensic store must not reach; the
  failure the ordering does allow is an unreachable object, and the planned key
  list is recorded in the ledger row so a reconcile can name it.
* **A legal hold blocks the TTL, and pinning is scoped to the bytes.**
  `legal_holds` matches either a media `sha256` or a job id, so pinning a
  verdict does not freeze its source file for every other tenant, while a digest
  pin does protect every organisation holding those bytes — which is the point,
  since they are one object. A `media` or `job` id that does not exist is a
  `404`: a hold naming a typoed digest would look like protection in the audit
  trail and provide none, the worst failure a hold can have. Releasing is a soft
  delete (`active=false`, `released_at`, `409` on a second release) because the
  row is the record that a hold *was* in force. A queued or running job
  **defers** its asset to the next pass rather than deleting under the worker.
* **The scheduler cannot be a purge on restart.** The sweep runs on the
  `WorkerFleet` thread and sleeps its interval *before* its first pass; planning
  takes a transaction-scoped advisory lock on Postgres (and `BEGIN IMMEDIATE` on
  SQLite), because the rows one replica's plan reads are the rows another
  replica's plan would otherwise delete.
* Forensic artifacts (ELA heatmaps) are **not** statically mounted. They are
  served only by `GET /api/v1/jobs/{id}/artifacts/{index}`, which authenticates,
  is tenant-scoped (another organisation's key gets `404`, whatever its role), and
  re-derives the file name from the job's own media digest instead of trusting the
  stored path — so a tampered report cannot point the endpoint at arbitrary files.
* Evidence bytes are reached only through `MediaStore`. For `SV_MEDIA_STORE=s3`
  the SigV4 credentials come from `SV_S3_ACCESS_KEY`/`SV_S3_SECRET_KEY` (env/secret
  store, never a file in the image or a value in the database) and are excluded
  from `describe()`, which is what `/readyz`, logs and the CLI print - asserted by
  test, because a backend identity that leaks the secret key is a credential
  disclosure through the health endpoint. Object names are derived from the media
  digest with the submitted filename reduced to `[A-Za-z0-9._+-]` (dot runs
  collapsed), so a hostile upload name can neither traverse a prefix nor break
  another tenant's object. Buckets must be private: URLs are not handed out,
  evidence is streamed via the authenticated API.
* No third-party calls are made by the pipeline itself (webhooks go only to
  endpoints you registered).
* Upload dedup is scoped **per organisation**: two tenants submitting identical
  bytes get two `MediaAsset` rows, because a job response inlines its media's
  filename and upload time and sharing the row would disclose the first tenant's
  to the second. The bytes are still written once — the object store keys on the
  digest, not on the row.
* The dashboard stores the operator's API key in the browser's `localStorage`
  only; it never transmits it anywhere except the API it was entered for. Four of
  its five tabs read the control plane and are marked `*` in the navigation,
  because they need a platform-scoped key; the jobs tab works with any
  organisation's own key and then shows only that organisation's queue.
* SQLite/PostgreSQL: use TLS and credential management per your platform
  (`SV_DATABASE_URL`); the default is local-disk only.

## Container & CI posture

* Image runs as non-root `svuser` (uid 10001), filesystem volumes isolated to
  `/app/data`, `no-new-privileges` set in compose, healthcheck via curl.
* CI runs lint + full test suite on every push and smoke-tests the built
  container (`/healthz`, `/readyz`) before it can be considered green.

## Threat notes for production hardening

| Threat | Hardening |
|---|---|
| Key exfiltration via logs | the app never logs raw keys (only `key_id` prefixes); rotate via revoke+recreate |
| A key handed to one tenant reaching another | tenancy is decided by `organisation` on every read and is role-blind; the control plane needs `platform_scope`; `tests/test_tenancy_matrix.py` enumerates all 32 exposed operations and `scripts/tenancy_e2e.py` proves the table bites by removing each enforcement in turn |
| Internal attacker rewriting audit history | chain verification fails on any historical mutation; export/archive ledger rows externally on a schedule |
| Webhook receiver spoofing | verify HMAC + timestamp; keep secrets per-endpoint and rotate |
| Model-evasion adversary | multi-detector independence, periodic calibration KPIs, ML plugin layer, provenance (C2PA) verification |
| Denial of service | rate limits + queue caps; deploy behind a gateway with body-size limits; scale workers via `SV_WORKER_COUNT` or external queue |
