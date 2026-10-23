from __future__ import annotations

import asyncio

import pytest

from patchbay.hub import live_v2
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

    # Keep the actual failure visible even when pytest abbreviates a long
    # successful-check history in the surrounding report.
    assert report["status"] == "passed", {
        "error": report.get("error"),
        "failed_checks": [check for check in report["checks"] if not check["passed"]],
    }
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


def test_live_hub_v2_fairness_waits_for_local_outbox_ack(tmp_path, monkeypatch):
    original_post_json = live_v2._FakeNetworkTransport.post_json

    async def delay_poison_phase_responses(
        self, path, payload, *, token="", timeout_seconds=None
    ):
        response = await original_post_json(
            self, path, payload, token=token, timeout_seconds=timeout_seconds
        )
        if self.poison_receipt_id:
            # Hub state can be visible to projection readers before the Edge
            # resumes from this response and records its local outbox ACK.
            await asyncio.sleep(0.6)
        return response

    monkeypatch.setattr(
        live_v2._FakeNetworkTransport,
        "post_json",
        delay_poison_phase_responses,
    )
    report = run_live_hub_v2_eval_sync(tmp_path)

    assert report["status"] == "passed", report
    fairness = next(
        check
        for check in report["checks"]
        if check["name"] == "poisoned_receipt_does_not_starve_newer_results"
    )
    assert fairness["passed"] is True
    assert fairness["details"]["pending_receipts"] == 1
    assert fairness["details"]["later_workers_completed"] == 2


@pytest.mark.asyncio
async def test_deterministic_executor_releases_process_free_terminal_cleanup(tmp_path):
    repo = live_v2._create_repo(tmp_path / "repo")
    config = live_v2._edge_config(tmp_path, repo, "executor-state")
    manager = live_v2.JobManager(config)
    job_id = manager.create_job("plan", "Complete deterministic fixture", str(repo))
    executor = live_v2._DeterministicCodexExecutor(config, manager)
    manager.update_job_state(job_id, live_v2.JobState.RUNNING)

    prior_task = asyncio.create_task(asyncio.Event().wait())
    executor.tasks[job_id] = prior_task
    executor._refresh_runtime_liveness_cache()
    assert executor.runtime_liveness_snapshot(job_id)["executor_task_alive"] is True

    executor.tasks.pop(job_id)
    prior_task.cancel()
    await asyncio.gather(prior_task, return_exceptions=True)
    completed_task = executor.schedule_job(job_id)
    await completed_task
    await asyncio.sleep(0)

    job = manager.get_job(job_id)
    assert job is not None
    assert job.state == live_v2.JobState.COMPLETED
    assert job.wrapper_cleanup_outcome == "process_not_live_after_terminal"
    snapshot = executor.runtime_liveness_snapshot(job_id)
    assert snapshot["executor_task_alive"] is False
    assert snapshot["process_alive"] is False
    assert snapshot["runtime_alive"] is False


@pytest.mark.asyncio
async def test_deterministic_executor_preserves_existing_cleanup_fence(tmp_path):
    repo = live_v2._create_repo(tmp_path / "repo")
    config = live_v2._edge_config(tmp_path, repo, "executor-state")
    manager = live_v2.JobManager(config)
    job_id = manager.create_job("plan", "Keep unresolved cleanup fenced", str(repo))
    manager.update_job_state(
        job_id,
        live_v2.JobState.PENDING,
        wrapper_cleanup_outcome="cleanup_unproven",
    )
    executor = live_v2._DeterministicCodexExecutor(config, manager)
    lease = await executor.repo_locks.acquire(
        str(repo), operation="fixture_cleanup_fence"
    )
    executor.repo_locks.bind_to_job(job_id, lease)

    try:
        completed_task = executor.schedule_job(job_id)
        await completed_task
        await asyncio.sleep(0)

        job = manager.get_job(job_id)
        assert job is not None
        assert job.state == live_v2.JobState.COMPLETED
        assert job.wrapper_cleanup_outcome == "cleanup_unproven"
        assert job_id not in executor._terminal_cleanup_completed
        assert lease.released is False
    finally:
        executor.repo_locks.release_job(job_id)
