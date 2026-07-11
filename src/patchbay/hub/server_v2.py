"""FastAPI transport for the opt-in PatchBay Hub V2 control plane.

This module deliberately has no import-time server or runtime singleton.
Callers may inject a ``HubV2App`` for tests; the default factory composes the
production pull-transport graph.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, MutableMapping, Protocol, runtime_checkable

import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from patchbay.auth import (
    AuthConfigurationError,
    AuthPolicy,
    auth_public_metadata,
    build_auth_policy,
    request_is_authorized,
)
from patchbay.connector.profiles import normalize_logging_paths
from patchbay.hub.protocol_v2 import (
    HUB_V2_PROTOCOL_METADATA,
    HubProtocolV2,
)
from patchbay.hub.broker import ATTEMPT_CONTRACT_ENTITY_TYPE
from patchbay.hub.store_v2 import HubStoreV2Conflict, HubStoreV2StateError
from patchbay.hub.tool_surface import HUB_V2_ACTION_MAP, HUB_V2_CONTRACT_HASH
from patchbay.protocol.context import RequestContext, make_client_ref, make_hashed_ref
from patchbay.security import internal_log_error, redact_sensitive_output

logger = logging.getLogger(__name__)

DEFAULT_MAX_REQUEST_BYTES = 1_048_576
DEFAULT_WORK_RUN_IDLE_SECONDS = 900
EDGE_V2_PREFIX = "/edge/v2"
EDGE_DISPATCH_ENTITY = "hub.edge_dispatch"
EDGE_RECEIPT_ENTITY = "hub.edge_receipt"


@runtime_checkable
class HubV2App(Protocol):
    """Minimum application boundary consumed by the HTTP transport."""

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]: ...


HubV2AppFactory = Callable[..., HubV2App]
HubV2ProtocolFactory = Callable[[HubV2App], HubProtocolV2]


class RequestBodyTooLarge(ValueError):
    """Raised before JSON parsing when the configured request bound is crossed."""


class EdgeRequestError(ValueError):
    """A caller-controlled Edge V2 transport request error."""


class StaleEdgeRequest(RuntimeError):
    """The application rejected a revision or immutable attempt fence."""


_EDGE_METHOD_NAMES: dict[str, tuple[str, ...]] = {
    "enroll": ("edge_enroll", "enroll_edge", "enroll_machine", "enroll"),
    "heartbeat": ("edge_heartbeat", "heartbeat_edge", "heartbeat"),
    "claim": ("edge_claim", "claim_edge_attempt", "claim_attempt", "claim"),
    "lease": ("edge_lease", "renew_edge_lease", "renew_lease", "lease"),
    "result": (
        "edge_result",
        "submit_edge_result",
        "finish_edge_attempt",
        "finish_attempt",
        "result",
    ),
    "outbox_ack": (
        "edge_outbox_ack",
        "acknowledge_edge_outbox",
        "acknowledge_outbox",
        "acknowledge_result",
        "outbox_ack",
    ),
    "projection": (
        "edge_projection",
        "publish_edge_projection",
        "record_edge_projection",
        "record_projection",
        "projection",
    ),
    "reconcile": (
        "edge_reconcile",
        "reconcile_edge_attempt",
        "reconcile_attempt",
        "reconcile",
    ),
}

_EDGE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "enroll": ("code", "machine_id"),
    "heartbeat": ("machine_id", "edge_generation", "projection_revision"),
    "claim": ("machine_id", "edge_generation", "contract_hash"),
    "lease": (
        "machine_id",
        "edge_generation",
        "operation_id",
        "attempt_id",
        "contract_hash",
        "fencing_token",
        "expected_revision",
    ),
    "result": ("machine_id", "edge_generation"),
    "outbox_ack": ("machine_id", "edge_generation"),
    "projection": ("machine_id", "edge_generation", "projection_revision"),
    "reconcile": ("machine_id", "edge_generation", "operation_id", "attempt_id"),
}

_ALLOWED_PUBLIC_CREDENTIALISH_FIELDS = frozenset(
    {
        "contract_hash",
        "fencing_token",
        "idempotency_key",
        "manifest_hash",
        "payload_hash",
        "preview_token",
        "preview_token_expires_at",
        "result_hash",
        "schema_hash",
        "semantic_payload_hash",
    }
)
_PRIVATE_PUBLIC_FIELDS = frozenset(
    {
        "authorization",
        "arguments_seen",
        "credential",
        "credentials",
        "machine_token",
        "node_token",
        "operation_payload",
        "password",
        "payload",
        "raw_payload",
        "request_payload",
        "secret",
        "storage_ref",
        "token_hash",
        "transient_payload",
    }
)
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{2,100}$")


def _default_config_path() -> Path:
    candidates = (
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parents[3] / "config.yaml",
    )
    return next((candidate for candidate in candidates if candidate.exists()), candidates[0])


def load_hub_v2_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load config for explicit V2 construction without creating a server."""

    env = environ if environ is not None else os.environ
    config_path = Path(path or env.get("PATCHBAY_CONFIG") or _default_config_path())
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError("PatchBay config must be a YAML object")
    normalize_logging_paths(config)
    return config


def _default_hub_v2_app_factory(config: Mapping[str, Any]) -> HubV2App:
    """Resolve the production domain composition lazily.

    Lazy import keeps the HTTP transport independently testable and gives tests
    a clean injection boundary.
    """

    try:
        transport_module = import_module("patchbay.hub.transport_v2")
        production_factory = getattr(
            transport_module, "create_production_hub_v2_app", None
        )
        if callable(production_factory):
            return _construct_hub_app(production_factory, config)
    except ModuleNotFoundError as error:
        if error.name != "patchbay.hub.transport_v2":
            raise

    try:
        module = import_module("patchbay.hub.app_v2")
    except ModuleNotFoundError as error:
        if error.name != "patchbay.hub.app_v2":
            raise
        raise RuntimeError(
            "Hub V2 requires an injected hub_app/hub_app_factory until patchbay.hub.app_v2 is available"
        ) from error
    for name in (
        "create_hub_app_v2",
        "create_hub_v2_app",
        "build_hub_v2_app",
        "create_app",
    ):
        factory = getattr(module, name, None)
        if callable(factory):
            return _construct_hub_app(factory, config)
    for name in ("HubAppV2", "HubV2App"):
        app_class = getattr(module, name, None)
        if callable(app_class):
            return _construct_hub_app(app_class, config)
    raise RuntimeError("patchbay.hub.app_v2 exposes neither HubAppV2 nor a Hub V2 app factory")


def _construct_hub_app(factory: Callable[..., HubV2App], config: Mapping[str, Any]) -> HubV2App:
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return factory(config)
    concrete = [
        parameter
        for parameter in parameters.values()
        if parameter.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    ]
    if not concrete:
        return factory()
    config_parameter = parameters.get("config")
    if config_parameter and config_parameter.kind is inspect.Parameter.KEYWORD_ONLY:
        return factory(config=config)
    if len(concrete) == 1 and concrete[0].kind is inspect.Parameter.KEYWORD_ONLY:
        return factory(**{concrete[0].name: config})
    return factory(config)


def _max_request_bytes(config: Mapping[str, Any], explicit: int | None) -> int:
    raw: Any = explicit
    if raw is None:
        server = config.get("server") if isinstance(config.get("server"), Mapping) else {}
        raw = server.get("max_request_bytes", DEFAULT_MAX_REQUEST_BYTES)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError) as error:
        raise ValueError("server.max_request_bytes must be an integer") from error


async def _read_limited_json(request: Request, *, limit: int) -> Any:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError as error:
            raise EdgeRequestError("Invalid Content-Length") from error
        if declared < 0:
            raise EdgeRequestError("Invalid Content-Length")
        if declared > limit:
            raise RequestBodyTooLarge

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise RequestBodyTooLarge
    try:
        return json.loads(
            body.decode("utf-8") or "{}",
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid JSON constant {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise EdgeRequestError("Parse error") from error


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "unauthorized", "message": "Unauthorized"}},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authorize_operator(request: Request, policy: AuthPolicy) -> JSONResponse | None:
    if request_is_authorized(policy, request.headers, request.query_params):
        return None
    logger.warning("Unauthorized Hub V2 operator request rejected: method=%s path=%s", request.method, request.url.path)
    return _unauthorized_response()


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization") or ""
    prefix, separator, token = authorization.partition(" ")
    if separator and prefix.casefold() == "bearer":
        return token.strip()
    return ""


def _resolve_principal_ref(app: Any, explicit: str) -> str:
    candidates: list[Any] = []
    candidates.extend(getattr(app, name, "") for name in ("principal_ref", "operator_principal_ref"))
    for owner in (getattr(app, "store", None), getattr(app, "runtime", None)):
        if owner is None:
            continue
        candidates.append(getattr(owner, "principal_ref", ""))
        nested_store = getattr(owner, "store", None)
        if nested_store is not None:
            candidates.append(getattr(nested_store, "principal_ref", ""))
    exposed = {str(value).strip() for value in candidates if str(value or "").strip()}
    requested = str(explicit or "").strip()
    if len(exposed) > 1:
        raise TypeError("HubV2App exposes conflicting operator principal references")
    principal = next(iter(exposed), requested)
    if requested and principal and requested != principal:
        raise TypeError("Explicit principal_ref does not match the persisted Hub operator principal")
    if not principal:
        raise TypeError("HubV2App must expose its persisted operator principal_ref")
    return principal


def _public_result(value: Any) -> Any:
    """Remove Edge credentials and raw transient payloads from MCP results."""

    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            normalized = name.casefold().replace("-", "_")
            credentialish = (
                normalized not in _ALLOWED_PUBLIC_CREDENTIALISH_FIELDS
                and (
                    normalized.endswith(("_key", "_token"))
                    or any(
                        marker in normalized
                        for marker in ("authorization", "credential", "password", "secret")
                    )
                )
            )
            raw_payload = normalized in {"payload", "payloads"} or normalized.endswith("_payload")
            if normalized in _PRIVATE_PUBLIC_FIELDS or credentialish or raw_payload:
                continue
            safe[name] = _public_result(child)
        return safe
    if isinstance(value, list):
        return [_public_result(item) for item in value]
    return redact_sensitive_output(value)


class _PublicToolHandler:
    def __init__(self, domain_app: HubV2App):
        self.domain_app = domain_app

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        result = self.domain_app.handle_tool_call(name, arguments, context=context)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise TypeError("HubV2App.handle_tool_call must return an object")
        return _public_result(deepcopy(dict(result)))


class _RuntimeEdgeController:
    """Edge HTTP application over the services exposed by ``HubAppV2``."""

    def __init__(self, domain_app: Any):
        candidate_runtime = getattr(domain_app, "runtime", None)
        self.runtime = candidate_runtime or (
            domain_app
            if callable(getattr(domain_app, "authenticate_machine", None))
            and callable(getattr(domain_app, "heartbeat", None))
            else None
        )
        self.broker = getattr(domain_app, "broker", None) or getattr(self.runtime, "broker", None)
        self.store = getattr(domain_app, "store", None) or getattr(self.runtime, "store", None)
        if self.runtime is None or self.broker is None or self.store is None:
            raise TypeError(
                "HubV2App must expose Edge methods or its runtime, broker, and store services"
            )

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
        return self.runtime.heartbeat(
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
            resource_status=(
                payload.get("resource_status")
                if isinstance(payload.get("resource_status"), Mapping)
                else None
            ),
        )

    def edge_projection(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token, require_contract=True)
        projection = payload.get("projection")
        if not isinstance(projection, Mapping):
            raise EdgeRequestError("projection must be an object")
        return self.runtime.heartbeat(
            machine_id=str(payload["machine_id"]),
            token=token,
            edge_generation=str(payload["edge_generation"]),
            projection_revision=int(payload["projection_revision"]),
            worker_projection=projection,
        )

    def edge_claim(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        machine = self._authenticate(payload, token, require_contract=True)
        machine_id = str(payload["machine_id"])
        edge_generation = str(payload["edge_generation"])
        generation_number = self._generation_number(edge_generation)
        contract_hash = self._requested_contract_hash(payload)
        try:
            maximum = max(1, min(int(payload.get("max_attempts") or 1), 64))
            available = max(0, int(payload.get("available_slots", maximum)))
        except (TypeError, ValueError) as error:
            raise EdgeRequestError("max_attempts and available_slots must be integers") from error
        maximum = min(maximum, available)
        if maximum < 1:
            return {"accepted": True, "attempt": None, "attempts": []}

        self.broker.expire_leases()
        claimed: list[dict[str, Any]] = []
        dispatches = sorted(
            self.store.list_entities(EDGE_DISPATCH_ENTITY),
            key=lambda item: (
                float(item["record"].get("created_at") or 0),
                str(item["entity_id"]),
            ),
        )
        for entity in dispatches:
            if len(claimed) >= maximum:
                break
            operation_id = str(entity["entity_id"])
            operation = self.store.get_operation(operation_id)
            if operation is None or operation.get("state") not in {"dispatchable", "running"}:
                continue
            dispatch_payload = self._dispatch_payload(entity)
            target = (
                dispatch_payload.get("target")
                if isinstance(dispatch_payload.get("target"), Mapping)
                else {}
            )
            target_machine = str(
                dispatch_payload.get("machine_id") or target.get("machine_id") or ""
            )
            target_generation = str(
                dispatch_payload.get("edge_generation")
                or target.get("edge_generation")
                or ""
            )
            if target_machine != machine_id or target_generation != edge_generation:
                continue
            offered = self.broker.offer_attempt(
                operation_id,
                machine_id=machine_id,
                edge_generation=generation_number,
                required_contract_hash=contract_hash,
                principal_ref=str(operation["principal_ref"]),
            )
            attempt = self.broker.claim_attempt(
                operation_id,
                str(offered["attempt_id"]),
                machine_id=machine_id,
                edge_generation=generation_number,
                contract_hash=contract_hash,
                fencing_token=int(offered["fencing_token"]),
                principal_ref=str(operation["principal_ref"]),
            )
            claimed.append(
                self._wire_attempt(
                    attempt,
                    operation=operation,
                    dispatch=entity["record"],
                    machine=machine,
                    external_generation=edge_generation,
                )
            )
        return {
            "accepted": True,
            "attempt": claimed[0] if claimed else None,
            "attempts": claimed,
        }

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
            raise StaleEdgeRequest("stale_attempt_revision")
        dispatch = self.store.get_entity(EDGE_DISPATCH_ENTITY, str(operation["operation_id"]))
        return {
            "accepted": True,
            "attempt": self._wire_attempt(
                saved,
                operation=operation,
                dispatch=dispatch["record"] if dispatch else {"payload": {}},
                machine=machine,
                external_generation=str(payload["edge_generation"]),
                contract=contract,
            ),
        }

    def edge_result(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token, require_contract=True)
        receipt = payload.get("receipt")
        receipt = dict(receipt) if isinstance(receipt, Mapping) else dict(payload)
        receipt_id = str(receipt.get("receipt_id") or "").strip()
        if not receipt_id:
            raise EdgeRequestError("receipt_id is required")
        combined = {**dict(payload), **receipt}
        operation, attempt, _ = self._require_attempt_fences(combined)
        operation_id = str(operation["operation_id"])
        attempt_id = str(attempt["attempt_id"])
        dispatch = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        expected_payload_hash = str(
            (dispatch or {}).get("record", {}).get("payload_hash")
            or operation.get("semantic_payload_hash")
            or ""
        )
        received_payload_hash = str(receipt.get("operation_payload_hash") or "").strip()
        if expected_payload_hash and received_payload_hash != expected_payload_hash:
            raise HubStoreV2Conflict("operation_payload_hash_mismatch")
        result = receipt.get("result")
        if result is not None and not isinstance(result, Mapping):
            raise EdgeRequestError("receipt.result must be an object")
        saved_operation = self.broker.finish_operation(
            operation_id,
            attempt_id,
            expected_operation_revision=int(operation["revision"]),
            machine_id=str(payload["machine_id"]),
            edge_generation=self._generation_number(str(payload["edge_generation"])),
            contract_hash=self._requested_contract_hash(combined),
            fencing_token=int(combined["fencing_token"]),
            result=result,
            transport_error=str(receipt.get("error") or ""),
            principal_ref=str(operation["principal_ref"]),
        )
        if saved_operation is None:
            raise StaleEdgeRequest("stale_operation_revision")
        saved_attempt = self.store.get_attempt(attempt_id)
        if saved_attempt is None:
            raise RuntimeError("Attempt disappeared after result commit")
        acknowledged = self.broker.acknowledge_result(
            operation_id,
            attempt_id,
            expected_revision=int(saved_attempt["revision"]),
            machine_id=str(payload["machine_id"]),
            edge_generation=self._generation_number(str(payload["edge_generation"])),
            contract_hash=self._requested_contract_hash(combined),
            fencing_token=int(combined["fencing_token"]),
            principal_ref=str(operation["principal_ref"]),
        )
        if acknowledged is None:
            raise StaleEdgeRequest("stale_attempt_revision")

        acknowledgement = {
            "receipt_id": receipt_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "fencing_token": int(combined["fencing_token"]),
            "edge_generation": str(payload["edge_generation"]),
        }
        if receipt_id:
            self._record_receipt(acknowledgement, machine_id=str(payload["machine_id"]))
        self._apply_dispatch_result(operation_id, receipt.get("result"))
        return {
            "accepted": True,
            "operation": saved_operation,
            "attempt": self._external_attempt(
                acknowledged,
                external_generation=str(payload["edge_generation"]),
            ),
            "receipt_acknowledgements": [acknowledgement] if receipt_id else [],
        }

    def edge_outbox_ack(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token, require_contract=True)
        receipt_ids = payload.get("receipt_ids")
        if not isinstance(receipt_ids, list):
            raise EdgeRequestError("receipt_ids must be an array")
        acknowledged: list[dict[str, Any]] = []
        for receipt_id in receipt_ids:
            entity = self.store.get_entity(EDGE_RECEIPT_ENTITY, str(receipt_id))
            if entity is None:
                continue
            record = entity["record"]
            if (
                record.get("machine_id") == payload["machine_id"]
                and record.get("edge_generation") == payload["edge_generation"]
            ):
                acknowledged.append(
                    {key: deepcopy(value) for key, value in record.items() if key != "machine_id"}
                )
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
        # Historical reconciliation is fenced by the stored attempt contract,
        # so it must survive a later advertised-contract upgrade.
        machine = self._authenticate(payload, token, require_contract=False)
        self.broker.expire_leases(operation_id=str(payload["operation_id"]))
        operation, attempt, contract = self._require_attempt_fences(payload)
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
                raise StaleEdgeRequest("stale_attempt_revision")
            attempt = saved

        local = payload.get("local_recovery")
        local = local if isinstance(local, Mapping) else {}
        receipt = local.get("receipt")
        if isinstance(receipt, Mapping) and receipt.get("result") is not None:
            return self.edge_result(
                {
                    **dict(payload),
                    "receipt": {**dict(receipt), **self._external_fences(attempt, payload)},
                },
                token=token,
            )
        dispatch = self.store.get_entity(EDGE_DISPATCH_ENTITY, str(operation["operation_id"]))
        return {
            "accepted": True,
            "found": True,
            "attempt": self._wire_attempt(
                attempt,
                operation=operation,
                dispatch=dispatch["record"] if dispatch else {"payload": {}},
                machine=machine,
                external_generation=str(payload["edge_generation"]),
                contract=contract,
            ),
        }

    def _authenticate(
        self,
        payload: Mapping[str, Any],
        token: str,
        *,
        require_contract: bool,
    ) -> dict[str, Any]:
        nested_contract = payload.get("contract")
        if isinstance(nested_contract, Mapping):
            nested_generation = str(nested_contract.get("edge_generation") or "").strip()
            if nested_generation and nested_generation != str(payload.get("edge_generation") or ""):
                raise HubStoreV2Conflict("attempt_edge_generation_mismatch")
        record = self.runtime.authenticate_machine(
            str(payload.get("machine_id") or ""),
            token,
            edge_generation=str(payload.get("edge_generation") or ""),
        )
        if require_contract:
            requested = self._requested_contract_hash(payload)
            advertised = str((record.get("capabilities") or {}).get("contract_hash") or "")
            # Permit an enrolled older Edge to finish only attempts bound to
            # its advertised contract during a rolling upgrade. Attempt-level
            # fences still reject mismatched operations, and placement blocks
            # new work until the Edge advertises the current contract.
            if requested != advertised:
                raise HubStoreV2Conflict("attempt_contract_hash_mismatch")
        return record

    @staticmethod
    def _requested_contract_hash(payload: Mapping[str, Any]) -> str:
        nested = payload.get("contract")
        nested = nested if isinstance(nested, Mapping) else {}
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
            raise EdgeRequestError("contract_hash is required")
        if len(values) != 1:
            raise HubStoreV2Conflict("attempt_contract_hash_mismatch")
        return values.pop()

    @staticmethod
    def _generation_number(edge_generation: str) -> int:
        digest = hashlib.sha256(edge_generation.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)

    @staticmethod
    def _dispatch_payload(entity: Mapping[str, Any]) -> dict[str, Any]:
        payload = entity.get("record", {}).get("payload") if "record" in entity else entity.get("payload")
        return deepcopy(dict(payload)) if isinstance(payload, Mapping) else {}

    def _require_attempt_fences(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        operation_id = str(payload.get("operation_id") or "").strip()
        attempt_id = str(payload.get("attempt_id") or "").strip()
        if not operation_id or not attempt_id:
            raise EdgeRequestError("operation_id and attempt_id are required")
        operation = self.store.get_operation(operation_id)
        attempt = self.store.get_attempt(attempt_id)
        contract_entity = self.store.get_entity(ATTEMPT_CONTRACT_ENTITY_TYPE, attempt_id)
        if operation is None or attempt is None or contract_entity is None:
            raise KeyError(attempt_id)
        contract = deepcopy(dict(contract_entity["record"]))
        expected = {
            "operation_id": operation_id,
            "machine_id": str(payload.get("machine_id") or ""),
            "edge_generation": self._generation_number(str(payload.get("edge_generation") or "")),
            "required_contract_hash": self._requested_contract_hash(payload),
            "fencing_token": int(payload.get("fencing_token") or 0),
        }
        actual = {
            "operation_id": str(attempt.get("operation_id") or ""),
            "machine_id": str(attempt.get("machine_id") or ""),
            "edge_generation": int(attempt.get("edge_generation") or -1),
            "required_contract_hash": str(contract.get("required_contract_hash") or ""),
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
        contract: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        dispatch_payload = self._dispatch_payload(dispatch)
        arguments = dispatch_payload.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = {
                key: deepcopy(value)
                for key, value in dispatch_payload.items()
                if key not in {"action", "context", "target", "machine_id", "edge_generation"}
            }
        action = str(dispatch_payload.get("action") or "")
        capabilities = (
            machine.get("capabilities") if isinstance(machine.get("capabilities"), Mapping) else {}
        )
        action_capabilities = capabilities.get("action_capabilities")
        if not isinstance(action_capabilities, Mapping):
            action_capabilities = capabilities.get("action_capability_versions")
        action_capabilities = (
            dict(action_capabilities) if isinstance(action_capabilities, Mapping) else {}
        )
        action_version = str(action_capabilities.get(action) or "")
        if not action_version:
            raise HubStoreV2Conflict("attempt_action_capability_mismatch")
        required_contract = str(
            (contract or {}).get("required_contract_hash")
            or attempt.get("required_contract_hash")
            or capabilities.get("contract_hash")
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
            **self._external_attempt(attempt, external_generation=external_generation),
            "machine_id": str(machine["machine_id"]),
            "required_edge_generation": external_generation,
            "required_contract_hash": required_contract,
            "required_action_capability_version": action_version,
            "action": action,
            "arguments": deepcopy(dict(arguments)),
            "payload": deepcopy(dispatch_payload),
            "target": deepcopy(
                dict(dispatch_payload.get("target"))
                if isinstance(dispatch_payload.get("target"), Mapping)
                else {}
            ),
            "context": deepcopy(
                dict(dispatch_payload.get("context"))
                if isinstance(dispatch_payload.get("context"), Mapping)
                else {}
            ),
            "idempotency_key": str(operation.get("idempotency_key") or ""),
            "operation_payload_hash": str(
                dispatch.get("payload_hash") or operation.get("semantic_payload_hash") or ""
            ),
            "requirements": requirements,
            "operation_revision": int(operation.get("revision") or 0),
        }
        tool_name = str(operation.get("tool") or "")
        if tool_name in HUB_V2_ACTION_MAP:
            wire["tool_name"] = tool_name
        for field in ("parent_operation_id", "item_id", "work_group_id", "lane_id"):
            value = operation.get(field) or dispatch_payload.get(field)
            if value:
                wire[field] = value
        return wire

    @staticmethod
    def _external_attempt(
        attempt: Mapping[str, Any],
        *,
        external_generation: str,
    ) -> dict[str, Any]:
        result = deepcopy(dict(attempt))
        result["edge_generation"] = external_generation
        return result

    @staticmethod
    def _external_fences(
        attempt: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "operation_id": str(attempt["operation_id"]),
            "attempt_id": str(attempt["attempt_id"]),
            "fencing_token": int(attempt["fencing_token"]),
            "edge_generation": str(payload["edge_generation"]),
            "contract_hash": _RuntimeEdgeController._requested_contract_hash(payload),
        }

    def _record_receipt(self, acknowledgement: Mapping[str, Any], *, machine_id: str) -> None:
        receipt_id = str(acknowledgement["receipt_id"])
        record = {**deepcopy(dict(acknowledgement)), "machine_id": machine_id}
        existing = self.store.get_entity(EDGE_RECEIPT_ENTITY, receipt_id)
        if existing is not None:
            if existing["record"] != record:
                raise HubStoreV2Conflict("receipt_identity_conflict")
            return
        self.store.put_entity(EDGE_RECEIPT_ENTITY, receipt_id, record, expected_revision=0)

    def _apply_dispatch_result(self, operation_id: str, result: Any) -> None:
        del result  # Authoritative worker truth arrives through the projection endpoint.
        dispatch = self.store.get_entity(EDGE_DISPATCH_ENTITY, operation_id)
        if dispatch is None:
            return
        record = deepcopy(dict(dispatch["record"]))
        record.update({"status": "complete", "updated_at": time.time()})
        self.store.put_entity(
            EDGE_DISPATCH_ENTITY,
            operation_id,
            record,
            expected_revision=int(dispatch["revision"]),
        )


def _client_metadata(message: Any) -> Mapping[str, Any]:
    if not isinstance(message, Mapping):
        return {}
    params = message.get("params")
    if not isinstance(params, Mapping):
        return {}
    metadata = params.get("_meta")
    return metadata if isinstance(metadata, Mapping) else {}


def _apply_request_metadata(
    session: MutableMapping[str, Any],
    message: Any,
    *,
    salt: str,
    idle_seconds: int,
) -> None:
    if not isinstance(message, Mapping):
        return
    params = message.get("params")
    params = params if isinstance(params, Mapping) else {}
    method = str(message.get("method") or "")
    if method == "initialize":
        client_info = params.get("clientInfo")
        if isinstance(client_info, Mapping):
            session["client_label"] = str(client_info.get("title") or client_info.get("name") or "")[:120]

    metadata = _client_metadata(message)
    refs = (
        ("openai/session", "chatgpt_session_ref", "chatgpt_session"),
        ("openai/subject", "chatgpt_subject_ref", "chatgpt_subject"),
        ("openai/organization", "chatgpt_organization_ref", "chatgpt_org"),
    )
    for source, target, prefix in refs:
        raw = str(metadata.get(source) or "").strip()
        if raw:
            session[target] = make_hashed_ref(f"{source}:{raw}", salt=salt, prefix=prefix)

    if method != "tools/call":
        return
    arguments = params.get("arguments")
    if isinstance(arguments, Mapping):
        group_id = str(arguments.get("work_group_id") or "").strip()
        lane_id = str(arguments.get("lane_id") or arguments.get("lane") or "").strip()
        if group_id:
            session["work_group_id"] = group_id
        if lane_id:
            session["lane_id"] = lane_id

    now = time.time()
    last_activity = float(session.get("work_run_last_activity_at") or 0)
    if not session.get("work_run_ref") or now - last_activity > idle_seconds:
        session["work_run_ref"] = f"run_{uuid.uuid4().hex[:12]}"
        session["work_run_started_at"] = now
    session["work_run_last_activity_at"] = now


def _context_for_session(
    session_id: str,
    session: MutableMapping[str, Any],
    *,
    salt: str,
    active_sessions: int,
) -> RequestContext:
    return RequestContext.from_session(
        session_id,
        session,
        salt=salt,
        active_mcp_sessions=active_sessions,
    )


def _edge_method(domain_app: Any, action: str) -> Callable[..., Any]:
    generic = getattr(domain_app, "handle_edge_request", None)
    if callable(generic):
        return generic
    for name in _EDGE_METHOD_NAMES[action]:
        candidate = getattr(domain_app, name, None)
        if callable(candidate):
            return candidate
    raise RuntimeError(f"HubV2App does not implement the Edge V2 {action} boundary")


def _call_arguments(
    method: Callable[..., Any],
    *,
    action: str,
    payload: Mapping[str, Any],
    token: str,
) -> tuple[list[Any], dict[str, Any]]:
    """Adapt the transport object to explicit or payload-style app methods."""

    signature = inspect.signature(method)
    parameters = signature.parameters
    generic = getattr(method, "__name__", "") == "handle_edge_request"
    has_var_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    payload_name = next((name for name in ("payload", "body", "request_data") if name in parameters), "")

    available = dict(payload)
    available.update(
        {
            "action": action,
            "edge_action": action,
            "token": token,
            "node_token": token,
            "bearer_token": token,
        }
    )
    if generic:
        if has_var_kwargs:
            return [], {"action": action, "payload": dict(payload), "token": token}
        kwargs = {name: available[name] for name in parameters if name in available}
        if payload_name:
            kwargs[payload_name] = dict(payload)
        return [], kwargs
    if payload_name:
        kwargs: dict[str, Any] = {payload_name: dict(payload)}
        for name in ("token", "node_token", "bearer_token"):
            if name in parameters:
                kwargs[name] = token
        return [], kwargs
    if has_var_kwargs:
        return [], {**dict(payload), "token": token}
    return [], {name: available[name] for name in parameters if name in available}


async def _dispatch_edge(
    domain_app: Any,
    *,
    action: str,
    payload: Mapping[str, Any],
    token: str,
) -> Mapping[str, Any]:
    method = _edge_method(domain_app, action)
    args, kwargs = _call_arguments(method, action=action, payload=payload, token=token)
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        raise StaleEdgeRequest("stale_revision")
    if not isinstance(result, Mapping):
        raise TypeError(f"Edge V2 {action} handler must return an object")
    return dict(result)


def _validate_edge_payload(action: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise EdgeRequestError("JSON object body is required")
    value = dict(payload)
    for field in _EDGE_REQUIRED_FIELDS[action]:
        candidate = value.get(field)
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            raise EdgeRequestError(f"{field} is required")
    return value


def _public_error_code(error: BaseException, default: str) -> str:
    value = str(error).strip().casefold().replace(" ", "_")
    return value if _SAFE_ERROR_CODE.fullmatch(value) else default


def _edge_error_response(error: BaseException) -> JSONResponse:
    if isinstance(error, KeyError):
        return JSONResponse(
            {"error": {"code": "not_found", "message": "Requested Hub V2 record was not found"}},
            status_code=404,
        )
    if isinstance(error, (HubStoreV2Conflict, HubStoreV2StateError, StaleEdgeRequest)):
        return JSONResponse(
            {
                "error": {
                    "code": _public_error_code(error, "edge_state_conflict"),
                    "message": "Request conflicts with current Hub V2 state",
                }
            },
            status_code=409,
        )
    if isinstance(error, EdgeRequestError):
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": str(error)}},
            status_code=400,
        )
    if isinstance(error, ValueError):
        message = str(error).casefold()
        unauthorized = any(term in message for term in ("unauthorized", "authentication", "credential", "token"))
        if unauthorized:
            return _unauthorized_response()
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "Invalid Edge V2 request"}},
            status_code=400,
        )
    logger.error("Hub V2 Edge handling error: %s", internal_log_error(error))
    return JSONResponse(
        {"error": {"code": "internal_error", "message": "Internal processing error"}},
        status_code=500,
    )


def create_hub_v2_server(
    config: Mapping[str, Any] | None = None,
    *,
    hub_app: HubV2App | None = None,
    hub_app_factory: HubV2AppFactory | None = None,
    protocol_factory: HubV2ProtocolFactory | None = None,
    auth_policy: AuthPolicy | None = None,
    environ: Mapping[str, str] | None = None,
    principal_ref: str = "",
    session_ref_salt: str = "",
    max_request_bytes: int | None = None,
) -> FastAPI:
    """Construct the opt-in Hub V2 HTTP surface around injected dependencies."""

    if hub_app is not None and hub_app_factory is not None:
        raise TypeError("Pass hub_app or hub_app_factory, not both")
    config_value = dict(config or {})
    factory = hub_app_factory or _default_hub_v2_app_factory
    owns_hub_app = hub_app is None
    domain_app = hub_app or _construct_hub_app(factory, config_value)
    if not callable(getattr(domain_app, "handle_tool_call", None)):
        raise TypeError("HubV2App must define handle_tool_call")
    try:
        for edge_action in _EDGE_METHOD_NAMES:
            _edge_method(domain_app, edge_action)
        edge_app: Any = domain_app
    except RuntimeError:
        edge_app = _RuntimeEdgeController(domain_app)

    resolved_principal = _resolve_principal_ref(domain_app, principal_ref)
    salt = session_ref_salt or resolved_principal
    request_limit = _max_request_bytes(config_value, max_request_bytes)
    try:
        operator_auth = auth_policy or build_auth_policy(config_value, environ=environ)
    except AuthConfigurationError as error:
        raise RuntimeError(str(error)) from error

    protocol_builder = protocol_factory or HubProtocolV2
    public_handler = _PublicToolHandler(domain_app)
    protocol = protocol_builder(public_handler)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            close = getattr(domain_app, "close", None) if owns_hub_app else None
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    api = FastAPI(title="PatchBay Hub V2", lifespan=lifespan)
    sessions: dict[str, dict[str, Any]] = {}

    app_config = config_value.get("app") if isinstance(config_value.get("app"), Mapping) else {}
    raw_idle = app_config.get("work_run_idle_seconds", DEFAULT_WORK_RUN_IDLE_SECONDS)
    try:
        idle_seconds = max(60, int(raw_idle))
    except (TypeError, ValueError):
        idle_seconds = DEFAULT_WORK_RUN_IDLE_SECONDS

    api.state.hub_v2_app = domain_app
    api.state.hub_v2_edge_app = edge_app
    api.state.hub_v2_protocol = protocol
    api.state.hub_v2_sessions = sessions
    api.state.hub_v2_principal_ref = resolved_principal
    api.state.hub_v2_auth_policy = operator_auth
    api.state.hub_v2_request_limit = request_limit

    @api.get("/")
    async def root(request: Request) -> Response:
        unauthorized = _authorize_operator(request, operator_auth)
        if unauthorized:
            return unauthorized
        return JSONResponse(
            {
                "name": "patchbay-hub-v2",
                "status": "running",
                "transport": "streamable-http",
                "principal_ref": resolved_principal,
                "auth": auth_public_metadata(operator_auth),
                "contract": dict(HUB_V2_PROTOCOL_METADATA),
            }
        )

    @api.get("/status")
    async def status(request: Request) -> Response:
        unauthorized = _authorize_operator(request, operator_auth)
        if unauthorized:
            return unauthorized
        return JSONResponse(
            {
                "server": "healthy",
                "mode": "hub-v2",
                "principal_ref": resolved_principal,
                "active_mcp_sessions": len(sessions),
                "auth": auth_public_metadata(operator_auth),
                "contract": dict(HUB_V2_PROTOCOL_METADATA),
            }
        )

    @api.get("/mcp")
    async def mcp_get(request: Request) -> Response:
        unauthorized = _authorize_operator(request, operator_auth)
        if unauthorized:
            return unauthorized
        return JSONResponse({"transport": "streamable-http", "message": "Use POST /mcp for JSON-RPC"})

    @api.post("/mcp")
    async def mcp_post(request: Request) -> Response:
        unauthorized = _authorize_operator(request, operator_auth)
        if unauthorized:
            return unauthorized

        session_id = request.headers.get("Mcp-Session-Id") or request.headers.get("MCP-Session-Id")
        if session_id and session_id not in sessions:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "Unknown or expired MCP session"},
                },
                status_code=404,
            )
        if not session_id:
            session_id = str(uuid.uuid4())
            sessions[session_id] = {
                "created_at": time.time(),
                "last_activity": time.time(),
                "client_ref": make_client_ref(session_id, salt=salt),
                "owner_ref": resolved_principal,
                "owner_scope": "server",
                "tool_mode": "hub-v2",
            }
        else:
            sessions[session_id]["last_activity"] = time.time()

        headers = {"Mcp-Session-Id": session_id}
        try:
            message = await _read_limited_json(request, limit=request_limit)
        except RequestBodyTooLarge:
            return JSONResponse(
                {"error": {"code": "request_too_large", "message": "Request body too large"}},
                status_code=413,
                headers=headers,
            )
        except EdgeRequestError:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
                headers=headers,
            )

        session = sessions[session_id]
        _apply_request_metadata(session, message, salt=salt, idle_seconds=idle_seconds)
        context = _context_for_session(
            session_id,
            session,
            salt=salt,
            active_sessions=len(sessions),
        )
        try:
            response = await protocol.handle_message(message, context=context)
        except Exception as error:
            logger.error("Hub V2 MCP handling error: %s", internal_log_error(error))
            message_id = message.get("id") if isinstance(message, Mapping) else None
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32603, "message": "Internal processing error"},
            }
        if response is None:
            return Response(status_code=204, headers=headers)
        return JSONResponse(response, headers=headers)

    @api.delete("/mcp")
    async def mcp_delete(request: Request) -> Response:
        unauthorized = _authorize_operator(request, operator_auth)
        if unauthorized:
            return unauthorized
        session_id = request.headers.get("Mcp-Session-Id") or request.headers.get("MCP-Session-Id")
        if session_id and sessions.pop(session_id, None) is not None:
            return Response(status_code=204)
        return JSONResponse(
            {"error": {"code": "session_not_found", "message": "Session not found"}},
            status_code=404,
        )

    async def edge_request(request: Request, action: str) -> Response:
        try:
            raw = await _read_limited_json(request, limit=request_limit)
            payload = _validate_edge_payload(action, raw)
            token = "" if action == "enroll" else _bearer_token(request)
            if action != "enroll" and not token:
                return _unauthorized_response()
            result = await _dispatch_edge(edge_app, action=action, payload=payload, token=token)
            return JSONResponse(result)
        except RequestBodyTooLarge:
            return JSONResponse(
                {"error": {"code": "request_too_large", "message": "Request body too large"}},
                status_code=413,
            )
        except Exception as error:
            return _edge_error_response(error)

    @api.post(f"{EDGE_V2_PREFIX}/enroll")
    async def edge_enroll(request: Request) -> Response:
        return await edge_request(request, "enroll")

    @api.post(f"{EDGE_V2_PREFIX}/heartbeat")
    async def edge_heartbeat(request: Request) -> Response:
        return await edge_request(request, "heartbeat")

    @api.post(f"{EDGE_V2_PREFIX}/claim")
    async def edge_claim(request: Request) -> Response:
        return await edge_request(request, "claim")

    @api.post(f"{EDGE_V2_PREFIX}/lease")
    async def edge_lease(request: Request) -> Response:
        return await edge_request(request, "lease")

    @api.post(f"{EDGE_V2_PREFIX}/result")
    async def edge_result(request: Request) -> Response:
        return await edge_request(request, "result")

    @api.post(f"{EDGE_V2_PREFIX}/outbox/ack")
    async def edge_outbox_ack(request: Request) -> Response:
        return await edge_request(request, "outbox_ack")

    @api.post(f"{EDGE_V2_PREFIX}/projection")
    async def edge_projection(request: Request) -> Response:
        return await edge_request(request, "projection")

    @api.post(f"{EDGE_V2_PREFIX}/reconcile")
    async def edge_reconcile(request: Request) -> Response:
        return await edge_request(request, "reconcile")

    return api


# Concise aliases make the factory natural to import without creating global
# state or confusing it with the domain-level ``HubV2App`` composition.
create_app = create_hub_v2_server
create_server_v2 = create_hub_v2_server


def main() -> None:
    """Run the opt-in production Hub V2 server from ``PATCHBAY_CONFIG``."""

    import uvicorn

    config = load_hub_v2_config()
    server = create_hub_v2_server(config)
    server_config = config.get("server") if isinstance(config.get("server"), Mapping) else {}
    uvicorn.run(
        server,
        host=str(server_config.get("host") or "127.0.0.1"),
        port=int(server_config.get("port") or 8000),
        log_level="info",
        access_log=bool(config.get("logging", {}).get("access_log", False))
        if isinstance(config.get("logging"), Mapping)
        else False,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "EDGE_V2_PREFIX",
    "HubV2App",
    "HubV2AppFactory",
    "RequestBodyTooLarge",
    "create_app",
    "create_hub_v2_server",
    "create_server_v2",
    "load_hub_v2_config",
]
