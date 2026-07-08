"""PatchBay Edge runtime for machines connected to an optional Hub."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
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
        "memory_telemetry_source": "/proc/meminfo",
        "memory_telemetry_confidence": "edge_visible",
    }


_LAST_CPU_SAMPLE: tuple[int, int] | None = None


def _read_proc_stat_cpu() -> tuple[int, int] | None:
    proc_stat = Path("/proc/stat")
    if not proc_stat.exists():
        return None
    try:
        line = proc_stat.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (IndexError, OSError):
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    values = [_as_int(part, 0) for part in parts[1:]]
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def _cpu_percent_status() -> dict[str, Any]:
    global _LAST_CPU_SAMPLE
    sample = _read_proc_stat_cpu()
    if sample is not None:
        previous = _LAST_CPU_SAMPLE
        _LAST_CPU_SAMPLE = sample
        if previous is not None:
            total_delta = sample[0] - previous[0]
            idle_delta = sample[1] - previous[1]
            if total_delta > 0:
                used = max(0, total_delta - max(0, idle_delta))
                return {
                    "cpu_percent": round(max(0.0, min(100.0, (used / total_delta) * 100.0)), 2),
                    "cpu_telemetry_source": "/proc/stat_delta",
                    "cpu_telemetry_confidence": "sampled",
                }

    try:
        load_one, _, _ = os.getloadavg()
    except (AttributeError, OSError):
        return {}
    cpu_count = os.cpu_count() or 1
    return {
        "cpu_percent": round(max(0.0, min(100.0, (load_one / cpu_count) * 100.0)), 2),
        "cpu_telemetry_source": "loadavg_1m_per_cpu",
        "cpu_telemetry_confidence": "pressure_estimate",
    }


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    release_path = Path("/proc/sys/kernel/osrelease")
    if not release_path.exists():
        return False
    release = release_path.read_text(encoding="utf-8", errors="replace").lower()
    return "microsoft" in release or "wsl" in release


def _edge_resource_overrides(config: Mapping[str, Any]) -> Mapping[str, Any]:
    hub_config = config.get("hub") if isinstance(config.get("hub"), Mapping) else {}
    edge_config = hub_config.get("edge") if isinstance(hub_config.get("edge"), Mapping) else {}
    resources = edge_config.get("resource_overrides") if isinstance(edge_config.get("resource_overrides"), Mapping) else {}
    return resources


def _configured_disk_override(config: Mapping[str, Any]) -> dict[str, Any]:
    resources = _edge_resource_overrides(config)
    free = os.environ.get("PATCHBAY_EDGE_DISK_FREE_BYTES", resources.get("disk_free_bytes"))
    total = os.environ.get("PATCHBAY_EDGE_DISK_TOTAL_BYTES", resources.get("disk_total_bytes"))
    used_percent = os.environ.get("PATCHBAY_EDGE_DISK_USED_PERCENT", resources.get("disk_used_percent"))
    if free is None and total is None and used_percent is None:
        return {}

    result: dict[str, Any] = {
        "disk_telemetry_source": str(os.environ.get("PATCHBAY_EDGE_DISK_SOURCE") or resources.get("disk_source") or "configured_override"),
        "disk_telemetry_confidence": "configured",
    }
    free_int = _as_int(free, -1) if free is not None else -1
    total_int = _as_int(total, -1) if total is not None else -1
    if free_int >= 0:
        result["disk_free_bytes"] = free_int
    if total_int > 0:
        result["disk_total_bytes"] = total_int
    if used_percent is not None:
        result["disk_used_percent"] = round(max(0.0, min(_as_float(used_percent, 0.0), 100.0)), 2)
    elif free_int >= 0 and total_int > 0:
        result["disk_used_percent"] = round(((total_int - min(free_int, total_int)) / total_int) * 100.0, 2)
    return result


def _windows_host_disk_status() -> dict[str, Any]:
    if not _is_wsl():
        return {}
    mount = Path("/mnt/c")
    if mount.exists():
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            usage = None
        if usage is not None:
            total = max(1, usage.total)
            return {
                "disk_host_free_bytes": usage.free,
                "disk_host_total_bytes": usage.total,
                "disk_host_used_percent": round(((usage.total - usage.free) / total) * 100.0, 2),
                "disk_host_source": "/mnt/c",
            }

    powershell = shutil.which("powershell.exe")
    if not powershell:
        return {}
    script = (
        "$d=Get-PSDrive -Name C; "
        "[Console]::WriteLine((@{Free=[int64]$d.Free;Used=[int64]$d.Used}|ConvertTo-Json -Compress))"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    free = _as_int(payload.get("Free"), -1)
    used = _as_int(payload.get("Used"), -1)
    if free < 0 or used < 0:
        return {}
    total = max(1, free + used)
    return {
        "disk_host_free_bytes": free,
        "disk_host_total_bytes": total,
        "disk_host_used_percent": round((used / total) * 100.0, 2),
        "disk_host_source": "powershell:C",
    }


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


def _disk_status(config: Mapping[str, Any]) -> dict[str, Any]:
    telemetry_path = _disk_telemetry_path(config)
    try:
        usage = shutil.disk_usage(telemetry_path)
    except OSError:
        return {}

    total = max(1, usage.total)
    status: dict[str, Any] = {
        "disk_filesystem_path": str(telemetry_path),
        "disk_filesystem_free_bytes": usage.free,
        "disk_filesystem_total_bytes": usage.total,
        "disk_filesystem_used_percent": round(((usage.total - usage.free) / total) * 100.0, 2),
    }

    configured = _configured_disk_override(config)
    if configured:
        status.update(configured)
        return status

    host = _windows_host_disk_status()
    if host:
        status.update(host)
        free = min(usage.free, _as_int(host.get("disk_host_free_bytes"), usage.free))
        host_total = _as_int(host.get("disk_host_total_bytes"), 0)
        status.update(
            {
                "disk_free_bytes": free,
                "disk_total_bytes": host_total if host_total > 0 else usage.total,
                "disk_used_percent": host.get("disk_host_used_percent", status["disk_filesystem_used_percent"]),
                "disk_telemetry_source": f"effective_min(filesystem,{host.get('disk_host_source')})",
                "disk_telemetry_confidence": "host",
            }
        )
        return status

    if _is_wsl():
        status.update(
            {
                "disk_telemetry_source": "wsl_virtual_filesystem",
                "disk_telemetry_confidence": "virtualized",
                "disk_telemetry_warning": (
                    "WSL reports the virtual Linux filesystem capacity. Configure "
                    "hub.edge.resource_overrides.disk_free_bytes or PATCHBAY_EDGE_DISK_FREE_BYTES "
                    "when host disk telemetry is unavailable."
                ),
            }
        )
        return status

    status.update(
        {
            "disk_free_bytes": usage.free,
            "disk_total_bytes": usage.total,
            "disk_used_percent": status["disk_filesystem_used_percent"],
            "disk_telemetry_source": "filesystem",
            "disk_telemetry_confidence": "filesystem",
        }
    )
    return status


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
    resource_status.update(_cpu_percent_status())
    resource_status.update(_memory_status())
    resource_status.update(_disk_status(config))
    return resource_status


async def worker_status(handler: ToolHandler) -> dict[str, Any]:
    try:
        result = await handler.handle_tool_call("codex_worker_status", {}, context=RequestContext.anonymous())
        return redact_sensitive_output(result)
    except Exception as error:
        return {"error": str(error)}


def _repo_path_from_projection(config: Mapping[str, Any], repo_path: str) -> Path:
    requested = str(repo_path or "").strip()
    workspaces = build_workspaces(config)
    if not requested:
        default_root = (config.get("repositories") or {}).get("default") if isinstance(config.get("repositories"), Mapping) else None
        return Path(str(default_root or Path.cwd())).expanduser().resolve(strict=False)
    if os.path.isabs(requested):
        return Path(requested).expanduser().resolve(strict=False)
    needle = requested.lower()
    for workspace in workspaces:
        haystack = " ".join(str(workspace.get(key) or "") for key in ("alias", "path")).lower()
        if needle == str(workspace.get("alias") or "").lower() or needle in haystack:
            return Path(str(workspace.get("path") or requested)).expanduser().resolve(strict=False)
    return Path(requested).expanduser().resolve(strict=False)


def _git_output(repo: Path, *args: str, timeout: float = 5) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def edge_preflight(config: Mapping[str, Any], arguments: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, Any]:
    repo = _repo_path_from_projection(config, str(arguments.get("repo_path") or ""))
    exists = repo.exists()
    git_repo = exists and bool(_git_output(repo, "rev-parse", "--is-inside-work-tree"))
    branch = _git_output(repo, "branch", "--show-current") if git_repo else ""
    head = _git_output(repo, "rev-parse", "--short", "HEAD") if git_repo else ""
    dirty_summary = ""
    if git_repo:
        raw_status = _git_output(repo, "status", "--short")
        lines = [line for line in raw_status.splitlines() if line.strip()]
        dirty_summary = "clean" if not lines else f"{len(lines)} changed/untracked paths"
    try:
        usage = shutil.disk_usage(_nearest_existing_path(repo))
        disk_free = usage.free
        disk_used_percent = round(((usage.total - usage.free) / max(1, usage.total)) * 100.0, 2)
    except OSError:
        disk_free = None
        disk_used_percent = None
    resources = build_resource_status(config, status)
    return {
        "ok": bool(exists),
        "repo_requested": str(arguments.get("repo_path") or ""),
        "repo_resolved": str(repo),
        "repo_exists": exists,
        "git_repo": git_repo,
        "branch": branch,
        "head": head,
        "dirty_status_summary": dirty_summary,
        "upstream_ahead_behind": "",
        "disk_free_bytes": disk_free,
        "disk_used_percent": disk_used_percent,
        "active_workers": resources.get("active_workers"),
        "max_concurrent_jobs": resources.get("max_concurrent_jobs"),
        "free_worker_slots": resources.get("free_worker_slots"),
        "queue_enabled": resources.get("queue_enabled"),
        "unintegrated_worker_warnings": [],
        "error": "" if exists else "repo path does not exist",
    }


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
            public_context = command.get("context") if isinstance(command.get("context"), dict) else {}
            public_context = dict(public_context)
            if command.get("work_group_id"):
                public_context["work_group_id"] = command.get("work_group_id")
            if command.get("lane_id"):
                public_context["lane_id"] = command.get("lane_id")
            context = RequestContext.from_public_metadata(public_context)
            if action == "patchbay_edge_preflight":
                status = await worker_status(self.handler)
                result = edge_preflight(self.config, arguments, status)
            else:
                result = await self.handler.handle_tool_call(action, dict(arguments), context=context)
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
