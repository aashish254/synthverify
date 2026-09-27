"""The job broker: how a job id reaches a worker, and nothing else.

`REQ-INFRA-2` names the seam in one clause - *"using* `process_job(db, id)` *as the unit of work"* -
so a broker decides **who gets handed which job id**, never what the worker does with it. The two
backends here differ in exactly that and nothing more.

`durable` is the property the rest of the app branches on. An in-process queue dies with its
process, so an API request that enqueues into one with no local fleet would be filing work into a
void, and `routes_media._dispatch` runs the job inline instead. A durable queue *is* shared state,
so handing a job to it is real dispatch even in a replica with no workers at all - which is the
configuration `AC-INFRA-2` runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class JobClaim:
    """One job, taken by one worker.

    `token` is the write fence: `None` means the backend has no notion of a lease (the embedded
    queue, or a job run inline by the request thread), and a token means only a worker that can
    still present it may record an outcome. That is what turns "two workers picked the same job"
    from a corruption into a no-op.
    """

    job_id: str
    token: str | None = None
    worker_id: str = ""


class JobBroker(ABC):
    """A queue of work whose unit is `process_job(db, job_id)`."""

    #: selector value, e.g. ``embedded`` / ``postgres`` - reported by ``/readyz``
    name: str = "abstract"
    #: whether the queue outlives this process, i.e. whether another replica can see the same work
    durable: bool = False

    def start(self) -> None:  # noqa: B027 - deliberately optional, not an unimplemented hook
        """Validate the configuration. Must raise rather than degrade quietly."""

    def stop(self, timeout: float = 5.0) -> None:  # noqa: B027 - see `start`
        """Release anything the backend holds. Default: nothing to do."""

    @abstractmethod
    def enqueue(self, job_id: str, priority: int = 5) -> None:
        """Make a durably-created job claimable."""

    @abstractmethod
    def claim(self, worker_id: str) -> JobClaim | None:
        """Take one claimable job, or return `None` if there is none.

        Contract, since the two backends wait differently: this may block for up to the configured
        poll interval, and it returns at most one job per call. It never raises to mean "empty".
        """

    @abstractmethod
    def depth(self) -> int:
        """How much work is waiting. `-1` if the backend cannot say cheaply."""

    @abstractmethod
    def recover(self) -> int:
        """Make stranded work claimable again; return how much. Called at startup."""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "durable": self.durable}
