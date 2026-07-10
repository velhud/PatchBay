"""Dependency-injected Hub V2 workspace manager adapter.

This module deliberately contains no protocol or server wiring.  It translates
the stable manager tools into fleet/runtime lookups and brokered Edge actions,
while leaving local path authorization to the Edge ``WorkspaceContext``
preflight and action handlers.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import posixpath
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from patchbay.hub.operations import PUBLIC_STATUSES, normalize_domain_result, public_envelope
from patchbay.hub.tool_surface import (
    HUB_V2_ACTION_MAP,
    HUB_V2_WORKSPACE_CHANGES_ACTION_MAP,
)
from patchbay.protocol.context import RequestContext


WORKSPACE_TOOL_NAMES = frozenset(
    {
        "patchbay_workspace_list",
        "patchbay_workspace_open",
        "patchbay_workspace_tree",
        "patchbay_workspace_search",
        "patchbay_workspace_read_file",
        "patchbay_workspace_changes",
    }
)

_ROUTE_FIELDS = frozenset(
    {"work_group_id", "lane", "machine_id", "workspace_ref", "repo_path", "ungrouped_reason"}
)
_UNGROUPED_REASONS = frozenset({"tiny_check", "operator_requested", "legacy_compat"})
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")


class WorkspaceFleetPort(Protocol):
    """Fleet facts required by the adapter."""

    def list_machines(self) -> Mapping[str, Any] | list[Mapping[str, Any]]:
        ...


class WorkspaceRuntimePort(Protocol):
    """Work-group lookup required by grouped workspace routes."""

    def get_work_group(
        self, work_group_id: str, *, context: RequestContext | None = None
    ) -> Mapping[str, Any] | None:
        ...


class WorkspaceBrokerPort(Protocol):
    """Read-only Edge action execution required by this adapter."""

    async def execute(
        self,
        *,
        machine_id: str,
        edge_generation: str,
        action: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class _WorkspaceRoute:
    machine: dict[str, Any]
    workspace: dict[str, Any]
    repo_path: str
    edge_generation: str
    work_group: dict[str, Any] | None = None

    @property
    def machine_id(self) -> str:
        return str(self.machine.get("machine_id") or "")

    def broker_target(self) -> dict[str, Any]:
        target = {
            "machine_id": self.machine_id,
            "edge_generation": self.edge_generation,
            "workspace_ref": str(self.workspace.get("workspace_ref") or ""),
            "workspace_projection_ref": str(
                self.workspace.get("workspace_projection_ref")
                or self.workspace.get("projection_ref")
                or ""
            ),
            "repo_path": self.repo_path,
        }
        if self.work_group:
            target["work_group_id"] = str(self.work_group.get("work_group_id") or "")
        return target


class _RouteError(ValueError):
    def __init__(self, reason: str, message: str, *, status: str = "blocked"):
        super().__init__(message)
        self.reason = reason
        self.status = status


class WorkspaceAdapter:
    """Implement the six Hub V2 workspace manager tools through injected ports."""

    def __init__(
        self,
        fleet: WorkspaceFleetPort,
        runtime: WorkspaceRuntimePort,
        broker: WorkspaceBrokerPort,
        *,
        max_workspace_results: int = 100,
        max_discovery_depth: int = 8,
    ):
        self.fleet = fleet
        self.runtime = runtime
        self.broker = broker
        self.max_workspace_results = max(1, int(max_workspace_results))
        self.max_discovery_depth = max(0, int(max_discovery_depth))

    async def dispatch(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        handlers = {
            "patchbay_workspace_list": self.workspace_list,
            "patchbay_workspace_open": self.workspace_open,
            "patchbay_workspace_tree": self.workspace_tree,
            "patchbay_workspace_search": self.workspace_search,
            "patchbay_workspace_read_file": self.workspace_read_file,
            "patchbay_workspace_changes": self.workspace_changes,
        }
        handler = handlers.get(str(tool_name or ""))
        if handler is None:
            raise ValueError(f"Unsupported workspace tool: {tool_name}")
        return await handler(arguments or {}, context=context)

    handle = dispatch
    handle_tool_call = dispatch

    async def workspace_list(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        del context  # Fleet visibility is enforced by the injected fleet port.
        args = dict(arguments)
        query = str(args.get("query") or "").strip()
        max_results = _bounded_integer(
            args.get("max_results"),
            default=self.max_workspace_results,
            lower=1,
            upper=self.max_workspace_results,
            field="max_results",
        )
        max_depth = _bounded_integer(
            args.get("max_depth"),
            default=min(3, self.max_discovery_depth),
            lower=0,
            upper=self.max_discovery_depth,
            field="max_depth",
        )
        machine_ids = _string_set(args.get("machine_ids"))
        required_tags = _string_set(args.get("required_tags"))
        include_offline = bool(args.get("include_offline", False))

        machines = await self._machines()
        machines = [
            machine
            for machine in machines
            if self._machine_is_visible(
                machine,
                machine_ids=machine_ids,
                required_tags=required_tags,
                include_offline=include_offline,
            )
        ]

        warnings: list[str] = []
        status = "ok"
        discovery_truncated = False
        discovery_cursor = ""
        if bool(args.get("discover", False)):
            discover = getattr(self.fleet, "discover_workspaces", None)
            if discover is None:
                status = "partial"
                warnings.append("Fleet workspace discovery is unavailable; known projections were returned.")
            else:
                discovered = await _await_if_needed(
                    discover(
                        query=query,
                        machine_ids=sorted(machine_ids),
                        required_tags=sorted(required_tags),
                        include_offline=include_offline,
                        max_depth=max_depth,
                        max_results=max_results,
                    )
                )
                machines, discovery_truncated, discovery_cursor = self._merge_discovery(
                    machines, discovered
                )
                machines = [
                    machine
                    for machine in machines
                    if self._machine_is_visible(
                        machine,
                        machine_ids=machine_ids,
                        required_tags=required_tags,
                        include_offline=include_offline,
                    )
                ]

        logical = self._aggregate_workspaces(machines, query=query)
        truncated = discovery_truncated or len(logical) > max_results
        page = logical[:max_results]
        next_cursor = discovery_cursor
        if truncated and not next_cursor and page:
            next_cursor = str(page[-1].get("workspace_ref") or "")
        result = {
            "workspaces": page,
            "count": len(page),
            "truncated": truncated,
            "next_cursor": next_cursor,
            "query": query,
        }
        return public_envelope(status, result=result, warnings=warnings)

    async def workspace_open(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._routed_action(
            "patchbay_workspace_open",
            arguments,
            allowed_fields={"include_tree", "max_depth", "max_entries", "include_hidden"},
            context=context,
        )

    async def workspace_tree(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._routed_action(
            "patchbay_workspace_tree",
            arguments,
            allowed_fields={"path", "max_depth", "max_entries", "include_hidden"},
            context=context,
        )

    async def workspace_search(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        if not str(arguments.get("query") or ""):
            return self._route_error(
                _RouteError("invalid_search_query", "query is required")
            )
        return await self._routed_action(
            "patchbay_workspace_search",
            arguments,
            allowed_fields={
                "query",
                "path",
                "glob",
                "regex",
                "include_hidden",
                "max_results",
                "timeout_ms",
            },
            context=context,
        )

    async def workspace_read_file(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        if not str(arguments.get("file_path") or ""):
            return self._route_error(
                _RouteError("invalid_file_path", "file_path is required")
            )
        return await self._routed_action(
            "patchbay_workspace_read_file",
            arguments,
            allowed_fields={"file_path", "start_line", "end_line", "max_bytes"},
            context=context,
        )

    async def workspace_changes(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        view = str(arguments.get("view") or "")
        action = HUB_V2_WORKSPACE_CHANGES_ACTION_MAP.get(view)
        if action is None:
            return self._route_error(
                _RouteError("invalid_changes_view", "view must be status, summary, or diff")
            )
        fields = {
            "status": {"file_path", "porcelain"},
            "summary": {"file_path", "staged", "include_diff", "max_bytes"},
            "diff": {"file_path", "staged", "max_bytes"},
        }[view]
        edge_arguments = {
            key: deepcopy(arguments[key])
            for key in fields
            if key in arguments and arguments[key] is not None
        }
        if view == "summary" and "max_bytes" in edge_arguments:
            edge_arguments["max_diff_bytes"] = edge_arguments.pop("max_bytes")
        return await self._routed_action(
            "patchbay_workspace_changes",
            arguments,
            allowed_fields=set(),
            action_override=action,
            edge_arguments=edge_arguments,
            context=context,
        )

    async def _routed_action(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        allowed_fields: set[str],
        action_override: str = "",
        edge_arguments: Mapping[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        try:
            route = await self._resolve_route(arguments, context=context)
        except _RouteError as error:
            return self._route_error(error)

        preflight_arguments = {
            "repo_path": route.repo_path,
            "workspace_ref": str(route.workspace.get("workspace_ref") or ""),
            "workspace_projection_ref": str(
                route.workspace.get("workspace_projection_ref")
                or route.workspace.get("projection_ref")
                or ""
            ),
        }
        if route.work_group:
            preflight_arguments["work_group_id"] = str(
                route.work_group.get("work_group_id") or ""
            )
        preflight = self._canonical_envelope(
            await self._execute(
                route,
                action="patchbay_edge_preflight",
                arguments=preflight_arguments,
                context=context,
            )
        )
        if preflight["status"] != "ok":
            return self._attach_route(preflight, route, preflight=preflight)
        preflight_failure = self._preflight_failure(preflight)
        if preflight_failure:
            blocked = public_envelope(
                "blocked",
                result={
                    **deepcopy(dict(preflight.get("result") or {})),
                    "reason": preflight_failure,
                },
                operation=preflight.get("operation"),
                warnings=preflight.get("warnings"),
                next_actions=preflight.get("next_actions"),
            )
            return self._attach_route(blocked, route, preflight=preflight)

        preflight_result = preflight["result"]
        resolved_repo = str(
            preflight_result.get("repo_resolved")
            or preflight_result.get("resolved_repo_path")
            or route.repo_path
        )
        if edge_arguments is None:
            call_arguments = {
                key: deepcopy(arguments[key])
                for key in allowed_fields
                if key in arguments and arguments[key] is not None
            }
        else:
            call_arguments = deepcopy(dict(edge_arguments))
        call_arguments["repo"] = resolved_repo

        action = action_override or HUB_V2_ACTION_MAP[tool_name]
        envelope = self._canonical_envelope(
            await self._execute(
                route,
                action=action,
                arguments=call_arguments,
                context=context,
            )
        )
        if (
            action == "codex_search_repo"
            and envelope["status"] == "ok"
            and bool(envelope["result"].get("timed_out"))
        ):
            envelope["status"] = "partial"
            envelope["result"].setdefault("partial", True)
        return self._attach_route(envelope, route, preflight=preflight)

    async def _resolve_route(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None,
    ) -> _WorkspaceRoute:
        args = dict(arguments)
        machines = await self._machines()
        work_group_id = str(args.get("work_group_id") or "").strip()
        if work_group_id:
            group = await self._work_group(work_group_id, context=context)
            if not group:
                raise _RouteError(
                    "work_group_not_found",
                    "The requested work group does not exist or is not visible.",
                    status="not_found",
                )
            machine_id = str(
                group.get("pinned_machine_id") or group.get("machine_id") or ""
            )
            if not machine_id:
                raise _RouteError("group_pin_missing", "The work group has no pinned machine.")
            machine = _find_machine(machines, machine_id)
            if machine is None:
                raise _RouteError(
                    "pinned_machine_not_found",
                    "The work group's pinned machine is unavailable.",
                    status="not_found",
                )
            pinned_generation = str(
                group.get("pinned_edge_generation") or group.get("edge_generation") or ""
            )
            current_generation = _edge_generation(machine)
            if pinned_generation and current_generation and pinned_generation != current_generation:
                raise _RouteError(
                    "pinned_edge_generation_unavailable",
                    "The work group is pinned to a different immutable Edge generation.",
                )
            projection = self._group_projection(machine, group)
            repo_path = str(
                group.get("resolved_repo_path")
                or _projection_path(projection)
                or group.get("repo_path")
                or ""
            )
            if not repo_path:
                raise _RouteError(
                    "workspace_projection_missing",
                    "The work group's pinned workspace has no machine-local projection.",
                )
            if not projection:
                projection = {
                    "workspace_ref": str(group.get("workspace_ref") or ""),
                    "workspace_projection_ref": str(
                        group.get("workspace_projection_ref") or ""
                    ),
                    "repo_path": repo_path,
                }
            return _WorkspaceRoute(
                machine=deepcopy(machine),
                workspace=deepcopy(projection),
                repo_path=repo_path,
                edge_generation=pinned_generation or current_generation,
                work_group=deepcopy(group),
            )

        machine_id = str(args.get("machine_id") or "").strip()
        if not machine_id:
            raise _RouteError(
                "workspace_target_required",
                "Use work_group_id or an explicit machine/workspace target.",
            )
        reason = str(args.get("ungrouped_reason") or "")
        if reason not in _UNGROUPED_REASONS:
            raise _RouteError(
                "ungrouped_reason_required",
                "Exceptional explicit workspace routes require a valid ungrouped_reason.",
            )
        machine = _find_machine(machines, machine_id)
        if machine is None:
            raise _RouteError(
                "machine_not_found", "The requested machine does not exist.", status="not_found"
            )

        workspace_ref = str(args.get("workspace_ref") or "").strip()
        repo_path = str(args.get("repo_path") or "").strip()
        if not workspace_ref and not repo_path:
            raise _RouteError(
                "workspace_target_required", "workspace_ref or repo_path is required."
            )
        if repo_path:
            _validate_requested_path(repo_path)

        projections = _machine_projections(machine)
        if workspace_ref:
            projection = next(
                (
                    item
                    for item in projections
                    if str(item.get("workspace_ref") or "") == workspace_ref
                ),
                None,
            )
            if projection is None:
                raise _RouteError(
                    "workspace_projection_not_found",
                    "The logical workspace has no projection on the requested machine.",
                    status="not_found",
                )
            resolved = _projection_path(projection) or repo_path
        else:
            projection, resolved = _resolve_repo_projection(projections, repo_path)
            if projection is None:
                projection = {
                    "workspace_ref": "",
                    "requested_repo_path": repo_path,
                    "repo_path": repo_path,
                    "authorization": "edge_preflight_required",
                }
                resolved = repo_path
        if not resolved:
            raise _RouteError(
                "workspace_projection_missing", "The workspace projection has no local path."
            )
        return _WorkspaceRoute(
            machine=deepcopy(machine),
            workspace=deepcopy(projection),
            repo_path=resolved,
            edge_generation=_edge_generation(machine),
        )

    async def _machines(self) -> list[dict[str, Any]]:
        payload = await _await_if_needed(self.fleet.list_machines())
        if isinstance(payload, Mapping):
            raw = payload.get("machines") or []
        else:
            raw = payload or []
        return [deepcopy(dict(machine)) for machine in raw if isinstance(machine, Mapping)]

    async def _work_group(
        self,
        work_group_id: str,
        *,
        context: RequestContext | None,
    ) -> dict[str, Any] | None:
        getter = getattr(self.runtime, "get_work_group", None)
        if getter is not None:
            payload = await _await_if_needed(getter(work_group_id, context=context))
        else:
            status = getattr(self.runtime, "work_group_status", None)
            if status is None:
                raise _RouteError(
                    "runtime_group_lookup_unavailable",
                    "The runtime cannot resolve work-group pins.",
                )
            payload = await _await_if_needed(
                status(work_group_id=work_group_id, context=context)
            )
        if not isinstance(payload, Mapping):
            return None
        group = payload.get("work_group") if isinstance(payload.get("work_group"), Mapping) else payload
        return deepcopy(dict(group))

    async def _execute(
        self,
        route: _WorkspaceRoute,
        *,
        action: str,
        arguments: Mapping[str, Any],
        context: RequestContext | None,
    ) -> Mapping[str, Any]:
        execute = getattr(self.broker, "execute_edge_action", None) or getattr(
            self.broker, "execute", None
        )
        if execute is None:
            raise RuntimeError("Workspace broker port does not expose execute")
        return await _await_if_needed(
            execute(
                machine_id=route.machine_id,
                edge_generation=route.edge_generation,
                action=action,
                arguments=deepcopy(dict(arguments)),
                target=route.broker_target(),
                context=context,
            )
        )

    def _canonical_envelope(self, value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping) and value.get("status") in PUBLIC_STATUSES and "result" in value:
            envelope = deepcopy(dict(value))
            envelope["result"] = deepcopy(dict(envelope.get("result") or {}))
            envelope["operation"] = deepcopy(dict(envelope.get("operation") or {}))
            envelope["warnings"] = deepcopy(list(envelope.get("warnings") or []))
            envelope["next_actions"] = deepcopy(list(envelope.get("next_actions") or []))
            return envelope
        if value is None or not isinstance(value, Mapping):
            return public_envelope("failed", result={"reason": "empty_edge_result"})
        return normalize_domain_result(value)

    @staticmethod
    def _preflight_failure(envelope: Mapping[str, Any]) -> str:
        if envelope.get("status") != "ok":
            return ""
        result = envelope.get("result") if isinstance(envelope.get("result"), Mapping) else {}
        for field in ("ok", "accepted", "ready", "allowed"):
            if field in result and result[field] is False:
                return str(result.get("reason") or result.get("error") or "workspace_preflight_failed")
        if result.get("blockers"):
            return str(result.get("reason") or "workspace_preflight_blocked")
        if str(result.get("error") or "").strip():
            return str(result.get("reason") or result.get("error"))
        return ""

    @staticmethod
    def _attach_route(
        envelope: Mapping[str, Any],
        route: _WorkspaceRoute,
        *,
        preflight: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = deepcopy(dict(envelope))
        result = deepcopy(dict(normalized.get("result") or {}))
        machine = _public_machine(route.machine)
        workspace = deepcopy(route.workspace)
        workspace.setdefault("workspace_ref", str(route.workspace.get("workspace_ref") or ""))
        workspace["repo_path"] = str(
            preflight.get("result", {}).get("repo_resolved") or route.repo_path
        )
        workspace["preflight"] = deepcopy(dict(preflight.get("result") or {}))
        result.setdefault("machine", machine)
        result.setdefault("workspace", workspace)
        result.setdefault("edge_generation", route.edge_generation)
        if route.work_group:
            result.setdefault("work_group", deepcopy(route.work_group))
        normalized["result"] = result
        normalized.setdefault("operation", {})
        normalized.setdefault("warnings", [])
        normalized.setdefault("next_actions", [])
        return normalized

    @staticmethod
    def _route_error(error: _RouteError) -> dict[str, Any]:
        return public_envelope(
            error.status,
            result={"reason": error.reason, "message": str(error)},
        )

    @staticmethod
    def _machine_is_visible(
        machine: Mapping[str, Any],
        *,
        machine_ids: set[str],
        required_tags: set[str],
        include_offline: bool,
    ) -> bool:
        machine_id = str(machine.get("machine_id") or "")
        if machine_ids and machine_id not in machine_ids:
            return False
        status = str(machine.get("status") or "online").lower()
        if status == "retired" or bool(machine.get("retired")):
            return False
        if status == "offline" and not include_offline:
            return False
        tags = {str(tag) for tag in machine.get("tags") or []}
        return not required_tags or required_tags.issubset(tags)

    def _aggregate_workspaces(
        self, machines: list[dict[str, Any]], *, query: str
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        query_text = query.casefold()
        for machine in machines:
            for raw_projection in _machine_projections(machine):
                projection = _public_projection(machine, raw_projection)
                workspace_ref = str(projection.get("workspace_ref") or "")
                if not workspace_ref:
                    workspace_ref = _derived_workspace_ref(projection)
                    projection["workspace_ref"] = workspace_ref
                aliases = _projection_aliases(projection)
                display_name = str(
                    projection.get("display_name")
                    or projection.get("repo_name")
                    or projection.get("name")
                    or next(iter(sorted(aliases, key=str.casefold)), "")
                    or posixpath.basename(_projection_path(projection).rstrip("/"))
                    or workspace_ref
                )
                searchable = " ".join(
                    [
                        workspace_ref,
                        display_name,
                        *sorted(aliases),
                        _projection_path(projection),
                        _repository_identity_text(projection),
                    ]
                ).casefold()
                if query_text and query_text not in searchable:
                    continue
                record = grouped.setdefault(
                    workspace_ref,
                    {
                        "workspace_ref": workspace_ref,
                        "display_name": display_name,
                        "aliases": set(),
                        "repository_identity": deepcopy(
                            projection.get("repository_identity")
                            or projection.get("repository")
                            or {}
                        ),
                        "projections": [],
                    },
                )
                record["aliases"].update(aliases)
                record["projections"].append(projection)

        result: list[dict[str, Any]] = []
        for record in grouped.values():
            projections = record["projections"]
            projections.sort(
                key=lambda item: (
                    item.get("machine_status") != "online",
                    str(item.get("machine_id") or ""),
                    str(item.get("workspace_projection_ref") or ""),
                )
            )
            readinesses = [str(item.get("readiness") or "") for item in projections]
            if "ready" in readinesses:
                readiness = "ready"
            elif "stale" in readinesses:
                readiness = "stale"
            elif readinesses and all(item == "offline" for item in readinesses):
                readiness = "offline"
            else:
                readiness = "requires_preflight"
            machine_ids = sorted({str(item.get("machine_id") or "") for item in projections})
            online_ids = sorted(
                {
                    str(item.get("machine_id") or "")
                    for item in projections
                    if item.get("machine_status") == "online"
                }
            )
            record["aliases"] = sorted(record["aliases"], key=str.casefold)
            record["readiness"] = readiness
            record["machine_availability"] = {
                "machine_ids": machine_ids,
                "online_machine_ids": online_ids,
                "total": len(machine_ids),
                "online": len(online_ids),
            }
            result.append(record)
        result.sort(
            key=lambda item: (
                str(item.get("display_name") or "").casefold(),
                str(item.get("workspace_ref") or ""),
            )
        )
        return result

    @staticmethod
    def _group_projection(
        machine: Mapping[str, Any], group: Mapping[str, Any]
    ) -> dict[str, Any]:
        projections = _machine_projections(machine)
        projection_ref = str(group.get("workspace_projection_ref") or "")
        if projection_ref:
            for projection in projections:
                if str(
                    projection.get("workspace_projection_ref")
                    or projection.get("projection_ref")
                    or ""
                ) == projection_ref:
                    return projection
        workspace_ref = str(group.get("workspace_ref") or "")
        if workspace_ref:
            for projection in projections:
                if str(projection.get("workspace_ref") or "") == workspace_ref:
                    return projection
        repo_path = str(group.get("resolved_repo_path") or group.get("repo_path") or "")
        if repo_path:
            projection, _resolved = _resolve_repo_projection(projections, repo_path)
            if projection:
                return projection
        return {}

    @staticmethod
    def _merge_discovery(
        machines: list[dict[str, Any]], discovered: Any
    ) -> tuple[list[dict[str, Any]], bool, str]:
        merged = deepcopy(machines)
        if not isinstance(discovered, Mapping):
            return merged, False, ""
        truncated = bool(discovered.get("truncated"))
        next_cursor = str(discovered.get("next_cursor") or "")
        discovered_machines = discovered.get("machines")
        if isinstance(discovered_machines, list):
            by_id = {str(machine.get("machine_id") or ""): machine for machine in merged}
            for raw_machine in discovered_machines:
                if not isinstance(raw_machine, Mapping):
                    continue
                machine = deepcopy(dict(raw_machine))
                machine_id = str(machine.get("machine_id") or "")
                if machine_id in by_id:
                    known = by_id[machine_id]
                    _append_machine_projections(known, _machine_projections(machine))
                else:
                    merged.append(machine)
            return merged, truncated, next_cursor

        projections = discovered.get("workspaces") or discovered.get("projections") or []
        if isinstance(projections, list):
            by_id = {str(machine.get("machine_id") or ""): machine for machine in merged}
            for raw_projection in projections:
                if not isinstance(raw_projection, Mapping):
                    continue
                projection = deepcopy(dict(raw_projection))
                machine = by_id.get(str(projection.get("machine_id") or ""))
                if machine is not None:
                    _append_machine_projections(machine, [projection])
        return merged, truncated, next_cursor

    # Convenient names for direct adapter use without exposing separate tools.
    list_workspaces = workspace_list
    open = workspace_open
    tree = workspace_tree
    search = workspace_search
    read_file = workspace_read_file
    changes = workspace_changes
    patchbay_workspace_list = workspace_list
    patchbay_workspace_open = workspace_open
    patchbay_workspace_tree = workspace_tree
    patchbay_workspace_search = workspace_search
    patchbay_workspace_read_file = workspace_read_file
    patchbay_workspace_changes = workspace_changes


HubWorkspaceAdapterV2 = WorkspaceAdapter
WorkspaceAdapterV2 = WorkspaceAdapter
HubWorkspaceAdapter = WorkspaceAdapter
WorkspaceAdapterFleetPort = WorkspaceFleetPort
WorkspaceAdapterRuntimePort = WorkspaceRuntimePort
WorkspaceAdapterBrokerPort = WorkspaceBrokerPort


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _bounded_integer(
    value: Any,
    *,
    default: int,
    lower: int,
    upper: int,
    field: str,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an integer") from error
    return max(lower, min(parsed, upper))


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("Expected a string array")
    return {str(item) for item in value if str(item)}


def _find_machine(
    machines: list[dict[str, Any]], machine_id: str
) -> dict[str, Any] | None:
    return next(
        (machine for machine in machines if str(machine.get("machine_id") or "") == machine_id),
        None,
    )


def _edge_generation(machine: Mapping[str, Any]) -> str:
    return str(
        machine.get("edge_generation")
        or machine.get("generation")
        or (machine.get("identity") or {}).get("edge_generation")
        or ""
    )


def _machine_projections(machine: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("workspace_projections", "workspaces", "projections"):
        value = machine.get(key)
        if isinstance(value, list):
            return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]
    return []


def _append_machine_projections(
    machine: dict[str, Any], projections: list[dict[str, Any]]
) -> None:
    for key in ("workspace_projections", "workspaces", "projections"):
        if isinstance(machine.get(key), list):
            machine[key].extend(deepcopy(projections))
            return
    machine["workspace_projections"] = deepcopy(projections)


def _projection_path(projection: Mapping[str, Any]) -> str:
    return str(
        projection.get("resolved_repo_path")
        or projection.get("local_path")
        or projection.get("repo_path")
        or projection.get("path")
        or projection.get("root")
        or ""
    ).strip()


def _projection_aliases(projection: Mapping[str, Any]) -> set[str]:
    aliases: set[str] = set()
    raw_aliases = projection.get("aliases")
    if isinstance(raw_aliases, str):
        aliases.add(raw_aliases)
    elif isinstance(raw_aliases, Mapping):
        aliases.update(str(item) for item in raw_aliases.keys() if str(item))
    elif isinstance(raw_aliases, (list, tuple, set, frozenset)):
        aliases.update(str(item) for item in raw_aliases if str(item))
    for key in ("alias", "repo_name", "name", "display_name"):
        value = str(projection.get(key) or "").strip()
        if value:
            aliases.add(value)
    workspace_alias = projection.get("workspace_alias")
    if isinstance(workspace_alias, Mapping):
        for key in ("requested", "canonical"):
            value = str(workspace_alias.get(key) or "").strip()
            if value:
                aliases.add(value)
    path = _projection_path(projection)
    if path:
        aliases.add(posixpath.basename(_normalize_remote_path(path).rstrip("/")))
    return {alias for alias in aliases if alias}


def _repository_identity_text(projection: Mapping[str, Any]) -> str:
    for key in ("repository_identity", "repository", "repo_identity"):
        value = projection.get(key)
        if isinstance(value, Mapping):
            return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
        if value:
            return str(value)
    git = projection.get("git")
    if isinstance(git, Mapping):
        for key in ("remote_url", "remote", "repository"):
            if git.get(key):
                return str(git[key])
    for key in ("remote_url", "repository_url"):
        if projection.get(key):
            return str(projection[key])
    return ""


def _derived_workspace_ref(projection: Mapping[str, Any]) -> str:
    identity = (
        _repository_identity_text(projection)
        or next(iter(sorted(_projection_aliases(projection), key=str.casefold)), "")
        or _projection_path(projection)
        or "unknown-workspace"
    )
    digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
    return f"workspace_{digest}"


def _public_machine(machine: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(machine[key])
        for key in (
            "machine_id",
            "display_name",
            "status",
            "tags",
            "role",
            "edge_generation",
            "last_seen_at",
        )
        if key in machine
    }


def _projection_readiness(
    machine: Mapping[str, Any], projection: Mapping[str, Any]
) -> str:
    machine_status = str(machine.get("status") or "online").lower()
    if machine_status == "offline":
        return "offline"
    status = str(projection.get("readiness") or projection.get("status") or "").lower()
    if status in {"ready", "stale", "offline", "requires_preflight"}:
        return status
    if bool(projection.get("stale")):
        return "stale"
    if projection.get("ready") is True:
        return "ready"
    preflight = projection.get("preflight")
    if isinstance(preflight, Mapping):
        preflight_status = str(preflight.get("status") or "").lower()
        if preflight_status in {"ok", "ready", "succeeded"} or preflight.get("ok") is True:
            return "ready"
        if preflight_status == "stale":
            return "stale"
    return "requires_preflight"


def _public_projection(
    machine: Mapping[str, Any], projection: Mapping[str, Any]
) -> dict[str, Any]:
    result = deepcopy(dict(projection))
    result["machine_id"] = str(machine.get("machine_id") or "")
    result["edge_generation"] = _edge_generation(machine)
    result["machine_status"] = str(machine.get("status") or "online")
    result["repo_path"] = _projection_path(projection)
    result["readiness"] = _projection_readiness(machine, projection)
    if "workspace_projection_ref" not in result and result.get("projection_ref"):
        result["workspace_projection_ref"] = result["projection_ref"]
    return result


def _normalize_remote_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/")
    prefix = ""
    if _WINDOWS_ABSOLUTE_RE.match(normalized):
        prefix, normalized = normalized[:2], normalized[2:]
    normalized = posixpath.normpath(normalized)
    return prefix + normalized


def _is_absolute_remote_path(value: str) -> bool:
    text = str(value or "")
    return text.startswith("/") or bool(_WINDOWS_ABSOLUTE_RE.match(text))


def _validate_requested_path(value: str) -> None:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise _RouteError("invalid_repo_path", "repo_path is invalid")
    normalized = text.replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise _RouteError("invalid_repo_path", "repo_path cannot contain parent traversal")


def _resolve_repo_projection(
    projections: list[dict[str, Any]], requested: str
) -> tuple[dict[str, Any] | None, str]:
    if not requested:
        return None, ""
    normalized_request = _normalize_remote_path(requested)
    folded_request = requested.casefold()
    ranked: list[tuple[tuple[int, int, int], dict[str, Any], str]] = []
    for projection in projections:
        path = _projection_path(projection)
        normalized_path = _normalize_remote_path(path) if path else ""
        aliases = {alias.casefold() for alias in _projection_aliases(projection)}
        git_specific = bool(projection.get("git")) or bool(_repository_identity_text(projection))
        specificity = len(normalized_path)
        if normalized_path and normalized_request == normalized_path:
            ranked.append(((0, 0, -specificity), projection, path))
            continue
        if folded_request in aliases:
            ranked.append(
                ((1, 0 if git_specific else 1, -specificity), projection, path or requested)
            )
            continue
        if (
            _is_absolute_remote_path(requested)
            and normalized_path
            and normalized_request.startswith(normalized_path.rstrip("/") + "/")
        ):
            ranked.append(((2, 0, -specificity), projection, requested))
            continue
        if (
            not _is_absolute_remote_path(requested)
            and normalized_path
            and not git_specific
        ):
            resolved = posixpath.normpath(posixpath.join(normalized_path, normalized_request))
            if resolved.startswith(normalized_path.rstrip("/") + "/"):
                candidate = deepcopy(projection)
                candidate["requested_repo_path"] = requested
                candidate["resolved_repo_path"] = resolved
                candidate["match_kind"] = "relative_repo_under_workspace"
                ranked.append(((3, 0, -specificity), candidate, resolved))
    if not ranked:
        return None, requested
    ranked.sort(key=lambda item: item[0])
    projection = deepcopy(ranked[0][1])
    resolved = ranked[0][2]
    projection.setdefault("requested_repo_path", requested)
    projection["resolved_repo_path"] = resolved
    return projection, resolved


__all__ = [
    "HubWorkspaceAdapter",
    "HubWorkspaceAdapterV2",
    "WorkspaceAdapter",
    "WorkspaceAdapterBrokerPort",
    "WorkspaceAdapterFleetPort",
    "WorkspaceAdapterRuntimePort",
    "WorkspaceAdapterV2",
    "WorkspaceBrokerPort",
    "WorkspaceFleetPort",
    "WorkspaceRuntimePort",
    "WORKSPACE_TOOL_NAMES",
]
