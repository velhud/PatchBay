#!/usr/bin/env python3
"""Real-Codex Phase 2 eval: isolated writing worker continuity.

This exercises the worker facade directly with the installed Codex CLI. It does
not open a tunnel or attach ChatGPT Developer Mode; that remains a separate
release eval.
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
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.workers.runtime import WORKER_ID_OPTION, WORKER_WORKTREE_OPTION, WorkerRuntime


TARGET_FILE = "worker_output.txt"
FIRST_MARKER = "phase2-first-turn"
SECOND_MARKER = "phase2-second-turn"


def build_config(root: Path, state_root: Path, timeout: int) -> dict:
    return {
        "server": {
            "max_concurrent_jobs": 1,
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
        "# Worker Phase 2 Eval\n\nThis repo checks isolated writing worker continuity.\n",
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


def worker_session(manager: JobManager, worker_id: str) -> str | None:
    for job in reversed(worker_jobs(manager, worker_id)):
        if job.session_id:
            return str(job.session_id)
        if (job.options or {}).get("resume_session_id"):
            return str(job.options["resume_session_id"])
    return None


def worker_worktree(manager: JobManager, worker_id: str) -> Path:
    for job in reversed(worker_jobs(manager, worker_id)):
        worktree = (job.options or {}).get(WORKER_WORKTREE_OPTION) or job.worktree_path
        if worktree:
            return Path(str(worktree)).expanduser().resolve()
    raise RuntimeError("Worker has no recorded worktree")


def assert_no_private_paths(public_payloads: list[dict], private_paths: list[Path]) -> None:
    rendered = json.dumps(public_payloads, sort_keys=True)
    leaked = [str(path) for path in private_paths if str(path) in rendered]
    if leaked:
        raise RuntimeError(f"Public worker payload leaked private path(s): {leaked}")


async def run_eval(timeout: int) -> dict:
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI is not available on PATH")

    with tempfile.TemporaryDirectory(prefix="codex-worker-phase2-") as temp:
        temp_root = Path(temp)
        repo = temp_root / "repo"
        state = temp_root / "state"
        init_repo(repo)
        config = build_config(repo, state, timeout)

        manager = JobManager(config)
        executor = JobExecutor(config, manager)
        runtime = WorkerRuntime(config, manager, executor)
        started = await runtime.start_worker(
            name="Isolated Implementer",
            brief=(
                f"Create {TARGET_FILE} in the repository root. Put exactly these two lines in it: "
                f"{FIRST_MARKER} and base-checkout-remains-clean. Do not commit. Report what you changed."
            ),
            repo_path=str(repo),
        )
        first = await wait_for_worker(runtime, started["worker_id"], timeout)
        if first["state"] != "idle" or not first["has_session"]:
            raise RuntimeError(f"First turn did not produce a resumable worker: {first}")

        first_session = worker_session(manager, started["worker_id"])
        first_worktree = worker_worktree(manager, started["worker_id"])
        if repo.resolve() in first_worktree.parents or first_worktree == repo.resolve():
            raise RuntimeError(f"Worker worktree is not external to the base repo: {first_worktree}")
        if not (first_worktree / TARGET_FILE).exists():
            raise RuntimeError(f"Worker did not create {TARGET_FILE} in the isolated worktree")
        first_text = (first_worktree / TARGET_FILE).read_text(encoding="utf-8")
        if FIRST_MARKER not in first_text:
            raise RuntimeError(f"First marker not found in worker file: {first_text!r}")
        if (repo / TARGET_FILE).exists():
            raise RuntimeError("Worker wrote to the base checkout")
        if git_status(repo):
            raise RuntimeError(f"Base checkout became dirty: {git_status(repo)!r}")

        first_changes = await runtime.inspect_worker(worker="Isolated Implementer", view="changes")
        if TARGET_FILE not in first_changes.get("changed_files", []):
            raise RuntimeError(f"Changed-file view did not include {TARGET_FILE}: {first_changes}")
        first_diff = await runtime.inspect_worker(worker="Isolated Implementer", view="diff", file_path=TARGET_FILE)
        if FIRST_MARKER not in first_diff.get("diff", ""):
            raise RuntimeError(f"Diff view did not include the first marker: {first_diff}")

        manager2 = JobManager(config)
        executor2 = JobExecutor(config, manager2)
        runtime2 = WorkerRuntime(config, manager2, executor2)
        listed = await runtime2.list_workers()
        if listed["count"] != 1:
            raise RuntimeError(f"Worker did not survive restart: {listed}")

        continued = await runtime2.message_worker(
            worker="Isolated Implementer",
            message=(
                f"Continue the same implementation in the same file. Append one new line exactly "
                f"{SECOND_MARKER}. Do not replace existing content and do not commit."
            ),
        )
        if not continued.get("accepted"):
            raise RuntimeError(f"Follow-up was not accepted: {continued}")
        second = await wait_for_worker(runtime2, started["worker_id"], timeout)
        if second["state"] != "idle":
            raise RuntimeError(f"Follow-up did not complete: {second}")

        second_session = worker_session(manager2, started["worker_id"])
        second_worktree = worker_worktree(manager2, started["worker_id"])
        if not first_session or second_session != first_session:
            raise RuntimeError(
                f"Conversation did not preserve the same session: first={first_session!r}, second={second_session!r}"
            )
        if second_worktree != first_worktree:
            raise RuntimeError(f"Worker did not reuse the same worktree: {first_worktree} vs {second_worktree}")

        final_text = (second_worktree / TARGET_FILE).read_text(encoding="utf-8")
        if FIRST_MARKER not in final_text or SECOND_MARKER not in final_text:
            raise RuntimeError(f"Worker file was not revised in place: {final_text!r}")
        if (repo / TARGET_FILE).exists():
            raise RuntimeError("Follow-up wrote to the base checkout")
        if git_status(repo):
            raise RuntimeError(f"Base checkout became dirty after follow-up: {git_status(repo)!r}")

        second_changes = await runtime2.inspect_worker(worker="Isolated Implementer", view="changes")
        second_diff = await runtime2.inspect_worker(worker="Isolated Implementer", view="diff", file_path=TARGET_FILE)
        if SECOND_MARKER not in second_diff.get("diff", ""):
            raise RuntimeError(f"Diff view did not include the follow-up marker: {second_diff}")

        assert_no_private_paths(
            [started, first, first_changes, first_diff, listed, continued, second, second_changes, second_diff],
            [repo.resolve(), first_worktree],
        )

        stopped = await runtime2.stop_worker(worker="Isolated Implementer", cleanup_workspace=True)
        if not stopped.get("workspace_cleaned"):
            raise RuntimeError(f"Worker cleanup did not discard the isolated workspace: {stopped}")
        if first_worktree.exists():
            raise RuntimeError("Worker worktree still exists after cleanup")

        return {
            "status": "passed",
            "worker": second["name"],
            "worker_id": second["worker_id"],
            "workspace_mode": second["workspace_mode"],
            "external_worktree_created": True,
            "base_checkout_remained_clean": True,
            "same_session_after_restart": True,
            "same_worktree_after_restart": True,
            "changed_files": second_changes.get("changed_files", []),
            "diff_available_on_demand": TARGET_FILE in second_diff.get("diff", ""),
            "private_paths_returned": False,
            "workspace_cleaned": True,
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
