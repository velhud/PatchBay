"""Explicit schema-1 admission indexes; no authority or cached absence."""
from .locks import file_lock
from .schema import ADMISSION_INDEXES
from .state_clock import _operator_only, _assert_quiescent


def install(store):
    _operator_only()
    with file_lock(store.home / "locks/maintenance", blocking=False):
        with store.transaction() as conn:
            _assert_quiescent(conn)
            for statement in ADMISSION_INDEXES:
                conn.execute(statement)
    return {"installed": True, "indexes": ["runs_queue_window", "runs_profile_receipts"]}
