"""The original limiter: a token bucket in this process, guarded by a thread lock.

Logic moved verbatim from ``synthverify/ratelimit.py`` - same refill arithmetic, same burst
semantics, same "first hit for a subject costs one token" behaviour - so a deployment that never
sets ``SV_RATE_LIMIT_BACKEND`` behaves exactly as it did before `REQ-INFRA-3` existed. It is also
the fallback the shared backend degrades *to*, which is what keeps `AC-FC-4`'s offline claim true
after the swap: the local bucket needs no outbound connection at all.

The trade-off it makes is visible in one sentence: the bucket is per-process, so ``n`` replicas
admit ``n`` times the configured budget. That is the property `AC-INFRA-3(a)` measures.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from synthverify.config import Settings
from synthverify.ratelimits.base import RateLimiter


@dataclass
class _Bucket:
    tokens: float
    updated_at: float = field(default_factory=time.monotonic)


class InProcessRateLimiter(RateLimiter):
    name = "in-process"

    def __init__(
        self,
        rpm: int | None = None,
        burst: int | None = None,
        settings: Settings | None = None,
    ):
        super().__init__(rpm, burst, settings)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, subject: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(subject)
            if bucket is None:
                self._buckets[subject] = _Bucket(tokens=self.burst - 1.0)
                return True, 0.0
            elapsed = now - bucket.updated_at
            bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
            bucket.updated_at = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            retry_after = (1.0 - bucket.tokens) / self.rate
            return False, retry_after
