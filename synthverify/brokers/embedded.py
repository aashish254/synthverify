"""The default broker: a priority queue inside this process.

This is `WorkerFleet`'s queue, moved rather than rewritten, so the shipped behaviour does not move
with it: same `maxsize`, same priority ordering, same monotonic sequence as the FIFO tiebreaker for
equal priorities, same startup sweep that re-enqueues jobs a crash left `queued`/`running`, same
metric. The spec keeps this the default for a reason - one process, zero external services, works
on a laptop and on a `postgres:16` box alike (FC-2).
"""

from __future__ import annotations

import queue
import threading

from sqlalchemy import select

from synthverify.brokers.base import JobBroker, JobClaim
from synthverify.db import AuditLedger, Job, JobStatus
from synthverify.metrics import METRICS


class EmbeddedBroker(JobBroker):
    """In-process priority queue. Not durable: it dies with the process."""

    name = "embedded"
    durable = False

    def __init__(self, db, *, queue_max_size: int = 512, poll_seconds: float = 0.5):
        self.db = db
        self.poll_seconds = poll_seconds
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=queue_max_size)
        self._seq = 0
        self._seq_lock = threading.Lock()

    def enqueue(self, job_id: str, priority: int = 5) -> None:
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        self._queue.put((priority, seq, job_id))
        METRICS.inc("synthverify_jobs_enqueued_total", {"priority": str(priority)})

    def claim(self, worker_id: str) -> JobClaim | None:
        try:
            _, _, job_id = self._queue.get(timeout=self.poll_seconds)
        except queue.Empty:
            return None
        # The embedded queue has one consumer set (this process), so a token would fence nothing.
        return JobClaim(job_id=job_id, token=None, worker_id=worker_id)

    def depth(self) -> int:
        return self._queue.qsize()

    def recover(self) -> int:
        """Re-enqueue jobs orphaned by a crash (`queued`/`running` rows, no live consumer)."""
        with self.db.session() as session:
            stale = session.execute(
                select(Job).where(Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]))
            ).scalars().all()
            for job in stale:
                self.enqueue(job.id, job.priority)
            if stale:
                AuditLedger(session).append(
                    actor="system:recovery",
                    action="jobs.recovered",
                    detail={"count": len(stale), "broker": self.name},
                )
                session.commit()
        return len(stale)
