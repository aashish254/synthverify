"""The rate limiter: one subject, one budget, wherever the budget lives.

`REQ-INFRA-3` names the seam in one clause - the swap to a shared bucket must be *invisible to
callers* - so this ABC carries the exact contract the old in-process class had:
``check(subject) -> (allowed, retry_after)``. Nothing else is public. Route handlers, the FastAPI
dependency in ``synthverify/ratelimit.py`` and every test that asserts on ``429``/``Retry-After``
keep working with either backend, which is why §4.3 calls the unchanged signature the FC-2 escape
hatch: the interface was already narrow enough that scaling it out needed no interface churn.

`degraded` is the one field the swap adds, and it exists because a shared bucket can go away while
the process keeps serving. It answers the operator question that ``name`` cannot - *"I configured
the shared limiter; am I actually sharing?"* - and it is reported by ``/readyz`` rather than
surfaced to the caller, because `AC-INFRA-3(b)` requires the request path to keep answering with
its own limit semantics instead of erroring.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from synthverify.config import Settings, get_settings


class RateLimiter(ABC):
    """Admits or refuses one request for one subject."""

    #: selector value, e.g. ``in-process`` / ``valkey`` - reported by ``/readyz``
    name: str = "abstract"

    def __init__(
        self,
        rpm: int | None = None,
        burst: int | None = None,
        settings: Settings | None = None,
    ):
        #: the limiter reads its budget from here, and a caller that supplies ``settings`` (a
        #: test, a per-org override) is not forced to stage environment variables first
        self.settings = settings or get_settings()
        self.rate = (rpm or self.settings.rate_limit_rpm) / 60.0  # tokens per second
        self.burst = burst or self.settings.rate_limit_burst

    @abstractmethod
    def check(self, subject: str) -> tuple[bool, float]:
        """Returns ``(allowed, retry_after_seconds)`` for one request on one subject.

        Contract, since the two backends keep their state in different places: this never raises
        to mean "over the limit" - a refusal is ``allowed=False`` plus a positive ``retry_after``.
        It is called on the request path, so an implementation that reaches for another process
        has to bound how long that reach takes; that bound is what keeps a dead shared bucket from
        turning into a hung API.
        """

    @property
    def degraded(self) -> bool:
        """Whether this limiter is *not* using the shared state it was configured for."""
        return False

    def close(self) -> None:  # noqa: B027 - deliberately optional, not an unimplemented hook
        """Release whatever the backend holds. Default: nothing to release."""
