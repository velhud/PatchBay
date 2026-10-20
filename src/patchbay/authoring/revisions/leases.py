"""Explicit writer capabilities and fencing epochs, independent of browser session metadata."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from ..errors import require
from ..persistence.gates import mutation
from ..persistence.locks import file_lock
from ..util import safe_id, utc_now


def token_hash(token: str) -> str:
    require(isinstance(token, str) and 16 <= len(token) <= 512, "LEASE_REQUIRED", "A valid task writer token is required")
    return hashlib.sha256(token.encode()).hexdigest()


class Leases:
    def __init__(self, store):
        self.store = store

    @mutation
    def claim(self, task_id: str, *, label: str = "author", token: str | None = None, takeover: bool = False) -> dict:
        safe_id(task_id)
        require(isinstance(label, str) and 0 < len(label) <= 200, "INVALID_REQUEST", "A bounded owner label is required")
        with file_lock(self.store.home / ("locks/task-" + task_id)), self.store.transaction() as conn:
            require(conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone(), "NOT_FOUND", "Task is unavailable")
            previous = conn.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            now = utc_now()
            matches = bool(previous and token and hmac.compare_digest(previous["token_hash"], token_hash(token)))
            if previous and previous["expires"] > now and not matches:
                require(takeover, "LEASE_CONFLICT", "Another writer owns this task; explicit takeover is required", label=previous["label"])
            if matches and previous["expires"] > now and not takeover:
                epoch = previous["epoch"]
                issued = token
            else:
                epoch = previous["epoch"] + 1 if previous else 1
                issued = secrets.token_urlsafe(48)
            expires = now + self.store.limits.lease_seconds
            conn.execute("INSERT INTO leases VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET token_hash=excluded.token_hash,label=excluded.label,epoch=excluded.epoch,expires=excluded.expires,updated=excluded.updated",
                         (task_id, token_hash(issued), label, epoch, expires, now))
            self.store.event(conn, "lease.claimed", task_id, {"epoch": epoch, "label": label, "takeover": takeover})
            return {"task_id": task_id, "token": issued, "epoch": epoch, "expires": expires, "label": label}

    def assert_owned(self, task_id: str, token: str, epoch: int, *, conn=None) -> None:
        safe_id(task_id)
        supplied = token_hash(token)
        if conn is not None:
            row = conn.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
        else:
            row = self.store.one("SELECT * FROM leases WHERE task_id=?", (task_id,), required=False)
        require(row and type(epoch) is int and row["epoch"] == epoch and row["expires"] > utc_now()
                and hmac.compare_digest(row["token_hash"], supplied),
                "LEASE_LOST", "Writer lease is missing, expired, or superseded")
