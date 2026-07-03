import asyncio
import sys
import time

import pytest

from patchbay.jobs.executor import JobExecutor, STALE_RUNNING_JOB_ERROR
from patchbay.jobs.manager import JobManager, JobState


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "server": {
            "max_concurrent_jobs": 1,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH", "PYTHONPATH"],
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }


@pytest.mark.asyncio
async def test_cancel_job_terminates_running_process(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "sleep", config["repositories"]["default"], {})

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    task = asyncio.create_task(executor.execute_job(job_id))
    for _ in range(50):
        if job_id in executor.processes:
            break
        await asyncio.sleep(0.02)

    result = await executor.cancel_job(job_id)
    await asyncio.wait_for(task, timeout=5)

    job = manager.get_job(job_id)
    assert result["cancelled"] is True
    assert result["process_signalled"] is True
    assert job.state == JobState.CANCELLED
    assert job.error == "Cancelled by request"
    assert job_id not in executor.processes


@pytest.mark.asyncio
async def test_cancel_unknown_job_returns_false(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)

    result = await executor.cancel_job("missing")

    assert result["cancelled"] is False
    assert "Unknown job" in result["reason"]


def test_reconcile_stale_running_job_marks_failed(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.jobs[job_id].started_at = time.time() - 60
    manager._persist_job(manager.jobs[job_id])

    result = executor.reconcile_stale_running_jobs(grace_seconds=0)

    job = manager.get_job(job_id)
    assert result["checked"] == 1
    assert result["reconciled"] == 1
    assert result["job_ids"] == [job_id]
    assert job.state == JobState.FAILED
    assert job.completed_at is not None
    assert job.error == STALE_RUNNING_JOB_ERROR


def test_reconcile_stale_running_job_honors_process_tracking_and_grace(tmp_path):
    config = make_config(tmp_path)
    config["server"]["max_concurrent_jobs"] = 0
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    tracked_job_id = manager.create_job("plan", "tracked", config["repositories"]["default"], {})
    grace_job_id = manager.create_job("plan", "launching", config["repositories"]["default"], {})
    manager.update_job_state(tracked_job_id, JobState.RUNNING)
    manager.update_job_state(grace_job_id, JobState.RUNNING)
    manager.jobs[tracked_job_id].started_at = 100.0
    manager.jobs[grace_job_id].started_at = 198.0
    executor.processes[tracked_job_id] = object()

    result = executor.reconcile_stale_running_jobs(grace_seconds=5, now=200.0)

    assert result["checked"] == 2
    assert result["reconciled"] == 0
    assert manager.get_job(tracked_job_id).state == JobState.RUNNING
    assert manager.get_job(grace_job_id).state == JobState.RUNNING


def test_zero_or_named_job_timeout_disables_codex_turn_timeout(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)

    config["server"]["job_timeout_seconds"] = 0
    assert executor._job_timeout_seconds() is None

    config["server"]["job_timeout_seconds"] = "unlimited"
    assert executor._job_timeout_seconds() is None

    config["server"]["job_timeout_seconds"] = 12
    assert executor._job_timeout_seconds() == 12.0


def test_queue_enabled_creates_execution_semaphore(tmp_path):
    config = make_config(tmp_path)
    config["server"]["queue_enabled"] = True
    config["server"]["max_concurrent_jobs"] = 2
    manager = JobManager(config)
    executor = JobExecutor(config, manager)

    assert executor._execution_semaphore is not None


@pytest.mark.asyncio
async def test_queue_enabled_waits_for_execution_slot(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["server"]["queue_enabled"] = True
    config["server"]["max_concurrent_jobs"] = 1
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    first_id = manager.create_job("plan", "first", config["repositories"]["default"], {"structured_output": False})
    second_id = manager.create_job("plan", "second", config["repositories"]["default"], {"structured_output": False})

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", "import time; time.sleep(0.25); print('done')"]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    first_task = asyncio.create_task(executor.execute_job(first_id))
    second_task = asyncio.create_task(executor.execute_job(second_id))
    for _ in range(50):
        if first_id in executor.processes:
            break
        await asyncio.sleep(0.02)

    assert manager.get_job(first_id).state == JobState.RUNNING
    assert manager.get_job(second_id).state == JobState.PENDING

    await asyncio.gather(first_task, second_task)

    assert manager.get_job(first_id).state == JobState.COMPLETED
    assert manager.get_job(second_id).state == JobState.COMPLETED
