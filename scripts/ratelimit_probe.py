#!/usr/bin/env python3
"""One process's view of a rate-limit budget - the child half of ``ratelimit_e2e.py``.

`AC-INFRA-3(a)` is a statement about *processes*: "two separate processes pointed at the same shared
backend admit fewer combined requests than two isolated in-process buckets". Nothing in a single
interpreter can prove that - a thread shares the object, a fork shares the module state, and an
async task shares both - so the harness runs this file twice, as two real OS processes, and adds up
what each was admitted.

Output is one JSON object on stdout: what this process was allowed, denied, how long it took, and
whether it thinks it is talking to the shared backend. The parent decides what is a pass.

    usage: SV_RATE_LIMIT_BACKEND=valkey SV_RL_SUBJECT=x SV_RL_ATTEMPTS=80 \
           python scripts/ratelimit_probe.py

``SV_RL_MODE=strict`` is how the harness mutates the product: it calls the shared-bucket operation
without ``check()``'s fallback wrapper, which is the behaviour an implementation *without* a
fallback would have. It exists so the fail-closed check can be shown to be capable of failing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main() -> int:
    from synthverify.config import get_settings
    from synthverify.ratelimits import build_rate_limiter

    subject = os.environ.get("SV_RL_SUBJECT", "probe")
    attempts = int(os.environ.get("SV_RL_ATTEMPTS", "200"))
    mode = os.environ.get("SV_RL_MODE", "check")

    settings = get_settings()
    limiter = build_rate_limiter(settings=settings)
    result: dict[str, object] = {
        "pid": os.getpid(),
        "subject": subject,
        "attempts": attempts,
        "backend": limiter.name,
        "burst": limiter.burst,
        "rate_per_second": limiter.rate,
        "admitted": 0,
        "denied": 0,
        "first_retry_after": None,
        "degraded": limiter.degraded,
        "error": None,
    }
    started = time.monotonic()
    try:
        for _ in range(attempts):
            if mode == "strict":
                # Bypass `check()` on purpose: this is the no-fallback mutation.
                allowed, retry_after = limiter._check_shared(subject)  # noqa: SLF001
            else:
                allowed, retry_after = limiter.check(subject)
            if allowed:
                result["admitted"] = int(str(result["admitted"])) + 1
            else:
                result["denied"] = int(str(result["denied"])) + 1
                if result["first_retry_after"] is None:
                    result["first_retry_after"] = retry_after
        result["degraded"] = limiter.degraded
    except Exception as exc:  # noqa: BLE001 - the parent reports the failure it was asked to expect
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        result["degraded"] = getattr(limiter, "degraded", None)
        traceback.print_exc(file=sys.stderr)
    finally:
        limiter.close()
    result["seconds"] = round(time.monotonic() - started, 4)
    result["mode"] = mode
    print(json.dumps(result))
    return 3 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
