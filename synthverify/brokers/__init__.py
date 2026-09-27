"""REQ-INFRA-2: the job-queue seam.

One interface, two backends - an in-process priority queue (the default, no external service) and
a shared PostgreSQL queue claimed with ``FOR UPDATE SKIP LOCKED`` - chosen by ``SV_JOB_BROKER`` at
deploy time. The unit of work on both is ``synthverify.worker.process_job(db, job_id)``, which is
the clause `AC-INFRA-2` depends on: the broker decides *which worker gets which job*, never what
the worker does.
"""

from synthverify.brokers.base import JobBroker, JobClaim
from synthverify.brokers.embedded import EmbeddedBroker
from synthverify.brokers.factory import (
    build_job_broker,
    configured_backend,
    get_job_broker,
    reset_job_broker_cache,
)

__all__ = [
    "EmbeddedBroker",
    "JobBroker",
    "JobClaim",
    "build_job_broker",
    "configured_backend",
    "get_job_broker",
    "reset_job_broker_cache",
]
