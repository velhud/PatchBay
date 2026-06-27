#!/usr/bin/env python3
"""Disposable live MCP eval for the ChatGPT-facing wrapper surface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOL_CARD_URI = "ui://widget/codex-mcp-wrapper-tool-card-v1.html"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a disposable live MCP eval without ChatGPT.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    parser.add_argument("--port", type=int, help="Local port. Defaults to a free loopback port.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Startup/probe timeout seconds.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep the disposable repo for debugging.")
    parser.add_argument("--verbose", action="store_true", help="Print launcher/server output on failure.")
    args = parser.parse_args()

    temp_dir = Path(tempfile.mkdtemp(prefix="codex-mcp-live-eval."))
    report: dict[str, Any] = {
        "name": "codex-mcp-wrapper-live-eval",
        "status": "failed",
        "checks": [],
    }
    process: subprocess.Popen[str] | None = None
    output_tail: list[str] = []

    try:
        repo = _create_disposable_repo(temp_dir)
        port = args.port or _free_port()
        env = dict(os.environ)
        env["HOME"] = str(temp_dir / "home")
        env["CODEX_MCP_HOME"] = str(temp_dir / "runtime")
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        process = _start_server(repo, port, env)
        _wait_for_health(port, process, output_tail, timeout=args.timeout)
        client = McpClient(f"http://127.0.0.1:{port}")

        health = client.get("/")
        _check(report, "health", health["transport"] == "streamable-http" and health["status"] == "running")

        session_id, initialize = client.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "codex-mcp-live-eval", "version": "1.0.0"},
                },
            }
        )
        _check(report, "initialize", bool(session_id) and initialize["result"]["serverInfo"]["name"] == "codex-mcp-wrapper")
        client.session_id = session_id

        tools_payload = client.rpc(2, "tools/list")
        tools = {tool["name"]: tool for tool in tools_payload["result"]["tools"]}
        required_tools = {
            "codex_open_workspace",
            "codex_read_file",
            "codex_list_skills",
            "codex_load_skill",
            "codex_workspace_snapshot",
            "codex_show_changes",
            "codex_git_status",
            "codex_write_file",
            "codex_worker_options",
            "codex_worker_start",
            "codex_worker_message",
            "codex_worker_list",
            "codex_worker_inspect",
            "codex_worker_stop",
            "codex_self_test",
            "read",
            "show_changes",
        }
        _check(report, "tools_list", required_tools <= set(tools))
        _check(report, "tool_card_metadata", all(tools[name]["_meta"]["openai/outputTemplate"] == TOOL_CARD_URI for name in required_tools))
        _check(
            report,
            "worker_tool_descriptors",
            tools["codex_worker_options"]["readOnlyHint"] is True
            and "models" in tools["codex_worker_options"]["outputSchema"]["properties"]
            and tools["codex_worker_start"]["readOnlyHint"] is False
            and "workspace_mode" in tools["codex_worker_start"]["inputSchema"]["properties"]
            and "model" in tools["codex_worker_start"]["inputSchema"]["properties"]
            and "reasoning_effort" in tools["codex_worker_start"]["inputSchema"]["properties"]
            and "view" in tools["codex_worker_inspect"]["inputSchema"]["properties"]
            and "cleanup_workspace" in tools["codex_worker_stop"]["inputSchema"]["properties"],
        )

        resources = client.rpc(3, "resources/list")
        resource_uris = {resource["uri"] for resource in resources["result"]["resources"]}
        _check(report, "resources_list", TOOL_CARD_URI in resource_uris)
        card = client.rpc(4, "resources/read", {"uri": TOOL_CARD_URI})
        _check(report, "resources_read", card["result"]["contents"][0]["mimeType"] == "text/html;profile=mcp-app")

        workspace = client.call_tool(5, "codex_open_workspace", {"repo_path": str(repo), "include_global_skills": False})
        workspace_data = workspace["result"]["structuredContent"]
        _check(report, "open_workspace", workspace_data["skills"] == ["repo-skill"])

        skills = client.call_tool(6, "codex_list_skills", {"repo_path": str(repo), "include_global_skills": False})
        skill_data = skills["result"]["structuredContent"]
        _check(report, "list_skills", skill_data["skill_count"] == 1 and skill_data["skill_inventory"][0]["path"].startswith("$WORKSPACE/"))

        loaded = client.call_tool(7, "codex_load_skill", {"repo_path": str(repo), "name": "repo-skill", "include_global_skills": False})
        loaded_data = loaded["result"]["structuredContent"]
        _check(report, "load_skill", "Use this skill for repo verification." in loaded_data["text"])

        readme = client.call_tool(8, "codex_read_file", {"repo_path": str(repo), "file_path": "README.md"})
        _check(report, "read_file", "Probe Repo" in readme["result"]["structuredContent"]["text"])

        alias_read = client.call_tool(81, "read", {"repo_path": str(repo), "path": "README.md"})
        _check(report, "alias_read_file", "Probe Repo" in alias_read["result"]["structuredContent"]["text"])

        git_status = client.call_tool(82, "codex_git_status", {"repo_path": str(repo)})
        _check(report, "git_status", "##" in git_status["result"]["structuredContent"]["text"])

        snapshot = client.call_tool(83, "codex_workspace_snapshot", {"repo_path": str(repo), "include_hidden": False})
        _check(report, "workspace_snapshot", "Workspace Snapshot" in snapshot["result"]["structuredContent"]["text"])

        show_changes = client.call_tool(84, "show_changes", {"repo_path": str(repo), "include_diff": True})
        _check(report, "alias_show_changes", "Workspace Changes" in show_changes["result"]["structuredContent"]["text"])

        env_read = client.call_tool(9, "codex_read_file", {"repo_path": str(repo), "file_path": ".env"})
        _check(report, "env_read_allowed", "TOKEN=" in env_read["result"]["structuredContent"]["text"])

        symlink = client.call_tool(10, "codex_read_file", {"repo_path": str(repo), "file_path": "outside-link.txt"})
        _check(report, "blocked_symlink_read", "error" in symlink and "symlink" in symlink["error"]["message"])

        direct_write = client.call_tool(11, "codex_write_file", {"repo_path": str(repo), "file_path": "new.txt", "content": "hello\n"})
        _check(report, "direct_write_enabled", direct_write["result"]["structuredContent"]["changed"] is True)

        full_bash = client.call_tool(111, "codex_run_command", {"repo_path": str(repo), "command": "cat new.txt"})
        _check(report, "full_bash_enabled", full_bash["result"]["structuredContent"]["stdout"] == "hello\n")

        self_test = client.call_tool(12, "codex_self_test", {})
        _check(report, "self_test", self_test["result"]["structuredContent"]["ready"] is True)

        report.update(
            {
                "status": "passed" if all(check["ok"] for check in report["checks"]) else "failed",
                "tool_count": len(tools),
                "skill_count": skill_data["skill_count"],
                "public_tunnel_used": False,
            }
        )
        _print_report(report, json_only=args.json)
        return 0 if report["status"] == "passed" else 1
    except Exception as error:
        report["error"] = str(error)
        if args.verbose:
            report["launcher_output_tail"] = output_tail[-80:]
        _print_report(report, json_only=args.json)
        return 1
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if not args.keep_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)


class McpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None

    def get(self, path: str) -> dict[str, Any]:
        with urllib.request.urlopen(self.base_url + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, message: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        data = json.dumps(message).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.base_url + "/mcp", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.headers.get("Mcp-Session-Id"), json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return None, json.loads(error.read().decode("utf-8"))

    def rpc(self, msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _, payload = self.post({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        return payload

    def call_tool(self, msg_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.rpc(msg_id, "tools/call", {"name": name, "arguments": arguments})


def _create_disposable_repo(temp_dir: Path) -> Path:
    repo = temp_dir / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Probe Repo\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Follow disposable eval rules.\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=do-not-read\n", encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("print('probe')\n", encoding="utf-8")
    skill = repo / "skills" / "repo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "name: repo-skill\n"
        "description: Repository eval skill\n\n"
        "Use this skill for repo verification.\n",
        encoding="utf-8",
    )
    outside = temp_dir / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        os.symlink(outside, repo / "outside-link.txt")
    except OSError:
        pass

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "README.md", "AGENTS.md", "src/app.py", "skills/repo-skill/SKILL.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Eval User", "-c", "user.email=eval@example.invalid", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def _start_server(repo: Path, port: int, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "scripts/start.py",
            "--root",
            str(repo),
            "--port",
            str(port),
            "--tunnel-mode",
            "none",
            "--no-profile",
            "--force",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_health(port: int, process: subprocess.Popen[str], output_tail: list[str], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _read_available_output(process, output_tail)
        if process.poll() is not None:
            raise RuntimeError("server exited before health check passed")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("transport") == "streamable-http":
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError("server did not become healthy")


def _read_available_output(process: subprocess.Popen[str], output_tail: list[str]) -> None:
    if not process.stdout:
        return
    while True:
        fd = process.stdout.fileno()
        readable, _, _ = select_with_timeout(fd, 0)
        if not readable:
            return
        line = process.stdout.readline()
        if not line:
            return
        output_tail.append(line.rstrip())
        del output_tail[:-120]


def select_with_timeout(fd: int, timeout: float) -> tuple[list[int], list[int], list[int]]:
    import select

    return select.select([fd], [], [], timeout)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def _check(report: dict[str, Any], name: str, ok: bool) -> None:
    report["checks"].append({"name": name, "ok": bool(ok)})
    if not ok:
        raise AssertionError(f"live MCP eval check failed: {name}")


def _print_report(report: dict[str, Any], *, json_only: bool) -> None:
    if json_only:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"{report['name']}: {report['status']}")
    for check in report["checks"]:
        print(f"- {'pass' if check['ok'] else 'fail'}: {check['name']}")
    if report.get("error"):
        print(f"error: {report['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
