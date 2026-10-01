#!/usr/bin/env bash
# AC-FC-4 at the OS boundary: analyse a real file inside a container that has no
# network namespace at all (--network none), so DNS and egress do not exist rather
# than merely going unused. CI's `docker` job runs this script; `make airgap` runs
# the identical commands locally.
#
#   usage: scripts/airgap.sh [image]        # default: synthverify:ci
set -euo pipefail

IMAGE="${1:-synthverify:ci}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${SV_AIRGAP_WORK:-/tmp/sv-airgap}"

docker info >/dev/null 2>&1 || {
    echo "error: no docker daemon - AC-FC-4's container proof needs one" >&2
    exit 3
}
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "error: image $IMAGE not built - run: docker build -f docker/Dockerfile -t $IMAGE ." >&2
    exit 3
}

rm -rf "$WORK" && mkdir -p "$WORK"
# The mount has to be writable by the *container's* uid, not the host's. `docker/Dockerfile` ends in
# `USER svuser` (uid 10001) and the host created this directory as whoever runs CI, so a default 0755
# mount gives the fixture write below a PermissionError. Docker Desktop's uid remapping hides this on a
# laptop; a Linux runner does not hide it. 0777 on a scratch fixture directory, deliberately.
chmod 0777 "$WORK"

# Control: prove the seal is real before trusting anything that runs inside it.
if docker run --rm --network none "$IMAGE" \
    python -c "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)" >/dev/null 2>&1; then
    echo "error: a --network none container reached the internet; the test would prove nothing" >&2
    exit 1
fi
echo "control : --network none has no route (expected: Network is unreachable)"

# The fixture lives in tests/, which is not in the image; mount the checkout read-only.
docker run --rm --network none -v "$REPO:/src:ro" -v "$WORK:/work" "$IMAGE" \
    python -c "import sys; sys.path.insert(0, '/src/tests'); \
from fixtures_gen import doctored_photo; \
open('/work/evidence.jpg', 'wb').write(doctored_photo())"
[ -s "$WORK/evidence.jpg" ] || { echo "error: fixture was not produced" >&2; exit 1; }
echo "fixture : $WORK/evidence.jpg ($(wc -c < "$WORK/evidence.jpg" | tr -d ' ') bytes, written sealed)"

docker run --rm --network none -v "$WORK:/work:ro" "$IMAGE" \
    python -m synthverify.cli analyze /work/evidence.jpg --json > "$WORK/verdict.json"

# coverage == 1.0 is the anti-vacuity half: a sealed run that silently skipped every
# detector would still print a verdict, and would not be a proof of anything.
"${SV_AIRGAP_PY:-python3}" - "$WORK/verdict.json" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
verdict = report["verdict"]
assert verdict["risk_tier"] in {"MEDIUM", "HIGH", "CRITICAL"}, verdict
assert report["detectors"], "no detector ran inside the sealed container"
assert verdict["detector_coverage"] == 1.0, verdict
print(
    "air-gapped verdict:",
    verdict["recommended_action"],
    verdict["risk_score"],
    f"({verdict['risk_tier']}, {len(report['detectors'])} detectors, "
    f"coverage {verdict['detector_coverage']:.0%})",
)
PY
