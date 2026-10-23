"""MCP protocol implementation and public tool definitions."""
import copy
import inspect
import json
from patchbay.mcp_versions import PREFERRED_MCP_VERSION, select_mcp_version
import logging
import re
from typing import Any, Dict, Optional

from patchbay.protocol.context import RequestContext
from patchbay.protocol.result_text import decision_text
from patchbay.protocol.resources import TOOL_CARD_URI, list_resources, read_resource, tool_cards_enabled
from patchbay.pro_requests.tool_surface import install_pro_request_tool_surface
from patchbay.security import internal_log_error, public_error_message
from patchbay.workers.tool_surface import install_worker_tool_surface

logger = logging.getLogger(__name__)
MAX_MCP_TEXT_RESULT_CHARS = 12_000
WORKER_RESULT_TOOLS = {
    "codex_worker_start",
    "codex_worker_message",
    "codex_worker_list",
    "codex_worker_status",
    "codex_worker_wait",
    "codex_worker_inspect",
    "codex_worker_integrate",
    "codex_worker_stop",
}


from patchbay.protocol.tool_catalog import *  # noqa: F401,F403
from patchbay.protocol.tool_catalog import (  # preserve historical helper imports
    _alias_input_schema, _boolean_arg, _file_alias_properties, _integer_arg,
    _repo_selector_properties, _string_arg, _string_array_arg,
)


def _tool_title(tool_name: str) -> str:
    words = tool_name.removeprefix("codex_").split("_")
    return "Codex " + " ".join(word.upper() if word == "mcp" else word.capitalize() for word in words)


def _tool_display_id(tool_name: str) -> str:
    return tool_name.removeprefix("codex_")


def _build_tool_annotations(tool: Dict[str, Any]) -> Dict[str, bool]:
    read_only = bool(tool.get("readOnlyHint", False))
    return {
        "readOnlyHint": read_only,
        "destructiveHint": tool["name"] in DESTRUCTIVE_TOOLS,
        "openWorldHint": tool["name"] in OPEN_WORLD_TOOLS,
        "idempotentHint": read_only and tool["name"] not in NON_IDEMPOTENT_TOOLS,
    }


def _build_tool_meta(tool_name: str, *, tool_cards: bool = False) -> Dict[str, Any]:
    invoking, invoked = TOOL_INVOCATION_STATUS.get(tool_name, ("Running tool", "Tool complete"))
    meta: Dict[str, Any] = {"securitySchemes": APP_SECURITY_SCHEMES}
    if tool_cards:
        meta.update(
            {
                "ui": {"resourceUri": TOOL_CARD_URI},
                "openai/outputTemplate": TOOL_CARD_URI,
                "openai/toolInvocation/invoking": invoking,
                "openai/toolInvocation/invoked": invoked,
            }
        )
    return meta


def enrich_tool_descriptor(tool: Dict[str, Any], *, tool_cards: bool = False) -> Dict[str, Any]:
    """Add ChatGPT Apps metadata while preserving the core MCP descriptor."""
    descriptor = dict(tool)
    descriptor.setdefault("title", _tool_title(tool["name"]))
    descriptor.setdefault("outputSchema", copy.deepcopy(TOOL_OUTPUT_SCHEMAS.get(tool["name"], GENERIC_OBJECT_OUTPUT_SCHEMA)))
    descriptor["securitySchemes"] = APP_SECURITY_SCHEMES
    descriptor["annotations"] = _build_tool_annotations(tool)

    existing_meta = dict(tool.get("_meta", {}))
    existing_meta.update(_build_tool_meta(tool["name"], tool_cards=tool_cards))
    descriptor["_meta"] = existing_meta

    return descriptor


def _alias_title(alias_name: str) -> str:
    return "PatchBay " + " ".join(word.capitalize() for word in alias_name.split("_"))


def alias_tool_descriptor(alias_name: str, canonical_name: str, *, tool_cards: bool = False) -> Dict[str, Any]:
    """Expose compatibility names without making them the internal architecture."""
    canonical = TOOLS_BY_NAME[canonical_name]
    input_schema = ALIAS_INPUT_SCHEMAS.get(alias_name, canonical["inputSchema"])
    canonical_description = str(canonical.get("description") or "")
    descriptor = enrich_tool_descriptor(
        {
            "name": alias_name,
            "title": _alias_title(alias_name),
            "description": (
                f"Compatibility alias for {canonical_name}. Same manager-first policy and side effects as "
                f"{canonical_name}: {canonical_description} Argument names are adapted for this alias."
            ),
            "inputSchema": copy.deepcopy(input_schema),
            "readOnlyHint": canonical["readOnlyHint"],
        },
        tool_cards=tool_cards,
    )
    descriptor["outputSchema"] = copy.deepcopy(TOOL_OUTPUT_SCHEMAS.get(canonical_name, GENERIC_OBJECT_OUTPUT_SCHEMA))
    descriptor["_meta"]["codex/canonicalTool"] = canonical_name
    descriptor["_meta"]["codex/aliasSource"] = "compatibility"
    return descriptor


def public_tool_descriptors(*, tool_cards: bool = False) -> list[Dict[str, Any]]:
    return [enrich_tool_descriptor(tool, tool_cards=tool_cards) for tool in TOOLS] + [
        alias_tool_descriptor(alias, canonical, tool_cards=tool_cards)
        for alias, canonical in COMPATIBILITY_TOOL_ALIASES.items()
        if canonical in TOOLS_BY_NAME
    ]


PUBLIC_TOOL_DESCRIPTORS = public_tool_descriptors(tool_cards=False)
PUBLIC_TOOL_DESCRIPTORS_BY_NAME = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}
PUBLIC_TOOL_DESCRIPTORS_WITH_CARDS = public_tool_descriptors(tool_cards=True)
PUBLIC_TOOL_DESCRIPTORS_WITH_CARDS_BY_NAME = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS_WITH_CARDS}

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
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED_POSSIBLE_SECRET]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_POSSIBLE_SECRET]"),
    (
        re.compile(r"(?i)(OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|GROQ_API_KEY|GEMINI_API_KEY)\s*=\s*[^\s]+"),
        "[REDACTED_POSSIBLE_SECRET]",
    ),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+"), r"\1[REDACTED_POSSIBLE_SECRET]"),
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:token|secret|password|credential|auth)[A-Za-z0-9_]*[\"']?\s*[:=]\s*)"
            r"(?!true\b|false\b|null\b)[^\"'\s,}&]+"
        ),
        r"\1[REDACTED_POSSIBLE_SECRET]",
    ),
]


def resolve_public_tool_name(external_tool_name: str) -> str:
    """Resolve advertised tool names and deprecated aliases only."""
    if external_tool_name in TOOLS_BY_NAME:
        return external_tool_name
    if external_tool_name in COMPATIBILITY_TOOL_ALIASES:
        return COMPATIBILITY_TOOL_ALIASES[external_tool_name]
    if external_tool_name in DEPRECATED_TOOL_ALIASES:
        return DEPRECATED_TOOL_ALIASES[external_tool_name]
    raise ValueError(f"Unknown or unavailable tool: {external_tool_name}")


def _json_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def _schema_type_matches(expected_type: str, value: Any) -> bool:
    actual_type = _json_type_name(value)
    if expected_type == "number":
        return actual_type in {"integer", "number"}
    return actual_type == expected_type


def _validate_value_against_schema(name: str, value: Any, schema: Dict[str, Any]) -> None:
    """Validate the subset of JSON Schema used by public MCP tool descriptors."""
    expected_type = schema.get("type")
    if expected_type and not _schema_type_matches(expected_type, value):
        raise ValueError(f"Invalid type for argument '{name}': expected {expected_type}, got {_json_type_name(value)}")

    enum_values = schema.get("enum")
    if enum_values is not None and value not in enum_values:
        allowed = ", ".join(str(item) for item in enum_values)
        raise ValueError(f"Invalid value for argument '{name}': expected one of {allowed}")

    if expected_type == "array":
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_value_against_schema(f"{name}[{index}]", item, item_schema)

    if expected_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)

        for required_name in required:
            if required_name not in value:
                raise ValueError(f"Missing required argument '{name}.{required_name}'")

        if additional is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValueError(f"Unknown argument '{name}.{unknown[0]}'")

        for child_name, child_value in value.items():
            child_schema = properties.get(child_name)
            if child_schema:
                _validate_value_against_schema(f"{name}.{child_name}", child_value, child_schema)

        _validate_any_of_required(value, schema.get("anyOf", []), prefix=name)


def _required_option_label(required_names: list[str], *, prefix: str = "") -> str:
    labels = []
    for required_name in required_names:
        labels.append(f"'{prefix}.{required_name}'" if prefix else f"'{required_name}'")
    return " and ".join(labels)


def _validate_any_of_required(value: Dict[str, Any], any_of: list[Dict[str, Any]], *, prefix: str = "") -> None:
    """Validate the small anyOf/required subset used by compatibility alias schemas."""
    if not any_of:
        return

    required_options = [option.get("required", []) for option in any_of if option.get("required")]
    if not required_options:
        return

    if any(all(required_name in value for required_name in required_names) for required_names in required_options):
        return

    labels = [_required_option_label(list(required_names), prefix=prefix) for required_names in required_options]
    raise ValueError(f"Missing required argument {' or '.join(labels)}")


def _unknown_argument_message(tool_name: str, argument_name: str) -> str:
    """Return a tool-specific validation hint for common ChatGPT schema mistakes."""
    return f"Unknown argument '{argument_name}'"


def validate_public_tool_arguments(tool_name: str, external_args: Dict[str, Any]) -> None:
    """Validate advertised public tool arguments before internal name translation."""
    tool = PUBLIC_TOOL_DESCRIPTORS_BY_NAME.get(tool_name) or TOOLS_BY_NAME.get(tool_name)
    if not tool:
        raise ValueError(f"Unknown or unavailable tool: {tool_name}")

    schema = tool.get("inputSchema", {})
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)

    for required_name in required:
        if required_name not in external_args:
            raise ValueError(f"Missing required argument '{required_name}'")

    if additional is False:
        unknown = sorted(set(external_args) - set(properties))
        if unknown:
            raise ValueError(_unknown_argument_message(tool_name, unknown[0]))

    for arg_name, value in external_args.items():
        arg_schema = properties.get(arg_name)
        if arg_schema:
            _validate_value_against_schema(arg_name, value, arg_schema)

    _validate_any_of_required(external_args, schema.get("anyOf", []))


def redact_sensitive_output(data: Any) -> Any:
    """Redact likely secrets before returning logs, config, or subprocess output."""
    if isinstance(data, str):
        redacted = data
        for pattern, replacement in SECRET_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    if isinstance(data, dict):
        return {k: redact_sensitive_output(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_sensitive_output(v) for v in data]
    return data


def translate_arguments(external_args: Dict[str, Any], external_tool_name: str | None = None) -> Dict[str, Any]:
    """Translate compatibility argument names to internal handler arguments."""
    internal_args: Dict[str, Any] = {}
    alias_specific = _alias_argument_mapping(external_tool_name)

    for ext_name, value in external_args.items():
        if ext_name in ("data_source", "repo_path") and isinstance(value, str) and value.strip() == "":
            continue

        int_name = alias_specific.get(ext_name, ARG_NAME_MAPPING.get(ext_name, ext_name))

        if int_name == "_output_format":
            internal_args["structured_output"] = value == "structured"
        else:
            internal_args[int_name] = value

    return internal_args


def _alias_argument_mapping(external_tool_name: str | None) -> Dict[str, str]:
    if external_tool_name not in COMPATIBILITY_TOOL_ALIASES:
        return {}
    mapping = {
        "root": "repo",
        "workspace_root": "repo",
        "repo_path": "repo",
    }
    if external_tool_name == "open_workspace":
        mapping["path"] = "repo"
        mapping["bootstrap_context"] = "_ignored_bootstrap_context"
    if external_tool_name in {"open_workspace", "workspace_snapshot"}:
        mapping["max_files"] = "max_entries"
    if external_tool_name in {"read", "write", "edit", "git_diff", "show_changes"}:
        mapping["path"] = "file_path"
    if external_tool_name in {"bash"}:
        mapping["cmd"] = "command"
    if external_tool_name in {"handoff_to_agent", "handoff_to_codex"}:
        mapping["task"] = "plan"
    if external_tool_name == "handoff_to_codex":
        mapping["agent"] = "_ignored_agent"
    return mapping


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

    if operation_type in {"codex_plan_job", "codex_apply_job", "codex_resume", "codex_interactive_reply"}:
        pointer["note"] = "Use codex_get_status and codex_get_result with job_id to inspect output."
    elif operation_type == "codex_interactive":
        pointer["note"] = "Use codex_interactive_reply with session_id to continue when a session_id is returned."

    return pointer


def _normalize_tool_mode(raw: Any) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in TOOL_MODE_CANONICAL else ""


def configured_tool_mode(config: Dict[str, Any]) -> str:
    raw = (
        config.get("app", {}).get("tool_mode")
        or config.get("mcp", {}).get("tool_mode")
        or config.get("server", {}).get("tool_mode")
        or "worker"
    )
    return _normalize_tool_mode(raw) or "worker"


def effective_tool_mode(config: Dict[str, Any], context: Optional[RequestContext] = None) -> str:
    """Return the current request's effective tool mode."""
    if context and context.session_data is not None:
        session_mode = _normalize_tool_mode(context.session_data.get("tool_mode"))
        if session_mode:
            return session_mode
    if context:
        context_mode = _normalize_tool_mode(context.tool_mode)
        if context_mode:
            return context_mode
    return configured_tool_mode(config)


def tool_is_available(config: Dict[str, Any], external_tool_name: str, *, mode: Optional[str] = None) -> bool:
    mode = _normalize_tool_mode(mode) or configured_tool_mode(config)
    if mode == "worker" and external_tool_name in COMPATIBILITY_TOOL_ALIASES:
        return False
    canonical = COMPATIBILITY_TOOL_ALIASES.get(external_tool_name, external_tool_name)
    return canonical in TOOL_MODE_CANONICAL[mode] and runtime_capability_enabled(config, canonical)


def runtime_capability_enabled(config: Dict[str, Any], canonical_tool_name: str) -> bool:
    """Return whether runtime config can actually execute this canonical tool."""
    power = config.get("power_tools")
    power_tools = power if isinstance(power, dict) else {}

    if canonical_tool_name in DIRECT_WRITE_TOOLS:
        return bool(power_tools.get("direct_write", False))

    if canonical_tool_name in BASH_POWER_TOOLS:
        return str(power_tools.get("bash_mode", "off")).strip().lower() in {"safe", "full"}

    if canonical_tool_name in SESSION_READ_POWER_TOOLS:
        return bool(power_tools.get("codex_session_read", False))

    return True


def tool_descriptors_for_mode(config: Dict[str, Any], *, mode: Optional[str] = None) -> list[Dict[str, Any]]:
    resolved_mode = _normalize_tool_mode(mode) or configured_tool_mode(config)
    descriptors = PUBLIC_TOOL_DESCRIPTORS_WITH_CARDS if tool_cards_enabled(config) else PUBLIC_TOOL_DESCRIPTORS
    return [
        descriptor
        for descriptor in descriptors
        if tool_is_available(config, descriptor["name"], mode=resolved_mode)
    ]


TOOL_MODE_DISPLAY_ORDER = ("worker", "standard", "full", "minimal")
TOOL_MODE_PURPOSES = {
    "worker": (
        "Recommended ChatGPT default. Worker-first context plus named Codex worker lifecycle; hides "
        "low-level job/session controls and compatibility aliases."
    ),
    "standard": "Worker tools plus core workspace, handoff, direct edit, command, and async job controls.",
    "full": "Everything in standard plus raw Codex session/review controls and compatibility aliases.",
    "minimal": "Small legacy compatibility surface for basic workspace operations, direct edits, and commands.",
}
TOOL_MODE_REFRESH_NOTE = (
    "A session-local mode switch changes the server's next tools/list response for the same MCP session. "
    "In ChatGPT Developer Mode, official docs only guarantee metadata updates after using the connector "
    "Refresh flow, so a running conversation may keep the old visible tool list until ChatGPT refreshes "
    "or reconnects."
)


def _tool_mode_names_in_display_order() -> list[str]:
    names = [mode for mode in TOOL_MODE_DISPLAY_ORDER if mode in TOOL_MODE_CANONICAL]
    names.extend(mode for mode in TOOL_MODE_CANONICAL if mode not in names)
    return names


def tool_mode_inventory(config: Dict[str, Any], *, current_mode: Optional[str] = None) -> Dict[str, Any]:
    """Return public information about available MCP tool modes."""
    default_mode = configured_tool_mode(config)
    current_mode = _normalize_tool_mode(current_mode) or default_mode
    modes = []
    for mode in _tool_mode_names_in_display_order():
        tool_names = [descriptor["name"] for descriptor in tool_descriptors_for_mode(config, mode=mode)]
        modes.append(
            {
                "mode": mode,
                "current": mode == current_mode,
                "tool_count": len(tool_names),
                "purpose": TOOL_MODE_PURPOSES.get(mode, "Custom tool mode."),
                "tool_names": tool_names,
            }
        )

    return {
        "current_mode": current_mode,
        "default_mode": default_mode,
        "available_modes": _tool_mode_names_in_display_order(),
        "modes": modes,
        "recommended_default": "worker",
        "persisted_to_config": False,
        "chatgpt_refresh_note": TOOL_MODE_REFRESH_NOTE,
    }


def switch_tool_mode(
    config: Dict[str, Any],
    mode: str,
    reason: Optional[str] = None,
    *,
    context: Optional[RequestContext] = None,
) -> Dict[str, Any]:
    """Switch the current MCP session's tool mode, falling back to process-local for direct calls."""
    target_mode = str(mode).strip().lower()
    if target_mode not in TOOL_MODE_CANONICAL:
        raise ValueError(f"Invalid tool mode: {mode}")

    previous_mode = effective_tool_mode(config, context)
    switch_scope = "process"
    note = "Tool mode was changed for this running server process only; config files were not modified."
    if context and context.session_data is not None:
        context.session_data["tool_mode"] = target_mode
        switch_scope = "session"
        note = "Tool mode was changed for this MCP session only; config files were not modified."
    else:
        config.setdefault("app", {})["tool_mode"] = target_mode

    inventory = tool_mode_inventory(config, current_mode=target_mode)
    inventory.update(
        {
            "previous_mode": previous_mode,
            "current_mode": target_mode,
            "changed": previous_mode != target_mode,
            "reason": reason or "",
            "persisted_to_config": False,
            "switch_scope": switch_scope,
            "note": note,
        }
    )
    return inventory


class MCPProtocol:
    """MCP Protocol handler implementing JSON-RPC 2.0."""

    def __init__(self, config: Dict[str, Any], tool_handler):
        self.config = config
        self.tool_handler = tool_handler
        self._tool_handler_accepts_context = "context" in inspect.signature(tool_handler.handle_tool_call).parameters
        self.server_info = {
            "name": "patchbay",
            "version": "0.1.0",
        }
        self.capabilities = {
            "tools": {
                "listChanged": True,
            },
            "resources": {
                "listChanged": False,
            },
        }

    async def handle_message(
        self,
        message: Dict[str, Any],
        *,
        context: Optional[RequestContext] = None,
    ) -> Optional[Dict[str, Any]]:
        """Handle an incoming JSON-RPC 2.0 message."""
        from patchbay.mcp_framing import request_identity, invalid_request
        from patchbay.workers.requests import WorkerAdmissionFault
        try:
            has_id, msg_id = request_identity(message)
        except ValueError as error:
            return invalid_request(str(error))
        if not has_id:
            return None
        context = context or RequestContext.anonymous()
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", {})

        logger.info("Handling MCP method: %s", method)

        try:
            if not isinstance(params, dict):
                raise ValueError('JSON-RPC params must be an object')
            if method == "notifications/initialized":
                logger.info("Client sent initialized notification")
                return None

            if method == "initialize":
                result = await self._handle_initialize(params, context=context)
            elif method == "tools/list":
                result = await self._handle_tools_list(params, context=context)
            elif method == "tools/call":
                result = await self._handle_tools_call(params, context=context)
            elif method == "resources/list":
                result = await self._handle_resources_list(params, context=context)
            elif method == "resources/read":
                result = await self._handle_resources_read(params, context=context)
            else:
                raise ValueError(f"Unknown method: {method}")

            if msg_id is not None:
                return {"jsonrpc": "2.0", "id": msg_id, "result": result}

            return None

        except WorkerAdmissionFault as error:
            return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': -32603,
                'message': 'Tool result unavailable; follow read-only recovery guidance', 'data': error.details}}
        except ValueError as e:
            logger.warning("Invalid MCP request for %s: %s", method, internal_log_error(e))
            if msg_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32602,
                        "message": public_error_message(e, default="Invalid request parameters.", allow_details=True),
                    },
                }
            return None
        except Exception as e:
            logger.error("Error handling %s: %s", method, internal_log_error(e))
            if msg_id is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32603, "message": "Internal processing error"},
                }
            return None

    async def _handle_initialize(
        self,
        params: Dict[str, Any],
        *,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        """Handle initialize request."""
        logger.info("MCP session initialized")
        version = select_mcp_version(params.get("protocolVersion", PREFERRED_MCP_VERSION))
        if context is not None and context.session_data is not None:
            context.session_data["protocol_version"] = version
        return {
            "protocolVersion": version,
            "serverInfo": self.server_info,
            "capabilities": self.capabilities,
            "instructions": SERVER_INSTRUCTIONS,
        }

    async def _handle_tools_list(
        self,
        params: Dict[str, Any],
        *,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        """Handle tools/list request."""
        current_mode = effective_tool_mode(self.config, context)
        descriptors = tool_descriptors_for_mode(self.config, mode=current_mode)
        logger.debug("Listing %s public tools for %s mode", len(descriptors), current_mode)
        return {"tools": descriptors}

    async def _handle_tools_call(
        self,
        params: Dict[str, Any],
        *,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        """Handle tools/call request with explicit tool resolution and redaction."""
        external_tool_name = params.get("name")
        external_arguments = params.get("arguments", {})
        if not isinstance(external_arguments, dict):
            raise ValueError("Tool arguments must be an object")
        current_mode = effective_tool_mode(self.config, context)
        if not tool_is_available(self.config, external_tool_name, mode=current_mode):
            raise ValueError(f"Tool is unavailable in {current_mode} mode: {external_tool_name}")

        internal_tool_name = resolve_public_tool_name(external_tool_name)
        if external_tool_name in PUBLIC_TOOL_DESCRIPTORS_BY_NAME:
            validate_public_tool_arguments(external_tool_name, external_arguments)
        internal_arguments = {
            key: value
            for key, value in translate_arguments(external_arguments, external_tool_name).items()
            if not key.startswith("_ignored_")
        }

        logger.info("Tool call: %s -> %s", external_tool_name, internal_tool_name)
        if internal_tool_name == "codex_tool_mode_info":
            result = tool_mode_inventory(self.config, current_mode=current_mode)
        elif internal_tool_name == "codex_tool_mode_switch":
            result = switch_tool_mode(
                self.config,
                internal_arguments["mode"],
                reason=internal_arguments.get("reason"),
                context=context,
            )
        else:
            if self._tool_handler_accepts_context:
                result = await self.tool_handler.handle_tool_call(internal_tool_name, internal_arguments, context=context)
            else:
                result = await self.tool_handler.handle_tool_call(internal_tool_name, internal_arguments)

        if (
            internal_tool_name
            in {"codex_plan_job", "codex_apply_job", "codex_interactive", "codex_resume", "codex_interactive_reply"}
            and "error" not in result
        ):
            result = create_pointer_response(result, internal_tool_name)
        result_meta = {
            "patchbay/tool_name": internal_tool_name,
            "patchbay/tool_id": _tool_display_id(internal_tool_name),
        }

        try:
            result = redact_sensitive_output(result)
            rendered = {
                "structuredContent": result,
                "content": [{"type": "text", "text": self._tool_result_text(result, internal_tool_name)}],
                "_meta": result_meta,
            }
            json.dumps(rendered, ensure_ascii=False, allow_nan=False).encode('utf-8')
            return rendered
        except Exception as error:
            if internal_tool_name not in WORKER_RESULT_TOOLS:
                raise
            from patchbay.workers.requests import WorkerAdmissionFault, fault_details
            kind = {'codex_worker_start': 'start_worker', 'codex_worker_message': 'message_worker'}.get(internal_tool_name)
            details = fault_details(getattr(self.tool_handler, 'worker_runtime', None), kind, context,
                                    internal_arguments.get('idempotency_key'))
            from patchbay.workers.tool_surface import WORKER_NON_IDEMPOTENT_TOOLS
            if internal_tool_name not in WORKER_NON_IDEMPOTENT_TOOLS:
                details.update(boundary='read_result_invalid', effect_may_exist=False,
                               guidance='Read result unavailable; no mutation was attempted. Retry a bounded read or inspect existing state.')
            raise WorkerAdmissionFault(details) from error

    def _tool_result_text(self, result: Dict[str, Any], tool_name: str) -> str:
        """Return compact human-visible text without duplicating large structured payloads."""
        if tool_name in WORKER_RESULT_TOOLS:
            return self._worker_tool_result_text(result, tool_name)
        # Preserve the existing summary boundary. Testing a literal-Unicode
        # rendering here would promote formerly summarized bulk source into
        # a second full model-visible copy.
        rendered = json.dumps(result, separators=(",", ":"))
        if len(rendered) <= MAX_MCP_TEXT_RESULT_CHARS:
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        keys = ", ".join(sorted(str(key) for key in result.keys())[:20])
        return (
            f"{_tool_display_id(tool_name)} returned structured content ({len(rendered)} JSON chars). "
            f"Top-level fields: {keys}. Full payload is in structuredContent. " + decision_text(result)
        )

    def _worker_tool_result_text(self, result: Dict[str, Any], tool_name: str) -> str:
        text = self._worker_orientation_text(result, tool_name)
        # The floor is independent of the prose: refusals, partial reads and
        # cleanup uncertainty must survive even when the normal tail is huge.
        return text[:900] + " | " + decision_text(result) + " | Full evidence: structuredContent and existing exact pages."

    def _worker_orientation_text(self, result: Dict[str, Any], tool_name: str) -> str:
        name = str(result.get("name") or result.get("worker") or result.get("summary") or _tool_display_id(tool_name))
        state = str(result.get("state") or result.get("status") or "")
        status_line = str(result.get("status_line") or "")
        note = str(result.get("note") or "")
        if result.get("poll_too_early"):
            retry = result.get("retry_after_seconds")
            retry_hint = f" Wait about {retry}s before the next monitoring check." if retry else " Wait before the next monitoring check."
            return f"Worker monitoring checked too soon; cached status returned, not a failure.{retry_hint}"
        if tool_name in {"codex_worker_status", "codex_worker_wait"}:
            summary = str(result.get("summary") or "Worker status ready")
            action = str(result.get("suggested_action") or "")
            retry = result.get("recommended_next_poll_seconds") or result.get("retry_after_seconds")
            wait_hint = f" Next poll: about {retry}s." if retry else ""
            return f"{summary}. Suggested action: {action or 'inspect'}.{wait_hint}"
        if tool_name == "codex_worker_list":
            team = result.get("team_status") if isinstance(result.get("team_status"), dict) else {}
            summary = str(team.get("summary") or f"{result.get('count', 0)} workers listed")
            return f"{summary}. Full worker list is in structuredContent."
        if result.get("stop_confirmation_required"):
            return f"Stop has not been carried out for {name}. Obtain the required authorized stop decision or continue observing. Force requires authority for this owned workload; stop does not authorize discarding files."
        pieces = [piece for piece in [name, state, status_line, note] if piece]
        if not pieces:
            pieces = [f"{_tool_display_id(tool_name)} result ready"]
        return " | ".join(pieces[:4])

    async def _handle_resources_list(
        self,
        params: Dict[str, Any],
        *,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        """Return ChatGPT Apps resource templates exposed by this server."""
        return {"resources": list_resources(self.config)}

    async def _handle_resources_read(
        self,
        params: Dict[str, Any],
        *,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        """Read a static MCP resource by URI."""
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError("resources/read requires a resource uri")
        return read_resource(uri, self.config)
