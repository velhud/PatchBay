"""Dependency-injected composition root for the complete Hub V2 tool surface.

This module intentionally contains no HTTP, MCP transport, or production server
wiring.  It composes the durable Hub services and exposes :class:`HubProtocolV2`
to a caller which supplies Edge delivery and canonical Pro Request storage.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from patchbay.hub.adapters.pro_requests import (
    FleetHubProRequestAdapterV2,
    HubProRequestAdapterV2,
    ProRequestCanonicalStore,
    ProRequestRoute,
)
from patchbay.hub.adapters.worker import HubWorkerAdapterV2, WorkerRoute
from patchbay.hub.adapters.workspace import WorkspaceAdapter
from patchbay.hub.backup_v2 import (
    AdmissionFreezeController,
    AdmissionFreezeGate,
    AdmissionFrozenError,
    admission_coordination_path,
)
from patchbay.hub.broker import EDGE_DISPATCH_ENTITY_TYPE, OperationBroker
from patchbay.hub.groups_v2 import derive_completion_contract
from patchbay.hub.identity import ManagerIdentity
from patchbay.hub.operations import PUBLIC_STATUSES, normalize_domain_result, public_envelope
from patchbay.hub.protocol_v2 import HubProtocolV2
from patchbay.hub.runtime_v2 import (
    ACTIVE_TURN_STATES,
    FLEET_WORKER_ENTITY,
    MACHINE_ENTITY,
    WORKER_PROJECTION_ENTITY,
    WORKSPACE_PROJECTION_ENTITY,
    WORK_GROUP_ENTITY,
    HubRuntimeV2,
)
from patchbay.hub.store_v2 import HubStoreV2, semantic_payload_hash
from patchbay.hub.tool_surface import (
    HUB_V2_TOOL_FAMILIES,
    HUB_V2_MUTATING_TOOL_NAMES,
    HUB_V2_TOOL_NAMES,
    normalize_hub_v2_next_actions,
)
from patchbay.protocol.context import RequestContext


EDGE_DISPATCH_ENTITY = EDGE_DISPATCH_ENTITY_TYPE
_TRANSIENT_PAYLOAD_KEY = "transient_payload"
_ARTIFACT_URL_PAYLOAD_KIND = "artifact_download_url"
_TERMINAL_OPERATION_STATES = frozenset({"succeeded", "blocked", "failed", "cancelled"})
_WORKER_MUTATION_TOOLS = frozenset(
    {
        "patchbay_worker_inbox",
        "patchbay_worker_start",
        "patchbay_worker_start_batch",
        "patchbay_worker_message",
        "patchbay_worker_integrate",
        "patchbay_worker_stop",
    }
)
_WORKER_SPECIFIC_TOOLS = frozenset(
    {
        "patchbay_worker_message",
        "patchbay_worker_inspect",
        "patchbay_worker_integrate",
        "patchbay_worker_stop",
    }
)
_WORKER_PROJECTION_TOOLS = frozenset(
    {
        "patchbay_worker_list",
        "patchbay_worker_status",
        "patchbay_worker_wait",
    }
)
_WORKER_EDGE_ACTIONS = frozenset(
    {
        "codex_worker_start",
        "codex_worker_message",
        "codex_worker_integrate",
        "codex_worker_stop",
    }
)
_PRO_REQUEST_MUTATION_TOOLS = frozenset(
    {
        "patchbay_pro_request_claim",
        "patchbay_pro_request_respond",
        "patchbay_pro_request_dispatch",
        "patchbay_pro_request_close",
    }
)


@runtime_checkable
class EdgeDeliveryPort(Protocol):
    """Injected network/Edge boundary used by the composition root."""

    async def execute(
        self,
        *,
        machine_id: str,
        edge_generation: str,
        action: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]: ...


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _invoke_with_supported_keywords(callback: Callable[..., Any], values: Mapping[str, Any]) -> Any:
    parameters = inspect.signature(callback).parameters
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return callback(**dict(values))
    accepted = {
        name: value
        for name, value in values.items()
        if name in parameters
        and parameters[name].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return callback(**accepted)


def _mapping(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _canonical_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    if str(value.get("status") or "") in PUBLIC_STATUSES and isinstance(value.get("result"), Mapping):
        operation = value.get("operation") if isinstance(value.get("operation"), Mapping) else {}
        operation_id = str(operation.get("operation_id") or "")
        return public_envelope(
            str(value["status"]),
            result=value.get("result"),
            operation=operation,
            warnings=list(value.get("warnings") or []),
            next_actions=normalize_hub_v2_next_actions(
                value.get("next_actions"), operation_id=operation_id
            ),
        )
    return _canonical_envelope(normalize_domain_result(value))


class EdgeDeliveryBridgeV2:
    """Normalize one injected Edge client for direct and operation delivery."""

    def __init__(self, delivery: EdgeDeliveryPort | Callable[..., Any]):
        if not callable(delivery) and not any(
            callable(getattr(delivery, name, None))
            for name in ("execute", "execute_edge_action", "dispatch_operation")
        ):
            raise TypeError("edge_delivery must be callable or expose an Edge delivery method")
        self.delivery = delivery

    async def execute(
        self,
        *,
        machine_id: str,
        edge_generation: str,
        action: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        callback = getattr(self.delivery, "execute_edge_action", None) or getattr(
            self.delivery, "execute", None
        )
        if not callable(callback):
            callback = self.delivery
        if not callable(callback):
            raise TypeError("edge_delivery does not expose direct Edge execution")
        result = _invoke_with_supported_keywords(
            callback,
            {
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "action": action,
                "arguments": deepcopy(dict(arguments)),
                "target": deepcopy(dict(target)),
                "context": context,
            },
        )
        result = await _maybe_await(result)
        if not isinstance(result, Mapping):
            raise TypeError("Edge delivery must return a mapping")
        return deepcopy(dict(result))

    async def dispatch_operation(
        self,
        *,
        operation: Mapping[str, Any],
        payload: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        callback = getattr(self.delivery, "dispatch_operation", None)
        if callable(callback):
            result = _invoke_with_supported_keywords(
                callback,
                {
                    "operation": deepcopy(dict(operation)),
                    "payload": deepcopy(dict(payload)),
                    "context": context,
                },
            )
            result = await _maybe_await(result)
            if not isinstance(result, Mapping):
                raise TypeError("Edge operation delivery must return a mapping")
            return deepcopy(dict(result))

        target = _mapping(payload.get("target"))
        machine_id = str(payload.get("machine_id") or target.get("machine_id") or "")
        edge_generation = str(
            payload.get("edge_generation") or target.get("edge_generation") or ""
        )
        arguments = _mapping(payload.get("arguments"))
        if not arguments:
            arguments = {
                key: deepcopy(value)
                for key, value in payload.items()
                if key
                not in {
                    "action",
                    "context",
                    "target",
                    "machine_id",
                    "edge_generation",
                }
            }
        return await self.execute(
            machine_id=machine_id,
            edge_generation=edge_generation,
            action=str(payload.get("action") or ""),
            arguments=arguments,
            target=target,
            context=context,
        )


class CanonicalProRequestStoreBridgeV2:
    """Preserve the canonical Pro Request store and explicit dispatch boundary."""

    def __init__(
        self,
        store: ProRequestCanonicalStore,
        edge: EdgeDeliveryBridgeV2,
    ):
        self.store = store
        self.edge = edge

    def _store_call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        callback = getattr(self.store, name, None)
        if not callable(callback):
            raise TypeError(f"Canonical Pro Request store does not expose {name}")
        result = callback(**kwargs)
        if not isinstance(result, Mapping):
            raise TypeError(f"Canonical Pro Request {name} must return a mapping")
        return deepcopy(dict(result))

    def list_requests(self, **kwargs: Any) -> dict[str, Any]:
        return self._store_call("list_requests", **kwargs)

    def read_request(self, **kwargs: Any) -> dict[str, Any]:
        return self._store_call("read_request", **kwargs)

    def claim_request(self, **kwargs: Any) -> dict[str, Any]:
        return self._store_call("claim_request", **kwargs)

    def respond_request(self, **kwargs: Any) -> dict[str, Any]:
        return self._store_call("respond_request", **kwargs)

    def close_request(self, **kwargs: Any) -> dict[str, Any]:
        return self._store_call("close_request", **kwargs)

    async def dispatch_pro_request(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
        operation_id: str = "",
        route: ProRequestRoute | None = None,
    ) -> dict[str, Any]:
        direct = getattr(self.store, "dispatch_request", None)
        if callable(direct):
            result = _invoke_with_supported_keywords(
                direct,
                {
                    **deepcopy(dict(arguments)),
                    "request_context": context,
                    "context": context,
                    "operation_id": operation_id,
                    "route": route,
                },
            )
            result = await _maybe_await(result)
            if not isinstance(result, Mapping):
                raise TypeError("Canonical Pro Request dispatch must return a mapping")
            return deepcopy(dict(result))

        if route is None:
            raise ValueError("A Pro Request route is required for Edge dispatch")
        target_name = str(arguments.get("target") or "origin_worker")
        mark_requested = getattr(self.store, "mark_dispatch_requested", None)
        if callable(mark_requested):
            manifest, refusal = mark_requested(
                request_id=str(arguments.get("request_id") or ""),
                target=target_name,
                request_context=context,
                takeover=bool(arguments.get("takeover", False)),
            )
            if refusal:
                return {"accepted": False, "request_id": manifest.get("id"), **dict(refusal)}

        result = await self.edge.execute(
            machine_id=route.machine_id,
            edge_generation=route.edge_generation,
            action="codex_pro_request_dispatch",
            arguments=arguments,
            target={
                "machine_id": route.machine_id,
                "edge_generation": route.edge_generation,
                "workspace_ref": route.workspace_ref,
                "work_group_id": route.work_group_id,
                "lane_id": route.lane,
                "operation_id": operation_id,
            },
            context=context,
        )
        domain = (
            _mapping(result.get("result"))
            if str(result.get("status") or "") in PUBLIC_STATUSES
            else deepcopy(result)
        )
        domain.setdefault("accepted", str(result.get("status") or "ok") == "ok")
        domain.setdefault("dispatched", bool(domain.get("accepted")))
        finish = getattr(self.store, "finish_dispatch", None)
        if callable(finish):
            request = finish(
                request_id=str(arguments.get("request_id") or ""),
                accepted=bool(domain.get("accepted")),
                target=target_name,
                dispatch_result=deepcopy(domain),
                request_context=context,
            )
            domain["request"] = deepcopy(dict(request))
        return domain


class HubRuntimeTargetPortV2:
    """Resolve worker/workspace routes from durable Hub V2 identities."""

    def __init__(self, runtime: HubRuntimeV2, edge: EdgeDeliveryBridgeV2):
        self.runtime = runtime
        self.store = runtime.store
        self.edge = edge

    def _identity(self, context: RequestContext | None) -> ManagerIdentity:
        return ManagerIdentity.from_request(context, principal_ref=self.store.principal_ref)

    def _visible_group(
        self, work_group_id: str, context: RequestContext | None
    ) -> dict[str, Any] | None:
        entity = self.store.get_entity(WORK_GROUP_ENTITY, work_group_id)
        identity = self._identity(context)
        if entity is None or entity["record"].get("principal_ref") != identity.principal_ref:
            return None
        return deepcopy(entity["record"])

    def get_work_group(
        self, work_group_id: str, *, context: RequestContext | None = None
    ) -> dict[str, Any] | None:
        return self._visible_group(str(work_group_id or ""), context)

    def _workers_for_target(
        self,
        *,
        work_group_id: str,
        machine_id: str,
        edge_generation: str,
    ) -> list[dict[str, Any]]:
        workers: list[dict[str, Any]] = []
        immutable_fields = (
            "fleet_worker_ref",
            "machine_id",
            "edge_generation",
            "edge_worker_id",
            "work_group_id",
            "lane_id",
            "workspace_ref",
            "name",
            "created_at",
        )
        for entity in self.store.list_entities(FLEET_WORKER_ENTITY):
            fleet = deepcopy(entity["record"])
            if (
                str(fleet.get("work_group_id") or "") != work_group_id
                or str(fleet.get("machine_id") or "") != machine_id
                or str(fleet.get("edge_generation") or "") != edge_generation
            ):
                continue
            projection_entity = self.store.get_entity(
                WORKER_PROJECTION_ENTITY, entity["entity_id"]
            )
            projection = deepcopy(projection_entity["record"]) if projection_entity else {}
            projection_matches = bool(projection) and all(
                not projection.get(field)
                or str(projection.get(field)) == str(fleet.get(field) or "")
                for field in (
                    "fleet_worker_ref",
                    "machine_id",
                    "edge_generation",
                    "edge_worker_id",
                    "work_group_id",
                )
            )
            value = projection if projection_matches else {}
            value.update({field: fleet.get(field) for field in immutable_fields if field in fleet})
            value["projection_missing"] = not projection_matches
            if not projection_matches:
                value.setdefault("worker_state", "available")
                value.setdefault("turn_state", "none")
                value.setdefault("liveness", "unknown")
                value.setdefault("integration_state", "uncertain")
                value.setdefault("review_disposition", "unreviewed")
            workers.append(value)
        workers.sort(
            key=lambda item: (
                str(item.get("lane_id") or ""),
                str(item.get("name") or "").casefold(),
                str(item.get("fleet_worker_ref") or ""),
            )
        )
        return workers

    def list_machines(self) -> dict[str, Any]:
        # Routing needs the complete authorized projection set. The public
        # fleet-status tool is intentionally bounded and must not become an
        # accidental internal routing database.
        return {"machines": self.runtime.routing_machine_views()}

    async def discover_workspaces(self, **kwargs: Any) -> dict[str, Any]:
        """Collect one bounded live discovery page from each eligible Edge."""
        query = str(kwargs.get("query") or "")
        allowed = {str(value) for value in kwargs.get("machine_ids") or []}
        required_tags = {str(value) for value in kwargs.get("required_tags") or []}
        include_offline = bool(kwargs.get("include_offline", False))
        max_depth = max(0, int(kwargs.get("max_depth") or 0))
        max_results = max(1, min(int(kwargs.get("max_results") or 50), 100))
        discovered: list[dict[str, Any]] = []
        truncated = False
        warnings: list[str] = []
        for machine in self.list_machines()["machines"]:
            machine_id = str(machine.get("machine_id") or "")
            if allowed and machine_id not in allowed:
                continue
            if required_tags and not required_tags.issubset(set(machine.get("tags") or [])):
                continue
            if machine.get("status") != "online":
                if include_offline:
                    warnings.append(f"Skipped offline machine {machine_id} during live discovery.")
                continue
            raw = await self.edge.execute(
                machine_id=machine_id,
                edge_generation=str(machine.get("edge_generation") or ""),
                action="codex_list_workspaces",
                arguments={
                    "query": query,
                    "discover": True,
                    "max_depth": max_depth,
                    "max_results": max_results,
                },
                target={"machine_id": machine_id},
                context=None,
            )
            envelope = normalize_domain_result(raw)
            if envelope["status"] not in {"ok", "partial"}:
                warnings.append(f"Workspace discovery failed on {machine_id}.")
                continue
            result = envelope.get("result") or {}
            for value in result.get("workspaces") or result.get("repositories") or []:
                if not isinstance(value, Mapping):
                    continue
                alias = str(value.get("alias") or value.get("name") or "")
                discovered.append(
                    {
                        "machine_id": machine_id,
                        "workspace_ref": str(value.get("workspace_ref") or alias),
                        "workspace_projection_ref": str(value.get("workspace_projection_ref") or ""),
                        "alias": alias,
                        "path": str(value.get("path") or value.get("repo_path") or value.get("local_path") or ""),
                        "git": bool(value.get("git") or value.get("git_repo")),
                    }
                )
            truncated = truncated or bool(result.get("truncated"))
        return {
            "workspaces": discovered,
            "truncated": truncated,
            "next_cursor": "",
            "warnings": warnings,
        }

    async def resolve_target(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        args = dict(arguments)
        identity = self._identity(context)
        group_id = str(args.get("work_group_id") or "")
        if not group_id and tool_name not in _WORKER_PROJECTION_TOOLS:
            group_id = str(
                (context.work_group_id if context else "")
                or self.runtime._current_group_id(identity)
                or ""
            )
        if not group_id and tool_name in _WORKER_PROJECTION_TOOLS:
            return public_envelope("blocked", result={"reason": "work_group_id_required"})
        group = self._visible_group(group_id, context) if group_id else None
        if group_id and group is None:
            return public_envelope("not_found", result={"reason": "work_group_not_found"})

        if group is None:
            machine_id = str(args.get("machine_id") or "")
            if not machine_id:
                return public_envelope(
                    "blocked", result={"reason": "work_group_or_machine_target_required"}
                )
            machine_entity = self.store.get_entity(MACHINE_ENTITY, machine_id)
            if machine_entity is None:
                return public_envelope("not_found", result={"reason": "machine_not_found"})
            machine = self.runtime._public_machine(machine_entity["record"], now=self.runtime._clock())
            return {
                "principal_ref": identity.principal_ref,
                "machine_id": machine_id,
                "edge_generation": machine.get("edge_generation") or "",
                "repo_path": str(args.get("repo_path") or ""),
                "workspace_ref": str(args.get("workspace_ref") or ""),
                "machine": machine,
            }

        readiness = self.runtime._derived_readiness(group)
        if tool_name in _WORKER_MUTATION_TOOLS and readiness.get("status") != "ready":
            return public_envelope(
                "blocked",
                result={
                    "reason": "work_group_not_ready",
                    "readiness": readiness,
                    "work_group": {"work_group_id": group_id},
                },
            )

        machine_id = str(group.get("pinned_machine_id") or "")
        machine_entity = self.store.get_entity(MACHINE_ENTITY, machine_id)
        if machine_entity is None:
            return public_envelope("not_found", result={"reason": "pinned_machine_not_found"})
        machine = self.runtime._public_machine(machine_entity["record"], now=self.runtime._clock())
        machine["workspaces"] = [
            deepcopy(item["record"])
            for item in self.store.list_entities(WORKSPACE_PROJECTION_ENTITY)
            if item["record"].get("machine_id") == machine_id
            and item["record"].get("edge_generation") == group.get("pinned_edge_generation")
            and item["record"].get("active")
        ]

        lane_id = str(args.get("lane") or (context.lane_id if context else "") or "")
        lanes = group.get("lanes") if isinstance(group.get("lanes"), Mapping) else {}
        if lane_id and lane_id not in lanes:
            return public_envelope("not_found", result={"reason": "lane_not_found"})
        if not lane_id and len(lanes) == 1:
            lane_id = str(next(iter(lanes)))
        lane = deepcopy(dict(lanes.get(lane_id) or {})) if lane_id else {}

        workers = self._workers_for_target(
            work_group_id=group_id,
            machine_id=machine_id,
            edge_generation=str(group.get("pinned_edge_generation") or ""),
        )
        worker: dict[str, Any] = {}
        fleet_ref = str(args.get("fleet_worker_ref") or "")
        worker_name = str(args.get("worker") or "").strip().casefold()
        if fleet_ref:
            matches = [item for item in workers if item.get("fleet_worker_ref") == fleet_ref]
        elif worker_name:
            matches = [
                item for item in workers if str(item.get("name") or "").strip().casefold() == worker_name
            ]
        elif tool_name in _WORKER_SPECIFIC_TOOLS and len(workers) == 1:
            matches = workers
        else:
            matches = []
        if (fleet_ref or worker_name) and not matches:
            return public_envelope("not_found", result={"reason": "worker_not_found"})
        if len(matches) > 1:
            return public_envelope("blocked", result={"reason": "ambiguous_worker_name"})
        if matches:
            worker = deepcopy(matches[0])
            lane_id = str(worker.get("lane_id") or lane_id)
            lane = deepcopy(dict(lanes.get(lane_id) or lane))

        projection_ref = str(group.get("workspace_projection_ref") or "")
        projection = self.store.get_entity(WORKSPACE_PROJECTION_ENTITY, projection_ref)
        workspace = deepcopy(projection["record"]) if projection else {
            "workspace_ref": str(group.get("workspace_ref") or ""),
            "workspace_projection_ref": projection_ref,
        }
        return {
            "principal_ref": identity.principal_ref,
            "work_group_id": group_id,
            "lane_id": lane_id,
            "machine_id": machine_id,
            "edge_generation": str(group.get("pinned_edge_generation") or ""),
            "workspace_ref": str(group.get("workspace_ref") or ""),
            "workspace_projection_ref": projection_ref,
            "repo_path": str(group.get("resolved_repo_path") or args.get("repo_path") or ""),
            "fleet_worker_ref": str(worker.get("fleet_worker_ref") or ""),
            "edge_worker_id": str(worker.get("edge_worker_id") or ""),
            "work_group": deepcopy(group),
            "lane": lane,
            "worker": worker,
            "projection_missing": bool(worker.get("projection_missing")) if worker else False,
            "machine": machine,
            "workspace": workspace,
        }

    async def execute_read(
        self,
        *,
        payload: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        target = _mapping(payload.get("target"))
        return await self.edge.execute(
            machine_id=str(payload.get("machine_id") or target.get("machine_id") or ""),
            edge_generation=str(
                payload.get("edge_generation") or target.get("edge_generation") or ""
            ),
            action=str(payload.get("action") or ""),
            arguments=_mapping(payload.get("arguments")),
            target=target,
            context=context,
        )

    async def execute(
        self,
        *,
        machine_id: str,
        edge_generation: str,
        action: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self.edge.execute(
            machine_id=machine_id,
            edge_generation=edge_generation,
            action=action,
            arguments=arguments,
            target=target,
            context=context,
        )


class HubWorkerProjectionPortV2:
    """Query and bounded-wait over authoritative Hub worker projections."""

    def __init__(
        self,
        runtime: HubRuntimeV2,
        *,
        max_wait_seconds: float = 30.0,
        minimum_poll_seconds: float = 20.0,
        recommended_poll_seconds: float = 30.0,
    ):
        self.runtime = runtime
        self.store = runtime.store
        self.minimum_poll_seconds = max(0.0, float(minimum_poll_seconds))
        self.recommended_poll_seconds = max(
            self.minimum_poll_seconds, float(recommended_poll_seconds)
        )
        self.max_wait_seconds = max(
            self.minimum_poll_seconds, float(max_wait_seconds)
        )
        self._poll_responses: dict[str, dict[str, Any]] = {}

    def _poll_cache_key(
        self,
        *,
        filters: Mapping[str, Any],
        route: Mapping[str, Any],
        context: RequestContext | None,
    ) -> str:
        identity = ManagerIdentity.from_request(
            context, principal_ref=self.store.principal_ref
        )
        group_id = str(filters.get("work_group_id") or route.get("work_group_id") or "")
        return ":".join(
            (
                identity.participant_ref,
                group_id or str(route.get("machine_id") or "ungrouped"),
            )
        )

    def _cached_poll_snapshot(
        self,
        cache_key: str,
    ) -> dict[str, Any] | None:
        cached = self._poll_responses.get(cache_key)
        if not cached:
            return None
        elapsed = max(0.0, time.monotonic() - float(cached.get("at") or 0.0))
        if elapsed >= self.minimum_poll_seconds:
            return None
        retry_after = max(1, int(self.minimum_poll_seconds - elapsed + 0.999))
        return {
            "snapshot": deepcopy(dict(cached.get("snapshot") or {})),
            "elapsed": elapsed,
            "retry_after": retry_after,
        }

    def _store_poll_snapshot(self, cache_key: str, snapshot: Mapping[str, Any]) -> None:
        self._poll_responses[cache_key] = {
            "at": time.monotonic(),
            "snapshot": deepcopy(dict(snapshot)),
        }
        if len(self._poll_responses) <= 1024:
            return
        oldest = min(
            self._poll_responses,
            key=lambda key: float(self._poll_responses[key].get("at") or 0.0),
        )
        self._poll_responses.pop(oldest, None)

    def _projection_snapshot(
        self,
        *,
        filters: Mapping[str, Any],
        route: Mapping[str, Any],
    ) -> dict[str, Any]:
        group_id = str(filters.get("work_group_id") or route.get("work_group_id") or "")
        values: list[tuple[int, dict[str, Any]]] = []
        for entity in self.store.list_entities(WORKER_PROJECTION_ENTITY):
            worker = deepcopy(entity["record"])
            if group_id and worker.get("work_group_id") != group_id:
                continue
            worker.setdefault("worker_id", str(worker.get("edge_worker_id") or ""))
            revision = max(
                int(entity.get("revision") or 0),
                int(worker.get("edge_projection_revision") or worker.get("projection_revision") or 0),
            )
            worker["projection_revision"] = revision
            values.append((revision, worker))
        values.sort(
            key=lambda item: (
                str(item[1].get("name") or "").casefold(),
                item[1].get("fleet_worker_ref") or "",
            )
        )
        group_entity = self.store.get_entity(WORK_GROUP_ENTITY, group_id) if group_id else None
        return {
            "values": values,
            "group": deepcopy(group_entity["record"]) if group_entity else {},
            "operations": self.runtime._operations_for_group(group_id) if group_id else [],
        }

    def _query_result(
        self,
        *,
        filters: Mapping[str, Any],
        route: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        lane_id = str(filters.get("lane") or route.get("lane_id") or "")
        fleet_ref = str(route.get("fleet_worker_ref") or "")
        edge_worker_id = str(route.get("edge_worker_id") or "")
        active_only = bool(filters.get("active_only", False))
        include_stopped = bool(filters.get("include_stopped", False))
        source = snapshot or self._projection_snapshot(filters=filters, route=route)
        values: list[tuple[int, dict[str, Any]]] = []
        for revision, source_worker in source.get("values") or []:
            worker = deepcopy(dict(source_worker))
            if lane_id and worker.get("lane_id") != lane_id:
                continue
            if fleet_ref and worker.get("fleet_worker_ref") != fleet_ref:
                continue
            if edge_worker_id and worker.get("edge_worker_id") != edge_worker_id:
                continue
            if active_only and worker.get("turn_state") not in ACTIVE_TURN_STATES:
                continue
            if not include_stopped and worker.get("worker_state") == "stopped":
                continue
            values.append((int(revision), worker))
        values.sort(key=lambda item: (str(item[1].get("name") or "").casefold(), item[1].get("fleet_worker_ref") or ""))

        try:
            start = max(0, int(filters.get("cursor") or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            limit = max(1, min(int(filters.get("limit") or 50), 100))
        except (TypeError, ValueError):
            limit = 50
        page = values[start : start + limit]
        projection_revision = max((item[0] for item in values), default=0)
        all_workers = [item[1] for item in values]
        workers = [item[1] for item in page]
        counts = {
            "total": len(values),
            "active": sum(item[1].get("turn_state") in ACTIVE_TURN_STATES for item in values),
            "completed": sum(item[1].get("turn_state") == "completed" for item in values),
            "failed": sum(item[1].get("turn_state") == "failed" for item in values),
        }
        group = deepcopy(dict(source.get("group") or {}))
        operations = deepcopy(list(source.get("operations") or []))
        completion_contract = (
            derive_completion_contract(group, all_workers, operations) if group else {}
        )
        worker_lines = [
            " | ".join(
                part
                for part in (
                    str(worker.get("name") or worker.get("fleet_worker_ref") or "worker"),
                    str(worker.get("turn_state") or "none"),
                    str(worker.get("liveness") or "unknown"),
                    str(worker.get("current_phase") or ""),
                )
                if part
            )
            for worker in workers
        ]
        action = completion_contract.get("recommended_next_action") if completion_contract else {}
        suggested_action = (
            str(action.get("tool") or "") if isinstance(action, Mapping) else ""
        )
        return {
            "summary": (
                f"{counts['active']} active, {counts['completed']} completed, "
                f"{counts['failed']} failed worker turns."
            ),
            "suggested_action": suggested_action,
            "worker_lines": worker_lines,
            "workers": workers,
            "count": len(workers),
            "total_known": len(values),
            "counts": counts,
            "projection_revision": projection_revision,
            "next_cursor": str(start + limit) if start + limit < len(values) else "",
            "status_current": True,
            "minimum_next_poll_seconds": int(self.minimum_poll_seconds),
            "recommended_next_poll_seconds": int(self.recommended_poll_seconds),
            "poll_guidance": (
                "Workers may remain active or quiet for many minutes. Follow the recommended action; "
                "a wait timeout is not completion and is not a failure."
            ),
            "completion_contract": completion_contract,
            "work_remaining": bool(completion_contract.get("work_remaining")),
            "final_response_allowed": bool(completion_contract.get("final_response_allowed", True)),
        }

    def query(
        self,
        *,
        view: str,
        filters: Mapping[str, Any],
        route: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        cache_key = self._poll_cache_key(filters=filters, route=route, context=context)
        cached = self._cached_poll_snapshot(cache_key)
        snapshot = (
            cached["snapshot"]
            if cached is not None
            else self._projection_snapshot(filters=filters, route=route)
        )
        result = self._query_result(filters=filters, route=route, snapshot=snapshot)
        result["view"] = view
        result.update(
            {
                "poll_too_early": cached is not None,
                "status_current": cached is None,
                "seconds_since_last_poll": (
                    int(cached["elapsed"]) if cached is not None else None
                ),
                "retry_after_seconds": (
                    int(cached["retry_after"])
                    if cached is not None
                    else int(self.recommended_poll_seconds)
                ),
            }
        )
        if cached is not None:
            result["poll_guidance"] = (
                f"This manager checked this work group {int(cached['elapsed'])}s ago. "
                f"Wait at least {int(cached['retry_after'])}s before another list/status pull; "
                f"the normal cadence is {int(self.minimum_poll_seconds)}-"
                f"{int(self.recommended_poll_seconds)} seconds. This cached response "
                "is not a failure and does not interrupt workers."
            )
        else:
            self._store_poll_snapshot(cache_key, snapshot)
        return result

    async def wait(
        self,
        *,
        filters: Mapping[str, Any],
        route: Mapping[str, Any],
        since_revision: int,
        timeout_seconds: float,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        requested_timeout = max(0.0, float(timeout_seconds))
        timeout = min(
            max(self.minimum_poll_seconds, requested_timeout), self.max_wait_seconds
        )
        started = time.monotonic()
        snapshot: dict[str, Any] = {}
        while True:
            snapshot = self._projection_snapshot(filters=filters, route=route)
            result = self._query_result(filters=filters, route=route, snapshot=snapshot)
            if int(result["projection_revision"]) > max(0, int(since_revision)):
                break
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.25, remaining))
        result["waited_seconds"] = int(round(time.monotonic() - started))
        result["requested_wait_seconds"] = int(requested_timeout)
        result["effective_wait_seconds"] = int(timeout)
        result["changed"] = int(result["projection_revision"]) > max(0, int(since_revision))
        result.update(
            {
                "view": "wait",
                "poll_too_early": False,
                "status_current": True,
                "seconds_since_last_poll": None,
                "retry_after_seconds": int(self.recommended_poll_seconds),
            }
        )
        cache_key = self._poll_cache_key(filters=filters, route=route, context=context)
        self._store_poll_snapshot(cache_key, snapshot)
        return result

    def get_worker(
        self,
        *,
        route: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any] | None:
        del context
        result = self._query_result(filters={}, route=route)
        workers = result["workers"]
        return deepcopy(workers[0]) if len(workers) == 1 else None


class HubBrokerEdgeDispatchPortV2:
    """Persist Edge payloads beside broker operations and deliver them explicitly."""

    def __init__(
        self,
        broker: OperationBroker,
        runtime: HubRuntimeV2,
        edge: EdgeDeliveryBridgeV2,
    ):
        self.broker = broker
        self.runtime = runtime
        self.store = runtime.store
        self.edge = edge

    def __getattr__(self, name: str) -> Any:
        return getattr(self.broker, name)

    def create_operation(self, **kwargs: Any) -> dict[str, Any]:
        payload = _mapping(kwargs.get("payload"))
        operation = self.broker.create_operation(**kwargs)
        self._remember(operation, payload)
        refreshed = self.store.get_operation(str(operation["operation_id"])) or operation
        result = deepcopy(dict(refreshed))
        # The replay marker is call-scoped and intentionally is not persisted
        # in SQLite.  Preserve it across this wrapper refresh so callers do not
        # repeat the domain side effect behind an idempotently reused operation.
        result["idempotent_replay"] = bool(operation.get("idempotent_replay"))
        return result

    def create_batch_operation(self, **kwargs: Any) -> dict[str, Any]:
        child_specs = kwargs.get("child_specs")
        if not isinstance(child_specs, list):
            raise ValueError("child_specs must be a list")
        dispatch_specs: list[dict[str, Any]] = []
        for raw_spec in child_specs:
            if not isinstance(raw_spec, Mapping):
                raise ValueError("child_specs entries must be objects")
            payload = _mapping(raw_spec.get("payload"))
            action = str(payload.get("action") or "")
            if action != "patchbay_edge_preflight" and not action.startswith("codex_"):
                raise ValueError("batch child does not carry an Edge dispatch action")
            dispatch_specs.append(
                {
                    "item_id": str(raw_spec.get("item_id") or ""),
                    "action": action,
                    "payload": payload,
                }
            )
        return self.broker.create_batch_operation(
            **kwargs, child_dispatch_specs=dispatch_specs
        )

    def create_child_operation(self, parent_operation_id: str, **kwargs: Any) -> dict[str, Any]:
        payload = _mapping(kwargs.get("payload"))
        operation = self.broker.create_child_operation(parent_operation_id, **kwargs)
        self._remember(operation, payload)
        refreshed = self.store.get_operation(str(operation["operation_id"])) or operation
        result = deepcopy(dict(refreshed))
        result["idempotent_replay"] = bool(operation.get("idempotent_replay"))
        return result

    def _remember(self, operation: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        action = str(payload.get("action") or "")
        if action != "patchbay_edge_preflight" and not action.startswith("codex_"):
            return
        operation_id = str(operation["operation_id"])
        source_payload_hash = semantic_payload_hash(payload)
        existing = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        if existing is not None:
            existing_hash = str(
                existing["record"].get("source_payload_hash")
                or existing["record"].get("payload_hash")
                or ""
            )
            if existing_hash != source_payload_hash:
                raise ValueError("operation_dispatch_payload_conflict")
            return
        durable_payload = self._persist_transient_payload(operation, payload)
        self.store.put_entity(
            EDGE_DISPATCH_ENTITY,
            operation_id,
            {
                "operation_id": operation_id,
                "action": action,
                "payload": durable_payload,
                "payload_hash": semantic_payload_hash(durable_payload),
                "source_payload_hash": source_payload_hash,
                "status": "pending",
                "created_at": operation.get("created_at") or time.time(),
            },
            expected_revision=0,
        )

    def _persist_transient_payload(
        self, operation: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        durable = deepcopy(dict(payload))
        arguments = durable.get("arguments")
        if not isinstance(arguments, dict):
            return durable
        artifact = arguments.get("artifact_file")
        if not isinstance(artifact, dict):
            return durable
        download_url = str(artifact.get("download_url") or "").strip()
        if not download_url:
            return durable

        artifact_metadata = {
            key: deepcopy(value)
            for key, value in artifact.items()
            if key != "download_url"
        }
        payload_record = self.broker.register_payload(
            str(operation["operation_id"]),
            payload_kind=_ARTIFACT_URL_PAYLOAD_KIND,
            checksum_sha256=hashlib.sha256(download_url.encode("utf-8")).hexdigest(),
            size_bytes=len(download_url.encode("utf-8")),
            storage_ref=download_url,
            expires_at=None,
            metadata={"artifact_file": artifact_metadata},
            principal_ref=str(operation.get("principal_ref") or ""),
        )
        artifact.pop("download_url", None)
        durable[_TRANSIENT_PAYLOAD_KEY] = {
            "payload_id": str(payload_record["payload_id"]),
            "payload_kind": _ARTIFACT_URL_PAYLOAD_KIND,
        }
        return durable

    def _hydrate_transient_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        hydrated = deepcopy(dict(payload))
        reference = hydrated.get(_TRANSIENT_PAYLOAD_KEY)
        if not isinstance(reference, Mapping):
            return hydrated
        payload_id = str(reference.get("payload_id") or "")
        metadata = self.store.get_payload_metadata(payload_id) if payload_id else None
        if metadata is None or metadata.get("status") not in {"ready", "acknowledged"}:
            raise ValueError("transient_payload_unavailable")
        arguments = hydrated.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("transient_payload_arguments_missing")
        artifact = arguments.get("artifact_file")
        if not isinstance(artifact, dict):
            raise ValueError("transient_payload_artifact_missing")
        artifact["download_url"] = str(metadata["storage_ref"])
        hydrated.pop(_TRANSIENT_PAYLOAD_KEY, None)
        return hydrated

    def _acknowledge_transient_payload(self, payload: Mapping[str, Any]) -> None:
        reference = payload.get(_TRANSIENT_PAYLOAD_KEY)
        if not isinstance(reference, Mapping):
            return
        payload_id = str(reference.get("payload_id") or "")
        metadata = self.store.get_payload_metadata(payload_id) if payload_id else None
        if metadata is None or metadata.get("status") != "ready":
            return
        acknowledged = self.broker.acknowledge_payload(
            payload_id,
            expected_revision=int(metadata["revision"]),
            principal_ref=str(
                (self.store.get_operation(str(metadata["operation_id"])) or {}).get(
                    "principal_ref"
                )
                or ""
            ),
        )
        latest = self.store.get_payload_metadata(payload_id)
        if acknowledged is None and (
            latest is None or latest.get("status") != "acknowledged"
        ):
            raise ValueError("transient_payload_acknowledgement_conflict")

    def _update_dispatch(self, operation_id: str, **changes: Any) -> None:
        entity = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        if entity is None:
            return
        if all(entity["record"].get(key) == value for key, value in changes.items()):
            return
        record = deepcopy(entity["record"])
        record.update(deepcopy(changes))
        record["updated_at"] = time.time()
        self.store.put_entity(
            EDGE_DISPATCH_ENTITY,
            operation_id,
            record,
            expected_revision=entity["revision"],
        )

    async def dispatch_pending(
        self,
        *,
        context: RequestContext | None = None,
        max_operations: int = 100,
    ) -> list[str]:
        delivered: list[str] = []
        # Operation state is authoritative. Query only work that can actually
        # be dispatched; scanning and rewriting terminal history on every MCP
        # call turns harmless status polling into unbounded database churn.
        limit = max(1, int(max_operations))
        dispatches = self.store.query_control_entities(
            EDGE_DISPATCH_ENTITY,
            operation_states=("created", "payload_ready", "dispatchable"),
            limit=limit,
        )
        for dispatch in dispatches:
            operation_id = str(dispatch["entity_id"])
            if await self.dispatch_if_pending(operation_id, context=context):
                delivered.append(operation_id)
        return delivered

    async def dispatch_if_pending(
        self,
        operation_id: str,
        *,
        context: RequestContext | None = None,
    ) -> bool:
        """Prepare and offer exactly one named operation, never global backlog."""

        operation = self.store.get_operation(operation_id)
        if operation is None:
            return False
        if operation["state"] == "created":
            operation = self.broker.prepare_operation(
                operation_id,
                expected_revision=int(operation["revision"]),
                principal_ref=str(operation["principal_ref"]),
            ) or self.store.get_operation(operation_id)
        if operation is not None and operation["state"] == "payload_ready":
            operation = self.broker.make_dispatchable(
                operation_id,
                expected_revision=int(operation["revision"]),
                principal_ref=str(operation["principal_ref"]),
            ) or self.store.get_operation(operation_id)
        if operation is None or operation["state"] != "dispatchable":
            return False
        await self.dispatch_operation(operation_id, context=context)
        return True

    async def dispatch_operation(
        self,
        operation_id: str,
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        dispatch = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        operation = self.store.get_operation(operation_id)
        if dispatch is None or operation is None:
            raise KeyError(f"Unknown Edge dispatch operation: {operation_id}")
        if operation["state"] in _TERMINAL_OPERATION_STATES:
            return operation
        if operation["state"] != "dispatchable":
            raise ValueError(f"Operation is not dispatchable: {operation_id}")
        running = self.broker.transition_operation(
            operation_id,
            expected_revision=int(operation["revision"]),
            state="running",
            principal_ref=str(operation["principal_ref"]),
        )
        operation = running or self.store.get_operation(operation_id)
        self._update_dispatch(operation_id, status="delivering")
        payload = _mapping(dispatch["record"].get("payload"))
        delivery_payload = (
            payload
            if callable(getattr(self.edge.delivery, "dispatch_operation", None))
            else self._hydrate_transient_payload(payload)
        )
        stored_context = payload.get("context")
        delivery_context = (
            RequestContext.from_public_metadata(dict(stored_context))
            if isinstance(stored_context, Mapping)
            else context
        )
        try:
            raw = await self.edge.dispatch_operation(
                operation=operation,
                payload=delivery_payload,
                context=delivery_context,
            )
        except Exception as error:
            return self._mark_delivery_unknown(operation_id, operation, error)

        envelope = _canonical_envelope(raw)
        if envelope["status"] == "pending":
            # Pull transport acceptance is not a domain result.  The Edge will
            # claim the durable attempt and post the authoritative outcome.
            self._update_dispatch(
                operation_id,
                status="offered",
                public_status="pending",
            )
            return self.store.get_operation(operation_id) or operation
        self._acknowledge_transient_payload(payload)
        if payload.get("action") == "patchbay_edge_preflight":
            result = self.runtime.record_preflight_result(
                work_group_id=str(payload.get("work_group_id") or ""),
                operation_id=operation_id,
                result=envelope["result"],
            )
            terminal = self.store.get_operation(operation_id)
            self._update_dispatch(
                operation_id,
                status="complete",
                public_status=result["status"],
            )
            return terminal or operation

        self._apply_worker_projection(
            payload,
            envelope["result"],
            source_operation_id=operation_id,
        )
        target_state = {
            "ok": "succeeded",
            "partial": "succeeded",
            "not_found": "succeeded",
            "blocked": "blocked",
            "failed": "failed",
            "pending": "outcome_unknown",
        }[str(envelope["status"])]
        terminal = self.broker.transition_operation(
            operation_id,
            expected_revision=int(operation["revision"]),
            state=target_state,
            principal_ref=str(operation["principal_ref"]),
            result=envelope,
        )
        terminal = terminal or self.store.get_operation(operation_id)
        self._update_dispatch(
            operation_id,
            status="complete" if target_state != "outcome_unknown" else "outcome_unknown",
            public_status=envelope["status"],
        )
        return terminal or operation

    def _mark_delivery_unknown(
        self,
        operation_id: str,
        operation: Mapping[str, Any],
        error: Exception,
    ) -> dict[str, Any]:
        current = self.store.get_operation(operation_id) or deepcopy(dict(operation))
        if current["state"] == "running":
            pending = public_envelope(
                "pending",
                result={"reason": "edge_delivery_outcome_unknown"},
                warnings=[{"code": "edge_delivery_outcome_unknown", "message": str(error)}],
                next_actions=[
                    {
                        "tool": "patchbay_operation_status",
                        "arguments": {"operation_id": operation_id},
                    }
                ],
            )
            saved = self.broker.transition_operation(
                operation_id,
                expected_revision=int(current["revision"]),
                state="outcome_unknown",
                principal_ref=str(current["principal_ref"]),
                result=pending,
                error={"reason": "edge_delivery_outcome_unknown", "message": str(error)},
            )
            current = saved or self.store.get_operation(operation_id) or current
        self._update_dispatch(operation_id, status="outcome_unknown", error=str(error))
        return current

    def _apply_worker_projection(
        self,
        payload: Mapping[str, Any],
        domain: Mapping[str, Any],
        *,
        source_operation_id: str = "",
    ) -> None:
        action = str(payload.get("action") or "")
        if action not in _WORKER_EDGE_ACTIONS:
            return
        target = _mapping(payload.get("target"))
        arguments = _mapping(payload.get("arguments"))
        worker = _mapping(domain.get("worker"))
        if not worker:
            worker = deepcopy(dict(domain))
        edge_worker_id = str(
            worker.get("edge_worker_id")
            or worker.get("worker_id")
            or domain.get("edge_worker_id")
            or domain.get("worker_id")
            or target.get("edge_worker_id")
            or ""
        )
        machine_id = str(payload.get("machine_id") or target.get("machine_id") or "")
        edge_generation = str(
            payload.get("edge_generation") or target.get("edge_generation") or ""
        )
        if not edge_worker_id or not machine_id or not edge_generation:
            return
        existing = next(
            (
                item["record"]
                for item in self.store.list_entities(WORKER_PROJECTION_ENTITY)
                if item["record"].get("machine_id") == machine_id
                and item["record"].get("edge_generation") == edge_generation
                and item["record"].get("edge_worker_id") == edge_worker_id
            ),
            {},
        )
        projected = {**deepcopy(dict(existing)), **worker}
        projected.update(
            {
                "edge_worker_id": edge_worker_id,
                "name": str(worker.get("name") or arguments.get("name") or existing.get("name") or edge_worker_id),
                "work_group_id": str(
                    payload.get("work_group_id")
                    or target.get("work_group_id")
                    or existing.get("work_group_id")
                    or ""
                ),
                "lane_id": str(
                    payload.get("lane_id")
                    or target.get("lane_id")
                    or existing.get("lane_id")
                    or "main"
                ),
                "workspace_ref": str(
                    payload.get("workspace_ref")
                    or target.get("workspace_ref")
                    or existing.get("workspace_ref")
                    or ""
                ),
            }
        )
        if action == "codex_worker_start":
            projected.setdefault("worker_state", "available")
            projected.setdefault("turn_state", "working")
            projected.setdefault("liveness", "active")
            projected.setdefault("integration_state", "not_applicable")
        elif action == "codex_worker_stop":
            projected.update(
                {
                    "worker_state": "stopped",
                    "turn_state": str(worker.get("turn_state") or "cancelled"),
                    "liveness": str(worker.get("liveness") or "terminal"),
                }
            )
        elif action == "codex_worker_integrate" and domain.get("applied") is True:
            projected["integration_state"] = "applied_to_checkout"
        work_group_id = str(projected.get("work_group_id") or "")
        if action == "codex_worker_integrate" and domain.get("applied") is True:
            self.runtime.record_group_base_mutation_snapshot(
                work_group_id=work_group_id,
                snapshot={
                    "head": "",
                    "changed_files": list(domain.get("main_changed_files") or []),
                    "dirty": True,
                    "observed_at": self.runtime._clock(),
                    "source": "accepted_worker_integration",
                },
                reason="accepted_worker_integration_changed_base_checkout",
                source_operation_id=source_operation_id,
            )
        elif action == "codex_worker_start" and str(
            arguments.get("workspace_mode") or "isolated_write"
        ) == "shared_write":
            self.runtime.mark_group_preflight_refresh_required(
                work_group_id=work_group_id,
                reason="shared_write_worker_can_change_base_checkout",
                source_operation_id=source_operation_id,
            )
        machine = self.store.get_entity(MACHINE_ENTITY, machine_id)
        if machine is None:
            return
        revision = int(self.store.schema_info()["v2_mutation_count"]) + 1
        self.runtime._persist_worker_snapshot(
            machine["record"],
            {"snapshot_kind": "delta", "workers": [projected], "tombstones": []},
            projection_revision=revision,
            received_at=self.runtime._clock(),
        )


class HubAppV2:
    """Complete, opt-in Hub V2 application graph without server wiring."""

    def __init__(
        self,
        state: str | Path | Mapping[str, Any] | HubStoreV2,
        *,
        edge_delivery: EdgeDeliveryPort | Callable[..., Any],
        canonical_pro_store: ProRequestCanonicalStore | None = None,
        pro_request_store: ProRequestCanonicalStore | None = None,
        pro_request_route: Mapping[str, Any] | None = None,
        clock: Callable[[], float] | None = None,
        admission_gate: AdmissionFreezeGate | None = None,
    ):
        if canonical_pro_store is not None and pro_request_store is not None:
            raise ValueError("Pass only one of canonical_pro_store or pro_request_store")
        canonical = canonical_pro_store or pro_request_store

        if isinstance(state, HubStoreV2):
            self.store = state
            self.config: dict[str, Any] = {}
            self._owns_store = False
        elif isinstance(state, Mapping):
            self.config = deepcopy(dict(state))
            self.store = HubStoreV2(self.config)
            self._owns_store = True
        else:
            self.config = {}
            self.store = HubStoreV2(state)
            self._owns_store = True

        self.edge = EdgeDeliveryBridgeV2(edge_delivery)
        self.admission_gate = admission_gate or AdmissionFreezeController(
            admission_coordination_path(self.store.path)
        )
        self.broker = OperationBroker(self.store, clock=clock)
        self.runtime = HubRuntimeV2(
            self.config,
            self.store,
            broker=self.broker,
            clock=clock,
        )
        self.dispatch_port = HubBrokerEdgeDispatchPortV2(self.broker, self.runtime, self.edge)
        self.runtime.broker = self.dispatch_port
        self.runtime_port = HubRuntimeTargetPortV2(self.runtime, self.edge)
        self.projection_port = HubWorkerProjectionPortV2(self.runtime)
        self.worker_adapter = HubWorkerAdapterV2(
            self.runtime_port,
            self.dispatch_port,
            self.projection_port,
        )
        self.workspace_adapter = WorkspaceAdapter(
            self.runtime_port,
            self.runtime_port,
            self.runtime_port,
        )
        if canonical is not None:
            self.pro_store_bridge = CanonicalProRequestStoreBridgeV2(canonical, self.edge)
            route = self._pro_route(pro_request_route, edge_delivery)
            self.pro_request_adapter = HubProRequestAdapterV2(
                self.store,
                self.pro_store_bridge,
                machine_id=route["machine_id"],
                edge_generation=route["edge_generation"],
                workspace_ref=route["workspace_ref"],
                work_group_id=route.get("work_group_id", ""),
                lane=route.get("lane", ""),
                visibility=route.get("visibility", "private"),
                origin_operation_id=route.get("origin_operation_id", ""),
                dispatch_executor=self.pro_store_bridge,
            )
        else:
            if pro_request_route:
                raise ValueError(
                    "pro_request_route is only valid with an injected canonical Pro Request store"
                )
            self.pro_store_bridge = None
            self.pro_request_adapter = FleetHubProRequestAdapterV2(
                self.store,
                self.runtime_port,
                self.edge,
                self.dispatch_port,
            )

        self.runtime.register_adapter("workers_and_artifacts", self.worker_adapter)
        self.runtime.register_adapter(
            "exceptional_manager_workspace_inspection", self.workspace_adapter
        )
        self.runtime.register_adapter("pro_requests", self.pro_request_adapter)
        self.tool_bindings = self._tool_bindings()
        if tuple(self.tool_bindings) != HUB_V2_TOOL_NAMES:
            raise RuntimeError("Hub V2 composition root does not cover the exact tool contract")
        self.protocol = HubProtocolV2(self)
        self._closed = False

    @staticmethod
    def _pro_route(
        route: Mapping[str, Any] | None,
        edge_delivery: Any,
    ) -> dict[str, str]:
        candidate = deepcopy(dict(route or {}))
        if not candidate:
            advertised = getattr(edge_delivery, "pro_request_route", None)
            if isinstance(advertised, Mapping):
                candidate = deepcopy(dict(advertised))
        if not candidate:
            candidate = {
                "machine_id": str(getattr(edge_delivery, "machine_id", "") or ""),
                "edge_generation": str(getattr(edge_delivery, "edge_generation", "") or ""),
                "workspace_ref": str(getattr(edge_delivery, "workspace_ref", "") or ""),
            }
        missing = [
            field for field in ("machine_id", "edge_generation", "workspace_ref") if not candidate.get(field)
        ]
        if missing:
            raise ValueError(
                "pro_request_route requires machine_id, edge_generation, and workspace_ref"
            )
        return {key: str(value) for key, value in candidate.items() if value is not None}

    @staticmethod
    def _tool_bindings() -> dict[str, str]:
        bindings: dict[str, str] = {}
        for family, names in HUB_V2_TOOL_FAMILIES.items():
            owner = {
                "workers_and_artifacts": "worker_adapter",
                "exceptional_manager_workspace_inspection": "workspace_adapter",
                "pro_requests": "pro_request_adapter",
            }.get(family, "runtime")
            for name in names:
                bindings[name] = owner
        return bindings

    @property
    def registered_tools(self) -> tuple[str, ...]:
        return tuple(self.tool_bindings)

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        if name not in self.tool_bindings:
            raise KeyError(f"Unknown Hub V2 tool: {name}")
        if name in HUB_V2_MUTATING_TOOL_NAMES:
            try:
                with self.admission_gate.admit_mutation():
                    result = await self._handle_tool_call_and_dispatch(
                        name, arguments, context=context
                    )
            except AdmissionFrozenError:
                state = getattr(self.admission_gate, "state", lambda: {})()
                return _canonical_envelope(
                    public_envelope(
                        "blocked",
                        result={
                            "reason": "hub_mutation_admission_frozen",
                            "admission": deepcopy(dict(state))
                            if isinstance(state, Mapping)
                            else {},
                        },
                        warnings=[
                            "Hub maintenance is blocking new mutations; existing status and result reconciliation remain available."
                        ],
                        next_actions=[
                            {"tool": "patchbay_fleet_status", "arguments": {}}
                        ],
                    )
                )
            return result

        result = deepcopy(
            dict(await self.runtime.handle_tool_call(name, arguments, context=context))
        )
        delivered: list[str] = []
        operation_id = str(_mapping(result.get("operation")).get("operation_id") or "")
        operation = self.store.get_operation(operation_id) if operation_id else None
        if (
            name != "patchbay_operation_status"
            and operation is not None
        ):
            try:
                with self.admission_gate.admit_mutation():
                    if await self.dispatch_port.dispatch_if_pending(
                        operation_id,
                        context=context,
                    ):
                        delivered.append(operation_id)
            except AdmissionFrozenError:
                delivered = []
        return await self._finalize_tool_result(
            name, arguments, result, delivered, context=context
        )

    async def dispatch_pending_operations(
        self,
        *,
        context: RequestContext | None = None,
        max_operations: int = 100,
    ) -> list[str]:
        """Run one explicit recovery/background dispatch cycle.

        MCP tools annotated read-only never call this method. Production
        lifecycle code may invoke it as a deliberate background mutation
        owner, while ordinary mutating tool calls continue to dispatch inside
        their own admission lease.
        """

        with self.admission_gate.admit_mutation():
            return await self.dispatch_port.dispatch_pending(
                context=context,
                max_operations=max_operations,
            )

    async def _handle_tool_call_and_dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None,
    ) -> Mapping[str, Any]:
        result = deepcopy(
            dict(await self.runtime.handle_tool_call(name, arguments, context=context))
        )
        delivered: list[str] = []
        for operation_id in self._result_operation_ids(result):
            if await self.dispatch_port.dispatch_if_pending(
                operation_id, context=context
            ):
                delivered.append(operation_id)
        return await self._finalize_tool_result(
            name, arguments, result, delivered, context=context
        )

    @staticmethod
    def _result_operation_ids(result: Mapping[str, Any]) -> list[str]:
        """Return only operations created or resumed by this tool result."""

        operation_ids: list[str] = []

        def add(value: Any) -> None:
            operation_id = str(_mapping(value).get("operation_id") or "")
            if operation_id and operation_id not in operation_ids:
                operation_ids.append(operation_id)

        add(result.get("operation"))
        payload = _mapping(result.get("result"))
        add(payload.get("readiness"))
        add(_mapping(payload.get("work_group")).get("readiness"))
        for item in payload.get("items") or []:
            if isinstance(item, Mapping):
                add(item.get("operation"))
        return operation_ids

    async def _finalize_tool_result(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        delivered: list[Mapping[str, Any]],
        *,
        context: RequestContext | None,
    ) -> Mapping[str, Any]:
        result = deepcopy(dict(result))
        if name == "patchbay_operation_status" and arguments.get("include_result"):
            result = self._refresh_operation_status_result(arguments, result)
        elif delivered and name in {
            "patchbay_work_group_create",
            "patchbay_work_group_resume",
            "patchbay_work_group_reassign",
        }:
            result = self._refresh_group_result(result, context=context)
        elif delivered and name in _WORKER_MUTATION_TOOLS:
            result = await self._refresh_worker_result(name, arguments, result, context=context)
        elif delivered and name in _PRO_REQUEST_MUTATION_TOOLS:
            result = self._refresh_pro_request_result(result)
        if name in {
            "patchbay_work_group_create",
            "patchbay_work_group_resume",
        }:
            result = await self._wait_for_group_preflight(
                arguments, result, context=context
            )
        return _canonical_envelope(result)

    def _refresh_operation_status_result(
        self,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Decorate completed remote results exactly as their owning tool does."""

        refreshed = deepcopy(dict(result))
        operation_id = str(arguments.get("operation_id") or "")
        operation = self.store.get_operation(operation_id) if operation_id else None
        operation_tool = str(
            (operation or {}).get("tool_name") or (operation or {}).get("tool") or ""
        )
        if (
            operation is None
            or not isinstance(self.pro_request_adapter, FleetHubProRequestAdapterV2)
            or not operation_tool.startswith(
                ("patchbay_pro_request_", "codex_pro_request_")
            )
        ):
            return refreshed
        decorated = self.pro_request_adapter.operation_result(operation)
        if decorated.get("status") == "pending":
            return refreshed
        payload = deepcopy(dict(refreshed.get("result") or {}))
        payload["domain_result"] = deepcopy(dict(decorated.get("result") or {}))
        refreshed["result"] = payload
        return refreshed

    def _refresh_pro_request_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = str(_mapping(result.get("operation")).get("operation_id") or "")
        operation = self.store.get_operation(operation_id) if operation_id else None
        if operation is None or not isinstance(
            self.pro_request_adapter, FleetHubProRequestAdapterV2
        ):
            return deepcopy(dict(result))
        return self.pro_request_adapter.operation_result(operation)

    def _refresh_group_result(
        self,
        result: Mapping[str, Any],
        *,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        original = deepcopy(dict(result))
        result_payload = _mapping(original.get("result"))
        group = _mapping(result_payload.get("work_group"))
        group_id = str(group.get("work_group_id") or "")
        if not group_id:
            return original
        refreshed = self.runtime.work_group_status(work_group_id=group_id, context=context)
        return self._merge_group_status_result(original, refreshed)

    @staticmethod
    def _merge_group_status_result(
        original: Mapping[str, Any],
        refreshed: Mapping[str, Any],
    ) -> dict[str, Any]:
        original_envelope = deepcopy(dict(original))
        if refreshed.get("status") != "ok":
            return original_envelope
        refreshed_payload = refreshed.get("result")
        if not isinstance(refreshed_payload, Mapping):
            return original_envelope
        merged = deepcopy(_mapping(original_envelope.get("result")))
        merged.update(deepcopy(dict(refreshed_payload)))
        return public_envelope(
            str(original_envelope.get("status") or "ok"),
            result=merged,
            operation=_mapping(original_envelope.get("operation")),
            warnings=list(original_envelope.get("warnings") or []),
            next_actions=list(original_envelope.get("next_actions") or []),
        )

    async def _wait_for_group_preflight(
        self,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        original = deepcopy(dict(result))
        requested_wait = arguments.get("wait_for_preflight_seconds", 0)
        if (
            not isinstance(requested_wait, int)
            or isinstance(requested_wait, bool)
            or requested_wait <= 0
        ):
            return original

        result_payload = _mapping(original.get("result"))
        group = _mapping(result_payload.get("work_group"))
        readiness = _mapping(result_payload.get("readiness"))
        if not readiness:
            readiness = _mapping(group.get("readiness"))
        if str(readiness.get("status") or "") != "pending":
            return original

        group_id = str(group.get("work_group_id") or "")
        if not group_id:
            return original
        baseline_revision = int(result_payload.get("status_revision") or 0)
        refreshed = await self.runtime.handle_tool_call(
            "patchbay_work_group_status",
            {
                "work_group_id": group_id,
                "since_revision": baseline_revision,
                "wait_for_change_seconds": requested_wait,
            },
            context=context,
        )
        return self._merge_group_status_result(original, refreshed)

    async def _refresh_worker_result(
        self,
        name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        operation_id = str(_mapping(result.get("operation")).get("operation_id") or "")
        operation = self.store.get_operation(operation_id) if operation_id else None
        if operation is None:
            return deepcopy(dict(result))
        route_result = await self.runtime_port.resolve_target(
            tool_name=name,
            arguments=arguments,
            context=context,
        )
        if str(route_result.get("status") or "") in PUBLIC_STATUSES:
            return deepcopy(dict(route_result))
        route = WorkerRoute.from_mapping(route_result, arguments=arguments)
        return self.worker_adapter._operation_result(operation, route=route)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "HubAppV2":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def create_hub_app_v2(
    state: str | Path | Mapping[str, Any] | HubStoreV2,
    **kwargs: Any,
) -> HubAppV2:
    return HubAppV2(state, **kwargs)


# Natural port spellings for callers composing tests or future network clients.
BrokerToEdgeDispatchPortV2 = HubBrokerEdgeDispatchPortV2
ProjectionQueryPortV2 = HubWorkerProjectionPortV2
RuntimeTargetResolutionPortV2 = HubRuntimeTargetPortV2
CanonicalProStoreBridgeV2 = CanonicalProRequestStoreBridgeV2


__all__ = [
    "BrokerToEdgeDispatchPortV2",
    "CanonicalProRequestStoreBridgeV2",
    "CanonicalProStoreBridgeV2",
    "EdgeDeliveryBridgeV2",
    "EdgeDeliveryPort",
    "HubAppV2",
    "HubBrokerEdgeDispatchPortV2",
    "HubRuntimeTargetPortV2",
    "HubWorkerProjectionPortV2",
    "ProjectionQueryPortV2",
    "RuntimeTargetResolutionPortV2",
    "create_hub_app_v2",
]
