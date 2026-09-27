"""The limiter factory: one config key decides where the budget is kept.

``SV_RATE_LIMIT_BACKEND=in-process|valkey`` mirrors ``SV_JOB_BROKER`` and fails closed on an
unknown name for the same reason: a limiter that silently falls back to a per-process bucket when
the operator asked for a global one turns a rate limit into ``n`` rate limits, which is precisely
the failure this setting exists to prevent. ``redis`` is rejected by name - the server choice is
Valkey (BSD-3), and Redis >= 7.4's RSALv2/SSPL tri-licence is out of scope under `FC-1`.
"""

from __future__ import annotations

from synthverify.config import Settings, get_settings
from synthverify.ratelimits.base import RateLimiter
from synthverify.ratelimits.in_process import InProcessRateLimiter


def build_rate_limiter(
    rpm: int | None = None,
    burst: int | None = None,
    settings: Settings | None = None,
) -> RateLimiter:
    """The configured limiter. Construction is cheap and does not contact the backend."""
    if settings is None:
        settings = get_settings()
    backend = (settings.rate_limit_backend or "in-process").strip().lower()
    if backend in {"in-process", "inprocess", "embedded"}:
        return InProcessRateLimiter(rpm, burst, settings)
    if backend == "valkey":
        from synthverify.ratelimits.valkey import ValkeyRateLimiter

        return ValkeyRateLimiter(rpm, burst, settings)
    raise ValueError(
        f"unknown SV_RATE_LIMIT_BACKEND {settings.rate_limit_backend!r}; "
        "expected 'in-process' or 'valkey' (the shared bucket runs on Valkey, BSD-3 - Redis >= 7.4 "
        "is RSALv2/SSPL and FC-1 forbids it)"
    )
