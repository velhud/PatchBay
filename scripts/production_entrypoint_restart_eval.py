#!/usr/bin/env python3
"""Exercise a state-preserving restart through PatchBay's production CLIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TERMINAL_OPERATION_STATES = {"succeeded", "blocked", "failed", "cancelled"}
UPGRADE_AUTHORITATIVE_TABLES = (
    "principals",
    "hub_identity",
    "legacy_imports",
    "entity_records",
    "entity_control_index",
    "operations",
    "attempts",
    "events",
    "payload_metadata",
)
EDGE_UPGRADE_AUTHORITATIVE_TABLES = (
    "edge_state",
    "operation_intents",
    "operation_attempts",
    "result_outbox",
    "control_loop_health",
)


class ProductionRestartEvalError(RuntimeError):
    """Raised when one production-entrypoint phase cannot prove its contract."""


@dataclass(frozen=True)
class FixturePaths:
    root: Path
    patchbay_home: Path
    home: Path
    codex_home: Path
    config: Path
    repository: Path
    hub_database: Path
    edge_profile: Path
    edge_journal: Path
    evidence: Path
    logs: Path
    bin_dir: Path

    @classmethod
    def under(cls, root: str | Path) -> "FixturePaths":
        resolved = Path(root).expanduser().resolve(strict=False)
        patchbay_home = resolved / "patchbay-home"
        return cls(
            root=resolved,
            patchbay_home=patchbay_home,
            home=resolved / "home",
            codex_home=resolved / "codex-home",
            config=resolved / "config" / "patchbay.yaml",
            repository=resolved / "workspace" / "restart-repo",
            hub_database=patchbay_home / "runtime" / "hub" / "production-hub.sqlite3",
            edge_profile=patchbay_home / "runtime" / "hub" / "edge-profile.json",
            edge_journal=patchbay_home / "runtime" / "hub" / "production-edge.sqlite3",
            evidence=resolved / "evidence" / "production-entrypoint-restart.json",
            logs=resolved / "logs",
            bin_dir=resolved / "bin",
        )

    def public_mapping(self) -> dict[str, str]:
        return {
            "fixture_root": str(self.root),
            "patchbay_home": str(self.patchbay_home),
            "config": str(self.config),
            "repository": str(self.repository),
            "hub_database": str(self.hub_database),
            "edge_profile": str(self.edge_profile),
            "edge_journal": str(self.edge_journal),
            "evidence": str(self.evidence),
        }


@dataclass
class HubProcess:
    process: subprocess.Popen[str]
    stdout_handle: Any
    stderr_handle: Any
    stdout_path: Path
    stderr_path: Path


@dataclass
class EdgeProcess:
    process: subprocess.Popen[str]
    stdout_handle: Any
    stderr_handle: Any
    stdout_path: Path
    stderr_path: Path


class McpClient:
    """Minimal synchronous Streamable HTTP client for the live eval."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session_id = ""
        self.request_id = 0

    def initialize(self) -> dict[str, Any]:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "patchbay-production-entrypoint-restart-eval",
                    "version": "1",
                },
            },
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = self._rpc(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
        )
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise ProductionRestartEvalError(f"{name} returned no structuredContent")
        return dict(structured)

    def _rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            f"{self.base_url}/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": method,
                    "params": dict(params),
                }
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if not self.session_id:
                    self.session_id = str(response.headers.get("Mcp-Session-Id") or "")
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ProductionRestartEvalError(
                f"MCP {method} returned HTTP {error.code}"
            ) from error
        if not isinstance(payload, Mapping):
            raise ProductionRestartEvalError(f"MCP {method} returned no object")
        if payload.get("error"):
            raise ProductionRestartEvalError(f"MCP {method} failed: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ProductionRestartEvalError(f"MCP {method} returned no result")
        return dict(result)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_is_within(path: str | Path, root: Path) -> bool:
    try:
        Path(path).expanduser().resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_stub_codex(paths: FixturePaths) -> None:
    paths.bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = paths.bin_dir / "codex"
    target.write_text(
        """#!/usr/bin/env python3
import json
import sys

sys.stdin.read()
result = {
    "summary": "Disposable restart-eval worker completed.",
    "detailed_report": "The temp-only Codex protocol stub completed one read-only production worker turn.",
    "evidence": ["A structured terminal result was emitted through the normal JobExecutor subprocess boundary."],
    "files_changed": [],
    "commands_run": [],
    "tests_run": [],
    "notes": "No real Codex account, credential, or external service was used.",
    "risks": [],
    "open_questions": [],
    "next_steps": [],
}
print(json.dumps({"type": "thread.started", "thread_id": "session-production-restart-eval"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}}), flush=True)
print(json.dumps({"type": "result", "data": result}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
""",
        encoding="utf-8",
    )
    target.chmod(0o700)


def _initialize_repository(paths: FixturePaths) -> None:
    paths.repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    (paths.repository / "README.md").write_text(
        "# Disposable PatchBay restart fixture\n",
        encoding="utf-8",
    )
    commands = (
        ("git", "init", "-q", str(paths.repository)),
        ("git", "-C", str(paths.repository), "config", "user.name", "PatchBay Eval"),
        (
            "git",
            "-C",
            str(paths.repository),
            "config",
            "user.email",
            "patchbay-eval@example.invalid",
        ),
        ("git", "-C", str(paths.repository), "add", "README.md"),
        (
            "git",
            "-C",
            str(paths.repository),
            "commit",
            "-q",
            "-m",
            "Initialize restart fixture",
        ),
    )
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=paths.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise ProductionRestartEvalError(
                "Could not initialize the disposable git repository"
            )


def _write_config(paths: FixturePaths, *, port: int) -> None:
    config = {
        "server": {
            "host": "127.0.0.1",
            "port": port,
            "max_concurrent_jobs": 2,
            "queue_enabled": True,
            "job_timeout_seconds": 20,
            "codex_session_start_timeout_seconds": 5,
            "codex_post_completion_exit_grace_seconds": 0.1,
            "codex_post_completion_cleanup_timeout_seconds": 1,
            "stale_running_job_grace_seconds": 1,
            "max_request_bytes": 2_000_000,
            "enable_cors": False,
        },
        "app": {"tool_mode": "worker", "tool_cards": False},
        "auth": {
            "enabled": False,
            "require_for_non_loopback": True,
            "require_for_tunnel": True,
            "tunnel_mode": "none",
        },
        "ownership": {"scope": "token"},
        "hub": {
            "control_plane": "v2",
            "state_db": str(paths.hub_database),
            "heartbeat_stale_seconds": 30,
            "semantic_wait_seconds": 0.05,
            "recovery_dispatch_interval_seconds": 0.1,
            "recovery_dispatch_batch_size": 20,
            "routing": {
                "enabled": False,
                "min_disk_free_bytes": 1,
                "allow_queue_when_full": False,
            },
            "edge": {
                "journal_file": str(paths.edge_journal),
                "max_concurrent_commands": 1,
                "resource_overrides": {
                    "disk_free_bytes": 10_000_000_000,
                    "disk_total_bytes": 20_000_000_000,
                    "disk_used_percent": 50,
                    "disk_source": "restart-eval-fixture",
                },
            },
        },
        "repositories": {
            "projects_base": str(paths.repository.parent),
            "default": str(paths.repository),
            "allowed": [str(paths.repository)],
            "discovery_roots": [],
        },
        "security": {
            "require_git_repo": True,
            "default_sandbox": "read-only",
            "allow_dangerously_bypass": False,
            "allowed_env_keys": [
                "PATH",
                "HOME",
                "USER",
                "SHELL",
                "TMPDIR",
                "CODEX_HOME",
                "PYTHONDONTWRITEBYTECODE",
            ],
            "blocked_globs": [],
        },
        "workers": {
            "worktree_root": str(paths.root / "worktrees"),
            "ignore_user_config": True,
            "heartbeat_fresh_seconds": 30,
            "heartbeat_quiet_seconds": 60,
        },
        "power_tools": {
            "direct_write": False,
            "bash_mode": "off",
            "codex_session_read": False,
            "codex_home": str(paths.codex_home),
        },
        "artifacts": {"enabled": True, "root": str(paths.root / "artifacts")},
        "pro_requests": {
            "root": str(paths.root / "pro-requests"),
            "mirror_enabled": False,
        },
        "logging": {
            "level": "WARNING",
            "audit_file": str(paths.logs / "audit.log"),
            "job_logs_dir": str(paths.logs / "jobs"),
            "job_state_dir": str(paths.logs / "jobs" / "state"),
            "private_evidence_dir": str(paths.logs / "private-evidence"),
            "worktrees_dir": str(paths.root / "worktrees" / "jobs"),
            "access_log": False,
            "private_evidence_log": False,
            "store_job_prompts": False,
            "store_mcp_transcripts": False,
            "write_raw_job_logs": False,
        },
    }
    paths.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.config.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def _enable_continuity_guards(paths: FixturePaths) -> dict[str, str]:
    """Pin the fixture to the exact Hub and Edge state it already created."""

    config = yaml.safe_load(paths.config.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ProductionRestartEvalError("Fixture config is not a mapping")
    hub = config.setdefault("hub", {})
    edge = hub.setdefault("edge", {})
    hub_snapshot = _read_hub_snapshot(paths.hub_database)
    edge_snapshot = _read_edge_snapshot(paths.edge_journal)
    hub["require_existing_state"] = True
    hub["expected_hub_id"] = str(hub_snapshot["hub_id"])
    edge["require_existing_journal"] = True
    paths.config.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )
    return {
        "hub_id": str(hub_snapshot["hub_id"]),
        "edge_generation": str(edge_snapshot["edge_generation"]),
    }


def _prove_continuity_guard_refusals(
    paths: FixturePaths,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Require both production entrypoints to reject missing durable state."""

    source = yaml.safe_load(paths.config.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise ProductionRestartEvalError("Fixture config is not a mapping")

    hub_config = json.loads(json.dumps(source))
    hub_config["hub"]["state_db"] = str(paths.root / "missing" / "hub.sqlite3")
    missing_hub_config = paths.config.with_name("missing-hub.yaml")
    missing_hub_config.write_text(
        yaml.safe_dump(hub_config, sort_keys=False),
        encoding="utf-8",
    )
    hub_refusal = _run_patchbay_cli_expect_failure(
        paths,
        environment,
        "hub-continuity-guard-refusal",
        [
            "hub",
            "start",
            "--config",
            str(missing_hub_config),
            "--host",
            "127.0.0.1",
            "--port",
            str(_free_port()),
        ],
        expected_text="Configured Hub V2 state database is missing",
    )

    edge_config = json.loads(json.dumps(source))
    edge_config["hub"]["edge"]["journal_file"] = str(
        paths.root / "missing" / "edge.sqlite3"
    )
    missing_edge_config = paths.config.with_name("missing-edge.yaml")
    missing_edge_config.write_text(
        yaml.safe_dump(edge_config, sort_keys=False),
        encoding="utf-8",
    )
    edge_refusal = _run_patchbay_cli_expect_failure(
        paths,
        environment,
        "edge-continuity-guard-refusal",
        ["edge", "run-once", "--config", str(missing_edge_config), "--json"],
        expected_text="Configured Edge journal is missing for generation",
    )
    return {"hub": hub_refusal, "edge": edge_refusal}


def _subprocess_environment(paths: FixturePaths) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PATCHBAY_HOME": str(paths.patchbay_home),
            "PATCHBAY_CONFIG": str(paths.config),
            "PATCHBAY_TUNNEL_MODE": "none",
            "HOME": str(paths.home),
            "CODEX_HOME": str(paths.codex_home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if part
            ),
            "PATH": os.pathsep.join(
                part for part in (str(paths.bin_dir), env.get("PATH", "")) if part
            ),
        }
    )
    return env


def _environment_fingerprint(environment: Mapping[str, str]) -> str:
    relevant = {
        key: str(environment.get(key) or "")
        for key in (
            "PATCHBAY_HOME",
            "PATCHBAY_CONFIG",
            "PATCHBAY_TUNNEL_MODE",
            "HOME",
            "CODEX_HOME",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONPATH",
            "PATH",
        )
    }
    encoded = json.dumps(
        relevant,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_patchbay_cli(
    paths: FixturePaths,
    env: Mapping[str, str],
    label: str,
    arguments: list[str],
    *,
    timeout: float = 45,
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "patchbay.cli", *arguments],
        cwd=ROOT,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    paths.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    stdout_log = completed.stdout
    if label == "hub-enrollment-code" and completed.returncode == 0:
        stdout_log = json.dumps({"code": "<redacted-after-use>"}) + "\n"
    (paths.logs / f"{label}.stdout.log").write_text(stdout_log, encoding="utf-8")
    (paths.logs / f"{label}.stderr.log").write_text(
        completed.stderr,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise ProductionRestartEvalError(
            f"Production CLI phase {label!r} failed; inspect its temp log"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ProductionRestartEvalError(
            f"Production CLI phase {label!r} returned invalid JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise ProductionRestartEvalError(
            f"Production CLI phase {label!r} returned no JSON object"
        )
    return dict(payload)


def _run_patchbay_cli_expect_failure(
    paths: FixturePaths,
    env: Mapping[str, str],
    label: str,
    arguments: list[str],
    *,
    expected_text: str,
    timeout: float = 20,
) -> dict[str, Any]:
    """Run one production CLI command and require a specific fail-closed path."""

    completed = subprocess.run(
        [sys.executable, "-m", "patchbay.cli", *arguments],
        cwd=ROOT,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    paths.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    (paths.logs / f"{label}.stdout.log").write_text(
        completed.stdout,
        encoding="utf-8",
    )
    (paths.logs / f"{label}.stderr.log").write_text(
        completed.stderr,
        encoding="utf-8",
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or expected_text not in combined:
        raise ProductionRestartEvalError(
            f"Production CLI phase {label!r} did not fail closed as expected"
        )
    return {
        "return_code": completed.returncode,
        "refused": True,
        "reason": expected_text,
    }


def _start_hub(
    paths: FixturePaths,
    env: Mapping[str, str],
    *,
    port: int,
    phase: str,
) -> HubProcess:
    paths.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    stdout_path = paths.logs / f"hub-{phase}.stdout.log"
    stderr_path = paths.logs / f"hub-{phase}.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "patchbay.cli",
            "hub",
            "start",
            "--config",
            str(paths.config),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=dict(env),
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        start_new_session=True,
    )
    hub = HubProcess(
        process=process,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    try:
        _wait_for_hub(f"http://127.0.0.1:{port}", hub)
    except Exception:
        _force_stop_hub(hub)
        raise
    return hub


def _start_edge_service(
    paths: FixturePaths,
    env: Mapping[str, str],
    *,
    phase: str,
) -> EdgeProcess:
    """Start the real long-running Edge production CLI."""

    paths.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    stdout_path = paths.logs / f"edge-{phase}.stdout.log"
    stderr_path = paths.logs / f"edge-{phase}.stderr.log"
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "patchbay.cli",
            "edge",
            "start",
            "--config",
            str(paths.config),
            "--interval-seconds",
            "0.05",
        ],
        cwd=ROOT,
        env=dict(env),
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        start_new_session=True,
    )
    edge = EdgeProcess(
        process=process,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    time.sleep(0.1)
    if process.poll() is not None:
        stdout_handle.close()
        stderr_handle.close()
        raise ProductionRestartEvalError(
            f"Production Edge exited during {phase!r} startup"
        )
    return edge


def _stop_edge_service(edge: EdgeProcess, *, timeout: float = 20) -> dict[str, Any]:
    """Stop the systemd-equivalent Edge process and require bounded exit."""

    if edge.process.poll() is None:
        edge.process.send_signal(signal.SIGTERM)
    try:
        return_code = edge.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _force_stop_edge(edge)
        raise ProductionRestartEvalError(
            "Production Edge did not stop inside the bounded shutdown timeout"
        ) from error
    finally:
        edge.stdout_handle.close()
        edge.stderr_handle.close()
    if return_code not in {0, -signal.SIGTERM}:
        raise ProductionRestartEvalError(
            f"Production Edge exited unexpectedly with code {return_code}"
        )
    return {
        "return_code": return_code,
        "signal": "SIGTERM" if return_code == -signal.SIGTERM else "",
        "bounded": True,
    }


def _force_stop_edge(edge: EdgeProcess) -> None:
    if edge.process.poll() is None:
        try:
            os.killpg(edge.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            edge.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    edge.stdout_handle.close()
    edge.stderr_handle.close()


def _prove_hub_startup_refusal(
    paths: FixturePaths,
    env: Mapping[str, str],
    *,
    port: int,
) -> dict[str, Any]:
    """Run the production Hub CLI and require the pre-migration fail-closed path."""

    paths.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    stdout_path = paths.logs / "hub-upgrade-refused.stdout.log"
    stderr_path = paths.logs / "hub-upgrade-refused.stderr.log"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "patchbay.cli",
            "hub",
            "start",
            "--config",
            str(paths.config),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    marker_refusal = (
        completed.returncode != 0
        and "migration is blocked until a validated pre-migration backup marker"
        in completed.stderr
    )
    if not marker_refusal:
        raise ProductionRestartEvalError(
            "Production Hub did not refuse the older schema for a missing "
            "pre-migration backup marker"
        )
    return {
        "return_code": completed.returncode,
        "refused": True,
        "reason": "missing_validated_pre_migration_backup_marker",
    }


def _wait_for_hub(base_url: str, hub: HubProcess, *, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if hub.process.poll() is not None:
            raise ProductionRestartEvalError(
                "Production Hub exited before its loopback health endpoint became ready"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if (
                isinstance(payload, Mapping)
                and payload.get("transport") == "streamable-http"
            ):
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise ProductionRestartEvalError(
        "Production Hub did not become ready before the bounded startup timeout"
    )


def _stop_hub(hub: HubProcess, *, timeout: float = 20) -> dict[str, Any]:
    if hub.process.poll() is None:
        hub.process.send_signal(signal.SIGTERM)
    try:
        return_code = hub.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _force_stop_hub(hub)
        raise ProductionRestartEvalError(
            "Production Hub did not stop inside the graceful shutdown timeout"
        ) from error
    finally:
        hub.stdout_handle.close()
        hub.stderr_handle.close()
    stderr = hub.stderr_path.read_text(encoding="utf-8", errors="replace")
    graceful_markers = {
        "application_shutdown_complete": "Application shutdown complete" in stderr,
        "server_process_finished": "Finished server process" in stderr,
    }
    clean = return_code in {0, -signal.SIGTERM} and all(graceful_markers.values())
    if not clean:
        raise ProductionRestartEvalError(
            "Production Hub did not emit complete graceful-shutdown evidence"
        )
    return {
        "return_code": return_code,
        "signal": "SIGTERM" if return_code == -signal.SIGTERM else "",
        "graceful_markers": graceful_markers,
        "clean": clean,
    }


def _force_stop_hub(hub: HubProcess) -> None:
    if hub.process.poll() is None:
        try:
            os.killpg(hub.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            hub.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if not hub.stdout_handle.closed:
        hub.stdout_handle.close()
    if not hub.stderr_handle.closed:
        hub.stderr_handle.close()


def _wait_for(
    probe: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    phase: str,
    timeout: float = 15,
    poll_interval: float = 0.05,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if predicate(last):
            return last
        time.sleep(poll_interval)
    raise ProductionRestartEvalError(
        f"Timed out waiting for production phase {phase!r}"
    )


def _wait_for_edge_online(
    client: McpClient,
    edge: EdgeProcess,
    *,
    machine_id: str,
    started_after: float,
    phase: str,
) -> dict[str, Any]:
    """Require a fresh heartbeat from this exact Edge process, not stale state."""

    def probe() -> dict[str, Any]:
        if edge.process.poll() is not None:
            raise ProductionRestartEvalError(
                f"Production Edge exited while waiting for {phase!r}"
            )
        return client.call("patchbay_fleet_status", {})

    return _wait_for(
        probe,
        lambda value: any(
            str(machine.get("machine_id") or "") == machine_id
            and str(machine.get("status") or "") == "online"
            and float(machine.get("last_seen_at") or 0) >= started_after
            for machine in value.get("result", {}).get("machines", [])
        ),
        phase=phase,
        timeout=30,
    )


def _json_record(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    return dict(value) if isinstance(value, Mapping) else {}


def _public_entity_record(
    entity_type: str, record: Mapping[str, Any]
) -> dict[str, Any]:
    fields = {
        "hub.machine": (
            "machine_id",
            "edge_generation",
            "status",
            "projection_revision",
        ),
        "hub.work_group": (
            "work_group_id",
            "pinned_machine_id",
            "pinned_edge_generation",
            "status",
        ),
        "hub.fleet_worker": (
            "fleet_worker_ref",
            "machine_id",
            "edge_generation",
            "edge_worker_id",
            "work_group_id",
            "lane_id",
        ),
        "hub.worker_projection": (
            "fleet_worker_ref",
            "machine_id",
            "edge_generation",
            "edge_worker_id",
            "work_group_id",
            "lane_id",
            "name",
            "turn_state",
            "liveness",
            "projection_revision",
            "edge_projection_revision",
        ),
    }[entity_type]
    value = {field: record[field] for field in fields if field in record}
    if entity_type == "hub.work_group":
        readiness = record.get("readiness")
        if isinstance(readiness, Mapping):
            value["readiness_status"] = str(readiness.get("status") or "")
    return value


def _read_hub_snapshot(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        metadata = connection.execute(
            "SELECT schema_version, v2_mutation_count FROM schema_metadata WHERE singleton = 1"
        ).fetchone()
        identity = connection.execute(
            "SELECT hub_id, principal_ref FROM hub_identity WHERE singleton = 1"
        ).fetchone()
        retained_entity_types = {
            "hub.machine",
            "hub.work_group",
            "hub.fleet_worker",
            "hub.worker_projection",
        }
        entities: dict[str, dict[str, dict[str, Any]]] = {}
        for row in connection.execute(
            "SELECT entity_type, entity_id, revision, record_json FROM entity_records ORDER BY entity_type, entity_id"
        ):
            entity_type = str(row["entity_type"])
            if entity_type not in retained_entity_types:
                continue
            record = _json_record(str(row["record_json"]))
            entities.setdefault(entity_type, {})[str(row["entity_id"])] = {
                "revision": int(row["revision"]),
                "record": _public_entity_record(entity_type, record),
            }
        operations = {
            str(row["operation_id"]): {
                "tool": str(row["tool"]),
                "state": str(row["state"]),
                "revision": int(row["revision"]),
            }
            for row in connection.execute(
                "SELECT operation_id, tool, state, revision FROM operations ORDER BY operation_id"
            )
        }
        max_event_revision = int(
            connection.execute(
                "SELECT COALESCE(MAX(event_revision), 0) FROM events"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    if metadata is None or identity is None:
        raise ProductionRestartEvalError(
            "Hub snapshot is missing schema or identity state"
        )
    return {
        "integrity_check": integrity,
        "schema_version": int(metadata["schema_version"]),
        "mutation_count": int(metadata["v2_mutation_count"]),
        "hub_id": str(identity["hub_id"]),
        "principal_ref": str(identity["principal_ref"]),
        "max_event_revision": max_event_revision,
        "entities": entities,
        "operations": operations,
    }


def _stable_rows_proof(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, Any]:
    columns = [
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    ]
    if not columns:
        raise ProductionRestartEvalError(
            f"Upgrade fixture is missing authoritative Hub table {table!r}"
        )
    rows = [
        [row[column] for column in columns]
        for row in connection.execute(f'SELECT * FROM "{table}"')
    ]
    encoded_rows = sorted(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for row in rows
    )
    encoded = json.dumps(
        {"columns": columns, "rows": encoded_rows},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return {
        "count": len(rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _read_exact_upgrade_state(path: Path) -> dict[str, Any]:
    """Read exact logical Hub state without opening a migration-capable wrapper."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        metadata = connection.execute(
            "SELECT schema_version, v2_mutation_count FROM schema_metadata WHERE singleton = 1"
        ).fetchone()
        identity = connection.execute(
            "SELECT hub_id, principal_ref FROM hub_identity WHERE singleton = 1"
        ).fetchone()
        tables = {
            table: _stable_rows_proof(connection, table)
            for table in UPGRADE_AUTHORITATIVE_TABLES
        }
        revisions = {
            "principals": {
                str(row["principal_ref"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT principal_ref, revision FROM principals ORDER BY principal_ref"
                )
            },
            "entities": {
                f"{row['entity_type']}:{row['entity_id']}": int(row["revision"])
                for row in connection.execute(
                    "SELECT entity_type, entity_id, revision FROM entity_records "
                    "ORDER BY entity_type, entity_id"
                )
            },
            "operations": {
                str(row["operation_id"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT operation_id, revision FROM operations ORDER BY operation_id"
                )
            },
            "attempts": {
                str(row["attempt_id"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT attempt_id, revision FROM attempts ORDER BY attempt_id"
                )
            },
            "payloads": {
                str(row["payload_id"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT payload_id, revision FROM payload_metadata ORDER BY payload_id"
                )
            },
            "events": [
                {
                    "event_revision": int(row["event_revision"]),
                    "event_id": str(row["event_id"]),
                    "entity_revision": (
                        int(row["entity_revision"])
                        if row["entity_revision"] is not None
                        else None
                    ),
                }
                for row in connection.execute(
                    "SELECT event_revision, event_id, entity_revision FROM events "
                    "ORDER BY event_revision"
                )
            ],
        }
        authoritative_encoded = json.dumps(
            {"tables": tables, "revisions": revisions},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        operation_group_index_exists = bool(
            connection.execute(
                "SELECT 1 FROM sqlite_schema "
                "WHERE type = 'table' AND name = 'operation_group_index'"
            ).fetchone()
        )
        operation_group_index_rows = (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM operation_group_index"
                ).fetchone()[0]
            )
            if operation_group_index_exists
            else 0
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    if metadata is None or identity is None:
        raise ProductionRestartEvalError(
            "Upgrade fixture is missing Hub schema or identity state"
        )
    return {
        "integrity_check": integrity,
        "schema_version": int(metadata["schema_version"]),
        "user_version": user_version,
        "mutation_count": int(metadata["v2_mutation_count"]),
        "hub_id": str(identity["hub_id"]),
        "principal_ref": str(identity["principal_ref"]),
        "authoritative_state_sha256": hashlib.sha256(authoritative_encoded).hexdigest(),
        "tables": tables,
        "revisions": revisions,
        "operation_group_index": {
            "exists": operation_group_index_exists,
            "rows": operation_group_index_rows,
        },
    }


def _authoritative_upgrade_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hub_id": state.get("hub_id"),
        "principal_ref": state.get("principal_ref"),
        "mutation_count": state.get("mutation_count"),
        "authoritative_state_sha256": state.get("authoritative_state_sha256"),
        "tables": state.get("tables"),
        "revisions": state.get("revisions"),
    }


def _read_exact_edge_upgrade_state(path: Path) -> dict[str, Any]:
    """Read schema-independent logical Edge state without opening its wrapper."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        metadata = connection.execute(
            "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
        ).fetchone()
        state = connection.execute(
            "SELECT edge_generation, projection_revision FROM edge_state "
            "WHERE singleton = 1"
        ).fetchone()
        tables: dict[str, dict[str, Any]] = {}
        for table in EDGE_UPGRADE_AUTHORITATIVE_TABLES:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if not (table == "result_outbox" and str(row[1]) == "hub_confirmed_at")
            ]
            if not columns:
                raise ProductionRestartEvalError(
                    f"Upgrade fixture is missing authoritative Edge table {table!r}"
                )
            projection = ", ".join(f'"{column}"' for column in columns)
            rows = [
                [row[column] for column in columns]
                for row in connection.execute(
                    f'SELECT {projection} FROM "{table}"'
                )
            ]
            encoded_rows = sorted(
                json.dumps(
                    row,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                for row in rows
            )
            encoded = json.dumps(
                {"columns": columns, "rows": encoded_rows},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            tables[table] = {
                "count": len(rows),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        revisions = {
            "attempts": {
                str(row["attempt_id"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT attempt_id, revision FROM operation_attempts "
                    "ORDER BY attempt_id"
                )
            },
            "projection_revision": int(state["projection_revision"]) if state else -1,
        }
        outbox_receipts = {
            str(row["receipt_id"]): {
                "attempt_id": str(row["attempt_id"]),
                "result_hash": str(row["result_hash"]),
                "acknowledged": row["acknowledged_at"] is not None,
            }
            for row in connection.execute(
                "SELECT receipt_id, attempt_id, result_hash, acknowledged_at "
                "FROM result_outbox ORDER BY receipt_id"
            )
        }
        confirmation_column_exists = "hub_confirmed_at" in {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("result_outbox")')
        }
        confirmation_index_exists = bool(
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'index' "
                "AND name = 'result_outbox_confirmation_pending_idx'"
            ).fetchone()
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        authoritative_encoded = json.dumps(
            {"tables": tables, "revisions": revisions},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    finally:
        connection.close()
    if metadata is None or state is None:
        raise ProductionRestartEvalError(
            "Upgrade fixture is missing Edge schema or generation state"
        )
    return {
        "integrity_check": integrity,
        "schema_version": int(metadata["schema_version"]),
        "user_version": user_version,
        "edge_generation": str(state["edge_generation"]),
        "projection_revision": int(state["projection_revision"]),
        "authoritative_state_sha256": hashlib.sha256(
            authoritative_encoded
        ).hexdigest(),
        "tables": tables,
        "revisions": revisions,
        "outbox_receipts": outbox_receipts,
        "confirmation_column_exists": confirmation_column_exists,
        "confirmation_index_exists": confirmation_index_exists,
    }


def _authoritative_edge_upgrade_state(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "edge_generation": state.get("edge_generation"),
        "projection_revision": state.get("projection_revision"),
        "authoritative_state_sha256": state.get("authoritative_state_sha256"),
        "tables": state.get("tables"),
        "revisions": state.get("revisions"),
    }


def _edge_durable_payload_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude expected heartbeat/loop timestamps while proving no work loss."""

    tables = state.get("tables") if isinstance(state.get("tables"), Mapping) else {}
    return {
        "edge_generation": state.get("edge_generation"),
        "operation_intents": tables.get("operation_intents"),
        "operation_attempts": tables.get("operation_attempts"),
        "attempt_revisions": (
            state.get("revisions", {}).get("attempts", {})
            if isinstance(state.get("revisions"), Mapping)
            else {}
        ),
    }


def _edge_payload_survived_service_transition(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Allow only Hub-confirmed outbox retirement across an Edge service turn."""

    if _edge_durable_payload_state(before) != _edge_durable_payload_state(after):
        return False
    older = (
        before.get("outbox_receipts")
        if isinstance(before.get("outbox_receipts"), Mapping)
        else {}
    )
    newer = (
        after.get("outbox_receipts")
        if isinstance(after.get("outbox_receipts"), Mapping)
        else {}
    )
    if not set(newer).issubset(older):
        return False
    if any(newer[key] != older[key] for key in newer):
        return False
    return all(
        bool(older[receipt_id].get("acknowledged"))
        for receipt_id in set(older).difference(newer)
        if isinstance(older[receipt_id], Mapping)
    )


def _hub_restart_state_is_monotonic(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Allow expected heartbeats while requiring every durable identity to survive."""

    if (
        before.get("hub_id") != after.get("hub_id")
        or before.get("principal_ref") != after.get("principal_ref")
    ):
        return False
    before_revisions = (
        before.get("revisions")
        if isinstance(before.get("revisions"), Mapping)
        else {}
    )
    after_revisions = (
        after.get("revisions")
        if isinstance(after.get("revisions"), Mapping)
        else {}
    )
    for category in ("principals", "entities", "operations", "attempts", "payloads"):
        older = (
            before_revisions.get(category)
            if isinstance(before_revisions.get(category), Mapping)
            else {}
        )
        newer = (
            after_revisions.get(category)
            if isinstance(after_revisions.get(category), Mapping)
            else {}
        )
        if set(older) != set(newer):
            return False
        if any(int(newer[key]) < int(value) for key, value in older.items()):
            return False
    older_events = list(before_revisions.get("events") or [])
    newer_events = list(after_revisions.get("events") or [])
    return len(newer_events) >= len(older_events) and newer_events[: len(older_events)] == older_events


def _read_edge_snapshot(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        metadata = connection.execute(
            "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
        ).fetchone()
        state = connection.execute(
            "SELECT edge_generation, projection_revision FROM edge_state WHERE singleton = 1"
        ).fetchone()
        intents = {
            str(row["operation_id"]): {
                "action": str(row["action"]),
                "edge_generation": str(row["edge_generation"]),
            }
            for row in connection.execute(
                "SELECT operation_id, action, edge_generation FROM operation_intents ORDER BY operation_id"
            )
        }
        attempts = {
            str(row["attempt_id"]): {
                "operation_id": str(row["operation_id"]),
                "edge_generation": str(row["edge_generation"]),
                "state": str(row["state"]),
                "revision": int(row["revision"]),
            }
            for row in connection.execute(
                "SELECT attempt_id, operation_id, edge_generation, state, revision FROM operation_attempts ORDER BY attempt_id"
            )
        }
        outbox = {
            str(row["receipt_id"]): {
                "operation_id": str(row["operation_id"]),
                "attempt_id": str(row["attempt_id"]),
                "edge_generation": str(row["edge_generation"]),
            }
            for row in connection.execute(
                "SELECT receipt_id, operation_id, attempt_id, edge_generation FROM result_outbox ORDER BY receipt_id"
            )
        }
    finally:
        connection.close()
    if metadata is None or state is None:
        raise ProductionRestartEvalError(
            "Edge snapshot is missing schema or generation state"
        )
    return {
        "integrity_check": integrity,
        "schema_version": int(metadata["schema_version"]),
        "edge_generation": str(state["edge_generation"]),
        "projection_revision": int(state["projection_revision"]),
        "intents": intents,
        "attempts": attempts,
        "outbox": outbox,
    }


def _entity_summary(
    hub: Mapping[str, Any], entity_type: str
) -> dict[str, dict[str, Any]]:
    entities = hub.get("entities") if isinstance(hub.get("entities"), Mapping) else {}
    values = (
        entities.get(entity_type)
        if isinstance(entities.get(entity_type), Mapping)
        else {}
    )
    return {
        str(key): dict(value)
        for key, value in values.items()
        if isinstance(value, Mapping)
    }


def _generation_set(snapshot: Mapping[str, Any]) -> list[str]:
    values = {
        str(snapshot.get("profile", {}).get("edge_generation") or ""),
        str(snapshot.get("edge", {}).get("edge_generation") or ""),
    }
    hub = snapshot.get("hub") if isinstance(snapshot.get("hub"), Mapping) else {}
    for entity_type in ("hub.machine", "hub.fleet_worker", "hub.worker_projection"):
        for entity in _entity_summary(hub, entity_type).values():
            record = (
                entity.get("record")
                if isinstance(entity.get("record"), Mapping)
                else {}
            )
            values.add(str(record.get("edge_generation") or ""))
    return sorted(value for value in values if value)


def _snapshot(paths: FixturePaths) -> dict[str, Any]:
    profile = json.loads(paths.edge_profile.read_text(encoding="utf-8"))
    if not isinstance(profile, Mapping):
        raise ProductionRestartEvalError("Edge profile fixture is not a JSON object")
    database_files = sorted(
        str(path.resolve(strict=False)) for path in paths.root.rglob("*.sqlite3")
    )
    snapshot = {
        "database_files": database_files,
        "config_sha256": _sha256(paths.config),
        "profile": {
            "path": str(paths.edge_profile),
            "sha256": _sha256(paths.edge_profile),
            "machine_id": str(profile.get("machine_id") or ""),
            "edge_generation": str(profile.get("edge_generation") or ""),
            "hub_url": str(profile.get("hub_url") or ""),
        },
        "hub": _read_hub_snapshot(paths.hub_database),
        "edge": _read_edge_snapshot(paths.edge_journal),
    }
    snapshot["generations"] = _generation_set(snapshot)
    return snapshot


def _identity_keys(hub: Mapping[str, Any], entity_type: str) -> list[str]:
    return sorted(_entity_summary(hub, entity_type))


def _revision_map(hub: Mapping[str, Any], entity_type: str) -> dict[str, int]:
    return {
        key: int(value.get("revision") or 0)
        for key, value in _entity_summary(hub, entity_type).items()
    }


def _revisions_monotonic(before: Mapping[str, int], after: Mapping[str, int]) -> bool:
    return set(before) == set(after) and all(
        int(after[key]) >= int(value) for key, value in before.items()
    )


def compare_restart_snapshots(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare stable identities exactly and revision-bearing records monotonically."""

    before_hub = before.get("hub") if isinstance(before.get("hub"), Mapping) else {}
    after_hub = after.get("hub") if isinstance(after.get("hub"), Mapping) else {}
    before_edge = before.get("edge") if isinstance(before.get("edge"), Mapping) else {}
    after_edge = after.get("edge") if isinstance(after.get("edge"), Mapping) else {}
    entity_types = (
        "hub.machine",
        "hub.work_group",
        "hub.fleet_worker",
        "hub.worker_projection",
    )
    identity_checks = {
        entity_type: _identity_keys(before_hub, entity_type)
        == _identity_keys(after_hub, entity_type)
        for entity_type in entity_types
    }
    revision_checks = {
        entity_type: _revisions_monotonic(
            _revision_map(before_hub, entity_type),
            _revision_map(after_hub, entity_type),
        )
        for entity_type in entity_types
    }
    before_operations = (
        before_hub.get("operations")
        if isinstance(before_hub.get("operations"), Mapping)
        else {}
    )
    after_operations = (
        after_hub.get("operations")
        if isinstance(after_hub.get("operations"), Mapping)
        else {}
    )
    operation_ids_stable = set(before_operations) == set(after_operations)
    operation_revisions_monotonic = operation_ids_stable and all(
        int(after_operations[key].get("revision") or 0)
        >= int(before_operations[key].get("revision") or 0)
        for key in before_operations
    )
    edge_identity_checks = {
        name: set(before_edge.get(name, {})) == set(after_edge.get(name, {}))
        for name in ("intents", "attempts", "outbox")
    }
    before_attempts = before_edge.get("attempts", {})
    after_attempts = after_edge.get("attempts", {})
    attempt_revisions_monotonic = edge_identity_checks["attempts"] and all(
        int(after_attempts[key].get("revision") or 0)
        >= int(before_attempts[key].get("revision") or 0)
        for key in before_attempts
    )
    checks = {
        "database_files_stable": before.get("database_files")
        == after.get("database_files"),
        "config_stable": before.get("config_sha256") == after.get("config_sha256"),
        "profile_stable": before.get("profile") == after.get("profile"),
        "hub_identity_stable": (
            before_hub.get("hub_id"),
            before_hub.get("principal_ref"),
        )
        == (
            after_hub.get("hub_id"),
            after_hub.get("principal_ref"),
        ),
        "generations_stable": before.get("generations") == after.get("generations"),
        "entity_identities_stable": all(identity_checks.values()),
        "operation_identities_stable": operation_ids_stable,
        "edge_journal_identities_stable": all(edge_identity_checks.values()),
        "hub_entity_revisions_monotonic": all(revision_checks.values()),
        "operation_revisions_monotonic": operation_revisions_monotonic,
        "edge_attempt_revisions_monotonic": attempt_revisions_monotonic,
        "hub_mutation_revision_monotonic": int(after_hub.get("mutation_count") or 0)
        >= int(before_hub.get("mutation_count") or 0),
        "hub_event_revision_monotonic": int(after_hub.get("max_event_revision") or 0)
        >= int(before_hub.get("max_event_revision") or 0),
        "edge_projection_revision_monotonic": int(
            after_edge.get("projection_revision") or 0
        )
        >= int(before_edge.get("projection_revision") or 0),
        "hub_integrity_preserved": before_hub.get("integrity_check") == ["ok"]
        and after_hub.get("integrity_check") == ["ok"],
        "edge_integrity_preserved": before_edge.get("integrity_check") == ["ok"]
        and after_edge.get("integrity_check") == ["ok"],
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "entity_identity_checks": identity_checks,
        "entity_revision_checks": revision_checks,
        "edge_identity_checks": edge_identity_checks,
    }


def _downgrade_upgrade_fixture_to_schema_two(path: Path) -> None:
    """Reverse only schema 3 so the fixture is an exact supported schema-2 store."""

    with sqlite3.connect(path) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'operation_group_index'"
        ).fetchone()
        if table is None:
            raise ProductionRestartEvalError(
                "Seed Hub is missing the schema-3 operation-group index"
            )
        connection.execute("DROP TABLE operation_group_index")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")


def _downgrade_edge_fixture_to_schema_two(path: Path) -> None:
    """Reverse only Edge schema 3 while preserving every schema-2 data row."""

    with sqlite3.connect(path) as connection:
        index = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'index' "
            "AND name = 'result_outbox_confirmation_pending_idx'"
        ).fetchone()
        columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("result_outbox")')
        }
        if index is None or "hub_confirmed_at" not in columns:
            raise ProductionRestartEvalError(
                "Seed Edge is missing the schema-3 confirmation contract"
            )
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute(
            "ALTER TABLE result_outbox DROP COLUMN hub_confirmed_at"
        )
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")


def _prove_edge_startup_refusal(
    paths: FixturePaths,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    return _run_patchbay_cli_expect_failure(
        paths,
        environment,
        "edge-upgrade-refused",
        ["edge", "run-once", "--config", str(paths.config), "--json"],
        expected_text=(
            "migration is blocked until a validated pre-migration backup marker"
        ),
    )


def _real_upgrade_rehearsal(paths: FixturePaths) -> dict[str, Any]:
    """Exercise a real schema-2 upgrade through production CLIs in its own fixture."""

    upgrade = FixturePaths.under(paths.root / "upgrade-rehearsal")
    _assert_temp_paths(upgrade)
    if not _path_is_within(upgrade.root, paths.root):
        raise ProductionRestartEvalError(
            "Upgrade rehearsal must remain inside the disposable parent fixture"
        )
    upgrade.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    upgrade.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    upgrade.codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _initialize_repository(upgrade)
    seed_port = _free_port()
    _write_config(upgrade, port=seed_port)
    environment = _subprocess_environment(upgrade)
    base_url = f"http://127.0.0.1:{seed_port}"

    seed_hub: HubProcess | None = None
    migrate_hub: HubProcess | None = None
    restart_hub: HubProcess | None = None
    seed_edge: EdgeProcess | None = None
    migrate_edge: EdgeProcess | None = None
    restart_edge: EdgeProcess | None = None
    group_id = ""
    try:
        seed_hub = _start_hub(
            upgrade,
            environment,
            port=seed_port,
            phase="upgrade-seed",
        )
        enrollment_code = _run_patchbay_cli(
            upgrade,
            environment,
            "hub-enrollment-code",
            [
                "hub",
                "enroll-code",
                "create",
                "--config",
                str(upgrade.config),
                "--name",
                "Production Upgrade Edge",
                "--tag",
                "upgrade-eval",
                "--json",
            ],
        )
        code = str(enrollment_code.get("code") or "")
        if not code:
            raise ProductionRestartEvalError(
                "Upgrade fixture enrollment CLI returned no code"
            )
        enrollment = _run_patchbay_cli(
            upgrade,
            environment,
            "edge-enroll",
            [
                "edge",
                "enroll",
                "--config",
                str(upgrade.config),
                "--hub",
                base_url,
                "--code",
                code,
                "--machine-id",
                "production-upgrade-edge",
                "--machine-name",
                "Production Upgrade Edge",
                "--tag",
                "upgrade-eval",
                "--json",
            ],
        )
        profile = enrollment.get("profile")
        if not isinstance(profile, Mapping):
            raise ProductionRestartEvalError(
                "Upgrade fixture Edge enrollment returned no public profile"
            )
        client = McpClient(base_url)
        client.initialize()
        seed_edge_started_at = time.time()
        seed_edge = _start_edge_service(upgrade, environment, phase="upgrade-seed")
        upgrade_machine_id = str(profile.get("machine_id") or "")
        _wait_for_edge_online(
            client,
            seed_edge,
            machine_id=upgrade_machine_id,
            started_after=seed_edge_started_at,
            phase="upgrade_seed_edge_online",
        )
        group_create = client.call(
            "patchbay_work_group_create",
            {
                "title": "Production schema upgrade witness",
                "goal": "Preserve exact durable Hub state across schema migration.",
                "repo_path": str(upgrade.repository),
                "machine_id": upgrade_machine_id,
                "lanes": [
                    {
                        "lane": "main",
                        "title": "Upgrade verification",
                        "role": "Evaluate",
                    }
                ],
                "execution_mode": "end_to_end",
                "definition_of_done": "Exact identity, state, and revisions survive.",
                "wait_for_preflight_seconds": 0,
                "idempotency_key": "production-schema-upgrade-group",
            },
        )
        group_id = str(
            group_create.get("result", {}).get("work_group", {}).get("work_group_id")
            or ""
        )
        if not group_id:
            raise ProductionRestartEvalError(
                "Upgrade fixture did not create a durable work group"
            )
        _wait_for(
            lambda: client.call(
                "patchbay_work_group_status",
                {
                    "work_group_id": group_id,
                    "include_workers": True,
                    "include_operations": True,
                },
            ),
            lambda value: value.get("result", {}).get("readiness", {}).get("status")
            == "ready",
            phase="upgrade_fixture_group_ready",
        )
        _stop_edge_service(seed_edge)
        seed_edge = None
        _stop_hub(seed_hub)
        seed_hub = None

        _downgrade_upgrade_fixture_to_schema_two(upgrade.hub_database)
        _downgrade_edge_fixture_to_schema_two(upgrade.edge_journal)
        older = _read_exact_upgrade_state(upgrade.hub_database)
        older_edge = _read_exact_edge_upgrade_state(upgrade.edge_journal)
        if older["schema_version"] != 2 or older["user_version"] != 2:
            raise ProductionRestartEvalError(
                "Upgrade fixture did not become a schema-2 Hub database"
            )
        if older["operation_group_index"]["exists"]:
            raise ProductionRestartEvalError(
                "Schema-2 upgrade fixture unexpectedly retained the schema-3 index"
            )
        if (
            older_edge["schema_version"] != 2
            or older_edge["user_version"] != 2
            or older_edge["confirmation_column_exists"]
            or older_edge["confirmation_index_exists"]
        ):
            raise ProductionRestartEvalError(
                "Upgrade fixture did not become an exact schema-2 Edge journal"
            )

        refusal = _prove_hub_startup_refusal(
            upgrade,
            environment,
            port=_free_port(),
        )
        refused_state = _read_exact_upgrade_state(upgrade.hub_database)
        edge_refusal = _prove_edge_startup_refusal(upgrade, environment)
        refused_edge_state = _read_exact_edge_upgrade_state(upgrade.edge_journal)

        backup_path = upgrade.root / "private-backups" / "hub-schema-two.sqlite3"
        restored_path = upgrade.root / "restored" / "hub-schema-two.sqlite3"
        edge_backup_path = (
            upgrade.root / "private-backups" / "edge-schema-two.sqlite3"
        )
        edge_restored_path = (
            upgrade.root / "restored" / "edge-schema-two.sqlite3"
        )
        deployed_revision = "production-restart-eval-schema-two"
        backup_created = _run_patchbay_cli(
            upgrade,
            environment,
            "hub-upgrade-backup-create",
            [
                "hub",
                "backup",
                "create",
                "--database",
                str(upgrade.hub_database),
                "--backup",
                str(backup_path),
                "--prepare-migration",
                "--deployed-revision",
                deployed_revision,
                "--json",
            ],
        )
        marker = dict(backup_created.get("pre_migration_backup") or {})
        manifest_path = Path(str(backup_created.get("manifest_path") or ""))
        if not backup_path.is_file() or not manifest_path.is_file():
            raise ProductionRestartEvalError(
                "Production backup CLI did not publish its complete immutable bundle"
            )
        immutable_before = {
            "database_sha256": _sha256(backup_path),
            "manifest_sha256": _sha256(manifest_path),
        }
        edge_backup_created = _run_patchbay_cli(
            upgrade,
            environment,
            "edge-upgrade-backup-create",
            [
                "edge",
                "backup",
                "create",
                "--database",
                str(upgrade.edge_journal),
                "--backup",
                str(edge_backup_path),
                "--prepare-migration",
                "--deployed-revision",
                deployed_revision,
                "--json",
            ],
        )
        edge_marker = dict(edge_backup_created.get("pre_migration_backup") or {})
        edge_manifest_path = Path(
            str(edge_backup_created.get("manifest_path") or "")
        )
        if not edge_backup_path.is_file() or not edge_manifest_path.is_file():
            raise ProductionRestartEvalError(
                "Production Edge backup CLI did not publish its immutable bundle"
            )
        edge_immutable_before = {
            "database_sha256": _sha256(edge_backup_path),
            "manifest_sha256": _sha256(edge_manifest_path),
        }

        migrate_port = seed_port
        migrate_hub = _start_hub(
            upgrade,
            environment,
            port=migrate_port,
            phase="upgrade-migrate",
        )
        migrated_client = McpClient(f"http://127.0.0.1:{migrate_port}")
        migrated_client.initialize()
        migrated_before_edge = _read_exact_upgrade_state(upgrade.hub_database)
        migrate_edge_started_at = time.time()
        migrate_edge = _start_edge_service(
            upgrade,
            environment,
            phase="upgrade-migrate",
        )
        _wait_for_edge_online(
            migrated_client,
            migrate_edge,
            machine_id=upgrade_machine_id,
            started_after=migrate_edge_started_at,
            phase="upgrade_migrated_edge_online",
        )
        migrated_group = migrated_client.call(
            "patchbay_work_group_status",
            {
                "work_group_id": group_id,
                "include_workers": True,
                "include_operations": True,
            },
        )
        migrated_edge = _read_exact_edge_upgrade_state(upgrade.edge_journal)
        _stop_edge_service(migrate_edge)
        migrate_edge = None
        _stop_hub(migrate_hub)
        migrate_hub = None
        migrated = _read_exact_upgrade_state(upgrade.hub_database)

        restart_port = seed_port
        restart_hub = _start_hub(
            upgrade,
            environment,
            port=restart_port,
            phase="upgrade-restarted",
        )
        restarted_client = McpClient(f"http://127.0.0.1:{restart_port}")
        restarted_client.initialize()
        restart_edge_started_at = time.time()
        restart_edge = _start_edge_service(
            upgrade,
            environment,
            phase="upgrade-restarted",
        )
        _wait_for_edge_online(
            restarted_client,
            restart_edge,
            machine_id=upgrade_machine_id,
            started_after=restart_edge_started_at,
            phase="upgrade_restarted_edge_online",
        )
        restarted_group = restarted_client.call(
            "patchbay_work_group_status",
            {
                "work_group_id": group_id,
                "include_workers": True,
                "include_operations": True,
            },
        )
        _stop_edge_service(restart_edge)
        restart_edge = None
        _stop_hub(restart_hub)
        restart_hub = None
        restarted = _read_exact_upgrade_state(upgrade.hub_database)
        restarted_edge = _read_exact_edge_upgrade_state(upgrade.edge_journal)

        restore_target_was_fresh = not any(
            candidate.exists()
            for candidate in (
                restored_path,
                Path(f"{restored_path}-wal"),
                Path(f"{restored_path}-shm"),
            )
        )
        restored = _run_patchbay_cli(
            upgrade,
            environment,
            "hub-upgrade-backup-restore",
            [
                "hub",
                "backup",
                "restore",
                "--backup",
                str(backup_path),
                "--restore-to",
                str(restored_path),
                "--expected-deployed-revision",
                deployed_revision,
                "--json",
            ],
        )
        restored_state = _read_exact_upgrade_state(restored_path)
        edge_restore_target_was_fresh = not any(
            candidate.exists()
            for candidate in (
                edge_restored_path,
                Path(f"{edge_restored_path}-wal"),
                Path(f"{edge_restored_path}-shm"),
            )
        )
        edge_restored = _run_patchbay_cli(
            upgrade,
            environment,
            "edge-upgrade-backup-restore",
            [
                "edge",
                "backup",
                "restore",
                "--backup",
                str(edge_backup_path),
                "--restore-to",
                str(edge_restored_path),
                "--expected-deployed-revision",
                deployed_revision,
                "--json",
            ],
        )
        edge_restored_state = _read_exact_edge_upgrade_state(edge_restored_path)
        immutable_after = {
            "database_sha256": _sha256(backup_path),
            "manifest_sha256": _sha256(manifest_path),
        }
        edge_immutable_after = {
            "database_sha256": _sha256(edge_backup_path),
            "manifest_sha256": _sha256(edge_manifest_path),
        }
    finally:
        if seed_edge is not None:
            _force_stop_edge(seed_edge)
        if migrate_edge is not None:
            _force_stop_edge(migrate_edge)
        if restart_edge is not None:
            _force_stop_edge(restart_edge)
        if seed_hub is not None:
            _force_stop_hub(seed_hub)
        if migrate_hub is not None:
            _force_stop_hub(migrate_hub)
        if restart_hub is not None:
            _force_stop_hub(restart_hub)

    older_authoritative = _authoritative_upgrade_state(older)
    restored_authoritative = _authoritative_upgrade_state(restored_state)
    older_edge_authoritative = _authoritative_edge_upgrade_state(older_edge)
    refused_edge_authoritative = _authoritative_edge_upgrade_state(
        refused_edge_state
    )
    restored_edge_authoritative = _authoritative_edge_upgrade_state(
        edge_restored_state
    )
    migrated_group_id = str(
        migrated_group.get("result", {}).get("work_group", {}).get("work_group_id")
        or migrated_group.get("result", {}).get("work_group_id")
        or ""
    )
    restarted_group_id = str(
        restarted_group.get("result", {}).get("work_group", {}).get("work_group_id")
        or restarted_group.get("result", {}).get("work_group_id")
        or ""
    )
    checks = {
        "production_startup_refused_older_schema_without_marker": refusal.get("refused")
        is True,
        "refused_start_left_exact_state_unchanged": _authoritative_upgrade_state(
            refused_state
        )
        == older_authoritative,
        "production_edge_refused_older_schema_without_marker": bool(
            edge_refusal.get("refused") is True
        ),
        "refused_edge_start_left_exact_state_unchanged": (
            refused_edge_authoritative == older_edge_authoritative
        ),
        "production_backup_cli_prepared_migration": bool(
            backup_created.get("valid")
            and backup_created.get("source_unchanged")
            and marker.get("valid")
            and marker.get("source_schema_version") == 2
            and marker.get("target_schema_version") == 3
        ),
        "production_hub_cli_migrated_schema": bool(
            migrated_before_edge.get("schema_version") == 3
            and migrated_before_edge.get("user_version") == 3
            and migrated_before_edge.get("operation_group_index", {}).get("exists")
            and migrated_before_edge.get("operation_group_index", {}).get("rows", 0)
            > 0
        ),
        "exact_identity_state_and_revisions_survived_migration": (
            _authoritative_upgrade_state(migrated_before_edge)
            == older_authoritative
        ),
        "production_edge_backup_cli_prepared_migration": bool(
            edge_backup_created.get("valid")
            and edge_backup_created.get("source_unchanged")
            and edge_marker.get("valid")
            and edge_marker.get("source_schema_version") == 2
            and edge_marker.get("target_schema_version") == 3
        ),
        "production_edge_cli_migrated_schema": bool(
            migrated_edge.get("schema_version") == 3
            and migrated_edge.get("user_version") == 3
            and migrated_edge.get("confirmation_column_exists")
            and migrated_edge.get("confirmation_index_exists")
        ),
        "edge_durable_payload_survived_migration": (
            _edge_payload_survived_service_transition(older_edge, migrated_edge)
        ),
        "migrated_group_visible_through_production_mcp": migrated_group_id == group_id,
        "second_production_restart_preserved_ids_and_monotonic_state": (
            _hub_restart_state_is_monotonic(migrated, restarted)
            and restarted.get("schema_version") == 3
            and restarted_group_id == group_id
        ),
        "second_edge_restart_preserved_ids_and_monotonic_state": bool(
            _edge_payload_survived_service_transition(
                migrated_edge, restarted_edge
            )
            and int(restarted_edge.get("projection_revision") or 0)
            >= int(migrated_edge.get("projection_revision") or 0)
            and restarted_edge.get("schema_version") == 3
        ),
        "immutable_backup_bundle_unchanged": immutable_after == immutable_before,
        "immutable_edge_backup_bundle_unchanged": (
            edge_immutable_after == edge_immutable_before
        ),
        "production_restore_used_fresh_path": bool(
            restore_target_was_fresh
            and restored.get("restored")
            and Path(str(restored.get("restore_path") or "")) == restored_path
        ),
        "fresh_restore_matches_exact_older_state": bool(
            restored_state.get("schema_version") == 2
            and restored_authoritative == older_authoritative
            and restored.get("pre_migration_backup_marker", {}).get("valid")
        ),
        "production_edge_restore_used_fresh_path": bool(
            edge_restore_target_was_fresh
            and edge_restored.get("restored")
            and Path(str(edge_restored.get("restore_path") or ""))
            == edge_restored_path
        ),
        "fresh_edge_restore_matches_exact_older_state": bool(
            edge_restored_state.get("schema_version") == 2
            and restored_edge_authoritative == older_edge_authoritative
            and edge_restored.get("pre_migration_backup_marker", {}).get("valid")
        ),
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "fixture_kind": "separate_disposable_schema_upgrade",
        "entrypoints": {
            "seed_and_migrate": "patchbay hub start",
            "edge_seed": "patchbay edge enroll / patchbay edge start",
            "backup": "patchbay hub backup create --prepare-migration",
            "restore": "patchbay hub backup restore",
            "edge_backup": "patchbay edge backup create --prepare-migration",
            "edge_restore": "patchbay edge backup restore",
            "manual_runtime_adapters": False,
        },
        "checks": [{"name": name, "passed": passed} for name, passed in checks.items()],
        "paths": {
            **upgrade.public_mapping(),
            "backup": str(backup_path),
            "backup_manifest": str(manifest_path),
            "restored_database": str(restored_path),
            "edge_backup": str(edge_backup_path),
            "edge_backup_manifest": str(edge_manifest_path),
            "edge_restored_database": str(edge_restored_path),
        },
        "group_id": group_id,
        "startup_refusal": refusal,
        "edge_startup_refusal": edge_refusal,
        "backup": {
            "database_generation": backup_created.get("database_generation"),
            "source_unchanged": backup_created.get("source_unchanged"),
            "pre_migration_backup": marker,
            "immutable_before": immutable_before,
            "immutable_after": immutable_after,
        },
        "restore": {
            "status": restored.get("status"),
            "restored": restored.get("restored"),
            "fresh_path": restore_target_was_fresh,
            "pre_migration_backup_marker_valid": restored.get(
                "pre_migration_backup_marker", {}
            ).get("valid"),
        },
        "edge_backup": {
            "database_generation": edge_backup_created.get("database_generation"),
            "source_unchanged": edge_backup_created.get("source_unchanged"),
            "pre_migration_backup": edge_marker,
            "immutable_before": edge_immutable_before,
            "immutable_after": edge_immutable_after,
        },
        "edge_restore": {
            "status": edge_restored.get("status"),
            "restored": edge_restored.get("restored"),
            "fresh_path": edge_restore_target_was_fresh,
            "pre_migration_backup_marker_valid": edge_restored.get(
                "pre_migration_backup_marker", {}
            ).get("valid"),
        },
        "older": older,
        "older_edge": older_edge,
        "migrated_before_edge": migrated_before_edge,
        "migrated": migrated,
        "migrated_edge": migrated_edge,
        "restarted": restarted,
        "restarted_edge": restarted_edge,
        "restored": restored_state,
        "restored_edge": edge_restored_state,
    }


def _assert_temp_paths(paths: FixturePaths) -> None:
    if not paths.root.is_absolute():
        raise ProductionRestartEvalError("Fixture root must be absolute")
    for value in paths.public_mapping().values():
        if not _path_is_within(value, paths.root):
            raise ProductionRestartEvalError(
                "Every exact evidence path must stay inside the temp fixture"
            )


def _run_eval(
    paths: FixturePaths,
    *,
    rehearse_old_schema: bool,
) -> dict[str, Any]:
    _assert_temp_paths(paths)
    paths.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_stub_codex(paths)
    _initialize_repository(paths)
    port = _free_port()
    _write_config(paths, port=port)
    environment = _subprocess_environment(paths)
    initial_environment_fingerprint = _environment_fingerprint(environment)
    stable_environment = {
        "PATCHBAY_HOME": environment["PATCHBAY_HOME"],
        "PATCHBAY_CONFIG": environment["PATCHBAY_CONFIG"],
        "HOME": environment["HOME"],
        "CODEX_HOME": environment["CODEX_HOME"],
    }
    base_url = f"http://127.0.0.1:{port}"
    first_hub: HubProcess | None = None
    restarted_hub: HubProcess | None = None
    first_edge: EdgeProcess | None = None
    restarted_edge: EdgeProcess | None = None
    first_stop_evidence: dict[str, Any] | None = None
    restarted_stop_evidence: dict[str, Any] | None = None
    first_edge_stop_evidence: dict[str, Any] | None = None
    restarted_edge_stop_evidence: dict[str, Any] | None = None
    try:
        first_hub = _start_hub(
            paths,
            environment,
            port=port,
            phase="initial",
        )
        enrollment_code = _run_patchbay_cli(
            paths,
            environment,
            "hub-enrollment-code",
            [
                "hub",
                "enroll-code",
                "create",
                "--config",
                str(paths.config),
                "--name",
                "Production Restart Edge",
                "--tag",
                "restart-eval",
                "--ttl-minutes",
                "10",
                "--json",
            ],
        )
        code = str(enrollment_code.get("code") or "")
        if not code:
            raise ProductionRestartEvalError(
                "Production enrollment-code CLI returned no code"
            )
        enrollment = _run_patchbay_cli(
            paths,
            environment,
            "edge-enroll",
            [
                "edge",
                "enroll",
                "--config",
                str(paths.config),
                "--hub",
                base_url,
                "--code",
                code,
                "--machine-id",
                "production-restart-edge",
                "--machine-name",
                "Production Restart Edge",
                "--tag",
                "restart-eval",
                "--json",
            ],
        )
        profile = enrollment.get("profile")
        if not isinstance(profile, Mapping):
            raise ProductionRestartEvalError(
                "Production Edge enrollment returned no public profile"
            )
        if Path(str(enrollment.get("profile_path") or "")) != paths.edge_profile:
            raise ProductionRestartEvalError(
                "Production Edge enrollment did not use the absolute temp profile path"
            )

        client = McpClient(base_url)
        initialized = client.initialize()
        first_edge_started_at = time.time()
        first_edge = _start_edge_service(paths, environment, phase="initial")
        machine_id = str(profile.get("machine_id") or "")
        fleet_before_group = _wait_for_edge_online(
            client,
            first_edge,
            machine_id=machine_id,
            started_after=first_edge_started_at,
            phase="edge_service_online_before_group",
        )
        group_create = client.call(
            "patchbay_work_group_create",
            {
                "title": "Production entrypoint restart",
                "goal": "Prove durable Hub and Edge identities survive an identical-environment restart.",
                "repo_path": str(paths.repository),
                "machine_id": machine_id,
                "lanes": [
                    {
                        "lane": "main",
                        "title": "Restart verification",
                        "role": "Evaluate",
                    }
                ],
                "execution_mode": "end_to_end",
                "definition_of_done": "One durable worker projection remains visible after restart without duplicate state.",
                "wait_for_preflight_seconds": 0,
                "idempotency_key": "production-entrypoint-restart-group",
            },
        )
        work_group = group_create.get("result", {}).get("work_group", {})
        group_id = str(work_group.get("work_group_id") or "")
        if not group_id:
            raise ProductionRestartEvalError(
                "Production Hub did not create a durable work group"
            )

        group_ready = _wait_for(
            lambda: client.call(
                "patchbay_work_group_status",
                {
                    "work_group_id": group_id,
                    "include_workers": True,
                    "include_operations": True,
                },
            ),
            lambda value: value.get("result", {}).get("readiness", {}).get("status")
            == "ready",
            phase="group_preflight_ready",
        )
        worker_start = client.call(
            "patchbay_worker_start",
            {
                "work_group_id": group_id,
                "lane": "main",
                "name": "Restart Witness",
                "brief": (
                    "Complete one read-only disposable turn through the normal PatchBay "
                    "worker subprocess boundary. Do not modify the repository."
                ),
                "workspace_mode": "read_only",
                "idempotency_key": "production-entrypoint-restart-worker",
            },
        )
        worker_operation_id = str(
            worker_start.get("operation", {}).get("operation_id") or ""
        )
        if not worker_operation_id:
            raise ProductionRestartEvalError(
                "Production Hub returned no durable worker operation"
            )

        worker_operation = _wait_for(
            lambda: client.call(
                "patchbay_operation_status",
                {"operation_id": worker_operation_id, "include_result": True},
            ),
            lambda value: str(
                value.get("operation", {}).get("state")
                or value.get("result", {}).get("operation", {}).get("state")
                or ""
            )
            in TERMINAL_OPERATION_STATES,
            phase="worker_operation_terminal",
        )
        workers_before_restart = _wait_for(
            lambda: client.call(
                "patchbay_worker_list",
                {
                    "work_group_id": group_id,
                    "include_stopped": True,
                    "limit": 20,
                },
            ),
            lambda value: (
                len(value.get("result", {}).get("workers", [])) == 1
                and str(
                    value.get("result", {}).get("workers", [])[0].get(
                        "turn_state"
                    )
                    or ""
                )
                == "completed"
                and bool(
                    value.get("result", {}).get("workers", [])[0].get(
                        "has_session"
                    )
                )
                and bool(
                    value.get("result", {}).get("workers", [])[0].get(
                        "can_message"
                    )
                )
            ),
            phase="worker_session_terminal_before_restart",
            timeout=65,
            poll_interval=20,
        )

        first_edge_stop_evidence = _stop_edge_service(first_edge)
        first_edge = None
        first_stop_evidence = _stop_hub(first_hub)
        first_hub = None
        continuity_identity = _enable_continuity_guards(paths)
        continuity_refusals = _prove_continuity_guard_refusals(paths, environment)
        before = _snapshot(paths)
        if any(
            operation.get("state") not in TERMINAL_OPERATION_STATES
            for operation in before["hub"]["operations"].values()
        ):
            raise ProductionRestartEvalError(
                "The pre-restart Hub snapshot still contains a nonterminal operation"
            )
        if len(_identity_keys(before["hub"], "hub.work_group")) != 1:
            raise ProductionRestartEvalError(
                "The pre-restart snapshot does not contain exactly one durable group"
            )
        if len(_identity_keys(before["hub"], "hub.worker_projection")) != 1:
            raise ProductionRestartEvalError(
                "The pre-restart snapshot does not contain exactly one worker projection"
            )

        restarted_environment = _subprocess_environment(paths)
        restarted_environment_fingerprint = _environment_fingerprint(
            restarted_environment
        )
        restarted_hub = _start_hub(
            paths,
            restarted_environment,
            port=port,
            phase="restarted",
        )
        restarted_client = McpClient(base_url)
        restarted_initialized = restarted_client.initialize()
        restarted_edge_started_at = time.time()
        restarted_edge = _start_edge_service(
            paths,
            restarted_environment,
            phase="restarted",
        )
        fleet_after_restart = _wait_for_edge_online(
            restarted_client,
            restarted_edge,
            machine_id=machine_id,
            started_after=restarted_edge_started_at,
            phase="edge_service_online_after_restart",
        )
        group_after_restart = restarted_client.call(
            "patchbay_work_group_status",
            {
                "work_group_id": group_id,
                "include_workers": True,
                "include_operations": True,
            },
        )
        workers_after_restart = restarted_client.call(
            "patchbay_worker_list",
            {
                "work_group_id": group_id,
                "include_stopped": True,
                "limit": 20,
            },
        )
        workers_after_items = workers_after_restart.get("result", {}).get(
            "workers", []
        )
        if len(workers_after_items) != 1:
            raise ProductionRestartEvalError(
                "Restarted Hub did not expose exactly one durable worker"
            )
        resumed_worker_ref = str(
            workers_after_items[0].get("fleet_worker_ref")
            or workers_after_items[0].get("worker_id")
            or ""
        )
        if not resumed_worker_ref:
            raise ProductionRestartEvalError(
                "Restarted worker projection has no durable fleet reference"
            )
        after_restart_before_follow_up = _snapshot(paths)
        follow_up = restarted_client.call(
            "patchbay_worker_message",
            {
                "work_group_id": group_id,
                "fleet_worker_ref": resumed_worker_ref,
                "message": (
                    "Confirm the same durable worker session survived the Hub restart. "
                    "Do not modify the repository."
                ),
                "idempotency_key": "production-entrypoint-restart-follow-up",
            },
        )
        follow_up_operation_id = str(
            follow_up.get("operation", {}).get("operation_id") or ""
        )
        if not follow_up_operation_id:
            raise ProductionRestartEvalError(
                "Restarted Hub returned no durable follow-up operation"
            )
        follow_up_operation = _wait_for(
            lambda: restarted_client.call(
                "patchbay_operation_status",
                {"operation_id": follow_up_operation_id, "include_result": True},
            ),
            lambda value: str(
                value.get("operation", {}).get("state")
                or value.get("result", {}).get("operation", {}).get("state")
                or ""
            )
            in TERMINAL_OPERATION_STATES,
            phase="worker_follow_up_terminal_after_restart",
        )
        workers_after_follow_up = _wait_for(
            lambda: restarted_client.call(
                "patchbay_worker_list",
                {
                    "work_group_id": group_id,
                    "include_stopped": True,
                    "limit": 20,
                },
            ),
            lambda value: (
                len(value.get("result", {}).get("workers", [])) == 1
                and int(
                    value.get("result", {}).get("workers", [])[0].get(
                        "turn_count"
                    )
                    or 0
                )
                >= 2
                and str(
                    value.get("result", {}).get("workers", [])[0].get(
                        "turn_state"
                    )
                    or ""
                )
                == "completed"
                and bool(
                    value.get("result", {}).get("workers", [])[0].get(
                        "has_session"
                    )
                )
            ),
            phase="same_worker_follow_up_completed_after_restart",
            timeout=65,
            poll_interval=20,
        )
        restarted_edge_stop_evidence = _stop_edge_service(restarted_edge)
        restarted_edge = None
        restarted_stop_evidence = _stop_hub(restarted_hub)
        restarted_hub = None
        after = _snapshot(paths)
    finally:
        if first_edge is not None:
            _force_stop_edge(first_edge)
        if restarted_edge is not None:
            _force_stop_edge(restarted_edge)
        if first_hub is not None:
            _force_stop_hub(first_hub)
        if restarted_hub is not None:
            _force_stop_hub(restarted_hub)

    comparison = compare_restart_snapshots(before, after_restart_before_follow_up)
    migration = _real_upgrade_rehearsal(paths) if rehearse_old_schema else None
    workers_before = workers_before_restart.get("result", {}).get("workers", [])
    workers_after = workers_after_restart.get("result", {}).get("workers", [])
    workers_after_follow_up_items = workers_after_follow_up.get("result", {}).get(
        "workers", []
    )
    worker_refs_before = sorted(
        str(worker.get("fleet_worker_ref") or worker.get("worker_id") or "")
        for worker in workers_before
    )
    worker_refs_after = sorted(
        str(worker.get("fleet_worker_ref") or worker.get("worker_id") or "")
        for worker in workers_after
    )
    path_values = list(paths.public_mapping().values())
    temp_path_check = all(_path_is_within(value, paths.root) for value in path_values)
    restarted_stable_environment = {
        "PATCHBAY_HOME": restarted_environment["PATCHBAY_HOME"],
        "PATCHBAY_CONFIG": restarted_environment["PATCHBAY_CONFIG"],
        "HOME": restarted_environment["HOME"],
        "CODEX_HOME": restarted_environment["CODEX_HOME"],
    }
    environment_reused = (
        stable_environment == restarted_stable_environment
        and initial_environment_fingerprint == restarted_environment_fingerprint
    )
    initial_server_name = str(initialized.get("serverInfo", {}).get("name") or "")
    restarted_server_name = str(
        restarted_initialized.get("serverInfo", {}).get("name") or ""
    )
    top_level_checks = {
        "production_hub_cli_started_twice": bool(initial_server_name)
        and initial_server_name == restarted_server_name,
        "production_edge_service_started_twice": bool(
            fleet_before_group.get("status") == "ok"
            and fleet_after_restart.get("status") == "ok"
        ),
        "bounded_edge_service_shutdowns": bool(
            first_edge_stop_evidence
            and first_edge_stop_evidence.get("bounded")
            and restarted_edge_stop_evidence
            and restarted_edge_stop_evidence.get("bounded")
        ),
        "clean_hub_shutdowns": bool(
            first_stop_evidence
            and first_stop_evidence.get("clean")
            and restarted_stop_evidence
            and restarted_stop_evidence.get("clean")
        ),
        "identical_environment_reused": environment_reused,
        "continuity_guards_pin_existing_state": bool(
            continuity_identity.get("hub_id") == before["hub"]["hub_id"]
            and continuity_identity.get("edge_generation")
            == before["edge"]["edge_generation"]
            and continuity_refusals["hub"].get("refused")
            and continuity_refusals["edge"].get("refused")
        ),
        "absolute_temp_paths_only": temp_path_check,
        "group_survived_restart": str(
            group_after_restart.get("result", {})
            .get("work_group", {})
            .get("work_group_id")
            or group_after_restart.get("result", {}).get("work_group_id")
            or ""
        )
        == group_id,
        "worker_projection_survived_restart": worker_refs_before == worker_refs_after
        and len(worker_refs_after) == 1,
        "same_worker_follow_up_completed_after_restart": bool(
            str(
                follow_up_operation.get("operation", {}).get("state")
                or follow_up_operation.get("result", {}).get("operation", {}).get(
                    "state"
                )
                or ""
            )
            in TERMINAL_OPERATION_STATES
            and len(workers_after_follow_up_items) == 1
            and str(
                workers_after_follow_up_items[0].get("fleet_worker_ref")
                or workers_after_follow_up_items[0].get("worker_id")
                or ""
            )
            == resumed_worker_ref
        ),
        "preflight_ready_before_restart": group_ready.get("result", {})
        .get("readiness", {})
        .get("status")
        == "ready",
        "worker_operation_terminal_before_restart": str(
            worker_operation.get("operation", {}).get("state")
            or worker_operation.get("result", {}).get("operation", {}).get("state")
            or ""
        )
        in TERMINAL_OPERATION_STATES,
        "no_new_database_or_journal": bool(
            comparison["checks"]["database_files_stable"]
        ),
        "no_new_generation": bool(comparison["checks"]["generations_stable"]),
        "no_new_group": bool(comparison["entity_identity_checks"]["hub.work_group"]),
        "no_new_worker": bool(
            comparison["entity_identity_checks"]["hub.fleet_worker"]
            and comparison["entity_identity_checks"]["hub.worker_projection"]
        ),
        "no_new_operation": bool(comparison["checks"]["operation_identities_stable"]),
        "revisions_do_not_regress": bool(
            comparison["checks"]["hub_entity_revisions_monotonic"]
            and comparison["checks"]["operation_revisions_monotonic"]
            and comparison["checks"]["edge_attempt_revisions_monotonic"]
            and comparison["checks"]["hub_mutation_revision_monotonic"]
            and comparison["checks"]["hub_event_revision_monotonic"]
            and comparison["checks"]["edge_projection_revision_monotonic"]
        ),
        "state_identity_and_revision_contract": bool(comparison["passed"]),
    }
    if migration is not None:
        top_level_checks["production_old_schema_upgrade_rehearsal"] = bool(
            migration.get("status") == "passed"
            and all(
                check.get("passed") is True for check in migration.get("checks", [])
            )
        )
    return {
        "name": "production_entrypoint_restart_eval",
        "status": "passed" if all(top_level_checks.values()) else "failed",
        "entrypoints": {
            "hub": "patchbay hub start",
            "hub_enrollment": "patchbay hub enroll-code create",
            "edge_enrollment": "patchbay edge enroll",
            "edge_service": "patchbay edge start",
            "hub_backup": "patchbay hub backup create --prepare-migration",
            "hub_restore": "patchbay hub backup restore",
            "hub_factory": "create_production_hub_v2_app",
            "edge_factory": "create_edge_v2_runner",
            "manual_runtime_adapters": False,
        },
        "checks": [
            {"name": name, "passed": passed}
            for name, passed in top_level_checks.items()
        ],
        "paths": paths.public_mapping(),
        "environment": stable_environment,
        "environment_fingerprints": {
            "before_restart": initial_environment_fingerprint,
            "after_restart": restarted_environment_fingerprint,
        },
        "durable_state": {
            "group_id": group_id,
            "worker_refs": worker_refs_after,
            "operation_ids": sorted(after["hub"]["operations"]),
            "generations": after["generations"],
            "database_files": after["database_files"],
        },
        "shutdowns": {
            "before_restart": first_stop_evidence,
            "after_restart": restarted_stop_evidence,
            "edge_before_restart": first_edge_stop_evidence,
            "edge_after_restart": restarted_edge_stop_evidence,
        },
        "continuity_guards": {
            "identity": continuity_identity,
            "refusals": continuity_refusals,
        },
        "before_restart": before,
        "after_restart": after_restart_before_follow_up,
        "after_follow_up": after,
        "comparison": comparison,
        "migration_rehearsal": migration,
    }


def run_production_entrypoint_restart_eval(
    fixture_root: str | Path | None = None,
    *,
    keep_temp: bool = False,
    rehearse_old_schema: bool = False,
) -> dict[str, Any]:
    """Run the restart eval and return a structured, temp-confined report."""

    owns_fixture = fixture_root is None
    root = Path(
        fixture_root
        or tempfile.mkdtemp(prefix="patchbay-production-entrypoint-restart-")
    ).resolve(strict=False)
    paths = FixturePaths.under(root)
    report: dict[str, Any]
    try:
        report = _run_eval(paths, rehearse_old_schema=rehearse_old_schema)
    except Exception as error:
        report = {
            "name": "production_entrypoint_restart_eval",
            "status": "failed",
            "checks": [],
            "paths": paths.public_mapping(),
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
            "migration_rehearsal": None,
        }
    report["fixture_retained"] = bool(not owns_fixture or keep_temp)
    try:
        paths.evidence.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.evidence.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        report["status"] = "failed"
        report["error"] = {
            "type": type(error).__name__,
            "message": "Could not write structured evidence inside the temp fixture",
        }
    if owns_fixture and not keep_temp:
        shutil.rmtree(paths.root, ignore_errors=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a disposable Hub/Edge restart evaluation through production PatchBay CLIs."
        )
    )
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the absolute temp fixture and evidence for inspection.",
    )
    parser.add_argument(
        "--fixture-root",
        help="Use an explicit disposable fixture root; it is never deleted.",
    )
    parser.add_argument(
        "--rehearse-old-schema",
        action="store_true",
        help=(
            "Also run a separate real schema-2 Hub refusal, backup, migration, "
            "restart, and restore rehearsal through production CLIs."
        ),
    )
    args = parser.parse_args()
    report = run_production_entrypoint_restart_eval(
        args.fixture_root,
        keep_temp=args.keep_temp,
        rehearse_old_schema=args.rehearse_old_schema,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['name']}: {report['status']}")
        for check in report.get("checks", []):
            marker = "PASS" if check["passed"] else "FAIL"
            print(f"[{marker}] {check['name']}")
        if report.get("error"):
            print(f"Error: {report['error']['type']}: {report['error']['message']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
