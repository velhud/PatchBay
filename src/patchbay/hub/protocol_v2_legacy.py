"""MCP protocol adapter for the frozen Hub V2 tool contract.

It owns MCP framing and descriptor validation while an injected async handler
owns every domain decision and operation result.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import re
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
    HUB_V2_MUTATING_TOOL_NAMES,
    get_hub_v2_tools,
    validate_hub_v2_registry,
)
from patchbay.protocol.context import RequestContext
from patchbay.protocol.result_text import decision_text
from patchbay.mcp_versions import PREFERRED_MCP_VERSION, select_mcp_version
from patchbay.security import redact_sensitive_output
from patchbay.hub.store_v2 import HubStoreV2

logger = logging.getLogger(__name__)


class _ResultContractFault(Exception):
    def __init__(self, data: dict):
        self.data = data

HUB_V2_SERVER_VERSION = "0.1.0"
HUB_V2_DEFAULT_MCP_VERSION = PREFERRED_MCP_VERSION

HUB_V2_INSTRUCTIONS = """
PatchBay Hub V2 lets ChatGPT manage durable Codex worker teams. Act as manager,
architect, and team lead; delegate non-trivial repository investigation,
implementation, testing, review, and synthesis to competent named workers.
Parallelize cleanly separated responsibilities and brief each colleague with
purpose, context, outcome, boundaries, deliverables, and verification evidence.
Use direct workspace tools only for focused orientation, tiny checks, or a
concrete doubt that worker follow-up did not resolve.

A valid Hub V2 connector exposes exactly 31 PatchBay tools. Verify that the
catalog includes fleet, workspace, group, worker lifecycle, Pro Request, and
operation-status controls. Fewer tools or a missing required lifecycle action
means the connector manifest is stale or partial: do not improvise a manual
replacement workflow or claim Hub work began; tell the user to refresh or
reconnect the connector.

For each non-trivial task: inspect fleet/workspaces, list existing groups, then
resume the same task or create one group. One task is one group with named lanes,
not one group per worker. Create normal tasks with execution_mode=end_to_end and
a concrete definition_of_done. Use asynchronous_handoff only when the user
explicitly wants work left running after the response. The group remains pinned
to one machine generation unless the user deliberately reassigns successor work.

The completion_contract returned by group and worker status is authoritative for
the management loop. When manager_must_continue=true or
final_response_allowed=false, do not produce a voluntary final answer. Follow
recommended_next_action. Active or quiet workers mean call patchbay_worker_wait,
normally in 20-30 second intervals, and continue until reports are ready. A wait
timeout means only that no projection changed during that interval. It is not a
failure, completion, or execution limit. Never invent a tool-call, generation,
or time limit; only report an explicit platform error or an unrecoverable
PatchBay blocker.

A recommended_next_action is either a complete callable tool action whose
arguments already satisfy the advertised schema, or natural-language manager
guidance when PatchBay cannot truthfully infer a shared brief, worker mission,
worker selector, outcome, summary, dispositions, or idempotency keys. Follow
that guidance by making the required managerial decisions; never execute it as
a partial call and never fabricate missing judgment just to satisfy a schema.

If the platform explicitly reports a tool-call, generation, response, or
context limit, do not stop workers, abandon the group, or misreport failure.
Return a continuation-state packet containing repository and revision,
work_group_id, pinned machine, lanes, worker names and models, completed and
active work, integration/commit/push state, PatchBay issues, blockers, and exact
next actions. On Continue, resume the same durable group and workers. Elapsed
time, healthy quiet work, and a wait timeout are not platform limits.

Use patchbay_worker_start_batch for a parallel team and patchbay_worker_start for
one lane. Continue the same completed worker with patchbay_worker_message when a
report needs correction, evidence, or deeper work. Use isolated_write for normal
parallel implementation; manager_controlled shared writes are an architect's
explicit concurrency choice. Preview and explicitly integrate accepted isolated
changes, verify requested outcomes, commit/push when requested, account for every
worker, and close the end-to-end group before the final answer.

A batch parent is Hub aggregate work, not an Edge command. While its children
run, follow the original operation id through aggregate_running and
wait_for_child_operations. A completed report can become visible before Codex
wrapper cleanup finishes; if message or integration returns cleanup_pending,
wait and retry after cleanup instead of replacing or force-stopping the worker.

Generate stable idempotency keys for mutations and reuse the same key only for an
identical retry. pending is not completion; follow operation/status guidance.
Continue through minor recoverable friction. Stop only for a genuinely unusable
tool surface, contradictory routing/state, unavailable required controls, or an
authoritatively lost execution.

Model routing is advisory and patchbay_worker_options is authoritative. Prefer
Spark for bounded small lanes with GPT-5.4 Mini as immediate fallback, Luna for
compact standard work, Terra for most substantial work, and Sol for difficult or
high-authority judgment. Sol medium is the normal default; use higher effort only
when difficulty or consequences justify it.
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
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeError as error:
            raise _InvalidRequest('JSON strings must be valid UTF-8') from error
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
        for index, action in enumerate(output.get("next_actions") or []):
            if not isinstance(action, Mapping):
                continue
            action_name = str(action.get("tool") or "")
            action_arguments = action.get("arguments")
            if action_arguments is None:
                action_arguments = {}
            if not isinstance(action_arguments, Mapping):
                raise _InvalidRequest(
                    f"output.next_actions[{index}].arguments: expected object"
                )
            try:
                validate_hub_v2_tool_arguments(action_name, action_arguments)
            except _InvalidRequest as error:
                raise _InvalidRequest(
                    f"output.next_actions[{index}] is not callable: {error}"
                ) from error
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
        from patchbay.mcp_framing import request_identity, invalid_request
        try:
            has_id, msg_id = request_identity(message)
        except ValueError as error:
            return invalid_request(str(error))
        if not has_id:
            return None
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
        except _ResultContractFault as error:
            response = self._error_response(has_id, msg_id, -32603, "Internal processing error")
            if response is not None:
                response["error"]["data"] = error.data
            return response
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
        try:
            protocol_version = select_mcp_version(params.get("protocolVersion", HUB_V2_DEFAULT_MCP_VERSION))
        except ValueError as error:
            raise _InvalidRequest(str(error)) from error
        if context is not None and context.session_data is not None:
            context.session_data["protocol_version"] = protocol_version
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

        from patchbay.hub.store_v2 import capture_operation_admissions
        with capture_operation_admissions(name, arguments.get('idempotency_key')) as admissions:
            try:
                if self._handler_accepts_context:
                    output = await self.handler.handle_tool_call(name, deepcopy(arguments), context=context)
                else:
                    output = await self.handler.handle_tool_call(name, deepcopy(arguments))  # type: ignore[call-arg]
            except Exception as error:
                data = self._result_fault_data(name, arguments, None, context, admissions=admissions)
                data.update(code='EFFECT_OUTCOME_UNAVAILABLE' if name in HUB_V2_MUTATING_TOOL_NAMES else 'READ_RESULT_UNAVAILABLE',
                            boundary='effect_outcome_needs_readback' if name in HUB_V2_MUTATING_TOOL_NAMES else 'read_result_unavailable',
                            effect_returned=False)
                raise _ResultContractFault(data) from error
        try:
            validate_hub_v2_tool_output(name, output)
            envelope = redact_sensitive_output(deepcopy(dict(output)))
            validate_hub_v2_tool_output(name, envelope)
            rendered = {"structuredContent": envelope,
                        "content": [{"type": "text", "text": self._compact_text(name, envelope)}],
                        "_meta": {"patchbay/tool_name": name, "patchbay/contract_version": HUB_V2_CONTRACT_VERSION,
                                  "patchbay/manifest_hash": HUB_V2_MANIFEST_HASH, "patchbay/schema_hash": HUB_V2_SCHEMA_HASH}}
            json.dumps(rendered, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (_HandlerContractError, UnicodeError, TypeError, ValueError, OverflowError, RecursionError) as error:
            raise _ResultContractFault(self._result_fault_data(name, arguments, output, context, admissions=admissions)) from error
        return rendered

    def _result_fault_data(self, name: str, arguments: Mapping, output: Any, context: RequestContext | None, *, admissions=()) -> dict:
        write = name in HUB_V2_MUTATING_TOOL_NAMES
        data = {"code": "INVALID_OUTPUT", "boundary": "effect_may_exist_result_invalid" if write else "read_result_invalid",
                "effect_may_exist": write, "success_asserted": False}
        # Only bounded, protocol-shaped request identities are projected. An
        # invalid result's operation ID additionally requires durable readback.
        def identity(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) else None
        group_id = identity(arguments.get("work_group_id"))
        operation_id = identity(arguments.get("operation_id"))
        key = arguments.get("idempotency_key")
        if write and isinstance(key, str) and len(key.encode("utf-8")) <= 200:
            data["original_key"] = key
        store = getattr(self.handler, "store", None)
        if len(admissions) > 1:
            data['admission_ambiguous'] = True
        if isinstance(store, HubStoreV2):
            operation = output.get('operation') if isinstance(output, Mapping) else None
            candidate = identity(admissions[0]) if len(admissions) == 1 else (
                identity(operation.get('operation_id')) if not admissions and isinstance(operation, Mapping) else None)
            if candidate:
                try:
                    row = store.get_operation(candidate)
                    principal = (context.owner_ref if context and context.owner_ref else store.principal_ref)
                    if row and row["principal_ref"] == principal and row["tool"] == name and row["idempotency_key"] == (key.strip() if isinstance(key, str) else key):
                        if group_id:
                            store.assert_operation_group_association(operation_id=candidate, work_group_id=group_id)
                        operation_id = candidate
                except Exception:
                    data["recovery_lookup_unavailable"] = True
        actions = []
        if operation_id:
            data["operation_id"] = operation_id
            actions.append({"tool": "patchbay_operation_status", "arguments": {"operation_id": operation_id}})
        if group_id:
            data["work_group_id"] = group_id
            actions.append({"tool": "patchbay_work_group_status", "arguments": {"work_group_id": group_id}})
        data["recovery_actions"] = actions or [{"tool": "patchbay_work_group_list", "arguments": {}}]
        data["guidance"] = ("No success or absence of an effect is asserted. Read the original operation/group before another mutation; an identical retry keeps the original key and payload."
                            if write else "The read result violated its contract; no mutation was attempted. Use the existing read-only recovery route.")
        return redact_sensitive_output(data)

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
            pieces.append(fallback[:1800] + ("... Orientation is partial; read full structuredContent." if len(fallback) > 1800 else ""))
        operation = envelope.get("operation")
        operation_map = operation if isinstance(operation, Mapping) else {}
        operation_id = operation_map.get("operation_id")
        if isinstance(operation_id, str) and operation_id and len(operation_id.encode("utf-8")) <= 128:
            pieces.append(f"Operation: {operation_id}.")
        pieces.append(decision_text(operation_map))
        next_actions = envelope.get("next_actions")
        if isinstance(next_actions, list) and next_actions:
            first = next_actions[0]
            if isinstance(first, str):
                action = first[:360]
            elif isinstance(first, Mapping):
                encoded = json.dumps(dict(first), ensure_ascii=False, separators=(",", ":"))
                action = encoded if len(encoded.encode("utf-8")) <= 2048 else "Action arguments exceed summary budget; read exact next_actions in structuredContent"
            else:
                action = ""
            if action:
                pieces.append(f"Next: {action}.")
        pieces.append(decision_text(result_map))
        for key in ("worker", "work_group", "completion_contract", "projection_freshness"):
            if isinstance(result_map.get(key), Mapping):
                pieces.append(key + ": " + decision_text(result_map[key]))
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
