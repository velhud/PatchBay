"""FastAPI transport for the opt-in PatchBay Hub V2 control plane.

This module deliberately has no import-time server or runtime singleton.
Callers may inject a ``HubV2App`` for tests; the default factory composes the
production pull-transport graph.
"""
from __future__ import annotations

import inspect
import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
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
from patchbay.hub.store_v2 import HubStoreV2Conflict, HubStoreV2StateError
from patchbay.hub.backup_v2 import AdmissionFrozenError
from patchbay.hub.transport_v2 import HubPullTransportBridgeV2
from patchbay.protocol.context import RequestContext, make_client_ref, make_hashed_ref
from patchbay.security import internal_log_error, redact_sensitive_output

logger = logging.getLogger(__name__)

DEFAULT_MAX_REQUEST_BYTES = 1_048_576
DEFAULT_WORK_RUN_IDLE_SECONDS = 900
DEFAULT_MCP_SESSION_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_MCP_SESSIONS = 1_024
DEFAULT_RECOVERY_DISPATCH_INTERVAL_SECONDS = 1.0
DEFAULT_RECOVERY_DISPATCH_BATCH_SIZE = 100
EDGE_V2_PREFIX = "/edge/v2"


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


class _RuntimeEdgeController(HubPullTransportBridgeV2):
    """Production pull transport used for injected Hub applications.

    Keeping this compatibility name lets the HTTP factory support runtime-backed
    injected apps without maintaining a second claim/result/recovery state machine.
    All Edge behavior is inherited from ``HubPullTransportBridgeV2``.
    """


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
    logger.error(
        "Hub V2 Edge handling error: %s",
        internal_log_error(error) or type(error).__name__,
    )
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
    mcp_session_ttl_seconds: float = DEFAULT_MCP_SESSION_TTL_SECONDS,
    max_mcp_sessions: int = DEFAULT_MAX_MCP_SESSIONS,
    monotonic_clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Construct the opt-in Hub V2 HTTP surface around injected dependencies.

    MCP sessions are process-local and bounded by idle TTL plus LRU cardinality.
    Evicted clients receive the normal expired-session error and may initialize a
    new, identity-isolated session by retrying without ``Mcp-Session-Id``.
    """

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
    clock = monotonic_clock or time.monotonic
    try:
        session_ttl_seconds = max(1.0, float(mcp_session_ttl_seconds))
        session_capacity = max(1, int(max_mcp_sessions))
    except (TypeError, ValueError) as error:
        raise ValueError("MCP session TTL and cardinality bounds must be numeric") from error

    hub_config = (
        config_value.get("hub")
        if isinstance(config_value.get("hub"), Mapping)
        else {}
    )
    try:
        recovery_dispatch_interval = max(
            0.1,
            min(
                float(
                    hub_config.get(
                        "recovery_dispatch_interval_seconds",
                        DEFAULT_RECOVERY_DISPATCH_INTERVAL_SECONDS,
                    )
                ),
                60.0,
            ),
        )
        recovery_dispatch_batch_size = max(
            1,
            min(
                int(
                    hub_config.get(
                        "recovery_dispatch_batch_size",
                        DEFAULT_RECOVERY_DISPATCH_BATCH_SIZE,
                    )
                ),
                1_000,
            ),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Hub recovery dispatch settings must be numeric") from error

    async def recovery_dispatch_loop() -> None:
        dispatch = getattr(domain_app, "dispatch_pending_operations", None)
        if not callable(dispatch):
            return
        while True:
            try:
                result = dispatch(max_operations=recovery_dispatch_batch_size)
                if inspect.isawaitable(result):
                    await result
            except AdmissionFrozenError:
                # Backup maintenance deliberately pauses new effect admission.
                pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Hub recovery dispatch cycle failed: %s",
                    internal_log_error(error),
                )
            await asyncio.sleep(recovery_dispatch_interval)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        recovery_task: asyncio.Task[None] | None = None
        if callable(getattr(domain_app, "dispatch_pending_operations", None)):
            recovery_task = asyncio.create_task(
                recovery_dispatch_loop(),
                name="patchbay-hub-v2-recovery-dispatch",
            )
        try:
            yield
        finally:
            if recovery_task is not None:
                recovery_task.cancel()
                try:
                    await recovery_task
                except asyncio.CancelledError:
                    pass
            close = getattr(domain_app, "close", None) if owns_hub_app else None
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    api = FastAPI(title="PatchBay Hub V2", lifespan=lifespan)
    sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
    in_flight_sessions: dict[str, int] = {}

    def prune_sessions(
        now: float,
        *,
        preserve_for_capacity: frozenset[str] = frozenset(),
    ) -> None:
        """Expire idle sessions, then evict inactive LRU entries to the cap."""

        for candidate_id, candidate in list(sessions.items()):
            if in_flight_sessions.get(candidate_id, 0) > 0:
                continue
            last_activity = float(candidate.get("_last_activity_monotonic") or 0.0)
            if now - last_activity >= session_ttl_seconds:
                sessions.pop(candidate_id, None)

        if len(sessions) <= session_capacity:
            return
        protected = {
            candidate_id
            for candidate_id, count in in_flight_sessions.items()
            if count > 0
        } | set(preserve_for_capacity)
        for candidate_id in list(sessions):
            if len(sessions) <= session_capacity:
                break
            if candidate_id in protected:
                continue
            sessions.pop(candidate_id, None)

    def finish_session_request(session_id: str) -> None:
        remaining = in_flight_sessions.get(session_id, 0) - 1
        if remaining > 0:
            in_flight_sessions[session_id] = remaining
            return
        in_flight_sessions.pop(session_id, None)
        other_inactive = any(
            candidate_id != session_id and in_flight_sessions.get(candidate_id, 0) == 0
            for candidate_id in sessions
        )
        preserve = frozenset() if other_inactive else frozenset({session_id})
        prune_sessions(clock(), preserve_for_capacity=preserve)

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
    api.state.hub_v2_in_flight_sessions = in_flight_sessions
    api.state.hub_v2_session_ttl_seconds = session_ttl_seconds
    api.state.hub_v2_max_sessions = session_capacity
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
        prune_sessions(clock())
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
        now_monotonic = clock()
        preserve = frozenset({session_id}) if session_id else frozenset()
        prune_sessions(now_monotonic, preserve_for_capacity=preserve)
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
                "_created_monotonic": now_monotonic,
                "_last_activity_monotonic": now_monotonic,
                "client_ref": make_client_ref(session_id, salt=salt),
                "owner_ref": resolved_principal,
                "owner_scope": "server",
                "tool_mode": "hub-v2",
            }
        else:
            sessions[session_id]["last_activity"] = time.time()
            sessions[session_id]["_last_activity_monotonic"] = now_monotonic
        sessions.move_to_end(session_id)
        in_flight_sessions[session_id] = in_flight_sessions.get(session_id, 0) + 1
        prune_sessions(now_monotonic, preserve_for_capacity=frozenset({session_id}))

        headers = {"Mcp-Session-Id": session_id}
        try:
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
        finally:
            finish_session_request(session_id)

    @api.delete("/mcp")
    async def mcp_delete(request: Request) -> Response:
        unauthorized = _authorize_operator(request, operator_auth)
        if unauthorized:
            return unauthorized
        session_id = request.headers.get("Mcp-Session-Id") or request.headers.get("MCP-Session-Id")
        prune_sessions(clock(), preserve_for_capacity=frozenset({session_id}) if session_id else frozenset())
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
    "DEFAULT_MAX_MCP_SESSIONS",
    "DEFAULT_MCP_SESSION_TTL_SECONDS",
    "EDGE_V2_PREFIX",
    "HubV2App",
    "HubV2AppFactory",
    "RequestBodyTooLarge",
    "create_app",
    "create_hub_v2_server",
    "create_server_v2",
    "load_hub_v2_config",
]
