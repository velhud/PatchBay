"""Bounded disk accounting for explicit admission and maintenance, not status polls."""
from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path
from ..errors import require


def tree_usage(root: Path, *, stop_above: int = 2**63, max_entries: int = 1000000, timeout_seconds: float = 10.0) -> dict:
    total, count, complete = 0, 0, True
    started, stack = time.monotonic(), [Path(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > max_entries or time.monotonic() - started > timeout_seconds:
                        return {"bytes": total, "entries": count, "complete": False}
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                    if total > stop_above:
                        return {"bytes": total, "entries": count, "complete": False}
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
    return {"bytes": total, "entries": count, "complete": complete}


class StorageBudget:
    def __init__(self, store):
        self.store = store

    def check(self, extra: int = 0) -> dict:
        free = shutil.disk_usage(self.store.home).free
        limits = self.store.limits
        require(free - max(extra, 0) >= limits.minimum_free_bytes, "RESOURCE_LIMIT", "Insufficient free disk for a new author operation")
        usage = tree_usage(self.store.home, stop_above=limits.total_storage_bytes)
        require(usage["complete"] and usage["bytes"] + max(extra, 0) <= limits.total_storage_bytes,
                "RESOURCE_LIMIT", "Author runtime storage budget exceeded or accounting is incomplete", observation=usage)
        return {**usage, "free_bytes": free}
