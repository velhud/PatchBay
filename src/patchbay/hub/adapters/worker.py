"""Transparent Hub V2 adapter for the mature Edge worker surface.

The adapter owns public-worker-to-Edge argument translation and semantic Hub
results. Fleet/group resolution, operation persistence, and authoritative
worker projections remain injected ports so this module has no server wiring.
"""
from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Mapping, Protocol

from patchbay.hub.broker import OperationBrokerConflict
from patchbay.hub.operations import PUBLIC_STATUSES, normalize_domain_result, public_envelope
from patchbay.hub.tool_surface import HUB_V2_ACTION_MAP
from patchbay.protocol.context import RequestContext


WORKER_TOOL_NAMES = frozenset(
    {
        "patchbay_worker_options",
        "patchbay_worker_inbox",
        "patchbay_worker_start",
        "patchbay_worker_start_batch",
        "patchbay_worker_message",
        "patchbay_worker_list",
        "patchbay_worker_status",
        "patchbay_worker_wait",
        "patchbay_worker_inspect",
        "patchbay_worker_integrate",
        "patchbay_worker_stop",
    }
)

PROJECTION_TOOLS = {
    "patchbay_worker_list": "list",
    "patchbay_worker_status": "status",
}

MUTATING_INBOX_ACTIONS = frozenset({"import_file", "cleanup"})
ACTIVE_TURN_STATES = frozenset({"queued", "starting", "working"})
TERMINAL_OPERATION_STATES = frozenset({"succeeded", "blocked", "failed", "cancelled"})

# These are the canonical mature Edge fields. Hub-only routing, operation, and
# selector fields are intentionally absent. Values are copied when explicitly
# supplied, including false, zero, empty lists, and pagination boundaries.
EDGE_ARGUMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "codex_worker_options": (
        "repo_path",
        "model",
        "max_models",
        "include_model_details",
    ),
    "codex_worker_inbox": (
        "action",
        "artifact_file",
        "artifact_id",
        "label",
        "repo_path",
        "view",
        "file_path",
        "max_bytes",
        "max_entries",
        "takeover",
        "takeover_reason",
    ),
    "codex_worker_start": (
        "name",
        "brief",
        "repo_path",
        "workspace_mode",
        "auto_suffix",
        "include_untracked_from_base",
        "context_from_workers",
        "context_from_artifacts",
        "context_detail",
        "model",
        "reasoning_effort",
        "allow_concurrent_shared_write",
    ),
    "codex_worker_message": (
        "worker",
        "message",
        "repo_path",
        "context_from_workers",
        "context_from_artifacts",
        "context_detail",
        "model",
        "reasoning_effort",
        "takeover",
        "takeover_reason",
    ),
    "codex_worker_inspect": (
        "worker",
        "wait_seconds",
        "view",
        "file_path",
        "repo_path",
        "start_line",
        "end_line",
        "max_bytes",
        "accepted_dirty_base",
    ),
    "codex_worker_integrate": (
        "worker",
        "repo_path",
        "preview_token",
        "allow_dirty_base",
        "accepted_dirty_base",
        "takeover",
        "takeover_reason",
    ),
    "codex_worker_stop": (
        "worker",
        "repo_path",
        "cleanup_workspace",
        "discard_unintegrated_changes",
        "force",
        "reason",
        "takeover",
        "takeover_reason",
    ),
}


class WorkerAdapterRuntimePort(Protocol):
    """Resolve immutable routes and execute bounded read-only Edge actions."""

    def resolve_target(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...

    def execute_read(
        self,
        *,
        payload: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...


class WorkerAdapterProjectionPort(Protocol):
    """Read and wait on authoritative Hub worker projections."""

    def query(
        self,
        *,
        view: str,
        filters: Mapping[str, Any],
        route: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...

    def wait(
        self,
        *,
        filters: Mapping[str, Any],
        route: Mapping[str, Any],
        since_revision: int,
        timeout_seconds: float,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...

    def get_worker(
        self,
        *,
        route: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None]:
        ...


class WorkerAdapterBrokerPort(Protocol):
    """Minimal operation-broker surface used by worker mutations."""

    def create_operation(self, **kwargs: Any) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...

    def create_child_operation(
        self, parent_operation_id: str, **kwargs: Any
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...

    def prepare_operation(
        self, operation_id: str, **kwargs: Any
    ) -> Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None]:
        ...

    def make_dispatchable(
        self, operation_id: str, **kwargs: Any
    ) -> Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None]:
        ...

    def aggregate_parent(
        self, parent_operation_id: str, **kwargs: Any
    ) -> Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None]:
        ...

    def associate_operation(
        self, operation_id: str, **kwargs: Any
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class WorkerRoute:
    """Normalized immutable target selected by the injected runtime port."""

    work_group_id: str = ""
    lane_id: str = ""
    machine_id: str = ""
    edge_generation: Any = ""
    workspace_ref: str = ""
    workspace_projection_ref: str = ""
    repo_path: str = ""
    fleet_worker_ref: str = ""
    edge_worker_id: str = ""
    principal_ref: str = ""
    work_group: Mapping[str, Any] = field(default_factory=dict)
    lane: Mapping[str, Any] = field(default_factory=dict)
    worker: Mapping[str, Any] = field(default_factory=dict)
    machine: Mapping[str, Any] = field(default_factory=dict)
    workspace: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, arguments: Mapping[str, Any]
    ) -> "WorkerRoute":
        source = deepcopy(dict(value))
        target = source.get("target") if isinstance(source.get("target"), Mapping) else {}
        group = _mapping(source.get("work_group") or source.get("group"))
        lane = _mapping(source.get("lane"))
        worker = _mapping(source.get("worker"))
        machine = _mapping(source.get("machine"))
        workspace = _mapping(source.get("workspace") or source.get("workspace_projection"))

        def pick(*values: Any) -> Any:
            return next((candidate for candidate in values if candidate not in (None, "")), "")

        work_group_id = str(
            pick(
                source.get("work_group_id"),
                target.get("work_group_id"),
                group.get("work_group_id"),
                arguments.get("work_group_id"),
            )
        )
        lane_id = str(
            pick(
                source.get("lane_id"),
                target.get("lane_id"),
                target.get("lane"),
                lane.get("lane_id"),
                lane.get("lane"),
                arguments.get("lane"),
            )
        )
        machine_id = str(
            pick(
                source.get("machine_id"),
                target.get("machine_id"),
                machine.get("machine_id"),
                group.get("pinned_machine_id"),
                arguments.get("machine_id"),
            )
        )
        edge_generation = pick(
            source.get("edge_generation"),
            source.get("pinned_edge_generation"),
            target.get("edge_generation"),
            machine.get("edge_generation"),
            group.get("pinned_edge_generation"),
        )
        workspace_ref = str(
            pick(
                source.get("workspace_ref"),
                target.get("workspace_ref"),
                workspace.get("workspace_ref"),
                group.get("workspace_ref"),
                arguments.get("workspace_ref"),
            )
        )
        workspace_projection_ref = str(
            pick(
                source.get("workspace_projection_ref"),
                target.get("workspace_projection_ref"),
                workspace.get("workspace_projection_ref"),
                workspace.get("projection_ref"),
            )
        )
        repo_path = str(
            pick(
                source.get("repo_path"),
                target.get("repo_path"),
                workspace.get("resolved_repo_path"),
                workspace.get("repo_path"),
                workspace.get("local_path"),
                workspace.get("path"),
                group.get("repo_path"),
                arguments.get("repo_path"),
            )
        )
        fleet_worker_ref = str(
            pick(
                source.get("fleet_worker_ref"),
                target.get("fleet_worker_ref"),
                worker.get("fleet_worker_ref"),
                arguments.get("fleet_worker_ref"),
            )
        )
        edge_worker_id = str(
            pick(
                source.get("edge_worker_id"),
                target.get("edge_worker_id"),
                worker.get("edge_worker_id"),
                worker.get("worker_id"),
                arguments.get("worker") if not arguments.get("fleet_worker_ref") else "",
            )
        )
        principal_ref = str(pick(source.get("principal_ref"), target.get("principal_ref")))

        if not group and work_group_id:
            group = {"work_group_id": work_group_id}
        if not lane and lane_id:
            lane = {"lane_id": lane_id}
        if not machine and machine_id:
            machine = {"machine_id": machine_id}
        if not workspace and (workspace_ref or workspace_projection_ref):
            workspace = {
                **({"workspace_ref": workspace_ref} if workspace_ref else {}),
                **(
                    {"workspace_projection_ref": workspace_projection_ref}
                    if workspace_projection_ref
                    else {}
                ),
            }
        if not worker and (fleet_worker_ref or edge_worker_id):
            worker = {
                **({"fleet_worker_ref": fleet_worker_ref} if fleet_worker_ref else {}),
                **({"edge_worker_id": edge_worker_id} if edge_worker_id else {}),
            }

        return cls(
            work_group_id=work_group_id,
            lane_id=lane_id,
            machine_id=machine_id,
            edge_generation=edge_generation,
            workspace_ref=workspace_ref,
            workspace_projection_ref=workspace_projection_ref,
            repo_path=repo_path,
            fleet_worker_ref=fleet_worker_ref,
            edge_worker_id=edge_worker_id,
            principal_ref=principal_ref,
            work_group=group,
            lane=lane,
            worker=worker,
            machine=machine,
            workspace=workspace,
        )

    def as_port_mapping(self) -> dict[str, Any]:
        return {
            "work_group_id": self.work_group_id,
            "lane_id": self.lane_id,
            "machine_id": self.machine_id,
            "edge_generation": self.edge_generation,
            "workspace_ref": self.workspace_ref,
            "workspace_projection_ref": self.workspace_projection_ref,
            "repo_path": self.repo_path,
            "fleet_worker_ref": self.fleet_worker_ref,
            "edge_worker_id": self.edge_worker_id,
            "principal_ref": self.principal_ref,
            "work_group": deepcopy(dict(self.work_group)),
            "lane": deepcopy(dict(self.lane)),
            "worker": deepcopy(dict(self.worker)),
            "machine": deepcopy(dict(self.machine)),
            "workspace": deepcopy(dict(self.workspace)),
        }

    def edge_target(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "work_group_id": self.work_group_id,
                "lane_id": self.lane_id,
                "machine_id": self.machine_id,
                "edge_generation": self.edge_generation,
                "workspace_ref": self.workspace_ref,
                "workspace_projection_ref": self.workspace_projection_ref,
                "fleet_worker_ref": self.fleet_worker_ref,
                "edge_worker_id": self.edge_worker_id,
            }.items()
            if value not in (None, "")
        }


class HubWorkerAdapterV2:
    """Handle all eleven frozen ``patchbay_worker_*`` manager tools."""

    def __init__(
        self,
        runtime: WorkerAdapterRuntimePort,
        broker: WorkerAdapterBrokerPort,
        projection: WorkerAdapterProjectionPort,
    ):
        self.runtime = runtime
        self.broker = broker
        self.projection = projection

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        if name not in WORKER_TOOL_NAMES:
            raise ValueError(f"Unsupported Hub V2 worker tool: {name}")
        args = deepcopy(dict(arguments))
        self._validate_action_gate(name, args)
        if name == "patchbay_worker_start_batch":
            self._prevalidate_batch(args)

        route_result = await _maybe_await(
            self.runtime.resolve_target(
                tool_name=name,
                arguments=deepcopy(args),
                context=context,
            )
        )
        if _is_public_envelope(route_result):
            return _complete_envelope(route_result)
        route = WorkerRoute.from_mapping(route_result, arguments=args)
        if name == "patchbay_worker_start_batch":
            self._validate_shared_write_policy(args, route)

        if name in PROJECTION_TOOLS:
            return await self._projection_read(
                view=PROJECTION_TOOLS[name], args=args, route=route, context=context
            )
        if name == "patchbay_worker_wait":
            return await self._projection_wait(args=args, route=route, context=context)
        if name == "patchbay_worker_message":
            active = await self._active_turn_projection(route=route, context=context)
            if active is not None:
                return public_envelope(
                    "blocked",
                    result=self._enrich_result(
                        {
                            "accepted": False,
                            "reason": "active_turn_in_progress",
                            "turn_state": str(active.get("turn_state") or "working"),
                        },
                        route,
                    ),
                    next_actions=[
                        {
                            "tool": "patchbay_worker_wait",
                            "arguments": {
                                **(
                                    {"work_group_id": route.work_group_id}
                                    if route.work_group_id
                                    else {}
                                ),
                                "since_revision": int(active.get("projection_revision") or 0),
                            },
                        }
                    ],
                )
        if name == "patchbay_worker_start_batch":
            try:
                return await self._start_batch(args=args, route=route, context=context)
            except OperationBrokerConflict as error:
                return self._broker_conflict_result(error, route=route)
        if self._is_mutation(name, args):
            try:
                return await self._create_single_operation(
                    name=name, args=args, route=route, context=context
                )
            except OperationBrokerConflict as error:
                return self._broker_conflict_result(error, route=route)
        return await self._routed_read(name=name, args=args, route=route, context=context)

    def build_edge_action_payload(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        route: WorkerRoute | Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Build the stable operation payload delivered to the selected Edge."""

        normalized = (
            route
            if isinstance(route, WorkerRoute)
            else WorkerRoute.from_mapping(route, arguments=arguments)
        )
        action = HUB_V2_ACTION_MAP.get(tool_name, "")
        if action not in EDGE_ARGUMENT_FIELDS:
            raise ValueError(f"{tool_name} is not a single Edge worker action")
        edge_arguments = {
            key: deepcopy(arguments[key])
            for key in EDGE_ARGUMENT_FIELDS[action]
            if key in arguments
        }
        if normalized.repo_path and not edge_arguments.get("repo_path"):
            edge_arguments["repo_path"] = normalized.repo_path
        if (
            action == "codex_worker_start"
            and str(edge_arguments.get("workspace_mode") or "isolated_write") == "shared_write"
            and str(normalized.work_group.get("shared_write_policy") or "serialized")
            == "manager_controlled"
        ):
            edge_arguments["allow_concurrent_shared_write"] = True
        if "worker" in EDGE_ARGUMENT_FIELDS[action] and normalized.edge_worker_id:
            edge_arguments["worker"] = normalized.edge_worker_id

        public_context = (
            context.durable_operation_metadata() if context is not None else {}
        )
        if normalized.work_group_id:
            public_context["work_group_id"] = normalized.work_group_id
        if normalized.lane_id:
            public_context["lane_id"] = normalized.lane_id

        return {
            "action": action,
            "arguments": edge_arguments,
            "target": normalized.edge_target(),
            "context": public_context,
            **(
                {"work_group_id": normalized.work_group_id}
                if normalized.work_group_id
                else {}
            ),
            **({"lane_id": normalized.lane_id} if normalized.lane_id else {}),
            **({"machine_id": normalized.machine_id} if normalized.machine_id else {}),
            **(
                {"edge_generation": normalized.edge_generation}
                if normalized.edge_generation not in (None, "")
                else {}
            ),
            **(
                {"workspace_ref": normalized.workspace_ref}
                if normalized.workspace_ref
                else {}
            ),
            **(
                {"workspace_projection_ref": normalized.workspace_projection_ref}
                if normalized.workspace_projection_ref
                else {}
            ),
            **(
                {"fleet_worker_ref": normalized.fleet_worker_ref}
                if normalized.fleet_worker_ref
                else {}
            ),
        }

    async def _routed_read(
        self,
        *,
        name: str,
        args: Mapping[str, Any],
        route: WorkerRoute,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        payload = self.build_edge_action_payload(name, args, route, context=context)
        raw = await _maybe_await(
            self.runtime.execute_read(payload=deepcopy(payload), context=context)
        )
        return self._semantic_result(raw, route=route)

    async def _projection_read(
        self,
        *,
        view: str,
        args: Mapping[str, Any],
        route: WorkerRoute,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        raw = await _maybe_await(
            self.projection.query(
                view=view,
                filters=deepcopy(dict(args)),
                route=route.as_port_mapping(),
                context=context,
            )
        )
        return self._semantic_result(raw, route=route)

    async def _projection_wait(
        self,
        *,
        args: Mapping[str, Any],
        route: WorkerRoute,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        if args.get("since_revision") is None:
            baseline = await _maybe_await(
                self.projection.query(
                    view="status",
                    filters=deepcopy(dict(args)),
                    route=route.as_port_mapping(),
                    context=context,
                )
            )
            if _is_public_envelope(baseline):
                baseline_result = baseline.get("result")
            else:
                baseline_result = baseline
            since_revision = (
                int(baseline_result.get("projection_revision") or 0)
                if isinstance(baseline_result, Mapping)
                else 0
            )
        else:
            since_revision = max(0, int(args.get("since_revision") or 0))
        raw = await _maybe_await(
            self.projection.wait(
                filters=deepcopy(dict(args)),
                route=route.as_port_mapping(),
                since_revision=since_revision,
                timeout_seconds=max(0.0, float(args.get("wait_seconds") or 0)),
                context=context,
            )
        )
        return self._semantic_result(raw, route=route)

    async def _active_turn_projection(
        self,
        *,
        route: WorkerRoute,
        context: RequestContext | None,
    ) -> dict[str, Any] | None:
        candidate = dict(route.worker)
        if str(candidate.get("turn_state") or "") not in ACTIVE_TURN_STATES:
            raw = await _maybe_await(
                self.projection.get_worker(route=route.as_port_mapping(), context=context)
            )
            if isinstance(raw, Mapping):
                if _is_public_envelope(raw):
                    result = raw.get("result")
                    if isinstance(result, Mapping):
                        raw = result.get("worker") or result
                if isinstance(raw, Mapping):
                    candidate = deepcopy(dict(raw))
        return candidate if str(candidate.get("turn_state") or "") in ACTIVE_TURN_STATES else None

    async def _create_single_operation(
        self,
        *,
        name: str,
        args: Mapping[str, Any],
        route: WorkerRoute,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        payload = self.build_edge_action_payload(name, args, route, context=context)
        operation = await _maybe_await(
            self.broker.create_operation(
                tool=name,
                logical_target=self._logical_target(name, args, route),
                idempotency_key=str(args.get("idempotency_key") or ""),
                payload=deepcopy(payload),
                principal_ref=route.principal_ref,
            )
        )
        await self._associate_operation(operation, route=route)
        operation = await self._ensure_dispatchable(operation, route=route)
        return self._operation_result(operation, route=route)

    async def _start_batch(
        self,
        *,
        args: Mapping[str, Any],
        route: WorkerRoute,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        child_specs = self._batch_child_specs(args, route=route, context=context)
        parent_payload = {
            "action": "compound.codex_worker_start",
            "target": route.edge_target(),
            "context": (
                context.durable_operation_metadata() if context is not None else {}
            ),
            "work_group_id": route.work_group_id,
            "shared_brief": args["shared_brief"],
            "items": [deepcopy(spec) for spec in child_specs],
        }
        parent = await _maybe_await(
            self.broker.create_operation(
                tool="patchbay_worker_start_batch",
                logical_target=self._logical_target(
                    "patchbay_worker_start_batch", args, route
                ),
                idempotency_key=str(args.get("idempotency_key") or ""),
                payload=parent_payload,
                principal_ref=route.principal_ref,
            )
        )
        await self._associate_operation(parent, route=route)
        if str(parent.get("state") or "") in TERMINAL_OPERATION_STATES:
            return self._operation_result(parent, route=route)

        child_operations: list[dict[str, Any]] = []
        for spec in child_specs:
            item_id = str(spec["item_id"])
            edge_payload = deepcopy(dict(spec["edge_payload"]))
            child = await _maybe_await(
                self.broker.create_child_operation(
                    str(parent["operation_id"]),
                    item_id=item_id,
                    tool="patchbay_worker_start",
                    logical_target=str(spec["logical_target"]),
                    payload={
                        "item_id": item_id,
                        "item_idempotency_key": spec["item_idempotency_key"],
                        **edge_payload,
                    },
                    principal_ref=route.principal_ref,
                )
            )
            child_route = WorkerRoute.from_mapping(
                {
                    **route.as_port_mapping(),
                    "lane_id": spec["lane_id"],
                    "lane": {"lane_id": spec["lane_id"]},
                },
                arguments=edge_payload["arguments"],
            )
            await self._associate_operation(child, route=child_route)
            child = await self._ensure_dispatchable(child, route=child_route)
            child_operations.append(
                {
                    "item_id": item_id,
                    "name": spec["name"],
                    "lane": spec["lane_id"],
                    "status": _public_status_for_operation(child),
                    "operation": self._public_operation(child, route=child_route),
                    "result": _operation_domain_result(child),
                }
            )

        aggregated = await _maybe_await(
            self.broker.aggregate_parent(
                str(parent["operation_id"]), principal_ref=route.principal_ref
            )
        )
        if isinstance(aggregated, Mapping):
            parent = deepcopy(dict(aggregated))
        if str(parent.get("state") or "") in TERMINAL_OPERATION_STATES:
            return self._operation_result(parent, route=route)

        counts = {
            "total": len(child_operations),
            "pending": sum(item["status"] == "pending" for item in child_operations),
            "ok": sum(item["status"] == "ok" for item in child_operations),
            "blocked": sum(item["status"] == "blocked" for item in child_operations),
            "failed": sum(item["status"] == "failed" for item in child_operations),
        }
        known_statuses = {item["status"] for item in child_operations}
        status = "partial" if "pending" not in known_statuses and len(known_statuses) > 1 else "pending"
        return public_envelope(
            status,
            result=self._enrich_result(
                {"items": child_operations, "counts": counts}, route
            ),
            operation=self._public_operation(parent, route=route),
            next_actions=self._operation_next_actions(parent),
        )

    def _batch_child_specs(
        self,
        args: Mapping[str, Any],
        *,
        route: WorkerRoute,
        context: RequestContext | None,
    ) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        shared_workers = list(args.get("context_from_workers") or [])
        shared_artifacts = list(args.get("context_from_artifacts") or [])
        for item_value in args.get("workers") or []:
            item = deepcopy(dict(item_value))
            lane_id = str(item["lane"]).strip()
            child_args: dict[str, Any] = {
                "name": item["name"],
                "brief": _joined_brief(str(args["shared_brief"]), str(item["mission"])),
            }
            for key in (
                "workspace_mode",
                "model",
                "reasoning_effort",
                "include_untracked_from_base",
                "auto_suffix",
            ):
                if key in item:
                    child_args[key] = deepcopy(item[key])
            worker_context = _stable_union(shared_workers, item.get("context_from_workers") or [])
            artifact_context = _stable_union(
                shared_artifacts, item.get("context_from_artifacts") or []
            )
            if worker_context:
                child_args["context_from_workers"] = worker_context
            if artifact_context:
                child_args["context_from_artifacts"] = artifact_context
            if "context_detail" in args:
                child_args["context_detail"] = args["context_detail"]

            child_route = WorkerRoute.from_mapping(
                {
                    **route.as_port_mapping(),
                    "lane_id": lane_id,
                    "lane": {"lane_id": lane_id},
                },
                arguments=child_args,
            )
            edge_payload = self.build_edge_action_payload(
                "patchbay_worker_start", child_args, child_route, context=context
            )
            specs.append(
                {
                    "item_id": str(item["item_id"]),
                    "item_idempotency_key": str(item["idempotency_key"]),
                    "name": str(item["name"]),
                    "lane_id": lane_id,
                    "logical_target": self._logical_target(
                        "patchbay_worker_start", child_args, child_route
                    ),
                    "edge_payload": edge_payload,
                }
            )
        return specs

    async def _associate_operation(
        self, operation: Mapping[str, Any], *, route: WorkerRoute
    ) -> None:
        if not route.work_group_id:
            return
        await _maybe_await(
            self.broker.associate_operation(
                str(operation["operation_id"]),
                work_group_id=route.work_group_id,
                principal_ref=route.principal_ref,
                kind="worker",
            )
        )

    async def _ensure_dispatchable(
        self, operation: Mapping[str, Any], *, route: WorkerRoute
    ) -> dict[str, Any]:
        current = deepcopy(dict(operation))
        if str(current.get("state") or "") == "created":
            updated = await _maybe_await(
                self.broker.prepare_operation(
                    str(current["operation_id"]),
                    expected_revision=int(current["revision"]),
                    principal_ref=route.principal_ref,
                )
            )
            if isinstance(updated, Mapping):
                current = deepcopy(dict(updated))
        if str(current.get("state") or "") == "payload_ready":
            updated = await _maybe_await(
                self.broker.make_dispatchable(
                    str(current["operation_id"]),
                    expected_revision=int(current["revision"]),
                    principal_ref=route.principal_ref,
                )
            )
            if isinstance(updated, Mapping):
                current = deepcopy(dict(updated))
        return current

    def _operation_result(
        self, operation: Mapping[str, Any], *, route: WorkerRoute
    ) -> dict[str, Any]:
        normalized = operation.get("result")
        public_operation = self._public_operation(operation, route=route)
        if isinstance(normalized, Mapping) and str(normalized.get("status") or "") in PUBLIC_STATUSES:
            envelope = _complete_envelope(normalized)
            envelope["result"] = self._enrich_result(envelope["result"], route)
            envelope["operation"] = public_operation
            return envelope
        return public_envelope(
            _public_status_for_operation(operation),
            result=self._enrich_result({}, route),
            operation=public_operation,
            next_actions=self._operation_next_actions(operation),
        )

    def _semantic_result(
        self, raw: Mapping[str, Any], *, route: WorkerRoute
    ) -> dict[str, Any]:
        if _is_public_envelope(raw):
            envelope = _complete_envelope(raw)
        else:
            payload = deepcopy(dict(raw))
            queue_state = str(payload.get("state") or payload.get("status") or "").lower()
            if payload.get("command_id") or queue_state in {"queued", "running", "accepted"}:
                operation = payload.get("operation") if isinstance(payload.get("operation"), Mapping) else {}
                envelope = public_envelope("pending", operation=operation)
            elif payload.get("error") and not payload.get("accepted"):
                envelope = public_envelope("failed", result=payload)
            else:
                envelope = normalize_domain_result(payload)
        envelope["result"] = self._enrich_result(envelope["result"], route)
        return envelope

    def _broker_conflict_result(
        self, error: Exception, *, route: WorkerRoute
    ) -> dict[str, Any]:
        reason = str(error) or "operation_conflict"
        return public_envelope(
            "blocked",
            result=self._enrich_result(
                {"reason": reason, "retryable": False}, route
            ),
        )

    def _enrich_result(
        self, result: Mapping[str, Any], route: WorkerRoute
    ) -> dict[str, Any]:
        enriched = deepcopy(dict(result))
        self._normalize_liveness(enriched)
        route_fields: tuple[tuple[str, Any], ...] = (
            ("work_group", route.work_group),
            ("lane", route.lane),
            ("worker", route.worker),
            ("machine", route.machine),
            ("workspace", route.workspace),
            ("fleet_worker_ref", route.fleet_worker_ref),
            (
                "edge_generation",
                str(route.edge_generation)
                if route.edge_generation not in (None, "")
                else "",
            ),
            ("workspace_ref", route.workspace_ref),
            ("workspace_projection_ref", route.workspace_projection_ref),
        )
        for key, value in route_fields:
            if value not in (None, "", {}):
                enriched.setdefault(key, deepcopy(value))
        workers = enriched.get("workers")
        if isinstance(workers, list):
            enriched["workers"] = [
                self._enrich_worker(item, route) if isinstance(item, Mapping) else item
                for item in workers
            ]
        return enriched

    @staticmethod
    def _enrich_worker(worker: Mapping[str, Any], route: WorkerRoute) -> dict[str, Any]:
        enriched = deepcopy(dict(worker))
        HubWorkerAdapterV2._normalize_liveness(enriched)
        for key, value in (
            ("work_group_id", route.work_group_id),
            ("lane_id", route.lane_id),
            ("machine_id", route.machine_id),
            (
                "edge_generation",
                str(route.edge_generation)
                if route.edge_generation not in (None, "")
                else "",
            ),
            ("workspace_ref", route.workspace_ref),
            ("workspace_projection_ref", route.workspace_projection_ref),
        ):
            if value not in (None, ""):
                enriched.setdefault(key, value)
        if route.fleet_worker_ref:
            enriched.setdefault("fleet_worker_ref", route.fleet_worker_ref)
        return enriched

    @staticmethod
    def _normalize_liveness(value: dict[str, Any]) -> None:
        """Preserve mature diagnostics while exposing the fleet state axis."""

        detail = value.get("liveness")
        if not isinstance(detail, Mapping):
            return
        copied = deepcopy(dict(detail))
        value["liveness_detail"] = copied
        state = str(copied.get("status") or "starting")
        value["liveness"] = (
            state
            if state in {"starting", "active", "quiet", "stale", "lost", "terminal"}
            else "starting"
        )

    @staticmethod
    def _is_mutation(name: str, args: Mapping[str, Any]) -> bool:
        if name == "patchbay_worker_inbox":
            return str(args.get("action") or "").strip().lower() in MUTATING_INBOX_ACTIONS
        return name in {
            "patchbay_worker_start",
            "patchbay_worker_message",
            "patchbay_worker_integrate",
            "patchbay_worker_stop",
        }

    @staticmethod
    def _validate_action_gate(name: str, args: Mapping[str, Any]) -> None:
        if name == "patchbay_worker_integrate" and not str(
            args.get("preview_token") or ""
        ).strip():
            raise ValueError("patchbay_worker_integrate requires preview_token")
        if name == "patchbay_worker_stop" and bool(args.get("cleanup_workspace")):
            if args.get("discard_unintegrated_changes") is not True:
                raise ValueError(
                    "cleanup_workspace=true requires discard_unintegrated_changes=true"
                )
        if name in {
            "patchbay_worker_inbox",
            "patchbay_worker_start",
            "patchbay_worker_start_batch",
            "patchbay_worker_message",
            "patchbay_worker_integrate",
            "patchbay_worker_stop",
        } and not str(args.get("idempotency_key") or "").strip():
            raise ValueError(f"{name} requires idempotency_key")

    @staticmethod
    def _prevalidate_batch(args: Mapping[str, Any]) -> None:
        if not str(args.get("work_group_id") or "").strip():
            raise ValueError("patchbay_worker_start_batch requires work_group_id")
        if not str(args.get("shared_brief") or "").strip():
            raise ValueError("patchbay_worker_start_batch requires shared_brief")
        workers = args.get("workers")
        if not isinstance(workers, list) or not workers:
            raise ValueError("patchbay_worker_start_batch requires at least one worker")
        item_ids: set[str] = set()
        item_keys: set[str] = set()
        names: dict[str, list[Mapping[str, Any]]] = {}
        for index, value in enumerate(workers):
            if not isinstance(value, Mapping):
                raise ValueError(f"workers[{index}] must be an object")
            item = dict(value)
            for field_name in ("item_id", "idempotency_key", "name", "lane", "mission"):
                if not str(item.get(field_name) or "").strip():
                    raise ValueError(f"workers[{index}].{field_name} is required")
            item_id = str(item["item_id"]).strip()
            item_key = str(item["idempotency_key"]).strip()
            if item_id in item_ids:
                raise ValueError(f"duplicate batch item_id: {item_id}")
            if item_key in item_keys:
                raise ValueError(f"duplicate batch item idempotency_key: {item_key}")
            item_ids.add(item_id)
            item_keys.add(item_key)
            names.setdefault(str(item["name"]).strip().casefold(), []).append(item)
        for duplicate_name, matching in names.items():
            if len(matching) > 1 and not all(bool(item.get("auto_suffix")) for item in matching):
                raise ValueError(
                    f"duplicate batch worker name requires auto_suffix=true for every item: {duplicate_name}"
                )

    @staticmethod
    def _validate_shared_write_policy(args: Mapping[str, Any], route: WorkerRoute) -> None:
        shared = [
            str(item.get("name") or "")
            for item in (args.get("workers") or [])
            if isinstance(item, Mapping)
            and str(item.get("workspace_mode") or "isolated_write") == "shared_write"
        ]
        if len(shared) <= 1:
            return
        policy = str(route.work_group.get("shared_write_policy") or "serialized")
        if policy != "manager_controlled":
            raise ValueError(
                "This work group uses shared_write_policy=serialized. Multiple shared_write workers require "
                "an architect-created group with shared_write_policy=manager_controlled, or isolated_write workers."
            )

    @staticmethod
    def _logical_target(
        name: str, args: Mapping[str, Any], route: WorkerRoute
    ) -> str:
        if name == "patchbay_worker_start_batch":
            return route.work_group_id or route.workspace_projection_ref or route.machine_id
        if name == "patchbay_worker_start":
            return ":".join(
                part
                for part in (
                    route.work_group_id or route.workspace_projection_ref or route.machine_id,
                    route.lane_id,
                    str(args.get("name") or ""),
                )
                if part
            )
        if route.fleet_worker_ref:
            return route.fleet_worker_ref
        if route.edge_worker_id:
            return f"{route.machine_id}@{route.edge_generation}:{route.edge_worker_id}"
        return (
            route.workspace_projection_ref
            or route.workspace_ref
            or route.work_group_id
            or route.machine_id
        )

    @staticmethod
    def _public_operation(
        operation: Mapping[str, Any], *, route: WorkerRoute
    ) -> dict[str, Any]:
        source = dict(operation)
        result: dict[str, Any] = {}
        field_map = {
            "operation_id": "operation_id",
            "parent_operation_id": "parent_operation_id",
            "state": "state",
            "attempt_id": "attempt_id",
            "attempt_state": "attempt_state",
            "fencing_token": "fencing_token",
            "idempotency_key": "idempotency_key",
            "semantic_payload_hash": "semantic_payload_hash",
            "revision": "revision",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "retryable": "retryable",
            "reconciliation_state": "reconciliation_state",
            "item_results": "item_results",
        }
        for public_name, source_name in field_map.items():
            value = source.get(source_name)
            if value not in (None, ""):
                result[public_name] = deepcopy(value)
        tool_name = source.get("tool_name") or source.get("tool")
        if tool_name:
            result["tool_name"] = str(tool_name)
        if route.machine_id:
            result.setdefault("machine_id", route.machine_id)
        if route.edge_generation not in (None, ""):
            result.setdefault("edge_generation", str(route.edge_generation))
        return result

    @staticmethod
    def _operation_next_actions(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
        if str(operation.get("state") or "") in TERMINAL_OPERATION_STATES:
            return []
        operation_id = str(operation.get("operation_id") or "")
        return (
            [
                {
                    "tool": "patchbay_operation_status",
                    "arguments": {"operation_id": operation_id},
                }
            ]
            if operation_id
            else []
        )


# The concise name is convenient for composition while the Hub-prefixed name
# states the public ownership boundary explicitly.
WorkerAdapterV2 = HubWorkerAdapterV2


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _mapping(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _is_public_envelope(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and str(value.get("status") or "") in PUBLIC_STATUSES
        and isinstance(value.get("result"), Mapping)
    )


def _complete_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    return public_envelope(
        str(value.get("status") or "failed"),
        result=value.get("result") if isinstance(value.get("result"), Mapping) else {},
        operation=(
            value.get("operation") if isinstance(value.get("operation"), Mapping) else {}
        ),
        warnings=list(value.get("warnings") or []),
        next_actions=list(value.get("next_actions") or []),
    )


def _public_status_for_operation(operation: Mapping[str, Any]) -> str:
    normalized = operation.get("result")
    if isinstance(normalized, Mapping) and str(normalized.get("status") or "") in PUBLIC_STATUSES:
        return str(normalized["status"])
    return {
        "succeeded": "ok",
        "blocked": "blocked",
        "failed": "failed",
        "cancelled": "blocked",
    }.get(str(operation.get("state") or ""), "pending")


def _operation_domain_result(operation: Mapping[str, Any]) -> dict[str, Any]:
    normalized = operation.get("result")
    if isinstance(normalized, Mapping) and isinstance(normalized.get("result"), Mapping):
        return deepcopy(dict(normalized["result"]))
    return {}


def _stable_union(left: list[Any], right: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in [*left, *right]:
        if value not in result:
            result.append(deepcopy(value))
    return result


def _joined_brief(shared_brief: str, mission: str) -> str:
    return f"{shared_brief.strip()}\n\nWorker mission:\n{mission.strip()}"


__all__ = [
    "ACTIVE_TURN_STATES",
    "EDGE_ARGUMENT_FIELDS",
    "HubWorkerAdapterV2",
    "WorkerAdapterBrokerPort",
    "WorkerAdapterProjectionPort",
    "WorkerAdapterRuntimePort",
    "WorkerAdapterV2",
    "WorkerRoute",
]
