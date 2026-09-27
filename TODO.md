# TODO — execution checklist for [`docs/goal-spec.md`](docs/goal-spec.md)

Protocol: pick the topmost unchecked task → implement → **verify with a real terminal run** →
mark `- [x]`. No task is checked without its acceptance command having passed. Push/remote
operations stay user-gated.

**Current gate (measured twice on 2026-09-27 during the release pass T52 — the second time on the tree that
ships — on the pinned package set, at four interpreter/architecture combinations):**
`./.venv/bin/python -m pytest tests/` = **753 tests: 732 passed, 21 skipped** in 157.7 s (junit
`tests=753 failures=0 errors=0 skipped=21`; the pass an hour earlier, at the same size, read 156.2 s), every
skip printed with its reason — **16** need
`SV_TEST_POSTGRES_URL` (5 Postgres halves of `AC-INFRA-1`, 8 `SKIP LOCKED` claim/lease cases, 3
advisory-lock-and-cross-connection-wait cases) and **5** need `SV_TEST_VALKEY` · the *same* command with both
set = **753 tests: 753 passed, 0 skipped** against a real `postgres:16` (server 16.15) **and**
`valkey/valkey:8`, in 235.9 s (junit; the earlier pass read 302.7 s), under the `pg_database` sampler —
**9 858 samples at 20 ms over 237.7 s, 253 distinct `svtest_*` databases, peak 1 alive at once, 0 surviving
after the run**, where the earlier pass printed 12 734 samples / 305.0 s / 252 distinct / **peak 2**. A
measurement travels with the conditions that made it *and* with the interval that resolved it (interpretation
10): a `peak_alive_at_once` is a lower bound even at 20 ms, which this pair proved by disagreeing about it. The
two fixtures that can hold a second database are therefore known from `tests/conftest.py:115` and
`tests/test_brokers.py:142`, not from the sampler; what both passes establish identically is the part the claim
needs — a couple live at most, and `svtest%` count **0** afterwards on both. That sampler
needed fixing before its numbers meant anything: its first pass printed `distinct=0` *beside a plausible
histogram* while its own polling closure raised on every iteration, and the instrument now exits non-zero if
any poll raises, which is why `sampler_errors=0` is part of the figure · the same 753, twice more, in
`python:3.12` (218.8 s) and `python:3.13` (215.6 s) containers on `linux/aarch64` — the earlier pass read
207.6 s and 250.2 s, **in the other order**, which is the reason nothing here ranks the two interpreters —
**0 failures, 0 errors, 0
skipped** on each, against the same two services over `--network host`, with the *current* tree mirrored into
`/src` rather than the image's baked copy. Those two legs found three machine-specific assertions; see T52. ·
`ruff check synthverify tests scripts` = **clean**, `make lint && make freedom` = **exit 0**, `make doctor` =
**exit 0** (two `[warn]`s, both about a service variable being unset) ·
`licenses` = **RESULT: PASS** (**47 packages from 20 declared roots**, roots `core, vision, valkey, dev,
build-system`; T39 added `packaging` to core and moved `setuptools`/`wheel` into `dev` so the gate
environment can grade them; **T44, T46, T47 and the release pass added no dependency at all**, which is what
"the identity half, the retention half and the release half cost nothing to licence" looks like in this
column) · `model-manifests` = **PASS**, idle · `dependency-lock` = **PASS** (48 pins; the one pin the host
closure cannot see is `greenlet==3.5.6`, which exists only on the shipping platform — a lock assertion that
hard-coded *which* of the two comparisons catches it passed here and failed on 3.12, so it is now
name-based and either-kind) · `alembic heads` = **`0007` (head)**, one line of descent — this block said
`0006` until the release pass re-ran the command rather than re-reading itself ·
`synthverify alert-rules` = **PASS** (**14 rules in 3 groups, 14 names referenced, all among the 20 the
build declares, 0 issues** — 13 are T41's, the 14th is T46's retention-sweep failure rule) ·
`pytest -m offline` = **12 passed, 0 skipped** (junit, 4.2 s) ·
`make postgres-e2e` = **16 checks passed, 0 failed** — one more than this block has carried, because T52 found
the script *crashing* since the tag on an assertion the T44 contract change made stale ·
`.github/workflows/ci.yml` **parses** into **twelve** jobs — `test / portability / freedom / migrations /
scale / limiter / tenancy / retention / ledger / tracing / docker / reproducible-image` — the `tenancy` and
`retention` jobs each carrying their gate, their mutations and a residue assertion, and `portability` being
the 2×3 macOS/Windows × 3.11/3.12/3.13 matrix the two container legs stand in for locally ·
`data/synthverify.db` migrated `0003` → … → `0007` in place and `synthverify audit-verify` prints
**`VERIFIED 3 audit entries in 0.0 s; 1 checkpoint(s) cross-checked;
head_hash=dbf5ab4ccce5ba0d84929fa8244f2933536585b6cf6a2df719d4ba6c11d5ddaa`**, so the pre-migration entries
still verify *and* the `0007` table is live on a long-lived file rather than only on a fixture.
`mypy` is **informational by design** (`make typecheck` ends in `|| true`, so it gates nothing): it currently
reads **`Found 21 errors in 12 files (checked 65 source files)`**, a figure this block carried as 14 until the
pass re-ran the command. Clearing it is T50, deliberately not folded into a release pass — every source change
would invalidate the four legs measured above.
**T41's own gates, unchanged by this tranche:** `make trace` = **29/29 checks PASS** (also through the CI
`--url` shape) · `make trace-mutations` = **all four caught** (**2 / 7 / 6 / 1**) ·
`tests/test_tracing.py` = **108 passed, 0 skipped on both dialects** ·
**T39's own gates:** `make lock-check` PASS · `make lock-e2e` = **10/10 checks**, two `--no-cache` builds
byte-identical (38 freeze lines) and 34 installed packages agreeing with the pins, FC-1 re-run inside the
image · `make lock-mutations` = **all four caught** · `make airgap` **PASS** on the pinned image (sealed
verdict `MANUAL_REVIEW 0.4029`, MEDIUM, 5 detectors, coverage 100%) ·
`make scale` = **15/15 PASS** on images rebuilt from this source and `make scale-mutations` = **both caught**
(**8** and **3** checks failing) · `make ratelimit` = **21/21 PASS** and `make ratelimit-mutations` =
**all three caught** (4 / 2 / 1 checks failing; the target exits 0 because each ran with `--expect-fail`, and
its guard turns an *uncaught* mutation red) ·
**T46's own gates:** `make retention` = anchors **PASS (all 13)** + **`[baseline] 42 cases, 0 failing`** and
`make retention-mutations` = **all thirteen caught**, exit 0 — see the T46 entry for the per-mode counts and
the three close-out witnesses.
**T47's and T51's own gates:** `make ledger` / `make ledger-postgres` = **`RESULT: PASS`** on both dialects
with `entries_checked == 1 000 000`, and `make ledger-mutations` = **all eleven caught** on a **35-case**
baseline, `--check-anchors` **11/11** · `tests/test_audit_checkpoints.py` = **36 passed, 0 skipped on both
dialects** · `make tenancy-mutations` green · `tests/test_event_loop.py` = **13 cases**, whose control measures
**1 005 ms** of stalled loop behind an `async def` handler against **3.6 ms** behind a plain `def` one.
Every count above comes from `--junitxml` or a script's own printed `RESULT:` line — never from a `pytest -q`
tail, never from a sampler without its interval, and never from a CI file that has not been parsed. **And the
figures were written into the documents after the legs ran, which is a change to shipped files:** the 21 guards
in `tests/test_release_hygiene.py` — the only tests that read prose — were re-executed afterwards and again as
the last action before the tree was frozen, and the suite itself was re-run on the edited tree. The Postgres and
container legs were not re-run for prose, on evidence: `find` for anything modified after they began returns
four `.md` files and nothing else, so the code they certified is the code that uploads. See spec §6.9.
**History, kept where it belongs:** this block read **716 tests: 695 passed, 21 skipped** in 150.5 s, with the
Postgres leg at 229.1 s and a sampler pass of **9 868 samples / 230.8 s / 236 distinct / `{0: 1025, 1: 8842,
2: 1}`**, at the close of T47; and **678 tests: 657 passed, 21 skipped** in 143.9 s, with the Postgres leg at
174.9 s and a sampler pass of **7 394 samples / 178.3 s / 207 distinct / `{0: 1037, 1: 6356, 2: 1}`**, at the
first close of T46 — before `remove_storage()`'s failure branch was driven and two cases were
added to `tests/test_retention.py`; those figures belong to those runs, and the ones above are the re-measured
set. Earlier still, the same block read **583 tests: 564 passed, 19 skipped** at the close of
T41, on `alembic heads = 0005` and a nine-job workflow, and it said `alert-rules` was *13 rules against 16
declared* — those figures were real for that run; `.github/workflows/ci.yml` genuinely did not parse until
T39 fixed two step names containing `host side: ` (a `ScannerError` at line 220 column 57), which is why no
hosted runner has ever executed any of it (spec §6.1 interpretation 6).

---

## M1 — Freedom gates (spec §9; FC-1, FC-3, FC-4, REQ-INFRA-1, REQ-INFRA-4)

**M1 status: 26 of 26 tasks verified and checked.** Every M1 acceptance command has now been run on this
machine: the air-gapped container e2e (T10) once a docker daemon was available, and the two claims that had
been left to CI — AC-INFRA-1 on Postgres (T25) and the product itself on Postgres (T26) — were executed
against a real server and *found bugs*, which is the argument for running them. What M1 still does not
claim: FC-3 is **idle** rather than satisfied (no ML detector is registered, so the gate has never judged a
real model). M2 stays unconverted into tasks because it requires the `OQ-5`/`OQ-6` weight-licensing
decisions (spec §12).

Order rationale: gates land *before* any model or infra choice, so an FC violation surfaces as a
CI failure rather than as a rewrite (spec §10 "abstraction tax").

### FC-1 — permissive-only dependency policy (automated)

- [x] **T1** `synthverify/compliance/licenses.py`: allowlist constants, licence resolution from
  installed distribution metadata (`License-Expression` → `License` → classifiers → `LICENSE` file
  text), normalisation to a SPDX-ish id, dependency-set builder from `pyproject.toml` (core + extras,
  transitively via `Requires-Dist`), `scan()` → `LicenseViolation` list.
  *Verify:* `./.venv/bin/python -m synthverify.cli licenses` exits 0 on this repo.
- [x] **T2** `licenses` CLI command wired into `synthverify/cli.py` (`--json`, non-zero exit on violation).
  *Verify:* `./.venv/bin/python -m synthverify.cli licenses --json | head` + `echo $?`.
- [x] **T3** `tests/test_freedom_licenses.py`: allowlist unit matrix (MIT/BSD/Apache/ISC/PSF/MPL accept;
  GPL/AGPL/SSPL/BSL/proprietary/unknown reject) **and** the real declared-dependency scan is clean.
  *Verify:* `./.venv/bin/pytest tests/test_freedom_licenses.py -q`.
- [x] **T4** CI: `freedom` job runs the licence scan + manifest validation + offline marker.
  *Verify:* `./.venv/bin/python -c "import yaml,sys;yaml.safe_load(open('.github/workflows/ci.yml'))"` and
  the local equivalents of each step pass.

### FC-3 — open weights or nothing (manifest schema + gate)

- [x] **T5** `synthverify/compliance/model_manifest.py`: manifest schema
  (`id`, `license`, `weights_sha256`, `dataset_license`, `eval_report{auc,eer,ece,per_group}`,
  `weights_source`, `detector`) + validator reusing the FC-1 allowlist; loader for `synthverify/models/*.json`.
  *Verify:* `./.venv/bin/pytest tests/test_model_manifests.py -q` (T7).
- [x] **T6** `synthverify/models/README.md` documenting the schema + `template.json.example`; directory is
  the scan root; absent directory is a clean pass (0 ML detectors today), not an error.
  *Verify:* `./.venv/bin/python -m synthverify.cli model-manifests` exits 0 with an explicit "no ML detectors registered".
- [x] **T7** `tests/test_model_manifests.py`: valid manifest passes; each disqualifier fails with a named
  reason (missing field, non-permissive licence, non-commercial dataset licence, bad sha256, missing
  per-group eval, unknown detector name); **every registered detector with `ml_model` set must have a
  valid manifest** (the actual gate that stops a non-free model sneaking in).
  *Verify:* as T5, plus `./.venv/bin/pytest -q -k manifest`.
- [x] **T8** `Detector.ml_model` class attribute (default `None`) in `synthverify/detectors/base.py` so ML
  plugins self-declare their manifest id; `model-manifests --check-registry` cross-checks both directions.
  *Verify:* T7 tests + CLI exit 0.
  *Note:* the cross-check is **unconditional** in `scan()` rather than behind `--check-registry`, so CI
  cannot pass by forgetting the flag; a mutation-checked GPL manifest in `models/` made the CLI exit 1.

### FC-4 — offline-capable, no phone-home (AC-FC-4)

- [x] **T9** `tests/test_offline.py`: hard socket guard (patch `socket.socket`, `socket.create_connection`,
  `socket.getaddrinfo` to raise) around the **full** paths: sync `/media/analyze`, async
  ingest→`process_job`, CLI subprocess `analyze`, audit-chain verify. Any outbound attempt fails the test.
  *Verify:* `./.venv/bin/pytest tests/test_offline.py -q` green, and the same file green under
  `./.venv/bin/pytest -q` in the full suite.
- [x] **T10** CI: network-isolated container e2e (`docker run --network none` executing a real file
  analysis) as the CI-side proof of AC-FC-4.
  *Verify:* the same three steps the `docker` job runs, executed locally on the wheel-built image.
  *Evidence (2026-09-26, Docker 29.6.2 linux/arm64):* `docker build -f docker/Dockerfile -t synthverify:ci .`
  → exit 0, image `93f0bd6e7963` 1.41 GB. Then, inside `--network none`:
  (1) `python -c …from fixtures_gen import doctored_photo` wrote `/tmp/evidence.jpg` (48 186 B, a real
  JPEG carrying the `Adobe Photoshop 25.1` tag — the host copy was deleted first, so the container made it);
  (2) `python -m synthverify.cli analyze /work/evidence.jpg --json` → exit 0, 6 419 B report;
  (3) the CI assertion passed: `risk_tier=MEDIUM`, `risk_score=0.4029`, `recommended_action=MANUAL_REVIEW`,
  **`detector_coverage=1.0`** with all five image detectors `status=ran` (ela 0.597 / metadata 0.5 /
  jpeg_history 0.15 / frequency 0.045 / noise 0.0) and flags `ELA_INCONSISTENT`, `EDITING_SOFTWARE_TAG` —
  i.e. nothing silently skipped, which is how a sealed container would otherwise fake a pass.
  *Non-vacuity control:* `docker run --network none synthverify:ci python -c "socket.create_connection(('1.1.1.1',443))"`
  fails with `OSError: [Errno 101] Network is unreachable`, so the run had no interface to phone home
  through rather than merely no code calling out. AC-FC-4 is now evidenced at both the syscall boundary
  (T9) and the OS network-namespace boundary.

### REQ-INFRA-4 — `MediaStore` abstraction (AC-INFRA-4: swap with no code change)

- [x] **T11** `synthverify/storage/base.py`: `MediaStore` ABC — `put(digest,name,data)`, `get(key)`,
  `exists(key)`, `delete(key)`, `location(key)`, `open_path(key)` (local-only, raises otherwise) and a
  content-addressed key scheme (`<sha[:2]>/<sha>_<name>`) defined **once** in the base.
  *Verify:* import + `./.venv/bin/python -m pytest tests/test_media_store.py -q` → 57 passed.
  `safe_filename()` now also collapses dot-runs (`..` → `_`), so a name can neither traverse nor leave a
  `..` fragment for a later path-joiner to misread.
- [x] **T12** `synthverify/storage/local.py`: filesystem backend preserving today's exact on-disk layout
  (existing media must stay readable).
  *Verify:* `tests/test_media_store.py::TestLocal` → 8 passed, incl. `test_layout_is_the_v1_on_disk_layout`
  (asserts the literal `<storage_dir>/aa/<sha>_<name>` path the pre-refactor route wrote) and
  `test_legacy_absolute_paths_stay_readable` (an absolute stored path is honoured verbatim). Writes are
  mkstemp + `fsync` + `os.replace`, proven by a failed-replace test that leaves no readable or partial object.
- [x] **T13** `synthverify/storage/s3.py`: S3-compatible backend (MinIO default) — AWS SigV4 via stdlib
  `hmac`/`hashlib` + existing `httpx`; **no new runtime dependency**; injectable transport for tests.
  *Verify:* `TestSigV4` (6) + `TestS3Behaviour` (8) green against an in-process **HTTP** S3 mock (a real
  loopback socket, so httpx's own transport is exercised) that re-derives the signature from the received
  bytes and distinguishes `SignatureDoesNotMatch` / `XAmzContentSHA256Mismatch` / `CredentialScopeMismatch`;
  a wrong secret and a wrong region each produce their own code, and a tampered body fails the independent
  verifier, so neither side is a rubber stamp. `httpx.TransportError` → `MediaStoreError`.
- [x] **T14** `config.py`: `media_store` (`local`|`s3`), `s3_endpoint/s3_bucket/s3_region/s3_access_key/
  s3_secret_key/s3_path_style/s3_prefix`; `get_media_store(settings)` factory + cache; `SV_MEDIA_STORE` env.
  *Verify:* `./.venv/bin/python -m pytest tests/test_media_store.py -q -k factory` → 6 passed (default is
  local, s3 selected by config alone, s3-without-a-bucket is a startup error, unknown backend refused,
  one cached instance per fingerprint and a new one after a change).
- [x] **T15** Refactor call sites only: `routes_media._save_and_create_job` (write) and
  `worker._load_media` (read) go through the store; `MediaAsset.storage_path` stores the store location.
  *Verify:* full `./.venv/bin/python -m pytest tests/` → 374 passed, exit 0 with no behavioural test
  edited. (The missing-object exception is `MediaNotFoundError`, renamed to keep ruff's N818 and the
  repo's `*Error` convention; call-site updates were name-only.)
- [x] **T16** Backend-parity test: one parametrized e2e ingest→verdict run over both backends, same
  assertions, zero backend-specific branches in the test body (that is AC-INFRA-4's actual claim).
  *Verify:* `./.venv/bin/python -m pytest tests/test_media_store.py -q -k parity` → 14 passed: 7 store
  operations × 2 backends plus `TestIngestThroughTheStore` (ingest→verdict and content dedupe × 2), the
  backend chosen only by `SV_*` env in the `store_config` fixture.

### REQ-INFRA-1 — migrations (AC-INFRA-1: `alembic upgrade head` ≡ `create_all()`)

- [x] **T17** Declare `alembic>=1.13` in `[project] dependencies` (MIT; needed at deploy time, so core not extra).
  *Verify:* `./.venv/bin/python -c "import alembic; print(alembic.__version__)"` → 1.20.0, already a core
  dependency at `pyproject.toml:37`, and the FC-1 scan lists it among the 18 roots with `license_id: MIT`,
  `status: allowed`, `violations: []` over 45 packages.
- [x] **T18** `alembic.ini` + `synthverify/migrations/{env.py,script.py.mako}` with `SV_DATABASE_URL`
  resolution and `Base.metadata` as target.
  *Verify:* `./.venv/bin/python -m alembic heads` → `0002 (head)`, single line of descent (asserted in
  `TestSingleLineOfDescent`). The ini carries **no** connection string; `env.py` resolves
  `SV_DATABASE_URL` at run time, supports `-x url=` / `--url`, and runs offline (`--sql`) so a deployment
  can review the DDL without a database. `render_as_batch` and `compare_type` are set from the dialect.
  Note: `./.venv/bin/alembic` cannot be used here - every console script in this migrated venv has a dead
  shebang, so the module form is the only one that runs.
- [x] **T19** `synthverify/migrations/versions/0001_baseline.py`: full v1 schema (7 tables + indexes),
  hand-written and idempotent-safe for stamping existing installs.
  *Verify:* every `create_table`/`create_index` is guarded on live inspection, so `upgrade head` over a
  `create_all()` database no-ops (`TestExistingInstallsConverge`), and a database that already holds one
  of the tables converges too (`TestTheChecksCanFail::test_a_table_that_already_exists_is_skipped_not_recreated`).
  **Defect found while writing it:** v1 declared `mapped_column(RiskTier.LOW.value, String(20))`, and the
  first positional argument of `mapped_column` is the *column name* - so the persisted column was literally
  `"LOW"` while the attribute was `Job.risk_tier` (confirmed in the `create_all()` DDL). The model is fixed
  and `0002_rename_legacy_risk_tier_column.py` repairs existing databases in place; the guard test
  `test_the_models_do_not_reintroduce_the_legacy_column` keeps it fixed.
- [x] **T20** `tests/test_migrations.py`: (a) fresh SQLite → `upgrade head` → `compare_metadata` against
  `Base.metadata` reports **no diffs**; (b) `create_all()` DB → `stamp head` → `upgrade head` no-ops;
  (c) the same on a real Postgres when `SV_TEST_POSTGRES_URL` is set (skipped otherwise, never faked).
  *Verify:* `./.venv/bin/python -m pytest tests/test_migrations.py -q -rs` → **17 passed, 5 skipped** with
  the visible reason "SV_TEST_POSTGRES_URL is unset: the Postgres half of AC-INFRA-1 is unverified here".
  Stronger than the task asked: (a) also compares the stored table+index DDL byte-for-byte against
  `create_all()`'s (whitespace-normalised only), `compare_type=True` so a `TIMESTAMP`/`TIMESTAMPTZ` slip
  cannot hide, and three non-vacuity tests (partial schema, dropped index, pre-existing table). At the time
  this was written the machine had no Postgres server, so the Postgres half was claimed for CI only — that
  gap is closed in **T25**, which ran these tests against a real `postgres:16` and fixed two defects the
  skip had been hiding.
  Real-data check: `make migrate` against the repo's pre-existing `data/synthverify.db` (built with the old
  model on 2026-09-17) recorded `alembic_version=0002` and renamed `jobs."LOW"` → `jobs.risk_tier`.
- [x] **T21** `db-upgrade` CLI command + Makefile `migrate` target.
  *Verify:* `./.venv/bin/python -m synthverify.cli db-upgrade --help` lists `--revision/--url/--print-sql/
  --stamp`; `make migrate` upgraded the dev database; and `TestDeployEntryPoint` runs the CLI as a
  subprocess with `cwd=/` and no `alembic.ini`, asserting the 7 tables appear, that `--stamp` records `0002`
  without touching tables, and that `--print-sql` emits all seven `CREATE TABLE`s **without creating a
  database file**. `synthverify.db.alembic_config()` builds the `Config` from the installed package path,
  which is also why `pyproject.toml` now ships `migrations/*.py` as package data.

### M1 closeout

- [x] **T22** Makefile: remove `|| true` from `lint` (it hides failures — the spec's "0 warnings" gate
  cannot be met by a swallowed exit code); add `licenses`, `model-manifests`, `migrate`, `freedom` targets.
  *Verify:* `make lint && make freedom` → both exit 0 (lint now also covers `scripts`, and `freedom` runs
  the offline suite after the gates). `make licenses`, `make model-manifests`, `make migrate` each verified
  separately above. The tool invocations switched to `$(PY) -m ruff` / `-m mypy` so the gate survives this
  venv's dead console-script shebangs; `typecheck` stays `|| true` because mypy is labelled informational
  here and is not part of the spec's gate.
- [x] **T23** Docs: README §2.5 status table + §7 checkboxes, `docs/architecture.md` scaling path
  (`MediaStore`, migrations, freedom gates), spec §6 status notes. No claim without a passing command.
  *Verify:* every quoted command re-run from the repo root, all green — `… -m pytest tests/` (391 passed,
  5 skipped), `… -m ruff check synthverify tests scripts`, `… -m pytest tests/test_media_store.py` (57),
  `… -m pytest tests/test_migrations.py -q -rs` (17 passed + 5 visible skips), `… -m pytest
  tests/test_offline.py -m offline` (11), `… -m synthverify.cli licenses` / `model-manifests` (RESULT:
  PASS), `… -m alembic heads` (`0002 (head)`), `make lint && make freedom` (exit 0), `make migrate`.
  Two fixes made *because* the commands were re-run rather than quoted from memory: `… db-upgrade
  --print-sql | head` dies on SIGPIPE, so the documented form redirects to a file; and the bare
  `synthverify …` console form cannot run on this checkout, which is now footnoted where docs use it.
  Written: README §2.5 (per-command table + the not-yet-run container proof), §2.5/§3/§4/§5 counts and
  layout, §6 items 12-15 (defects found: the `mapped_column` name bug, the SPDX paren-strip bug, the
  chance-level-AUC loophole, the S3 prefix double-apply), §7 P1 checkboxes with what actually landed vs
  what remains, and two new *Known limitations* rows for the unrun air-gapped test and the CI-only
  Postgres. `docs/architecture.md` gains MediaStore / schema-evolution / freedom-gate sections and an
  updated scaling row; `docs/security.md` gains the object-store credential posture; `docs/goal-spec.md`
  gains §6.1 (four recorded interpretations + three explicit non-claims), an M1 exit status under §9 and
  two risk rows moved to *closed*; §12's `OQ-1..OQ-6` are untouched — still the user's to settle.
- [x] **T24** Full-suite gate: `pytest tests/ -q` all green and `ruff check synthverify tests scripts` clean.
  *Verify:* `./.venv/bin/python -m pytest tests/` → **391 passed, 5 skipped in 55.02s**; `./.venv/bin/python
  -m ruff check synthverify tests scripts` → **All checks passed!**; output recorded in README §2.5. The 5
  skips are the Postgres-only AC-INFRA-1 cases, visible by design (`make test` runs with `-rs`). Module
  form is used everywhere because every console script in this migrated venv has a dead shebang.

### M1 evidence completed after a docker daemon became available

These two exist because T10's blocker (no container runtime, no Postgres server) lifted on 2026-09-26:
`Docker.app` was started, which made both of the milestone's "CI-only" claims locally executable. Each one
found something.

- [x] **T25** Run AC-INFRA-1's skipped Postgres half against a real server, and fix what it exposes.
  *Verify:* `docker run -d --name sv-pg16 -p 5432:5432 -e POSTGRES_USER=sv -e POSTGRES_PASSWORD=sv
  -e POSTGRES_DB=sv_test postgres:16` (server `16.15`), then
  `SV_TEST_POSTGRES_URL="postgresql+pg8000://sv:sv@127.0.0.1:5432/sv_test" … pytest tests/test_migrations.py
  -q -rs` → **22 passed, 0 skipped**; the same env over the whole suite → **396 tests, 0 failures, 0 errors,
  0 skips**, exit 0 (read back from `--junitxml`, not from a progress bar).
  *Two defects that only a live server could show, both fixed in `tests/test_migrations.py`:*
  (1) `_throwaway_postgres_database()` yielded `str(url)` — SQLAlchemy renders a `URL` with the password
  masked as `***`, so every Postgres test authenticated with a literal asterisk (`28P01 password
  authentication failed`); now `render_as_string(hide_password=False)`.
  (2) The simulated v1 row inserted `jobs.media_id='media-1'` with no parent, which SQLite ignores (foreign
  keys are off by default) and Postgres rejects (`23503 jobs_media_id_fkey`); the helper now creates the
  `media_assets` row first, which is also what a real v1 database contained.
  Neither path had ever executed — not locally, and not in CI either, since the `migrations` job was written
  in the same session. Lesson recorded as README §6 item 16.
  - **Correction, added at T27–T29:** the clause "the same env over the whole suite → 396 tests, 0 skips" was
    *count-true and claim-false*. Setting the variable un-skipped the five Postgres cases and the junit line
    did read `skipped=0`, but `tests/conftest.py`'s `app_env` still hard-coded a SQLite file, so ~340 of those
    396 tests never touched the server. The number could not have got worse, which is the test for whether a
    number is evidence. T27's seam is what makes that sentence mean what it appears to mean, and T29's
    re-run carries the server-side witness (32 live per-test `svtest_*` databases) that this entry lacked.
- [x] **T26** Prove the *product* — not just its schema — runs on Postgres, and make the proof re-runnable.
  *Verify:* `make postgres-e2e` → `… python scripts/postgres_e2e.py` → **15 checks passed, 0 failed,
  RESULT: PASS**. It starts its own throwaway `postgres:16` container when given no server (and removes
  only that container), creates an empty database, boots uvicorn so `create_all()` builds the schema, then
  asserts over HTTP: `/healthz` + `/readyz.database.ok`, unauthenticated ingest → `401`, sync analyze →
  `MEDIUM / 0.4029`, org-scoped analyst key, three ingests of identical bytes → **1 `media_assets` row /
  3 jobs** (dedupe is by digest, cross-org), `jobs."LOW"` absent in `information_schema`, `report` JSON
  identical across the sync/async/admin views, per-org profile `strict-pg` → **BLOCK** while the global
  profile returns **MANUAL_REVIEW** for the same score, ELA heatmap served back as a 29 040-byte PNG, the
  content-addressed object present on disk, and the audit chain verifying → tamper detected (`break_at_seq`
  reported) → verifying again after the edit is reverted.
  *Ordering fact the first draft of this script got wrong and the assertion caught:* routing resolves the
  organisation's policy profile **when the worker runs the job**, so a profile created after ingestion
  changes nothing. The check now creates the profile before ingesting.
  *Also run in the CI shape:* `SV_TEST_POSTGRES_URL=… python scripts/postgres_e2e.py --port 8099` against an
  already-listening server → same 15/15, and it leaves a server it did not create alone. The `migrations`
  job gained that step, so the claim is executed on every push rather than being transcript-only.

---

## M3 (first tranche) — finish AC-INFRA-1's remaining clause

Spec §4.3: *"`AC-INFRA-1` CI matrix runs the whole suite against Postgres and asserts `alembic upgrade
head` produces a schema identical to `create_all()` on a fresh DB."* The schema half closed with T25.
The **whole-suite** half did not: only `tests/test_migrations.py` could reach Postgres at all, because the
shared fixture hard-coded a SQLite file. `README.md` §7's "PostgreSQL reference deployment" box was the one
that had to stay unchecked until this tranche landed, and it is unblocked — the prerequisite the table below
cited ("`MediaStore`/Alembic must land first") is M1, and the docker daemon + `postgres:16` image are here.
*Outcome, after T27–T33:* that box still reads `- [ ]`, but for a narrower reason than it did — the test side
of a Postgres deployment is now proven on both dialects, and what is left is operational sizing and a
supported-version statement, neither of which a test can supply.

- [x] **T27** One env override moves *every* fixture-built database: `tests/conftest.py` reads
  `SV_TEST_POSTGRES_URL` and, when set, gives each test a private database on that server (created up
  front, dropped with `WITH (FORCE)` on teardown); when unset the behaviour is the current SQLite file, so
  the default run and its runtime are untouched. No test body gains a dialect branch — that is the point.
  - **Name settled during implementation:** the variable is `SV_TEST_POSTGRES_URL`, not a new
    `SV_TEST_DATABASE_URL`. `tests/test_migrations.py` already read that name to gate its Postgres cases,
    and two knobs means one of them can be set alone. One variable, one meaning: *every* database the
    harness builds.
  - Evidence: `new_database_url()` returns `(url, teardown)`; `app_env` and the new `database_env` fixture
    are its only two callers. During a run of `tests/test_api.py tests/test_policy_profiles.py` the server
    reported **32 distinct `svtest_*` databases, one live at a time, 0 left behind afterwards**, with names
    carrying the test's own node id (`svtest_test_analyze_ai_image_blocks0_test_29b24e76`) — so the seam is
    observably creating real server-side databases, which is the difference between a claim and a green dot.
  - Mutation check for the leak gate: created `svtest_canary_deadbeef` by hand → the CI assertion reported
    `left behind: 1` and **exited 1**; dropped it → back to 0/exit 0.
- [x] **T28** Close the four hard-coded SQLite escapes so the Postgres leg cannot pass on a partly-SQLite
  suite: `tests/test_e2e.py` (CLI `audit-verify`, CLI `create-key`, the live-uvicorn SDK fixture) and
  `tests/test_offline.py` (CLI subprocess under the socket guard) go through the same seam. Loopback stays
  permitted by the offline guard, so a local server cannot weaken AC-FC-4's meaning.
  - `grep -rn 'SV_DATABASE_URL' tests/` now returns three lines, all of them passing a `new_database_url()`
    result: `tests/conftest.py:90` (`app_env`), `tests/conftest.py:115` (`database_env`, the child-process
    case), `tests/test_e2e.py:93` (live uvicorn). `grep -rn 'sqlite:///' tests/` returns exactly two, both
    intentional: the SQLite fallback inside `new_database_url`, and `_url()` in `test_migrations.py`, whose
    job is replaying a *v1 SQLite* database on both legs.
  - `tests/test_offline.py`'s module docstring now says why a loopback server does not soften FC-4: the
    database is the thing being verified against, not an outbound dependency, and a socket to 127.0.0.1
    puts no packet on a real interface. Its two CLI-subprocess tests use `database_env`, so the audit-chain
    proof ran on Postgres too (8/8 e2e, 19/19 e2e+offline on both dialects).
- [x] **T29** Run the whole suite on Postgres locally (`postgres:16`), green, and fix what it exposes —
  naming every defect it finds, the way T25 did.
  - Command: `SV_TEST_POSTGRES_URL="postgresql+pg8000://sv:sv@127.0.0.1:5432/sv_test" ./.venv/bin/python -m
    pytest tests/ -q -rs --junitxml=…` against a `postgres:16` container (server **16.15**), started for the
    run and removed after it.
  - **396 tests, 0 failures, 0 errors, 0 skips in 60.8 s** — against **396 / 5 skipped in 56.4 s** on the
    SQLite defaults. The five skips are the Postgres half of AC-INFRA-1, and their disappearance *is* the
    evidence that the leg is real; a leg that quietly stayed on SQLite would still report 5.
  - **What it exposed: no product defect.** Stated plainly, because a run that finds nothing is only worth
    quoting if the absence is named. The two bugs T25 found (password masked into the connection URL by
    `str()`, and a `media_assets` parent row missing under real FK enforcement) were already fixed, so this
    run is the first time the *rest* of the suite — routes, workers, webhooks, policy routing, dedupe, the
    audit chain, the SDK over HTTP — has ever executed against Postgres, and it agreed with SQLite on all
    396. The schema-equality, product-boot and whole-suite readings of AC-INFRA-1 now corroborate each
    other instead of being three separate guesses.
  - Harness gaps it did expose, both fixed here: (1) the four SQLite escapes above, without which the leg
    would have "passed" with ~340 of its tests on a different database engine than it claimed; (2) three
    throwaway-database prefixes (`svtest_*`, `synthverify_migrations_*`, `synthverify_app_*`) made a single
    "did the harness clean up after itself" assertion impossible — they are now one `svtest_` prefix, and
    the count of survivors is a CI step.
- [x] **T30** CI: the `test` job becomes a two-leg matrix (`sqlite`, `postgres`) over the same suite, so
  "the whole suite against Postgres" is executed on every push. The Postgres leg must fail loudly rather
  than silently degrade to SQLite if the service is missing.
  - `.github/workflows/ci.yml`: `strategy.matrix.dialect: [sqlite, postgres]`, `fail-fast: false`, one
    `postgres:16` service with a `pg_isready` health check, and
    `SV_TEST_POSTGRES_URL: ${{ matrix.dialect == 'postgres' && 'postgresql+pg8000://…' || '' }}` — the
    empty string *is* the SQLite leg, so there is no second copy of the run command to drift.
  - Three anti-degradation steps, in order: **the Postgres leg may not silently degrade to SQLite** (a
    preflight that raises `KeyError` on a missing variable and fails on an unreachable server before a
    single test runs); the suite itself with `-q -rs` so a skip prints its reason instead of hiding in a dot
    line; **every fixture database was dropped** (one `like 'svtest%'` count, non-zero exits 1).
  - Verification, since CI cannot be run here: both heredocs were executed **locally against the same
    `postgres:16`** — preflight printed `PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on aarch64-unknown-linux-gnu`,
    the leak gate reported `left behind: 0` and exit 0, and the canary above showed it exits 1 when it
    should. `ruff check synthverify tests scripts` passes in both legs' file set; the YAML parses and its
    step/`if` structure was dumped and read back.
  - The `migrations` job no longer repeats `pytest tests/test_migrations.py` — both matrix legs run that
    file now — and keeps what only it can prove: the product booting on Postgres (`scripts/postgres_e2e.py`)
    and `synthverify db-upgrade` from a bare install, deliberately without the `vision` extras so schema
    work cannot drag in heavy dependencies (AC-FC-6).
- [x] **T31** Docs: README §2.5 rows + §7 P1 box + the Postgres *Known limitations* row, `docs/goal-spec.md`
  §6.1 (claimed list) and §9 (AC-INFRA-1 wording), `docs/architecture.md` scaling row.
  - While rewriting those rows I found the honest version of an uncomfortable fact, and it is now in the
    docs rather than in a commit message: **README §2.5 already carried a "Full suite against Postgres 16 ·
    396 passed, 0 skipped" row that this tranche had not earned.** Setting `SV_TEST_POSTGRES_URL` un-skipped
    five tests and the junit line read `skipped=0`, while ~340 tests quietly kept opening their own SQLite
    file — same number, different world, because the number was never measuring the claim. The row is now
    backed by a witness the server itself produces (32 live per-test `svtest_*` databases mid-run, 0 after),
    and README §6 item **18** states the lesson: *a number that cannot get worse is not a gate.*
  - README: status line, CI-pipeline sentence, §2.5 rows (both full-suite rows, the two `test_e2e.py` rows,
    the syscall-boundary row), the "Still not claimed" paragraph, §4 commands (both dialects, plus the dead
    `./.venv/bin/pip` fixed to `-m pip`), §5 layout (`conftest.py` names the seam), §7 Alembic row and the
    PostgreSQL-reference row, and the *Known limitations* Postgres bullet — which no longer says the suite
    stops at SQLite.
  - `docs/goal-spec.md`: §6.1 gains a bullet for AC-INFRA-1's whole-suite clause and **interpretation 6**,
    which says plainly that the clause's "CI matrix" is a committed matrix plus a local run and that **no
    hosted runner has ever executed any gate in this spec, because nothing is pushed from** — a
    non-claim added to the "Explicitly not claimed" list rather than buried. §9 M1 exit status now names
    three evidence levels for AC-INFRA-1 and two open items; §13 counts six interpretations.
  - `docs/architecture.md`: the schema-evolution section explains the single-database-builder rule and why a
    dialect branch in a test body would invalidate the leg; the Scaling path row for Database states the
    whole-suite fact and keeps the one thing still unknown (pool sizing for an instance class) visible.
- [x] **T32** Final gate re-run with counts recorded in both dialects, plus lint / freedom / airgap /
  postgres-e2e, so nothing earlier in this file is quoted from memory.
  - One sequence, every step from the repository root, exit 0:
    `make lint` → **All checks passed!** · `pytest tests/` → **396 tests, 391 passed, 5 skipped** in 53.6 s ·
    `SV_TEST_POSTGRES_URL=… pytest tests/` → **396 tests, 396 passed, 0 skipped** in 64.6 s (both from
    `--junitxml`, parsed, not read off a dot line) · CI leak gate → **left behind: 0** ·
    `make freedom` → FC-1 **RESULT: PASS**, FC-3 **RESULT: PASS** ("idle, not skipped"), FC-4 offline
    **11 passed** · `make postgres-e2e` → **15 checks passed, 0 failed / RESULT: PASS** ·
    `make airgap` → seal control reports `--network none has no route (expected: Network is unreachable)`,
    then **MANUAL_REVIEW 0.4029 (MEDIUM, 5 detectors, coverage 100%)** from inside the sealed container ·
    `make typecheck` → **14 errors in 6 files**, unchanged from §2.5's documented informational count.
  - Post-run hygiene: the `postgres:16` container started for this tranche was removed, `docker ps -a` shows
    no `sv-*`, and nothing listens on :5432. The `synthverify:ci` image stays — `make airgap` rebuilds it, but
    the local daemon needs it present to run the gate at all.
  - Re-running the sequence after the documentation edits caught something the first pass had hidden, and it
    is T33 below: `make airgap` **failed** — not the analysis, the `docker build` in front of it.

- [x] **T33** Fix the defect T32's re-run exposed: editing a text file could break FC-4's container gate.
  - *Symptom:* after the T31 README edits, `make airgap` died inside `pip install` on a PyPI socket error —
    the same command that had passed minutes earlier with no product change.
    and a single `RUN pip install --no-cache-dir ".[vision]"` whose layer inputs include
    `COPY pyproject.toml README.md ./` and `COPY synthverify ./synthverify`. setuptools reads `README.md`
    for the package metadata, so *any* docs or source edit re-executed that RUN, and with the cache disabled
    every run re-downloaded its 33 packages (opencv, scipy, numpy and friends) from the network. A freedom
    gate whose pass/fail depends on a transient pypi failure is not a gate.
  - *Fix:* a BuildKit cache mount — `RUN --mount=type=cache,target=/root/.cache/pip pip install ".[vision]"`
    — and dropping the now-counterproductive `PIP_NO_CACHE_DIR=1`. The mount is not part of the image, so
    layer size is unchanged; only the rebuild path differs.
  - *Verification, before and after, on a content change (BuildKit keys `COPY` layers on content, so a bare
    `touch` proves nothing — that was the first, invalid probe):* cold cache **4 min 04 s**, re-download
    included; after the fix, same class of edit → **26.1 s** total build, and the log for that build parses
    to **33 packages installed, 32 distinct wheels served from the cache mount, `Downloading` occurring 0
    times**. The air-gapped gate then passed on that image: seal control reports no route, verdict
    **MANUAL_REVIEW 0.4029 (MEDIUM, 5 detectors, coverage 100%)**. The probe line appended to `README.md`
    for the test was removed and the file restored **byte-identical** (`cmp` clean), and the image was
    rebuilt from the restored source — `docker run --network none synthverify:ci python -c "print(open(
    '/app/README.md').read().count('wheel-cache probe'))"` prints **0**, which is the check that nothing the
    probe wrote is shipped.
  - *Still open, named rather than fixed:* the image installs **unpinned** latest versions (this build pulled
    `sqlalchemy 2.1.1`, `starlette 1.7.0`, `numpy 2.4.6`, `opencv-python 5.0.0.93` — all ahead of what
    `.venv` tested against). Reproducible container builds need a lock step; that is an M3 `REQ-OPS`-adjacent
    task, not part of AC-INFRA-1, and it is the reason this tranche does not claim the image is *reproducible*
    — only that its build no longer depends on the network being kind.

**M3 first-tranche status: 7 of 7 tasks verified and checked (T27–T33).** AC-INFRA-1 is closed as far as
this machine can close it: the whole suite runs against Postgres, `upgrade head ≡ create_all()` holds on
both dialects, and both facts are re-runnable with one environment variable. What is still open about
AC-INFRA-1 is not engineering: a hosted runner has never executed the matrix (spec §6.1 interpretation 6),
and that waits on a push, which stays user-gated.

Deliberately **not** in this tranche: `REQ-INFRA-2/3` (external broker, Redis-shaped limiter) and
`REQ-INFRA-5` (retention/TTL) are separate M3 items with their own acceptance criteria (`AC-INFRA-2`'s
two-replica run needs the broker seam first), and `REQ-DET-*`/M2 stays parked on `OQ-5`/`OQ-6`.

---

## M3 (second tranche) — free horizontal scale: `REQ-INFRA-2`, `REQ-INFRA-3`

Spec §4.3's Queue row: *"`REQ-INFRA-2` swap-in external broker using `process_job(db, id)` as the unit
of work; embedded fleet stays the default … Redis (BSD), RabbitMQ (MPL), or Postgres `SKIP LOCKED` —
**no paid broker**, and the Postgres-only option must be first-class since many operators already run
it."* And `AC-INFRA-2`: *"A two-replica docker-compose test (Postgres + Redis, no embedded worker)
ingests 200 jobs and every job completes exactly once, with idempotency preserved."*

The prerequisite is met and the infra is local: `postgres:16` is pulled, the whole suite already runs
against it (T27–T29), and `process_job(db, job_id)` is already module-level with no thread-local state
(`synthverify/worker.py:126`), which is exactly the seam the spec says to keep.

**Facts read before scheduling, because they shape the tasks:**
- `Job` (`synthverify/db.py:175-202`) has `status`, `attempts`, `started_at`, `finished_at` and
  `Index("ix_jobs_org_status", …)` but **no claim or lease column**. A `SKIP LOCKED` broker therefore
  needs a new Alembic revision, and `AC-INFRA-1`'s `upgrade head ≡ create_all()` metadata diff is the
  gate that will fail if the model and the revision ever disagree.
- `Settings.embedded_worker: bool = True` (`config.py:66`) and `app.py:97` already give a
  worker-less replica mode, and `routes_media.py:277` / `routes_jobs.py:125` already fall back to
  `process_job` inline when `app.state.fleet is None`. `AC-INFRA-2`'s "no embedded worker" is
  configurable today; what does not exist is a queue that survives leaving the process.
- `docs/goal-spec.md` §7 conditions 3 and 4 cite `AC-INFRA-1..4` and `AC-IDAM-1..4`, but **`AC-INFRA-3`
  and `AC-IDAM-2` are not defined anywhere in §4** — `REQ-INFRA-3` and `REQ-IDAM-2` are requirements
  with no acceptance criterion, so two of the v2 definition-of-done conditions reference ids that a
  test could never point at. That is T34, and it is a spec defect, not a code one.
- `make airgap`'s container installs **unpinned** latests (T33: it pulled `sqlalchemy 2.1.1`,
  `starlette 1.7.0`, `numpy 2.4.6`, `opencv-python 5.0.0.93`). That is T39. *(Closed: the image installs with
  `-c docker/requirements-lock.txt`, 48 pins, and `make lock-e2e` compares a built image's freeze with them.)*

### Pre-declared verification standard for this tranche

Per the standing rule that a passing count is not evidence, each task names its witness and its
mutation check *before* implementation, and no box gets ticked without both having been run:

| Task | Witness (from the system under test) | Mutation check (how the gate can fail) |
|---|---|---|
| T35 broker seam | `/readyz` reports the selected backend, and an `SV_JOB_BROKER=postgres` process puts its claims in the `jobs` table where a `psql` query can read them | an unknown backend name must raise at startup, not silently fall back to embedded |
| T36 exactly-once claim | two concurrent consumers on real `postgres:16` over 200 jobs, then `SELECT … GROUP BY id HAVING count(*) > 1` over `job.completed` audit events returns **0 rows**, and every job has `attempts = 1` | run the same 200 jobs through a claim that drops `SKIP LOCKED`/the status guard and the duplicate count must go **non-zero** — a gate that cannot see double-processing is not a gate |
| T37 two replicas | the DB after the compose run: 200 rows `completed`, every one claimed by exactly two replica identities, 0 jobs with two results, and `/admin/audit/verify` still `verified: true` after four processes wrote the ledger at once | start the stack with no worker tier so nothing drains the durable queue (the 200 jobs must then *not* complete), and re-run it with `SV_JOB_BROKER=embedded` so ownership is never recorded in the shared table — both must exit 1 |
| T40 concurrent ledger | appends from 8 threads on SQLite **and** 8 threads on `postgres:16`: every row lands, `verify_chain` returns `verified=True`, and no statement dies with `database is locked` | the same 8×25 append loop driven through the pre-fix read-then-insert, reproduced inside the test against its own database, must report `verified=False` — otherwise the check cannot see a fork and T37's chain clause is theatre |
| T38 shared limiter | two separate OS processes against one Valkey bucket permit **fewer** total requests than two isolated in-process buckets would | stop the Valkey container: the limiter must fail **closed to the in-process bucket** (offline-safe, FC-4), not hang or 500 |
| T39 pinned image | two builds from unchanged source print **byte-identical** `pip freeze` output | bump one pin and the lock-consistency check must exit 1 |

- [x] **T34** Spec hygiene: define the missing acceptance criteria so every requirement in §4.3/§4.4 has
  a testable clause and §7 stops citing ids that do not exist.
  - `AC-INFRA-3` (for `REQ-INFRA-3`, §4.3): the limiter's contract is the test — a backend swap keeps
    `check(subject) -> (allowed, retry_after)` exactly, two processes sharing the backend see one
    global budget (admitting **fewer** combined requests than two isolated buckets), an unreachable
    backend degrades to the in-process bucket rather than failing the request (so `AC-FC-4`'s offline
    claim survives the swap), and no route handler changes line.
  - `AC-IDAM-2` (for `REQ-IDAM-2`, §4.4): a scope matrix test — a credential minted with `jobs:read` +
    `media:submit` is admitted to ingest/job-read and rejected **`403`** on `admin:policy:write` and
    `artifacts:read` naming the missing scope, no scope combination reads another organisation's
    resource (`404`, per `AC-IDAM-3`), and pre-scope keys keep their role-equivalent grant in the same
    table. The `403`/`404` split is stated deliberately: an unknown scope on a *known* credential does
    not leak existence the way a cross-org read would.
  - **Two more holes the measurement found, both in §4.3, fixed for the same reason:** `REQ-INFRA-5`
    (retention/TTL + legal-hold pinning) and `REQ-INFRA-6` (exemplars + trace propagation) had **no
    acceptance criterion either**, and §7's condition 3 read `AC-INFRA-1..4` — a range naming one id
    that did not exist while omitting two requirements that did. `AC-INFRA-5` (sweep honours a
    legal-hold pin, is idempotent, and the pin is itself ledger-recorded) and `AC-INFRA-6` (one trace id
    across request → worker → ledger, alert rules shipped as a parsing file, no metered vendor) are now
    defined and condition 3 reads `1..6`. Condition 4's "scopes" clause became citable.
  - **This widened the definition of done**, so §13 carries it as its own dated entry rather than letting
    it pass as a typo fix: no requirement text changed, no FC touched, and the widening *is* the
    correction — under the old numbering v2 could have been declared done with no shared limiter, no
    retention sweep and no trace correlation. Recorded as §6.1 **interpretation 7**.
  - *Evidence, by measurement rather than assertion* (a script over the spec text, not a grep of my own
    new sentences): §4.3 pairs `REQ-INFRA-1..6` with `AC-INFRA-1..6` and §4.4 pairs `REQ-IDAM-1..4` with
    `AC-IDAM-1..4` — all twelve now resolve. **Every `AC-*` id §7 cites exists**: the set it names is
    `AC-DET-3`, `AC-DET-5`, `AC-IDAM-1..4`, `AC-INFRA-1..6`, `AC-PUBLIC-2/3`, `AC-RT-1`, and each is
    defined in a backticked clause. 25 acceptance criteria are defined across the spec.
  - *Found and deliberately **not** fixed,* named because a partial sweep is only honest if the
    remainder is written down: 15 requirements outside §4.3/§4.4 still carry their acceptance clause in
    the requirement prose instead of a numbered id — `REQ-DET-2/6/7/8`, `REQ-OPS-1/2/4`, `REQ-PUBLIC-1/4`,
    `REQ-REACH-2/3`, `REQ-RT-2/3`, `REQ-XAI-1/3`. Left alone for two reasons: §7 cites no non-existent id
    for any of them, and writing acceptance criteria for the M2/M4/M5 areas means inventing targets that
    §12 reserves as operator decisions (`OQ-5`/`OQ-6` in particular). That limit is now stated in
    interpretation 7 itself.
- [x] **T35** `JobBroker` seam (`synthverify/brokers/`, mirroring the `synthverify/storage/` pattern):
  `base.py` contract + `embedded.py` (today's `WorkerFleet`, unchanged behaviour, still the default) +
  `postgres.py` (`SELECT … FOR UPDATE SKIP LOCKED`) + `factory.py` selected by `SV_JOB_BROKER`, plus
  `config.py` fields and `/readyz` reporting. Routes enqueue through the broker, not the fleet.
  - `process_job(db, job_id)` stays the unit of work — the spec's own words — so the two backends
    differ only in *how a job id is handed to a worker thread*, never in what the worker does.
  - **Verify:** new `tests/test_brokers.py` passes on SQLite for `embedded` and on real `postgres:16`
    for `postgres`; an unknown name raises `ValueError` at build time; the existing 396 tests still pass
    untouched, which is the check that the default behaviour did not move.
  - *Executed.* `synthverify/brokers/{base,embedded,postgres,factory,__init__}.py`; `WorkerFleet` now
    takes a broker and keeps only the threads, the webhook retry loop and `process_job`. The queue's
    own semantics (maxsize, priority, FIFO tie-break, the crash sweep, `…_queue_depth`) moved into
    `EmbeddedBroker` rather than being re-derived, which is why the pre-existing suite is the witness:
    **408 passed / 14 skipped of 422 collected on SQLite** and **422 passed / 0 skipped on `postgres:16`
    in 70.8 s**, with no test file other than `test_brokers.py` and the migration-head literals changed.
  - *Counting correction, because a witness has to be re-readable:* these rows earlier quoted "418
    passed / 13 skipped" and "422 passed", read off the tail of a `pytest -q` run. `pytest -q -rs` at
    full-suite scale ends on the skip summary, and the tail being read had no stats line in it at all:
    **422 is the collected total**, which is 408 passed + 14 skipped. The numbers above now come from
    `--junitxml` (`tests="422" failures="0" errors="0" skipped="14"`, and `skipped="0"` on the Postgres
    leg), which reports the same thing regardless of how the terminal writer ordered its lines.
  - *Seam fails three ways, each with a test:* an unknown `SV_JOB_BROKER` raises at build (not silently
    embedded); `postgres` on a SQLite URL raises — *"Use the 'embedded' broker instead of pretending"*;
    and `postgres` on a schema behind `0003` raises at `start()`, before the first claim.
  - `/readyz` reports `job_broker`, `durable_queue`, `embedded_workers` and `queue_depth` read from the
    broker, so the witness the table asks for is a `curl` away. The routes call
    `submit_job(app.state, job)`; its one rule (a durable broker is shared state, the in-process one is
    a void unless this process has a fleet) is what `AC-INFRA-2` turns on, and it is tested in all three
    branches by `TestSubmitRule`.
  - *Found by a test, not by reading:* `test_the_postgres_broker_refuses_sqlite` **passed** on the
    Postgres leg for the wrong reason — the shared fixture hands it a Postgres database, so the
    assertion proved nothing there. It now constructs `Database("sqlite:///:memory:")` explicitly, and
    the test says why that is the one place in the suite a dialect is named.
- [x] **T36** Exactly-once claim: Alembic `0003` adds the lease columns, `postgres.py` claims a job by
  writing its own identity + token in the same transaction that flips `queued → running`, and expired
  leases are reclaimable so a crashed replica cannot wedge the queue.
  - **Verify:** the T36 row of the table above, both halves, against `postgres:16`. `AC-INFRA-1`'s
    metadata-diff test must stay green across `0003` — it is the built-in check that model and
    migration cannot drift.
  - *Executed.* `0003_add_job_broker_lease_columns.py` adds `claim_token`/`claimed_by`/`lease_expires_at`
    idempotently; the model declares them **last** in `Job`, because `ALTER TABLE` appends and
    `AC-INFRA-1`'s `sqlite_master` DDL diff compares column order. The first version of the model put
    them after `finished_at`'s neighbours and that gate failed with a real diff — the gate worked.
  - One statement does the claim: `UPDATE jobs SET status='running', claim_token=…, claimed_by=…,
    lease_expires_at=…, attempts=attempts+1 WHERE id = (SELECT id … WHERE status='queued'
    ORDER BY priority, created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id`
    (`sqlalchemy.update().scalar_subquery()`), so the attempt counter cannot disagree with the row.
  - **The write fence is the other half.** `_write_outcome()` puts `WHERE claim_token = :token` *inside*
    the UPDATE rather than reading the token first: a check-then-write lets a slow, dead worker and its
    replacement both commit. A loser's write matches 0 rows, rolls back, increments
    `synthverify_jobs_fenced_total` and logs — the reclaim that cost it the write is already audited as
    `jobs.leases_expired`.
  - *Witness, both halves, on real `postgres:16`:* `TestTwoConsumersExactlyOnce` runs **200 jobs through
    the real pipeline** (measured 24 ms/job, so the run is ~10 s and a reduced detector set was not
    needed) against two `WorkerFleet`s on two `PostgresBroker`s with distinct replica ids. From the
    database afterwards: 200 `completed`, `attempts == 1` for every job, verdicts present, `claimed_by`
    drawn from exactly `{replica-a, replica-b}` across 4 worker identities, **`_duplicate_completions()`
    = `[]`** and exactly 200 `job.completed` ledger rows. `SELECT … GROUP BY actor HAVING count(*) > 1`
    over the ledger is the witness the table asked for.
  - *Mutation, run twice.* (1) The duplicate detector has a canary: `test_detecting_a_duplicate_is_the_point`
    writes two `job.completed` rows for one job through `AuditLedger` and asserts the helper reports
    `[(f"job:{job_id}", 2)]` — so a `_duplicate_completions() == []` that could never be non-empty is
    caught, per the standing rule that a gate which cannot get worse is not a gate. (2) The claim's
    guards are covered behaviourally by the other `TestPostgresBroker` cases: 4 threads × 50 jobs yield
    50 claims with 50 distinct tokens and no double claim (`SKIP LOCKED` + the `status='queued'` guard),
    and only a lease that has actually expired is reclaimable.
  - *Design correction the tests forced:* clearing `claimed_by` on completion destroys the evidence of
    *which replica ran the job*, which is exactly what an operator reads after a job doubles back. The
    rule now written into the model, `_write_outcome()`, `recover()` and two tests is: **`claim_token` +
    `lease_expires_at` are ownership (released at terminal write), `claimed_by` is history (kept).**
  - Also measured and written into the test: `execution_options(isolation_level="AUTOCOMMIT")` does not
    take effect through this engine + `pg8000` pairing — the schema-behind fixture's `DROP COLUMN` was
    visible on its own connection and rolled back when it returned to the pool, so it needs an explicit
    `conn.commit()`.
- [x] **T37** `AC-INFRA-2`: `docker/compose-scale.yml` (postgres + two app replicas,
  `SV_EMBEDDED_WORKER=false`, `SV_JOB_BROKER=postgres`, no embedded queue at all) +
  `scripts/scale_e2e.py` ingesting **200 jobs** over HTTP and asserting exactly-once **from the
  database**, wired to a `make scale` target and a CI step.
  - `AC-INFRA-2` names Redis; the spec's own §4.3 says the Postgres-only path must be first-class, so
    the Postgres broker is the run that must pass here, and the Redis-broker variant is recorded as an
    explicitly-not-claimed item unless a broker for it lands in this tranche.
  - **Verify:** the T37 row above, plus `docker ps -a` clean afterwards (no container or `/tmp/sv-*`
    left behind — standing hygiene rule).
  - *Corrections to my own pre-declaration, written down rather than quietly fixed:* the witness as
    first worded ("200 distinct `claimed_by` values") cannot be true by construction — `claimed_by`
    carries the worker thread (`<replica>:w<n>`), so 200 jobs over 4 threads is 4 distinct values from
    2 replica identities. The gate is now "every job single-owner, exactly 2 replica identities", which
    is the claim AC-INFRA-2 makes. And the pre-declared mutation ("one replica with
    `SV_EMBEDDED_WORKER=true`") **does not fail**: with a durable broker a fleet in the API process just
    drains the same shared queue, which is still the correct topology. What actually can fail is the
    thing the table now says — no consumer tier at all, and an in-process broker so ownership never
    reaches the shared table. Both are run, in T37's evidence.
  - *Facts that decide the topology, each read or measured before writing it:* the replica that
    ingests is not the replica that processes, so `/app/data` must be one shared volume (the local
    store's `storage_path` is an absolute path and `put()` is write-then-rename — both read before
    relying on it); four processes booting `create_all()` against one fresh Postgres database is a DDL
    race, and two of them racing to mint the bootstrap admin key is a unique-violation race, so the
    stack runs `synthverify db-upgrade` as a one-shot and sequence the rest behind `api1` becoming
    healthy; and the shipped image has **no Postgres driver**, because §6.1 note 4 puts production on
    psycopg (LGPL, dynamically linked) while keeping `pg8000` (BSD) verification-only and out of the
    FC-1 scan — hence `docker/Dockerfile.postgres`, an overlay that installs the pinned driver from
    `docker/requirements-postgres.txt` onto the same base, and `scripts/scale_e2e.py` reading the
    database with `pg8000` from the host. The product writes through psycopg and the *verifier* reads
    through BSD: the two-driver shape is the note, exercised instead of asserted.
  - *Executed.* `docker/compose-scale.yml` (project `sv-scale`, services `postgres`, one-shot
    `migrate`, `api1`/`api2` with `SV_EMBEDDED_WORKER=false` + `SV_JOB_BROKER=postgres`, `worker1`/
    `worker2` running `python -m synthverify.cli worker`, one shared `sv-scale-data` volume, each app
    container given an explicit `hostname` so `claimed_by` names the service that ran the job),
    `docker/Dockerfile.postgres` + `docker/requirements-postgres.txt` (`psycopg[binary]==3.3.6`),
    `scripts/scale_e2e.py`, a `synthverify worker` CLI subcommand (`cli.py:_cmd_worker`, no HTTP
    listener, SIGTERM-driven `WorkerFleet.stop()`), `make scale` / `make scale-mutations`, and a `scale`
    job in `.github/workflows/ci.yml` that runs the stack **and both mutations** and then asserts no
    `sv-scale-*` container survived.
  - *Witness — `./.venv/bin/python scripts/scale_e2e.py --no-build`, all 15 checks, printed:*
    **`RESULT: PASS`, 15 passed / 0 failed**, 200 jobs ingested in 4.5 s (45/s) across two replicas,
    `200 jobs drained from the shared queue (0 still queued/running after 0s)`,
    `both replicas report an empty queue ([0, 0])`, `every job completed exactly once ({'completed': 200})`,
    `0 job(s) with attempts <> 1`, `0 unowned job(s); replica hostnames ['worker1', 'worker2']`,
    `200 job.completed rows, 0 duplicated job id(s)`, `0 lease-expiry event(s)`,
    `200 media rows, 200 job rows, 200 job ids handed out`,
    `25 replays to the other replica, 25 returned the original job, 0 new job(s)`,
    `601 entries from 200 jobs across 2 API replicas and 2 worker containers` with `verified: true`,
    and the driver delta itself: `exit 1: ModuleNotFoundError: No module named 'psycopg'` from the base
    image on the same network. `containers left from this project: none`.
  - *Mutation, both halves, each exit 1 as required:*
    **A `--without-workers`** → `RESULT: MUTATION CAUGHT (8 check(s) failed)` — nothing drained after
    61 s (`{'queued': 200}`), both replicas still reporting depth 200, 200 unowned jobs, 0
    `job.completed` rows, 200 jobs with no verdict, and the HTTP read-back coming back
    `queued / None / coverage None`. **B `--embedded`** → `RESULT: MUTATION CAUGHT (3 check(s) failed)`
    — `/readyz` reports `('embedded', False, True)` where the topology check demands
    `('postgres', True, False)`, and every one of the 200 completed jobs has `claimed_by IS NULL`, i.e.
    the work happened inside the process that accepted it and the shared table learned nothing. That is
    precisely the failure mode `AC-INFRA-2`'s "no embedded worker" clause exists to catch, so the gate
    can see it.
  - *What the mutations caught in the harness itself, which is the point of running them:* A crashed
    the script first — `AttributeError: 'NoneType' object has no attribute 'split'` while collecting
    replica identities, because every `claimed_by` was NULL. Guarded, and only then did it produce the
    8-failure report above; an unguarded crash is a broken script, not a caught mutation. And the
    verdict read-back originally sampled *the first job*, which replica 0 had accepted, from replica 0 —
    under mutation B it therefore "passed" while asserting nothing about cross-replica visibility. It now
    deliberately reads a job the **other** replica accepted (`accepted by replica 1, read from replica
    0`), so what it proves is that the verdict and its artifact travelled through the shared database
    and volume; under mutation A the same check fails on its own terms.
  - *Not claimed:* the Redis-backed broker `AC-INFRA-2` also names is not implemented — §4.3 makes the
    Postgres-only path first-class, so this run satisfies the requirement's intent and the Redis variant
    stays openly unshipped (also tracked under T38's Valkey decision). And the `scale` CI job has never
    run on hosted CI: this repo has no remote and nothing has been pushed, so that job is a definition
    verified by executing its three steps locally, not a green tick from a pipeline.
- [x] **T40** *(scheduled by T37's own measurement, not by reading the spec)* Keep the audit chain a
  chain when more than one writer exists. `AuditLedger.append()` reads the current head and inserts a
  row pointing at it, with nothing between the two statements, so two committed transactions can both
  point at the same parent and the chain **forks**. Fix it so a fork is impossible rather than
  unlikely, on both dialects, and prove the fix under the concurrency `AC-INFRA-2` deploys with.
  - **Verify:** the T40 row of the table above.
  - *Executed, and the numbers below are reproducible from the test file's own helpers* (`APPENDERS = 8`,
    `PER_APPENDOR = 25`, so 200 appends per run). Measured **before** the fix, driving the v1
    read-then-insert against the shipped engines:
    ```
    sqlite:   pre-fix append -> rows=200 verified=False forked_parents=8
    postgres: pre-fix append -> rows=200 verified=False forked_parents=111
    ```
    111 of 200 rows pointing at a parent somebody else already extended is the PostgreSQL shape; SQLite
    forks too, just less dramatically, and with **zero driver errors** either way — which is the part
    that matters. `worker_count` defaults to 2 and the embedded fleet appends `job.completed` from those
    threads, so the documented single-node deployment — the default install, on one box, with no Redis
    and no queue — corrupts the ledger that `README.md` and §4.1's SDG-16 framing put the
    accountability claim on. Nothing in the then-green suite noticed, because every audit assertion ran
    single-threaded. (An earlier draft of this row quoted 150/100-row probes with 31 forks; those were
    ad-hoc scripts at 4 and 6 threads, since replaced by the test so the mutation is run by `pytest`,
    not by a hand.)
  - *Witness, both halves of the table row, from the two suite legs:* `tests/test_audit_concurrency.py`
    on `postgres:16` → `test_a_postgres_writer_that_takes_no_lock_forks_the_chain` **PASS** (0.266 s),
    `test_a_sqlite_writer_on_the_pre_fix_engine_forks_the_chain` **PASS** (0.121 s), and on the shipped
    append path `test_concurrent_appenders_leave_one_chain` **PASS** (0.398 s, 8 threads, one chain,
    `verified=True`, every row present) plus `test_a_later_writer_waits_and_then_extends_the_same_chain`
    **PASS** (1.103 s — the second writer is *still blocked* after a full second, then extends the head
    the first one committed, so the mechanism is shown to be a hold rather than a lucky ordering). The
    SQLite leg runs the same four with the PostgreSQL-only one skipped **loudly** for `-rs`. No
    `database is locked` error surfaced in either leg.
  - *Corroborated in the deployment that found it:* `AC-INFRA-2`'s stack now ends with
    **601 ledger entries, `verified: true`, written concurrently by four processes**, and
    `tests/test_brokers.py`'s 200-job two-consumer test carries the same chain assertion (that is the
    run that exposed the fork — it found T40, not the other way round).
  - *Mechanism, per dialect, with the reason each is the right one there:* PostgreSQL takes
    `pg_advisory_xact_lock(hashtextextended('synthverify.audit_chain', 0))` at the top of
    `AuditLedger.append()` (`db.py:_lock_chain`, gated on the session's dialect, name in
    `db.AUDIT_CHAIN_LOCK_NAME`) and releases it at commit — exactly the lifetime the read-then-insert
    needs, no DDL, no retry loop, and a second writer blocks rather than guesses. SQLite gets
    SQLAlchemy's documented pessimistic begin (`BEGIN IMMEDIATE` from an `engine "begin"` listener, with
    the DBAPI's own `isolation_level` turned off in the connect hook) on the existing SQLite branch of
    `_make_engine`, which is the same hold-until-commit guarantee provided by the file lock the app
    already sets `busy_timeout` on. What the fix costs was not measured as a before/after (no pre-fix
    timing run exists); what is on record is the post-fix wall time of the runs that exercise it: SQLite
    leg 63.0 s, Postgres leg 70.8 s, the 200-job two-consumer broker test 3.8 s.
  - *Not taken, with the reason:* a `UNIQUE (prev_hash)` index — a forked chain cannot be *stored*,
    which is a better invariant than a lock that avoids creating one. But `CREATE UNIQUE INDEX` fails on
    every database that already forked, and this project's own dev databases have; the only way to
    upgrade them is to recompute `prev_hash`/`entry_hash` over history, i.e. **silently rewrite a
    tamper-evident ledger to make a constraint pass**. That trade is not T40's to make, so the index is
    named here as the follow-up it is not yet safe to run, and `verify_chain` stays the detector.
  - *Also rejected after being built and run, not in the abstract:* index + `SAVEPOINT` retry loop, at
    the same 8 × 25 concurrency. On PostgreSQL it livelocked — `RuntimeError: append could not claim a
    chain head … duplicate key value violates unique constraint`, i.e. every appender retried into the
    same collision until the budget ran out. On SQLite the same shape died with
    `OperationalError: database is locked`, because retry-inside-a-write-transaction is exactly what
    `busy_timeout` cannot fix. Two driver errors where the lock approach produces none is the
    measurement that ended that design.
- [x] **T38** `REQ-INFRA-3`: `SV_RATE_LIMIT_BACKEND=in-process|valkey` behind the identical
  `RateLimiter.check()` signature (`synthverify/ratelimit.py`, 67 lines today) — same interface, no
  caller churn, which §4.3 calls *"the FC-2 escape hatch"*.
  - Licence honesty first: the **server** choice is Valkey (BSD-3), not Redis ≥ 7.4 (RSALv2/SSPL
    tri-license), and the **client** must be an MIT-licensed pure-Python one pulled in as an optional
    extra so FC-1's scan of the default install keeps passing with `redis` absent.
  - **Verify:** the T38 row above, `make freedom` still `RESULT: PASS` with the new extra present, and
    `pytest -m offline` still green with no Valkey reachable.
  - *Executed.* `synthverify/ratelimits/` — `base.py` (the `RateLimiter` ABC: `name`, `check()`, and the
    `degraded` property that did not exist before this task, plus an optional `close()`), `in_process.py`
    (today's bucket moved verbatim), `valkey.py` (one Lua token bucket run atomically server-side, reading
    `redis.call('TIME')` so the *server* owns the clock, with a bounded reach and a cooldown window),
    `factory.py` (selection, and `redis` refused by name with the licence reason in the message),
    `synthverify/ratelimit.py` reduced to the FastAPI dependency alone, five `SV_RATE_LIMIT_*` settings,
    `app.py` building the limiter from the factory and closing it on shutdown, `/readyz` reporting
    `rate_limit_backend` + `rate_limit_degraded`, `synthverify_rate_limit_fallback_total` as the counted
    event, the optional `valkey = ["valkey>=6.0,<7"]` extra, `tests/test_rate_limit_backends.py` (15), a
    new `tests/test_offline.py` case that puts the swap *inside* the air-gap guard, `scripts/ratelimit_probe.py`
    + `scripts/ratelimit_e2e.py`, `make ratelimit` / `make ratelimit-mutations` / `valkey-up` / `valkey-down`,
    a `limiter` job and a Valkey service in `.github/workflows/ci.yml`, `.env.example`, README §2.3/§2.4/§2.5/§4/§5/§6/§7,
    `docs/architecture.md`, `docs/api.md` and spec §4.3 + §6.1 (interpretation 11) + §6.3 + §13.
  - *Witness — `make ratelimit`, 21 checks, exit 0, the numbers as printed.* Control first: **80** admitted
    across two processes with private buckets at burst 40, `[40, 40]`. One shared Valkey: **40** admitted,
    split `[20, 20]`, `attempts=[60, 60]`, neither process believing it had degraded
    (`[('valkey', False), ('valkey', False])`), refusals carrying `retry_after ≈ 9.99 s`, and a **third**
    process on the same subject `admitted=0 of 80 attempts` — the budget demonstrably lives in the server.
    Through the real app: `/readyz` = `valkey/degraded=False`, and 10 concurrent requests give
    `[200×6, 429×4]` in 0.023 s with `Retry-After: 1`. Outage, `docker stop` under the running app:
    `[200×6, 429×3]` in **0.021 s** (0.017 s on the previous run), `valkey/degraded=True`,
    `synthverify_rate_limit_fallback_total{backend="valkey"} 1.0`, two fresh processes surviving with
    `admitted=[40, 40]` instead of erroring. A *silent* backend — a socket that accepts and never answers —
    abandoned in **0.302 s** at a 0.3 s reach bound. `docker start`: budget global again (**40** across 2
    processes) and the untouched app self-recovered (`degraded=False`). `left behind: none`.
  - *Mutation, all three caught through the Makefile target (exit 0 because each ran with `--expect-fail`;
    the guard's `echo "mutation X was NOT caught"; exit 1` is what would fire):* `isolated` — **4** failures,
    including the headline `80 shared vs 80 isolated` and the third process being admitted (40 of 80) where
    it must be refused; `nofallback` — **2**, both outage probes returning
    `ConnectionError: Error 61 … Connection refused` instead of answering; `no-timeout` — **1**, the silent
    socket costing **6.003 s** against the check's own 2.0 s bound. The pass run and the mutation runs are
    separate CI steps, and `pytest -m offline` (12) is what pins the *shape* of the `except`: a taxonomy
    catch (`ValkeyError, OSError`) satisfies clause (b) against a refused port and turns the sealed
    air-gap case into a 500, because the guard raises a plain `RuntimeError` out of `getaddrinfo`.
  - *Corrections this task owes, written down rather than quietly fixed.* (1) An overclaim of mine from
    earlier in the tranche: `ApiKey.rate_limit_rpm` is stored on the row and **not** consulted by the
    dependency, so the limit applied is the configured global one for every key — now said explicitly in
    `ratelimit.py` and `docs/architecture.md`. (2) `tests/test_rate_limit_backends.py`'s skip reason told
    the operator to run `make valkey`, a target that does not exist; it names `make valkey-up` now. (3) The
    sampler figure this task's docs inherited — "80 distinct `svtest_*` databases, never more than one alive
    at a time" — was produced by a **0.2 s** poll and was an artifact of it: at 20 ms the same run yields 95
    names and finds **2** databases alive at once, which is deliberate in `tests/conftest.py:115` and
    `tests/test_brokers.py:142`. README §2.5, §6 item 18 and spec §6.1 interpretation 10 + §6.3 are restated
    with the interval attached. (4) `scripts/ratelimit_e2e.py:wait_for_pong` slept only in its
    `except OSError` branch, so on a cold Docker daemon — proxy accepting before the container could answer —
    it burned all 60 attempts in milliseconds and `make ratelimit` failed on a healthy stack with
    `no RESP PONG from 127.0.0.1:54050`. Treating "connected" as "answered" is not a readiness wait; fixed,
    and the run then went 21/21.
  - *Not claimed.* `docker/compose-scale.yml` is untouched, so its two replicas still carry per-process
    buckets under a deliberately non-binding `SV_RATE_LIMIT_RPM=20000` — this task proves the limiter, not
    the compose file, and README's "Still not claimed" says so. The shipped image installs no Valkey client
    (`make airgap` passes on the sealed container with the default backend, which is the witness that the
    optional extra stays optional). No Redis-protocol queue (interpretation 8). No hosted-runner execution
    (interpretation 6): the `limiter` job's eight steps were run here, not on GitHub.
- [x] **T39** Container reproducibility: a committed lock for the image's dependency set, the
  Dockerfile installing from it, and a consistency check that the lock covers every declared extra so
  a `pyproject.toml` edit cannot silently install something unpinned again.
  - Scope stated narrowly and on purpose: this makes the *package set* deterministic, which is what T33
    left open. Bit-for-bit reproducible image digests are a different problem (timestamps, layer
    ordering, base-image drift) and stay in Known limitations if not achieved.
  - **Verify:** the T39 row above, `make airgap` passing on the pinned image, and README's
    *Known limitations* bullet on unpinned installs removed **only if** the two-build check is byte-equal.
  - **Executed.** `docker/requirements-lock.txt` — 48 pins, installed with `-c` by `docker/Dockerfile`,
    `docker/Dockerfile.postgres` (the overlay may add `psycopg`, may not move a locked package), `make setup`
    and all six CI install legs. `make lock-check` → `RESULT: PASS`, `47 packages in the declared closure, 48
    pinned`, groups `core=11, dev=6, valkey=1, vision=2`, 0 issues. `make lock-e2e` → **10 checks, 10 PASS**:
    two `docker build --no-cache` runs print **byte-identical `pip freeze --all`** (`38 lines each`), and from
    the image itself `34 installed, 0 disagree` with the pins, no non-pin install form, `greenlet present as
    pinned`, `pip install -c requirements-lock.txt` asserted out of the Dockerfile. `tests/` gained
    `test_dependency_lock.py` plus marker/extra cases in `test_freedom_licenses.py`: the two files are
    **129 passed**, and the whole suite is **475 tests — 456 passed / 19 visible skips** on SQLite (57.3 s) and
    **475 passed / 0 skipped** on `postgres:16` + `valkey/valkey:8` (71.6 s, `0` `svtest_*` databases surviving)
    — i.e. the product runs green on the exact set the image installs. `ruff check synthverify tests scripts`
    clean. Re-measured on the pinned image, not on the pre-T39 one: `make airgap` PASS (sealed verdict
    `MANUAL_REVIEW 0.4029`, MEDIUM, 5 detectors, coverage 100%), `make postgres-e2e` **15/15**, `make scale`
    **15/15** with both mutations caught (**8** and **3** checks failing), `make ratelimit` and its three
    mutations, `pytest -m offline` and `make lint && make freedom` per the header above.
  - **Witness (corroboration from the thing under test, not from the file).** Every claim above that concerns
    the *image* is read out of a built image: `pip freeze --all` from the container's own interpreter, FC-1
    executed *inside* it over `--groups core,vision` (`34 packages from 13 declared roots`, `RESULT: PASS`,
    `greenlet 3.5.6` graded `MIT AND PSF-2.0`), and the same scan on the host checked in the same run *not* to
    see greenlet (`47 packages from 20 roots`) — so the image-side gate cannot be a restatement of the host's.
    The byte-identity claim is measured with `--no-cache` on both builds, because with the layer cache on it
    would only restate the cache key. And the lock is green on a second platform: inside a `linux/aarch64`,
    python 3.11.16 container with the full closure installed *through* the lock, `dependency-lock` reports
    `48 packages in the declared closure, 48 pinned → PASS` (linux's markers pull `greenlet` into the
    host-visible closure, so the exemption is exercised rather than trusted) and `licenses` passes over all 48.
  - **Mutation, all four caught through the Makefile target** (`make lock-mutations`; each `--mutate` run
    exits 0 only *because* the gate noticed, and the guard's `echo "mutation X was NOT caught"; exit 1` is what
    would fire otherwise): `bump-pin` — `dependency-lock` exits 1 and names the `DRIFT` for the moved `anyio`
    pin; `new-dep` — exits 1 naming the `UNPINNED pg8000` a doctored `pyproject.toml` declared; `doctored-pin`
    — the build succeeds *and* the image really contains `anyio 4.15.1` where the committed file says
    `4.14.2`, so 5 checks pass and 1 fails (the doctoring reaches the artifact and is noticed: `-c` is wired,
    not decorative); `no-lock` — 4 pass, 2 fail: with an empty constraints file the build still succeeds (pip
    is not the gate), then **9 of 33** installed packages disagree with the file and `greenlet` is **absent**
    from that image. That last one is the strongest evidence in the task: an unpinned build was not merely
    picking newer versions, it was producing a *different package set*. The two offline mutations run against
    copies in a temporary directory, so no interrupted run can leave the repository half-edited.
  - **Corrections this task owes, written down rather than quietly fixed.** (1) **`.github/workflows/ci.yml`
    did not parse.** Two step names contain `host side: `, which is illegal in a YAML plain scalar, so
    `yaml.safe_load` raised `ScannerError` at line 220 column 57 and a runner would have rejected the entire
    workflow. Every sentence in this repository about "CI runs this" described a pipeline that could not load;
    both names are quoted, the file parses into seven jobs, and interpretation 6's non-claim now has a second
    and stronger reason. (2) **`make setup` was `.venv/bin/pip install …`** — the stale-shebang problem README
    §2.5 opens with — which exited 127; an install target that could never have installed anything is now
    `$(PY) -m pip`. (3) The **first lock was internally inconsistent** (`numpy==1.26.4` beside an
    `opencv-python` whose metadata demands `numpy>=2`) *and `dependency-lock` called it PASS*, because the
    check reads installed metadata and this repo's venv was `--system-site-packages` over Homebrew's, where
    two copies of a package may disagree with each other and nothing asks them to. `docker build` — the thing
    the lock exists for — was the witness that said so. The lock was regenerated from a clean,
    pip-resolved environment and the provenance rule now appears in the module docstring and the lock header.
    (4) The closure walk's marker handling was **under**-reporting: a bare `Marker.evaluate()` has no `extra`
    binding, so every fused line (`sys_platform != "win32" and extra == "standard"`) evaluated false and
    uvicorn's runtime deps vanished — the lock went **47 → 42 while looking more correct**. Markers are now
    evaluated per requested extra and the scan prints what it declined to follow (`extra=243, marker=14`).
    (5) A `freeze` taken from a background build task while `#12 exporting` was still running measured the
    *previous* tag and produced a byte-identity result that meant nothing; the harness now waits for the
    build to exit before reading the image, which is what makes the `--no-cache` claim above belong to these
    two builds. (6) README §2.5, §4, §5, §6 item 22, §7 and the banner carried 438-test and 46-package
    figures: now 475 and 47/20, with the 20 ms sampler's `95 distinct / max 2 alive` explicitly labelled as
    belonging to the 438-test suite that produced it. (7) `setuptools` and `wheel` were added to `dev`, and
    `packaging` to core — FC-1's root count moved 19 → 20 and a clean virtualenv stopped failing the gate with
    `VIOLATION wheel: declared in build-system but not installed`. That is the *strengthening* direction: no
    exclusion was added, no allow-list widened, and the MISSING rule was left exactly as strict as the run
    that caught it.
  - **Not claimed.** Bit-for-bit reproducible *images*: the base is referenced by tag (`python:3.11-slim`), so
    its interpreter and its own `pip`/`setuptools`/`wheel` can move under a pinned package set, layer
    timestamps differ, and `-c` does not reach a PEP 517 isolated build environment — the lock's own header,
    `dependency_lock`'s docstring, README's Known limitations and spec §6.1's bullet all say so rather than
    leaving the silence to be read as coverage. The linux evidence is `linux/aarch64`; `linux/amd64` (what a
    hosted runner uses) is inferred from the same markers, not measured. No hosted-runner execution
    (interpretation 6): the `reproducible-image` job's ten steps were run here. And `docker/compose*.yml` still
    build from these images rather than pulling a published digest — there is nothing to publish to, because
    pushes stay user-gated.

- [x] **T41** `REQ-INFRA-6` / `AC-INFRA-6`: exemplars + trace propagation on OSS only, with the alert
  rules shipped as a file. Verbatim acceptance text: *"A request's trace id appears in its `/metrics`
  exemplars, in its job's audit ledger row, and in the log line its worker emits; the alert rules ship as
  a file in the repo and parse, and no element of that path requires a metered vendor (FC-5)."* Three
  surfaces, and the reason they are one task is that the middle one is what makes the third real: the
  worker that finishes a job runs in another thread — in a scaled deployment another *process* — so it
  cannot read a request-scoped variable, and the trace id has to be carried on the row.
  - Sub-checkboxes, in the order they are landed:
    - [x] `synthverify/tracing.py`: W3C `traceparent` parse/build, validated ids only (32-hex trace,
      16-hex span, `ff` version and all-zero ids rejected, over-long headers rejected), a contextvar, and
      a `bind_trace()` scope the worker can enter from a stored id.
    - [x] `metrics.py`: exemplar storage per series + `render_openmetrics()` (family-name `TYPE`,
      `_total` samples, `# EOF`), `render()` left as text 0.0.4 because that format has no exemplar
      syntax, and a counted drop for any label value that fails validation.
    - [x] `app.py` `observe`: resolve-or-mint the trace id, bind it, echo `traceparent` + `X-Trace-Id`,
      put `trace_id`/`span_id` on the HTTP counter's exemplar. `/metrics` negotiates on `Accept`.
    - [x] Alembic `0004`: `jobs.trace_id`, `audit_events.trace_id` + their indexes, declared **last** in
      both models so `create_all()` and a migrated schema agree byte-for-byte in `sqlite_master`.
    - [x] `AuditLedger.append()` inherits the bound trace id, and `compute_hash()` commits to it **only
      when present**, so every chain written before this revision still verifies.
    - [x] `worker.py`: bind the job's stored trace id for the whole unit of work, and name it in the log
      line the worker emits for that job.
    - [x] `docker/prometheus-alerts.yml`: rules that reference only metrics this build actually emits.
    - [x] `tests/test_tracing.py`, `scripts/trace_e2e.py`, `make trace` / `make trace-mutations`, a CI
      job, and the four docs (README §2.3/§2.5/§5/§7, architecture, spec §6.5 + §13).
  - **Pre-declared verification standard.** No surface counts as covered unless the id is read back from
    the artefact, and each claim carries a mutation that could have failed it:
    1. *exemplar* — one real HTTP ingest carrying a chosen `traceparent`, then the OpenMetrics scrape that
    `trace_id` is found in; mutation `no-exemplar` (a shadow copy of the package whose renderer drops
    them) must turn the gate red.
    2. *ledger* — the `media.ingested` / `job.created` / `job.completed` rows for that job read over
    `/api/v1/admin/audit`, all carrying it, with `/audit/verify` green; mutation `tampered-trace` (an
    `UPDATE` of one stored id while the app runs) must make `verified=false`.
    3. *log line* — the substring read out of the *server process's* captured stderr, not out of a
    `caplog`; mutation `lenient-parser` (a shadow copy that trusts the header verbatim) must be caught by
    the hostile-header check.
    4. *rules* — every metric name in the file cross-checked against the live scrape; mutation
    `renamed-metric` must fail the same check. Plus the compatibility witness that a pre-`0004` ledger
    row hashes to a digest computed from the old payload, and the negative proof that a plain
    `threading.Thread` does **not** inherit the contextvar — which is the sentence that justifies the
    column existing.
  - *Not claimed until proven otherwise:* no distributed tracing backend, collector or metered APM is
    added (FC-5), no span tree — one trace id per request is what the AC asks for, and `docker/`'s
    compose files do not run Prometheus, so the rules file is shipped and parsed here rather than shown
    firing.
  - **Measured at close (2026-09-26), and only these figures.** `tests/test_tracing.py` = **108 collected,
    108 passed, 0 skipped** on *both* dialects (junit) — the clause-1/2/3 surfaces are covered in-process
    too, not just by the gate. Whole suite: **583 tests, 564 passed, 19 skipped** in 69.4 s on SQLite, and
    **583 passed, 0 skipped** in 85.5 s (93.6 s on a repeat) against real `postgres:16` + `valkey/valkey:8`,
    with **0** `svtest_*` databases surviving. `make trace` = **29 checks passed, 0 failed** in the shape
    that starts its own container *and* through `--url postgresql+pg8000://…`, which is the command the CI
    step actually runs; `make trace-mutations` = all four caught (**2 / 7 / 6 / 1** checks failing for
    `no-exemplar` / `lenient-parser` / `renamed-metric` / `tampered-trace`), again in both shapes.
    `synthverify alert-rules` = **RESULT: PASS**, 13 rules in 3 groups, 13 referenced names against 16
    declared metrics. `data/synthverify.db` migrated `0003` → `0004`; `audit-verify` still reports
    `VERIFIED 3 audit entries` for rows written before the column existed.
    `ruff` clean, `make lint && make freedom` exit 0, `alembic heads` = `0004`, and `.github/workflows/ci.yml`
    parses into **eight** jobs with the `tracing` job's **ten** steps.
  - **Corrections this task owes, written down rather than quietly fixed.** Writing the gate found three
    product defects, all in the path `AC-INFRA-6` names, and all three invisible to an in-process test:
    (1) `render_openmetrics()` emitted exemplars **without their braces** — a syntactically invalid
    exposition, so every real OpenMetrics scrape of this build would have failed; the gate now imports a
    strict reader rather than pattern-matching. (2) `metrics_in_expr()` read `sum(rate(x[5m])) by (path)` as
    naming two metrics, so the rules cross-check would have demanded a metric called `path`; label matchers,
    ranges, grouping and offsets are stripped first. (3) `synthverify worker` never installed the trace log
    handler — it has no lifespan to do it — so the root logger sat at `WARNING` with **zero handlers** and
    the completion line that clause 3 requires went nowhere; measured before the fix (an `info` printed
    nothing while a `warning` did) and fixed in `_cmd_worker`. Beyond those: an earlier session recorded the
    pinned pre-`0004` digest as `7f7a1e55c7aa…`, which **is not reproducible from this tree** — the shipped,
    measured literal is
    `976645573bfbb1c96f19ecc07530f00c68028d97018b390497faef66e84399e6`
    (`tests/test_tracing.py:956`, spec §6.5), and no file anywhere still carries the wrong value. And one
    claim was **refused** rather than written: mid-session reasoning "found" a masked-password failure at
    `tests/test_brokers.py:270` and a 9-skip Postgres leg; neither the string nor that junit file exists, so
    nothing was fixed and no defect was recorded.

- [x] **T44** `REQ-IDAM-3` / `AC-IDAM-3`: tenant isolation **proven**, not assumed. Verbatim acceptance
  text: *"A table-driven test enumerates all GET/POST routes with an org-B key against org-A resources and
  asserts `404` (never `403`, which leaks existence)."* Two words in that sentence set the scope: **all**
  (so the gate has to be generated from the surface, not from memory) and **never** (so a refusal must not
  distinguish "not yours" from "does not exist" — which is only expressible if no role short-circuits
  tenancy).
  - Sub-checkboxes, in the order they were landed:
    - [x] `tests/test_tenancy_matrix.py` written **first**, run against shipped code, and the failures read
      before anything was fixed. It found five leaks, listed under "measured" below.
    - [x] `synthverify/auth.py`: `is_platform_scoped()`, a role-blind `visible_to()`, `org_clause()`,
      `not_found_or_self()` and `require_platform(*roles)` / `require_platform_admin`;
      `platform_organisation()` (the reserved-name design) deleted.
    - [x] `synthverify/db.py`: `ApiKey.platform_scope`, declared **last** and **nullable**, surfaced in
      `to_dict()`.
    - [x] Alembic `0005`: guarded `batch_add_column` + `UPDATE api_keys SET platform_scope = true WHERE
      key_id = 'bootstrap' AND role = 'admin'`; `downgrade()` drops the column.
    - [x] `routes_admin.py`: `KeyCreate.platform_scope` with a model validator refusing it unless
      `role == admin`; the reserved-organisation rejection and the two `_not_platform` helpers deleted;
      the audit `detail` records the flag.
    - [x] `routes_jobs.py` / `routes_media.py`: `_get_job_or_404` as the single enforcement point (five job
      routes incl. the previously unchecked `reanalyze`), `org_clause` on the collection route,
      **per-organisation** content dedup and **per-organisation** idempotency lookup.
    - [x] `ratelimit.py`: the metric label that named each subject removed from the unauthenticated
      `/metrics`; only `authenticated=true|false` remains.
    - [x] `scripts/tenancy_e2e.py` (shadow-copy mutation harness), `make tenancy` / `make tenancy-mutations`,
      the CI `tenancy` job, and the docs pass (README §2.3/§2.5/§4/§6 item 24/§7, `docs/security.md`,
      `docs/api.md`, `docs/architecture.md` §Tenancy, dashboard copy, spec §6.6/§9/§13).
  - **Pre-declared verification standard**, kept: no case counts as coverage unless the expectation came from
    the schema rather than from a human list, and no fix counts unless a mutation that re-introduces the old
    behaviour makes a named test fail.
  - **Measured at close (2026-09-26), and only these figures.**
    `tests/test_tenancy_matrix.py` = **41 collected, 41 passed, 0 skipped** on *both* dialects (junit
    `tests=41 failures=0 errors=0 skipped=0`; 22.6 s SQLite, 27.9 s `postgres:16` + Valkey) — 1 completeness
    case against `create_app().openapi()`'s **32 operations**, 5 cross-org `404`-not-`403` cases, 19
    control-plane refusals, 4 public-endpoint cases, 2 ingest-stamping cases, and 10 named-property cases
    (dedup, idempotency, `platform_scope` needs admin, a minted platform key works, the magic org name
    grants nothing, tenant-admin reaches no platform surface, sync analyze persists nothing, the limiter
    label, the dashboard shell).
    `make tenancy` = **`[baseline] 41 cases, 0 failing → RESULT: PASS`**.
    `make tenancy-mutations` = **all six caught**, as failing cases out of 41: `admin-bypass` **6**,
    `existence-oracle` **6**, `shared-dedup` **2**, `idempotency-global` **1**, `control-plane-open` **21**,
    `subject-label` **1**; each run prints the `caught by:` test ids, and the harness refuses to no-op
    (`mutation … is stale` if its quoted source has drifted). No `sv-tenancy-*` scratch left behind
    (`find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'sv-tenancy-*'` → empty), which the CI job also asserts.
    Whole suite: **624 tests, 605 passed, 19 skipped** in 91.9 s on SQLite defaults, and
    **624 passed, 0 skipped** in 131.9 s against real `postgres:16` + `valkey/valkey:8` with **0** `svtest_*`
    databases surviving (junit `/tmp/t44-final-sqlite.xml`, `/tmp/t44-final-pg.xml`).
    `data/synthverify.db` migrated `0004` → `0005` in place — `platform_scope` appended as the last physical
    column, its one pre-existing `bootstrap`/`admin` row promoted to `1` — after which
    `synthverify audit-verify` still reports **`VERIFIED 3 audit entries`**. `ruff check` clean,
    `alembic heads` = `0005`, and `.github/workflows/ci.yml` parses into **nine** jobs.
  - **Corrections this task owes, written down rather than quietly fixed.** (1) Five product defects, all in
    shipped code, none visible to the previously-green suite: `POST /jobs/{id}/reanalyze` had **no tenancy
    check at all** (a *write* into another tenant, and the only job route not going through the shared
    helper); `role: admin` bypassed tenancy on **every** job route; `MediaAsset` dedup matched on `sha256`
    alone, so a second tenant's upload reused the first's row and had that tenant's **filename and asset id
    inlined into its own report** — a leak on the path where the product *succeeds*, which is why no
    "my verdict is correct" test could see it; an `idempotency_key` was a global handle, so a replay
    returned *another organisation's* job; and the limiter put a per-subject label on the unauthenticated
    `/metrics`. (2) A design was replaced, not just tested: the first cut promoted the bootstrap key through
    a **sentinel `organisation` value**, which would have made a tenant name a capability, so the matrix now
    mints a key whose organisation **is** that string and asserts it receives nothing. (3) An intermediate
    model of `platform_scope` used a `NOT NULL` column with `server_default=sa.false()`; that forces a table
    rebuild in SQLite batch mode, which needs a live connection, and it broke
    `test_cli_print_sql_emits_the_whole_schema_without_creating_a_database` with
    `CommandError: This operation cannot proceed in --sql mode` — the reason the column is nullable,
    documented in both `db.py` and `0005`. (4) Two pre-existing webhook tests
    (`tests/test_admin_webhooks.py::test_signed_delivery_on_completion`, `::test_delivery_ledger`) had been
    *passing for the wrong reason* under the magic-string model — the reserved name was matching in webhook
    and policy lookups — and they are green again now that `organisation` is a pure tenant label. (5) The
    completeness gate is bounded by the schema, and the schema is incomplete: `/dashboard` is a static mount,
    so it appears in no OpenAPI document. Rather than leave that surface unproven it got
    `test_dashboard_shell_carries_no_credential_and_no_tenant_data`, which asserts the served HTML carries no
    key, no job/asset id, no organisation name and no `sv_live_<32 hex>` — and which failed on its first run
    because the shell's own *placeholder* text contains `sv_live_…`, so the assertion is a regex for a
    complete key rather than a substring check.
  - **Not claimed.** This proves *tenant* isolation for the credential type that exists. It is not
    `REQ-IDAM-1` (JWT/OIDC) or `REQ-IDAM-2` (scopes), and it does not bound ledger verification time
    (`AC-IDAM-4`). `403` still exists in the API — for a missing capability, per the spec's own
    `AC-IDAM-2` wording — and there is **no per-organisation admin** in v1, which is a recorded
    interpretation (spec §13) rather than an oversight. No hosted runner executed the `tenancy` job
    (interpretation 6): its baseline, six mutation steps and residue assertion were run here.

---

- [x] **T46** `REQ-INFRA-5` / `AC-INFRA-5`: a retention TTL that a scheduler enforces, a pin can stop, and
  a second pass cannot repeat. Verbatim acceptance text: *"A retention test: with a per-org TTL configured,
  past-due assets and their jobs are swept by the scheduler while (a) a legal-hold pin on an audit-relevant
  resource blocks its deletion and is itself recorded in the ledger, and (b) the sweep is idempotent —
  running it twice deletes nothing twice and reports the same set."* Three claims in one sentence, and the
  third is the one that decided the design: **idempotent** over *content-addressed* storage means the plan
  has to count references rather than rows, because since T44 several tenants' rows can name one object.
  - Sub-checkboxes, in the order they were landed:
    - [x] Alembic `0006` (`0006_retention.py`): `retention_policies` (one row per organisation,
      `media_ttl_days`) and `legal_holds` (`resource_kind` `media`|`job`, `resource_ref`, `organisation`,
      `reason`, `active`, `created_by`, `released_at`, plus a `(kind, ref)` index); `downgrade()` drops
      both. `tests/test_migrations.py`'s `HEAD_REVISION` → `0006` and `TABLES` covering both new tables.
      Releasing a hold is a **soft** delete, because the row is the record that a hold was in force over a
      period — a vanished pin cannot be shown to have existed.
    - [x] `synthverify/retention.py`: `effective_ttl_days()`, `active_hold_for()` (oldest pin first, so a
      pin's `reason` is attributable), `plan_sweep()`, `_plan_objects()` (the reference count over
      `media_keys` and artifact names), `apply_sweep()` (rows + the `retention.swept` ledger entry, caller
      commits), `remove_storage()` (bytes, after that commit), `sweep_once()` and `_lock_sweep()`.
    - [x] Seven platform-scoped admin routes: `GET`/`PUT`/`DELETE` `/retention/policies[/{org}]`,
      `GET`/`POST` `/retention/holds`, `DELETE /retention/holds/{hold_id}`, `POST /retention/sweep` with
      `dry_run` defaulting to **true** — all four `AC-IDAM-3` rules honoured, and the completeness gate in
      that matrix is what put them in the table (41 → 48 cases, unasked).
    - [x] `synthverify retention-sweep [--apply] [--organisation] [--limit] [--json]`: preview unless
      `--apply`, same `sweep_once()` as the thread and the route.
    - [x] `WorkerFleet`'s `sv-retention-sweep` thread: **sleeps before its first pass**, interval treated as
      a floor (`max(1.0, …)`), a failed pass logged and counted rather than fatal, and the import of
      `sweep_once` inside the loop body so the scheduler is testable without patching the package at import.
    - [x] Four counters (`sweeps_total{dry_run}`, `deleted_total{kind}`, `held_total{kind}`,
      `sweep_errors_total`) in `HELP_TEXTS`, **no `organisation` label** (T44's finding about an
      unauthenticated scrape), and the 14th shipped alert rule `SynthVerifyRetentionSweepFailing`.
    - [x] `scripts/retention_e2e.py` — shadow-copy harness, thirteen `--mutate` modes, `--check-anchors`,
      `--skip-baseline`; `make retention` / `make retention-mutations`; the 20-step CI `retention` job.
    - [x] Docs pass: README §2.3/§2.5/§5/§6 (item 25)/§7, `docs/architecture.md` §Retention, `docs/api.md`,
      `docs/security.md`, `.env.example`, spec §6.7/§9/§13.
    - [x] **Close-out witnesses for the three claims that were only prose** (see corrections (6)/(7)/(10)):
      `tests/test_audit_concurrency.py::TestTheSweepBarrier` (3 cases),
      `tests/test_media_store.py::TestSweptBytesLeaveTheStore` (4 cases, both backends), and
      `tests/test_retention.py::TestTheStoreRefuses` (2 cases: the post-commit refusal raises *after* the
      deletes, and no later pass can reclaim the orphan it leaves).
  - **Pre-declared verification standard**, kept: a deletion feature is proved by what it *refuses* to
    delete, so every claim had to have (i) a case that pins the resource and shows it survives, (ii) a
    mutation that removes the guard and is caught by a *named* test, and (iii) for anything the docs assert
    in prose, a witness read off the running system rather than off the source. The two prose-only claims
    found at close-out were upgraded to tests in this same task rather than left as a note.
  - **Measured at close (2026-09-26), and only these figures.** These are the numbers from the re-measurement
    the close-out forced (corrections (10)/(11)), not from this task's first close; the superseded set is
    preserved in the History paragraph at the top of this file.
    `tests/test_retention.py` = **42 collected, 42 passed, 0 skipped** on SQLite (junit `tests=42 failures=0
    errors=0 skipped=0`, 49.2 s; 5 policy-is-opt-in, 5 sweep, 9 legal hold, 4 idempotence, **2
    `TestTheStoreRefuses`**, 4 scheduler, 7 API surface, 1 metrics, 1 CLI, 4 `TestTheTestsCanFail`), and
    **42 of the 112** in `tests/test_retention.py tests/test_tenancy_matrix.py tests/test_migrations.py` on
    `postgres:16` + Valkey (**112/112 passed, 0 skipped**, 91.5 s — 42 retention + 48 tenancy + 22 migrations).
    `make retention` = `--check-anchors` **PASS (all 13 mutation anchors quote exactly one place in the
    source)** then **`[baseline] 42 cases, 0 failing → RESULT: PASS`**.
    `make retention-mutations` = **all thirteen caught**, exit 0, as failing cases out of 42: `no-hold` **8**,
    `job-pin-ignored` **1**, `shared-object` **1**, `shared-artifact` **1**, `dry-run-applies` **3**,
    `ttl-never-reads` **25**, `audit-no-detail` **1**, `inflight-undeferred` **1**, `scheduler-dies` **1**,
    `scheduler-uncounted` **1**, `scheduler-interval` **8**, `storage-swallows` **2** (both
    `TestTheStoreRefuses` cases), `sweep-counted-late` **1** (the delta assertion). Two counts moved with the
    widened denominator and must be read as what they are: `ttl-never-reads` 23 → **25** because both new
    cases are past-due cases, and `scheduler-interval` **13 → 8**, which is *not* stable — the mutation makes
    a sweep thread spin against a shared SQLite file, so repeats measured **8, 12, 12**. The gate's contract
    is the verdict (`RESULT: MUTATION CAUGHT`), never the count, and the count is reported here as a range
    rather than smoothed to a single number.
    No `sv-retention-*` scratch survived
    (`find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'sv-retention-*'` → no matches).
    `tests/test_tenancy_matrix.py`, re-measured because T46 widened it = **48/48 passed, 0 skipped** on both
    dialects (26.4 s SQLite / 33.6 s Postgres, junit `tests=48 failures=0 errors=0 skipped=0`), against
    `create_app().openapi()`'s **39** operations (**26** control-plane, **5** cross-org `404`, **4** public,
    **2** ingest-stamped, 11 named-property cases); `make tenancy-mutations` = all six caught at **6 / 6 / 2
    / 1 / 28 / 1** of 48.
    `tests/test_audit_concurrency.py` + `tests/test_brokers.py` = **29 passed, 0 skipped** on Postgres
    (8.4 s) / **18 passed, 11 skipped** with a reason each on SQLite (1.6 s). The three new barrier cases, on
    the Postgres leg: baseline shadow run **`tests=7 failures=0 skipped=0`**, `lock-skipped` (the
    `_lock_sweep(session)` call deleted from `sweep_once`) → **1 of 7**, caught by
    `test_the_shipped_sweep_takes_the_barrier_before_it_plans`; `lock-neutered` (the dialect guard inverted so
    the statement never runs) → **1 of 7**, caught by
    `test_a_replica_that_locks_for_the_barrier_waits_for_the_other_sweep`.
    `tests/test_media_store.py` = **61 passed, 0 skipped** on both dialects (17.7 s / 18.2 s), including the
    four new `TestSweptBytesLeaveTheStore` cases; its teeth probe replaced `store.delete(store.location(k))`
    with `Path(store.location(k)).unlink()` on a shadow copy → the two `local` cases stayed green and
    `test_a_planned_object_is_removed_from_the_store_that_holds_it[s3]` failed, i.e. the witness is the
    object store and nothing else.
    Whole suite: **680 tests — 659 passed, 21 skipped** in 151.8 s on SQLite defaults (junit
    `tests=680 failures=0 errors=0 skipped=21`), and **680 passed, 0 skipped** in 193.1 s against real
    `postgres:16` + `valkey/valkey:8` (junit `tests=680 failures=0 errors=0 skipped=0`). Re-run under the
    database sampler on that leg to check the cleanup claim rather than assert it: **8 086 samples at 20 ms
    over 194.7 s, 210 distinct `svtest_*` databases seen, peak 2 alive at once, and 0 surviving after the
    run** (`{0: 1130, 1: 6954, 2: 2}` — two samples caught the overlap the concurrency tests create).
    `make lint` clean · `make alerts` = **PASS, 14 rules in 3 groups, 14 names, all among the 20 declared,
    0 issues** · `make freedom` = **PASS, 20 declared roots** (T46 added no dependency), with its offline
    clause measured: `tests/test_offline.py -m offline` = **12 passed, 0 skipped** in 4.8 s ·
    `alembic heads` = **`0006`** · `data/synthverify.db` migrated `0005` → `0006` in place, `legal_holds` and
    `retention_policies` both present and **empty**, and `synthverify audit-verify` on that file reporting
    **`VERIFIED 3 audit entries`** (head `dbf5ab4ccce5ba0d…`) · `.github/workflows/ci.yml` parses into **ten**
    jobs with the `retention` job at **20** steps.
  - **Corrections this task owes, written down rather than quietly fixed.** (1) **One shipped-code defect:**
    `POST /admin/retention/holds` answered `201` for a fabricated `resource_ref`, because the job branch
    collapsed existence to a `bool` (`exists = session.get(Job, ref) is not None`) and the guard then tested
    `if exists is None` — `False is not None`, so every missing job passed. A hold that names nothing is
    worse than no hold: it reads as protection in the audit trail. Existence is now a real `select(...).
    scalar_one_or_none()`. (2) **A wrong root-cause note from the session that found it** — "`session.get`
    never returns `None` for a String primary key" — measured false: `session.get(Job, 'does-not-exist')`
    returns `None` exactly as documented, and the defect was only the type mismatch. The note is corrected
    here rather than reused. (3) **`legal_hold: None` in the ledger:** the primary key is a Python-side
    default, so it does not exist when the row is constructed; the first `legal_hold.created` entries named
    no hold until `session.flush()` was added before the append. (4) **A flake this task created and then
    had to earn back:** the scheduler case configured a 0.2 s interval and slept `1.0` s, racing the loop's
    own one-second floor — red about one randomized run in five, never with `-p no:randomly`. It now waits
    for the event (two recorded passes on a 20 s deadline), and the assertions got **stronger**, not looser:
    the passes must also be spaced ≥ the interval apart, and the failures read off `/metrics` must *equal*
    the passes that raised. Three new mutations (`scheduler-dies`, `scheduler-uncounted`,
    `scheduler-interval`) exist to show those three assertions bite. (5) **A metric check that could not
    fail:** asserting a counter's *name* appears in a scrape is a substring match reading the same for one
    error as for twenty; `_counter()` now parses the sample's value. (6) **A documented claim with no test:**
    `architecture.md` and `worker.py` said the sweep's planning reads are serialised by an advisory lock, and
    nothing exercised `_lock_sweep`. It does now, including the pre-fix shape that proves the wait is the
    barrier's. (7) **The same, for the object store:** `remove_storage()` had only ever been driven against
    the filesystem, where "unlink the file" and "issue a signed `DELETE /bucket/key`" look identical from a
    test. Asserted on both backends now, and caught there by a filesystem-only fake. (8) **An overstatement
    corrected mid-flight:** this task's own gate was reported as finished while it was still on mode G of
    eleven (thirteen now); the claim was retracted and the run read to completion (`pgrep` plus the log)
    before any number was written into a doc. (9) The modes used to each re-run the unmutated baseline first —
    twelve identical 40-case runs per gate when there were eleven modes, fourteen today — which is why
    `--check-anchors` and `--skip-baseline` exist: stale anchors now raise in a second instead of no-opping,
    and the baseline certifies once. (10) **A shipped docstring describing the wrong failure shape, and a
    README draft that copied it:** `remove_storage()` claimed storage failures are "reported rather than
    raised", which is true of artifact files and false of objects — a store that refuses a key raises
    `MediaStoreError` *after* the commit, because "we deleted it" and "the bucket said no" are different
    statements about evidence and only the first belongs in a report field. No test drove the refusal, so the
    prose could not have been falsified; `_RefusingStore` drives it now, the docstring matches the code, and
    the claim carries two cases and two mutations (`storage-swallows`, `sweep-counted-late`) instead of a
    sentence. (11) **Two counters disagreeing about one event, and an assertion too weak to notice:**
    `retention_sweeps_total` was incremented after `remove_storage()`, so a pass whose rows went and whose
    bytes refused left `retention_deleted_total{kind="media_row"}` moved against zero recorded sweeps. The
    increment sits at the commit now. And the first draft of the test that caught it asserted
    `sweeps >= 1.0` on a **process-global** counter — satisfied by any earlier successful sweep in the same
    process, and therefore unbreakable by `sweep-counted-late`; re-expressed as a before/after delta around
    the refused pass, that mutation fails it as exactly one case.
  - **Not claimed.** The sweep is exercised against the filesystem and against the S3 *protocol* through a
    loopback mock that re-verifies signatures independently — not against a running MinIO. One pass holds one
    transaction, so a very large due set converges over passes under `SV_RETENTION_BATCH_LIMIT` rather than
    being sharded across replicas; the advisory lock makes replicas take turns, it does not split work.
    `AC-INFRA-5` says nothing about *what* the TTL should be, and this task does not either:
    `SV_RETENTION_DEFAULT_DAYS` ships unset, so an install that configures nothing deletes nothing. No hosted
    runner executed the `retention` job (interpretation 6): its anchors, baseline, thirteen mutation steps and
    residue assertion were run here. **And what the sweep does not reach, found while writing the limitation
    rather than before shipping:** `webhook_deliveries` rows survive their job (`job_id` is an indexed column,
    not a foreign key, and a grep of every `delete(` call site in `synthverify/` shows nothing issues a
    `DELETE` against that table), and `payload` holds the full `job.to_dict(include_result=True)` — so swept
    evidence keeps a findings copy there; swept rows leave their `retention.swept` event and every prior
    ledger entry in the chain, which is the append-only property, not an oversight. Both are now in
    `README.md` §7 and spec §6.7 as limitations; an erasure requirement would be new §4.3 text with its own
    acceptance criterion, which is a decision this task does not own.

- [x] **T47** `REQ-IDAM-4` / `AC-IDAM-4`: a checkpointed chain that verifies 1 M events inside the clause —
  **measured, not asserted**. Verbatim acceptance text: *"Benchmark proves chain verify of 1 M events < 60 s
  single-threaded, and a checkpoint-insertion test detects tampering inside a checkpointed range."* Two
  clauses that ask for different instruments — a *benchmark* for the first, a *test* for the second — so
  running only the suite would have left the sentence half-proved. Full narrative: spec §6.8; every figure:
  the four `AC-IDAM-4` rows of `README.md` §2.5.
  - Sub-checkboxes, in the order they were landed:
    - [x] Alembic `0007` (`0007_audit_checkpoints.py`): `audit_checkpoints` (a seal names the range it closes
      via `prev_seq`, records `events_in_range`, the `head_hash` the range reaches, its own `chain_hash` over
      the sealed record, and `entries_hashed`) plus the `chain_hash` index — **and it writes no rows**, so a
      migrated install has a schema and no fixed points until it appends or backfills. `HEAD_REVISION` →
      `0007`, `TABLES` covering the new table.
    - [x] Sealing **inside the append transaction** every `SV_AUDIT_CHECKPOINT_EVERY` (5 000 by default),
      opt-out, and rolling back together with the append it sealed — a seal that can outlive its event is a
      fixed point over a range that never existed.
    - [x] `AuditLedger.verify()` cross-checking four invariants per seal (the sealed head, the seal chain, the
      seal's own columns against its digest, the recorded range size), reading ten columns through a
      server-side cursor, and reporting `entries_checked` beside `checkpoints_checked` so "did the scheme let
      you skip work?" is answerable from the response.
    - [x] `synthverify audit-checkpoint [--backfill] [--every N]`, which **verifies before it seals** and
      refuses to seal a chain that does not check out.
    - [x] `scripts/ledger_bench.py` (both read shapes on every run, four tampering probes, one phase per
      process so each peak RSS is attributable) and `scripts/ledger_e2e.py` (shadow copy, eleven `--mutate`
      modes, `--check-anchors`, `--skip-baseline`); `make ledger` / `make ledger-postgres` /
      `make ledger-mutations`; the 19-step CI `ledger` job.
    - [x] `tests/test_audit_checkpoints.py` — 36 cases: 5 sealing, 7 verify-contract, 9 tampering inside a
      sealed range, 4 backfill, 10 `TestTheDigestRule` (a pinned `BASE_DIGEST`, one per covered column, the
      pointer *in* the digest, `trace_id` only when present), 1 `slow` 100 000-row clause.
    - [x] Docs pass: `README.md` §2.3/§2.5/§5/§6 (item 26)/§7, `docs/architecture.md`, `docs/api.md`,
      `SECURITY.md`, spec §6.8/§9/§13.
  - **Measured at close (2026-09-27), and only these figures.** `make ledger` and `make ledger-postgres` =
    `RESULT: PASS`, exit 0 on both dialects. SQLite (idle floor 60.0 MiB): 1 000 000 events in 69.8 s, sealed
    into **200** ranges in 14.9 s by the shipped backfill, verified in **11.38 s at 65.5 MiB (+5.5)** where the
    read it replaces took **17.72 s at 2 391.9 MiB**. `postgres:16` 16.15 (floor 65.8 MiB): insert 499.4 s,
    seal 18.4 s, verified in **16.40 s at 69.3 MiB (+3.5)** against **23.18 s at 2 461.8 MiB**.
    `entries_checked == 1,000,000` on both legs. `tests/test_audit_checkpoints.py` = **36 passed, 0 skipped**
    on both dialects (5.3 s SQLite, 62.3 s Postgres). `make ledger-mutations` = `--check-anchors` **PASS
    (all 11)**, baseline **35 cases, 0 failing**, **all eleven caught** (failing cases out of 35:
    `no-seal-on-append` 12, `wrong-seal-head` 13, `seal-head-unchecked` 2, `seal-chain-unchecked` 1,
    `seal-hash-unchecked` 1, `range-count-unchecked` 1, `tail-seal-unchecked` 1,
    `backfill-without-verifying` 1, `no-stream-cursor` 1, `digest-ignores-detail` 6, `digest-ignores-prev` 4),
    and fed a build with `stream_results` removed **the benchmark itself** fails at 50 000 events
    (`materialising allocated 120.0 MiB over the floor, streaming 59.6 MiB`) — a mutation check of the
    instrument, since the verdict is identical either way. No `sv-ledger-*` scratch survived.
  - **What the criterion was actually about.** The time clause was already met before any of this
    (18.35 s → 13.67 s and 23.18 s → 16.40 s on the two measured pairs); **the memory was the defect** — an
    admin endpoint allocating proportional to the ledger's size is a denial of service an auditor hands
    themselves. Hence the bench prints both read shapes on every run, and the memory clause is written as a
    delta over a measured idle floor so it can fail at small sizes.
  - **The consequence §4.4 does not ask about, and the task it created**: making the walk measurable produced
    the app's first request that can run for tens of seconds, in an `async def` over a synchronous session —
    so the walk held the **event loop**. Delivered as **T51** below.

---

## Release pass (v1.0.0) — making it publishable, and what a stranger found

**Scope, as fixed by the user:** the release form is a **clone-and-run download** (`like openclaw, omniroute`),
not a hosted service. That choice decides what "complete" means here: the install path has to work from a bare
interpreter on four OSes, the docs have to answer the questions a first-time reader asks in order, and every
published number has to be re-measured rather than inherited. **Push/remote operations stay user-gated**, so
this section ends at `git init` + tag + artifacts on disk.

- [x] **T51** *(`docs/goal-spec.md` AC-IDAM-4's recorded consequence; scheduled by measurement, not by
  reading §4.4)* Keep long-running requests off the event loop — **as a class**. Verbatim from spec §13's
  record of it: *"The fix is one word — hand the handler to Starlette's threadpool — attached to a decision
  that reaches every route in the file."* It was a decision, so it landed as its own tranche-shaped task
  rather than inside `AC-IDAM-4`'s close-out.
  - Sub-checkboxes:
    - [x] **`grep` before fixing.** `synthverify/` contains **no `asyncio` call at all**, and 40 `async def`
      FastAPI handlers with four `await` sites between them — every one of them a coroutine that then does
      synchronous SQLAlchemy, storage, subprocess or network work. The defect was therefore the *shape*, not
      the route `AC-IDAM-4` happened to name.
    - [x] The **seven** endpoints whose work is bounded by data volume or by an outbound call converted from
      `async def` to `def`, so Starlette dispatches them to its thread pool: `POST /media/ingest`,
      `POST /media/ingest/batch`, `POST /media/analyze`, `POST /jobs/{job_id}/reanalyze`,
      `POST /admin/webhooks/{webhook_id}/test`, `GET /admin/audit/verify`,
      `POST /admin/retention/sweep`. The other thirty-three stay coroutines: a query that returns in
      milliseconds is not starvation risk, and converting them all would have been churn dressed as rigour.
    - [x] The rule stated **where it can be read** — a module comment in `routes_media.py` and a comment above
      the three slow `routes_admin.py` handlers naming what each blocks on (`SV_WEBHOOK_TIMEOUT_SECONDS`,
      the ledger walk, one object-store round trip per expired asset) and why a coroutine there is a frozen
      replica rather than a slow request.
    - [x] `UploadFile` contract checked, not assumed: the sync form is `upload.file.read()`, and it is correct
      because Starlette 0.52.1's `MultiPartParser` rewinds the spooled temp file (`await part.file.seek(0)`)
      and `UploadFile.read()` does not seek. Pinned by
      `tests/test_event_loop.py::test_upload_bytes_reach_a_sync_handler_at_position_zero` and by the
      empty-body case, so a future Starlette that stops rewinding fails loudly instead of returning 422s.
    - [x] `tests/test_event_loop.py` — **13 cases**: the seven endpoints held at their work step
      (`run_pipeline`, `submit_job`, `deliver_now`, `AuditLedger.verify`, `sweep_once`) while `/healthz` is
      probed, one case asserting the released handler returns a **real report** rather than a stub, the
      upload-position and empty-body contracts, the two control cases, and one structural case asserting
      `not inspect.iscoroutinefunction` for all seven `(method, path)` pairs.
    - [x] **The instrument proven able to fail, twice over.** A probe issued against a frozen loop answers in
      a millisecond *the instant it unfreezes*, so the first draft of the control was vacuous; the harness now
      also treats "the handler reached its work step and completed without ever being released" as the failure
      it is. Control measurements (`/tmp/sv-loop-probe.py`, two runs): a 1.0 s held work step behind
      `async def` stalls a concurrent `/healthz` for **1 005.3 ms** and **1 006.8 ms**; behind `def`,
      **3.6 ms** and **3.3 ms**. The original defect measurement, from `AC-IDAM-4`'s probe with a 2.0 s stub in
      place of the ledger walk, was **1.63 s** against a `/healthz` route that touches no database.
    - [x] **Blast radius checked: zero.** No new dependency, no new route, no new metric, no migration, no
      change to any verdict. One new test module.
  - **Verified by re-running the gates that touch the request path** (because a thread pool is a different
    context-propagation story, and `contextvars`-based trace IDs cross the hop or `make trace` says so):
    `make trace` = **29/29 checks PASS**, `make scale` = **15/15 PASS**, `make ratelimit` = **21/21 PASS**,
    `make tenancy-mutations` = **all six caught** (failing cases out of 48: `admin-bypass` 6,
    `existence-oracle` 6, `shared-dedup` 2, `idempotency-global` 1, `control-plane-open` 28,
    `subject-label` 1), `tests/test_event_loop.py` = **13 passed, 0 skipped**, and the full suite on both
    dialects — every count in the gate block at the top of this file.
  - **What this task found on the way, and fixed inside itself:** `make postgres-e2e` had been crashing since
    `1.0.0` was tagged. See **T52**'s fifth release-pass defect; it is recorded there rather than folded in
    here, because it was not caused by T51.
  - **Not claimed.** The seven handlers now occupy **worker threads**, and Starlette's default pool is bounded
    (`anyio` capacity limiter, 40 tokens): seven concurrent 1 M-event walks would queue behind each other
    instead of freezing the process. That is the correct failure shape — a slow endpoint rather than a dead
    replica — but it is not an argument for putting `/admin/audit/verify` behind a load balancer's health
    check, and `SECURITY.md` says so in those terms. Pool size is not configurable in this build, and no
    measurement of throughput under a saturated pool was made here.

- [x] **T52** **The release pass itself: run the install the way a stranger would, and re-measure every
  published number.** No acceptance text in `docs/goal-spec.md` asks for this — `FC-1..FC-11` and the
  `AC-*` clauses ask what the *product* must do — so the criterion was written by the user's release
  decision: someone clones this, runs `make setup && make test`, and it must work on their machine.
  - **Defects found by that method and fixed in `1.0.0`** (all ten in `CHANGELOG.md`, each with its own
    witness): `make setup` installed `.[dev]` only while the freedom and lock gates grade every root, so a
    clean clone reported 14 failures out of the box and CI could not see it; `--system-site-packages` is what
    had hidden that (a Homebrew `scipy` satisfying a pin the lock never described); the shipped image installed
    `.[vision]` while `docker-compose.yml` documents `SV_RATE_LIMIT_BACKEND=valkey` as the scale-out path, so
    the first request after configuring the documented backend raised at limiter construction; the first-boot
    admin key sat on disk at the process umask between `write_text()` and `chmod()`; **every long-running
    handler occupied the event loop** (T51, found by `grep` after `AC-IDAM-4` made one of them measurable);
    **`make postgres-e2e` had been crashing since the tag** — it asserted that two organisations
    uploading the same bytes yield *one* `MediaAsset` row, which was true before T44 changed the contract to
    per-digest **and** per-organisation, so `session.scalar_one()` raised `MultipleResultsFound` before any
    check could print. The last of those was proven **pre-existing** by re-running the identical script against
    a pre-T51 clone (`/tmp/sv-clone`): same crash, same line, which is the difference between a regression and
    a gate nobody executed. It now asserts both halves of the contract and reports
    **16 checks passed, 0 failed**. And **the console's Action column could never have rendered a value**: the
    queue table read `j.result.verdict.recommended_action` from a payload that omits `result` by design
    (`include_result=False`), so every row printed an em dash under a header promising a routing decision,
    while the detail panel one click away showed the same verdict correctly. Then three that only a *different
    pair of eyes or a different machine* could surface: **`requires-python` promised 3.10 and the package cannot
    be imported on 3.10** (`synthverify/db.py` imports `enum.StrEnum`; measured on `python:3.10-slim` as
    `ImportError: cannot import name 'StrEnum' from 'enum'` — `pip install` succeeds, so the failure landed on
    the user who trusted the metadata), **three assertions plus one test double only worked on the machine that
    wrote them** (see the portability bullet), and **`SECURITY.md` linked to two documents a reader cannot
    reach** (`](architecture.md)` and `](../README.md)` resolve on the author's tree and 404 from the repository
    root — found by the guard written for the class, which printed exactly those two when run before the fix).
  - **How the seventh was found, because it is the pass's whole argument:** by *looking at a screenshot*. The
    release pass drove a headless Chrome against the clean-clone server over the DevTools protocol (no new
    dependency; the driver is scratch code, not shipped) to capture the four README screens, and the queue
    table's blank column was visible in the image. No test could have caught it as tests stood: the API tests
    asserted the *detail* shape only, and the console is static HTML that nothing renders in a test. Fixed at
    the serializer (`Job.to_dict` derives the action from the stored verdict — `list_jobs` already loads each
    row whole, so the summary gains a field without gaining a query) plus the flat read in the table, and
    pinned by three cases in `tests/test_api.py::TestJobQueueSummary`: list value equals detail value, the key
    is present as `null` before a report exists so the row shape never varies by status, and the shipped
    console source must not read `j.result` for it. Each case was checked by undoing its own fix — the first
    two fail together on the serializer mutation (2 of 3), the third fails alone on the template mutation.
  - **The four README screenshots** (`docs/images/`) are from the clean-clone server described above: `make
    setup` on a fresh copy, `make serve`, seven fixture files POSTed to `/api/v1/media/ingest` with the
    bootstrap key that first boot wrote, and the console driven over CDP with that key read from the file at
    run time so it never appears in a command line. They are the reason the seventh defect exists as a
    finding rather than as a guess, and they are what a reviewer can re-take.
  - **The ninth defect class was found by running the suite somewhere else.** The pass executed the whole
    suite in `python:3.12` and `python:3.13` containers on `linux/aarch64` against the same `postgres:16` +
    `valkey/valkey:8` (`--network host`, loopback URLs, `pg8000` installed in-leg exactly as CI's `test` job
    installs it): **753 collected, 0 failures, 0 errors, 0 skipped** on each, twice — in 207.6 s and 250.2 s, and
    again on the final tree in 218.8 s and 215.6 s. Three
    things were wrong only off this laptop. A `check_lock` assertion hard-coded
    `["drift"]`/`["target-drift"]` by *position*: SQLAlchemy pulls `greenlet` into the installed closure on
    Linux and Windows but not on Apple Silicon, so which of the two comparisons catches a disagreeing pin is
    host-dependent — rewritten to name the package and accept either kind, then **mutation-checked both ways**
    by silencing each of the two `check_lock` reports in turn. A marker test asked whether the *host* was below
    a `python_version` marker, which inverted to `assert False is True` on 3.13; it now reads
    `sys.version_info`. And `tests/test_media_store.py`'s in-process S3 mock recorded a request *after*
    `end_headers()` flushed the reply, so a test that inspected `requests` as soon as the response arrived raced
    that thread — a race the host's timing had happened to win on every earlier run, and lost elsewhere. Every
    handler now logs before it replies; the witness is a 0.5 s delay injected into `_record`, under which all
    **61** cases still pass, and the counter-witness is the original ordering restored in `do_HEAD`, which fails
    **14**. **The method note is the point:** all three had passed on every one of the machine's earlier
    600-plus-test runs, and a green suite had inherited each of their premises from the code being tested.
    Also recorded here because it is the same lesson applied to this pass's own instrument: the `pg_database`
    sampler's first attempt printed `distinct=0 | sampler_errors=11067` beside a *plausible* histogram, because
    its polling closure raised on every iteration while still recording row counts — so `peak` and `histogram`
    were real measurements and `distinct` was vacuous. The instrument now exits non-zero when any poll raises,
    and `sampler_errors=0` travels with the number.
  - **What a stranger's install path got** (`tests/test_release_hygiene.py`, **21 cases**, each one a pin that
    fails when a doc, a Makefile line and the code stop agreeing): `make setup` / `make help` / `make doctor`
    (the latter names the missing extra or service instead of leaving fourteen gate failures to interpret);
    `docs/INSTALL.md` walked per-OS including Windows and the no-toolchain container; the Makefile,
    `pyproject.toml` and CI naming the **same** extras set; the image carrying the runtime extras and not the
    test toolchain; the README quickstart's commands existing as written; every relative link in every shipped
    Markdown file resolving to a path that is in the repository (the guard for that one failed its first
    full-suite run on the `CHANGELOG.md` entry *describing* the bug — a checker that treats `` `](x)` `` inside
    a code span as a link cannot report the syntax it detects, so it now skips code the way a renderer does,
    and was mutation-checked by appending a genuinely dangling link); `requires-python`, the `make setup`
    interpreter probe, the prose in three documents and the `classifiers` all naming **3.11** as the floor; the
    portability job running **every advertised interpreter on both laptop platforms**, which is what stops a
    classifier from reappearing that CI never executes; `py.typed`, `[project.urls]`,
    `LICENSE`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.gitattributes`, the PR/issue
    templates and `dependabot.yml` all present and non-stub.
  - **Re-measured rather than inherited** (the whole point of the pass — see the gate block at the top of this
    file for the current figures): both host suite legs with `--junitxml`, the two container legs with theirs,
    the sampler pass with its interval, `make freedom`, `make doctor`, `make lock-check`,
    `make airgap`, `make postgres-e2e`, `make tenancy-mutations`, `make trace`, `make scale`, `make ratelimit`,
    `synthverify licenses` / `model-manifests` / `dependency-lock` / `alert-rules` / `audit-verify`,
    `alembic heads` and `alembic current`, `pytest -m offline`, `pytest tests/ --collect-only -q` (which sums to
    753 across 22 files, so the 716 → 753 delta is attributable rather than asserted), and
    `.github/workflows/ci.yml` parsed by `yaml.safe_load` into its job list. Four literals moved in the process
    and are recorded where they were found: the workflow has **twelve** jobs (the eleventh/twelfth split
    happened when `portability` was added to test the install path on a bare interpreter), `alembic heads` is
    **`0007`** (the header block above said `0006` until this pass re-ran it), `make postgres-e2e` prints **16**
    checks where the block carried **15**, and `mypy` reads **`Found 21 errors in 12 files (checked 65 source
    files)`** where the README's row said 14 — that last one is a *documentation* fix, not a code fix: the
    number is informational (`make typecheck` ends in `|| true`), and clearing it is T50 rather than something
    to do inside a release pass, because every `synthverify/` change invalidates the four measured legs above.
  - **Release preparation, user-gated as always:** `git init`, one first commit, the `v1.0.0` annotated tag,
    `git archive` tarball + zip and `shasum -a 256` checksums **on disk**, and nothing pushed. The
    `https://github.com/aashish254/synthverify` URLs in `README.md`, `docs/INSTALL.md`, `CHANGELOG.md`, the
    issue template and `pyproject.toml`'s `[project.urls]` are the *declared* destination; they were not
    verified to exist, because verifying them would mean creating them.

**Not scheduled in this tranche, with the reason:** `REQ-IDAM-1/2` (M3's identity half, minus the tenancy
matrix T44 and the retention sweep T46 that just landed — unstarted code, not
a vendor gate: `AC-IDAM-1` asks for a *locally minted* JWT against a test-key Jwks, so it needs no running
provider, and MIT/Apache-2.0 verifiers are inside `ALLOWED_LICENSES`. It is unscheduled because it adds a
declared dependency and a second credential type to every route, which is a spec §13 decision rather than
something to slip in at the end of a tranche), and anything gated on
`OQ-1..OQ-6` (spec §12, user decisions). `REQ-INFRA-5` was in this sentence as "needs a policy decision that
is not in the spec"; it turned out to need no decision at all — the spec's refusal to name a number is
satisfied by making absence mean "keep it", so it is scheduled and delivered as **T46** above.
`AC-IDAM-4` was in this sentence as "a checkpoint scheme that does not exist yet"; it exists, and
`REQ-IDAM-4` is delivered as **T47** — which is also where **T51** came from, since the benchmark that proved
the criterion is what made the walk's effect on the event loop measurable.

---

## Not yet scheduled — why

| Spec area | Blocker |
|---|---|
| REQ-DET-1..8 (ML plugins, C2PA, perplexity) | M2; needs a weight-selection decision (`OQ-5`, `OQ-6`) — FC-3 disqualifies most public deepfake weights, so this is a research task, not a coding task |
| REQ-IDAM-1/2 (OIDC, scopes finer than three roles) | M3 — **unstarted, and not vendor-blocked**: `AC-IDAM-1` needs only a locally minted JWT against a test-key Jwks, and MIT/Apache-2.0 verifiers pass `ALLOWED_LICENSES`. Held back because OIDC lands a declared dependency plus a second credential type across every route, which is a §13 decision. (`AC-IDAM-3` was in this row too; delivered as **T44** — a test plus five fixes inside the credential model that already existed, needing no new dependency. **`REQ-INFRA-5` was its own row here** on the reading that it needed a retention-number decision §4.3 never makes; T46 delivered it without one, by making the absence of a policy row mean "keep it". **`REQ-IDAM-4`/`AC-IDAM-4` was the third item in this row**, described as "a 1 M-event benchmark with a checkpoint scheme that does not exist yet"; the scheme exists and the benchmark has been run on both dialects, as **T47**.) |
| REQ-PUBLIC-1..4, REQ-REACH-* | M4 — gated on `OQ-1` (eligibility) and `OQ-4` (who funds the community host) |
| REQ-RT-1..3, REQ-XAI-2/3, REQ-OPS-2/3 | M4/M5 |
| *Delivered since this table was written* | `REQ-INFRA-6` as **T41** (spec §6.5), `AC-IDAM-3` as **T44** (§6.6), `REQ-INFRA-5` as **T46** (§6.7), `REQ-IDAM-4`/`AC-IDAM-4` as **T47** (§6.8), and the event-loop consequence of T47 as **T51** plus the clone-and-run release pass as **T52** (spec §6.9). The first four were rows here, on readings that turned out to be mine rather than the spec's: each needed no decision, no vendor and no new dependency — only the work of enumerating what the criterion actually says |

Answering `OQ-1..OQ-6` is a user decision (spec §12); those milestones stay unconverted into tasks
until then rather than being guessed at.
