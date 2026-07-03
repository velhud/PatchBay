import asyncio
import subprocess
from pathlib import Path

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.pro_requests import ProRequestStore
from patchbay.tools.handler import ToolHandler


def init_repo(path: Path) -> Path:
    path.mkdir()
    (path / "README.md").write_text("# Dispatch Pro Requests\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Pro Dispatch", "-c", "user.email=pro-dispatch@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def config(tmp_path: Path, repo: Path) -> dict:
    return {
        "app": {"tool_mode": "worker"},
        "server": {"max_concurrent_jobs": 5, "job_timeout_seconds": 30, "job_cleanup_after_hours": 24},
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {"require_git_repo": False, "default_sandbox": "read-only", "blocked_globs": []},
        "power_tools": {"direct_write": False, "bash_mode": "off", "codex_session_read": False},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "pro_requests": {
            "root": str(tmp_path / "runtime" / "pro-requests"),
            "mirror_enabled": True,
            "mirror_dir": ".ai-bridge/pro-requests",
        },
        "workers": {"worktree_root": str(tmp_path / "workers")},
    }


class RecordingExecutor(JobExecutor):
    def __init__(self, config, manager):
        super().__init__(config, manager)
        self.started = []

    async def execute_job(self, job_id):
        self.started.append(job_id)


@pytest.mark.asyncio
async def test_dispatch_to_idle_origin_worker_messages_existing_worker(tmp_path):
    repo = init_repo(tmp_path / "repo")
    cfg = config(tmp_path, repo)
    manager = JobManager(cfg)
    executor = RecordingExecutor(cfg, manager)
    handler = ToolHandler(cfg, manager, executor)

    started = await handler.handle_tool_call(
        "codex_worker_start",
        {"name": "Origin Worker", "brief": "Start.", "workspace_mode": "read_only"},
    )
    await asyncio.sleep(0)
    first_job = manager.get_job(executor.started[0])
    manager.update_job_state(first_job.job_id, JobState.COMPLETED, result={"summary": "ready", "files_changed": []}, session_id="sess", exit_code=0)

    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    request_id = handler.pro_request_store.create_request(
        repo_path=str(repo),
        title="Origin",
        origin_kind="patchbay_worker",
        origin_worker="Origin Worker",
        report_path=str(report),
    )["id"]
    await handler.handle_tool_call(
        "codex_pro_request_respond",
        {
            "request_id": request_id,
            "response_markdown": "Full response",
            "worker_message_markdown": "Worker instruction",
        },
    )

    dispatched = await handler.handle_tool_call("codex_pro_request_dispatch", {"request_id": request_id, "target": "origin_worker"})
    await asyncio.sleep(0)

    assert dispatched["accepted"] is True
    assert dispatched["request"]["status"] == "dispatched_to_worker"
    resume_job = manager.get_job(executor.started[-1])
    assert resume_job.mode == "resume"
    assert "Worker instruction" in resume_job.prompt


@pytest.mark.asyncio
async def test_dispatch_to_busy_origin_worker_is_blocked_not_queued(tmp_path):
    repo = init_repo(tmp_path / "repo")
    cfg = config(tmp_path, repo)
    manager = JobManager(cfg)
    executor = RecordingExecutor(cfg, manager)
    handler = ToolHandler(cfg, manager, executor)

    await handler.handle_tool_call("codex_worker_start", {"name": "Busy Worker", "brief": "Start.", "workspace_mode": "read_only"})
    await asyncio.sleep(0)
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    request_id = handler.pro_request_store.create_request(
        repo_path=str(repo),
        title="Busy",
        origin_kind="patchbay_worker",
        origin_worker="Busy Worker",
        report_path=str(report),
    )["id"]
    await handler.handle_tool_call("codex_pro_request_respond", {"request_id": request_id, "response_markdown": "answer"})

    before_jobs = len(manager.jobs)
    dispatched = await handler.handle_tool_call("codex_pro_request_dispatch", {"request_id": request_id, "target": "origin_worker"})

    assert dispatched["accepted"] is False
    assert dispatched["request"]["status"] == "dispatch_blocked"
    assert len(manager.jobs) == before_jobs
    assert "intentionally does not add a message queue" in dispatched["dispatch_result"]["note"]


@pytest.mark.asyncio
async def test_dispatch_to_new_worker_starts_isolated_worker(tmp_path):
    repo = init_repo(tmp_path / "repo")
    cfg = config(tmp_path, repo)
    manager = JobManager(cfg)
    executor = RecordingExecutor(cfg, manager)
    handler = ToolHandler(cfg, manager, executor)
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    request_id = handler.pro_request_store.create_request(repo_path=str(repo), title="New", report_path=str(report))["id"]
    await handler.handle_tool_call("codex_pro_request_respond", {"request_id": request_id, "response_markdown": "new worker answer"})

    dispatched = await handler.handle_tool_call(
        "codex_pro_request_dispatch",
        {"request_id": request_id, "target": "new_worker", "new_worker_name": "New Pro Worker"},
    )
    await asyncio.sleep(0)

    assert dispatched["accepted"] is True
    assert dispatched["request"]["status"] == "dispatched_to_worker"
    job = manager.get_job(executor.started[-1])
    assert job.options["_worker_name"] == "New Pro Worker"
    assert job.options["_worker_workspace_mode"] == "isolated_write"
