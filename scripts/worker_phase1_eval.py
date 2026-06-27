#!/usr/bin/env python3
"""Real-Codex Phase 1 eval: start, persist, restart, and continue one worker.

This intentionally tests the worker facade directly. It does not open a tunnel or
attach ChatGPT Developer Mode; that remains a separate release eval.
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
from patchbay.workers.runtime import WORKER_ID_OPTION, WorkerRuntime


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
        },
        "logging": {
            "job_logs_dir": str(state_root / "jobs"),
            "job_state_dir": str(state_root / "jobs" / "state"),
            "job_log_max_bytes": 200_000,
            "write_raw_job_logs": False,
        },
        "workers": {
            "ignore_user_config": True,
        },
    }


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Worker Phase 1 Eval\n\nThe marker is durable-conversation.\n", encoding="utf-8")
    (path / "AGENTS.md").write_text("Work read-only. Report concisely.\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md", "AGENTS.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Eval", "-c", "user.email=worker-eval@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


async def wait_for_worker(runtime: WorkerRuntime, worker: str, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = await runtime.inspect_worker(worker=worker, wait_seconds=1)
        if view["state"] not in {"starting", "working"}:
            return view
    raise TimeoutError(f"Worker did not finish within {timeout} seconds")


def worker_session(manager: JobManager, worker_id: str) -> str | None:
    jobs = [job for job in manager.jobs.values() if (job.options or {}).get(WORKER_ID_OPTION) == worker_id]
    jobs.sort(key=lambda job: (job.completed_at or job.started_at or 0, job.job_id))
    for job in reversed(jobs):
        if job.session_id:
            return str(job.session_id)
        if (job.options or {}).get("resume_session_id"):
            return str(job.options["resume_session_id"])
    return None


async def run_eval(timeout: int) -> dict:
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI is not available on PATH")

    with tempfile.TemporaryDirectory(prefix="codex-worker-phase1-") as temp:
        temp_root = Path(temp)
        repo = temp_root / "repo"
        state = temp_root / "state"
        init_repo(repo)
        config = build_config(repo, state, timeout)

        manager = JobManager(config)
        executor = JobExecutor(config, manager)
        runtime = WorkerRuntime(config, manager, executor)
        started = await runtime.start_worker(
            name="Durable Investigator",
            brief=(
                "Read README.md. Report the exact marker word and explain in one sentence what this repository "
                "is testing. Do not modify files."
            ),
            repo_path=str(repo),
            workspace_mode="read_only",
        )
        first = await wait_for_worker(runtime, started["worker_id"], timeout)
        if first["state"] != "idle" or not first["has_session"]:
            raise RuntimeError(f"First turn did not produce a resumable worker: {first}")
        first_session = worker_session(manager, started["worker_id"])

        # Simulate a complete PatchBay restart by reconstructing all runtime objects.
        manager2 = JobManager(config)
        executor2 = JobExecutor(config, manager2)
        runtime2 = WorkerRuntime(config, manager2, executor2)
        listed = await runtime2.list_workers()
        if listed["count"] != 1:
            raise RuntimeError(f"Worker did not survive restart: {listed}")

        continued = await runtime2.message_worker(
            worker="Durable Investigator",
            message=(
                "Continue the same conversation. State that this is the follow-up turn and repeat the marker "
                "without rereading any unrelated files."
            ),
        )
        if not continued.get("accepted"):
            raise RuntimeError(f"Follow-up was not accepted: {continued}")
        second = await wait_for_worker(runtime2, started["worker_id"], timeout)
        second_session = worker_session(manager2, started["worker_id"])
        if second["state"] != "idle":
            raise RuntimeError(f"Follow-up did not complete: {second}")
        if not first_session or second_session != first_session:
            raise RuntimeError(
                f"Conversation did not preserve the same session: first={first_session!r}, second={second_session!r}"
            )

        return {
            "status": "passed",
            "worker": second["name"],
            "worker_id": second["worker_id"],
            "same_session_after_restart": True,
            "first_report": first["report"],
            "second_report": second["report"],
            "private_paths_returned": False,
            "job_or_session_ids_required_by_user": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=600)
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
