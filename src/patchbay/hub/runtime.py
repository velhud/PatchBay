"""Hub-side fleet registry and command routing."""
from __future__ import annotations

import fnmatch
import secrets
import time
from copy import deepcopy
from typing import Any, Mapping

from patchbay.hub.store import HubStore, generate_secret, token_hash


DEFAULT_HEARTBEAT_STALE_SECONDS = 90
DEFAULT_ROUTING_MIN_DISK_FREE_BYTES = 2_147_483_648
DEFAULT_ROUTING_WEIGHTS = {"worker_ratio": 0.60, "memory_ratio": 0.20, "cpu_ratio": 0.20}


def _clean_id(value: str, *, field: str) -> str:
    cleaned = "".join(ch for ch in str(value or "").strip().lower().replace("_", "-") if ch.isalnum() or ch == "-")
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    if not cleaned:
        raise ValueError(f"{field} is required")
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip("-")
    return cleaned


def _clean_text(value: Any, max_chars: int = 160) -> str:
    return " ".join(str(value or "").split())[:max_chars]


def _clean_tags(values: Any) -> list[str]:
    if isinstance(values, str):
        raw = [part.strip() for part in values.split(",")]
    elif isinstance(values, list):
        raw = [str(part).strip() for part in values]
    else:
        raw = []
    tags: list[str] = []
    for item in raw:
        tag = _clean_id(item, field="tag") if item else ""
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:25]


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def public_machine_view(machine: Mapping[str, Any], *, now: float, stale_seconds: int) -> dict[str, Any]:
    last_seen = float(machine.get("last_seen_at") or 0)
    status = "online" if last_seen and now - last_seen <= stale_seconds else "offline"
    return {
        "machine_id": machine.get("machine_id"),
        "display_name": machine.get("display_name"),
        "status": status,
        "tags": list(machine.get("tags") or []),
        "role": machine.get("role") or "",
        "last_seen_at": machine.get("last_seen_at"),
        "last_seen_age_seconds": round(now - last_seen, 3) if last_seen else None,
        "capabilities": deepcopy(machine.get("capabilities") or {}),
        "workspaces": deepcopy(machine.get("workspaces") or []),
        "worker_status": deepcopy(machine.get("worker_status") or {}),
        "resource_status": deepcopy(machine.get("resource_status") or {}),
    }


class HubRuntime:
    """Fleet-level operations exposed through hub MCP tools and edge HTTP APIs."""

    def __init__(self, config: Mapping[str, Any], store: HubStore | None = None):
        self.config = dict(config)
        self.store = store or HubStore(config)

    @property
    def stale_seconds(self) -> int:
        raw = (self.config.get("hub") or {}).get("heartbeat_stale_seconds", DEFAULT_HEARTBEAT_STALE_SECONDS)
        try:
            return max(10, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_HEARTBEAT_STALE_SECONDS

    def routing_settings(self) -> dict[str, Any]:
        hub_config = self.config.get("hub") if isinstance(self.config.get("hub"), Mapping) else {}
        routing_config = hub_config.get("routing") if isinstance(hub_config.get("routing"), Mapping) else {}
        weights_config = routing_config.get("weights") if isinstance(routing_config.get("weights"), Mapping) else {}
        weights: dict[str, float] = {}
        for key, default in DEFAULT_ROUTING_WEIGHTS.items():
            weights[key] = max(0.0, _as_float(weights_config.get(key), default))
        if sum(weights.values()) <= 0:
            weights = dict(DEFAULT_ROUTING_WEIGHTS)
        return {
            "enabled": _as_bool(routing_config.get("enabled"), False),
            "min_disk_free_bytes": max(0, _as_int(routing_config.get("min_disk_free_bytes"), DEFAULT_ROUTING_MIN_DISK_FREE_BYTES)),
            "allow_queue_when_full": _as_bool(routing_config.get("allow_queue_when_full"), False),
            "weights": weights,
        }

    def create_enrollment_code(self, *, name: str, tags: Any = None, ttl_minutes: int = 30) -> dict[str, Any]:
        display_name = _clean_text(name, 120)
        if not display_name:
            raise ValueError("name is required")
        ttl = max(1, min(int(ttl_minutes or 30), 1440))
        code = f"PB-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
        expires_at = time.time() + ttl * 60

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            payload.setdefault("enrollment_codes", {})[code] = {
                "code": code,
                "display_name": display_name,
                "tags": _clean_tags(tags),
                "created_at": time.time(),
                "expires_at": expires_at,
                "used_at": None,
            }
            self.store.append_event(payload, "enrollment_code.created", {"code": code, "display_name": display_name})
            return deepcopy(payload["enrollment_codes"][code])

        return self.store.update(mutate)

    def enroll_machine(
        self,
        *,
        code: str,
        machine_id: str,
        display_name: str,
        tags: Any = None,
        role: str = "",
        capabilities: Mapping[str, Any] | None = None,
        workspaces: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized_code = _clean_text(code, 64).upper()
        node_id = _clean_id(machine_id, field="machine_id")
        name = _clean_text(display_name, 120) or node_id
        node_token = generate_secret("node")
        now = time.time()

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            codes = payload.setdefault("enrollment_codes", {})
            record = codes.get(normalized_code)
            if not record:
                raise ValueError("Unknown enrollment code")
            if record.get("used_at"):
                raise ValueError("Enrollment code was already used")
            if float(record.get("expires_at") or 0) < now:
                raise ValueError("Enrollment code expired")
            record["used_at"] = now
            machine = {
                "machine_id": node_id,
                "display_name": name,
                "tags": _clean_tags(tags) or list(record.get("tags") or []),
                "role": _clean_text(role, 80),
                "created_at": now,
                "updated_at": now,
                "last_seen_at": None,
                "token_hash": token_hash(node_token),
                "capabilities": dict(capabilities or {}),
                "workspaces": [dict(item) for item in (workspaces or [])],
            }
            payload.setdefault("machines", {})[node_id] = machine
            self.store.append_event(payload, "machine.enrolled", {"machine_id": node_id, "display_name": name})
            return {"machine": public_machine_view(machine, now=now, stale_seconds=self.stale_seconds), "node_token": node_token}

        return self.store.update(mutate)

    def authenticate_machine(self, machine_id: str, token: str) -> dict[str, Any]:
        node_id = _clean_id(machine_id, field="machine_id")
        payload = self.store.read()
        machine = payload.get("machines", {}).get(node_id)
        if not machine or machine.get("token_hash") != token_hash(str(token or "")):
            raise ValueError("Unauthorized edge node")
        return machine

    def heartbeat(
        self,
        *,
        machine_id: str,
        token: str,
        capabilities: Mapping[str, Any] | None = None,
        workspaces: list[Mapping[str, Any]] | None = None,
        worker_status: Mapping[str, Any] | None = None,
        resource_status: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        node_id = _clean_id(machine_id, field="machine_id")
        self.authenticate_machine(node_id, token)
        now = time.time()

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            machine = payload["machines"][node_id]
            machine["last_seen_at"] = now
            machine["updated_at"] = now
            if capabilities is not None:
                machine["capabilities"] = dict(capabilities)
            if workspaces is not None:
                machine["workspaces"] = [dict(item) for item in workspaces]
            if worker_status is not None:
                machine["worker_status"] = dict(worker_status)
            if resource_status is not None:
                machine["resource_status"] = dict(resource_status)
            self.store.append_event(payload, "machine.heartbeat", {"machine_id": node_id})
            return {"accepted": True, "machine": public_machine_view(machine, now=now, stale_seconds=self.stale_seconds)}

        return self.store.update(mutate)

    def list_machines(self, *, query: str = "", tags: Any = None, include_offline: bool = True) -> dict[str, Any]:
        payload = self.store.read()
        now = time.time()
        wanted_tags = set(_clean_tags(tags))
        query_text = str(query or "").strip().lower()
        machines = []
        for machine in payload.get("machines", {}).values():
            view = public_machine_view(machine, now=now, stale_seconds=self.stale_seconds)
            if not include_offline and view["status"] != "online":
                continue
            if wanted_tags and not wanted_tags.intersection(set(view.get("tags") or [])):
                continue
            haystack = " ".join([str(view.get("machine_id") or ""), str(view.get("display_name") or ""), " ".join(view.get("tags") or [])]).lower()
            if query_text and query_text not in haystack:
                continue
            machines.append(view)
        machines.sort(key=lambda item: (item["status"] != "online", str(item.get("display_name") or "")))
        return {
            "machines": machines,
            "count": len(machines),
            "online_count": sum(1 for machine in machines if machine["status"] == "online"),
            "offline_count": sum(1 for machine in machines if machine["status"] == "offline"),
            "hub_id": payload.get("hub_id"),
        }

    def fleet_status(self) -> dict[str, Any]:
        machines = self.list_machines()["machines"]
        active_workers = []
        for machine in machines:
            worker_status = machine.get("worker_status") if isinstance(machine.get("worker_status"), dict) else {}
            for line in worker_status.get("worker_lines") or []:
                active_workers.append({"machine_id": machine["machine_id"], "status_line": line})
        return {
            "summary": f"{sum(1 for m in machines if m['status'] == 'online')}/{len(machines)} machines online; {len(active_workers)} worker status lines visible",
            "machines": machines,
            "active_workers": active_workers[:50],
            "recommended_next_action": "Choose a machine_id and start or inspect workers.",
        }

    def machine_workspaces(self, *, machine_id: str = "") -> dict[str, Any]:
        payload = self.list_machines()
        machines = payload["machines"]
        if machine_id:
            node_id = _clean_id(machine_id, field="machine_id")
            machines = [machine for machine in machines if machine["machine_id"] == node_id]
        return {
            "machines": [
                {
                    "machine_id": machine["machine_id"],
                    "display_name": machine["display_name"],
                    "status": machine["status"],
                    "workspaces": machine.get("workspaces") or [],
                }
                for machine in machines
            ],
            "count": len(machines),
        }

    def create_command(self, *, machine_id: str, action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        node_id = _clean_id(machine_id, field="machine_id")
        command_id = f"cmd_{secrets.token_hex(10)}"
        now = time.time()

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            if node_id not in payload.get("machines", {}):
                raise ValueError(f"Unknown machine_id: {node_id}")
            command = {
                "command_id": command_id,
                "machine_id": node_id,
                "action": _clean_text(action, 80),
                "arguments": dict(arguments),
                "state": "queued",
                "created_at": now,
                "updated_at": now,
                "claimed_at": None,
                "completed_at": None,
                "result": None,
                "error": "",
            }
            payload.setdefault("commands", {})[command_id] = command
            self.store.append_event(payload, "command.queued", {"command_id": command_id, "machine_id": node_id, "action": action})
            return self.public_command(command)

        return self.store.update(mutate)

    def recommend_machine(self, *, required_tags: Any = None) -> dict[str, Any]:
        settings = self.routing_settings()
        wanted_tags = set(_clean_tags(required_tags))
        routing_meta = {
            "enabled": settings["enabled"],
            "required_tags": sorted(wanted_tags),
            "min_disk_free_bytes": settings["min_disk_free_bytes"],
            "allow_queue_when_full": settings["allow_queue_when_full"],
            "weights": settings["weights"],
            "policy": "availability_only",
        }
        if not settings["enabled"]:
            return {
                "enabled": False,
                "selected_machine_id": "",
                "selected_machine": None,
                "ranked_candidates": [],
                "rejected_candidates": [],
                "routing": routing_meta,
                "recommended_next_action": "Hub availability routing is disabled. Use explicit machine_id with patchbay_worker_start.",
            }

        ranked: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        machines = self.list_machines(include_offline=True)["machines"]
        for machine in machines:
            candidate = self._routing_candidate(machine, settings=settings, required_tags=wanted_tags)
            if candidate.get("eligible"):
                ranked.append(candidate)
            else:
                rejected.append(candidate)

        ranked.sort(
            key=lambda item: (
                _as_float(item.get("score"), 999.0),
                -_as_int(item.get("free_worker_slots"), 0),
                -_as_int(item.get("memory_available_bytes"), 0),
                str(item.get("machine_id") or ""),
            )
        )
        selected = ranked[0] if ranked else None
        return {
            "enabled": True,
            "selected_machine_id": selected.get("machine_id") if selected else "",
            "selected_machine": deepcopy(selected.get("machine")) if selected else None,
            "ranked_candidates": ranked,
            "rejected_candidates": rejected,
            "routing": routing_meta,
            "recommended_next_action": (
                f"Start the worker on {selected.get('machine_id')} or pass an explicit machine_id to override auto-routing."
                if selected
                else "No eligible online machine is currently available. Use an explicit machine_id only if you intend to override routing."
            ),
        }

    def _routing_candidate(self, machine: Mapping[str, Any], *, settings: Mapping[str, Any], required_tags: set[str]) -> dict[str, Any]:
        machine_id = str(machine.get("machine_id") or "")
        capabilities = machine.get("capabilities") if isinstance(machine.get("capabilities"), Mapping) else {}
        resources = machine.get("resource_status") if isinstance(machine.get("resource_status"), Mapping) else {}
        tags = set(machine.get("tags") or [])
        reasons: list[str] = []
        if machine.get("status") != "online":
            reasons.append("offline")
        if not bool(capabilities.get("codex_worker_tools")):
            reasons.append("codex worker tools unavailable")
        if required_tags and not required_tags.issubset(tags):
            reasons.append(f"missing required tags: {', '.join(sorted(required_tags - tags))}")

        active_workers = max(0, _as_int(resources.get("active_workers"), 0))
        max_jobs = max(0, _as_int(resources.get("max_concurrent_jobs"), _as_int(capabilities.get("max_concurrent_jobs"), 0)))
        queue_enabled = _as_bool(resources.get("queue_enabled"), _as_bool(capabilities.get("queue_enabled"), False))
        free_slots = max(0, _as_int(resources.get("free_worker_slots"), max(0, max_jobs - active_workers) if max_jobs else 0))
        has_free_slot = free_slots > 0 or max_jobs == 0
        if not has_free_slot and not (queue_enabled and bool(settings.get("allow_queue_when_full"))):
            reasons.append("no free worker slot")

        disk_free_bytes = resources.get("disk_free_bytes")
        disk_free = _as_int(disk_free_bytes, -1) if disk_free_bytes is not None else None
        min_disk = _as_int(settings.get("min_disk_free_bytes"), DEFAULT_ROUTING_MIN_DISK_FREE_BYTES)
        if disk_free is not None and disk_free < min_disk:
            reasons.append("disk free below routing minimum")

        worker_ratio = active_workers / max_jobs if max_jobs > 0 else (1.0 if active_workers else 0.0)
        worker_ratio = max(0.0, min(worker_ratio, 1.0))
        memory_percent = max(0.0, min(_as_float(resources.get("memory_used_percent"), 0.0), 100.0))
        cpu_percent = max(0.0, min(_as_float(resources.get("cpu_percent"), 0.0), 100.0))
        disk_percent = max(0.0, min(_as_float(resources.get("disk_used_percent"), 0.0), 100.0))
        weights = settings.get("weights") if isinstance(settings.get("weights"), Mapping) else DEFAULT_ROUTING_WEIGHTS
        score = (
            _as_float(weights.get("worker_ratio"), DEFAULT_ROUTING_WEIGHTS["worker_ratio"]) * worker_ratio
            + _as_float(weights.get("memory_ratio"), DEFAULT_ROUTING_WEIGHTS["memory_ratio"]) * (memory_percent / 100.0)
            + _as_float(weights.get("cpu_ratio"), DEFAULT_ROUTING_WEIGHTS["cpu_ratio"]) * (cpu_percent / 100.0)
        )
        disk_penalty = 0.0
        if disk_percent >= 95.0:
            disk_penalty = 0.25
        elif disk_percent >= 90.0:
            disk_penalty = 0.10
        score += disk_penalty

        return {
            "eligible": not reasons,
            "machine_id": machine_id,
            "display_name": machine.get("display_name") or machine_id,
            "score": round(score, 6),
            "score_reasons": {
                "worker_ratio": round(worker_ratio, 6),
                "memory_ratio": round(memory_percent / 100.0, 6),
                "cpu_ratio": round(cpu_percent / 100.0, 6),
                "disk_penalty": disk_penalty,
                "weights": deepcopy(weights),
            },
            "active_workers": active_workers,
            "max_concurrent_jobs": max_jobs,
            "free_worker_slots": free_slots,
            "queue_enabled": queue_enabled,
            "memory_available_bytes": max(0, _as_int(resources.get("memory_available_bytes"), 0)),
            "disk_free_bytes": disk_free,
            "disk_used_percent": disk_percent,
            "rejected_reasons": reasons,
            "machine": deepcopy(machine),
        }

    def claim_next_command(self, *, machine_id: str, token: str) -> dict[str, Any]:
        node_id = _clean_id(machine_id, field="machine_id")
        self.authenticate_machine(node_id, token)
        now = time.time()

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            for command in sorted(payload.get("commands", {}).values(), key=lambda item: float(item.get("created_at") or 0)):
                if command.get("machine_id") == node_id and command.get("state") == "queued":
                    command["state"] = "running"
                    command["claimed_at"] = now
                    command["updated_at"] = now
                    self.store.append_event(payload, "command.claimed", {"command_id": command["command_id"], "machine_id": node_id})
                    return {"command": deepcopy(command)}
            return {"command": None}

        return self.store.update(mutate)

    def finish_command(self, *, machine_id: str, token: str, command_id: str, result: Mapping[str, Any] | None = None, error: str = "") -> dict[str, Any]:
        node_id = _clean_id(machine_id, field="machine_id")
        self.authenticate_machine(node_id, token)
        now = time.time()

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            command = payload.get("commands", {}).get(str(command_id))
            if not command or command.get("machine_id") != node_id:
                raise ValueError("Unknown command for this machine")
            command["state"] = "failed" if error else "completed"
            command["completed_at"] = now
            command["updated_at"] = now
            command["result"] = dict(result or {})
            command["error"] = _clean_text(error, 1000)
            self.store.append_event(payload, "command.finished", {"command_id": command_id, "machine_id": node_id, "state": command["state"]})
            return self.public_command(command)

        return self.store.update(mutate)

    def command_status(self, *, command_id: str = "", machine_id: str = "", state: str = "") -> dict[str, Any]:
        payload = self.store.read()
        commands = []
        for command in payload.get("commands", {}).values():
            if command_id and command.get("command_id") != command_id:
                continue
            if machine_id and command.get("machine_id") != _clean_id(machine_id, field="machine_id"):
                continue
            if state and command.get("state") != state:
                continue
            commands.append(self.public_command(command))
        commands.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return {"commands": commands[:50], "count": len(commands)}

    def public_command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        result = command.get("result")
        return {
            "command_id": command.get("command_id"),
            "machine_id": command.get("machine_id"),
            "action": command.get("action"),
            "state": command.get("state"),
            "created_at": command.get("created_at"),
            "updated_at": command.get("updated_at"),
            "completed_at": command.get("completed_at"),
            "error": command.get("error") or "",
            "result": deepcopy(result) if isinstance(result, dict) else result,
        }

    def find_workspace_machines(self, query: str) -> dict[str, Any]:
        needle = str(query or "").strip()
        if not needle:
            return {"matches": [], "count": 0}
        matches = []
        for machine in self.list_machines()["machines"]:
            for workspace in machine.get("workspaces") or []:
                text = " ".join(str(workspace.get(key) or "") for key in ("alias", "repo_name", "root", "branch"))
                if fnmatch.fnmatch(text.lower(), f"*{needle.lower()}*"):
                    matches.append({"machine_id": machine["machine_id"], "display_name": machine["display_name"], "workspace": workspace})
        return {"matches": matches, "count": len(matches)}
