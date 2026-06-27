#!/usr/bin/env python3
"""Real-Codex Phase 3 eval: multi-worker natural-language coordination.

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
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.workers.runtime import WORKER_ID_OPTION, WORKER_WORKTREE_OPTION, WorkerRuntime


TARGET_FILE = "phase3_worker_output.txt"
MARKER = "phase3-peer-context-marker"


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
        "# Worker Phase 3 Eval\n\nThis repo checks multi-worker peer-context coordination.\n",
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


def assert_no_private_paths(renderable: object, private_paths: list[Path]) -> None:
    rendered = json.dumps(renderable, sort_keys=True) if not isinstance(renderable, str) else renderable
    leaked = [str(path) for path in private_paths if str(path) in rendered]
    if leaked:
        raise RuntimeError(f"Private path leaked: {leaked}")


async def run_eval(timeout: int) -> dict:
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI is not available on PATH")

    with tempfile.TemporaryDirectory(prefix="codex-worker-phase3-") as temp:
        temp_root = Path(temp)
        repo = temp_root / "repo"
        state = temp_root / "state"
        init_repo(repo)
        config = build_config(repo, state, timeout)

        manager = JobManager(config)
        executor = JobExecutor(config, manager)
        runtime = WorkerRuntime(config, manager, executor)

        implementer = await runtime.start_worker(
            name="Phase 3 Implementer",
            brief=(
                f"Create {TARGET_FILE} in the repository root. Put exactly this line in it: {MARKER}. "
                "Do not commit. Report what you changed."
            ),
            repo_path=str(repo),
            workspace_mode="isolated_write",
        )
        first = await wait_for_worker(runtime, implementer["worker_id"], timeout)
        if first["state"] != "idle" or not first["has_session"]:
            raise RuntimeError(f"Implementer did not produce a resumable session: {first}")

        first_session = worker_session(manager, implementer["worker_id"])
        first_worktree = worker_worktree(manager, implementer["worker_id"])
        target_path = first_worktree / TARGET_FILE
        if not target_path.exists():
            raise RuntimeError(f"Implementer did not create {TARGET_FILE}")
        if MARKER not in target_path.read_text(encoding="utf-8"):
            raise RuntimeError(f"Expected marker missing from {TARGET_FILE}")
        if git_status(repo):
            raise RuntimeError(f"Base checkout became dirty: {git_status(repo)!r}")

        changes = await runtime.inspect_worker(worker="Phase 3 Implementer", view="changes")
        if TARGET_FILE not in changes.get("changed_files", []):
            raise RuntimeError(f"Changed-file inventory did not include {TARGET_FILE}: {changes}")

        reviewer = await runtime.start_worker(
            name="Phase 3 Reviewer",
            brief=(
                "Review the Implementer's diff from the peer context. Do not edit files. "
                f"Report whether the diff contains {MARKER}."
            ),
            repo_path=str(repo),
            workspace_mode="read_only",
            context_from_workers=["Phase 3 Implementer"],
            context_detail="diff",
        )
        reviewer_jobs = worker_jobs(manager, reviewer["worker_id"])
        reviewer_prompt = reviewer_jobs[-1].prompt
        if MARKER not in reviewer_prompt or "Bounded diff:" not in reviewer_prompt:
            raise RuntimeError("Reviewer prompt did not receive peer diff context")
        assert_no_private_paths(reviewer_prompt, [repo.resolve(), first_worktree])

        reviewer_done = await wait_for_worker(runtime, reviewer["worker_id"], timeout)
        if reviewer_done["state"] != "idle" or not reviewer_done["has_session"]:
            raise RuntimeError(f"Reviewer did not complete with a session: {reviewer_done}")

        relayed = await runtime.message_worker(
            worker="Phase 3 Implementer",
            message="Read the reviewer's report from peer context. Acknowledge it. Do not edit files.",
            context_from_workers=["Phase 3 Reviewer"],
            context_detail="report",
        )
        if not relayed.get("accepted"):
            raise RuntimeError(f"Relay back to implementer was not accepted: {relayed}")
        resume_jobs = [job for job in worker_jobs(manager, implementer["worker_id"]) if job.mode == "resume"]
        if not resume_jobs:
            raise RuntimeError("Relay back to implementer did not create a resume job")
        relay_prompt = resume_jobs[-1].prompt
        if "Context from worker: Phase 3 Reviewer" not in relay_prompt:
            raise RuntimeError("Implementer follow-up did not receive reviewer context")
        relay_done = await wait_for_worker(runtime, implementer["worker_id"], timeout)
        if relay_done["state"] != "idle":
            raise RuntimeError(f"Implementer relay follow-up did not complete: {relay_done}")
        if worker_session(manager, implementer["worker_id"]) != first_session:
            raise RuntimeError("Implementer conversation did not preserve the same session after relay")
        if worker_worktree(manager, implementer["worker_id"]) != first_worktree:
            raise RuntimeError("Implementer did not reuse the same worktree after relay")
        if git_status(repo):
            raise RuntimeError(f"Base checkout became dirty after relay: {git_status(repo)!r}")

        team = await runtime.list_workers()
        if team["count"] != 2 or "Phase 3 Implementer" not in team.get("team_report", ""):
            raise RuntimeError(f"Team report is not usable: {team}")
        assert_no_private_paths([implementer, first, changes, reviewer, reviewer_done, relayed, relay_done, team], [repo.resolve(), first_worktree])

        return {
            "status": "passed",
            "workers": ["Phase 3 Implementer", "Phase 3 Reviewer"],
            "peer_diff_context_delivered": True,
            "peer_report_context_delivered": True,
            "same_implementer_session_after_relay": True,
            "same_implementer_worktree_after_relay": True,
            "base_checkout_remained_clean": True,
            "team_report_available": True,
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
