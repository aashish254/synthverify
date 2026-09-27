# Contributing

Thanks for reading this before sending a patch — this repository has an unusual testing culture, and
knowing it up front saves a round trip.

The short version: **a test that cannot fail is not a test here.** Every acceptance criterion is
graded by a check that would notice its absence, and most are additionally graded by a *mutation*
run that removes the enforcement and requires the suite to go red. If your change makes a gate
unable to see a failure, that is the bug, even when CI is green.

## Three commands

```bash
make setup     # self-contained .venv, every declared extra, from the lock
make doctor    # names what is missing, if anything
make test      # the whole suite, ~150 s, SQLite, no services needed
```

`make help` lists the rest. Anything the README claims, a `make` target runs.

## The gate catalogue

Each row is a real script under `scripts/` plus a `tests/` module. Run the pair for whatever you
touched; CI runs all of them.

| Area | Gate | Its mutation runs |
|---|---|---|
| Tenant isolation | `make tenancy` (48-cell matrix) | `make tenancy-mutations` — 6 |
| Audit ledger verification | `make ledger`, `make ledger-postgres` | `make ledger-mutations` — 11 |
| Retention sweep | `make retention` | `make retention-mutations` — 13 |
| Shared rate limiter | `make ratelimit` | `make ratelimit-mutations` — 3 |
| Two-replica exactly-once queue | `make scale` | `make scale-mutations` — 2 |
| Reproducible image | `make lock-check`, `make lock-e2e` | `make lock-mutations` — 4 |
| Tracing across processes | `make trace`, `make alerts` | `make trace-mutations` — 4 |
| Offline / licence / model freedom | `make freedom`, `make airgap` | the gates *are* the refusals |

A mutation harness copies `synthverify/` and `tests/` into a scratch tree, applies a named patch, and
runs the relevant cases against it. Two conventions matter if you add one:

- **Anchors are load-bearing.** Each patch quotes source it must find exactly once;
  `--check-anchors` verifies that before anything runs, and a stale anchor raises rather than
  silently mutating nothing. A no-op patch would report "0 cases failed" and look like a gate that
  passed, which is the failure mode this whole culture exists to avoid.
- **Counts come from `--junitxml`, never from a substring of stdout.**

## The constraints are requirements, not preferences

`docs/goal-spec.md` §4 carries FC-1 … FC-11. They decide what the product is *allowed* to be, so
they bind contributions:

- **FC-1 — licences.** Runtime and build-time dependencies must be permissive. This is why the
  broker is Postgres and the shared bucket is Valkey rather than Redis ≥ 7.4 (RSALv2/SSPL), and why
  the Postgres test driver is `pg8000` rather than `psycopg2` (LGPL). Adding a dependency means
  adding a graded root: `make licenses` must go green *and* the lock must describe it
  (`make lock-check` checks both directions).
- **FC-3 — models.** Any learned detector needs an open-weights, open-data manifest with measured
  per-group error rates. `make model-manifests` refuses otherwise. This is why the shipped detectors
  are statistical: nothing has cleared that gate yet.
- **FC-4 — offline.** No metered service, no phone-home, no network call in the analysis path. Proved
  at the syscall boundary and in a container with no route off the host. A PR that adds telemetry
  will be closed.

Relaxing an FC constraint requires a **"Rejection of FC"** section in the PR explaining why the goal
survives without it. Absent that section, the constraint stands.

### Refreshing a dependency version

`docker/requirements-lock.txt` is generated, and the gate checks it in both directions: every
declared requirement must be pinned, and no pin may describe something nothing declares. So a
version change is two steps, in this order, and there is no shortcut through the second:

```bash
# 1. edit the declaration in pyproject.toml, then resolve a real environment from it
./.venv/bin/python -m pip install -e ".[vision,valkey,dev]"
# 2. regenerate the lock from *that* environment, and let both gates judge the result
./.venv/bin/python -m synthverify.cli dependency-lock --write
make lock-check && make licenses
```

Generating the lock from an environment you edited by hand copies its inconsistencies straight into
the pins, which is why the CLI refuses to be the second step rather than the first. If you are bumping a pin because of a CVE, say so in the PR and include `make lock-e2e` output: that is the
run which proves the image actually installs what the lock now claims.


## Making a claim the way this repo does

1. **Name the seam the criterion names.** `AC-INFRA-3` is about a *shared budget across processes*,
   so the witness is two OS processes against one bucket — not two in-process buckets in one test.
2. **Corroborate with an independent read.** A count is not proof: the ledger gate reads the database
   afterwards with `psql`; the scale gate asserts on rows, not on HTTP 200s.
3. **Size the instrument so the bug can fail.** A 200-job run cannot see a rare double-claim.
4. **Verify your own literals from disk or from printed output**, not from memory. Several documented
   numbers in this project's history were inherited from a run that had since changed.
5. **Label it:** *measured* (I ran it, here is the output), *projected* (an estimate), or *gated*
   (CI will run it, I could not). Windows is in the third category. `docs/goal-spec.md` §13 is where
   these admissions live, per tranche.

## Documented in four places on purpose

| File | Owns |
|---|---|
| `README.md` | the public face: what it does, how to run it, what it cannot do |
| `docs/goal-spec.md` | targets, requirements, acceptance criteria, Freedom Constraints, interpretations |
| `docs/architecture.md` | design and reasoning |
| `docs/api.md` | the surface: routes, payloads, status codes |
| `docs/security.md` | controls and threat model |
| `SECURITY.md` / `CHANGELOG.md` / `docs/INSTALL.md` | the ones a stranger reads first |

A change to behaviour is a change to at least one of them. `tests/test_release_hygiene.py` fails
files that reference an interpreter path or an OS prompt specific to a contributor's machine, and
asserts the Makefile, `pyproject.toml` and CI agree on the install set — those are the doc-invariant
checks; the rest is judgment, and reviewers will ask.

## Pull requests

- One acceptance criterion (or one bug) per PR. `TODO.md` items are already atomic; reference one.
- The description says **what was executed** and what it printed. "Tests pass" is not evidence.
- New behaviour gets a test that would fail without it, and — where the shape allows — a mutation
  that proves the test can fail.
- Schema changes need a migration, and `make test` must pass on both dialects (CI's Postgres leg
  covers what your SQLite run cannot).
- `ruff check synthverify tests scripts` is a gate, not a suggestion. `make lint`.
- Style: line length 110, `from __future__ import annotations` at the top, docstrings that explain
  *why* a thing exists rather than restating the code. Comments in this repo are load-bearing — many
  record the constraint or the bug that shaped a line — so match that register instead of removing
  them.

## Issues

Bug reports: the command, the version, `make doctor`'s output, and the *printed* result rather than
a description of it. Feature requests: say which requirement in `docs/goal-spec.md` §4 it serves, or
that it serves none and why that is acceptable. For anything security-shaped, read
[`SECURITY.md`](SECURITY.md) and use private vulnerability reporting rather than an issue.

## What this project will not accept

Metered or copylefted dependencies, telemetry, hosted sign-in as a hard requirement, verdict claims
that a model has not earned, and a check that cannot fail.
