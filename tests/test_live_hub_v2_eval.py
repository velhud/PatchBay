from __future__ import annotations

from patchbay.hub.live_v2 import run_live_hub_v2_eval_sync
from patchbay.hub.tool_surface import HUB_V2_TOOL_NAMES


def test_live_hub_v2_eval_report(tmp_path):
    report = run_live_hub_v2_eval_sync(tmp_path)

    assert report["status"] == "passed", report
    assert report["tool_count"] == 31
    assert tuple(report["tool_names"]) == HUB_V2_TOOL_NAMES
    assert report["result_posts_are_edge_generated"] is True
    assert report["workers"] == {
        "count": 2,
        "names": ["Reader", "Writer"],
        "same_worker_turns": 3,
    }
    assert report["integration"] == {
        "base_changed": True,
        "commit_created": False,
        "worker_worktree_preserved": True,
        "changed_file": "live-v2-worker.txt",
    }
    assert report["failure_scenarios"]["machine_pin"] == "passed"
    assert report["failure_scenarios"]["lost_result_response"] == "passed"
    assert report["failure_scenarios"]["pending_receipts_before_restart"] == 1
    assert report["failure_scenarios"]["new_effects_after_restart"] == 0
    assert report["restart"]["hub_history_restored"] is True
    assert report["restart"]["edge_history_restored"] is True

    checks = {check["name"]: check for check in report["checks"]}
    assert checks and all(check["passed"] for check in checks.values())
    assert set(checks) >= {
        "server_started",
        "mcp_initialize",
        "exact_31_tools",
        "two_real_edges_online",
        "two_workspace_projections",
        "real_preflight_ready",
        "group_machine_pin_enforced",
        "two_workers_completed_via_projection",
        "worker_inspection",
        "same_worker_continuation",
        "isolated_worktree_write",
        "opaque_integration_preview_token",
        "integration_changes_base_without_commit",
        "lost_result_response_is_durable",
        "group_closed_after_receipt_recovery",
        "lost_result_reconciled_after_restart",
        "hub_and_edge_history_survive_restart",
    }
