from __future__ import annotations

from patchbay.hub.groups_v2 import create_successor_group, derive_group_activity, validate_close_dispositions


def worker(ref: str, **overrides):
    value = {
        "fleet_worker_ref": ref,
        "turn_state": "completed",
        "liveness": "terminal",
        "integration_state": "no_changes",
        "review_disposition": "not_required",
    }
    value.update(overrides)
    return value


def test_group_activity_comes_from_workers_and_operations() -> None:
    result = derive_group_activity(
        [worker("fworker_1", turn_state="working", liveness="active")],
        [{"state": "running"}],
    )
    assert result["activity"] == "active"
    assert result["counts"]["active"] == 1


def test_lost_worker_or_unknown_operation_requires_recovery() -> None:
    result = derive_group_activity(
        [worker("fworker_1", liveness="lost")],
        [{"state": "outcome_unknown"}],
    )
    assert result["activity"] == "recovery_required"


def test_close_requires_disposition_for_every_worker() -> None:
    result = validate_close_dispositions([worker("fworker_1")], {}, outcome="complete")
    assert result["accepted"] is False
    assert result["missing_dispositions"] == ["fworker_1"]


def test_active_worker_can_only_close_with_leave_running() -> None:
    active = worker("fworker_1", turn_state="working", liveness="active")
    blocked = validate_close_dispositions([active], {"fworker_1": "no_changes"}, outcome="abandoned")
    allowed = validate_close_dispositions([active], {"fworker_1": "leave_running"}, outcome="abandoned")
    assert blocked["accepted"] is False
    assert allowed["accepted"] is True


def test_unintegrated_changes_cannot_be_called_no_changes() -> None:
    changed = worker("fworker_1", integration_state="not_integrated")
    result = validate_close_dispositions([changed], {"fworker_1": "no_changes"}, outcome="complete")
    assert result["accepted"] is False


def test_successor_does_not_mutate_predecessor_pin_or_workers() -> None:
    predecessor = {
        "work_group_id": "group_old",
        "title": "Task",
        "goal": "Finish",
        "workspace_ref": "workspace_repo",
        "pinned_machine_id": "machine_old",
        "pinned_edge_generation": "edgegen_old",
        "worker_refs": ["fworker_old"],
    }
    successor = create_successor_group(
        predecessor,
        machine_id="machine_new",
        edge_generation="edgegen_new",
        reason="old machine offline",
        now=1,
    )
    assert predecessor["pinned_machine_id"] == "machine_old"
    assert successor["pinned_machine_id"] == "machine_new"
    assert successor["supersedes"] == "group_old"
    assert successor["worker_refs"] == []
    assert successor["predecessor_snapshot"]["worker_refs"] == ["fworker_old"]
