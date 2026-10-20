from __future__ import annotations

import time
import uuid

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.service import AuthorService
from patchbay.authoring.persistence.control import Control


def insert_run(service, task, state: str, *, operation_state: str | None = None):
    operation_id = "op_" + uuid.uuid4().hex
    now = time.time()
    record = service.tasks.get(task["id"])
    git = service.repositories.git(record["repository_id"])
    operation_state = operation_state or state
    with service.store.transaction() as conn:
        conn.execute(
            "INSERT INTO operations(id,task_id,kind,idempotency_key,fingerprint,revision,state,phase,payload,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, task["id"], "checks.run", "control-fixture-" + operation_id,
             "fixture", 0, operation_state, state, "{}", now, now),
        )
        conn.execute(
            "INSERT INTO runs(id,task_id,revision,commit_oid,tree_oid,profile,profile_digest,state,cleanup,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, task["id"], 0, record["current_commit"], git.tree(record["current_commit"]),
             "{}", "fixture", state, "pending" if state in {"launching", "running", "cancelling"} else "not_required",
             now, now),
        )
    return operation_id


def insert_operation(service, task, state: str, phase: str):
    operation_id = "op_" + uuid.uuid4().hex
    now = time.time()
    with service.store.transaction() as conn:
        conn.execute(
            "INSERT INTO operations(id,task_id,kind,idempotency_key,fingerprint,revision,state,phase,payload,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, task["id"], "controller.fixture", "control-fixture-" + operation_id,
             "fixture", None, state, phase, "{}", now, now),
        )
    return operation_id


def close_fixture_run(service, operation_id: str):
    now = time.time()
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='cancelled',cleanup='proven',updated=? WHERE id=?", (now, operation_id))
        conn.execute("UPDATE operations SET state='cancelled',phase='terminal',updated=? WHERE id=?", (now, operation_id))


def test_pause_resume_is_durable_idempotent_and_gates_only_admission(setup_author):
    service, _, _, task, _ = setup_author
    control = Control(service.store)
    control.assert_admission(task["id"])

    result = control.pause(key="pause-once", actor="operator")
    assert result == {"paused": True}
    assert control.pause(key="pause-once", actor="operator") == result
    with pytest.raises(AuthorError) as conflict:
        control.pause(key="pause-once", actor="different-operator")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(AuthorError) as paused:
        control.assert_admission(task["id"])
    assert paused.value.code == "CONTROL_PAUSED"

    # Read/export remains available while admission is paused.
    assert service.tasks.get(task["id"])["id"] == task["id"]
    assert service.snapshots.export(task["id"])["task_id"] == task["id"]

    reopened = AuthorService(service.store.home)
    recovered = Control(reopened.store)
    assert recovered.status()["paused"] is True
    assert recovered.pause(key="pause-once", actor="operator") == result
    resumed = recovered.resume(key="resume-once", actor="operator")
    assert resumed == {"paused": False}
    assert recovered.resume(key="resume-once", actor="operator") == resumed
    recovered.assert_admission(task["id"])


def test_pause_status_reports_drain_and_wait_states_without_signalling_runs(setup_author):
    service, _, _, task, _ = setup_author
    active = insert_run(service, task, "running")
    queued = insert_run(service, task, "queued")
    awaiting = insert_operation(service, task, "awaiting_approval", "awaiting_approval")
    decision = insert_operation(service, task, "queued", "pending_decision")
    uncertain = insert_operation(service, task, "unknown_execution", "ownership_unresolved")
    control = Control(service.store)
    paused = control.pause(key="pause-with-work", actor="operator")
    status = control.status()
    assert status["paused"] is True
    assert status["drain_state"] == "draining"
    assert "awaiting_controller" in status["states"]
    assert "draining" in status["states"]
    assert "awaiting_approval" in status["states"]
    assert "pending_decision" in status["states"]
    assert status["counts"]["uncertain"]["count"] == 1
    assert status["counts"]["pending_decision"]["count"] == 1
    assert status["counts"]["active"]["count"] == 1
    assert status["counts"]["awaiting_approval"]["count"] == 1
    # Active and uncertain effects remain fenced and untouched by pause.
    assert service.store.one("SELECT state FROM runs WHERE id=?", (active,))["state"] == "running"
    assert service.store.operation(uncertain)["state"] == "unknown_execution"
    assert service.store.one("SELECT state FROM operations WHERE id=?", (queued,))["state"] == "queued"
    assert service.store.operation(awaiting)["state"] == "awaiting_approval"
    assert service.store.operation(decision)["phase"] == "pending_decision"
    assert control.pause(key="pause-with-work", actor="operator") == paused
    close_fixture_run(service, active)


def test_revoke_atomically_blocks_queued_checks_but_preserves_active_effects(setup_author):
    service, _, _, task, _ = setup_author
    queued = insert_run(service, task, "queued")
    active = insert_run(service, task, "running")
    control = Control(service.store)

    result = control.revoke(task["id"], key="revoke-task", actor="operator")
    assert result["queued_checks_blocked"] == 1
    assert result["active_effects_preserved"] == 1
    blocked_operation = service.store.operation(queued)
    assert blocked_operation["state"] == "blocked"
    assert blocked_operation["error"]["code"] == "TASK_REVOKED"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (queued,))["state"] == "blocked"
    assert service.store.one("SELECT state FROM runs WHERE id=?", (active,))["state"] == "running"
    assert service.store.operation(active)["state"] == "running"

    with pytest.raises(AuthorError) as revoked:
        control.assert_granted(task["id"])
    assert revoked.value.code == "TASK_REVOKED"
    with pytest.raises(AuthorError) as admission:
        control.assert_admission(task["id"])
    assert admission.value.code == "TASK_REVOKED"

    reopened = Control(AuthorService(service.store.home).store)
    assert reopened.revoke(task["id"], key="revoke-task", actor="operator") == result
    reopened.restore_grant(task["id"], key="restore-task", actor="operator")
    reopened.assert_granted(task["id"])
    # Restoration does not revive a check that revocation blocked.
    assert reopened.store.operation(queued)["state"] == "blocked"
    close_fixture_run(service, active)


def test_control_keys_are_cross_action_conflict_checked_and_restore_does_not_resume(setup_author):
    service, _, _, task, _ = setup_author
    control = Control(service.store)
    control.pause(key="shared", actor="operator")
    with pytest.raises(AuthorError) as conflict:
        control.resume(key="shared", actor="operator")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

    control.revoke(task["id"], key="revoke", actor="operator")
    control.restore_grant(task["id"], key="restore", actor="operator")
    assert control.status()["paused"] is True
    control.resume(key="resume", actor="operator")
    control.assert_admission(task["id"])
