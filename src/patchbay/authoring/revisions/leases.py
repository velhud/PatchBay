"""Explicit writer capabilities and fencing epochs, independent of browser session metadata."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import json
import uuid
from ..errors import require
from ..persistence.gates import mutation
from ..persistence.control import Control
from ..persistence.locks import file_lock
from ..util import safe_id, utc_now, canonical, fingerprint
from ..authority import assert_operator, current_identity
from ..util import strict_json


def token_hash(token: str) -> str:
    require(isinstance(token, str) and 16 <= len(token) <= 512, "LEASE_REQUIRED", "A valid task writer token is required")
    return hashlib.sha256(token.encode()).hexdigest()


class Leases:
    def __init__(self, store):
        self.store = store

    @mutation
    def claim(self, task_id: str, *, label: str = "author", token: str | None = None, takeover: bool = False, key: str | None = None) -> dict:
        safe_id(task_id)
        identity = current_identity()
        require(identity.task_ids is None or task_id in identity.task_ids, "FORBIDDEN",
                "This principal is not granted access to the requested task", status=403)
        require(isinstance(label, str) and 0 < len(label) <= 200, "INVALID_REQUEST", "A bounded owner label is required")
        if takeover:
            assert_operator(task_id, identity)
        request = {"label": label, "token_hash": token_hash(token) if token else None, "takeover": takeover}
        operation, _ = self.store.begin_operation(kind="lease.claim", task_id=task_id,
            key=key or "local-" + uuid.uuid4().hex, fingerprint=fingerprint(request), payload=request)
        with file_lock(self.store.home / ("locks/task-" + task_id)), self.store.transaction() as conn:
            replay = conn.execute("SELECT result FROM operations WHERE id=? AND state='completed'", (operation["id"],)).fetchone()
            if replay:
                return json.loads(replay["result"])
            Control(self.store).assert_granted(task_id, conn=conn)
            require(conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone(), "NOT_FOUND", "Task is unavailable")
            previous = conn.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            owner_row = conn.execute("SELECT value FROM metadata WHERE key=?", (self._owner_key(task_id),)).fetchone()
            owner = strict_json(owner_row[0]) if owner_row else None
            now = utc_now()
            matches = bool(previous and token and hmac.compare_digest(previous["token_hash"], token_hash(token)))
            if previous and previous["expires"] > now and not matches:
                require(takeover, "LEASE_CONFLICT", "Another writer owns this task; explicit takeover is required", label=previous["label"])
            if previous and previous["expires"] > now and matches:
                if owner is None:
                    require(identity.legacy or takeover, "LEASE_LOST",
                            "This legacy lease has no principal binding; an operator must explicitly take it over")
                else:
                    require(owner.get("principal_id") == identity.principal_id or takeover, "LEASE_LOST",
                            "Writer lease belongs to a different authenticated principal")
                    require(owner.get("epoch") == previous["epoch"] and owner.get("token_hash") == previous["token_hash"],
                            "CORRUPT_STATE", "Lease owner binding does not match the active lease")
            if matches and previous["expires"] > now and not takeover:
                epoch = previous["epoch"]
                issued = token
            else:
                epoch = previous["epoch"] + 1 if previous else 1
                issued = secrets.token_urlsafe(48)
            expires = now + self.store.limits.lease_seconds
            conn.execute("INSERT INTO leases VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET token_hash=excluded.token_hash,label=excluded.label,epoch=excluded.epoch,expires=excluded.expires,updated=excluded.updated",
                         (task_id, token_hash(issued), label, epoch, expires, now))
            owner_record = {"principal_id": identity.principal_id, "epoch": epoch, "token_hash": token_hash(issued)}
            conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (self._owner_key(task_id), canonical(owner_record).decode()))
            self.store.event(conn, "lease.claimed", task_id, {"epoch": epoch, "label": label, "takeover": takeover})
            result = {"task_id": task_id, "token": issued, "epoch": epoch, "expires": expires, "label": label,
                      "request_operation_id": operation["id"]}
            conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                         (canonical(result).decode(), now, operation["id"]))
            return result

    def assert_owned(self, task_id: str, token: str, epoch: int, *, conn=None) -> None:
        safe_id(task_id)
        supplied = token_hash(token)
        identity = current_identity()
        require(identity.task_ids is None or task_id in identity.task_ids, "FORBIDDEN",
                "This principal is not granted access to the requested task", status=403)
        if conn is not None:
            row = conn.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            owner_row = conn.execute("SELECT value FROM metadata WHERE key=?", (self._owner_key(task_id),)).fetchone()
        else:
            row = self.store.one("SELECT * FROM leases WHERE task_id=?", (task_id,), required=False)
            owner_row = self.store.one("SELECT value FROM metadata WHERE key=?", (self._owner_key(task_id),), required=False)
        require(row and type(epoch) is int and row["epoch"] == epoch and row["expires"] > utc_now()
                and hmac.compare_digest(row["token_hash"], supplied),
                "LEASE_LOST", "Writer lease is missing, expired, or superseded")
        if owner_row is None:
            require(identity.legacy, "LEASE_LOST", "This legacy lease has no principal binding")
        else:
            owner = strict_json(owner_row["value"])
            require(owner.get("principal_id") == identity.principal_id
                    and owner.get("epoch") == row["epoch"] and owner.get("token_hash") == supplied,
                    "LEASE_LOST", "Writer lease belongs to a different authenticated principal")

    @staticmethod
    def _owner_key(task_id: str) -> str:
        return "author.lease.owner." + task_id
