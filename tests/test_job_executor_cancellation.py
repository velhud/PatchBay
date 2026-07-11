import asyncio
import json
import os
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
        "locks": {"root": str(tmp_path / "locks")},
        "power_tools": {"codex_home": str(tmp_path / "codex-home")},
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
async def test_parse_result_persists_fallback_from_latest_agent_message(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    result_file = tmp_path / "logs" / "jobs" / "fallback_result.json"
    agent_message = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "status": "completed",
                "text": "I checked the UI surface and found no blocker.",
            },
        }
    )

    result = await executor._parse_result(agent_message.encode("utf-8"), result_file, {"structured_output": True})

    assert result["summary"] == "I checked the UI surface and found no blocker."
    assert result["result_source"] == "latest_agent_message_text"
    assert result["final_structured_result"] is False
    assert result["codex_result_event_seen"] is False
    assert result["parsed_output_schema_valid"] is False
    assert json.loads(result_file.read_text(encoding="utf-8"))["summary"] == result["summary"]


@pytest.mark.asyncio
async def test_parse_result_raw_fallback_prefers_useful_tail(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    result_file = tmp_path / "logs" / "jobs" / "raw_tail_result.json"
    noisy_start = "\n".join(json.dumps({"type": "event", "index": index}) for index in range(30))
    useful_message = json.dumps({"type": "error", "message": "Late useful error after broad search."})
    stdout = f"{noisy_start}\n{useful_message}\n".encode("utf-8")

    result = await executor._parse_result(stdout, result_file, {"structured_output": True})

    assert result["final_structured_result"] is False
    assert result["raw_output_available"] is True
    assert "Late useful error" in result["stdout_preview"]
    assert '"index": 0' not in result["stdout_preview"]


def test_attach_failure_diagnostic_overrides_raw_fallback_summary(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)

    result = executor._attach_failure_diagnostic(
        {
            "summary": "No final structured worker report was captured, but PatchBay preserved bounded raw Codex output for manager inspection.",
            "files_changed": [],
        },
        {
            "category": "codex_auth_refresh_failed",
            "public_message": "Codex authentication failed before the worker could run.",
            "manager_guidance": "Refresh Codex login before retrying.",
            "operator_action": "Run `codex login`.",
            "retry_without_operator_action": False,
        },
    )

    assert result["summary"] == "Codex authentication failed before the worker could run."
    assert result["failure_diagnostic"]["category"] == "codex_auth_refresh_failed"
    assert "Refresh Codex login" in result["notes"]


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


@pytest.mark.asyncio
async def test_cancel_job_without_live_process_persists_partial_artifact(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "queued or orphaned", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)

    result = await executor.cancel_job(job_id, reason="Stop stale worker")

    job = manager.get_job(job_id)
    result_file = tmp_path / "logs" / "jobs" / f"{job_id}_result.json"
    persisted = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["cancelled"] is True
    assert result["process_signalled"] is False
    assert job.state == JobState.CANCELLED
    assert job.result["partial"] is True
    assert job.result["status"] == "cancelled"
    assert "stopped before PatchBay captured Codex output" in job.result["summary"]
    assert persisted["partial"] is True


def test_job_manager_recovers_missing_result_from_artifact(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "lost result", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.FAILED, error="failed before state included result")
    manager.jobs[job_id].result = None
    manager._persist_job(manager.jobs[job_id])
    result_file = tmp_path / "logs" / "jobs" / f"{job_id}_result.json"
    result_file.write_text(
        json.dumps(
            {
                "summary": "Recovered artifact report.",
                "files_changed": [],
                "failure_diagnostic": {"category": "codex_auth_refresh_failed"},
            }
        ),
        encoding="utf-8",
    )

    reloaded = JobManager(config)

    assert reloaded.get_job(job_id).result["summary"] == "Recovered artifact report."
    assert reloaded.get_job(job_id).result["failure_diagnostic"]["category"] == "codex_auth_refresh_failed"


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


def test_reconcile_stale_running_job_honors_live_process_pid(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "inspect", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.jobs[job_id].started_at = time.time() - 60
    manager.jobs[job_id].last_heartbeat_at = time.time()
    manager.jobs[job_id].process_pid = os.getpid()
    manager._persist_job(manager.jobs[job_id])

    result = executor.reconcile_stale_running_jobs(grace_seconds=0)

    assert result["checked"] == 1
    assert result["reconciled"] == 0
    assert manager.get_job(job_id).state == JobState.RUNNING


def test_zombie_process_is_not_considered_live(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    executor = JobExecutor(config, JobManager(config))

    class ZombieStat:
        def read_text(self, **kwargs):
            return "123 (codex) Z 1 123 123 0 0 0 0 0 0 0 0 0 0 0 0 0 0 55"

    monkeypatch.setattr("patchbay.jobs.executor.Path", lambda value: ZombieStat())
    assert executor._process_pid_is_live(123) is False


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


@pytest.mark.asyncio
async def test_codex_startup_gate_serializes_launch_until_session_created(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["server"]["queue_enabled"] = True
    config["server"]["max_concurrent_jobs"] = 2
    config["server"]["codex_session_start_timeout_seconds"] = 3
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    first_id = manager.create_job("plan", "first", config["repositories"]["default"], {"structured_output": False})
    second_id = manager.create_job("plan", "second", config["repositories"]["default"], {"structured_output": False})
    starts = tmp_path / "starts.jsonl"

    def script(name: str, delay_before_session: float) -> str:
        thread = json.dumps({"type": "thread.started", "thread_id": f"session-{name}"})
        return (
            "import pathlib,time\n"
            f"pathlib.Path({str(starts)!r}).open('a').write({name!r} + ' ' + str(time.time()) + '\\n')\n"
            f"time.sleep({delay_before_session})\n"
            f"print({thread!r}, flush=True)\n"
            "time.sleep(0.1)\n"
            f"print({name!r} + ' done', flush=True)\n"
        )

    def fake_command(mode, prompt, cwd, options=None):
        if prompt == "first":
            return [sys.executable, "-u", "-c", script("first", 0.45)]
        return [sys.executable, "-u", "-c", script("second", 0.0)]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    first_task = asyncio.create_task(executor.execute_job(first_id))
    await asyncio.sleep(0.05)
    second_task = asyncio.create_task(executor.execute_job(second_id))

    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=6)

    entries = []
    for line in starts.read_text(encoding="utf-8").splitlines():
        name, timestamp = line.split()
        entries.append((name, float(timestamp)))
    assert [name for name, _ in entries] == ["first", "second"]
    assert entries[1][1] - entries[0][1] >= 0.35
    assert manager.get_job(first_id).session_id == "session-first"
    assert manager.get_job(second_id).session_id == "session-second"
    assert manager.get_job(first_id).state == JobState.COMPLETED
    assert manager.get_job(second_id).state == JobState.COMPLETED


@pytest.mark.asyncio
async def test_codex_startup_gate_releases_when_process_exits_before_session(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config["server"]["queue_enabled"] = True
    config["server"]["max_concurrent_jobs"] = 2
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    first_id = manager.create_job("plan", "first", config["repositories"]["default"], {"structured_output": False})
    second_id = manager.create_job("plan", "second", config["repositories"]["default"], {"structured_output": False})

    first_script = "import sys; sys.stderr.write('startup failed before session\\n'); sys.exit(1)"
    second_thread = json.dumps({"type": "thread.started", "thread_id": "session-second"})
    second_script = f"print({second_thread!r}, flush=True); print('ok', flush=True)"

    def fake_command(mode, prompt, cwd, options=None):
        if prompt == "first":
            return [sys.executable, "-u", "-c", first_script]
        return [sys.executable, "-u", "-c", second_script]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    await asyncio.wait_for(
        asyncio.gather(
            asyncio.create_task(executor.execute_job(first_id)),
            asyncio.create_task(executor.execute_job(second_id)),
        ),
        timeout=6,
    )

    assert manager.get_job(first_id).state == JobState.FAILED
    assert manager.get_job(second_id).state == JobState.COMPLETED
    assert manager.get_job(second_id).session_id == "session-second"


@pytest.mark.asyncio
async def test_codex_startup_file_lock_waits_for_external_holder(tmp_path):
    pytest.importorskip("fcntl")
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    key = executor._codex_startup_gate_key()
    lock_path = executor._codex_startup_lock_path(key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    import fcntl

    holder = lock_path.open("a+b")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    started = time.monotonic()
    acquire_task = asyncio.create_task(executor._acquire_codex_startup_file_lock(key))
    await asyncio.sleep(0.15)
    assert not acquire_task.done()
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()

    file_handle, acquired_path = await asyncio.wait_for(acquire_task, timeout=2)
    try:
        assert acquired_path == str(lock_path)
        assert time.monotonic() - started >= 0.12
    finally:
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
        file_handle.close()


@pytest.mark.asyncio
async def test_codex_auth_refresh_failure_is_classified_and_reported(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "auth failure", config["repositories"]["default"], {})

    stdout_events = [
        {"type": "thread.started", "thread_id": "session-auth-failed"},
        {"type": "turn.started"},
        {
            "type": "error",
            "message": "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.",
        },
        {
            "type": "turn.failed",
            "error": {
                "message": "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.",
            },
        },
    ]
    script = (
        "import json,sys\n"
        "events = " + repr(stdout_events) + "\n"
        "for event in events:\n"
        "    print(json.dumps(event), flush=True)\n"
        "sys.stderr.write('code: refresh_token_reused\\n')\n"
        "sys.exit(1)\n"
    )

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-u", "-c", script]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    await asyncio.wait_for(executor.execute_job(job_id), timeout=5)

    job = manager.get_job(job_id)
    result_file = tmp_path / "logs" / "jobs" / f"{job_id}_result.json"
    persisted = json.loads(result_file.read_text(encoding="utf-8"))
    assert job.state == JobState.FAILED
    assert job.session_id == "session-auth-failed"
    assert "Codex authentication failed" in job.error
    assert job.result["failure_diagnostic"]["category"] == "codex_auth_refresh_failed"
    assert job.result["failure_diagnostic"]["retry_without_operator_action"] is False
    assert "operator re-authentication is required" in job.result["notes"]
    assert persisted["failure_diagnostic"]["category"] == "codex_auth_refresh_failed"


@pytest.mark.asyncio
async def test_codex_usage_limit_is_classified_without_blaming_patchbay(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "quota failure", config["repositories"]["default"], {})
    message = "You have reached your usage limit. Try again at 11:46 PM."
    events = [
        {"type": "thread.started", "thread_id": "session-quota-failed"},
        {"type": "turn.failed", "error": {"message": message}},
    ]
    script = (
        "import json,sys\n"
        "events = " + repr(events) + "\n"
        "for event in events: print(json.dumps(event), flush=True)\n"
        "sys.exit(1)\n"
    )
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )

    await asyncio.wait_for(executor.execute_job(job_id), timeout=5)

    job = manager.get_job(job_id)
    assert job.state == JobState.FAILED
    assert job.result["failure_diagnostic"]["category"] == "codex_usage_limit"
    assert job.result["failure_diagnostic"]["retry_without_operator_action"] is True
    assert "11:46 PM" in job.result["failure_diagnostic"]["retry_hint"]
    assert "not a PatchBay" in job.result["notes"]
