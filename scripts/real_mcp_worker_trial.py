#!/usr/bin/env python3
"""Durable real MCP worker trial harness.

The harness intentionally writes evidence as it runs. If the process is
interrupted, the output directory should still contain useful partial
`calls.jsonl`, `results.json`, and `summary.md` artifacts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import select
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

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOOL_CARD_URI = "ui://widget/patchbay-tool-card-v2.html"
REQUIRED_WORKER_TOOLS = {
    "codex_open_workspace",
    "codex_worker_options",
    "codex_worker_start",
    "codex_worker_message",
    "codex_worker_list",
    "codex_worker_inspect",
    "codex_worker_integrate",
    "codex_worker_stop",
    "codex_self_test",
}
ACTIVE_STATES = {"starting", "working"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)=([^\s,;}]+)"),
]
UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
PRIVATE_BRANCH_PATTERN = re.compile(r"\bcodex/(?:worker|job)-[A-Za-z0-9._/-]+\b")
CONNECTOR_NOISE_PATTERNS = {
    "oauth": re.compile(r"oauth", re.IGNORECASE),
    "connector": re.compile(r"connector", re.IGNORECASE),
    "mcp_server": re.compile(r"\bmcp[-_ ]?(server|connector|oauth|remote|tool)\b", re.IGNORECASE),
    "mcp_servers_config": re.compile(r"mcp_servers", re.IGNORECASE),
}
PROMPT_KEYS = {"brief", "message", "prompt", "spec", "content"}


class SimulatedInterruption(RuntimeError):
    """Raised by --simulate-interrupt-after-calls for partial-evidence tests."""


class TrialFailure(RuntimeError):
    """Raised when a trial assertion fails after evidence has been written."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def path_aliases(value: str) -> set[str]:
    aliases = {str(value)}
    if value.startswith(("/", "~")):
        try:
            resolved = str(Path(value).expanduser().resolve())
            aliases.add(resolved)
        except Exception:
            pass
    for item in list(aliases):
        if item.startswith("/private/var/"):
            aliases.add(item.replace("/private/var/", "/var/", 1))
        elif item.startswith("/var/"):
            aliases.add("/private" + item)
    return aliases


class TrialRecorder:
    def __init__(self, out_dir: Path, replacements: dict[str, str]):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.replacements: dict[str, str] = {}
        for source, target in replacements.items():
            if not source:
                continue
            for candidate in path_aliases(source):
                self.replacements[candidate] = target
        self.calls_path = out_dir / "calls.jsonl"
        self.results_path = out_dir / "results.json"
        self.summary_path = out_dir / "summary.md"
        self.call_sequence: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {
            "name": "real-mcp-worker-trial",
            "status": "running",
            "classification": "in_progress",
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "tool_mode": "",
            "chatgpt_tunnel_status": "not_run_direct_local_mcp_only",
            "checks": [],
            "workers": [],
            "calls_jsonl": "calls.jsonl",
            "summary_md": "summary.md",
        }
        self._write_all()

    def set_metadata(self, **values: Any) -> None:
        self.report.update(self.sanitize(values))
        self._write_all()

    def log_call_event(self, event: dict[str, Any]) -> None:
        safe = self.sanitize(event)
        with self.calls_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if safe.get("phase") == "response":
            self.call_sequence.append(
                {
                    "index": safe.get("index"),
                    "client": safe.get("client"),
                    "method": safe.get("method"),
                    "tool": safe.get("tool"),
                    "mcp_id": safe.get("mcp_id"),
                    "ok": safe.get("ok"),
                    "duration_seconds": safe.get("duration_seconds"),
                }
            )
            self.report["mcp_call_sequence"] = self.call_sequence
            self._write_all()

    def check(self, name: str, ok: bool, *, classification: str, evidence: str) -> None:
        item = {
            "name": name,
            "ok": bool(ok),
            "classification": classification,
            "evidence": evidence,
            "at": utc_now(),
        }
        self.report.setdefault("checks", []).append(self.sanitize(item))
        self._write_all()
        if not ok:
            raise TrialFailure(f"check failed: {name}: {evidence}")

    def add_worker(self, name: str) -> None:
        workers = self.report.setdefault("workers", [])
        if name not in workers:
            workers.append(name)
            self._write_all()

    def mark_status(self, status: str, *, classification: str, error: str | None = None) -> None:
        self.report["status"] = status
        self.report["classification"] = classification
        self.report["updated_at"] = utc_now()
        if error:
            self.report["error"] = self.sanitize(error)
        self._write_all()

    def sanitize(self, value: Any, *, request: bool = False) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, child in value.items():
                if request and str(key) in PROMPT_KEYS:
                    text = str(child or "")
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                    cleaned[key] = f"<redacted-natural-language-field chars={len(text)} sha256={digest}>"
                else:
                    cleaned[key] = self.sanitize(child, request=request)
            return cleaned
        if isinstance(value, list):
            return [self.sanitize(item, request=request) for item in value]
        if isinstance(value, tuple):
            return [self.sanitize(item, request=request) for item in value]
        if not isinstance(value, str):
            return value

        safe = value
        for source, replacement in sorted(self.replacements.items(), key=lambda item: len(item[0]), reverse=True):
            safe = safe.replace(source, replacement)
            safe = safe.replace(source.replace("\\", "/"), replacement)
        for pattern in SECRET_PATTERNS:
            safe = pattern.sub(lambda match: match.group(0).split("=", 1)[0] + "=[REDACTED]" if "=" in match.group(0) else "[REDACTED_POSSIBLE_SECRET]", safe)
        safe = UUID_PATTERN.sub("<uuid>", safe)
        safe = PRIVATE_BRANCH_PATTERN.sub("<worker-branch>", safe)
        return safe

    def _write_all(self) -> None:
        self.report["updated_at"] = utc_now()
        _write_json_atomic(self.results_path, self.sanitize(self.report))
        self.summary_path.write_text(render_summary(self.sanitize(self.report)), encoding="utf-8")


class McpClient:
    def __init__(
        self,
        base_url: str,
        recorder: TrialRecorder,
        *,
        client_label: str = "client-a",
        simulate_interrupt_after_calls: int = 0,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.recorder = recorder
        self.client_label = client_label
        self.session_id: str | None = None
        self.timeout = timeout
        self.index = 0
        self.response_count = 0
        self.simulate_interrupt_after_calls = max(0, int(simulate_interrupt_after_calls or 0))

    def get(self, path: str, *, label: str) -> dict[str, Any]:
        self.index += 1
        index = self.index
        started = time.monotonic()
        self.recorder.log_call_event(
            {
                "at": utc_now(),
                "index": index,
                "client": self.client_label,
                "phase": "request",
                "transport": "http",
                "method": "GET",
                "tool": label,
                "url": self.base_url + path,
            }
        )
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self._log_response(index, "GET", label, payload, started, http_status=response.status)
                return payload
        except SimulatedInterruption:
            raise
        except Exception as error:
            self._log_response(index, "GET", label, {"error": f"{type(error).__name__}: {error}"}, started, ok=False)
            raise

    def rpc(self, msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _, payload = self.post({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        return payload

    def call_tool(self, msg_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.rpc(msg_id, "tools/call", {"name": name, "arguments": arguments})

    def post(self, message: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        self.index += 1
        index = self.index
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        tool = str(params.get("name") or "") if isinstance(params, dict) else ""
        started = time.monotonic()
        self.recorder.log_call_event(
            {
                "at": utc_now(),
                "index": index,
                "client": self.client_label,
                "phase": "request",
                "transport": "mcp-http",
                "method": method,
                "tool": tool or None,
                "mcp_id": message.get("id"),
                "request": self.recorder.sanitize(message, request=True),
            }
        )
        data = json.dumps(message).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.base_url + "/mcp", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                session_id = response.headers.get("Mcp-Session-Id")
                self._log_response(index, method, tool, payload, started, http_status=response.status, mcp_id=message.get("id"), session_id=session_id)
                return session_id, payload
        except SimulatedInterruption:
            raise
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"error": raw}
            self._log_response(index, method, tool, payload, started, http_status=error.code, ok=False, mcp_id=message.get("id"))
            return None, payload
        except Exception as error:
            payload = {"error": f"{type(error).__name__}: {error}"}
            self._log_response(index, method, tool, payload, started, ok=False, mcp_id=message.get("id"))
            raise

    def _log_response(
        self,
        index: int,
        method: str,
        tool: str,
        payload: dict[str, Any],
        started: float,
        *,
        ok: bool = True,
        http_status: int | None = None,
        mcp_id: Any = None,
        session_id: str | None = None,
    ) -> None:
        self.recorder.log_call_event(
            {
                "at": utc_now(),
                "index": index,
                "client": self.client_label,
                "phase": "response",
                "method": method,
                "tool": tool or None,
                "mcp_id": mcp_id,
                "ok": bool(ok),
                "http_status": http_status,
                "session_id": "<mcp-session-id>" if session_id else None,
                "duration_seconds": round(time.monotonic() - started, 3),
                "response": payload,
            }
        )
        self.response_count += 1
        if self.simulate_interrupt_after_calls and self.response_count >= self.simulate_interrupt_after_calls:
            raise SimulatedInterruption(f"simulated interruption after {self.response_count} response(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a real MCP-over-HTTP worker lifecycle trial against a disposable repo.")
    parser.add_argument(
        "--output-dir",
        default=".local/validation/real_mcp_trial",
        help="Parent directory for timestamped evidence. Defaults to ignored local state.",
    )
    parser.add_argument("--port", type=int, help="Local port. Defaults to a free loopback port.")
    parser.add_argument("--tool-mode", default="worker", choices=["worker", "full", "standard"], help="PatchBay tool mode to launch.")
    parser.add_argument("--startup-timeout", type=float, default=25.0)
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument("--worker-timeout", type=float, default=600.0)
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print final result JSON.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--simulate-interrupt-after-calls", type=int, default=0)
    parser.add_argument("--include-safety-cases", action="store_true", help="Run real-MCP worker safety negative cases.")
    parser.add_argument(
        "--multi-client",
        action="store_true",
        help="Run an additional two-session MCP scenario covering session-local modes, shared inspection, refusal, and takeover.",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir) / timestamp()
    temp_dir = Path(tempfile.mkdtemp(prefix="codex-real-mcp-worker-trial."))
    repo = temp_dir / "repo"
    runtime = temp_dir / "runtime"
    recorder = TrialRecorder(
        out_dir,
        {
            str(temp_dir): "<trial-temp>",
            str(repo): "<trial-repo>",
            str(runtime): "<trial-runtime>",
            str(ROOT): "<PatchBay-repo>",
        },
    )
    process: subprocess.Popen[str] | None = None
    output_tail: list[str] = []

    try:
        init_repo(repo)
        port = args.port or free_port()
        server_url = f"http://127.0.0.1:{port}/mcp"
        trial_config = write_trial_config(repo, runtime, tool_mode=args.tool_mode, multi_client=args.multi_client)
        env = dict(os.environ)
        env["PATCHBAY_HOME"] = str(runtime)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(trial_config),
            "--root",
            str(repo),
            "--port",
            str(port),
            "--tool-mode",
            args.tool_mode,
            "--tunnel-mode",
            "none",
            "--no-profile",
            "--force",
        ]
        recorder.set_metadata(
            output_dir=str(out_dir),
            disposable_repo=str(repo),
            runtime_root=str(runtime),
            trial_config=str(trial_config),
            server_url=server_url,
            server_command=command,
            tool_mode=args.tool_mode,
            multi_client=args.multi_client,
            python_version=sys.version.split()[0],
            codex_version=codex_version(),
            codex_user_config_policy="worker_jobs_use_--ignore-user-config; Codex auth still uses CODEX_HOME",
            git_branch=git_output(["rev-parse", "--abbrev-ref", "HEAD"]),
            git_commit=git_output(["rev-parse", "--short", "HEAD"]),
        )

        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_for_health(port, process, output_tail, timeout=args.startup_timeout)
        client = McpClient(
            f"http://127.0.0.1:{port}",
            recorder,
            client_label="client-a",
            simulate_interrupt_after_calls=args.simulate_interrupt_after_calls,
            timeout=args.request_timeout,
        )

        run_trial(
            client,
            recorder,
            repo,
            runtime,
            worker_timeout=args.worker_timeout,
            include_safety_cases=args.include_safety_cases,
            multi_client=args.multi_client,
        )
        recorder.mark_status("passed", classification="direct_evidence")
        print_result(recorder.sanitize(recorder.report), args.json)
        return 0
    except SimulatedInterruption as error:
        recorder.mark_status("partial", classification="simulated_interruption", error=str(error))
        print_result(recorder.sanitize(recorder.report), args.json)
        return 2
    except KeyboardInterrupt:
        recorder.mark_status("partial", classification="interrupted", error="Interrupted by user.")
        print_result(recorder.sanitize(recorder.report), args.json)
        return 2
    except Exception as error:
        if process:
            read_available_output(process, output_tail)
        if output_tail:
            recorder.set_metadata(launcher_output_tail=output_tail[-80:] if args.verbose else output_tail[-20:])
        recorder.mark_status("failed", classification="runtime_bug_or_environment_blocker", error=f"{type(error).__name__}: {error}")
        print_result(recorder.sanitize(recorder.report), args.json)
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


def run_trial(
    client: McpClient,
    recorder: TrialRecorder,
    repo: Path,
    runtime: Path,
    *,
    worker_timeout: float,
    include_safety_cases: bool,
    multi_client: bool,
) -> None:
    health = client.get("/", label="health")
    recorder.check(
        "health",
        health.get("transport") == "streamable-http" and health.get("status") == "running",
        classification="direct_evidence",
        evidence="Loopback server responded with Streamable HTTP health metadata.",
    )

    session_id, initialize = client.post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "real-mcp-worker-trial", "version": "1.0.0"},
            },
        }
    )
    client.session_id = session_id
    recorder.check(
        "initialize",
        bool(session_id) and initialize.get("result", {}).get("serverInfo", {}).get("name") == "patchbay",
        classification="direct_evidence",
        evidence="MCP initialize returned serverInfo and session header.",
    )

    tools_payload = client.rpc(2, "tools/list")
    tools = {tool["name"]: tool for tool in tools_payload.get("result", {}).get("tools", [])}
    recorder.check(
        "worker_mode_tools",
        REQUIRED_WORKER_TOOLS <= set(tools) and "codex_get_status" not in tools,
        classification="direct_evidence",
        evidence="Worker mode exposed worker/context tools and hid low-level job status tools.",
    )

    secondary_client: McpClient | None = None
    if multi_client:
        secondary_client = run_multi_client_session_probe(client, recorder)

    resources = client.rpc(3, "resources/list")
    resource_uris = {resource["uri"] for resource in resources.get("result", {}).get("resources", [])}
    recorder.check(
        "resources_list",
        TOOL_CARD_URI in resource_uris,
        classification="direct_evidence",
        evidence="Tool card resource is available over MCP.",
    )
    card = client.rpc(4, "resources/read", {"uri": TOOL_CARD_URI})
    recorder.check(
        "resources_read",
        card.get("result", {}).get("contents", [{}])[0].get("mimeType") == "text/html;profile=mcp-app",
        classification="direct_evidence",
        evidence="Tool card resource body is readable.",
    )

    self_test = structured(client.call_tool(5, "codex_self_test", {}))
    recorder.check(
        "self_test",
        self_test.get("ready") is True,
        classification="direct_evidence",
        evidence="Self-test reported the loopback direct connection ready.",
    )

    workspace = structured(
        client.call_tool(
            6,
            "codex_open_workspace",
            {"repo_path": str(repo), "include_tree": True, "include_global_skills": False},
        )
    )
    recorder.check(
        "open_workspace",
        workspace.get("git", {}).get("is_git_repo") is True
        and "src/" in workspace.get("tree", {}).get("text", "")
        and "example.py" in workspace.get("tree", {}).get("text", ""),
        classification="direct_evidence",
        evidence="Workspace orientation returned git metadata and the disposable repo tree.",
    )

    if include_safety_cases:
        run_safety_cases(client, recorder, repo, runtime, worker_timeout=worker_timeout)

    commit_count_before = git_commit_count(repo)
    worker_name = "Small Implementer"
    recorder.add_worker(worker_name)
    started = structured(
        client.call_tool(
            7,
            "codex_worker_start",
            {
                "repo_path": str(repo),
                "name": worker_name,
                "workspace_mode": "isolated_write",
                "brief": (
                    "Make this tiny disposable repo change. In src/example.py, change answer() so it returns "
                    "exactly \"worker-result\". Create docs/worker-note.md containing exactly "
                    "\"worker note from real MCP trial\" followed by a newline. Do not commit. Report the files changed."
                ),
            },
        )
    )
    recorder.check(
        "worker_start",
        started.get("accepted") is True and started.get("state") in ACTIVE_STATES,
        classification="direct_evidence",
        evidence="Writing worker was accepted and started through MCP HTTP.",
    )

    finished = wait_for_worker(client, worker_name, worker_timeout)
    recorder.check(
        "worker_completed",
        finished.get("state") == "idle" and finished.get("has_session") is True,
        classification="direct_evidence",
        evidence=f"Worker completed with state={finished.get('state')} and resumable session={finished.get('has_session')}.",
    )

    integration_client = client
    if secondary_client is not None:
        integration_client = run_multi_client_worker_takeover(
            secondary_client,
            client,
            recorder,
            worker_name,
            worker_timeout=worker_timeout,
        )

    changes = structured(integration_client.call_tool(1001, "codex_worker_inspect", {"worker": worker_name, "view": "changes"}))
    changed_files = set(changes.get("changed_files") or [])
    recorder.check(
        "changes_view",
        {"src/example.py", "docs/worker-note.md"} <= changed_files,
        classification="direct_evidence",
        evidence=f"Changes view reported: {sorted(changed_files)}.",
    )

    worker_file = structured(
        integration_client.call_tool(
            1006,
            "codex_worker_inspect",
            {"worker": worker_name, "view": "file", "file_path": "docs/worker-note.md"},
        )
    )
    recorder.check(
        "worker_file_view",
        worker_file.get("source") == "worker_workspace"
        and worker_file.get("exists") is True
        and "worker note from real MCP trial" in worker_file.get("text", ""),
        classification="direct_evidence",
        evidence="Worker-side file view returned the new file before base integration.",
    )

    diff = structured(integration_client.call_tool(1002, "codex_worker_inspect", {"worker": worker_name, "view": "diff", "file_path": "src/example.py"}))
    recorder.check(
        "one_file_diff",
        "+    return \"worker-result\"" in diff.get("diff", "") or "+return \"worker-result\"" in diff.get("diff", ""),
        classification="direct_evidence",
        evidence="One-file diff includes the expected answer() change.",
    )

    base_before_integration = git_status(repo)
    recorder.check(
        "base_clean_before_integration",
        base_before_integration == "",
        classification="direct_evidence",
        evidence="Base checkout stayed clean while the worker worked in an isolated worktree.",
    )

    preview = structured(integration_client.call_tool(1003, "codex_worker_inspect", {"worker": worker_name, "view": "integration_preview"}))
    recorder.check(
        "integration_preview",
        preview.get("can_apply") is True and preview.get("applied") is False,
        classification="direct_evidence",
        evidence="Read-only integration preview reported a clean applicable patch.",
    )

    integrated = structured(integration_client.call_tool(1004, "codex_worker_integrate", {"worker": worker_name}))
    commit_count_after = git_commit_count(repo)
    recorder.check(
        "integration_applied",
        integrated.get("applied") is True and (repo / "docs" / "worker-note.md").exists(),
        classification="direct_evidence",
        evidence="Worker result applied to the base checkout.",
    )
    recorder.check(
        "integration_did_not_commit",
        commit_count_after == commit_count_before,
        classification="direct_evidence",
        evidence=f"Commit count before/after integration stayed {commit_count_before}; harness created no normalizing commit.",
    )
    recorder.check(
        "base_dirty_after_integration",
        "src/example.py" in git_status(repo) and "docs/worker-note.md" in git_status(repo),
        classification="direct_evidence",
        evidence="Base checkout is dirty after integration, proving the result was applied without committing.",
    )

    listed = structured(integration_client.call_tool(1005, "codex_worker_list", {}))
    recorder.check(
        "worker_list_after_integration",
        "applied_to_checkout" in json.dumps(listed),
        classification="direct_evidence",
        evidence="Worker list preserved integration state after apply.",
    )

    noise = scan_job_stderr_for_connector_noise(runtime)
    recorder.check(
        "codex_connector_noise_scan",
        noise["matches"] == 0,
        classification="direct_evidence" if noise["matches"] == 0 else "environment_blocker",
        evidence=(
            f"Scanned {noise['stderr_logs_scanned']} worker stderr artifact(s); "
            f"connector/OAuth noise matches={noise['matches']} categories={noise['matched_categories']}."
        ),
    )

    scan_text = "\n".join(
        [
            recorder.calls_path.read_text(encoding="utf-8"),
            recorder.results_path.read_text(encoding="utf-8"),
            recorder.summary_path.read_text(encoding="utf-8"),
        ]
    )
    private_leaks = [str(repo), str(repo.parent / "runtime"), str(repo.parent)]
    pattern_leaks = []
    for name, pattern in {
        "uuid": UUID_PATTERN,
        "private_worker_branch": PRIVATE_BRANCH_PATTERN,
        "possible_secret": re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    }.items():
        if pattern.search(scan_text):
            pattern_leaks.append(name)
    recorder.check(
        "artifact_private_path_scan",
        not any(path in scan_text for path in private_leaks) and not pattern_leaks,
        classification="direct_evidence",
        evidence=(
            "Progressive artifacts contain sanitized placeholders instead of disposable local paths, "
            f"private branch names, UUID-like session ids, or obvious token patterns. pattern_leaks={pattern_leaks}."
        ),
    )


def run_multi_client_session_probe(primary: McpClient, recorder: TrialRecorder) -> McpClient:
    secondary = McpClient(primary.base_url, recorder, client_label="client-b", timeout=primary.timeout)
    session_id, initialize = secondary.post(
        {
            "jsonrpc": "2.0",
            "id": 4001,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "real-mcp-worker-trial-client-b", "version": "1.0.0"},
            },
        }
    )
    secondary.session_id = session_id
    recorder.check(
        "multi_client_initialize",
        bool(session_id)
        and session_id != primary.session_id
        and initialize.get("result", {}).get("serverInfo", {}).get("name") == "patchbay",
        classification="direct_evidence",
        evidence="A second logical MCP client initialized with a separate transport session.",
    )

    before_primary_names = tool_names(primary.rpc(4002, "tools/list"))
    before_secondary_names = tool_names(secondary.rpc(4003, "tools/list"))
    recorder.check(
        "multi_client_initial_worker_modes",
        "codex_resume" not in before_primary_names and "codex_resume" not in before_secondary_names,
        classification="direct_evidence",
        evidence="Both MCP sessions started in worker mode and hid low-level resume tools.",
    )

    switched = structured(
        primary.call_tool(
            4004,
            "codex_tool_mode_switch",
            {"mode": "full", "reason": "Multi-client trial verifies session-local mode switching."},
        )
    )
    primary_full_names = tool_names(primary.rpc(4005, "tools/list"))
    secondary_worker_names = tool_names(secondary.rpc(4006, "tools/list"))
    recorder.check(
        "multi_client_session_local_tool_modes",
        switched.get("switch_scope") == "session"
        and "codex_resume" in primary_full_names
        and "codex_resume" not in secondary_worker_names,
        classification="direct_evidence",
        evidence="Switching client A to full mode did not expand client B's tool catalog.",
    )

    self_test = structured(secondary.call_tool(4007, "codex_self_test", {}))
    coordination = self_test.get("coordination") or {}
    recorder.check(
        "multi_client_coordination_metadata",
        coordination.get("shared_server") is True
        and coordination.get("active_mcp_sessions", 0) >= 2
        and coordination.get("raw_session_ids_returned") is False
        and str(primary.session_id) not in json.dumps(coordination),
        classification="direct_evidence",
        evidence="Self-test reported shared-server coordination metadata without raw session ids.",
    )

    switched_back = structured(
        primary.call_tool(
            4008,
            "codex_tool_mode_switch",
            {"mode": "worker", "reason": "Return the primary trial client to the recommended worker catalog."},
        )
    )
    primary_worker_names = tool_names(primary.rpc(4009, "tools/list"))
    recorder.check(
        "multi_client_primary_mode_restored",
        switched_back.get("current_mode") == "worker" and "codex_resume" not in primary_worker_names,
        classification="direct_evidence",
        evidence="Client A returned to worker mode before worker lifecycle checks continued.",
    )
    return secondary


def run_multi_client_worker_takeover(
    secondary: McpClient,
    primary: McpClient,
    recorder: TrialRecorder,
    worker_name: str,
    *,
    worker_timeout: float,
) -> McpClient:
    visible = structured(secondary.call_tool(5001, "codex_worker_inspect", {"worker": worker_name, "view": "report"}))
    recorder.check(
        "multi_client_other_owner_inspect_allowed",
        visible.get("owned_by_current_client") is False and visible.get("ownership_status") == "other_connection",
        classification="direct_evidence",
        evidence="Client B could inspect a worker created by client A and saw safe other-owner metadata.",
    )

    refused = structured(
        secondary.call_tool(
            5002,
            "codex_worker_message",
            {
                "worker": worker_name,
                "message": "Do not edit files. This message should be refused unless takeover is explicit.",
            },
        )
    )
    recorder.check(
        "multi_client_other_owner_message_refused",
        refused.get("accepted") is False and refused.get("takeover_required") is True,
        classification="direct_evidence",
        evidence="Client B could not mutate client A's worker without explicit takeover.",
    )

    accepted = structured(
        secondary.call_tool(
            5003,
            "codex_worker_message",
            {
                "worker": worker_name,
                "takeover": True,
                "takeover_reason": "User confirmed client B should continue this disposable trial worker.",
                "message": (
                    "Do not edit files and do not commit. Reply that takeover was acknowledged and that the "
                    "existing worker changes remain ready for integration."
                ),
            },
        )
    )
    recorder.check(
        "multi_client_takeover_message_accepted",
        accepted.get("accepted") is True and accepted.get("takeover_performed") is True,
        classification="direct_evidence",
        evidence="Client B explicitly took over the worker and delivered a continuation message.",
    )

    takeover_finished = wait_for_worker(secondary, worker_name, worker_timeout)
    recorder.check(
        "multi_client_takeover_turn_completed",
        takeover_finished.get("state") == "idle" and takeover_finished.get("owned_by_current_client") is True,
        classification="direct_evidence",
        evidence="The takeover continuation completed and client B became the current owner.",
    )

    primary_view = structured(primary.call_tool(5004, "codex_worker_inspect", {"worker": worker_name, "view": "report"}))
    secondary_view = structured(secondary.call_tool(5005, "codex_worker_inspect", {"worker": worker_name, "view": "report"}))
    recorder.check(
        "multi_client_ownership_transferred",
        primary_view.get("owned_by_current_client") is False
        and primary_view.get("ownership_status") == "other_connection"
        and secondary_view.get("owned_by_current_client") is True
        and secondary_view.get("ownership_status") == "current_client",
        classification="direct_evidence",
        evidence="After explicit takeover, ownership flags flipped for the two MCP sessions.",
    )
    return secondary


def run_safety_cases(client: McpClient, recorder: TrialRecorder, repo: Path, runtime: Path, *, worker_timeout: float) -> None:
    active_name = "Active Worker"
    recorder.add_worker(active_name)
    active_started = structured(
        client.call_tool(
            3001,
            "codex_worker_start",
            {
                "repo_path": str(repo),
                "name": active_name,
                "workspace_mode": "isolated_write",
                "brief": (
                    "Create docs/active-worker.md with one short sentence. Do not commit. "
                    "Report what you changed."
                ),
            },
        )
    )
    recorder.check(
        "safety_active_worker_started",
        active_started.get("accepted") is True and active_started.get("state") in ACTIVE_STATES,
        classification="direct_evidence",
        evidence="Active worker accepted before integration refusal probe.",
    )
    active_integrate = structured(client.call_tool(3002, "codex_worker_integrate", {"worker": active_name}))
    recorder.check(
        "safety_active_worker_integrate_refused",
        active_integrate.get("applied") is False
        and active_integrate.get("can_apply") is False
        and "still working" in str(active_integrate.get("note", "")).lower(),
        classification="direct_evidence",
        evidence="codex_worker_integrate refused an active worker turn.",
    )
    wait_for_worker(client, active_name, worker_timeout)

    safety_name = "Safety Worker"
    recorder.add_worker(safety_name)
    safety_started = structured(
        client.call_tool(
            3010,
            "codex_worker_start",
            {
                "repo_path": str(repo),
                "name": safety_name,
                "workspace_mode": "isolated_write",
                "brief": "Inspect this disposable repo and report ready. Do not edit files and do not commit.",
            },
        )
    )
    recorder.check(
        "safety_worker_started",
        safety_started.get("accepted") is True,
        classification="direct_evidence",
        evidence="Reusable isolated safety worker started.",
    )
    wait_for_worker(client, safety_name, worker_timeout)
    safety_worktree = worker_worktree_for_name(runtime, safety_name)
    ensure_text_file(safety_worktree / "docs" / "safety-worker.md", "safety worker change\n")

    readonly_name = "Read Only Worker"
    recorder.add_worker(readonly_name)
    readonly_started = structured(
        client.call_tool(
            3020,
            "codex_worker_start",
            {
                "repo_path": str(repo),
                "name": readonly_name,
                "workspace_mode": "read_only",
                "brief": "Inspect this disposable repo and report its files. Do not edit.",
            },
        )
    )
    recorder.check(
        "safety_readonly_worker_started",
        readonly_started.get("accepted") is True,
        classification="direct_evidence",
        evidence="Read-only worker accepted.",
    )
    wait_for_worker(client, readonly_name, worker_timeout)
    readonly_integrate = structured(client.call_tool(3021, "codex_worker_integrate", {"worker": readonly_name}))
    recorder.check(
        "safety_readonly_integrate_refused",
        readonly_integrate.get("applied") is False
        and "only isolated writing workers" in str(readonly_integrate.get("note", "")).lower(),
        classification="direct_evidence",
        evidence="codex_worker_integrate refused a read-only worker.",
    )

    dirty_file = repo / "docs" / "local-dirty.md"
    dirty_file.write_text("local dirty change\n", encoding="utf-8")
    dirty_preview = structured(client.call_tool(3030, "codex_worker_inspect", {"worker": safety_name, "view": "integration_preview"}))
    recorder.check(
        "safety_dirty_base_refused",
        dirty_preview.get("can_apply") is False and dirty_preview.get("base_dirty") is True,
        classification="direct_evidence",
        evidence="Integration preview refused while the base checkout had local changes.",
    )
    dirty_file.unlink()

    blocked_path = safety_worktree / ".env"
    blocked_path.write_text("TOKEN=fixture\n", encoding="utf-8")
    blocked_preview = structured(client.call_tool(3040, "codex_worker_inspect", {"worker": safety_name, "view": "integration_preview"}))
    recorder.check(
        "safety_blocked_env_refused",
        blocked_preview.get("can_apply") is False and ".env" in (blocked_preview.get("blocked_files") or []),
        classification="direct_evidence",
        evidence="Integration preview refused a worker result containing a blocked .env path.",
    )
    blocked_path.unlink()

    binary_path = safety_worktree / "binary.dat"
    binary_path.write_bytes(b"\x00\x01worker-binary")
    binary_preview = structured(client.call_tool(3050, "codex_worker_inspect", {"worker": safety_name, "view": "integration_preview"}))
    recorder.check(
        "safety_untracked_binary_refused",
        binary_preview.get("can_apply") is False and "binary.dat" in (binary_preview.get("skipped_files") or []),
        classification="direct_evidence",
        evidence="Integration preview refused an untracked binary file that could not be safely patched.",
    )
    binary_path.unlink()

    readme = safety_worktree / "README.md"
    readme.write_text("# Worker-side README\n\nDisposable repo for real MCP worker validation.\n", encoding="utf-8")
    (repo / "README.md").write_text("# Base-side README\n\nDisposable repo for real MCP worker validation.\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Trial", "-c", "user.email=worker-trial@example.invalid", "commit", "-q", "-m", "move base readme"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    conflict_preview = structured(client.call_tool(3060, "codex_worker_inspect", {"worker": safety_name, "view": "integration_preview"}))
    recorder.check(
        "safety_conflict_preview_refused",
        conflict_preview.get("can_apply") is False and conflict_preview.get("apply_check") == "conflict",
        classification="direct_evidence",
        evidence="Integration preview reported a patch conflict after the base branch moved.",
    )

    cleanup = structured(client.call_tool(3070, "codex_worker_stop", {"worker": safety_name, "cleanup_workspace": True}))
    listed = structured(client.call_tool(3071, "codex_worker_list", {}))
    other = [item for item in listed.get("workers", []) if item.get("name") == active_name]
    recorder.check(
        "safety_cleanup_isolated_one_worker",
        cleanup.get("workspace_cleaned") is True
        and cleanup.get("workspace_available") is False
        and bool(other)
        and other[0].get("workspace_available") is True,
        classification="direct_evidence",
        evidence="Cleaning one isolated worker workspace preserved another worker workspace.",
    )


def wait_for_worker(client: McpClient, worker_name: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    msg_id = 200
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        remaining = max(1, int(min(10, deadline - time.monotonic())))
        last = structured(
            client.call_tool(
                msg_id,
                "codex_worker_inspect",
                {"worker": worker_name, "wait_seconds": remaining, "view": "report"},
            )
        )
        msg_id += 1
        if last.get("state") not in ACTIVE_STATES:
            return last
        time.sleep(0.25)
    raise TimeoutError(f"{worker_name} did not finish within {timeout_seconds} seconds; last state={last.get('state')}")


def structured(payload: dict[str, Any]) -> dict[str, Any]:
    if "error" in payload:
        raise TrialFailure(f"MCP error response: {payload['error']}")
    result = payload.get("result", {})
    content = result.get("structuredContent")
    if not isinstance(content, dict):
        raise TrialFailure(f"MCP response did not include structuredContent: {payload}")
    return content


def tool_names(payload: dict[str, Any]) -> set[str]:
    if "error" in payload:
        raise TrialFailure(f"MCP error response: {payload['error']}")
    tools = payload.get("result", {}).get("tools", [])
    return {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Worker Trial Repo\n\nDisposable repo for real MCP worker validation.\n", encoding="utf-8")
    (path / "AGENTS.md").write_text("Follow disposable trial rules. Do not commit changes.\n", encoding="utf-8")
    src = path / "src"
    src.mkdir()
    (src / "example.py").write_text(
        "def answer():\n"
        "    return \"original\"\n",
        encoding="utf-8",
    )
    docs = path / "docs"
    docs.mkdir()
    (docs / "notes.md").write_text("Initial notes.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "README.md", "AGENTS.md", "src/example.py", "docs/notes.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Worker Trial", "-c", "user.email=worker-trial@example.invalid", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
    )


def worker_worktree_for_name(runtime: Path, worker_name: str) -> Path:
    state_dir = runtime / "logs" / "jobs" / "state"
    for record in sorted(state_dir.glob("*.json")):
        data = json.loads(record.read_text(encoding="utf-8"))
        options = data.get("options") or {}
        if options.get("_worker_name") != worker_name:
            continue
        worktree = options.get("_worker_worktree_path") or data.get("worktree_path")
        if worktree:
            return Path(str(worktree)).expanduser().resolve()
    raise RuntimeError(f"No worker worktree found for {worker_name}")


def ensure_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_trial_config(repo: Path, runtime: Path, *, tool_mode: str, multi_client: bool = False) -> Path:
    runtime.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    config.setdefault("repositories", {})["default"] = str(repo)
    config.setdefault("repositories", {})["allowed"] = [str(repo)]
    config.setdefault("app", {})["tool_mode"] = tool_mode
    config.setdefault("auth", {})["tunnel_mode"] = "none"
    config.setdefault("server", {})["max_concurrent_jobs"] = 1
    if multi_client:
        # The trial intentionally needs two logical MCP clients to be different
        # coordination owners so it can exercise takeover refusal/transfer.
        # Production defaults still use token scope with server-owner fallback
        # when no token is present.
        config.setdefault("ownership", {})["scope"] = "transport_session"
    security = config.setdefault("security", {})
    if not security.get("blocked_globs"):
        security["blocked_globs"] = [
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            ".git",
            ".git/**",
            "**/.git/**",
            "**/*secret*",
        ]
    config["logging"] = {
        **(config.get("logging") or {}),
        "job_logs_dir": str(runtime / "logs" / "jobs"),
        "job_state_dir": str(runtime / "logs" / "jobs" / "state"),
    }
    config["workers"] = {
        **(config.get("workers") or {}),
        "worktree_root": str(runtime / "worker-worktrees"),
        "ignore_user_config": True,
    }
    path = runtime / "trial-config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def scan_job_stderr_for_connector_noise(runtime: Path) -> dict[str, Any]:
    log_dir = runtime / "logs" / "jobs"
    categories: set[str] = set()
    matches = 0
    scanned = 0
    for path in sorted(log_dir.glob("*_stderr.log")):
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, pattern in CONNECTOR_NOISE_PATTERNS.items():
            if pattern.search(text):
                categories.add(name)
                matches += 1
    return {
        "stderr_logs_scanned": scanned,
        "matches": matches,
        "matched_categories": sorted(categories),
    }


def wait_for_health(port: int, process: subprocess.Popen[str], output_tail: list[str], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        read_available_output(process, output_tail)
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


def git_output(args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10)
    return (result.stdout if result.returncode == 0 else result.stderr).strip()


def git_status(repo: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def git_commit_count(repo: Path) -> int:
    output = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return int(output)


def codex_version() -> str:
    codex = shutil.which("codex")
    if not codex:
        return "codex-not-found"
    result = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=10)
    return (result.stdout or result.stderr).strip() or f"codex-version-exit-{result.returncode}"


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def render_summary(report: dict[str, Any]) -> str:
    checks = report.get("checks") or []
    lines = [
        "# Real MCP Worker Trial",
        "",
        f"Status: `{report.get('status', 'unknown')}`",
        f"Classification: `{report.get('classification', 'unknown')}`",
        "",
        "## Environment",
        "",
        f"- Started: `{report.get('started_at', '')}`",
        f"- Updated: `{report.get('updated_at', '')}`",
        f"- Tool mode: `{report.get('tool_mode', '')}`",
        f"- ChatGPT/tunnel status: `{report.get('chatgpt_tunnel_status', '')}`",
        f"- Codex CLI: `{report.get('codex_version', '')}`",
        f"- Codex user config policy: `{report.get('codex_user_config_policy', '')}`",
        f"- Python: `{report.get('python_version', '')}`",
        f"- Server URL: `{report.get('server_url', '')}`",
        "",
        "## Server Command",
        "",
        "```bash",
        " ".join(str(part) for part in report.get("server_command", [])),
        "```",
        "",
        "## Checks",
        "",
        "| Check | Result | Classification | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for check in checks:
        result = "pass" if check.get("ok") else "fail"
        evidence = str(check.get("evidence") or "").replace("\n", " ")
        lines.append(f"| `{check.get('name')}` | {result} | `{check.get('classification')}` | {evidence} |")
    lines.extend(["", "## MCP Call Sequence", ""])
    for event in report.get("mcp_call_sequence") or []:
        tool = f" tool={event.get('tool')}" if event.get("tool") else ""
        client = f" client={event.get('client')}" if event.get("client") else ""
        lines.append(
            f"- #{event.get('index')} `{event.get('method')}`{client}{tool} id={event.get('mcp_id')} "
            f"ok={event.get('ok')} duration={event.get('duration_seconds')}s"
        )
    if report.get("error"):
        lines.extend(["", "## Error", "", str(report["error"])])
    lines.append("")
    return "\n".join(lines)


def print_result(report: dict[str, Any], json_only: bool) -> None:
    if json_only:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"Real MCP worker trial: {report.get('status')} ({report.get('classification')})")
    print(f"Evidence: {report.get('output_dir', '')}")


if __name__ == "__main__":
    raise SystemExit(main())
