import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from patchbay.jobs.executor import (
    _JOB_PROCESS_MARKER_VERSION,
    _JOB_PROCESS_MARKER_VERSION_OPTION,
    JobExecutor,
    terminal_cleanup_pending,
)
from patchbay.jobs.manager import JobManager, JobState
from patchbay.jobs.process_supervisor import cleanup_proof_budget_seconds
from patchbay.jobs.session_terminal import CodexSessionTerminalObserver
from patchbay.repo_locks import RepoMutationBusy, mark_repo_lock_options


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
            "codex_post_completion_cleanup_timeout_seconds": 1,
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


def full_result(summary="COMPLETE"):
    return {
        "summary": summary,
        "detailed_report": "Completed the bounded test assignment.",
        "evidence": ["fixture evidence"],
        "files_changed": [],
        "commands_run": [],
        "tests_run": ["fixture"],
        "notes": "",
        "risks": [],
        "open_questions": [],
        "next_steps": [],
    }


def test_supervisor_cleanup_call_budget_covers_both_discovery_layers(tmp_path):
    config = make_config(tmp_path)
    executor = JobExecutor(config, JobManager(config))
    proof_budget = cleanup_proof_budget_seconds()

    assert executor._post_completion_cleanup_call_timeout_seconds(
        1.0, supervisor_contract=True
    ) >= proof_budget * 2.0 + 9.0
    assert executor._post_completion_cleanup_call_timeout_seconds(
        0.1, supervisor_contract=True
    ) == 1.0


@pytest.mark.asyncio
async def test_reconciliation_releases_proven_terminal_orphan_repo_lease(
    tmp_path,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(
        repo, operation="shared_write_fixture"
    )
    job_id = manager.create_job(
        "resume",
        "proven terminal orphan",
        repo,
        mark_repo_lock_options({}, operation="shared_write_fixture"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("PROVEN_TERMINAL"),
        wrapper_cleanup_outcome="terminated_after_terminal",
    )

    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="before_reconcile")

    reconciliation = executor.reconcile_stale_running_jobs(grace_seconds=0)

    assert reconciliation["orphaned_repo_leases_released"] == 1
    assert reconciliation["orphaned_repo_lease_job_ids"] == [job_id]
    assert job_id not in executor.repo_locks.bound_job_ids()
    next_lease = await executor.repo_locks.acquire(
        repo, operation="after_reconcile"
    )
    next_lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_outcome",
    [
        None,
        "cleanup_pending",
        "cleanup_blocked_untrusted_process_identity",
    ],
)
async def test_proven_terminal_lease_reconciliation_keeps_unproven_cleanup_locked(
    tmp_path, cleanup_outcome,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(
        repo, operation="pending_cleanup_fixture"
    )
    job_id = manager.create_job(
        "resume",
        "pending cleanup",
        repo,
        mark_repo_lock_options({}, operation="pending_cleanup_fixture"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("PENDING_CLEANUP"),
        wrapper_cleanup_outcome=cleanup_outcome,
    )

    assert executor._release_proven_terminal_repo_leases() == []
    assert job_id in executor.repo_locks.bound_job_ids()
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="must_remain_busy")

    executor.repo_locks.release_job(job_id)


@pytest.mark.asyncio
async def test_repo_lease_reconciliation_keeps_missing_job_locked(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(
        repo, operation="missing_job_fixture"
    )
    job_id = "missing-job"
    executor.repo_locks.bind_to_job(job_id, lease)

    assert executor._release_proven_terminal_repo_leases() == []
    assert job_id in executor.repo_locks.bound_job_ids()
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="must_fail_closed")

    executor.repo_locks.release_job(job_id)


@pytest.mark.asyncio
async def test_repo_lease_reconciliation_keeps_untrusted_cleanup_locked(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(
        repo, operation="untrusted_cleanup_fixture"
    )
    job_id = manager.create_job(
        "resume",
        "untrusted cleanup",
        repo,
        mark_repo_lock_options({}, operation="untrusted_cleanup_fixture"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("UNTRUSTED_CLEANUP"),
        wrapper_cleanup_outcome="terminated_after_terminal",
    )
    monkeypatch.setattr(
        executor,
        "_recorded_cleanup_has_untrusted_live_members",
        lambda _job: True,
    )

    assert executor._release_proven_terminal_repo_leases() == []
    assert job_id in executor.repo_locks.bound_job_ids()
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="must_fail_untrusted")

    executor.repo_locks.release_job(job_id)


def test_liveness_refresh_skips_process_discovery_for_proven_terminal_cleanup(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    proven_id = manager.create_job("plan", "proven", config["repositories"]["default"])
    manager.update_job_state(
        proven_id,
        JobState.COMPLETED,
        wrapper_cleanup_outcome="process_exited",
    )
    missing_id = manager.create_job("plan", "missing", config["repositories"]["default"])
    manager.update_job_state(missing_id, JobState.COMPLETED)
    running_id = manager.create_job("plan", "running", config["repositories"]["default"])
    manager.update_job_state(running_id, JobState.RUNNING)
    discovered: list[str] = []

    def record_discovery(job_id):
        discovered.append(job_id)
        return executor._inactive_runtime_liveness()

    monkeypatch.setattr(executor, "_runtime_liveness", record_discovery)

    executor._refresh_runtime_liveness_cache()

    assert proven_id not in discovered
    assert missing_id in discovered
    assert running_id in discovered
    assert executor.runtime_liveness_snapshot(proven_id) == (
        executor._inactive_runtime_liveness()
    )


@pytest.mark.asyncio
async def test_cancelling_startup_file_lock_wait_releases_all_lock_ownership(
    tmp_path,
):
    fcntl = pytest.importorskip("fcntl")
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    key = executor._codex_startup_gate_key()
    lock_path = executor._codex_startup_lock_path(key)
    holder = lock_path.open("a+b")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    waiting = asyncio.create_task(executor._acquire_codex_startup_file_lock(key))
    await asyncio.sleep(0.1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()

    acquired, _ = await asyncio.wait_for(
        executor._acquire_codex_startup_file_lock(key), timeout=1
    )
    assert acquired is not None
    fcntl.flock(acquired.fileno(), fcntl.LOCK_UN)
    acquired.close()


@pytest.mark.asyncio
async def test_oversized_json_event_is_drained_and_capture_is_bounded(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    config["logging"]["job_log_max_bytes"] = 32_000
    config["logging"]["process_capture_max_bytes"] = 64_000
    config["logging"]["process_event_line_max_bytes"] = 512_000
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan",
        "large event",
        config["repositories"]["default"],
        {"json_events": False},
    )
    result = full_result("OVERSIZED_EVENT_DRAINED")
    script = (
        "import json; "
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':'x'*200000}}), flush=True); "
        f"print(json.dumps({result!r}), flush=True)"
    )
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )

    await asyncio.wait_for(executor.execute_job(job_id), timeout=8)

    durable = manager.get_job(job_id)
    assert durable.state == JobState.COMPLETED
    assert durable.result["summary"] == "OVERSIZED_EVENT_DRAINED"
    assert durable.stdout_bytes_seen > 200_000
    stdout_log = tmp_path / "logs" / "jobs" / f"{job_id}_stdout.log"
    assert stdout_log.stat().st_size < 40_000


@pytest.mark.asyncio
async def test_restart_accepts_gated_supervisor_proof_without_persisted_pid(
    tmp_path,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="pre_pid_crash")
    job_id = manager.create_job(
        "plan",
        "gated supervisor crash",
        repo,
        mark_repo_lock_options({}, operation="pre_pid_crash"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    executor._persist_process_marker_contract(job_id)
    executor._mark_process_supervisor_spawned(job_id)
    manager.update_job_state(
        job_id,
        JobState.FAILED,
        wrapper_cleanup_outcome="cleanup_pending",
    )
    job = manager.get_job(job_id)
    proof = Path(job.options["_job_process_supervisor_cleanup_proof"])
    proof.write_text("patchbay-supervisor-cleanup-v2:999999991\n", encoding="ascii")
    # The original process has crashed in this scenario, so its kernel-held
    # checkout lock is gone before the replacement process starts.
    lease.release()

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    assert restarted._supervisor_cleanup_proven(job_id) is True
    restarted.reconcile_stale_running_jobs(grace_seconds=0)
    assert not terminal_cleanup_pending(
        restarted_manager.get_job(job_id).wrapper_cleanup_outcome
    )
    next_lease = await restarted.repo_locks.acquire(
        repo, operation="after_pre_pid_crash"
    )
    next_lease.release()


@pytest.mark.asyncio
async def test_restart_before_supervisor_spawn_does_not_require_impossible_proof(
    tmp_path,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="pre_supervisor_crash")
    job_id = manager.create_job(
        "plan",
        "crash before supervisor spawn",
        repo,
        mark_repo_lock_options({}, operation="pre_supervisor_crash"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    executor._persist_process_marker_contract(job_id)
    job = manager.get_job(job_id)
    assert executor._supervisor_cleanup_contract_installed(job) is False
    manager.update_job_state(
        job_id,
        JobState.FAILED,
        wrapper_cleanup_outcome="cleanup_pending",
    )
    lease.release()

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    reconciled = restarted.reconcile_stale_running_jobs(grace_seconds=0)
    assert reconciled["cleanup_reconciled"] == 1
    assert not terminal_cleanup_pending(
        restarted_manager.get_job(job_id).wrapper_cleanup_outcome
    )
    next_lease = await restarted.repo_locks.acquire(
        repo, operation="after_pre_supervisor_crash"
    )
    next_lease.release()


async def settle_darwin_supervisor_uncertainty(executor, manager, job_id):
    """Settle Darwin cleanup and tear down a fail-closed sentinel if one exists."""

    if sys.platform != "darwin":
        return manager.get_job(job_id).wrapper_cleanup_outcome
    deadline = time.monotonic() + 3
    while terminal_cleanup_pending(
        manager.get_job(job_id).wrapper_cleanup_outcome
    ) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    initial = manager.get_job(job_id).wrapper_cleanup_outcome
    if initial != "cleanup_blocked_untrusted_process_identity":
        return initial
    job = manager.get_job(job_id)
    if (job.options or {}).get("_repo_mutation_lock"):
        with pytest.raises(RepoMutationBusy):
            await executor.repo_locks.acquire(
                job.repo_path, operation="must_remain_blocked_during_uncertainty"
            )
    proof = Path(job.options["_job_process_supervisor_cleanup_proof"])
    record = proof.read_text(encoding="ascii").strip()
    prefix = f"patchbay-supervisor-cleanup-unproven-v2:{job.process_pid}:"
    assert record.startswith(prefix)
    sentinel_pid = int(record.removeprefix(prefix))
    try:
        os.kill(sentinel_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process = executor.processes.get(job_id)
    if process is not None and process.returncode is None:
        os.kill(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), timeout=3)
    executor.processes.pop(job_id, None)
    # This is fixture teardown after the fail-closed assertion, not a forged
    # production cleanup proof. Production retains the lock until explicit
    # operator recovery or a machine restart.
    executor.repo_locks.release_job(job_id)
    return initial


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


def test_observer_prime_to_end_ignores_existing_terminal_marker(tmp_path):
    config = make_config(tmp_path)
    session_id = "session-prime-to-end"
    now = datetime.now(timezone.utc).isoformat()
    source = write_session(
        tmp_path / "codex-home",
        session_id,
        [
            {
                "timestamp": now,
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "prior turn"},
            }
        ],
    )
    observer = CodexSessionTerminalObserver(config, session_id, not_before=0)

    initial_offset = observer.prime_to_end()
    assert initial_offset == source.stat().st_size
    assert observer.poll().completed is False

    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "new turn"},
                }
            )
            + "\n"
        )

    snapshot = observer.poll()
    assert snapshot.completed is True
    assert snapshot.final_message == "new turn"


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
    child_pid_file = tmp_path / "lingering-child.pid"
    child_ready_file = tmp_path / "lingering-child.ready"
    final_result = full_result("SESSION_TERMINAL_OK")
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
child_code = "import pathlib, signal, time; signal.signal(signal.SIGTERM, lambda *_: None); pathlib.Path({str(child_ready_file)!r}).write_text('ready', encoding='utf-8'); time.sleep(30)"
child = subprocess.Popen([sys.executable, '-c', child_code])
pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding='utf-8')
deadline = time.time() + 3
while not pathlib.Path({str(child_ready_file)!r}).exists() and time.time() < deadline:
    time.sleep(0.02)
with path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'agent_message', 'message': json.dumps({final_result!r})}}}}) + '\\n')
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'task_complete', 'last_agent_message': json.dumps({final_result!r})}}}}) + '\\n')
time.sleep(30)
"""

    monkeypatch.setattr(executor, "_build_codex_command", lambda *args, **kwargs: [sys.executable, "-u", "-c", script])

    await asyncio.wait_for(executor.execute_job(job_id), timeout=6)

    job = manager.get_job(job_id)
    assert job.state == JobState.COMPLETED
    assert job.result["summary"] == "SESSION_TERMINAL_OK"
    assert job.terminal_source == "session_task_complete"
    if sys.platform == "darwin":
        cleanup_outcome = await settle_darwin_supervisor_uncertainty(
            executor, manager, job_id
        )
        assert not terminal_cleanup_pending(cleanup_outcome)
    else:
        assert job.wrapper_cleanup_outcome == "terminated_after_terminal"
    assert job.options[_JOB_PROCESS_MARKER_VERSION_OPTION] == (
        _JOB_PROCESS_MARKER_VERSION
    )
    assert job.exit_code != 0
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.time() + 2
    while executor._process_pid_is_live(child_pid) and time.time() < deadline:
        await asyncio.sleep(0.05)
    assert executor._process_pid_is_live(child_pid) is False


@pytest.mark.asyncio
async def test_terminal_cleanup_reaps_detached_unmarked_descendant(tmp_path, monkeypatch):
    if not Path("/proc").is_dir():
        pytest.skip("Linux /proc process-marker coverage")
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "detached", config["repositories"]["default"], {})
    session_id = "session-detached-descendant"
    session_file = (
        tmp_path
        / "codex-home"
        / "sessions"
        / "2026"
        / "07"
        / "11"
        / f"rollout-test-{session_id}.jsonl"
    )
    child_pid_file = tmp_path / "detached-child.pid"
    final_result = full_result("DETACHED_DESCENDANT_REAPED")
    script = f"""
import json, os, pathlib, subprocess, sys, time
path = pathlib.Path({str(session_file)!r})
path.parent.mkdir(parents=True, exist_ok=True)
now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
path.write_text(json.dumps({{'timestamp': now, 'type': 'session_meta', 'payload': {{'id': {session_id!r}, 'cwd': '/fixture'}}}}) + '\\n', encoding='utf-8')
print(json.dumps({{'type': 'thread.started', 'thread_id': {session_id!r}}}), flush=True)
child_env = os.environ.copy()
child_env.pop('PATCHBAY_JOB_MARKER', None)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'], start_new_session=True, env=child_env)
pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding='utf-8')
with path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'task_complete', 'last_agent_message': json.dumps({final_result!r})}}}}) + '\\n')
time.sleep(30)
"""
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )

    await asyncio.wait_for(executor.execute_job(job_id), timeout=8)

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert manager.get_job(job_id).state == JobState.COMPLETED
    assert manager.get_job(job_id).result["summary"] == "DETACHED_DESCENDANT_REAPED"
    assert executor._process_pid_is_live(child_pid) is False


@pytest.mark.asyncio
async def test_fast_exiting_target_cannot_escape_detached_unmarked_descendant(
    tmp_path, monkeypatch
):
    if os.name != "posix":
        pytest.skip("POSIX process-supervisor coverage")
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    options = mark_repo_lock_options(
        {"json_events": False}, operation="fast_detached_test"
    )
    job_id = manager.create_job("plan", "fast detached", repo, options)
    lease = await executor.repo_locks.acquire(
        repo, operation="fast_detached_test"
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    child_pid_file = tmp_path / "fast-detached-child.pid"
    result = full_result("FAST_DETACHED_REAPED")
    script = f"""
import json, os, pathlib, subprocess, sys
child_env = os.environ.copy()
child_env.pop('PATCHBAY_JOB_MARKER', None)
child = subprocess.Popen(
    [sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'],
    start_new_session=True,
    env=child_env,
)
pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding='utf-8')
print(json.dumps({result!r}), flush=True)
"""
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )

    await asyncio.wait_for(executor.execute_job(job_id), timeout=8)

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert manager.get_job(job_id).state == JobState.COMPLETED
    assert manager.get_job(job_id).result["summary"] == "FAST_DETACHED_REAPED"
    assert executor._process_pid_is_live(child_pid) is False
    await settle_darwin_supervisor_uncertainty(executor, manager, job_id)
    next_lease = await executor.repo_locks.acquire(
        repo, operation="after_fast_detached"
    )
    next_lease.release()


@pytest.mark.asyncio
async def test_cancellation_while_spawn_handle_is_withheld_never_starts_target(
    tmp_path, monkeypatch
):
    if os.name != "posix":
        pytest.skip("POSIX gated-spawn coverage")
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    options = mark_repo_lock_options(
        {"json_events": False}, operation="spawn_cancel_test"
    )
    job_id = manager.create_job("plan", "cancel spawn", repo, options)
    lease = await executor.repo_locks.acquire(repo, operation="spawn_cancel_test")
    executor.repo_locks.bind_to_job(job_id, lease)
    target_started = tmp_path / "spawn-target-started"
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [
            sys.executable,
            "-u",
            "-c",
            (
                "import pathlib; "
                f"pathlib.Path({str(target_started)!r}).write_text('started', encoding='utf-8')"
            ),
        ],
    )
    real_spawn = asyncio.create_subprocess_exec
    supervisor_spawned = asyncio.Event()
    return_handle = asyncio.Event()
    spawned_processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        spawned_processes.append(process)
        supervisor_spawned.set()
        await return_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    execute_task = asyncio.create_task(executor.execute_job(job_id))
    await asyncio.wait_for(supervisor_spawned.wait(), timeout=3)

    cancel_task = asyncio.create_task(
        executor.cancel_job(job_id, reason="cancel during OS spawn")
    )
    await asyncio.sleep(0)
    return_handle.set()
    outcome = await asyncio.wait_for(cancel_task, timeout=5)
    await asyncio.wait_for(execute_task, timeout=5)

    assert outcome["cancelled"] is True
    assert manager.get_job(job_id).state == JobState.CANCELLED
    assert target_started.exists() is False
    assert spawned_processes
    assert spawned_processes[0].returncode is not None
    assert executor._job_has_live_runtime(job_id) is False
    proof_path = Path(
        manager.get_job(job_id).options["_job_process_supervisor_cleanup_proof"]
    )
    supervisor_stderr = b""
    if spawned_processes[0].stderr is not None:
        supervisor_stderr = await spawned_processes[0].stderr.read()
    assert proof_path.exists(), (
        f"returncode={spawned_processes[0].returncode} "
        f"stderr={supervisor_stderr.decode('utf-8', errors='replace')}"
    )
    proof = proof_path.read_text(encoding="ascii").strip()
    assert proof in {
        f"patchbay-supervisor-cleanup-v2:{spawned_processes[0].pid}",
        f"patchbay-supervisor-gated-v3:{spawned_processes[0].pid}",
    }
    assert executor._supervisor_cleanup_proven(
        job_id, process=spawned_processes[0]
    )
    next_lease = await executor.repo_locks.acquire(
        repo, operation="after_spawn_cancel"
    )
    next_lease.release()


@pytest.mark.asyncio
async def test_terminal_cleanup_completion_ignores_executor_epilogue(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    options = mark_repo_lock_options({}, operation="epilogue_test")
    job_id = manager.create_job("plan", "epilogue", repo, options)
    lease = await executor.repo_locks.acquire(repo, operation="epilogue_test")
    executor.repo_locks.bind_to_job(job_id, lease)
    manager.update_job_state(job_id, JobState.RUNNING)

    release_epilogue = asyncio.Event()

    async def epilogue():
        await release_epilogue.wait()

    task = asyncio.create_task(epilogue())
    executor.tasks[job_id] = task
    executor._transition_job_terminal_with_cleanup(
        job_id,
        JobState.COMPLETED,
        result={"summary": "done", "files_changed": []},
        wrapper_cleanup_outcome="cleanup_pending",
    )
    executor._complete_terminal_cleanup(job_id, "process_not_live_after_terminal")

    liveness = executor._runtime_liveness(job_id)
    assert task.done() is False
    assert liveness["executor_task_alive"] is False
    assert liveness["runtime_alive"] is False
    next_lease = await executor.repo_locks.acquire(
        repo, operation="after_executor_epilogue"
    )
    next_lease.release()
    release_epilogue.set()
    await task


@pytest.mark.asyncio
async def test_restart_cleanup_reaps_persisted_exact_unmarked_descendant(tmp_path):
    if not Path("/proc").is_dir():
        pytest.skip("Linux exact descendant identity coverage")
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    options = mark_repo_lock_options({}, operation="shared_write_test")
    job_id = manager.create_job("plan", "restart descendant", repo, options)
    child_env = os.environ.copy()
    child_env.pop("PATCHBAY_JOB_MARKER", None)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        env=child_env,
    )
    identity = executor._process_identity(child.pid)
    assert identity
    executor._persist_descendant_tracking_options(
        job_id, identities={child.pid: identity}
    )
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        process_pid=999_999_999,
        process_pgid=999_999_999,
        process_identity=None,
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("RESTART_DESCENDANT_REAPED"),
        terminal_source="session_task_complete",
        wrapper_cleanup_outcome="cleanup_pending",
    )

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    try:
        outcome = restarted.reconcile_stale_running_jobs(grace_seconds=0)
        assert outcome["cleanup_reconciled"] == 0
        with pytest.raises(RepoMutationBusy):
            await restarted.repo_locks.acquire(repo, operation="premature_overlap")
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert child.poll() is not None
        deadline = time.monotonic() + 5
        while (
            restarted_manager.get_job(job_id).wrapper_cleanup_outcome
            != "terminated_after_terminal_recovery"
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        assert restarted_manager.get_job(job_id).wrapper_cleanup_outcome == (
            "terminated_after_terminal_recovery"
        )
        lease = await restarted.repo_locks.acquire(repo, operation="after_reap")
        lease.release()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def test_legacy_recorded_pid_identity_never_authorizes_signal_or_release(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "recycled", config["repositories"]["default"], {})
    job = manager.get_job(job_id)
    job.process_pid = 424242
    job.process_pgid = 424242
    job.process_identity = "linux-proc-start:old"
    monkeypatch.setattr(executor, "_process_pid_is_live", lambda pid: True)
    monkeypatch.setattr(executor, "_process_identity", lambda pid: "linux-proc-start:new")
    monkeypatch.setattr(executor, "_process_group_members_from_proc", lambda pgid: set())
    monkeypatch.setattr(executor, "_job_marked_process_pids", lambda worker_job_id: set())
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    outcome = executor._terminate_recorded_process(job)

    assert outcome == "cleanup_blocked_untrusted_process_identity"
    assert signals == []


def test_linux_process_identity_includes_boot_id_and_legacy_identity_is_unknown(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "identity", config["repositories"]["default"], {})
    job = manager.get_job(job_id)
    stat_fields = ["S", *("0" for _ in range(18)), "777"]

    monkeypatch.setattr(executor, "_linux_boot_id", lambda: "boot-a")
    original_read_text = Path.read_text

    def read_process_stat(path, *args, **kwargs):
        if path == Path("/proc/424242/stat"):
            return f"424242 (codex) {' '.join(stat_fields)}"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_process_stat)

    assert executor._process_identity(424242) == "linux-proc-start-v2:boot-a:777"
    job.process_pid = 424242
    job.process_identity = "linux-proc-start:777"
    assert executor._recorded_process_pid_is_trustworthy(job) is False
    assert executor._exact_linux_identity_disproves_ownership(job) is False

    job.process_identity = "linux-proc-start-v2:boot-a:777"
    monkeypatch.setattr(
        executor,
        "_process_identity",
        lambda pid: "linux-proc-start-v2:boot-b:777",
    )
    assert executor._recorded_process_pid_is_trustworthy(job) is False
    assert executor._exact_linux_identity_disproves_ownership(job) is True


def test_linux_unreadable_proc_entry_makes_marker_and_group_scans_unknown(
    tmp_path, monkeypatch
):
    if not Path("/proc").is_dir():
        pytest.skip("Linux /proc scan coverage")
    config = make_config(tmp_path)
    executor = JobExecutor(config, JobManager(config))
    unreadable_pid = os.getpid()
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def guarded_read_bytes(path):
        if path == Path(f"/proc/{unreadable_pid}/environ"):
            raise PermissionError("auditor unreadable environ")
        return original_read_bytes(path)

    def guarded_read_text(path, *args, **kwargs):
        if path == Path(f"/proc/{unreadable_pid}/stat"):
            raise PermissionError("auditor unreadable stat")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    assert executor._job_marked_process_pids("auditor", force_refresh=True) is None

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    assert executor._process_group_members_from_proc(os.getpgrp()) is None


def test_unreadable_proc_entry_is_unrelated_only_with_same_boot_start_proof(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "marker proof", config["repositories"]["default"], {})
    job = manager.get_job(job_id)
    job.process_identity = "linux-proc-start-v2:boot-a:200"
    job.options["_job_process_login_uid"] = 1000
    entry = Path("/proc/999")

    monkeypatch.setattr(executor, "_linux_boot_id", lambda: "boot-a")
    monkeypatch.setattr(executor, "_linux_login_uid", lambda candidate: 2000)
    monkeypatch.setattr(executor, "_linux_proc_start_ticks", lambda candidate: 300)
    assert executor._unreadable_proc_entry_cannot_match_job_marker(entry, job_id)

    monkeypatch.setattr(executor, "_linux_login_uid", lambda candidate: 1000)
    monkeypatch.setattr(executor, "_linux_proc_start_ticks", lambda candidate: 100)
    assert executor._unreadable_proc_entry_cannot_match_job_marker(entry, job_id)

    monkeypatch.setattr(executor, "_linux_proc_start_ticks", lambda candidate: 300)
    assert not executor._unreadable_proc_entry_cannot_match_job_marker(entry, job_id)

    monkeypatch.setattr(executor, "_linux_boot_id", lambda: "boot-b")
    assert not executor._unreadable_proc_entry_cannot_match_job_marker(entry, job_id)

    job.process_identity = "linux-proc-start:200"
    monkeypatch.setattr(executor, "_linux_boot_id", lambda: "boot-a")
    monkeypatch.setattr(executor, "_linux_proc_start_ticks", lambda candidate: 100)
    assert not executor._unreadable_proc_entry_cannot_match_job_marker(entry, job_id)


def test_positive_marker_match_survives_partial_proc_scan_but_absence_is_unknown(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "positive marker", config["repositories"]["default"], {})
    marker = executor._job_process_marker(job_id)
    proc = Path("/proc")
    unreadable = proc / "101"
    marked = proc / "202"

    monkeypatch.setattr(Path, "is_dir", lambda path: path == proc)
    monkeypatch.setattr(Path, "iterdir", lambda path: iter((unreadable, marked)))

    def read_bytes(path):
        if path == unreadable / "environ":
            raise PermissionError("potentially job-owned entry")
        if path == marked / "environ":
            return f"PATCHBAY_JOB_MARKER={marker}".encode("utf-8") + b"\0"
        raise AssertionError(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, **kwargs: "202 (worker) S 1 202 202 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 500",
    )
    monkeypatch.setattr(
        executor,
        "_unreadable_proc_entry_cannot_match_job_marker",
        lambda entry, scan_job_id: False,
    )

    assert executor._job_marked_process_pids(job_id, force_refresh=True) == {202}

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: (_ for _ in ()).throw(PermissionError("unknown absence")),
    )
    assert executor._job_marked_process_pids(job_id, force_refresh=True) is None

    monkeypatch.setattr(
        executor,
        "_unreadable_proc_entry_cannot_match_job_marker",
        lambda entry, scan_job_id: True,
    )
    assert executor._job_marked_process_pids(job_id, force_refresh=True) == set()


def test_portable_marker_scan_requires_environment_suffix_and_fails_closed_on_churn(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    executor = JobExecutor(config, JobManager(config))
    job_id = "portable-marker-job"
    marker_token = (
        f"PATCHBAY_JOB_MARKER={executor._job_process_marker(job_id)}"
    )
    plain_rows = {
        101: ("S", f"/bin/tool --label {marker_token}"),
        202: ("S", "/bin/worker --detached"),
    }
    expanded_rows = {
        101: ("S", f"/bin/tool --label {marker_token} PATH=/bin"),
        202: ("S", f"/bin/worker --detached PATH=/bin {marker_token}"),
    }
    monkeypatch.setattr(
        executor,
        "_portable_process_rows",
        lambda *, include_environment: (
            expanded_rows if include_environment else plain_rows
        ),
    )

    assert executor._job_marked_process_pids_from_ps(job_id) == {202}

    expanded_rows[202] = ("S", "/bin/worker --detached PATH=/bin")
    assert executor._job_marked_process_pids_from_ps(job_id) == set()

    expanded_rows[202] = ("S", "/different/process PATH=/bin")
    assert executor._job_marked_process_pids_from_ps(job_id) is None


@pytest.mark.asyncio
async def test_non_linux_portable_marker_keeps_detached_child_barrier_until_reap(
    tmp_path,
):
    if Path("/proc").is_dir():
        pytest.skip("portable ps marker discovery is exercised on non-Linux hosts")
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(
        repo, operation="portable_detached_test"
    )
    options = mark_repo_lock_options({}, operation="portable_detached_test")
    options[_JOB_PROCESS_MARKER_VERSION_OPTION] = _JOB_PROCESS_MARKER_VERSION
    job_id = manager.create_job("plan", "portable detached", repo, options)
    executor.repo_locks.bind_to_job(job_id, lease)
    wrapper_pid = 2_000_000_000
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        process_pid=wrapper_pid,
        process_pgid=wrapper_pid,
        process_identity=None,
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("PORTABLE_DETACHED"),
        terminal_source="session_task_complete",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    child_env = os.environ.copy()
    child_env["PATCHBAY_JOB_MARKER"] = executor._job_process_marker(job_id)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        env=child_env,
    )

    class ExitedWrapper:
        pid = wrapper_pid
        returncode = 0

    process = ExitedWrapper()
    executor.processes[job_id] = process

    try:
        deadline = time.monotonic() + 3
        marked_pids = executor._job_marked_process_pids(
            job_id, force_refresh=True
        )
        while child.pid not in (marked_pids or set()) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            marked_pids = executor._job_marked_process_pids(
                job_id, force_refresh=True
            )
        assert child.pid in (marked_pids or set())

        assert executor._retain_or_release_terminal_cleanup(job_id, process) is True
        assert child.poll() is None
        assert terminal_cleanup_pending(
            manager.get_job(job_id).wrapper_cleanup_outcome
        )
        with pytest.raises(RepoMutationBusy):
            await executor.repo_locks.acquire(repo, operation="detached_overlap")

        await asyncio.wait_for(executor.cleanup_tasks[job_id], timeout=5)
        child.wait(timeout=3)
        assert child.poll() is not None
        assert manager.get_job(job_id).wrapper_cleanup_outcome == (
            "terminated_after_terminal_async"
        )
        next_lease = await executor.repo_locks.acquire(
            repo, operation="after_portable_reap"
        )
        next_lease.release()
    finally:
        cleanup_task = executor.cleanup_tasks.get(job_id)
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
        executor.processes.pop(job_id, None)
        executor.repo_locks.release_job(job_id)


@pytest.mark.asyncio
async def test_non_linux_unknown_detached_descendant_keeps_lock_and_cleanup_pending(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="shared_write_test")
    options = mark_repo_lock_options({}, operation="shared_write_test")
    options[_JOB_PROCESS_MARKER_VERSION_OPTION] = _JOB_PROCESS_MARKER_VERSION
    job_id = manager.create_job("plan", "portable unknown", repo, options)
    executor.repo_locks.bind_to_job(job_id, lease)
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        process_pid=999_999,
        process_pgid=999_999,
        process_identity=None,
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("PORTABLE_UNKNOWN"),
        terminal_source="session_task_complete",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    child_env = os.environ.copy()
    child_env["PATCHBAY_JOB_MARKER"] = executor._job_process_marker(job_id)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        env=child_env,
    )

    class ExitedWrapper:
        pid = 999_999
        returncode = 0

    process = ExitedWrapper()
    executor.processes[job_id] = process
    monkeypatch.setattr(executor, "_process_group_liveness", lambda pgid: False)
    monkeypatch.setattr(
        executor,
        "_job_marked_process_pids",
        lambda *args, **kwargs: None,
    )

    try:
        assert executor._retain_or_release_terminal_cleanup(job_id, process) is True
        await asyncio.sleep(0)
        assert executor._process_pid_is_live(child.pid) is True
        assert terminal_cleanup_pending(
            manager.get_job(job_id).wrapper_cleanup_outcome
        )
        with pytest.raises(RepoMutationBusy):
            await executor.repo_locks.acquire(repo, operation="detached_overlap")
    finally:
        cleanup_task = executor.cleanup_tasks.get(job_id)
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        child.terminate()
        child.wait(timeout=3)
        executor.processes.pop(job_id, None)
        executor.repo_locks.release_job(job_id)


@pytest.mark.asyncio
async def test_linux_empty_marker_scan_disproves_reused_process_group_ownership(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    options = mark_repo_lock_options({}, operation="shared_write_test")
    options[_JOB_PROCESS_MARKER_VERSION_OPTION] = _JOB_PROCESS_MARKER_VERSION
    job_id = manager.create_job(
        "plan",
        "reused linux group",
        repo,
        options,
    )
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        process_pid=424242,
        process_pgid=424242,
        process_identity="linux-proc-start:old",
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("durable"),
        terminal_source="session_task_complete",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    monkeypatch.setattr(executor, "_process_pid_is_live", lambda pid: False)
    monkeypatch.setattr(executor, "_process_identity", lambda pid: None)
    monkeypatch.setattr(
        executor, "_process_group_members_from_proc", lambda pgid: {424243}
    )
    monkeypatch.setattr(executor, "_job_marked_process_pids", lambda job_id: set())
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: signals.append(("pid", pid, sig))
    )
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: signals.append(("pgid", pgid, sig))
    )

    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)

    assert outcome["cleanup_reconciled"] == 1
    assert manager.get_job(job_id).wrapper_cleanup_outcome == (
        "process_not_live_after_terminal"
    )
    assert signals == []
    lease = await executor.repo_locks.acquire(repo, operation="reused_group_disproved")
    lease.release()


@pytest.mark.asyncio
async def test_legacy_job_empty_marker_scan_cannot_disprove_live_group_ownership(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    job_id = manager.create_job(
        "plan",
        "legacy live group",
        repo,
        mark_repo_lock_options({}, operation="shared_write_test"),
    )
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        process_pid=424242,
        process_pgid=424242,
        process_identity="linux-proc-start:old",
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("legacy durable"),
        terminal_source="session_task_complete",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    monkeypatch.setattr(executor, "_process_pid_is_live", lambda pid: False)
    monkeypatch.setattr(executor, "_process_identity", lambda pid: None)
    monkeypatch.setattr(
        executor, "_process_group_members_from_proc", lambda pgid: {424243}
    )
    monkeypatch.setattr(executor, "_job_marked_process_pids", lambda job_id: set())
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        os, "kill", lambda pid, sig: signals.append(("pid", pid, sig))
    )
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: signals.append(("pgid", pgid, sig))
    )

    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)

    assert outcome["cleanup_reconciled"] == 0
    assert manager.get_job(job_id).wrapper_cleanup_outcome == (
        "cleanup_blocked_untrusted_process_identity"
    )
    assert _JOB_PROCESS_MARKER_VERSION_OPTION not in manager.get_job(job_id).options
    assert signals == []
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="legacy_group_must_block")


@pytest.mark.asyncio
async def test_non_linux_restart_dead_leader_live_group_fails_closed_without_signals(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    job_id = manager.create_job(
        "plan",
        "persisted non-linux group",
        repo,
        mark_repo_lock_options({}, operation="shared_write_test"),
    )
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        process_pid=424242,
        process_pgid=424242,
        process_identity=None,
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("durable"),
        terminal_source="session_task_complete",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    monkeypatch.setattr(executor, "_process_identity", lambda pid: None)
    monkeypatch.setattr(executor, "_process_pid_is_live", lambda pid: False)
    monkeypatch.setattr(executor, "_process_group_members_from_proc", lambda pgid: None)
    monkeypatch.setattr(
        executor,
        "_process_group_members_from_ps",
        lambda pgid: {424243},
    )
    monkeypatch.setattr(executor, "_job_marked_process_pids", lambda job_id: None)
    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: signals.append(("pid", pid, sig)),
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pgid, sig: signals.append(("pgid", pgid, sig)),
    )

    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)

    durable = manager.get_job(job_id)
    assert outcome["cleanup_reconciled"] == 0
    assert durable.wrapper_cleanup_outcome == (
        "cleanup_blocked_untrusted_process_identity"
    )
    assert signals == []
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="must_remain_blocked")

    monkeypatch.setattr(executor, "_process_group_members_from_ps", lambda pgid: set())
    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)
    assert outcome["cleanup_reconciled"] == 1
    lease = await executor.repo_locks.acquire(repo, operation="group_is_gone")
    lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_version", [None, _JOB_PROCESS_MARKER_VERSION])
async def test_non_linux_restarted_running_job_with_unknown_live_owner_keeps_barrier(
    tmp_path, monkeypatch, marker_version
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    options = mark_repo_lock_options({}, operation="shared_write_test")
    if marker_version is not None:
        options[_JOB_PROCESS_MARKER_VERSION_OPTION] = marker_version
    job_id = manager.create_job("plan", "restarted running", repo, options)
    old = time.time() - 60
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=old,
        process_started_at=old,
        last_heartbeat_at=old,
        process_pid=454545,
        process_pgid=454545,
        process_identity=None,
    )
    monkeypatch.setattr(executor, "_process_identity", lambda pid: None)
    monkeypatch.setattr(executor, "_process_pid_is_live", lambda pid: True)
    monkeypatch.setattr(executor, "_process_group_members_from_proc", lambda pgid: None)
    monkeypatch.setattr(executor, "_process_group_members_from_ps", lambda pgid: {454545})
    monkeypatch.setattr(executor, "_job_marked_process_pids", lambda job_id: None)

    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)

    durable = manager.get_job(job_id)
    assert outcome["reconciled"] == 1
    assert durable.state == JobState.FAILED
    assert durable.wrapper_cleanup_outcome == (
        "cleanup_blocked_untrusted_process_identity"
    )
    assert durable.options.get(_JOB_PROCESS_MARKER_VERSION_OPTION) == marker_version
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="must_remain_blocked")
    executor.repo_locks.release_job(job_id)
    assert executor.repo_locks.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_manager_cancel_persists_cleanup_pending_before_process_cleanup(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    job_id = manager.create_job(
        "plan",
        "cancel crash boundary",
        repo,
        mark_repo_lock_options({}, operation="shared_write_test"),
    )
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        process_pid=434343,
        process_pgid=434343,
        process_identity="linux-proc-start:recorded",
    )
    executor.processes[job_id] = type(
        "TrackedProcess", (), {"pid": 434343, "returncode": None}
    )()

    original_transition = manager.transition_job_terminal
    barrier_seen_before_transition = False

    def transition_with_barrier_check(*args, **kwargs):
        nonlocal barrier_seen_before_transition
        if args[1] == JobState.CANCELLED:
            barrier_seen_before_transition = bool(
                executor.repo_locks._cleanup_job_repos.get(job_id)
            )
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(manager, "transition_job_terminal", transition_with_barrier_check)

    async def crash_after_terminal_transition(*args, **kwargs):
        raise RuntimeError("simulated crash after cancellation commit")

    monkeypatch.setattr(executor, "_terminate_process", crash_after_terminal_transition)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await executor.cancel_job(job_id, reason="manager stop")

    durable = manager.get_job(job_id)
    assert durable.state == JobState.CANCELLED
    assert durable.terminal_source == "manager_cancellation"
    assert durable.wrapper_cleanup_outcome == "cleanup_pending"
    assert barrier_seen_before_transition is True
    assert durable.result["status"] == "cancelled"
    assert durable.result["partial"] is True
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="post_cancel_crash")

    restarted_manager = JobManager(config)
    assert restarted_manager.get_job(job_id).result["status"] == "cancelled"
    restarted = JobExecutor(config, restarted_manager)
    monkeypatch.setattr(restarted, "_process_pid_is_live", lambda pid: False)
    monkeypatch.setattr(restarted, "_process_identity", lambda pid: None)
    monkeypatch.setattr(restarted, "_process_group_members_from_proc", lambda pgid: None)
    monkeypatch.setattr(restarted, "_process_group_members_from_ps", lambda pgid: {434344})
    monkeypatch.setattr(restarted, "_job_marked_process_pids", lambda job_id: None)

    restarted.reconcile_stale_running_jobs(grace_seconds=0)

    assert restarted_manager.get_job(job_id).wrapper_cleanup_outcome == (
        "cleanup_blocked_untrusted_process_identity"
    )
    with pytest.raises(RepoMutationBusy):
        await restarted.repo_locks.acquire(repo, operation="restart_must_remain_blocked")


@pytest.mark.asyncio
async def test_cancel_during_startup_gate_retains_lock_and_never_launches(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="startup_gate_test")
    job_id = manager.create_job(
        "plan",
        "cancel while queued at startup",
        repo,
        mark_repo_lock_options({}, operation="startup_gate_test"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    gate_entered = asyncio.Event()
    release_gate = asyncio.Event()
    launches: list[list[str]] = []

    class GateLease:
        def release(self, _reason=""):
            return None

    async def delayed_gate(_job_id):
        gate_entered.set()
        await release_gate.wait()
        return GateLease()

    async def forbidden_launch(*cmd, **_kwargs):
        launches.append(list(cmd))
        raise AssertionError("Codex launched after cancellation")

    monkeypatch.setattr(executor, "_acquire_codex_startup_gate", delayed_gate)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_launch)

    task = executor.schedule_job(job_id)
    await asyncio.wait_for(gate_entered.wait(), timeout=2)
    cancelled = await executor.cancel_job(job_id, reason="manager stop")

    assert cancelled["cancelled"] is True
    await asyncio.wait_for(asyncio.shield(task), timeout=3)
    deadline = time.monotonic() + 3
    while (
        terminal_cleanup_pending(manager.get_job(job_id).wrapper_cleanup_outcome)
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(0.05)

    assert launches == []
    assert manager.get_job(job_id).state == JobState.CANCELLED
    assert not terminal_cleanup_pending(
        manager.get_job(job_id).wrapper_cleanup_outcome
    )
    next_lease = await executor.repo_locks.acquire(
        repo, operation="after_startup_cancel_ack"
    )
    next_lease.release()


@pytest.mark.asyncio
async def test_pidless_marker_process_is_reconciled_before_cancel_releases_lock(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="pidless_cancel")
    options = mark_repo_lock_options({}, operation="pidless_cancel")
    options[_JOB_PROCESS_MARKER_VERSION_OPTION] = _JOB_PROCESS_MARKER_VERSION
    job_id = manager.create_job("plan", "pidless launch window", repo, options)
    executor.repo_locks.bind_to_job(job_id, lease)
    manager.update_job_state(job_id, JobState.RUNNING)
    alive = {424_242}
    signals: list[int] = []

    def marked(_job_id, *, force_refresh=False):
        return set(alive)

    def signal_marked(_job_id, sig):
        signals.append(int(sig))
        alive.clear()
        return True

    monkeypatch.setattr(executor, "_job_marked_process_pids", marked)
    monkeypatch.setattr(executor, "_signal_job_marked_processes", signal_marked)
    monkeypatch.setattr(
        executor, "_recorded_cleanup_has_untrusted_live_members", lambda _job: False
    )

    cancelled = await executor.cancel_job(job_id, reason="recover pidless launch")

    assert cancelled["cancelled"] is True
    assert cancelled["process_signalled"] is True
    assert signals and signals[0] == int(signal.SIGTERM)
    assert manager.get_job(job_id).state == JobState.CANCELLED
    assert not terminal_cleanup_pending(
        manager.get_job(job_id).wrapper_cleanup_outcome
    )
    next_lease = await executor.repo_locks.acquire(
        repo, operation="after_pidless_cleanup"
    )
    next_lease.release()


@pytest.mark.asyncio
async def test_stdout_turn_completion_waits_for_exact_session_report_before_cleanup(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "stdout ordering", config["repositories"]["default"], {}
    )
    session_id = "session-stdout-before-observer"
    session_file = (
        tmp_path
        / "codex-home"
        / "sessions"
        / "2026"
        / "07"
        / "11"
        / f"rollout-test-{session_id}.jsonl"
    )
    final_result = full_result("EXACT_SESSION_REPORT_FIRST")
    script = f"""
import json, pathlib, time
path = pathlib.Path({str(session_file)!r})
path.parent.mkdir(parents=True, exist_ok=True)
now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
path.write_text(json.dumps({{'timestamp': now, 'type': 'session_meta', 'payload': {{'id': {session_id!r}, 'cwd': '/fixture'}}}}) + '\\n', encoding='utf-8')
print(json.dumps({{'type': 'thread.started', 'thread_id': {session_id!r}}}), flush=True)
with path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'task_complete', 'last_agent_message': json.dumps({final_result!r})}}}}) + '\\n')
print(json.dumps({{'type': 'turn.completed'}}), flush=True)
time.sleep(30)
"""
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )
    original_terminate = executor._terminate_process
    cleanup_snapshots: list[tuple[JobState, object, str]] = []

    async def checked_terminate(*args, **kwargs):
        current = manager.get_job(job_id)
        cleanup_snapshots.append(
            (current.state, current.result, str(current.terminal_source or ""))
        )
        return await original_terminate(*args, **kwargs)

    monkeypatch.setattr(executor, "_terminate_process", checked_terminate)

    await asyncio.wait_for(executor.execute_job(job_id), timeout=6)

    durable = manager.get_job(job_id)
    assert cleanup_snapshots
    assert all(state == JobState.COMPLETED for state, _, _ in cleanup_snapshots)
    assert all(isinstance(result, dict) for _, result, _ in cleanup_snapshots)
    assert all(source == "session_task_complete" for _, _, source in cleanup_snapshots)
    assert durable.result["summary"] == "EXACT_SESSION_REPORT_FIRST"
    assert durable.terminal_source == "session_task_complete"


@pytest.mark.asyncio
async def test_stdout_turn_completed_is_terminal_even_when_wrapper_exits_nonzero(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "nonzero wrapper after completion", config["repositories"]["default"], {}
    )
    result = full_result("STDOUT_TERMINAL_NONZERO")
    script = (
        "import json,sys\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':json.dumps({result!r})}}}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
        "sys.exit(7)\n"
    )
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )

    await asyncio.wait_for(executor.execute_job(job_id), timeout=5)

    durable = manager.get_job(job_id)
    assert durable.state == JobState.COMPLETED
    assert durable.result["summary"] == "STDOUT_TERMINAL_NONZERO"
    assert durable.exit_code == 7
    assert durable.terminal_source == "stdout_turn_completed"


@pytest.mark.asyncio
async def test_result_parser_failure_after_turn_completed_preserves_completion(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "parser failure after completion", config["repositories"]["default"], {}
    )
    result = full_result("PARSER_FAILURE_RECOVERED")
    script = (
        "import json\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':json.dumps({result!r})}}}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
    )
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )

    async def fail_parser(*args, **kwargs):
        raise RuntimeError("fixture parser crash")

    monkeypatch.setattr(executor, "_parse_result", fail_parser)

    await asyncio.wait_for(executor.execute_job(job_id), timeout=5)

    durable = manager.get_job(job_id)
    assert durable.state == JobState.COMPLETED
    assert durable.terminal_source == "stdout_turn_completed"
    assert durable.result["summary"] == "PARSER_FAILURE_RECOVERED"
    assert durable.result["completion_evidence_recovered"] is True


@pytest.mark.asyncio
async def test_lingering_wrapper_promotes_stdout_completion_after_bounded_grace(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    config["server"]["codex_post_completion_exit_grace_seconds"] = 0.1
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "stdout completion with lingering wrapper", config["repositories"]["default"], {}
    )
    result = full_result("LINGERING_STDOUT_COMPLETED")
    script = (
        "import json,time\n"
        f"print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':json.dumps({result!r})}}}}), flush=True)\n"
        "print(json.dumps({'type':'turn.completed'}), flush=True)\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )

    started = time.monotonic()
    await asyncio.wait_for(executor.execute_job(job_id), timeout=6)

    durable = manager.get_job(job_id)
    assert time.monotonic() - started < 6
    assert durable.state == JobState.COMPLETED
    assert durable.terminal_source == "stdout_turn_completed"
    assert durable.result["summary"] == "LINGERING_STDOUT_COMPLETED"


def test_restart_promotes_durable_stdout_completion_evidence(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "crash after completion", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING, started_at=time.time() - 60)
    state = {"semantic_terminal_seen": False}
    result = full_result("DURABLE_STDOUT_RECOVERY")

    executor._observe_stdout_event(
        job_id,
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(result),
                    },
                }
            )
            + "\n"
        ).encode(),
        state,
    )
    executor._observe_stdout_event(
        job_id,
        b'{"type":"turn.completed"}\n',
        state,
    )
    before_restart = manager.get_job(job_id)
    assert before_restart.state == JobState.RUNNING
    assert before_restart.terminal_source is None
    assert before_restart.completion_evidence_source == "stdout_turn_completed"

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    outcome = restarted.reconcile_stale_running_jobs(grace_seconds=0)

    durable = restarted_manager.get_job(job_id)
    assert outcome["recovered_completed"] == 1
    assert durable.state == JobState.COMPLETED
    assert durable.terminal_source == "stdout_turn_completed"
    assert durable.result["summary"] == "DURABLE_STDOUT_RECOVERY"
    assert durable.result["detailed_report"] == (
        "Completed the bounded test assignment."
    )
    assert durable.result["evidence"] == ["fixture evidence"]
    assert durable.result["tests_run"] == ["fixture"]
    assert durable.result["completion_evidence_recovered"] is True
    assert durable.result["report_completeness"] == "recovered"


def test_restart_prefers_exact_session_report_over_stdout_completion_evidence(
    tmp_path,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "exact report wins", config["repositories"]["default"], {}
    )
    session_id = "session-exact-over-stdout"
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=time.time() - 60,
        process_started_at=time.time() - 60,
        session_id=session_id,
    )
    state = {"semantic_terminal_seen": False, "session_id": session_id}
    executor._observe_stdout_event(
        job_id,
        b'{"type":"turn.completed"}\n',
        state,
    )
    write_session(
        tmp_path / "codex-home",
        session_id,
        [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": json.dumps(
                        full_result("EXACT_SESSION_WINS")
                    ),
                },
            }
        ],
    )

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    outcome = restarted.reconcile_stale_running_jobs(grace_seconds=0)

    durable = restarted_manager.get_job(job_id)
    assert outcome["recovered_completed"] == 1
    assert durable.state == JobState.COMPLETED
    assert durable.terminal_source == "session_task_complete"
    assert durable.result["summary"] == "EXACT_SESSION_WINS"


def test_restart_recovers_turn_completed_without_agent_message(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "completion only", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING, started_at=time.time() - 60)
    executor._observe_stdout_event(
        job_id,
        b'{"type":"turn.completed"}\n',
        {"semantic_terminal_seen": False},
    )

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    restarted.reconcile_stale_running_jobs(grace_seconds=0)

    durable = restarted_manager.get_job(job_id)
    assert durable.state == JobState.COMPLETED
    assert durable.terminal_source == "stdout_turn_completed"
    assert "restarted before" in durable.result["summary"]


def test_unknown_completion_evidence_version_is_not_promoted(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "unknown evidence", config["repositories"]["default"], {}
    )
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=time.time() - 60,
        last_heartbeat_at=time.time() - 60,
    )
    manager.record_completion_evidence(
        job_id,
        source="stdout_turn_completed",
        observed_at=time.time() - 30,
        fallback_result=full_result("MUST_NOT_PROMOTE"),
        result_status="structured",
    )
    manager.update_job_state(
        job_id, JobState.RUNNING, completion_evidence_version=99
    )

    restarted_manager = JobManager(config)
    outcome = JobExecutor(config, restarted_manager).reconcile_stale_running_jobs(
        grace_seconds=0
    )

    durable = restarted_manager.get_job(job_id)
    assert outcome["recovered_completed"] == 0
    assert durable.state == JobState.FAILED
    assert durable.result["summary"] != "MUST_NOT_PROMOTE"


def test_result_artifact_replace_failure_preserves_previous_valid_json(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    result_file = tmp_path / "logs" / "jobs" / "atomic-result.json"
    executor._write_result_file(
        result_file, {"summary": "before", "files_changed": []}
    )
    original_replace = os.replace

    def fail_target_replace(source, destination):
        if Path(destination) == result_file:
            raise OSError("fixture replace interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_target_replace)

    with pytest.raises(OSError, match="fixture replace interruption"):
        executor._write_result_file(
            result_file, {"summary": "after", "files_changed": []}
        )

    assert json.loads(result_file.read_text(encoding="utf-8"))["summary"] == "before"
    assert not list(result_file.parent.glob(f".{result_file.name}.*.tmp"))


def test_persisted_scan_uncertainty_does_not_keep_stale_running_job_alive_forever(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan",
        "stale uncertain runtime",
        config["repositories"]["default"],
        {"_job_descendant_scan_uncertain_v1": True},
    )
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=time.time() - 120,
        process_started_at=time.time() - 120,
        process_pid=999_999_991,
        process_pgid=999_999_991,
    )
    executor = JobExecutor(config, manager)
    monkeypatch.setattr(executor, "_process_parent_snapshot", lambda **kwargs: {})
    monkeypatch.setattr(executor, "_process_group_liveness", lambda _pgid: False)

    first = executor.reconcile_stale_running_jobs(grace_seconds=0)
    first_job = manager.get_job(job_id)
    first_state = first_job.state
    first_cleanup = first_job.wrapper_cleanup_outcome
    second = executor.reconcile_stale_running_jobs(grace_seconds=0)

    assert first["reconciled"] == 1
    assert first_state == JobState.FAILED
    assert first_cleanup == "cleanup_blocked_untrusted_process_identity"
    assert second["cleanup_reconciled"] == 1
    assert manager.get_job(job_id).wrapper_cleanup_outcome == (
        "process_not_live_after_terminal"
    )


@pytest.mark.asyncio
async def test_missing_supervisor_proof_retains_repo_lock_until_proof_exists(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    repo = config["repositories"]["default"]
    proof = tmp_path / "logs" / "jobs" / "missing-supervisor.proof"
    options = mark_repo_lock_options(
        {
            "_job_process_supervisor_version": 2,
            "_job_process_supervisor_cleanup_proof": str(proof),
        },
        operation="shared_write_test",
    )
    job_id = manager.create_job("plan", "proof gate", repo, options)
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=time.time() - 60,
        process_started_at=time.time() - 60,
        process_pid=999_999_992,
        process_pgid=999_999_992,
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("PROOF_GATED"),
        terminal_source="stdout_turn_completed",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    restarted = JobExecutor(config, JobManager(config))
    monkeypatch.setattr(restarted, "_process_group_liveness", lambda _pgid: False)
    monkeypatch.setattr(restarted, "_job_marked_process_pids", lambda *args, **kwargs: set())

    blocked = restarted.reconcile_stale_running_jobs(grace_seconds=0)
    with pytest.raises(RepoMutationBusy):
        await restarted.repo_locks.acquire(repo, operation="must_wait_for_proof")

    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text("patchbay-supervisor-cleanup-v2:999999992\n", encoding="ascii")
    released = restarted.reconcile_stale_running_jobs(grace_seconds=0)
    lease = await restarted.repo_locks.acquire(repo, operation="after_proof")
    lease.release()

    assert blocked["cleanup_reconciled"] == 0
    assert released["cleanup_reconciled"] == 1


@pytest.mark.asyncio
async def test_invalid_utf8_is_tolerated_and_reaps_child_before_releasing_lock(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="shared_write_test")
    job_id = manager.create_job(
        "plan",
        "invalid stderr",
        repo,
        mark_repo_lock_options({}, operation="shared_write_test"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    child_pid_file = tmp_path / "invalid-utf8-child.pid"
    script = f"""
import json, os, pathlib, subprocess, sys
child = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(30)'],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding='utf-8')
os.write(2, b'\\xff')
"""
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )
    await asyncio.wait_for(executor.execute_job(job_id), timeout=4)

    durable = manager.get_job(job_id)
    assert durable.state == JobState.COMPLETED
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    assert executor._process_pid_is_live(child_pid) is False
    cleanup_outcome = await settle_darwin_supervisor_uncertainty(
        executor, manager, job_id
    )
    durable = manager.get_job(job_id)
    if cleanup_outcome == "cleanup_blocked_untrusted_process_identity":
        assert terminal_cleanup_pending(durable.wrapper_cleanup_outcome)
    else:
        assert not terminal_cleanup_pending(durable.wrapper_cleanup_outcome)
    next_lease = await executor.repo_locks.acquire(repo, operation="after_reap")
    next_lease.release()


@pytest.mark.asyncio
async def test_reader_failure_after_process_exit_is_not_silently_swallowed(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    job_id = manager.create_job("plan", "reader failure", repo, {})
    manager.update_job_state(job_id, JobState.RUNNING)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "pass",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    original_read = process.stdout.read

    async def fail_after_exit(size=-1):
        data = await original_read(size)
        if not data:
            # Finish after process.wait() and the live reader-check loop so the
            # final-drain exception path is exercised deterministically.
            await asyncio.sleep(0.7)
            raise OSError("delayed stdout drain failure")
        return data

    process.stdout.read = fail_after_exit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="stdout reader failed after process exit"):
        await executor._communicate_with_progress(
            job_id,
            process,
            stdin_data=None,
            total_timeout=5,
            session_start_timeout=None,
            expect_session=False,
        )


@pytest.mark.asyncio
async def test_semantic_completion_is_durable_before_wrapper_cleanup_finishes(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="shared_write_test")
    job_id = manager.create_job(
        "plan",
        "persist first",
        repo,
        mark_repo_lock_options({}, operation="shared_write_test"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    session_id = "session-durable-before-cleanup"
    session_file = (
        tmp_path
        / "codex-home"
        / "sessions"
        / "2026"
        / "07"
        / "11"
        / f"rollout-test-{session_id}.jsonl"
    )
    final_result = {"summary": "DURABLE_BEFORE_CLEANUP", "files_changed": []}
    script = f"""
import json, pathlib, time
path = pathlib.Path({str(session_file)!r})
path.parent.mkdir(parents=True, exist_ok=True)
now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
path.write_text(json.dumps({{'timestamp': now, 'type': 'session_meta', 'payload': {{'id': {session_id!r}, 'cwd': '/fixture'}}}}) + '\\n', encoding='utf-8')
print(json.dumps({{'type': 'thread.started', 'thread_id': {session_id!r}}}), flush=True)
time.sleep(0.2)
with path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'task_complete', 'last_agent_message': json.dumps({final_result!r})}}}}) + '\\n')
time.sleep(30)
"""
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    original_terminate = executor._terminate_process
    original_wait_for_exit = executor._wait_for_process_group_exit
    delayed_once = False

    async def delayed_supervisor_proof(
        cleanup_job_id, process, pgid, *, timeout
    ):
        nonlocal delayed_once
        if not delayed_once:
            delayed_once = True
            proof_delay = min(
                8.1, cleanup_proof_budget_seconds() - 0.5
            )
            assert timeout >= proof_delay
            await asyncio.sleep(proof_delay)
            timeout -= proof_delay
        return await original_wait_for_exit(
            cleanup_job_id, process, pgid, timeout=timeout
        )

    async def held_terminate(job_id, process, **kwargs):
        cleanup_started.set()
        await allow_cleanup.wait()
        return await original_terminate(job_id, process, **kwargs)

    monkeypatch.setattr(executor, "_terminate_process", held_terminate)
    monkeypatch.setattr(
        executor, "_wait_for_process_group_exit", delayed_supervisor_proof
    )
    execution_task = asyncio.create_task(executor.execute_job(job_id))

    await asyncio.wait_for(cleanup_started.wait(), timeout=3)
    durable = manager.get_job(job_id)
    liveness = executor._runtime_liveness(job_id)
    assert durable.state == JobState.COMPLETED
    assert durable.result["summary"] == "DURABLE_BEFORE_CLEANUP"
    assert durable.wrapper_cleanup_outcome == "cleanup_pending"
    assert execution_task.done() is False
    assert liveness["executor_task_alive"] is True
    assert liveness["process_alive"] is True
    reconciliation = executor.reconcile_stale_running_jobs(grace_seconds=0)
    assert reconciliation["cleanup_reconciled"] == 0
    assert manager.get_job(job_id).wrapper_cleanup_outcome == "cleanup_pending"
    assert execution_task.done() is False
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="overlapping_mutation")

    allow_cleanup.set()
    # The supervisor owns descendant discovery and cleanup proof. Under
    # concurrent host load it is intentionally allowed to outlive the old
    # sub-second wrapper deadline rather than being killed before it can prove
    # the process tree empty.
    await asyncio.wait_for(execution_task, timeout=12)
    assert manager.get_job(job_id).wrapper_cleanup_outcome == "terminated_after_terminal"
    next_lease = await executor.repo_locks.acquire(repo, operation="next_mutation")
    next_lease.release()


@pytest.mark.asyncio
async def test_proven_terminal_cleanup_is_monotonic_across_stale_liveness_race(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    leases = {}
    published_after_release = []
    stale_liveness_calls = 0
    original_transition = manager.transition_job_terminal

    def stale_liveness(*args, **kwargs):
        nonlocal stale_liveness_calls
        stale_liveness_calls += 1
        return True

    def transition_with_release_check(job_id, state, **kwargs):
        if kwargs.get("wrapper_cleanup_outcome") == "terminated_after_terminal":
            assert leases[job_id].released is True
            published_after_release.append(job_id)
        return original_transition(job_id, state, **kwargs)

    monkeypatch.setattr(
        executor, "_tracked_process_or_group_liveness", stale_liveness
    )
    monkeypatch.setattr(
        manager, "transition_job_terminal", transition_with_release_check
    )

    for iteration in range(50):
        lease = await executor.repo_locks.acquire(
            repo, operation=f"cleanup-race-{iteration}"
        )
        job_id = manager.create_job(
            "plan",
            f"cleanup race {iteration}",
            repo,
            mark_repo_lock_options({}, operation=f"cleanup-race-{iteration}"),
        )
        leases[job_id] = lease
        executor.repo_locks.bind_to_job(job_id, lease)
        manager.update_job_state(job_id, JobState.RUNNING)
        original_transition(
            job_id,
            JobState.COMPLETED,
            result=full_result(f"RACE_{iteration}"),
            terminal_source="session_task_complete",
            wrapper_cleanup_outcome="cleanup_pending",
        )
        process = type(
            "ReapedProcess", (), {"pid": 900_000 + iteration, "returncode": 0}
        )()
        executor.processes[job_id] = process

        retained = executor._retain_or_release_terminal_cleanup(
            job_id,
            process,
            cleanup_reaped=True,
            cleanup_complete_outcome="terminated_after_terminal",
        )
        executor._transition_job_terminal_with_cleanup(
            job_id,
            JobState.COMPLETED,
            wrapper_cleanup_outcome="cleanup_retry_pending_process_live",
        )
        executor.processes[job_id] = process
        stale_retained = executor._retain_or_release_terminal_cleanup(
            job_id, process
        )

        assert retained is False
        assert stale_retained is False
        assert lease.released is True
        assert job_id not in executor.processes
        assert job_id not in executor.cleanup_tasks
        assert manager.get_job(job_id).wrapper_cleanup_outcome == (
            "terminated_after_terminal"
        )
        probe = await executor.repo_locks.acquire(
            repo, operation=f"after-cleanup-race-{iteration}"
        )
        probe.release()

    assert stale_liveness_calls == 0
    assert len(published_after_release) == 50


@pytest.mark.asyncio
async def test_cleanup_timeout_keeps_process_and_repo_lock_owned_until_async_reap(
    tmp_path,
    monkeypatch,
):
    config = make_config(tmp_path)
    config["server"]["codex_post_completion_cleanup_timeout_seconds"] = 0.1
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    lease = await executor.repo_locks.acquire(repo, operation="shared_write_timeout")
    job_id = manager.create_job(
        "plan",
        "cleanup timeout",
        repo,
        mark_repo_lock_options({}, operation="shared_write_timeout"),
    )
    executor.repo_locks.bind_to_job(job_id, lease)
    session_id = "session-cleanup-timeout"
    session_file = (
        tmp_path
        / "codex-home"
        / "sessions"
        / "2026"
        / "07"
        / "11"
        / f"rollout-test-{session_id}.jsonl"
    )
    result = full_result("CLEANUP_TIMEOUT_DURABLE")
    script = f"""
import json, pathlib, time
path = pathlib.Path({str(session_file)!r})
path.parent.mkdir(parents=True, exist_ok=True)
now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
path.write_text(json.dumps({{'timestamp': now, 'type': 'session_meta', 'payload': {{'id': {session_id!r}, 'cwd': '/fixture'}}}}) + '\\n', encoding='utf-8')
print(json.dumps({{'type': 'thread.started', 'thread_id': {session_id!r}}}), flush=True)
time.sleep(0.1)
with path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({{'timestamp': now, 'type': 'event_msg', 'payload': {{'type': 'task_complete', 'last_agent_message': json.dumps({result!r})}}}}) + '\\n')
time.sleep(30)
"""
    monkeypatch.setattr(
        executor,
        "_build_codex_command",
        lambda *args, **kwargs: [sys.executable, "-u", "-c", script],
    )
    permit_reap = asyncio.Event()
    async_cleanup_started = asyncio.Event()
    original_terminate = executor._terminate_process
    calls = 0

    async def delayed_terminate(cleanup_job_id, process, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(5)
        async_cleanup_started.set()
        await permit_reap.wait()
        return await original_terminate(cleanup_job_id, process, **kwargs)

    monkeypatch.setattr(executor, "_terminate_process", delayed_terminate)

    await asyncio.wait_for(executor.execute_job(job_id), timeout=4)
    await asyncio.wait_for(async_cleanup_started.wait(), timeout=2)
    durable = manager.get_job(job_id)
    assert durable.state == JobState.COMPLETED
    assert durable.result["summary"] == "CLEANUP_TIMEOUT_DURABLE"
    assert terminal_cleanup_pending(durable.wrapper_cleanup_outcome)
    with pytest.raises(RepoMutationBusy):
        await executor.repo_locks.acquire(repo, operation="overlapping_mutation")

    permit_reap.set()
    await asyncio.wait_for(executor.cleanup_tasks[job_id], timeout=3)
    assert manager.get_job(job_id).wrapper_cleanup_outcome == (
        "terminated_after_terminal_async"
    )
    next_lease = await executor.repo_locks.acquire(repo, operation="next_mutation")
    next_lease.release()


def test_session_terminal_json_is_only_certified_when_full_schema_matches(tmp_path):
    config = make_config(tmp_path)
    executor = JobExecutor(config, JobManager(config))

    incomplete = executor._result_from_session_message(
        json.dumps(
            {
                "summary": "thin",
                "files_changed": [],
                "parsed_output_schema_valid": True,
                "final_structured_result": True,
            }
        ),
        tmp_path / "incomplete-result.json",
    )
    complete = executor._result_from_session_message(
        json.dumps(full_result("complete")),
        tmp_path / "complete-result.json",
    )

    assert incomplete["parsed_output_schema_valid"] is False
    assert incomplete["final_structured_result"] is False
    assert complete["parsed_output_schema_valid"] is True
    assert complete["final_structured_result"] is True


@pytest.mark.asyncio
async def test_codex_result_event_cannot_self_certify_invalid_payload(tmp_path):
    config = make_config(tmp_path)
    executor = JobExecutor(config, JobManager(config))
    supplied = {
        "summary": "thin",
        "files_changed": [],
        "parsed_output_schema_valid": True,
        "final_structured_result": True,
    }
    stdout = (json.dumps({"type": "result", "data": supplied}) + "\n").encode()

    result = await executor._parse_result(stdout, tmp_path / "result-event.json")

    assert result["parsed_output_schema_valid"] is False
    assert result["final_structured_result"] is False


@pytest.mark.asyncio
async def test_resume_ignores_prior_terminal_marker_and_waits_for_new_turn(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    session_id = "session-resume-without-thread-started"
    prior_timestamp = datetime.fromtimestamp(time.time() - 0.25, timezone.utc).isoformat()
    session_file = write_session(
        tmp_path / "codex-home",
        session_id,
        [
            {
                "timestamp": prior_timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": json.dumps(full_result("PRIOR_TURN_MUST_NOT_REPLAY")),
                },
            }
        ],
    )
    initial_size = session_file.stat().st_size
    final_result = full_result("RESUME_TERMINAL_OK")
    job_id = manager.create_job(
        "resume",
        "continue",
        config["repositories"]["default"],
        {"json_events": True, "resume_session_id": session_id},
    )
    script = f"""
import json, pathlib, subprocess, sys, time
path = pathlib.Path({str(session_file)!r})
time.sleep(1.0)
now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
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
    assert job.result["summary"] == "RESUME_TERMINAL_OK"
    assert job.session_id == session_id
    assert job.terminal_source == "session_task_complete"
    if sys.platform == "darwin":
        cleanup_outcome = await settle_darwin_supervisor_uncertainty(
            executor, manager, job_id
        )
        assert not terminal_cleanup_pending(cleanup_outcome)
    else:
        assert job.wrapper_cleanup_outcome == "terminated_after_terminal"
    assert job.options["_session_terminal_observation_session_id"] == session_id
    assert job.options["_session_terminal_observation_initial_offset"] == initial_size


def test_restart_recovery_uses_persisted_resume_observation_offset(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    session_id = "session-resume-restart-cursor"
    prior_timestamp = datetime.fromtimestamp(time.time() - 0.25, timezone.utc).isoformat()
    source = write_session(
        tmp_path / "codex-home",
        session_id,
        [
            {
                "timestamp": prior_timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": json.dumps(full_result("PRIOR_RESTART_TURN")),
                },
            }
        ],
    )
    job_id = manager.create_job(
        "resume",
        "continue after restart",
        config["repositories"]["default"],
        {"json_events": True, "resume_session_id": session_id},
    )
    job = manager.get_job(job_id)
    initial_offset = executor._prepare_session_observation(job_id, job)
    started_at = time.time()
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=started_at,
        process_started_at=started_at,
    )
    assert manager.get_job(job_id).session_id == session_id

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    restarted_job = restarted_manager.get_job(job_id)
    assert restarted_job.options["_session_terminal_observation_initial_offset"] == initial_offset
    assert restarted._recover_completed_session(restarted_job) is False

    now = datetime.now(timezone.utc).isoformat()
    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": now,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": json.dumps(full_result("NEW_RESTART_TURN")),
                    },
                }
            )
            + "\n"
        )

    assert restarted._recover_completed_session(restarted_job) is True
    recovered = restarted_manager.get_job(job_id)
    assert recovered.state == JobState.COMPLETED
    assert recovered.result["summary"] == "NEW_RESTART_TURN"


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


def test_cancellation_first_remains_cancelled_after_late_session_completion(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "cancel first", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)

    assert manager.transition_job_terminal(
        job_id,
        JobState.CANCELLED,
        terminal_source="manager_cancellation",
        terminal_observed_at=10,
        result={"summary": "cancelled", "files_changed": []},
    )
    assert not manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        terminal_source="session_task_complete",
        terminal_observed_at=11,
        result=full_result("TOO_LATE"),
    )

    durable = manager.get_job(job_id)
    assert durable.state == JobState.CANCELLED
    assert durable.terminal_source == "manager_cancellation"
    assert durable.result["summary"] == "cancelled"
    assert durable.late_terminal_source == "session_task_complete"


@pytest.mark.asyncio
async def test_durable_stdout_evidence_beats_manager_cancellation(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "completion before stop", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.record_completion_evidence(
        job_id,
        source="stdout_turn_completed",
        observed_at=time.time(),
        fallback_result=full_result("EVIDENCE_BEATS_CANCEL"),
        result_status="structured",
    )

    outcome = await executor.cancel_job(job_id, reason="manager stop")

    durable = manager.get_job(job_id)
    assert outcome["cancelled"] is False
    assert outcome["completed"] is True
    assert durable.state == JobState.COMPLETED
    assert durable.terminal_source == "stdout_turn_completed"
    assert durable.result["summary"] == "EVIDENCE_BEATS_CANCEL"


def test_exact_completed_report_cannot_be_overwritten_by_generic_state_updates(
    tmp_path,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "plan", "preserve exact report", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)
    exact = full_result("EXACT_REPORT")
    assert manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=exact,
        terminal_source="session_task_complete",
        terminal_observed_at=10,
        session_id="session-exact",
        exit_code=0,
    )

    manager.update_job_state(
        job_id,
        JobState.CANCELLED,
        result={"summary": "partial cancellation"},
        error="cancelled",
    )
    manager.update_job_state(
        job_id,
        JobState.COMPLETED,
        result={"summary": "thin replacement"},
        terminal_source="process_exit",
        exit_code=1,
        wrapper_cleanup_outcome="terminated_after_terminal",
    )

    job = manager.get_job(job_id)
    assert job.state == JobState.COMPLETED
    assert job.result == exact
    assert job.error is None
    assert job.terminal_source == "session_task_complete"
    assert job.session_id == "session-exact"
    assert job.exit_code == 0
    assert job.wrapper_cleanup_outcome == "terminated_after_terminal"


@pytest.mark.asyncio
async def test_cancel_race_cannot_replace_exact_completion_report(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "completion during cancel", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)
    exact = full_result("EXACT_DURING_CANCEL")

    def complete_during_probe(job, **kwargs):
        del job, kwargs
        assert manager.transition_job_terminal(
            job_id,
            JobState.COMPLETED,
            result=exact,
            terminal_source="session_task_complete",
            terminal_observed_at=time.time(),
            session_id="session-cancel-race",
            exit_code=0,
        )
        return False

    monkeypatch.setattr(executor, "_recover_completed_session", complete_during_probe)

    outcome = await executor.cancel_job(job_id, reason="late manager stop")

    job = manager.get_job(job_id)
    assert outcome["cancelled"] is False
    assert job.state == JobState.COMPLETED
    assert job.result == exact
    assert job.terminal_source == "session_task_complete"


def test_stale_reconciliation_cannot_replace_exact_completion_report(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "completion during stale scan", config["repositories"]["default"], {}
    )
    old = time.time() - 600
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=old,
        last_heartbeat_at=old,
    )
    exact = full_result("EXACT_DURING_STALE_RECONCILIATION")

    def complete_during_probe(job, **kwargs):
        del job, kwargs
        assert manager.transition_job_terminal(
            job_id,
            JobState.COMPLETED,
            result=exact,
            terminal_source="session_task_complete",
            terminal_observed_at=time.time(),
            session_id="session-stale-race",
            exit_code=0,
        )
        return False

    monkeypatch.setattr(executor, "_recover_completed_session", complete_during_probe)
    monkeypatch.setattr(executor, "_job_has_live_runtime", lambda _job_id: False)

    report = executor.reconcile_stale_running_jobs(grace_seconds=1, now=time.time())

    job = manager.get_job(job_id)
    assert report["reconciled"] == 0
    assert job.state == JobState.COMPLETED
    assert job.result == exact
    assert job.terminal_source == "session_task_complete"


@pytest.mark.asyncio
async def test_cancel_session_recovery_probe_does_not_block_event_loop(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "responsive cancel", config["repositories"]["default"], {}
    )
    manager.update_job_state(job_id, JobState.RUNNING)
    owning_loop = asyncio.get_running_loop()
    observed_loops: list[asyncio.AbstractEventLoop | None] = []

    def slow_recovery_probe(job, *, event_loop=None):
        observed_loops.append(event_loop)
        time.sleep(0.2)
        return False

    monkeypatch.setattr(executor, "_recover_completed_session", slow_recovery_probe)
    loop_advanced = asyncio.Event()

    async def heartbeat():
        await asyncio.sleep(0.02)
        loop_advanced.set()

    heartbeat_task = asyncio.create_task(heartbeat())
    cancel_task = asyncio.create_task(executor.cancel_job(job_id, reason="manager stop"))

    await asyncio.wait_for(loop_advanced.wait(), timeout=0.1)
    assert cancel_task.done() is False
    result = await cancel_task
    await heartbeat_task

    assert result["cancelled"] is True
    assert observed_loops == [owning_loop]


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


@pytest.mark.asyncio
async def test_cancellation_commit_is_not_reversed_by_late_session_file_write(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job(
        "plan", "race after probe", config["repositories"]["default"], {}
    )
    session_id = "session-completes-inside-cancel"
    started_at = time.time() - 5
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=started_at,
        process_started_at=started_at,
        session_id=session_id,
    )
    session_file = write_session(tmp_path / "codex-home", session_id, [])
    result = full_result("RACE_FINAL_REPORT")
    original_transition = executor._transition_job_terminal_with_cleanup
    injected = False

    def transition_after_report_arrives(job_id_arg, state, **kwargs):
        nonlocal injected
        if state == JobState.CANCELLED and not injected:
            injected = True
            now = datetime.now(timezone.utc).isoformat()
            with session_file.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": now,
                            "type": "event_msg",
                            "payload": {
                                "type": "task_complete",
                                "last_agent_message": json.dumps(result),
                            },
                        }
                    )
                    + "\n"
                )
        return original_transition(job_id_arg, state, **kwargs)

    monkeypatch.setattr(
        executor,
        "_transition_job_terminal_with_cleanup",
        transition_after_report_arrives,
    )

    outcome = await executor.cancel_job(
        job_id, reason="manager stop raced exact terminal event"
    )

    recovered = manager.get_job(job_id)
    assert injected is True
    assert outcome["cancelled"] is True
    assert recovered.state == JobState.CANCELLED
    assert recovered.terminal_source == "manager_cancellation"
    assert recovered.result["summary"] != "RACE_FINAL_REPORT"


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


def test_restart_recovery_establishes_cleanup_barrier_before_terminal_transition(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    repo = config["repositories"]["default"]
    job_id = manager.create_job(
        "plan",
        "recover with barrier",
        repo,
        mark_repo_lock_options({}, operation="shared_write_test"),
    )
    session_id = "session-recovery-barrier-order"
    started_at = time.time() - 60
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
        [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": json.dumps(full_result("BARRIER_ORDER")),
                },
            }
        ],
    )
    executor = JobExecutor(config, manager)
    original_transition = manager.transition_job_terminal
    barrier_state_at_transitions = []

    def transition_with_barrier_check(*args, **kwargs):
        if args[1] == JobState.COMPLETED:
            barrier_state_at_transitions.append(
                bool(executor.repo_locks._cleanup_job_repos.get(job_id))
            )
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(manager, "transition_job_terminal", transition_with_barrier_check)

    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)

    assert outcome["recovered_completed"] == 1
    assert barrier_state_at_transitions[0] is True
    assert manager.get_job(job_id).state == JobState.COMPLETED
    assert executor.repo_locks.shutdown(timeout=1)


@pytest.mark.asyncio
async def test_restart_cleanup_holds_mutation_barrier_until_recorded_process_dies(
    tmp_path,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    repo = config["repositories"]["default"]
    options = mark_repo_lock_options({}, operation="shared_write_test")
    job_id = manager.create_job("plan", "cleanup", repo, options)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    started = time.time() - 5
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=started,
        process_started_at=started,
        process_pid=process.pid,
        process_pgid=process.pid,
        process_identity=executor._process_identity(process.pid),
    )
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("durable"),
        terminal_source="session_task_complete",
        terminal_observed_at=time.time(),
        wrapper_cleanup_outcome="cleanup_pending",
    )

    restarted_manager = JobManager(config)
    restarted = JobExecutor(config, restarted_manager)
    reconcile_started = time.monotonic()
    outcome = restarted.reconcile_stale_running_jobs(grace_seconds=0)

    assert outcome["cleanup_reconciled"] == 0
    assert time.monotonic() - reconcile_started < 0.5
    with pytest.raises(RepoMutationBusy):
        await restarted.repo_locks.acquire(repo, operation="premature_mutation")
    if manager.get_job(job_id).process_identity:
        deadline = time.monotonic() + 5
        while (
            restarted_manager.get_job(job_id).wrapper_cleanup_outcome
            != "terminated_after_terminal_recovery"
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        assert restarted_manager.get_job(job_id).wrapper_cleanup_outcome == (
            "terminated_after_terminal_recovery"
        )
        process.wait(timeout=3)
    else:
        assert restarted_manager.get_job(job_id).wrapper_cleanup_outcome == (
            "cleanup_blocked_untrusted_process_identity"
        )
        process.terminate()
        process.wait(timeout=3)
        assert restarted.reconcile_stale_running_jobs(grace_seconds=0)[
            "cleanup_reconciled"
        ] == 1
    lease = await restarted.repo_locks.acquire(repo, operation="next_mutation")
    lease.release()


@pytest.mark.asyncio
async def test_reconciliation_prioritizes_semantic_completion_over_stuck_wrapper_task(
    tmp_path,
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "recover wrapper", config["repositories"]["default"], {})
    session_id = "session-stuck-wrapper"
    started_at = time.time() - 60
    manager.update_job_state(
        job_id,
        JobState.RUNNING,
        started_at=started_at,
        process_started_at=started_at,
        session_id=session_id,
        terminal_source="session_task_complete",
        terminal_observed_at=time.time() - 30,
        last_event="session_task_complete",
    )
    write_session(
        tmp_path / "codex-home",
        session_id,
        [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": json.dumps(
                        {"summary": "WRAPPER_RECOVERED", "files_changed": []}
                    ),
                },
            }
        ],
    )
    wrapper_task = asyncio.create_task(asyncio.Event().wait())
    executor.tasks[job_id] = wrapper_task

    outcome = executor.reconcile_stale_running_jobs(grace_seconds=0)

    await asyncio.sleep(0)
    await asyncio.gather(wrapper_task, return_exceptions=True)
    recovered = manager.get_job(job_id)
    assert outcome["recovered_completed"] == 1
    assert recovered.state == JobState.COMPLETED
    assert recovered.result["summary"] == "WRAPPER_RECOVERED"
    assert wrapper_task.cancelled() is True
    assert executor._runtime_liveness(job_id)["runtime_alive"] is False


@pytest.mark.asyncio
async def test_threaded_reconciliation_marshals_task_cancellation_to_owner_loop(
    tmp_path
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "threaded cleanup", config["repositories"]["default"], {})
    manager.update_job_state(job_id, JobState.RUNNING, started_at=time.time() - 60)
    manager.transition_job_terminal(
        job_id,
        JobState.COMPLETED,
        result=full_result("THREADSAFE_CANCEL"),
        terminal_source="session_task_complete",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    owner_thread = threading.get_ident()

    class FakeTask:
        cancel_thread = None

        def done(self):
            return False

        def cancel(self):
            self.cancel_thread = threading.get_ident()

    class RecordingLoop:
        def __init__(self):
            self.callbacks = []

        def call_soon_threadsafe(self, callback, *args):
            self.callbacks.append((callback, args))

    task = FakeTask()
    loop = RecordingLoop()
    executor.tasks[job_id] = task

    reconciled = await asyncio.to_thread(
        executor._reconcile_terminal_cleanup,
        manager.get_job(job_id),
        event_loop=loop,
    )

    assert reconciled is False
    assert task.cancel_thread is None
    assert len(loop.callbacks) == 1
    assert manager.get_job(job_id).wrapper_cleanup_outcome == "cleanup_pending"
    callback, args = loop.callbacks.pop()
    callback(*args)
    assert task.cancel_thread == owner_thread

    task.done = lambda: True
    reconciled_after_ack = await asyncio.to_thread(
        executor._reconcile_terminal_cleanup,
        manager.get_job(job_id),
        event_loop=loop,
    )

    assert reconciled_after_ack is True
    assert manager.get_job(job_id).wrapper_cleanup_outcome == (
        "process_not_live_after_terminal"
    )


@pytest.mark.asyncio
async def test_runtime_liveness_does_not_call_executor_task_a_process(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "task only", config["repositories"]["default"], {})
    wrapper_task = asyncio.create_task(asyncio.Event().wait())
    executor.tasks[job_id] = wrapper_task

    liveness = executor._runtime_liveness(job_id)

    assert liveness["executor_task_alive"] is True
    assert liveness["process_alive"] is False
    assert liveness["runtime_alive"] is True
    wrapper_task.cancel()
    await asyncio.gather(wrapper_task, return_exceptions=True)


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
