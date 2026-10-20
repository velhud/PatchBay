"""Atomic operation watermarks and explicit schema-1 clock installation."""
from __future__ import annotations

import time

import pytest

from patchbay.authoring.authority import AuthIdentity, identity_context
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.persistence.schema import (STATE_CLOCK_GLOBAL, STATE_CLOCK_MARKER,
                                                  STATE_CLOCK_PREFIX)
from patchbay.authoring.persistence.state_clock import install, watermark
from patchbay.authoring.persistence.store import Store


def _insert_operation(store, operation_id="op_clock_1", task_id="task_clock_a", *, state="queued"):
    now = time.time()
    with store.transaction() as conn:
        conn.execute("""INSERT INTO operations
            (id,task_id,kind,idempotency_key,fingerprint,state,phase,payload,created,updated)
            VALUES(?,?,'clock.test',?,'fingerprint',?,'accepted','{}',?,?)""",
                     (operation_id, task_id, operation_id, state, now, now))


def _drop_clocks(store):
    with store.transaction() as conn:
        for name in ("operations_state_clock_insert", "operations_state_clock_update", "operations_state_clock_delete"):
            conn.execute("DROP TRIGGER IF EXISTS " + name)
        conn.execute("DELETE FROM metadata WHERE key=? OR key=? OR key LIKE ?",
                     (STATE_CLOCK_MARKER, STATE_CLOCK_GLOBAL, STATE_CLOCK_PREFIX + "%"))


def _insert_running_run(store, task_id="task_clock_active"):
    now = time.time()
    with store.transaction() as conn:
        conn.execute("INSERT INTO repositories(id,name,source,default_branch,object_format,policy,created,updated) "
                     "VALUES(?,'clock-repo','/fixture','main','sha1','{}',?,?)",
                     ("repo_clock_active", now, now))
        conn.execute("""INSERT INTO tasks
            (id,repository_id,goal,origin_commit,current_commit,current_revision,policy,policy_digest,status,created,updated)
            VALUES(?,?,'fixture','origin','origin',0,'{}','policy','active',?,?)""",
                     (task_id, "repo_clock_active", now, now))
        conn.execute("""INSERT INTO operations
            (id,task_id,kind,idempotency_key,fingerprint,state,phase,payload,created,updated)
            VALUES('op_clock_active',?,'checks.run','active','fingerprint','running','verifying','{}',?,?)""",
                     (task_id, now, now))
        conn.execute("""INSERT INTO runs
            (id,task_id,revision,commit_oid,tree_oid,profile,profile_digest,state,cleanup,created,updated)
            VALUES('op_clock_active',?,0,'origin','tree','{}','profile','running','proven',?,?)""",
                     (task_id, now, now))


def test_fresh_schema_triggers_count_insert_update_delete_and_scope_moves(tmp_path):
    store = Store.initialize(tmp_path / "fresh-state")
    assert store.identity["schema_version"] == 1
    assert watermark(store) == 0
    assert watermark(store, "task_clock_a") == 0

    _insert_operation(store)
    assert watermark(store) == 1
    assert watermark(store, "task_clock_a") == 1

    # Direct lifecycle changes count even when a client leaves updated unchanged.
    with store.transaction() as conn:
        conn.execute("UPDATE operations SET phase='verifying' WHERE id='op_clock_1'")
    assert watermark(store) == 2
    assert watermark(store, "task_clock_a") == 2

    # Scope changes increment both old and new task generations exactly once.
    with store.transaction() as conn:
        conn.execute("UPDATE operations SET task_id='task_clock_b' WHERE id='op_clock_1'")
    assert watermark(store) == 3
    assert watermark(store, "task_clock_a") == 3
    assert watermark(store, "task_clock_b") == 1

    with store.transaction() as conn:
        conn.execute("DELETE FROM operations WHERE id='op_clock_1'")
    assert watermark(store) == 4
    assert watermark(store, "task_clock_b") == 2

    installed = store.rows("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'operations_state_clock_%'")
    assert {row["name"] for row in installed} == {
        "operations_state_clock_insert", "operations_state_clock_update", "operations_state_clock_delete"}


def test_watermark_can_share_the_callers_read_snapshot(tmp_path):
    store = Store.initialize(tmp_path / "snapshot-clock")
    _insert_operation(store)
    with store.connect() as reader:
        reader.execute("BEGIN")
        initial = watermark(store, "task_clock_a", conn=reader)
        with store.transaction() as writer:
            writer.execute("UPDATE operations SET phase='verifying' WHERE id='op_clock_1'")
        assert watermark(store, "task_clock_a", conn=reader) == initial
        reader.commit()
    assert watermark(store, "task_clock_a") == initial + 1


def test_clockless_schema_requires_explicit_operator_install_and_seeds_history(tmp_path):
    store = Store.initialize(tmp_path / "old-schema-one")
    _insert_operation(store)
    _insert_operation(store, "op_clock_2", "task_clock_b", state="completed")
    _drop_clocks(store)
    original_operations = store.rows("SELECT id,task_id,state FROM operations ORDER BY id")
    original_event_count = store.one("SELECT COUNT(*) AS n FROM events")["n"]

    # Opening and reading old state is read-only and never installs triggers.
    reopened = Store(store.home)
    with pytest.raises(AuthorError) as missing_clock:
        watermark(reopened, "task_clock_a")
    assert missing_clock.value.code == "STATE_UPGRADE_REQUIRED"
    assert not reopened.rows("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'operations_state_clock_%'")
    assert reopened.one("SELECT value FROM metadata WHERE key=?", (STATE_CLOCK_MARKER,), required=False) is None
    assert reopened.one("SELECT COUNT(*) AS n FROM events")["n"] == original_event_count

    reader = AuthIdentity(principal_id="reader-1", role="reader")
    with identity_context(reader), pytest.raises(AuthorError) as forbidden:
        install(reopened)
    assert forbidden.value.code == "FORBIDDEN"
    assert not reopened.rows("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'operations_state_clock_%'")
    scoped_operator = AuthIdentity(principal_id="task-operator", role="operator", task_ids=("task_clock_a",))
    with identity_context(scoped_operator), pytest.raises(AuthorError) as scoped_forbidden:
        install(reopened)
    assert scoped_forbidden.value.code == "FORBIDDEN"

    installed = install(reopened)
    assert installed == {"installed": True, "already_installed": False,
                         "global_generation": 1, "tasks_seeded": 2}
    assert watermark(reopened) == 1
    assert watermark(reopened, "task_clock_a") == 1
    assert watermark(reopened, "task_clock_b") == 1
    assert reopened.rows("SELECT id,task_id,state FROM operations ORDER BY id") == original_operations
    assert reopened.one("SELECT COUNT(*) AS n FROM events")["n"] == original_event_count

    replay = install(reopened)
    assert replay == {"installed": True, "already_installed": True,
                      "global_generation": 1, "tasks_seeded": 0}
    with reopened.transaction() as conn:
        conn.execute("UPDATE operations SET result='null' WHERE id='op_clock_2'")
    assert watermark(reopened) == 2
    assert watermark(reopened, "task_clock_b") == 2


def test_clock_installation_fails_closed_when_any_run_is_active_or_uncertain(tmp_path):
    store = Store.initialize(tmp_path / "active-old-state")
    _insert_running_run(store)
    _drop_clocks(store)

    with pytest.raises(AuthorError) as error:
        install(store)
    assert error.value.code == "MAINTENANCE_BLOCKED"
    assert store.one("SELECT value FROM metadata WHERE key=?", (STATE_CLOCK_MARKER,), required=False) is None
    assert not store.rows("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'operations_state_clock_%'")
    assert store.one("SELECT state FROM runs WHERE id='op_clock_active'")["state"] == "running"
