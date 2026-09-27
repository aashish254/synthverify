"""REQ-INFRA-4: the media-storage seam.

One interface, two backends (local filesystem, S3-compatible object storage),
chosen by ``SV_MEDIA_STORE`` at deploy time. AC-INFRA-4 is the claim this package
exists to make: swapping ``local`` -> ``s3`` requires **no code change**, so the
parity test in ``tests/test_media_store.py`` runs the same assertions against
both.
"""

from synthverify.storage.base import (
    MediaNotFoundError,
    MediaStore,
    MediaStoreError,
    NotLocalError,
    content_key,
    safe_filename,
)
from synthverify.storage.factory import get_media_store, reset_media_store_cache
from synthverify.storage.local import LocalMediaStore
from synthverify.storage.s3 import S3MediaStore

__all__ = [
    "LocalMediaStore",
    "MediaNotFoundError",
    "MediaStore",
    "MediaStoreError",
    "NotLocalError",
    "S3MediaStore",
    "content_key",
    "get_media_store",
    "reset_media_store_cache",
    "safe_filename",
]
