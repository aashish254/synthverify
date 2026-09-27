"""The broker factory: one config key decides who hands work to whom.

``SV_JOB_BROKER=embedded|postgres`` is the whole deployment switch, exactly like
``SV_MEDIA_STORE``, and it fails closed on an unknown name: a typo in a queue setting must not
silently start a second, invisible queue in the API process. That is the mutation check the
``postgres`` backend is designed around - an operator who means "shared queue" and gets
"in-process queue" runs a deployment where a restart loses work, and it must say so at boot.
"""

from __future__ import annotations

import threading

from synthverify.brokers.base import JobBroker
from synthverify.brokers.embedded import EmbeddedBroker

_LOCK = threading.Lock()
_CACHE: dict[tuple[str, ...], JobBroker] = {}


def build_job_broker(settings, db) -> JobBroker:
    """Construct the configured broker for one :class:`Database`. No caching."""
    backend = (settings.job_broker or "embedded").strip().lower()
    if backend in {"embedded", "in-process", "inprocess"}:
        return EmbeddedBroker(
            db,
            queue_max_size=settings.queue_max_size,
            poll_seconds=settings.broker_poll_seconds,
        )
    if backend == "postgres":
        from synthverify.brokers.postgres import PostgresBroker

        return PostgresBroker(
            db,
            lease_seconds=settings.worker_lease_seconds,
            poll_seconds=settings.broker_poll_seconds,
        )
    raise ValueError(f"unknown SV_JOB_BROKER {settings.job_broker!r}; expected 'embedded' or 'postgres'")


def get_job_broker(db, settings=None) -> JobBroker:
    """The configured broker, created once per distinct (database, backend) pair.

    Keyed on the database URL as well as the selector because the embedded broker *is* its queue:
    handing a second ``Database`` the cached instance would file jobs into a queue nobody reads.
    """
    if settings is None:
        from synthverify.config import get_settings  # here to avoid an import cycle

        settings = get_settings()
    backend = (settings.job_broker or "embedded").strip().lower()
    key = (backend, db.database_url, str(settings.worker_lease_seconds), str(settings.broker_poll_seconds))
    with _LOCK:
        broker = _CACHE.get(key)
        if broker is None:
            broker = build_job_broker(settings, db)
            _CACHE[key] = broker
    return broker


def reset_job_broker_cache() -> None:
    """Drop cached brokers. Tests use this between configuration changes."""
    with _LOCK:
        _CACHE.clear()


def configured_backend() -> str:
    """Just the backend name, for ``/readyz`` and the CLI."""
    from synthverify.config import get_settings

    return (get_settings().job_broker or "embedded").strip().lower()
