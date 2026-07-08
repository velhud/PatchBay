"""PatchBay Edge runtime for machines connected to an optional Hub."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from patchbay.connector.profiles import resolve_runtime_path
from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.protocol.context import RequestContext
from patchbay.security import redact_sensitive_output
from patchbay.tools.handler import ToolHandler


EDGE_PROFILE_VERSION = 1


def edge_profile_path(environ: Mapping[str, str] | None = None) -> Path:
    return resolve_runtime_path(None, "hub", "edge-profile.json", environ=environ)


def load_edge_profile(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    path = edge_profile_path(environ)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def save_edge_profile(profile: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> str:
    path = edge_profile_path(environ)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = dict(profile)
    payload["version"] = EDGE_PROFILE_VERSION
    payload["updated_at"] = time.time()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return str(path)


def public_edge_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key not in {"node_token"}}


def normalize_hub_url(hub_url: str) -> str:
    value = str(hub_url or "").strip().rstrip("/")
    if not value:
        raise ValueError("Hub URL is required")
    if value.endswith("/mcp"):
        value = value[: -len("/mcp")]
    return value


def post_json(
    hub_url: str,
    path: str,
    payload: Mapping[str, Any],
    *,
    token: str = "",
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    url = f"{normalize_hub_url(hub_url)}{path}"
    body = json.dumps(dict(payload)).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Hub request failed: {error.code} {detail}") from error
    return json.loads(raw or "{}")


def build_capabilities(config: Mapping[str, Any]) -> dict[str, Any]:
    server_config = config.get("server", {}) if isinstance(config.get("server"), dict) else {}
    security_config = config.get("security", {}) if isinstance(config.get("security"), dict) else {}
    power_config = config.get("power_tools", {}) if isinstance(config.get("power_tools"), dict) else {}
    return {
        "codex_worker_tools": True,
        "max_concurrent_jobs": server_config.get("max_concurrent_jobs"),
        "queue_enabled": bool(server_config.get("queue_enabled", False)),
        "default_sandbox": security_config.get("default_sandbox"),
        "direct_write": bool(power_config.get("direct_write", False)),
        "bash_mode": power_config.get("bash_mode", "off"),
    }


def build_workspaces(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    repositories = config.get("repositories", {}) if isinstance(config.get("repositories"), dict) else {}
    roots: list[str] = []
    default_root = repositories.get("default")
    if default_root:
        roots.append(str(default_root))
    for item in repositories.get("allowed") or []:
        if item:
            roots.append(str(item))

    seen: set[str] = set()
    workspaces: list[dict[str, Any]] = []
    for root in roots:
        path = Path(root).expanduser().resolve(strict=False)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        workspaces.append(
            {
                "alias": path.name or key,
                "path": key,
                "exists": path.exists(),
                "git": (path / ".git").exists(),
            }
        )
    return workspaces


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _active_workers_from_status(status: Mapping[str, Any]) -> int:
    active = status.get("active")
    if isinstance(active, bool):
        return int(active)
    if isinstance(active, int):
        return max(0, active)
    counts = status.get("counts")
    if isinstance(counts, Mapping):
        return max(0, _as_int(counts.get("active"), 0))
    workers = status.get("workers")
    if isinstance(workers, list):
        return len(workers)
    return 0


def _memory_status() -> dict[str, Any]:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return {}
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            values[key] = _as_int(parts[1]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    if total <= 0 or available < 0:
        return {}
    used_percent = max(0.0, min(100.0, ((total - available) / total) * 100.0))
    return {
        "memory_used_percent": round(used_percent, 2),
        "memory_available_bytes": available,
        "memory_total_bytes": total,
    }


def _cpu_percent_estimate() -> float | None:
    try:
        load_one, _, _ = os.getloadavg()
    except (AttributeError, OSError):
        return None
    cpu_count = os.cpu_count() or 1
    return round(max(0.0, min(100.0, (load_one / cpu_count) * 100.0)), 2)


def _nearest_existing_path(path: Path) -> Path:
    current = path.expanduser().resolve(strict=False)
    while not current.exists() and current.parent != current:
        current = current.parent
    return current if current.exists() else Path.cwd()


def _disk_telemetry_path(config: Mapping[str, Any]) -> Path:
    workers = config.get("workers") if isinstance(config.get("workers"), Mapping) else {}
    logging_config = config.get("logging") if isinstance(config.get("logging"), Mapping) else {}
    repositories = config.get("repositories") if isinstance(config.get("repositories"), Mapping) else {}
    for value in (
        workers.get("worktree_root"),
        logging_config.get("job_logs_dir"),
        repositories.get("default"),
    ):
        if value:
            return _nearest_existing_path(Path(str(value)))
    return Path.cwd()


def build_resource_status(config: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = build_capabilities(config)
    active_workers = _active_workers_from_status(status)
    max_jobs = max(0, _as_int(capabilities.get("max_concurrent_jobs"), 0))
    free_slots = max(0, max_jobs - active_workers) if max_jobs else 0
    resource_status: dict[str, Any] = {
        "active_workers": active_workers,
        "max_concurrent_jobs": max_jobs,
        "free_worker_slots": free_slots,
        "queue_enabled": bool(capabilities.get("queue_enabled", False)),
    }
    cpu_percent = _cpu_percent_estimate()
    if cpu_percent is not None:
        resource_status["cpu_percent"] = cpu_percent
    resource_status.update(_memory_status())
    try:
        usage = shutil.disk_usage(_disk_telemetry_path(config))
        total = max(1, usage.total)
        resource_status["disk_free_bytes"] = usage.free
        resource_status["disk_used_percent"] = round(((usage.total - usage.free) / total) * 100.0, 2)
    except OSError:
        pass
    return resource_status


async def worker_status(handler: ToolHandler) -> dict[str, Any]:
    try:
        result = await handler.handle_tool_call("codex_worker_status", {}, context=RequestContext.anonymous())
        return redact_sensitive_output(result)
    except Exception as error:
        return {"error": str(error)}


def enroll_edge(
    config: Mapping[str, Any],
    *,
    hub_url: str,
    code: str,
    machine_id: str = "",
    display_name: str = "",
    tags: list[str] | None = None,
    role: str = "",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    machine_id = machine_id or socket.gethostname().lower().replace(".", "-")
    display_name = display_name or socket.gethostname()
    payload = {
        "code": code,
        "machine_id": machine_id,
        "display_name": display_name,
        "tags": tags or [],
        "role": role,
        "capabilities": build_capabilities(config),
        "workspaces": build_workspaces(config),
    }
    result = post_json(hub_url, "/edge/enroll", payload)
    token = str(result.get("node_token") or "")
    if not token:
        raise RuntimeError("Hub did not return a node token")
    profile = {
        "hub_url": normalize_hub_url(hub_url),
        "machine_id": machine_id,
        "display_name": display_name,
        "tags": tags or [],
        "role": role,
        "node_token": token,
        "enrolled_at": time.time(),
    }
    profile_path = save_edge_profile(profile, environ=environ)
    return {"profile_path": profile_path, "profile": public_edge_profile(profile), "machine": result.get("machine")}


class EdgeRunner:
    """Long-running edge loop that reuses one local ToolHandler."""

    def __init__(self, config: dict[str, Any], profile: Mapping[str, Any] | None = None):
        self.config = config
        self.profile = dict(profile or load_edge_profile())
        if not self.profile:
            raise ValueError("No edge profile found. Run `patchbay edge enroll` first.")
        self.manager = JobManager(config)
        self.executor = JobExecutor(config, self.manager)
        self.handler = ToolHandler(config, self.manager, self.executor)

    @property
    def hub_url(self) -> str:
        return str(self.profile.get("hub_url") or "")

    @property
    def machine_id(self) -> str:
        return str(self.profile.get("machine_id") or "")

    @property
    def token(self) -> str:
        return str(self.profile.get("node_token") or "")

    async def heartbeat(self) -> dict[str, Any]:
        status = await worker_status(self.handler)
        payload = {
            "machine_id": self.machine_id,
            "capabilities": build_capabilities(self.config),
            "workspaces": build_workspaces(self.config),
            "worker_status": status,
            "resource_status": build_resource_status(self.config, status),
        }
        return await asyncio.to_thread(post_json, self.hub_url, "/edge/heartbeat", payload, token=self.token)

    async def poll(self) -> dict[str, Any]:
        return await asyncio.to_thread(
            post_json,
            self.hub_url,
            "/edge/poll",
            {"machine_id": self.machine_id},
            token=self.token,
        )

    async def send_result(self, command_id: str, *, result: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(
            post_json,
            self.hub_url,
            "/edge/result",
            {"machine_id": self.machine_id, "command_id": command_id, "result": result or {}, "error": error},
            token=self.token,
        )

    async def execute_command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        command_id = str(command.get("command_id") or "")
        action = str(command.get("action") or "")
        arguments = command.get("arguments") if isinstance(command.get("arguments"), dict) else {}
        if not command_id or not action:
            return {"skipped": True, "reason": "No command claimed"}
        try:
            result = await self.handler.handle_tool_call(action, dict(arguments), context=RequestContext.anonymous())
            return await self.send_result(command_id, result=redact_sensitive_output(result))
        except Exception as error:
            return await self.send_result(command_id, error=str(error))

    async def run_once(self) -> dict[str, Any]:
        heartbeat_result = await self.heartbeat()
        poll_result = await self.poll()
        command = poll_result.get("command") if isinstance(poll_result.get("command"), dict) else None
        if not command:
            return {"heartbeat": heartbeat_result, "poll": poll_result, "executed": False}
        result = await self.execute_command(command)
        return {"heartbeat": heartbeat_result, "poll": poll_result, "executed": True, "result": result}

    async def run_loop(self, interval_seconds: float = 5) -> None:
        interval = max(1.0, float(interval_seconds))
        while True:
            await self.run_once()
            await asyncio.sleep(interval)
