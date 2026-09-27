"""The shared limiter: one token bucket per subject, kept inside Valkey.

Two licence facts drive this file. The **server** is Valkey (BSD-3), not Redis >= 7.4, whose
RSALv2/SSPL tri-licence `FC-1` forbids - the RESP protocol Valkey speaks is what makes the swap
possible, not the Redis name. The **client** is ``valkey`` (valkey-py, MIT, pure Python) and it is
imported here rather than at module scope, so it stays an optional extra: a default install has no
queue-or-limiter client at all, and ``make licenses`` grades it only where the extra is installed.

Why the arithmetic runs server-side: `AC-INFRA-3(a)`'s claim is that the budget is *global*, and a
read-modify-write done by each replica is a lost-update race in disguise - two replicas would both
read 1.0 tokens and both admit. A Lua script executes atomically against the keyspace, and the
clock it reads is the server's ``TIME``, so five replicas can disagree about their own wall clocks
and still drain one bucket. That is also why there is no client-side caching of the token count:
the value that makes the budget global is the value the server holds.

Why a failure degrades instead of propagating: `AC-INFRA-3(b)` requires an unreachable shared
bucket to fall back to the in-process one, so `AC-FC-4` ("runs with outbound sockets blocked")
survives the swap. Two details make that real rather than decorative - the connect and socket
timeouts bound *how long* a call takes (a fallback reached by hanging is still a hung API), and the
cooldown stops every request from re-paying that timeout while the server is down. The cost is a
short window of per-process budgets after recovery, which is the weaker-but-honest behaviour the
acceptance criterion asks for.
"""

from __future__ import annotations

import time

from synthverify.config import Settings
from synthverify.metrics import METRICS
from synthverify.ratelimits.in_process import InProcessRateLimiter

#: One atomic drain of one subject's bucket. Returns ``{allowed, retry_after_micros}``; the
#: fractional value travels as an integer because RESP integers are integers.
_TOKEN_BUCKET_LUA = """
local now = redis.call('TIME')
local ts = now[1] + now[2] / 1000000
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local fields = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local seen = tonumber(fields[2])
if seen == nil then
  redis.call('HSET', KEYS[1], 'tokens', burst - 1, 'ts', ts)
  redis.call('PEXPIRE', KEYS[1], ARGV[3])
  return {1, 0}
end
local tokens = tonumber(fields[1]) + (ts - seen) * rate
if tokens > burst then
  tokens = burst
end
local allowed = 0
local retry_us = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_us = math.floor((1 - tokens) / rate * 1000000 + 0.5)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts)
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return {allowed, retry_us}
"""

KEY_PREFIX = "synthverify:ratelimit:"


class ValkeyRateLimiter(InProcessRateLimiter):
    """A shared bucket that answers locally the moment sharing stops working.

    Subclassing the in-process limiter is the degradation mechanism, not reuse for its own sake:
    the fallback bucket has to be *this object's* local bucket, with the same configured rate and
    burst, so a replica that loses Valkey still enforces the same per-process limit it would have
    enforced with ``SV_RATE_LIMIT_BACKEND=in-process``.
    """

    name = "valkey"

    def __init__(
        self,
        rpm: int | None = None,
        burst: int | None = None,
        settings: Settings | None = None,
        *,
        timeout: float | None = None,
        cooldown: float | None = None,
    ):
        import valkey  # here, not at module scope: the extra is optional by FC-1 design

        super().__init__(rpm, burst, settings)
        self._cooldown = cooldown if cooldown is not None else self.settings.rate_limit_fallback_cooldown_seconds
        reach = timeout if timeout is not None else self.settings.rate_limit_timeout_seconds
        # An idle bucket must not outlive its refill, or a quiet subject would get a free burst it
        # already spent; 2x the drain-to-refill window plus a minute of slack is the bound.
        self._ttl_ms = int(min(3_600_000, max(60_000, (self.burst / self.rate) * 2000 + 60_000)))
        self._client = valkey.Redis(
            host=self.settings.rate_limit_valkey_host,
            port=self.settings.rate_limit_valkey_port,
            socket_connect_timeout=reach,
            socket_timeout=reach,
        )
        self._script = self._client.register_script(_TOKEN_BUCKET_LUA)
        self._local_only_until = 0.0
        self.last_error: str | None = None

    @property
    def degraded(self) -> bool:
        return time.monotonic() < self._local_only_until

    def check(self, subject: str) -> tuple[bool, float]:
        if not self.degraded:
            try:
                return self._check_shared(subject)
            except Exception as exc:  # noqa: BLE001 - unreachability arrives in many costumes
                # A timeout, a refused connection, a name that will not resolve and a syscall an
                # air-gapped host blocks (AC-FC-4) are all the same event here: this backend cannot
                # answer. `AC-INFRA-3(b)` asks for degradation, not for a taxonomy of failures, so
                # anything the shared call raises is answered locally.
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                # Stop probing for a cooldown window so one outage costs one timed-out call per
                # window rather than one per request; the budget is enforced locally meanwhile.
                self._local_only_until = time.monotonic() + self._cooldown
                METRICS.inc(
                    "synthverify_rate_limit_fallback_total",
                    {"backend": self.name},
                    help="requests served by the in-process bucket while the shared limiter was unreachable",
                )
        return super().check(subject)

    def _check_shared(self, subject: str) -> tuple[bool, float]:
        allowed, retry_us = self._script(
            keys=[KEY_PREFIX + subject],
            args=[self.rate, self.burst, self._ttl_ms],
        )
        return bool(allowed), int(retry_us) / 1_000_000.0

    def close(self) -> None:
        self._client.close()
