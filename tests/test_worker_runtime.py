import asyncio
import errno
import subprocess
import time
from pathlib import Path

import pytest

import patchbay.jobs.manager as job_manager_module
from patchbay.jobs.executor import JobExecutor, STALE_RUNNING_JOB_ERROR
from patchbay.jobs.manager import JobManager, JobState
from patchbay.ownership import (
    CURRENT_OWNER_SCHEMA,
    OWNER_CLIENT_REF_OPTION,
    OWNER_CREATED_AT_OPTION,
    OWNER_LABEL_OPTION,
    OWNER_LAST_SEEN_AT_OPTION,
    OWNER_SCHEMA_OPTION,
    OWNER_SCOPE_OPTION,
    OWNER_SESSION_HASH_OPTION,
)
from patchbay.protocol.context import RequestContext
from patchbay.workers.runtime import (
    WORKER_BASE_REPO_OPTION,
    WORKER_CHATGPT_SESSION_REF_OPTION,
    WORKER_ID_OPTION,
    WORKER_MODE_OPTION,
    WORKER_MODEL_OPTION,
    WORKER_NAME_OPTION,
    WORKER_REASONING_EFFORT_OPTION,
    WORKER_WORK_RUN_REF_OPTION,
    WorkerRuntime,
)


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# worker test\n", encoding="utf-8")
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
            "max_concurrent_jobs": 3,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
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
        "workers": {"worktree_root": str(tmp_path / "worker-worktrees")},
        "locks": {"root": str(tmp_path / "locks")},
    }


def init_extra_repo(path: Path, name: str = "extra") -> Path:
    path.mkdir()
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Test", "-c", "user.email=worker-test@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


class RecordingExecutor:
    def __init__(self, manager):
        self.manager = manager
        self.started = []
        self.scheduled = []
        self.cancelled = []

    def schedule_job(self, job_id):
        self.scheduled.append(job_id)
        self.started.append(job_id)

    async def execute_job(self, job_id):
        self.started.append(job_id)

    async def cancel_job(self, job_id, reason="Cancelled by request"):
        self.cancelled.append(job_id)
        self.manager.update_job_state(job_id, JobState.CANCELLED, error=reason)
        return {"cancelled": True, "job_id": job_id, "state": "cancelled"}


class DelayedPartialCancelExecutor(RecordingExecutor):
    async def cancel_job(self, job_id, reason="Cancelled by request"):
        self.cancelled.append(job_id)
        self.manager.update_job_state(job_id, JobState.CANCELLED, error=reason)

        async def attach_partial_result():
            await asyncio.sleep(0.05)
            self.manager.update_job_state(
                job_id,
                JobState.CANCELLED,
                result={
                    "summary": "Partial evidence attached after stop.",
                    "files_changed": [],
                    "partial": True,
                    "status": "cancelled",
                },
                last_event="process.cancelled",
            )

        asyncio.create_task(attach_partial_result())
        return {"cancelled": True, "job_id": job_id, "state": "cancelled"}


class FailingCreateJobManager(JobManager):
    def create_job(self, *args, **kwargs):
        raise RuntimeError("forced job creation failure")


def request_context(client_ref: str, label: str = "") -> RequestContext:
    return RequestContext(transport_session_id=f"session-{client_ref}", client_ref=client_ref, client_label=label)


def owner_context(owner_ref: str, client_ref: str, label: str = "") -> RequestContext:
    return RequestContext(
        transport_session_id=f"session-{client_ref}",
        client_ref=client_ref,
        owner_ref=owner_ref,
        owner_scope="token",
        client_label=label,
    )


def chatgpt_context(run_ref: str, *, chat_ref: str = "chatgpt_session_test") -> RequestContext:
    now = time.time()
    return RequestContext(
        transport_session_id=f"transport-{run_ref}",
        client_ref=f"client-{run_ref}",
        owner_ref="owner_shared",
        owner_scope="token",
        client_label="ChatGPT",
        chatgpt_session_ref=chat_ref,
        chatgpt_subject_ref="chatgpt_subject_test",
        work_run_ref=run_ref,
        work_run_started_at=now,
        work_run_last_activity_at=now,
    )


@pytest.mark.asyncio
async def test_start_worker_defaults_to_isolated_worktree_and_hides_backend_ids(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    result = await runtime.start_worker(
        name="Repository Investigator",
        brief="Inspect the repository architecture.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)

    assert result["name"] == "Repository Investigator"
    assert result["state"] == "starting"
    assert result["workspace_mode"] == "isolated_write"
    assert result["workspace_available"] is True
    assert result["accepted"] is True
    assert "job_id" not in result
    assert "session_id" not in result
    assert config["repositories"]["default"] not in str(result)

    job = next(iter(manager.jobs.values()))
    assert job.mode == "interactive"
    assert job.repo_path == config["repositories"]["default"]
    assert job.worktree_path != config["repositories"]["default"]
    assert job.options["sandbox"] == "workspace-write"
    assert job.options["_worker_workspace_mode"] == "isolated_write"
    assert job.options["_worker_worktree_path"] == job.worktree_path
    assert job.options[WORKER_NAME_OPTION] == "Repository Investigator"
    assert job.options[WORKER_ID_OPTION] == result["worker_id"]
    assert "report back like an engineer" in job.prompt
    assert executor.started == [job.job_id]
    assert executor.scheduled == [job.job_id]


@pytest.mark.asyncio
async def test_worker_uses_codex_bypass_when_configured_for_danger_full_access(tmp_path):
    config = make_config(tmp_path)
    config["security"]["default_sandbox"] = "danger-full-access"
    config["security"]["allow_dangerously_bypass"] = True
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    result = await runtime.start_worker(
        name="Unrestricted Worker",
        brief="Inspect with unrestricted VM Codex permissions.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)

    assert result["accepted"] is True
    job = next(iter(manager.jobs.values()))
    assert job.options["dangerously_bypass"] is True
    assert job.options["sandbox"] == "workspace-write"


@pytest.mark.asyncio
async def test_worker_owner_metadata_is_private_and_public_flags_are_session_relative(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    client_a = request_context("client_a", "Chat A")
    client_b = request_context("client_b", "Chat B")

    result = await runtime.start_worker(
        name="Owned Worker",
        brief="Inspect ownership.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=client_a,
    )
    await asyncio.sleep(0)

    assert result["owned_by_current_client"] is True
    assert result["ownership_status"] == "current_client"
    assert result["owner_label"] == "Chat A"
    assert OWNER_SESSION_HASH_OPTION not in str(result)
    assert OWNER_CLIENT_REF_OPTION not in str(result)

    job = manager.get_job(executor.started[0])
    assert job.options[OWNER_SESSION_HASH_OPTION] == "client_a"
    assert job.options[OWNER_CLIENT_REF_OPTION] == "client_a"
    assert job.options[OWNER_SCOPE_OPTION] == "transport_session"
    assert job.options[OWNER_SCHEMA_OPTION] == CURRENT_OWNER_SCHEMA
    assert job.options[OWNER_LABEL_OPTION] == "Chat A"
    assert isinstance(job.options[OWNER_CREATED_AT_OPTION], float)
    assert isinstance(job.options[OWNER_LAST_SEEN_AT_OPTION], float)

    same_client = await runtime.list_workers(request_context=client_a)
    other_client = await runtime.list_workers(request_context=client_b)
    assert same_client["workers"][0]["owned_by_current_client"] is True
    assert other_client["workers"][0]["owned_by_current_client"] is False
    assert other_client["workers"][0]["ownership_status"] == "other_connection"
    assert "different PatchBay coordination owner" in other_client["workers"][0]["ownership_note"]
    assert "takeover=true" in other_client["workers"][0]["ownership_note"]
    assert OWNER_SESSION_HASH_OPTION not in str(other_client)
    assert "client_a" not in str(other_client)

    reloaded_manager = JobManager(config)
    reloaded_runtime = WorkerRuntime(config, reloaded_manager, RecordingExecutor(reloaded_manager))
    reloaded = await reloaded_runtime.list_workers(scope="history", request_context=client_b)
    assert reloaded["workers"][0]["owned_by_current_client"] is False
    assert reloaded["workers"][0]["ownership_status"] == "other_connection"


@pytest.mark.asyncio
async def test_stable_owner_ref_survives_short_lived_transport_sessions(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    first_transport = owner_context("owner_shared", "client_first", "ChatGPT")
    second_transport = owner_context("owner_shared", "client_second", "ChatGPT")

    await runtime.start_worker(
        name="Stable Owner Worker",
        brief="Inspect ownership with short-lived transport sessions.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=first_transport,
    )
    await asyncio.sleep(0)

    job = manager.get_job(executor.started[0])
    assert job.options[OWNER_SESSION_HASH_OPTION] == "owner_shared"
    assert job.options[OWNER_CLIENT_REF_OPTION] == "client_first"
    assert job.options[OWNER_SCOPE_OPTION] == "token"
    assert job.options[OWNER_SCHEMA_OPTION] == CURRENT_OWNER_SCHEMA

    seen = await runtime.list_workers(request_context=second_transport)
    assert seen["workers"][0]["owned_by_current_client"] is True
    assert seen["workers"][0]["ownership_status"] == "current_client"
    assert seen["workers"][0]["ownership_scope"] == "token"


@pytest.mark.asyncio
async def test_legacy_owner_metadata_is_reported_separately_from_known_other_owner(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    legacy_client = request_context("client_a", "Old Chat")
    current_client = owner_context("owner_shared", "client_current", "ChatGPT")

    started = await runtime.start_worker(
        name="Legacy Owner Worker",
        brief="Inspect legacy ownership.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=legacy_client,
    )
    await asyncio.sleep(0)

    job = manager.get_job(executor.started[0])
    job.options.pop(OWNER_SCOPE_OPTION, None)
    job.options.pop(OWNER_SCHEMA_OPTION, None)
    manager._persist_job(job)

    listed = await runtime.list_workers(request_context=current_client)
    worker = listed["workers"][0]
    assert worker["worker_id"] == started["worker_id"]
    assert worker["owned_by_current_client"] is False
    assert worker["ownership_status"] == "legacy_connection"
    assert worker["ownership_scope"] == "token"
    assert "older PatchBay version" in worker["ownership_note"]
    assert "rewrite the item's owner metadata" in worker["ownership_note"]
    assert OWNER_SESSION_HASH_OPTION not in str(worker)
    assert "client_a" not in str(worker)

    refused = await runtime.message_worker(
        worker=started["worker_id"],
        message="Continue after ownership migration.",
        request_context=current_client,
    )
    assert refused["accepted"] is False
    assert refused["takeover_required"] is True
    assert refused["ownership_status"] == "legacy_connection"

    first_job = manager.get_job(executor.started[0])
    manager.update_job_state(first_job.job_id, JobState.COMPLETED, result={"summary": "ready"}, session_id="session-a")
    accepted = await runtime.message_worker(
        worker=started["worker_id"],
        message="Continue after ownership migration.",
        request_context=current_client,
        takeover=True,
        takeover_reason="User confirmed this is the same PatchBay task.",
    )
    await asyncio.sleep(0)

    assert accepted["accepted"] is True
    assert accepted["takeover_performed"] is True
    resume_job = manager.get_job(executor.started[-1])
    assert resume_job.options[OWNER_SESSION_HASH_OPTION] == "owner_shared"
    assert resume_job.options[OWNER_CLIENT_REF_OPTION] == "client_current"
    assert resume_job.options[OWNER_SCOPE_OPTION] == "token"
    assert resume_job.options[OWNER_SCHEMA_OPTION] == CURRENT_OWNER_SCHEMA


@pytest.mark.asyncio
async def test_worker_message_requires_takeover_for_other_owner_and_transfers_control(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    client_a = request_context("client_a", "Chat A")
    client_b = request_context("client_b", "Chat B")

    started = await runtime.start_worker(
        name="Takeover Worker",
        brief="Start work.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=client_a,
    )
    await asyncio.sleep(0)
    first_job = manager.get_job(executor.started[0])
    manager.update_job_state(first_job.job_id, JobState.COMPLETED, result={"summary": "ready"}, session_id="session-a")

    refused = await runtime.message_worker(
        worker=started["worker_id"],
        message="Continue from another chat.",
        request_context=client_b,
    )

    assert refused["accepted"] is False
    assert refused["takeover_required"] is True
    assert refused["owned_by_current_client"] is False
    assert refused["required_action"] == "call again with takeover=true after user confirms this is intentional"
    assert executor.started == [first_job.job_id]
    assert executor.scheduled == [first_job.job_id]
    assert "client_a" not in str(refused)

    accepted = await runtime.message_worker(
        worker=started["worker_id"],
        message="Continue from another chat.",
        request_context=client_b,
        takeover=True,
        takeover_reason="User asked this chat to continue it.",
    )
    await asyncio.sleep(0)

    assert accepted["accepted"] is True
    assert accepted["takeover_performed"] is True
    resume_job = manager.get_job(executor.started[-1])
    assert resume_job.options[OWNER_SESSION_HASH_OPTION] == "client_b"
    assert resume_job.options[OWNER_CLIENT_REF_OPTION] == "client_b"
    assert resume_job.options["_mcp_takeover_reason"] == "User asked this chat to continue it."
    assert executor.scheduled[-1] == resume_job.job_id

    seen_by_a = await runtime.list_workers(request_context=client_a)
    assert seen_by_a["workers"][0]["owned_by_current_client"] is False
    assert seen_by_a["workers"][0]["ownership_status"] == "other_connection"


@pytest.mark.asyncio
async def test_worker_stop_requires_takeover_for_other_owner(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    client_a = request_context("client_a", "Chat A")
    client_b = request_context("client_b", "Chat B")

    started = await runtime.start_worker(
        name="Stop Protected Worker",
        brief="Keep running.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=client_a,
    )
    await asyncio.sleep(0)
    first_job = manager.get_job(executor.started[0])

    refused = await runtime.stop_worker(worker=started["worker_id"], request_context=client_b)
    assert refused["stopped"] is False
    assert refused["takeover_required"] is True
    assert first_job.job_id not in executor.cancelled
    assert manager.get_job(first_job.job_id).state == JobState.PENDING

    stopped = await runtime.stop_worker(worker=started["worker_id"], request_context=client_b, takeover=True, force=True)
    assert stopped["takeover_performed"] is True
    assert first_job.job_id in executor.cancelled
    assert manager.get_job(first_job.job_id).options[OWNER_CLIENT_REF_OPTION] == "client_b"


@pytest.mark.asyncio
async def test_start_worker_accepts_model_and_reasoning_effort(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    result = await runtime.start_worker(
        name="Deep Worker",
        brief="Inspect the repository architecture.",
        repo_path=config["repositories"]["default"],
        model="gpt-5.5",
        reasoning_effort="high",
    )
    await asyncio.sleep(0)

    job = next(iter(manager.jobs.values()))
    assert result["model"] == "gpt-5.5"
    assert result["reasoning_effort"] == "high"
    assert job.options["model"] == "gpt-5.5"
    assert job.options[WORKER_MODEL_OPTION] == "gpt-5.5"
    assert job.options[WORKER_REASONING_EFFORT_OPTION] == "high"
    assert job.options["config_overrides"] == ['model_reasoning_effort="high"']


@pytest.mark.asyncio
async def test_worker_message_inherits_or_overrides_execution_options(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    await runtime.start_worker(
        name="Continuity Worker",
        brief="Inspect the repository architecture.",
        repo_path=config["repositories"]["default"],
        model="gpt-5.5",
        reasoning_effort="medium",
    )
    await asyncio.sleep(0)
    first_job = next(iter(manager.jobs.values()))
    manager.update_job_state(
        first_job.job_id,
        JobState.COMPLETED,
        result={"summary": "Ready.", "files_changed": []},
        session_id="session-abc",
        exit_code=0,
    )

    inherited = await runtime.message_worker(worker="Continuity Worker", message="Continue.")
    await asyncio.sleep(0)
    inherited_job = manager.get_job(executor.started[-1])
    assert inherited["model"] == "gpt-5.5"
    assert inherited["reasoning_effort"] == "medium"
    assert inherited_job.options["model"] == "gpt-5.5"
    assert inherited_job.options[WORKER_REASONING_EFFORT_OPTION] == "medium"

    manager.update_job_state(
        inherited_job.job_id,
        JobState.COMPLETED,
        result={"summary": "Ready again.", "files_changed": []},
        session_id="session-abc",
        exit_code=0,
    )
    overridden = await runtime.message_worker(
        worker="Continuity Worker",
        message="Continue with deeper reasoning.",
        reasoning_effort="xhigh",
    )
    await asyncio.sleep(0)
    override_job = manager.get_job(executor.started[-1])
    assert overridden["reasoning_effort"] == "xhigh"
    assert override_job.options["model"] == "gpt-5.5"
    assert override_job.options[WORKER_REASONING_EFFORT_OPTION] == "xhigh"


@pytest.mark.asyncio
async def test_worker_options_can_ignore_user_config_for_isolated_trials(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["ignore_user_config"] = True
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    await runtime.start_worker(
        name="Isolated Config Worker",
        brief="Inspect the repository architecture.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)

    job = next(iter(manager.jobs.values()))
    assert job.options["ignore_user_config"] is True


@pytest.mark.asyncio
async def test_worker_report_redacts_private_branch_and_uuid_values(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Redaction Worker",
        brief="Inspect the repository architecture.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)
    job = next(iter(manager.jobs.values()))
    branch_name = job.options["_worker_branch_name"]
    raw_uuid = "dc82f84c-13d1-4a7f-8076-6236c33ac4c2"
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={
            "summary": f"Git remained clean on branch {branch_name}; session {raw_uuid}; path {job.worktree_path}",
            "files_changed": [],
        },
        session_id="session-redacted",
        exit_code=0,
    )

    report = await runtime.inspect_worker(worker=started["worker_id"])
    assert "codex/worker-" not in report["report"]
    assert raw_uuid not in report["report"]
    assert str(job.worktree_path) not in report["report"]
    assert "[worker-branch]" in report["report"]
    assert "[id]" in report["report"]


@pytest.mark.asyncio
async def test_start_worker_rolls_back_isolated_worktree_when_job_creation_fails(tmp_path):
    config = make_config(tmp_path)
    manager = FailingCreateJobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    with pytest.raises(RuntimeError, match="forced job creation failure"):
        await runtime.start_worker(
            name="Rollback Implementer",
            brief="Create work.",
            repo_path=config["repositories"]["default"],
        )

    worker_root = Path(config["workers"]["worktree_root"])
    assert worker_root.exists()
    assert list(worker_root.iterdir()) == []
    assert manager.jobs == {}
    assert executor.started == []
    branches = subprocess.run(
        ["git", "branch", "--list", "codex/worker-*"],
        cwd=config["repositories"]["default"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert branches.stdout.strip() == ""


def test_create_worker_worktree_reports_full_filesystem(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)

    class FakeGit:
        def worktree(self, *args):
            raise OSError(errno.ENOSPC, "No space left on device")

    class FakeCommit:
        hexsha = "abc123"

    class FakeHead:
        commit = FakeCommit()

    class FakeRepo:
        head = FakeHead()
        git = FakeGit()

    monkeypatch.setattr(job_manager_module.git, "Repo", lambda repo_path: FakeRepo())

    with pytest.raises(ValueError, match="Worker worktree could not be created: host filesystem is full"):
        manager.create_worker_worktree("worker-full-disk", config["repositories"]["default"])


@pytest.mark.asyncio
async def test_start_worker_rolls_back_worktree_when_concurrency_limit_rejects_job(tmp_path):
    config = make_config(tmp_path)
    config["server"]["max_concurrent_jobs"] = 1
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    active_job_id = manager.create_job("interactive", "already running", config["repositories"]["default"], {})
    manager.update_job_state(active_job_id, JobState.RUNNING)

    with pytest.raises(RuntimeError, match="Maximum active jobs"):
        await runtime.start_worker(
            name="Rejected Parallel Implementer",
            brief="Create work.",
            repo_path=config["repositories"]["default"],
        )

    worker_root = Path(config["workers"]["worktree_root"])
    assert worker_root.exists()
    assert list(worker_root.iterdir()) == []
    assert len(manager.jobs) == 1
    assert executor.started == []
    branches = subprocess.run(
        ["git", "branch", "--list", "codex/worker-*"],
        cwd=config["repositories"]["default"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert branches.stdout.strip() == ""


@pytest.mark.asyncio
async def test_completed_worker_survives_restart_and_continues_same_session(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Session Investigator",
        brief="Inspect session continuity.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    await asyncio.sleep(0)
    first_job = next(iter(manager.jobs.values()))
    manager.update_job_state(
        first_job.job_id,
        JobState.COMPLETED,
        result={
            "summary": "I found the existing continuation path.",
            "notes": "No code was changed.",
            "next_steps": ["Continue the same thread after restart."],
            "files_changed": [],
        },
        session_id="session-123",
        exit_code=0,
    )

    report = await runtime.inspect_worker(worker=started["worker_id"])
    assert report["state"] == "idle"
    assert report["workspace_mode"] == "read_only"
    assert report["has_session"] is True
    assert "existing continuation path" in report["report"]

    reloaded_manager = JobManager(config)
    reloaded_executor = RecordingExecutor(reloaded_manager)
    reloaded_runtime = WorkerRuntime(config, reloaded_manager, reloaded_executor)

    listed = await reloaded_runtime.list_workers(scope="history")
    assert listed["count"] == 1
    assert listed["workers"][0]["name"] == "Session Investigator"

    continued = await reloaded_runtime.message_worker(
        worker="Session Investigator",
        message="Continue and explain the restart behavior.",
    )
    await asyncio.sleep(0)

    assert continued["accepted"] is True
    jobs = list(reloaded_manager.jobs.values())
    resume_job = next(job for job in jobs if job.mode == "resume")
    assert resume_job.options["resume_session_id"] == "session-123"
    assert resume_job.options["sandbox"] == "read-only"
    assert resume_job.options[WORKER_ID_OPTION] == started["worker_id"]
    assert reloaded_executor.started == [resume_job.job_id]
    assert reloaded_executor.scheduled == [resume_job.job_id]


@pytest.mark.asyncio
async def test_message_does_not_create_a_queue_while_worker_is_busy(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Busy Worker",
        brief="Inspect slowly.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    before = len(manager.jobs)

    result = await runtime.message_worker(worker=started["worker_id"], message="Change direction.")

    assert result["accepted"] is False
    assert result["state"] == "starting"
    assert "does not add a message queue" in result["note"]
    assert len(manager.jobs) == before


@pytest.mark.asyncio
async def test_start_worker_can_auto_suffix_existing_name(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    first = await runtime.start_worker(
        name="Repeat Phase Worker",
        brief="First run.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    second = await runtime.start_worker(
        name="Repeat Phase Worker",
        brief="Second run.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        auto_suffix=True,
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["name"].startswith("Repeat Phase Worker ")
    assert second["name"] != first["name"]


@pytest.mark.asyncio
async def test_running_worker_public_view_exposes_liveness_and_checkpoints(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Live Checkpoint Worker",
        brief="Investigate slowly and report checkpoints.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-live-checkpoint",
        process_started_at=time.time(),
        process_pid=12345,
        last_heartbeat_at=time.time(),
        last_event="item.completed",
        progress="Worker checkpoint: first pass complete",
        checkpoints=[
            {
                "kind": "agent_message",
                "event": "item.completed",
                "at": time.time(),
                "summary": "I finished the first pass and am still checking edge cases.",
            }
        ],
    )

    view = await runtime.inspect_worker(worker=started["worker_id"], view="status")

    assert view["state"] == "working"
    assert view["can_message"] is False
    assert view["can_message_reason"] == "active_turn_running"
    assert view["active_steering_supported"] is False
    assert view["followup_mode"] == "next_turn_after_completion"
    assert view["liveness"]["status"] == "active"
    assert view["liveness"]["phase"] == "model_reasoning"
    assert view["liveness"]["suggested_action"] == "wait"
    assert view["liveness"]["heartbeat_age_seconds"] is not None
    assert view["latest_partial_note"]["available"] is True
    assert view["latest_checkpoints"][0]["kind"] == "agent_message"
    assert "first pass" in view["latest_checkpoints"][0]["summary"]
    assert view["latest_turn"]["latest_checkpoint"]["summary"] == view["latest_checkpoints"][0]["summary"]
    assert view["checkpoint_count"] == 1
    assert view["report_artifacts"][0]["kind"] == "live_checkpoints"
    assert view["worker_report_files_note"].startswith("No repo report files because this is a read_only worker")
    assert view["activity_since_last_check"]["first_check"] is True
    assert "Live Checkpoint Worker: active" in view["status_line"]
    assert "still running" in view["report"]


@pytest.mark.asyncio
async def test_liveness_thresholds_are_configurable_display_policy(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["heartbeat_fresh_seconds"] = 1
    config["workers"]["heartbeat_quiet_seconds"] = 5
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Quiet Worker",
        brief="Investigate slowly.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-quiet",
        process_started_at=time.time() - 10,
        last_heartbeat_at=time.time() - 3,
        last_event="stdout",
    )

    view = await runtime.inspect_worker(worker=started["worker_id"], view="status")

    assert view["liveness"]["status"] == "quiet"
    assert view["liveness"]["heartbeat_fresh_seconds"] == 1
    assert view["liveness"]["heartbeat_quiet_seconds"] == 5
    assert "quiet" in view["report"]


@pytest.mark.asyncio
async def test_worker_status_reports_compact_team_deltas(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Delta Worker",
        brief="Investigate with visible status.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-delta",
        process_started_at=time.time(),
        process_pid=12345,
        last_heartbeat_at=time.time(),
        last_event="item.started",
        current_phase="command_running",
        current_item_type="command_execution",
        current_command_preview="rg worker runtime",
        current_command_started_at=time.time(),
        event_count=1,
        stdout_bytes_seen=100,
    )

    first = await runtime.worker_status(repo_path=config["repositories"]["default"], include_stopped=True)

    assert first["summary"].startswith("Workers: 1 total | 1 active")
    assert first["since_last_check"]["first_check"] is True
    assert "baseline recorded" in first["since_last_check_line"]
    assert first["worker_lines"][0].startswith("Delta Worker: active")
    assert "rg worker runtime" not in first["worker_lines"][0]
    assert first["workers"][0]["current_command"]["running"] is True
    assert first["workers"][0]["current_command"]["kind"] == "shell_command"
    assert "preview" not in first["workers"][0]["current_command"]
    assert first["minimum_next_poll_seconds"] == 10
    assert first["recommended_next_poll_seconds"] == 20
    assert "10-20 seconds" in first["poll_guidance"]

    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        last_heartbeat_at=time.time(),
        last_event="item.completed",
        current_phase="command_completed_waiting_for_model",
        current_command_preview=None,
        current_command_started_at=None,
        last_command_preview="rg worker runtime",
        last_command_completed_at=time.time(),
        event_count=5,
        stdout_bytes_seen=1124,
        checkpoints=[
            {
                "kind": "agent_message",
                "event": "item.completed",
                "at": time.time(),
                "summary": "I found the status layer and am checking report surfacing.",
            }
        ],
    )

    second = await runtime.worker_status(repo_path=config["repositories"]["default"], include_stopped=True, force_refresh=True)

    assert second["since_last_check"]["first_check"] is False
    assert second["since_last_check"]["events_delta"] == 4
    assert second["since_last_check"]["stdout_bytes_delta"] == 1024
    assert second["since_last_check"]["partial_notes_delta"] == 1
    assert second["workers"][0]["latest_partial_note"]["available"] is True
    assert "partial note" in second["worker_lines"][0]
    listed = await runtime.list_workers(repo_path=config["repositories"]["default"], include_stopped=True)
    assert listed["team_status"]["recommended_next_poll_seconds"] == 20
    assert "Do not poll every few seconds" in listed["team_report"]


def test_context_from_workers_accepts_ten_and_rejects_eleven(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    expected = [f"worker-{index}" for index in range(10)]
    assert runtime._normalize_context_workers(expected) == expected
    with pytest.raises(ValueError, match="capped at 10"):
        runtime._normalize_context_workers([f"worker-{index}" for index in range(11)])


@pytest.mark.asyncio
async def test_cancelled_worker_report_uses_partial_result(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Partial Worker",
        brief="Start and get cancelled.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.CANCELLED,
        result={
            "summary": "I mapped the UI routes before cancellation.",
            "files_changed": [],
            "partial": True,
            "status": "cancelled",
        },
        session_id="session-partial",
        checkpoints=[
            {
                "kind": "agent_message",
                "event": "item.completed",
                "at": time.time(),
                "summary": "I mapped the UI routes before cancellation.",
            }
        ],
        error="Cancelled by request",
    )

    view = await runtime.inspect_worker(worker=started["worker_id"])

    assert view["state"] == "stopped"
    assert "Partial report" in view["report"]
    assert "UI routes" in view["report"]
    assert view["report_artifacts"][0]["kind"] == "structured_result"
    assert view["report_artifacts"][0]["partial"] is True
    assert view["latest_checkpoints"][0]["summary"] == "I mapped the UI routes before cancellation."


@pytest.mark.asyncio
async def test_cancelled_worker_raw_only_result_is_not_called_partial_report(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Raw Cancelled Worker",
        brief="Start and get cancelled before final schema.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.CANCELLED,
        result={
            "summary": "No final structured worker report was captured, but PatchBay preserved bounded raw Codex output for manager inspection.",
            "files_changed": [],
            "partial": True,
            "status": "cancelled",
            "final_structured_result": False,
            "raw_output_available": True,
            "stdout_preview": "The worker had already mapped the data import path.",
        },
        session_id="session-raw-cancelled",
        error="Cancelled by request",
    )

    view = await runtime.inspect_worker(worker=started["worker_id"])

    assert "Partial report:" not in view["report"]
    assert "stopped before a final structured report" in view["report"]
    assert "mapped the data import path" in view["report"]


@pytest.mark.asyncio
async def test_completed_worker_report_exposes_detailed_evidence_fields(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Detailed Reporter",
        brief="Investigate and report with evidence.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={
            "summary": "Mapped the request path.",
            "detailed_report": "The worker traced the route handler, service layer, and test fixture before recommending a focused fix.",
            "evidence": ["web/server.py defines the route", "tests cover the existing response shape"],
            "files_changed": [],
            "commands_run": ["rg route"],
            "tests_run": [],
            "notes": "No code changed.",
            "risks": ["Data fixture coverage is thin"],
            "open_questions": ["Whether the UI should expose this field"],
            "next_steps": ["Ask implementation worker to patch the route"],
        },
        session_id="session-detailed",
    )

    view = await runtime.inspect_worker(worker=started["worker_id"])

    assert "Mapped the request path." in view["report"]
    assert "traced the route handler" in view["report"]
    assert "Evidence: web/server.py defines the route" in view["report"]
    assert "Risks: Data fixture coverage is thin" in view["report"]
    assert "Open questions: Whether the UI should expose this field" in view["report"]
    assert view["report_artifacts"][0]["evidence_count"] == 2
    assert view["report_artifacts"][0]["risk_count"] == 1
    assert view["report_artifacts"][0]["open_question_count"] == 1
    assert "detailed_report" in view["report_artifacts"][0]["fields_present"]


@pytest.mark.asyncio
async def test_unstructured_worker_result_surfaces_preserved_output(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Unstructured Reporter",
        brief="Finish without schema.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={
            "summary": "No final structured worker report was captured, but PatchBay preserved bounded raw Codex output for manager inspection.",
            "files_changed": [],
            "notes": "Could not extract a final structured Codex result event.",
            "final_structured_result": False,
            "raw_output_available": True,
            "stdout_preview": "I inspected the dataflow and found the batch importer route.",
        },
        session_id="session-unstructured",
    )

    view = await runtime.inspect_worker(worker=started["worker_id"])

    assert "preserved bounded raw Codex output" in view["report"]
    assert "Preserved raw-output preview" in view["report"]
    assert "batch importer route" in view["report"]
    assert view["report_artifacts"][0]["final_structured_result"] is False
    assert view["report_artifacts"][0]["stdout_preview_available"] is True


@pytest.mark.asyncio
async def test_stop_worker_requires_confirmation_for_live_recent_turn(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["stop_confirmation_grace_seconds"] = 300
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Graceful Stop Worker",
        brief="Keep working.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-graceful-stop",
        process_started_at=time.time(),
        last_heartbeat_at=time.time(),
        last_event="stdout",
    )

    first = await runtime.stop_worker(worker=started["worker_id"])

    assert first["stopped"] is False
    assert first["stop_confirmation_required"] is True
    assert first["force_required"] is True
    assert first["force_parameter"] == "force"
    assert first["suggested_action"] == "wait_or_force_stop"
    assert "force=true" in first["note"]
    assert job.job_id not in executor.cancelled
    assert manager.get_job(job.job_id).state == JobState.RUNNING

    forced = await runtime.stop_worker(worker=started["worker_id"], force=True)

    assert forced["stopped"] is True
    assert forced["stop_confirmation_required"] is False
    assert job.job_id in executor.cancelled


@pytest.mark.asyncio
async def test_stop_worker_allows_stale_long_command_without_confirmation(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["heartbeat_fresh_seconds"] = 60
    config["workers"]["heartbeat_quiet_seconds"] = 120
    config["workers"]["stop_confirmation_grace_seconds"] = 300
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Stale Command Worker",
        brief="Run a broad command.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    now = time.time()
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-stale-command",
        process_started_at=now - 900,
        last_heartbeat_at=now - 500,
        last_event="item.started",
        current_command_preview="rg broad-pattern .",
        current_command_started_at=now - 500,
    )

    result = await runtime.stop_worker(worker=started["worker_id"])

    assert result["stopped"] is True
    assert result["stop_confirmation_required"] is False
    assert job.job_id in executor.cancelled


@pytest.mark.asyncio
async def test_stop_worker_preserves_manager_reason_in_cancellation_evidence(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    started = await runtime.start_worker(
        name="Reasoned Stop Worker",
        brief="Keep working.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-reasoned-stop",
        process_started_at=time.time() - 900,
        last_heartbeat_at=time.time() - 500,
    )

    result = await runtime.stop_worker(
        worker=started["worker_id"],
        force=True,
        reason="Reconcile a completed verification wrapper.",
    )

    assert result["stopped"] is True
    assert manager.get_job(job.job_id).error == "Reconcile a completed verification wrapper."


@pytest.mark.asyncio
async def test_stop_worker_waits_briefly_for_partial_report_artifact(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["stop_artifact_wait_seconds"] = 1
    manager = JobManager(config)
    executor = DelayedPartialCancelExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Stop Evidence Worker",
        brief="Start then stop.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-stop-evidence",
        process_started_at=time.time(),
        last_heartbeat_at=time.time(),
    )

    view = await runtime.stop_worker(worker=started["worker_id"], force=True)

    assert view["stopped"] is True
    assert "Partial evidence attached after stop" in view["report"]
    assert view["report_artifacts"][0]["partial"] is True


@pytest.mark.asyncio
async def test_stop_worker_existing_checkpoint_does_not_skip_partial_result_wait(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["stop_artifact_wait_seconds"] = 1
    manager = JobManager(config)
    executor = DelayedPartialCancelExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Checkpoint Before Stop",
        brief="Start then stop after checkpoint.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-checkpoint-before-stop",
        process_started_at=time.time(),
        last_heartbeat_at=time.time(),
        checkpoints=[{"kind": "agent_message", "at": time.time(), "summary": "Earlier checkpoint."}],
    )

    view = await runtime.stop_worker(worker=started["worker_id"], force=True)

    assert view["stopped"] is True
    assert "Partial evidence attached after stop" in view["report"]
    assert view["report_artifacts"][0]["partial"] is True
    assert view["latest_checkpoints"][0]["summary"] == "Earlier checkpoint."


@pytest.mark.asyncio
async def test_public_worker_last_activity_tracks_heartbeat_not_start_time(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Live Activity Worker",
        brief="Keep running.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[0])
    started_at = time.time() - 600
    heartbeat_at = time.time()
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        started_at=started_at,
        process_started_at=started_at + 1,
        last_heartbeat_at=heartbeat_at,
        last_stdout_at=heartbeat_at,
        session_id="session-live-activity",
    )

    view = await runtime.inspect_worker(worker=started["worker_id"], view="status")

    assert view["last_activity_at"] >= heartbeat_at - 0.01
    assert view["last_activity_at"] > started_at + 500


@pytest.mark.asyncio
async def test_failed_worker_public_view_exposes_auth_diagnostic(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    job_id = manager.create_job(
        "interactive",
        "inspect",
        config["repositories"]["default"],
        {
            WORKER_ID_OPTION: "wrk_auth_failed",
            WORKER_NAME_OPTION: "Auth Failed Worker",
            WORKER_MODE_OPTION: "read_only",
            WORKER_BASE_REPO_OPTION: config["repositories"]["default"],
            "sandbox": "read-only",
        },
    )
    manager.update_job_state(
        job_id,
        JobState.FAILED,
        error=(
            "Codex authentication failed before the worker could run: the local Codex access token "
            "could not be refreshed. Log in to Codex again on this host, then retry the worker."
        ),
        result={
            "summary": "Codex authentication failed before the worker could run.",
            "files_changed": [],
            "failure_diagnostic": {
                "category": "codex_auth_refresh_failed",
                "operator_action": "Run `codex login` for the same user/CODEX_HOME used by PatchBay, then retry a small worker.",
                "retry_without_operator_action": False,
            },
        },
        exit_code=1,
    )

    view = await runtime.inspect_worker(worker="Auth Failed Worker", view="status")

    assert view["state"] == "failed"
    assert "Codex authentication failed" in view["report"]
    assert view["latest_turn"]["failure_category"] == "codex_auth_refresh_failed"
    assert view["latest_turn"]["failure_retry_without_operator_action"] is False
    assert "codex login" in view["latest_turn"]["failure_operator_action"]
    assert view["liveness"]["failure_category"] == "codex_auth_refresh_failed"
    assert view["liveness"]["suggested_action"] == "reauthenticate"
    assert "Operator action" in view["report"]


@pytest.mark.asyncio
async def test_inspect_reconciles_stale_running_worker_without_waiting(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)
    job_id = manager.create_job(
        "interactive",
        "inspect",
        config["repositories"]["default"],
        {
            WORKER_ID_OPTION: "wrk_stale",
            WORKER_NAME_OPTION: "Stale Worker",
            WORKER_MODE_OPTION: "read_only",
            WORKER_BASE_REPO_OPTION: config["repositories"]["default"],
            "sandbox": "read-only",
        },
    )
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.jobs[job_id].started_at = time.time() - 700
    manager._persist_job(manager.jobs[job_id])

    started = time.monotonic()
    result = await runtime.inspect_worker(worker="Stale Worker", wait_seconds=30, view="diagnostics")

    assert time.monotonic() - started < 1
    assert result["state"] == "failed"
    assert "no live Codex process is tracked" in result["report"]
    assert result["latest_turn"]["failure_category"] == "patchbay_runtime_tracking_lost"
    assert config["repositories"]["default"] not in str(result)
    assert manager.get_job(job_id).state == JobState.FAILED
    assert manager.get_job(job_id).error == STALE_RUNNING_JOB_ERROR


@pytest.mark.asyncio
async def test_list_workers_reconciles_stale_running_worker(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)
    job_id = manager.create_job(
        "interactive",
        "inspect",
        config["repositories"]["default"],
        {
            WORKER_ID_OPTION: "wrk_list_stale",
            WORKER_NAME_OPTION: "List Stale Worker",
            WORKER_MODE_OPTION: "read_only",
            WORKER_BASE_REPO_OPTION: config["repositories"]["default"],
            "sandbox": "read-only",
        },
    )
    manager.update_job_state(job_id, JobState.RUNNING)
    manager.jobs[job_id].started_at = time.time() - 700
    manager._persist_job(manager.jobs[job_id])

    result = await runtime.list_workers(scope="history")

    assert result["count"] == 1
    assert result["active"] == 0
    assert result["workers"][0]["state"] == "failed"
    assert manager.get_job(job_id).state == JobState.FAILED


@pytest.mark.asyncio
async def test_worker_list_filters_active_stopped_owner_and_created_after(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    client_a = owner_context("owner_a", "client_a", "Chat A")
    client_b = owner_context("owner_b", "client_b", "Chat B")

    active = await runtime.start_worker(
        name="Active Worker",
        brief="Keep running.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=client_a,
    )
    old_timestamp = time.time() - 100
    manager.get_job(executor.started[-1]).started_at = old_timestamp
    manager._persist_job(manager.get_job(executor.started[-1]))

    stopped = await runtime.start_worker(
        name="Stopped Worker",
        brief="Stop this.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=client_a,
    )
    manager.update_job_state(executor.started[-1], JobState.CANCELLED, error="stopped")

    other = await runtime.start_worker(
        name="Other Owner Worker",
        brief="Owned elsewhere.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=client_b,
    )

    active_only = await runtime.list_workers(active_only=True, request_context=client_a)
    assert [item["worker_id"] for item in active_only["workers"]] == [active["worker_id"], other["worker_id"]]

    without_stopped = await runtime.list_workers(include_stopped=False, request_context=client_a)
    assert stopped["worker_id"] not in {item["worker_id"] for item in without_stopped["workers"]}

    owned = await runtime.list_workers(owned_only=True, scope="history", request_context=client_a)
    assert {item["worker_id"] for item in owned["workers"]} == {active["worker_id"], stopped["worker_id"]}

    recent = await runtime.list_workers(created_after=time.time() - 10, scope="history", request_context=client_a)
    assert active["worker_id"] not in {item["worker_id"] for item in recent["workers"]}


@pytest.mark.asyncio
async def test_worker_status_current_scope_hides_historical_terminal_workers(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    old_ctx = chatgpt_context("run_old")
    current_ctx = chatgpt_context("run_current")

    old_done = await runtime.start_worker(
        name="Old Done",
        brief="Historical completed work.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=old_ctx,
    )
    manager.update_job_state(executor.started[-1], JobState.COMPLETED, result={"summary": "old done"}, session_id="old-session")

    current_done = await runtime.start_worker(
        name="Current Done",
        brief="Current completed work.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=current_ctx,
    )
    manager.update_job_state(executor.started[-1], JobState.COMPLETED, result={"summary": "current done"}, session_id="current-session")

    active_old_run = await runtime.start_worker(
        name="Active Old Run",
        brief="Still running from old run.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
        request_context=old_ctx,
    )
    manager.update_job_state(executor.started[-1], JobState.RUNNING, process_started_at=time.time(), session_id="active-session")

    status = await runtime.worker_status(request_context=current_ctx, scope="current")
    visible_ids = {item["worker_id"] for item in status["workers"]}

    assert current_done["worker_id"] in visible_ids
    assert active_old_run["worker_id"] in visible_ids
    assert old_done["worker_id"] not in visible_ids
    assert status["scope"]["applied"] == "current"
    assert status["scope"]["current_work_run_ref"] == "run_current"
    assert status["hidden_workers"]["count"] == 1
    assert "scope='conversation'" in status["hidden_workers"]["how_to_show"]

    conversation = await runtime.worker_status(request_context=current_ctx, scope="conversation", force_refresh=True)
    conversation_ids = {item["worker_id"] for item in conversation["workers"]}
    assert {old_done["worker_id"], current_done["worker_id"], active_old_run["worker_id"]} <= conversation_ids

    job = manager.get_job(executor.started[1])
    assert job.options[WORKER_CHATGPT_SESSION_REF_OPTION] == "chatgpt_session_test"
    assert job.options[WORKER_WORK_RUN_REF_OPTION] == "run_current"


@pytest.mark.asyncio
async def test_list_workers_does_not_scan_worker_changes(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    await runtime.start_worker(
        name="Large Workspace Worker",
        brief="Inspect a large workspace.",
        repo_path=config["repositories"]["default"],
    )

    def fail_if_called(jobs):
        raise AssertionError("worker list must not scan git changes")

    monkeypatch.setattr(runtime, "_changed_files", fail_if_called)

    result = await runtime.list_workers()

    assert result["count"] == 1
    assert result["workers"][0]["has_changes"] is False
    assert result["workers"][0]["changes_checked"] is False


@pytest.mark.asyncio
async def test_worker_file_view_pages_large_text_files(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["file_response_max_bytes"] = 120
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Report Writer",
        brief="Write a report.",
        repo_path=config["repositories"]["default"],
    )
    job = manager.get_job(executor.started[-1])
    report = Path(job.worktree_path) / "worker-report-large.md"
    report.write_text("\n".join(f"line {i} " + ("x" * 20) for i in range(1, 30)), encoding="utf-8")

    first = await runtime.inspect_worker(
        worker=started["worker_id"],
        view="file",
        file_path="worker-report-large.md",
        max_bytes=90_000,
    )

    assert first["exists"] is True
    assert first["truncated"] is True
    assert first["max_bytes_applied"] == 120
    assert first["next_start_line"] > first["start_line"]
    assert "capped to 120 bytes" in first["note"]
    assert "worker-report-large.md" in [item["file_path"] for item in first["worker_report_files"]]
    assert first["worker_report_files"][0]["location"] == "worker_worktree_only"

    second = await runtime.inspect_worker(
        worker=started["worker_id"],
        view="file",
        file_path="worker-report-large.md",
        start_line=first["next_start_line"],
        max_bytes=120,
    )

    assert second["start_line"] == first["next_start_line"]
    assert second["text"]


@pytest.mark.asyncio
async def test_duplicate_worker_names_are_rejected_case_insensitively(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    await runtime.start_worker(
        name="Reviewer",
        brief="Review the design.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )

    with pytest.raises(ValueError, match="already exists"):
        await runtime.start_worker(
            name=" reviewer ",
            brief="Review again.",
            repo_path=config["repositories"]["default"],
            workspace_mode="read_only",
        )


@pytest.mark.asyncio
async def test_same_worker_name_is_allowed_in_different_workspaces_and_scoped_on_lookup(tmp_path):
    config = make_config(tmp_path)
    other_repo = init_extra_repo(tmp_path / "other-repo", name="other")
    config["repositories"]["allowed"].append(str(other_repo))
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    first = await runtime.start_worker(
        name="Reviewer",
        brief="Review the default repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    second = await runtime.start_worker(
        name="reviewer",
        brief="Review the other repo.",
        repo_path=str(other_repo),
        workspace_mode="read_only",
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert first["worker_id"] != second["worker_id"]

    default_view = await runtime.inspect_worker(worker="Reviewer", repo_path=config["repositories"]["default"])
    other_view = await runtime.inspect_worker(worker="Reviewer", repo_path=str(other_repo))
    assert default_view["worker_id"] == first["worker_id"]
    assert other_view["worker_id"] == second["worker_id"]

    with pytest.raises(ValueError, match="ambiguous"):
        await runtime.inspect_worker(worker="Reviewer")


@pytest.mark.asyncio
async def test_stop_cancels_active_turn_but_preserves_worker(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Stopping Worker",
        brief="Inspect the project.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = next(iter(manager.jobs.values()))
    manager.update_job_state(job.job_id, JobState.RUNNING)

    result = await runtime.stop_worker(worker=started["worker_id"], force=True)

    assert result["stopped"] is True
    assert result["state"] == "stopped"
    assert executor.cancelled == [job.job_id]
    listed = await runtime.list_workers(scope="history")
    assert listed["count"] == 1


@pytest.mark.asyncio
async def test_worker_status_soft_cooldown_does_not_reset_deltas(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["status_minimum_poll_seconds"] = 20
    config["workers"]["status_recommended_poll_seconds"] = 30
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Polling Worker",
        brief="Inspect the repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[-1])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-polling",
        last_heartbeat_at=time.time(),
        event_count=1,
    )

    first = await runtime.worker_status(repo_path=config["repositories"]["default"])
    second = await runtime.worker_status(repo_path=config["repositories"]["default"])

    assert first["poll_too_early"] is False
    assert first["status_current"] is True
    assert second["poll_too_early"] is True
    assert second["status_current"] is False
    assert second["retry_after_seconds"] >= 1
    assert second["workers"][0]["worker_id"] == started["worker_id"]


@pytest.mark.asyncio
async def test_worker_list_uses_soft_monitoring_cooldown(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["status_minimum_poll_seconds"] = 20
    config["workers"]["status_recommended_poll_seconds"] = 30
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Listed Worker",
        brief="Inspect the repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )

    first = await runtime.list_workers(repo_path=config["repositories"]["default"])
    second = await runtime.list_workers(repo_path=config["repositories"]["default"])

    assert first["poll_too_early"] is False
    assert first["status_current"] is True
    assert second["poll_too_early"] is True
    assert second["status_current"] is False
    assert second["workers"][0]["worker_id"] == started["worker_id"]


@pytest.mark.asyncio
async def test_worker_monitoring_cache_is_cleared_by_new_worker(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["status_minimum_poll_seconds"] = 20
    config["workers"]["status_recommended_poll_seconds"] = 30
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    empty = await runtime.list_workers(repo_path=config["repositories"]["default"])
    assert empty["count"] == 0

    started = await runtime.start_worker(
        name="Fresh Worker",
        brief="Inspect the repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    listed = await runtime.list_workers(repo_path=config["repositories"]["default"])

    assert listed["poll_too_early"] is False
    assert listed["count"] == 1
    assert listed["workers"][0]["worker_id"] == started["worker_id"]


@pytest.mark.asyncio
async def test_worker_inspect_status_uses_soft_monitoring_cooldown(tmp_path):
    config = make_config(tmp_path)
    config["workers"]["status_minimum_poll_seconds"] = 20
    config["workers"]["status_recommended_poll_seconds"] = 30
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Inspectable Worker",
        brief="Inspect the repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = manager.get_job(executor.started[-1])
    manager.update_job_state(
        job.job_id,
        JobState.RUNNING,
        session_id="session-inspectable",
        last_heartbeat_at=time.time(),
        event_count=1,
    )

    first = await runtime.inspect_worker(worker=started["worker_id"], view="status")
    second = await runtime.inspect_worker(worker=started["worker_id"], view="status")

    assert first["poll_too_early"] is False
    assert first["status_current"] is True
    assert second["poll_too_early"] is True
    assert second["status_current"] is False
    assert second["worker_id"] == started["worker_id"]


@pytest.mark.asyncio
async def test_worker_wait_returns_fresh_status_after_delay(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    config["workers"]["status_minimum_poll_seconds"] = 20
    config["workers"]["status_recommended_poll_seconds"] = 30
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    await runtime.start_worker(
        name="Wait Worker",
        brief="Inspect the repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("patchbay.workers.runtime.asyncio.sleep", fake_sleep)

    result = await runtime.worker_wait(repo_path=config["repositories"]["default"], wait_seconds=1)

    assert result["status_current"] is True
    assert result["poll_too_early"] is False
    assert result["requested_wait_seconds"] == 1
    assert result["minimum_wait_seconds_applied"] == 20
    assert result["waited_seconds"] >= 20
    assert "patient manager path" in result["wait_guidance"]
    assert "configured minimum" in result["wait_guidance"]


@pytest.mark.asyncio
async def test_worker_wait_respects_recent_status_cooldown(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    config["workers"]["status_minimum_poll_seconds"] = 20
    config["workers"]["status_recommended_poll_seconds"] = 30
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    await runtime.start_worker(
        name="Patient Worker",
        brief="Inspect the repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    await runtime.worker_status(repo_path=config["repositories"]["default"])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        return None

    monkeypatch.setattr("patchbay.workers.runtime.asyncio.sleep", fake_sleep)

    result = await runtime.worker_wait(repo_path=config["repositories"]["default"], wait_seconds=1)

    assert sleeps
    assert sleeps[0] >= 20
    assert result["status_current"] is True
    assert result["poll_too_early"] is False


@pytest.mark.asyncio
async def test_message_rechecks_current_allowed_roots(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Scoped Worker",
        brief="Inspect the repo.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = next(iter(manager.jobs.values()))
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "done"},
        session_id="session-scoped",
        exit_code=0,
    )
    config["repositories"]["allowed"] = []

    with pytest.raises(ValueError, match="No allowed repository roots configured"):
        await runtime.message_worker(worker=started["worker_id"], message="Continue.")


def test_cleanup_keeps_worker_jobs_as_durable_worker_identity(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job(
        "interactive",
        "inspect",
        config["repositories"]["default"],
        {
            WORKER_ID_OPTION: "wrk_fixture",
            WORKER_NAME_OPTION: "Fixture Worker",
            "sandbox": "read-only",
        },
    )
    manager.update_job_state(job_id, JobState.COMPLETED, result={"summary": "done"}, session_id="session-1")
    manager.jobs[job_id].completed_at = time.time() - (48 * 3600)
    manager._persist_job(manager.jobs[job_id])

    manager.cleanup_old_jobs()

    assert manager.get_job(job_id) is not None


@pytest.mark.asyncio
async def test_isolated_worker_continues_in_same_worktree_and_reports_changes(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)
    client_a = request_context("client_a", "Chat A")

    started = await runtime.start_worker(
        name="Implementer",
        brief="Create a file.",
        repo_path=config["repositories"]["default"],
        request_context=client_a,
    )
    await asyncio.sleep(0)
    first_job = manager.get_job(executor.started[0])
    worker_path = first_job.worktree_path
    assert worker_path
    worker_file = Path(worker_path) / "worker.txt"
    worker_file.write_text("first turn\n", encoding="utf-8")
    manager.update_job_state(
        first_job.job_id,
        JobState.COMPLETED,
        result={"summary": "Created worker.txt"},
        session_id="session-write",
        exit_code=0,
    )

    changes = await runtime.inspect_worker(worker="Implementer", view="changes")
    assert changes["has_changes"] is True
    assert changes["changed_files"] == ["worker.txt"]
    assert "worker.txt" in changes["report"]
    assert str(worker_path) not in str(changes)

    diff = await runtime.inspect_worker(worker="Implementer", view="diff", file_path="worker.txt")
    assert "+first turn" in diff["diff"]
    assert str(worker_path) not in diff["diff"]
    assert not (Path(config["repositories"]["default"]) / "worker.txt").exists()

    reloaded_manager = JobManager(config)
    reloaded_executor = RecordingExecutor(reloaded_manager)
    reloaded_runtime = WorkerRuntime(config, reloaded_manager, reloaded_executor)
    continued = await reloaded_runtime.message_worker(
        worker="Implementer",
        message="Revise the same file.",
        request_context=client_a,
    )
    await asyncio.sleep(0)

    assert continued["accepted"] is True
    resume_job = reloaded_manager.get_job(reloaded_executor.started[-1])
    assert resume_job.mode == "resume"
    assert resume_job.worktree_path == worker_path
    assert resume_job.options["resume_session_id"] == "session-write"
    assert resume_job.options["_worker_worktree_path"] == worker_path
    assert resume_job.options["_codex_cwd"] == worker_path
    assert resume_job.options["sandbox"] == "workspace-write"
    assert resume_job.options[OWNER_SESSION_HASH_OPTION] == "client_a"
    assert resume_job.options[OWNER_CLIENT_REF_OPTION] == "client_a"


@pytest.mark.asyncio
async def test_cleanup_workspace_discards_isolated_worktree_without_deleting_worker(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(manager)
    runtime = WorkerRuntime(config, manager, executor)

    started = await runtime.start_worker(
        name="Disposable Implementer",
        brief="Create work.",
        repo_path=config["repositories"]["default"],
    )
    await asyncio.sleep(0)
    job = manager.get_job(executor.started[0])
    worktree_path = Path(job.worktree_path)
    manager.update_job_state(job.job_id, JobState.COMPLETED, result={"summary": "done"}, session_id="session-clean")

    result = await runtime.stop_worker(worker=started["worker_id"], cleanup_workspace=True)

    assert result["workspace_cleaned"] is True
    assert result["workspace_available"] is False
    assert worktree_path.exists() is False
    listed = await runtime.list_workers(scope="history")
    assert listed["count"] == 1

    rejected = await runtime.message_worker(worker=started["worker_id"], message="Continue.")
    assert rejected["accepted"] is False
    assert "will not fall back" in rejected["note"]
