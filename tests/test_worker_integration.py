import asyncio
import subprocess
from pathlib import Path

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.ownership import OWNER_CLIENT_REF_OPTION
from patchbay.protocol.context import RequestContext
from patchbay.tools.handler import ToolHandler
from patchbay.workers.runtime import WorkerRuntime


def init_repo(repo: Path) -> None:
    repo.mkdir()
    (repo / "README.md").write_text("# worker integration\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Test", "-c", "user.email=worker-test@example.invalid", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def make_config(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    return {
        "server": {"max_concurrent_jobs": 3, "job_timeout_seconds": 30, "job_cleanup_after_hours": 24},
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "workers": {"worktree_root": str(tmp_path / "worker-worktrees")},
        "security": {
            "require_git_repo": True,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".env", ".env.*", "**/.env", "**/.env.*", ".git", ".git/**", "**/.git/**", "**/*secret*"],
            "max_diff_bytes": 200_000,
        },
        "power_tools": {"direct_write": False, "bash_mode": "off"},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "locks": {"root": str(tmp_path / "locks")},
    }


class RecordingExecutor(JobExecutor):
    def __init__(self, config, manager):
        super().__init__(config, manager)
        self.started = []

    async def execute_job(self, job_id):
        self.started.append(job_id)


def request_context(client_ref: str, label: str = "") -> RequestContext:
    return RequestContext(transport_session_id=f"session-{client_ref}", client_ref=client_ref, client_label=label)


@pytest.mark.asyncio
async def test_integration_preview_and_apply_worker_result_to_base_checkout(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(name="Implementer", brief="Create the note file.", repo_path=config["repositories"]["default"])
    await asyncio.sleep(0)
    first_job = manager.get_job(started["worker_id"].replace("wrk_", ""))
    assert first_job is None  # worker ids are not job ids
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    worker_root = Path(job.worktree_path)
    (worker_root / "worker-note.txt").write_text("from worker\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created worker-note.txt"}, session_id="session-1")

    preview = await runtime.inspect_worker(worker="Implementer", view="integration_preview")
    assert preview["can_apply"] is True
    assert preview["apply_check"] == "clean"
    assert preview["changed_files"] == ["worker-note.txt"]
    assert str(worker_root) not in str(preview)
    assert config["repositories"]["default"] not in str(preview)
    assert not (Path(config["repositories"]["default"]) / "worker-note.txt").exists()

    file_view = await runtime.inspect_worker(worker="Implementer", view="file", file_path="worker-note.txt")
    assert file_view["source"] == "worker_workspace"
    assert file_view["exists"] is True
    assert "1 | from worker" in file_view["text"]
    assert str(worker_root) not in str(file_view)
    assert config["repositories"]["default"] not in str(file_view)

    applied = await runtime.integrate_worker(worker="Implementer")
    assert applied["applied"] is True
    assert applied["integration_state"] == "applied_to_checkout"
    assert (Path(config["repositories"]["default"]) / "worker-note.txt").read_text(encoding="utf-8") == "from worker\n"
    assert (worker_root / "worker-note.txt").exists()
    assert str(worker_root) not in str(applied)
    assert config["repositories"]["default"] not in str(applied)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED],
)
async def test_integration_waits_for_internal_terminal_wrapper_cleanup(tmp_path, terminal_state):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Cleanup Pending Implementer",
        brief="Create the note file.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)
    job = next(
        job
        for job in manager.jobs.values()
        if (job.options or {}).get("_worker_id") == started["worker_id"]
    )
    (Path(job.worktree_path) / "worker-note.txt").write_text(
        "from worker\n", encoding="utf-8"
    )
    manager.update_job_state(
        job.job_id,
        terminal_state,
        result={"summary": "Created worker-note.txt"} if terminal_state == JobState.COMPLETED else None,
        error="Worker turn failed." if terminal_state == JobState.FAILED else None,
        session_id="session-cleanup-pending",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    executor.processes[job.job_id] = type(
        "UnverifiedLiveProcess", (), {"returncode": None}
    )()

    preview = await runtime.inspect_worker(
        worker="Cleanup Pending Implementer", view="integration_preview"
    )
    applied = await runtime.integrate_worker(worker="Cleanup Pending Implementer")

    assert preview["can_apply"] is False
    assert preview["cleanup_pending"] is True
    assert applied["applied"] is False
    assert applied["cleanup_pending"] is True
    assert not (Path(config["repositories"]["default"]) / "worker-note.txt").exists()
    executor.processes.pop(job.job_id, None)


@pytest.mark.asyncio
async def test_integration_and_followup_refuse_independent_live_runtime_evidence(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Live Runtime Guard",
        brief="Create the note file.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)
    job = next(
        item
        for item in manager.jobs.values()
        if (item.options or {}).get("_worker_id") == started["worker_id"]
    )
    (Path(job.worktree_path) / "worker-note.txt").write_text(
        "from live runtime\n", encoding="utf-8"
    )
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "Report is durable."},
        session_id="session-live-runtime-guard",
        wrapper_cleanup_outcome="process_exited",
    )
    monkeypatch.setattr(
        executor,
        "runtime_liveness_snapshot",
        lambda _job_id: {
            "executor_task_alive": True,
            "process_alive": True,
            "runtime_alive": True,
        },
    )

    preview = await runtime.inspect_worker(
        worker="Live Runtime Guard", view="integration_preview"
    )
    applied = await runtime.integrate_worker(worker="Live Runtime Guard")
    followup = await runtime.message_worker(
        worker="Live Runtime Guard", message="Continue with another turn."
    )

    assert preview["can_apply"] is False
    assert preview["cleanup_unresolved"] is True
    assert applied["applied"] is False
    assert applied["cleanup_unresolved"] is True
    assert followup["accepted"] is False
    assert followup["cleanup_unresolved"] is True
    assert not (
        Path(config["repositories"]["default"]) / "worker-note.txt"
    ).exists()


@pytest.mark.asyncio
async def test_worker_followup_waits_for_terminal_cleanup_then_allows_one_resume(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Cleanup Pending Messenger",
        brief="Complete the first turn.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)
    first_job = next(
        job
        for job in manager.jobs.values()
        if (job.options or {}).get("_worker_id") == started["worker_id"]
    )
    manager.update_job_state(
        first_job.job_id,
        JobState.COMPLETED,
        result={"summary": "First turn complete."},
        session_id="session-cleanup-message",
        wrapper_cleanup_outcome="cleanup_pending",
    )
    executor.processes[first_job.job_id] = type(
        "UnverifiedLiveProcess", (), {"returncode": None}
    )()
    newer_job_id = manager.create_job(
        "resume",
        "Already durable newer turn",
        first_job.repo_path,
        dict(first_job.options or {}),
    )
    manager.update_job_state(
        newer_job_id,
        JobState.COMPLETED,
        result={"summary": "Newer turn has no pending cleanup."},
        session_id="session-cleanup-message",
        wrapper_cleanup_outcome="process_exited",
    )

    blocked = await runtime.message_worker(
        worker="Cleanup Pending Messenger",
        message="Continue with the second turn.",
    )
    projection = runtime.projection_snapshot()["workers"][0]

    assert blocked["accepted"] is False
    assert blocked["cleanup_pending"] is True
    assert blocked["recommended_next_action"] == "retry_codex_worker_message"
    assert blocked["can_message"] is False
    assert blocked["can_message_reason"] == "terminal_cleanup_pending"
    assert projection["can_message"] is False
    assert projection["cleanup_pending"] is True
    assert len(runtime._jobs_for_worker(started["worker_id"])) == 2

    executor.processes.pop(first_job.job_id, None)
    manager.update_job_state(
        first_job.job_id,
        JobState.COMPLETED,
        wrapper_cleanup_outcome="terminated_after_terminal_async",
    )
    resumed = await runtime.message_worker(
        worker="Cleanup Pending Messenger",
        message="Continue with the second turn.",
    )
    duplicate = await runtime.message_worker(
        worker="Cleanup Pending Messenger",
        message="Do not create a third concurrent turn.",
    )

    assert resumed["accepted"] is True
    assert duplicate["accepted"] is False
    assert len(runtime._jobs_for_worker(started["worker_id"])) == 3


@pytest.mark.asyncio
async def test_untrusted_persisted_process_requires_recovery_in_public_state(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)
    started = await runtime.start_worker(
        name="Recovery Required Worker",
        brief="Complete one turn.",
        repo_path=config["repositories"]["default"],
    )
    job = runtime._jobs_for_worker(started["worker_id"])[0]
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "Report is durable."},
        session_id="session-recovery-required",
        process_pid=454545,
        process_pgid=454545,
        wrapper_cleanup_outcome="cleanup_blocked_untrusted_process_identity",
    )
    executor._process_pid_is_live = lambda pid: False
    executor._process_identity = lambda pid: None
    executor._process_group_members_from_proc = lambda pgid: None
    executor._process_group_members_from_ps = lambda pgid: {454546}
    executor._job_marked_process_pids = lambda job_id: None

    blocked = await runtime.message_worker(
        worker="Recovery Required Worker",
        message="Start another turn.",
    )

    assert blocked["accepted"] is False
    assert blocked["cleanup_pending"] is True
    assert blocked["recovery_required"] is True
    assert blocked["cleanup_recovery_required"] is True
    assert blocked["recommended_next_action"] == (
        "report_patchbay_cleanup_recovery_required"
    )
    assert blocked["can_message_reason"] == "terminal_cleanup_recovery_required"


@pytest.mark.asyncio
async def test_integration_requires_takeover_for_other_owner(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)
    client_a = request_context("client_a", "Chat A")
    client_b = request_context("client_b", "Chat B")

    started = await runtime.start_worker(
        name="Protected Implementer",
        brief="Create the note file.",
        repo_path=config["repositories"]["default"],
        request_context=client_a,
    )
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    worker_root = Path(job.worktree_path)
    (worker_root / "worker-note.txt").write_text("from protected worker\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created worker-note.txt"}, session_id="session-1")

    refused = await runtime.integrate_worker(worker="Protected Implementer", request_context=client_b)
    assert refused["applied"] is False
    assert refused["takeover_required"] is True
    assert refused["owned_by_current_client"] is False
    assert not (Path(config["repositories"]["default"]) / "worker-note.txt").exists()

    applied = await runtime.integrate_worker(
        worker="Protected Implementer",
        request_context=client_b,
        takeover=True,
        takeover_reason="User accepted the other chat's worker result.",
    )
    assert applied["applied"] is True
    assert applied["takeover_performed"] is True
    assert (Path(config["repositories"]["default"]) / "worker-note.txt").read_text(encoding="utf-8") == "from protected worker\n"
    assert manager.get_job(job.job_id).options[OWNER_CLIENT_REF_OPTION] == "client_b"


@pytest.mark.asyncio
async def test_integration_refuses_when_base_repo_mutation_lock_is_busy(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(name="Locked Implementer", brief="Create the note.", repo_path=config["repositories"]["default"])
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    worker_root = Path(job.worktree_path)
    (worker_root / "worker-note.txt").write_text("from locked worker\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created worker-note.txt"}, session_id="session-1")

    lease = await runtime.repo_locks.acquire(config["repositories"]["default"], operation="test_holder")
    try:
        refused = await runtime.integrate_worker(worker="Locked Implementer")
        isolated = await runtime.start_worker(name="Parallel Isolated", brief="Start safely.", repo_path=config["repositories"]["default"])
        shared = await runtime.start_worker(
            name="Shared Writer",
            brief="Write in base checkout.",
            repo_path=config["repositories"]["default"],
            workspace_mode="shared_write",
        )
    finally:
        lease.release()

    assert refused["repo_busy"] is True
    assert refused["applied"] is False
    assert not (Path(config["repositories"]["default"]) / "worker-note.txt").exists()
    assert isolated["accepted"] is True
    assert isolated["workspace_mode"] == "isolated_write"
    assert shared["accepted"] is False
    assert shared["repo_busy"] is True


@pytest.mark.asyncio
async def test_architect_can_start_multiple_manager_controlled_shared_writers(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    first = await runtime.start_worker(
        name="Shared Writer One",
        brief="Own the first bounded subsystem.",
        repo_path=config["repositories"]["default"],
        workspace_mode="shared_write",
        allow_concurrent_shared_write=True,
    )
    second = await runtime.start_worker(
        name="Shared Writer Two",
        brief="Own the second bounded subsystem.",
        repo_path=config["repositories"]["default"],
        workspace_mode="shared_write",
        allow_concurrent_shared_write=True,
    )
    await asyncio.sleep(0)

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert first["shared_write_concurrency"] == "manager_controlled"
    assert second["shared_write_concurrency"] == "manager_controlled"
    assert len(executor.started) == 2


@pytest.mark.asyncio
async def test_integration_refuses_dirty_base_by_default(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(name="Implementer", brief="Edit readme.", repo_path=config["repositories"]["default"])
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, "README.md").write_text("# worker version\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Edited README"}, session_id="session-1")

    Path(config["repositories"]["default"], "local.txt").write_text("local work\n", encoding="utf-8")

    preview = await runtime.inspect_worker(worker="Implementer", view="integration_preview")
    assert preview["can_apply"] is False
    assert preview["base_dirty"] is True
    assert "local.txt" in preview["base_changed_files"]

    applied = await runtime.integrate_worker(worker="Implementer")
    assert applied["applied"] is False
    assert "dirty" in applied["note"].lower()


@pytest.mark.asyncio
async def test_integration_accepts_named_dirty_base_patterns(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(name="Implementer", brief="Edit readme.", repo_path=config["repositories"]["default"])
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, "worker-note.txt").write_text("from worker\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created note"}, session_id="session-1")

    base = Path(config["repositories"]["default"])
    accepted = base / "dev" / "big_update"
    accepted.mkdir(parents=True)
    (accepted / "00-phase-one.md").write_text("accepted phase artifact\n", encoding="utf-8")

    blocked_preview = await runtime.inspect_worker(worker="Implementer", view="integration_preview")
    assert blocked_preview["can_apply"] is False
    assert blocked_preview["unexpected_base_changed_files"] == ["dev/big_update/00-phase-one.md"]

    preview = await runtime.inspect_worker(
        worker="Implementer",
        view="integration_preview",
        accepted_dirty_base=["dev/big_update/00-*.md"],
    )
    assert preview["can_apply"] is True
    assert preview["accepted_dirty_base_files"] == ["dev/big_update/00-phase-one.md"]
    assert preview["unexpected_base_changed_files"] == []

    applied = await runtime.integrate_worker(worker="Implementer", accepted_dirty_base=["dev/big_update/00-*.md"])
    assert applied["applied"] is True
    assert (base / "worker-note.txt").read_text(encoding="utf-8") == "from worker\n"


@pytest.mark.asyncio
async def test_isolated_worker_can_copy_selected_untracked_base_files(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    base = Path(config["repositories"]["default"])
    docs = base / "dev" / "big_update"
    docs.mkdir(parents=True)
    (docs / "00-phase-one.md").write_text("phase one context\n", encoding="utf-8")
    (docs / "scratch.md").write_text("not accepted\n", encoding="utf-8")

    started = await runtime.start_worker(
        name="Context Worker",
        brief="Use accepted docs.",
        repo_path=config["repositories"]["default"],
        include_untracked_from_base=["dev/big_update/00-*.md"],
    )
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])

    assert Path(job.worktree_path, "dev/big_update/00-phase-one.md").read_text(encoding="utf-8") == "phase one context\n"
    assert not Path(job.worktree_path, "dev/big_update/scratch.md").exists()
    assert job.options["_worker_included_untracked_base_files"] == ["dev/big_update/00-phase-one.md"]
    assert job.options["_worker_included_untracked_base_digests"]["dev/big_update/00-phase-one.md"]


@pytest.mark.asyncio
async def test_integration_ignores_unchanged_included_untracked_base_files(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    base = Path(config["repositories"]["default"])
    docs = base / "dev" / "big_update"
    docs.mkdir(parents=True)
    (docs / "00-phase-one.md").write_text("phase one context\n", encoding="utf-8")

    started = await runtime.start_worker(
        name="Context Implementer",
        brief="Use accepted docs and create a note.",
        repo_path=config["repositories"]["default"],
        include_untracked_from_base=["dev/big_update/00-*.md"],
    )
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, "worker-note.txt").write_text("from worker\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created note"}, session_id="session-1")

    preview = await runtime.inspect_worker(
        worker="Context Implementer",
        view="integration_preview",
        accepted_dirty_base=["dev/big_update/00-*.md"],
    )

    assert preview["can_apply"] is True
    assert preview["changed_files"] == ["worker-note.txt"]
    assert preview["accepted_dirty_base_files"] == ["dev/big_update/00-phase-one.md"]

    applied = await runtime.integrate_worker(
        worker="Context Implementer",
        accepted_dirty_base=["dev/big_update/00-*.md"],
    )
    assert applied["applied"] is True
    assert (base / "worker-note.txt").read_text(encoding="utf-8") == "from worker\n"
    assert (base / "dev/big_update/00-phase-one.md").read_text(encoding="utf-8") == "phase one context\n"


@pytest.mark.asyncio
async def test_integration_blocks_modified_included_untracked_base_files(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    base = Path(config["repositories"]["default"])
    docs = base / "dev" / "big_update"
    docs.mkdir(parents=True)
    (docs / "00-phase-one.md").write_text("phase one context\n", encoding="utf-8")

    started = await runtime.start_worker(
        name="Context Editor",
        brief="Edit accepted docs.",
        repo_path=config["repositories"]["default"],
        include_untracked_from_base=["dev/big_update/00-*.md"],
    )
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, "dev/big_update/00-phase-one.md").write_text("worker edited context\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Edited copied context"}, session_id="session-1")

    preview = await runtime.inspect_worker(
        worker="Context Editor",
        view="integration_preview",
        accepted_dirty_base=["dev/big_update/00-*.md"],
    )

    assert preview["can_apply"] is False
    assert preview["modified_included_untracked_base_files"] == ["dev/big_update/00-phase-one.md"]
    assert "copied from accepted untracked base context" in preview["note"]


@pytest.mark.asyncio
async def test_integration_preview_reports_conflict_without_mutating_base(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(name="Implementer", brief="Edit readme.", repo_path=config["repositories"]["default"])
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, "README.md").write_text("# worker version\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Edited README"}, session_id="session-1")

    base = Path(config["repositories"]["default"])
    (base / "README.md").write_text("# moved main\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=base, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Test", "-c", "user.email=worker-test@example.invalid", "commit", "-m", "main moved"],
        cwd=base,
        check=True,
        capture_output=True,
    )

    preview = await runtime.inspect_worker(worker="Implementer", view="integration_preview")
    assert preview["base_moved"] is True
    assert preview["can_apply"] is False
    assert preview["apply_check"] == "conflict"
    assert "README.md" in preview["conflict_summary"]
    assert (base / "README.md").read_text(encoding="utf-8") == "# moved main\n"


@pytest.mark.asyncio
async def test_integration_blocks_secret_like_paths(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(name="Implementer", brief="Create env.", repo_path=config["repositories"]["default"])
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created .env"}, session_id="session-1")

    preview = await runtime.inspect_worker(worker="Implementer", view="integration_preview")
    assert preview["can_apply"] is False
    assert preview["blocked_files"] == [".env"]
    assert "TOKEN=secret" not in str(preview)


@pytest.mark.asyncio
async def test_integration_skips_untracked_binary_files(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(name="Implementer", brief="Create binary.", repo_path=config["repositories"]["default"])
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, "binary.dat").write_bytes(b"\x00\x01worker-binary")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created binary"}, session_id="session-1")

    preview = await runtime.inspect_worker(worker="Implementer", view="integration_preview")
    assert preview["can_apply"] is False
    assert preview["skipped_files"] == ["binary.dat"]
    assert preview["patch_bytes"] == 0


@pytest.mark.asyncio
async def test_tool_handler_exposes_worker_integrate(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    started = await handler.handle_tool_call(
        "codex_worker_start",
        {"name": "Implementer", "brief": "Create file.", "repo": config["repositories"]["default"]},
    )
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    Path(job.worktree_path, "worker-note.txt").write_text("from handler\n", encoding="utf-8")
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "Created note"}, session_id="session-1")

    preview = await handler.handle_tool_call("codex_worker_inspect", {"worker": "Implementer", "view": "integration_preview"})
    assert preview["can_apply"] is True
    applied = await handler.handle_tool_call("codex_worker_integrate", {"worker": "Implementer"})
    assert applied["applied"] is True
    assert (Path(config["repositories"]["default"]) / "worker-note.txt").exists()
