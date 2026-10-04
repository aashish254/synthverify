# v1.0.0 — SynthVerify

A synthetic-media verification pipeline you can run yourself: 11 hand-written heuristic
detectors, a hash-chained tamper-evident audit trail, and a fused decision that routes each
item to PROCEED / MANUAL_REVIEW / ESCALATE / BLOCK. It is a research prototype, and this
release's own measurements say why it must be treated as one.

## Read this first: what the shipped detectors are, and are not

The detectors are fixed signal-processing heuristics — noise residuals, JPEG quantization
history, frequency content, ELA, metadata — **not trained models** (no `ml_model` is set on
any of them). We measured them against a real labelled corpus (2,376 images from
`OwensLab/CommunityForensics-Eval`'s `CompEval` split, eight generators, held-out AUCs with
DeLong intervals; full provenance in [`docs/corpus-communityforensics.md`](corpus-communityforensics.md)):

- Best pooled held-out AUC: `noise` **0.8585** [0.7920, 0.9063]; `frequency` 0.4352,
  `ela` 0.3551, `metadata` 0.0377 — **three of the four reporting detectors land below
  chance**.
- Null controls (one class on both sides) put `noise` at **0.6998** separating two pools of
  *real* photographs from each other. A detector that moves that much on differences that
  are not generation is not measuring generation, so 0.8585 is reported as **confounded,
  not as performance**.
- The corpus itself confounds container and size with class, so every fake-vs-real AUC here
  is an upper bound, not an estimate.

**Therefore: the shipped heuristics are NOT sufficient for real deepfake detection.** Do
not route consequential decisions to them. The learned-detector plugin path is specified,
licensed-audited, and **unmeasured** — it is not part of what this release proves.

## What this release is

- **Verification machinery you can self-host**: FastAPI service, worker, SQLite or
  PostgreSQL, optional Valkey rate limiting, S3-compatible or local media store, C2PA-ready
  manifest schema, hash-chained ledger with public verify CLI.
- **An audit trail that can fail loudly**: every run appends to a SHA-256 chain;
  `synthverify verify` re-derives it; the tamper tests mutate rows and expect red.
- **A measurement harness for the day detectors are better than the plumbing**:
  `eval`/`score` CLI over hash-split corpora, AUC with DeLong intervals, split-scoped
  reporting that refuses unnamed splits and thin cells by design.

## Verification status of this build

- **Test suite**: 1033 tests, 0 failures, 0 errors, 21 skips (service-dependent only),
  166.3 s — measured 2026-10-04 at commit `6e18a59`, the product tree this release tags
  (Python 3.11, SQLite, macOS).
- **Gates re-measured on this tree, 2026-10-04**: `make lock-check` (52 packages in the
  declared closure, 54 pinned, PASS); mypy clean across 73 source files; the full suite
  above. The evaluator-self-check gates — `make eval` metrics harness (16 mutation anchors,
  baselines 55 and 96 tests green) and `make eval-fixture` chain (47/47 checks) — carry the
  2026-09-29 measurement recorded in README §2.5 and were not re-run this day.
- **Hosted CI**: the run accompanying this release,
  https://github.com/aashish254/synthverify/actions/runs/37194935808 (head `6e18a59`), is
  **19 jobs, 19 success** — including all three `portability (windows-latest, 3.11/3.12/3.13)`
  legs, where pytest passed on Windows for the first time (the 3.11 leg prints
  `1033 tests, 0 failures, 0 errors, 23 skipped`: 21 service-dependent skips plus the two
  named POSIX mode-bit skips the Windows job's skip policy allows). macOS legs green at the
  same size with 21 skips. Earlier runs on this repository failed only on the environment
  claims narrated in README §2.5.

## Fixed since the first draft of this tag

An earlier `v1.0.0` draft pointed at `59ba190`, before hosted CI had executed pytest on
Windows. That run found three Windows defects, all fixed at `6e18a59` — the tree this tag's
release is cut from — and none present in what you download:

1. The split-file digest was computed over LF text while Windows' default text-mode write
   committed CRLF bytes — records cited a digest that did not match the artifact.
2. The local media store produced `ff\<sha>_<name>` keys on Windows, breaking the documented
   `<sha[:2]>/<sha>_<name>` scheme shared with the S3 backend.
3. First-boot admin-key generation called `os.fchmod`, which does not exist on Windows.

The witness that they are fixed is the green `portability (windows-latest, 3.11/3.12/3.13)`
matrix above: 1033 tests passing on Windows itself, not on a projection of it.

## Install

```bash
git clone https://github.com/aashish254/synthverify && cd synthverify
git checkout v1.0.0
make setup && make test        # no GPU, no model downloads, no telemetry
make serve                      # or: docker compose -f docker/docker-compose.yml up
```

Windows: use the PowerShell path in [`docs/INSTALL.md`](INSTALL.md).
Docker-only: `docker build -f docker/Dockerfile -t synthverify:1.0.0 .`

## License

MIT (`LICENSE`). Corpus provenance and per-source notes:
[`docs/corpus-communityforensics.md`](corpus-communityforensics.md).
