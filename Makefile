.PHONY: help setup doctor test test-fast lint typecheck eval eval-mutations eval-fixture run demo migrate licenses model-manifests freedom lock-check lock-e2e lock-mutations airgap postgres-e2e scale scale-images scale-mutations valkey-up valkey-down ratelimit ratelimit-mutations alerts trace trace-mutations tenancy tenancy-mutations retention retention-mutations ledger ledger-postgres ledger-mutations docker-build docker-up clean

PY := ./.venv/bin/python
# `-m pip`, not `./.venv/bin/pip`: the console shims in this venv do not resolve, and a gate
# target that dies with exit 127 looks like a passing target that had nothing to say.
PIP := $(PY) -m pip

# First interpreter on PATH that satisfies `requires-python` (`>=3.11`), newest first. macOS still
# ships a 3.9 `python3`, and a venv built on it dies in `pip install` with a metadata error that
# names neither the version nor the fix. The floor is 3.11 rather than 3.10 because the package
# imports `enum.StrEnum`, which does not exist before 3.11 - `pip install` would succeed on 3.10 and
# the first `import synthverify` would not. Override when needed:
# `make setup PYTHON=/usr/bin/python3.12`. On Windows there is no `make`; INSTALL.md gives the
# equivalent `.venv\Scripts\python.exe` commands.
PYTHON ?= $(shell for p in python3.13 python3.12 python3.11 python3; do \
	command -v $$p >/dev/null 2>&1 || continue; \
	if $$p -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then echo $$p; break; fi; \
done)

# Every declared extra, because the gates grade declared roots and a root that is not installed
# cannot be graded - which FC-1 treats as a violation, not a pass (`docs/goal-spec.md`). `tests/
# test_release_hygiene.py` pins that this line covers `[project.optional-dependencies]` exactly,
# so an extra added to pyproject.toml cannot quietly make `make setup` install an incomplete
# environment again. `--system-site-packages` is deliberately absent: it lets a Homebrew-installed
# `scipy` satisfy a pin the lock never described, so the env passes locally and the same command
# fails on a clean machine.
ALL_EXTRAS := vision,valkey,jwt,c2pa,dev

setup:            ## create a self-contained venv and install the locked closure, every extra
	@if [ -z "$(PYTHON)" ]; then \
	  echo "No Python >= 3.11 found on PATH."; \
	  echo "  Install one (brew install python@3.11, apt install python3.11, or python.org),"; \
	  echo "  or point make at it:  make setup PYTHON=/path/to/python3.12"; exit 1; fi
	@echo "Creating .venv from $(PYTHON) ($$($(PYTHON) -V 2>&1))"
	$(PYTHON) -m venv .venv
	$(PIP) install --quiet --disable-pip-version-check -c docker/requirements-lock.txt -e ".[$(ALL_EXTRAS)]"
	@$(PY) scripts/doctor.py --quiet && echo "setup complete: run 'make test'"

doctor:           ## tell a first-time user what their environment is missing, by name
	@if [ ! -x "$(PY)" ]; then \
	  echo "No .venv at $(PY). Run 'make setup' first."; exit 1; fi
	@$(PY) scripts/doctor.py

help:             ## list every target with its description
	@awk -F'##' '/^[a-z0-9_-]+:.*##/ { split($$1, a, ":"); printf "  %-20s %s\n", a[1], $$2 }' \
		Makefile | sort | sed 's/[[:space:]]*$$//'
	@echo ""
	@echo "First time here?  make setup && make doctor && make test"

test:             ## run the full test suite (-rs so a skip is never silent)
	$(PY) -m pytest tests/ -v -rs --tb=short

test-fast:        ## unit tests only (skip e2e markers)
	$(PY) -m pytest tests/ -q -m "not e2e"

lint:             ## ruff lint - a real gate, no swallowed exit code
	$(PY) -m ruff check synthverify tests scripts

typecheck:        ## mypy - a real gate now that the tree is at zero
	$(PY) -m mypy synthverify --ignore-missing-imports

eval:             ## the evaluator, evaluated: both suites against an unmutated shadow copy, after their anchors are checked
	$(PY) scripts/metrics_e2e.py --check-anchors
	$(PY) scripts/metrics_e2e.py

eval-mutations:   ## the sixteen ways a headline number can be quietly wrong - eleven in the metric, five in the read - each caught
	$(PY) scripts/metrics_e2e.py --check-anchors
	$(PY) scripts/metrics_e2e.py
	$(PY) scripts/metrics_e2e.py --mutate auc-sign-inverted --expect-fail --skip-baseline || { echo "mutation A (auc-sign-inverted) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate auc-ties-full-credit --expect-fail --skip-baseline || { echo "mutation B (auc-ties-full-credit) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate delong-ties-full-credit --expect-fail --skip-baseline || { echo "mutation C (delong-ties-full-credit) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate delong-clamped-normal-ci --expect-fail --skip-baseline || { echo "mutation D (delong-clamped-normal-ci) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate confusion-threshold-exclusive --expect-fail --skip-baseline || { echo "mutation E (confusion-threshold-exclusive) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate eer-jumps-instead-of-interpolating --expect-fail --skip-baseline || { echo "mutation F (eer-jumps-instead-of-interpolating) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate ece-decision-convention --expect-fail --skip-baseline || { echo "mutation G (ece-decision-convention) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate reliability-equal-width-bins --expect-fail --skip-baseline || { echo "mutation H (reliability-equal-width-bins) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate bootstrap-median-lower-bound --expect-fail --skip-baseline || { echo "mutation I (bootstrap-median-lower-bound) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate operating-point-ignores-the-cap --expect-fail --skip-baseline || { echo "mutation J (operating-point-ignores-the-cap) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate ap-rank-not-threshold --expect-fail --skip-baseline || { echo "mutation K (ap-rank-not-threshold) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate split-filter-ignored --expect-fail --skip-baseline || { echo "mutation L (split-filter-ignored) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate label-cross-check-dropped --expect-fail --skip-baseline || { echo "mutation M (label-cross-check-dropped) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate subset-returns-everything --expect-fail --skip-baseline || { echo "mutation N (subset-returns-everything) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate empty-selection-quietly-succeeds --expect-fail --skip-baseline || { echo "mutation O (empty-selection-quietly-succeeds) was NOT caught"; exit 1; }
	$(PY) scripts/metrics_e2e.py --mutate unfiltered-read-undeclared --expect-fail --skip-baseline || { echo "mutation P (unfiltered-read-undeclared) was NOT caught"; exit 1; }

eval-fixture:     ## the whole measurement chain over a generated fixture corpus: score --dry-run, score, resume, eval --by-generator --split-file
	$(PY) scripts/eval_fixture_e2e.py

licenses:         ## FC-1 gate: every dependency permissively licensed, data included
	$(PY) -m synthverify.cli licenses

model-manifests:  ## FC-3 gate: every ML detector needs open weights + measured error rates
	$(PY) -m synthverify.cli model-manifests

freedom:          ## all automated Freedom Constraint gates (FC-1, FC-3, FC-4 pointer)
	$(PY) -m synthverify.cli freedom
	$(PY) -m pytest tests/test_offline.py -m offline -q

lock-check:       ## T39 gate: the committed lock pins exactly the declared closure, both directions
	$(PY) -m synthverify.cli dependency-lock

lock-e2e:         ## T39 at the OS boundary: two cold builds print byte-identical freeze, and that freeze equals the lock
	$(PY) scripts/lock_e2e.py

lock-mutations:   ## the four ways a lock can lie: a moved pin, an unplanned dependency, a pin that was never installed, no lock
	$(PY) scripts/lock_e2e.py --mutate bump-pin --expect-fail || { echo "mutation A (bump-pin) was NOT caught"; exit 1; }
	$(PY) scripts/lock_e2e.py --mutate new-dep --expect-fail || { echo "mutation B (new-dep) was NOT caught"; exit 1; }
	$(PY) scripts/lock_e2e.py --mutate doctored-pin --expect-fail || { echo "mutation C (doctored-pin) was NOT caught"; exit 1; }
	$(PY) scripts/lock_e2e.py --mutate no-lock --expect-fail || { echo "mutation D (no-lock) was NOT caught"; exit 1; }

airgap:           ## AC-FC-4 at the OS boundary: analyse a file in a container with no network
	docker build -f docker/Dockerfile -t synthverify:ci .
	SV_AIRGAP_PY=$(PY) bash scripts/airgap.sh synthverify:ci

postgres-e2e:     ## AC-INFRA-1: run the product itself on a real Postgres server
	$(PY) scripts/postgres_e2e.py

scale-images:
	docker build -f docker/Dockerfile -t synthverify:ci .
	docker build -f docker/Dockerfile.postgres --build-arg BASE=synthverify:ci -t synthverify:scale .

scale: scale-images  ## AC-INFRA-2: two replicas + Postgres, 200 jobs, every job exactly once
	$(PY) scripts/scale_e2e.py --no-build

scale-mutations: scale-images  ## the two ways AC-INFRA-2 can be faked: no consumer tier, and a private queue
	$(PY) scripts/scale_e2e.py --no-build --without-workers --deadline 60 --expect-fail || { echo "mutation A was NOT caught"; exit 1; }
	$(PY) scripts/scale_e2e.py --no-build --embedded --deadline 60 --expect-fail || { echo "mutation B was NOT caught"; exit 1; }

valkey-up:        ## a Valkey on 127.0.0.1:6379, for the shared-bucket tests and a limiter-backed dev server
	docker run -d --name sv-valkey -p 127.0.0.1:6379:6379 valkey/valkey:8 --save '' --appendonly no
	@echo "now run:  SV_TEST_VALKEY=127.0.0.1:6379 make test"

valkey-down:      ## remove the Valkey started by valkey-up
	docker rm -f sv-valkey

ratelimit:        ## AC-INFRA-3: two OS processes on one budget, then a stopped and a silent backend
	$(PY) scripts/ratelimit_e2e.py

ratelimit-mutations:  ## the three ways AC-INFRA-3 can be faked: private bucket, no fallback, no timeout
	$(PY) scripts/ratelimit_e2e.py --mutate isolated --expect-fail || { echo "mutation A was NOT caught"; exit 1; }
	$(PY) scripts/ratelimit_e2e.py --mutate nofallback --expect-fail || { echo "mutation B was NOT caught"; exit 1; }
	$(PY) scripts/ratelimit_e2e.py --mutate no-timeout --expect-fail || { echo "mutation C was NOT caught"; exit 1; }

alerts:           ## AC-INFRA-6's rules clause, offline: the shipped file parses and names only metrics this build declares
	$(PY) -m synthverify.cli alert-rules
trace:            ## AC-INFRA-6 across two processes: one trace id read back from exemplar, ledger, log line and rules
	$(PY) scripts/trace_e2e.py

trace-mutations:  ## the four ways AC-INFRA-6 can be faked: a renderer without exemplars, a trusting parser, a renamed metric, an edited trace id
	$(PY) scripts/trace_e2e.py --mutate no-exemplar --expect-fail || { echo "mutation A (no-exemplar) was NOT caught"; exit 1; }
	$(PY) scripts/trace_e2e.py --mutate lenient-parser --expect-fail || { echo "mutation B (lenient-parser) was NOT caught"; exit 1; }
	$(PY) scripts/trace_e2e.py --mutate renamed-metric --expect-fail || { echo "mutation C (renamed-metric) was NOT caught"; exit 1; }
	$(PY) scripts/trace_e2e.py --mutate tampered-trace --expect-fail || { echo "mutation D (tampered-trace) was NOT caught"; exit 1; }

tenancy:          ## AC-IDAM-3's gate has teeth check, offline: the matrix on an unmutated build
	$(PY) scripts/tenancy_e2e.py

tenancy-mutations:  ## the six ways AC-IDAM-3 can be faked: admin bypass, 403 oracle, shared dedup, global idempotency, open control plane, subject label
	$(PY) scripts/tenancy_e2e.py --mutate admin-bypass --expect-fail || { echo "mutation A (admin-bypass) was NOT caught"; exit 1; }
	$(PY) scripts/tenancy_e2e.py --mutate existence-oracle --expect-fail || { echo "mutation B (existence-oracle) was NOT caught"; exit 1; }
	$(PY) scripts/tenancy_e2e.py --mutate shared-dedup --expect-fail || { echo "mutation C (shared-dedup) was NOT caught"; exit 1; }
	$(PY) scripts/tenancy_e2e.py --mutate idempotency-global --expect-fail || { echo "mutation D (idempotency-global) was NOT caught"; exit 1; }
	$(PY) scripts/tenancy_e2e.py --mutate control-plane-open --expect-fail || { echo "mutation E (control-plane-open) was NOT caught"; exit 1; }
	$(PY) scripts/tenancy_e2e.py --mutate subject-label --expect-fail || { echo "mutation F (subject-label) was NOT caught"; exit 1; }

retention:        ## AC-INFRA-5's gate has teeth check, offline: the sweep suite on an unmutated build
	$(PY) scripts/retention_e2e.py --check-anchors
	$(PY) scripts/retention_e2e.py

retention-mutations:  ## the thirteen ways AC-INFRA-5 can be faked: no hold, dead job pin, shared object, shared artifact, dry run deletes, TTL ignored, empty audit, deleted in-flight job, dead scheduler, uncounted failures, ignored interval, swallowed storage refusal, sweep counted after the storage half
	$(PY) scripts/retention_e2e.py --check-anchors
	$(PY) scripts/retention_e2e.py
	$(PY) scripts/retention_e2e.py --mutate no-hold --expect-fail --skip-baseline || { echo "mutation A (no-hold) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate job-pin-ignored --expect-fail --skip-baseline || { echo "mutation B (job-pin-ignored) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate shared-object --expect-fail --skip-baseline || { echo "mutation C (shared-object) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate shared-artifact --expect-fail --skip-baseline || { echo "mutation D (shared-artifact) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate dry-run-applies --expect-fail --skip-baseline || { echo "mutation E (dry-run-applies) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate ttl-never-reads --expect-fail --skip-baseline || { echo "mutation F (ttl-never-reads) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate audit-no-detail --expect-fail --skip-baseline || { echo "mutation G (audit-no-detail) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate inflight-undeferred --expect-fail --skip-baseline || { echo "mutation H (inflight-undeferred) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate scheduler-dies --expect-fail --skip-baseline || { echo "mutation I (scheduler-dies) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate scheduler-uncounted --expect-fail --skip-baseline || { echo "mutation J (scheduler-uncounted) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate scheduler-interval --expect-fail --skip-baseline || { echo "mutation K (scheduler-interval) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate storage-swallows --expect-fail --skip-baseline || { echo "mutation L (storage-swallows) was NOT caught"; exit 1; }
	$(PY) scripts/retention_e2e.py --mutate sweep-counted-late --expect-fail --skip-baseline || { echo "mutation M (sweep-counted-late) was NOT caught"; exit 1; }

ledger:           ## AC-IDAM-4 measured on this machine: 1 M chained events verified, both read shapes, four tampering probes
	$(PY) scripts/ledger_bench.py --events 1000000 --every 5000

ledger-postgres:  ## the same criterion against a real server (SV_TEST_POSTGRES_URL), in a database this run creates and drops
	SV_TEST_POSTGRES_URL="$${SV_TEST_POSTGRES_URL:?set SV_TEST_POSTGRES_URL to the postgres:16 server, e.g. postgresql+pg8000://sv:sv@127.0.0.1:5432/sv_test}" \
	  $(PY) scripts/ledger_bench.py --postgres --events 1000000 --every 5000

ledger-mutations: ## the eleven ways AC-IDAM-4 can be faked: no seal on append, an off-by-one seal, the sealed head unchecked, the seal chain unchecked, a seal whose digest is not recomputed, the range count unchecked, an unreachable tail seal, backfill that does not verify, no server-side cursor, a digest that ignores the payload, a digest that ignores the pointer
	$(PY) scripts/ledger_e2e.py --check-anchors
	$(PY) scripts/ledger_e2e.py
	$(PY) scripts/ledger_e2e.py --mutate no-seal-on-append --expect-fail --skip-baseline || { echo "mutation A (no-seal-on-append) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate wrong-seal-head --expect-fail --skip-baseline || { echo "mutation B (wrong-seal-head) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate seal-head-unchecked --expect-fail --skip-baseline || { echo "mutation C (seal-head-unchecked) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate seal-chain-unchecked --expect-fail --skip-baseline || { echo "mutation D (seal-chain-unchecked) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate seal-hash-unchecked --expect-fail --skip-baseline || { echo "mutation E (seal-hash-unchecked) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate range-count-unchecked --expect-fail --skip-baseline || { echo "mutation F (range-count-unchecked) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate tail-seal-unchecked --expect-fail --skip-baseline || { echo "mutation G (tail-seal-unchecked) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate backfill-without-verifying --expect-fail --skip-baseline || { echo "mutation H (backfill-without-verifying) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate no-stream-cursor --expect-fail --skip-baseline || { echo "mutation I (no-stream-cursor) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate digest-ignores-detail --expect-fail --skip-baseline || { echo "mutation J (digest-ignores-detail) was NOT caught"; exit 1; }
	$(PY) scripts/ledger_e2e.py --mutate digest-ignores-prev --expect-fail --skip-baseline || { echo "mutation K (digest-ignores-prev) was NOT caught"; exit 1; }

migrate:          ## REQ-INFRA-1: bring the schema to head (SV_DATABASE_URL or --url)
	$(PY) -m synthverify.cli db-upgrade

run:              ## dev server on :8080
	$(PY) -m synthverify.cli serve --host 0.0.0.0 --port 8080

demo:             ## end-to-end demo against the local fixtures
	$(PY) scripts/demo.py

docker-build:
	docker build -f docker/Dockerfile -t synthverify:1.0.0 .

docker-up:
	docker compose -f docker/docker-compose.yml up -d

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ data/media/* data/artifacts/*
