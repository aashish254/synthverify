"""The ``MediaStore`` contract (REQ-INFRA-4).

The key scheme is defined **once**, here, so a local file and an S3 object for
the same upload differ only by prefix::

    <sha256[:2]>/<sha256>_<sanitised-filename>

Two consequences worth stating explicitly:

* **Content-addressed, so dedupe is free.** Two organisations uploading the same
  evidence produce one object, and ``put`` of bytes already stored is a no-op.
* **A location is a string stored in ``MediaAsset.storage_path``.** It is derived
  from the key by the backend (``str`` path locally, ``s3://bucket/key`` in the
  object store), which is what lets AC-INFRA-4's swap happen with no migration
  and no code change: readers hand the string back to whichever store is
  configured.

Backends never raise for a missing object except through ``MediaNotFoundError``, so
callers can turn that into one honest pipeline error rather than an
``OSError``/``httpx`` leak.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

#: Sharded two hex chars deep: 256 buckets, small enough for any filesystem,
#: large enough that no directory grows with the whole corpus. This is also the
#: layout a v1 deployment already has on disk, so the seam changes nothing there.
_SHARD_PREFIX = 2
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._+-]+")
_DOT_RUN = re.compile(r"\.{2,}")
_MAX_NAME_CHARS = 96


class MediaStoreError(RuntimeError):
    """Base class for storage failures the pipeline is expected to handle."""


class MediaNotFoundError(MediaStoreError):
    """No object at that location - not a corrupted one, an absent one."""


class NotLocalError(MediaStoreError):
    """The caller asked for a filesystem path from a non-filesystem backend."""


def safe_filename(filename: str) -> str:
    """Reduce a user-supplied name to something path-safe, keeping it readable.

    The name is only ever kept for humans browsing the store; the digest is the
    identity. Slashes, ``..`` and control characters are removed rather than
    escaped, because a stored name must never be able to redirect a write.
    """
    base = filename.replace("/", "_").replace("\\", "_")
    base = _UNSAFE_NAME.sub("_", base)
    base = _DOT_RUN.sub("_", base).strip("._")
    if not base:
        return "upload.bin"
    if len(base) > _MAX_NAME_CHARS:
        stem, dot, ext = base.rpartition(".")
        ext = ext if dot and len(ext) <= 12 else "bin"
        base = f"{stem[: _MAX_NAME_CHARS - len(ext) - 1].rstrip('._')}.{ext}"
    return base or "upload.bin"


def content_key(digest: str, filename: str) -> str:
    """The one key scheme: ``<sha[:2]>/<sha>_<name>``."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"content keys need a sha256 hex digest, got {digest!r}")
    return f"{digest[:_SHARD_PREFIX]}/{digest}_{safe_filename(filename)}"


class MediaStore(ABC):
    """Where verified evidence bytes live. See the module docstring."""

    #: Configuration-visible name of the backend (``local`` | ``s3``).
    backend: str = "media-store"

    # ------------------------------------------------------------------ write

    @abstractmethod
    def put(self, digest: str, filename: str, data: bytes) -> str:
        """Store ``data`` under the content key for ``digest``; return its location.

        Idempotent: writing bytes that are already there must not fail, corrupt,
        or duplicate the object.
        """

    # ------------------------------------------------------------------- read

    @abstractmethod
    def get(self, location: str) -> bytes:
        """Return the stored bytes, or raise :class:`MediaNotFoundError`."""

    @abstractmethod
    def exists(self, location: str) -> bool:
        """Is there an object at ``location``?"""

    @abstractmethod
    def delete(self, location: str) -> bool:
        """Remove an object; ``True`` if something was deleted (REQ-INFRA-5)."""

    # ------------------------------------------------------- location <-> key

    @abstractmethod
    def location(self, key: str) -> str:
        """The string to persist in ``MediaAsset.storage_path`` for this key."""

    @abstractmethod
    def key_for_location(self, location: str) -> str:
        """Inverse of :meth:`location`, accepting a bare key too."""

    # ------------------------------------------------------------- extras

    def open_path(self, location: str) -> Path:
        """A filesystem path, for backends that have one.

        Only the local backend can honour this. Detectors must not depend on it:
        the S3-backed deployment reads bytes through :meth:`get`, which is what
        keeps AC-INFRA-4's "no code change" claim true.
        """
        raise NotLocalError(f"{self.backend} media is not addressable as a filesystem path: {location}")

    def describe(self) -> str:
        """Human-readable identity for logs, ``/readyz`` and the CLI."""
        return self.backend

    # --------------------------------------------------------------- helpers

    @staticmethod
    def key(digest: str, filename: str) -> str:
        """Content key for an upload - shared by every backend."""
        return content_key(digest, filename)
