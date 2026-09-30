# Architecture

## System overview

```
                              ┌─────────────────────────────────────────────────────────┐
                              │                     FastAPI app                          │
   ┌──────────┐   POST /ingest│  ┌──────────┐   ┌──────────────┐   ┌─────────────────┐  │
   │ client / │──────────────▶│  │  routes  │──▶│ MediaAsset + │──▶│  WorkerFleet    │  │
   │ workflow │◀──────────────│  │  (auth,  │   │  Job (DB)    │   │  N threads      │  │
   └──────────┘   202 job_id   │  │ ratelim) │   └──────────────┘   └───────┬─────────┘  │
                               │  └──────────┘                              │            │
   ┌──────────┐  POST /analyze│        │                                   ▼            │
   │  SDK /   │──────────────▶│        │                        ┌────────────────────┐  │
   │  CLI     │◀──────────────│        │                        │  orchestrator:     │  │
   └──────────┘   200 report   │        │                        │  sniff → detectors │  │
                               │        ▼                        └─────────┬──────────┘  │
   ┌──────────┐   GET /metrics│  ┌──────────────┐                         ▼             │
   │ Prometheus│──────────────│  │ audit ledger │                  ┌──────────────┐    │
   └──────────┘               │  │ (hash chain) │◀────────────────│  xai.fuse()  │    │
                               │  └──────────────┘                  └──────────────┘    │
   ┌──────────┐  POST /hook  │                                              │           │
   │ webhook  │◀─────────────│  ┌────────────────────────┐                  ▼           │
   │ receiver │  HMAC signed │  │ WebhookDelivery ledger │◀─────── retry w/ backoff   │
   └──────────┘              │  └────────────────────────┘      (webhook retry loop)     │
                               └─────────────────────────────────────────────────────────┘
```

## Request lifecycle (asynchronous verification)

1. **Ingest** — `POST /api/v1/media/ingest` authenticates the API key
   (SHA-256 lookup; role and organisation enforced), applies the rate limit (a token bucket, in this process or in Valkey — see §Scaling path),
   reads the upload, sniffs the media type from magic bytes and stores the
   bytes content-addressed (`<sha256[:2]>/<sha256>_<name>`). An `Idempotency
   Key` (form field) makes retries safe: same key + org returns the original job.
2. **Job creation** — a `Job` row (status `queued`) and audit events
   (`media.ingested`, `job.created`) are committed *before* enqueueing, so a
   crash between steps cannot orphan work.
3. **Dispatch — through the `JobBroker` seam** (`SV_JOB_BROKER`). `embedded` (the default) hands the id
   to the in-process `WorkerFleet` pool by priority, and on boot re-enqueues any `queued`/`running` rows
   it finds (crash recovery); without a fleet, routes process jobs inline. `postgres` makes the same
   hand-off a single statement against the shared table —
   `UPDATE jobs SET status='running', claim_token=…, claimed_by=…, lease_expires_at=…,
   attempts=attempts+1 WHERE id = (SELECT id … WHERE status='queued' ORDER BY priority, created_at
   LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id` — so concurrent replicas cannot take the same row, and a
   lease that outlives its worker is reclaimable. `claim_token`/`lease_expires_at` are ownership and are
   released at the terminal write; `claimed_by` is history and is kept, because "which replica ran this
   job?" is the first thing an operator asks after a job doubles back.
4. **Pipeline** — the orchestrator builds a lazily-decoding
   `DetectionContext` shared by all detectors of that media type (one image
   decode serves five detectors), runs each detector under a timing guard,
   and converts unexpected exceptions into `ERROR` results (a broken detector
   degrades the verdict's *coverage*, it never 500s the job).
5. **Fusion** — `synthverify.xai.aggregate` produces the
   `VerificationReport` (verdict, narrative, evidence ranking, flags,
   routing action).
6. **Persistence + callbacks** — the report is stored on the job, a
   `job.completed` audit entry is appended, matching webhook endpoints get a
   `WebhookDelivery` row, and delivery is attempted immediately; failures
   back off exponentially (2^n seconds) up to `SV_WEBHOOK_MAX_ATTEMPTS`,
   driven by the retry loop.

## Detector framework

```
Detector (abstract)
  name, media_types, weight, description
  detect(ctx) -> DetectorResult
  run(ctx)     -> timed wrapper (errors → ERROR result)

DetectionContext  (lazy, shared decode)
  .image   -> PIL Image          .audio -> AudioSignal (numpy)
  .video   -> VideoFrames        .text  -> str
```

* **Registry** — `@register` self-registers plugin classes;
  `detectors_for(media_type, requested)` selects and validates.
* **Result contract** — `score` (0..1 synthetic-likelihood), `confidence`
  (0..1 self-assessed reliability *for this input*), `flags` (machine codes),
  `findings` (human sentences with measured numbers), `evidence`
  (structured metrics, JSON-safe), optional artifact (e.g. ELA heatmap PNG).

### Built-in detectors

| Detector | Media | Signal used |
|---|---|---|
| `ela` | image | Re-compression error-field uniformity (16×16 grid), hotspot localization; heatmap artifact |
| `metadata` | image | EXIF camera/software/timestamps, PNG text chunks, AI-tool signature list, C2PA/JUMBF markers |
| `frequency` | image | Radial spectral profile (p98 per ring), JPEG-harmonic exclusion, checkerboard lattice energy |
| `noise` | image | Per-block MAD noise floor, cross-block inconsistency, 2px-lag residual autocorrelation (lossless only) |
| `jpeg_history` | image | DQT parsing, IJG quality inversion, double-compression evidence |
| `audio_spectral` | audio | STFT flatness stability, spectral-flux discontinuities, high-band occupancy |
| `audio_dynamics` | audio | Silence-run structure, digital-zero share, dynamic range, clipping |
| `audio_metadata` | audio | RIFF INFO walk, TTS encoder signatures, sample-rate tells |
| `video_temporal` | video | Photometric flicker (2nd derivative), adaptive duplicate-frame hashing, cut structure |
| `video_metadata` | video | Generator-native geometry, fps sanity, per-frame error-level CV |
| `text_stylometry` | text | Burstiness (sentence-length CV), LLM stock phrases, connective density, TTR, n-gram repetition |

## XAI fusion engine

```
fused = Σ(wᵢ · sᵢ · cᵢ) / Σ(wᵢ · cᵢ)            # confidence-weighted vote
fused = max(fused, maxᵢ(sᵢ·cᵢ) · 0.9)           # strong-evidence override
fused = max(fused, 0.90)                        # declared-synthetic rule
                                                # (conf ≥ 0.85 ∧ score ≥ 0.85)
confidence = mean(cᵢ) · f(coverage)             # skips discount certainty
```

Routing policy (per-org overridable, `PUT /api/v1/admin/policy`):

| Condition | Action |
|---|---|
| coverage/confidence too low | `NEEDS_HUMAN_REVIEW` (report marked inconclusive) |
| declared-synthetic rule or `fused ≥ block_score` | `BLOCK` |
| `fused ≥ escalate_score` or ≥3 flags | `ESCALATE` |
| `fused ≥ review_score` or ≥2 flags or review-forcing flag | `MANUAL_REVIEW` |
| otherwise | `PROCEED` |

## Data model

`ApiKey` (hashed secrets, roles, and a `rate_limit_rpm` column the limiter does **not** consult yet — the applied budget is the configured global one per key; `platform_scope`, added by revision `0005`, is what lets a key read more than one organisation) · `MediaAsset`
(content-addressed) · `Job` (lifecycle + result JSON + the queue lease: `claim_token` /
`claimed_by` / `lease_expires_at`, added by revision `0003`) · `WebhookEndpoint` /
`WebhookDelivery` (subscription + delivery ledger) · `PolicyProfile` ·
`RetentionPolicy` (one row per organisation, `media_ttl_days`; **the absence of a row is "keep it"**) and
`LegalHold` (a `media` digest or a `job` id pinned against deletion, released by flag rather than by
`DELETE`, because the row is the record that the hold existed) — both added by revision `0006` ·
`AuditEvent` (hash chain: `entry_hash = SHA256(canonical(entry + prev_hash))`, and `prev_hash`
is only trustworthy while one writer at a time may extend the chain — see *Concurrency*) ·
`AuditCheckpoint` (a seal every `SV_AUDIT_CHECKPOINT_EVERY` events: the sealed `entry_hash`, how many
events its range held, the previous seal's digest and its own — added by revision `0007`, see
*Audit checkpoints*).

## Tenancy (`synthverify/auth.py`)

Two axes, decided independently, because conflating them is the defect this section documents: *which
endpoints* a credential may call is its **role**, and *whose data* it may read is its
**`organisation`**. Until `AC-IDAM-3` was proven, `role=admin` short-circuited the organisation filter
on every job route — so a key minted for one newsroom read every other one's queue, artifacts, and the
media metadata those responses inline. Tenancy is now **role-blind**: `visible_to()` compares
organisations and nothing else.

* **One enforcement point per resource family.** Every `/api/v1/jobs/**` handler that addresses an
  existing job goes through `_get_job_or_404`, which authenticates tenancy at the lookup. The
  alternative — a check per handler — is how `POST /jobs/{id}/reanalyze` ended up with none; and it is a
  write, so a second tenant could queue forensics on another tenant's media and create a job *inside*
  that tenant's organisation.
* **`404`, never `403`.** `not_found_or_self()` raises the identical response a missing row would
  produce, so no endpoint is an oracle for "does this id exist under another organisation?". The matrix
  asserts both halves: the real foreign row and an unowned id must be indistinguishable.
* **`platform_scope` is a column, not a role and not a name.** Cross-organisation reach belongs to one
  explicit flag on the credential (revision `0005`), which keeps `organisation` a pure tenant label.
  Reserving one organisation *value* as "the platform" would have made the scope a function of a string
  a tenant registration supplies — and would have broken webhook and policy matching, which both key off
  `organisation`.
* **The control plane is not an org-level surface.** `/api/v1/admin/**` mints keys for any organisation,
  points webhooks at any organisation, and reads the whole ledger, so `require_platform()` demands role
  `admin` **and** the flag. v1 therefore has no per-organisation administration: newsrooms run on
  `analyst` and `service` keys, and the operator's platform key does the administering.
* **Dedup is scoped per organisation.** Content addressing keys the *object store* on the digest, so two
  tenants uploading identical bytes still write one file; the `MediaAsset` **row** is per organisation,
  because `Job.to_dict` inlines `media.to_dict()` and a shared row would hand the second tenant the
  first one's filename, asset id, and upload time. The client `Idempotency Key` is scoped the same way —
  it is a handle inside one organisation, not a global one.

`tests/test_tenancy_matrix.py` enumerates the exposed surface from the app's own OpenAPI document and
fails if any operation has no decided rule, so a new endpoint cannot ship unproven.
`scripts/tenancy_e2e.py` then removes each enforcement, one at a time, from a copy of the package and
requires the matrix to go red: a green table is only evidence once its teeth have been measured.

## Concurrency

Two claims in this product are about *ordering between writers*, so each is enforced where the
write happens rather than checked afterwards:

| Claim | Mechanism | Why it has to be the write |
|---|---|---|
| A job is processed by exactly one worker | `FOR UPDATE SKIP LOCKED` claim + `claim_token` written in the same statement; the terminal `UPDATE` carries `WHERE claim_token = :token` | a check-then-write lets a slow, dead worker and the replica that took its lease both commit. The loser's write matches 0 rows, rolls back and counts `synthverify_jobs_fenced_total` — the reclaim is already audited as `jobs.leases_expired` |
| The audit ledger is a chain, not a tree | PostgreSQL: `pg_advisory_xact_lock(hashtextextended('synthverify.audit_chain', 0))` at the top of `AuditLedger.append()`, released at commit. SQLite: `BEGIN IMMEDIATE` on every transaction (`isolation_level=None` on the DBAPI + an engine `begin` listener) | `append()` reads the current head *then* inserts a row pointing at it. Nothing in between the two statements is a lock, so two committed transactions may share a parent and the chain **forks** — `verify_chain` then reports `verified=False` on bytes nobody tampered with. Measured: 200 appends from 8 threads left 111 rows on Postgres (and 8 on SQLite) pointing at an already-extended parent |
| A rate limit is one limit, not one per replica | the drain happens *inside* Valkey: one Lua script reads the server's `TIME`, refills, compares and decrements, and returns `{allowed, retry_after}` atomically | a client-side read-modify-write is a lost update in disguise — two replicas that both read 1.0 token both admit, and `n` replicas quietly hold `n` budgets. Measured across two OS processes: private buckets admit 80 against a burst of 40, the shared bucket admits 40 (`scripts/ratelimit_e2e.py`) |

Both mechanisms hold the lock for exactly the lifetime the read-then-write pair needs (until commit),
which is also why neither needs a retry loop: an index-and-retry design (`UNIQUE (prev_hash)` +
`SAVEPOINT`) was built and rejected on measurement — it livelocked on Postgres and died of
`database is locked` on SQLite, and adding that index to an already-forked database would require
recomputing every `prev_hash`, i.e. rewriting a tamper-evident ledger to make a constraint pass.
`tests/test_audit_concurrency.py` carries both halves: the pre-fix append shape must fork (so the
check can see a fork), and the shipped append shape must not, at the same concurrency, on both dialects.

## Audit checkpoints (`synthverify/db.py`) — `REQ-IDAM-4`

`AC-IDAM-4` asks for two things at once — chain verification of 1 M events in under 60 s, and a
checkpoint-insertion test that detects tampering *inside* a checkpointed range — while `REQ-IDAM-4`
insists the verifier "still verif[ies] **every** hash between checkpoints". Those pull against each
other, and the design resolves it by making a seal a fixed point to compare against rather than a
licence to skip work:

* **What a seal records** — `seq`, the `entry_hash` of the event that closed the range, `prev_seq`,
  **how many events its range held**, the previous seal's `chain_hash`, and the seal's own digest over
  all of it. The seals therefore chain over themselves: pruning or editing one is a break in a second
  chain, not a missing row in a lookup table.
* **What the walk checks, in order** (`AuditLedger.verify()` — every row, then each seal it passes): the
  predecessor pointer, the entry digest, the sealed head against the chain head, the previous seal's
  digest against the seal chain, the seal's own digest against its columns, and the recorded range size
  against the rows actually walked — plus a tail guard so a seal whose events no longer exist cannot be
  quietly ignored. The order is load-bearing for what gets *named*: an edit with the tail re-hashed is
  caught by the seal that closes the range, while a deletion that was then re-sealed is caught by
  nothing else — the recorded count is the one invariant a writer who recomputes both chains still has
  to satisfy.
* **Cost** — the walk is a server-side cursor over ten columns, 1 000 rows a page, measured at the
  criterion's own size by `scripts/ledger_bench.py` (which prints both read shapes on every run): 1 M
  events verified in 11.4 s at 65.5 MiB on SQLite and 16.4 s at 69.3 MiB on `postgres:16` (a second pair
  of runs: 7.4 s and 13.7 s — the seconds track machine load, the megabytes do not), against 2 392 MiB and
  2 462 MiB for the read that materialises every row. At 200 000 events the cursor is what
  makes it a page rather than a copy — 67.3 MiB with `stream_results`, 302.3 MiB without, ~3 s either
  way. So `batch` is a memory knob and not a correctness knob, which is why the suite pins the *report*
  identical at batch 1 and batch 5 000 instead of trusting it.
* **Backfill** — `synthverify audit-checkpoint --backfill` seals history that predates `0007`, and
  refuses to seal a chain that does not first verify. Promoting a forgery into a set of fixed points is
  the one way this scheme could make a tampered ledger *look* certified, so the guard is the feature.
* **The boundary, stated not assumed** — [`docs/security.md`](security.md). A writer who re-links the
  tail, re-points every seal *and* rewrites the recorded counts is outside what any commitment stored in
  the database they can write to can prove; the bench asserts that case still returns `verified=True` on
  every run and fails if it stops being true, so the documentation cannot drift better than the code.
* **Teeth** — `scripts/ledger_e2e.py` takes each enforcement away in turn: eleven modes (no seal on
  append, an off-by-one seal, each of the four per-seal checks, the tail guard, backfill without
  verifying, the server-side cursor, and a digest that omits the payload or the pointer), each requiring
  `tests/test_audit_checkpoints.py` to go red. A `PATCHES` anchor whose quoted source no longer matches
  raises rather than silently mutating nothing — a no-op patch would report "0 cases failed" and look
  like a gate that passed.

## Media storage (`synthverify/storage/`)

Evidence bytes are reached only through the `MediaStore` contract, so *where*
media lives is configuration rather than code:

| | |
|---|---|
| Key scheme | `<sha256[:2]>/<sha256>_<sanitised-name>`, defined **once** in `base.py`; the digest is the identity, the name is decoration |
| Consequence | dedupe is free: the same bytes uploaded by two organisations are one object, and `put` of bytes already stored is a no-op. The same scheme makes *deletion* a reference count rather than a `DELETE` — see *Retention* |
| Persisted value | `MediaAsset.storage_path` holds a **location**, which is backend-derived: an absolute path locally, `s3://bucket/prefix/key` for an object store |
| Backends | `LocalMediaStore` (keeps v1's exact on-disk layout; writes mkstemp → `fsync` → `os.replace` so a reader never sees a partial object) · `S3MediaStore` (MinIO/AWS/Ceph/R2; AWS SigV4 implemented on stdlib `hmac`/`hashlib` over the existing `httpx` - **no new dependency**) |
| Selection | `SV_MEDIA_STORE=local\|s3` + `SV_S3_ENDPOINT/BUCKET/REGION/ACCESS_KEY/SECRET_KEY/PREFIX/PATH_STYLE`, through a factory that caches one store per configuration |
| Round trip | `location()`/`key_for_location()` speak the *content* key; the configured prefix is applied only when a request is built, so a location and its key round-trip exactly |
| Failure shape | one typed error pair - `MediaNotFoundError` (absent) and `MediaStoreError` (unreachable endpoint, wrong bucket, foreign row) - so the pipeline never leaks `OSError`/`httpx` into a response |

The contract is enforced by a parity suite: the same operations and the same
ingest→verdict run execute against **both** backends with no `if backend == ...`
branch in the test body, which is the only way "swap it with no code change" is
more than a slogan.

## Retention (`synthverify/retention.py`) — `REQ-INFRA-5`

A TTL over a forensic store is not one operation, so the module is split into the three steps that have
different failure modes:

| | |
|---|---|
| `plan_sweep()` | decides, touches nothing. Per organisation: read `retention_policies`, and **no row means skip the org entirely** (not "zero days"). The cutoff goes into SQL (`media_assets.created_at` is indexed) and is re-checked in Python through `as_utc`, because SQLite hands back naive datetimes |
| `apply_sweep()` | deletes the planned **rows** and appends the `retention.swept` ledger entry, then the caller commits |
| `remove_storage()` | removes the **bytes**, after that commit, records what actually went — and lets a store that refuses raise |

Between planning and deleting sit three checks, each its own reason this is a module rather than a
`DELETE … WHERE created_at < …`:

* **Legal holds.** `active_hold_for()` matches the asset's `sha256` (kind `media`) or any of its job ids
  (kind `job`) against an active `legal_holds` row, oldest first, so a pin's `reason` is attributable and
  two pins on one resource agree. A held asset is reported in `plan.held` and stays exactly where it is.
* **Work in flight.** A `queued`/`running` job **defers** its asset (`plan.deferred`) rather than deleting
  the row underneath the worker. The outcome write is fenced on `claim_token`, so this is not a
  correctness requirement — it is a refusal to throw away compute and leave a verdict with nowhere to go.
* **Reference counts.** `content_key` is `<digest[:2]>/<digest>_<name>`, so after `AC-IDAM-3` made dedup
  per-organisation, *two tenants' rows can name one object* — same bytes, same filename. A key is
  removable only when no row anywhere still names it (`sha256` is indexed, so this is one bounded lookup
  per digest, not a scan). Artifact files are shared the same way, because the orchestrator names them
  `<digest[:12]>_<detector>_<artifact>`: the surviving jobs of the same digest are the only rows that can
  still reference them, so a held or unexpired tenant keeps its heatmap while the expired tenant's rows go.

**Order, and what each side records.** Rows plus the ledger entry commit **first**; storage is removed
after. A sweep that rolled back after deleting an object would leave rows pointing at evidence that no
longer exists — the one state this store must not reach. The asymmetry it accepts instead is an
*unreachable* object, and the ledger row carries the planned key list precisely so that state is
reconcilable rather than merely invisible. That split is also why the report has two count dicts:
`counts` is what the pass **decided** (and is the shape the ledger can honestly hold, since it is written
inside the transaction), `outcomes` is what the storage half **did** (`removed` vs `absent`,
`removed` vs `kept` — "we deleted it" and "it was not there" are different statements about evidence, and
the second is what a repeat pass over the same keys looks like).

**When the storage half fails.** The two file kinds fail differently, because they fail in front of
different people. An artifact that will not unlink is *reported* (`artifact_files_kept`) and the pass
returns: it is a local file on a box the operator can log into. An object the store refuses to delete
*raises* `MediaStoreError` — the same typed error the S3 backend raises for an unreachable endpoint and a
`403` alike — and it raises **after** the commit, so the deletes stand, the scheduler counts the failure,
and the alert rule can fire. Swallowing that refusal would turn a leak into a 200. This is also why
`synthverify_retention_sweeps_total` is incremented at the commit rather than at the end of `sweep_once()`:
a pass whose rows went but whose bytes refused has happened, and `retention_deleted_total` already says so —
two counters describing one event differently is a scrape nobody can trust. `tests/test_retention.py`'s
`TestTheStoreRefuses` holds a store to that refusal and asserts the residue from both ends, including the
part with no recovery: a later pass plans nothing for a tenant whose rows are already gone, so reclaiming
the orphan means reading the key list out of the `retention.swept` entry.

**Where the scheduler lives.** A `WorkerFleet` thread, not the API lifespan: a fleet is what a deployment
of this product always has, an HTTP listener is not, and one pass per interval across replicas is
serialised by the same advisory lock the ledger appends use (`pg_advisory_xact_lock`, `BEGIN IMMEDIATE` on
SQLite) because *planning reads are what decide deletion* — two replicas must not both plan against the
same rows. It **sleeps before its first pass**, so restarting a fleet cannot purge a tenant the moment it
comes back up, and a failed pass is logged and counted rather than allowed to kill the loop. The interval is
a **floor, not a promise**: passes never start less than a second apart, whatever `SV_RETENTION_SWEEP_INTERVAL_SECONDS`
says, so a mistyped `0` cannot turn the delete path into a spin. `SV_RETENTION_BATCH_LIMIT`
bounds how many assets one transaction holds; the next pass takes the rest, which is what makes "the sweep
is idempotent" an operational property rather than a lucky one.

**Same code, three doors.** `POST /admin/retention/sweep` (platform admin, `dry_run` defaults to **true**),
`synthverify retention-sweep` (previews unless given `--apply`), and the scheduler thread all call
`sweep_once()`. The preview returns the same `SweepPlan` a real pass consumes, so "what would this delete"
and "what did this delete" cannot drift, and `apply_sweep(dry_run=True)` returns before it touches a
session — a preview cannot delete by oversight.

**Metrics.** `synthverify_retention_sweeps_total` (by `dry_run`), `synthverify_retention_deleted_total`
(by `kind`: `media_row`, `job_row`, `media_object`, `artifact_file`), `synthverify_retention_held_total`
(by `kind`: `media`, `job`) and `synthverify_retention_sweep_errors_total`. **No `organisation` label
anywhere**, for the reason T44 found: `/metrics` is unauthenticated, so a tenant name in a label value
would be a tenant-enumeration oracle. Org-scoped facts go to the admin API, which is authenticated.

## Schema evolution (`synthverify/migrations/`)

`Base.metadata.create_all()` still bootstraps a development database, but it is no
longer the deploy path. Alembic owns the schema:

* `make migrate` / `synthverify db-upgrade` applies pending revisions;
  `--print-sql` emits DDL without connecting (review it before an air-gapped
  change), `--stamp` records a revision for a database that `create_all()` built.
* `alembic.ini` carries **no** connection string - `SV_DATABASE_URL` (or `--url`)
  is resolved at run time, so a mistyped ini cannot migrate the wrong database.
* Revisions are **guarded on inspection**: every `create_table`/`create_index`
  checks live state first, because installs that predate Alembic have the tables
  and no `alembic_version` row. `upgrade head` on such a database converges.
* `0001_baseline` reproduces the seven v1 tables and their indexes;
  `0002_rename_legacy_risk_tier_column` repairs the column v1 misnamed `"LOW"`;
  `0003_add_job_broker_lease_columns` adds the queue lease (`claim_token`, `claimed_by`,
  `lease_expires_at`) that `SV_JOB_BROKER=postgres` claims with;
  `0004_add_trace_correlation_columns` adds `jobs.trace_id` (the carrier from the ingest request to
  whichever process runs the job) and `audit_events.trace_id` (the same id recorded per ledger entry),
  both nullable, both indexed, both appended so `ALTER TABLE` order still matches `Base.metadata`;
  `0005_platform_scope_bootstrap_key` adds the column that separates *whose data* from *which endpoints*;
  `0006_retention` adds the `retention_policies` and `legal_holds` tables, and seeds
  **nothing** - the spec states the mechanism and never a number of days, so an install that applies the
  migration and configures no row deletes no evidence.
* The model declares those three columns **last** in `Job`, because `ALTER TABLE` appends and the
  stored-DDL comparison below compares column order. The first draft put them mid-table and the gate
  failed with a real diff — which is the intended reading of a "same DDL" test.
* Invariant under test: a fresh `upgrade head` leaves **zero**
  `compare_metadata` diffs against the models (`compare_type` on, so a Postgres
  `TIMESTAMP` slip cannot hide) and stores DDL identical to `create_all()`'s.
* Both halves of that invariant run on **both dialects** (`tests/test_migrations.py`,
  SQLite by default plus Postgres when `SV_TEST_POSTGRES_URL` is set), and
  `scripts/postgres_e2e.py` goes past the schema: it boots uvicorn against a real
  Postgres server and drives auth, ingestion, cross-org dedupe, per-org routing,
  artifacts and the audit chain over HTTP. Schema equality is not the same claim as
  "the app runs", so it is not tested as if it were.
* The same variable moves **the entire suite**, not just this file: `tests/conftest.py`'s
  `new_database_url()` is the only place a test database is built, so with the variable set
  every test — API fixtures, CLI subprocesses, the live-uvicorn SDK run, the socket-guarded
  offline cases — gets a private database on that server, created for the test and dropped
  with `WITH (FORCE)` afterwards. No test body branches on dialect; if one ever did, the
  Postgres leg would be measuring the branch rather than the product.

## Scaling path

| Concern | Default (single node) | Production scale |
|---|---|---|
| Database | SQLite (WAL) | PostgreSQL via `SV_DATABASE_URL` — schema versioned by Alembic. The suite is **1023 tests** and the last full `postgres:16` pass ran it to **0 skips** at the pre-T48/T49 size of 980; the 43 scope/OIDC cases added since are dialect-agnostic and green on the SQLite leg, so the Postgres figure is pending a re-run on the 1023 tree rather than restated. The same suite reached **0 skips** inside `linux/aarch64` containers on two newer interpreters at the `1.0.0` size of 753, which those two legs have not since been re-run against (`tests/conftest.py`'s `SV_TEST_POSTGRES_URL` seam); connection-pool sizing for a given instance class is still an open operational question |
| Queue | in-process `PriorityQueue` + crash recovery (`SV_JOB_BROKER=embedded`) | `SV_JOB_BROKER=postgres`: the `jobs` table *is* the broker — `FOR UPDATE SKIP LOCKED` claim + lease + fenced terminal write, so N replicas and N worker containers compete over one queue with no Redis, no RabbitMQ and no paid broker. `process_job(db, id)` is the unit of work in both, which is why the swap does not touch the orchestrator. Verified by `make scale` (`docker/compose-scale.yml`, 2 API replicas + 2 workers + 200 jobs, asserted from the database) |
| Rate limits | in-process token bucket (`SV_RATE_LIMIT_BACKEND=in-process`) | `SV_RATE_LIMIT_BACKEND=valkey`: one bucket per subject inside a **Valkey** server (BSD-3 — `redis` is refused as a selector value because Redis ≥ 7.4 is RSALv2/SSPL, so FC-1 survives the swap), drained by a single Lua script that reads the *server's* clock, so `n` replicas cannot each re-derive a full budget and cannot lose an update to a read-modify-write race. The `check(subject) -> (allowed, retry_after)` contract, its semantics and every route handler are unchanged (`AC-INFRA-3(c)`), and an unreachable backend **degrades to the in-process bucket** behind a bounded reach (`SV_RATE_LIMIT_TIMEOUT_SECONDS`) and a re-probe cooldown, so FC-4's offline claim survives. Verified by `make ratelimit` (21 assertions: two OS processes admitting 40 where private buckets admit 80, then a stopped container, a socket that never answers, and a restart) |
| Storage | local content-addressed FS | `SV_MEDIA_STORE=s3` → MinIO/AWS via the same `MediaStore` contract (already implemented) |
| Concurrent ledger writes | `BEGIN IMMEDIATE` on SQLite | `pg_advisory_xact_lock()` on a named key (`synthverify/db.py`'s `AUDIT_CHAIN_LOCK_NAME`) covering the read-head→insert pair, so two committed transactions can never share a parent. `AuditLedger`'s chain is a claim about **concurrency**, not just about content: without the serialisation, 8 threads × 25 appends forked the chain on both dialects (`README.md` §6 item 20) |
| Detectors | heuristic (fast, deterministic) | add ML plugins; run in worker pool; CPU-only stays a gate (FC-2) |
| Webhooks | embedded retry loop | durable queue or event bus |

**The split-node shape, concretely** (`docker/compose-scale.yml`, run by `make scale`):
`postgres` → a one-shot `migrate` (`synthverify db-upgrade`, `service_completed_successfully`) →
`api1` (owns the bootstrap key; everything else waits for it to be healthy) → `api2`, `worker1`,
`worker2`. The API replicas run with `SV_EMBEDDED_WORKER=false`, so an ingest that is accepted by one
replica is completed by a different process — which is the only configuration in which "exactly once"
is a claim about a database rather than about a process. Two consequences are built into the file rather
than left to the operator: `/app/data` is **one shared volume** (the local store's `storage_path` is an
absolute path, so the processing replica must be able to read the bytes the ingesting replica wrote),
and every app container pins its `hostname`, because `jobs.claimed_by` is read back as evidence and a
default hostname is a generated id. The single-node default remains the documented happy path; this is
the same image plus a driver overlay (`docker/Dockerfile.postgres`).

## Reproducible build (`docker/requirements-lock.txt`)

`pyproject.toml` states *what* is needed with ranges; `docker/requirements-lock.txt` states *which version*
each member of that set resolves to, and is consumed as a **constraints** file
(`pip install -c docker/requirements-lock.txt …`) by the image, by `make setup` and by every CI leg. That
choice is what lets one file serve three different install profiles: an entry the requested extra does not
pull in is inert, so the `dev` pins never interfere with the image's `core,vision` set, while a matched
constraint is a fact. `-r` would have made the lock a second statement of intent next to `pyproject.toml`,
and the two would drift.

Two properties of that design are load-bearing rather than incidental:

* **The check compares installed metadata, not a PyPI re-resolution**, so it needs no network and inherits
  one obligation: it must run where every declared extra is installed, because an extra nobody installed has
  a closure nobody can read. `synthverify dependency-lock` therefore reports both sides — the pins and the
  per-group counts of what was considered — and fails in each direction: a closure member with no pin (a
  dependency added without regenerating the file), a pin no dependency wants any more, and a pin whose
  version differs from the installed one.
* **A regenerated lock must come from an environment pip resolved, not one edited by hand.** The first
  version of this file pinned `numpy==1.26.4` beside an `opencv-python` whose own metadata demands
  `numpy>=2`; nothing in the hand-migrated venv complained, and the image build was what said so. A
  virtualenv created with `--system-site-packages` can silently inherit a second copy of a package, which is
  why the gate's numbers are quoted per platform and `scripts/lock_e2e.py` measures the artifact rather than
  the host.

One pin is in the file for the image and not for the machine that wrote it: `greenlet`, which SQLAlchemy
requires only on the platforms in its `platform_machine` marker. A darwin host walk cannot discover it, so it
is declared in `dependency_lock.TARGET_PLATFORM_PINS` with its reason, and `scripts/lock_e2e.py` checks it
both ways — present in the built image at that version (or the exemption is unearned) and absent from the
host scan (or the image-side run is theatre). `docs/goal-spec.md` §6.1's "not claimed" bullet records what
this still does not pin: the base image tag, and therefore the interpreter's own `pip`/`setuptools`/`wheel`
and `-c`'s inability to reach a PEP 517 isolated build environment.

## Freedom gates (`synthverify/compliance/`)

Three constraints decide whether this product is *allowed* to ship, and each is a
command rather than a review comment:

| Gate | Command | What it refuses |
|---|---|---|
| FC-1 licences | `synthverify licenses` | any dependency (transitively, from `pyproject.toml`) that is not permissively licensed - and anything whose licence cannot be identified, which fails **closed**. SPDX `OR` needs one branch, `AND` needs all; ambiguous short ids resolve to the most restrictive reading. The walk is offline (installed metadata, no resolver) and **PEP 508-marker-aware**: a `Requires-Dist` line is only in scope if its marker selects this interpreter and the requested extras, which is why the report prints how many lines it did *not* follow and why `--groups core,vision` exists to run FC-1 on a platform other than the one holding the checkout |
| FC-3 model manifests | `synthverify model-manifests` | an ML detector whose **weights or training-data licence** is not permissive, whose weights are gated, whose runtime needs a GPU, or whose eval report lacks a *committed* gate per measured metric (`auc` gate at/below chance is rejected) and per-demographic-group error rates. The registry cross-check is unconditional, so CI cannot skip it by forgetting a flag |
| FC-4 offline (in-process) | `pytest -m offline` | a pipeline that reaches the network: outbound `socket` calls are refused at the syscall boundary while ingest→verdict, sync analyze, worker runs and the CLI all complete (loopback stays open, so the guard cannot pass vacuously) |
| FC-4 offline (sealed) | `make airgap` | the same verdict cycle with **no network namespace at all**: a `--network none` container generates the fixture and runs `cli analyze`, and the script fails if egress was actually possible or if the verdict was not risk-bearing |
| Lock consistency (T39) | `synthverify dependency-lock`, `make lock-e2e` | a `docker/requirements-lock.txt` that disagrees with `pyproject.toml`'s declared closure in either direction, a lock line that is not a `name==version` pin, and - in the e2e form - a built image whose `pip freeze` does not equal the pins or whose two cold builds do not equal each other |

`make freedom` runs FC-1, FC-3 and the lock check (`synthverify freedom` chains all three) plus the
offline tests; `make airgap` adds the container-level proof; `make lock-e2e` adds the container-level
proof for the version set.
`scripts/airgap.sh` is the single implementation that CI's `docker` job and the local
target both invoke, so the two cannot drift. FC-3 is why `synthverify/models/README.md`
exists: the licence of a training corpus cannot be settled by reading code, so it is
settled by a machine-checkable claim next to it.

*The commands above are the product's console form. On a checkout whose venv has
unusable script shebangs, run them as `./.venv/bin/python -m synthverify.cli …` and
`./.venv/bin/python -m pytest …` (see README §2.5).*

## Verification & observability

* `/metrics` — Prometheus counters/gauges (jobs by tier, HTTP by route/status, auth outcomes, queue metrics,
  `synthverify_jobs_fenced_total` for writes that lost their lease, and
  `synthverify_rate_limit_fallback_total{backend=…}` for requests served by the local bucket while a shared
  limiter was unreachable — degradation is an event that gets counted, not a state that gets hidden).
  `synthverify_metrics_exemplars_dropped_total` counts exemplars this build *refused*, so a caller cannot
  discover by trial and error that a label value was rejected. The endpoint negotiates: the historical
  text format 0.0.4 is the default and carries no exemplars — that format has no syntax for them — while
  `Accept: application/openmetrics-text` returns OpenMetrics 1.0.0 with exemplars and the `# EOF`
  terminator the format requires. `Vary: Accept` is set because two different bodies answer one URL.
* `/healthz` (liveness) and `/readyz` for orchestrators. `/readyz` is what makes a deployment topology
  inspectable rather than inferred: `job_broker`, `durable_queue`, `embedded_workers` and `queue_depth`
  are read from the live broker and fleet, so "are these replicas worker-less?" is one `curl` — and it is
  the check `scripts/scale_e2e.py` fails when an operator points the stack at `SV_JOB_BROKER=embedded`.
  `rate_limit_backend` and `rate_limit_degraded` do the same for the limiter: the first is what was
  *configured*, the second is what is *happening*, and read together they answer "am I actually sharing a
  budget?" without the probe ever contacting the backend whose outage it would report.
* `GET /api/v1/admin/audit/verify` — recomputes the whole chain; any edit,
  deletion or reordering of history is reported with the offending `seq`. A database written
  concurrently *before* the ledger serialisation above can legitimately report a fork at a historical
  `seq`: that is the detector working, not a new defect, and no history is rewritten to hide it.
* The scale run is asserted **from the database**, not from responses: `scripts/scale_e2e.py` reads the
  `jobs` and `audit_events` tables with a BSD driver (`pg8000`, host-side, verification-only per
  `docs/goal-spec.md` §6.1 note 4) while the product writes through `psycopg` inside the containers. That
  split is deliberate — it is the note exercised rather than asserted, and the run also proves the shipped
  image cannot reach Postgres at all until the `Dockerfile.postgres` overlay adds the driver.

### Trace correlation (`synthverify/tracing.py`) — `REQ-INFRA-6`

One 128-bit id ties a request to everything it caused, and it is chosen to need no service:

* **Inbound is validated or replaced, never echoed.** `parse_traceparent` implements W3C trace-context
  exactly: version `00`–`fe` (`ff` is reserved), 32 lowercase hex trace id (all-zero rejected), 16 hex
  span id (all-zero rejected), 2 hex flags with bit 0 as `sampled`, unknown *future* versions tolerated
  for their extra fields, and the 560-byte carrier limit — the minimum the spec requires a vendor to
  accept — enforced before parsing. Anything else is
  discarded and a fresh id is minted, so the only values that can reach a Prometheus label, a database
  column or a log line are ones this code produced. `TraceIdFilter` then puts `sv_trace_id` on every
  record, which is why the format string can reference a field that may be unset.
* **The scope is a contextvar**, so it follows a request through `await` but deliberately *not* across a
  thread or process boundary. That is the reason `jobs.trace_id` exists: the ingest request writes the id
  into the row, and `process_job` binds it again in whichever worker — same thread pool, another replica,
  or the `synthverify worker` process — so the log line and the ledger rows a job produces carry the id of
  the request that created it. Nothing propagates a caller's header to a webhook target.
* **Exemplars, not spans.** `exemplar_labels()` returns `{trace_id, span_id}` for the current scope and
  `metrics.inc(..., exemplar=…)` attaches them to the *sample* (an OpenMetrics exemplar has no value of its
  own), which is what lets a Prometheus query point at one real request. Parent/child relationships are
  not modelled — there is no span tree, no collector, no OTLP and no vendor SDK anywhere in the path
  (`FC-5`). A hop's own span id is what lands in the exemplar, so the link identifies work *this* replica
  did rather than a caller's.
* **The ledger certifies the correlation.** `audit_events.trace_id` is part of the entry hash *only when
  set*, which keeps chains written before `0004` verifiable (the exact pre-`0004` digest is pinned as a
  literal in `tests/test_tracing.py` rather than recomputed by the code under test), and editing a stored
  id breaks the chain at that `seq`. `--mutate tampered-trace` in the e2e gate exists to prove that.
* **The alert rules are a repo artifact, checked two ways.** `synthverify alert-rules`
  (`synthverify/compliance/alert_rules.py`) parses `docker/prometheus-alerts.yml`, strips label
  matchers, range vectors, grouping and offsets from each `expr` before deciding what is a metric name,
  and fails if any name it references is not one this build declares. `scripts/trace_e2e.py` then does
  the converse against a *live* scrape: every name in the exposition must be declared, and every name the
  rules reference that this run's topology should produce must be present — so a metric renamed under the
  rules file is caught even though the offline parse still passes. Neither proves the rules fire: no
  Prometheus is run here (`docker/` ships none), which is stated in README §7 rather than glossed.
