"""Hub-side fleet registry and command routing."""
from __future__ import annotations

import fnmatch
import secrets
import time
from copy import deepcopy
from typing import Any, Mapping

from patchbay.hub.store import HubStore, generate_secret, token_hash


DEFAULT_HEARTBEAT_STALE_SECONDS = 90


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
