"""HTTP server for optional PatchBay Hub mode."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from patchbay.auth import (
    AuthConfigurationError,
    auth_public_metadata,
    build_auth_policy,
    request_token,
    request_is_authorized,
)
from patchbay.connector.profiles import normalize_logging_paths, resolve_runtime_path
from patchbay.hub.protocol import HubProtocol
from patchbay.hub.runtime import HubRuntime
from patchbay.protocol.context import RequestContext, make_client_ref, make_hashed_ref
from patchbay.security import internal_log_error, public_error_message

logger = logging.getLogger(__name__)


def _default_config_path() -> Path:
    candidates = [
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parents[3] / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_hub_config() -> dict[str, Any]:
    path = Path(os.environ.get("PATCHBAY_CONFIG", _default_config_path()))
    with open(path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    normalize_logging_paths(config)
    return config


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

config = load_hub_config()
try:
    auth_policy = build_auth_policy(config)
except AuthConfigurationError as exc:
    raise RuntimeError(str(exc)) from exc

runtime = HubRuntime(config)
protocol = HubProtocol(runtime)
app = FastAPI(title="PatchBay Hub")
sessions: dict[str, dict[str, Any]] = {}
work_runs: dict[str, dict[str, Any]] = {}
DEFAULT_WORK_RUN_IDLE_SECONDS = 900


def _load_session_ref_salt() -> str:
    configured = os.environ.get("PATCHBAY_SESSION_REF_SALT")
    if configured:
        return configured
    path = resolve_runtime_path(None, "hub", "session-ref-salt", environ=os.environ)
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = uuid.uuid4().hex
        path.write_text(value + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        logger.warning("Could not persist Hub session ref salt; falling back to process-local salt")
        return uuid.uuid4().hex


_SESSION_REF_SALT = _load_session_ref_salt()


class RequestBodyTooLarge(Exception):
    """Raised when an MCP or edge request body exceeds configured limits."""


def _max_request_bytes() -> int:
    try:
        configured = int(config.get("server", {}).get("max_request_bytes", 1_048_576))
    except (TypeError, ValueError):
        configured = 1_048_576
    return max(1, configured)


async def _read_limited_json(request: Request) -> Any:
    limit = _max_request_bytes()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise RequestBodyTooLarge(f"Request body exceeds max_request_bytes ({limit})")
        except ValueError as exc:
            raise json.JSONDecodeError("Invalid Content-Length", content_length, 0) from exc

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise RequestBodyTooLarge(f"Request body exceeds max_request_bytes ({limit})")
    return json.loads(body.decode("utf-8") or "{}")


def _unauthorized_response() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def _authorize_mcp_request(request: Request) -> Optional[JSONResponse]:
    if request_is_authorized(auth_policy, request.headers, request.query_params):
        return None
    logger.warning("Unauthorized hub MCP request rejected: method=%s path=%s", request.method, request.url.path)
    return _unauthorized_response()


def _mcp_session_id(request: Request) -> Optional[str]:
    return request.headers.get("Mcp-Session-Id") or request.headers.get("MCP-Session-Id")


def _ownership_scope() -> str:
    raw = (config.get("ownership") or {}).get("scope") if isinstance(config.get("ownership"), dict) else None
    scope = str(raw or "token").strip().lower().replace("-", "_")
    if scope in {"transport", "transport_session", "session", "mcp_session"}:
        return "transport_session"
    if scope in {"server", "shared", "single_user"}:
        return "server"
    return "token"


def _owner_ref_for_request(request: Request, session_id: str) -> tuple[str, str]:
    scope = _ownership_scope()
    if scope == "transport_session":
        return make_client_ref(session_id, salt=_SESSION_REF_SALT), scope
    if scope == "token":
        token = request_token(request.headers, request.query_params, auth_policy)
        if token:
            return make_client_ref(f"token:{token}", salt=_SESSION_REF_SALT), scope
    return make_client_ref("hub-server-owner", salt=_SESSION_REF_SALT), "server"


def _request_context_for_session(session_id: str) -> RequestContext:
    return RequestContext.from_session(
        session_id,
        sessions[session_id],
        salt=_SESSION_REF_SALT,
        active_mcp_sessions=len(sessions),
    )


def _client_meta_from_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    params = message.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def _tool_name_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    params = message.get("params")
    if not isinstance(params, dict):
        return ""
    return str(params.get("name") or "")


def _is_work_activity(message: Any) -> bool:
    return isinstance(message, dict) and str(message.get("method") or "") == "tools/call" and bool(_tool_name_from_message(message))


def _work_run_idle_seconds() -> int:
    app_config = config.get("app", {}) if isinstance(config.get("app"), dict) else {}
    raw = app_config.get("work_run_idle_seconds") or app_config.get("conversation_work_run_idle_seconds")
    try:
        value = int(raw if raw is not None else DEFAULT_WORK_RUN_IDLE_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_WORK_RUN_IDLE_SECONDS
    return max(60, value)


def _work_run_key(session_data: dict[str, Any]) -> str:
    return (
        str(session_data.get("chatgpt_session_ref") or "")
        or str(session_data.get("owner_ref") or "")
        or str(session_data.get("client_ref") or "")
        or "anonymous"
    )


def _apply_chatgpt_client_metadata(session_id: str, message: Any) -> None:
    session_data = sessions[session_id]
    session_data["client_ref"] = make_client_ref(session_id, salt=_SESSION_REF_SALT)
    meta = _client_meta_from_message(message)
    openai_session = str(meta.get("openai/session") or "").strip()
    openai_subject = str(meta.get("openai/subject") or "").strip()
    openai_org = str(meta.get("openai/organization") or "").strip()
    if openai_session:
        session_data["chatgpt_session_ref"] = make_hashed_ref(
            f"openai/session:{openai_session}",
            salt=_SESSION_REF_SALT,
            prefix="chatgpt_session",
        )
    if openai_subject:
        session_data["chatgpt_subject_ref"] = make_hashed_ref(
            f"openai/subject:{openai_subject}",
            salt=_SESSION_REF_SALT,
            prefix="chatgpt_subject",
        )
    if openai_org:
        session_data["chatgpt_organization_ref"] = make_hashed_ref(
            f"openai/organization:{openai_org}",
            salt=_SESSION_REF_SALT,
            prefix="chatgpt_org",
        )
    if not _is_work_activity(message):
        return
    now = asyncio.get_event_loop().time()
    wall_now = time.time()
    idle_seconds = _work_run_idle_seconds()
    key = _work_run_key(session_data)
    run = work_runs.get(key)
    if not run or now - float(run.get("last_activity_monotonic") or 0) > idle_seconds:
        run = {
            "work_run_ref": f"run_{uuid.uuid4().hex[:12]}",
            "key": key,
            "started_at": wall_now,
            "last_activity_at": wall_now,
            "last_activity_monotonic": now,
            "idle_seconds": idle_seconds,
        }
        work_runs[key] = run
    else:
        run["last_activity_at"] = wall_now
        run["last_activity_monotonic"] = now
    session_data["work_run_ref"] = run["work_run_ref"]
    session_data["work_run_started_at"] = run["started_at"]
    session_data["work_run_last_activity_at"] = run["last_activity_at"]


async def _request_json_or_error(request: Request) -> tuple[Any | None, JSONResponse | None]:
    try:
        return await _read_limited_json(request), None
    except RequestBodyTooLarge:
        return None, JSONResponse({"error": "Request body too large"}, status_code=413)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        logger.warning("Invalid hub request JSON: %s", internal_log_error(error))
        return None, JSONResponse({"error": "Parse error"}, status_code=400)


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :]
    return ""


def _edge_auth(payload: Any, request: Request) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("JSON object body is required")
    machine_id = str(payload.get("machine_id") or "").strip()
    token = _bearer_token(request)
    if not runtime.authenticate_machine(machine_id, token):
        raise ValueError("Machine authentication failed")
    return machine_id, token


@app.get("/")
async def root(request: Request):
    unauthorized = _authorize_mcp_request(request)
    if unauthorized:
        return unauthorized
    return {
        "name": "patchbay-hub",
        "version": "0.1.0",
        "transport": "streamable-http",
        "status": "running",
        "auth": auth_public_metadata(auth_policy),
        "fleet": runtime.fleet_status().get("summary"),
    }


@app.get("/status")
async def status(request: Request):
    unauthorized = _authorize_mcp_request(request)
    if unauthorized:
        return unauthorized
    return {"server": "healthy", "hub": runtime.fleet_status(), "auth": auth_public_metadata(auth_policy)}


@app.get("/mcp")
async def mcp_get(request: Request):
    unauthorized = _authorize_mcp_request(request)
    if unauthorized:
        return unauthorized
    return JSONResponse({"transport": "streamable-http", "message": "Use POST /mcp for JSON-RPC"})


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    unauthorized = _authorize_mcp_request(request)
    if unauthorized:
        return unauthorized

    session_id = _mcp_session_id(request)
    if not session_id:
        session_id = str(uuid.uuid4())
        owner_ref, owner_scope = _owner_ref_for_request(request, session_id)
        sessions[session_id] = {
            "created_at": asyncio.get_event_loop().time(),
            "last_activity": asyncio.get_event_loop().time(),
            "client_ref": make_client_ref(session_id, salt=_SESSION_REF_SALT),
            "owner_ref": owner_ref,
            "owner_scope": owner_scope,
            "tool_mode": "hub",
        }
    elif session_id not in sessions:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unknown or expired MCP session"}, "id": None},
            status_code=404,
        )
    else:
        sessions[session_id]["last_activity"] = asyncio.get_event_loop().time()
        owner_ref, owner_scope = _owner_ref_for_request(request, session_id)
        sessions[session_id]["owner_ref"] = owner_ref
        sessions[session_id]["owner_scope"] = owner_scope

    message, error_response = await _request_json_or_error(request)
    if error_response:
        error_response.headers["Mcp-Session-Id"] = session_id
        return error_response

    _apply_chatgpt_client_metadata(session_id, message)
    try:
        response = await protocol.handle_message(message, context=_request_context_for_session(session_id))
    except Exception as error:
        logger.error("Hub MCP handling error: %s", internal_log_error(error))
        response = {
            "jsonrpc": "2.0",
            "id": message.get("id") if isinstance(message, dict) else None,
            "error": {"code": -32603, "message": "Internal processing error"},
        }
    if response:
        return JSONResponse(response, headers={"Mcp-Session-Id": session_id})
    return Response(status_code=204, headers={"Mcp-Session-Id": session_id})


@app.delete("/mcp")
async def mcp_session_delete(request: Request):
    unauthorized = _authorize_mcp_request(request)
    if unauthorized:
        return unauthorized
    session_id = _mcp_session_id(request)
    if session_id and session_id in sessions:
        del sessions[session_id]
        return Response(status_code=204)
    return JSONResponse({"error": "Session not found"}, status_code=404)


@app.post("/edge/enroll")
async def edge_enroll(request: Request):
    payload, error_response = await _request_json_or_error(request)
    if error_response:
        return error_response
    try:
        result = runtime.enroll_machine(
            code=str(payload.get("code") or ""),
            machine_id=str(payload.get("machine_id") or ""),
            display_name=str(payload.get("display_name") or ""),
            tags=payload.get("tags") or [],
            role=str(payload.get("role") or ""),
            capabilities=payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else None,
            workspaces=payload.get("workspaces") if isinstance(payload.get("workspaces"), list) else None,
        )
        return JSONResponse(result)
    except ValueError as error:
        return JSONResponse({"error": public_error_message(error, allow_details=True)}, status_code=400)


@app.post("/edge/heartbeat")
async def edge_heartbeat(request: Request):
    payload, error_response = await _request_json_or_error(request)
    if error_response:
        return error_response
    try:
        machine_id, token = _edge_auth(payload, request)
        result = runtime.heartbeat(
            machine_id=machine_id,
            token=token,
            capabilities=payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else None,
            workspaces=payload.get("workspaces") if isinstance(payload.get("workspaces"), list) else None,
            worker_status=payload.get("worker_status") if isinstance(payload.get("worker_status"), dict) else None,
            resource_status=payload.get("resource_status") if isinstance(payload.get("resource_status"), dict) else None,
        )
        return JSONResponse(result)
    except ValueError as error:
        return JSONResponse({"error": public_error_message(error, allow_details=True)}, status_code=401)


@app.post("/edge/poll")
async def edge_poll(request: Request):
    payload, error_response = await _request_json_or_error(request)
    if error_response:
        return error_response
    try:
        machine_id, token = _edge_auth(payload, request)
        return JSONResponse(runtime.claim_next_command(machine_id=machine_id, token=token))
    except ValueError as error:
        return JSONResponse({"error": public_error_message(error, allow_details=True)}, status_code=401)


@app.post("/edge/result")
async def edge_result(request: Request):
    payload, error_response = await _request_json_or_error(request)
    if error_response:
        return error_response
    try:
        machine_id, token = _edge_auth(payload, request)
        return JSONResponse(
            runtime.finish_command(
                machine_id=machine_id,
                token=token,
                command_id=str(payload.get("command_id") or ""),
                result=payload.get("result") if isinstance(payload.get("result"), dict) else None,
                error=str(payload.get("error") or ""),
            )
        )
    except ValueError as error:
        return JSONResponse({"error": public_error_message(error, allow_details=True)}, status_code=400)


@app.on_event("startup")
async def startup_event():
    logger.info("PatchBay Hub starting...")
    logger.info("Endpoint: http://%s:%s/mcp", config["server"]["host"], config["server"]["port"])


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=config["server"]["host"],
        port=int(config["server"]["port"]),
        log_level="info",
        access_log=bool(config.get("logging", {}).get("access_log", False)),
    )


if __name__ == "__main__":
    main()
