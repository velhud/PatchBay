import subprocess
import time
from copy import deepcopy
from pathlib import Path

import pytest

import patchbay.workers.runtime as runtime_module
from patchbay.jobs.manager import JobManager, JobState
from patchbay.protocol.context import RequestContext
from patchbay.workers.runtime import WORKER_INTEGRATION_TOKENS_OPTION, WorkerRuntime
from patchbay.workers.tool_surface import WORKER_TOOLS, WORKER_VIEW_SCHEMA


def init_repo(repo: Path) -> None:
    repo.mkdir()
    (repo / "README.md").write_text("# worker integration tokens\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    commit(repo, "init")


def commit(repo: Path, message: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Worker Test",
            "-c",
            "user.email=worker-test@example.invalid",
            "commit",
            "-m",
            message,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def make_config(tmp_path: Path, *, token_ttl_seconds: float = 300) -> dict:
    repo = tmp_path / "repo"
    init_repo(repo)
    return {
        "server": {"max_concurrent_jobs": 4, "job_timeout_seconds": 30, "job_cleanup_after_hours": 24},
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "workers": {"worktree_root": str(tmp_path / "worker-worktrees")},
        "hub": {"integration_preview_token_ttl_seconds": token_ttl_seconds},
        "security": {
            "require_git_repo": True,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".env", ".env.*", "**/.env", "**/.env.*", ".git", ".git/**", "**/.git/**"],
            "max_diff_bytes": 200_000,
        },
        "power_tools": {"direct_write": False, "bash_mode": "off"},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "locks": {"root": str(tmp_path / "locks")},
    }


class RecordingExecutor:
    def __init__(self, manager: JobManager):
        self.manager = manager
        self.started: list[str] = []

    def schedule_job(self, job_id: str) -> None:
        self.started.append(job_id)

    async def cancel_job(self, job_id: str, reason: str = "Cancelled by request") -> dict:
        self.manager.update_job_state(job_id, JobState.CANCELLED, error=reason)
        return {"cancelled": True, "job_id": job_id, "state": "cancelled"}


def hub_context(*, principal: str = "principal-a", participant: str = "participant-a") -> RequestContext:
    return RequestContext(
        client_ref=f"transport-{participant}",
        owner_ref=principal,
        chatgpt_session_ref=participant,
        work_group_id="group-a",
        lane_id="implementation",
        tool_mode="hub",
    )


async def completed_worker(
    runtime: WorkerRuntime,
    manager: JobManager,
    *,
    name: str = "Implementer",
    file_name: str = "worker-note.txt",
    contents: str = "from worker\n",
    request_context: RequestContext | None = None,
) -> tuple[dict, object]:
    started = await runtime.start_worker(
        name=name,
        brief="Create the requested test note.",
        repo_path=runtime.config["repositories"]["default"],
        request_context=request_context,
    )
    job = next(
        job
        for job in manager.jobs.values()
        if (job.options or {}).get("_worker_id") == started["worker_id"]
    )
    Path(job.worktree_path, file_name).write_text(contents, encoding="utf-8")
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": f"Created {file_name}"},
        session_id=f"session-{name}",
    )
    return started, job


def runtime_for(config: dict) -> tuple[JobManager, WorkerRuntime]:
    manager = JobManager(config)
    return manager, WorkerRuntime(config, manager, RecordingExecutor(manager))


@pytest.mark.asyncio
async def test_hub_requires_opaque_token_and_binds_principal_participant_and_worker(tmp_path):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    context = hub_context()
    await completed_worker(runtime, manager, request_context=context)
    await completed_worker(
        runtime,
        manager,
        name="Other Worker",
        file_name="other-note.txt",
        request_context=context,
    )

    missing = await runtime.integrate_worker(
        worker="Implementer",
        request_context=context,
        idempotency_key="apply-1",
    )
    assert missing["reason"] == "preview_token_required"

    preview = await runtime.inspect_worker(
        worker="Implementer",
        view="integration_preview",
        request_context=context,
    )
    token = preview["preview_token"]
    assert token.startswith("pit2.")
    assert preview["patch_sha256"] not in token
    assert preview["worker_id"] not in token
    assert str(tmp_path) not in token

    wrong_principal = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=token,
        idempotency_key="apply-1",
        request_context=hub_context(principal="principal-b"),
        takeover=True,
    )
    assert wrong_principal["reason"] == "preview_token_principal_mismatch"

    wrong_participant = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=token,
        idempotency_key="apply-1",
        request_context=hub_context(participant="participant-b"),
    )
    assert wrong_participant["reason"] == "preview_token_participant_mismatch"

    wrong_worker = await runtime.integrate_worker(
        worker="Other Worker",
        preview_token=token,
        idempotency_key="apply-1",
        request_context=context,
    )
    assert wrong_worker["reason"] == "invalid_preview_token"

    tampered = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=token[:-1] + ("A" if token[-1] != "A" else "B"),
        idempotency_key="apply-1",
        request_context=context,
    )
    assert tampered["reason"] == "invalid_preview_token"

    edge_forwarded = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=token,
        request_context=context,
    )
    assert edge_forwarded["applied"] is True


@pytest.mark.asyncio
async def test_preview_token_rejects_patch_base_dirty_and_pattern_staleness(tmp_path):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    context = hub_context()
    _, job = await completed_worker(runtime, manager, request_context=context)

    patch_preview = await runtime.inspect_worker(
        worker="Implementer", view="integration_preview", request_context=context
    )
    Path(job.worktree_path, "worker-note.txt").write_text("changed after preview\n", encoding="utf-8")
    stale_patch = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=patch_preview["preview_token"],
        idempotency_key="patch-stale",
        request_context=context,
    )
    assert stale_patch["reason"] == "stale_preview_token"
    assert "patch_sha256" in stale_patch["stale_bindings"]
    assert stale_patch["retryable"] is True
    assert stale_patch["recommended_next_action"] == "review_fresh_integration_preview"
    assert stale_patch["fresh_preview"]["can_apply"] is True
    assert stale_patch["next_arguments"]["preview_token"] == stale_patch["fresh_preview"]["preview_token"]

    fresh_patch = await runtime.inspect_worker(
        worker="Implementer", view="integration_preview", request_context=context
    )
    base = Path(config["repositories"]["default"])
    (base / "README.md").write_text("# base moved\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=base, check=True, capture_output=True)
    commit(base, "move base")
    stale_head = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=fresh_patch["preview_token"],
        idempotency_key="head-stale",
        request_context=context,
    )
    assert stale_head["reason"] == "stale_preview_token"
    assert "base_head" in stale_head["stale_bindings"]

    clean_head = await runtime.inspect_worker(
        worker="Implementer", view="integration_preview", request_context=context
    )
    (base / "phase.md").write_text("phase one\n", encoding="utf-8")
    stale_dirty = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=clean_head["preview_token"],
        idempotency_key="dirty-stale",
        request_context=context,
    )
    assert stale_dirty["reason"] == "stale_preview_token"
    assert "dirty_worktree_fingerprint" in stale_dirty["stale_bindings"]

    accepted = await runtime.inspect_worker(
        worker="Implementer",
        view="integration_preview",
        accepted_dirty_base=["phase*.md"],
        request_context=context,
    )
    assert accepted["can_apply"] is True
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "Revised report after preview"},
        session_id="session-Implementer",
    )
    stale_revision = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=accepted["preview_token"],
        accepted_dirty_base=["phase*.md"],
        idempotency_key="revision-stale",
        request_context=context,
    )
    assert stale_revision["reason"] == "stale_preview_token"
    assert "worker_revision" in stale_revision["stale_bindings"]

    accepted = await runtime.inspect_worker(
        worker="Implementer",
        view="integration_preview",
        accepted_dirty_base=["phase*.md"],
        request_context=context,
    )
    stale_patterns = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=accepted["preview_token"],
        accepted_dirty_base=["other*.md"],
        idempotency_key="patterns-stale",
        request_context=context,
    )
    assert stale_patterns["reason"] == "stale_preview_token"
    assert "accepted_dirty_base" in stale_patterns["stale_bindings"]


@pytest.mark.asyncio
async def test_expired_preview_token_is_blocked(tmp_path):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    context = hub_context()
    _, job = await completed_worker(runtime, manager, request_context=context)
    preview = await runtime.inspect_worker(
        worker="Implementer", view="integration_preview", request_context=context
    )

    options = dict(job.options or {})
    state = deepcopy(options[WORKER_INTEGRATION_TOKENS_OPTION])
    state["tokens"][preview["preview_token_id"]]["claims"]["expires_at"] = time.time() - 1
    options[WORKER_INTEGRATION_TOKENS_OPTION] = state
    manager.update_job_options(job.job_id, options)

    expired = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=preview["preview_token"],
        idempotency_key="expired",
        request_context=context,
    )
    assert expired["reason"] == "preview_token_expired"
    assert expired["applied"] is False


@pytest.mark.asyncio
async def test_successful_token_replay_returns_prior_result_without_second_apply(tmp_path):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    context = hub_context()
    _, job = await completed_worker(runtime, manager, request_context=context)
    preview = await runtime.inspect_worker(
        worker="Implementer", view="integration_preview", request_context=context
    )

    applied = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=preview["preview_token"],
        idempotency_key="apply-once",
        request_context=context,
    )
    assert applied["applied"] is True
    assert applied["idempotent_replay"] is False
    base_file = Path(config["repositories"]["default"], "worker-note.txt")
    assert base_file.read_text(encoding="utf-8") == "from worker\n"

    conflict = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=preview["preview_token"],
        idempotency_key="different-key",
        request_context=context,
    )
    assert conflict["reason"] == "idempotency_payload_conflict"

    options = dict(job.options or {})
    state = deepcopy(options[WORKER_INTEGRATION_TOKENS_OPTION])
    state["tokens"][preview["preview_token_id"]]["claims"]["expires_at"] = time.time() - 1
    options[WORKER_INTEGRATION_TOKENS_OPTION] = state
    manager.update_job_options(job.job_id, options)

    replay = await runtime.integrate_worker(
        worker="Implementer",
        preview_token=preview["preview_token"],
        idempotency_key="apply-once",
        request_context=context,
    )
    assert replay["applied"] is True
    assert replay["idempotent_replay"] is True
    assert replay["patch_sha256"] == applied["patch_sha256"]
    assert base_file.read_text(encoding="utf-8") == "from worker\n"


@pytest.mark.asyncio
async def test_crash_before_git_apply_retries_from_durable_applying_disposition(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    context = hub_context()
    _, job = await completed_worker(runtime, manager, request_context=context)
    preview = await runtime.inspect_worker(
        worker="Implementer", view="integration_preview", request_context=context
    )
    real_run = runtime_module.subprocess.run

    def fail_before_apply(command, *args, **kwargs):
        if command[:2] == ["git", "apply"] and "--check" not in command:
            raise RuntimeError("simulated crash before git apply")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(runtime_module.subprocess, "run", fail_before_apply)
    with pytest.raises(RuntimeError, match="before git apply"):
        await runtime.integrate_worker(
            worker="Implementer",
            preview_token=preview["preview_token"],
            idempotency_key="crash-before",
            request_context=context,
        )
    persisted = manager.get_job(job.job_id).options[WORKER_INTEGRATION_TOKENS_OPTION]
    record = persisted["tokens"][preview["preview_token_id"]]
    assert record["disposition"] == "applying"
    assert record["idempotency_key"] == "crash-before"
    assert not Path(config["repositories"]["default"], "worker-note.txt").exists()

    monkeypatch.setattr(runtime_module.subprocess, "run", real_run)
    recovered_manager, recovered_runtime = runtime_for(config)
    recovered = await recovered_runtime.integrate_worker(
        worker="Implementer",
        preview_token=preview["preview_token"],
        idempotency_key="crash-before",
        request_context=context,
    )
    assert recovered["applied"] is True
    assert recovered["idempotent_replay"] is False
    assert Path(config["repositories"]["default"], "worker-note.txt").exists()
    recovered_job = recovered_manager.get_job(job.job_id)
    recovered_record = recovered_job.options[WORKER_INTEGRATION_TOKENS_OPTION]["tokens"][preview["preview_token_id"]]
    assert recovered_record["disposition"] == "applied"


@pytest.mark.asyncio
async def test_crash_after_git_apply_reconciles_reverse_check_and_returns_replay(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    context = hub_context()
    _, job = await completed_worker(runtime, manager, request_context=context)
    preview = await runtime.inspect_worker(
        worker="Implementer", view="integration_preview", request_context=context
    )
    real_persist = runtime._persist_integration_token_state
    failed_once = False

    def lose_response_after_apply(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get("integrated_result") is not None and not failed_once:
            failed_once = True
            raise RuntimeError("simulated response loss after git apply")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(runtime, "_persist_integration_token_state", lose_response_after_apply)
    with pytest.raises(RuntimeError, match="response loss"):
        await runtime.integrate_worker(
            worker="Implementer",
            preview_token=preview["preview_token"],
            idempotency_key="crash-after",
            request_context=context,
        )
    assert Path(config["repositories"]["default"], "worker-note.txt").exists()
    persisted = manager.get_job(job.job_id).options[WORKER_INTEGRATION_TOKENS_OPTION]
    assert persisted["tokens"][preview["preview_token_id"]]["disposition"] == "applying"

    recovered_manager, recovered_runtime = runtime_for(config)
    recovered = await recovered_runtime.integrate_worker(
        worker="Implementer",
        preview_token=preview["preview_token"],
        idempotency_key="crash-after",
        request_context=context,
    )
    assert recovered["applied"] is True
    assert recovered["idempotent_replay"] is True
    assert recovered["reconciled_after_crash"] is True
    recovered_job = recovered_manager.get_job(job.job_id)
    recovered_record = recovered_job.options[WORKER_INTEGRATION_TOKENS_OPTION]["tokens"][preview["preview_token_id"]]
    assert recovered_record["disposition"] == "applied"
    assert recovered_record["reconciled_after_crash"] is True


@pytest.mark.asyncio
async def test_cleanup_requires_explicit_confirmation_only_for_unintegrated_changes(tmp_path):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    started, job = await completed_worker(runtime, manager)
    worktree = Path(job.worktree_path)

    preserved = await runtime.stop_worker(worker=started["worker_id"], cleanup_workspace=True)
    assert preserved["workspace_cleaned"] is False
    assert preserved["discard_confirmation_required"] is True
    assert preserved["unintegrated_changed_files"] == ["worker-note.txt"]
    assert worktree.exists()

    discarded = await runtime.stop_worker(
        worker=started["worker_id"],
        cleanup_workspace=True,
        discard_unintegrated_changes=True,
    )
    assert discarded["workspace_cleaned"] is True
    assert discarded["discard_confirmation_required"] is False
    assert not worktree.exists()

    safe_started = await runtime.start_worker(
        name="No Changes",
        brief="Inspect only.",
        repo_path=config["repositories"]["default"],
    )
    safe_job = next(
        item for item in manager.jobs.values() if (item.options or {}).get("_worker_id") == safe_started["worker_id"]
    )
    manager.update_job_state(safe_job.job_id, JobState.COMPLETED, result={"summary": "No changes"})
    safe_cleanup = await runtime.stop_worker(worker=safe_started["worker_id"], cleanup_workspace=True)
    assert safe_cleanup["workspace_cleaned"] is True
    assert safe_cleanup["discard_confirmation_required"] is False

    integrated_started, integrated_job = await completed_worker(
        runtime,
        manager,
        name="Integrated Changes",
        file_name="integrated-note.txt",
    )
    integrated = await runtime.integrate_worker(worker=integrated_started["worker_id"])
    assert integrated["applied"] is True
    integrated_cleanup = await runtime.stop_worker(
        worker=integrated_started["worker_id"],
        cleanup_workspace=True,
    )
    assert integrated_cleanup["workspace_cleaned"] is True
    assert integrated_cleanup["discard_confirmation_required"] is False
    assert not Path(integrated_job.worktree_path).exists()


@pytest.mark.asyncio
async def test_local_single_machine_integration_remains_token_optional(tmp_path):
    config = make_config(tmp_path)
    manager, runtime = runtime_for(config)
    await completed_worker(runtime, manager)

    applied = await runtime.integrate_worker(worker="Implementer")

    assert applied["applied"] is True
    assert Path(config["repositories"]["default"], "worker-note.txt").exists()


def test_canonical_worker_descriptors_expose_optional_v2_fields():
    by_name = {tool["name"]: tool for tool in WORKER_TOOLS}
    integrate = by_name["codex_worker_integrate"]["inputSchema"]
    stop = by_name["codex_worker_stop"]["inputSchema"]

    assert integrate["properties"]["preview_token"]["type"] == "string"
    assert integrate["properties"]["idempotency_key"]["type"] == "string"
    assert "preview_token" not in integrate["required"]
    assert stop["properties"]["discard_unintegrated_changes"]["type"] == "boolean"
    assert "discard_unintegrated_changes" not in stop["required"]
    assert WORKER_VIEW_SCHEMA["properties"]["preview_token_expires_at"]["type"] == "number"
    assert WORKER_VIEW_SCHEMA["properties"]["discard_confirmation_required"]["type"] == "boolean"
