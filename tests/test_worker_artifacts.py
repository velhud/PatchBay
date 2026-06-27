import asyncio
import json
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.ownership import OWNER_CLIENT_REF_OPTION, OWNER_SESSION_HASH_OPTION
from patchbay.protocol.context import RequestContext
from patchbay.tools.handler import ToolHandler
from patchbay.workers.runtime import WorkerRuntime


def init_repo(repo: Path) -> None:
    repo.mkdir()
    (repo / "README.md").write_text("# artifact inbox\n", encoding="utf-8")
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
        "artifacts": {"root": str(tmp_path / "runtime" / "artifacts")},
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


class RecordingExecutor(JobExecutor):
    def __init__(self, config, manager):
        super().__init__(config, manager)
        self.started = []

    async def execute_job(self, job_id):
        self.started.append(job_id)


def request_context(client_ref: str, label: str = "") -> RequestContext:
    return RequestContext(transport_session_id=f"session-{client_ref}", client_ref=client_ref, client_label=label)


def make_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)


@contextmanager
def serve_directory(directory: Path):
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.asyncio
async def test_worker_inbox_imports_repeated_artifacts_and_inspects_without_repo_edits(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, RecordingExecutor(config, manager))
    source_one = tmp_path / "one.txt"
    source_two = tmp_path / "two.txt"
    source_one.write_text("first artifact\n", encoding="utf-8")
    source_two.write_text("second artifact\n", encoding="utf-8")

    with serve_directory(tmp_path) as base_url:
        first = await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": f"{base_url}/one.txt", "file_name": "one.txt", "mime_type": "text/plain"},
                "label": "first",
            },
        )
        second = await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": f"{base_url}/two.txt", "file_name": "two.txt", "mime_type": "text/plain"},
                "label": "second",
            },
        )

    assert first["artifact_id"] != second["artifact_id"]
    assert first["top_level_entries"] == ["one.txt"]
    assert "download_url" not in str(first)
    assert str(tmp_path) not in str(first)
    assert not (Path(config["repositories"]["default"]) / "one.txt").exists()

    listed = await handler.handle_tool_call("codex_worker_inbox", {"action": "list"})
    assert listed["count"] == 2

    inspected = await handler.handle_tool_call(
        "codex_worker_inbox",
        {"action": "inspect", "artifact_id": first["artifact_id"], "view": "file", "file_path": "one.txt"},
    )
    assert inspected["exists"] is True
    assert inspected["text"] == "first artifact\n"


@pytest.mark.asyncio
async def test_worker_inbox_owner_metadata_is_private_and_session_relative(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, RecordingExecutor(config, manager))
    source = tmp_path / "owned.txt"
    source.write_text("owned artifact\n", encoding="utf-8")
    client_a = request_context("client_a", "Chat A")
    client_b = request_context("client_b", "Chat B")

    with serve_directory(tmp_path) as base_url:
        imported = await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": f"{base_url}/owned.txt", "file_name": "owned.txt"},
            },
            context=client_a,
        )

    assert imported["owned_by_current_client"] is True
    assert imported["ownership_status"] == "current_client"
    assert imported["owner_label"] == "Chat A"
    assert OWNER_SESSION_HASH_OPTION not in str(imported)
    assert "client_a" not in str(imported)

    workspace_id = handler.artifact_store.workspace_id(config["repositories"]["default"])
    metadata_path = handler.artifact_store.root / workspace_id / imported["artifact_id"] / "artifact.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata[OWNER_SESSION_HASH_OPTION] == "client_a"
    assert metadata[OWNER_CLIENT_REF_OPTION] == "client_a"

    listed = await handler.handle_tool_call("codex_worker_inbox", {"action": "list"}, context=client_b)
    assert listed["artifacts"][0]["owned_by_current_client"] is False
    assert listed["artifacts"][0]["ownership_status"] == "other_connection"
    assert OWNER_SESSION_HASH_OPTION not in str(listed)
    assert "client_a" not in str(listed)


@pytest.mark.asyncio
async def test_worker_inbox_cleanup_requires_takeover_for_other_owner(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, RecordingExecutor(config, manager))
    source = tmp_path / "cleanup.txt"
    source.write_text("cleanup artifact\n", encoding="utf-8")
    client_a = request_context("client_a", "Chat A")
    client_b = request_context("client_b", "Chat B")

    with serve_directory(tmp_path) as base_url:
        imported = await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": f"{base_url}/cleanup.txt", "file_name": "cleanup.txt"},
            },
            context=client_a,
        )

    refused = await handler.handle_tool_call(
        "codex_worker_inbox",
        {"action": "cleanup", "artifact_id": imported["artifact_id"]},
        context=client_b,
    )
    assert refused["removed"] is False
    assert refused["takeover_required"] is True
    assert refused["owned_by_current_client"] is False

    listed = await handler.handle_tool_call("codex_worker_inbox", {"action": "list"}, context=client_a)
    assert listed["count"] == 1

    removed = await handler.handle_tool_call(
        "codex_worker_inbox",
        {"action": "cleanup", "artifact_id": imported["artifact_id"], "takeover": True},
        context=client_b,
    )
    assert removed["removed"] is True
    assert removed["takeover_performed"] is True
    listed_after = await handler.handle_tool_call("codex_worker_inbox", {"action": "list"}, context=client_a)
    assert listed_after["count"] == 0


@pytest.mark.asyncio
async def test_worker_inbox_allows_sensitive_looking_zip_members_but_rejects_traversal(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, RecordingExecutor(config, manager))
    archive = tmp_path / "payload.zip"
    make_zip(archive, {".env": "TOKEN=abc\n", "auth/session.json": '{"ok": true}\n'})

    with serve_directory(tmp_path) as base_url:
        imported = await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": f"{base_url}/payload.zip", "file_name": "payload.zip", "mime_type": "application/zip"},
            },
        )
    tree = await handler.handle_tool_call(
        "codex_worker_inbox",
        {"action": "inspect", "artifact_id": imported["artifact_id"], "view": "tree"},
    )
    assert ".env" in tree["entries"]
    assert "auth/session.json" in tree["entries"]

    bad_archive = tmp_path / "bad.zip"
    make_zip(bad_archive, {"../escape.txt": "escape\n"})
    with serve_directory(tmp_path) as base_url, pytest.raises(ValueError, match="escapes"):
        await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": f"{base_url}/bad.zip", "file_name": "bad.zip"},
            },
        )


@pytest.mark.asyncio
async def test_artifact_context_materializes_into_worker_and_is_excluded_from_integration(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)
    archive = tmp_path / "update.zip"
    make_zip(archive, {".env": "TOKEN=abc\n", "docs/update.md": "from artifact\n"})

    with serve_directory(tmp_path) as base_url:
        imported = await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": f"{base_url}/update.zip", "file_name": "update.zip", "mime_type": "application/zip"},
            },
        )

    started = await handler.handle_tool_call(
        "codex_worker_start",
        {
            "name": "Artifact Implementer",
            "brief": "Use the imported update package and create a normal note.",
            "context_from_artifacts": [imported["artifact_id"]],
        },
    )
    await asyncio.sleep(0)
    job = next(job for job in manager.jobs.values() if (job.options or {}).get("_worker_id") == started["worker_id"])
    worker_root = Path(job.worktree_path)
    artifact_root = worker_root / ".ai-bridge" / "imported-artifacts"
    assert (artifact_root / "ARTIFACTS.md").is_file()
    assert (artifact_root / imported["artifact_id"] / ".env").is_file()
    assert imported["artifact_id"] in job.prompt
    assert str(tmp_path) not in job.prompt

    (worker_root / "worker-note.txt").write_text("from worker\n", encoding="utf-8")
    manager.update_job_state(
        job.job_id,
        JobState.COMPLETED,
        result={"summary": "Created note after reading .ai-bridge/imported-artifacts/ARTIFACTS.md"},
        session_id="session-1",
    )

    preview = await handler.handle_tool_call(
        "codex_worker_inspect",
        {"worker": "Artifact Implementer", "view": "integration_preview"},
    )
    assert preview["can_apply"] is True
    assert preview["changed_files"] == ["worker-note.txt"]
    assert all(not path.startswith(".ai-bridge/imported-artifacts") for path in preview["changed_files"])
    assert ".ai-bridge/imported-artifacts" not in preview["report"]
    assert "[imported-artifact-context]" in preview["report"]


@pytest.mark.asyncio
async def test_artifact_context_requires_isolated_workers(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    runtime = WorkerRuntime(config, manager, executor)
    source = tmp_path / "payload.txt"
    source.write_text("payload\n", encoding="utf-8")
    with serve_directory(tmp_path) as base_url:
        imported = runtime.artifact_store.import_file(
            repo_path=config["repositories"]["default"],
            artifact_file={"download_url": f"{base_url}/payload.txt", "file_name": "payload.txt"},
        )

    with pytest.raises(ValueError, match="workspace_mode=isolated_write"):
        await runtime.start_worker(
            name="Reader",
            brief="Read payload.",
            repo_path=config["repositories"]["default"],
            workspace_mode="read_only",
            context_from_artifacts=[imported["artifact_id"]],
        )


@pytest.mark.asyncio
async def test_worker_inbox_rejects_file_scheme_download_urls(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, RecordingExecutor(config, manager))
    source = tmp_path / "local.txt"
    source.write_text("local\n", encoding="utf-8")

    with pytest.raises(ValueError, match="HTTP\\(S\\)"):
        await handler.handle_tool_call(
            "codex_worker_inbox",
            {
                "action": "import_file",
                "artifact_file": {"download_url": source.as_uri(), "file_name": "local.txt"},
            },
        )
