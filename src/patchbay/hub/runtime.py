"""Hub-side fleet registry and command routing."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import secrets
import time
from copy import deepcopy
from typing import Any, Mapping

from patchbay.hub.store import HubStore, generate_secret, token_hash
from patchbay.protocol.context import RequestContext


DEFAULT_HEARTBEAT_STALE_SECONDS = 90
DEFAULT_ROUTING_MIN_DISK_FREE_BYTES = 2_147_483_648
DEFAULT_ROUTING_WEIGHTS = {"worker_ratio": 0.60, "memory_ratio": 0.20, "cpu_ratio": 0.20}
GROUP_STATUSES = {"active", "paused", "waiting_for_machine", "blocked", "degraded", "complete", "abandoned", "superseded", "recovery_required"}
LANE_STATUSES = {"planned", "queued", "active", "quiet", "stale", "lost", "idle", "failed", "superseded"}
UNGROUPED_REASONS = {"tiny_check", "operator_requested", "legacy_compat"}


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


def _optional_clean_id(value: Any, *, field: str) -> str:
    if value in (None, ""):
        return ""
    return _clean_id(str(value), field=field)


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


def _clean_machine_ids(values: Any) -> list[str]:
    if isinstance(values, str):
        raw = [part.strip() for part in values.split(",")]
    elif isinstance(values, list):
        raw = [str(part).strip() for part in values]
    else:
        raw = []
    machine_ids: list[str] = []
    for item in raw:
        if not item:
            continue
        machine_id = _clean_id(item, field="machine_id")
        if machine_id not in machine_ids:
            machine_ids.append(machine_id)
    return machine_ids[:50]


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


def _json_hash(value: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(value), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_context(context: RequestContext | None) -> dict[str, Any]:
    if not context:
        return {}
    return context.public_metadata()


def _manager_ref(context: RequestContext | None) -> str:
    if not context:
        return "anonymous"
    return (
        context.chatgpt_session_ref
        or context.owner_ref
        or context.chatgpt_subject_ref
        or context.client_ref
        or "anonymous"
    )


def _argument_summary(action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "codex_worker_start": ("name", "repo_path", "workspace_mode", "model", "reasoning_effort", "auto_suffix"),
        "codex_worker_message": ("worker", "repo_path", "model", "reasoning_effort"),
        "codex_worker_status": ("repo_path", "scope", "active_only"),
        "codex_worker_wait": ("repo_path", "wait_seconds", "scope", "active_only"),
        "codex_worker_inspect": ("worker", "view", "repo_path", "file_path", "max_bytes"),
        "codex_worker_stop": ("worker", "force", "repo_path"),
        "codex_worker_integrate": ("worker", "repo_path", "allow_dirty_base"),
        "codex_worker_options": ("repo_path",),
        "patchbay_edge_preflight": ("repo_path",),
    }.get(action, ())
    return {key: arguments[key] for key in allowed if key in arguments and arguments.get(key) not in (None, "")}


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

    def _machine_view(self, payload: Mapping[str, Any], machine_id: str) -> dict[str, Any] | None:
        machine = payload.get("machines", {}).get(machine_id)
        if not machine:
            return None
        return public_machine_view(machine, now=time.time(), stale_seconds=self.stale_seconds)

    def _workspace_projection_matches(self, machine: Mapping[str, Any], repo_path: str) -> tuple[bool, dict[str, Any] | None]:
        value = str(repo_path or "").strip().lower()
        if not value:
            return True, None
        workspaces = machine.get("workspaces") or []
        if not workspaces:
            return True, None
        for workspace in workspaces:
            haystack = " ".join(
                str(workspace.get(key) or "")
                for key in ("alias", "repo_name", "root", "path", "branch")
            ).lower()
            if value in haystack or any(part and part in haystack for part in value.replace("\\", "/").split("/")[-2:]):
                return True, deepcopy(workspace)
        return False, None

    def _choose_group_machine(
        self,
        payload: Mapping[str, Any],
        *,
        machine_id: str = "",
        repo_path: str = "",
        allowed_machine_ids: Any = None,
        required_tags: Any = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        allowed = set(_clean_machine_ids(allowed_machine_ids))
        if machine_id:
            node_id = _clean_id(machine_id, field="machine_id")
            if allowed and node_id not in allowed:
                raise ValueError("machine_id is not included in allowed_machine_ids")
            machine = self._machine_view(payload, node_id)
            if not machine:
                raise ValueError(f"Unknown machine_id: {node_id}")
            if machine.get("status") != "online":
                raise ValueError(f"Machine {node_id} is not online")
            matches, workspace = self._workspace_projection_matches(machine, repo_path)
            if not matches:
                raise ValueError(f"Machine {node_id} does not advertise workspace {repo_path!r}")
            return node_id, machine, {"mode": "explicit_machine", "workspace": workspace}

        recommendation = self.recommend_machine(required_tags=required_tags, allowed_machine_ids=sorted(allowed), repo_path=repo_path)
        selected = str(recommendation.get("selected_machine_id") or "")
        if not selected:
            raise ValueError("No eligible machine is available for this work group; pass explicit machine_id or adjust allowed machines")
        return selected, recommendation.get("selected_machine") or {}, {"mode": "availability", "recommendation": recommendation}

    def _group_public(self, group: Mapping[str, Any], *, include_private: bool = False) -> dict[str, Any]:
        data = {
            "work_group_id": group.get("work_group_id"),
            "title": group.get("title"),
            "goal": group.get("goal"),
            "status": group.get("status"),
            "visibility": group.get("visibility"),
            "routing_policy": group.get("routing_policy"),
            "repo_path": group.get("repo_path") or "",
            "pinned_machine_id": group.get("pinned_machine_id") or "",
            "created_at": group.get("created_at"),
            "updated_at": group.get("updated_at"),
            "last_activity_at": group.get("last_activity_at"),
            "lanes": deepcopy(group.get("lanes") or {}),
            "preflight": deepcopy(group.get("preflight") or {}),
            "worker_refs": deepcopy(group.get("worker_refs") or []),
            "command_ids": list(group.get("command_ids") or []),
            "superseded_by": group.get("superseded_by") or "",
            "supersedes": group.get("supersedes") or "",
        }
        if include_private:
            data["manager_refs"] = list(group.get("manager_refs") or [])
            data["idempotency_key_hash"] = group.get("idempotency_key_hash") or ""
        return data

    def _current_group_id(self, payload: Mapping[str, Any], context: RequestContext | None) -> str:
        return str(payload.get("current_work_group_by_manager", {}).get(_manager_ref(context)) or "")

    def _normalize_lanes(self, lanes: Any) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        if isinstance(lanes, str):
            lanes = [lanes]
        if isinstance(lanes, list):
            for item in lanes:
                if isinstance(item, Mapping):
                    lane_id = _clean_id(item.get("lane_id") or item.get("name") or item.get("title") or "lane", field="lane")
                    title = _clean_text(item.get("title") or item.get("name") or lane_id, 80)
                    role = _clean_text(item.get("role") or "", 120)
                else:
                    lane_id = _clean_id(item, field="lane")
                    title = _clean_text(item, 80)
                    role = ""
                normalized[lane_id] = {"lane_id": lane_id, "title": title, "role": role, "status": "planned", "worker_refs": [], "command_ids": []}
        if not normalized:
            normalized["main"] = {"lane_id": "main", "title": "main", "role": "", "status": "planned", "worker_refs": [], "command_ids": []}
        return normalized

    def _group_preflight_arguments(self, group: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "work_group_id": group.get("work_group_id"),
            "repo_path": group.get("repo_path") or "",
            "expected_branch": group.get("expected_branch") or "",
        }

    def _group_visibility_allows_details(self, group: Mapping[str, Any], context: RequestContext | None) -> bool:
        if str(group.get("visibility") or "private") == "shared":
            return True
        return _manager_ref(context) in set(group.get("manager_refs") or [])

    def _similar_active_groups(self, payload: Mapping[str, Any], *, repo_path: str, title: str, manager_ref: str) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        title_text = _clean_text(title, 120).lower()
        repo_text = _clean_text(repo_path, 400)
        for group in payload.get("work_groups", {}).values():
            if group.get("status") in {"complete", "abandoned", "superseded"}:
                continue
            same_repo = bool(repo_text) and str(group.get("repo_path") or "") == repo_text
            same_title = bool(title_text) and str(group.get("title") or "").lower() == title_text
            if not (same_repo or same_title):
                continue
            if str(group.get("visibility") or "private") == "private" and manager_ref not in set(group.get("manager_refs") or []):
                warnings.append(
                    {
                        "work_group_id": group.get("work_group_id"),
                        "status": group.get("status"),
                        "repo_path": group.get("repo_path") or "",
                        "pinned_machine_id": group.get("pinned_machine_id") or "",
                        "detail": "private_group_visible_as_collision_warning_only",
                    }
                )
            else:
                warnings.append(self._group_public(group))
        return warnings[:10]

    def create_work_group(
        self,
        *,
        title: str,
        goal: str,
        repo_path: str = "",
        machine_id: str = "",
        allowed_machine_ids: Any = None,
        lanes: Any = None,
        visibility: str = "",
        idempotency_key: str = "",
        routing_policy: str = "",
        make_current: bool = True,
        context: RequestContext | None = None,
        required_tags: Any = None,
    ) -> dict[str, Any]:
        title_text = _clean_text(title, 160)
        goal_text = _clean_text(goal, 2000)
        if not title_text:
            raise ValueError("title is required")
        if not goal_text:
            raise ValueError("goal is required")
        visibility_value = _clean_id(visibility or "private", field="visibility")
        if visibility_value not in {"private", "shared"}:
            raise ValueError("visibility must be private or shared")
        routing_value = _clean_id(routing_policy or "keep_together", field="routing_policy")
        if routing_value not in {"keep-together", "distributed-report-only"}:
            raise ValueError("routing_policy must be keep_together or distributed_report_only")
        routing_value = routing_value.replace("-", "_")
        if routing_value != "keep_together":
            raise ValueError("distributed_report_only is reserved for a later Hub release")
        manager_ref = _manager_ref(context)
        idem_hash = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest() if idempotency_key else ""

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            groups = payload.setdefault("work_groups", {})
            if idem_hash:
                for group in groups.values():
                    if group.get("idempotency_key_hash") == idem_hash:
                        if make_current:
                            payload.setdefault("current_work_group_by_manager", {})[manager_ref] = group["work_group_id"]
                        return {
                            "accepted": True,
                            "idempotent_replay": True,
                            "work_group": self._group_public(group, include_private=self._group_visibility_allows_details(group, context)),
                            "recommended_next_action": "Resume this existing work group instead of creating a duplicate.",
                        }

            selected_machine_id, selected_machine, routing_meta = self._choose_group_machine(
                payload,
                machine_id=machine_id,
                repo_path=repo_path,
                allowed_machine_ids=allowed_machine_ids,
                required_tags=required_tags,
            )
            now = time.time()
            group_id = f"grp_{secrets.token_hex(8)}"
            group = {
                "work_group_id": group_id,
                "title": title_text,
                "goal": goal_text,
                "status": "active",
                "visibility": visibility_value,
                "routing_policy": routing_value,
                "repo_path": _clean_text(repo_path, 400),
                "pinned_machine_id": selected_machine_id,
                "allowed_machine_ids": _clean_machine_ids(allowed_machine_ids),
                "required_tags": _clean_tags(required_tags),
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "manager_refs": [manager_ref],
                "lanes": self._normalize_lanes(lanes),
                "worker_refs": [],
                "command_ids": [],
                "preflight": {"status": "pending", "command_id": "", "result": None, "error": "", "updated_at": now},
                "idempotency_key_hash": idem_hash,
                "routing": routing_meta,
                "superseded_by": "",
                "supersedes": "",
                "reassignment_history": [],
            }
            groups[group_id] = group
            if make_current:
                payload.setdefault("current_work_group_by_manager", {})[manager_ref] = group_id
            self.store.append_event(
                payload,
                "work_group.created",
                {"work_group_id": group_id, "machine_id": selected_machine_id, "manager_ref": manager_ref},
            )
            preflight = self._queue_command_record(
                payload,
                machine_id=selected_machine_id,
                action="patchbay_edge_preflight",
                arguments=self._group_preflight_arguments(group),
                context=context,
                work_group_id=group_id,
                routing={"reason": "group_create_preflight"},
                internal=True,
            )
            group["preflight"]["command_id"] = preflight["command_id"]
            warnings = self._similar_active_groups(payload, repo_path=group["repo_path"], title=title_text, manager_ref=manager_ref)
            return {
                "accepted": True,
                "idempotent_replay": False,
                "work_group": self._group_public(group, include_private=True),
                "selected_machine": selected_machine,
                "preflight_command": self.public_command(preflight),
                "similar_active_group_warnings": [item for item in warnings if item.get("work_group_id") != group_id],
                "recommended_next_action": "Wait for preflight to complete, then start workers inside lanes for this work group.",
            }

        return self.store.update(mutate)

    def list_work_groups(
        self,
        *,
        scope: str = "current",
        status: str = "",
        repo_path: str = "",
        machine_id: str = "",
        include_closed: bool = False,
        query: str = "",
        limit: int = 20,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        payload = self.store.read()
        manager_ref = _manager_ref(context)
        scope_value = _clean_id(scope or "current", field="scope")
        wanted_status = _clean_id(status, field="status") if status else ""
        closed = {"complete", "abandoned", "superseded"}
        current_id = self._current_group_id(payload, context)
        query_text = str(query or "").strip().lower()
        repo_text = _clean_text(repo_path, 400)
        machine_filter = _optional_clean_id(machine_id, field="machine_id")
        groups: list[dict[str, Any]] = []
        hidden_private = 0
        hidden_closed = 0
        for group in payload.get("work_groups", {}).values():
            group_status = str(group.get("status") or "")
            if wanted_status and group_status != wanted_status:
                continue
            if not include_closed and group_status in closed:
                hidden_closed += 1
                continue
            if repo_text and str(group.get("repo_path") or "") != repo_text:
                continue
            if machine_filter and str(group.get("pinned_machine_id") or "") != machine_filter:
                continue
            if query_text:
                haystack = " ".join([str(group.get("title") or ""), str(group.get("goal") or ""), str(group.get("repo_path") or "")]).lower()
                if query_text not in haystack:
                    continue
            visible = self._group_visibility_allows_details(group, context)
            if scope_value == "current" and group.get("work_group_id") != current_id:
                continue
            if scope_value == "owned" and manager_ref not in set(group.get("manager_refs") or []):
                continue
            if not visible:
                hidden_private += 1
                continue
            groups.append(self._group_public(group, include_private=visible))
        if scope_value == "current" and not groups:
            recent = self.list_work_groups(
                scope="owned",
                status=status,
                repo_path=repo_path,
                machine_id=machine_id,
                include_closed=include_closed,
                query=query,
                limit=limit,
                context=context,
            )
            recent["current_work_group_id"] = current_id
            recent["recommended_next_action"] = "No current work group is selected. Resume one listed group or create a new group."
            return recent
        groups.sort(key=lambda item: float(item.get("last_activity_at") or item.get("created_at") or 0), reverse=True)
        bounded_limit = max(1, min(int(limit or 20), 100))
        return {
            "groups": groups[:bounded_limit],
            "count": len(groups),
            "hidden_private_count": hidden_private,
            "hidden_closed_count": hidden_closed,
            "current_work_group_id": current_id,
            "recommended_next_action": "Resume an existing active group or create one group for the new task.",
        }

    def work_group_status(self, *, work_group_id: str = "", context: RequestContext | None = None) -> dict[str, Any]:
        payload = self.store.read()
        group_id = str(work_group_id or self._current_group_id(payload, context) or "")
        if not group_id:
            return {
                "found": False,
                "current_work_group_id": "",
                "recommended_next_action": "Call patchbay_work_group_list, resume an existing group, or create a new group before starting workers.",
            }
        group = payload.get("work_groups", {}).get(group_id)
        if not group:
            raise ValueError(f"Unknown work_group_id: {group_id}")
        include_private = self._group_visibility_allows_details(group, context)
        if not include_private:
            return {
                "found": True,
                "work_group_id": group_id,
                "visibility": group.get("visibility"),
                "status": group.get("status"),
                "detail": "private_group_visible_as_collision_warning_only",
            }
        command_ids = list(group.get("command_ids") or [])
        commands = [self.public_command(payload.get("commands", {}).get(command_id, {})) for command_id in command_ids if command_id in payload.get("commands", {})]
        active_commands = [cmd for cmd in commands if cmd.get("state") in {"queued", "running"}]
        counts = {
            "queued_commands": sum(1 for cmd in commands if cmd.get("state") == "queued"),
            "running_commands": sum(1 for cmd in commands if cmd.get("state") == "running"),
            "completed_commands": sum(1 for cmd in commands if cmd.get("state") == "completed"),
            "failed_commands": sum(1 for cmd in commands if cmd.get("state") == "failed"),
            "active_lanes": sum(1 for lane in (group.get("lanes") or {}).values() if lane.get("status") in {"queued", "active", "quiet", "stale"}),
            "lost_lanes": sum(1 for lane in (group.get("lanes") or {}).values() if lane.get("status") == "lost"),
            "stale_lanes": sum(1 for lane in (group.get("lanes") or {}).values() if lane.get("status") == "stale"),
            "worker_refs": len(group.get("worker_refs") or []),
        }
        machine = self._machine_view(payload, str(group.get("pinned_machine_id") or ""))
        recommendation = self.recommend_machine(work_group_id=group_id)
        next_action = "Start or continue workers inside lanes."
        if group.get("preflight", {}).get("status") == "pending":
            next_action = "Wait for group preflight to complete before starting workers."
        elif group.get("preflight", {}).get("status") == "failed":
            next_action = "Fix or override failed preflight before starting workers."
        elif active_commands:
            next_action = "Use group status or command status after the recommended wait interval; do not rapid-poll."
        if recommendation.get("blocked_reason"):
            next_action = "Pinned machine is unavailable or blocked; wait or explicitly reassign the group."
        return {
            "found": True,
            "work_group": self._group_public(group, include_private=True),
            "pinned_machine": machine,
            "counts": counts,
            "commands": commands[-50:],
            "active_commands": active_commands,
            "machine_recommendation": recommendation,
            "recommended_poll_seconds": 20,
            "recommended_next_action": next_action,
        }

    def resume_work_group(
        self,
        *,
        work_group_id: str,
        takeover: bool = False,
        takeover_reason: str = "",
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        group_id = str(work_group_id or "").strip()
        if not group_id:
            raise ValueError("work_group_id is required")
        manager_ref = _manager_ref(context)

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            group = payload.setdefault("work_groups", {}).get(group_id)
            if not group:
                raise ValueError(f"Unknown work_group_id: {group_id}")
            if not self._group_visibility_allows_details(group, context) and not takeover:
                raise ValueError("Private work group belongs to another manager; pass takeover=true with a reason to resume it")
            if group.get("status") in {"complete", "abandoned", "superseded"} and not takeover:
                raise ValueError("Closed or superseded work group cannot be resumed without takeover")
            now = time.time()
            managers = group.setdefault("manager_refs", [])
            if manager_ref not in managers:
                managers.append(manager_ref)
            group["updated_at"] = now
            group["last_activity_at"] = now
            if group.get("status") in {"paused", "waiting_for_machine", "blocked", "degraded", "recovery_required"}:
                group["status"] = "active"
            payload.setdefault("current_work_group_by_manager", {})[manager_ref] = group_id
            group["preflight"] = {"status": "pending", "command_id": "", "result": None, "error": "", "updated_at": now}
            preflight = self._queue_command_record(
                payload,
                machine_id=str(group.get("pinned_machine_id") or ""),
                action="patchbay_edge_preflight",
                arguments=self._group_preflight_arguments(group),
                context=context,
                work_group_id=group_id,
                routing={"reason": "group_resume_preflight", "takeover": bool(takeover), "takeover_reason": _clean_text(takeover_reason, 200)},
                internal=True,
            )
            group["preflight"]["command_id"] = preflight["command_id"]
            self.store.append_event(payload, "work_group.resumed", {"work_group_id": group_id, "manager_ref": manager_ref})
            return {
                "accepted": True,
                "work_group": self._group_public(group, include_private=True),
                "preflight_command": self.public_command(preflight),
                "recommended_next_action": "Wait for preflight to complete, then continue this work group.",
            }

        return self.store.update(mutate)

    def close_work_group(
        self,
        *,
        work_group_id: str,
        outcome: str,
        summary: str,
        force: bool = False,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        group_id = str(work_group_id or "").strip()
        if not group_id:
            raise ValueError("work_group_id is required")
        outcome_text = _clean_text(outcome, 80)
        summary_text = _clean_text(summary, 2000)
        if not outcome_text:
            raise ValueError("outcome is required")
        if not summary_text:
            raise ValueError("summary is required")

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            group = payload.setdefault("work_groups", {}).get(group_id)
            if not group:
                raise ValueError(f"Unknown work_group_id: {group_id}")
            if not self._group_visibility_allows_details(group, context):
                raise ValueError("Private work group belongs to another manager")
            commands = [
                payload.get("commands", {}).get(command_id)
                for command_id in group.get("command_ids") or []
                if command_id in payload.get("commands", {})
            ]
            active = [self.public_command(command) for command in commands if command and command.get("state") in {"queued", "running"}]
            if active and not force:
                return {
                    "accepted": False,
                    "error": "Work group still has active commands or workers.",
                    "active_commands": active,
                    "recommended_next_action": "Wait, inspect, stop workers explicitly, or close with force=true if you are intentionally leaving work active.",
                }
            now = time.time()
            final_status = "complete" if outcome_text.lower() in {"complete", "completed", "success", "done"} else "abandoned"
            group["status"] = final_status
            group["outcome"] = outcome_text
            group["summary"] = summary_text
            group["closed_at"] = now
            group["updated_at"] = now
            self.store.append_event(payload, "work_group.closed", {"work_group_id": group_id, "outcome": outcome_text})
            return {
                "accepted": True,
                "work_group": self._group_public(group, include_private=True),
                "recommended_next_action": "The group is closed. Closing did not stop workers or delete worktrees.",
            }

        return self.store.update(mutate)

    def reassign_work_group(
        self,
        *,
        work_group_id: str,
        machine_id: str = "",
        allowed_machine_ids: Any = None,
        reason: str = "",
        context: RequestContext | None = None,
        required_tags: Any = None,
    ) -> dict[str, Any]:
        group_id = str(work_group_id or "").strip()
        if not group_id:
            raise ValueError("work_group_id is required")
        reason_text = _clean_text(reason, 500)
        if not reason_text:
            raise ValueError("reason is required")

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            group = payload.setdefault("work_groups", {}).get(group_id)
            if not group:
                raise ValueError(f"Unknown work_group_id: {group_id}")
            if not self._group_visibility_allows_details(group, context):
                raise ValueError("Private work group belongs to another manager")
            old_machine = str(group.get("pinned_machine_id") or "")
            new_machine, selected_machine, routing_meta = self._choose_group_machine(
                payload,
                machine_id=machine_id,
                repo_path=str(group.get("repo_path") or ""),
                allowed_machine_ids=allowed_machine_ids or group.get("allowed_machine_ids") or [],
                required_tags=required_tags or group.get("required_tags") or [],
            )
            if new_machine == old_machine:
                raise ValueError("Selected machine is already the pinned machine")
            now = time.time()
            for lane in group.setdefault("lanes", {}).values():
                if lane.get("status") not in {"failed", "superseded"}:
                    lane["status"] = "superseded"
            successor_lane_id = f"successor-{int(now)}"
            group["lanes"][successor_lane_id] = {
                "lane_id": successor_lane_id,
                "title": successor_lane_id,
                "role": "successor work after machine reassign",
                "status": "planned",
                "worker_refs": [],
                "command_ids": [],
            }
            group.setdefault("reassignment_history", []).append(
                {"from_machine_id": old_machine, "to_machine_id": new_machine, "reason": reason_text, "at": now}
            )
            group["pinned_machine_id"] = new_machine
            group["status"] = "active"
            group["updated_at"] = now
            group["last_activity_at"] = now
            group["routing"] = routing_meta
            group["preflight"] = {"status": "pending", "command_id": "", "result": None, "error": "", "updated_at": now}
            preflight = self._queue_command_record(
                payload,
                machine_id=new_machine,
                action="patchbay_edge_preflight",
                arguments=self._group_preflight_arguments(group),
                context=context,
                work_group_id=group_id,
                routing={"reason": "group_reassign_preflight", "old_machine_id": old_machine},
                internal=True,
            )
            group["preflight"]["command_id"] = preflight["command_id"]
            self.store.append_event(payload, "work_group.reassigned", {"work_group_id": group_id, "from": old_machine, "to": new_machine})
            return {
                "accepted": True,
                "work_group": self._group_public(group, include_private=True),
                "selected_machine": selected_machine,
                "successor_lane_id": successor_lane_id,
                "preflight_command": self.public_command(preflight),
                "recommended_next_action": "Start successor workers on the new machine. Live Codex workers were not moved.",
            }

        return self.store.update(mutate)

    def _queue_command_record(
        self,
        payload: dict[str, Any],
        *,
        machine_id: str,
        action: str,
        arguments: Mapping[str, Any],
        context: RequestContext | None = None,
        work_group_id: str = "",
        lane: str = "",
        routing: Mapping[str, Any] | None = None,
        internal: bool = False,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        node_id = _clean_id(machine_id, field="machine_id")
        command_id = f"cmd_{secrets.token_hex(10)}"
        now = time.time()
        manager_ref = _manager_ref(context)
        action_name = _clean_text(action, 80)
        args = dict(arguments)
        command = {
            "command_id": command_id,
            "machine_id": node_id,
            "action": action_name,
            "arguments": args,
            "arguments_hash": _json_hash(args),
            "arguments_summary": _argument_summary(action_name, args),
            "context": _public_context(context),
            "manager_ref": manager_ref,
            "work_group_id": work_group_id,
            "lane_id": lane,
            "routing": deepcopy(dict(routing or {})),
            "internal": bool(internal),
            "idempotency_key_hash": hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest() if idempotency_key else "",
            "state": "queued",
            "created_at": now,
            "updated_at": now,
            "claimed_at": None,
            "lease_expires_at": None,
            "completed_at": None,
            "result": None,
            "error": "",
        }
        payload.setdefault("commands", {})[command_id] = command
        if work_group_id and work_group_id in payload.setdefault("work_groups", {}):
            group = payload["work_groups"][work_group_id]
            group.setdefault("command_ids", []).append(command_id)
            group["updated_at"] = now
            group["last_activity_at"] = now
            if lane:
                lane_record = group.setdefault("lanes", {}).setdefault(
                    lane,
                    {"lane_id": lane, "title": lane, "role": "", "status": "planned", "worker_refs": [], "command_ids": []},
                )
                lane_record.setdefault("command_ids", []).append(command_id)
                if action_name == "codex_worker_start":
                    lane_record["status"] = "queued"
        self.store.append_event(
            payload,
            "command.queued",
            {"command_id": command_id, "machine_id": node_id, "action": action_name, "work_group_id": work_group_id, "lane": lane},
        )
        return command

    def create_command(
        self,
        *,
        machine_id: str,
        action: str,
        arguments: Mapping[str, Any],
        context: RequestContext | None = None,
        work_group_id: str = "",
        lane: str = "",
        routing: Mapping[str, Any] | None = None,
        internal: bool = False,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        node_id = _clean_id(machine_id, field="machine_id")
        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            if node_id not in payload.get("machines", {}):
                raise ValueError(f"Unknown machine_id: {node_id}")
            command = self._queue_command_record(
                payload,
                machine_id=node_id,
                action=action,
                arguments=arguments,
                context=context,
                work_group_id=work_group_id,
                lane=lane,
                routing=routing,
                internal=internal,
                idempotency_key=idempotency_key,
            )
            return self.public_command(command)

        return self.store.update(mutate)

    def queue_worker_command(
        self,
        *,
        machine_id: str = "",
        action: str,
        arguments: Mapping[str, Any],
        context: RequestContext | None = None,
        work_group_id: str = "",
        lane: str = "",
        ungrouped_reason: str = "",
        auto_routing_ok: bool = False,
        required_tags: Any = None,
    ) -> dict[str, Any]:
        action_name = _clean_text(action, 80)
        if not action_name:
            raise ValueError("action is required")
        group_id = str(work_group_id or "").strip()
        lane_id = _optional_clean_id(lane, field="lane")
        control_keys = {
            "machine_id",
            "work_group_id",
            "lane",
            "ungrouped_reason",
            "auto_routing_ok",
            "required_tags",
            "refresh",
            "preflight_override",
        }
        routed_args = {key: value for key, value in dict(arguments).items() if key not in control_keys and value not in (None, "")}
        preflight_override = _as_bool(arguments.get("preflight_override"), False)

        def mutate(payload: dict[str, Any]) -> dict[str, Any]:
            target_machine = _optional_clean_id(machine_id, field="machine_id")
            routing_meta: dict[str, Any] = {}
            if group_id:
                group = payload.setdefault("work_groups", {}).get(group_id)
                if not group:
                    raise ValueError(f"Unknown work_group_id: {group_id}")
                if not self._group_visibility_allows_details(group, context):
                    raise ValueError("Private work group belongs to another manager")
                pinned = str(group.get("pinned_machine_id") or "")
                if target_machine and target_machine != pinned:
                    raise ValueError("Grouped worker command must use the pinned machine unless the group is explicitly reassigned")
                target_machine = pinned
                if action_name == "codex_worker_start":
                    if not lane_id:
                        raise ValueError("lane is required for grouped worker starts")
                    preflight = group.get("preflight") if isinstance(group.get("preflight"), Mapping) else {}
                    if preflight.get("status") != "ok" and not preflight_override:
                        raise ValueError("Work group preflight is not ok; wait for preflight or use preflight_override for operator recovery")
                    recommendation = self.recommend_machine(work_group_id=group_id, required_tags=required_tags or group.get("required_tags") or [])
                    if recommendation.get("blocked_reason"):
                        raise ValueError("Pinned machine is not eligible; wait, free capacity, or reassign the work group")
                    routing_meta = {"policy": "group_keep_together", "work_group_id": group_id, "pinned_machine_id": target_machine}
                lane_record = group.setdefault("lanes", {}).setdefault(
                    lane_id or "main",
                    {"lane_id": lane_id or "main", "title": lane_id or "main", "role": "", "status": "planned", "worker_refs": [], "command_ids": []},
                )
                if action_name in {"codex_worker_message", "codex_worker_wait", "codex_worker_status", "codex_worker_inspect"}:
                    lane_record["status"] = "active"
            elif action_name == "codex_worker_start":
                reason = _clean_id(ungrouped_reason, field="ungrouped_reason").replace("-", "_") if ungrouped_reason else ""
                if reason not in UNGROUPED_REASONS:
                    raise ValueError("Hub worker starts without work_group_id require ungrouped_reason: tiny_check, operator_requested, or legacy_compat")
                if not target_machine:
                    raise ValueError("machine_id is required for ungrouped worker starts")
                routing_meta = {"policy": "explicit_ungrouped", "ungrouped_reason": reason}
            elif not target_machine:
                raise ValueError("machine_id is required for routed worker commands")
            if not target_machine or target_machine not in payload.get("machines", {}):
                raise ValueError(f"Unknown machine_id: {target_machine}")
            context_for_edge = context
            if group_id or lane_id:
                metadata = _public_context(context)
                if group_id:
                    metadata["work_group_id"] = group_id
                if lane_id:
                    metadata["lane_id"] = lane_id
                context_for_edge = RequestContext.from_public_metadata(metadata)
            command = self._queue_command_record(
                payload,
                machine_id=target_machine,
                action=action_name,
                arguments=routed_args,
                context=context_for_edge,
                work_group_id=group_id,
                lane=lane_id,
                routing=routing_meta,
            )
            return self.public_command(command)

        return self.store.update(mutate)

    def queue_auto_worker_start(
        self,
        *,
        arguments: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        group_id = str(arguments.get("work_group_id") or "").strip()
        lane_id = str(arguments.get("lane") or "").strip()
        if not group_id:
            return {
                "accepted": False,
                "error": "patchbay_worker_start_auto requires work_group_id.",
                "recommended_next_action": "Create or resume one work group, then start workers inside lanes.",
            }
        if not lane_id:
            return {
                "accepted": False,
                "error": "patchbay_worker_start_auto requires lane.",
                "recommended_next_action": "Choose a lane name for this worker inside the group.",
            }
        if not _as_bool(arguments.get("auto_routing_ok"), False):
            return {
                "accepted": False,
                "error": "patchbay_worker_start_auto requires auto_routing_ok=true.",
                "recommended_next_action": "Confirm that grouped availability routing is intended for this worker.",
            }
        payload = self.store.read()
        group = payload.get("work_groups", {}).get(group_id)
        if not group:
            return {"accepted": False, "error": f"Unknown work_group_id: {group_id}"}
        command = self.queue_worker_command(
            machine_id=str(group.get("pinned_machine_id") or ""),
            action="codex_worker_start",
            arguments=arguments,
            context=context,
            work_group_id=group_id,
            lane=lane_id,
            auto_routing_ok=True,
            required_tags=arguments.get("required_tags") or [],
        )
        command["accepted"] = True
        command["routing"] = self.recommend_machine(work_group_id=group_id)
        command["note"] = "Command queued on this work group's pinned machine; auto-routing did not scatter workers."
        return command

    def recommend_machine(
        self,
        *,
        required_tags: Any = None,
        allowed_machine_ids: Any = None,
        repo_path: str = "",
        work_group_id: str = "",
    ) -> dict[str, Any]:
        settings = self.routing_settings()
        wanted_tags = set(_clean_tags(required_tags))
        allowed = set(_clean_machine_ids(allowed_machine_ids))
        repo_text = _clean_text(repo_path, 400)
        routing_meta = {
            "enabled": settings["enabled"],
            "required_tags": sorted(wanted_tags),
            "allowed_machine_ids": sorted(allowed),
            "repo_path": repo_text,
            "min_disk_free_bytes": settings["min_disk_free_bytes"],
            "allow_queue_when_full": settings["allow_queue_when_full"],
            "weights": settings["weights"],
            "policy": "availability_only",
        }
        payload = self.store.read()
        group = payload.get("work_groups", {}).get(str(work_group_id or "")) if work_group_id else None
        if group:
            pinned = str(group.get("pinned_machine_id") or "")
            machine = self._machine_view(payload, pinned)
            if not machine:
                return {
                    "enabled": settings["enabled"],
                    "work_group_id": work_group_id,
                    "selected_machine_id": "",
                    "selected_machine": None,
                    "ranked_candidates": [],
                    "rejected_candidates": [{"machine_id": pinned, "rejected_reasons": ["pinned machine is unknown"]}],
                    "blocked_reason": "pinned_machine_unknown",
                    "routing": {**routing_meta, "policy": "group_keep_together"},
                    "recommended_next_action": "Reassign the work group or restore the pinned machine enrollment.",
                }
            candidate = self._routing_candidate(machine, settings=settings, required_tags=wanted_tags or set(group.get("required_tags") or []))
            matches, workspace = self._workspace_projection_matches(machine, str(group.get("repo_path") or repo_text))
            if not matches:
                candidate.setdefault("rejected_reasons", []).append("pinned machine does not advertise this workspace")
            if candidate.get("rejected_reasons"):
                candidate["eligible"] = False
            return {
                "enabled": settings["enabled"],
                "work_group_id": work_group_id,
                "selected_machine_id": pinned if candidate.get("eligible") else "",
                "selected_machine": deepcopy(machine) if candidate.get("eligible") else None,
                "ranked_candidates": [candidate] if candidate.get("eligible") else [],
                "rejected_candidates": [] if candidate.get("eligible") else [candidate],
                "workspace_projection": workspace,
                "blocked_reason": "" if candidate.get("eligible") else "pinned_machine_not_eligible",
                "routing": {**routing_meta, "policy": "group_keep_together", "pinned_machine_id": pinned},
                "recommended_next_action": (
                    f"Use the pinned machine {pinned} for this work group."
                    if candidate.get("eligible")
                    else "Wait for the pinned machine, free capacity, or explicitly reassign the work group."
                ),
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
            if allowed and candidate.get("machine_id") not in allowed:
                candidate.setdefault("rejected_reasons", []).append("machine is not in allowed_machine_ids")
            matches, workspace = self._workspace_projection_matches(machine, repo_text)
            if not matches:
                candidate.setdefault("rejected_reasons", []).append("workspace projection does not match repo_path")
            if workspace:
                candidate["workspace_projection"] = workspace
            if candidate.get("rejected_reasons"):
                candidate["eligible"] = False
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
                f"Create a work group pinned to {selected.get('machine_id')}, or pass an explicit machine_id for a tiny/operator-requested ungrouped start."
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
        role = str(machine.get("role") or "").lower()
        is_wsl_machine = "wsl" in role or any(str(tag).lower() in {"wsl", "wsl2"} for tag in tags)
        disk_confidence = str(resources.get("disk_telemetry_confidence") or "")
        disk_warning = str(resources.get("disk_telemetry_warning") or "")
        if is_wsl_machine and not disk_confidence and disk_free is not None:
            disk_free = None
            disk_confidence = "legacy_wsl_untrusted"
            disk_warning = (
                "Legacy WSL edge did not label disk telemetry. Hub ignored disk_free_bytes because WSL can report "
                "virtual ext4/VHD capacity instead of real Windows-host free space."
            )
        min_disk = _as_int(settings.get("min_disk_free_bytes"), DEFAULT_ROUTING_MIN_DISK_FREE_BYTES)
        if disk_free is not None and disk_free < min_disk:
            reasons.append("disk free below routing minimum")

        worker_ratio = active_workers / max_jobs if max_jobs > 0 else (1.0 if active_workers else 0.0)
        worker_ratio = max(0.0, min(worker_ratio, 1.0))
        memory_percent = max(0.0, min(_as_float(resources.get("memory_used_percent"), 0.0), 100.0))
        cpu_percent = max(0.0, min(_as_float(resources.get("cpu_percent"), 0.0), 100.0))
        disk_percent = max(0.0, min(_as_float(resources.get("disk_used_percent"), 0.0), 100.0))
        cpu_source = str(resources.get("cpu_telemetry_source") or "")
        cpu_confidence = str(resources.get("cpu_telemetry_confidence") or "")
        memory_source = str(resources.get("memory_telemetry_source") or "")
        memory_confidence = str(resources.get("memory_telemetry_confidence") or "")
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
        elif disk_confidence in {"virtualized", "legacy_wsl_untrusted"}:
            disk_penalty = 0.03
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
                "cpu_telemetry_source": cpu_source,
                "cpu_telemetry_confidence": cpu_confidence,
                "memory_telemetry_source": memory_source,
                "memory_telemetry_confidence": memory_confidence,
                "disk_telemetry_confidence": disk_confidence,
                "weights": deepcopy(weights),
            },
            "active_workers": active_workers,
            "max_concurrent_jobs": max_jobs,
            "free_worker_slots": free_slots,
            "queue_enabled": queue_enabled,
            "memory_available_bytes": max(0, _as_int(resources.get("memory_available_bytes"), 0)),
            "memory_total_bytes": max(0, _as_int(resources.get("memory_total_bytes"), 0)),
            "cpu_percent": cpu_percent,
            "cpu_telemetry_source": cpu_source,
            "cpu_telemetry_confidence": cpu_confidence,
            "memory_used_percent": memory_percent,
            "memory_telemetry_source": memory_source,
            "memory_telemetry_confidence": memory_confidence,
            "disk_free_bytes": disk_free,
            "disk_used_percent": disk_percent,
            "disk_telemetry_confidence": disk_confidence,
            "disk_telemetry_warning": disk_warning,
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
                    command["lease_expires_at"] = now + max(30, _as_int((self.config.get("hub") or {}).get("command_lease_seconds"), 300))
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
            work_group_id = str(command.get("work_group_id") or "")
            lane_id = str(command.get("lane_id") or "")
            if work_group_id and work_group_id in payload.setdefault("work_groups", {}):
                group = payload["work_groups"][work_group_id]
                group["updated_at"] = now
                group["last_activity_at"] = now
                if command.get("action") == "patchbay_edge_preflight":
                    group["preflight"] = {
                        "status": "failed" if error else ("ok" if (result or {}).get("ok", True) else "failed"),
                        "command_id": command_id,
                        "result": dict(result or {}),
                        "error": _clean_text(error or str((result or {}).get("error") or ""), 1000),
                        "updated_at": now,
                    }
                    if group["preflight"]["status"] == "failed":
                        group["status"] = "blocked"
                elif command.get("action") == "codex_worker_start":
                    lane = group.setdefault("lanes", {}).setdefault(
                        lane_id or "main",
                        {"lane_id": lane_id or "main", "title": lane_id or "main", "role": "", "status": "planned", "worker_refs": [], "command_ids": []},
                    )
                    if error:
                        lane["status"] = "failed"
                        group["status"] = "degraded" if group.get("status") == "active" else group.get("status")
                    else:
                        lane["status"] = "active"
                        worker_payload = result if isinstance(result, Mapping) else {}
                        worker_id = str(worker_payload.get("worker_id") or worker_payload.get("id") or "")
                        worker_name = str(worker_payload.get("name") or command.get("arguments", {}).get("name") or "")
                        ref = {
                            "machine_id": node_id,
                            "worker_id": worker_id,
                            "name": worker_name,
                            "lane_id": lane_id or "main",
                            "command_id": command_id,
                        }
                        if ref not in group.setdefault("worker_refs", []):
                            group["worker_refs"].append(ref)
                        if ref not in lane.setdefault("worker_refs", []):
                            lane["worker_refs"].append(ref)
                elif error and lane_id:
                    lane = group.setdefault("lanes", {}).setdefault(
                        lane_id,
                        {"lane_id": lane_id, "title": lane_id, "role": "", "status": "planned", "worker_refs": [], "command_ids": []},
                    )
                    lane["status"] = "failed"
            self.store.append_event(payload, "command.finished", {"command_id": command_id, "machine_id": node_id, "state": command["state"]})
            return self.public_command(command)

        return self.store.update(mutate)

    def command_status(self, *, command_id: str = "", machine_id: str = "", state: str = "", work_group_id: str = "") -> dict[str, Any]:
        payload = self.store.read()
        commands = []
        for command in payload.get("commands", {}).values():
            if command_id and command.get("command_id") != command_id:
                continue
            if machine_id and command.get("machine_id") != _clean_id(machine_id, field="machine_id"):
                continue
            if state and command.get("state") != state:
                continue
            if work_group_id and command.get("work_group_id") != work_group_id:
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
            "work_group_id": command.get("work_group_id") or "",
            "lane_id": command.get("lane_id") or "",
            "manager_ref": command.get("manager_ref") or "",
            "arguments_summary": deepcopy(command.get("arguments_summary") or {}),
            "arguments_hash": command.get("arguments_hash") or "",
            "routing": deepcopy(command.get("routing") or {}),
            "internal": bool(command.get("internal")),
            "created_at": command.get("created_at"),
            "updated_at": command.get("updated_at"),
            "claimed_at": command.get("claimed_at"),
            "lease_expires_at": command.get("lease_expires_at"),
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
