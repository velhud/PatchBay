import asyncio
import subprocess
from pathlib import Path

import pytest

from patchbay.jobs.manager import JobManager, JobState
from patchbay.jobs.executor import JobExecutor
from patchbay.tools.handler import ToolHandler


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# worker tools\n", encoding="utf-8")
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
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".env", ".git", ".git/**", "**/.git/**"],
        },
        "power_tools": {
            "direct_write": False,
            "bash_mode": "off",
            "codex_session_read": False,
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "workers": {"worktree_root": str(tmp_path / "worker-worktrees")},
    }


def init_repo(path: Path, title: str) -> Path:
    path.mkdir()
    (path / "README.md").write_text(f"# {title}\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Test", "-c", "user.email=worker-test@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


class RecordingExecutor(JobExecutor):
    def __init__(self, config, manager):
        super().__init__(config, manager)
        self.started = []

    async def execute_job(self, job_id):
        self.started.append(job_id)


@pytest.mark.asyncio
async def test_tool_handler_exposes_worker_option_menu(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    def fake_menu(config_arg, **kwargs):
        assert config_arg is config
        assert kwargs["model"] == "gpt-5.5"
        return {
            "source": "test",
            "models": [{"id": "gpt-5.5"}],
            "reasoning_efforts": [{"effort": "high"}],
            "next_step": "Use codex_worker_start.",
        }

    monkeypatch.setattr("patchbay.tools.handler.worker_option_menu", fake_menu)

    result = await handler.handle_tool_call("codex_worker_options", {"model": "gpt-5.5"})

    assert result["source"] == "test"
    assert result["models"][0]["id"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_tool_handler_exposes_natural_worker_flow(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    started = await handler.handle_tool_call(
        "codex_worker_start",
        {
            "name": "Architecture Reader",
            "brief": "Read the architecture and report its main boundary.",
            "model": "gpt-5.5",
            "reasoning_effort": "high",
        },
    )
    await asyncio.sleep(0)
    first_job = manager.get_job(executor.started[0])
    manager.update_job_state(
        first_job.job_id,
        JobState.COMPLETED,
        result={"summary": "ChatGPT manages intent; Codex performs local work.", "files_changed": []},
        session_id="session-abc",
        exit_code=0,
    )

    inspected = await handler.handle_tool_call("codex_worker_inspect", {"worker": "Architecture Reader"})
    assert inspected["state"] == "idle"
    assert "ChatGPT manages intent" in inspected["report"]

    continued = await handler.handle_tool_call(
        "codex_worker_message",
        {"worker": "Architecture Reader", "message": "Now explain the continuation path."},
    )
    await asyncio.sleep(0)
    assert continued["accepted"] is True
    resume_job = manager.get_job(executor.started[-1])
    assert resume_job.mode == "resume"
    assert resume_job.options["resume_session_id"] == "session-abc"
    assert resume_job.worktree_path == first_job.worktree_path
    assert resume_job.options["sandbox"] == "workspace-write"
    assert resume_job.options["model"] == "gpt-5.5"
    assert resume_job.options["_worker_reasoning_effort"] == "high"

    workers = await handler.handle_tool_call("codex_worker_list", {})
    assert workers["count"] == 1
    assert "team_status" in workers
    assert "job_id" not in str(workers)
    assert "session-abc" not in str(workers)

    status = await handler.handle_tool_call("codex_worker_status", {})
    assert status["count"] == 1
    assert status["worker_lines"]
    assert "session-abc" not in str(status)


@pytest.mark.asyncio
async def test_tool_handler_scopes_worker_names_and_hints_worker_file_reads(tmp_path):
    config = make_config(tmp_path)
    other_repo = init_repo(tmp_path / "other-repo", "other worker tools")
    config["repositories"]["allowed"].append(str(other_repo))
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    other = await handler.handle_tool_call(
        "codex_worker_start",
        {
            "name": "Small Implementer",
            "brief": "Inspect the other repo.",
            "repo_path": str(other_repo),
            "workspace_mode": "read_only",
        },
    )
    current = await handler.handle_tool_call(
        "codex_worker_start",
        {
            "name": "Small Implementer",
            "brief": "Create a worker note in this repo.",
            "workspace_mode": "isolated_write",
        },
    )
    await asyncio.sleep(0)

    assert other["accepted"] is True
    assert current["accepted"] is True
    assert other["worker_id"] != current["worker_id"]

    all_status = await handler.handle_tool_call("codex_worker_status", {})
    assert all_status["count"] == 2
    assert {worker["name"] for worker in all_status["workers"]} == {"Small Implementer"}
    assert {worker["workspace_name"] for worker in all_status["workers"]} == {"repo", "other-repo"}

    scoped_status = await handler.handle_tool_call(
        "codex_worker_status",
        {"repo_path": config["repositories"]["default"], "force_refresh": True},
    )
    assert scoped_status["count"] == 1
    assert scoped_status["workers"][0]["workspace_name"] == "repo"

    job = next(
        job
        for job in manager.jobs.values()
        if (job.options or {}).get("_worker_id") == current["worker_id"]
    )
    worker_root = Path(job.worktree_path)
    (worker_root / "docs").mkdir(exist_ok=True)
    (worker_root / "docs" / "worker-note.md").write_text("from isolated worker\n", encoding="utf-8")
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "Created docs/worker-note.md", "files_changed": ["docs/worker-note.md"]},
        session_id="session-worker",
        exit_code=0,
    )

    file_view = await handler.handle_tool_call(
        "codex_worker_inspect",
        {"worker": "Small Implementer", "view": "file", "file_path": "docs/worker-note.md"},
    )
    assert file_view["worker_id"] == current["worker_id"]
    assert file_view["exists"] is True
    assert "from isolated worker" in file_view["text"]

    with pytest.raises(ValueError, match="codex_worker_inspect.*view=\"file\""):
        await handler.handle_tool_call("codex_read_file", {"file_path": "docs/worker-note.md"})
