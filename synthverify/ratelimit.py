"""Per-API-key rate limiting on the request path.

The token-bucket *implementation* lives in :mod:`synthverify.ratelimits` (``REQ-INFRA-3``: an
in-process bucket by default, a Valkey-shared one on demand). This module is what routes import,
and it is deliberately still one dependency function: `AC-INFRA-3`'s claim is that swapping where
the budget is kept costs no caller changes, so the seam has to be reachable from a config key
rather than from a new import.

The subject is the API key, not the client address, because the budget is a per-tenant contract -
and an address-bound bucket behind a shared load balancer would let one tenant spend another's
limit. Note what the key's ``rate_limit_rpm`` column does *not* do: it is stored on the row and is
not consulted here, so the limit applied is the configured global one for every key. Per-key
budgets are a feature, not a wiring detail, and this module is where they would land.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from synthverify.metrics import METRICS
from synthverify.ratelimits.base import RateLimiter


def rate_limit(request: Request) -> None:
    """FastAPI dependency applied to verified media endpoints."""
    api_key = getattr(request.state, "api_key", None)
    subject = api_key.key_id if api_key is not None else (request.client.host if request.client else "anonymous")
    limiter: RateLimiter = request.app.state.rate_limiter
    allowed, retry_after = limiter.check(subject)
    if not allowed:
        # Deliberately not `subject`: `/metrics` is unauthenticated, so a per-subject label would
        # publish every tenant's key id to anyone who can scrape - and for an unauthenticated
        # caller the subject is a client IP, which makes the series count attacker-controlled.
        METRICS.inc(
            "synthverify_rate_limited_total",
            {"authenticated": "true" if api_key is not None else "false"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={"Retry-After": f"{max(1, int(retry_after) + 1)}"},
        )
