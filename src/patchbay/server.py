"""
PatchBay - Streamable HTTP transport implementation.
Exposes selected Codex CLI workflows to MCP-compatible clients.

This implementation follows the MCP specification for Streamable HTTP transport:
- Single /mcp endpoint for all JSON-RPC communication
- Mcp-Session-Id header for session management
- Standard request/response semantics
"""
import asyncio
import json
import logging
import os
import time
import uuid
import yaml
from pathlib import Path
from typing import Dict, Optional, Any

from patchbay.auth import (
    AuthConfigurationError,
    auth_public_metadata,
    build_auth_policy,
    request_is_authorized,
    request_token,
)
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from patchbay.jobs.manager import JobManager
from patchbay.jobs.executor import JobExecutor
from patchbay.protocol.context import RequestContext, make_client_ref, make_hashed_ref
from patchbay.protocol.mcp import MCPProtocol
from patchbay.connector.profiles import normalize_logging_paths
from patchbay.evidence import EvidenceRecorder
from patchbay.security import internal_log_error
from patchbay.tools.handler import ToolHandler

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load configuration
def _default_config_path() -> Path:
    candidates = [
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parents[2] / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


config_path = Path(os.environ.get("PATCHBAY_CONFIG", _default_config_path()))
with open(config_path) as f:
    config = yaml.safe_load(f)
normalize_logging_paths(config)

try:
    auth_policy = build_auth_policy(config)
except AuthConfigurationError as exc:
    raise RuntimeError(str(exc)) from exc

# Setup audit logging
audit_log_path = Path(config['logging']['audit_file'])
audit_log_path.parent.mkdir(parents=True, exist_ok=True)

audit_logger = logging.getLogger('audit')
audit_handler = logging.FileHandler(audit_log_path)
audit_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)
evidence_recorder = EvidenceRecorder(config)

# Initialize app
app = FastAPI(title="PatchBay")

# CORS is disabled by default. Enable only for trusted local UIs.
if config.get("server", {}).get("enable_cors", False):
    allowed_origins = config.get("server", {}).get(
        "allowed_origins",
        ["http://127.0.0.1:3000", "http://localhost:3000"],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["POST", "GET", "DELETE"],
        allow_headers=["Content-Type", "Mcp-Session-Id", "Authorization"],
        expose_headers=["Mcp-Session-Id"],
    )

# Initialize components
job_manager = JobManager(config)
job_executor = JobExecutor(config, job_manager)
tool_handler = ToolHandler(config, job_manager, job_executor)
mcp_protocol = MCPProtocol(config, tool_handler)

# Session management for Streamable HTTP
# Maps session_id -> session data (can store per-session state if needed)
sessions: Dict[str, Dict[str, Any]] = {}
work_runs: Dict[str, Dict[str, Any]] = {}
_SESSION_REF_SALT = os.environ.get("PATCHBAY_SESSION_REF_SALT") or uuid.uuid4().hex
DEFAULT_WORK_RUN_IDLE_SECONDS = 900


class RequestBodyTooLarge(Exception):
    """Raised when an MCP request body exceeds configured limits."""


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        content={"error": "Unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unknown_session_response() -> JSONResponse:
    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "error": {"code": -32001, "message": "Unknown or expired MCP session"},
            "id": None,
        },
        status_code=404,
    )


def _mcp_session_id(request: Request) -> Optional[str]:
    # Headers are case-insensitive, but keep both spellings visible because MCP
    # docs and clients have used both forms.
    return request.headers.get("Mcp-Session-Id") or request.headers.get("MCP-Session-Id")


def _request_context_for_session(session_id: str) -> RequestContext:
    return RequestContext.from_session(
        session_id,
        sessions[session_id],
        salt=_SESSION_REF_SALT,
        active_mcp_sessions=len(sessions),
    )


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

    return make_client_ref("server-owner", salt=_SESSION_REF_SALT), "server"


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


def _work_run_idle_seconds() -> int:
    app_config = config.get("app", {}) if isinstance(config.get("app"), dict) else {}
    raw = app_config.get("work_run_idle_seconds") or app_config.get("conversation_work_run_idle_seconds")
    try:
        value = int(raw if raw is not None else DEFAULT_WORK_RUN_IDLE_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_WORK_RUN_IDLE_SECONDS
    return max(60, value)


def _is_work_activity(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    method = str(message.get("method") or "")
    if method != "tools/call":
        return False
    return bool(_tool_name_from_message(message))


def _work_run_key(session_data: Dict[str, Any]) -> str:
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


def _max_request_bytes() -> int:
    configured = int(config.get("server", {}).get("max_request_bytes", 1_048_576))
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

    return json.loads(body.decode("utf-8"))


def _authorize_request(request: Request) -> Optional[JSONResponse]:
    if request_is_authorized(auth_policy, request.headers, request.query_params):
        return None
    logger.warning("Unauthorized request rejected: method=%s path=%s", request.method, request.url.path)
    return _unauthorized_response()


@app.get("/")
async def root(request: Request):
    """Health check endpoint"""
    unauthorized = _authorize_request(request)
    if unauthorized:
        return unauthorized
    return {
        "name": "patchbay",
        "version": "0.1.0",
        "transport": "streamable-http",
        "status": "running",
        "active_operations": len([j for j in job_manager.jobs.values() if j.state.value == "running"]),
        "active_sessions": len(sessions),
        "auth": auth_public_metadata(auth_policy),
    }


@app.get("/status")
async def status(request: Request):
    """Server status endpoint"""
    unauthorized = _authorize_request(request)
    if unauthorized:
        return unauthorized
    return {
        "server": "healthy",
        "transport": "streamable-http",
        "auth": auth_public_metadata(auth_policy),
        "jobs": {
            "total": len(job_manager.jobs),
            "pending": len([j for j in job_manager.jobs.values() if j.state.value == "pending"]),
            "running": len([j for j in job_manager.jobs.values() if j.state.value == "running"]),
            "completed": len([j for j in job_manager.jobs.values() if j.state.value == "completed"]),
            "failed": len([j for j in job_manager.jobs.values() if j.state.value == "failed"])
        },
        "sessions": len(sessions)
    }


@app.get("/mcp")
async def mcp_get(request: Request):
    """GET handler for /mcp - stops 405 spam from probing clients"""
    unauthorized = _authorize_request(request)
    if unauthorized:
        return unauthorized
    return JSONResponse(
        content={"transport": "streamable-http", "message": "Use POST /mcp for JSON-RPC"},
        status_code=200,
    )


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """
    Streamable HTTP MCP endpoint.
    
    This is the single endpoint for all MCP JSON-RPC communication.
    - Client POSTs JSON-RPC messages
    - Server returns JSON-RPC responses in HTTP response body
    - Session management via Mcp-Session-Id header
    """
    unauthorized = _authorize_request(request)
    if unauthorized:
        return unauthorized

    # Get or create session ID
    session_id = _mcp_session_id(request)
    is_new_session = False
    
    if not session_id:
        # New session - generate ID
        session_id = str(uuid.uuid4())
        is_new_session = True
        owner_ref, owner_scope = _owner_ref_for_request(request, session_id)
        sessions[session_id] = {
            "created_at": asyncio.get_event_loop().time(),
            "last_activity": asyncio.get_event_loop().time(),
            "client_ref": make_client_ref(session_id, salt=_SESSION_REF_SALT),
            "owner_ref": owner_ref,
            "owner_scope": owner_scope,
        }
        logger.info("New MCP session created: %s", make_client_ref(session_id, salt=_SESSION_REF_SALT))
    elif session_id not in sessions:
        logger.warning("Rejected unknown MCP session ID: %s", make_client_ref(session_id, salt=_SESSION_REF_SALT))
        return _unknown_session_response()
    else:
        # Update last activity
        sessions[session_id]["last_activity"] = asyncio.get_event_loop().time()
        owner_ref, owner_scope = _owner_ref_for_request(request, session_id)
        sessions[session_id]["owner_ref"] = owner_ref
        sessions[session_id]["owner_scope"] = owner_scope
    
    # Parse request body
    try:
        message = await _read_limited_json(request)
    except RequestBodyTooLarge as e:
        logger.warning("Request body too large: %s", e)
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32000, "message": "Request body too large"},
                "id": None,
            },
            status_code=413,
            headers={"Mcp-Session-Id": session_id},
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("Invalid JSON request body: %s", internal_log_error(e))
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None
            },
            status_code=400,
            headers={"Mcp-Session-Id": session_id}
        )

    _apply_chatgpt_client_metadata(session_id, message)
    request_context = _request_context_for_session(session_id)
    
    # Log audit metadata only by default. Prompt/response bodies can contain secrets.
    params = message.get("params", {}) if isinstance(message, dict) else {}
    audit_logger.info(
        "[%s] method=%s id=%s tool=%s",
        request_context.client_ref,
        message.get("method"),
        message.get("id"),
        params.get("name"),
    )
    evidence_recorder.record_mcp_event(
        client_ref=request_context.client_ref,
        owner_ref=request_context.owner_ref,
        direction="request",
        message=message,
    )
    
    # Handle MCP message
    try:
        response = await mcp_protocol.handle_message(message, context=request_context)
        
        if response:
            evidence_recorder.record_mcp_event(
                client_ref=request_context.client_ref,
                owner_ref=request_context.owner_ref,
                direction="response",
                response=response,
                status_code=200,
            )
            if config.get("logging", {}).get("log_response_bodies", False):
                audit_logger.info("[%s] response=%s", session_id, json.dumps(response))
            
            # Return JSON-RPC response with session header
            return JSONResponse(
                content=response,
                headers={"Mcp-Session-Id": session_id}
            )
        else:
            evidence_recorder.record_mcp_event(
                client_ref=request_context.client_ref,
                owner_ref=request_context.owner_ref,
                direction="response",
                status_code=204,
            )
            # Notification - no response expected (return 204 or empty)
            return Response(
                status_code=204,
                headers={"Mcp-Session-Id": session_id}
            )
            
    except Exception as e:
        logger.error("Message handling error: %s", internal_log_error(e))
        error_response = {
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": "Internal processing error"},
            "id": message.get("id"),
        }
        evidence_recorder.record_mcp_event(
            client_ref=request_context.client_ref,
            owner_ref=request_context.owner_ref,
            direction="response",
            response=error_response,
            status_code=500,
        )
        return JSONResponse(
            content=error_response,
            status_code=500,
            headers={"Mcp-Session-Id": session_id}
        )


@app.delete("/mcp")
async def mcp_session_delete(request: Request):
    """
    Delete/close an MCP session.
    Per MCP spec, clients can DELETE to close a session.
    """
    unauthorized = _authorize_request(request)
    if unauthorized:
        return unauthorized

    session_id = _mcp_session_id(request)
    
    if session_id and session_id in sessions:
        del sessions[session_id]
        logger.info(f"Session deleted: {session_id}")
        return Response(status_code=204)
    
    return JSONResponse(
        content={"error": "Session not found"},
        status_code=404
    )


# Keep legacy /sse endpoint for backwards compatibility / transport probing
@app.get("/sse")
async def sse_legacy(request: Request):
    """
    Legacy SSE endpoint.
    Returns 410 Gone to indicate this transport is no longer supported.
    Clients should use Streamable HTTP at /mcp instead.
    """
    return JSONResponse(
        content={
            "error": "SSE transport deprecated",
            "message": "Please use Streamable HTTP transport at /mcp",
            "specification": "https://modelcontextprotocol.io/specification/2025-03-26/basic/transports"
        },
        status_code=410
    )


@app.on_event("startup")
async def startup_event():
    """Server startup tasks"""
    logger.info("PatchBay starting (Streamable HTTP transport)...")
    logger.info(f"Max concurrent jobs: {config['server']['max_concurrent_jobs']}")
    logger.info("HTTP auth: %s", "enabled" if auth_policy.enabled else "disabled")
    logger.info(f"Endpoint: http://{config['server']['host']}:{config['server']['port']}/mcp")
    
    # Start cleanup task
    asyncio.create_task(periodic_cleanup())


@app.on_event("shutdown")
async def shutdown_event():
    """Server shutdown tasks"""
    logger.info("PatchBay shutting down...")
    
    # Preserve durable records for inspection after restart while stopping live subprocesses.
    await job_executor.cancel_all_running("Server shut down before the job completed.")
    
    # Clear sessions
    sessions.clear()


async def periodic_cleanup():
    """Periodically clean up old jobs and stale sessions"""
    while True:
        try:
            await asyncio.sleep(3600)  # Every hour
            
            # Clean up old jobs
            job_manager.cleanup_old_jobs()
            
            # Clean up stale sessions (older than 24 hours)
            current_time = asyncio.get_event_loop().time()
            stale_sessions = [
                sid for sid, data in sessions.items()
                if current_time - data.get("last_activity", 0) > 86400
            ]
            for sid in stale_sessions:
                del sessions[sid]
                logger.info(f"Cleaned up stale session: {sid}")

            stale_run_keys = [
                key for key, data in work_runs.items()
                if current_time - data.get("last_activity_monotonic", 0) > 86400
            ]
            for key in stale_run_keys:
                del work_runs[key]
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Cleanup error: %s", internal_log_error(e))


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host=config['server']['host'],
        port=config['server']['port'],
        log_level="info",
        access_log=bool(config.get("logging", {}).get("access_log", False)),
    )
