import asyncio
import json
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
async def test_cancelled_json_worker_preserves_partial_report_and_checkpoints(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "stream then cancel", config["repositories"]["default"], {})

    thread = json.dumps({"type": "thread.started", "thread_id": "session-cancelled"})
    agent_message = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_checkpoint",
                "type": "agent_message",
                "status": "completed",
                "text": json.dumps(
                    {
                        "summary": "I finished the first evidence pass and am continuing.",
                        "files_changed": [],
                        "commands_run": ["rg --files"],
                        "tests_run": [],
                    }
                ),
            },
        }
    )
    script = (
        "import time\n"
        f"print({thread!r}, flush=True)\n"
        f"print({agent_message!r}, flush=True)\n"
        "time.sleep(30)\n"
    )

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-u", "-c", script]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    task = asyncio.create_task(executor.execute_job(job_id))
    for _ in range(80):
        job = manager.get_job(job_id)
        if job and job.checkpoints:
            break
        await asyncio.sleep(0.03)

    result = await executor.cancel_job(job_id)
    await asyncio.wait_for(task, timeout=5)

    job = manager.get_job(job_id)
    result_file = tmp_path / "logs" / "jobs" / f"{job_id}_result.json"
    assert result["cancelled"] is True
    assert job.state == JobState.CANCELLED
    assert job.session_id == "session-cancelled"
    assert job.result["partial"] is True
    assert job.result["status"] == "cancelled"
    assert "first evidence pass" in job.result["summary"]
    assert job.checkpoints[-1]["kind"] == "agent_message"
    assert "first evidence pass" in job.checkpoints[-1]["summary"]
    assert json.loads(result_file.read_text(encoding="utf-8"))["partial"] is True


@pytest.mark.asyncio
async def test_cancel_job_marks_cancelled_before_waiting_for_process_exit(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "cancel ordering", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)

    class RunningProcess:
        returncode = None

    executor.processes[job_id] = RunningProcess()

    async def fake_terminate_process(cancelled_job_id, process):
        assert cancelled_job_id == job_id
        assert manager.get_job(job_id).state == JobState.CANCELLED
        return True

    monkeypatch.setattr(executor, "_terminate_process", fake_terminate_process)

    result = await executor.cancel_job(job_id)

    assert result["cancelled"] is True
    assert result["process_signalled"] is True
    assert manager.get_job(job_id).state == JobState.CANCELLED


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


@pytest.mark.asyncio
async def test_reconcile_stale_running_job_honors_live_task_after_process_exit(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "parsing", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.jobs[job_id].started_at = time.time() - 60
    manager._persist_job(manager.jobs[job_id])

    class ExitedProcess:
        returncode = 0

    async def live_task():
        await asyncio.sleep(30)

    task = asyncio.create_task(live_task())
    executor.tasks[job_id] = task
    executor.processes[job_id] = ExitedProcess()
    try:
        result = executor.reconcile_stale_running_jobs(grace_seconds=0)
    finally:
        executor.processes.pop(job_id, None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert result["checked"] == 1
    assert result["reconciled"] == 0
    assert manager.get_job(job_id).state == JobState.RUNNING
    assert manager.get_job(job_id).error is None


@pytest.mark.asyncio
async def test_reconcile_stale_running_job_honors_live_executor_task(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "launching", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.jobs[job_id].started_at = time.time() - 60
    manager._persist_job(manager.jobs[job_id])

    async def live_task():
        await asyncio.sleep(30)

    task = asyncio.create_task(live_task())
    executor.tasks[job_id] = task
    try:
        result = executor.reconcile_stale_running_jobs(grace_seconds=0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert result["checked"] == 1
    assert result["reconciled"] == 0
    assert manager.get_job(job_id).state == JobState.RUNNING


@pytest.mark.asyncio
async def test_schedule_job_keeps_task_tracked_until_completion(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "quick", config["repositories"]["default"], {"structured_output": False})

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", "print('done')"]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    task = executor.schedule_job(job_id)
    assert executor.tasks[job_id] is task
    await asyncio.wait_for(task, timeout=5)
    await asyncio.sleep(0)

    assert manager.get_job(job_id).state == JobState.COMPLETED
    assert job_id not in executor.tasks
    assert manager.get_job(job_id).process_started_at is not None


@pytest.mark.asyncio
async def test_json_events_update_session_and_heartbeat_before_completion(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "stream", config["repositories"]["default"], {"structured_output": False})

    thread = json.dumps({"type": "thread.started", "thread_id": "session-live"})
    command = (
        "import sys,time;"
        f"print({thread!r}, flush=True);"
        "time.sleep(1);"
        "print('done', flush=True)"
    )

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", command]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    task = asyncio.create_task(executor.execute_job(job_id))
    for _ in range(50):
        job = manager.get_job(job_id)
        if job.session_id == "session-live":
            break
        await asyncio.sleep(0.03)

    running = manager.get_job(job_id)
    assert running.state == JobState.RUNNING
    assert running.session_id == "session-live"
    assert running.last_event == "thread.started"
    assert running.last_heartbeat_at is not None

    await asyncio.wait_for(task, timeout=5)
    completed = manager.get_job(job_id)
    assert completed.state == JobState.COMPLETED
    assert completed.session_id == "session-live"


@pytest.mark.asyncio
async def test_codex_process_without_json_session_fails_startup_timeout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["server"]["job_timeout_seconds"] = 0
    config["server"]["codex_session_start_timeout_seconds"] = 0.2
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "hang", config["repositories"]["default"], {})

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    await asyncio.wait_for(executor.execute_job(job_id), timeout=5)

    job = manager.get_job(job_id)
    assert job.state == JobState.FAILED
    assert "did not create a JSON session" in job.error
    assert job.process_pid is not None
    assert job.completed_at is not None


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
