import asyncio
import subprocess
from pathlib import Path

import pytest

from patchbay.jobs.manager import JobManager, JobState
from patchbay.workers.runtime import WORKER_ID_OPTION, WorkerRuntime


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# worker coordination\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Test", "-c", "user.email=worker-test@example.invalid", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return {
        "server": {
            "max_concurrent_jobs": 4,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "max_diff_bytes": 200_000,
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "workers": {"worktree_root": str(tmp_path / "worker-worktrees")},
        "locks": {"root": str(tmp_path / "locks")},
    }


class RecordingExecutor:
    def __init__(self, manager):
        self.manager = manager
        self.started = []

    async def execute_job(self, job_id):
        self.started.append(job_id)

    async def cancel_job(self, job_id, reason="Cancelled by request"):
        self.manager.update_job_state(job_id, JobState.CANCELLED, error=reason)
        return {"cancelled": True, "job_id": job_id, "state": "cancelled"}


@pytest.mark.asyncio
async def test_start_worker_can_include_peer_report_context_without_backend_ids(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    source = await runtime.start_worker(
        name="Source Investigator",
        brief="Inspect the source.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    await asyncio.sleep(0)
    source_job = manager.get_job(executor.started[-1])
    manager.update_job_state(
        source_job.job_id,
        JobState.COMPLETED,
        result={"summary": "The connector boundary is already clean.", "files_changed": []},
        session_id="session-source",
        exit_code=0,
    )

    reviewer = await runtime.start_worker(
        name="Reviewer",
        brief="Review Source Investigator's conclusion and report whether you agree.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        context_from_workers=["Source Investigator"],
        context_detail="report",
    )
    await asyncio.sleep(0)
    reviewer_job = manager.get_job(executor.started[-1])

    assert reviewer["context_sources"] == ["Source Investigator"]
    assert reviewer["context_detail"] == "report"
    assert reviewer["context_truncated"] is False
    assert "Peer worker context follows" in reviewer_job.prompt
    assert "Context from worker: Source Investigator" in reviewer_job.prompt
    assert "connector boundary is already clean" in reviewer_job.prompt
    assert "session-source" not in reviewer_job.prompt
    assert source["worker_id"] not in reviewer_job.prompt
    assert config["repositories"]["default"] not in reviewer_job.prompt


@pytest.mark.asyncio
async def test_peer_diff_context_is_bounded_and_workspace_relative(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    implementer = await runtime.start_worker(
        name="Implementer",
        brief="Create a file.",
        repo_path=config["repositories"]["default"],
        workspace_mode="isolated_write",
    )
    await asyncio.sleep(0)
    implementer_job = manager.get_job(executor.started[-1])
    worktree = Path(implementer_job.worktree_path)
    (worktree / "implementation.txt").write_text("phase-three-peer-diff\n", encoding="utf-8")
    manager.update_job_state(
        implementer_job.job_id,
        JobState.COMPLETED,
        result={"summary": "Created implementation.txt", "files_changed": ["implementation.txt"]},
        session_id="session-impl",
        exit_code=0,
    )

    reviewer = await runtime.start_worker(
        name="Diff Reviewer",
        brief="Review Implementer's concrete change.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        context_from_workers=["Implementer"],
        context_detail="diff",
    )
    await asyncio.sleep(0)
    reviewer_job = manager.get_job(executor.started[-1])

    assert reviewer["context_sources"] == ["Implementer"]
    assert reviewer["context_detail"] == "diff"
    assert "Changed files:" in reviewer_job.prompt
    assert "implementation.txt" in reviewer_job.prompt
    assert "Bounded diff:" in reviewer_job.prompt
    assert "+phase-three-peer-diff" in reviewer_job.prompt
    assert str(worktree) not in reviewer_job.prompt
    assert config["repositories"]["default"] not in reviewer_job.prompt
    assert reviewer_job.options["sandbox"] == "read-only"


@pytest.mark.asyncio
async def test_message_worker_can_relay_context_from_another_worker(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    target = await runtime.start_worker(
        name="Target Worker",
        brief="Start target.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    await asyncio.sleep(0)
    target_job = manager.get_job(executor.started[-1])
    manager.update_job_state(
        target_job.job_id,
        JobState.COMPLETED,
        result={"summary": "Target is ready."},
        session_id="session-target",
        exit_code=0,
    )

    source = await runtime.start_worker(
        name="Source Worker",
        brief="Start source.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    await asyncio.sleep(0)
    source_job = manager.get_job(executor.started[-1])
    manager.update_job_state(
        source_job.job_id,
        JobState.COMPLETED,
        result={"summary": "Source says use the smaller patch."},
        session_id="session-source",
        exit_code=0,
    )

    delivered = await runtime.message_worker(
        worker="Target Worker",
        message="Consider Source Worker's recommendation and respond.",
        context_from_workers=["Source Worker"],
        context_detail="report",
    )
    await asyncio.sleep(0)
    resume_job = manager.get_job(executor.started[-1])

    assert delivered["accepted"] is True
    assert delivered["context_sources"] == ["Source Worker"]
    assert resume_job.mode == "resume"
    assert resume_job.options["resume_session_id"] == "session-target"
    assert resume_job.options[WORKER_ID_OPTION] == target["worker_id"]
    assert "Source says use the smaller patch" in resume_job.prompt
    assert "session-source" not in resume_job.prompt


@pytest.mark.asyncio
async def test_worker_list_returns_team_report(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    first = await runtime.start_worker(
        name="Alpha",
        brief="Inspect alpha.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    second = await runtime.start_worker(
        name="Beta",
        brief="Inspect beta.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    await asyncio.sleep(0)
    for job in manager.jobs.values():
        manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": f"Report for {job.options['_worker_name']}"}, session_id=f"session-{job.job_id}")

    listed = await runtime.list_workers()

    assert listed["count"] == 2
    assert "team_report" in listed
    assert "Alpha" in listed["team_report"]
    assert "Beta" in listed["team_report"]
    assert "job_id" not in listed["team_report"]
    assert first["worker_id"] not in listed["team_report"]
    assert second["worker_id"] not in listed["team_report"]
