# ---------------------------------------------------------------------------
# SynthVerify - Enterprise Synthetic Media Verification Pipeline
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg: audio/video decode fallback; curl: healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY synthverify ./synthverify

# The wheel cache is a BuildKit mount, not `--no-cache-dir`, on purpose. README.md is in
# this layer's inputs (setuptools reads it for the package metadata), and so is every file
# under synthverify/, so any documentation or source edit re-runs this RUN. With
# `--no-cache-dir` that meant re-downloading ~30 wheels from PyPI - which broke the
# air-gapped analysis gate once on a transient network error, i.e. a docs edit could make a
# freedom gate fail. Cached, the rebuild stays local and never depends on the network.
#
# `-c` (constraints) rather than `-r` (requirements): pyproject.toml stays the single statement of
# *what* is needed and the lock only fixes *which version* each thing in that set resolves to. An
# entry the requested extra does not pull in is inert, so one lock serves the image, `make setup`
# and CI. The lock is a COPY in this layer's inputs, so editing a pin re-runs this RUN and the
# install becomes the only place the change lands.
#
# `LOCK` exists so `scripts/lock_e2e.py` can build a *deliberately wrong* constraints file and show
# the gate notices; the default is the committed lock, and every other caller gets exactly it.
#
# `valkey` is in the image because the shared rate-limit bucket is a *runtime* capability
# (`docker-compose.yml` names `SV_RATE_LIMIT_BACKEND=valkey` as the scale-out path), and
# `synthverify/ratelimits/valkey.py` imports its client lazily by FC-1 design: an image without it
# would raise at limiter construction rather than serve. `dev` is not, so the image stays smaller
# than the test environment.
ARG LOCK=docker/requirements-lock.txt
COPY ${LOCK} ./requirements-lock.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -c requirements-lock.txt ".[vision,valkey]" \
    && useradd -m -u 10001 svuser \
    && mkdir -p /app/data/media /app/data/artifacts \
    && chown -R svuser:svuser /app

USER svuser

ENV SV_DATABASE_URL="sqlite:////app/data/synthverify.db" \
    SV_STORAGE_DIR="/app/data/media" \
    SV_ARTIFACTS_DIR="/app/data/artifacts" \
    SV_ENVIRONMENT="production"

VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["python", "-m", "synthverify.cli", "serve", "--host", "0.0.0.0", "--port", "8080"]
