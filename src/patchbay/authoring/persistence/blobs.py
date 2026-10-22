"""Immutable content-addressed files; bytes are durable before DB references."""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
import uuid
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

    def open_verified(self, sha: str, *, limit: int = 512 * 1024**2):
        """Return the same regular descriptor that was verified, positioned at zero.

        Content storage is private and immutable. Path replacement after open
        cannot substitute a different file for delivery through this handle.
        """
        path = self.path(sha)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0))
        except OSError as exc:
            from ..errors import AuthorError
            raise AuthorError('CORRUPT_STATE', 'Referenced artifact cannot be opened', sha256=sha) from exc
        stream = os.fdopen(descriptor, 'rb')
        try:
            before = os.fstat(descriptor)
            require(stat.S_ISREG(before.st_mode), 'CORRUPT_STATE', 'Artifact is not a regular file')
            require(before.st_size <= limit, 'RESOURCE_LIMIT', 'Artifact exceeds the delivery budget')
            h, count = hashlib.sha256(), 0
            for chunk in iter(lambda: stream.read(65536), b''):
                h.update(chunk)
                count += len(chunk)
                require(count <= before.st_size, 'CORRUPT_STATE', 'Artifact changed during verification')
            after = os.fstat(descriptor)
            binding = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            require(count == before.st_size and binding(before) == binding(after)
                    and h.hexdigest() == sha, 'CORRUPT_STATE', 'Stored artifact hash or size mismatch', sha256=sha)
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise

    @staticmethod
    def delivery_chunks(stream, sha: str, expected_bytes: int):
        """Keep the final chunk until delivery identity is checked again."""
        h, count, pending = hashlib.sha256(), 0, b''
        for chunk in iter(lambda: stream.read(65536), b''):
            h.update(chunk)
            count += len(chunk)
            require(count <= expected_bytes, 'CORRUPT_STATE', 'Artifact grew during delivery')
            if pending:
                yield pending
            pending = chunk
        require(count == expected_bytes and h.hexdigest() == sha,
                'CORRUPT_STATE', 'Artifact changed during delivery', sha256=sha)
        if pending:
            yield pending

    def export(self, sha: str, output: Path) -> dict:
        """Verify, sync and expose a complete sibling file without clobbering."""
        output = Path(output)
        parent = os.open(output.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                         | getattr(os, 'O_NOFOLLOW', 0))
        temporary = '.author-export-' + uuid.uuid4().hex
        created = False
        try:
            with self.open_verified(sha) as source:
                size = os.fstat(source.fileno()).st_size
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                created = True
                with os.fdopen(descriptor, 'wb') as target:
                    for chunk in self.delivery_chunks(source, sha, size):
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    os.link(temporary, output.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError as exc:
                    from ..errors import AuthorError
                    raise AuthorError('OUTPUT_EXISTS', 'Export destination already exists; choose a new filename') from exc
                os.fsync(parent)
                return {'output': str(output), 'sha256': sha, 'bytes': size, 'delivery': 'verified'}
        finally:
            try:
                if created:
                    os.unlink(temporary, dir_fd=parent)
            finally:
                os.close(parent)

    def verify(self, sha: str) -> bool:
        path = self.path(sha)
        if path.is_symlink() or not path.is_file():
            return False
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                h.update(chunk)
        return h.hexdigest() == sha
