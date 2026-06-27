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
