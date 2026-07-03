import asyncio
import subprocess
from pathlib import Path

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.pro_requests import ProRequestStore
from patchbay.protocol.context import RequestContext
from patchbay.protocol.mcp import PUBLIC_TOOL_DESCRIPTORS, tool_descriptors_for_mode, validate_public_tool_arguments
from patchbay.tools.handler import ToolHandler


def init_repo(path: Path) -> Path:
    path.mkdir()
    (path / "README.md").write_text("# Tools Pro Requests\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Pro Tools", "-c", "user.email=pro-tools@example.invalid", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def config(tmp_path: Path, repo: Path) -> dict:
    return {
        "app": {"tool_mode": "worker"},
        "server": {"max_concurrent_jobs": 2, "job_timeout_seconds": 30, "job_cleanup_after_hours": 24},
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {"require_git_repo": False, "default_sandbox": "read-only", "blocked_globs": []},
        "power_tools": {"direct_write": False, "bash_mode": "off", "codex_session_read": False},
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
        "pro_requests": {
            "root": str(tmp_path / "runtime" / "pro-requests"),
            "mirror_enabled": True,
            "mirror_dir": ".ai-bridge/pro-requests",
        },
        "workers": {"worktree_root": str(tmp_path / "workers")},
    }


def test_pro_request_tools_are_advertised_with_correct_annotations():
    by_name = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}
    expected = {
        "codex_pro_request_list",
        "codex_pro_request_read",
        "codex_pro_request_claim",
        "codex_pro_request_respond",
        "codex_pro_request_dispatch",
        "codex_pro_request_close",
    }
    assert expected <= set(by_name)
    assert by_name["codex_pro_request_list"]["annotations"]["readOnlyHint"] is True
    assert by_name["codex_pro_request_read"]["annotations"]["readOnlyHint"] is True
    assert by_name["codex_pro_request_respond"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": False,
    }
    assert by_name["codex_pro_request_dispatch"]["annotations"]["openWorldHint"] is True
    assert "does not execute, dispatch" in by_name["codex_pro_request_respond"]["description"]
    assert "does not queue silently" in by_name["codex_pro_request_dispatch"]["description"]
    worker_names = {tool["name"] for tool in tool_descriptors_for_mode({"app": {"tool_mode": "worker"}})}
    assert expected <= worker_names
    validate_public_tool_arguments("codex_pro_request_respond", {"request_id": "proreq_20260629_142210_abcdef", "response_markdown": "ok"})


@pytest.mark.asyncio
async def test_pro_request_handler_minimal_chatgpt_loop(tmp_path):
    repo = init_repo(tmp_path / "repo")
    cfg = config(tmp_path, repo)
    report = tmp_path / "report.md"
    report.write_text("# Report\n\nNeed help.\n", encoding="utf-8")
    store = ProRequestStore(cfg)
    request_id = store.create_request(repo_path=str(repo), title="Need help", report_path=str(report))["id"]
    manager = JobManager(cfg)
    executor = JobExecutor(cfg, manager)
    handler = ToolHandler(cfg, manager, executor)
    ctx = RequestContext.from_session("chatgpt-pro", {"client_label": "ChatGPT Pro"}, salt="salt")

    listed = await handler.handle_tool_call("codex_pro_request_list", {}, context=ctx)
    assert listed["requests"][0]["id"] == request_id

    read = await handler.handle_tool_call("codex_pro_request_read", {"request_id": request_id}, context=ctx)
    assert "Need help" in read["report_markdown"]

    claimed = await handler.handle_tool_call("codex_pro_request_claim", {"request_id": request_id}, context=ctx)
    assert claimed["accepted"] is True

    before = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True, check=True).stdout
    responded = await handler.handle_tool_call(
        "codex_pro_request_respond",
        {
            "request_id": request_id,
            "response_kind": "architecture_plan",
            "response_markdown": "# Response\n\nPlan.",
            "worker_message_markdown": "Implement this plan.",
        },
        context=ctx,
    )
    after = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True, check=True).stdout
    assert responded["accepted"] is True
    assert responded["dispatched"] is False
    assert before == after


@pytest.mark.asyncio
async def test_pro_request_dispatch_without_origin_worker_blocks(tmp_path):
    repo = init_repo(tmp_path / "repo")
    cfg = config(tmp_path, repo)
    report = tmp_path / "report.md"
    report.write_text("report\n", encoding="utf-8")
    request_id = ProRequestStore(cfg).create_request(repo_path=str(repo), title="No origin", report_path=str(report))["id"]
    manager = JobManager(cfg)
    executor = JobExecutor(cfg, manager)
    handler = ToolHandler(cfg, manager, executor)
    await handler.handle_tool_call(
        "codex_pro_request_respond",
        {"request_id": request_id, "response_markdown": "answer"},
    )

    result = await handler.handle_tool_call("codex_pro_request_dispatch", {"request_id": request_id, "target": "origin_worker"})
    assert result["accepted"] is False
    assert result["request"]["status"] == "dispatch_blocked"
    assert "no origin worker" in result["dispatch_result"]["note"]
