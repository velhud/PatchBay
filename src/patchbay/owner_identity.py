"""Persist the local owner-hashing namespace across server restarts."""
from __future__ import annotations

import fcntl
import os
import uuid
from pathlib import Path


def session_ref_salt(config: dict) -> str:
    explicit = os.environ.get("PATCHBAY_SESSION_REF_SALT")
    if explicit:
        return explicit
    logging = config.get("logging", {})
    root = Path(logging.get("job_state_dir") or Path(logging.get("job_logs_dir", ".local/logs/jobs")) / "state")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "owner-namespace"
    with (root / "owner-namespace.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.is_symlink():
            raise ValueError("Owner namespace must not be a symlink")
        if path.exists():
            value = path.read_text().strip()
            if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Invalid persisted owner namespace; explicit recovery is required")
            return value
        value = uuid.uuid4().hex
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
            output.write(value + "\n")
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return value
