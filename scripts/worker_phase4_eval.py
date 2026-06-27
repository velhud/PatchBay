#!/usr/bin/env python3
"""Real-Codex Phase 4 eval: accept and integrate one worker result.

This exercises the worker facade directly with the installed Codex CLI. It does
not open a tunnel or attach ChatGPT Developer Mode; those remain separate
release evals.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from job_executor import JobExecutor
from job_manager import JobManager
from worker_runtime import WORKER_ID_OPTION, WORKER_WORKTREE_OPTION, WorkerRuntime


TARGET_FILE = "phase4_worker_result.txt"
MARKER = "phase4-accepted-result-marker"


def build_config(root: Path, state_root: Path, timeout: int) -> dict:
    return {
        "server": {
            "max_concurrent_jobs": 2,
            "job_timeout_seconds": timeout,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(root), "allowed": [str(root)]},
        "security": {
            "require_git_repo": True,
            "default_sandbox": "read-only",
            "allow_dangerously_bypass": False,
            "allowed_env_keys": ["PATH", "HOME", "USER", "SHELL", "TMPDIR", "OPENAI_API_KEY"],
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".env", ".env.*", "**/.env", "**/.env.*", ".git", ".git/**", "**/.git/**", "**/*secret*"],
            "max_diff_bytes": 200_000,
        },
        "logging": {
            "job_logs_dir": str(state_root / "jobs"),
            "job_state_dir": str(state_root / "jobs" / "state"),
            "job_log_max_bytes": 200_000,
            "write_raw_job_logs": False,
        },
        "workers": {
            "worktree_root": str(state_root / "worker-worktrees"),
            "ignore_user_config": True,
        },
    }


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "README.md").write_text(
        "# Worker Phase 4 Eval\n\nThis repo checks accepting one worker result into the base checkout.\n",
        encoding="utf-8",
    )
    (path / "AGENTS.md").write_text(
        "Use the repository root. Do not commit changes. Report concisely.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md", "AGENTS.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Eval", "-c", "user.email=worker-eval@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def git_status(path: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def wait_for_worker(runtime: WorkerRuntime, worker: str, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = await runtime.inspect_worker(worker=worker, wait_seconds=1)
        if view["state"] not in {"starting", "working"}:
            return view
    raise TimeoutError(f"Worker did not finish within {timeout} seconds")


def worker_jobs(manager: JobManager, worker_id: str):
    jobs = [job for job in manager.jobs.values() if (job.options or {}).get(WORKER_ID_OPTION) == worker_id]
    return sorted(jobs, key=lambda job: (job.completed_at or job.started_at or 0, job.job_id))


def worker_worktree(manager: JobManager, worker_id: str) -> Path:
    for job in reversed(worker_jobs(manager, worker_id)):
        worktree = (job.options or {}).get(WORKER_WORKTREE_OPTION) or job.worktree_path
        if worktree:
            return Path(str(worktree)).expanduser().resolve()
    raise RuntimeError("Worker has no recorded worktree")


def assert_no_private_paths(renderable: object, private_paths: list[Path]) -> None:
    rendered = json.dumps(renderable, sort_keys=True) if not isinstance(renderable, str) else renderable
    leaked = [str(path) for path in private_paths if str(path) in rendered]
    if leaked:
        raise RuntimeError(f"Private path leaked: {leaked}")


async def run_eval(timeout: int) -> dict:
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI is not available on PATH")

    with tempfile.TemporaryDirectory(prefix="codex-worker-phase4-") as temp:
        temp_root = Path(temp)
        repo = temp_root / "repo"
        state = temp_root / "state"
        init_repo(repo)
        config = build_config(repo, state, timeout)

        manager = JobManager(config)
        executor = JobExecutor(config, manager)
        runtime = WorkerRuntime(config, manager, executor)

        implementer = await runtime.start_worker(
            name="Phase 4 Implementer",
            brief=(
                f"Create {TARGET_FILE} in the repository root. Put exactly this line in it: {MARKER}. "
                "Do not commit. Report what you changed and what remains to verify."
            ),
            repo_path=str(repo),
            workspace_mode="isolated_write",
        )
        finished = await wait_for_worker(runtime, implementer["worker_id"], timeout)
        if finished["state"] != "idle" or not finished["has_session"]:
            raise RuntimeError(f"Implementer did not produce a resumable idle worker: {finished}")

        worktree = worker_worktree(manager, implementer["worker_id"])
        target = worktree / TARGET_FILE
        if not target.exists():
            raise RuntimeError(f"Implementer did not create {TARGET_FILE}")
        if MARKER not in target.read_text(encoding="utf-8"):
            raise RuntimeError(f"Expected marker missing from {TARGET_FILE}")
        if git_status(repo):
            raise RuntimeError(f"Base checkout became dirty before integration: {git_status(repo)!r}")

        preview = await runtime.inspect_worker(worker="Phase 4 Implementer", view="integration_preview")
        if not preview.get("can_apply"):
            raise RuntimeError(f"Integration preview did not pass: {preview}")
        if preview.get("applied"):
            raise RuntimeError(f"Preview must not apply changes: {preview}")
        assert_no_private_paths(preview, [repo.resolve(), worktree])

        applied = await runtime.integrate_worker(worker="Phase 4 Implementer")
        if not applied.get("applied"):
            raise RuntimeError(f"Worker result was not applied: {applied}")
        base_target = repo / TARGET_FILE
        if not base_target.exists():
            raise RuntimeError(f"Integrated file missing from base checkout: {TARGET_FILE}")
        if MARKER not in base_target.read_text(encoding="utf-8"):
            raise RuntimeError(f"Integrated file did not contain marker: {TARGET_FILE}")
        if TARGET_FILE not in git_status(repo):
            raise RuntimeError(f"Base checkout did not show integrated change: {git_status(repo)!r}")
        if not target.exists():
            raise RuntimeError("Worker worktree was removed during integration")
        assert_no_private_paths(applied, [repo.resolve(), worktree])

        listed = await runtime.list_workers()
        if "applied_to_checkout" not in json.dumps(listed):
            raise RuntimeError(f"Worker list did not preserve integration state: {listed}")
        assert_no_private_paths(listed, [repo.resolve(), worktree])

        return {
            "status": "passed",
            "worker": "Phase 4 Implementer",
            "integration_preview_clean": True,
            "worker_result_applied_to_base_checkout": True,
            "worker_worktree_preserved": True,
            "base_checkout_dirty_after_apply": True,
            "private_paths_returned": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    try:
        result = asyncio.run(run_eval(args.timeout))
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
