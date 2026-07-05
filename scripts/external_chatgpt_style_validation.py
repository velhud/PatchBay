#!/usr/bin/env python3
"""External ChatGPT-style MCP validation harness.

This script drives PatchBay through its Streamable HTTP MCP surface as one or
more independent ChatGPT-like clients. It intentionally writes local evidence
under .local/validation and uses only disposable repositories.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOOL_CARD_URI = "ui://widget/patchbay-tool-card-v2.html"
ACTIVE_WORKER_STATES = {"starting", "working"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)=([^\s,;}]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._-]+"),
]


class ValidationFailure(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Evidence:
    def __init__(self, out_dir: Path, replacements: dict[str, str]):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = out_dir / "results.json"
        self.summary_path = out_dir / "summary.md"
        self.calls_path = out_dir / "calls.jsonl"
        self.replacements = {}
        for source, target in replacements.items():
            if source:
                self.replacements[str(source)] = target
        self.report: dict[str, Any] = {
            "name": "external-chatgpt-style-validation",
            "status": "running",
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "scenarios": [],
            "commands": [],
            "output_dir": str(out_dir),
        }
        self._write()

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): self.sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(v) for v in value]
        if not isinstance(value, str):
            return value
        text = value
        for source, repl in sorted(self.replacements.items(), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(source, repl)
            text = text.replace(source.replace("\\", "/"), repl)
        for pattern in SECRET_PATTERNS:
            def repl(match: re.Match[str]) -> str:
                raw = match.group(0)
                if raw.lower().startswith("authorization:"):
                    return match.group(1) + "[REDACTED]"
                if "=" in raw:
                    return raw.split("=", 1)[0] + "=[REDACTED]"
                return "[REDACTED_POSSIBLE_SECRET]"

            text = pattern.sub(repl, text)
        return text

    def command(self, name: str, cmd: list[str], code: int, stdout: str, stderr: str = "") -> None:
        self.report["commands"].append(
            self.sanitize(
                {
                    "name": name,
                    "command": cmd,
                    "exit_code": code,
                    "stdout_tail": stdout[-8000:],
                    "stderr_tail": stderr[-8000:],
                }
            )
        )
        self._write()

    def call(self, event: dict[str, Any]) -> None:
        safe = self.sanitize(event)
        with self.calls_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, sort_keys=True) + "\n")

    @contextlib.contextmanager
    def scenario(self, ident: str, title: str):
        item = {
            "id": ident,
            "title": title,
            "status": "running",
            "started_at": utc_now(),
            "checks": [],
        }
        self.report["scenarios"].append(item)
        self._write()
        try:
            yield Scenario(self, item)
        except Blocked as blocked:
            item["status"] = "blocked"
            item["blocker"] = self.sanitize(str(blocked))
            item["updated_at"] = utc_now()
            self._write()
        except Exception as error:
            item["status"] = "failed"
            item["error"] = self.sanitize(f"{type(error).__name__}: {error}")
            item["updated_at"] = utc_now()
            self._write()
            raise
        else:
            if item["status"] == "running":
                item["status"] = "passed"
            item["updated_at"] = utc_now()
            self._write()

    def finish(self) -> None:
        statuses = [scenario["status"] for scenario in self.report["scenarios"]]
        self.report["status"] = "failed" if "failed" in statuses else "passed_with_blockers" if "blocked" in statuses else "passed"
        self.report["updated_at"] = utc_now()
        self._write()

    def _write(self) -> None:
        self.report["updated_at"] = utc_now()
        safe = self.sanitize(self.report)
        tmp = self.results_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.results_path)
        self.summary_path.write_text(render_summary(safe), encoding="utf-8")


class Scenario:
    def __init__(self, evidence: Evidence, item: dict[str, Any]):
        self.evidence = evidence
        self.item = item

    def check(self, name: str, ok: bool, evidence: str, **extra: Any) -> None:
        record = {"name": name, "ok": bool(ok), "evidence": evidence, **extra, "at": utc_now()}
        self.item["checks"].append(self.evidence.sanitize(record))
        self.evidence._write()
        if not ok:
            raise ValidationFailure(f"{self.item['id']}:{name}: {evidence}")

    def note(self, **values: Any) -> None:
        self.item.update(self.evidence.sanitize(values))
        self.evidence._write()


class Blocked(RuntimeError):
    pass


class McpClient:
    def __init__(self, base_url: str, evidence: Evidence, label: str, timeout: float = 60.0, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.evidence = evidence
        self.label = label
        self.timeout = timeout
        self.token = token
        self.session_id: str | None = None
        self.counter = 0

    def get(self, path: str, *, expect_error: bool = False) -> dict[str, Any]:
        self.counter += 1
        url = self.base_url + path
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        started = time.monotonic()
        self.evidence.call({"phase": "request", "client": self.label, "method": "GET", "url": url})
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.evidence.call({"phase": "response", "client": self.label, "method": "GET", "status": response.status, "duration": round(time.monotonic() - started, 3), "payload": payload})
                return payload
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"error": raw}
            self.evidence.call({"phase": "response", "client": self.label, "method": "GET", "status": error.code, "duration": round(time.monotonic() - started, 3), "payload": payload})
            if expect_error:
                return {"http_error": error.code, "payload": payload}
            raise

    def rpc(self, method: str, params: dict[str, Any] | None = None, *, expect_error: bool = False) -> dict[str, Any]:
        self.counter += 1
        message = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}}
        data = json.dumps(message).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.evidence.call({"phase": "request", "client": self.label, "method": method, "params": params or {}})
        started = time.monotonic()
        request = urllib.request.Request(self.base_url + "/mcp", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.headers.get("Mcp-Session-Id"):
                    self.session_id = response.headers.get("Mcp-Session-Id")
                self.evidence.call({"phase": "response", "client": self.label, "method": method, "status": response.status, "duration": round(time.monotonic() - started, 3), "session_id": "<mcp-session-id>" if self.session_id else None, "payload": payload})
                return payload
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"error": raw}
            self.evidence.call({"phase": "response", "client": self.label, "method": method, "status": error.code, "duration": round(time.monotonic() - started, 3), "payload": payload})
            if expect_error:
                return {"http_error": error.code, "payload": payload}
            raise

    def initialize(self) -> dict[str, Any]:
        return self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": f"chatgpt-style-{self.label}", "version": "1.0.0"},
            },
        )

    def call_tool(self, name: str, args: dict[str, Any], *, expect_error: bool = False) -> dict[str, Any]:
        return self.rpc("tools/call", {"name": name, "arguments": args}, expect_error=expect_error)


class ServerProcess:
    def __init__(self, repo: Path, runtime: Path, config: Path, tool_mode: str, evidence: Evidence, *, port: int | None = None, token: str | None = None):
        self.repo = repo
        self.runtime = runtime
        self.config = config
        self.tool_mode = tool_mode
        self.evidence = evidence
        self.port = port or free_port()
        self.token = token
        self.process: subprocess.Popen[str] | None = None
        self.output_tail: list[str] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = dict(os.environ)
        env["PATCHBAY_HOME"] = str(self.runtime)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.token:
            env["PATCHBAY_HTTP_TOKEN"] = self.token
        command = [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(self.config),
            "--root",
            str(self.repo),
            "--port",
            str(self.port),
            "--tool-mode",
            self.tool_mode,
            "--tunnel-mode",
            "none",
            "--no-profile",
            "--force",
        ]
        self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_for_health(self.port, self.process, self.output_tail, timeout=30)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def client(self, label: str) -> McpClient:
        return McpClient(self.base_url, self.evidence, label, token=self.token)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run external ChatGPT-style validation against disposable PatchBay MCP servers.")
    parser.add_argument("--output-dir", default=".local/validation/external_chatgpt_style")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--skip-heavy-codex", action="store_true", help="Skip real Codex worker/job scenarios.")
    parser.add_argument("--skip-public-tunnel", action="store_true")
    parser.add_argument("--ngrok-hostname", default=os.environ.get("PATCHBAY_VALIDATION_NGROK_HOSTNAME", ""))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    temp_dir = Path(tempfile.mkdtemp(prefix="patchbay-external-validation."))
    out_dir = Path(args.output_dir) / timestamp()
    repo = temp_dir / "repo"
    runtime = temp_dir / "runtime"
    codex_home = temp_dir / "codex-home"
    evidence = Evidence(
        out_dir,
        {
            str(temp_dir): "<validation-temp>",
            str(repo): "<validation-repo>",
            str(runtime): "<validation-runtime>",
            str(codex_home): "<validation-codex-home>",
            str(ROOT): "<PatchBay-repo>",
        },
    )
    process: ServerProcess | None = None
    try:
        init_repo(repo)
        write_codex_session_fixture(codex_home)
        config = write_config(repo, runtime, codex_home, tool_mode="worker")
        evidence.report["environment"] = evidence.sanitize(
            {
                "codex_version": run_capture(["codex", "--version"])[1].strip() if shutil.which("codex") else "codex-not-found",
                "python": sys.version.split()[0],
                "repo": str(repo),
                "runtime": str(runtime),
                "codex_home": str(codex_home),
            }
        )
        evidence._write()

        if not args.skip_baseline:
            scenario_baseline(evidence)
        scenario_connector_setup(evidence, repo)

        process = ServerProcess(repo, runtime, config, "worker", evidence)
        process.start()
        client_a = process.client("chat-a")
        client_a.initialize()
        scenario_worker_surface(evidence, client_a, repo)
        scenario_full_aliases(evidence, client_a, repo)
        scenario_artifacts(evidence, client_a, repo, temp_dir)
        scenario_session_discovery(evidence, client_a)
        scenario_handoff(evidence, client_a, repo)
        scenario_repo_busy(evidence, client_a, process.client("chat-b"), repo)
        cleanup_repo(repo)

        if not args.skip_heavy_codex:
            scenario_artifact_worker_use(evidence, client_a, repo)
            scenario_worker_use_case(evidence, client_a, repo)
            scenario_worker_restart(evidence, process, repo)
            scenario_multi_worker(evidence, process.client("chat-a2"), repo)
            power_client = process.client("power-a")
            power_client.initialize()
            structured(power_client.call_tool("codex_tool_mode_switch", {"mode": "full", "reason": "low-level job validation after restart"}))
            scenario_low_level_jobs(evidence, power_client, repo)
            scenario_resume(evidence, power_client, repo)
        else:
            mark_blocked(evidence, "S06", "Single ChatGPT Worker Use Case", "Skipped by --skip-heavy-codex.")
            mark_blocked(evidence, "S08B", "Artifact Inbox File Use Case With Worker", "Skipped by --skip-heavy-codex.")
            mark_blocked(evidence, "S07", "Worker Continuation After Restart", "Skipped by --skip-heavy-codex.")
            mark_blocked(evidence, "S10", "Multi-Worker Collaboration", "Skipped by --skip-heavy-codex.")
            mark_blocked(evidence, "S13", "Plan/Apply Job Use Case", "Skipped by --skip-heavy-codex.")
            mark_blocked(evidence, "S14", "Resume/Interactive Continuation Use Case", "Skipped by --skip-heavy-codex.")

        process.stop()
        process = None
        scenario_descriptor_truthfulness(evidence, repo, temp_dir)
        scenario_public_tunnel(evidence, repo, temp_dir, args)
        scenario_real_chatgpt_manual_gate(evidence)

        evidence.finish()
        if args.json:
            print(json.dumps(evidence.sanitize(evidence.report), indent=2, sort_keys=True))
        else:
            print(f"External ChatGPT-style validation: {evidence.report['status']}")
            print(f"Evidence: {out_dir}")
        return 0 if evidence.report["status"] in {"passed", "passed_with_blockers"} else 1
    except Exception as error:
        evidence.report["status"] = "failed"
        evidence.report["error"] = evidence.sanitize(f"{type(error).__name__}: {error}")
        evidence._write()
        if args.json:
            print(json.dumps(evidence.sanitize(evidence.report), indent=2, sort_keys=True))
        else:
            print(f"External ChatGPT-style validation failed: {error}")
            print(f"Evidence: {out_dir}")
        return 1
    finally:
        if process:
            process.stop()
        shutil.rmtree(temp_dir, ignore_errors=True)


def scenario_baseline(evidence: Evidence) -> None:
    with evidence.scenario("S01", "Baseline Sanity") as s:
        for name, command in [
            ("codex_version", ["codex", "--version"]),
            ("compileall", [sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests"]),
            ("pytest", [sys.executable, "-m", "pytest", "tests", "-q"]),
            ("live_mcp_eval", [sys.executable, "scripts/live_mcp_eval.py", "--json"]),
        ]:
            code, stdout, stderr = run_capture(command, timeout=120)
            evidence.command(name, command, code, stdout, stderr)
            s.check(name, code == 0, f"{name} exit code {code}.")
        live = json.loads((evidence.report["commands"][-1]["stdout_tail"] or "{}"))
        s.check("live_tool_count", live.get("tool_count", 0) >= 60, f"live eval tool_count={live.get('tool_count')}")
        s.check("live_status", live.get("status") == "passed", f"live eval status={live.get('status')}")


def scenario_connector_setup(evidence: Evidence, repo: Path) -> None:
    with evidence.scenario("S02", "Connector Setup And Token Behavior") as s:
        code, stdout, stderr = run_capture(
            [sys.executable, "scripts/start.py", "--root", str(repo), "--tool-mode", "worker", "--print-only", "--json", "--no-profile"],
            timeout=30,
        )
        evidence.command("start_print_only_json", [sys.executable, "scripts/start.py", "--root", "<repo>", "--tool-mode", "worker", "--print-only", "--json", "--no-profile"], code, stdout, stderr)
        s.check("print_only_passes", code == 0, "start.py --print-only --json passed.")
        payload = json.loads(stdout)
        guide = payload.get("setup_guide") or {}
        s.check("setup_guide_present", bool(guide.get("chatgpt_steps")) and bool(guide.get("controls")), "setup_guide includes ChatGPT steps and controls.")
        s.check("token_not_revealed", "patchbay_token=" not in stdout, "normal print-only JSON did not reveal query token.")

        no_token_env = dict(os.environ)
        no_token_env.pop("PATCHBAY_HTTP_TOKEN", None)
        code, stdout, stderr = run_capture(
            [sys.executable, "scripts/start.py", "--root", str(repo), "--public-base-url", "https://example.invalid", "--print-only", "--json", "--no-profile"],
            env=no_token_env,
            timeout=30,
        )
        evidence.command("public_preview_without_token", [sys.executable, "scripts/start.py", "--root", "<repo>", "--public-base-url", "https://example.invalid", "--print-only", "--json", "--no-profile"], code, stdout, stderr)
        s.check("public_without_token_fails", code != 0, "public URL preview failed closed without PATCHBAY_HTTP_TOKEN.")

        token = "validation-token-" + "x" * 24
        env = dict(os.environ)
        env["PATCHBAY_HTTP_TOKEN"] = token
        code, stdout, stderr = run_capture(
            [sys.executable, "scripts/start.py", "--root", str(repo), "--public-base-url", "https://example.invalid", "--tool-mode", "worker", "--print-only", "--json", "--no-profile"],
            env=env,
            timeout=30,
        )
        evidence.command("public_preview_with_token_redacted", [sys.executable, "scripts/start.py", "--root", "<repo>", "--public-base-url", "https://example.invalid", "--tool-mode", "worker", "--print-only", "--json", "--no-profile"], code, stdout, stderr)
        s.check("public_with_token_passes", code == 0, "public URL preview passed with PATCHBAY_HTTP_TOKEN.")
        s.check("token_redacted_without_reveal", token not in stdout, "token value not present without --reveal-token.")
        code, stdout, stderr = run_capture(
            [sys.executable, "scripts/start.py", "--root", str(repo), "--public-base-url", "https://example.invalid", "--tool-mode", "worker", "--print-only", "--json", "--no-profile", "--reveal-token"],
            env=env,
            timeout=30,
        )
        evidence.command("public_preview_with_reveal", [sys.executable, "scripts/start.py", "--root", "<repo>", "--public-base-url", "https://example.invalid", "--tool-mode", "worker", "--print-only", "--json", "--no-profile", "--reveal-token"], code, stdout, stderr)
        s.check("token_revealed_only_explicitly", code == 0 and "patchbay_token=" in stdout, "query token is revealed only with --reveal-token.")


def scenario_worker_surface(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S03", "Worker-Mode Tool Surface") as s:
        tools = tools_by_name(client.rpc("tools/list"))
        required = {"codex_open_workspace", "codex_worker_start", "codex_worker_inbox", "codex_worker_inspect", "codex_self_test"}
        s.check("worker_tools_visible", required <= set(tools), f"worker mode contains {sorted(required)}.")
        s.check("low_level_hidden", "codex_get_status" not in tools and "read" not in tools, "worker mode hides low-level job tools and aliases.")
        s.check(
            "cards_not_advertised_by_default",
            all("openai/outputTemplate" not in tool.get("_meta", {}) for tool in tools.values()),
            "default worker surface does not advertise Apps widget templates.",
        )
        resources = client.rpc("resources/list")
        uris = {item["uri"] for item in resources["result"]["resources"]}
        s.check("cards_not_listed_by_default", TOOL_CARD_URI not in uris, "default resources omit the optional v2 card.")
        self_test = structured(client.call_tool("codex_self_test", {}))
        serialized = json.dumps(self_test)
        s.check("coordination_metadata", self_test.get("coordination", {}).get("raw_session_ids_returned") is False, "self_test reports coordination without raw session ids.")
        s.check("raw_session_not_returned", str(client.session_id) not in serialized, "raw MCP session id absent from self_test payload.")
        workspace = structured(client.call_tool("codex_open_workspace", {"repo_path": str(repo), "include_global_skills": False}))
        s.check("workspace_opened", workspace.get("git", {}).get("is_git_repo") is True, "workspace opened through worker-mode context tool.")


def scenario_full_aliases(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S04", "Full-Power Tool Surface And Alias Schemas") as s:
        switched = structured(client.call_tool("codex_tool_mode_switch", {"mode": "full", "reason": "external validation"}))
        s.check("switch_full", switched.get("current_mode") == "full", "session switched to full mode.")
        tools = tools_by_name(client.rpc("tools/list"))
        for name in ["read", "bash", "show_changes", "open_workspace", "codex_sessions"]:
            s.check(f"{name}_visible", name in tools, f"{name} visible in full mode.")
            s.check(f"{name}_schema_precise", tools[name]["inputSchema"].get("additionalProperties") is False, f"{name} schema rejects unknown properties.")
        read = structured(client.call_tool("read", {"repo_path": str(repo), "path": "README.md"}))
        s.check("read_alias_path", "External Validation Repo" in read.get("text", ""), "read alias accepts path.")
        read_file_path = structured(client.call_tool("read", {"repo_path": str(repo), "file_path": "README.md"}))
        s.check("read_alias_file_path", "External Validation Repo" in read_file_path.get("text", ""), "read alias accepts file_path.")
        command = structured(client.call_tool("bash", {"repo_path": str(repo), "cmd": "printf alias-ok"}))
        s.check("bash_alias_cmd", command.get("stdout") == "alias-ok", "bash alias accepts cmd.")
        changes = structured(client.call_tool("show_changes", {"repo_path": str(repo), "path": "README.md", "include_diff": True}))
        s.check("show_changes_path", changes.get("path") == "README.md" or "Workspace Changes" in changes.get("text", ""), "show_changes accepts path scope.")
        bad = client.call_tool("read", {"repo_path": str(repo), "path": "README.md", "unexpected": True}, expect_error=True)
        s.check("unknown_arg_rejected", "error" in bad or bad.get("http_error"), "unknown alias argument rejected.")


def scenario_descriptor_truthfulness(evidence: Evidence, repo: Path, temp_dir: Path) -> None:
    with evidence.scenario("S05", "Runtime Descriptor Truthfulness") as s:
        runtime = temp_dir / "narrow-runtime"
        codex_home = temp_dir / "narrow-codex-home"
        config = write_config(repo, runtime, codex_home, tool_mode="full", direct_write=False, bash_mode="off", session_read=False)
        server = ServerProcess(repo, runtime, config, "full", evidence)
        try:
            server.start()
            client = server.client("narrow")
            client.initialize()
            names = set(tools_by_name(client.rpc("tools/list")))
            hidden = {"codex_write_file", "codex_edit_file", "codex_run_command", "codex_read_session", "write", "edit", "bash", "read_codex_session"}
            s.check("disabled_tools_hidden", not (hidden & names), f"disabled tools hidden: {sorted(hidden - names)}.")
            call = client.call_tool("codex_run_command", {"repo_path": str(repo), "command": "pwd"}, expect_error=True)
            s.check("disabled_call_rejected", "error" in call or call.get("http_error"), "disabled bash call rejected.")
        finally:
            server.stop()


def scenario_artifacts(evidence: Evidence, client: McpClient, repo: Path, temp_dir: Path) -> None:
    with evidence.scenario("S08-S09", "Artifact Inbox File/Zip And Rejection Cases") as s:
        source_dir = temp_dir / "artifact-source"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "brief.txt").write_text("artifact says create artifact-used.md\n", encoding="utf-8")
        make_zip(source_dir / "bundle.zip", {"notes/brief.md": "zip artifact note\n", "secrets/API_TOKEN.txt": "not actually secret\n"})
        make_zip(source_dir / "escape.zip", {"../escape.txt": "bad\n"})
        with serve_directory(source_dir) as base:
            first = structured(
                client.call_tool(
                    "codex_worker_inbox",
                    {
                        "repo_path": str(repo),
                        "action": "import_file",
                        "artifact_file": {"download_url": f"{base}/brief.txt", "file_name": "brief.txt", "mime_type": "text/plain"},
                        "label": "brief",
                    },
                )
            )
            second = structured(
                client.call_tool(
                    "codex_worker_inbox",
                    {
                        "repo_path": str(repo),
                        "action": "import_file",
                        "artifact_file": {"download_url": f"{base}/bundle.zip", "file_name": "bundle.zip", "mime_type": "application/zip"},
                        "label": "bundle",
                    },
                )
            )
            rejected_zip = client.call_tool(
                "codex_worker_inbox",
                {
                    "repo_path": str(repo),
                    "action": "import_file",
                    "artifact_file": {"download_url": f"{base}/escape.zip", "file_name": "escape.zip", "mime_type": "application/zip"},
                },
                expect_error=True,
            )
        s.check("file_imported", first.get("kind") == "file" and first.get("artifact_id"), "text artifact imported.")
        s.check("zip_imported", second.get("kind") == "archive" and "notes" in second.get("top_level_entries", []), "zip artifact imported.")
        listed = structured(client.call_tool("codex_worker_inbox", {"repo_path": str(repo), "action": "list"}))
        s.check("repeated_artifacts_listed", listed.get("count", 0) >= 2, "artifact list includes repeated imports.")
        inspected = structured(client.call_tool("codex_worker_inbox", {"repo_path": str(repo), "action": "inspect", "artifact_id": first["artifact_id"], "view": "file", "file_path": "brief.txt"}))
        s.check("artifact_file_inspected", inspected.get("exists") is True and "artifact says" in inspected.get("text", ""), "artifact file view works.")
        file_url = client.call_tool(
            "codex_worker_inbox",
            {"repo_path": str(repo), "action": "import_file", "artifact_file": {"download_url": "file:///tmp/not-allowed.txt", "file_name": "not-allowed.txt"}},
            expect_error=True,
        )
        s.check("file_url_rejected", "error" in file_url or file_url.get("http_error"), "file:// artifact import rejected.")
        s.check("zip_traversal_rejected", "error" in rejected_zip or rejected_zip.get("http_error"), "zip traversal rejected.")
        s.check("repo_unchanged_by_import", not (repo / "brief.txt").exists() and not (repo / "bundle.zip").exists(), "imports did not edit base checkout.")
        serialized = json.dumps([first, second, listed, inspected])
        s.check("no_download_url_leak", "download_url" not in serialized and str(temp_dir) not in serialized, "artifact outputs omit download URL and local storage path.")
        evidence.report["artifact_fixture_id"] = first["artifact_id"]
        evidence._write()


def scenario_artifact_worker_use(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S08B", "Artifact Inbox File Use Case With Worker") as s:
        artifact_id = evidence.report.get("artifact_fixture_id")
        if not artifact_id:
            raise Blocked("No artifact fixture id recorded by S08-S09.")
        started = structured(
            client.call_tool(
                "codex_worker_start",
                {
                    "repo_path": str(repo),
                    "name": "Artifact User",
                    "context_from_artifacts": [artifact_id],
                    "brief": (
                        "Use the imported artifact context. Create docs/artifact-used.md with exactly "
                        "'artifact context used'. Do not copy imported-artifacts into final changes and do not commit."
                    ),
                },
            )
        )
        s.check("worker_started_with_artifact", started.get("accepted") is True, "worker accepted context_from_artifacts.")
        wait_for_worker(client, "Artifact User", 600)
        changes = structured(client.call_tool("codex_worker_inspect", {"worker": "Artifact User", "view": "changes"}))
        changed = set(changes.get("changed_files") or [])
        s.check("artifact_change_visible", "docs/artifact-used.md" in changed, "worker created artifact-derived file.")
        s.check("imported_artifacts_excluded", not any(path.startswith(".ai-bridge/imported-artifacts/") for path in changed), "imported artifact directory excluded from changed files.")
        preview = structured(client.call_tool("codex_worker_inspect", {"worker": "Artifact User", "view": "integration_preview"}))
        serialized = json.dumps(preview)
        s.check("artifact_preview_excludes_imports", ".ai-bridge/imported-artifacts" not in serialized, "integration preview excludes imported artifact context.")


def scenario_worker_use_case(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S06", "Single ChatGPT Worker Use Case") as s:
        commit_count_before = git_commit_count(repo)
        options = structured(client.call_tool("codex_worker_options", {}))
        s.check("worker_options", bool(options.get("models") or options.get("reasoning_efforts")), "worker options returned.")
        started = structured(
            client.call_tool(
                "codex_worker_start",
                {
                    "repo_path": str(repo),
                    "name": "Implementer",
                    "brief": "Edit docs/task.md so it contains exactly 'implemented by worker' and do not commit.",
                },
            )
        )
        s.check("worker_started", started.get("accepted") is True, "Implementer accepted.")
        finished = wait_for_worker(client, "Implementer", 600)
        s.check("worker_idle", finished.get("state") == "idle", "Implementer reached idle state.")
        changes = structured(client.call_tool("codex_worker_inspect", {"worker": "Implementer", "view": "changes"}))
        s.check("changes_visible", "docs/task.md" in (changes.get("changed_files") or []), "worker changed docs/task.md.")
        worker_file = structured(client.call_tool("codex_worker_inspect", {"worker": "Implementer", "view": "file", "file_path": "docs/task.md"}))
        s.check("worker_file_visible", "implemented by worker" in worker_file.get("text", ""), "worker-side file visible.")
        diff = structured(client.call_tool("codex_worker_inspect", {"worker": "Implementer", "view": "diff", "file_path": "docs/task.md"}))
        s.check("worker_diff_visible", "implemented by worker" in diff.get("diff", ""), "one-file worker diff visible.")
        s.check("base_clean_before_apply", git_status(repo) == "", "base checkout clean before integration.")
        preview = structured(client.call_tool("codex_worker_inspect", {"worker": "Implementer", "view": "integration_preview"}))
        s.check("preview_can_apply", preview.get("can_apply") is True, "integration preview can apply.")
        applied = structured(client.call_tool("codex_worker_integrate", {"worker": "Implementer"}))
        s.check("applied", applied.get("applied") is True, "worker result applied.")
        s.check("no_commit", git_commit_count(repo) == commit_count_before, "integration did not create a commit.")
        s.check("base_dirty_after_apply", "docs/task.md" in git_status(repo), "base checkout dirty after apply.")
        cleanup_repo(repo)


def scenario_worker_restart(evidence: Evidence, server: ServerProcess, repo: Path) -> None:
    with evidence.scenario("S07", "Worker Continuation After Restart") as s:
        client = server.client("restart-a")
        client.initialize()
        started = structured(
            client.call_tool(
                "codex_worker_start",
                {
                    "repo_path": str(repo),
                    "name": "Restartable",
                    "brief": "Inspect this repo and report the current value in src/example.py. Do not edit and do not commit.",
                },
            )
        )
        s.check("worker_started", started.get("accepted") is True, "Restartable worker accepted.")
        wait_for_worker(client, "Restartable", 600)
        server.stop()
        server.start()
        restarted = server.client("restart-b")
        restarted.initialize()
        listed = structured(restarted.call_tool("codex_worker_list", {"repo_path": str(repo)}))
        names = {item.get("name") for item in listed.get("workers", [])}
        s.check("worker_relisted", "Restartable" in names, "same worker name listed after server restart.")
        continued = structured(
            restarted.call_tool(
                "codex_worker_message",
                {"worker": "Restartable", "message": "Continue from the same context. Reply with one short sentence and do not edit files."},
            )
        )
        s.check("restart_requires_takeover", continued.get("takeover_required") is True, "restart uses a new MCP client owner and requires explicit takeover.")
        continued = structured(
            restarted.call_tool(
                "codex_worker_message",
                {
                    "worker": "Restartable",
                    "message": "Continue from the same context. Reply with one short sentence and do not edit files.",
                    "takeover": True,
                },
            )
        )
        s.check("message_accepted", continued.get("accepted") is True, "continuation by name accepted after explicit takeover.")
        finished = wait_for_worker(restarted, "Restartable", 600)
        s.check("continued_idle", finished.get("state") == "idle", "continued worker reached idle.")


def scenario_multi_worker(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S10", "Multi-Worker Collaboration") as s:
        started = structured(
            client.call_tool(
                "codex_worker_start",
                {
                    "repo_path": str(repo),
                    "name": "Peer Implementer",
                    "brief": "Create docs/peer.md with exactly 'peer implementation'. Do not commit.",
                },
            )
        )
        s.check("implementer_started", started.get("accepted") is True, "Peer Implementer accepted.")
        wait_for_worker(client, "Peer Implementer", 600)
        reviewer = structured(
            client.call_tool(
                "codex_worker_start",
                {
                    "repo_path": str(repo),
                    "name": "Peer Reviewer",
                    "workspace_mode": "read_only",
                    "context_from_workers": ["Peer Implementer"],
                    "context_detail": "diff",
                    "brief": "Review the provided worker diff and report whether it is minimal. Do not edit.",
                },
            )
        )
        s.check("reviewer_started", reviewer.get("accepted") is True, "Peer Reviewer accepted with worker diff context.")
        wait_for_worker(client, "Peer Reviewer", 600)
        followup = structured(
            client.call_tool(
                "codex_worker_message",
                {
                    "worker": "Peer Implementer",
                    "context_from_workers": ["Peer Reviewer"],
                    "context_detail": "report",
                    "message": "Read the reviewer report and reply whether changes remain ready. Do not edit.",
                },
            )
        )
        s.check("review_sent_back", followup.get("accepted") is True, "reviewer report sent back to implementer.")
        wait_for_worker(client, "Peer Implementer", 600)
        team = structured(client.call_tool("codex_worker_list", {"repo_path": str(repo)}))
        s.check("team_report", bool(team.get("team_report")), "worker list includes team_report.")
        s.check("base_clean", git_status(repo) == "", "base checkout stayed clean during multi-worker collaboration.")


def scenario_multi_conversation_from_existing_trial(evidence: Evidence) -> None:
    # Kept as documentation hook; scenario S11 is covered by real_mcp_worker_trial
    # when this script is run as part of the full command set.
    pass


def scenario_repo_busy(evidence: Evidence, client_a: McpClient, client_b: McpClient, repo: Path) -> None:
    with evidence.scenario("S12", "Repository Mutation Lock And repo_busy") as s:
        client_b.initialize()
        switched = structured(client_b.call_tool("codex_tool_mode_switch", {"mode": "full", "reason": "repo_busy validation needs direct-write tool visibility"}))
        s.check("client_b_full_mode", switched.get("current_mode") == "full", "second MCP session switched to full mode for mutation-lock test.")
        result_holder: dict[str, Any] = {}

        def run_sleep() -> None:
            result_holder["command"] = client_a.call_tool("codex_run_command", {"repo_path": str(repo), "command": "sleep 4"})

        thread = threading.Thread(target=run_sleep, daemon=True)
        thread.start()
        time.sleep(0.5)
        busy = structured(client_b.call_tool("codex_write_file", {"repo_path": str(repo), "file_path": "busy.txt", "content": "busy\n"}))
        thread.join(timeout=10)
        s.check("write_refused_while_command_running", busy.get("repo_busy") is True, "direct write returned repo_busy while command held mutation lock.")
        s.check("no_partial_write", not (repo / "busy.txt").exists(), "busy write did not partially modify checkout.")


def scenario_low_level_jobs(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S13", "Plan/Apply Job Use Case") as s:
        plan = structured(client.call_tool("codex_plan_job", {"repo_path": str(repo), "spec": "Inspect this tiny repo and summarize src/example.py. Do not edit."}))
        s.check("plan_job_started", bool(plan.get("job_id")), "plan job returned job_id.")
        plan_result = wait_for_job(client, plan["job_id"], 600)
        s.check("plan_completed", plan_result.get("state") == "completed", "plan job completed.")
        apply = structured(client.call_tool("codex_apply_job", {"repo_path": str(repo), "spec": "Create docs/apply-job.md containing exactly 'apply job worked'. Do not commit."}))
        s.check("apply_job_started", bool(apply.get("job_id")), "apply job returned job_id.")
        apply_result = wait_for_job(client, apply["job_id"], 600)
        s.check("apply_completed", apply_result.get("state") == "completed", "apply job completed.")
        diff = structured(client.call_tool("codex_get_diff", {"job_id": apply["job_id"], "file_path": "docs/apply-job.md"}))
        s.check("apply_diff_readable", "apply job worked" in json.dumps(diff), "apply job diff/result inspectable.")


def scenario_resume(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S14", "Resume/Interactive Continuation Use Case") as s:
        started = structured(client.call_tool("codex_interactive", {"repo_path": str(repo), "spec": "Reply with exactly: resume seed. Do not edit files.", "sandbox": "read-only"}))
        s.check("interactive_started", bool(started.get("job_id")), "interactive job returned job_id.")
        first = wait_for_job(client, started["job_id"], 600)
        session_ref = first.get("session_ref")
        if not session_ref:
            raise Blocked("Codex did not return a session_ref for this interactive run.")
        resumed = structured(client.call_tool("codex_resume", {"repo_path": str(repo), "session_id": session_ref, "spec": "Reply with exactly: resume worked. Do not edit files.", "sandbox": "read-only"}))
        s.check("resume_started", bool(resumed.get("job_id")), "resume returned job_id.")
        second = wait_for_job(client, resumed["job_id"], 600)
        s.check("resume_completed", second.get("state") == "completed", "resume job completed.")


def scenario_session_discovery(evidence: Evidence, client: McpClient) -> None:
    with evidence.scenario("S15", "Codex Session Discovery") as s:
        sessions = structured(client.call_tool("codex_list_sessions", {"query": "inspect"}))
        serialized = json.dumps(sessions)
        s.check("session_discovered", sessions.get("count", 0) >= 1, "configured Codex home fixture session discovered.")
        s.check("metadata_only", sessions.get("transcripts_returned") is False and sessions.get("repo_paths_returned") is False, "list returns metadata only.")
        s.check("no_private_path", "/private/path/that/must/not/return" not in serialized, "private cwd not returned.")
        session_id = sessions["sessions"][0]["session_id"]
        read = structured(client.call_tool("codex_read_session", {"session_id": session_id, "max_messages": 5}))
        read_serialized = json.dumps(read)
        s.check("transcript_read_enabled", read.get("transcript_returned") is True, "bounded transcript read works when enabled.")
        s.check("transcript_redacted", "fixture-value" not in read_serialized and "[REDACTED" in read_serialized, "transcript redaction applied.")


def scenario_handoff(evidence: Evidence, client: McpClient, repo: Path) -> None:
    with evidence.scenario("S16", "Handoff Workflow") as s:
        written = structured(client.call_tool("codex_write_handoff", {"repo_path": str(repo), "plan": "Create docs/handoff.md in a local agent pass.", "title": "Validation handoff"}))
        s.check("handoff_written", ".ai-bridge" in written.get("path", ""), "handoff plan written under .ai-bridge.")
        status = structured(client.call_tool("codex_get_handoff_status", {"repo_path": str(repo), "create_if_missing": True}))
        s.check("handoff_status", "current-plan.md" in status.get("text", ""), "handoff status readable.")
        bridge = repo / ".ai-bridge"
        bridge.mkdir(exist_ok=True)
        (bridge / "implementation-diff.patch").write_text("diff --git a/docs/handoff.md b/docs/handoff.md\n", encoding="utf-8")
        diff = structured(client.call_tool("codex_get_handoff_diff", {"repo_path": str(repo)}))
        s.check("handoff_diff", "docs/handoff.md" in diff.get("text", ""), "handoff diff readable.")


def scenario_public_tunnel(evidence: Evidence, repo: Path, temp_dir: Path, args: argparse.Namespace) -> None:
    with evidence.scenario("S17", "Public Tunnel MCP Simulation") as s:
        if args.skip_public_tunnel:
            raise Blocked("Skipped by --skip-public-tunnel.")
        if not shutil.which("ngrok"):
            raise Blocked("ngrok is not installed.")
        code, stdout, stderr = run_capture(["ngrok", "config", "check"], timeout=20)
        evidence.command("ngrok_config_check", ["ngrok", "config", "check"], code, stdout, stderr)
        if code != 0:
            raise Blocked("ngrok config check failed.")
        if not args.ngrok_hostname:
            raise Blocked("ngrok is installed and configured, but no PATCHBAY_VALIDATION_NGROK_HOSTNAME/--ngrok-hostname was provided for start.py --tunnel-mode ngrok.")
        token = "validation-token-" + "t" * 24
        runtime = temp_dir / "tunnel-runtime"
        codex_home = temp_dir / "tunnel-codex-home"
        config = write_config(repo, runtime, codex_home, tool_mode="worker")
        port = free_port()
        env = dict(os.environ)
        env["PATCHBAY_HTTP_TOKEN"] = token
        env["PATCHBAY_HOME"] = str(runtime)
        command = [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(config),
            "--root",
            str(repo),
            "--port",
            str(port),
            "--tool-mode",
            "worker",
            "--tunnel-mode",
            "ngrok",
            "--hostname",
            args.ngrok_hostname,
            "--reveal-token",
            "--no-profile",
            "--force",
        ]
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        tail: list[str] = []
        try:
            wait_for_health(port, process, tail, timeout=45)
            public = f"https://{args.ngrok_hostname}".rstrip("/")
            external = McpClient(public, evidence, "public-tunnel", token=token)
            health = external.get("/")
            s.check("public_health", health.get("status") == "running", "public tunnel health passed with Bearer token.")
            external.initialize()
            names = set(tools_by_name(external.rpc("tools/list")))
            s.check("public_tools", "codex_worker_start" in names and "codex_get_status" not in names, "public worker-mode tools list passed.")
            no_token = McpClient(public, evidence, "public-no-token")
            rejected = no_token.get("/", expect_error=True)
            s.check("missing_token_rejected", rejected.get("http_error") in {401, 403}, "missing token rejected.")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def scenario_real_chatgpt_manual_gate(evidence: Evidence) -> None:
    with evidence.scenario("S18", "Real ChatGPT Developer Mode Manual Gate"):
        raise Blocked("Not executable from this Codex process without controlling the real ChatGPT Developer Mode UI. MCP-level simulation evidence is recorded separately.")


def mark_blocked(evidence: Evidence, ident: str, title: str, message: str) -> None:
    with evidence.scenario(ident, title):
        raise Blocked(message)


def wait_for_worker(client: McpClient, worker: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = structured(client.call_tool("codex_worker_inspect", {"worker": worker, "view": "report", "wait_seconds": 10}))
        if last.get("state") not in ACTIVE_WORKER_STATES:
            return last
        time.sleep(0.2)
    raise TimeoutError(f"worker {worker} did not finish; last state={last.get('state')}")


def wait_for_job(client: McpClient, job_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = structured(client.call_tool("codex_get_status", {"job_id": job_id}))
        if status.get("state") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(2)
    result = structured(client.call_tool("codex_get_result", {"job_id": job_id}))
    return result if result else status


def structured(payload: dict[str, Any]) -> dict[str, Any]:
    if "error" in payload:
        raise ValidationFailure(f"MCP error response: {payload['error']}")
    result = payload.get("result", {})
    content = result.get("structuredContent")
    if isinstance(content, dict):
        return content
    raise ValidationFailure(f"MCP response missing structuredContent: {payload}")


def tools_by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if "error" in payload:
        raise ValidationFailure(f"MCP error response: {payload['error']}")
    return {tool["name"]: tool for tool in payload.get("result", {}).get("tools", [])}


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# External Validation Repo\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Disposable validation repo. Do not commit generated changes.\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "example.py").write_text("def answer():\n    return 'original'\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "task.md").write_text("pending\n", encoding="utf-8")
    skill = repo / "skills" / "repo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("name: repo-skill\ndescription: Disposable validation skill\n\nUse this skill only for validation.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Validation", "-c", "user.email=validation@example.invalid", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def write_config(repo: Path, runtime: Path, codex_home: Path, *, tool_mode: str, direct_write: bool = True, bash_mode: str = "full", session_read: bool = True) -> Path:
    runtime.mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    config.setdefault("app", {})["tool_mode"] = tool_mode
    config.setdefault("repositories", {})["default"] = str(repo)
    config.setdefault("repositories", {})["allowed"] = [str(repo)]
    config.setdefault("auth", {})["tunnel_mode"] = "none"
    config.setdefault("server", {})["max_concurrent_jobs"] = 2
    config.setdefault("logging", {})["job_logs_dir"] = str(runtime / "logs" / "jobs")
    config.setdefault("logging", {})["job_state_dir"] = str(runtime / "logs" / "jobs" / "state")
    config["artifacts"] = {
        **(config.get("artifacts") or {}),
        "root": str(runtime / "artifacts"),
        "allowed_download_schemes": ["http", "https"],
        "max_archive_bytes": 5_000_000,
        "max_unpacked_bytes": 5_000_000,
        "max_single_file_bytes": 1_000_000,
        "max_file_count": 100,
    }
    config["workers"] = {
        **(config.get("workers") or {}),
        "worktree_root": str(runtime / "worker-worktrees"),
        "ignore_user_config": True,
    }
    config["locks"] = {"root": str(runtime / "locks")}
    config["power_tools"] = {
        **(config.get("power_tools") or {}),
        "direct_write": direct_write,
        "bash_mode": bash_mode,
        "codex_session_read": session_read,
        "codex_home": str(codex_home),
        "codex_session_max_messages": 40,
        "codex_session_max_bytes": 80_000,
        "codex_session_max_scan_files": 1000,
        "codex_session_max_scan_depth": 6,
    }
    security = config.setdefault("security", {})
    security["blocked_globs"] = [".env", ".env.*", "**/.env", "**/.env.*", ".git", ".git/**", "**/.git/**"]
    path = runtime / f"validation-{tool_mode}.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_codex_session_fixture(codex_home: Path) -> None:
    session_id = "019e4789-9b15-77e0-8ddc-13b9525fd730"
    path = codex_home / "sessions" / "2026" / "06" / "22"
    path.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": "2026-06-22T10:00:00Z", "type": "session_meta", "payload": {"id": session_id, "cwd": "/private/path/that/must/not/return"}},
        {"timestamp": "2026-06-22T10:01:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "please inspect token=fixture-value"}]}},
        {"timestamp": "2026-06-22T10:02:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
    ]
    (path / f"rollout-2026-06-22T00-00-00-{session_id}.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def make_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)


@contextlib.contextmanager
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


def cleanup_repo(repo: Path) -> None:
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], cwd=repo, check=True, capture_output=True)


def git_status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def git_commit_count(repo: Path) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return int(out)


def run_capture(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def wait_for_health(port: int, process: subprocess.Popen[str], output_tail: list[str], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        read_available_output(process, output_tail)
        if process.poll() is not None:
            raise RuntimeError("server exited before health check passed: " + "\n".join(output_tail[-20:]))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("transport") == "streamable-http":
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError("server did not become healthy: " + "\n".join(output_tail[-20:]))


def read_available_output(process: subprocess.Popen[str], output_tail: list[str]) -> None:
    if not process.stdout:
        return
    while True:
        readable, _, _ = select.select([process.stdout.fileno()], [], [], 0)
        if not readable:
            return
        line = process.stdout.readline()
        if not line:
            return
        output_tail.append(line.rstrip())
        del output_tail[:-200]


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        "# External ChatGPT-Style Validation",
        "",
        f"Status: `{report.get('status')}`",
        f"Started: `{report.get('started_at')}`",
        f"Updated: `{report.get('updated_at')}`",
        "",
        "## Scenarios",
        "",
        "| ID | Scenario | Status | Checks |",
        "| --- | --- | --- | --- |",
    ]
    for item in report.get("scenarios", []):
        checks = item.get("checks") or []
        passed = sum(1 for check in checks if check.get("ok"))
        lines.append(f"| `{item.get('id')}` | {item.get('title')} | `{item.get('status')}` | {passed}/{len(checks)} |")
        if item.get("blocker"):
            lines.append(f"|  | blocker |  | {item.get('blocker')} |")
        if item.get("error"):
            lines.append(f"|  | error |  | {item.get('error')} |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
