# Install

Three ways to run SynthVerify, from "I want a verdict on one file" to "I am deploying four
replicas". Every command below is printed the way it was run; where a claim is gated on a machine
this repository does not have, the row says so.

## What you need

| | Required for | Version | Notes |
|---|---|---|---|
| Python | everything but the Docker path | **>= 3.11** (`requires-python`) | 3.11 is what every gate in this repo was measured on. The floor is 3.11 and not 3.10 because the package imports `enum.StrEnum`, which 3.10 does not have — `pip install` succeeds there and the first `import synthverify` raises. 3.12 and 3.13 each run the whole suite on `linux/aarch64` (see the portability row in the README); macOS and Windows run in CI |
| `make` | the short commands | any | Absent by default on Windows — the PowerShell equivalent of each target is spelled out below |
| Docker | the Postgres/Valkey legs, `make scale`, `make airgap` | any recent engine | Optional. The default configuration is SQLite plus an embedded worker, and needs nothing running |
| `ffmpeg` | decoding MP3/AAC/Ogg | any | Optional. 8/16/24/32-bit PCM WAV decodes natively, without it |
| ~500 MB | `.venv` with all three extras | — | `opencv-python` and `scipy` are the bulk of it |

## macOS and Linux

```bash
git clone https://github.com/aashish254/synthverify.git
cd synthverify
make setup          # creates .venv, installs every declared extra from docker/requirements-lock.txt
make doctor         # names anything missing; exit 0 means the environment is complete
make test           # 753 tests, ~3 min, SQLite - no services required
make demo           # 9 synthetic-vs-authentic samples through the real pipeline
```

`make setup` chooses the newest `python3.13 … python3` on `PATH` that satisfies `requires-python`.
If you want a specific interpreter, say so explicitly:

```bash
make setup PYTHON=/usr/local/bin/python3.12
rm -rf .venv && make setup      # the venv is the only state `setup` owns; deleting it is the reset
```

Then start the service:

```bash
make run            # API + dashboard + 2 embedded workers on http://127.0.0.1:8080
open http://127.0.0.1:8080/dashboard
```

The first boot creates `data/synthverify.db`, `data/media/`, `data/artifacts/` and writes an admin
key to `data/bootstrap_admin_key.txt` with mode `0600`. That file is git-ignored, is printed to the
log **once** (at generation, never on later boots), and is the only way in until you mint more:

```bash
KEY=$(cat data/bootstrap_admin_key.txt)
curl -s -X POST http://localhost:8080/api/v1/media/analyze \
  -H "X-API-Key: $KEY" -F file=@your-image.png
```

## Windows

There is no `make` by default, so here is what `make setup` and `make test` actually run. In
PowerShell, from the clone directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c docker\requirements-lock.txt -e ".[vision,valkey,dev]"
.\.venv\Scripts\python.exe scripts\doctor.py
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m synthverify.cli serve --port 8080
```

The paths in this README and its `make` commands are POSIX (`.venv/bin/python`); on Windows they
live under `.venv\Scripts\`. Docker Desktop works for the Postgres and Valkey legs if you want zero
skips, and `pg8000` — the CI-only driver the Postgres tests use, deliberately pure-Python so it
cannot fail to build — installs with `pip install "pg8000>=1.21.5"`.

> **Status of this section: written from the code, not from a Windows run.** No Windows machine has
> executed this suite. CI carries a `windows-latest` leg, so the first green run there is the
> evidence; until then treat 3.11 on Linux/macOS as the measured path and this section as the
> intended one.

## Docker, without a Python toolchain

```bash
docker build -f docker/Dockerfile -t synthverify:1.0.0 .
docker run -d -p 8080:8080 -v sv-data:/app/data synthverify:1.0.0
docker logs $(docker ps -q -n 1) 2>&1 | grep -i "admin api key"
docker compose -f docker/docker-compose.yml up -d      # the same thing, with the volume and restart policy
```

Use one or the other: `docker-compose.yml` pins `container_name: synthverify`, so a bare
`docker run` left in the way makes `compose up` fail on the name.

The image installs `.[vision,valkey]` — the runtime extras, including the shared-bucket client, and
not the test toolchain. Set `SV_BOOTSTRAP_ADMIN_KEY` rather than reading the generated one from a
log in anything resembling production.

## What the extras buy

| Extra | Packages | Without it |
|---|---|---|
| `vision` | `opencv-python`, `scipy` | The frequency and signal detectors self-report as skipped; the pipeline still returns a verdict with lower detector coverage |
| `valkey` | `valkey` (the client, MIT) | `SV_RATE_LIMIT_BACKEND=valkey` raises at limiter construction. The default `in-process` backend needs nothing |
| `dev` | `pytest`, `ruff`, `mypy`, `setuptools`, `wheel` | You cannot run the suite or the gates — which is why `make setup` installs all three |

`FC-1` grades the *installed metadata* of every declared root, and treats a root it cannot read as
a violation rather than a pass. That is why `make setup` installs the whole set and why an
environment created some other way shows up as fourteen red tests rather than a warning: the gate
is refusing to certify what it cannot see. `make doctor` turns those fourteen traces back into the
one sentence that names the missing extra.

## Optional services, for the legs that skip

```bash
docker run -d --name sv-pg16 -p 5432:5432 \
  -e POSTGRES_USER=sv -e POSTGRES_PASSWORD=sv -e POSTGRES_DB=sv_test postgres:16
./.venv/bin/python -m pip install "pg8000>=1.21.5"
make valkey-up
SV_TEST_POSTGRES_URL="postgresql+pg8000://sv:sv@127.0.0.1:5432/sv_test" \
  SV_TEST_VALKEY=127.0.0.1:6379 make test
```

That is the 0-skip run: 16 Postgres-gated and 5 Valkey-gated tests stop skipping. The container
names above are what `make doctor` and `make valkey-down` expect.

## Upgrading an existing install

```bash
git pull
rm -rf .venv && make setup          # the lock, not your shell history, decides the versions
make migrate                        # alembic upgrade head, against SV_DATABASE_URL or the default SQLite
./.venv/bin/python -m synthverify.cli audit-verify    # the ledger must still verify
```

`make migrate` converges a database that predates Alembic without losing rows. A long-lived SQLite
file that has not been migrated is the one way to make `audit-verify` fail loudly with
`no such table: audit_checkpoints` — the message is correct, and the fix is the line above it.

## Uninstalling

`rm -rf .venv synthverify.egg-info` removes the environment; `rm -rf data/` removes the database,
uploaded media, artifacts and the bootstrap key. Nothing else is written outside the checkout, and
there is no telemetry, no licence call and no network egress in the analysis path (`make airgap`
proves it inside a container with no route off the host).
