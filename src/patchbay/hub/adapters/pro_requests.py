"""Hub V2 adapter for the canonical Pro Request subsystem.

The adapter owns Hub coordination metadata and operation correlation only.  The
canonical :class:`patchbay.pro_requests.store.ProRequestStore` remains the
authority for request/report/response state, and an injected dispatcher remains
the only boundary which may message or start a worker.
"""
from __future__ import annotations

import inspect
import json
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from patchbay.hub.identity import ManagerIdentity, stable_ref
from patchbay.hub.operations import PUBLIC_STATUSES, public_envelope
from patchbay.hub.store_v2 import (
    HubStoreV2,
    HubStoreV2Conflict,
    semantic_payload_hash,
)
from patchbay.protocol.context import RequestContext


PRO_REQUEST_ASSOCIATION_ENTITY = "hub.pro_request_association"
PRO_REQUEST_TOOL_PREFIX = "patchbay_pro_request_"
CANONICAL_TOOL_PREFIX = "codex_pro_request_"
MUTATING_ACTIONS = frozenset({"claim", "respond", "dispatch", "close"})
TERMINAL_OPERATION_STATES = frozenset({"succeeded", "blocked", "failed", "cancelled"})
DEFAULT_CLAIM_LEASE_SECONDS = 15 * 60.0

_HUB_ARGUMENTS = frozenset(
    {
        "idempotency_key",
        "expected_revision",
        "work_group_id",
        "lane",
        "machine_id",
        "workspace_ref",
    }
)


@runtime_checkable
class ProRequestCanonicalStore(Protocol):
    """Canonical storage calls consumed by the adapter."""

    def list_requests(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def read_request(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def claim_request(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def respond_request(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def close_request(self, **kwargs: Any) -> Mapping[str, Any]: ...


DispatchExecutor = Callable[..., Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class ProRequestRoute:
    """Immutable Edge affinity used to qualify one Pro Request projection."""

    machine_id: str
    edge_generation: str
    workspace_ref: str
    work_group_id: str = ""
    lane: str = ""
    visibility: str = "private"
    origin_operation_id: str = ""

    def __post_init__(self) -> None:
        for field in ("machine_id", "edge_generation", "workspace_ref"):
            if not str(getattr(self, field) or "").strip():
                raise ValueError(f"{field} is required")
        if self.visibility not in {"private", "shared"}:
            raise ValueError("visibility must be private or shared")


class HubProRequestAdapterV2:
    """Machine-affine, injected adapter for all six Hub V2 Pro Request actions.

    The production composition injects the canonical store and dispatch
    boundary; tests may inject alternatives.
    """

    def __init__(
        self,
        hub_store: HubStoreV2,
        pro_request_store: ProRequestCanonicalStore,
        *,
        machine_id: str,
        edge_generation: str,
        workspace_ref: str,
        work_group_id: str = "",
        lane: str = "",
        visibility: str = "private",
        origin_operation_id: str = "",
        dispatch_executor: DispatchExecutor | Any | None = None,
        dispatch: DispatchExecutor | Any | None = None,
        reference_salt: str = "",
        claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
        clock: Callable[[], float] | None = None,
    ):
        if dispatch_executor is not None and dispatch is not None:
            raise ValueError("Pass only one of dispatch_executor or dispatch")
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")
        self.hub_store = hub_store
        self.pro_request_store = pro_request_store
        self.route = ProRequestRoute(
            machine_id=str(machine_id).strip(),
            edge_generation=str(edge_generation).strip(),
            workspace_ref=str(workspace_ref).strip(),
            work_group_id=str(work_group_id or "").strip(),
            lane=str(lane or "").strip(),
            visibility=visibility,
            origin_operation_id=str(origin_operation_id or "").strip(),
        )
        self.dispatch_executor = dispatch_executor if dispatch_executor is not None else dispatch
        self.reference_salt = reference_salt or hub_store.principal_ref
        self.claim_lease_seconds = float(claim_lease_seconds)
        self._clock = clock or time.time

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        """Handle one Pro Request tool call for injection into ``HubProtocolV2``."""

        if not name.startswith(PRO_REQUEST_TOOL_PREFIX):
            raise ValueError(f"Unsupported Hub V2 Pro Request tool: {name}")
        action = name.removeprefix(PRO_REQUEST_TOOL_PREFIX)
        handlers = {
            "list": self.list_requests,
            "read": self.read_request,
            "claim": self.claim_request,
            "respond": self.respond_request,
            "dispatch": self.dispatch_request,
            "close": self.close_request,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError(f"Unsupported Hub V2 Pro Request tool: {name}")
        return await handler(arguments, context=context)

    async def list_requests(
        self,
        arguments: Mapping[str, Any] | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs)
        route_error = self._route_error(args)
        if route_error:
            return route_error
        route = self._route_for(args, context)
        identity = self._identity(context)
        canonical = self.pro_request_store.list_requests(
            repo_path=args.get("repo_path"),
            statuses=args.get("status") or [],
            include_closed=bool(args.get("include_closed", False)),
            limit=int(args.get("limit") or 10),
            request_context=context,
        )
        result = deepcopy(dict(canonical))
        visible: list[dict[str, Any]] = []
        hidden = 0
        for item in canonical.get("requests") or []:
            if not isinstance(item, Mapping):
                continue
            association = self._ensure_association(item, route, identity)
            if not self._is_visible(association, route, identity):
                hidden += 1
                continue
            visible.append(self._decorate_request(item, association))
        result["requests"] = visible
        result["count"] = len(visible)
        result["total_known"] = len(visible)
        result["hidden_count"] = hidden
        self._add_route_result(result, route)
        return public_envelope("ok", result=result)

    async def read_request(
        self,
        arguments: Mapping[str, Any] | str | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs, scalar_field="request_id")
        route_error = self._route_error(args)
        if route_error:
            return route_error
        resolved = self._resolve_request(args, context)
        if resolved is None:
            return self._not_found(str(args.get("request_id") or ""))
        raw_id, association, canonical = resolved
        result = deepcopy(dict(canonical))
        result["request"] = self._decorate_request(canonical.get("request") or {}, association)
        result["request_ref"] = association["request_ref"]
        self._add_route_result(result, self._route_for(args, context), association=association)
        return public_envelope("ok", result=result)

    async def claim_request(
        self,
        arguments: Mapping[str, Any] | str | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs, scalar_field="request_id")
        resolved = self._resolve_visible_mutation(args, context)
        if isinstance(resolved, dict):
            return resolved
        raw_id, association, _canonical, route, identity = resolved
        started = self._begin_operation("claim", args, association, route, identity)
        if isinstance(started, dict) and "status" in started:
            return started
        operation = started

        expected = args.get("expected_revision")
        if expected is None:
            domain = {
                "accepted": False,
                "reason": "expected_revision_required",
                "request_ref": association["request_ref"],
                "current_revision": association.get("canonical_revision"),
            }
            association = self._record_operation(association["request_ref"], "claim", operation["operation_id"])
            domain["request"] = self._decorate_request({}, association)
            return self._finish_operation(operation, "blocked", domain)

        try:
            with self.hub_store.immediate_transaction() as connection:
                association, entity_revision = self._association_in_transaction(
                    connection, association["request_ref"]
                )
                canonical = self._canonical_read(raw_id, context)
                request = canonical.get("request") or {}
                current_revision = int(request.get("revision") or 0)
                refusal = self._revision_refusal(expected, current_revision, association)
                if refusal is None:
                    refusal = self._claim_refusal(association, identity, args)
                if refusal is not None:
                    domain = refusal
                else:
                    domain = dict(
                        self.pro_request_store.claim_request(
                            request_id=raw_id,
                            note=str(args.get("note") or ""),
                            request_context=context,
                            takeover=bool(args.get("takeover", False)),
                        )
                    )
                    request = domain.get("request") or request
                association = self._updated_association(
                    association,
                    request,
                    action="claim",
                    operation_id=operation["operation_id"],
                )
                if domain.get("accepted") is True:
                    association["claim"] = {
                        "participant_ref": identity.participant_ref,
                        "claimed_revision": int((domain.get("request") or {}).get("revision") or current_revision),
                        "lease_expires_at": self._clock() + self.claim_lease_seconds,
                        "operation_id": operation["operation_id"],
                        "takeover": bool(args.get("takeover", False)),
                        "takeover_reason": str(args.get("takeover_reason") or ""),
                    }
                self.hub_store._put_entity_in_transaction(
                    connection,
                    PRO_REQUEST_ASSOCIATION_ENTITY,
                    association["request_ref"],
                    association,
                    expected_revision=entity_revision,
                    legacy_classification=None,
                )
        except Exception as error:
            return self._finish_operation(
                operation,
                "failed",
                {"failed": True, "reason": "claim_storage_error", "message": str(error)},
            )

        domain = self._decorate_domain(domain, association)
        return self._finish_operation(operation, self._domain_status(domain), domain)

    async def respond_request(
        self,
        arguments: Mapping[str, Any] | str | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs, scalar_field="request_id")
        return self._storage_mutation("respond", args, context)

    async def close_request(
        self,
        arguments: Mapping[str, Any] | str | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs, scalar_field="request_id")
        return self._storage_mutation("close", args, context)

    async def dispatch_request(
        self,
        arguments: Mapping[str, Any] | str | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs, scalar_field="request_id")
        resolved = self._resolve_visible_mutation(args, context)
        if isinstance(resolved, dict):
            return resolved
        raw_id, association, canonical, route, identity = resolved
        target = str(args.get("target") or "origin_worker")
        if target not in {"origin_worker", "new_worker"}:
            return public_envelope(
                "blocked",
                result={"accepted": False, "reason": "invalid_dispatch_target", "target": target},
            )
        started = self._begin_operation("dispatch", args, association, route, identity)
        if isinstance(started, dict) and "status" in started:
            return started
        parent = started

        refusal = self._mutation_refusal(args, association, canonical, identity)
        if refusal is not None:
            association = self._record_operation(association["request_ref"], "dispatch", parent["operation_id"])
            return self._finish_operation(parent, "blocked", self._decorate_domain(refusal, association))
        if self.dispatch_executor is None:
            association = self._record_operation(association["request_ref"], "dispatch", parent["operation_id"])
            domain = {
                "accepted": False,
                "dispatched": False,
                "reason": "dispatch_executor_unavailable",
                "note": "No injected canonical dispatch executor is available.",
            }
            return self._finish_operation(parent, "blocked", self._decorate_domain(domain, association))

        child = self._create_dispatch_child(parent, args, association, target)
        canonical_args = self._canonical_arguments(args)
        canonical_args["request_id"] = raw_id
        try:
            dispatched = await self._invoke_dispatch(
                canonical_args,
                context=context,
                operation_id=child["operation_id"],
                route=route,
            )
        except Exception as error:
            association = self._record_operations(
                association["request_ref"],
                {"dispatch": parent["operation_id"], "dispatch_target": child["operation_id"]},
            )
            child = self._mark_unknown(child, error)
            parent = self._mark_unknown(parent, error)
            return public_envelope(
                "pending",
                result={
                    "accepted": True,
                    "dispatched": False,
                    "reason": "dispatch_outcome_unknown",
                    "request_ref": association["request_ref"],
                    "dispatch_operation": self._public_operation(child),
                    "applied": False,
                    "committed": False,
                },
                operation=self._public_operation(parent),
                warnings=[{"code": "dispatch_outcome_unknown", "message": str(error)}],
                next_actions=[
                    {
                        "tool": "patchbay_operation_status",
                        "arguments": {"operation_id": parent["operation_id"]},
                    }
                ],
            )

        domain = dict(dispatched)
        domain.setdefault("dispatched", bool(domain.get("accepted")))
        domain.setdefault("applied", False)
        domain.setdefault("committed", False)
        domain.setdefault("hidden_queueing", False)
        child_status = self._domain_status(domain)
        child_envelope = self._finish_operation(child, child_status, domain)
        child_public = child_envelope["operation"]
        domain["dispatch_operation"] = child_public
        domain["dispatch_target"] = target

        post_request = (domain.get("request") or canonical.get("request") or {})
        association = self._refresh_and_record_operations(
            association,
            post_request,
            {"dispatch": parent["operation_id"], "dispatch_target": child["operation_id"]},
        )
        domain = self._decorate_domain(domain, association)
        item_results = [
            {
                "item_id": "dispatch_target",
                "target": target,
                "status": child_status,
                "operation_id": child["operation_id"],
            }
        ]
        return self._finish_operation(parent, child_status, domain, item_results=item_results)

    async def list(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.list_requests(*args, **kwargs)

    async def read(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.read_request(*args, **kwargs)

    async def claim(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.claim_request(*args, **kwargs)

    async def respond(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.respond_request(*args, **kwargs)

    async def dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.dispatch_request(*args, **kwargs)

    async def close(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.close_request(*args, **kwargs)

    def _storage_mutation(
        self,
        action: str,
        args: Mapping[str, Any],
        context: RequestContext | None,
    ) -> dict[str, Any]:
        resolved = self._resolve_visible_mutation(args, context)
        if isinstance(resolved, dict):
            return resolved
        raw_id, association, _canonical, route, identity = resolved
        started = self._begin_operation(action, args, association, route, identity)
        if isinstance(started, dict) and "status" in started:
            return started
        operation = started

        try:
            with self.hub_store.immediate_transaction() as connection:
                association, entity_revision = self._association_in_transaction(
                    connection, association["request_ref"]
                )
                canonical = self._canonical_read(raw_id, context)
                refusal = self._mutation_refusal(args, association, canonical, identity)
                if refusal is not None:
                    domain = refusal
                    request = canonical.get("request") or {}
                elif action == "respond":
                    domain = dict(
                        self.pro_request_store.respond_request(
                            request_id=raw_id,
                            response_kind=str(args.get("response_kind") or "analysis"),
                            response_markdown=str(args.get("response_markdown") or ""),
                            recommended_next_action=str(args.get("recommended_next_action") or ""),
                            worker_message_markdown=str(args.get("worker_message_markdown") or ""),
                            request_context=context,
                            takeover=bool(args.get("takeover", False)),
                        )
                    )
                    domain.setdefault("response_stored", bool(domain.get("accepted")))
                    domain["dispatched"] = False
                    domain.setdefault("applied", False)
                    domain.setdefault("committed", False)
                    request = domain.get("request") or canonical.get("request") or {}
                else:
                    domain = dict(
                        self.pro_request_store.close_request(
                            request_id=raw_id,
                            reason=str(args.get("reason") or ""),
                            status=str(args.get("status") or "closed"),
                            request_context=context,
                            takeover=bool(args.get("takeover", False)),
                        )
                    )
                    domain.setdefault("dispatched", False)
                    domain.setdefault("applied", False)
                    domain.setdefault("committed", False)
                    request = domain.get("request") or canonical.get("request") or {}

                association = self._updated_association(
                    association,
                    request,
                    action=action,
                    operation_id=operation["operation_id"],
                )
                if bool(args.get("takeover")) and domain.get("accepted") is True:
                    claim = dict(association.get("claim") or {})
                    if claim:
                        claim["participant_ref"] = identity.participant_ref
                        claim["takeover"] = True
                        claim["takeover_reason"] = str(args.get("takeover_reason") or "")
                        association["claim"] = claim
                self.hub_store._put_entity_in_transaction(
                    connection,
                    PRO_REQUEST_ASSOCIATION_ENTITY,
                    association["request_ref"],
                    association,
                    expected_revision=entity_revision,
                    legacy_classification=None,
                )
        except Exception as error:
            return self._finish_operation(
                operation,
                "failed",
                {"failed": True, "reason": f"{action}_storage_error", "message": str(error)},
            )

        domain = self._decorate_domain(domain, association)
        return self._finish_operation(operation, self._domain_status(domain), domain)

    def _resolve_visible_mutation(
        self,
        args: Mapping[str, Any],
        context: RequestContext | None,
    ) -> tuple[str, dict[str, Any], Mapping[str, Any], ProRequestRoute, ManagerIdentity] | dict[str, Any]:
        route_error = self._route_error(args)
        if route_error:
            return route_error
        resolved = self._resolve_request(args, context)
        if resolved is None:
            return self._not_found(str(args.get("request_id") or ""))
        raw_id, association, canonical = resolved
        route = self._route_for(args, context)
        identity = self._identity(context)
        return raw_id, association, canonical, route, identity

    def _resolve_request(
        self,
        args: Mapping[str, Any],
        context: RequestContext | None,
    ) -> tuple[str, dict[str, Any], Mapping[str, Any]] | None:
        requested = str(args.get("request_id") or "").strip()
        if not requested:
            return None
        route = self._route_for(args, context)
        identity = self._identity(context)
        association_entity = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, requested)
        if association_entity is None:
            candidate_ref = self._request_ref(requested)
            association_entity = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, candidate_ref)
        if association_entity is not None:
            association = deepcopy(association_entity["record"])
            if not self._is_visible(association, route, identity):
                return None
            raw_id = str(association["request_id"])
        else:
            raw_id = requested
        try:
            canonical = self._canonical_read(raw_id, context, args=args)
        except ValueError as error:
            if "not found" in str(error).lower():
                return None
            raise
        association = self._ensure_association(canonical.get("request") or {}, route, identity)
        if not self._is_visible(association, route, identity):
            return None
        association = self._refresh_association(association, canonical.get("request") or {})
        return raw_id, association, canonical

    def _canonical_read(
        self,
        request_id: str,
        context: RequestContext | None,
        *,
        args: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        values = args or {}
        return self.pro_request_store.read_request(
            request_id=request_id,
            include_report=values.get("include_report", True) is not False,
            include_response=values.get("include_response", True) is not False,
            include_events=bool(values.get("include_events", False)),
            max_report_bytes=values.get("max_report_bytes"),
            max_response_bytes=values.get("max_response_bytes"),
            request_context=context,
        )

    def _ensure_association(
        self,
        request: Mapping[str, Any],
        route: ProRequestRoute,
        identity: ManagerIdentity,
    ) -> dict[str, Any]:
        request_id = str(request.get("id") or "").strip()
        if not request_id:
            raise ValueError("Canonical Pro Request result omitted id")
        request_ref = self._request_ref(request_id)
        current = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
        if current is not None:
            return deepcopy(current["record"])
        operation_ids = [route.origin_operation_id] if route.origin_operation_id else []
        association = {
            "request_ref": request_ref,
            "request_id": request_id,
            "principal_ref": identity.principal_ref,
            "owner_participant_ref": identity.participant_ref,
            "machine_id": route.machine_id,
            "edge_generation": route.edge_generation,
            "workspace_ref": route.workspace_ref,
            "work_group_id": route.work_group_id,
            "lane": route.lane,
            "visibility": route.visibility,
            "origin_operation_id": route.origin_operation_id,
            "operation_ids": operation_ids,
            "action_operation_ids": {},
            "canonical_revision": int(request.get("revision") or 0),
            "status": str(request.get("status") or ""),
            "claim": {},
        }
        try:
            saved = self.hub_store.put_entity(
                PRO_REQUEST_ASSOCIATION_ENTITY,
                request_ref,
                association,
                expected_revision=0,
            )
            return deepcopy(saved["record"])
        except HubStoreV2Conflict:
            saved = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
            if saved is None:
                raise
            return deepcopy(saved["record"])

    def _refresh_association(
        self,
        association: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        current_revision = int(association.get("canonical_revision") or 0)
        request_revision = int(request.get("revision") or 0)
        request_status = str(request.get("status") or association.get("status") or "")
        if request_revision == current_revision and request_status == association.get("status"):
            return deepcopy(dict(association))

        def update(record: dict[str, Any]) -> None:
            record["canonical_revision"] = request_revision
            record["status"] = request_status

        saved = self.hub_store.update_entity(
            PRO_REQUEST_ASSOCIATION_ENTITY,
            str(association["request_ref"]),
            update,
        )
        return deepcopy(saved["record"])

    def _association_in_transaction(
        self,
        connection: Any,
        request_ref: str,
    ) -> tuple[dict[str, Any], int]:
        row = connection.execute(
            "SELECT revision, record_json FROM entity_records WHERE entity_type = ? AND entity_id = ?",
            (PRO_REQUEST_ASSOCIATION_ENTITY, request_ref),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown Pro Request association: {request_ref}")
        return json.loads(str(row["record_json"])), int(row["revision"])

    def _updated_association(
        self,
        association: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        action: str,
        operation_id: str,
    ) -> dict[str, Any]:
        result = deepcopy(dict(association))
        if request:
            result["canonical_revision"] = int(request.get("revision") or result.get("canonical_revision") or 0)
            result["status"] = str(request.get("status") or result.get("status") or "")
        self._append_operation(result, action, operation_id)
        return result

    def _record_operation(self, request_ref: str, action: str, operation_id: str) -> dict[str, Any]:
        return self._record_operations(request_ref, {action: operation_id})

    def _record_operations(self, request_ref: str, operations: Mapping[str, str]) -> dict[str, Any]:
        def update(record: dict[str, Any]) -> None:
            for action, operation_id in operations.items():
                self._append_operation(record, action, operation_id)

        saved = self.hub_store.update_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref, update)
        return deepcopy(saved["record"])

    def _refresh_and_record_operations(
        self,
        association: Mapping[str, Any],
        request: Mapping[str, Any],
        operations: Mapping[str, str],
    ) -> dict[str, Any]:
        def update(record: dict[str, Any]) -> None:
            if request:
                record["canonical_revision"] = int(request.get("revision") or record.get("canonical_revision") or 0)
                record["status"] = str(request.get("status") or record.get("status") or "")
            for action, operation_id in operations.items():
                self._append_operation(record, action, operation_id)

        saved = self.hub_store.update_entity(
            PRO_REQUEST_ASSOCIATION_ENTITY,
            str(association["request_ref"]),
            update,
        )
        return deepcopy(saved["record"])

    @staticmethod
    def _append_operation(record: dict[str, Any], action: str, operation_id: str) -> None:
        operation_ids = record.setdefault("operation_ids", [])
        if operation_id not in operation_ids:
            operation_ids.append(operation_id)
        record.setdefault("action_operation_ids", {})[action] = operation_id

    def _begin_operation(
        self,
        action: str,
        args: Mapping[str, Any],
        association: Mapping[str, Any],
        route: ProRequestRoute,
        identity: ManagerIdentity,
    ) -> dict[str, Any]:
        key = str(args.get("idempotency_key") or "").strip()
        if not key:
            return public_envelope(
                "blocked",
                result={
                    "accepted": False,
                    "reason": "idempotency_key_required",
                    "request_ref": association["request_ref"],
                },
            )
        payload = {
            key: deepcopy(value)
            for key, value in args.items()
            if key not in {"idempotency_key", "request_id"}
        }
        payload.update(
            {
                "request_ref": association["request_ref"],
                "participant_ref": identity.participant_ref,
                "work_group_id": route.work_group_id,
                "lane": route.lane,
                "machine_id": route.machine_id,
                "edge_generation": route.edge_generation,
                "workspace_ref": route.workspace_ref,
            }
        )
        try:
            operation = self.hub_store.create_operation(
                tool=f"{PRO_REQUEST_TOOL_PREFIX}{action}",
                logical_target=str(association["request_ref"]),
                idempotency_key=key,
                payload=payload,
                principal_ref=identity.principal_ref,
            )
        except HubStoreV2Conflict as error:
            return public_envelope(
                "blocked",
                result={
                    "accepted": False,
                    "reason": str(error),
                    "request_ref": association["request_ref"],
                },
            )
        if operation.get("idempotent_replay"):
            return self._replay_operation(operation)
        return self._advance_to_running(operation)

    def _create_dispatch_child(
        self,
        parent: Mapping[str, Any],
        args: Mapping[str, Any],
        association: Mapping[str, Any],
        target: str,
    ) -> dict[str, Any]:
        item_id = "dispatch_target"
        child_key = "child_" + semantic_payload_hash(
            {"parent_operation_id": parent["operation_id"], "item_id": item_id}
        )
        tool = "patchbay_worker_message" if target == "origin_worker" else "patchbay_worker_start"
        logical_target = f"{association['request_ref']}/{target}"
        child = self.hub_store.create_operation(
            tool=tool,
            logical_target=logical_target,
            idempotency_key=child_key,
            payload={
                "request_ref": association["request_ref"],
                "target": target,
                "message_source": args.get("message_source") or "worker_message_markdown",
                "new_worker_name": args.get("new_worker_name") or "",
                "workspace_mode": args.get("workspace_mode") or "isolated_write",
            },
            parent_operation_id=str(parent["operation_id"]),
            item_id=item_id,
        )
        return self._advance_to_running(child) if not child.get("idempotent_replay") else child

    def _advance_to_running(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(operation)
        for state in ("payload_ready", "dispatchable", "running"):
            if result.get("state") == state:
                continue
            result = self.hub_store.cas_operation_state(
                str(result["operation_id"]),
                expected_revision=int(result["revision"]),
                state=state,
            )
            if result is None:
                stored = self.hub_store.get_operation(str(operation["operation_id"]))
                if stored is None:
                    raise HubStoreV2Conflict("operation_disappeared")
                return stored
        return result

    def _finish_operation(
        self,
        operation: Mapping[str, Any],
        status: str,
        result: Mapping[str, Any],
        *,
        warnings: list[Any] | None = None,
        next_actions: list[Any] | None = None,
        item_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        terminal = "failed" if status == "failed" else "blocked" if status in {"blocked", "not_found"} else "succeeded"
        stored_envelope = {
            "status": status,
            "result": deepcopy(dict(result)),
            "warnings": deepcopy(warnings or []),
            "next_actions": deepcopy(next_actions or []),
        }
        current = self.hub_store.get_operation(str(operation["operation_id"])) or dict(operation)
        if current["state"] not in TERMINAL_OPERATION_STATES:
            saved = self.hub_store.cas_operation_state(
                str(current["operation_id"]),
                expected_revision=int(current["revision"]),
                state=terminal,
                result=stored_envelope,
            )
            current = saved or self.hub_store.get_operation(str(current["operation_id"])) or current
        public_operation = self._public_operation(current)
        if item_results:
            public_operation["item_results"] = deepcopy(item_results)
        return public_envelope(
            status,
            result=result,
            operation=public_operation,
            warnings=warnings,
            next_actions=next_actions,
        )

    def _mark_unknown(self, operation: Mapping[str, Any], error: Exception) -> dict[str, Any]:
        current = self.hub_store.get_operation(str(operation["operation_id"])) or dict(operation)
        if current.get("state") == "running":
            saved = self.hub_store.cas_operation_state(
                str(current["operation_id"]),
                expected_revision=int(current["revision"]),
                state="outcome_unknown",
                error={"reason": "dispatch_outcome_unknown", "message": str(error)},
            )
            if saved is not None:
                return saved
        return self.hub_store.get_operation(str(operation["operation_id"])) or current

    def _replay_operation(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        stored = operation.get("result")
        if isinstance(stored, Mapping) and isinstance(stored.get("result"), Mapping):
            public_operation = self._public_operation(operation)
            result = deepcopy(dict(stored["result"]))
            item = result.get("dispatch_operation")
            if isinstance(item, Mapping):
                public_operation["item_results"] = [
                    {
                        "item_id": "dispatch_target",
                        "status": stored.get("status") or "ok",
                        "operation_id": item.get("operation_id"),
                        "target": result.get("dispatch_target"),
                    }
                ]
            return public_envelope(
                str(stored.get("status") or "ok"),
                result=result,
                operation=public_operation,
                warnings=list(stored.get("warnings") or []),
                next_actions=list(stored.get("next_actions") or []),
            )
        return public_envelope(
            "pending",
            result={"reason": "operation_in_progress_or_reconciling"},
            operation=self._public_operation(operation),
            next_actions=[
                {
                    "tool": "patchbay_operation_status",
                    "arguments": {"operation_id": operation["operation_id"]},
                }
            ],
        )

    @staticmethod
    def _public_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "operation_id": operation.get("operation_id"),
            "tool_name": operation.get("tool"),
            "state": operation.get("state"),
            "idempotency_key": operation.get("idempotency_key"),
            "semantic_payload_hash": operation.get("semantic_payload_hash"),
            "revision": operation.get("revision"),
            "created_at": operation.get("created_at"),
            "updated_at": operation.get("updated_at"),
        }
        if operation.get("parent_operation_id"):
            result["parent_operation_id"] = operation["parent_operation_id"]
        return {key: value for key, value in result.items() if value not in (None, "")}

    async def _invoke_dispatch(
        self,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None,
        operation_id: str,
        route: ProRequestRoute,
    ) -> Mapping[str, Any]:
        executor = self.dispatch_executor
        handler = getattr(executor, "handle_tool_call", None)
        if callable(handler):
            result = handler(f"{CANONICAL_TOOL_PREFIX}dispatch", dict(arguments), context=context)
        else:
            callback = getattr(executor, "dispatch_pro_request", None)
            if not callable(callback):
                callback = executor
            if not callable(callback):
                raise TypeError("dispatch_executor must be callable or define handle_tool_call")
            parameters = inspect.signature(callback).parameters
            keyword: dict[str, Any] = {}
            if "context" in parameters or any(
                value.kind is inspect.Parameter.VAR_KEYWORD for value in parameters.values()
            ):
                keyword["context"] = context
                keyword["operation_id"] = operation_id
                keyword["route"] = route
            result = callback(dict(arguments), **keyword)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise TypeError("dispatch_executor must return a mapping")
        return result

    def _mutation_refusal(
        self,
        args: Mapping[str, Any],
        association: Mapping[str, Any],
        canonical: Mapping[str, Any],
        identity: ManagerIdentity,
    ) -> dict[str, Any] | None:
        request = canonical.get("request") or {}
        expected = args.get("expected_revision")
        if expected is not None:
            refusal = self._revision_refusal(expected, int(request.get("revision") or 0), association)
            if refusal is not None:
                return refusal
        return self._claim_refusal(association, identity, args)

    @staticmethod
    def _revision_refusal(
        expected: Any,
        actual: int,
        association: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        try:
            expected_value = int(expected)
        except (TypeError, ValueError):
            return {"accepted": False, "reason": "invalid_expected_revision", "actual_revision": actual}
        if expected_value == actual:
            return None
        return {
            "accepted": False,
            "reason": "stale_revision",
            "expected_revision": expected_value,
            "actual_revision": actual,
            "request_ref": association["request_ref"],
        }

    def _claim_refusal(
        self,
        association: Mapping[str, Any],
        identity: ManagerIdentity,
        args: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        claim = association.get("claim") or {}
        claimant = str(claim.get("participant_ref") or "")
        active = float(claim.get("lease_expires_at") or 0) > self._clock()
        if claimant and claimant != identity.participant_ref and active and not bool(args.get("takeover")):
            return {
                "accepted": False,
                "reason": "claim_held_by_another_participant",
                "takeover_required": True,
                "claim_revision": claim.get("claimed_revision"),
                "lease_expires_at": claim.get("lease_expires_at"),
                "request_ref": association["request_ref"],
            }
        return None

    def _decorate_domain(
        self,
        domain: Mapping[str, Any],
        association: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = deepcopy(dict(domain))
        if isinstance(result.get("request"), Mapping):
            result["request"] = self._decorate_request(result["request"], association)
        result["request_ref"] = association["request_ref"]
        self._add_route_result(result, self._route_from_association(association), association=association)
        return result

    @staticmethod
    def _decorate_request(
        request: Mapping[str, Any],
        association: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = deepcopy(dict(request))
        metadata = {
            "request_ref": association.get("request_ref"),
            "principal_ref": association.get("principal_ref"),
            "machine_id": association.get("machine_id"),
            "edge_generation": association.get("edge_generation"),
            "workspace_ref": association.get("workspace_ref"),
            "work_group_id": association.get("work_group_id"),
            "lane": association.get("lane"),
            "lane_id": association.get("lane"),
            "visibility": association.get("visibility"),
            "origin_operation_id": association.get("origin_operation_id"),
            "operation_ids": deepcopy(association.get("operation_ids") or []),
            "action_operation_ids": deepcopy(association.get("action_operation_ids") or {}),
            "claim_lease": deepcopy(association.get("claim") or {}),
        }
        for key, value in metadata.items():
            result.setdefault(key, value)
        return result

    @staticmethod
    def _add_route_result(
        result: dict[str, Any],
        route: ProRequestRoute,
        *,
        association: Mapping[str, Any] | None = None,
    ) -> None:
        source = association or {}
        machine_id = str(source.get("machine_id") or route.machine_id)
        edge_generation = str(source.get("edge_generation") or route.edge_generation)
        workspace_ref = str(source.get("workspace_ref") or route.workspace_ref)
        work_group_id = str(source.get("work_group_id") or route.work_group_id)
        lane = str(source.get("lane") or route.lane)
        result["machine"] = {"machine_id": machine_id, "edge_generation": edge_generation}
        result["workspace"] = {"workspace_ref": workspace_ref}
        if work_group_id:
            result["work_group"] = {"work_group_id": work_group_id}
        if lane:
            result["lane"] = {"lane": lane, "lane_id": lane}

    def _is_visible(
        self,
        association: Mapping[str, Any],
        route: ProRequestRoute,
        identity: ManagerIdentity,
    ) -> bool:
        if association.get("principal_ref") != identity.principal_ref:
            return False
        if association.get("machine_id") != route.machine_id:
            return False
        if association.get("edge_generation") != route.edge_generation:
            return False
        if association.get("workspace_ref") != route.workspace_ref:
            return False
        if association.get("visibility") == "shared":
            return True
        if association.get("owner_participant_ref") == identity.participant_ref:
            return True
        group = str(association.get("work_group_id") or "")
        return bool(group and group == route.work_group_id)

    def _route_for(
        self,
        args: Mapping[str, Any],
        context: RequestContext | None,
    ) -> ProRequestRoute:
        work_group_id = str(
            args.get("work_group_id")
            or (context.work_group_id if context else "")
            or self.route.work_group_id
        ).strip()
        lane = str(args.get("lane") or (context.lane_id if context else "") or self.route.lane).strip()
        return ProRequestRoute(
            machine_id=self.route.machine_id,
            edge_generation=self.route.edge_generation,
            workspace_ref=self.route.workspace_ref,
            work_group_id=work_group_id,
            lane=lane,
            visibility=self.route.visibility,
            origin_operation_id=self.route.origin_operation_id,
        )

    @staticmethod
    def _route_from_association(association: Mapping[str, Any]) -> ProRequestRoute:
        return ProRequestRoute(
            machine_id=str(association.get("machine_id") or ""),
            edge_generation=str(association.get("edge_generation") or ""),
            workspace_ref=str(association.get("workspace_ref") or ""),
            work_group_id=str(association.get("work_group_id") or ""),
            lane=str(association.get("lane") or ""),
            visibility=str(association.get("visibility") or "private"),
            origin_operation_id=str(association.get("origin_operation_id") or ""),
        )

    def _route_error(self, args: Mapping[str, Any]) -> dict[str, Any] | None:
        machine = str(args.get("machine_id") or "")
        workspace = str(args.get("workspace_ref") or "")
        if machine and machine != self.route.machine_id:
            return public_envelope(
                "not_found",
                result={"found": False, "reason": "machine_route_mismatch"},
            )
        if workspace and workspace != self.route.workspace_ref:
            return public_envelope(
                "not_found",
                result={"found": False, "reason": "workspace_route_mismatch"},
            )
        return None

    def _identity(self, context: RequestContext | None) -> ManagerIdentity:
        return ManagerIdentity.from_request(context, principal_ref=self.hub_store.principal_ref)

    def _request_ref(self, request_id: str) -> str:
        return stable_ref(
            "proreq",
            self.route.machine_id,
            self.route.edge_generation,
            request_id,
            salt=self.reference_salt,
        )

    @staticmethod
    def _canonical_arguments(args: Mapping[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in args.items() if key not in _HUB_ARGUMENTS}

    @staticmethod
    def _domain_status(domain: Mapping[str, Any]) -> str:
        status = str(domain.get("status") or "")
        if domain.get("accepted") is False or status in {
            "blocked",
            "refused",
            "repo_busy",
            "capacity_blocked",
            "needs_confirmation",
        }:
            return "blocked"
        if domain.get("failed") is True or status in {"failed", "error"}:
            return "failed"
        if domain.get("partial") is True or status == "partial":
            return "partial"
        return "ok"

    @staticmethod
    def _not_found(request_id: str) -> dict[str, Any]:
        return public_envelope(
            "not_found",
            result={"found": False, "request_id": request_id, "reason": "pro_request_not_visible_or_missing"},
        )


class FleetHubProRequestAdapterV2(HubProRequestAdapterV2):
    """Production adapter backed by sanitized Hub projections and Edge calls."""

    _PROJECTION_FIELDS = frozenset(
        {
            "request_id",
            "status",
            "revision",
            "created_at",
            "updated_at",
            "workspace_id",
            "workspace_ref",
            "repo_name",
            "priority",
            "kind",
            "response_exists",
            "origin_available_for_dispatch",
            "attachment_count",
        }
    )
    _TERMINAL_REQUEST_STATUSES = frozenset({"closed", "cancelled", "superseded"})

    def __init__(self, hub_store: HubStoreV2, runtime: Any, edge: Any, broker: Any):
        self.hub_store = hub_store
        self.runtime = runtime
        self.edge = edge
        self.broker = broker
        self.reference_salt = hub_store.principal_ref

    def ingest_projection(
        self,
        machine: Mapping[str, Any],
        projection: Mapping[str, Any],
        *,
        projection_revision: int,
        received_at: float,
    ) -> None:
        """Persist only the metadata allowlist needed for fleet discovery/routing."""

        machine_id = str(machine.get("machine_id") or "")
        edge_generation = str(machine.get("edge_generation") or "")
        values = projection.get("pro_requests") or []
        if not isinstance(values, list):
            raise ValueError("Pro Request projection must be an array")
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError("Each Pro Request projection must be an object")
            raw_id = str(value.get("request_id") or "").strip()
            if not raw_id:
                raise ValueError("Pro Request projection requires request_id")
            request_ref = self._fleet_request_ref(machine_id, edge_generation, raw_id)
            seen.add(request_ref)
            metadata = {
                key: deepcopy(value[key])
                for key in self._PROJECTION_FIELDS
                if key in value
            }
            workspace_ref = self._resolve_workspace_ref(
                machine_id,
                edge_generation,
                str(metadata.get("workspace_ref") or ""),
                str(metadata.get("repo_name") or ""),
            )
            record = {
                **metadata,
                "request_ref": request_ref,
                "edge_request_id": raw_id,
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "workspace_ref": workspace_ref,
                "projection_revision": int(projection_revision),
                "received_at": float(received_at),
                "active": True,
            }
            self._upsert_association(request_ref, record)

        tombstones = projection.get("pro_request_tombstones") or []
        if not isinstance(tombstones, list):
            raise ValueError("Pro Request projection tombstones must be an array")
        for value in tombstones:
            if not isinstance(value, Mapping) or not value.get("request_id"):
                continue
            request_ref = self._fleet_request_ref(
                machine_id, edge_generation, str(value["request_id"])
            )
            self._deactivate_association(request_ref, projection_revision, received_at)

        if projection.get("complete_pro_request_set") is True:
            for entity in self.hub_store.list_entities(PRO_REQUEST_ASSOCIATION_ENTITY):
                record = entity["record"]
                if (
                    record.get("machine_id") == machine_id
                    and record.get("edge_generation") == edge_generation
                    and record.get("active")
                    and entity["entity_id"] not in seen
                ):
                    self._deactivate_association(
                        entity["entity_id"], projection_revision, received_at
                    )

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        if not name.startswith(PRO_REQUEST_TOOL_PREFIX):
            raise ValueError(f"Unsupported Hub V2 Pro Request tool: {name}")
        action = name.removeprefix(PRO_REQUEST_TOOL_PREFIX)
        if action == "list":
            return await self.list_requests(arguments, context=context)
        if action == "read":
            return await self.read_request(arguments, context=context)
        if action in MUTATING_ACTIONS:
            return await self._routed_mutation(name, arguments, context=context)
        raise ValueError(f"Unsupported Hub V2 Pro Request tool: {name}")

    async def list_requests(
        self,
        arguments: Mapping[str, Any] | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs)
        route = await self._selector_route(args, context)
        if self._is_envelope(route):
            return dict(route)
        statuses = args.get("status") or []
        if isinstance(statuses, str):
            statuses = [statuses]
        status_filter = {str(value) for value in statuses}
        include_closed = bool(args.get("include_closed", False))
        records: list[dict[str, Any]] = []
        for entity in self.hub_store.list_entities(PRO_REQUEST_ASSOCIATION_ENTITY):
            record = deepcopy(dict(entity["record"]))
            if not record.get("active") or not self._route_matches(record, route):
                continue
            status = str(record.get("status") or "")
            if status_filter and status not in status_filter:
                continue
            if not include_closed and status in self._TERMINAL_REQUEST_STATUSES:
                continue
            records.append(self._projection_public(record))
        records.sort(
            key=lambda item: (float(item.get("updated_at") or 0), item["request_ref"]),
            reverse=True,
        )
        limit = max(1, min(int(args.get("limit") or 10), 100))
        page = records[:limit]
        return public_envelope(
            "ok",
            result={
                "requests": page,
                "count": len(page),
                "total_known": len(records),
                "truncated": len(records) > limit,
                "projection_only": True,
                "private_content_projected": False,
                "next_step": "Call patchbay_pro_request_read with a request_ref to retrieve private content from its Edge.",
            },
        )

    async def read_request(
        self,
        arguments: Mapping[str, Any] | str | None = None,
        *,
        context: RequestContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        args = _merge_arguments(arguments, kwargs, scalar_field="request_id")
        association = await self._visible_association(args, context)
        if isinstance(association, dict) and association.get("status") in PUBLIC_STATUSES:
            return association
        assert isinstance(association, Mapping)
        edge_args = self._edge_arguments(args, association)
        raw = await self.edge.execute(
            machine_id=str(association["machine_id"]),
            edge_generation=str(association["edge_generation"]),
            action="codex_pro_request_read",
            arguments=edge_args,
            target=self._edge_target(association, args),
            context=context,
        )
        return self._decorate_edge_envelope(raw, association)

    async def _routed_mutation(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None,
    ) -> dict[str, Any]:
        args = deepcopy(dict(arguments))
        association = await self._visible_association(args, context)
        if isinstance(association, dict) and association.get("status") in PUBLIC_STATUSES:
            return association
        assert isinstance(association, Mapping)
        expected = args.get("expected_revision")
        if expected is None:
            return public_envelope(
                "blocked",
                result={
                    "accepted": False,
                    "reason": "expected_revision_required",
                    "request_ref": association["request_ref"],
                    "current_revision": int(association.get("revision") or 0),
                },
            )
        payload = {
            "action": name.replace(PRO_REQUEST_TOOL_PREFIX, CANONICAL_TOOL_PREFIX, 1),
            "arguments": self._edge_arguments(args, association, include_expected=True),
            "target": self._edge_target(association, args),
            "context": (
                context.durable_operation_metadata() if context is not None else {}
            ),
            "machine_id": association["machine_id"],
            "edge_generation": association["edge_generation"],
        }
        operation = self.broker.create_operation(
            tool=name,
            logical_target=f"pro-request:{association['request_ref']}",
            idempotency_key=str(args.get("idempotency_key") or ""),
            payload=payload,
            principal_ref=self.hub_store.principal_ref,
        )
        operation = self._ensure_dispatchable(operation)
        return self.operation_result(operation, association=association)

    def operation_result(
        self,
        operation: Mapping[str, Any],
        *,
        association: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if association is None:
            association = self._association_for_operation(operation)
        result = operation.get("result")
        if isinstance(result, Mapping) and str(result.get("status") or "") in PUBLIC_STATUSES:
            envelope = self._decorate_edge_envelope(result, association or {})
            envelope["operation"] = self._public_broker_operation(
                operation, association=association
            )
            return envelope
        state = str(operation.get("state") or "")
        status = {
            "succeeded": "ok",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "failed",
        }.get(state, "pending")
        return public_envelope(
            status,
            result=self._route_fields(association or {}),
            operation=self._public_broker_operation(operation, association=association),
            next_actions=(
                [{"tool": "patchbay_operation_status", "arguments": {"operation_id": operation["operation_id"]}}]
                if status == "pending"
                else []
            ),
        )

    async def _visible_association(
        self, args: Mapping[str, Any], context: RequestContext | None
    ) -> Mapping[str, Any] | dict[str, Any]:
        request_ref = str(args.get("request_id") or "")
        entity = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
        if entity is None or not entity["record"].get("active"):
            return self._not_found(request_ref)
        association = deepcopy(dict(entity["record"]))
        route = await self._selector_route(args, context)
        if self._is_envelope(route):
            return dict(route)
        if not self._route_matches(association, route):
            return self._not_found(request_ref)
        return association

    async def _selector_route(
        self, args: Mapping[str, Any], context: RequestContext | None
    ) -> Mapping[str, Any]:
        if not any(args.get(key) for key in ("work_group_id", "machine_id", "workspace_ref")):
            return {}
        if args.get("workspace_ref") and not any(
            args.get(key) for key in ("work_group_id", "machine_id")
        ):
            return {"workspace_ref": str(args["workspace_ref"])}
        return await _maybe_await(
            self.runtime.resolve_target(
                tool_name="patchbay_pro_request_list",
                arguments=args,
                context=context,
            )
        )

    @staticmethod
    def _route_matches(record: Mapping[str, Any], route: Mapping[str, Any]) -> bool:
        return all(
            not route.get(key) or str(record.get(key) or "") == str(route.get(key) or "")
            for key in ("machine_id", "edge_generation", "workspace_ref")
        )

    def _decorate_edge_envelope(
        self, raw: Mapping[str, Any], association: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self._is_envelope(raw):
            envelope = {
                "status": str(raw["status"]),
                "result": deepcopy(dict(raw.get("result") or {})),
                "operation": deepcopy(dict(raw.get("operation") or {})),
                "warnings": deepcopy(list(raw.get("warnings") or [])),
                "next_actions": deepcopy(list(raw.get("next_actions") or [])),
            }
        else:
            envelope = public_envelope("ok", result=raw)
        domain = envelope["result"]
        request = domain.get("request") if isinstance(domain.get("request"), Mapping) else None
        if request is not None:
            domain["request"] = self._decorate_canonical_request(request, association)
            self._refresh_from_canonical(association, request)
        domain.update(self._route_fields(association))
        domain.setdefault("applied", False)
        domain.setdefault("committed", False)
        if domain.get("response_stored"):
            domain.setdefault("dispatched", False)
        return envelope

    def _refresh_from_canonical(
        self, association: Mapping[str, Any], request: Mapping[str, Any]
    ) -> None:
        request_ref = str(association.get("request_ref") or "")
        entity = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
        if entity is None:
            return
        record = deepcopy(dict(entity["record"]))
        for key in (
            "status",
            "revision",
            "created_at",
            "updated_at",
            "workspace_id",
            "repo_name",
            "priority",
            "kind",
            "attachment_count",
        ):
            if key in request:
                record[key] = deepcopy(request[key])
        response = request.get("response") if isinstance(request.get("response"), Mapping) else {}
        origin = request.get("origin") if isinstance(request.get("origin"), Mapping) else {}
        record["response_exists"] = bool(response.get("exists"))
        record["origin_available_for_dispatch"] = bool(
            origin.get("origin_available_for_dispatch")
        )
        self.hub_store.put_entity(
            PRO_REQUEST_ASSOCIATION_ENTITY,
            request_ref,
            record,
            expected_revision=entity["revision"],
        )

    def _projection_public(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "request_ref": record["request_ref"],
            "status": record.get("status"),
            "revision": int(record.get("revision") or 0),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "workspace_id": record.get("workspace_id"),
            "workspace_ref": record.get("workspace_ref"),
            "repo_name": record.get("repo_name"),
            "priority": record.get("priority"),
            "kind": record.get("kind"),
            "response": {"exists": bool(record.get("response_exists"))},
            "origin": {
                "origin_available_for_dispatch": bool(
                    record.get("origin_available_for_dispatch")
                )
            },
            "attachment_count": int(record.get("attachment_count") or 0),
            **self._route_fields(record),
        }

    def _decorate_canonical_request(
        self, request: Mapping[str, Any], association: Mapping[str, Any]
    ) -> dict[str, Any]:
        value = deepcopy(dict(request))
        value["request_ref"] = association.get("request_ref")
        value.update(self._route_fields(association))
        return value

    @staticmethod
    def _route_fields(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: record[key]
            for key in ("request_ref", "machine_id", "edge_generation", "workspace_ref")
            if record.get(key) not in (None, "")
        }

    def _edge_arguments(
        self,
        args: Mapping[str, Any],
        association: Mapping[str, Any],
        *,
        include_expected: bool = False,
    ) -> dict[str, Any]:
        excluded = set(_HUB_ARGUMENTS)
        if include_expected:
            excluded.remove("expected_revision")
        result = {
            key: deepcopy(value)
            for key, value in args.items()
            if key not in excluded and key != "idempotency_key"
        }
        result["request_id"] = association["edge_request_id"]
        return result

    @staticmethod
    def _edge_target(
        association: Mapping[str, Any], args: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "machine_id": association["machine_id"],
            "edge_generation": association["edge_generation"],
            "workspace_ref": association.get("workspace_ref") or "",
            "request_id": association["edge_request_id"],
            "work_group_id": str(args.get("work_group_id") or ""),
            "lane_id": str(args.get("lane") or ""),
        }

    def _association_for_operation(
        self, operation: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        target = str(operation.get("logical_target") or "")
        request_ref = target.removeprefix("pro-request:") if target.startswith("pro-request:") else ""
        entity = (
            self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
            if request_ref
            else None
        )
        return deepcopy(entity["record"]) if entity is not None else None

    def _ensure_dispatchable(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        current = deepcopy(dict(operation))
        if current.get("state") == "created":
            current = self.broker.prepare_operation(
                str(current["operation_id"]),
                expected_revision=int(current["revision"]),
                principal_ref=str(current["principal_ref"]),
            ) or current
        if current.get("state") == "payload_ready":
            current = self.broker.make_dispatchable(
                str(current["operation_id"]),
                expected_revision=int(current["revision"]),
                principal_ref=str(current["principal_ref"]),
            ) or current
        return deepcopy(dict(current))

    @staticmethod
    def _public_broker_operation(
        operation: Mapping[str, Any], *, association: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        result = {
            key: deepcopy(operation[key])
            for key in (
                "operation_id",
                "tool_name",
                "state",
                "revision",
                "created_at",
                "updated_at",
                "parent_operation_id",
            )
            if operation.get(key) not in (None, "")
        }
        if association:
            result["machine_id"] = str(association.get("machine_id") or "")
            result["edge_generation"] = str(association.get("edge_generation") or "")
        return result

    def _fleet_request_ref(self, machine_id: str, edge_generation: str, raw_id: str) -> str:
        return stable_ref(
            "proreq", machine_id, edge_generation, raw_id, salt=self.reference_salt
        )

    def _resolve_workspace_ref(
        self,
        machine_id: str,
        edge_generation: str,
        advertised_ref: str,
        repo_name: str,
    ) -> str:
        names = {value.casefold() for value in (advertised_ref, repo_name) if value}
        candidates: list[str] = []
        for entity in self.hub_store.list_entities("hub.workspace_projection"):
            record = entity["record"]
            if (
                record.get("machine_id") != machine_id
                or record.get("edge_generation") != edge_generation
                or not record.get("active")
            ):
                continue
            aliases = {str(value).casefold() for value in record.get("aliases") or []}
            if names.intersection(aliases) or advertised_ref == record.get("workspace_ref"):
                candidates.append(str(record.get("workspace_ref") or ""))
        return sorted(set(candidates))[0] if candidates else advertised_ref

    def _upsert_association(self, request_ref: str, record: Mapping[str, Any]) -> None:
        entity = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
        self.hub_store.put_entity(
            PRO_REQUEST_ASSOCIATION_ENTITY,
            request_ref,
            record,
            expected_revision=entity["revision"] if entity is not None else 0,
        )

    def _deactivate_association(
        self, request_ref: str, projection_revision: int, received_at: float
    ) -> None:
        entity = self.hub_store.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
        if entity is None:
            return
        record = deepcopy(dict(entity["record"]))
        record.update(
            {
                "active": False,
                "projection_revision": int(projection_revision),
                "received_at": float(received_at),
            }
        )
        self.hub_store.put_entity(
            PRO_REQUEST_ASSOCIATION_ENTITY,
            request_ref,
            record,
            expected_revision=entity["revision"],
        )

    @staticmethod
    def _is_envelope(value: Mapping[str, Any]) -> bool:
        return str(value.get("status") or "") in PUBLIC_STATUSES and isinstance(
            value.get("result"), Mapping
        )


def _merge_arguments(
    arguments: Mapping[str, Any] | str | None,
    kwargs: Mapping[str, Any],
    *,
    scalar_field: str = "",
) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        result = dict(arguments)
    elif arguments is None:
        result = {}
    elif scalar_field:
        result = {scalar_field: arguments}
    else:
        raise TypeError("arguments must be a mapping")
    overlap = set(result).intersection(kwargs)
    if overlap:
        raise TypeError(f"Duplicate argument: {sorted(overlap)[0]}")
    result.update(kwargs)
    return result


# Keep natural import spellings available.
ProRequestAdapterV2 = HubProRequestAdapterV2
HubProRequestAdapter = HubProRequestAdapterV2
ProRequestAdapter = HubProRequestAdapterV2


__all__ = [
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "FleetHubProRequestAdapterV2",
    "HubProRequestAdapter",
    "HubProRequestAdapterV2",
    "PRO_REQUEST_ASSOCIATION_ENTITY",
    "ProRequestAdapter",
    "ProRequestAdapterV2",
    "ProRequestCanonicalStore",
    "ProRequestRoute",
]
