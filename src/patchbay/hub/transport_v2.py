"""Production pull-transport bridge for the Hub V2 application graph.

The Hub application creates operations while an Edge runner polls independently.
This bridge is both the ``HubAppV2.edge_delivery`` implementation and the Edge
facade discovered by :mod:`patchbay.hub.server_v2`.  It contains no global app
or server wiring.
"""
from __future__ import annotations

import hashlib
import inspect
import secrets
import time
from copy import deepcopy
from typing import Any, Mapping

from patchbay.hub.broker import (
    ATTEMPT_CONTRACT_ENTITY_TYPE,
    OperationBroker,
)
from patchbay.hub.operations import (
    PUBLIC_STATUSES,
    TERMINAL_OPERATION_STATES,
    public_envelope,
)
from patchbay.hub.runtime_v2 import MACHINE_ENTITY, WORK_GROUP_ENTITY, HubRuntimeV2
from patchbay.hub.store_v2 import (
    HubStoreV2,
    HubStoreV2Conflict,
    HubStoreV2StateError,
    semantic_payload_hash,
)
from patchbay.hub.tool_surface import HUB_V2_ACTION_MAP, HUB_V2_CONTRACT_HASH
from patchbay.protocol.context import RequestContext


EDGE_DISPATCH_ENTITY = "hub.edge_dispatch"
EDGE_RECEIPT_ENTITY = "hub.edge_receipt"
_TRANSIENT_PAYLOAD_KEY = "transient_payload"

DEFAULT_SEMANTIC_WAIT_SECONDS = 5.0
MAX_SEMANTIC_WAIT_SECONDS = 30.0
DEFAULT_RECEIPT_ACK_LIMIT = 100

_CLAIMABLE_OPERATION_STATES = frozenset(
    {"dispatchable", "running", "outcome_unknown", "reconciling"}
)
_RESUMABLE_ATTEMPT_STATES = frozenset(
    {"offered", "claimed", "executing", "effect_recorded"}
)
_PREFLIGHT_ACTION = "patchbay_edge_preflight"
_PREFLIGHT_EXECUTION_ACTION = "codex_open_workspace"


class HubPullTransportBridgeV2:
    """Bridge Hub operations to an independently polling V2 Edge.

    Construction is intentionally two-phase because ``HubAppV2`` requires its
    delivery port while this bridge requires the app's broker/runtime/store::

        bridge = HubPullTransportBridgeV2()
        app = HubAppV2(..., edge_delivery=bridge)
        bridge.bind(app)

    The bound bridge may then be passed to ``create_hub_v2_server`` as its
    ``hub_app``.  MCP calls are delegated to the app and Edge endpoint discovery
    finds the explicit ``edge_*`` methods below.
    """

    def __init__(
        self,
        app: Any | None = None,
        *,
        semantic_wait_seconds: float = DEFAULT_SEMANTIC_WAIT_SECONDS,
        receipt_ack_limit: int = DEFAULT_RECEIPT_ACK_LIMIT,
        monotonic: Any = None,
    ):
        wait = float(semantic_wait_seconds)
        if wait < 0:
            raise ValueError("semantic_wait_seconds must be non-negative")
        self.semantic_wait_seconds = min(wait, MAX_SEMANTIC_WAIT_SECONDS)
        self.receipt_ack_limit = max(1, min(int(receipt_ack_limit), 10_000))
        self._monotonic = monotonic or time.monotonic
        self._app: Any | None = None
        self._store: HubStoreV2 | None = None
        self._broker: OperationBroker | None = None
        self._runtime: HubRuntimeV2 | None = None
        if app is not None:
            self.bind(app)

    def bind(self, app: Any) -> "HubPullTransportBridgeV2":
        """Bind once to the composed Hub app without replacing its services."""

        store = getattr(app, "store", None)
        broker = getattr(app, "broker", None)
        runtime = getattr(app, "runtime", None)
        if not isinstance(store, HubStoreV2):
            raise TypeError("Hub pull transport requires HubAppV2.store")
        if not isinstance(broker, OperationBroker):
            raise TypeError("Hub pull transport requires HubAppV2.broker")
        if not isinstance(runtime, HubRuntimeV2):
            raise TypeError("Hub pull transport requires HubAppV2.runtime")
        if runtime.store is not store or broker.store is not store:
            raise TypeError("Hub app runtime, broker, and transport must share one store")
        if self._app is not None and self._app is not app:
            raise RuntimeError("Hub pull transport is already bound to another app")
        self._app = app
        self._store = store
        self._broker = broker
        self._runtime = runtime
        return self

    attach = bind
    bind_app = bind

    @property
    def app(self) -> Any:
        return self._require_bound()[0]

    @property
    def store(self) -> HubStoreV2:
        return self._require_bound()[1]

    @property
    def broker(self) -> OperationBroker:
        return self._require_bound()[2]

    @property
    def runtime(self) -> HubRuntimeV2:
        return self._require_bound()[3]

    @property
    def principal_ref(self) -> str:
        return self.store.principal_ref

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        """Delegate the public app boundary when used as the server facade."""

        result = self.app.handle_tool_call(name, arguments, context=context)
        result = await _maybe_await(result)
        if not isinstance(result, Mapping):
            raise TypeError("HubAppV2.handle_tool_call must return an object")
        return deepcopy(dict(result))

    def close(self) -> None:
        close = getattr(self.app, "close", None)
        if callable(close):
            close()

    # -- HubAppV2 Edge delivery ---------------------------------------

    async def dispatch_operation(
        self,
        *,
        operation: Mapping[str, Any],
        payload: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Persist and offer a broker attempt without executing it inline."""

        del context
        operation_id = _required_text(operation.get("operation_id"), "operation_id")
        current = self.store.get_operation(operation_id)
        if current is None:
            raise KeyError(operation_id)
        if current["state"] in TERMINAL_OPERATION_STATES:
            return self._operation_envelope(current)

        dispatch = self._persist_dispatch(current, payload)
        attempt = self._offer_dispatch(current, dispatch)
        current = self.store.get_operation(operation_id) or current

        # HubBrokerEdgeDispatchPortV2 still owns a push-era completion step.
        # Advancing its stale running revision keeps the durable pull offer
        # claimable and prevents a synthetic pending response from becoming an
        # outcome-unknown terminalization path.
        if current["state"] == "running":
            advanced = self.broker.transition_operation(
                operation_id,
                expected_revision=int(current["revision"]),
                state="reconciling",
                principal_ref=str(current["principal_ref"]),
            )
            current = advanced or self.store.get_operation(operation_id) or current

        pending = public_envelope(
            "pending",
            result={
                "reason": "awaiting_edge_claim",
                "attempt_id": str(attempt["attempt_id"]),
            },
            operation=self._public_operation(current),
            next_actions=[
                {
                    "tool": "patchbay_operation_status",
                    "arguments": {"operation_id": operation_id},
                }
            ],
        )
        return pending

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
        """Execute a direct read as a brokered operation with a bounded wait."""

        public_context = context.public_metadata() if context is not None else {}
        payload = {
            "action": _required_text(action, "action"),
            "arguments": deepcopy(dict(arguments)),
            "target": deepcopy(dict(target)),
            "context": public_context,
            "machine_id": _required_text(machine_id, "machine_id"),
            "edge_generation": _required_text(edge_generation, "edge_generation"),
        }
        operation = self.broker.create_operation(
            tool=str(action),
            logical_target=self._logical_target(action, target),
            idempotency_key=f"read_{secrets.token_hex(16)}",
            payload=payload,
            principal_ref=self.store.principal_ref,
        )
        operation = self.broker.prepare_operation(
            str(operation["operation_id"]),
            expected_revision=int(operation["revision"]),
            principal_ref=str(operation["principal_ref"]),
        ) or operation
        operation = self.broker.make_dispatchable(
            str(operation["operation_id"]),
            expected_revision=int(operation["revision"]),
            principal_ref=str(operation["principal_ref"]),
        ) or operation
        dispatch = self._persist_dispatch(operation, payload)
        self._offer_dispatch(operation, dispatch)
        return await self._wait_for_semantic_result(
            str(operation["operation_id"]),
            principal_ref=str(operation["principal_ref"]),
        )

    async def execute_edge_action(self, **kwargs: Any) -> dict[str, Any]:
        return await self.execute(**kwargs)

    # -- Edge endpoint facade -----------------------------------------

    def edge_enroll(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.runtime.enroll_machine(
            code=str(payload.get("code") or ""),
            machine_id=str(payload.get("machine_id") or ""),
            display_name=str(payload.get("display_name") or ""),
            tags=payload.get("tags"),
            role=str(payload.get("role") or ""),
            capabilities=(
                payload.get("capabilities")
                if isinstance(payload.get("capabilities"), Mapping)
                else None
            ),
            workspaces=(
                payload.get("workspaces")
                if isinstance(payload.get("workspaces"), list)
                else None
            ),
            edge_generation=str(payload.get("edge_generation") or ""),
        )

    def edge_heartbeat(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token, require_contract=False)
        response = self.runtime.heartbeat(
            machine_id=str(payload["machine_id"]),
            token=token,
            edge_generation=str(payload["edge_generation"]),
            projection_revision=int(payload["projection_revision"]),
            capabilities=(
                payload.get("capabilities")
                if isinstance(payload.get("capabilities"), Mapping)
                else None
            ),
            workspaces=(
                payload.get("workspaces")
                if isinstance(payload.get("workspaces"), list)
                else None
            ),
            worker_status=(
                payload.get("worker_status")
                if isinstance(payload.get("worker_status"), Mapping)
                else None
            ),
            worker_projection=(
                payload.get("worker_projection")
                if isinstance(payload.get("worker_projection"), Mapping)
                else None
            ),
            resource_status=(
                payload.get("resource_status")
                if isinstance(payload.get("resource_status"), Mapping)
                else None
            ),
        )
        return self._control_response(response, payload)

    def edge_projection(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token, require_contract=True)
        projection = payload.get("projection")
        if not isinstance(projection, Mapping):
            raise ValueError("projection must be an object")
        response = self.runtime.heartbeat(
            machine_id=str(payload["machine_id"]),
            token=token,
            edge_generation=str(payload["edge_generation"]),
            projection_revision=int(payload["projection_revision"]),
            worker_projection=projection,
        )
        return self._control_response(response, payload)

    def edge_claim(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        machine = self._authenticate(payload, token, require_contract=True)
        try:
            maximum = max(1, min(int(payload.get("max_attempts") or 1), 64))
            available = max(0, int(payload.get("available_slots", maximum)))
        except (TypeError, ValueError) as error:
            raise ValueError("max_attempts and available_slots must be integers") from error
        maximum = min(maximum, available)
        if maximum < 1:
            return self._control_response(
                {"accepted": True, "attempt": None, "attempts": []}, payload
            )

        machine_id = str(payload["machine_id"])
        external_generation = str(payload["edge_generation"])
        generation_number = self._generation_number(external_generation)
        contract_hash = self._requested_contract_hash(payload)
        self.broker.expire_leases()
        claimed: list[dict[str, Any]] = []

        for entity in self._dispatches_for_machine(machine_id, external_generation):
            if len(claimed) >= maximum:
                break
            operation_id = str(entity["entity_id"])
            operation = self.store.get_operation(operation_id)
            if operation is None or operation["state"] not in _CLAIMABLE_OPERATION_STATES:
                continue
            attempt = self._active_attempt(operation_id)
            if attempt is None or attempt["state"] not in _RESUMABLE_ATTEMPT_STATES:
                continue
            contract = self.store.get_entity(
                ATTEMPT_CONTRACT_ENTITY_TYPE, str(attempt["attempt_id"])
            )
            if contract is None:
                continue
            if (
                attempt["machine_id"] != machine_id
                or int(attempt["edge_generation"]) != generation_number
                or contract["record"].get("required_contract_hash") != contract_hash
            ):
                continue
            if not self._dispatch_is_action_compatible(entity["record"], machine):
                continue
            if operation["state"] == "outcome_unknown":
                operation = self.broker.transition_operation(
                    operation_id,
                    expected_revision=int(operation["revision"]),
                    state="reconciling",
                    principal_ref=str(operation["principal_ref"]),
                ) or self.store.get_operation(operation_id) or operation
            try:
                saved = self.broker.claim_attempt(
                    operation_id,
                    str(attempt["attempt_id"]),
                    machine_id=machine_id,
                    edge_generation=generation_number,
                    contract_hash=contract_hash,
                    fencing_token=int(attempt["fencing_token"]),
                    lease_seconds=payload.get("lease_seconds"),
                    principal_ref=str(operation["principal_ref"]),
                )
            except HubStoreV2Conflict as error:
                if str(error) == "attempt_lease_expired":
                    continue
                raise
            current_operation = self.store.get_operation(operation_id) or operation
            claimed.append(
                self._wire_attempt(
                    saved,
                    operation=current_operation,
                    dispatch=entity["record"],
                    machine=machine,
                    external_generation=external_generation,
                    contract=contract["record"],
                )
            )
        return self._control_response(
            {
                "accepted": True,
                "attempt": claimed[0] if claimed else None,
                "attempts": claimed,
            },
            payload,
        )

    def edge_lease(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        machine = self._authenticate(payload, token, require_contract=True)
        operation, attempt, contract = self._require_attempt_fences(payload)
        fences = {
            "expected_revision": int(payload["expected_revision"]),
            "machine_id": str(payload["machine_id"]),
            "edge_generation": self._generation_number(str(payload["edge_generation"])),
            "contract_hash": self._requested_contract_hash(payload),
            "fencing_token": int(payload["fencing_token"]),
            "principal_ref": str(operation["principal_ref"]),
        }
        if attempt["state"] == "claimed":
            saved = self.broker.mark_attempt_executing(
                str(operation["operation_id"]),
                str(attempt["attempt_id"]),
                **fences,
            )
        else:
            saved = self.broker.renew_lease(
                str(operation["operation_id"]),
                str(attempt["attempt_id"]),
                lease_seconds=payload.get("lease_seconds"),
                **fences,
            )
        if saved is None:
            raise HubStoreV2Conflict("stale_attempt_revision")
        dispatch = self.store.get_entity(
            EDGE_DISPATCH_ENTITY, str(operation["operation_id"])
        )
        response = {
            "accepted": True,
            "attempt": self._wire_attempt(
                saved,
                operation=self.store.get_operation(str(operation["operation_id"]))
                or operation,
                dispatch=dispatch["record"] if dispatch else {"payload": {}},
                machine=machine,
                external_generation=str(payload["edge_generation"]),
                contract=contract,
            ),
        }
        return self._control_response(response, payload)

    def edge_result(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token, require_contract=True)
        receipt_value = payload.get("receipt")
        receipt = dict(receipt_value) if isinstance(receipt_value, Mapping) else dict(payload)
        receipt_id = _required_text(receipt.get("receipt_id"), "receipt_id")
        combined = {**dict(payload), **receipt}
        operation, attempt, _ = self._require_attempt_fences(combined)
        operation_id = str(operation["operation_id"])
        attempt_id = str(attempt["attempt_id"])
        dispatch = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        if dispatch is None:
            raise KeyError(operation_id)
        expected_payload_hash = str(
            dispatch["record"].get("payload_hash")
            or operation.get("semantic_payload_hash")
            or ""
        )
        received_payload_hash = str(receipt.get("operation_payload_hash") or "")
        if expected_payload_hash and received_payload_hash != expected_payload_hash:
            raise HubStoreV2Conflict("operation_payload_hash_mismatch")

        domain_result, uncertain = self._receipt_domain_result(
            receipt, dispatch["record"]
        )
        current = self.store.get_operation(operation_id) or operation
        if current["state"] == "outcome_unknown" and not uncertain:
            current = self.broker.transition_operation(
                operation_id,
                expected_revision=int(current["revision"]),
                state="reconciling",
                principal_ref=str(current["principal_ref"]),
            ) or self.store.get_operation(operation_id) or current

        if uncertain:
            saved_operation = self._record_uncertain_receipt(
                current,
                attempt,
                combined=combined,
                domain_result=domain_result,
                error=str(receipt.get("error") or ""),
            )
        else:
            saved_operation = self.broker.finish_operation(
                operation_id,
                attempt_id,
                expected_operation_revision=int(current["revision"]),
                machine_id=str(payload["machine_id"]),
                edge_generation=self._generation_number(str(payload["edge_generation"])),
                contract_hash=self._requested_contract_hash(combined),
                fencing_token=int(combined["fencing_token"]),
                result=domain_result,
                principal_ref=str(current["principal_ref"]),
            )
            if saved_operation is None:
                raise HubStoreV2Conflict("stale_operation_revision")

        saved_attempt = self.store.get_attempt(attempt_id)
        if saved_attempt is None:
            raise RuntimeError("Attempt disappeared after result commit")
        if saved_attempt["state"] != "acknowledged":
            acknowledged = self.broker.acknowledge_result(
                operation_id,
                attempt_id,
                expected_revision=int(saved_attempt["revision"]),
                machine_id=str(payload["machine_id"]),
                edge_generation=self._generation_number(str(payload["edge_generation"])),
                contract_hash=self._requested_contract_hash(combined),
                fencing_token=int(combined["fencing_token"]),
                principal_ref=str(current["principal_ref"]),
            )
            if acknowledged is None:
                raise HubStoreV2Conflict("stale_attempt_revision")
        else:
            acknowledged = saved_attempt

        acknowledgement = {
            "receipt_id": receipt_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "fencing_token": int(combined["fencing_token"]),
            "edge_generation": str(payload["edge_generation"]),
        }
        self._record_receipt(
            acknowledgement,
            machine_id=str(payload["machine_id"]),
            contract_hash=self._requested_contract_hash(combined),
            operation_payload_hash=expected_payload_hash,
            result_hash=semantic_payload_hash(domain_result),
        )
        self._acknowledge_transient_payload(dispatch["record"])
        self._update_dispatch(
            operation_id,
            status="outcome_unknown" if uncertain else "complete",
            public_status=(
                "pending"
                if uncertain
                else self._operation_envelope(saved_operation)["status"]
            ),
        )
        if not uncertain:
            self._record_group_preflight_if_needed(
                operation_id, dispatch["record"], domain_result
            )
            self._record_group_preflight_invalidation_if_needed(
                operation_id, dispatch["record"], domain_result
            )
        return {
            "accepted": True,
            "operation": saved_operation,
            "attempt": self._external_attempt(
                acknowledged, external_generation=str(payload["edge_generation"])
            ),
            "receipt_acknowledgements": [acknowledgement],
        }

    def _record_group_preflight_invalidation_if_needed(
        self,
        operation_id: str,
        dispatch: Mapping[str, Any],
        domain_result: Mapping[str, Any],
    ) -> None:
        payload = dispatch.get("payload") if isinstance(dispatch.get("payload"), Mapping) else {}
        action = str(payload.get("action") or "")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), Mapping) else {}
        group_id = str(payload.get("work_group_id") or arguments.get("work_group_id") or "")
        reason = ""
        if action == "codex_worker_integrate" and domain_result.get("applied") is True:
            reason = "accepted_worker_integration_changed_base_checkout"
        elif action == "codex_worker_start" and str(
            arguments.get("workspace_mode") or "isolated_write"
        ) == "shared_write" and domain_result.get("accepted") is not False:
            reason = "shared_write_worker_can_change_base_checkout"
        if reason:
            self.runtime.mark_group_preflight_refresh_required(
                work_group_id=group_id,
                reason=reason,
                source_operation_id=operation_id,
            )

    def edge_outbox_ack(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token, require_contract=True)
        receipt_ids = payload.get("receipt_ids")
        if not isinstance(receipt_ids, list):
            raise ValueError("receipt_ids must be an array")
        acknowledged: list[dict[str, Any]] = []
        for value in receipt_ids:
            receipt_id = str(value)
            entity = self.store.get_entity(EDGE_RECEIPT_ENTITY, receipt_id)
            if entity is None:
                continue
            record = deepcopy(entity["record"])
            if (
                record.get("machine_id") == payload["machine_id"]
                and record.get("edge_generation") == payload["edge_generation"]
            ):
                if record.get("status") != "retired":
                    record.update(status="retired", retired_at=time.time())
                    try:
                        saved = self.store.put_entity(
                            EDGE_RECEIPT_ENTITY,
                            receipt_id,
                            record,
                            expected_revision=int(entity["revision"]),
                        )
                        record = saved["record"]
                    except HubStoreV2Conflict:
                        current = self.store.get_entity(EDGE_RECEIPT_ENTITY, receipt_id)
                        if current is None or current["record"].get("status") != "retired":
                            raise
                        record = current["record"]
                acknowledged.append(self._public_receipt(record))
        return {
            "accepted": len(acknowledged) == len(receipt_ids),
            "acknowledged_receipts": acknowledged,
            "receipt_acknowledgements": acknowledged,
        }

    def edge_reconcile(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        # Reconciliation may refer to a historical attempt contract after an
        # Edge or Hub upgrade. Machine generation/token authentication plus the
        # exact durable attempt fences below are the authority here.
        machine = self._authenticate(payload, token, require_contract=False)
        operation_id = str(payload.get("operation_id") or "")
        self.broker.expire_leases(operation_id=operation_id)
        operation, attempt, contract = self._require_attempt_fences(payload)
        local_value = payload.get("local_recovery")
        local = dict(local_value) if isinstance(local_value, Mapping) else {}
        receipt = local.get("receipt")
        if isinstance(receipt, Mapping) and receipt.get("result") is not None:
            return self.edge_result(
                {
                    **dict(payload),
                    **self._external_fences(attempt, payload),
                    "receipt": {
                        **dict(receipt),
                        **self._external_fences(attempt, payload),
                    },
                },
                token=token,
            )

        if attempt["state"] == "lease_expired":
            saved = self.broker.begin_reconciliation(
                str(operation["operation_id"]),
                str(attempt["attempt_id"]),
                expected_revision=int(attempt["revision"]),
                machine_id=str(payload["machine_id"]),
                edge_generation=self._generation_number(str(payload["edge_generation"])),
                contract_hash=self._requested_contract_hash(payload),
                fencing_token=int(payload["fencing_token"]),
                principal_ref=str(operation["principal_ref"]),
            )
            if saved is None:
                raise HubStoreV2Conflict("stale_attempt_revision")
            attempt = saved
            operation = self.store.get_operation(operation_id) or operation
        elif operation["state"] == "outcome_unknown":
            operation = self.broker.transition_operation(
                operation_id,
                expected_revision=int(operation["revision"]),
                state="reconciling",
                principal_ref=str(operation["principal_ref"]),
            ) or self.store.get_operation(operation_id) or operation

        disposition = str(payload.get("disposition") or "")
        if not disposition and local.get("found") is False and attempt["state"] == "reconciling":
            disposition = "manual_recovery"
        if disposition in {"retryable", "manual_recovery"} and attempt["state"] == "reconciling":
            completed = self.broker.complete_reconciliation(
                operation_id,
                str(attempt["attempt_id"]),
                disposition=disposition,
                expected_revision=int(attempt["revision"]),
                machine_id=str(payload["machine_id"]),
                edge_generation=self._generation_number(str(payload["edge_generation"])),
                contract_hash=self._requested_contract_hash(payload),
                fencing_token=int(payload["fencing_token"]),
                principal_ref=str(operation["principal_ref"]),
            )
            if completed is None:
                raise HubStoreV2Conflict("stale_attempt_revision")
            attempt = completed

        dispatch = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        wire = self._wire_attempt(
            attempt,
            operation=self.store.get_operation(operation_id) or operation,
            dispatch=dispatch["record"] if dispatch else {"payload": {}},
            machine=machine,
            external_generation=str(payload["edge_generation"]),
            contract=contract,
        )
        response: dict[str, Any] = {
            "accepted": True,
            "found": local.get("found") is not False,
            "attempt": wire,
        }
        if disposition:
            response["disposition"] = disposition
        if (
            str(local.get("recovery_action") or "") == "execute_intent"
            and attempt["state"] in _RESUMABLE_ATTEMPT_STATES
        ):
            response["resume_attempts"] = [wire]
        return self._control_response(response, payload)

    # Natural spellings accepted by server_v2 endpoint discovery.
    enroll_edge = edge_enroll
    enroll_machine = edge_enroll
    heartbeat_edge = edge_heartbeat
    claim_edge_attempt = edge_claim
    claim_attempt = edge_claim
    renew_edge_lease = edge_lease
    renew_lease = edge_lease
    submit_edge_result = edge_result
    finish_edge_attempt = edge_result
    acknowledge_edge_outbox = edge_outbox_ack
    publish_edge_projection = edge_projection
    reconcile_edge_attempt = edge_reconcile

    # -- Durable transport helpers ------------------------------------

    def _require_bound(
        self,
    ) -> tuple[Any, HubStoreV2, OperationBroker, HubRuntimeV2]:
        if (
            self._app is None
            or self._store is None
            or self._broker is None
            or self._runtime is None
        ):
            raise RuntimeError("Hub pull transport is not bound to HubAppV2")
        return self._app, self._store, self._broker, self._runtime

    def _persist_dispatch(
        self,
        operation: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        operation_id = str(operation["operation_id"])
        payload_value = deepcopy(dict(payload))
        payload_hash = semantic_payload_hash(payload_value)
        machine, machine_id, edge_generation = self._dispatch_machine(payload_value)
        capabilities = _mapping(machine.get("capabilities"))
        contract_hash = str(
            payload_value.get("required_contract_hash")
            or capabilities.get("contract_hash")
            or ""
        )
        if not contract_hash or contract_hash != HUB_V2_CONTRACT_HASH:
            raise HubStoreV2Conflict("edge_contract_mismatch")
        action = _required_text(payload_value.get("action"), "action")
        execution_action = self._execution_action(action)
        action_version = self._action_capability_version(
            capabilities, execution_action
        )
        if not action_version:
            raise HubStoreV2Conflict("edge_action_capability_mismatch")

        existing = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        if existing is not None:
            old_hash = str(existing["record"].get("payload_hash") or "")
            if old_hash and old_hash != payload_hash:
                raise HubStoreV2Conflict("operation_dispatch_payload_conflict")
            record = deepcopy(existing["record"])
            expected_revision = int(existing["revision"])
        else:
            record = {
                "operation_id": operation_id,
                "created_at": operation.get("created_at") or time.time(),
            }
            expected_revision = 0
        record.update(
            {
                "operation_id": operation_id,
                "action": action,
                "execution_action": execution_action,
                "payload": payload_value,
                "payload_hash": payload_hash,
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "required_contract_hash": contract_hash,
                "required_action_capability_version": action_version,
                "status": "offered",
                "updated_at": time.time(),
            }
        )
        saved = self.store.put_entity(
            EDGE_DISPATCH_ENTITY,
            operation_id,
            record,
            expected_revision=expected_revision,
        )
        return saved["record"]

    def _offer_dispatch(
        self,
        operation: Mapping[str, Any],
        dispatch: Mapping[str, Any],
    ) -> dict[str, Any]:
        attempt = self.broker.offer_attempt(
            str(operation["operation_id"]),
            machine_id=str(dispatch["machine_id"]),
            edge_generation=self._generation_number(str(dispatch["edge_generation"])),
            required_contract_hash=str(dispatch["required_contract_hash"]),
            principal_ref=str(operation["principal_ref"]),
        )
        self._update_dispatch(
            str(operation["operation_id"]),
            status="offered",
            attempt_id=str(attempt["attempt_id"]),
            fencing_token=int(attempt["fencing_token"]),
        )
        return attempt

    def _dispatch_machine(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str, str]:
        target = _mapping(payload.get("target"))
        machine_id = _required_text(
            payload.get("machine_id") or target.get("machine_id"), "machine_id"
        )
        edge_generation = _required_text(
            payload.get("edge_generation") or target.get("edge_generation"),
            "edge_generation",
        )
        entity = self.store.get_entity(MACHINE_ENTITY, machine_id)
        if entity is None:
            raise KeyError(machine_id)
        machine = deepcopy(entity["record"])
        if machine.get("retired_at"):
            raise HubStoreV2StateError("Edge node is retired")
        if machine.get("edge_generation") != edge_generation:
            raise HubStoreV2Conflict("edge_generation_mismatch")
        return machine, machine_id, edge_generation

    async def _wait_for_semantic_result(
        self,
        operation_id: str,
        *,
        principal_ref: str,
    ) -> dict[str, Any]:
        deadline = self._monotonic() + self.semantic_wait_seconds
        cursor = self._latest_event_revision(operation_id)
        while True:
            operation = self.store.get_operation(operation_id)
            if operation is None or operation.get("principal_ref") != principal_ref:
                return public_envelope(
                    "not_found", result={"reason": "operation_not_found"}
                )
            if operation["state"] in TERMINAL_OPERATION_STATES:
                return self._operation_envelope(operation)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._operation_envelope(operation)
            await self.broker.wait_for_event_revision(
                operation_id,
                after_revision=cursor,
                timeout_seconds=remaining,
                principal_ref=principal_ref,
            )
            cursor = self._latest_event_revision(operation_id)

    def _latest_event_revision(self, operation_id: str) -> int:
        row = self.store.connection.execute(
            "SELECT COALESCE(MAX(event_revision), 0) FROM events WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return int(row[0])

    def _operation_envelope(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        normalized = operation.get("result")
        if (
            isinstance(normalized, Mapping)
            and normalized.get("status") in PUBLIC_STATUSES
            and isinstance(normalized.get("result"), Mapping)
        ):
            envelope = public_envelope(
                str(normalized["status"]),
                result=normalized.get("result"),
                operation=self._public_operation(operation),
                warnings=list(normalized.get("warnings") or []),
                next_actions=list(normalized.get("next_actions") or []),
            )
            return envelope
        status = {
            "succeeded": "ok",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "blocked",
        }.get(str(operation.get("state") or ""), "pending")
        result = {}
        next_actions: list[Any] = []
        if status == "pending":
            result = {"reason": "edge_result_pending"}
            next_actions = [
                {
                    "tool": "patchbay_operation_status",
                    "arguments": {"operation_id": operation["operation_id"]},
                }
            ]
        return public_envelope(
            status,
            result=result,
            operation=self._public_operation(operation),
            next_actions=next_actions,
        )

    @staticmethod
    def _public_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": str(operation.get("operation_id") or ""),
            "parent_operation_id": str(operation.get("parent_operation_id") or ""),
            "item_id": str(operation.get("item_id") or ""),
            "tool_name": str(operation.get("tool") or ""),
            "state": str(operation.get("state") or ""),
            "revision": int(operation.get("revision") or 0),
            "created_at": operation.get("created_at"),
            "updated_at": operation.get("updated_at"),
        }

    @staticmethod
    def _logical_target(action: str, target: Mapping[str, Any]) -> str:
        for field in (
            "fleet_worker_ref",
            "edge_worker_id",
            "workspace_projection_ref",
            "workspace_ref",
            "repo_path",
            "work_group_id",
            "machine_id",
        ):
            if target.get(field):
                return f"{action}:{field}:{target[field]}"
        return f"{action}:direct"

    def _is_group_readiness_preflight(
        self, operation_id: str, payload: Mapping[str, Any]
    ) -> bool:
        if payload.get("action") != "patchbay_edge_preflight":
            return False
        group_id = str(payload.get("work_group_id") or "")
        group = self.store.get_entity(WORK_GROUP_ENTITY, group_id) if group_id else None
        return bool(
            group
            and group["record"].get("readiness", {}).get("operation_id")
            == operation_id
        )

    def _authenticate(
        self,
        payload: Mapping[str, Any],
        token: str,
        *,
        require_contract: bool,
    ) -> dict[str, Any]:
        machine_id = _required_text(payload.get("machine_id"), "machine_id")
        edge_generation = _required_text(
            payload.get("edge_generation"), "edge_generation"
        )
        machine = self.runtime.authenticate_machine(
            machine_id, token, edge_generation=edge_generation
        )
        nested = payload.get("contract")
        if isinstance(nested, Mapping):
            nested_generation = str(nested.get("edge_generation") or "")
            if nested_generation and nested_generation != edge_generation:
                raise HubStoreV2Conflict("attempt_edge_generation_mismatch")
        if require_contract:
            requested = self._requested_contract_hash(payload)
            advertised = str(_mapping(machine.get("capabilities")).get("contract_hash") or "")
            # During a rolling upgrade, an older Edge may still own attempts
            # created under its previously advertised contract. Placement
            # already prevents new work on an incompatible Edge, while the
            # attempt fences below bind claim/result to the stored contract.
            if requested != advertised:
                raise HubStoreV2Conflict("attempt_contract_hash_mismatch")
        return machine

    @staticmethod
    def _requested_contract_hash(payload: Mapping[str, Any]) -> str:
        nested = _mapping(payload.get("contract"))
        values = {
            str(value).strip()
            for value in (
                payload.get("contract_hash"),
                payload.get("required_contract_hash"),
                nested.get("contract_hash"),
            )
            if str(value or "").strip()
        }
        if not values:
            raise ValueError("contract_hash is required")
        if len(values) != 1:
            raise HubStoreV2Conflict("attempt_contract_hash_mismatch")
        return values.pop()

    @staticmethod
    def _generation_number(edge_generation: str) -> int:
        digest = hashlib.sha256(edge_generation.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)

    def _dispatches_for_machine(
        self, machine_id: str, edge_generation: str
    ) -> list[dict[str, Any]]:
        values = [
            entity
            for entity in self.store.list_entities(EDGE_DISPATCH_ENTITY)
            if entity["record"].get("machine_id") == machine_id
            and entity["record"].get("edge_generation") == edge_generation
        ]
        values.sort(
            key=lambda item: (
                float(item["record"].get("created_at") or 0),
                str(item["entity_id"]),
            )
        )
        return values

    def _active_attempt(self, operation_id: str) -> dict[str, Any] | None:
        row = self.store.connection.execute(
            """
            SELECT attempt_id FROM attempts
            WHERE operation_id = ?
            ORDER BY fencing_token DESC, created_at DESC LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        return self.store.get_attempt(str(row["attempt_id"])) if row else None

    def _dispatch_is_action_compatible(
        self,
        dispatch: Mapping[str, Any],
        machine: Mapping[str, Any],
    ) -> bool:
        capabilities = _mapping(machine.get("capabilities"))
        action = str(
            dispatch.get("execution_action")
            or self._execution_action(str(dispatch.get("action") or ""))
        )
        actual = self._action_capability_version(capabilities, action)
        required = str(dispatch.get("required_action_capability_version") or "")
        return bool(actual and required and actual == required)

    @staticmethod
    def _action_capability_version(
        capabilities: Mapping[str, Any], action: str
    ) -> str:
        versions = capabilities.get("action_capabilities")
        if not isinstance(versions, Mapping):
            versions = capabilities.get("action_capability_versions")
        return str(versions.get(action) or "") if isinstance(versions, Mapping) else ""

    def _require_attempt_fences(
        self, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        operation_id = _required_text(payload.get("operation_id"), "operation_id")
        attempt_id = _required_text(payload.get("attempt_id"), "attempt_id")
        operation = self.store.get_operation(operation_id)
        attempt = self.store.get_attempt(attempt_id)
        contract_entity = self.store.get_entity(
            ATTEMPT_CONTRACT_ENTITY_TYPE, attempt_id
        )
        if operation is None or attempt is None or contract_entity is None:
            raise KeyError(attempt_id)
        contract = deepcopy(contract_entity["record"])
        expected = {
            "operation_id": operation_id,
            "machine_id": str(payload.get("machine_id") or ""),
            "edge_generation": self._generation_number(
                str(payload.get("edge_generation") or "")
            ),
            "required_contract_hash": self._requested_contract_hash(payload),
            "fencing_token": int(payload.get("fencing_token") or 0),
        }
        actual = {
            "operation_id": str(attempt.get("operation_id") or ""),
            "machine_id": str(attempt.get("machine_id") or ""),
            "edge_generation": int(attempt.get("edge_generation") or -1),
            "required_contract_hash": str(
                contract.get("required_contract_hash") or ""
            ),
            "fencing_token": int(attempt.get("fencing_token") or 0),
        }
        for field, value in expected.items():
            if actual[field] != value:
                raise HubStoreV2Conflict(f"attempt_{field}_mismatch")
        return operation, attempt, contract

    def _wire_attempt(
        self,
        attempt: Mapping[str, Any],
        *,
        operation: Mapping[str, Any],
        dispatch: Mapping[str, Any],
        machine: Mapping[str, Any],
        external_generation: str,
        contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        dispatch_payload = _mapping(dispatch.get("payload"))
        dispatch_payload = self._hydrate_transient_payload(dispatch_payload)
        arguments = dispatch_payload.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = {
                key: deepcopy(value)
                for key, value in dispatch_payload.items()
                if key
                not in {"action", "context", "target", "machine_id", "edge_generation"}
            }
        dispatch_action = str(
            dispatch.get("action") or dispatch_payload.get("action") or ""
        )
        action = str(
            dispatch.get("execution_action")
            or self._execution_action(dispatch_action)
        )
        if dispatch_action == _PREFLIGHT_ACTION:
            arguments = {
                "repo": str(
                    arguments.get("repo")
                    or arguments.get("repo_path")
                    or dispatch_payload.get("repo_path")
                    or ""
                ),
                "include_tree": False,
                "include_skills": False,
            }
        capabilities = _mapping(machine.get("capabilities"))
        action_version = self._action_capability_version(capabilities, action)
        required_contract = str(
            contract.get("required_contract_hash")
            or dispatch.get("required_contract_hash")
            or ""
        )
        requirements = {
            "protocol_version": str(capabilities.get("protocol_version") or ""),
            "contract_version": str(capabilities.get("contract_version") or ""),
            "manifest_hash": str(capabilities.get("manifest_hash") or ""),
            "schema_hash": str(capabilities.get("schema_hash") or ""),
            "contract_hash": required_contract,
            "edge_generation": external_generation,
            "action_capabilities": {action: action_version},
        }
        wire = {
            **self._external_attempt(
                attempt, external_generation=external_generation
            ),
            "machine_id": str(machine["machine_id"]),
            "required_edge_generation": external_generation,
            "required_contract_hash": required_contract,
            "required_action_capability_version": action_version,
            "action": action,
            "arguments": deepcopy(dict(arguments)),
            "payload": dispatch_payload,
            "target": _mapping(dispatch_payload.get("target")),
            "context": _mapping(dispatch_payload.get("context")),
            "idempotency_key": str(operation.get("idempotency_key") or ""),
            "operation_payload_hash": str(dispatch.get("payload_hash") or ""),
            "requirements": requirements,
            "operation_revision": int(operation.get("revision") or 0),
        }
        tool_name = str(operation.get("tool") or "")
        if HUB_V2_ACTION_MAP.get(tool_name) == action:
            wire["tool_name"] = tool_name
        for field in ("parent_operation_id", "item_id", "work_group_id", "lane_id"):
            value = operation.get(field) or dispatch_payload.get(field)
            if value:
                wire[field] = value
        return wire

    @staticmethod
    def _external_attempt(
        attempt: Mapping[str, Any], *, external_generation: str
    ) -> dict[str, Any]:
        value = deepcopy(dict(attempt))
        value["edge_generation"] = external_generation
        return value

    def _receipt_domain_result(
        self,
        receipt: Mapping[str, Any],
        dispatch: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        raw = receipt.get("result")
        if raw is not None and not isinstance(raw, Mapping):
            raise ValueError("receipt.result must be an object")
        result = deepcopy(dict(raw or {}))
        status = str(result.get("status") or "")
        if status in PUBLIC_STATUSES and isinstance(result.get("result"), Mapping):
            inner = deepcopy(dict(result.get("result") or {}))
            if status == "partial":
                inner["partial"] = True
            elif status == "blocked":
                inner.setdefault("accepted", False)
                inner.setdefault("reason", "domain_blocked")
            elif status == "failed":
                inner.setdefault("failed", True)
            elif status == "not_found":
                inner.setdefault("found", False)
            result = inner

        outcome = str(receipt.get("outcome") or "")
        uncertain = bool(receipt.get("uncertain")) or outcome == "outcome_unknown" or status == "pending"
        if outcome == "blocked":
            result.setdefault("accepted", False)
            result.setdefault("reason", "domain_blocked")
        elif outcome == "failed":
            result.setdefault("failed", True)
        if dispatch.get("action") == _PREFLIGHT_ACTION:
            result = self._normalize_preflight_result(result, dispatch)
        if dispatch.get("action") == _PREFLIGHT_ACTION and result.get("ok") is False:
            result.setdefault("accepted", False)
            result.setdefault("reason", "workspace_preflight_failed")
        if uncertain:
            result.setdefault("reason", "outcome_unknown")
        return result, uncertain

    @staticmethod
    def _execution_action(action: str) -> str:
        return _PREFLIGHT_EXECUTION_ACTION if action == _PREFLIGHT_ACTION else action

    def _normalize_preflight_result(
        self,
        result: Mapping[str, Any],
        dispatch: Mapping[str, Any],
    ) -> dict[str, Any]:
        facts = deepcopy(dict(result))
        payload = _mapping(dispatch.get("payload"))
        machine = self.store.get_entity(
            MACHINE_ENTITY, str(dispatch.get("machine_id") or "")
        )
        machine_record = machine["record"] if machine else {}
        resources = _mapping(machine_record.get("resource_status"))
        capabilities = _mapping(machine_record.get("capabilities"))
        requested = str(payload.get("repo_path") or "")
        resolved = str(
            facts.get("repo_resolved")
            or facts.get("resolved_repo_path")
            or facts.get("root")
            or facts.get("path")
            or requested
        )
        exists = facts.get("repo_exists")
        if exists is None:
            exists = facts.get("exists")
        if exists is None:
            exists = bool(resolved)
        git = facts.get("git")
        git_mapping = git if isinstance(git, Mapping) else {}
        normalized = {
            **facts,
            "ok": bool(facts.get("ok", exists)),
            "repo_requested": str(facts.get("repo_requested") or requested),
            "repo_resolved": resolved,
            "repo_exists": bool(exists),
            "git_repo": bool(
                facts.get("git_repo")
                if "git_repo" in facts
                else git_mapping or git is True
            ),
            "branch": str(facts.get("branch") or git_mapping.get("branch") or ""),
            "head": str(
                facts.get("head")
                or git_mapping.get("head")
                or git_mapping.get("commit")
                or ""
            ),
            "dirty_status_summary": deepcopy(
                facts.get("dirty_status_summary")
                or git_mapping.get("status")
                or {}
            ),
            "disk_free_bytes": facts.get(
                "disk_free_bytes", resources.get("disk_free_bytes", -1)
            ),
            "active_workers": facts.get(
                "active_workers", resources.get("active_workers", 0)
            ),
            "max_concurrent_jobs": facts.get(
                "max_concurrent_jobs",
                resources.get(
                    "max_concurrent_jobs", capabilities.get("max_concurrent_jobs", 0)
                ),
            ),
            "free_worker_slots": facts.get(
                "free_worker_slots",
                resources.get(
                    "free_worker_slots", capabilities.get("max_concurrent_jobs", 1)
                ),
            ),
            "queue_enabled": bool(
                facts.get(
                    "queue_enabled",
                    resources.get(
                        "queue_enabled", capabilities.get("queue_enabled", False)
                    ),
                )
            ),
            "unintegrated_worker_warnings": list(
                facts.get("unintegrated_worker_warnings") or []
            ),
        }
        return normalized

    def _record_uncertain_receipt(
        self,
        operation: Mapping[str, Any],
        attempt: Mapping[str, Any],
        *,
        combined: Mapping[str, Any],
        domain_result: Mapping[str, Any],
        error: str,
    ) -> dict[str, Any]:
        operation_id = str(operation["operation_id"])
        attempt_id = str(attempt["attempt_id"])
        current_attempt = self.store.get_attempt(attempt_id) or deepcopy(dict(attempt))
        pending = public_envelope(
            "pending",
            result={**deepcopy(dict(domain_result)), "error": error} if error else domain_result,
        )
        if current_attempt["state"] in {"executing", "effect_recorded", "reconciling"}:
            saved_attempt = self.broker.transition_attempt(
                operation_id,
                attempt_id,
                expected_revision=int(current_attempt["revision"]),
                machine_id=str(combined["machine_id"]),
                edge_generation=self._generation_number(str(combined["edge_generation"])),
                contract_hash=self._requested_contract_hash(combined),
                fencing_token=int(combined["fencing_token"]),
                state="result_ready",
                principal_ref=str(operation["principal_ref"]),
                result=pending,
            )
            if saved_attempt is None:
                raise HubStoreV2Conflict("stale_attempt_revision")
        current = self.store.get_operation(operation_id) or deepcopy(dict(operation))
        if current["state"] == "running":
            saved_operation = self.broker.transition_operation(
                operation_id,
                expected_revision=int(current["revision"]),
                state="outcome_unknown",
                principal_ref=str(current["principal_ref"]),
                result=pending,
                error={"reason": "edge_outcome_unknown", "message": error},
            )
            return saved_operation or self.store.get_operation(operation_id) or current
        return current

    def _record_receipt(
        self,
        acknowledgement: Mapping[str, Any],
        *,
        machine_id: str,
        contract_hash: str,
        operation_payload_hash: str,
        result_hash: str,
    ) -> None:
        receipt_id = str(acknowledgement["receipt_id"])
        record = {
            **deepcopy(dict(acknowledgement)),
            "machine_id": machine_id,
            "contract_hash": contract_hash,
            "operation_payload_hash": operation_payload_hash,
            "result_hash": result_hash,
            "status": "pending",
            "created_at": time.time(),
        }
        existing = self.store.get_entity(EDGE_RECEIPT_ENTITY, receipt_id)
        if existing is not None:
            immutable_keys = (
                "receipt_id",
                "operation_id",
                "attempt_id",
                "fencing_token",
                "edge_generation",
                "machine_id",
                "contract_hash",
                "operation_payload_hash",
                "result_hash",
            )
            if any(
                existing["record"].get(key) != record.get(key)
                for key in immutable_keys
            ):
                raise HubStoreV2Conflict("receipt_identity_conflict")
            return
        self.store.put_entity(
            EDGE_RECEIPT_ENTITY, receipt_id, record, expected_revision=0
        )

    def _record_group_preflight_if_needed(
        self,
        operation_id: str,
        dispatch: Mapping[str, Any],
        domain_result: Mapping[str, Any],
    ) -> None:
        payload = _mapping(dispatch.get("payload"))
        if payload.get("action") != "patchbay_edge_preflight":
            return
        group_id = str(payload.get("work_group_id") or "")
        group = self.store.get_entity(WORK_GROUP_ENTITY, group_id) if group_id else None
        if (
            group is None
            or group["record"].get("readiness", {}).get("operation_id")
            != operation_id
        ):
            return
        self.runtime.record_preflight_result(
            work_group_id=group_id,
            operation_id=operation_id,
            result=domain_result,
        )

    def _update_dispatch(self, operation_id: str, **changes: Any) -> None:
        entity = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        if entity is None:
            return
        record = deepcopy(entity["record"])
        record.update(deepcopy(changes))
        record["updated_at"] = time.time()
        self.store.put_entity(
            EDGE_DISPATCH_ENTITY,
            operation_id,
            record,
            expected_revision=int(entity["revision"]),
        )

    def _control_response(
        self,
        response: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        value = deepcopy(dict(response))
        acknowledgements = self._receipt_acknowledgements(
            str(payload.get("machine_id") or ""),
            str(payload.get("edge_generation") or ""),
        )
        if acknowledgements:
            value["receipt_acknowledgements"] = acknowledgements
        reconciliation = self._reconciliation_requests(
            str(payload.get("machine_id") or ""),
            str(payload.get("edge_generation") or ""),
        )
        if reconciliation:
            value["reconciliation_requests"] = reconciliation
        return value

    def _receipt_acknowledgements(
        self, machine_id: str, edge_generation: str
    ) -> list[dict[str, Any]]:
        records = [
            entity["record"]
            for entity in self.store.list_entities(EDGE_RECEIPT_ENTITY)
            if entity["record"].get("machine_id") == machine_id
            and entity["record"].get("edge_generation") == edge_generation
            and entity["record"].get("status", "pending") != "retired"
        ]
        records.sort(
            key=lambda record: (
                float(record.get("created_at") or 0),
                str(record.get("receipt_id") or ""),
            )
        )
        return [
            self._public_receipt(record)
            for record in records[: self.receipt_ack_limit]
        ]

    def _hydrate_transient_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        hydrated = deepcopy(dict(payload))
        reference = hydrated.get(_TRANSIENT_PAYLOAD_KEY)
        if not isinstance(reference, Mapping):
            return hydrated
        payload_id = str(reference.get("payload_id") or "")
        metadata = self.store.get_payload_metadata(payload_id) if payload_id else None
        if metadata is None or metadata.get("status") not in {"ready", "acknowledged"}:
            raise HubStoreV2StateError("transient_payload_unavailable")
        arguments = hydrated.get("arguments")
        if not isinstance(arguments, dict):
            raise HubStoreV2StateError("transient_payload_arguments_missing")
        artifact = arguments.get("artifact_file")
        if not isinstance(artifact, dict):
            raise HubStoreV2StateError("transient_payload_artifact_missing")
        artifact["download_url"] = str(metadata["storage_ref"])
        hydrated.pop(_TRANSIENT_PAYLOAD_KEY, None)
        return hydrated

    def _acknowledge_transient_payload(self, dispatch: Mapping[str, Any]) -> None:
        payload = _mapping(dispatch.get("payload"))
        reference = payload.get(_TRANSIENT_PAYLOAD_KEY)
        if not isinstance(reference, Mapping):
            return
        payload_id = str(reference.get("payload_id") or "")
        metadata = self.store.get_payload_metadata(payload_id) if payload_id else None
        if metadata is None or metadata.get("status") != "ready":
            return
        operation = self.store.get_operation(str(metadata["operation_id"]))
        acknowledged = self.broker.acknowledge_payload(
            payload_id,
            expected_revision=int(metadata["revision"]),
            principal_ref=str((operation or {}).get("principal_ref") or ""),
        )
        latest = self.store.get_payload_metadata(payload_id)
        if acknowledged is None and (
            latest is None or latest.get("status") != "acknowledged"
        ):
            raise HubStoreV2Conflict("transient_payload_acknowledgement_conflict")

    def _reconciliation_requests(
        self, machine_id: str, edge_generation: str
    ) -> list[dict[str, Any]]:
        generation = self._generation_number(edge_generation) if edge_generation else -1
        rows = self.store.connection.execute(
            """
            SELECT operation_id, attempt_id, fencing_token, state
            FROM attempts
            WHERE machine_id = ? AND edge_generation = ?
              AND state IN ('lease_expired', 'reconciling')
            ORDER BY updated_at, attempt_id LIMIT 100
            """,
            (machine_id, generation),
        ).fetchall()
        return [
            {
                "operation_id": str(row["operation_id"]),
                "attempt_id": str(row["attempt_id"]),
                "fencing_token": int(row["fencing_token"]),
                "state": str(row["state"]),
            }
            for row in rows
        ]

    @staticmethod
    def _public_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(record[key])
            for key in (
                "receipt_id",
                "operation_id",
                "attempt_id",
                "fencing_token",
                "edge_generation",
            )
            if key in record
        }

    @staticmethod
    def _external_fences(
        attempt: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "operation_id": str(attempt["operation_id"]),
            "attempt_id": str(attempt["attempt_id"]),
            "fencing_token": int(attempt["fencing_token"]),
            "edge_generation": str(payload["edge_generation"]),
            "contract_hash": HubPullTransportBridgeV2._requested_contract_hash(
                payload
            ),
        }


def _mapping(value: Any) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _required_text(value: Any, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# Stable role-oriented aliases keep imports natural across composition layers.
HubPullTransportV2 = HubPullTransportBridgeV2
HubTransportBridgeV2 = HubPullTransportBridgeV2
HubEdgePullTransportV2 = HubPullTransportBridgeV2
PullTransportBridgeV2 = HubPullTransportBridgeV2


def create_production_hub_v2_app(
    config: Mapping[str, Any],
) -> HubPullTransportBridgeV2:
    """Compose the production Hub V2 graph behind the pull-transport facade."""

    from patchbay.hub.app_v2 import HubAppV2
    config_value = deepcopy(dict(config))
    hub = config_value.get("hub") if isinstance(config_value.get("hub"), Mapping) else {}
    bridge = HubPullTransportBridgeV2(
        semantic_wait_seconds=float(hub.get("semantic_wait_seconds") or DEFAULT_SEMANTIC_WAIT_SECONDS)
    )
    app = HubAppV2(
        config_value,
        edge_delivery=bridge,
    )
    bridge.bind(app)
    return bridge


__all__ = [
    "DEFAULT_SEMANTIC_WAIT_SECONDS",
    "EDGE_DISPATCH_ENTITY",
    "EDGE_RECEIPT_ENTITY",
    "HubEdgePullTransportV2",
    "HubPullTransportBridgeV2",
    "HubPullTransportV2",
    "HubTransportBridgeV2",
    "PullTransportBridgeV2",
    "create_production_hub_v2_app",
]
