from __future__ import annotations

import pytest

from patchbay.hub.live_v2 import _wait_until, run_live_hub_v2_eval_sync
from patchbay.hub.tool_surface import HUB_V2_TOOL_NAMES


@pytest.mark.asyncio
async def test_wait_timeout_names_phase_and_reports_observed_state():
    with pytest.raises(TimeoutError) as captured:
        await _wait_until(
            lambda: False,
            phase="edge_result_projection",
            timeout_seconds=0.01,
            diagnostics=lambda: {
                "pending_receipts": 2,
                "worker_states": ["working"],
            },
        )

    message = str(captured.value)
    assert "edge_result_projection" in message
    assert "0.0s" in message
    assert "pending_receipts" in message
    assert "worker_states" in message


def test_live_hub_v2_eval_report(tmp_path):
    report = run_live_hub_v2_eval_sync(tmp_path)

    assert report["status"] == "passed", report
    assert report["tool_count"] == 31
    assert tuple(report["tool_names"]) == HUB_V2_TOOL_NAMES
    assert report["result_posts_are_edge_generated"] is True
    assert report["workers"] == {
        "count": 5,
        "names": ["Reader", "Writer", "Fairness 1", "Fairness 2", "Fairness 3"],
        "same_worker_turns": 4,
    }
    assert report["integration"] == {
        "base_changed": True,
        "commit_created": False,
        "worker_worktree_preserved": True,
        "changed_file": "live-v2-worker.txt",
    }
    assert report["failure_scenarios"]["machine_pin"] == "passed"
    assert report["failure_scenarios"]["lost_result_response"] == "passed"
    assert report["failure_scenarios"]["poisoned_receipt_fairness"] == "passed"
    assert report["failure_scenarios"]["pending_receipts_before_restart"] == 1
    assert report["failure_scenarios"]["new_effects_after_restart"] == 0
    assert report["restart"]["hub_history_restored"] is True
    assert report["restart"]["edge_history_restored"] is True
    assert report["restart"]["same_worker_session_preserved"] is True
    assert report["restart"]["same_worker_workspace_preserved"] is True
    assert report["restart"]["authorized_followup_effects"] == 1

    checks = {check["name"]: check for check in report["checks"]}
    assert checks and all(check["passed"] for check in checks.values())
    assert set(checks) >= {
        "server_started",
        "mcp_initialize",
        "mcp_real_tcp_boundary",
        "exact_31_tools",
        "two_real_edges_online",
        "two_workspace_projections",
        "startup_fallbacks_are_identifier_rich",
        "real_preflight_ready",
        "end_to_end_contract_blocks_premature_final",
        "concurrent_manager_groups_do_not_cross_contaminate",
        "second_manager_group_closes_independently",
        "group_machine_pin_enforced",
        "two_workers_completed_via_projection",
        "batch_parent_is_truthful_aggregate_work",
        "completed_workers_still_require_integration_and_closure",
        "worker_inspection",
        "same_worker_continuation",
        "poisoned_receipt_does_not_starve_newer_results",
        "poisoned_receipt_recovers_without_duplicate_execution",
        "isolated_worktree_write",
        "opaque_integration_preview_token",
        "stale_preview_returns_replacement_token",
        "integration_changes_base_without_commit",
        "lost_result_response_is_durable",
        "group_closed_after_receipt_recovery",
        "lost_result_reconciled_after_restart",
        "same_worker_continues_after_hub_edge_restart",
        "hub_and_edge_history_survive_restart",
    }
