"""Explicit installation and bounded reads for operation-change watermarks."""
from __future__ import annotations

from ..authority import assert_task_scope, current_identity
from ..errors import AuthorError, require
from .locks import file_lock
from .schema import (STATE_CLOCK_GLOBAL, STATE_CLOCK_MARKER, STATE_CLOCK_PREFIX,
                     STATE_CLOCK_TRIGGERS)

_TRIGGER_NAMES = ("operations_state_clock_insert", "operations_state_clock_update",
                  "operations_state_clock_delete")
_QUIESCENT_STATES = ("completed", "failed", "blocked", "timed_out", "cancelled")


def _operator_only() -> None:
    identity = current_identity()
    require(identity.role == "operator" and identity.task_ids is None, "FORBIDDEN",
            "Installing Author state clocks requires an unscoped operator")


def _assert_quiescent(conn) -> None:
    active = conn.execute(
        "SELECT 1 FROM runs WHERE state NOT IN ('completed','failed','blocked','timed_out','cancelled') "
        "OR cleanup IN ('pending','unresolved') LIMIT 1").fetchone()
    require(active is None, "MAINTENANCE_BLOCKED",
            "Finish or reconcile active and uncertain checks before installing state clocks")


def _generation(value, *, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AuthorError("CORRUPT_STATE", f"State clock {name} is invalid") from exc
    require(parsed >= 0 and str(parsed) == str(value), "CORRUPT_STATE", f"State clock {name} is invalid")
    return parsed


def install(store) -> dict:
    """Install additive operation triggers on a quiescent schema-1 deployment.

    This is intentionally never called by ``Store`` construction or reads. The
    global maintenance lock and transaction make trigger creation and seeding
    atomic with respect to all supported Author writers.
    """
    _operator_only()
    with file_lock(store.home / "locks/maintenance", blocking=False):
        with store.transaction() as conn:
            _assert_quiescent(conn)
            marker = conn.execute("SELECT value FROM metadata WHERE key=?", (STATE_CLOCK_MARKER,)).fetchone()
            existing = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)", _TRIGGER_NAMES)}
            if marker:
                require(marker[0] == "1", "INCOMPATIBLE_STATE", "Unsupported operation state-clock version")
                require(existing == set(_TRIGGER_NAMES), "CORRUPT_STATE", "Installed operation state-clock triggers are incomplete")
                global_row = conn.execute("SELECT value FROM metadata WHERE key=?", (STATE_CLOCK_GLOBAL,)).fetchone()
                require(global_row is not None, "CORRUPT_STATE", "Global operation state clock is missing")
                return {"installed": True, "already_installed": True,
                        "global_generation": _generation(global_row[0], name="global generation"),
                        "tasks_seeded": 0}

            # Schema-1 state predates triggers. A nonzero baseline marks all
            # prior history as one generation without scanning every operation
            # at request time or deleting/re-writing any historical rows.
            operation_exists = conn.execute("SELECT 1 FROM operations LIMIT 1").fetchone() is not None
            conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)",
                         (STATE_CLOCK_GLOBAL, "1" if operation_exists else "0"))
            task_ids = [row[0] for row in conn.execute(
                "SELECT DISTINCT task_id FROM operations WHERE task_id<>''")]
            conn.executemany("INSERT INTO metadata(key,value) VALUES(?, '1')",
                             ((STATE_CLOCK_PREFIX + task_id,) for task_id in task_ids))
            for statement in STATE_CLOCK_TRIGGERS:
                conn.execute(statement)
            conn.execute("INSERT INTO metadata(key,value) VALUES(?, '1')", (STATE_CLOCK_MARKER,))
            return {"installed": True, "already_installed": False,
                    "global_generation": 1 if operation_exists else 0,
                    "tasks_seeded": len(task_ids)}


def watermark(store, task_id: str | None = None, *, conn=None) -> int:
    """Return one indexed generation, failing closed until explicit upgrade.

    Pass an existing SQLite read transaction when the generation must be
    snapshot-consistent with other records fetched by the caller.
    """
    if task_id is None:
        key = STATE_CLOCK_GLOBAL
    else:
        assert_task_scope(task_id)
        key = STATE_CLOCK_PREFIX + task_id

    def read_from(connection) -> int:
        marker = connection.execute("SELECT value FROM metadata WHERE key=?", (STATE_CLOCK_MARKER,)).fetchone()
        require(marker is not None, "STATE_UPGRADE_REQUIRED",
                "Install operation state clocks with the explicit upgrade command before paging operation history")
        require(marker[0] == "1", "INCOMPATIBLE_STATE", "Unsupported operation state-clock version")
        triggers = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)", _TRIGGER_NAMES)}
        require(triggers == set(_TRIGGER_NAMES), "CORRUPT_STATE", "Installed operation state-clock triggers are incomplete")
        row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None and task_id is not None:
            operation = connection.execute("SELECT 1 FROM operations WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
            require(operation is None, "CORRUPT_STATE", "Task operation state clock is missing", task_id=task_id)
            return 0
        require(row is not None, "CORRUPT_STATE", "Operation state clock is missing", key=key)
        return _generation(row[0], name="generation")

    if conn is not None:
        return read_from(conn)
    with store.connect() as connection:
        return read_from(connection)
