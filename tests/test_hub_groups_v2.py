from __future__ import annotations

from collections.abc import Mapping

from patchbay.hub.groups_v2 import (
    create_successor_group,
    derive_completion_contract,
    derive_group_activity,
    validate_close_dispositions,
)
from patchbay.hub.operations import public_envelope
from patchbay.hub.protocol_v2 import (
    validate_hub_v2_tool_arguments,
    validate_hub_v2_tool_output,
)


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


def test_healthy_pending_operation_states_are_active_not_uncertain() -> None:
    for state in ("created", "payload_ready", "dispatchable", "running"):
        result = derive_group_activity([], [{"state": state}])

        assert result["activity"] == "active"
        assert result["counts"]["active_operations"] == 1
        assert result["counts"]["uncertain_operations"] == 0


def test_preflight_only_group_waits_on_group_status_not_worker_wait() -> None:
    contract = derive_completion_contract(
        {"work_group_id": "group_preflight", "status": "open"},
        [],
        [{"state": "running"}],
    )

    assert contract["reason"] == "operations_active"
    assert contract["recommended_next_action"] == {
        "tool": "patchbay_work_group_status",
        "reason": (
            "A group-level operation such as repository preflight is still active. "
            "Wait for authoritative group state; no worker exists to wait on yet."
        ),
        "arguments": {
            "work_group_id": "group_preflight",
            "include_workers": True,
            "include_operations": True,
            "include_integrations": True,
            "wait_for_change_seconds": 30,
        },
    }


def test_reconciling_operation_is_uncertain_not_ordinary_active_work() -> None:
    result = derive_group_activity([], [{"state": "reconciling"}])

    assert result["activity"] == "recovery_required"
    assert result["counts"]["active_operations"] == 0
    assert result["counts"]["uncertain_operations"] == 1


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


def test_reviewed_failure_is_the_managers_explicit_close_disposition() -> None:
    failed = worker(
        "fworker_failed", turn_state="failed", review_disposition="unreviewed"
    )

    result = validate_close_dispositions(
        [failed], {"fworker_failed": "reviewed_failure"}, outcome="complete"
    )

    assert result["accepted"] is True


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


def test_every_completion_recommendation_is_callable_or_explicit_guidance() -> None:
    open_group = {
        "work_group_id": "group_contract",
        "status": "open",
        "execution_mode": "end_to_end",
        "goal": "Finish the assigned task end to end.",
    }
    scenarios = {
        "recovery": (
            open_group,
            [worker("fworker_recovery", liveness="lost")],
            [{"state": "outcome_unknown"}],
        ),
        "active_worker": (
            open_group,
            [worker("fworker_active", turn_state="working", liveness="active")],
            [],
        ),
        "active_operation": (open_group, [], [{"state": "running"}]),
        "unintegrated": (
            open_group,
            [worker("fworker_writer", integration_state="not_integrated")],
            [],
        ),
        "workers_not_started": (open_group, [], []),
        "ready_for_manager_review": (
            open_group,
            [worker("fworker_done")],
            [],
        ),
        "terminal_group": (
            {**open_group, "status": "complete"},
            [worker("fworker_closed")],
            [],
        ),
    }

    for scenario, (group, workers, operations) in scenarios.items():
        contract = derive_completion_contract(group, workers, operations)
        action = contract["recommended_next_action"]
        emitted_actions = []
        if isinstance(action, Mapping):
            assert set(action) == {"tool", "reason", "arguments"}, scenario
            validate_hub_v2_tool_arguments(action["tool"], action["arguments"])
            if contract["manager_must_continue"]:
                emitted_actions.append(dict(action))
        else:
            assert isinstance(action, str) and action.strip(), scenario

        validate_hub_v2_tool_output(
            "patchbay_work_group_status",
            public_envelope(
                "ok",
                result={"completion_contract": contract},
                next_actions=emitted_actions,
            ),
        )


def test_completion_contract_never_fabricates_required_manager_inputs() -> None:
    group = {
        "work_group_id": "group_manager_inputs",
        "status": "open",
        "execution_mode": "end_to_end",
        "goal": "Complete the task.",
    }

    no_workers = derive_completion_contract(group, [], [])["recommended_next_action"]
    ready_to_close = derive_completion_contract(
        group,
        [worker("fworker_done")],
        [],
    )["recommended_next_action"]

    assert isinstance(no_workers, str)
    assert "shared brief" in no_workers
    assert "idempotency" in no_workers
    assert isinstance(ready_to_close, str)
    assert "truthful outcome" in ready_to_close
    assert "disposition every worker" in ready_to_close


def test_unintegrated_recommendation_uses_an_exact_worker_selector() -> None:
    group = {
        "work_group_id": "group_unintegrated",
        "status": "open",
        "execution_mode": "end_to_end",
        "goal": "Integrate accepted work.",
    }

    action = derive_completion_contract(
        group,
        [worker("fworker_exact", integration_state="not_integrated")],
        [],
    )["recommended_next_action"]

    assert isinstance(action, Mapping)
    assert action["tool"] == "patchbay_worker_inspect"
    assert action["arguments"] == {
        "work_group_id": "group_unintegrated",
        "fleet_worker_ref": "fworker_exact",
        "view": "integration_preview",
    }
    validate_hub_v2_tool_arguments(action["tool"], action["arguments"])
