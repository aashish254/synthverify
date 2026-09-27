# Security

SynthVerify is an evidence-handling system: its value is that a verdict can be traced to named
forensic evidence and that the audit trail behind it cannot be quietly rewritten. Threats here are
therefore mostly about *trust boundaries between tenants* and *tamper evidence*, not about taking a
service offline.

## Reporting a vulnerability

Use **private vulnerability reporting** on this repository (Security → Report a vulnerability). That
keeps the issue out of public issues while it is still exploitable. If private reporting is
unavailable, open an issue titled `security: <one line>` with **no** reproduction steps in the body
and wait for a maintainer to convert it to a private advisory.

What to include, in rough order of usefulness: the endpoint or CLI command, the configuration you
ran it under (`SV_ENVIRONMENT`, `SV_JOB_BROKER`, `SV_RATE_LIMIT_BACKEND`), the tenant/scope
combination if it involves more than one organisation, and the smallest request sequence that
reproduces it. Expect an acknowledgement within a week; this is an MIT-licensed project maintained
without a security budget, and that is stated rather than promised around.

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | yes — fixes land here while they are current |
| < 1.0 | no public support; the pre-1.0 tree had no migration contract |

## What the code promises, and where each promise is checked

A claim without a test is a comment, so every row below names the command that grades it. If you
find a way to satisfy a check while violating the promise, that is the bug — report it.

| Promise | Mechanism | Graded by |
|---|---|---|
| One tenant cannot read, mutate, or infer the existence of another tenant's resources | Every query is scoped by the credential's organisation; a cross-tenant reference on a *known* id answers `403`, an unknown id `404`, so the status code is not an existence oracle | `make tenancy` (48-cell matrix), `make tenancy-mutations` (6 ways to fake it) |
| The admin control plane is not reachable with a data-plane credential | `ApiKey.platform_scope` gates `/api/v1/admin/**`; a non-platform admin key is refused with a message naming `platform_scope` | the same matrix |
| The audit ledger cannot be edited, reordered, truncated or re-hashed without detection | Hash-chained entries plus sealed checkpoints; verification walks **every** row and compares four independent per-seal invariants, a range count, and a tail guard | `make ledger`, `make ledger-postgres`, `make ledger-mutations` (11 removals of that enforcement) |
| Rate limiting degrades to a *smaller* allowance, never an unlimited one, when its backend dies | The shared Valkey bucket falls back to the in-process bucket and flips `rate_limit_degraded`; unknown backend names raise at startup | `make ratelimit`, `make ratelimit-mutations` (3) |
| Analysis performs no network egress | No HTTP client is reachable from the detector path; FC-4 is proven twice — at the syscall boundary and inside a container with no route off the host | `make airgap`, `make freedom` |
| Every dependency is permissively licensed | FC-1 grades installed metadata for each declared root and treats an unreadable root as a violation | `make licenses` |
| The image you run is the image we pinned | The lock is checked against `pyproject.toml` in both directions, and two `--no-cache` builds must print byte-identical `pip freeze` | `make lock-check`, `make lock-e2e`, `make lock-mutations` (4) |
| Schema upgrades do not rewrite history | Alembic revisions converge on a pre-Alembic database in place; `upgrade head` must leave zero `compare_metadata` diffs | `make test` (`tests/test_migrations.py`), `make postgres-e2e` |
| Retention is enforced by policy, and refuses when it is wrong | Per-organisation TTLs, legal holds pin rows, a bucket that refuses deletion is reported rather than swallowed, and a dry run must not delete | `make retention`, `make retention-mutations` (13) |

## Boundaries that are documented, not fixed

**A writer who can rewrite the whole ledger can rewrite the ledger.** Someone with read-write SQL
access to the audit database can re-link a chain tail, re-point every checkpoint *and* correct the
recorded range counts; at that point every commitment stored inside the database they control agrees
with itself. `scripts/ledger_bench.py` asserts on every run that this case still verifies, and the
control that actually protects you is **where the database is** — one that your application
credential can write to is a matter of record-keeping, not of evidence. For evidentiary use, ship the
sealed checkpoints to a store the application cannot write to. The same reasoning is in
[`docs/architecture.md`](docs/architecture.md) under "The boundary, stated not assumed".

**A verdict is a forensic signal, not a fact.** The detectors report signal statistics and metadata
anomalies with per-detector contributions; on a file that defeats every implemented detector, the
correct and the fabricated answer both come out `PROCEED`. Never make an irreversible decision on a
single low-confidence report — the `conclusive`, `detector_coverage` and per-evidence fields exist so
downstream policy can require a human. See [`README.md` — Known limitations](README.md#known-limitations-documented-accepted-for-v1).

**`GET /api/v1/admin/audit/verify` is a long request, not a fast one.** Verifying one million
chained events takes seconds and a fixed amount of memory (measured: 11.4 s at 65.5 MiB on SQLite,
16.4 s at 69.3 MiB on `postgres:16`). Put it behind the admin gate and expect it to hold a request
slot for that long. It no longer stalls the process: the handler runs on Starlette's thread pool
(plain `def` since T51), so `/healthz` and the dashboard keep answering while a walk is running —
which is why it is safe to leave a liveness probe pointed at the same replica. For a ledger in the
millions the CLI path, `synthverify audit-verify`, is the one to schedule.

## Configuration that a public deployment must change

| Setting | Default | Why it matters |
|---|---|---|
| `SV_BOOTSTRAP_ADMIN_KEY` | unset → a key is generated and written to `data/bootstrap_admin_key.txt` (`0600`), and its value is logged **once**, at generation | Set this in any environment whose filesystem or log aggregation is shared. The generated key is a full admin credential |
| `SV_ENVIRONMENT` | `development` | Production mode turns off the conveniences that read a key from disk |
| `SV_DATABASE_URL` | SQLite under `data/` | SQLite is a single-writer file: fine for one process, wrong for replicas. Use Postgres before scaling out |
| `SV_JOB_BROKER` | `embedded` | `postgres` is what makes claims shared across processes; embedded workers hold their own queue and cannot be audited across replicas |
| TLS | not terminated here | The API key travels in a header. Terminate TLS in front of the service; nothing in this repository expects to own a certificate |
| `/dashboard` | served by the same process | It is an operator console. Do not expose it to the public internet unauthenticated |

## Scope

**In scope:** the API, the CLI, the worker/scheduler tiers, the dashboard, the audit chain and its
verification, tenant isolation, the rate limiter, the retention sweep, the containers, the lock.

**Out of scope:** the accuracy of a verdict on a specific file (that is a research question, tracked
as a limitation rather than a vulnerability), denial-of-service against a hosted instance, and any
deployment where the operator has write access to the audit database.
