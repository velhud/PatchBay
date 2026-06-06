"""
Codex MCP Wrapper - Streamable HTTP transport implementation.
Exposes selected Codex CLI workflows to MCP-compatible clients.

This implementation follows the MCP specification for Streamable HTTP transport:
- Single /mcp endpoint for all JSON-RPC communication
- Mcp-Session-Id header for session management
- Standard request/response semantics
"""
import asyncio
import json
import logging
import uuid
import yaml
from pathlib import Path
from typing import Dict, Optional, Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from job_manager import JobManager
from job_executor import JobExecutor
from mcp_protocol import MCPProtocol
from tools import ToolHandler

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load configuration
config_path = Path(__file__).parent / 'config.yaml'
with open(config_path) as f:
    config = yaml.safe_load(f)

# Setup audit logging
audit_log_path = Path(config['logging']['audit_file'])
audit_log_path.parent.mkdir(parents=True, exist_ok=True)

audit_logger = logging.getLogger('audit')
audit_handler = logging.FileHandler(audit_log_path)
audit_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)

# Initialize app
app = FastAPI(title="Codex MCP Wrapper")

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


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "name": "codex-mcp-wrapper",
        "version": "0.1.0",
        "transport": "streamable-http",
        "status": "running",
        "active_operations": len([j for j in job_manager.jobs.values() if j.state.value == "running"]),
        "active_sessions": len(sessions)
    }


@app.get("/status")
async def status():
    """Server status endpoint"""
    return {
        "server": "healthy",
        "transport": "streamable-http",
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
async def mcp_get():
    """GET handler for /mcp - stops 405 spam from probing clients"""
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
    # Get or create session ID
    session_id = request.headers.get("Mcp-Session-Id")
    is_new_session = False
    
    if not session_id:
        # New session - generate ID
        session_id = str(uuid.uuid4())
        is_new_session = True
        sessions[session_id] = {
            "created_at": asyncio.get_event_loop().time(),
            "last_activity": asyncio.get_event_loop().time()
        }
        logger.info(f"New MCP session created: {session_id}")
    elif session_id not in sessions:
        # Unknown session ID - create it (be permissive)
        sessions[session_id] = {
            "created_at": asyncio.get_event_loop().time(),
            "last_activity": asyncio.get_event_loop().time()
        }
        logger.info(f"Accepted new session ID from client: {session_id}")
    else:
        # Update last activity
        sessions[session_id]["last_activity"] = asyncio.get_event_loop().time()
    
    # Parse request body
    try:
        message = await request.json()
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON: {e}")
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None
            },
            status_code=400,
            headers={"Mcp-Session-Id": session_id}
        )
    
    # Log audit metadata only by default. Prompt/response bodies can contain secrets.
    params = message.get("params", {}) if isinstance(message, dict) else {}
    audit_logger.info(
        "[%s] method=%s id=%s tool=%s",
        session_id,
        message.get("method"),
        message.get("id"),
        params.get("name"),
    )
    
    # Handle MCP message
    try:
        response = await mcp_protocol.handle_message(message)
        
        if response:
            if config.get("logging", {}).get("log_response_bodies", False):
                audit_logger.info("[%s] response=%s", session_id, json.dumps(response))
            
            # Return JSON-RPC response with session header
            return JSONResponse(
                content=response,
                headers={"Mcp-Session-Id": session_id}
            )
        else:
            # Notification - no response expected (return 204 or empty)
            return Response(
                status_code=204,
                headers={"Mcp-Session-Id": session_id}
            )
            
    except Exception as e:
        logger.exception(f"Message handling error: {e}")
        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": "Internal processing error"},
                "id": message.get("id")
            },
            status_code=500,
            headers={"Mcp-Session-Id": session_id}
        )


@app.delete("/mcp")
async def mcp_session_delete(request: Request):
    """
    Delete/close an MCP session.
    Per MCP spec, clients can DELETE to close a session.
    """
    session_id = request.headers.get("Mcp-Session-Id")
    
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
    logger.info("Codex MCP Wrapper starting (Streamable HTTP transport)...")
    logger.info(f"Default repository: {config['repositories']['default']}")
    logger.info(f"Max concurrent jobs: {config['server']['max_concurrent_jobs']}")
    logger.info(f"Endpoint: http://{config['server']['host']}:{config['server']['port']}/mcp")
    
    # Start cleanup task
    asyncio.create_task(periodic_cleanup())


@app.on_event("shutdown")
async def shutdown_event():
    """Server shutdown tasks"""
    logger.info("Codex MCP Wrapper shutting down...")
    
    # Clean up all jobs
    for job_id in list(job_manager.jobs.keys()):
        job_manager.cleanup_job(job_id)
    
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
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Cleanup error: {e}")


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host=config['server']['host'],
        port=config['server']['port'],
        log_level="info"
    )
