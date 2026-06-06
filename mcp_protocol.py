"""MCP protocol implementation and public tool definitions."""
import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


SERVER_INSTRUCTIONS = """
Local-first Codex CLI wrapper for repository maintenance.

Default workflow:
1. Use read-only planning before apply jobs.
2. Use apply jobs only for repositories under configured allowed roots.
3. Review diffs before merging generated changes.
4. Never pass secrets, API keys, auth files, or private customer data.
5. Mutating tools require explicit user intent.
6. Keep the server bound to localhost unless authentication and network controls are configured.
"""


CODEX_COMMON_PARAMS = {
    "model": {
        "type": "string",
        "description": "Optional Codex model override.",
    },
    "images": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional image paths to pass to Codex.",
    },
    "search": {
        "type": "boolean",
        "description": "Enable Codex web search when supported by the installed CLI.",
    },
    "features": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "enable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Codex feature flags to enable.",
            },
            "disable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Codex feature flags to disable.",
            },
        },
        "description": "Codex feature flag configuration.",
    },
    "profile": {
        "type": "string",
        "description": "Codex config profile name.",
    },
    "add_dirs": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Additional paths to include. Every path must be under configured allowed roots.",
    },
    "sandbox": {
        "type": "string",
        "enum": ["read-only", "workspace-write"],
        "description": "Codex sandbox mode. Defaults to the configured read-only sandbox.",
    },
    "approval_policy": {
        "type": "string",
        "enum": ["untrusted", "on-failure", "on-request", "never"],
        "description": "Codex approval policy.",
    },
    "network": {
        "type": "boolean",
        "description": "Enable network access when supported by the selected Codex sandbox configuration.",
    },
    "config_overrides": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Codex -c overrides. Disabled by default unless allowed in config.yaml.",
    },
    "full_auto": {
        "type": "boolean",
        "description": "Allow Codex full-auto mode when supported. Dangerous bypass is not exposed as a public tool argument.",
    },
    "structured_output": {
        "type": "boolean",
        "description": "Request structured output when supported. Default: true.",
    },
    "json_events": {
        "type": "boolean",
        "description": "Request Codex JSON event output when supported. Default: true.",
    },
}


TOOLS = [
    {
        "name": "codex_plan_job",
        "description": "Start a read-only Codex repository analysis job. Returns a job_id for status and result inspection.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "spec": {
                    "type": "string",
                    "description": "Analysis instructions for Codex.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Owned or authorized repository path under configured allowed roots.",
                },
                **CODEX_COMMON_PARAMS,
            },
            "required": ["spec"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_apply_job",
        "description": "Start a Codex apply job in an isolated git worktree. Review the resulting diff before merging.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "spec": {
                    "type": "string",
                    "description": "Change request for Codex.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Owned or authorized repository path under configured allowed roots.",
                },
                **CODEX_COMMON_PARAMS,
            },
            "required": ["spec"],
        },
        "readOnlyHint": False,
    },
    {
        "name": "codex_get_status",
        "description": "Get status for an async Codex job.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by codex_plan_job or codex_apply_job.",
                }
            },
            "required": ["job_id"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_get_result",
        "description": "Fetch a completed Codex job result. Blocks briefly while a job is still running.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by codex_plan_job or codex_apply_job.",
                }
            },
            "required": ["job_id"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_get_diff",
        "description": "Fetch a unified diff for one file from an apply job worktree.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Apply job ID.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Repository-relative file path to inspect.",
                },
            },
            "required": ["job_id", "file_path"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_review",
        "description": "Run Codex review against owned or authorized repository changes.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "spec": {
                    "type": "string",
                    "description": "Optional review instructions.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Owned or authorized repository path under configured allowed roots.",
                },
                "uncommitted": {
                    "type": "boolean",
                    "description": "Review uncommitted local changes.",
                },
                "base": {
                    "type": "string",
                    "description": "Base revision for review.",
                },
                "commit": {
                    "type": "string",
                    "description": "Commit revision for review.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional review title.",
                },
                "model": CODEX_COMMON_PARAMS["model"],
                "config_overrides": CODEX_COMMON_PARAMS["config_overrides"],
            },
            "required": [],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_resume",
        "description": "Resume a prior Codex session in an owned or authorized repository.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Codex session/thread ID to resume.",
                },
                "spec": {
                    "type": "string",
                    "description": "Optional follow-up instructions.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Owned or authorized repository path under configured allowed roots.",
                },
                "model": CODEX_COMMON_PARAMS["model"],
                "images": CODEX_COMMON_PARAMS["images"],
                "full_auto": CODEX_COMMON_PARAMS["full_auto"],
                "config_overrides": CODEX_COMMON_PARAMS["config_overrides"],
            },
            "required": ["session_id"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_interactive",
        "description": "Start a Codex exec session and return output plus a session ID when available.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "spec": {
                    "type": "string",
                    "description": "Initial Codex instructions.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Owned or authorized repository path under configured allowed roots.",
                },
                **CODEX_COMMON_PARAMS,
            },
            "required": ["spec"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_interactive_reply",
        "description": "Continue a Codex exec session with follow-up instructions.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Codex session/thread ID.",
                },
                "spec": {
                    "type": "string",
                    "description": "Follow-up instructions.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Owned or authorized repository path under configured allowed roots.",
                },
            },
            "required": ["session_id", "spec"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_get_config",
        "description": "Return redacted Codex configuration metadata and available features. Raw local config is never returned.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "readOnlyHint": True,
    },
]


PUBLIC_TOOL_NAMES = {tool["name"] for tool in TOOLS}

DEPRECATED_TOOL_ALIASES = {
    "query_text_analytics": "codex_plan_job",
    "update_content_record": "codex_apply_job",
    "check_operation_status": "codex_get_status",
    "fetch_operation_result": "codex_get_result",
    "fetch_record_delta": "codex_get_diff",
    "analyze_content_changes": "codex_review",
    "continue_session": "codex_resume",
    "start_conversational_query": "codex_interactive",
    "continue_conversational_query": "codex_interactive_reply",
    "get_system_config": "codex_get_config",
}

ARG_NAME_MAPPING = {
    "spec": "prompt",
    "repo_path": "repo",
    "data_source": "repo",
    "reference_id": "job_id",
    "record_path": "file_path",
    "session_ref": "session_id",
    "engine_variant": "model",
    "media_refs": "images",
    "enable_external_lookup": "search",
    "capability_flags": "features",
    "config_profile": "profile",
    "additional_paths": "add_dirs",
    "network_enabled": "network",
    "config_params": "config_overrides",
    "batch_mode": "full_auto",
    "output_format": "_output_format",
    "stream_events": "json_events",
    "include_pending": "uncommitted",
    "baseline": "base",
    "revision": "commit",
    "label": "title",
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(
        r"(?i)(OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|GROQ_API_KEY|GEMINI_API_KEY)\s*=\s*[^\s]+"
    ),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(token|secret|password|credential|auth)[\"'\s:=]+[^\"'\s,}]+"),
]


def resolve_public_tool_name(external_tool_name: str) -> str:
    """Resolve advertised tool names and deprecated aliases only."""
    if external_tool_name in PUBLIC_TOOL_NAMES:
        return external_tool_name
    if external_tool_name in DEPRECATED_TOOL_ALIASES:
        return DEPRECATED_TOOL_ALIASES[external_tool_name]
    raise ValueError(f"Unknown or unavailable tool: {external_tool_name}")


def redact_sensitive_output(data: Any) -> Any:
    """Redact likely secrets before returning logs, config, or subprocess output."""
    if isinstance(data, str):
        redacted = data
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED_POSSIBLE_SECRET]", redacted)
        return redacted
    if isinstance(data, dict):
        return {k: redact_sensitive_output(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_sensitive_output(v) for v in data]
    return data


def translate_arguments(external_args: Dict[str, Any]) -> Dict[str, Any]:
    """Translate compatibility argument names to internal handler arguments."""
    internal_args: Dict[str, Any] = {}

    for ext_name, value in external_args.items():
        if ext_name in ("data_source", "repo_path") and isinstance(value, str) and value.strip() == "":
            continue

        int_name = ARG_NAME_MAPPING.get(ext_name, ext_name)

        if int_name == "_output_format":
            internal_args["structured_output"] = value == "structured"
        else:
            internal_args[int_name] = value

    return internal_args


def create_pointer_response(result: Dict[str, Any], operation_type: str) -> Dict[str, Any]:
    """Transform job-creation results into small reference responses."""
    pointer = {
        "status": result.get("status", result.get("state", "unknown")),
        "operation_type": operation_type,
    }

    if result.get("job_id"):
        pointer["job_id"] = result["job_id"]
    if result.get("session_id"):
        pointer["session_id"] = result["session_id"]
    if result.get("mode"):
        pointer["mode"] = result["mode"]
    if result.get("worktree_path"):
        pointer["worktree_path"] = result["worktree_path"]
    if result.get("branch_name"):
        pointer["branch_name"] = result["branch_name"]
    if result.get("summary"):
        pointer["summary"] = result["summary"]
    if result.get("files_changed"):
        pointer["files_changed"] = result["files_changed"]

    has_error = result.get("status") == "error" or bool(result.get("error"))
    if has_error:
        pointer["status"] = "error"
        if result.get("error"):
            pointer["error"] = result["error"]
        if result.get("stderr"):
            pointer["stderr"] = result["stderr"]
        if "exit_code" in result:
            pointer["exit_code"] = result["exit_code"]

    if operation_type in {"codex_plan_job", "codex_apply_job"}:
        pointer["note"] = "Use codex_get_status and codex_get_result with job_id to inspect output."
    elif operation_type == "codex_interactive":
        pointer["note"] = "Use codex_interactive_reply with session_id to continue when a session_id is returned."

    return pointer


class MCPProtocol:
    """MCP Protocol handler implementing JSON-RPC 2.0."""

    def __init__(self, config: Dict[str, Any], tool_handler):
        self.config = config
        self.tool_handler = tool_handler
        self.server_info = {
            "name": "codex-mcp-wrapper",
            "version": "0.1.0",
        }
        self.capabilities = {
            "tools": {
                "listChanged": True,
            }
        }

    async def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle an incoming JSON-RPC 2.0 message."""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", {})

        logger.info("Handling MCP method: %s", method)

        try:
            if method == "notifications/initialized":
                logger.info("Client sent initialized notification")
                return None

            if method == "initialize":
                result = await self._handle_initialize(params)
            elif method == "tools/list":
                result = await self._handle_tools_list(params)
            elif method == "tools/call":
                result = await self._handle_tools_call(params)
            else:
                raise ValueError(f"Unknown method: {method}")

            if msg_id is not None:
                return {"jsonrpc": "2.0", "id": msg_id, "result": result}

            return None

        except ValueError as e:
            logger.warning("Invalid MCP request for %s: %s", method, e)
            if msg_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": str(e)},
                }
            return None
        except Exception as e:
            logger.exception("Error handling %s: %s", method, e)
            if msg_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": "Internal processing error"},
                }
            return None

    async def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request."""
        logger.info("MCP session initialized")
        return {
            "protocolVersion": params.get("protocolVersion", "2025-11-25"),
            "serverInfo": self.server_info,
            "capabilities": self.capabilities,
            "instructions": SERVER_INSTRUCTIONS,
        }

    async def _handle_tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/list request."""
        logger.debug("Listing %s public tools", len(TOOLS))
        return {"tools": TOOLS}

    async def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request with explicit tool resolution and redaction."""
        external_tool_name = params.get("name")
        external_arguments = params.get("arguments", {})
        if not isinstance(external_arguments, dict):
            raise ValueError("Tool arguments must be an object")

        internal_tool_name = resolve_public_tool_name(external_tool_name)
        internal_arguments = translate_arguments(external_arguments)

        logger.info("Tool call: %s -> %s", external_tool_name, internal_tool_name)
        result = await self.tool_handler.handle_tool_call(internal_tool_name, internal_arguments)

        if internal_tool_name in {"codex_plan_job", "codex_apply_job", "codex_interactive"} and "error" not in result:
            result = create_pointer_response(result, internal_tool_name)
        else:
            result = redact_sensitive_output(result)

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, indent=2),
                }
            ]
        }
