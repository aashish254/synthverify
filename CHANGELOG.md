# Changelog

All notable changes to SynthVerify are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file is generated from the work, not from intent: every entry below names the command that
grades the claim it makes. Where a claim is measured on one machine and gated on another, the entry
says which.

## [Unreleased]

Nothing is queued ahead of `1.0.1` *for this release*: the requirement work through `1.0.0` is done, and what
remains below is open work, not a gate this version has skipped. The open items are tracked in
[`docs/goal-spec.md`](docs/goal-spec.md) §9 and in `TODO.md`:

- `REQ-IDAM-2` — scope-granted credentials (`jobs:read`, `media:submit`, …) alongside roles, with a
  scope-matrix test (`TODO.md` T48).
- `REQ-IDAM-1` — JWT / OIDC subject tokens. This adds the first new runtime dependency since the licence gate
  was written, so it is an FC-1 decision as well as a feature (`TODO.md` T49).
- `TODO.md` **T50** — a launch-proof pass and a `docs/OPERATIONS.md` runbook. It also owns the 21 informational
  mypy findings, which gate nothing today because `make typecheck` ends in `|| true`.
- `OQ-1` … `OQ-6` — open questions that are operator weightings, not code.

## [1.0.0] — 2026-09-27

First public release. The whole of it is runnable from a clone with `make setup && make test`;
`docs/INSTALL.md` covers macOS, Linux, Windows and a container with no Python toolchain.

### Added

- **Detector engine** — 11 forensic detectors behind a plugin registry (metadata, ELA, noise
  residual, FFT/frequency, CDF, audio spectrum, text stylometry, and the vision extras that
  `opencv`/`scipy` unlock), each self-reporting coverage so a missing dependency degrades the
  verdict's confidence rather than failing the request.
- **XAI fusion** — per-detector contributions, named flags, a recommended workflow action
  (`PROCEED` / `MANUAL_REVIEW` / `ESCALATE` / `BLOCK`) and a plain-language summary, all in one
  versioned response envelope (`synthverify.report/v1`).
- **Async pipeline** — `POST /api/v1/media/ingest` with webhook or poll completion, retries with
  backoff, per-organisation policy profiles, and a worker tier that runs either embedded in the API
  process or as a separate deployment against Postgres.
- **Durable queue on Postgres** (`REQ-INFRA-2`) — `SKIP LOCKED` claims, leases, exactly-once
  processing proven across two concurrent consumers over 200 jobs; `make scale` runs it on real
  containers and `make scale-mutations` proves the gate can see a double-processing (`TODO.md` T36,
  T37).
- **Shared rate limiter on Valkey** (`REQ-INFRA-3`) — one token bucket per subject across separate
  OS processes, failing closed to the in-process bucket when Valkey dies; `make ratelimit` plus
  3 mutation runs (`TODO.md` T38).
- **Tamper-evident audit ledger** (`REQ-IDAM-3`, `REQ-IDAM-4`) — hash-chained events with sealed
  checkpoints; verification walks every row and compares four per-seal invariants, a range count
  and a tail guard. `GET /api/v1/admin/audit/verify` streams one million events in seconds using a
  fixed amount of memory: `make ledger`, `make ledger-postgres`, and `make ledger-mutations` for
  the eleven ways that claim can be faked (`TODO.md` T44, T47).
- **Tenant isolation** — every resource scoped by the credential's organisation, with a `403` /
  `404` split chosen so the status code is not an existence oracle. Graded by a 48-cell matrix plus
  6 mutation runs (`make tenancy`, `make tenancy-mutations`).
- **Retention** (`REQ-INFRA-5`) — per-organisation TTLs, legal holds that pin rows, storage objects
  and artifacts deleted in the same sweep, and a bucket that refuses deletion reported rather than
  swallowed. `make retention`, `make retention-mutations` (13) (`TODO.md` T46).
- **Observability** (`REQ-INFRA-6`) — `/metrics`, one trace id followed across two OS processes into
  its metric exemplar, its audit rows and its worker's log line, and a shipped Prometheus rule file
  that a build-time gate checks against the metrics this code actually declares. `make alerts`,
  `make trace`, `make trace-mutations`.
- **Dashboard** — operator console at `/dashboard`: queue depth, jobs, verdicts, artifact rendering
  (ELA heatmaps), audit verification.
- **SDK** (`synthverify.client.SynthVerifyClient`) and a CLI whose `analyze` subcommand judges a
  file with no server and no database.
- **Migrations** (`REQ-INFRA-1`) — Alembic revisions that converge on a pre-Alembic database in
  place; `make migrate`, and the Postgres half verified by `make postgres-e2e`.
- **Reproducible image** (`TODO.md` T39) — `docker/requirements-lock.txt` is checked against
  `pyproject.toml` in both directions, and two `--no-cache` builds must print byte-identical
  `pip freeze`: `make lock-check`, `make lock-e2e`, `make lock-mutations` (4).
- **Freedom gates** (`FC-1`, `FC-3`, `FC-4`) — a permissive-only dependency licence scan that treats
  an unreadable root as a violation, a model-manifest gate (open weights, open data, per-group error
  rates), and an offline proof run twice: at the syscall boundary and inside a container with no
  route off the host. `make freedom`, `make airgap`.
- **`make doctor` and `make help`** — `doctor` names what a first-time environment is missing
  (which extra, which service) instead of leaving fourteen gate failures to be interpreted.

### Fixed, in the release pass

These eleven came out of running the install path the way a stranger would — clone, bare
interpreter, `make setup`, `make test` — and, for the last seven, of doing something a test suite does
not do: grepping the handlers instead of exercising them, re-running a gate instead of inheriting its
number, taking a screenshot for this README, installing the interpreter the metadata advertises,
running the whole suite on an architecture it had never run on, following a documentation link to
its target, and re-running the four suite legs on the final tree rather than quoting the first pass
through it. The pattern is worth naming: **a green suite had inherited the premise of each one from the
code the test was meant to check** — which is how `1.0.0` can be the
release where this is fixed rather than the release that shipped it. The precedent is `AC-IDAM-3`
(`TODO.md` T44), where enumerating the routes from the OpenAPI document rather than from the test
file found **five** live cross-tenant leaks no previously-green test could see, one of them on the
path where the product *succeeds*:

- **`make setup` installed an incomplete environment.** It pulled `.[dev]` only, while FC-1 and the
  lock gate grade *every* declared root and fail closed on a root they cannot read. A clean clone
  therefore reported 14 failures out of the box, and CI could not see it because CI installs all
  three extras. `setup` now installs `.[vision,valkey,dev]`, and
  `tests/test_release_hygiene.py` pins that the Makefile, `pyproject.toml` and CI name the same set
  so an added extra cannot silently reopen the hole.
- **`--system-site-packages` is gone from the installer.** It was what let the incomplete install
  look healthy locally: a Homebrew-installed `scipy` satisfied a pin the lock never described.
  `make doctor` now fails an environment whose venv inherits site-packages, because that
  environment cannot be reproduced on another machine.
- **The shipped image could not serve its own documented rate-limit backend.**
  `docker/docker-compose.yml` names `SV_RATE_LIMIT_BACKEND=valkey` as the scale-out path, but the
  image installed `.[vision]`; the client is imported lazily by design, so the setting raised at
  limiter construction. The image now installs `.[vision,valkey]` and a hygiene test pins that it
  carries the runtime extras and not the test toolchain.
- **The first-boot admin key was briefly world-readable.** `write_text()` followed by `chmod()`
  left a window in which a full admin credential sat on disk at the process umask - `0644` on a
  default host. The mode is now supplied to `open()` and pinned with `fchmod`, and four tests cover
  the resulting behaviour: owner-only under both a permissive and a restrictive umask, content
  equal to the key the database was handed, no second mint on a later boot, and an
  operator-supplied `SV_BOOTSTRAP_ADMIN_KEY` never copied to disk at all.
- **Every long-running request handler occupied the event loop.** `GET /api/v1/admin/audit/verify`
  was an `async def` over a synchronous SQLAlchemy session, so the 11–16 s the ledger walk takes at
  1 M events was time the process could not answer *any* other request — including `/healthz`, which
  in the shipped single-container shape is a failing liveness probe, a restarting container and a
  blank dashboard while one admin clicks one button. It was found by grepping rather than by a
  red test: 40 `async def` handlers with four `await` sites between them and no `asyncio` call
  anywhere in `synthverify/`. The seven endpoints whose work is bounded by data volume or by an
  outbound call — `/media/ingest`, `/media/ingest/batch`, `/media/analyze`,
  `/jobs/{job_id}/reanalyze`, `/admin/webhooks/{webhook_id}/test`, `/admin/audit/verify`,
  `/admin/retention/sweep` — are plain `def` now, so Starlette dispatches them to its thread pool
  and the request holds a worker thread instead of the replica. `tests/test_event_loop.py` (13
  cases) holds each endpoint's work step open with a blocking stand-in and probes `/healthz` while it
  is held, plus a structural check that all seven are non-coroutines. Its own control carries the
  measurement: a 1.0 s held work step stalls a concurrent request for **1 005 ms** and **1 007 ms**
  behind `async def` and **3.6 ms** / **3.3 ms** behind `def` — and the first shape asserts the raise
  *"the loop was occupied"*, because a probe issued against a frozen loop appears to answer in a
  millisecond the moment it unfreezes.
- **`make postgres-e2e` had been crashing since `1.0.0` was tagged, and printed nothing while doing
  it.** It asserted that uploading the same bytes as two organisations yields *one* `MediaAsset`
  row — `session.scalar_one()` over a query written before `TODO.md` T44 changed the contract to
  per-digest **and** per-organisation — so it raised `MultipleResultsFound` on the dedup check
  before any check could report. Re-running the identical script against a pre-T51 clone reproduced
  the crash exactly, which is what separates it from the change that appeared to cause it. It now
  asserts both halves of the T44 contract: two rows with their own organisation and filename, and one
  stored object behind them (`16 checks passed, 0 failed`). A gate that dies before its first
  `check()` is indistinguishable from one that never runs, and the Makefile target had been exiting
  non-zero on a red test suite long enough that nobody read it.
- **The console's Action column could never have rendered a value.** The queue table read
  `j.result.verdict.recommended_action`, but `GET /api/v1/jobs` serialises rows with
  `include_result=False` — the report is deliberately not on the wire for a list, and the detail
  panel one click away was reading the same verdict from a payload that *does* carry it. So every
  row printed an em dash under a header promising an action, on a screen that had passed review
  several times because review looked at the numbers in the other six columns. Nothing could have
  caught this as tests stood: the API tests only ever asserted the detail shape, and the console is
  static HTML with no test that renders it. Fixed at the serializer rather than by inlining reports
  into a list — `Job.to_dict` now derives `recommended_action` from the stored verdict, which costs
  no extra query because `list_jobs` already loads each row whole — and the table reads the flat
  key. Three cases in `tests/test_api.py::TestJobQueueSummary` pin both ends: the list value must
  equal the detail value, the key must exist as `null` for a job with no report yet so the row shape
  never varies by status, and the shipped console source must not read `j.result` for it. Each was
  checked by undoing its own fix and watching the matching case fail.
- **`requires-python` promised Python 3.10, and the package cannot be imported on 3.10.** The
  metadata declared `>=3.10` while `synthverify/db.py` imports `enum.StrEnum`, which arrived in 3.11,
  so `pip install` on 3.10 *succeeds* — the declared floor is what pip checks, not what the code
  imports — and the first `import synthverify` raises. Measured on `python:3.10-slim`:
  `ImportError: cannot import name 'StrEnum' from 'enum'`. The floor is now 3.11 in
  `pyproject.toml`, in the interpreter probe `make setup` runs, and in both documents a reader
  decides from, and the `3.10` classifier is gone. One hygiene test pins all of those to the same
  number and refuses any `Programming Language :: Python :: 3.x` below it, because the classifier is
  where an unsupported version advertises itself to a resolver.
- **Three assertions and one test double only worked on the machine that wrote them**, found by
  running the whole
  suite on `linux/aarch64` under CPython 3.12 and 3.13 — the two interpreters the metadata claims to
  support and that had never executed it:
  - `tests/test_dependency_lock.py` asserted that deleting the exempt `greenlet` pin produces exactly
    one finding, `["unpinned"]`. That is true on Apple Silicon, where SQLAlchemy's
    `platform_machine` marker keeps `greenlet` out of the closure, and false on Linux and Windows,
    where the same edit legitimately produces two. The assertion now names *which package* is
    complained about instead of *how many complaints* there are, so both hosts pass and neither
    claim is looser than before.
  - `tests/test_freedom_licenses.py` asserted a `python_version` marker's outcome against a literal
    version, which inverts on 3.13: the case that meant to grade a passing marker failed with
    `assert False is True`. It is now written relative to `sys.version_info`, like the platform case
    beside it.
  - The in-test S3 mock logged each request *after* flushing its response, so a test that read the
    request log as soon as its own call returned was racing the handler thread — which is why
    `HEAD` of a missing key produced `IndexError: list index out of range` on a container and passed
    on the host. It was a race, not a dialect difference: the container's scheduler simply won the
    write-versus-log order often enough to expose it. Every `_record` now happens before the flush. The fix is witnessed by
    injecting a 0.5 s delay into `_record`: **all 61 cases in the file pass** in that shape, while
    the previous ordering fails **14** of them, including the one that flaked. (Both mutations were
    run, and `check_lock`'s two greenlet reports were each silenced in turn to confirm the rewritten
    assertion still catches the defect it guards.)

- **`SECURITY.md` pointed at two documents a reader cannot reach.** Its cross-references were written
  as `](architecture.md)` and `](../README.md)` - correct on the tree their author was editing, 404
  from the repository root where a hosted renderer resolves them. Found by the guard written for the
  class, `test_no_shipped_document_links_to_a_path_the_reader_does_not_have`, which walked this
  repository before anything was fixed and printed exactly those two; it now checks every shipped
  Markdown file - README, `docs/`, the policies, the issue templates - for a relative target that is
  not there. Anchors are deliberately exempt, because a wrong anchor still lands the reader on the
  right page. **The guard then found a second defect, in itself:** its first full-suite run failed on
  *this file's* entry about the bug, because quoting `](../README.md)` in prose is indistinguishable
  from linking it to a checker that ignores Markdown's code spans. It now parses like a renderer -
  fenced blocks and backticked spans are not links - which is also why it can describe the syntax it
  detects. The fix was mutation-checked the other way too: a dangling `](missing-page.md)` appended to
  `docs/architecture.md` makes the case fail with exactly that string, and removing it turns the class
  green again. **The class is the finding, not the two links:** a document is an interface, and not one
  of the suite's tests had ever resolved a link through it.

- **The tree that was measured was not the tree that ships - and the fix is a second measurement, not a
  sentence.** Four legs ran, their numbers were substituted into the README, `TODO.md` and
  `docs/goal-spec.md`, and *then* those documents were edited. That is this release's own defect class applied
  to its own bookkeeping: a shipped document is a gated artefact, and `tests/test_release_hygiene.py` reads
  live files. The guards - the only tests anywhere that read prose - were re-executed after the last edit
  (**21 passed**, 0.5 s), and re-run again as the final action before the tree was frozen, which is the only
  order in which that claim means anything. A full SQLite pass on the edited tree (**753 collected, 732 passed,
  21 skipped, 0 failures**, 165.6 s) is recorded too, but not as the witness for the prose: it overlapped the
  last of these edits, and saying so is the point. What it does settle is that a documentation sweep cannot break
  the suite. The Postgres and container legs were not re-run for prose, on evidence rather than assumption:
  `find` for anything modified after the confirmation pass began returns four `.md` files and nothing else - no
  `.py`, no `pyproject.toml`, no `Makefile`, no workflow - so the code they certified is byte-for-byte the code
  that uploads. **The repeat bought a correction, not just a confirmation.** The earlier
  Postgres pass caught the two-database overlap with a 20 ms `pg_database` sampler
  (`peak_alive_at_once=2`); this one caught none of it (`peak_alive_at_once=1`, and no `2` bucket in the
  histogram at all). So even at 20 ms a peak is a *lower* bound - the poll has to land inside a window that is
  milliseconds wide - and the second-database fixtures now rest on the source that opens them
  (`tests/conftest.py:115`, `tests/test_brokers.py:142`) rather than on a sampler that can miss them. Every
  count agreed across the two passes while all four timings moved: 156.2 → 157.7 s on SQLite, 302.7 → 235.9 s
  on Postgres, and the two container legs **swapped order** (207.6 s / 250.2 s became 218.8 s / 215.6 s), which
  is the concrete reason no sentence in this repository claims one interpreter is faster than another. The
  distinct-database count moved too (252 → 253) at an identical collected total, so the README now says what it
  really tracks: which fixtures open a second database in a given run, not how many tests ran.

### Known limitations, stated here as well as in the README

- **The detectors are signal-statistical, not learned.** No model manifest has been registered
  (`FC-3` has nothing to grade: 0 manifests), so a fabrication that defeats the implemented
  statistics is reported as authentic. `list-detectors` prints what actually ran, and every verdict
  carries its coverage and confidence for that reason.
- **The hosted CI has never run.** Every number in this repository came from the commands being
  executed on one machine. `windows-latest`, `macos-latest` and Python 3.12/3.13 are carried as CI
  legs; 3.12 and 3.13 have also been run locally in containers. A Windows run has not.
- **A long `audit/verify` holds a request slot** for seconds on a large ledger. It is an admin
  endpoint, it is not a health check, and since T51 it is not a loop blocker either — it occupies a
  worker thread while the replica keeps answering; see [`SECURITY.md`](SECURITY.md).
- **Verification is only as strong as where the database lives.** An operator with read-write SQL
  access can produce a ledger that agrees with itself; `SECURITY.md` and `docs/architecture.md`
  state the boundary rather than claiming past it.

[Unreleased]: https://github.com/aashish254/synthverify/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/aashish254/synthverify/releases/tag/v1.0.0
