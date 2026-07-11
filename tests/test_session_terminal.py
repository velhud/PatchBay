import asyncio
import json
import sys
import time
from datetime import datetime, timezone

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.jobs.session_terminal import CodexSessionTerminalObserver


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex_home = tmp_path / "codex-home"
    return {
        "server": {
            "max_concurrent_jobs": 2,
            "job_timeout_seconds": 0,
            "job_cleanup_after_hours": 24,
            "codex_session_start_timeout_seconds": 3,
            "codex_post_completion_exit_grace_seconds": 0.1,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
        },
        "power_tools": {"codex_home": str(codex_home)},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
            "job_log_max_bytes": 200_000,
            "write_raw_job_logs": False,
        },
    }


def write_session(codex_home, session_id, records):
    path = codex_home / "sessions" / "2026" / "07" / "11" / f"rollout-test-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": "/fixture"},
    }
    path.write_text(
        "\n".join(json.dumps(value) for value in [meta, *records]) + "\n",
        encoding="utf-8",
    )
    return path


def test_observer_requires_exact_session_and_current_turn(tmp_path):
    config = make_config(tmp_path)
    old_timestamp = time.time() - 300
    old_iso = datetime.fromtimestamp(old_timestamp, timezone.utc).isoformat()
    write_session(
        tmp_path / "codex-home",
        "session-old",
        [
            {"timestamp": old_iso, "type": "event_msg", "payload": {"type": "agent_message", "message": "old"}},
            {"timestamp": old_iso, "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "old"}},
        ],
    )
    write_session(
        tmp_path / "codex-home",
        "session-other",
        [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "wrong session"},
            }
        ],
    )

    observer = CodexSessionTerminalObserver(config, "session-old", not_before=time.time() - 10)

    assert observer.poll().completed is False


def test_observer_ignores_prior_turn_and_accepts_new_turn_in_same_session(tmp_path):
    config = make_config(tmp_path)
    session_id = "session-resumed"
    old_iso = datetime.fromtimestamp(time.time() - 300, timezone.utc).isoformat()
    source = write_session(
        tmp_path / "codex-home",
        session_id,
        [
            {
                "timestamp": old_iso,
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "prior turn"},
            }
        ],
    )
    observer = CodexSessionTerminalObserver(config, session_id, not_before=time.time() - 2)
    assert observer.poll().completed is False

    now = datetime.now(timezone.utc).isoformat()
    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": now,
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "current turn"},
                }
            )
            + "\n"
        )

    snapshot = observer.poll()
    assert snapshot.completed is True
    assert snapshot.final_message == "current turn"


def test_observer_tolerates_partial_line_and_detects_appended_terminal(tmp_path):
    config = make_config(tmp_path)
    session_id = "session-current"
    source = write_session(tmp_path / "codex-home", session_id, [])
    observer = CodexSessionTerminalObserver(config, session_id, not_before=time.time() - 2)

    assert observer.poll().completed is False
    with source.open("ab") as handle:
        handle.write(b'{"timestamp":"2026-07-11T12:00:00Z","type":"event_msg"')
    assert observer.poll().completed is False
    with source.open("ab") as handle:
        handle.write(b'}\n')
        now = datetime.now(timezone.utc).isoformat()
        handle.write(
            (
                json.dumps(
                    {
                        "timestamp": now,
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "final answer"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": now,
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )

    snapshot = observer.poll()

    assert snapshot.completed is True
    assert snapshot.source == "session_task_complete"
    assert snapshot.final_message == "final answer"


@pytest.mark.asyncio
async def test_executor_completes_when_session_is_terminal_but_wrapper_lingers(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "linger", config["repositories"]["default"], {})
    session_id = "session-lingering-wrapper"
    session_file = (
        tmp_path
        / "codex-home"
        / "sessions"
        / "2026"
        / "07"
        / "11"
        / f"rollout-test-{session_id}.jsonl"
    )
    final_result = {"summary": "SESSION_TERMINAL_OK", "files_changed": [], "tests_run": ["fixture"]}
    script = f"""
import json, pathlib, subprocess, sys, time
path = pathlib.Path({str(session_file)!r})
path.parent.mkdir(parents=True, exist_ok=True)
now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
records = [
    {{'timestamp': now, 'type': 'session_meta', 'payload': {{'id': {session_id!r}, 'cwd': '/fixture'}}}},
]
path.write_text('\\n'.join(json.dumps(value) for value in records) + '\\n', encoding='utf-8')
print(json.dumps({{'type': 'thread.started', 'thread_id': {session_id!r}}}), flush=True)
time.sleep(0.2)
with path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'agent_message', 'message': json.dumps({final_result!r})}}}}) + '\\n')
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'task_complete', 'last_agent_message': json.dumps({final_result!r})}}}}) + '\\n')
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
time.sleep(30)
"""

    monkeypatch.setattr(executor, "_build_codex_command", lambda *args, **kwargs: [sys.executable, "-u", "-c", script])

    await asyncio.wait_for(executor.execute_job(job_id), timeout=6)

    job = manager.get_job(job_id)
    assert job.state == JobState.COMPLETED
    assert job.result["summary"] == "SESSION_TERMINAL_OK"
    assert job.terminal_source == "session_task_complete"
    assert job.wrapper_cleanup_outcome == "terminated_after_terminal"
    assert job.exit_code != 0


def test_first_terminal_decision_wins_and_late_source_is_recorded(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "race", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)

    assert manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        terminal_source="session_task_complete",
        terminal_observed_at=10,
    )
    assert not manager.transition_job_terminal(
        job_id,
        JobState.CANCELLED,
        terminal_source="manager_cancellation",
        terminal_observed_at=11,
    )

    job = manager.get_job(job_id)
    assert job.state == JobState.COMPLETED
    assert job.terminal_source == "session_task_complete"
    assert job.late_terminal_source == "manager_cancellation"


@pytest.mark.asyncio
async def test_stop_cannot_cancel_after_semantic_completion_is_claimed(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "race", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING)
    assert manager.claim_job_semantic_completion(
        job_id,
        source="session_task_complete",
        observed_at=time.time(),
    )

    outcome = await executor.cancel_job(job_id, reason="manager stopped it")

    assert outcome["cancelled"] is False
    assert manager.get_job(job_id).state == JobState.RUNNING
    assert manager.get_job(job_id).terminal_source == "session_task_complete"
    assert manager.get_job(job_id).late_terminal_source == "manager_cancellation"


@pytest.mark.asyncio
async def test_stop_recovers_completed_session_report_before_cancellation(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "recover-before-stop", config["repositories"]["default"], {})
    session_id = "session-complete-before-stop"
    started_at = time.time() - 5
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=started_at,
        process_started_at=started_at,
        session_id=session_id,
    )
    write_session(
        tmp_path / "codex-home",
        session_id,
        [{
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": json.dumps({"summary": "FINAL_REPORT", "files_changed": []})},
        }],
    )

    outcome = await executor.cancel_job(job_id, reason="manager stop raced completion")

    assert outcome["cancelled"] is False
    assert outcome["completed"] is True
    recovered = manager.get_job(job_id)
    assert recovered.state == JobState.COMPLETED
    assert recovered.result["summary"] == "FINAL_REPORT"


def test_restart_reconciliation_recovers_exact_terminal_session(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "recover", config["repositories"]["default"], {})
    started_at = time.time() - 5
    session_id = "session-restart-recovery"
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=started_at,
        process_started_at=started_at,
        session_id=session_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    final_result = {"summary": "RECOVERED_OK", "files_changed": []}
    write_session(
        tmp_path / "codex-home",
        session_id,
        [
            {
                "timestamp": now,
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": json.dumps(final_result)},
            }
        ],
    )

    recovered_manager = JobManager(config)
    executor = JobExecutor(config, recovered_manager)
    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)

    recovered = recovered_manager.get_job(job_id)
    assert outcome["recovered_completed"] == 1
    assert outcome["reconciled"] == 0
    assert recovered.state == JobState.COMPLETED
    assert recovered.result["summary"] == "RECOVERED_OK"
    assert recovered.terminal_source == "session_task_complete"


def test_restart_reconciliation_does_not_adopt_other_session(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("plan", "recover", config["repositories"]["default"], {})
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=time.time() - 60,
        process_started_at=time.time() - 60,
        session_id="session-missing",
    )
    write_session(
        tmp_path / "codex-home",
        "session-unrelated",
        [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "wrong"},
            }
        ],
    )

    outcome = JobExecutor(config, manager).reconcile_stale_running_jobs(grace_seconds=0)

    assert outcome["recovered_completed"] == 0
    assert manager.get_job(job_id).state == JobState.FAILED
