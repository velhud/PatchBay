"""Immutable content-addressed files; bytes are durable before DB references."""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
from pathlib import Path

from ..errors import require
from ..util import fsync_dir


class Blobs:
    def __init__(self, root: Path):
        self.root = root

    def path(self, sha: str) -> Path:
        require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha), "INVALID_REQUEST", "Invalid SHA-256 identity")
        return self.root / sha[:2] / sha[2:]

    def put(self, data: bytes) -> str:
        return self.put_stream(io.BytesIO(data), limit=len(data))

    put_bytes = put

    def put_file(self, path: Path, *, limit: int = 512 * 1024**2) -> str:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "RESOURCE_LIMIT", "Input must be a bounded regular file")
            return self.put_stream(stream, limit=limit)

    def put_stream(self, stream, *, limit: int) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".blob-", dir=self.root)
        size, h = 0, hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as target:
                while True:
                    data = stream.read(65536)
                    if not data:
                        break
                    size += len(data)
                    require(size <= limit, "RESOURCE_LIMIT", "Content exceeds its byte limit")
                    h.update(data)
                    target.write(data)
                target.flush()
                os.fsync(target.fileno())
            sha = h.hexdigest()
            destination = self.path(sha)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                require(not destination.is_symlink() and destination.stat().st_size == size
                        and self.verify(sha), "CORRUPT_STATE", "Existing content-addressed bytes are corrupt")
            else:
                os.replace(name, destination)
                fsync_dir(destination.parent)
            return sha
        finally:
            Path(name).unlink(missing_ok=True)

    def read(self, sha: str, *, limit: int = 512 * 1024**2) -> bytes:
        path = self.path(sha)
        require(path.is_file() and not path.is_symlink(), "CORRUPT_STATE", "Referenced content is unavailable", sha256=sha)
        require(path.stat().st_size <= limit, "RESOURCE_LIMIT", "Content exceeds the read budget")
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == sha, "CORRUPT_STATE", "Stored content hash mismatch", sha256=sha)
        return data

    def verify(self, sha: str) -> bool:
        path = self.path(sha)
        if path.is_symlink() or not path.is_file():
            return False
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                h.update(chunk)
        return h.hexdigest() == sha
