"""Cross-process advisory locks with same-thread nested acquisition support."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from ..errors import require

_local = threading.local()


@contextmanager
def file_lock(path: Path, *, shared: bool = False):
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
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        held[key] = shared
        try:
            yield
        finally:
            held.pop(key, None)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
