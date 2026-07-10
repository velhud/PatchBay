"""PatchBay Edge runtime for machines connected to an optional Hub."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import secrets
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


EDGE_PROFILE_VERSION = 2
EDGE_PROTOCOL_VERSION = "2"
_DEFAULT_MAX_BACKGROUND_COMMANDS = 4
_MAX_BACKGROUND_COMMANDS = 64


def _new_edge_generation() -> str:
    return f"edgegen_{secrets.token_hex(12)}"


def _ensure_edge_generation(profile: dict[str, Any]) -> bool:
    if str(profile.get("edge_generation") or "").strip():
        return False
    profile["edge_generation"] = _new_edge_generation()
    return True


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
    capabilities = {
        "codex_worker_tools": True,
        "max_concurrent_jobs": server_config.get("max_concurrent_jobs"),
        "queue_enabled": bool(server_config.get("queue_enabled", False)),
        "default_sandbox": security_config.get("default_sandbox"),
        "direct_write": bool(power_config.get("direct_write", False)),
        "bash_mode": power_config.get("bash_mode", "off"),
    }
    capabilities.update(_hub_v2_contract_capabilities())
    return capabilities


def _hub_v2_contract_capabilities() -> dict[str, Any]:
    fields: dict[str, Any] = {
        "protocol_version": EDGE_PROTOCOL_VERSION,
        "contract_version": "",
        "manifest_hash": "",
        "schema_hash": "",
        "contract_hash": "",
        "action_capability_version": "",
        "action_capabilities": {},
        "action_capability_versions": {},
    }
    try:
        tool_surface = importlib.import_module("patchbay.hub.tool_surface")
        action_map = getattr(tool_surface, "HUB_V2_EDGE_ACTION_MAP")
        action_specs = getattr(tool_surface, "HUB_V2_ACTION_SPECS")
        default_action_version = str(getattr(tool_surface, "HUB_V2_ACTION_CAPABILITY_VERSION"))
        action_capabilities: dict[str, str] = {}
        for public_name, action in dict(action_map).items():
            action_name = str(action or "").strip()
            if not action_name:
                continue
            spec = action_specs.get(public_name) if isinstance(action_specs, Mapping) else None
            version = spec.get("capability_version") if isinstance(spec, Mapping) else default_action_version
            action_capabilities[action_name] = str(version or default_action_version)
        for spec in action_specs.values():
            if not isinstance(spec, Mapping):
                continue
            version = str(spec.get("capability_version") or default_action_version)
            view_actions = spec.get("view_actions")
            if isinstance(view_actions, Mapping):
                for action in view_actions.values():
                    action_name = str(action or "").strip()
                    if action_name:
                        action_capabilities[action_name] = version
        fields.update(
            {
                "contract_version": str(getattr(tool_surface, "HUB_V2_CONTRACT_VERSION")),
                "manifest_hash": str(getattr(tool_surface, "HUB_V2_MANIFEST_HASH")),
                "schema_hash": str(getattr(tool_surface, "HUB_V2_SCHEMA_HASH")),
                "contract_hash": str(getattr(tool_surface, "HUB_V2_CONTRACT_HASH")),
                "action_capability_version": default_action_version,
                "action_capabilities": dict(sorted(action_capabilities.items())),
                "action_capability_versions": dict(sorted(action_capabilities.items())),
            }
        )
    except Exception:
        # WP-00 may be landing concurrently. Empty contract fields advertise an
        # incompatible Edge without breaking V1 enrollment or heartbeats.
        pass
    return fields


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
        return sum(
            1
            for worker in workers
            if isinstance(worker, Mapping)
            and str(worker.get("turn_state") or worker.get("state") or "")
            in {"starting", "working"}
        )
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


def _is_windows_host_mount(path: Path) -> bool:
    try:
        if not os.path.ismount(path):
            return False
    except OSError:
        return False
    mounts = Path("/proc/mounts")
    if not mounts.exists():
        return True
    try:
        lines = mounts.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    mount_path = str(path)
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == mount_path:
            return parts[2].lower() in {"drvfs", "9p"}
    return False


def _windows_host_disk_status() -> dict[str, Any]:
    if not _is_wsl():
        return {}
    mount = Path("/mnt/c")
    if mount.exists() and _is_windows_host_mount(mount):
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
    machine = result.get("machine") if isinstance(result.get("machine"), Mapping) else {}
    edge_generation = str(result.get("edge_generation") or machine.get("edge_generation") or "").strip()
    profile = {
        "hub_url": normalize_hub_url(hub_url),
        "machine_id": machine_id,
        "display_name": display_name,
        "tags": tags or [],
        "role": role,
        "node_token": token,
        "enrolled_at": time.time(),
    }
    if edge_generation:
        profile["edge_generation"] = edge_generation
    else:
        _ensure_edge_generation(profile)
    profile_path = save_edge_profile(profile, environ=environ)
    return {"profile_path": profile_path, "profile": public_edge_profile(profile), "machine": result.get("machine")}


class EdgeRunner:
    """Long-running edge loop that reuses one local ToolHandler."""

    def __init__(self, config: dict[str, Any], profile: Mapping[str, Any] | None = None):
        self.config = config
        self._persist_profile = profile is None
        self.profile = dict(profile or load_edge_profile())
        if not self.profile:
            raise ValueError("No edge profile found. Run `patchbay edge enroll` first.")
        profile_changed = _ensure_edge_generation(self.profile)
        self._edge_generation = str(self.profile["edge_generation"])
        self._projection_revision = max(0, _as_int(self.profile.get("projection_revision"), 0))
        self.profile["projection_revision"] = self._projection_revision
        if profile_changed and self._persist_profile:
            save_edge_profile(self.profile)
        self.manager = JobManager(config)
        self.executor = JobExecutor(config, self.manager)
        self.handler = ToolHandler(config, self.manager, self.executor)
        self._command_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._target_lock_users: dict[str, int] = {}
        self._background_errors: list[str] = []
        self._command_task_limit = self._max_background_commands()

    @property
    def hub_url(self) -> str:
        return str(self.profile.get("hub_url") or "")

    @property
    def machine_id(self) -> str:
        return str(self.profile.get("machine_id") or "")

    @property
    def token(self) -> str:
        return str(self.profile.get("node_token") or "")

    @property
    def edge_generation(self) -> str:
        return self._edge_generation

    @property
    def projection_revision(self) -> int:
        return self._projection_revision

    @property
    def background_errors(self) -> tuple[str, ...]:
        return tuple(self._background_errors)

    def _max_background_commands(self) -> int:
        hub_config = self.config.get("hub") if isinstance(self.config.get("hub"), Mapping) else {}
        edge_config = hub_config.get("edge") if isinstance(hub_config.get("edge"), Mapping) else {}
        server_config = self.config.get("server") if isinstance(self.config.get("server"), Mapping) else {}
        configured = edge_config.get("max_concurrent_commands")
        if configured is None:
            configured = server_config.get("max_concurrent_jobs")
        limit = _as_int(configured, _DEFAULT_MAX_BACKGROUND_COMMANDS)
        return max(1, min(limit, _MAX_BACKGROUND_COMMANDS))

    def _advance_projection_revision(self) -> int:
        self._projection_revision += 1
        self.profile["edge_generation"] = self._edge_generation
        self.profile["projection_revision"] = self._projection_revision
        if self._persist_profile:
            save_edge_profile(self.profile)
        return self._projection_revision

    async def heartbeat(self) -> dict[str, Any]:
        projection_revision = self._advance_projection_revision()
        status = await worker_status(self.handler)
        payload = {
            "machine_id": self.machine_id,
            "edge_generation": self.edge_generation,
            "projection_revision": projection_revision,
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
            {
                "machine_id": self.machine_id,
                "edge_generation": self.edge_generation,
                "projection_revision": self.projection_revision,
            },
            token=self.token,
        )

    async def send_result(self, command_id: str, *, result: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(
            post_json,
            self.hub_url,
            "/edge/result",
            {
                "machine_id": self.machine_id,
                "edge_generation": self.edge_generation,
                "projection_revision": self.projection_revision,
                "command_id": command_id,
                "result": result or {},
                "error": error,
            },
            token=self.token,
        )

    def _command_compatibility_error(self, command: Mapping[str, Any]) -> str:
        requirements: dict[str, Any] = {}
        for key in ("requirements", "required_contract"):
            value = command.get(key)
            if isinstance(value, Mapping):
                requirements.update(value)

        def required(*keys: str) -> str:
            for key in keys:
                value = command.get(key)
                if value not in (None, ""):
                    return str(value)
            for key in keys:
                nested_key = key.removeprefix("required_")
                value = requirements.get(nested_key)
                if value not in (None, ""):
                    return str(value)
            return ""

        capabilities = build_capabilities(self.config)
        comparisons = (
            (
                "protocol_version",
                required("required_protocol_version", "required_protocol"),
                str(capabilities.get("protocol_version") or ""),
            ),
            ("contract_version", required("required_contract_version"), str(capabilities.get("contract_version") or "")),
            (
                "contract_hash",
                required("required_contract_hash", "required_hash"),
                str(capabilities.get("contract_hash") or ""),
            ),
            ("manifest_hash", required("required_manifest_hash"), str(capabilities.get("manifest_hash") or "")),
            ("schema_hash", required("required_schema_hash"), str(capabilities.get("schema_hash") or "")),
            (
                "edge_generation",
                required("required_edge_generation", "required_generation", "edge_generation"),
                self.edge_generation,
            ),
        )
        mismatches = [
            f"{field} requires {expected!r}, edge has {actual!r}"
            for field, expected, actual in comparisons
            if expected and expected != actual
        ]

        action = str(command.get("action") or "")
        required_action_version = required("required_action_capability_version")
        required_action_versions = command.get("required_action_capabilities")
        if not isinstance(required_action_versions, Mapping):
            required_action_versions = requirements.get("action_capabilities")
        if not required_action_version and isinstance(required_action_versions, Mapping):
            required_action_version = str(required_action_versions.get(action) or "")
        if required_action_version:
            action_capabilities = capabilities.get("action_capabilities")
            actual_action_version = (
                str(action_capabilities.get(action) or "") if isinstance(action_capabilities, Mapping) else ""
            )
            if required_action_version != actual_action_version:
                mismatches.append(
                    f"action capability {action!r} requires {required_action_version!r}, edge has {actual_action_version!r}"
                )
        return "; ".join(mismatches)

    async def execute_command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        command_id = str(command.get("command_id") or "")
        action = str(command.get("action") or "")
        arguments = command.get("arguments") if isinstance(command.get("arguments"), dict) else {}
        if not command_id or not action:
            return {"skipped": True, "reason": "No command claimed"}
        try:
            compatibility_error = self._command_compatibility_error(command)
            if compatibility_error:
                return await self.send_result(
                    command_id,
                    result={
                        "status": "blocked",
                        "reason": "incompatible_edge_contract",
                        "details": compatibility_error,
                    },
                    error=f"incompatible_edge_contract: {compatibility_error}",
                )
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

    def _command_target_key(self, command: Mapping[str, Any]) -> str:
        for key in ("target_ref", "fleet_worker_ref", "operation_target"):
            value = command.get(key)
            if value:
                return f"target:{value}"
        target = command.get("target")
        if isinstance(target, Mapping):
            for key in ("fleet_worker_ref", "worker", "worker_id", "name", "repo_path", "workspace_ref"):
                value = target.get(key)
                if value:
                    return f"{key}:{value}"
        elif target:
            return f"target:{target}"

        action = str(command.get("action") or "")
        arguments = command.get("arguments") if isinstance(command.get("arguments"), Mapping) else {}
        if action in {
            "codex_worker_message",
            "codex_worker_stop",
            "codex_worker_integrate",
            "codex_worker_cleanup",
        }:
            for key in ("fleet_worker_ref", "worker", "worker_id"):
                value = arguments.get(key)
                if value:
                    return f"{key}:{value}"
        if action == "codex_worker_start" and arguments.get("name"):
            return f"worker_name:{arguments.get('repo_path') or ''}:{arguments['name']}"
        return ""

    async def _execute_claimed_command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        target_key = self._command_target_key(command)
        if not target_key:
            return await self.execute_command(command)
        lock = self._target_locks.setdefault(target_key, asyncio.Lock())
        self._target_lock_users[target_key] = self._target_lock_users.get(target_key, 0) + 1
        try:
            async with lock:
                return await self.execute_command(command)
        finally:
            remaining = self._target_lock_users.get(target_key, 1) - 1
            if remaining > 0:
                self._target_lock_users[target_key] = remaining
            else:
                self._target_lock_users.pop(target_key, None)
                if self._target_locks.get(target_key) is lock:
                    self._target_locks.pop(target_key, None)

    def _collect_command_task(self, task: asyncio.Task[dict[str, Any]]) -> None:
        self._command_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as error:
            self._background_errors.append(str(error))

    def _schedule_command(self, command: Mapping[str, Any]) -> bool:
        if len(self._command_tasks) >= self._command_task_limit:
            return False
        command_id = str(command.get("command_id") or "unknown")
        task = asyncio.create_task(self._execute_claimed_command(command), name=f"patchbay-edge-{command_id}")
        self._command_tasks.add(task)
        task.add_done_callback(self._collect_command_task)
        return True

    async def _heartbeat_loop(self, interval: float) -> None:
        while True:
            await self.heartbeat()
            await asyncio.sleep(interval)

    async def _poll_loop(self, interval: float) -> None:
        while True:
            poll_result = await self.poll()
            command = poll_result.get("command") if isinstance(poll_result.get("command"), dict) else None
            if command and not self._schedule_command(command):
                command_id = str(command.get("command_id") or "")
                if command_id:
                    await self.send_result(
                        command_id,
                        result={"status": "blocked", "reason": "edge_execution_capacity"},
                        error="edge_execution_capacity: background command limit reached",
                    )
            await asyncio.sleep(interval)

    async def shutdown(self, *, cancel: bool = False, timeout_seconds: float | None = None) -> None:
        tasks = tuple(self._command_tasks)
        if not tasks:
            self._target_locks.clear()
            self._target_lock_users.clear()
            return
        if cancel:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            hub_config = self.config.get("hub") if isinstance(self.config.get("hub"), Mapping) else {}
            edge_config = hub_config.get("edge") if isinstance(hub_config.get("edge"), Mapping) else {}
            timeout = _as_float(
                timeout_seconds if timeout_seconds is not None else edge_config.get("shutdown_timeout_seconds"),
                5.0,
            )
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=max(0.0, timeout))
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._command_tasks.clear()
        self._target_locks.clear()
        self._target_lock_users.clear()

    async def run_once(self) -> dict[str, Any]:
        heartbeat_result = await self.heartbeat()
        poll_result = await self.poll()
        command = poll_result.get("command") if isinstance(poll_result.get("command"), dict) else None
        if not command:
            return {"heartbeat": heartbeat_result, "poll": poll_result, "executed": False}
        result = await self.execute_command(command)
        return {"heartbeat": heartbeat_result, "poll": poll_result, "executed": True, "result": result}

    async def run_loop(self, interval_seconds: float = 5) -> None:
        interval = max(0.01, float(interval_seconds))
        control_tasks = (
            asyncio.create_task(self._heartbeat_loop(interval), name="patchbay-edge-heartbeat"),
            asyncio.create_task(self._poll_loop(interval), name="patchbay-edge-poll"),
        )
        cancelled = False
        try:
            await asyncio.gather(*control_tasks)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            for task in control_tasks:
                task.cancel()
            await asyncio.gather(*control_tasks, return_exceptions=True)
            await self.shutdown(cancel=cancelled)
