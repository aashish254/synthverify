"""REQ-INFRA-3: the shared-rate-limit seam.

One contract - ``check(subject) -> (allowed, retry_after)`` - and two backends: a token bucket in
this process (the default, no external service) and one shared bucket per subject inside a Valkey
server, chosen by ``SV_RATE_LIMIT_BACKEND`` at deploy time. Both live behind
:class:`~synthverify.ratelimits.base.RateLimiter`, and the shared one answers locally when Valkey
is unreachable, so the request path never hangs and never 500s because a limiter's dependency is
down.

``synthverify.ratelimit`` stays the caller-facing module: the FastAPI dependency routes use is the
same function it always was, which is `AC-INFRA-3(c)` - *no route handler changes line*.
"""

from synthverify.ratelimits.base import RateLimiter
from synthverify.ratelimits.factory import build_rate_limiter
from synthverify.ratelimits.in_process import InProcessRateLimiter

__all__ = [
    "InProcessRateLimiter",
    "RateLimiter",
    "build_rate_limiter",
]
