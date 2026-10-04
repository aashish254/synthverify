"""The local-filesystem backend - today's layout, unchanged.

``<storage_dir>/<sha[:2]>/<sha256>_<name>`` is what v1 already writes, so
enabling this backend on an existing deployment reads every pre-refactor row:
:attr:`MediaAsset.storage_path` holds a plain absolute path, and a stored path is
honoured verbatim when it exists rather than being re-derived under a possibly
moved root.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from synthverify.storage.base import MediaNotFoundError, MediaStore


class LocalMediaStore(MediaStore):
    """Media on the operator's own disk. The default, and the FC-2 baseline."""

    backend = "local"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ write

    def put(self, digest: str, filename: str, data: bytes) -> str:
        key = self.key(digest, filename)
        path = self.resolve(key)
        if path.exists():
            return self.location(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a reader must never see a half-written upload, which
        # on a shared volume would otherwise turn a partial file into a verdict.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.location(key)

    # ------------------------------------------------------------------- read

    def get(self, location: str) -> bytes:
        path = self.resolve(location)
        if not path.is_file():
            raise MediaNotFoundError(f"no media at {location}")
        return path.read_bytes()

    def exists(self, location: str) -> bool:
        return self.resolve(location).is_file()

    def delete(self, location: str) -> bool:
        path = self.resolve(location)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    # ------------------------------------------------------- location <-> key

    def location(self, key: str) -> str:
        return str(self.root / key)

    def key_for_location(self, location: str) -> str:
        path = self.resolve(location)
        try:
            # `as_posix`, not `str`: the key scheme is `<sha[:2]>/<sha>_<name>` on every backend,
            # and a Windows `str()` of the same relative path yields backslashes the S3 parity
            # claims and the retention sweep's key comparisons both read as a different key.
            return path.relative_to(self.root).as_posix()
        except ValueError:
            # Written by an older deployment with a different root: keep the
            # shard/<sha>_<name> tail, which is the part that is portable.
            return "/".join(path.parts[-2:])

    # ----------------------------------------------------------------- extras

    def open_path(self, location: str) -> Path:
        path = self.resolve(location)
        if not path.is_file():
            raise MediaNotFoundError(f"no media at {location}")
        return path

    def describe(self) -> str:
        return f"local:{self.root}"

    # ---------------------------------------------------------------- helpers

    def resolve(self, location: str) -> Path:
        """Accept a full path (legacy rows, this backend's own output) or a key."""
        candidate = Path(location)
        if candidate.is_absolute():
            return candidate
        return self.root / location
