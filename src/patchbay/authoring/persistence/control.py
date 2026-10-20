"""Durable controller pause and per-task admission grants.

Control only gates new side effects. It does not cancel active work, resume
unknown execution, or alter retained candidates/evidence. Interface adapters
must enforce operator identity before calling restore_grant or revoke.
"""
from __future__ import annotations

import json
from ..authority import assert_operator

from ..errors import require
from ..util import canonical, fingerprint, safe_id, utc_now

_GLOBAL_KEY = "author.control.state"
_TASK_PREFIX = "author.control.task."
_JOURNAL_PREFIX = "author.control.operation."
_MAX_STATUS_COUNT = 1_000_000
_MAX_CONTROL_RECORDS = 10000


class Control:
    def __init__(self, store):
        self.store = store

    def pause(self, *, key: str, actor: str) -> dict:
        """Stop new task/package/check admission and report the durable result."""
        assert_operator()
        self._validate_action(key, actor)
        action = {"action": "pause", "actor": actor}
        fp = fingerprint(action)
        with self.store.transaction() as conn:
            replay = self._replay(conn, key, fp)
            if replay is not None:
                return replay
            state = {"paused": True, "actor": actor, "updated": utc_now(), "key": key}
            conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (_GLOBAL_KEY, canonical(state).decode()))
            result = {"paused": True}
            self._record(conn, key, fp, result)
            self.store.event(conn, "control.paused", "", {"actor": actor, "key": key})
            return result

    def resume(self, *, key: str, actor: str) -> dict:
        """Resume new admission; revoked task grants remain revoked."""
        assert_operator()
        self._validate_action(key, actor)
        action = {"action": "resume", "actor": actor}
        fp = fingerprint(action)
        with self.store.transaction() as conn:
            replay = self._replay(conn, key, fp)
            if replay is not None:
                return replay
            state = {"paused": False, "actor": actor, "updated": utc_now(), "key": key}
            conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (_GLOBAL_KEY, canonical(state).decode()))
            result = {"paused": False}
            self._record(conn, key, fp, result)
            self.store.event(conn, "control.resumed", "", {"actor": actor, "key": key})
            return result

    def assert_admission(self, task_id: str | None = None, *, conn=None) -> None:
        """Guard only new admissions; callers should run this before effects."""
        if conn is None:
            with self.store.connect() as connection:
                row = connection.execute("SELECT value FROM metadata WHERE key=?", (_GLOBAL_KEY,)).fetchone()
                state = json.loads(row[0]) if row else {"paused": False}
                require(not state.get("paused"), "CONTROL_PAUSED", "New task, package, and check admission is paused")
                if task_id is not None:
                    self.assert_granted(task_id, conn=connection)
            return
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (_GLOBAL_KEY,)).fetchone()
        state = json.loads(row[0]) if row else {"paused": False}
        require(not state.get("paused"), "CONTROL_PAUSED", "New task, package, and check admission is paused")
        if task_id is not None:
            self.assert_granted(task_id, conn=conn)

    def assert_granted(self, task_id: str, *, conn=None) -> None:
        """Reject effects for a task whose explicit grant was revoked."""
        safe_id(task_id)
        key = _TASK_PREFIX + task_id
        if conn is None:
            value = self.store.one("SELECT value FROM metadata WHERE key=?", (key,), required=False)
        else:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            value = {"value": row[0]} if row else None
        grant = json.loads(value["value"]) if value else {"granted": True}
        require(grant.get("granted", True), "TASK_REVOKED", "Task grant was revoked; restore it explicitly before new effects",
                task_id=task_id)

    def revoke(self, task_id: str, *, key: str, actor: str, cancel_queued: bool = True) -> dict:
        """Revoke future task effects and optionally block queued checks atomically."""
        assert_operator(task_id)
        safe_id(task_id)
        self._validate_action(key, actor)
        require(type(cancel_queued) is bool, "INVALID_REQUEST", "cancel_queued must be boolean")
        action = {"action": "revoke", "task_id": task_id, "actor": actor,
                  "cancel_queued": cancel_queued}
        fp = fingerprint(action)
        with self.store.transaction() as conn:
            replay = self._replay(conn, key, fp)
            if replay is not None:
                return replay
            require(conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None,
                    "NOT_FOUND", "Task is unavailable", status=404)
            now = utc_now()
            grant = {"granted": False, "actor": actor, "updated": now, "key": key}
            conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (_TASK_PREFIX + task_id, canonical(grant).decode()))
            blocked = 0
            if cancel_queued:
                rows = conn.execute("SELECT o.id FROM operations o JOIN runs r ON r.id=o.id "
                                    "WHERE o.task_id=? AND o.kind='checks.run' AND o.state='queued' AND r.state='queued'",
                                    (task_id,)).fetchall()
                error = canonical({"code": "TASK_REVOKED", "message": "Task grant was revoked before this check started"}).decode()
                for row in rows:
                    operation_id = row["id"]
                    conn.execute("UPDATE runs SET state='blocked',updated=? WHERE id=? AND state='queued'",
                                 (now, operation_id))
                    changed = conn.execute("UPDATE operations SET state='blocked',phase='terminal',error=?,updated=? "
                                           "WHERE id=? AND state='queued'", (error, now, operation_id))
                    blocked += changed.rowcount
                    self.store.event(conn, "checks.blocked_task_revoked", task_id,
                                     {"operation_id": operation_id})
            active = conn.execute("SELECT COUNT(*) FROM runs WHERE task_id=? AND state IN ('launching','running','cancelling')",
                                  (task_id,)).fetchone()[0]
            result = {"task_id": task_id, "granted": False,
                      "queued_checks_blocked": blocked, "active_effects_preserved": active}
            self._record(conn, key, fp, result)
            self.store.event(conn, "control.task_revoked", task_id,
                             {"actor": actor, "key": key, "queued_checks_blocked": blocked,
                              "active_effects_preserved": active})
            return result

    def restore_grant(self, task_id: str, *, key: str, actor: str) -> dict:
        """Restore one task grant after the caller's operator authorization."""
        assert_operator(task_id)
        safe_id(task_id)
        self._validate_action(key, actor)
        action = {"action": "restore_grant", "task_id": task_id, "actor": actor}
        fp = fingerprint(action)
        with self.store.transaction() as conn:
            replay = self._replay(conn, key, fp)
            if replay is not None:
                return replay
            require(conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None,
                    "NOT_FOUND", "Task is unavailable", status=404)
            grant = {"granted": True, "actor": actor, "updated": utc_now(), "key": key}
            conn.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (_TASK_PREFIX + task_id, canonical(grant).decode()))
            result = {"task_id": task_id, "granted": True}
            self._record(conn, key, fp, result)
            self.store.event(conn, "control.task_grant_restored", task_id, {"actor": actor, "key": key})
            return result

    def status(self) -> dict:
        """Return bounded counts and truthful controller/drain waiting states."""
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (_GLOBAL_KEY,)).fetchone()
            state = json.loads(row[0]) if row else {"paused": False}
            counts = {
                "active": self._count(conn, "SELECT COUNT(*) FROM runs WHERE state IN ('launching','running','cancelling')"),
                "queued": self._count(conn, "SELECT COUNT(*) FROM operations WHERE state='queued'"),
                "uncertain": self._count(conn, "SELECT COUNT(*) FROM operations WHERE state IN ('unknown_execution','publication_unknown')"),
                "awaiting_approval": self._count(conn, "SELECT COUNT(*) FROM operations WHERE state='awaiting_approval'"),
                "pending_decision": self._count(conn, "SELECT COUNT(*) FROM operations WHERE phase IN ('pending_decision','awaiting_controller','awaiting_input')"),
            }
        paused = bool(state.get("paused"))
        states = []
        if paused:
            states.append("awaiting_controller")
            states.append("draining" if counts["active"]["count"] else "paused")
        if counts["uncertain"]["count"]:
            states.append("awaiting_controller")
        if counts["awaiting_approval"]["count"]:
            states.append("awaiting_approval")
        if counts["pending_decision"]["count"]:
            states.append("pending_decision")
        if counts["queued"]["count"]:
            states.append("queued")
        if counts["active"]["count"]:
            states.append("active")
        if not states:
            states.append("idle")
        # Preserve order while de-duplicating `awaiting_controller`.
        return {"paused": paused, "counts": counts, "states": list(dict.fromkeys(states)),
                "drain_state": "draining" if paused and counts["active"]["count"] else ("paused" if paused else "open"),
                "background_model_continuation": False}

    def _replay(self, conn, key: str, fp: str) -> dict | None:
        key, fp = self.store.scoped_key(key), self.store.scoped_fingerprint(fp)
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (_JOURNAL_PREFIX + key,)).fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        require(record["fingerprint"] == fp, "IDEMPOTENCY_CONFLICT",
                "Control idempotency key was already used for different semantics")
        return record["result"]

    def _record(self, conn, key: str, fp: str, result: dict) -> None:
        key, fp = self.store.scoped_key(key), self.store.scoped_fingerprint(fp)
        count = conn.execute("SELECT COUNT(*) FROM metadata WHERE key LIKE ?",
                             (_JOURNAL_PREFIX + "%",)).fetchone()[0]
        require(count < _MAX_CONTROL_RECORDS, "RESOURCE_LIMIT", "Control journal capacity is full")
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)",
                     (_JOURNAL_PREFIX + key,
                      canonical({"fingerprint": fp, "result": result, "completed": utc_now()}).decode()))

    @staticmethod
    def _validate_action(key: str, actor: str) -> None:
        require(isinstance(key, str) and 0 < len(key) <= 200 and "\x00" not in key,
                "INVALID_REQUEST", "A bounded control idempotency key is required")
        require(isinstance(actor, str) and 0 < len(actor) <= 200 and "\x00" not in actor,
                "INVALID_REQUEST", "A bounded control actor is required")

    @staticmethod
    def _count(conn, sql: str) -> dict:
        value = int(conn.execute(sql).fetchone()[0])
        return {"count": min(value, _MAX_STATUS_COUNT), "capped": value > _MAX_STATUS_COUNT}
