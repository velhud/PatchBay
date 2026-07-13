import asyncio
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.workers.runtime import (
    WORKER_BASE_REPO_OPTION,
    WORKER_ID_OPTION,
    WORKER_LANE_ID_OPTION,
    WORKER_MODE_OPTION,
    WORKER_NAME_OPTION,
    WORKER_WORKTREE_OPTION,
    WORKER_WORK_GROUP_ID_OPTION,
    WorkerRuntime,
)


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# worker projection\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Worker Test",
            "-c",
            "user.email=worker-test@example.invalid",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return {
        "server": {
            "max_concurrent_jobs": 10,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
            "stale_running_job_grace_seconds": 0,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "workers": {
            "worktree_root": str(tmp_path / "worker-worktrees"),
            "heartbeat_fresh_seconds": 10,
            "heartbeat_quiet_seconds": 300,
        },
        "locks": {"root": str(tmp_path / "locks")},
    }


class ProjectionExecutor:
    def __init__(self):
        self.live_job_ids = set()
        self.reconcile_calls = 0

    def _job_has_live_runtime(self, job_id):
        return job_id in self.live_job_ids

    def reconcile_stale_running_jobs(self):
        self.reconcile_calls += 1
        return {"checked": 0, "reconciled": 0, "recovered_completed": 0}

    async def reconcile_stale_running_jobs_async(self):
        return await asyncio.to_thread(self.reconcile_stale_running_jobs)


def add_worker(
    manager,
    executor,
    *,
    worker_id,
    name,
    state,
    heartbeat_age=None,
    live=False,
    workspace_mode="read_only",
    result=None,
    error=None,
):
    repo = manager.config["repositories"]["default"]
    options = {
        WORKER_ID_OPTION: worker_id,
        WORKER_NAME_OPTION: name,
        WORKER_MODE_OPTION: workspace_mode,
        WORKER_BASE_REPO_OPTION: repo,
        WORKER_WORK_GROUP_ID_OPTION: "grp-projection",
        WORKER_LANE_ID_OPTION: f"lane-{name.casefold().replace(' ', '-')}",
    }
    if workspace_mode == "isolated_write":
        worktree = Path(manager.config["workers"]["worktree_root"]) / worker_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", f"projection/{worker_id}", str(worktree)],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        options[WORKER_WORKTREE_OPTION] = str(worktree)
    job_id = manager.create_job("interactive", "Projection fixture", repo, options)
    now = time.time()
    if state == JobState.RUNNING:
        manager.update_job_state(
            job_id,
            state,
            started_at=now - 120,
            launch_started_at=now - 120,
            process_started_at=now - 119,
            session_id=f"session-{worker_id}",
            last_heartbeat_at=now - float(heartbeat_age or 0),
            last_event="turn.running",
        )
    else:
        manager.update_job_state(
            job_id,
            state,
            started_at=now - 120,
            completed_at=now - 30,
            session_id=f"session-{worker_id}",
            result=result,
            error=error,
        )
    if live:
        executor.live_job_ids.add(job_id)
    return job_id


@pytest.mark.parametrize(
    ("label", "job_state", "heartbeat_age", "live", "worker_state", "turn_state", "liveness"),
    [
        ("active", JobState.RUNNING, 1, True, "available", "working", "active"),
        ("quiet", JobState.RUNNING, 60, True, "available", "working", "quiet"),
        ("completed", JobState.COMPLETED, None, False, "available", "completed", "terminal"),
        ("failed", JobState.FAILED, None, False, "available", "failed", "terminal"),
        ("stopped", JobState.CANCELLED, None, False, "stopped", "cancelled", "terminal"),
        ("lost", JobState.RUNNING, 60, False, "available", "working", "lost"),
    ],
)
def test_projection_snapshot_separates_worker_turn_and_liveness_axes(
    tmp_path,
    label,
    job_state,
    heartbeat_age,
    live,
    worker_state,
    turn_state,
    liveness,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = ProjectionExecutor()
    runtime = WorkerRuntime(config, manager, executor)
    result = {"summary": f"{label} report"} if job_state == JobState.COMPLETED else None
    error = "projection failure" if job_state == JobState.FAILED else None
    add_worker(
        manager,
        executor,
        worker_id=f"wrk-{label}",
        name=label.title(),
        state=job_state,
        heartbeat_age=heartbeat_age,
        live=live,
        result=result,
        error=error,
    )

    worker = runtime.projection_snapshot()["workers"][0]

    assert executor.reconcile_calls == 1
    assert worker["edge_worker_id"] == f"wrk-{label}"
    assert worker["worker_state"] == worker_state
    assert worker["turn_state"] == turn_state
    assert worker["liveness"] == liveness
    assert worker["integration_state"] == "not_applicable"
    assert worker["review_disposition"] == ("not_required" if label == "completed" else "unreviewed")
    assert worker["work_group_id"] == "grp-projection"
    assert worker["lane_id"] == f"lane-{label}"


def test_projection_snapshot_is_full_history_after_restart_and_emits_tombstones(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = ProjectionExecutor()
    add_worker(
        manager,
        executor,
        worker_id="wrk-complete",
        name="Complete",
        state=JobState.COMPLETED,
        result={"summary": "Durable completed report"},
    )
    add_worker(
        manager,
        executor,
        worker_id="wrk-stopped",
        name="Stopped",
        state=JobState.CANCELLED,
    )

    restarted_manager = JobManager(config)
    restarted = WorkerRuntime(config, restarted_manager, ProjectionExecutor())
    snapshot = restarted.projection_snapshot(
        previous_edge_worker_ids=["wrk-stopped", "wrk-removed", "wrk-complete"]
    )

    assert snapshot["snapshot_kind"] == "full"
    assert snapshot["full_history"] is True
    assert snapshot["complete_worker_set"] is True
    assert snapshot["omission_means_tombstone"] is True
    assert snapshot["present_edge_worker_ids"] == ["wrk-complete", "wrk-stopped"]
    assert snapshot["tombstones"] == [{"edge_worker_id": "wrk-removed"}]
    assert [worker["edge_worker_id"] for worker in snapshot["workers"]] == ["wrk-complete", "wrk-stopped"]
    assert snapshot["workers"][0]["report_summary"] == "Durable completed report"


def test_projection_snapshot_reconciles_untracked_durable_running_job(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    repo = config["repositories"]["default"]
    job_id = manager.create_job(
        "interactive",
        "Interrupted durable worker",
        repo,
        {
            WORKER_ID_OPTION: "wrk-interrupted",
            WORKER_NAME_OPTION: "Interrupted",
            WORKER_MODE_OPTION: "read_only",
            WORKER_BASE_REPO_OPTION: repo,
            WORKER_WORK_GROUP_ID_OPTION: "grp-projection",
            WORKER_LANE_ID_OPTION: "recovery",
        },
    )
    old = time.time() - 120
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=old,
        launch_started_at=old,
        process_started_at=old,
        last_heartbeat_at=old,
        last_event="turn.running",
    )
    runtime = WorkerRuntime(config, manager, JobExecutor(config, manager))

    worker = runtime.projection_snapshot()["workers"][0]

    assert worker["turn_state"] == "failed"
    assert worker["liveness"] == "terminal"


@pytest.mark.asyncio
async def test_projection_snapshot_async_schedules_terminal_cleanup_on_owner_loop(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)
    job_id = add_worker(
        manager,
        executor,
        worker_id="wrk-cleanup-pending",
        name="Cleanup Pending",
        state=JobState.COMPLETED,
        result={"summary": "Semantic work completed."},
    )
    manager.update_job_state(
        job_id,
        JobState.COMPLETED,
        wrapper_cleanup_outcome="cleanup_pending",
    )

    class TrackedProcess:
        returncode = None

    process = TrackedProcess()
    executor.processes[job_id] = process
    monkeypatch.setattr(executor, "_terminal_cleanup_has_active_owner", lambda _: False)

    owner_loop = asyncio.get_running_loop()
    cleanup_scheduled = asyncio.Event()
    cleanup_loop: list[asyncio.AbstractEventLoop] = []

    async def record_cleanup(cleanup_job_id, cleanup_process):
        assert cleanup_job_id == job_id
        assert cleanup_process is process
        cleanup_loop.append(asyncio.get_running_loop())
        cleanup_scheduled.set()

    monkeypatch.setattr(executor, "_terminal_cleanup_loop", record_cleanup)
    reconcile_started = threading.Event()
    release_reconcile = threading.Event()
    reconcile_calls = 0
    reconcile = executor.reconcile_stale_running_jobs

    def blocking_reconcile(*, grace_seconds=None, now=None, event_loop=None):
        nonlocal reconcile_calls
        reconcile_calls += 1
        assert event_loop is owner_loop
        reconcile_started.set()
        assert release_reconcile.wait(timeout=1)
        return reconcile(
            grace_seconds=grace_seconds,
            now=now,
            event_loop=event_loop,
        )

    monkeypatch.setattr(executor, "reconcile_stale_running_jobs", blocking_reconcile)
    projection_task = asyncio.create_task(runtime.projection_snapshot_async())

    assert await asyncio.to_thread(reconcile_started.wait, 0.5)
    await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
    assert not projection_task.done()
    release_reconcile.set()

    snapshot = await asyncio.wait_for(projection_task, timeout=1)
    await asyncio.wait_for(cleanup_scheduled.wait(), timeout=1)

    assert snapshot["workers"][0]["cleanup_pending"] is True
    assert reconcile_calls == 1
    assert cleanup_loop == [owner_loop]


def test_projection_snapshot_reports_changes_and_deterministic_content_revisions(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = ProjectionExecutor()
    runtime = WorkerRuntime(config, manager, executor)
    job_id = add_worker(
        manager,
        executor,
        worker_id="wrk-change",
        name="Change",
        state=JobState.COMPLETED,
        workspace_mode="isolated_write",
        result={"summary": "Created the projection evidence."},
    )
    worktree = Path(manager.jobs[job_id].worktree_path)
    (worktree / "projection.txt").write_text("projection evidence\n", encoding="utf-8")
    changed_files = runtime._changed_files
    change_scans = 0

    def counted_changed_files(jobs):
        nonlocal change_scans
        change_scans += 1
        return changed_files(jobs)

    runtime._changed_files = counted_changed_files

    first = runtime.projection_snapshot()
    second = runtime.projection_snapshot()
    (worktree / "post-snapshot.txt").write_text("new evidence\n", encoding="utf-8")
    mutated = runtime.projection_snapshot()
    worker = first["workers"][0]

    assert first["content_revision"] == f"sha256:{first['content_sha256']}"
    assert first["content_revision"] == second["content_revision"]
    assert first["content_sha256"] == second["content_sha256"]
    assert change_scans == 3
    assert mutated["content_revision"] != second["content_revision"]
    assert mutated["workers"][0]["change_summary"]["changed_files"] == [
        "post-snapshot.txt",
        "projection.txt",
    ]
    assert worker["content_revision"] == f"sha256:{worker['content_sha256']}"
    assert worker["report_summary"] == "Created the projection evidence."
    assert worker["change_summary"] == {
        "available": True,
        "has_changes": True,
        "change_count": 1,
        "changed_files": ["projection.txt"],
        "truncated": False,
    }
    assert worker["integration_state"] == "not_integrated"

    options = dict(manager.jobs[job_id].options or {})
    options["_worker_integrated_at"] = time.time()
    manager.update_job_options(job_id, options)
    integrated = runtime.projection_snapshot()

    assert integrated["content_revision"] != first["content_revision"]
    assert change_scans == 4
    assert integrated["workers"][0]["integration_state"] == "applied_to_checkout"
    assert integrated["workers"][0]["review_disposition"] == "accepted"


def test_projection_snapshot_does_not_read_or_mutate_manager_poll_delta_caches(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = ProjectionExecutor()
    runtime = WorkerRuntime(config, manager, executor)
    add_worker(
        manager,
        executor,
        worker_id="wrk-cache",
        name="Cache",
        state=JobState.COMPLETED,
        result={"summary": "Cache-independent projection"},
    )
    runtime._status_poll_snapshots = {
        "manager-a": {"wrk-cache": {"state": "working", "event_count": 7}}
    }
    runtime._status_poll_responses = {
        "status-a": {"at": 123.0, "payload": {"status_current": True}}
    }
    poll_snapshots_before = deepcopy(runtime._status_poll_snapshots)
    poll_responses_before = deepcopy(runtime._status_poll_responses)

    snapshot = runtime.projection_snapshot(previous_edge_worker_ids=["wrk-cache"])

    assert snapshot["present_edge_worker_ids"] == ["wrk-cache"]
    assert runtime._status_poll_snapshots == poll_snapshots_before
    assert runtime._status_poll_responses == poll_responses_before


def test_projection_snapshot_retries_when_job_is_added_during_collection(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = ProjectionExecutor()
    runtime = WorkerRuntime(config, manager, executor)
    add_worker(
        manager,
        executor,
        worker_id="wrk-first",
        name="First",
        state=JobState.COMPLETED,
        result={"summary": "First complete"},
    )
    second_job_id = add_worker(
        manager,
        executor,
        worker_id="wrk-concurrent",
        name="Concurrent",
        state=JobState.COMPLETED,
        result={"summary": "Concurrent complete"},
    )
    second_job = manager.jobs.pop(second_job_id)

    class ConcurrentAdditionJobs(dict):
        added = False

        def values(self):
            iterator = super().values()
            for job in iterator:
                yield job
                if not self.added:
                    self.added = True
                    self[second_job.job_id] = second_job

    manager.jobs = ConcurrentAdditionJobs(manager.jobs)

    snapshot = runtime.projection_snapshot()

    assert snapshot["present_edge_worker_ids"] == ["wrk-concurrent", "wrk-first"]


def test_projection_snapshot_isolates_malformed_worker_projection(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = ProjectionExecutor()
    runtime = WorkerRuntime(config, manager, executor)
    add_worker(
        manager,
        executor,
        worker_id="wrk-valid",
        name="Valid",
        state=JobState.COMPLETED,
        result={"summary": "Valid report"},
    )
    add_worker(
        manager,
        executor,
        worker_id="wrk-malformed",
        name="Malformed",
        state=JobState.COMPLETED,
        result={"summary": "Malformed report"},
    )
    project = runtime._worker_projection

    def malformed_projection(jobs):
        if runtime._worker_identity(jobs)[0] == "wrk-malformed":
            raise ValueError("private projection detail /secret/worktree")
        return project(jobs)

    monkeypatch.setattr(runtime, "_worker_projection", malformed_projection)

    snapshot = runtime.projection_snapshot()
    workers = {worker["edge_worker_id"]: worker for worker in snapshot["workers"]}

    assert workers["wrk-valid"]["report_summary"] == "Valid report"
    malformed = workers["wrk-malformed"]
    assert malformed["name"] == "Malformed"
    assert malformed["work_group_id"] == "grp-projection"
    assert malformed["lane_id"] == "lane-malformed"
    assert malformed["worker_state"] == "projection_error"
    assert malformed["projection_error_category"] == "invalid_worker_projection"
    assert malformed["can_message"] is False
    assert "/secret/worktree" not in str(snapshot)
