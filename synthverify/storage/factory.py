"""The store factory: one config key decides where evidence bytes live.

``SV_MEDIA_STORE=local|s3`` is the whole deployment switch (AC-INFRA-4), so the
cache is keyed on every field that changes a backend's identity - pointing a live
process at a new bucket or root takes a config reload, not a code change.

The cache exists for a second reason: the API builds a store per request and the
embedded workers build one per job, and a shared ``httpx.Client`` (connection
pool) is the difference between a working MinIO deployment and one that opens a
new TCP connection per byte read.
"""

from __future__ import annotations

import threading

from synthverify.storage.base import MediaStore
from synthverify.storage.local import LocalMediaStore
from synthverify.storage.s3 import S3MediaStore

_LOCK = threading.Lock()
_CACHE: dict[tuple[str, ...], MediaStore] = {}


def _fingerprint(settings) -> tuple[str, ...]:
    backend = (settings.media_store or "local").strip().lower()
    if backend == "s3":
        return (
            "s3", str(settings.s3_endpoint), str(settings.s3_bucket), str(settings.s3_region),
            "path" if settings.s3_path_style else "vhost", str(settings.s3_prefix),
            str(settings.s3_access_key), str(settings.s3_secret_key),
        )
    return (backend, str(settings.storage_dir))


def build_media_store(settings) -> MediaStore:
    """Construct a store from a :class:`~synthverify.config.Settings`. No caching."""
    backend = (settings.media_store or "local").strip().lower()
    if backend in {"local", "fs", "filesystem"}:
        return LocalMediaStore(settings.storage_dir)
    if backend == "s3":
        if not settings.s3_bucket:
            raise ValueError("SV_MEDIA_STORE=s3 requires SV_S3_BUCKET")
        if not settings.s3_endpoint:
            raise ValueError("SV_MEDIA_STORE=s3 requires SV_S3_ENDPOINT (MinIO or AWS)")
        return S3MediaStore(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            path_style=settings.s3_path_style,
            prefix=settings.s3_prefix,
        )
    raise ValueError(f"unknown SV_MEDIA_STORE {settings.media_store!r}; expected 'local' or 's3'")


def get_media_store(settings=None) -> MediaStore:
    """The configured store, created once per distinct configuration."""
    if settings is None:
        from synthverify.config import get_settings  # here to avoid an import cycle

        settings = get_settings()
    key = _fingerprint(settings)
    with _LOCK:
        store = _CACHE.get(key)
        if store is None:
            store = build_media_store(settings)
            _CACHE[key] = store
    return store


def reset_media_store_cache() -> None:
    """Drop cached stores. Tests use this between configuration changes."""
    with _LOCK:
        _CACHE.clear()


def configured_backend() -> str:
    """Just the backend name, for ``/readyz`` and the CLI - no credentials."""
    from synthverify.config import get_settings

    return (get_settings().media_store or "local").strip().lower()
