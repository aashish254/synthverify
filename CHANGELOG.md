# Changelog

All notable changes to SynthVerify are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file is generated from the work, not from intent: every entry below names the command that
grades the claim it makes. Where a claim is measured on one machine and gated on another, the entry
says which.

## [Unreleased]

No *release-blocking* work is queued ahead of `1.0.1`: the requirements through `1.0.0` are done, and
what remains below is open work, not a gate this version has skipped. That sentence's scope has since
moved — new `REQ-*` work has landed under **Added** since the tag: `REQ-DET-3`'s measurement machinery
(the thesis tranche) and `REQ-DET-5` / `AC-DET-5` (C2PA cryptographic provenance validation, the first
entry below). Both are *new* requirement work rather than a gate 1.0.0 skipped. The open
items are tracked in [`docs/goal-spec.md`](docs/goal-spec.md) §9 and in `TODO.md`:

- `REQ-IDAM-2` — scope-granted credentials (`jobs:read`, `media:submit`, …) alongside roles, with a
  scope-matrix test. **Delivered as T48** — see **Added** below.
- `REQ-IDAM-1` — JWT / OIDC subject tokens. This adds the first new runtime dependency since the licence gate
  was written, so it is an FC-1 decision as well as a feature. **Delivered as T49** — see **Added** below.
- `TODO.md` **T50** — the mypy half is **done**: the tree is at zero and `make typecheck` is now a gate (see
  **Added**). What remains under this number is the launch-proof pass and a `docs/OPERATIONS.md` runbook.
- `TODO.md` **T57** — the `calibration` marker comparing measured metrics against the committed manifest gates.
  It was queued behind **T59** because a gate needs a number to gate, and **T59** has now printed **28** of them
  on `commfor_eval_v1` (four detectors, each as a pooled held-out cell plus six reporting generator cells, with
  nine refusals beside them) and left **461** `calibration` rows in the committed split that nothing reads yet.
- `TODO.md` **T60** — a live row-losing collision: `ScoreTable.load()`, `samples()`, `scored_sample_ids()` and
  the runner's resume set key on bare `sample_id`, while the split layer now keys on `dataset/sample_id`. Two
  corpora sharing a filename drop a row and call it a duplicate.
- `OQ-1` … `OQ-6` — open questions that are operator weightings, not code.

### Added

- **`REQ-DET-5` / `AC-DET-5` — C2PA cryptographic provenance validation on ingest**, behind a new
  optional `c2pa` extra. Previously `metadata` only scanned for `jumb`/`c2pa` *byte markers*, which any
  attacker can splice in; it now walks the JUMBF superbox, decodes the CBOR claim, and verifies a real
  RFC 8152 `COSE_Sign1` (`Sig_structure = ["Signature1", protected, external_aad, payload]`, ES256 key in
  an RFC 9360 `x5chain`) against the embedded X.509 certificate, then checks the claim's SHA-256
  `c2pa.hash.data` hard binding over the container with the credential removed (PNG `caBX` chunk, JPEG
  `COM` segment). Four verdicts — `authentic-provenance` / `provenance-invalid` / `provenance-stripped`
  / `provenance-unverifiable` — each ride on the report as a first-class `provenance` field, gate the
  `CAMERA_ORIGIN_DECLARED` authenticity flag off when a credential fails (`AC-DET-5`), and are recorded
  in the hash-chained audit ledger as a `provenance.validated` event on both the worker and the
  synchronous `/analyze` path. Absent and unverifiable are kept *apart* from invalid, and no verdict ever
  raises: a malformed manifest or an air-gapped image without the extra degrades to a clear record.
  Scope is machinery-correctness (the validator's accept/reject/degrade logic), not byte-level interop
  with a third-party C2PA profile — ISO-BMFF box hashing is out of scope, stated in both modules.
  The first new runtime dependency since `REQ-IDAM-1`: `cbor2` (**MIT**, FC-1 clean from installed
  metadata), with `cryptography` promoted to a declared root. `make licenses` now reads **53 packages
  from 24 declared roots**, and `make lock-check` **PASS** at **55 pinned** (groups `core=11, dev=9,
  c2pa=2, jwt=1, valkey=1, vision=2`). `cbor2` is added to `dev` so CI/contributors grade it and the
  provenance tests run rather than skip; it is deliberately **not** in the shipped image, which serves
  `provenance-unverifiable` instead of crashing. Graded by `tests/test_provenance.py` (23 cases) with
  credentials minted by `tests/fixtures_c2pa.py`; two mutations (always-valid binding, always-valid
  signature) each turn the matching tamper test red, so the suite is load-bearing on both tamper vectors.

- **`synthverify/eval/` — the measurement half of the product.** AUC (Mann-Whitney, ties at half credit
  so a constant detector scores *exactly* chance), DeLong's interval computed in O(n log n) with
  `searchsorted` and published on the logit scale, interpolated EER, average precision kept distinct
  from AUC, equal-mass reliability bins with ECE defined as calibration-in-the-large, an
  `operating_point(max_fpr=…)` that answers `GOAL-2`'s question in one call or returns `None`, a seeded
  percentile bootstrap that refuses an interval it cannot support, per-generator breakdowns that mark
  their own thin cells, and `to_eval_report()`, whose output is validated by the same
  `synthverify.model-manifest/v1` code CI runs. numpy only — no `scikit-learn`, no `scipy`, so the
  metrics do not need the extra that the detectors do.
  Nothing in `metrics.py` reads a dataset — that half arrived afterwards as `datasets.py` — so the maths
  was checkable before a corpus existed, and every array these tests score is still hand-built in the
  test file.
- **`scripts/metrics_e2e.py`, `make eval`, `make eval-mutations`, and a thirteenth CI job
  (`evaluation`)** — the evaluator, evaluated. The ten ways a headline metric could be quietly wrong
  when this landed (inverted
  AUC, ties credited twice, DeLong off the Mann-Whitney identity, a clamped normal interval where a
  logit one is claimed, an exclusive decision threshold, an EER read off the far side of a jump, ECE
  switched to the decision convention, equal-width calibration bins, a percentile interval whose lower
  arm is the median, an operating point that ignores its own FPR cap) are each applied to a shadow copy
  of the package, and the suite has to go red. Graded by
  `make eval && make eval-mutations` = **anchors PASS 10/10, baseline 53 metric tests 0 failing, all ten
  caught**; `pytest tests/test_eval_metrics.py` = **53 passed**.
  Two of the ten survived their first draft and neither fault was in the module: every pre-existing
  tie test used continuous scores, so a wrong tie term in DeLong changed nothing any of them could
  see, and a percentile interval that starts at the median is still deterministic, still widens as n
  shrinks and still refuses thin data. Both now have a discriminating test.
- **`synthverify/eval/scoretable.py` — the append-only score ledger a night-long run writes into**, so
  the measurement half has a place to put a number before it has a number. Line-delimited CSV with
  `os.fsync` after every batch: a batch that landed survives `kill -9`, and a final line with no
  newline is recognised from the line itself and dropped rather than becoming a measurement. Loading
  keeps the **last** row per `(sample_id, detector)` — a bug fixed at 03:00 can be re-scored without
  deleting the day's work — and surfaces `duplicate_rows` and `torn_line` instead of applying that
  policy silently. `vectors(detector, status="ran")` is the analysis seam and the status filter is its
  whole point: `SKIPPED` and `ERROR` rows carry `score = 0.0` because `DetectorResult` must carry *a*
  number, and on this scale 0.0 reads as "confidently authentic", so leaving them in would inflate AUC
  with work the detector never did. `latency_ms()` is `GOAL-1`'s column over every status.
  `vectors_by_group(group_by="generator")` was described here as RQ1's table until the CLI ran it and
  found that a generator directory is one class by construction, so no grouping alone can produce a cell
  with an AUC in it; `ScoreTable.cells_against_reals()` is that table, arrived with **T56** below.
  **The format was chosen by measuring, not inherited from the plan**, which said gzip-CSV: at ~9,000
  samples × ~12 detectors the table is ~108 k rows and under 10 MB, so compression buys nothing, while
  appending a second gzip member leaves members 2..n without a readable header and recovering from a
  mid-member truncation needs `GzipFile`'s private per-member offset — the exact durability property the
  format exists for. Graded by `pytest tests/test_eval_scoretable.py` = **37 passed**, and
  `pytest tests/test_eval_metrics.py tests/test_eval_scoretable.py` = **90 passed, 0 failed**.
  Two of those tests exist because the suite found bugs rather than confirming intent: precision was
  being rounded in the serialiser instead of on the row, so a row held in memory and the same row read
  back were unequal — which is a resumed run appending a duplicate score and last-write-wins picking
  one of them quietly; and the `DetectorResult` adapter used `getattr(result, "status", None)`, so a
  status outside the vocabulary became the string `"none"` and only blew up later as a schema error with
  no pointer back to the caller.
- **`synthverify/eval/datasets.py` and `synthverify/eval/split.py` — the corpus seam and the split that
  makes a number admissible.** The loader **never downloads**: `goal-spec.md` FC-4 requires the product
  to run fully offline, and the corpora this thesis uses are `CC BY-NC-SA-4.0` / research-only, which
  makes them operator-supplied files rather than dependencies, so nothing that ships may fetch them. A
  test enforces that at the module level by asserting the source contains no `urllib`, `requests`,
  `httpx`, `huggingface_hub`, `socket` or `urlretrieve`. What remains is a reader with teeth: it walks a
  declared directory layout, refuses an image that matched no declared generator instead of quietly
  ignoring it, and writes a committed CSV **manifest** (`sample_id, dataset, generator, truth, path`)
  that the next run reads back — because "the label came from the directory name" is a claim, and a
  manifest is the form of that claim someone can audit. `read_manifest` refuses the five ways it can lie
  about a corpus: a header that is a different file, a row missing a field, a truth that is not 0/1, a
  path that no longer resolves ("a missing file is not a zero-scored sample" — a loader that skipped it
  would score 9,800 images and report n = 10,000), and a repeated sample id. Three corpora are declared
  in `CORPORA` with their licences, and `CNNSpot` carries `admissible_for_headline=False` because its
  uniform 224 px downsampling lets a detector win on resolution rather than content.
  **The split is the load-bearing half.** Assignment is `sha256(f"{seed}|{sample_id}")`'s first eight
  bytes over 2⁶⁴, bucketed against cumulative proportions — not a row index, not directory order, because
  either of those re-labels samples when a corpus is filtered, grown or re-downloaded, which silently
  moves data out of the held-out set and would make the thesis number unreproducible with no error to
  notice. `train` / `calibration` / `validation` / `held_out_test` at 50/20/15/15 keeps the set that
  fits fusion weights provably disjoint from the set that scores them. Two properties the hash buys and
  an index does not are tested by name: **subset stability** (every sample of a filtered corpus keeps
  its split) and **shuffle invariance**. The assignment is a committed JSONL file whose first line
  declares format, seed and proportions, and `load()` **re-derives every row against the file's own
  seed** and refuses the file if a row disagrees — that is the check that catches a hand-edited split,
  the one edit that turns a held-out set into a leak. `.digest` is the SHA-256 of those bytes, so a run
  can be tied to the exact split it used and a reviewer can reproduce the tie with `sha256sum`.
  `thin_cells()` and `empty_cells()` report the shape problem *before* a six-hour pass rather than in
  the results after it: a generator with one image occupies one bucket and is simply absent from the
  other three, which otherwise reads as "every generator generalises".
  Graded by `pytest tests/test_eval_split.py` = **37 passed**, `pytest tests/test_eval_datasets.py` =
  **39 passed**, and the whole measurement package `pytest tests/test_eval_{metrics,scoretable,split,datasets}.py`
  = **166 passed, 0 failed**. Three defects were caught by writing the tests rather than by reading the
  code. Two were in the tests: an `empty_cells()` assertion that a twelve-sample corpus satisfied
  trivially (it does reach all four splits), now pinned to a one-image generator that provably cannot;
  and `manifest_digest(m) == manifest_digest(m)`, which asserted nothing, replaced by the digest against
  `hashlib.sha256(m.read_bytes())` plus a pair that requires it to be blind to write order and sensitive
  to a row. The third was in the register: `CORPORA` carried `m4`, which is a **text** corpus from the
  plan's optional generalisation experiment, marked `admissible_for_headline=True` on a licence recorded
  as unresolved — a category error for an image loader and, worse, exactly the thing `AC-DET-1b`
  forbids. It is gone, the register is pinned as a set so a fourth corpus is a decision made twice, and a
  new test refuses any entry whose licence hedges while claiming a headline number.
- **`synthverify score` and `synthverify eval` — the harness becomes a command** (`synthverify/cli.py`,
  `synthverify/eval/runner.py`, `scripts/eval_fixture_e2e.py`). `score` runs every selected detector over
  a corpus into the resumable CSV table: one `DetectionContext` per sample so the image detectors share a
  single decode, one fsynced batch per sample so a kill at 03:00 loses the sample in flight and nothing
  else, `row_from_result` adapting each opinion, and `SKIPPED`/`ERROR` conversion inherited from
  `Detector.run()` rather than reimplemented. **Resume is the requirement**: `sample_id` is the key, so
  the second invocation writes **0** rows, `--require-all` widens the definition to "every named
  detector has spoken" for the detector added mid-night, and `--force` is the only way to re-score — which
  is not an overwrite, because the table is append-only: both answers stay in the file, `load()` resolves
  the pair to the newer row, and `duplicate_rows` says so on every read. `--limit`/`--sample` make a
  thirty-second smoke run the same code path as the overnight one. `--scan-dir` writes the manifest and
  the split file for a corpus already on disk — nothing here downloads one (FC-4) — and refuses to
  rewrite either artefact without `--overwrite`, because re-issuing a split silently changes which
  samples are held out from every run already measured against it. `--dry-run` prints the plan and
  commits nothing, including the split digest it *would* write, so the check that catches the wrong
  corpus cannot itself be the thing that commits one. `eval` prints the pooled metrics and, with
  `--by-generator`, one cell per generator pooled with every real (RQ1's table); a cell under
  `MIN_CELL_N=30` prints no number by default and `--allow-thin` marks the line `THIN`, with every
  refusal counted into the exit code. `synthverify analyze` gained `--dir/--jsonl` for the stranger with a
  folder of their own images and no corpus metadata, which kept its single-file form.
  Graded by `pytest tests/test_eval_runner.py` = **22 passed**, `pytest tests/test_cli_eval.py` = **15
  passed** (new file: argv in, exit code and stdout out), `make eval-fixture` = **30 checks passed, 0
  failed, RESULT: PASS**, and a seventeenth step in the CI `evaluation` job so the chain cannot rot.
  The fixture leg asserts *refusals* as the correct answer for twelve images on purpose: it proves the
  chain without pretending to prove a number. It found three things reading the library had not — see
  **Changed**.
- **`synthverify eval` reads one named split** (`--split-file`, `--split`; `synthverify/cli.py`,
  `synthverify/eval/runner.py`, `synthverify/eval/scoretable.py`). The gap between *measuring* correctly
  and *reporting* correctly was unguarded: `eval` read every row in the score table, and a table is
  append-only across a walk, so the same command over a table holding `train`, `calibration`,
  `validation` and `held_out_test` printed a headline AUC computed over rows that include the ones a
  detector was fitted on — labelled held-out, because nothing said otherwise. `--split-file F` now makes
  the read a *filter*: the default is `held_out_test`, the rows kept are cross-checked against the
  committed assignment rather than trusted, the rows belonging to other splits are reported as set aside
  instead of lost, `--split train,held_out_test` reads a union, and `--json` carries a `split` block with
  the file, that file's own SHA-256, the split names and the kept/dropped/sample counts. Four refusals
  came with it, each with its reason: a `truth` cell edited in the CSV (the refusal quotes both
  statements — `table says real/truth=1, the split file says real/truth=0` — and does not touch the file),
  a split file whose rows no longer follow from its own seed (`… this file is stale for its own seed`,
  refused before a row of it is read), a selection with no scored row underneath it (printed with the
  file's own cell sizes), and a selection filtered down to one class — refused with `AUC is undefined with
  only one class present` and no number printed beside it. `--split` without a file to check it against is
  refused too, because a name is not a partition. Two library pieces moved with it: `ScoreRow.key`
  addresses a row by **dataset and** sample id (two corpora legitimately contain
  `COCO_val2014_000000000042.jpg`), and `ScoreTable.subset(keys)` is a read-only view that carries the
  file's `torn_line` and `duplicate_rows` notes. `read_splits()` returns a frozen `SplitRead` whose
  `describe()` is the text the CLI prints, so the counts an operator reads are the counts the filter
  computed.
  Graded by `pytest tests/test_cli_eval.py tests/test_eval_runner.py tests/test_eval_scoretable.py` =
  **95 passed, 0 failed** (17 of them new) at this entry's close, and **96 passed** on the tree that ships —
  the extra case is **T59**'s refusal-message test, landing in `tests/test_cli_eval.py`, and the whole
  three-file command is re-run rather than inherited. `make eval` = **all 16 anchors quote exactly one place** then
  **both baselines green (55 and 96 tests, 0 failing)**, `make eval-mutations` = **16/16 caught** (8 s wall
  on the shipped tree) with five of the modes aimed at the read rather than the maths, `make eval-fixture` = **47 checks
  passed, 0 failed**, and eleven new `--mutate` steps' worth of CI names in the `evaluation` job (16 in
  total, set-equal to `metrics_e2e.PATCHES`). The mode that matters most is `unfiltered-read-undeclared`:
  with it, `--split held_out_test` filters nothing, prints a confident table, and says no word about it —
  which is exactly the shape of the leak, and the reason a new flag on its own would not have been a fix.
  The row-losing collision this exposed (the score table's own dedup and resume keys are still bare
  `sample_id` while the split layer now qualifies them) is recorded as **T60** rather than folded in here:
  it changes which rows a table *keeps*, which is a different claim from which rows a metric may read.
- **The first metrics this repository has ever printed from bytes it did not generate**
  (`docs/corpus-communityforensics.md`, `scripts/commfor_fetch.py`, `scripts/commfor_check.py`,
  `scripts/commfor_nullcontrol.py`, `scripts/commfor_plan_v1{,b,c,d}.json`). 2,376 images / 592.0 MB,
  fetched in four bounded passes from the `CompEval` split of `OwensLab/CommunityForensics-Eval`
  (`gated: False`, `cc-by-nc-sa-4.0` as served), recorded with per-file provenance — source row index,
  request URL and byte offset, the generator's own `model_name`, the paired `real_source` pool, the
  14-key `provenance.jsonl` record — under tranche id `commfor_eval_v1`. **The corpus itself is not in the
  repository and never will be**: it sits under `data/corpora/`, which `.gitignore` excludes, because
  third-party research images are non-commercial and a clone-and-run release must not redistribute them.
  What ships is the four plans, the three scripts that replay them, and the document that states every
  number with the command that printed it.
  Two deliberate deviations from the task as written, both recorded in that document: the plan named
  `CommunityForensics-Small`, and the Hub serves that repository with **exactly one split, called
  `train`** (10,542 rows), so scoring it would have measured the detectors on the training distribution
  of the same set — the leak **T58** closed at the reporting end, re-opened at the data end where no flag
  can see it. `CompEval` is the split the authors publish *for* evaluation, and every row fetched here
  carries `"split": "test"` and `"subset": "CompEval"` in its own source metadata (`Counter` over 2,376:
  one value each). Second, the real class is CompEval's paired real pools, not the COCO validation set the
  authors' contamination note also prescribes, because the fetch keeps PNG and JPEG containers and the real
  pools serve WEBP rows — which is a selection effect of *this tool*, counted as a skip rather than quietly
  shrinking a pool, and it is the confound the results inherit.
  The chain, as executed: `commfor_check.py` → `integrity: 2376 records, 0 fault(s)` / `RESULT: PASS`, with
  the written manifest cross-checked row-for-row against provenance; `synthverify score` → 2,376 samples /
  **11,880 rows** in 60.8 s with `unreadable: 0` and `unscored: 0`, manifest `96a56408…`, split file
  `b28c5ec2…`, sizes `train=1198, calibration=461, validation=351, held_out_test=366`;
  `synthverify eval --split held_out_test --by-generator` → **28 AUCs and 9 refusals** over the 1,830 held-out
  rows, with the other 10,050 reported as set aside.
  What it says is not kind, and the document does not soften it: the only headline above chance is `noise` at
  **0.8585 [0.7920, 0.9063]**, while `frequency` **0.4352**, `ela` **0.3551** and `metadata` **0.0377** sit
  *below* chance against these reals, and `jpeg_history` is refused for one class present (it only runs on
  JPEGs and every fake in the tranche is PNG — `coverage: jpeg_history ran=691, skipped=1685` is the same fact
  before any metric exists). `metadata` then prints **the identical AUC and the identical interval in all six
  reporting generator cells**, which is a container classifier wearing a forensics label. The four null
  controls in `scripts/commfor_nullcontrol.py` — same class on both sides by construction, so 0.5 is what no
  artefact looks like — are what turns the headline from a result into a refutation: **`noise` separates two
  pools of real photographs at 0.6998 [0.6197, 0.7693]** and separates DeciDiffusionV2's fakes from LCM's at
  the same size, container and source pool at **0.3857**. A detector that moves that much on differences that
  are not generation is not measuring generation.
  One product defect came out of reading the real report rather than the fixture: a refusal said `below
  MIN_CELL_N=30` while naming only the cell's *total*, so an operator could not see which side was thin —
  `synthverify/cli.py` now prints the failing counts on both the pooled line and the per-cell line
  (`refused: pos/neg=10/106 of n=116 below MIN_CELL_N=30`). Found by running the report, written red first as
  `tests/test_cli_eval.py::test_a_refusal_names_the_count_that_failed_not_the_cells_total`, then green.
  Graded on this tree: `make eval` → `--check-anchors` **PASS (16/16)**, **`[baseline metrics] 55 tests, 0
  failing`** and **`[baseline split-read] 96 tests, 0 failing`**; `make eval-mutations` → **16/16 caught**,
  13/11/1/1/1/1/1/2/1/1/2 red of the 55 metric cases and 9/1/8/1/1 of the 96 split-read cases;
  `make eval-fixture` → **47 checks passed, 0 failed**; `pytest tests/test_cli_eval.py
  tests/test_eval_runner.py tests/test_eval_scoretable.py` → **96**. **What this does not claim:** it is not
  evidence that SynthVerify detects synthetic media, it is evidence that the shipped heuristics do not clear
  this corpus's confound; no learned detector was in the run; calibration on these 461 rows (**T57**) and a
  confound-matched tranche (which needs a new fetch — there are **zero** PNG reals at any of the fake sizes)
  are both still open.
- **Credentials got finer, then got a second type — `REQ-IDAM-2` (T48) and `REQ-IDAM-1` (T49).**
  `SCOPE_VOCABULARY` (14 tokens) in `synthverify/auth.py`, a nullable `ApiKey.scopes` column (Alembic `0008`,
  added last so `create_all()` and a migrated DB keep byte-identical `sqlite_master`), `effective_scopes()`
  falling through to the role grant when a key predates scopes, and `require_scope(*scopes)` — whose 403 names
  the missing token — wired into `/media/ingest[+/batch]`, `/jobs[/{id}]`, `/jobs/{id}/reanalyze` and
  `/jobs/{id}/artifacts[/{index}]`; a `service` key that reaches `/admin/*` is refused on the platform-admin
  role gate *before* scope evaluation. Then OIDC: `Authorization: Bearer <jwt>` verifies against a locally
  minted test-key JWKS through **PyJWT** (graded `MIT` by `synthverify licenses`) with
  `SV_OIDC_ALLOWED_ALGORITHMS` checked *before* PyJWT
  sees the token, which is what defeats algorithm confusion. A token and an API key travel the same code path
  behind one `@runtime_checkable Principal` protocol, so `require_role` / `require_scope` / `visible_to` do not
  branch on credential type. The verifier ships as the optional `[jwt]` extra (`PyJWT[crypto]` → `cryptography`
  /Apache-2.0, `cffi`/MIT-0, `pycparser`), so a default install still pulls no JWT library and `oidc_enabled`
  short-circuits before any socket, leaving the FC-4 air-gap untouched. Claim paths default to the `sv_` prefix
  (`sv_role`/`sv_scopes`/`sv_org`), deliberately **not** `synthverify_` — that prefix is the Prometheus metric
  namespace and the alert-rules gate grades every `synthverify_…` string literal as an emitted metric that must
  carry a HELP text.
  *Measured, not asserted, on this tree (2026-10-01).* `pytest tests/test_scopes.py
  tests/test_tenancy_matrix.py tests/test_migrations.py` → **80 passed, 5 skipped** (15 scope, 48 tenancy, 17
  migration; Postgres-only skips); `pytest tests/test_oidc.py` → **23 passed** (with the 15 scope tests,
  `tests/test_oidc.py tests/test_scopes.py` = **38 passed in 7.68 s**), including
  `test_ac_idam_1_rejects_a_token_for_another_audience` (a token for another audience cannot authenticate),
  `test_alg_confusion_is_refused` (the allow-list check fires before PyJWT sees the token) and
  `test_oidc_disabled_short_circuits_before_the_network` (a default install reaches no JWKS socket). `test_the_declared_root_count_matches_what_the_gates_print`
  now reads **22 declared roots → 52 packages** and the lock carried **53 pins** at that measurement (**54**
  since the Windows platform-pin table added `colorama`, below), both re-derived from the
  resolved closure rather than asserted; `MIT-0` was added to `ALLOWED_LICENSES` for `cffi`. The full SQLite leg
  through `.venv/bin/python`: **1023 tests → 1002 passed, 0 failed, 21 skipped in 163.8 s** at that
  measurement, and **1026 → 1005 passed, 0 failed, 21 skipped in 165.2 s** on the tree that ships this entry
  (the +3 are the platform-lock cases; the 21 are Postgres/Valkey-only). The Postgres leg and the two container
  legs (`make lock-e2e`, `make airgap`) are
  **RE-MEASURE PENDING** on this machine — the services are down and the thermal budget says do not spin them up
  for a doc pass; the README rows carry that label rather than restating the pre-change numbers.
- **`mypy` cleared to zero and became a real gate (T50).** The tree read **21 errors in 12 files** while
  `make typecheck` ended in `|| true` — informational precisely because nobody ran it. Every finding is closed
  (`Success: no issues found in 73 source files`): `cast`s at the `importlib.metadata` `PackageMetadata` /
  `PackagePath` seam in `compliance/licenses.py` that typeshed does not model, the missing annotations across
  `auth.py`/`cli.py`/`routes_media.py`/`routes_admin.py`/`config.py` and the pre-existing `0004` migration, and
  `types-PyYAML` (PEP 561) declared in `dev` so the alert-rules scanner's `import yaml` type-checks. `make
  typecheck` no longer swallows the exit code and `.github/workflows/ci.yml` gained a **Type check** step beside
  Lint, so a regression is caught rather than accumulated.

### Changed

- **The `AC-INFRA-3` rate-limit gate now rendezvous its two probe processes before timing them**
  (`scripts/ratelimit_e2e.py`, `scripts/ratelimit_probe.py`, graded by `make ratelimit` and the CI `limiter`
  job). The children were started and left to race each other; on a loaded runner a probe whose interpreter
  finished importing after its sibling had already drained the shared bucket reported `0 admitted`, and the
  "both processes participated and both were refused" check failed on the *scheduling* rather than on the
  limiter — the flake the hosted `limiter` job had been tripping. Each child now raises a ready flag and blocks
  on it; the parent opens a go-file gate only once every child has arrived (bounded, so a crashed probe still
  surfaces through `communicate`), and the standalone-probe path is a no-op when the env vars are absent. Only
  *when* the processes start changed: the limiter, subject, attempts and every assertion are untouched, so the
  `isolated`/`nofallback`/`no-timeout` mutations are still caught. A fresh local run of the barrier-fixed harness
  prints the balanced `[(20, 60), (20, 60)]` participation at `21 checks passed, 0 failed`.
- **§2.5 of `README.md` no longer carries `RE-MEASURE PENDING` rows, because hosted run #3 cleared them**
  (`gh run view 37357793993`, head `1ec212d`, **19 jobs green / 0 red**). The Postgres/Valkey full-suite leg,
  `scripts/postgres_e2e.py` (16 checks), the macOS/Windows interpreter-portability matrix and the linux
  lock/reproducible-image gate are flipped from the tagged `753`-size to that run's printed figures; the Windows
  legs are now recorded as *passing* (`1056 tests, 0 failures, 0 errors, 23 skipped`) rather than only reaching
  `pytest`. The single row still labelled previous is the `linux/aarch64` container pair (no hosted runner is
  arm64, and this box's Docker is down for thermal-budget reasons), so it stays honest rather than borrowed.
- **The operator console was redesigned** (`synthverify/dashboard/index.html`). It now reads as a
  forensic case file rather than a generic admin page: a sticky rail whose tabs carry live per-tab
  indicators, one rotated double-ruled verdict seal as the loud element, numbered exhibits (`E1`…`En`
  per detector, `A1`…`An` per artifact), and three type layers that keep an identifier (mono), a
  written opinion (serif) and interface chrome (grotesque) visibly distinct. It is still one file with
  no build step and no CDN, so an air-gapped console cannot phone home (FC-4, `REQ-OPS-4`).
- Two layout defects that measurement caught and eyeballing did not: the credentials and callbacks
  tables overflowed their grid track and painted under the adjacent form, so a visible "Deliveries"
  button could not be clicked; and the queue's seven columns needed 793px in a 470px track beside the
  open-record card. Both tabs are now stacked, every wide table sits in its own scroll container, and
  the two-pane split only engages above 1500px where the queue has 730px of its own.
- Graded by `pytest tests/test_api.py tests/test_release_hygiene.py tests/test_tenancy_matrix.py`
  (93 passed, 0 failed), by the four screenshots at the top of `README.md` (which render this file),
  and by a headless pass that asserts zero horizontal overflow, zero clipped or spilled boxes and zero
  unlabelled controls at 1240px, 1680px and 390px.
- **Average precision is credited at the threshold a positive sits at, not at its row rank**
  (`synthverify/eval/metrics.py`). Found by running `synthverify eval --by-generator` over a twelve-image
  fixture: the pooled cell and the generator cell held the *same twelve rows* and printed **0.3468** and
  **0.5022**. The rank form also gave a detector that emits one constant score **AP 1.0** whenever its
  rows happened to land positives-first — a headline number that was a property of the score table's file
  order. Ties are now grouped the way `roc_curve()` already grouped them, which is the module's own
  convention rather than a new one. Policed by two tests and by a new mutation mode
  (`ap-rank-not-threshold`, the eleventh) that reinstates the rank rule and is confirmed caught: `make
  eval` = **anchors PASS, all 11 quote exactly one place** and **`[baseline] 55 metric tests, 0 failing`**,
  `make eval-mutations` = **all eleven caught** at **13/11/1/1/1/1/1/2/1/1/2** red per mode in
  Makefile order — mode B moved from 10 to 11 because the constant-score test asserts the AUC as well,
  and every other count is the one T53, T54 and T55 recorded.
- **`--by-generator` measures a cell against the real photographs, and says when it cannot**
  (`synthverify/eval/scoretable.py`). `vectors_by_group("generator")` returns single-class buckets —
  a generator directory *is* one class under these labels — so every cell raised
  `InsufficientLabelsError`, the CLI swallowed it, and the breakdown printed nothing while looking like a
  detector that had scored nothing. `cells_against_reals()` joins each fake group with every real row,
  read once so the cells are comparable with each other, and refusals now travel back to the printer with
  their reason and their exit code.
- **A mistake in `score`'s argv is a message with exit 2, not a traceback** (`synthverify/cli.py`).
  `--sample` and `--limit` are validated inside `pending_samples`, which the handler had left outside its
  guard, so a typo in a command queued for six hours produced a Python stack and exit 1. The `try` now
  covers argv-through-write; the regression is asserted in `tests/test_cli_eval.py` and in the CI leg.
  `eval --json` also gained the `sufficient_sample` key its own docstring had been promising: the text
  table marks a thin line `THIN`, and a thin number reaching a machine reader without that flag is the
  failure the gate exists to prevent.
- **The measurement gates are documented where the other gates are, and the README's fence pairing is
  repaired** (`README.md`). `make eval`, `make eval-mutations` and `make eval-fixture` join the gate
  list, and §2.5 gains three rows, so that section's own promise — every command, its exact output, and
  what the number does *not* prove — holds for the harness as it already does for the infrastructure
  gates. The rows were written against a fresh run on this tree, not inherited: `make eval` = **anchors
  PASS, all 11 quote exactly one place** + **`[baseline] 55 metric tests, 0 failing → RESULT: PASS`**,
  `make eval-mutations` = **11/11 caught, exit 0** at **13/11/1/1/1/1/1/2/1/1/2**, `make eval-fixture` =
  **30/30, RESULT: PASS**. The defect found on the way: that gate list had **no opening code fence**, so
  its closing ` ``` ` *opened* a block instead of closing one — the eighteen `make …` lines rendered as
  loose prose and the two paragraphs after them, plus a literal ` ```bash `, rendered as monospace code,
  until the next bare fence re-paired. `grep -c '^```' README.md` read **19** (odd) before the fix and
  **20** (even) after, and a CommonMark-correct pairing scan (a fence closes only with no info string and
  at least its opener's length, which is why ` ```bash` inside a block is content) now reports the list
  as one code run and every block after it correctly paired. Graded with `pytest
  tests/test_release_hygiene.py tests/test_cli_eval.py tests/test_eval_metrics.py` = **91 collected, 0
  failures, 0 errors, 0 skipped** in 0.612 s (junit) — the doc guards, the CLI's own 15, and the 55
  metric tests the gate counts — and `make lint` = **All checks passed!**.

### Fixed — the first hosted CI run

`github.com/aashish254/synthverify` had never executed a workflow before this push. The first run
(`gh run view 36869183171`) is the evidence for everything below: **19 jobs — 12 success, 7 failure**,
and the 12 greens are the ones that had never run off a laptop (`test (3.11, postgres)`, `test (3.11,
sqlite)`, all three `portability (macos-latest, …)` legs, `evaluation`, `tenancy`, `retention`, `limiter`,
`freedom`, `tracing`, `migrations`). The 7 reds were **five distinct causes**, and none of them was a
product defect: every one was a claim about an environment that only the environment can adjudicate.

- **`docker` — a fixture mount the container could not write.** Literal:
  `PermissionError: [Errno 13] Permission denied: '/work/evidence.jpg'` in
  `scripts/airgap.sh`. `docker/Dockerfile` ends in `USER svuser` (uid 10001) and the host created
  `$WORK` mode 0755 as whoever runs CI, so the fixture write inside the air-gapped container failed.
  Docker Desktop's uid remapping hides this on a laptop — the gate had passed 100% of the times it was
  ever run, which were all on laptops. Fixed by `chmod 0777 "$WORK"` on the scratch fixture directory,
  deliberately, with the reason in the comment above it.
- **`scale` — `--wait` asked of a service that has no healthcheck to wait for.** Literal:
  `container sv-scale-6e60446c-worker2-1 has no healthcheck configured` followed by
  `RuntimeError: \`docker compose up\` failed for ['api1', 'api2', 'worker1', 'worker2']`, while every one
  of those containers was already running. `docker/compose-scale.yml` sets `healthcheck: disable: true`
  on the worker tier on purpose — a `synthverify worker` opens no listening socket, so no probe could
  mean anything by "ready", and inventing one would be theatre. `docker compose up --wait` exits
  non-zero on such a service anyway. Fixed by splitting the services in `scripts/scale_e2e.py`: the API
  replicas still come up `--wait --wait-timeout 240`, the consumers come up without it, and their
  liveness is proved where this script proves everything else — in the database, by `claimed_by` naming
  both worker hostnames. The same commit makes `dump()` print `ps --all` before the logs and names the
  service list that failed, so the next such death says which tier died instead of raising one masked
  `RuntimeError` after a `ps` that listed nothing.
- **`ledger` — the install step's name claimed a driver it did not install.** Literal:
  `ModuleNotFoundError: No module named 'pg8000'`. The job's step was labelled as installing the
  product's Postgres driver; it installed `.[vision,dev]`, and `psycopg` only ever enters the build
  through `docker/Dockerfile.postgres` (FC-1's note in `docs/goal-spec.md` §6.1). `scripts/ledger_bench.py`
  verifies a million rows through whatever `SV_TEST_POSTGRES_URL` names, which in CI is BSD-3 `pg8000`,
  exactly like the four legs that already install it (`test`, `migrations`, `scale`, `tracing`). Fixed by
  installing it and renaming the step to say what it actually puts in the environment.
- **`portability (windows-latest, 3.11 / 3.12 / 3.13)` — the lock check mistook one platform's closure
  for the project's.** Literal from the environment doctor:
  `[fail] dependency lock  \`make lock-check\` exits 1 in this environment.   RESULT: FAIL (2 problem(s))`
  — and *that* is the second bug, because the count of two was all `doctor.py` surfaced: the two names
  had to be recovered by re-running the real `check_lock` against a simulated win32 environment
  (`scripts/`-side, throwaway) before they could be diagnosed. They are `colorama: unpinned` and
  `uvloop: stale`. Both are correct findings *about a Windows host* and both are wrong as gate failures:
  `click` requires `colorama; platform_system == "Windows"` and `pytest` requires
  `colorama>=0.4; sys_platform == "win32"`, so a Windows closure genuinely contains a package no darwin
  or linux closure mentions (nothing pinned it → `unpinned`), and `uvloop` is required by
  `uvicorn[standard]` under `sys_platform != "win32"`, so it is genuinely absent from a Windows install
  while being a real pin of the project (`stale`). The existing answer for exactly this shape was
  `TARGET_PLATFORM_PINS` — the hand-declared, reason-carrying table that let `greenlet` into the lock for
  the linux image — and applying it again by hand would have been the wrong lesson, because it is
  *declared* where a marker line already states the fact. So the exemption is now derived: the FC-1
  closure walk already reports every requirement line it declines to follow, with its parent and its
  marker, and `closure_versions` returns those as `inert` (a pin inert on this platform is exempt from
  *stale* with its evidence printed — `inert on this platform, pinned anyway: uvloop==0.22.1 - declared
  by uvicorn as \`uvloop>=0.15.1; (sys_platform != 'win32' …) and extra == 'standard'\``), while a matrix
  platform that needs a package this host cannot discover is added to the new
  `MATRIX_PLATFORM_PINS` table, measured rather than guessed (`pip download colorama
  --platform win_amd64 --python-version 3.12` → **0.4.6**). `PLATFORM_PINS` is the union, `--write-lock`
  emits it, and the presence requirement still fires in the other direction: a lock missing `colorama`
  is `unpinned` on every platform, so an exemption cannot rot into a free-for-all.
  Graded on this tree: `simulated windows: 51 closure, 54 pins, 0 issue(s)` and `simulated linux:
  52 closure, 54 pins, 0 issue(s)` running the shipped `check_lock`, `make lock-check` →
  **52 packages in the declared closure, 54 pinned … RESULT: PASS**, and **3 new cases** in
  `tests/test_dependency_lock.py` for the derived marker evidence, the matrix pin in both directions,
  and the printed exemption (suite **1026 collected, 1005 passed, 0 failed, 0 errors, 21 skipped in
  165.2 s**, junit).
- **`reproducible-image` — the anti-theatre assertion was only true on Apple Silicon.** Literal:
  `FAIL  the host scan genuinely does not see it (so the image run is not theatre) (… 53 packages from 22
  declared roots …; greenlet in host output: True)`, with **9 checks passed, 1 failed**. The check asserted
  that the *host* FC-1 scan must NOT contain `greenlet`, so that grading it inside the image would prove
  something about the artifact rather than the host. On a linux runner `greenlet` legitimately *is* in the
  host closure, so the check failed on a true artifact and a passing build. Fixed by branching on which
  platform the claim is being made from: where the host cannot see the pin, absence remains the witness;
  where it can, the witness becomes that the image run grades a *different* closure than the host does
  (`53 packages from 22 declared roots` on the runner against `34 from 13` inside the image — the two
  counts are compared, not assumed). The `--check-anchors`-style discipline held: no assertion was
  weakened, and the mutation arms that this gate exists to catch still go red.

**The claim this section makes, and the one it does not.** What is measured here is the diagnosis and the
local re-derivation of each failure, on a tree where the Docker daemon is down for thermal-budget reasons,
so `make airgap`, `make scale`, `make ledger-postgres`, `make docker`, `make lock-e2e` and the Windows
suite itself have **not** been re-run locally — the labelled rows in §2.5 of `README.md` stay
**RE-MEASURE PENDING**, and the next hosted run is the witness for those seven jobs. Notably unproven until
then: the Windows *test suite* had never got as far as `pytest`, because the doctor stopped the job first.
**Corrected by run #2, below:** that prediction was right about the job and wrong about the cause it would
next meet — Windows did reach `Run test suite`, and the sentence this section wrote as "nothing in `tests/`
skips on `sys_platform`, so the ≤21-skip ceiling should hold there" turned out to be the *second* thing that
needed measuring: two POSIX mode-bit cases have no referent on NTFS, and the ceiling is now 21 + 2 named
skips rather than an untested 21. See the T63 entry.
**Corrected by run #3 (`gh run view 37357793993`, head `1ec212d`):** that run came back **19 jobs green, 0 red**,
so the seven jobs this section projected forward to a hosted witness for are witnessed, the three Windows legs
now *pass* (each `1056 tests, 0 failures, 0 errors, 23 skipped`) rather than merely reaching `pytest`, and the
labelled §2.5 rows it kept pending — the Postgres/Valkey full leg, `postgres_e2e.py`, the interpreter-portability
matrix and the linux lock/reproducible-image gate — are flipped to that run's printed figures. The one §2.5 row
still labelled previous is the `linux/aarch64` container pair, because no hosted runner is arm64.

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
