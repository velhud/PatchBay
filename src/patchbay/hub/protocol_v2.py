"""MCP protocol adapter for the frozen Hub V2 tool contract.

It owns MCP framing and descriptor validation while an injected async handler
owns every domain decision and operation result.
"""
from __future__ import annotations

import inspect
import logging
import math
from copy import deepcopy
from typing import Any, Mapping, Protocol, runtime_checkable

from patchbay.hub.tool_surface import (
    HUB_V2_ACTION_CAPABILITY_VERSION,
    HUB_V2_CONTRACT_HASH,
    HUB_V2_CONTRACT_VERSION,
    HUB_V2_EXPECTED_TOOL_COUNT,
    HUB_V2_MANIFEST_HASH,
    HUB_V2_SCHEMA_HASH,
    HUB_V2_TOOLS_BY_NAME,
    get_hub_v2_tools,
    validate_hub_v2_registry,
)
from patchbay.protocol.context import RequestContext
from patchbay.security import redact_sensitive_output

logger = logging.getLogger(__name__)

HUB_V2_SERVER_VERSION = "0.1.0"
HUB_V2_DEFAULT_MCP_VERSION = "2025-11-25"

HUB_V2_INSTRUCTIONS = """
PatchBay Hub V2 is a manager control plane for durable Codex worker teams.
ChatGPT's default role is manager, architect, and team lead, not primary file
reader, implementer, or routine diff reviewer. Codex workers are competent
employees who investigate, design, implement, test, review, and report. For
serious work, ask which parallel worker team should be appointed and use the
configured fleet capacity when responsibilities split cleanly; do not impose an
artificial one-or-two-worker limit or reduce depth merely to save tokens, tool
calls, or worker effort.

Start with patchbay_fleet_status and patchbay_workspace_list to confirm current
usable capacity and the logical workspace. For each non-trivial user task,
follow patchbay_work_group_list -> patchbay_work_group_resume for the same task
or patchbay_work_group_create for a new task -> patchbay_work_group_status ->
patchbay_work_group_close. One user task equals one work group, not one group
per worker. Use named lanes and multiple workers when responsibilities can be
split cleanly. A group is pinned to one immutable machine generation. Do not
scatter one task across machines or reassign it merely because another machine
looks available; reassignment creates successor work and does not move live
workers, worktrees, sessions, or artifacts.

Brief workers as real colleagues. Give them the task and product purpose,
relevant current context and authority, desired outcome, scope, constraints and
non-goals, relationship to other lanes, expected deliverable, evidence and
verification requirements, and what decisions they may make. Do not force the
manager to precompute every file path: tell workers to find the relevant files
and cite evidence when that is part of the assignment. Use a shared brief plus
specific lane missions for batch starts. If a report is thin, contradictory,
missing evidence, or important, continue the same worker with
patchbay_worker_message before replacing it or manually redoing its work.

Model routing is advisory. Call patchbay_worker_options because the installed
Codex catalog is the availability and effort authority. Use GPT-5.6 Luna for
compact standard lanes, GPT-5.6 Terra for most serious investigator,
implementer, debugger, and reviewer lanes, and GPT-5.6 Sol for highest-authority
architecture, difficult synthesis, unresolved failures, sensitive judgment, or
final review. For every bounded small-worker assignment that either Spark or
GPT-5.4 Mini can handle, choose Spark first because it is dramatically faster
and uses a separate preview quota. If Spark is unavailable, quota-depleted, or
too context-constrained, immediately continue or retry the same assignment with
GPT-5.4 Mini; preserve the lane and record the fallback rather than abandoning
the task. GPT-5.4 and GPT-5.5 are availability, compatibility, or evidence-backed
regression fallbacks. Codex CLI 0.144.1 exposes ultra for supported models such as Terra
and Sol; it may delegate internally inside one worker. Prefer explicit named
PatchBay lanes when visible ownership, reports, worktrees, or integration matter.

Before every mutating call, generate an opaque stable idempotency_key and keep
it with the intended tool and logical target. Reuse that same key with the same
payload after an interruption or uncertain response. Never retry the action
with a new key merely because the first outcome is pending. Batch starts also
require one stable key for the parent and one stable key and item_id per worker.

After group create or resume, wait for strict workspace preflight and readiness
before starting workers. Use patchbay_worker_start_batch for a well-separated
team, or patchbay_worker_start for one named lane. Continue a completed worker
with patchbay_worker_message; an active turn is blocked rather than secretly
queued or steered. Manage workers by human name or immutable fleet worker
reference, not backend job ids, branch names, or worktree paths.

Monitor through patchbay_work_group_status, patchbay_worker_status, or
patchbay_worker_wait. Wait about 20-30 seconds between ordinary monitoring
checks unless the returned next action says otherwise. pending means the Hub
does not yet know the domain outcome; it is not worker success. Preserve the
operation id and use patchbay_operation_status after the recommended wait.
blocked, failed, partial, and not_found are domain envelopes, not MCP transport
errors: read their structured reason, warnings, and next_actions instead of
inventing a queue receipt or treating transport acceptance as completion.

Use workspace read/search/change tools only for focused manager orientation,
briefing, exact verification, or a tiny task. Delegate broad repository work to
workers. Before integration, inspect the worker's integration_preview, retain
its preview_token, then call patchbay_worker_integrate explicitly. Integration
applies without committing and must never be inferred from a completed turn.
Before the final answer, review the group result and close it with an explicit
outcome and disposition for every worker, or clearly leave active work running.

Continue through minor non-blocking friction and record it for PatchBay
debugging. Stop and report when the visible tool catalog cannot perform the
required workflow, workers cannot be started or continued, group/routing state
is contradictory, required mutation or integration controls are absent, or
diagnostics show a real lost/stalled execution. If ChatGPT reaches a tool-call,
generation, or context limit, do not cancel workers or abandon the group. Return
a continuation note with repo, revision, work_group_id, pinned machine, lanes,
worker names/models, completed and active work, integration/commit/push state,
issues, blockers, and exact next actions so the user can continue the same task.

Mutating calls may return pending. Before claiming success or issuing a new
mutation, follow patchbay_operation_status or the relevant status tool until the
durable outcome is known. Group close is complete only when a later authoritative
group status reports the terminal lifecycle, not merely because the close call
was transported successfully.
"""

HUB_V2_PROTOCOL_METADATA: dict[str, Any] = {
    "patchbay/contract_version": HUB_V2_CONTRACT_VERSION,
    "patchbay/action_capability_version": HUB_V2_ACTION_CAPABILITY_VERSION,
    "patchbay/tool_count": HUB_V2_EXPECTED_TOOL_COUNT,
    "patchbay/manifest_hash": HUB_V2_MANIFEST_HASH,
    "patchbay/schema_hash": HUB_V2_SCHEMA_HASH,
    "patchbay/contract_hash": HUB_V2_CONTRACT_HASH,
}


@runtime_checkable
class HubV2ToolHandler(Protocol):
    """Async domain boundary consumed by :class:`HubProtocolV2`."""

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        """Return one already-semantic canonical Hub V2 output envelope."""


class _InvalidRequest(ValueError):
    """A caller-controlled JSON-RPC or tool argument error."""


class _MethodNotFound(_InvalidRequest):
    """A syntactically valid request for an unsupported MCP method."""


class _HandlerContractError(RuntimeError):
    """The injected handler violated its trusted output contract."""


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _type_matches(value: Any, expected: str) -> bool:
    actual = _json_type(value)
    if expected == "number":
        return actual in {"integer", "number"} and not (
            isinstance(value, float) and not math.isfinite(value)
        )
    return actual == expected


def _validate_json_value(value: Any, *, path: str) -> None:
    """Reject Python-only values that cannot cross a JSON-RPC transport."""
    value_type = _json_type(value)
    if value_type not in {"null", "boolean", "string", "object", "array", "integer", "number"}:
        raise _InvalidRequest(f"{path}: value is not JSON-compatible ({value_type})")
    if isinstance(value, float) and not math.isfinite(value):
        raise _InvalidRequest(f"{path}: number must be finite")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _InvalidRequest(f"{path}: object field names must be strings")
            _validate_json_value(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, path=f"{path}[{index}]")


def _branch_matches(value: Any, schema: Mapping[str, Any]) -> bool:
    try:
        _validate_schema(value, schema, path="$")
    except _InvalidRequest:
        return False
    return True


def _validate_schema(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    """Validate the JSON Schema keywords used by the frozen Hub V2 registry."""
    expected = schema.get("type")
    if isinstance(expected, str) and not _type_matches(value, expected):
        raise _InvalidRequest(f"{path}: expected {expected}, got {_json_type(value)}")
    if isinstance(expected, list) and not any(_type_matches(value, item) for item in expected):
        raise _InvalidRequest(f"{path}: value does not match any allowed type")

    if "const" in schema and value != schema["const"]:
        raise _InvalidRequest(f"{path}: expected constant value {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        raise _InvalidRequest(f"{path}: expected one of {allowed}")

    all_of = schema.get("allOf", [])
    if isinstance(all_of, list):
        for branch in all_of:
            _validate_schema(value, branch, path=path)

    any_of = schema.get("anyOf", [])
    if isinstance(any_of, list) and any_of and not any(_branch_matches(value, branch) for branch in any_of):
        raise _InvalidRequest(f"{path}: value does not satisfy any allowed schema")

    one_of = schema.get("oneOf", [])
    if isinstance(one_of, list) and one_of:
        matches = sum(_branch_matches(value, branch) for branch in one_of)
        if matches != 1:
            raise _InvalidRequest(f"{path}: value must satisfy exactly one allowed schema")

    condition = schema.get("if")
    if isinstance(condition, Mapping):
        selected = schema.get("then") if _branch_matches(value, condition) else schema.get("else")
        if isinstance(selected, Mapping):
            _validate_schema(value, selected, path=path)

    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            properties = {}
        required = schema.get("required", [])
        if isinstance(required, list):
            for field in required:
                if field not in value:
                    raise _InvalidRequest(f"{path}: missing required field {field!r}")

        additional = schema.get("additionalProperties", True)
        for field, child in value.items():
            child_path = f"{path}.{field}"
            child_schema = properties.get(field)
            if isinstance(child_schema, Mapping):
                _validate_schema(child, child_schema, path=child_path)
            elif additional is False:
                raise _InvalidRequest(f"{path}: unknown field {field!r}")
            elif isinstance(additional, Mapping):
                _validate_schema(child, additional, path=child_path)

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            raise _InvalidRequest(f"{path}: expected at least {minimum_items} items")
        maximum_items = schema.get("maxItems")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            raise _InvalidRequest(f"{path}: expected at most {maximum_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, path=f"{path}[{index}]")

    if _json_type(value) in {"integer", "number"}:
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise _InvalidRequest(f"{path}: expected a value greater than or equal to {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise _InvalidRequest(f"{path}: expected a value less than or equal to {maximum}")


def validate_hub_v2_tool_arguments(name: str, arguments: Mapping[str, Any]) -> None:
    """Validate one public tool call against its advertised descriptor."""
    descriptor = HUB_V2_TOOLS_BY_NAME.get(name)
    if descriptor is None:
        raise _InvalidRequest(f"Unknown Hub V2 tool: {name}")
    _validate_json_value(arguments, path="arguments")
    _validate_schema(arguments, descriptor["inputSchema"], path="arguments")


def validate_hub_v2_tool_output(name: str, output: Any) -> None:
    """Validate one trusted handler result against its advertised envelope."""
    descriptor = HUB_V2_TOOLS_BY_NAME.get(name)
    if descriptor is None:
        raise _HandlerContractError(f"Handler returned output for unknown Hub V2 tool: {name}")
    try:
        _validate_json_value(output, path="output")
        _validate_schema(output, descriptor["outputSchema"], path="output")
    except _InvalidRequest as error:
        raise _HandlerContractError(f"Invalid {name} handler output: {error}") from error


class HubProtocolV2:
    """Strict MCP framing around an injected Hub V2 domain handler."""

    def __init__(self, handler: HubV2ToolHandler):
        handle_tool_call = getattr(handler, "handle_tool_call", None)
        if not callable(handle_tool_call):
            raise TypeError("Hub V2 handler must define async handle_tool_call(name, arguments, *, context=...)")
        validate_hub_v2_registry()
        self.handler = handler
        parameters = inspect.signature(handle_tool_call).parameters
        self._handler_accepts_context = "context" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        )
        self.server_info = {
            "name": "patchbay-hub",
            "title": "PatchBay Hub V2",
            "version": HUB_V2_SERVER_VERSION,
        }
        self.capabilities = {"tools": {"listChanged": False}}

    async def handle_message(
        self,
        message: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any] | None:
        """Handle one JSON-RPC message without interpreting domain outcomes."""
        has_id = isinstance(message, Mapping) and "id" in message
        msg_id = message.get("id") if isinstance(message, Mapping) else None
        try:
            method, params = self._request_parts(message)
            if method == "notifications/initialized":
                return None
            if method == "initialize":
                result = await self._handle_initialize(params, context=context)
            elif method == "tools/list":
                result = await self._handle_tools_list(params, context=context)
            elif method == "tools/call":
                result = await self._handle_tools_call(params, context=context)
            else:
                raise _MethodNotFound(f"Method not found: {method}")
            if not has_id:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except _MethodNotFound as error:
            return self._error_response(has_id, msg_id, -32601, str(error))
        except _InvalidRequest as error:
            return self._error_response(has_id, msg_id, -32602, str(error))
        except Exception:
            logger.exception("Hub V2 protocol internal fault")
            return self._error_response(has_id, msg_id, -32603, "Internal processing error")

    def _request_parts(self, message: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        if not isinstance(message, Mapping):
            raise _InvalidRequest("JSON-RPC request must be an object")
        if message.get("jsonrpc", "2.0") != "2.0":
            raise _InvalidRequest("jsonrpc must be '2.0'")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise _InvalidRequest("JSON-RPC method must be a non-empty string")
        raw_params = message.get("params", {})
        if not isinstance(raw_params, Mapping):
            raise _InvalidRequest("JSON-RPC params must be an object")
        _validate_json_value(raw_params, path="params")
        return method, dict(raw_params)

    async def _handle_initialize(
        self,
        params: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        protocol_version = params.get("protocolVersion", HUB_V2_DEFAULT_MCP_VERSION)
        if not isinstance(protocol_version, str) or not protocol_version:
            raise _InvalidRequest("initialize.protocolVersion must be a non-empty string")
        return {
            "protocolVersion": protocol_version,
            "serverInfo": deepcopy(self.server_info),
            "capabilities": deepcopy(self.capabilities),
            "instructions": HUB_V2_INSTRUCTIONS,
            "_meta": deepcopy(HUB_V2_PROTOCOL_METADATA),
        }

    async def _handle_tools_list(
        self,
        params: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        unknown = set(params) - {"cursor"}
        if unknown:
            raise _InvalidRequest(f"tools/list: unknown field {sorted(unknown)[0]!r}")
        if "cursor" in params and not isinstance(params["cursor"], str):
            raise _InvalidRequest("tools/list.cursor must be a string")
        return {
            "tools": get_hub_v2_tools(),
            "_meta": deepcopy(HUB_V2_PROTOCOL_METADATA),
        }

    async def _handle_tools_call(
        self,
        params: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        unknown = set(params) - {"name", "arguments", "_meta"}
        if unknown:
            raise _InvalidRequest(f"tools/call: unknown field {sorted(unknown)[0]!r}")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _InvalidRequest("tools/call.name must be a non-empty string")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise _InvalidRequest("tools/call.arguments must be an object")
        if "_meta" in params and not isinstance(params["_meta"], Mapping):
            raise _InvalidRequest("tools/call._meta must be an object")
        arguments = dict(arguments)
        validate_hub_v2_tool_arguments(name, arguments)

        if self._handler_accepts_context:
            output = await self.handler.handle_tool_call(name, deepcopy(arguments), context=context)
        else:
            output = await self.handler.handle_tool_call(name, deepcopy(arguments))  # type: ignore[call-arg]
        validate_hub_v2_tool_output(name, output)
        envelope = redact_sensitive_output(deepcopy(dict(output)))
        validate_hub_v2_tool_output(name, envelope)
        return {
            "structuredContent": envelope,
            "content": [{"type": "text", "text": self._compact_text(name, envelope)}],
            "_meta": {
                "patchbay/tool_name": name,
                "patchbay/contract_version": HUB_V2_CONTRACT_VERSION,
                "patchbay/manifest_hash": HUB_V2_MANIFEST_HASH,
                "patchbay/schema_hash": HUB_V2_SCHEMA_HASH,
            },
        }

    @staticmethod
    def _compact_text(name: str, envelope: Mapping[str, Any]) -> str:
        status = str(envelope.get("status") or "unknown")
        result = envelope.get("result")
        result_map = result if isinstance(result, Mapping) else {}
        summary = next(
            (
                value.strip()
                for key in ("summary", "status_line", "message", "note", "reason")
                if isinstance((value := result_map.get(key)), str) and value.strip()
            ),
            "",
        )
        if len(summary) > 360:
            summary = summary[:357].rstrip() + "..."
        title = name.removeprefix("patchbay_").replace("_", " ")
        pieces = [f"{title}: {status}."]
        if summary:
            pieces.append(summary)
        # Some ChatGPT connector paths have transiently failed to surface
        # structuredContent. Keep startup/orientation tools usable by including
        # a bounded identifier-rich fallback in ordinary MCP text content.
        fallback = HubProtocolV2._manager_fallback_text(name, result_map)
        if fallback:
            pieces.append(fallback)
        operation = envelope.get("operation")
        operation_map = operation if isinstance(operation, Mapping) else {}
        operation_id = operation_map.get("operation_id")
        if isinstance(operation_id, str) and operation_id:
            pieces.append(f"Operation: {operation_id}.")
        next_actions = envelope.get("next_actions")
        if isinstance(next_actions, list) and next_actions:
            first = next_actions[0]
            if isinstance(first, str):
                action = first
            elif isinstance(first, Mapping):
                action = str(first.get("tool") or "")
            else:
                action = ""
            if action:
                pieces.append(f"Next: {action}.")
        return " ".join(pieces)

    @staticmethod
    def _manager_fallback_text(name: str, result: Mapping[str, Any]) -> str:
        rows: list[str] = []
        if name == "patchbay_fleet_status":
            for value in list(result.get("machines") or [])[:12]:
                if isinstance(value, Mapping):
                    rows.append(
                        f"{value.get('machine_id', '?')}={value.get('status', 'unknown')}"
                    )
            return "Machines: " + ", ".join(rows) + "." if rows else "No machines returned."
        if name == "patchbay_workspace_list":
            for value in list(result.get("workspaces") or [])[:12]:
                if not isinstance(value, Mapping):
                    continue
                projections = value.get("projections") or []
                paths = [
                    str(item.get("repo_path") or item.get("local_path") or item.get("path") or "")
                    for item in projections[:2]
                    if isinstance(item, Mapping)
                ]
                rows.append(
                    f"{value.get('workspace_ref', '?')}"
                    + (f" ({', '.join(path for path in paths if path)})" if any(paths) else "")
                )
            return "Workspaces: " + "; ".join(rows) + "." if rows else "No workspaces returned."
        if name == "patchbay_work_group_list":
            for value in list(result.get("work_groups") or [])[:12]:
                if isinstance(value, Mapping):
                    rows.append(
                        f"{value.get('work_group_id', '?')} [{value.get('status', 'unknown')}] {value.get('title', '')}".strip()
                    )
            return "Groups: " + "; ".join(rows) + "." if rows else "No work groups returned."
        if name == "patchbay_worker_options":
            models = result.get("models") or []
            for value in list(models)[:16]:
                if isinstance(value, Mapping):
                    rows.append(str(value.get("id") or value.get("model") or value.get("name") or ""))
                elif value:
                    rows.append(str(value))
            default = str(result.get("default_model") or "")
            text = "Models: " + ", ".join(item for item in rows if item) + "." if rows else "No models returned."
            return text + (f" Default: {default}." if default else "")
        if name in {"patchbay_work_group_create", "patchbay_work_group_status", "patchbay_work_group_resume"}:
            group = result.get("work_group") if isinstance(result.get("work_group"), Mapping) else {}
            group_id = str(group.get("work_group_id") or result.get("work_group_id") or "")
            machine_id = str(group.get("pinned_machine_id") or "")
            readiness = result.get("readiness") if isinstance(result.get("readiness"), Mapping) else {}
            if group_id:
                return f"Group: {group_id}." + (f" Machine: {machine_id}." if machine_id else "") + (f" Readiness: {readiness.get('status')}." if readiness.get("status") else "")
        return ""

    @staticmethod
    def _error_response(
        has_id: bool,
        msg_id: Any,
        code: int,
        message: str,
    ) -> dict[str, Any] | None:
        if not has_id:
            return None
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }


# Keep both natural import spellings available.
HubV2Protocol = HubProtocolV2


__all__ = [
    "HUB_V2_DEFAULT_MCP_VERSION",
    "HUB_V2_INSTRUCTIONS",
    "HUB_V2_PROTOCOL_METADATA",
    "HUB_V2_SERVER_VERSION",
    "HubProtocolV2",
    "HubV2Protocol",
    "HubV2ToolHandler",
    "validate_hub_v2_tool_arguments",
    "validate_hub_v2_tool_output",
]
