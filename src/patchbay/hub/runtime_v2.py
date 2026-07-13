"""Adjacent, opt-in Hub V2 coordination runtime.

The production Hub still uses :mod:`patchbay.hub.runtime`.  This module composes
the V2 identity, SQLite store, operation broker, and group projection contracts
without wiring any public server route or reimplementing Edge-owned handlers.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import posixpath
import secrets
import time
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Protocol

from patchbay.hub.broker import OperationBroker
from patchbay.hub.groups_v2 import (
    ACTIVE_TURN_STATES,
    TERMINAL_OPERATION_STATES,
    create_successor_group,
    derive_completion_contract,
    validate_close_dispositions,
)
from patchbay.hub.identity import (
    FleetWorkerIdentity,
    ManagerIdentity,
    WorkspaceProjectionIdentity,
    new_ref,
    stable_ref,
    validate_ref,
)
from patchbay.hub.operations import public_envelope
from patchbay.hub.store_v2 import HubStoreV2, HubStoreV2Conflict
from patchbay.hub.tool_surface import (
    HUB_V2_CONTRACT_HASH,
    HUB_V2_CONTRACT_VERSION,
    HUB_V2_MANIFEST_HASH,
    HUB_V2_SCHEMA_HASH,
    HUB_V2_TOOL_FAMILIES,
)
from patchbay.protocol.context import RequestContext


ENROLLMENT_ENTITY = "hub.enrollment_code"
MACHINE_ENTITY = "hub.machine"
MACHINE_GENERATION_ENTITY = "hub.machine_generation"
EDGE_PROJECTION_ENTITY = "hub.edge_projection"
WORKSPACE_ENTITY = "hub.workspace"
WORKSPACE_PROJECTION_ENTITY = "hub.workspace_projection"
PARTICIPANT_ENTITY = "hub.participant"
CURRENT_GROUP_ENTITY = "hub.current_work_group"
WORK_GROUP_ENTITY = "hub.work_group"
FLEET_WORKER_ENTITY = "hub.fleet_worker"
WORKER_PROJECTION_ENTITY = "hub.worker_projection"
OPERATION_GROUP_ENTITY = "hub.operation_group"

DEFAULT_HEARTBEAT_STALE_SECONDS = 90
DEFAULT_MIN_DISK_FREE_BYTES = 2_147_483_648
DEFAULT_RESULT_LIMIT = 100
MAX_RESULT_LIMIT = 500
DEFAULT_GROUP_STATUS_DETAIL_LIMIT = 100
MAX_GROUP_STATUS_DETAIL_LIMIT = 100

ADAPTER_FAMILIES = frozenset(
    {
        "workers_and_artifacts",
        "exceptional_manager_workspace_inspection",
        "pro_requests",
    }
)
_LOCAL_TOOL_FAMILIES = frozenset(
    {"fleet_and_discovery", "work_groups", "exceptional_operation_recovery"}
)
_TOOL_FAMILY = {
    name: family
    for family, names in HUB_V2_TOOL_FAMILIES.items()
    for name in names
}


class HubToolFamilyAdapter(Protocol):
    """Execution boundary for a not-yet-wired V2 tool family."""

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]: ...


def _clean_text(value: Any, *, field: str, maximum: int = 2_000) -> str:
    text = " ".join(str(value or "").split())[:maximum]
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_text(value: Any, maximum: int = 2_000) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _string_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    raw = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError(f"{field} must be a list")
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if text != "/":
        text = text.rstrip("/")
    return text


def _safe_relative_path(value: str) -> bool:
    if not value or value.startswith(("/", "~")):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


class HubRuntimeV2:
    """Durable V2 fleet/workspace/group coordination core.

    Worker, workspace-inspection, and Pro Request domain behavior stays behind
    adapters composed by :class:`patchbay.hub.app_v2.HubAppV2`.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | HubStoreV2 | None = None,
        store: HubStoreV2 | None = None,
        *,
        broker: OperationBroker | None = None,
        clock: Callable[[], float] | None = None,
    ):
        if isinstance(config, HubStoreV2):
            if store is not None:
                raise TypeError("Pass the HubStoreV2 as config or store, not both")
            store = config
            config = {}
        self.config = dict(config or {})
        self.store = store or HubStoreV2(self.config)
        self._owns_store = store is None
        self.broker = broker or OperationBroker(self.store)
        self._clock = clock or time.time
        self._adapters: dict[str, Any] = {}
        self._identity_salt = self.store.principal_ref

    @property
    def stale_seconds(self) -> int:
        hub = self.config.get("hub") if isinstance(self.config.get("hub"), Mapping) else {}
        return max(1, _as_int(hub.get("heartbeat_stale_seconds"), DEFAULT_HEARTBEAT_STALE_SECONDS))

    @property
    def min_disk_free_bytes(self) -> int:
        hub = self.config.get("hub") if isinstance(self.config.get("hub"), Mapping) else {}
        routing = hub.get("routing") if isinstance(hub.get("routing"), Mapping) else {}
        return max(0, _as_int(routing.get("min_disk_free_bytes"), DEFAULT_MIN_DISK_FREE_BYTES))

    @property
    def routing_enabled(self) -> bool:
        hub = self.config.get("hub") if isinstance(self.config.get("hub"), Mapping) else {}
        routing = hub.get("routing") if isinstance(hub.get("routing"), Mapping) else {}
        value = routing.get("enabled", True)
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "HubRuntimeV2":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    # -- Adapter boundary -------------------------------------------------

    def register_adapter(self, family: str, adapter: Any, *, replace: bool = False) -> None:
        family_value = str(family or "").strip()
        if family_value not in ADAPTER_FAMILIES:
            if family_value in _LOCAL_TOOL_FAMILIES:
                raise ValueError(f"{family_value} is implemented by HubRuntimeV2")
            raise ValueError(f"Unknown Hub V2 adapter family: {family_value}")
        if not callable(adapter) and not callable(getattr(adapter, "handle_tool_call", None)) and not callable(
            getattr(adapter, "dispatch", None)
        ):
            raise TypeError("Adapter must be callable or define handle_tool_call/dispatch")
        if family_value in self._adapters and not replace:
            raise ValueError(f"Adapter already registered for {family_value}")
        self._adapters[family_value] = adapter

    register_tool_family_adapter = register_adapter

    def unregister_adapter(self, family: str) -> Any | None:
        return self._adapters.pop(str(family or "").strip(), None)

    async def dispatch_adapter(
        self,
        family: str,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        family_value = str(family or "").strip()
        if family_value not in ADAPTER_FAMILIES:
            raise ValueError(f"Unknown Hub V2 adapter family: {family_value}")
        if name not in HUB_V2_TOOL_FAMILIES[family_value]:
            raise ValueError(f"{name} does not belong to {family_value}")
        adapter = self._adapters.get(family_value)
        if adapter is None:
            return public_envelope(
                "blocked",
                result={"reason": "tool_family_adapter_not_registered", "family": family_value, "tool": name},
                next_actions=["Complete the owning Hub V2 handler WorkPacket before public wiring."],
            )
        handler = getattr(adapter, "handle_tool_call", None) or getattr(adapter, "dispatch", None) or adapter
        try:
            result = handler(name, dict(arguments), context=context)
        except TypeError as error:
            if "context" not in str(error):
                raise
            result = handler(name, dict(arguments))
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise TypeError(f"Adapter for {family_value} returned a non-object result")
        return dict(result)

    dispatch_tool_family = dispatch_adapter

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        """Implement Hub-owned tools and delegate the three Edge-owned families."""

        args = dict(arguments)
        local: dict[str, Callable[..., Any]] = {
            "patchbay_fleet_status": self.fleet_status,
            "patchbay_workspace_list": self.workspace_list,
            "patchbay_work_group_create": self.create_work_group,
            "patchbay_work_group_list": self.list_work_groups,
            "patchbay_work_group_status": self.work_group_status,
            "patchbay_work_group_resume": self.resume_work_group,
            "patchbay_work_group_reassign": self.reassign_work_group,
            "patchbay_work_group_close": self.close_work_group,
        }
        try:
            if name == "patchbay_operation_status":
                return await self.operation_status(context=context, **args)
            if name == "patchbay_work_group_status":
                return await self._wait_for_work_group_status(args, context=context)
            if name in local:
                if name.startswith("patchbay_work_group_") or name == "patchbay_fleet_status":
                    args["context"] = context
                result = local[name](**args)
                if inspect.isawaitable(result):
                    result = await result
                return result
            family = _TOOL_FAMILY.get(name)
            if family in ADAPTER_FAMILIES:
                return await self.dispatch_adapter(family, name, args, context=context)
            raise KeyError(f"Unknown Hub V2 tool: {name}")
        except HubStoreV2Conflict as error:
            reason = str(error or "").strip()
            if not reason or not reason.replace("_", "").isalnum():
                reason = "hub_state_conflict"
            next_action = (
                "Reuse the idempotency key only with the exact original arguments. For an intentionally different "
                "action, use a new key; inspect the existing group or operation before retrying."
                if reason == "idempotency_payload_conflict"
                else "Refresh the authoritative group or operation status before deciding whether to retry."
            )
            return public_envelope(
                "blocked",
                result={"reason": reason, "retry_safe": False},
                warnings=["The request conflicted with durable Hub state; no second mutation was applied."],
                next_actions=[next_action],
            )

    async def _wait_for_work_group_status(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        args = dict(arguments)
        requested_wait = max(0, min(_as_int(args.pop("wait_for_change_seconds", 0), 0), 30))
        supplied_revision = args.pop("since_revision", None)
        current = self.work_group_status(context=context, **args)
        if current.get("status") != "ok" or requested_wait <= 0:
            return current
        result = current.get("result") if isinstance(current.get("result"), Mapping) else {}
        baseline = (
            max(0, _as_int(supplied_revision, 0))
            if supplied_revision is not None
            else max(0, _as_int(result.get("status_revision"), 0))
        )
        started = time.monotonic()
        group_id = str(
            ((result.get("work_group") or {}).get("work_group_id") or "")
            if isinstance(result.get("work_group"), Mapping)
            else ""
        )
        current_revision = _as_int(result.get("status_revision"), 0)
        while current_revision == baseline:
            remaining = requested_wait - (time.monotonic() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.1, remaining))
            current_revision = self.store.work_group_status_revision(group_id)
            if current_revision != baseline:
                current = self.work_group_status(context=context, **args)
                if current.get("status") != "ok":
                    return current
                result = (
                    current.get("result")
                    if isinstance(current.get("result"), Mapping)
                    else {}
                )
                current_revision = _as_int(result.get("status_revision"), 0)
        result = deepcopy(dict(result))
        result["waited_seconds"] = round(time.monotonic() - started, 3)
        result["requested_wait_seconds"] = requested_wait
        result["changed"] = _as_int(result.get("status_revision"), 0) != baseline
        current["result"] = result
        contract = result.get("completion_contract")
        if isinstance(contract, Mapping) and contract.get("manager_must_continue"):
            action = contract.get("recommended_next_action")
            if isinstance(action, Mapping) and action:
                current["next_actions"] = [deepcopy(dict(action))]
        return current

    # -- Machine identity and heartbeat ----------------------------------

    def create_enrollment_code(
        self, *, name: str, tags: Any = None, ttl_minutes: int = 30
    ) -> dict[str, Any]:
        display_name = _clean_text(name, field="name", maximum=120)
        ttl = max(1, min(_as_int(ttl_minutes, 30), 1_440))
        code = f"PB-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
        now = self._clock()
        record = {
            "code": code,
            "display_name": display_name,
            "tags": _string_list(tags, field="tags"),
            "created_at": now,
            "expires_at": now + ttl * 60,
            "used_at": None,
        }
        self.store.put_entity(ENROLLMENT_ENTITY, code, record)
        self.store.append_event("machine.enrollment_code_created", {"code": code})
        return deepcopy(record)

    def enroll_machine(
        self,
        *,
        code: str,
        machine_id: str,
        display_name: str = "",
        tags: Any = None,
        role: str = "",
        capabilities: Mapping[str, Any] | None = None,
        workspaces: list[Mapping[str, Any]] | None = None,
        edge_generation: str = "",
    ) -> dict[str, Any]:
        code_value = str(code or "").strip().upper()
        enrollment = self.store.get_entity(ENROLLMENT_ENTITY, code_value)
        if enrollment is None:
            raise ValueError("Unknown enrollment code")
        code_record = enrollment["record"]
        now = self._clock()
        if code_record.get("used_at"):
            raise ValueError("Enrollment code was already used")
        if _as_float(code_record.get("expires_at")) < now:
            raise ValueError("Enrollment code expired")

        machine = validate_ref(machine_id, field="machine_id")
        generation = validate_ref(edge_generation, field="edge_generation") if edge_generation else new_ref("edgegen")
        existing = self.store.get_entity(MACHINE_ENTITY, machine)
        if existing and existing["record"].get("edge_generation") == generation:
            raise HubStoreV2Conflict("edge_generation_reuse")
        token = "node_" + secrets.token_urlsafe(32)
        name_value = _optional_text(display_name, 120) or str(code_record.get("display_name") or machine)
        machine_record = {
            "machine_id": machine,
            "display_name": name_value,
            "edge_generation": generation,
            "token_hash": _token_hash(token),
            "tags": _string_list(tags, field="tags") or list(code_record.get("tags") or []),
            "role": _optional_text(role, 80),
            "capabilities": deepcopy(dict(capabilities or {})),
            "resource_status": {},
            "worker_status": {},
            "created_at": existing["record"].get("created_at", now) if existing else now,
            "generation_created_at": now,
            "updated_at": now,
            "last_seen_at": None,
            "projection_revision": 0,
            "retired_at": None,
        }
        generation_record = {
            "machine_id": machine,
            "edge_generation": generation,
            "created_at": now,
            "superseded_at": None,
            "superseded_by": "",
        }
        if existing:
            old_generation = str(existing["record"].get("edge_generation") or "")
            old = self.store.get_entity(MACHINE_GENERATION_ENTITY, old_generation) if old_generation else None
            if old:
                old_record = deepcopy(old["record"])
                old_record.update({"superseded_at": now, "superseded_by": generation})
                self.store.put_entity(
                    MACHINE_GENERATION_ENTITY,
                    old_generation,
                    old_record,
                    expected_revision=old["revision"],
                )
        self.store.put_entity(MACHINE_GENERATION_ENTITY, generation, generation_record)
        self.store.put_entity(
            MACHINE_ENTITY,
            machine,
            machine_record,
            expected_revision=existing["revision"] if existing else 0,
        )
        used = deepcopy(code_record)
        used.update({"used_at": now, "machine_id": machine, "edge_generation": generation})
        self.store.put_entity(ENROLLMENT_ENTITY, code_value, used, expected_revision=enrollment["revision"])
        self.store.append_event(
            "machine.enrolled",
            {"machine_id": machine, "edge_generation": generation},
            entity_type=MACHINE_ENTITY,
            entity_id=machine,
        )
        if workspaces:
            self._persist_workspace_snapshot(machine_record, workspaces, projection_revision=0, received_at=now)
        return {
            "machine": self._public_machine(machine_record, now=now),
            "edge_generation": generation,
            "node_token": token,
        }

    def authenticate_machine(
        self, machine_id: str, token: str, *, edge_generation: str = ""
    ) -> dict[str, Any]:
        machine = validate_ref(machine_id, field="machine_id")
        entity = self.store.get_entity(MACHINE_ENTITY, machine)
        if entity is None or not hmac.compare_digest(
            str(entity["record"].get("token_hash") or ""), _token_hash(str(token or ""))
        ):
            raise ValueError("Unauthorized edge node")
        record = entity["record"]
        if record.get("retired_at"):
            raise ValueError("Edge node is retired")
        if edge_generation and record.get("edge_generation") != edge_generation:
            raise ValueError("Edge generation is not current for this machine")
        return deepcopy(record)

    def retire_machine(self, *, machine_id: str, reason: str = "") -> dict[str, Any]:
        entity = self.store.get_entity(MACHINE_ENTITY, validate_ref(machine_id, field="machine_id"))
        if entity is None:
            raise ValueError(f"Unknown machine_id: {machine_id}")
        record = deepcopy(entity["record"])
        record.update(
            {
                "retired_at": record.get("retired_at") or self._clock(),
                "retired_reason": _optional_text(reason, 500),
                "updated_at": self._clock(),
            }
        )
        saved = self.store.put_entity(MACHINE_ENTITY, machine_id, record, expected_revision=entity["revision"])
        return self._public_machine(saved["record"], now=self._clock())

    def restore_machine(self, *, machine_id: str) -> dict[str, Any]:
        entity = self.store.get_entity(MACHINE_ENTITY, validate_ref(machine_id, field="machine_id"))
        if entity is None:
            raise ValueError(f"Unknown machine_id: {machine_id}")
        record = deepcopy(entity["record"])
        record.update({"retired_at": None, "retired_reason": "", "updated_at": self._clock()})
        saved = self.store.put_entity(MACHINE_ENTITY, machine_id, record, expected_revision=entity["revision"])
        return self._public_machine(saved["record"], now=self._clock())

    def heartbeat(
        self,
        *,
        machine_id: str,
        token: str,
        edge_generation: str,
        projection_revision: int,
        capabilities: Mapping[str, Any] | None = None,
        workspaces: list[Mapping[str, Any]] | None = None,
        worker_status: Mapping[str, Any] | None = None,
        worker_projection: Mapping[str, Any] | None = None,
        resource_status: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        machine_record = self.authenticate_machine(machine_id, token, edge_generation=edge_generation)
        revision = _as_int(projection_revision, -1)
        if revision < 0:
            raise ValueError("projection_revision must be non-negative")
        now = self._clock()
        prior_revision = _as_int(machine_record.get("projection_revision"), 0)
        accepted = revision > prior_revision

        machine_record["last_seen_at"] = now
        machine_record["updated_at"] = now
        # Resource pressure is heartbeat telemetry, not a versioned workspace or
        # worker projection. Refresh it even when the Edge repeats the current
        # projection revision; otherwise normal projection-first loops leave the
        # availability router permanently blind to CPU, memory, disk, and slots.
        if resource_status is not None:
            machine_record["resource_status"] = deepcopy(dict(resource_status))
        if accepted:
            machine_record["projection_revision"] = revision
            if capabilities is not None:
                machine_record["capabilities"] = deepcopy(dict(capabilities))
            if worker_status is not None:
                machine_record["worker_status"] = deepcopy(dict(worker_status))
            elif worker_projection is not None:
                # V2 publishes the authoritative worker snapshot through the
                # dedicated projection endpoint before the next heartbeat. Keep
                # the fleet machine summary aligned with the same accepted
                # revision instead of retaining an older embedded snapshot.
                machine_record["worker_status"] = deepcopy(dict(worker_projection))
        current = self.store.get_entity(MACHINE_ENTITY, machine_id)
        if current is None:
            raise HubStoreV2Conflict("machine_disappeared")
        saved_machine = self.store.put_entity(
            MACHINE_ENTITY, machine_id, machine_record, expected_revision=current["revision"]
        )
        if not accepted:
            return {
                "accepted": True,
                "projection_accepted": False,
                "ignored_revision": revision,
                "current_projection_revision": prior_revision,
                "machine": self._public_machine(saved_machine["record"], now=now),
            }

        if workspaces is not None:
            self._persist_workspace_snapshot(
                machine_record, workspaces, projection_revision=revision, received_at=now
            )
        projection = worker_projection
        if projection is None and isinstance(worker_status, Mapping) and isinstance(worker_status.get("workers"), list):
            projection = worker_status
        projection_kind = str((projection or {}).get("snapshot_kind") or "full")
        gap = prior_revision > 0 and revision > prior_revision + 1
        projection_applied = False
        if projection is not None and not (gap and projection_kind != "full"):
            self._persist_worker_snapshot(
                machine_record,
                projection,
                projection_revision=revision,
                received_at=now,
            )
            pro_request_adapter = self._adapters.get("pro_requests")
            ingest_pro_requests = getattr(pro_request_adapter, "ingest_projection", None)
            if callable(ingest_pro_requests):
                ingest_pro_requests(
                    machine_record,
                    projection,
                    projection_revision=revision,
                    received_at=now,
                )
            projection_applied = True
        edge_projection_id = stable_ref("edgeproj", machine_id, edge_generation, salt=self._identity_salt)
        self._upsert_entity(
            EDGE_PROJECTION_ENTITY,
            edge_projection_id,
            {
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "projection_revision": revision,
                "snapshot_kind": projection_kind,
                "projection_gap": gap,
                "worker_projection_applied": projection_applied,
                "received_at": now,
            },
        )
        self.store.append_event(
            "machine.heartbeat",
            {
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "projection_revision": revision,
                "projection_gap": gap,
            },
            entity_type=MACHINE_ENTITY,
            entity_id=machine_id,
            entity_revision=saved_machine["revision"],
        )
        return {
            "accepted": True,
            "projection_accepted": True,
            "projection_applied": projection_applied,
            "request_full_snapshot": bool(gap and projection_kind != "full"),
            "machine": self._public_machine(saved_machine["record"], now=now),
        }

    # -- Workspace projections ------------------------------------------

    def _workspace_identity(self, workspace: Mapping[str, Any]) -> tuple[str, str, list[str]]:
        repository_identity = ""
        for field in ("repository_identity", "repo_identity", "remote_url", "git_remote", "remote"):
            value = workspace.get(field)
            if value:
                repository_identity = str(value).strip()
                break
        local_path = _normalize_path(workspace.get("path") or workspace.get("root"))
        aliases: list[str] = []
        for value in (
            workspace.get("alias"),
            workspace.get("repo_name"),
            workspace.get("name"),
            PurePosixPath(local_path).name if local_path else "",
        ):
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)
        logical_key = repository_identity.casefold() if repository_identity else (aliases[0].casefold() if aliases else local_path.casefold())
        if not logical_key:
            raise ValueError("Workspace projection requires repository identity, alias, or path")
        requested_ref = str(workspace.get("workspace_ref") or "").strip()
        workspace_ref = (
            validate_ref(requested_ref, field="workspace_ref")
            if requested_ref
            else stable_ref("workspace", logical_key, salt=self._identity_salt)
        )
        return workspace_ref, repository_identity, aliases

    def _persist_workspace_snapshot(
        self,
        machine: Mapping[str, Any],
        workspaces: list[Mapping[str, Any]],
        *,
        projection_revision: int,
        received_at: float,
    ) -> None:
        machine_id = str(machine["machine_id"])
        edge_generation = str(machine["edge_generation"])
        seen: set[str] = set()
        for advertised in workspaces:
            if not isinstance(advertised, Mapping):
                raise ValueError("Each workspace projection must be an object")
            workspace_ref, repository_identity, aliases = self._workspace_identity(advertised)
            local_path = _normalize_path(advertised.get("path") or advertised.get("root"))
            projection_identity = WorkspaceProjectionIdentity.create(
                workspace_ref=workspace_ref,
                machine_id=machine_id,
                edge_generation=edge_generation,
                local_identity=repository_identity or local_path or aliases[0],
                salt=self._identity_salt,
            )
            seen.add(projection_identity.projection_ref)
            logical = self.store.get_entity(WORKSPACE_ENTITY, workspace_ref)
            logical_record = deepcopy(logical["record"]) if logical else {
                "workspace_ref": workspace_ref,
                "display_name": aliases[0] if aliases else workspace_ref,
                "repository_identity": repository_identity,
                "aliases": [],
                "projection_refs": [],
                "created_at": received_at,
            }
            if repository_identity and logical_record.get("repository_identity") not in {"", repository_identity}:
                raise HubStoreV2Conflict("workspace_repository_identity_conflict")
            logical_record["repository_identity"] = repository_identity or logical_record.get("repository_identity", "")
            logical_record["aliases"] = sorted(set(logical_record.get("aliases") or []).union(aliases), key=str.casefold)
            projection_refs = list(logical_record.get("projection_refs") or [])
            if projection_identity.projection_ref not in projection_refs:
                projection_refs.append(projection_identity.projection_ref)
            logical_record["projection_refs"] = projection_refs
            logical_record["updated_at"] = received_at
            self.store.put_entity(
                WORKSPACE_ENTITY,
                workspace_ref,
                logical_record,
                expected_revision=logical["revision"] if logical else 0,
            )
            projection_record = {
                "workspace_projection_ref": projection_identity.projection_ref,
                "workspace_ref": workspace_ref,
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "local_path": local_path,
                "repository_identity": repository_identity,
                "aliases": aliases,
                "exists": bool(advertised.get("exists", True)),
                "git": bool(advertised.get("git", False)),
                "active": True,
                "advertised": deepcopy(dict(advertised)),
                "projection_revision": projection_revision,
                "received_at": received_at,
            }
            self._upsert_entity(
                WORKSPACE_PROJECTION_ENTITY,
                projection_identity.projection_ref,
                projection_record,
            )
        for entity in self.store.list_entities(WORKSPACE_PROJECTION_ENTITY):
            record = entity["record"]
            if (
                record.get("machine_id") == machine_id
                and record.get("edge_generation") == edge_generation
                and record.get("workspace_projection_ref") not in seen
                and record.get("active")
            ):
                replacement = deepcopy(record)
                replacement.update({"active": False, "omitted_at": received_at, "projection_revision": projection_revision})
                self.store.put_entity(
                    WORKSPACE_PROJECTION_ENTITY,
                    entity["entity_id"],
                    replacement,
                    expected_revision=entity["revision"],
                )

    def _matching_workspace_projections(
        self,
        *,
        workspace_ref: str = "",
        repo_path: str = "",
        machine_ids: Any = None,
        include_offline: bool = False,
    ) -> list[dict[str, Any]]:
        requested_ref = str(workspace_ref or "").strip()
        requested_path = _normalize_path(repo_path)
        allowed = set(_string_list(machine_ids, field="machine_ids"))
        machines = {
            entity["entity_id"]: self._public_machine(entity["record"], now=self._clock())
            for entity in self.store.list_entities(MACHINE_ENTITY)
        }
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for entity in self.store.list_entities(WORKSPACE_PROJECTION_ENTITY):
            projection = deepcopy(entity["record"])
            machine_id = str(projection.get("machine_id") or "")
            machine = machines.get(machine_id)
            if not projection.get("active") or machine is None:
                continue
            if allowed and machine_id not in allowed:
                continue
            if not include_offline and machine["status"] != "online":
                continue
            if requested_ref and projection.get("workspace_ref") != requested_ref:
                continue
            local_path = _normalize_path(projection.get("local_path"))
            aliases = {str(item).casefold() for item in projection.get("aliases") or []}
            if local_path:
                aliases.add(PurePosixPath(local_path).name.casefold())
            match_kind = "workspace_ref" if requested_ref else "all"
            priority = 0 if requested_ref else 4
            resolved_path = local_path
            if requested_path and requested_ref:
                folded = requested_path.casefold()
                root = local_path.casefold() if local_path else ""
                if local_path and (folded == root or folded.startswith(root.rstrip("/") + "/")):
                    match_kind = "workspace_ref_and_path"
                    resolved_path = requested_path
                elif local_path and not projection.get("git") and _safe_relative_path(requested_path):
                    candidate = posixpath.normpath(posixpath.join(local_path, requested_path))
                    if candidate != local_path and candidate.startswith(local_path.rstrip("/") + "/"):
                        match_kind = "workspace_ref_and_relative_path"
                        resolved_path = candidate
                    else:
                        continue
                else:
                    continue
            elif requested_path and not requested_ref:
                folded = requested_path.casefold()
                if local_path and (folded == local_path.casefold() or folded.startswith(local_path.casefold() + "/")):
                    priority, match_kind, resolved_path = 1, "workspace_path", requested_path
                elif folded in aliases:
                    priority, match_kind = 2, "workspace_alias"
                elif local_path and not projection.get("git") and _safe_relative_path(requested_path):
                    candidate = posixpath.normpath(posixpath.join(local_path, requested_path))
                    if candidate != local_path and candidate.startswith(local_path.rstrip("/") + "/"):
                        priority, match_kind, resolved_path = 3, "relative_under_workspace_root", candidate
                    else:
                        continue
                else:
                    continue
            projection.update(
                {
                    "match_kind": match_kind,
                    "requested_repo_path": requested_path,
                    "resolved_path": resolved_path,
                    "machine": machine,
                }
            )
            ranked.append((priority, machine_id, projection))
        ranked.sort(key=lambda item: (item[0], item[1], str(item[2].get("workspace_projection_ref") or "")))
        if requested_path and ranked:
            best_priority = ranked[0][0]
            ranked = [item for item in ranked if item[0] == best_priority]
        return [item[2] for item in ranked]

    def workspace_list(
        self,
        *,
        query: str = "",
        discover: bool = False,
        machine_ids: Any = None,
        required_tags: Any = None,
        include_offline: bool = False,
        max_depth: int = 0,
        max_results: int = DEFAULT_RESULT_LIMIT,
    ) -> dict[str, Any]:
        del max_depth  # Discovery belongs to a later routed workspace adapter.
        query_text = str(query or "").strip().casefold()
        allowed = set(_string_list(machine_ids, field="machine_ids"))
        wanted_tags = set(_string_list(required_tags, field="required_tags"))
        machine_views = {
            entity["entity_id"]: self._public_machine(entity["record"], now=self._clock())
            for entity in self.store.list_entities(MACHINE_ENTITY)
        }
        workspaces: list[dict[str, Any]] = []
        for entity in self.store.list_entities(WORKSPACE_ENTITY):
            logical = deepcopy(entity["record"])
            projections: list[dict[str, Any]] = []
            for projection_ref in logical.get("projection_refs") or []:
                projection_entity = self.store.get_entity(WORKSPACE_PROJECTION_ENTITY, projection_ref)
                if projection_entity is None or not projection_entity["record"].get("active"):
                    continue
                projection = deepcopy(projection_entity["record"])
                machine = machine_views.get(str(projection.get("machine_id") or ""))
                if machine is None or (allowed and machine["machine_id"] not in allowed):
                    continue
                if wanted_tags and not wanted_tags.issubset(set(machine.get("tags") or [])):
                    continue
                if not include_offline and machine["status"] != "online":
                    continue
                projection["machine_status"] = machine["status"]
                projection["compatibility"] = machine["compatibility"]
                projection["availability"] = self._projection_availability(machine, projection)
                projections.append(projection)
            haystack = " ".join(
                [
                    str(logical.get("workspace_ref") or ""),
                    str(logical.get("display_name") or ""),
                    str(logical.get("repository_identity") or ""),
                    *[str(alias) for alias in logical.get("aliases") or []],
                    *[str(item.get("local_path") or "") for item in projections],
                ]
            ).casefold()
            if query_text and query_text not in haystack:
                continue
            if projections or (not allowed and not wanted_tags and include_offline):
                logical["projections"] = projections
                logical["availability"] = self._logical_workspace_availability(projections)
                workspaces.append(logical)
        workspaces.sort(key=lambda item: (str(item.get("display_name") or "").casefold(), item["workspace_ref"]))
        limit = max(1, min(_as_int(max_results, DEFAULT_RESULT_LIMIT), MAX_RESULT_LIMIT))
        truncated = len(workspaces) > limit
        result = {
            "workspaces": workspaces[:limit],
            "count": len(workspaces),
            "truncated": truncated,
            "next_cursor": str(limit) if truncated else "",
            "query": str(query or ""),
        }
        warnings = []
        if discover:
            warnings.append(
                {
                    "code": "discovery_adapter_not_wired",
                    "message": "Returned persisted workspace projections only.",
                }
            )
        return public_envelope("ok", result=result, warnings=warnings)

    # -- Worker projections ---------------------------------------------

    def _persist_worker_snapshot(
        self,
        machine: Mapping[str, Any],
        projection: Mapping[str, Any],
        *,
        projection_revision: int,
        received_at: float,
    ) -> None:
        workers = projection.get("workers") or []
        if not isinstance(workers, list):
            raise ValueError("Worker projection workers must be a list")
        machine_id = str(machine["machine_id"])
        edge_generation = str(machine["edge_generation"])
        seen_edge_ids: set[str] = set()
        for value in workers:
            if not isinstance(value, Mapping):
                raise ValueError("Each worker projection must be an object")
            edge_worker_id = str(value.get("edge_worker_id") or value.get("worker_id") or "").strip()
            if not edge_worker_id:
                raise ValueError("Worker projection requires edge_worker_id")
            seen_edge_ids.add(edge_worker_id)
            identity = FleetWorkerIdentity.create(
                machine_id=machine_id,
                edge_generation=edge_generation,
                edge_worker_id=edge_worker_id,
                salt=self._identity_salt,
            )
            existing = self.store.get_entity(FLEET_WORKER_ENTITY, identity.fleet_worker_ref)
            work_group_id = str(value.get("work_group_id") or "")
            group_entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id) if work_group_id else None
            workspace_ref = str(value.get("workspace_ref") or "")
            if not workspace_ref and group_entity:
                workspace_ref = str(group_entity["record"].get("workspace_ref") or "")
            immutable = {
                "fleet_worker_ref": identity.fleet_worker_ref,
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "edge_worker_id": edge_worker_id,
                "work_group_id": work_group_id,
                "lane_id": str(value.get("lane_id") or value.get("lane") or "main"),
                "workspace_ref": workspace_ref,
                "name": str(value.get("name") or value.get("worker_name") or edge_worker_id),
                "created_at": received_at,
            }
            if existing:
                old = existing["record"]
                for field in ("machine_id", "edge_generation", "edge_worker_id", "work_group_id", "lane_id", "workspace_ref"):
                    if old.get(field) and immutable.get(field) and old[field] != immutable[field]:
                        raise HubStoreV2Conflict(f"immutable_fleet_worker_{field}_conflict")
                immutable = {**deepcopy(old), **{key: value for key, value in immutable.items() if value}}
                immutable["created_at"] = old.get("created_at", received_at)
            self.store.put_entity(
                FLEET_WORKER_ENTITY,
                identity.fleet_worker_ref,
                immutable,
                expected_revision=existing["revision"] if existing else 0,
            )
            worker_projection = deepcopy(dict(value))
            worker_projection.update(
                {
                    **immutable,
                    "worker_state": str(value.get("worker_state") or "available"),
                    "turn_state": str(value.get("turn_state") or "none"),
                    "liveness": str(value.get("liveness") or "terminal"),
                    "integration_state": str(value.get("integration_state") or "not_applicable"),
                    "review_disposition": str(value.get("review_disposition") or "unreviewed"),
                    "edge_projection_revision": projection_revision,
                    "received_at": received_at,
                    "tombstoned": False,
                }
            )
            existing_projection = self.store.get_entity(
                WORKER_PROJECTION_ENTITY, identity.fleet_worker_ref
            )
            if (
                existing_projection is not None
                and not existing_projection["record"].get("tombstoned")
                and value.get("content_sha256")
                and existing_projection["record"].get("content_sha256")
                == value.get("content_sha256")
            ):
                continue
            self._upsert_entity(WORKER_PROJECTION_ENTITY, identity.fleet_worker_ref, worker_projection)
            if (
                worker_projection.get("workspace_mode") == "shared_write"
                and worker_projection.get("turn_state")
                in {"completed", "failed", "cancelled"}
                and work_group_id
                and isinstance(worker_projection.get("base_checkout_snapshot"), Mapping)
            ):
                self.record_group_base_mutation_snapshot(
                    work_group_id=work_group_id,
                    snapshot=worker_projection["base_checkout_snapshot"],
                    reason="shared_write_worker_turn_finished",
                    source_operation_id=f"{identity.fleet_worker_ref}:{projection_revision}",
                )

        tombstones = projection.get("tombstones") or []
        tombstoned_edge_ids = {
            str(item.get("edge_worker_id") or "")
            for item in tombstones
            if isinstance(item, Mapping) and item.get("edge_worker_id")
        }
        complete = bool(
            projection.get("complete_worker_set")
            or projection.get("omission_means_tombstone")
            or str(projection.get("snapshot_kind") or "") == "full"
        )
        for worker_entity in self.store.list_entities(FLEET_WORKER_ENTITY):
            worker = worker_entity["record"]
            if worker.get("machine_id") != machine_id or worker.get("edge_generation") != edge_generation:
                continue
            edge_worker_id = str(worker.get("edge_worker_id") or "")
            if edge_worker_id not in tombstoned_edge_ids and not (complete and edge_worker_id not in seen_edge_ids):
                continue
            projection_entity = self.store.get_entity(WORKER_PROJECTION_ENTITY, worker_entity["entity_id"])
            if projection_entity is None:
                continue
            replacement = deepcopy(projection_entity["record"])
            replacement.update(
                {
                    "liveness": "lost",
                    "tombstoned": True,
                    "tombstoned_at": received_at,
                    "edge_projection_revision": projection_revision,
                    "received_at": received_at,
                }
            )
            self.store.put_entity(
                WORKER_PROJECTION_ENTITY,
                projection_entity["entity_id"],
                replacement,
                expected_revision=projection_entity["revision"],
            )

    def _workers_for_group(self, work_group_id: str) -> list[dict[str, Any]]:
        workers: list[dict[str, Any]] = []
        for worker_ref in self.store.worker_refs_for_work_group(work_group_id):
            entity = self.store.get_entity(FLEET_WORKER_ENTITY, worker_ref)
            if entity is None:
                continue
            projection = self.store.get_entity(WORKER_PROJECTION_ENTITY, entity["entity_id"])
            value = deepcopy(projection["record"] if projection else entity["record"])
            value.setdefault("worker_state", "available")
            value.setdefault("turn_state", "none")
            value.setdefault("liveness", "lost")
            value.setdefault("integration_state", "uncertain")
            value.setdefault("review_disposition", "unreviewed")
            workers.append(value)
        workers.sort(key=lambda item: (str(item.get("lane_id") or ""), str(item.get("name") or "").casefold()))
        return workers

    # -- Placement and group lifecycle ----------------------------------

    def _manager_identity(self, context: RequestContext | None) -> ManagerIdentity:
        return ManagerIdentity.from_request(context, principal_ref=self.store.principal_ref)

    def _public_machine(self, record: Mapping[str, Any], *, now: float) -> dict[str, Any]:
        last_seen = _as_float(record.get("last_seen_at"), 0)
        retired = bool(record.get("retired_at"))
        status = "retired" if retired else ("online" if last_seen and now - last_seen <= self.stale_seconds else "offline")
        capabilities = deepcopy(dict(record.get("capabilities") or {}))
        contract_hash = str(capabilities.get("contract_hash") or "")
        compatibility = "compatible" if contract_hash == HUB_V2_CONTRACT_HASH else "incompatible"
        resources = deepcopy(dict(record.get("resource_status") or {}))
        projection_health = deepcopy(dict(resources.get("projection_health") or {}))
        projection_age = projection_health.get("projection_age_seconds")
        projection_status = "unknown"
        if projection_health.get("last_success_at"):
            projection_status = (
                "stale"
                if projection_age is not None and float(projection_age) > self.stale_seconds
                else "current"
            )
        elif projection_health.get("consecutive_failures"):
            projection_status = "failed"
        return {
            "machine_id": record.get("machine_id"),
            "display_name": record.get("display_name"),
            "edge_generation": record.get("edge_generation"),
            "status": status,
            "compatibility": compatibility,
            "contract_hash": contract_hash,
            "tags": list(record.get("tags") or []),
            "role": record.get("role") or "",
            "last_seen_at": record.get("last_seen_at"),
            "last_seen_age_seconds": round(now - last_seen, 3) if last_seen else None,
            "projection_revision": _as_int(record.get("projection_revision"), 0),
            "capabilities": capabilities,
            "worker_status": deepcopy(dict(record.get("worker_status") or {})),
            "resource_status": resources,
            "worker_projection_status": projection_status,
            "projection_health": projection_health,
            "retired_at": record.get("retired_at"),
        }

    def _projection_availability(
        self, machine: Mapping[str, Any], projection: Mapping[str, Any]
    ) -> str:
        if machine.get("status") != "online":
            return "offline"
        if machine.get("compatibility") != "compatible":
            return "incompatible_edge"
        if projection.get("exists") is False:
            return "workspace_missing"
        return "preflight_required"

    @staticmethod
    def _logical_workspace_availability(projections: list[Mapping[str, Any]]) -> str:
        values = {str(item.get("availability") or "") for item in projections}
        for status in ("ready", "preflight_required", "incompatible_edge", "offline", "workspace_missing"):
            if status in values:
                return status
        return "unavailable"

    def _capacity(self, machine: Mapping[str, Any]) -> dict[str, Any]:
        resources = machine.get("resource_status") if isinstance(machine.get("resource_status"), Mapping) else {}
        capabilities = machine.get("capabilities") if isinstance(machine.get("capabilities"), Mapping) else {}
        active = max(0, _as_int(resources.get("active_workers"), 0))
        maximum = max(0, _as_int(resources.get("max_concurrent_jobs"), _as_int(capabilities.get("max_concurrent_jobs"), 0)))
        free = _as_int(resources.get("free_worker_slots"), maximum - active if maximum else 1)
        queue_enabled = bool(resources.get("queue_enabled", capabilities.get("queue_enabled", False)))
        return {
            "active_workers": active,
            "max_concurrent_jobs": maximum,
            "free_worker_slots": max(0, free),
            "queue_enabled": queue_enabled,
            "worker_ratio": active / maximum if maximum else 0.0,
            "memory_ratio": max(0.0, min(_as_float(resources.get("memory_used_percent"), 0.0) / 100.0, 1.0)),
            "cpu_ratio": max(0.0, min(_as_float(resources.get("cpu_percent"), 0.0) / 100.0, 1.0)),
            "disk_free_bytes": _as_int(resources.get("disk_free_bytes"), -1),
        }

    def _place_group(
        self,
        *,
        workspace_ref: str = "",
        repo_path: str = "",
        machine_id: str = "",
        allowed_machine_ids: Any = None,
        required_tags: Any = None,
        exclude_pin: tuple[str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
        allowed = set(_string_list(allowed_machine_ids, field="allowed_machine_ids"))
        tags = set(_string_list(required_tags, field="required_tags"))
        if machine_id:
            validate_ref(machine_id, field="machine_id")
            if allowed and machine_id not in allowed:
                raise ValueError("machine_id is not included in allowed_machine_ids")
            allowed = {machine_id}
        projections = self._matching_workspace_projections(
            workspace_ref=workspace_ref,
            repo_path=repo_path,
            machine_ids=sorted(allowed),
            include_offline=True,
        )
        if not workspace_ref and not repo_path:
            projections = []
            for entity in self.store.list_entities(MACHINE_ENTITY):
                machine = self._public_machine(entity["record"], now=self._clock())
                if allowed and machine["machine_id"] not in allowed:
                    continue
                projections.append(
                    {
                        "workspace_ref": "",
                        "workspace_projection_ref": "",
                        "resolved_path": "",
                        "machine_id": machine["machine_id"],
                        "edge_generation": machine["edge_generation"],
                        "exists": True,
                        "machine": machine,
                    }
                )
        candidates: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        for projection in projections:
            machine = projection["machine"]
            pin = (str(machine["machine_id"]), str(machine["edge_generation"]))
            reasons: list[str] = []
            if exclude_pin and pin == exclude_pin:
                reasons.append("predecessor_pin")
            if machine["status"] != "online":
                reasons.append("machine_offline")
            if machine["compatibility"] != "compatible":
                reasons.append("incompatible_edge")
            if tags and not tags.issubset(set(machine.get("tags") or [])):
                reasons.append("required_tags_missing")
            if projection.get("exists") is False:
                reasons.append("workspace_missing")
            capacity = self._capacity(machine)
            if capacity["free_worker_slots"] <= 0 and not capacity["queue_enabled"]:
                reasons.append("capacity_blocked")
            if 0 <= capacity["disk_free_bytes"] < self.min_disk_free_bytes:
                reasons.append("disk_feasibility_blocked")
            summary = {
                "machine_id": machine["machine_id"],
                "edge_generation": machine["edge_generation"],
                "workspace_ref": projection.get("workspace_ref") or "",
                "workspace_projection_ref": projection.get("workspace_projection_ref") or "",
                "resolved_path": projection.get("resolved_path") or projection.get("local_path") or "",
                "capacity": capacity,
                "reasons": reasons,
            }
            if reasons:
                rejections.append(summary)
                continue
            summary["score"] = round(
                capacity["worker_ratio"] * 0.60
                + capacity["memory_ratio"] * 0.20
                + capacity["cpu_ratio"] * 0.20,
                6,
            )
            summary["machine"] = machine
            summary["projection"] = projection
            candidates.append(summary)
        candidates.sort(
            key=lambda item: (
                item["capacity"]["free_worker_slots"] <= 0,
                item["score"],
                -item["capacity"]["free_worker_slots"],
                item["machine_id"],
            )
        )
        return (candidates[0] if candidates else None), candidates, rejections

    def _normalize_lanes(self, lanes: Any) -> dict[str, dict[str, Any]]:
        values = lanes or [{"lane": "main", "title": "main", "role": ""}]
        if not isinstance(values, list):
            raise ValueError("lanes must be a list")
        result: dict[str, dict[str, Any]] = {}
        for item in values:
            if isinstance(item, Mapping):
                lane = str(item.get("lane") or item.get("lane_id") or "").strip()
                title = _optional_text(item.get("title") or lane, 120)
                role = _optional_text(item.get("role"), 500)
            else:
                lane, title, role = str(item or "").strip(), str(item or "").strip(), ""
            if not lane:
                raise ValueError("Each lane requires a lane value")
            if lane in result:
                raise ValueError(f"Duplicate lane: {lane}")
            result[lane] = {"lane_id": lane, "lane": lane, "title": title or lane, "role": role}
        return result

    def _persist_participation(
        self, identity: ManagerIdentity, work_group_id: str, *, takeover: bool = False
    ) -> None:
        now = self._clock()
        participant = self.store.get_entity(PARTICIPANT_ENTITY, identity.participant_ref)
        record = deepcopy(participant["record"]) if participant else identity.public_metadata()
        groups = list(record.get("work_group_ids") or [])
        if work_group_id not in groups:
            groups.append(work_group_id)
        record.update(
            {
                "participant_ref": identity.participant_ref,
                "principal_ref": identity.principal_ref,
                "work_group_ids": groups,
                "current_work_group_id": work_group_id,
                "last_resumed_at": now,
                "takeover": bool(takeover),
            }
        )
        self.store.put_entity(
            PARTICIPANT_ENTITY,
            identity.participant_ref,
            record,
            expected_revision=participant["revision"] if participant else 0,
        )
        pointer = self.store.get_entity(CURRENT_GROUP_ENTITY, identity.participant_ref)
        self.store.put_entity(
            CURRENT_GROUP_ENTITY,
            identity.participant_ref,
            {
                "principal_ref": identity.principal_ref,
                "participant_ref": identity.participant_ref,
                "work_group_id": work_group_id,
                "updated_at": now,
            },
            expected_revision=pointer["revision"] if pointer else 0,
        )

    def _current_group_id(self, identity: ManagerIdentity) -> str:
        pointer = self.store.get_entity(CURRENT_GROUP_ENTITY, identity.participant_ref)
        if pointer is None or pointer["record"].get("principal_ref") != identity.principal_ref:
            return ""
        group_id = str(pointer["record"].get("work_group_id") or "")
        group = self.store.get_entity(WORK_GROUP_ENTITY, group_id) if group_id else None
        if group is None or group["record"].get("status") != "open":
            return ""
        return group_id

    def _operation_summary(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": str(operation.get("operation_id") or ""),
            "parent_operation_id": str(operation.get("parent_operation_id") or ""),
            "tool_name": str(operation.get("tool") or operation.get("tool_name") or ""),
            "state": str(operation.get("state") or "created"),
            "idempotency_key": str(operation.get("idempotency_key") or ""),
            "semantic_payload_hash": str(operation.get("semantic_payload_hash") or ""),
            "revision": _as_int(operation.get("revision"), 1),
            "created_at": _as_float(operation.get("created_at"), self._clock()),
            "updated_at": _as_float(operation.get("updated_at"), self._clock()),
        }

    def _complete_hub_operation(
        self, operation: Mapping[str, Any], envelope: Mapping[str, Any]
    ) -> dict[str, Any]:
        current = dict(operation)
        next_preparation_state = {
            "created": "payload_ready",
            "payload_ready": "dispatchable",
            "dispatchable": "running",
        }
        while current["state"] in next_preparation_state:
            state = next_preparation_state[current["state"]]
            current = self.broker.transition_operation(
                current["operation_id"], expected_revision=current["revision"], state=state
            ) or self.store.get_operation(current["operation_id"])
        if current["state"] in TERMINAL_OPERATION_STATES:
            return current
        target = {
            "ok": "succeeded",
            "partial": "succeeded",
            "not_found": "succeeded",
            "blocked": "blocked",
            "failed": "failed",
            "pending": "outcome_unknown",
        }[str(envelope["status"])]
        if current["state"] == "outcome_unknown" and target in TERMINAL_OPERATION_STATES:
            current = self.broker.transition_operation(
                current["operation_id"], expected_revision=current["revision"], state="reconciling"
            ) or self.store.get_operation(current["operation_id"])
        return self.broker.transition_operation(
            current["operation_id"],
            expected_revision=current["revision"],
            state=target,
            result=envelope,
        ) or self.store.get_operation(current["operation_id"])

    def _create_preflight_operation(
        self, group: Mapping[str, Any], *, idempotency_key: str, reason: str
    ) -> dict[str, Any]:
        payload = {
            "action": "patchbay_edge_preflight",
            "reason": reason,
            "work_group_id": group["work_group_id"],
            "machine_id": group["pinned_machine_id"],
            "edge_generation": group["pinned_edge_generation"],
            "workspace_ref": group.get("workspace_ref") or "",
            "workspace_projection_ref": group.get("workspace_projection_ref") or "",
            "repo_path": group.get("resolved_repo_path") or "",
            "repository_identity": group.get("repository_identity") or "",
            "required_contract_hash": HUB_V2_CONTRACT_HASH,
        }
        operation = self.broker.create_operation(
            tool="patchbay_edge_preflight",
            logical_target=str(group["work_group_id"]),
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if operation["state"] == "created":
            operation = self.broker.prepare_operation(
                operation["operation_id"], expected_revision=operation["revision"]
            ) or operation
        if operation["state"] == "payload_ready":
            operation = self.broker.make_dispatchable(
                operation["operation_id"], expected_revision=operation["revision"]
            ) or operation
        self._upsert_entity(
            OPERATION_GROUP_ENTITY,
            operation["operation_id"],
            {"operation_id": operation["operation_id"], "work_group_id": group["work_group_id"], "kind": "preflight"},
        )
        return operation

    def _finish_group_creation(
        self,
        *,
        group_id: str,
        operation: Mapping[str, Any],
        idempotency_key: str,
        identity: ManagerIdentity,
        candidates: list[Mapping[str, Any]] | None = None,
        rejections: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Finish or replay every post-persist group-creation side effect."""

        group_entity = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
        if group_entity is None:
            raise HubStoreV2Conflict("group_create_entity_missing")
        if str(operation.get("state") or "") in TERMINAL_OPERATION_STATES:
            return self._group_envelope(group_entity["record"], operation=operation)
        group = deepcopy(group_entity["record"])
        self._persist_participation(identity, group_id)
        preflight = self._create_preflight_operation(
            group,
            idempotency_key=f"{idempotency_key}:preflight",
            reason="group_create",
        )
        readiness = group.get("readiness") if isinstance(group.get("readiness"), Mapping) else {}
        if str(readiness.get("operation_id") or "") != str(preflight["operation_id"]):
            group["readiness"] = {
                **deepcopy(dict(readiness)),
                "operation_id": preflight["operation_id"],
                "updated_at": self._clock(),
            }
            group_entity = self.store.put_entity(
                WORK_GROUP_ENTITY,
                group_id,
                group,
                expected_revision=group_entity["revision"],
            )

        self._upsert_entity(
            OPERATION_GROUP_ENTITY,
            str(operation["operation_id"]),
            {
                "operation_id": operation["operation_id"],
                "work_group_id": group_id,
                "kind": "group_create",
            },
        )
        current = self.store.get_operation(str(operation["operation_id"])) or dict(operation)
        event_needed = str(current.get("state") or "") not in TERMINAL_OPERATION_STATES
        terminal = (
            self._complete_hub_operation(
                current, public_envelope("ok", result={"work_group_id": group_id})
            )
            if event_needed
            else current
        )
        saved_group = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
        if saved_group is None:
            raise HubStoreV2Conflict("group_create_entity_missing")
        if event_needed:
            self.store.append_event(
                "work_group.created",
                {
                    "work_group_id": group_id,
                    "machine_id": saved_group["record"]["pinned_machine_id"],
                    "edge_generation": saved_group["record"]["pinned_edge_generation"],
                    "participant_ref": identity.participant_ref,
                },
                entity_type=WORK_GROUP_ENTITY,
                entity_id=group_id,
                entity_revision=saved_group["revision"],
            )
        return self._group_envelope(
            saved_group["record"],
            operation=terminal,
            candidate_summary=[
                self._public_candidate(item) for item in list(candidates or [])
            ],
            rejection_summary=list(rejections or []),
        )

    def _finish_group_reassignment(
        self,
        *,
        predecessor_id: str,
        successor_id: str,
        operation: Mapping[str, Any],
        idempotency_key: str,
        identity: ManagerIdentity,
        candidates: list[Mapping[str, Any]] | None = None,
        rejections: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Finish or replay one durable successor-group transition."""

        successor_entity = self.store.get_entity(WORK_GROUP_ENTITY, successor_id)
        if successor_entity is None:
            raise HubStoreV2Conflict("group_reassign_successor_missing")
        if str(operation.get("state") or "") in TERMINAL_OPERATION_STATES:
            return self._group_envelope(successor_entity["record"], operation=operation)
        successor = deepcopy(successor_entity["record"])
        preflight = self._create_preflight_operation(
            successor,
            idempotency_key=f"{idempotency_key}:successor-preflight",
            reason="group_reassign",
        )
        readiness = (
            successor.get("readiness")
            if isinstance(successor.get("readiness"), Mapping)
            else {}
        )
        if str(readiness.get("operation_id") or "") != str(preflight["operation_id"]):
            successor["readiness"] = {
                **deepcopy(dict(readiness)),
                "operation_id": preflight["operation_id"],
                "updated_at": self._clock(),
            }
            successor_entity = self.store.put_entity(
                WORK_GROUP_ENTITY,
                successor_id,
                successor,
                expected_revision=successor_entity["revision"],
            )

        self._cancel_unclaimed_group_operations(
            predecessor_id,
            exclude_operation_ids={str(operation["operation_id"])},
        )
        predecessor_entity = self.store.get_entity(WORK_GROUP_ENTITY, predecessor_id)
        if predecessor_entity is None:
            raise HubStoreV2Conflict("group_reassign_predecessor_missing")
        predecessor = deepcopy(predecessor_entity["record"])
        if predecessor.get("status") == "open":
            predecessor["status"] = "superseded"
            predecessor["lifecycle"] = "superseded"
            predecessor["superseded_by"] = successor_id
            predecessor["updated_at"] = self._clock()
            self.store.put_entity(
                WORK_GROUP_ENTITY,
                predecessor_id,
                predecessor,
                expected_revision=predecessor_entity["revision"],
            )
        elif not (
            predecessor.get("status") == "superseded"
            and str(predecessor.get("superseded_by") or "") == successor_id
        ):
            raise HubStoreV2Conflict("group_reassign_predecessor_state_conflict")

        self._persist_participation(identity, successor_id)
        self._upsert_entity(
            OPERATION_GROUP_ENTITY,
            str(operation["operation_id"]),
            {
                "operation_id": operation["operation_id"],
                "work_group_id": successor_id,
                "kind": "group_reassign",
            },
        )
        current = self.store.get_operation(str(operation["operation_id"])) or dict(operation)
        terminal = (
            self._complete_hub_operation(
                current,
                public_envelope(
                    "ok",
                    result={
                        "work_group_id": successor_id,
                        "predecessor_work_group_id": predecessor_id,
                    },
                ),
            )
            if str(current.get("state") or "") not in TERMINAL_OPERATION_STATES
            else current
        )
        saved_successor = self.store.get_entity(WORK_GROUP_ENTITY, successor_id)
        if saved_successor is None:
            raise HubStoreV2Conflict("group_reassign_successor_missing")
        return self._group_envelope(
            saved_successor["record"],
            operation=terminal,
            candidate_summary=[
                self._public_candidate(item) for item in list(candidates or [])
            ],
            rejection_summary=list(rejections or []),
        )

    def _finish_group_close(
        self,
        *,
        group_id: str,
        operation: Mapping[str, Any],
        outcome: str,
        summary: str,
        dispositions: Mapping[str, str],
        active_work_disposition: str,
    ) -> dict[str, Any]:
        """Finish or replay group closure after its durable operation exists."""

        group_entity = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
        if group_entity is None:
            raise HubStoreV2Conflict("group_close_entity_missing")
        if str(operation.get("state") or "") in TERMINAL_OPERATION_STATES:
            return self._group_envelope(group_entity["record"], operation=operation)
        group = deepcopy(group_entity["record"])
        now = self._clock()
        if group.get("status") == "open":
            group.update(
                {
                    "status": "closed",
                    "lifecycle": "closed",
                    "outcome": outcome,
                    "summary": summary,
                    "closure_dispositions": dict(dispositions),
                    "active_work_disposition": active_work_disposition,
                    "closed_at": now,
                    "updated_at": now,
                }
            )
            group_entity = self.store.put_entity(
                WORK_GROUP_ENTITY,
                group_id,
                group,
                expected_revision=group_entity["revision"],
            )
        elif not (
            group.get("status") == "closed"
            and group.get("outcome") == outcome
            and str(group.get("summary") or "") == summary
            and dict(group.get("closure_dispositions") or {}) == dict(dispositions)
            and group.get("active_work_disposition") == active_work_disposition
        ):
            raise HubStoreV2Conflict("group_close_state_conflict")

        for pointer in self.store.list_entities(CURRENT_GROUP_ENTITY):
            if pointer["record"].get("work_group_id") != group_id:
                continue
            replacement = deepcopy(pointer["record"])
            replacement.update({"work_group_id": "", "updated_at": now})
            self.store.put_entity(
                CURRENT_GROUP_ENTITY,
                pointer["entity_id"],
                replacement,
                expected_revision=pointer["revision"],
            )
        self._upsert_entity(
            OPERATION_GROUP_ENTITY,
            str(operation["operation_id"]),
            {
                "operation_id": operation["operation_id"],
                "work_group_id": group_id,
                "kind": "group_close",
            },
        )
        current = self.store.get_operation(str(operation["operation_id"])) or dict(operation)
        terminal = (
            self._complete_hub_operation(
                current,
                public_envelope(
                    "ok", result={"work_group_id": group_id, "outcome": outcome}
                ),
            )
            if str(current.get("state") or "") not in TERMINAL_OPERATION_STATES
            else current
        )
        saved = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
        if saved is None:
            raise HubStoreV2Conflict("group_close_entity_missing")
        return self._group_envelope(saved["record"], operation=terminal)

    def _finish_group_resume(
        self,
        *,
        group_id: str,
        operation: Mapping[str, Any],
        idempotency_key: str,
        identity: ManagerIdentity,
        takeover: bool,
        takeover_reason: str,
    ) -> dict[str, Any]:
        """Finish or replay one durable resume without reapplying terminal side effects."""

        group_entity = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
        if group_entity is None:
            raise HubStoreV2Conflict("group_resume_entity_missing")
        if str(operation.get("state") or "") in TERMINAL_OPERATION_STATES:
            return self._group_envelope(group_entity["record"], operation=operation)
        group = deepcopy(group_entity["record"])
        if group.get("status") != "open":
            raise HubStoreV2Conflict("group_resume_state_conflict")
        participants = list(group.get("participants") or [])
        if identity.participant_ref not in participants:
            participants.append(identity.participant_ref)
        group["participants"] = participants
        group["active_participant_ref"] = identity.participant_ref
        group["updated_at"] = self._clock()
        group["last_takeover_reason"] = (
            _optional_text(takeover_reason, 500) if takeover else ""
        )
        preflight = self._create_preflight_operation(
            group,
            idempotency_key=f"{idempotency_key}:preflight",
            reason="group_resume",
        )
        readiness = (
            deepcopy(dict(group.get("readiness") or {}))
            if isinstance(group.get("readiness"), Mapping)
            else {}
        )
        if str(readiness.get("operation_id") or "") != str(preflight["operation_id"]):
            group["readiness"] = {
                "status": "pending",
                "reason": "strict_preflight_refresh_required",
                "operation_id": preflight["operation_id"],
                "updated_at": self._clock(),
            }
        saved = self.store.put_entity(
            WORK_GROUP_ENTITY,
            group_id,
            group,
            expected_revision=group_entity["revision"],
        )
        self._persist_participation(identity, group_id, takeover=takeover)
        self._upsert_entity(
            OPERATION_GROUP_ENTITY,
            str(operation["operation_id"]),
            {
                "operation_id": operation["operation_id"],
                "work_group_id": group_id,
                "kind": "group_resume",
            },
        )
        current = self.store.get_operation(str(operation["operation_id"])) or dict(operation)
        terminal = self._complete_hub_operation(
            current, public_envelope("ok", result={"work_group_id": group_id})
        )
        return self._group_envelope(saved["record"], operation=terminal)

    def create_work_group(
        self,
        *,
        title: str,
        goal: str,
        idempotency_key: str,
        workspace_ref: str = "",
        repo_path: str = "",
        machine_id: str = "",
        allowed_machine_ids: Any = None,
        required_tags: Any = None,
        lanes: Any = None,
        visibility: str = "private",
        shared_write_policy: str = "serialized",
        execution_mode: str = "end_to_end",
        definition_of_done: str = "",
        wait_for_preflight_seconds: int = 0,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        del wait_for_preflight_seconds  # Network waiting belongs to the broker/transport boundary.
        title_value = _clean_text(title, field="title", maximum=160)
        goal_value = _clean_text(goal, field="goal", maximum=8_000)
        key = _clean_text(idempotency_key, field="idempotency_key", maximum=256)
        visibility_value = str(visibility or "private")
        if visibility_value not in {"private", "shared"}:
            raise ValueError("visibility must be private or shared")
        shared_write_policy_value = str(shared_write_policy or "serialized").strip().lower()
        if shared_write_policy_value not in {"serialized", "manager_controlled"}:
            raise ValueError("shared_write_policy must be serialized or manager_controlled")
        execution_mode_value = str(execution_mode or "end_to_end").strip().lower()
        if execution_mode_value not in {"end_to_end", "asynchronous_handoff"}:
            raise ValueError("execution_mode must be end_to_end or asynchronous_handoff")
        definition_of_done_value = _optional_text(definition_of_done, 8_000) or goal_value
        identity = self._manager_identity(context)
        target = str(workspace_ref or repo_path or machine_id or "fleet")
        payload = {
            "title": title_value,
            "goal": goal_value,
            "workspace_ref": workspace_ref,
            "repo_path": repo_path,
            "machine_id": machine_id,
            "allowed_machine_ids": _string_list(allowed_machine_ids, field="allowed_machine_ids"),
            "required_tags": _string_list(required_tags, field="required_tags"),
            "lanes": lanes or [],
            "visibility": visibility_value,
            "shared_write_policy": shared_write_policy_value,
            "execution_mode": execution_mode_value,
            "definition_of_done": definition_of_done_value,
        }
        operation = self.broker.create_operation(
            tool="patchbay_work_group_create",
            logical_target=target,
            idempotency_key=key,
            payload=payload,
            principal_ref=identity.principal_ref,
        )
        group_id = stable_ref(
            "group", str(operation["operation_id"]), salt=self._identity_salt
        )
        association = self.store.get_entity(OPERATION_GROUP_ENTITY, operation["operation_id"])
        if operation.get("idempotent_replay") and association:
            group_id = str(association["record"].get("work_group_id") or "")
            group_entity = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
            if group_entity:
                return self._finish_group_creation(
                    group_id=group_id,
                    operation=operation,
                    idempotency_key=key,
                    identity=identity,
                )
        if operation.get("idempotent_replay"):
            # The deterministic identity closes the crash window between the
            # group write and its operation association.  Replaying the parent
            # finishes the same group and preflight instead of inventing a new
            # task object.
            recovered_group = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
            if recovered_group is not None:
                return self._finish_group_creation(
                    group_id=group_id,
                    operation=operation,
                    idempotency_key=key,
                    identity=identity,
                )
            if str(operation.get("state") or "") in TERMINAL_OPERATION_STATES:
                saved = operation.get("result")
                if isinstance(saved, Mapping) and str(saved.get("status") or "") != "ok":
                    replay = deepcopy(dict(saved))
                    replay["operation"] = self._operation_summary(operation)
                    return replay
                return public_envelope(
                    "blocked",
                    result={
                        "reason": "group_create_recovery_required",
                        "operation_id": operation["operation_id"],
                    },
                    operation=self._operation_summary(operation),
                )

        if not machine_id and not self.routing_enabled:
            blocked = public_envelope(
                "blocked",
                result={
                    "reason": "routing_disabled",
                    "routing_enabled": False,
                    "recommended_next_action": "Retry with an explicit machine_id.",
                },
            )
            terminal = self._complete_hub_operation(operation, blocked)
            blocked["operation"] = self._operation_summary(terminal)
            return blocked

        selected, candidates, rejections = self._place_group(
            workspace_ref=workspace_ref,
            repo_path=repo_path,
            machine_id=machine_id,
            allowed_machine_ids=allowed_machine_ids,
            required_tags=required_tags,
        )
        if selected is None:
            blocked = public_envelope(
                "blocked",
                result={"reason": "no_eligible_machine", "candidate_summary": [], "rejection_summary": rejections},
            )
            terminal = self._complete_hub_operation(operation, blocked)
            blocked["operation"] = self._operation_summary(terminal)
            return blocked

        now = self._clock()
        projection = selected["projection"]
        group = {
            "work_group_id": group_id,
            "create_operation_id": operation["operation_id"],
            "principal_ref": identity.principal_ref,
            "title": title_value,
            "goal": goal_value,
            "status": "open",
            "lifecycle": "open",
            "visibility": visibility_value,
            "routing_policy": "keep_together",
            "shared_write_policy": shared_write_policy_value,
            "execution_mode": execution_mode_value,
            "definition_of_done": definition_of_done_value,
            "workspace_ref": str(projection.get("workspace_ref") or workspace_ref or ""),
            "workspace_projection_ref": str(projection.get("workspace_projection_ref") or ""),
            "repository_identity": str(projection.get("repository_identity") or ""),
            "requested_repo_path": str(repo_path or ""),
            "resolved_repo_path": str(projection.get("resolved_path") or projection.get("local_path") or repo_path or ""),
            "pinned_machine_id": selected["machine_id"],
            "pinned_edge_generation": selected["edge_generation"],
            "allowed_machine_ids": payload["allowed_machine_ids"],
            "required_tags": payload["required_tags"],
            "lanes": self._normalize_lanes(lanes),
            "participants": [identity.participant_ref],
            "active_participant_ref": identity.participant_ref,
            "readiness": {"status": "pending", "reason": "strict_preflight_required"},
            "routing": {
                "mode": "explicit_machine" if machine_id else "availability_only",
                "selected_machine_id": selected["machine_id"],
                "selection_score": selected["score"],
            },
            "created_at": now,
            "updated_at": now,
            "supersedes": "",
            "superseded_by": "",
        }
        self.store.put_entity(WORK_GROUP_ENTITY, group_id, group, expected_revision=0)
        return self._finish_group_creation(
            group_id=group_id,
            operation=operation,
            idempotency_key=key,
            identity=identity,
            candidates=candidates,
            rejections=rejections,
        )

    def record_preflight_result(
        self,
        *,
        work_group_id: str,
        operation_id: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        group_entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id)
        if group_entity is None:
            raise ValueError(f"Unknown work_group_id: {work_group_id}")
        group = deepcopy(group_entity["record"])
        if group.get("readiness", {}).get("operation_id") != operation_id:
            raise ValueError("Preflight operation does not belong to this group")
        operation = self.store.get_operation(operation_id)
        if operation is None or operation.get("logical_target") != work_group_id:
            raise ValueError("Unknown preflight operation")
        facts = deepcopy(dict(result))
        blockers: list[str] = []
        if not facts.get("ok") or facts.get("repo_exists") is False:
            blockers.append("workspace_missing")
        expected_path = _normalize_path(group.get("resolved_repo_path"))
        resolved_path = _normalize_path(facts.get("repo_resolved") or expected_path)
        if expected_path and resolved_path != expected_path:
            blockers.append("workspace_path_mismatch")
        expected_identity = str(group.get("repository_identity") or "")
        actual_identity = str(facts.get("repository_identity") or facts.get("repo_identity") or "")
        if expected_identity and actual_identity and expected_identity != actual_identity:
            blockers.append("repository_identity_mismatch")
        disk_free = _as_int(facts.get("disk_free_bytes"), -1)
        if 0 <= disk_free < self.min_disk_free_bytes:
            blockers.append("disk_feasibility_blocked")
        if _as_int(facts.get("free_worker_slots"), 1) <= 0 and not facts.get("queue_enabled"):
            blockers.append("capacity_blocked")
        status = "failed" if blockers else "ready"
        observed_at = self._clock()
        facts_revision = str(
            facts.get("head")
            or facts.get("head_revision")
            or facts.get("base_revision")
            or facts.get("revision")
            or ""
        )
        group["readiness"] = {
            "status": status,
            "operation_id": operation_id,
            "facts": facts,
            "blockers": blockers,
            "observed_at": observed_at,
            "facts_revision": facts_revision,
            "currentness": "current" if not blockers else "failed",
            "updated_at": observed_at,
        }
        saved = self.store.put_entity(
            WORK_GROUP_ENTITY, work_group_id, group, expected_revision=group_entity["revision"]
        )
        terminal_status = "blocked" if blockers else "ok"
        terminal = self._complete_hub_operation(
            operation,
            public_envelope(terminal_status, result={"preflight": facts, "blockers": blockers}),
        )
        return self._group_envelope(saved["record"], operation=terminal)

    def mark_group_preflight_refresh_required(
        self,
        *,
        work_group_id: str,
        reason: str,
        source_operation_id: str = "",
    ) -> dict[str, Any] | None:
        """Label persisted preflight facts as a historical snapshot after base mutation."""
        if not work_group_id:
            return None
        entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id)
        if entity is None:
            return None
        group = deepcopy(entity["record"])
        readiness = deepcopy(dict(group.get("readiness") or {}))
        if readiness.get("status") not in {"ready", "failed"}:
            return group
        now = self._clock()
        readiness.update(
            {
                "currentness": "refresh_required",
                "stale_reason": str(reason or "base_checkout_may_have_changed")[:200],
                "stale_since": now,
                "stale_source_operation_id": str(source_operation_id or ""),
                "updated_at": now,
            }
        )
        group["readiness"] = readiness
        group["updated_at"] = now
        saved = self.store.put_entity(
            WORK_GROUP_ENTITY,
            work_group_id,
            group,
            expected_revision=entity["revision"],
        )
        self.store.append_event(
            "work_group.preflight_refresh_required",
            {
                "work_group_id": work_group_id,
                "reason": readiness["stale_reason"],
                "source_operation_id": readiness["stale_source_operation_id"],
            },
            entity_type=WORK_GROUP_ENTITY,
            entity_id=work_group_id,
            entity_revision=saved["revision"],
        )
        return deepcopy(saved["record"])

    def record_group_base_mutation_snapshot(
        self,
        *,
        work_group_id: str,
        snapshot: Mapping[str, Any],
        reason: str,
        source_operation_id: str = "",
    ) -> dict[str, Any] | None:
        """Reconcile current Git facts from an authoritative completed mutation."""
        if not work_group_id:
            return None
        entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id)
        if entity is None:
            return None
        group = deepcopy(entity["record"])
        if group.get("status") != "open":
            return group
        readiness = deepcopy(dict(group.get("readiness") or {}))
        facts = deepcopy(dict(readiness.get("facts") or {}))
        git = deepcopy(dict(facts.get("git") or {}))
        changed_files = [str(value) for value in snapshot.get("changed_files") or []]
        dirty = bool(snapshot.get("dirty", changed_files))
        head = str(snapshot.get("head") or facts.get("head") or git.get("commit") or "")
        git.update(
            {
                "commit": head,
                "dirty": dirty,
                "status_short": changed_files,
            }
        )
        facts.update(
            {
                "head": head,
                "git": git,
                "dirty_status_summary": (
                    "clean" if not dirty else f"{len(changed_files)} changed/untracked paths"
                ),
            }
        )
        now = self._clock()
        readiness.update(
            {
                "status": "ready",
                "currentness": "current",
                "facts": facts,
                "facts_revision": head,
                "observed_at": float(snapshot.get("observed_at") or now),
                "mutation_reconciled_reason": str(reason or "base_checkout_changed")[:200],
                "mutation_source_operation_id": str(source_operation_id or ""),
                "updated_at": now,
            }
        )
        readiness.pop("stale_reason", None)
        readiness.pop("stale_since", None)
        readiness.pop("stale_source_operation_id", None)
        group["readiness"] = readiness
        group["updated_at"] = now
        saved = self.store.put_entity(
            WORK_GROUP_ENTITY,
            work_group_id,
            group,
            expected_revision=entity["revision"],
        )
        self.store.append_event(
            "work_group.base_mutation_snapshot_reconciled",
            {
                "work_group_id": work_group_id,
                "reason": readiness["mutation_reconciled_reason"],
                "source_operation_id": readiness["mutation_source_operation_id"],
            },
            entity_type=WORK_GROUP_ENTITY,
            entity_id=work_group_id,
            entity_revision=saved["revision"],
        )
        return deepcopy(saved["record"])

    def list_work_groups(
        self,
        *,
        scope: str = "current",
        status: str = "",
        workspace_ref: str = "",
        machine_id: str = "",
        query: str = "",
        include_closed: bool = False,
        cursor: str = "",
        limit: int = 20,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        identity = self._manager_identity(context)
        if scope not in {"current", "owned", "recent", "history"}:
            raise ValueError("scope must be current, owned, recent, or history")
        current_id = self._current_group_id(identity)
        query_text = str(query or "").casefold()
        values: list[dict[str, Any]] = []
        hidden_closed = 0
        for entity in self.store.list_entities(WORK_GROUP_ENTITY):
            group = entity["record"]
            if group.get("principal_ref") != identity.principal_ref:
                continue
            if not include_closed and group.get("status") != "open":
                hidden_closed += 1
                continue
            if scope == "current" and group.get("work_group_id") != current_id:
                continue
            if scope == "recent" and identity.participant_ref not in set(group.get("participants") or []):
                continue
            if workspace_ref and group.get("workspace_ref") != workspace_ref:
                continue
            if machine_id and group.get("pinned_machine_id") != machine_id:
                continue
            group_id = str(group["work_group_id"])
            snapshot = self.store.work_group_status_projection(
                group_id,
                operation_limit=0,
                worker_limit=DEFAULT_GROUP_STATUS_DETAIL_LIMIT,
                integration_limit=0,
            )
            activity = self._summary_activity_and_contract(
                group,
                worker_summary=snapshot["worker_summary"],
                operation_summary=snapshot["operation_summary"],
            )[0]["activity"]
            if status and status not in {
                group.get("status"),
                group.get("readiness", {}).get("status"),
                group.get("outcome"),
                activity,
            }:
                continue
            haystack = " ".join(
                str(group.get(field) or "")
                for field in ("title", "goal", "workspace_ref", "resolved_repo_path", "pinned_machine_id")
            ).casefold()
            if query_text and query_text not in haystack:
                continue
            values.append(
                self._public_group(
                    group,
                    workers=snapshot["workers"],
                    worker_summary=snapshot["worker_summary"],
                    operation_summary=snapshot["operation_summary"],
                    lane_summaries=snapshot["lane_summaries"],
                )
            )
        values.sort(key=lambda item: _as_float(item.get("updated_at"), 0), reverse=True)
        start = max(0, _as_int(cursor, 0))
        bounded = max(1, min(_as_int(limit, 20), 100))
        page = values[start : start + bounded]
        next_cursor = str(start + bounded) if start + bounded < len(values) else ""
        return public_envelope(
            "ok",
            result={
                "work_groups": page,
                "count": len(values),
                "hidden_counts": {"closed": hidden_closed, "other_principal": 0},
                "next_cursor": next_cursor,
                "current_work_group_id": current_id,
            },
        )

    def work_group_status(
        self,
        *,
        work_group_id: str = "",
        since_revision: int = 0,
        wait_for_change_seconds: int = 0,
        include_workers: bool = True,
        include_operations: bool = True,
        include_integrations: bool = True,
        worker_cursor: str = "",
        worker_limit: int = DEFAULT_GROUP_STATUS_DETAIL_LIMIT,
        operation_cursor: str = "",
        operation_limit: int = DEFAULT_GROUP_STATUS_DETAIL_LIMIT,
        integration_cursor: str = "",
        integration_limit: int = DEFAULT_GROUP_STATUS_DETAIL_LIMIT,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        del since_revision, wait_for_change_seconds
        identity = self._manager_identity(context)
        group_id = str(work_group_id or self._current_group_id(identity) or "")
        if not group_id:
            return public_envelope("not_found", result={"reason": "no_current_work_group"})
        entity = self.store.get_entity(WORK_GROUP_ENTITY, group_id)
        if entity is None or entity["record"].get("principal_ref") != identity.principal_ref:
            return public_envelope("not_found", result={"reason": "work_group_not_found"})
        return self._group_envelope(
            entity["record"],
            include_workers=include_workers,
            include_operations=include_operations,
            include_integrations=include_integrations,
            worker_offset=max(0, _as_int(worker_cursor, 0)),
            worker_limit=max(
                1,
                min(
                    _as_int(worker_limit, DEFAULT_GROUP_STATUS_DETAIL_LIMIT),
                    MAX_GROUP_STATUS_DETAIL_LIMIT,
                ),
            ),
            operation_offset=max(0, _as_int(operation_cursor, 0)),
            operation_limit=max(
                1,
                min(
                    _as_int(operation_limit, DEFAULT_GROUP_STATUS_DETAIL_LIMIT),
                    MAX_GROUP_STATUS_DETAIL_LIMIT,
                ),
            ),
            integration_offset=max(0, _as_int(integration_cursor, 0)),
            integration_limit=max(
                1,
                min(
                    _as_int(integration_limit, DEFAULT_GROUP_STATUS_DETAIL_LIMIT),
                    MAX_GROUP_STATUS_DETAIL_LIMIT,
                ),
            ),
        )

    def resume_work_group(
        self,
        *,
        work_group_id: str,
        idempotency_key: str,
        takeover: bool = False,
        takeover_reason: str = "",
        wait_for_preflight_seconds: int = 0,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        del wait_for_preflight_seconds
        identity = self._manager_identity(context)
        group_entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id)
        if group_entity is None or group_entity["record"].get("principal_ref") != identity.principal_ref:
            return public_envelope("not_found", result={"reason": "work_group_not_found"})
        group = deepcopy(group_entity["record"])
        if group.get("status") != "open":
            return public_envelope("blocked", result={"reason": "closed_group_cannot_resume"})
        active = str(group.get("active_participant_ref") or "")
        if active and active != identity.participant_ref and not takeover:
            return public_envelope(
                "blocked",
                result={"reason": "active_participant_requires_takeover", "active_participant_ref": active},
            )
        if takeover and active and active != identity.participant_ref and not str(takeover_reason or "").strip():
            raise ValueError("takeover_reason is required when taking over another active participant")
        key = _clean_text(idempotency_key, field="idempotency_key", maximum=256)
        operation = self.broker.create_operation(
            tool="patchbay_work_group_resume",
            logical_target=work_group_id,
            idempotency_key=key,
            payload={"work_group_id": work_group_id, "takeover": takeover, "takeover_reason": takeover_reason},
            principal_ref=identity.principal_ref,
        )
        return self._finish_group_resume(
            group_id=work_group_id,
            operation=operation,
            idempotency_key=key,
            identity=identity,
            takeover=takeover,
            takeover_reason=takeover_reason,
        )

    def reassign_work_group(
        self,
        *,
        work_group_id: str,
        reason: str,
        idempotency_key: str,
        machine_id: str = "",
        allowed_machine_ids: Any = None,
        required_tags: Any = None,
        carry_context: str = "reports",
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        if carry_context not in {"reports", "reports_and_changes", "none"}:
            raise ValueError("carry_context must be reports, reports_and_changes, or none")
        identity = self._manager_identity(context)
        predecessor_entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id)
        if predecessor_entity is None or predecessor_entity["record"].get("principal_ref") != identity.principal_ref:
            return public_envelope("not_found", result={"reason": "work_group_not_found"})
        predecessor = deepcopy(predecessor_entity["record"])
        key = _clean_text(idempotency_key, field="idempotency_key", maximum=256)
        operation = self.broker.create_operation(
            tool="patchbay_work_group_reassign",
            logical_target=work_group_id,
            idempotency_key=key,
            payload={
                "work_group_id": work_group_id,
                "reason": reason,
                "machine_id": machine_id,
                "allowed_machine_ids": _string_list(
                    allowed_machine_ids, field="allowed_machine_ids"
                ),
                "required_tags": _string_list(required_tags, field="required_tags"),
                "carry_context": carry_context,
            },
            principal_ref=identity.principal_ref,
        )
        association = self.store.get_entity(
            OPERATION_GROUP_ENTITY, operation["operation_id"]
        )
        if operation.get("idempotent_replay") and association:
            associated_successor_id = str(
                association["record"].get("work_group_id") or ""
            )
            successor_entity = self.store.get_entity(
                WORK_GROUP_ENTITY, associated_successor_id
            )
            if successor_entity:
                return self._finish_group_reassignment(
                    predecessor_id=work_group_id,
                    successor_id=associated_successor_id,
                    operation=operation,
                    idempotency_key=key,
                    identity=identity,
                )
        successor_id = stable_ref(
            "group",
            str(operation["operation_id"]),
            "successor",
            salt=self._identity_salt,
        )
        if operation.get("idempotent_replay"):
            recovered_successor = self.store.get_entity(
                WORK_GROUP_ENTITY, successor_id
            )
            if recovered_successor is not None:
                return self._finish_group_reassignment(
                    predecessor_id=work_group_id,
                    successor_id=successor_id,
                    operation=operation,
                    idempotency_key=key,
                    identity=identity,
                )
        coordination = self._coordination_blocker(predecessor, identity)
        if coordination:
            blocked = public_envelope("blocked", result={"reason": coordination})
            terminal = self._complete_hub_operation(operation, blocked)
            blocked["operation"] = self._operation_summary(terminal)
            return blocked
        if predecessor.get("status") != "open":
            blocked = public_envelope(
                "blocked", result={"reason": "closed_group_cannot_reassign"}
            )
            terminal = self._complete_hub_operation(operation, blocked)
            blocked["operation"] = self._operation_summary(terminal)
            return blocked
        if not machine_id and not self.routing_enabled:
            blocked = public_envelope(
                "blocked",
                result={
                    "reason": "routing_disabled",
                    "routing_enabled": False,
                    "recommended_next_action": "Retry with an explicit machine_id.",
                },
            )
            terminal = self._complete_hub_operation(operation, blocked)
            blocked["operation"] = self._operation_summary(terminal)
            return blocked
        selected, candidates, rejections = self._place_group(
            workspace_ref=str(predecessor.get("workspace_ref") or ""),
            repo_path=str(predecessor.get("requested_repo_path") or ""),
            machine_id=machine_id,
            allowed_machine_ids=allowed_machine_ids or predecessor.get("allowed_machine_ids") or [],
            required_tags=required_tags or predecessor.get("required_tags") or [],
            exclude_pin=(
                str(predecessor.get("pinned_machine_id") or ""),
                str(predecessor.get("pinned_edge_generation") or ""),
            ),
        )
        if selected is None:
            blocked = public_envelope(
                "blocked",
                result={"reason": "no_eligible_successor", "rejection_summary": rejections},
            )
            terminal = self._complete_hub_operation(operation, blocked)
            blocked["operation"] = self._operation_summary(terminal)
            return blocked
        successor = create_successor_group(
            predecessor,
            machine_id=selected["machine_id"],
            edge_generation=selected["edge_generation"],
            reason=_clean_text(reason, field="reason", maximum=1_000),
            successor_id=successor_id,
            now=self._clock(),
        )
        successor.update(
            {
                "create_operation_id": operation["operation_id"],
                "principal_ref": identity.principal_ref,
                "status": "open",
                "lifecycle": "open",
                "workspace_projection_ref": selected["workspace_projection_ref"],
                "repository_identity": str(selected["projection"].get("repository_identity") or ""),
                "requested_repo_path": str(predecessor.get("requested_repo_path") or ""),
                "resolved_repo_path": selected["resolved_path"],
                "allowed_machine_ids": _string_list(allowed_machine_ids, field="allowed_machine_ids")
                or list(predecessor.get("allowed_machine_ids") or []),
                "required_tags": _string_list(required_tags, field="required_tags")
                or list(predecessor.get("required_tags") or []),
                "participants": [identity.participant_ref],
                "active_participant_ref": identity.participant_ref,
                "carry_context": carry_context,
                "readiness": {"status": "pending", "reason": "strict_preflight_required"},
                "routing": {
                    "mode": "explicit_machine" if machine_id else "availability_only",
                    "selected_machine_id": selected["machine_id"],
                    "selection_score": selected["score"],
                },
            }
        )
        self.store.put_entity(WORK_GROUP_ENTITY, successor_id, successor, expected_revision=0)
        return self._finish_group_reassignment(
            predecessor_id=work_group_id,
            successor_id=successor_id,
            operation=operation,
            idempotency_key=key,
            identity=identity,
            candidates=candidates,
            rejections=rejections,
        )

    def close_work_group(
        self,
        *,
        work_group_id: str,
        outcome: str,
        summary: str,
        worker_dispositions: Any,
        idempotency_key: str,
        active_work_disposition: str = "refuse",
        cleanup_completed_workspaces: bool = False,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        del cleanup_completed_workspaces  # Cleanup remains Edge-owned.
        if outcome not in {"complete", "partial", "abandoned", "failed"}:
            raise ValueError("outcome must be complete, partial, abandoned, or failed")
        if active_work_disposition not in {"refuse", "stop", "leave_running"}:
            raise ValueError("active_work_disposition must be refuse, stop, or leave_running")
        identity = self._manager_identity(context)
        group_entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id)
        if group_entity is None or group_entity["record"].get("principal_ref") != identity.principal_ref:
            return public_envelope("not_found", result={"reason": "work_group_not_found"})
        group = deepcopy(group_entity["record"])
        if group.get("status") == "open":
            coordination = self._coordination_blocker(group, identity)
            if coordination:
                return public_envelope("blocked", result={"reason": coordination})
        workers = self._workers_for_group(work_group_id)
        dispositions, disposition_errors = self._normalize_dispositions(worker_dispositions, workers)
        validation = validate_close_dispositions(workers, dispositions, outcome=outcome)
        blockers = list(validation["blockers"])
        blockers.extend(disposition_errors)
        active_workers = [worker for worker in workers if worker.get("turn_state") in ACTIVE_TURN_STATES]
        if active_workers and active_work_disposition == "refuse":
            blockers.append({"reason": "active_work_disposition_refuse"})
        if active_workers and active_work_disposition == "stop":
            blockers.append({"reason": "worker_stop_operation_required"})
        if outcome == "complete":
            if active_work_disposition == "leave_running" or any(
                disposition == "leave_running" for disposition in dispositions.values()
            ):
                blockers.append({"reason": "leave_running_cannot_complete"})
        preliminarily_accepted = (
            validation["accepted"]
            and not disposition_errors
            and not blockers
            and not validation["missing_dispositions"]
            and not validation["invalid_dispositions"]
        )
        if not preliminarily_accepted:
            return public_envelope(
                "blocked",
                result={
                    "reason": "close_disposition_refused",
                    "validation": {**validation, "blockers": blockers},
                    "workers": workers,
                },
            )

        summary_value = _clean_text(summary, field="summary", maximum=8_000)
        key = _clean_text(
            idempotency_key,
            field="idempotency_key",
            maximum=256,
        )
        existing_close = self.store.get_operation_by_idempotency(
            tool="patchbay_work_group_close",
            logical_target=work_group_id,
            idempotency_key=key,
            principal_ref=identity.principal_ref,
        )
        existing_close_id = str(
            (existing_close or {}).get("operation_id") or ""
        )
        operations = self._operations_for_group(work_group_id)
        unsafe_operations = [
            item
            for item in operations
            if str(item.get("operation_id") or "") != existing_close_id
            if item.get("state") in {"running", "outcome_unknown", "reconciling"}
        ]
        if unsafe_operations:
            # Refusal is validation, not durable work. Do not create a close
            # operation that would itself remain active and poison the group's
            # completion contract after the original work becomes terminal.
            return public_envelope(
                "blocked",
                result={
                    "reason": "close_disposition_refused",
                    "validation": {
                        **validation,
                        "blockers": [
                            {
                                "reason": "active_or_uncertain_operations",
                                "count": len(unsafe_operations),
                            }
                        ],
                    },
                    "workers": workers,
                },
            )
        operation = self.broker.create_operation(
            tool="patchbay_work_group_close",
            logical_target=work_group_id,
            idempotency_key=key,
            payload={
                "work_group_id": work_group_id,
                "outcome": outcome,
                "summary": summary_value,
                "worker_dispositions": dispositions,
                "active_work_disposition": active_work_disposition,
            },
            principal_ref=identity.principal_ref,
        )
        if group.get("status") != "open" and not operation.get("idempotent_replay"):
            blocked = public_envelope(
                "blocked", result={"reason": "closed_group_is_immutable"}
            )
            terminal = self._complete_hub_operation(operation, blocked)
            blocked["operation"] = self._operation_summary(terminal)
            return blocked

        for pending in operations:
            if (
                str(pending.get("operation_id") or "")
                != str(operation["operation_id"])
                and pending.get("state")
                in {"created", "payload_ready", "dispatchable"}
            ):
                self.broker.cancel_operation(
                    pending["operation_id"],
                    expected_revision=pending["revision"],
                    reason="work_group_closed_before_claim",
                )
        return self._finish_group_close(
            group_id=work_group_id,
            operation=operation,
            outcome=outcome,
            summary=summary_value,
            dispositions=dispositions,
            active_work_disposition=active_work_disposition,
        )

    # -- Public projections and operation recovery ----------------------

    def fleet_status(
        self,
        *,
        include_offline: bool = True,
        include_retired: bool = False,
        query: str = "",
        tags: Any = None,
        include_workspaces: bool = False,
        since_revision: int = 0,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        del since_revision
        identity = self._manager_identity(context)
        query_text = str(query or "").casefold()
        wanted_tags = set(_string_list(tags, field="tags"))
        machines: list[dict[str, Any]] = []
        hidden_retired = 0
        for entity in self.store.list_entities(MACHINE_ENTITY):
            machine = self._public_machine(entity["record"], now=self._clock())
            if machine["status"] == "retired" and not include_retired:
                hidden_retired += 1
                continue
            if machine["status"] == "offline" and not include_offline:
                continue
            if wanted_tags and not wanted_tags.issubset(set(machine.get("tags") or [])):
                continue
            haystack = " ".join(
                [str(machine.get("machine_id") or ""), str(machine.get("display_name") or ""), *machine.get("tags", [])]
            ).casefold()
            if query_text and query_text not in haystack:
                continue
            if include_workspaces:
                machine["workspaces"] = [
                    deepcopy(projection["record"])
                    for projection in self.store.list_entities(WORKSPACE_PROJECTION_ENTITY)
                    if projection["record"].get("machine_id") == machine["machine_id"]
                    and projection["record"].get("edge_generation") == machine["edge_generation"]
                    and projection["record"].get("active")
                ]
            machines.append(machine)
        machines.sort(key=lambda item: (item["status"] != "online", str(item["display_name"]).casefold()))
        current_id = self._current_group_id(identity)
        current = self.store.get_entity(WORK_GROUP_ENTITY, current_id) if current_id else None
        owned = [
            self._public_group(entity["record"], workers=self._workers_for_group(entity["entity_id"]))
            for entity in self.store.list_entities(WORK_GROUP_ENTITY)
            if entity["record"].get("principal_ref") == identity.principal_ref
            and entity["record"].get("status") == "open"
        ]
        counts = {
            "online": sum(item["status"] == "online" for item in machines),
            "offline": sum(item["status"] == "offline" for item in machines),
            "retired": sum(item["status"] == "retired" for item in machines),
            "incompatible": sum(item["compatibility"] != "compatible" for item in machines),
            "hidden_retired": hidden_retired,
        }
        return public_envelope(
            "ok",
            result={
                "hub": {"principal_ref": identity.principal_ref, **self.store.schema_info()},
                "contract_version": HUB_V2_CONTRACT_VERSION,
                "manifest_hash": HUB_V2_MANIFEST_HASH,
                "schema_hash": HUB_V2_SCHEMA_HASH,
                "routing_enabled": self.routing_enabled,
                "counts": counts,
                "machines": machines,
                "current_work_group": self._public_group(current["record"], workers=self._workers_for_group(current_id)) if current else {},
                "owned_active_groups": owned,
            },
        )

    async def operation_status(
        self,
        *,
        operation_id: str,
        wait_seconds: int = 0,
        include_result: bool = False,
        since_revision: int = 0,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        identity = self._manager_identity(context)
        envelope = await self.broker.operation_status(
            operation_id,
            principal_ref=identity.principal_ref,
            wait_seconds=wait_seconds,
            include_result=include_result,
            since_revision=since_revision,
        )
        operation = self.store.get_operation(operation_id)
        if operation is not None and operation.get("principal_ref") == identity.principal_ref:
            envelope["operation"] = self._operation_summary(operation)
        revision = int(
            ((envelope.get("result") or {}).get("dispatch") or {}).get("event_revision")
            or (envelope.get("operation") or {}).get("revision")
            or 0
        )
        operation_state = str((envelope.get("operation") or {}).get("state") or "")
        internal_wait_states = {
            "created",
            "payload_ready",
            "dispatchable",
            "running",
            "outcome_unknown",
            "reconciling",
        }
        normalized_actions: list[Any] = []
        for item in envelope.get("next_actions") or []:
            if not isinstance(item, Mapping) or item.get("tool") or not item.get("action"):
                normalized_actions.append(item)
                continue
            if operation_state in internal_wait_states:
                normalized_actions.append(
                    {
                        "tool": "patchbay_operation_status",
                        "arguments": {
                            "operation_id": operation_id,
                            "wait_seconds": 20,
                            "since_revision": revision,
                        },
                        "reason": str(item["action"]),
                    }
                )
            else:
                # Terminal guidance such as "use_domain_result" is advice, not
                # a callable MCP tool. Keep it as a plain action string so the
                # manager never sees an invented tool name.
                normalized_actions.append(str(item["action"]))
        envelope["next_actions"] = normalized_actions
        return envelope

    # -- Internal projection helpers ------------------------------------

    def _operations_for_group(self, work_group_id: str) -> list[dict[str, Any]]:
        operation_ids = set(self.store.operation_ids_for_work_group(work_group_id))
        rows = self.store.connection.execute(
            """
            SELECT operation.operation_id
            FROM operations AS operation
            WHERE operation.logical_target = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM entity_records AS association
                  WHERE association.entity_type = ?
                    AND association.entity_id = operation.operation_id
              )
            ORDER BY operation.created_at, operation.operation_id
            """,
            (work_group_id, OPERATION_GROUP_ENTITY),
        ).fetchall()
        operation_ids.update(str(row["operation_id"]) for row in rows)
        return [
            operation
            for operation_id in sorted(operation_ids)
            if (operation := self.store.get_operation(operation_id)) is not None
        ]

    def _cancel_unclaimed_group_operations(
        self, work_group_id: str, *, exclude_operation_ids: set[str] | None = None
    ) -> list[str]:
        excluded = exclude_operation_ids or set()
        operations = self._operations_for_group(work_group_id)
        operations.sort(key=lambda item: (not bool(item.get("parent_operation_id")), item["created_at"]))
        cancelled: list[str] = []
        for operation in operations:
            operation_id = str(operation["operation_id"])
            if operation_id in excluded or operation.get("state") not in {
                "created",
                "payload_ready",
                "dispatchable",
            }:
                continue
            active_children = self.store.connection.execute(
                """
                SELECT 1 FROM operations
                WHERE parent_operation_id = ?
                  AND state NOT IN ('succeeded', 'blocked', 'failed', 'cancelled')
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            active_attempt = self.store.connection.execute(
                """
                SELECT 1 FROM attempts
                WHERE operation_id = ?
                  AND state IN ('claimed', 'executing', 'effect_recorded', 'result_ready', 'reconciling')
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if active_children is not None or active_attempt is not None:
                continue
            result = self.broker.cancel_operation(
                operation_id,
                expected_revision=int(operation["revision"]),
                reason="work_group_reassigned_before_claim",
            )
            if result is not None:
                cancelled.append(operation_id)
        return cancelled

    @staticmethod
    def _summary_activity_and_contract(
        group: Mapping[str, Any],
        *,
        worker_summary: Mapping[str, Any],
        operation_summary: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        counts = {
            "workers": _as_int(worker_summary.get("total"), 0),
            "active": _as_int(worker_summary.get("active"), 0),
            "quiet": _as_int(worker_summary.get("quiet"), 0),
            "stale": _as_int(worker_summary.get("stale"), 0),
            "lost": _as_int(worker_summary.get("lost"), 0),
            "failed": _as_int(worker_summary.get("failed"), 0),
            "unintegrated": _as_int(worker_summary.get("unintegrated"), 0),
            "uncertain_operations": _as_int(operation_summary.get("uncertain"), 0),
            "active_operations": _as_int(operation_summary.get("active"), 0),
        }
        if counts["lost"] or counts["uncertain_operations"]:
            activity_name = "recovery_required"
        elif counts["stale"]:
            activity_name = "degraded"
        elif counts["active"] or counts["active_operations"]:
            activity_name = "active"
        elif counts["workers"]:
            activity_name = "idle"
        else:
            activity_name = "planned"
        activity = {"activity": activity_name, "counts": counts}

        representative_workers: list[dict[str, Any]] = []
        if counts["workers"]:
            representative_workers.append(
                {
                    "turn_state": (
                        "working"
                        if counts["active"]
                        else ("failed" if counts["failed"] else "completed")
                    ),
                    "liveness": (
                        "lost"
                        if counts["lost"]
                        else ("stale" if counts["stale"] else "terminal")
                    ),
                    "integration_state": (
                        "not_integrated"
                        if counts["unintegrated"]
                        else "integrated"
                    ),
                }
            )
        representative_operations: list[dict[str, Any]] = []
        if counts["uncertain_operations"]:
            representative_operations.append({"state": "reconciling"})
        elif counts["active_operations"]:
            representative_operations.append({"state": "running"})
        elif _as_int(operation_summary.get("total"), 0):
            representative_operations.append({"state": "succeeded"})
        completion_contract = derive_completion_contract(
            group, representative_workers, representative_operations
        )
        completion_contract["activity"] = activity_name
        completion_contract["activity_counts"] = deepcopy(counts)
        return activity, completion_contract

    def _public_group(
        self,
        group: Mapping[str, Any],
        *,
        workers: list[Mapping[str, Any]],
        worker_summary: Mapping[str, Any] | None = None,
        operation_summary: Mapping[str, Any] | None = None,
        lane_summaries: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if worker_summary is None or operation_summary is None or lane_summaries is None:
            snapshot = self.store.work_group_status_projection(
                str(group["work_group_id"]),
                operation_limit=0,
                worker_limit=0,
                integration_limit=0,
            )
            worker_summary = snapshot["worker_summary"]
            operation_summary = snapshot["operation_summary"]
            lane_summaries = snapshot["lane_summaries"]
        activity, completion_contract = self._summary_activity_and_contract(
            group,
            worker_summary=worker_summary,
            operation_summary=operation_summary,
        )
        lanes: list[dict[str, Any]] = []
        lane_records = group.get("lanes") if isinstance(group.get("lanes"), Mapping) else {}
        lane_summary_by_id = {
            str(item.get("lane_id") or "main"): dict(item)
            for item in lane_summaries
        }
        lane_ids = set(lane_records).union(lane_summary_by_id)
        for lane_id in sorted(lane_ids):
            lane_workers = [worker for worker in workers if str(worker.get("lane_id") or "main") == lane_id]
            lane = deepcopy(dict(lane_records.get(lane_id) or {"lane_id": lane_id, "lane": lane_id, "title": lane_id, "role": ""}))
            lane_summary = lane_summary_by_id.get(lane_id, {})
            lane_activity_counts = {
                "workers": _as_int(lane_summary.get("worker_count"), len(lane_workers)),
                "active": _as_int(lane_summary.get("active"), 0),
                "quiet": _as_int(lane_summary.get("quiet"), 0),
                "stale": _as_int(lane_summary.get("stale"), 0),
                "lost": _as_int(lane_summary.get("lost"), 0),
                "failed": _as_int(lane_summary.get("failed"), 0),
                "unintegrated": _as_int(lane_summary.get("unintegrated"), 0),
                "uncertain_operations": 0,
                "active_operations": 0,
            }
            if lane_activity_counts["lost"]:
                lane_activity = "recovery_required"
            elif lane_activity_counts["stale"]:
                lane_activity = "degraded"
            elif lane_activity_counts["active"]:
                lane_activity = "active"
            elif lane_activity_counts["workers"]:
                lane_activity = "idle"
            else:
                lane_activity = "planned"
            lane.update({"activity": lane_activity, "counts": lane_activity_counts})
            lane["worker_refs"] = [str(worker.get("fleet_worker_ref") or "") for worker in lane_workers]
            lane["worker_count"] = lane_activity_counts["workers"]
            lane["worker_refs_truncated"] = len(lane["worker_refs"]) < lane["worker_count"]
            lanes.append(lane)
        worker_refs = [str(worker.get("fleet_worker_ref") or "") for worker in workers]
        worker_total = _as_int(worker_summary.get("total"), len(worker_refs))
        return {
            "work_group_id": group.get("work_group_id"),
            "title": group.get("title"),
            "goal": group.get("goal"),
            "status": group.get("status"),
            "lifecycle": group.get("lifecycle"),
            "visibility": group.get("visibility"),
            "shared_write_policy": group.get("shared_write_policy") or "serialized",
            "execution_mode": completion_contract["execution_mode"],
            "definition_of_done": completion_contract["definition_of_done"],
            "completion_contract": completion_contract,
            "workspace_ref": group.get("workspace_ref") or "",
            "workspace_projection_ref": group.get("workspace_projection_ref") or "",
            "requested_repo_path": group.get("requested_repo_path") or "",
            "resolved_repo_path": group.get("resolved_repo_path") or "",
            "pinned_machine_id": group.get("pinned_machine_id"),
            "pinned_edge_generation": group.get("pinned_edge_generation"),
            "readiness": self._derived_readiness(group),
            "activity": activity["activity"],
            "activity_counts": activity["counts"],
            "outcome": group.get("outcome") or "",
            "summary": group.get("summary") or "",
            "participants": list(group.get("participants") or []),
            "active_participant_ref": group.get("active_participant_ref") or "",
            "lanes": lanes,
            "worker_refs": worker_refs,
            "worker_count": worker_total,
            "worker_refs_truncated": len(worker_refs) < worker_total,
            "supersedes": group.get("supersedes") or "",
            "superseded_by": group.get("superseded_by") or "",
            "closure_dispositions": deepcopy(dict(group.get("closure_dispositions") or {})),
            "created_at": group.get("created_at"),
            "updated_at": group.get("updated_at"),
            "closed_at": group.get("closed_at"),
        }

    def _derived_readiness(self, group: Mapping[str, Any]) -> dict[str, Any]:
        readiness = deepcopy(dict(group.get("readiness") or {}))
        if group.get("status") != "open":
            return readiness
        machine_id = str(group.get("pinned_machine_id") or "")
        generation = str(group.get("pinned_edge_generation") or "")
        machine_entity = self.store.get_entity(MACHINE_ENTITY, machine_id) if machine_id else None
        if machine_entity is None or machine_entity["record"].get("edge_generation") != generation:
            return {**readiness, "status": "machine_unavailable", "reason": "pinned_generation_unavailable"}
        machine = self._public_machine(machine_entity["record"], now=self._clock())
        if machine["status"] != "online":
            return {**readiness, "status": "machine_unavailable", "reason": "pinned_machine_offline"}
        if machine["compatibility"] != "compatible":
            return {**readiness, "status": "incompatible_edge", "reason": "pinned_edge_contract_mismatch"}
        projection_ref = str(group.get("workspace_projection_ref") or "")
        projection = self.store.get_entity(WORKSPACE_PROJECTION_ENTITY, projection_ref) if projection_ref else None
        if projection_ref and (
            projection is None
            or not projection["record"].get("active")
            or projection["record"].get("exists") is False
        ):
            return {**readiness, "status": "failed", "reason": "workspace_missing"}
        return readiness

    def _group_envelope(
        self,
        group: Mapping[str, Any],
        *,
        operation: Mapping[str, Any] | None = None,
        include_workers: bool = True,
        include_operations: bool = True,
        include_integrations: bool = True,
        worker_offset: int = 0,
        worker_limit: int = DEFAULT_GROUP_STATUS_DETAIL_LIMIT,
        operation_offset: int = 0,
        operation_limit: int = DEFAULT_GROUP_STATUS_DETAIL_LIMIT,
        integration_offset: int = 0,
        integration_limit: int = DEFAULT_GROUP_STATUS_DETAIL_LIMIT,
        candidate_summary: list[Mapping[str, Any]] | None = None,
        rejection_summary: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        group_id = str(group["work_group_id"])
        projection = self.store.work_group_status_projection(
            group_id,
            operation_offset=operation_offset,
            operation_limit=operation_limit if include_operations else 0,
            worker_offset=worker_offset,
            worker_limit=worker_limit if include_workers else 0,
            integration_offset=integration_offset,
            integration_limit=integration_limit if include_integrations else 0,
        )
        workers = projection["workers"]
        for worker in workers:
            worker.setdefault("worker_state", "available")
            worker.setdefault("turn_state", "none")
            worker.setdefault("liveness", "lost")
            worker.setdefault("integration_state", "uncertain")
            worker.setdefault("review_disposition", "unreviewed")
        public_group = self._public_group(
            group,
            workers=workers,
            worker_summary=projection["worker_summary"],
            operation_summary=projection["operation_summary"],
            lane_summaries=projection["lane_summaries"],
        )
        completion_contract = deepcopy(dict(public_group["completion_contract"]))

        def page_metadata(
            *, total: int, offset: int, limit: int, returned: int, included: bool
        ) -> dict[str, Any]:
            next_offset = offset + returned
            return {
                "included": included,
                "total": total,
                "cursor": str(offset),
                "limit": limit,
                "returned": returned,
                "next_cursor": str(next_offset) if included and next_offset < total else "",
                "truncated": included and next_offset < total,
            }

        operation_total = _as_int(projection["operation_summary"].get("total"), 0)
        worker_total = _as_int(projection["worker_summary"].get("total"), 0)
        integration_total = _as_int(projection["integration_summary"].get("total"), 0)
        result: dict[str, Any] = {
            "work_group": public_group,
            "lanes": deepcopy(public_group["lanes"]),
            "readiness": deepcopy(public_group["readiness"]),
            "completion_contract": completion_contract,
            "status_revision": projection["status_revision"],
            "operation_summary": deepcopy(projection["operation_summary"]),
            "worker_summary": deepcopy(projection["worker_summary"]),
            "operation_page": page_metadata(
                total=operation_total,
                offset=operation_offset,
                limit=operation_limit,
                returned=len(projection["operations"]),
                included=include_operations,
            ),
            "worker_page": page_metadata(
                total=worker_total,
                offset=worker_offset,
                limit=worker_limit,
                returned=len(workers),
                included=include_workers,
            ),
            "routing": deepcopy(dict(group.get("routing") or {})),
            "candidate_summary": deepcopy(list(candidate_summary or [])),
            "rejection_summary": deepcopy(list(rejection_summary or [])),
        }
        if include_workers:
            result["workers"] = workers
        if include_operations:
            result["operations"] = [
                self._operation_summary(item) for item in projection["operations"]
            ]
        if include_integrations:
            result["integration_summary"] = deepcopy(
                projection["integration_summary"]
            )
            result["integrations"] = deepcopy(projection["integrations"])
            result["integration_page"] = page_metadata(
                total=integration_total,
                offset=integration_offset,
                limit=integration_limit,
                returned=len(projection["integrations"]),
                included=True,
            )
        next_actions: list[dict[str, Any]] = []
        if completion_contract.get("manager_must_continue"):
            action = completion_contract.get("recommended_next_action")
            if isinstance(action, Mapping) and action:
                next_actions.append(deepcopy(dict(action)))
        return public_envelope(
            "ok",
            result=result,
            operation=self._operation_summary(operation) if operation else {},
            next_actions=next_actions,
        )

    def _coordination_blocker(
        self, group: Mapping[str, Any], identity: ManagerIdentity
    ) -> str:
        if identity.participant_ref not in set(group.get("participants") or []):
            return "participant_must_resume_group"
        if self._current_group_id(identity) != group.get("work_group_id"):
            return "group_is_not_current_for_participant"
        if group.get("active_participant_ref") not in {"", identity.participant_ref}:
            return "another_participant_is_active"
        return ""

    def _normalize_dispositions(
        self, values: Any, workers: list[Mapping[str, Any]]
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        by_name: dict[str, list[str]] = {}
        for worker in workers:
            by_name.setdefault(str(worker.get("name") or "").casefold(), []).append(
                str(worker.get("fleet_worker_ref") or "")
            )
        normalized: dict[str, str] = {}
        errors: list[dict[str, str]] = []
        if isinstance(values, Mapping):
            items = [
                {"fleet_worker_ref": key, "disposition": value}
                for key, value in values.items()
            ]
        elif isinstance(values, list):
            items = values
        else:
            raise ValueError("worker_dispositions must be a list or object")
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("Each worker disposition must be an object")
            worker_ref = str(item.get("fleet_worker_ref") or "")
            if not worker_ref:
                matches = by_name.get(str(item.get("worker") or "").casefold(), [])
                if len(matches) != 1:
                    errors.append({"worker": str(item.get("worker") or ""), "reason": "ambiguous_or_unknown_worker"})
                    continue
                worker_ref = matches[0]
            disposition = str(item.get("disposition") or "")
            if disposition == "discarded":
                if item.get("discard_unintegrated_changes") is not True:
                    errors.append({"worker": worker_ref, "reason": "discard_requires_explicit_consent"})
                    continue
                disposition = "discarded_explicitly"
            normalized[worker_ref] = disposition
        return normalized, errors

    @staticmethod
    def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(value)
            for key, value in candidate.items()
            if key not in {"machine", "projection"}
        }

    def _upsert_entity(
        self, entity_type: str, entity_id: str, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        existing = self.store.get_entity(entity_type, entity_id)
        return self.store.put_entity(
            entity_type,
            entity_id,
            record,
            expected_revision=existing["revision"] if existing else 0,
        )


# Compatibility aliases for stable call-site naming.
HubRuntime = HubRuntimeV2
HubV2Runtime = HubRuntimeV2
