"""HTTP server for optional PatchBay Hub mode."""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
    request_is_authorized,
)
from patchbay.connector.profiles import normalize_logging_paths
from patchbay.hub.protocol import HubProtocol
from patchbay.hub.runtime import HubRuntime
from patchbay.protocol.context import RequestContext, make_client_ref
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
_SESSION_REF_SALT = os.environ.get("PATCHBAY_SESSION_REF_SALT") or uuid.uuid4().hex


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


def _request_context_for_session(session_id: str) -> RequestContext:
    return RequestContext.from_session(
        session_id,
        sessions[session_id],
        salt=_SESSION_REF_SALT,
        active_mcp_sessions=len(sessions),
    )


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
        sessions[session_id] = {
            "created_at": asyncio.get_event_loop().time(),
            "last_activity": asyncio.get_event_loop().time(),
            "client_ref": make_client_ref(session_id, salt=_SESSION_REF_SALT),
            "owner_ref": make_client_ref("hub-server-owner", salt=_SESSION_REF_SALT),
            "owner_scope": "hub",
            "tool_mode": "hub",
        }
    elif session_id not in sessions:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unknown or expired MCP session"}, "id": None},
            status_code=404,
        )
    else:
        sessions[session_id]["last_activity"] = asyncio.get_event_loop().time()

    message, error_response = await _request_json_or_error(request)
    if error_response:
        error_response.headers["Mcp-Session-Id"] = session_id
        return error_response

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
