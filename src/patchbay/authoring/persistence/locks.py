"""Cross-process locks, explicit nonblocking maintenance, and safe nested use."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from ..errors import AuthorError, require

_local = threading.local()


@contextmanager
def file_lock(path: Path, *, shared: bool = False, blocking: bool = True):
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("Author runtime locking requires POSIX; use Linux/WSL2") from exc
    path = path.absolute()
    key = str(path)
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    if key in held:
        require(shared or not held[key], "LOCK_ORDER", "Cannot upgrade an already-held shared lock")
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            fcntl.flock(descriptor, mode | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise AuthorError("MAINTENANCE_BLOCKED", "An author operation currently owns the maintenance boundary") from exc
        held[key] = shared
        try:
            yield
        finally:
            held.pop(key, None)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
