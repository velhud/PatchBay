"""Regression coverage for the 2026-10-18 public PatchBay audit.

These tests use disposable repositories and no model inference.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.pro_requests.store import ProRequestStore
from patchbay.repo_locks import job_requires_repo_mutation_lock
from patchbay.tools.handler import ToolHandler
from patchbay.workers.integration_state import build_candidate_snapshot, create_snapshot_commit
from patchbay.workers.runtime import WorkerRuntime
from patchbay.workspace.context import WorkspaceContext


def git(repo: Path, *args: str, text: bool = True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=text
    ).stdout


def configuration(root: Path) -> dict:
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    git(repo, "init")
    git(repo, "add", "README.md")
    git(
        repo,
        "-c",
        "user.name=Regression",
        "-c",
        "user.email=regression@example.invalid",
        "commit",
        "-m",
        "Initial",
    )
    return {
        "server": {
            "max_concurrent_jobs": 10,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "workers": {"worktree_root": str(root / "worker-worktrees")},
        "security": {
            "require_git_repo": True,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "blocked_globs": [".git", ".git/**"],
            "max_diff_bytes": 500_000,
            "max_read_bytes": 5_000,
        },
        "logging": {
            "job_logs_dir": str(root / "logs"),
            "job_state_dir": str(root / "state"),
            "worktrees_dir": str(root / "job-worktrees"),
        },
        "locks": {"root": str(root / "locks")},
        "artifacts": {"root": str(root / "artifacts")},
        "pro_requests": {
            "root": str(root / "pro-requests"),
            "mirror_enabled": False,
            "max_response_bytes": 200_000,
        },
    }


class NoModelExecutor:
    def schedule_job(self, job_id: str) -> None:
        return None

    async def reconcile_stale_running_jobs_async(self):
        return {}

    def runtime_liveness_snapshot(self, job_id: str):
        return {
            "runtime_alive": False,
            "process_alive": False,
            "executor_task_alive": False,
        }


async def completed_worker(root: Path, name: str = "Luna"):
    config = configuration(root)
    manager = JobManager(config)
    runtime = WorkerRuntime(config, manager, NoModelExecutor())
    await runtime.start_worker(
        name=name,
        brief="Implement this bounded regression fixture.",
        repo_path=config["repositories"]["default"],
        model="gpt-6-luna",
        reasoning_effort="high",
    )
    job = list(manager.jobs.values())[-1]
    return config, manager, runtime, job


def complete(manager: JobManager, job) -> None:
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        session_id="session-regression",
        result={"summary": "Fixture turn complete"},
        wrapper_cleanup_outcome="process_exited",
    )


def test_candidate_snapshot_preserves_git_file_semantics(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Regression",
        "-c",
        "user.email=regression@example.invalid",
        "commit",
        "-m",
        "base",
    )
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "name with spaces.txt").write_text("space\n")
    (repo / "café.txt").write_text("unicode\n")
    (repo / "script.sh").write_text("#!/bin/sh\necho ok\n")
    (repo / "script.sh").chmod(0o755)
    (repo / "link.txt").symlink_to("README.md")
    (repo / "no-newline.txt").write_bytes(b"abc")
    (repo / "binary.bin").write_bytes(b"a\0b\xff")
    (repo / "README.md").write_text("committed worker state\n")
    git(repo, "add", "README.md")
    git(
        repo,
        "-c",
        "user.name=Regression",
        "-c",
        "user.email=regression@example.invalid",
        "commit",
        "-m",
        "worker commit",
    )

    first = build_candidate_snapshot(repo, base)
    assert {
        "README.md",
        "name with spaces.txt",
        "café.txt",
        "script.sh",
        "link.txt",
        "no-newline.txt",
        "binary.bin",
    } <= set(first.changed_files)
    snapshot = create_snapshot_commit(
        repo,
        tree_sha=first.tree_sha,
        parent_revision=base,
        ref_name="refs/patchbay/tests/integrated",
    )
    (repo / "second.txt").write_text("follow-up\n")
    second = build_candidate_snapshot(repo, snapshot)
    assert second.changed_files == ("second.txt",)


@pytest.mark.asyncio
async def test_worker_integration_preserves_paths_modes_and_cleanup(tmp_path: Path):
    config, manager, runtime, job = await completed_worker(tmp_path)
    worker = Path(job.worktree_path)
    base = Path(config["repositories"]["default"])
    (worker / "name with spaces.txt").write_text("space\n")
    (worker / "café.txt").write_text("unicode\n")
    (worker / "script.sh").write_text("#!/bin/sh\necho ok\n")
    (worker / "script.sh").chmod(0o755)
    (worker / "link.txt").symlink_to("README.md")
    (worker / "no-newline.txt").write_bytes(b"abc")
    (worker / "binary.bin").write_bytes(b"a\0b\xff")
    complete(manager, job)

    preview = await runtime.inspect_worker(worker="Luna", view="integration_preview")
    assert "name with spaces.txt" in preview["changed_files"]
    assert "café.txt" in preview["changed_files"]
    applied = await runtime.integrate_worker(
        worker="Luna", preview_token=preview["preview_token"]
    )
    assert applied["applied"] and not applied["skipped_files"]
    assert (base / "name with spaces.txt").read_text() == "space\n"
    assert (base / "café.txt").read_text() == "unicode\n"
    assert (base / "script.sh").stat().st_mode & 0o111
    assert (base / "link.txt").is_symlink()
    assert os.readlink(base / "link.txt") == "README.md"
    assert (base / "no-newline.txt").read_bytes() == b"abc"
    assert (base / "binary.bin").read_bytes() == b"a\0b\xff"

    stopped = await runtime.stop_worker(worker="Luna", cleanup_workspace=True)
    assert stopped["workspace_cleaned"]
    assert not stopped["discard_confirmation_required"]
    assert (base / "name with spaces.txt").exists()


@pytest.mark.asyncio
async def test_followup_integration_uses_last_delivered_snapshot(tmp_path: Path):
    config, manager, runtime, first_job = await completed_worker(tmp_path)
    worker = Path(first_job.worktree_path)
    base = Path(config["repositories"]["default"])
    (worker / "first.txt").write_text("first\n")
    complete(manager, first_job)
    first_preview = await runtime.inspect_worker(worker="Luna", view="integration_preview")
    assert (await runtime.integrate_worker(
        worker="Luna", preview_token=first_preview["preview_token"]
    ))["applied"]

    followup = await runtime.message_worker(
        worker="Luna", message="Make the independent second change."
    )
    assert followup["accepted"]
    second_job = list(manager.jobs.values())[-1]
    (worker / "second.txt").write_text("second\n")
    complete(manager, second_job)
    preview = await runtime.inspect_worker(
        worker="Luna",
        view="integration_preview",
        accepted_dirty_base=["first.txt"],
    )
    assert preview["changed_files"] == ["second.txt"]
    assert preview["integration_state"] == "not_integrated"
    projection = runtime._worker_projection(runtime._resolve_worker("Luna"))
    assert projection["review_disposition"] == "unreviewed"
    second = await runtime.integrate_worker(
        worker="Luna",
        preview_token=preview["preview_token"],
        accepted_dirty_base=["first.txt"],
    )
    assert second["applied"]
    assert (base / "first.txt").read_text() == "first\n"
    assert (base / "second.txt").read_text() == "second\n"


@pytest.mark.asyncio
async def test_committed_worker_change_is_integrated(tmp_path: Path):
    config, manager, runtime, job = await completed_worker(tmp_path)
    worker = Path(job.worktree_path)
    base = Path(config["repositories"]["default"])
    (worker / "README.md").write_text("worker committed implementation\n")
    git(worker, "add", "README.md")
    git(
        worker,
        "-c",
        "user.name=Regression",
        "-c",
        "user.email=regression@example.invalid",
        "commit",
        "-m",
        "Worker implementation",
    )
    complete(manager, job)
    preview = await runtime.inspect_worker(worker="Luna", view="integration_preview")
    assert preview["changed_files"] == ["README.md"]
    applied = await runtime.integrate_worker(
        worker="Luna", preview_token=preview["preview_token"]
    )
    assert applied["applied"]
    assert (base / "README.md").read_text() == "worker committed implementation\n"


@pytest.mark.asyncio
async def test_explicit_read_only_never_emits_bypass(tmp_path: Path):
    config = configuration(tmp_path)
    config["security"].update(
        default_sandbox="danger-full-access", allow_dangerously_bypass=True
    )
    manager = JobManager(config)
    runtime = WorkerRuntime(config, manager, NoModelExecutor())
    started = await runtime.start_worker(
        name="Reviewer",
        brief="Review only.",
        repo_path=config["repositories"]["default"],
        workspace_mode="read_only",
    )
    job = list(manager.jobs.values())[-1]
    command = JobExecutor(config, manager)._build_codex_command(
        job.mode, job.prompt, job.worktree_path, job.options
    )
    assert started["workspace_mode"] == "read_only"
    assert job.options["sandbox"] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert not job_requires_repo_mutation_lock(job.mode, job.options)


@pytest.mark.asyncio
async def test_worker_file_pagination_never_skips_or_renumbers(tmp_path: Path):
    _config, manager, runtime, job = await completed_worker(tmp_path)
    worker = Path(job.worktree_path)
    (worker / "review.txt").write_text("first\n" + "X" * 500 + "\nthird\n")
    complete(manager, job)
    first = await runtime.inspect_worker(
        worker="Luna", view="file", file_path="review.txt", max_bytes=100
    )
    assert first["text"] == "1 | first"
    assert first["next_start_line"] == 2
    second = await runtime.inspect_worker(
        worker="Luna",
        view="file",
        file_path="review.txt",
        start_line=2,
        max_bytes=100,
    )
    assert second["text"].startswith("2 | " + "X" * 500)
    assert second["next_start_line"] == 3
    third = await runtime.inspect_worker(
        worker="Luna",
        view="file",
        file_path="review.txt",
        start_line=3,
        max_bytes=100,
    )
    assert third["text"] == "3 | third"


def test_base_file_pagination_never_silently_clips_line(tmp_path: Path):
    config = configuration(tmp_path)
    repo = Path(config["repositories"]["default"])
    (repo / "source.txt").write_text("first\n" + "X" * 500 + "\nthird\n")
    context = WorkspaceContext(config)
    first = context.read_file(
        {"repo": str(repo), "file_path": "source.txt", "max_bytes": 20}
    )
    assert first["text"] == "1 | first"
    assert first["next_start_line"] == 2
    second = context.read_file(
        {
            "repo": str(repo),
            "file_path": "source.txt",
            "start_line": 2,
            "max_bytes": 20,
        }
    )
    assert second["text"].startswith("2 | " + "X" * 500)
    assert second["line_exceeded_requested_max_bytes"] == 2
    assert second["next_start_line"] == 3


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "pthread_sigmask"),
    reason="POSIX signal-mask contract",
)
def test_supervised_target_does_not_inherit_blocked_term_int(tmp_path: Path):
    from patchbay.jobs import process_supervisor

    output = tmp_path / "mask.txt"
    script = (
        "import pathlib, signal; "
        "mask=signal.pthread_sigmask(signal.SIG_BLOCK,set()); "
        f"pathlib.Path({str(output)!r}).write_text("
        "str(int(signal.SIGTERM in mask))+','+str(int(signal.SIGINT in mask)))"
    )
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    try:
        pid, _tracker, gate = process_supervisor._fork_target(
            [sys.executable, "-c", script]
        )
        os.write(gate, b"1")
        os.close(gate)
        waited, status = os.waitpid(pid, 0)
        assert waited == pid
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
    assert output.read_text() == "0,0"


class RecordingWorkers:
    def __init__(self):
        self.started: list[dict] = []

    async def start_worker(self, **kwargs):
        self.started.append(kwargs)
        return {"accepted": True, "worker_id": "worker-new"}


@pytest.mark.asyncio
async def test_pro_dispatch_is_complete_and_pins_new_worker_model(tmp_path: Path):
    config = configuration(tmp_path)
    handler = ToolHandler(config, JobManager(config), NoModelExecutor())
    workers = RecordingWorkers()
    handler.worker_runtime = workers
    report = tmp_path / "report.md"
    report.write_text("Implement the correction.\n")
    request = handler.pro_request_store.create_request(
        repo_path=config["repositories"]["default"],
        title="Response fidelity",
        report_path=str(report),
    )
    body = "A" * 60_000 + "\nFINAL_INSTRUCTION_MARKER"
    handler.pro_request_store.respond_request(
        request_id=request["id"],
        response_kind="implementation",
        response_markdown=body,
    )
    result = await handler._codex_pro_request_dispatch(
        {
            "request_id": request["id"],
            "target": "new_worker",
            "message_source": "response_markdown",
            "model": "gpt-6-luna",
            "reasoning_effort": "high",
        }
    )
    assert result["accepted"] and result["dispatch_text_complete"]
    assert workers.started[-1]["brief"].endswith("FINAL_INSTRUCTION_MARKER")
    assert workers.started[-1]["model"] == "gpt-6-luna"
    assert workers.started[-1]["reasoning_effort"] == "high"


def test_pro_request_mutations_are_serialized_and_revision_is_shared(tmp_path: Path):
    config = configuration(tmp_path)
    first = ProRequestStore(config)
    second = ProRequestStore(config)
    report = tmp_path / "report.md"
    report.write_text("Question\n")
    request = first.create_request(
        repo_path=config["repositories"]["default"],
        title="Concurrent updates",
        report_path=str(report),
    )
    initial_token = first.projection_state_revision

    # Slow both reads enough to exercise overlap. The cross-process/root guard
    # must ensure the second reader observes the first completed mutation.
    for store in (first, second):
        original = store._load_request
        def slow_load(request_id, original=original):
            value = original(request_id)
            time.sleep(0.05)
            return value
        store._load_request = slow_load

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim = pool.submit(
            first.claim_request, request_id=request["id"], note="CLAIM_MARKER"
        )
        response = pool.submit(
            second.respond_request,
            request_id=request["id"],
            response_kind="analysis",
            response_markdown="Response",
        )
        assert claim.result(timeout=10)["accepted"]
        assert response.result(timeout=10)["accepted"]

    manifest, _ = first._load_request(request["id"])
    assert manifest["revision"] == 3
    assert "CLAIM_MARKER" in str(manifest.get("claim"))
    assert manifest["response"]["exists"] is True
    assert first.projection_state_revision == second.projection_state_revision
    assert first.projection_state_revision > initial_token
