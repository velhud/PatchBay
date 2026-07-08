"""MCP protocol surface for PatchBay Hub mode."""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Mapping

from patchbay.hub.runtime import HubRuntime
from patchbay.protocol.context import RequestContext
from patchbay.security import public_error_message, redact_sensitive_output

logger = logging.getLogger(__name__)


HUB_INSTRUCTIONS = """
PatchBay Hub is a fleet-level ChatGPT-to-Codex control plane. ChatGPT is the
manager of multiple PatchBay machines. Each machine runs its own local Codex
workers, local repositories, local worktrees, local credentials, and local
authority policy.

Start with patchbay_fleet_status. If the user names a machine, route there. If
the user names a project or repository, inspect patchbay_machine_workspaces and
choose machines that advertise that workspace. Use multiple machines when work
benefits from parallel investigation or separate implementation lanes.

When hub availability routing is enabled, patchbay_machine_recommend and
patchbay_worker_start_auto may choose the least-busy eligible online machine.
This auto-routing is availability-only: current worker slots, CPU, memory, disk
feasibility, online state, and explicit required tags. It does not infer task
meaning, task complexity, or model needs. Use auto-routing only when the user has
not named a machine; explicit machine_id always overrides it. If routing is
disabled, choose machine_id explicitly.

Workers are machine-local employees. Do not assume a Codex worker can migrate
between machines. Use machine_id explicitly when starting, messaging, inspecting,
stopping, or integrating a worker. For cross-machine synthesis, collect reports
from machine-local workers and send bounded report context to a synthesis worker.

Do not treat fleet status as a historical archive. Use current status first,
then inspect or wait when a worker is active. Direct manual file reading is not
the default manager behavior; delegate repository work to workers on the right
machine and use direct inspection only for focused verification or escalation.
"""


def _string_schema(description: str = "") -> dict[str, Any]:
    payload = {"type": "string"}
    if description:
        payload["description"] = description
    return payload


def _string_array_schema(description: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    if description:
        payload["description"] = description
    return payload


def _tool(name: str, description: str, properties: Mapping[str, Any] | None = None, required: list[str] | None = None, *, read_only: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "title": name.replace("_", " ").title(),
        "description": description,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": dict(properties or {}),
            "required": required or [],
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "openWorldHint": not read_only,
            "idempotentHint": read_only,
        },
        "securitySchemes": [{"type": "noauth"}],
        "_meta": {"securitySchemes": [{"type": "noauth"}]},
    }


HUB_TOOLS = [
    _tool("patchbay_fleet_status", "Return compact online/offline machine and visible worker status for the whole PatchBay fleet."),
    _tool(
        "patchbay_machine_list",
        "List enrolled PatchBay machines, tags, capabilities, online status, and safe workspace projections.",
        {
            "query": _string_schema("Optional machine name/tag search."),
            "tags": {"type": "array", "items": {"type": "string"}},
            "include_offline": {"type": "boolean"},
        },
    ),
    _tool(
        "patchbay_machine_workspaces",
        "Show advertised workspaces on one machine or the whole fleet so ChatGPT can choose where to route work.",
        {"machine_id": _string_schema("Optional machine_id filter.")},
    ),
    _tool(
        "patchbay_machine_recommend",
        "Recommend the least-busy eligible online machine using availability-only routing. Read-only; no semantic task classification.",
        {"required_tags": _string_array_schema("Optional required machine tags. All listed tags must match.")},
    ),
    _tool(
        "patchbay_worker_options",
        "Queue a request for model/reasoning options on a selected machine. Use before choosing model/reasoning for workers when needed.",
        {"machine_id": _string_schema("Target PatchBay machine id.")},
        ["machine_id"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_start",
        "Start a named Codex worker on a selected machine. The command is routed to that machine's local PatchBay Edge.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "name": _string_schema("Human worker name."),
            "brief": _string_schema("Natural-language worker brief."),
            "repo_path": _string_schema("Optional machine-local repo path or configured alias."),
            "workspace_mode": _string_schema("isolated_write, read_only, or shared_write."),
            "model": _string_schema("Optional Codex model."),
            "reasoning_effort": _string_schema("Optional reasoning effort."),
        },
        ["machine_id", "name", "brief"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_start_auto",
        "Start a named Codex worker on the least-busy eligible online machine when hub availability routing is enabled.",
        {
            "name": _string_schema("Human worker name."),
            "brief": _string_schema("Natural-language worker brief."),
            "repo_path": _string_schema("Optional machine-local repo path or configured alias."),
            "workspace_mode": _string_schema("isolated_write, read_only, or shared_write."),
            "model": _string_schema("Optional Codex model."),
            "reasoning_effort": _string_schema("Optional reasoning effort."),
            "required_tags": _string_array_schema("Optional required machine tags. All listed tags must match."),
        },
        ["name", "brief"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_message",
        "Continue a named worker on its machine with a natural-language follow-up.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "worker": _string_schema("Worker name or id on that machine."),
            "message": _string_schema("Natural-language follow-up."),
            "repo_path": _string_schema("Optional repo disambiguation."),
        },
        ["machine_id", "worker", "message"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_status",
        "Return known fleet worker status, or queue a refresh command for a selected machine.",
        {
            "machine_id": _string_schema("Optional machine id; omit or use all for fleet status."),
            "refresh": {"type": "boolean", "description": "Queue a fresh machine-local worker status request."},
            "repo_path": _string_schema("Optional repo filter for refresh."),
        },
        read_only=False,
    ),
    _tool(
        "patchbay_worker_wait",
        "Queue a patient worker status refresh on one machine. Use instead of rapid polling.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "wait_seconds": {"type": "integer", "minimum": 0},
            "repo_path": _string_schema("Optional repo filter."),
        },
        ["machine_id"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_inspect",
        "Inspect one worker on one machine by routing a bounded inspect request to that machine.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "worker": _string_schema("Worker name or id."),
            "view": _string_schema("report, compact, status, diagnostics, changes, diff, file, or integration_preview."),
            "repo_path": _string_schema("Optional repo disambiguation."),
            "file_path": _string_schema("Optional worker file/diff path."),
            "max_bytes": {"type": "integer", "minimum": 1},
        },
        ["machine_id", "worker"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_stop",
        "Stop a worker turn on one machine. This is an interruption and may require confirmation on the edge.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "worker": _string_schema("Worker name or id."),
            "force": {"type": "boolean"},
            "repo_path": _string_schema("Optional repo disambiguation."),
        },
        ["machine_id", "worker"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_integrate",
        "Apply an accepted isolated worker result on the same machine where that worker ran. Does not commit.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "worker": _string_schema("Worker name or id."),
            "repo_path": _string_schema("Optional repo disambiguation."),
            "allow_dirty_base": {"type": "boolean"},
        },
        ["machine_id", "worker"],
        read_only=False,
    ),
    _tool(
        "patchbay_command_status",
        "Inspect hub-routed command state. Useful when an edge is offline or a command is still queued.",
        {
            "command_id": _string_schema("Optional command id."),
            "machine_id": _string_schema("Optional machine id."),
            "state": _string_schema("Optional queued, running, completed, or failed filter."),
        },
    ),
]


class HubProtocol:
    """Minimal MCP protocol handler for hub mode."""

    def __init__(self, runtime: HubRuntime):
        self.runtime = runtime
        self.server_info = {"name": "patchbay-hub", "version": "0.1.0"}
        self.capabilities = {"tools": {"listChanged": True}, "resources": {"listChanged": False}}

    async def handle_message(self, message: Mapping[str, Any], *, context: RequestContext | None = None) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            if method == "notifications/initialized":
                return None
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", "2025-11-25"),
                    "serverInfo": self.server_info,
                    "capabilities": self.capabilities,
                    "instructions": HUB_INSTRUCTIONS,
                }
            elif method == "tools/list":
                result = {"tools": deepcopy(HUB_TOOLS)}
            elif method == "tools/call":
                result = await self._tool_call(params)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/read":
                raise ValueError("PatchBay Hub exposes no resources in this release")
            else:
                raise ValueError(f"Unknown method: {method}")
            if msg_id is None:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except ValueError as error:
            if msg_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": public_error_message(error, allow_details=True)},
            }
        except Exception as error:
            logger.exception("Hub protocol error: %s", error)
            if msg_id is None:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": "Internal processing error"}}

    async def _tool_call(self, params: Mapping[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        payload = await self._dispatch(name, arguments)
        payload = redact_sensitive_output(payload)
        return {
            "structuredContent": payload,
            "content": [{"type": "text", "text": self._text(payload, name)}],
            "_meta": {"patchbay/tool_name": name, "patchbay/tool_id": name.replace("patchbay_", "")},
        }

    async def _dispatch(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "patchbay_fleet_status":
            return self.runtime.fleet_status()
        if name == "patchbay_machine_list":
            return self.runtime.list_machines(
                query=str(arguments.get("query") or ""),
                tags=arguments.get("tags") or [],
                include_offline=bool(arguments.get("include_offline", True)),
            )
        if name == "patchbay_machine_workspaces":
            return self.runtime.machine_workspaces(machine_id=str(arguments.get("machine_id") or ""))
        if name == "patchbay_machine_recommend":
            return self.runtime.recommend_machine(required_tags=arguments.get("required_tags") or [])
        if name == "patchbay_command_status":
            return self.runtime.command_status(
                command_id=str(arguments.get("command_id") or ""),
                machine_id=str(arguments.get("machine_id") or ""),
                state=str(arguments.get("state") or ""),
            )
        if name == "patchbay_worker_start_auto":
            recommendation = self.runtime.recommend_machine(required_tags=arguments.get("required_tags") or [])
            selected_machine_id = str(recommendation.get("selected_machine_id") or "")
            if not recommendation.get("enabled"):
                return {
                    "accepted": False,
                    "error": "Hub availability routing is disabled.",
                    "routing": recommendation,
                    "recommended_next_action": "Use patchbay_worker_start with an explicit machine_id.",
                }
            if not selected_machine_id:
                return {
                    "accepted": False,
                    "error": "No eligible machine is available for auto-routing.",
                    "routing": recommendation,
                    "recommended_next_action": "Wait for capacity, relax required_tags, or use explicit machine_id if you intend to override routing.",
                }
            routed_args = {key: value for key, value in arguments.items() if key not in {"required_tags"} and value not in (None, "")}
            command = self.runtime.create_command(machine_id=selected_machine_id, action="codex_worker_start", arguments=routed_args)
            command["accepted"] = True
            command["routing"] = recommendation
            command["note"] = "Command queued by availability-only routing. Explicit machine_id still overrides auto-routing."
            return command
        action_map = {
            "patchbay_worker_options": "codex_worker_options",
            "patchbay_worker_start": "codex_worker_start",
            "patchbay_worker_message": "codex_worker_message",
            "patchbay_worker_wait": "codex_worker_wait",
            "patchbay_worker_inspect": "codex_worker_inspect",
            "patchbay_worker_stop": "codex_worker_stop",
            "patchbay_worker_integrate": "codex_worker_integrate",
        }
        if name == "patchbay_worker_status" and not bool(arguments.get("refresh")):
            return self.runtime.fleet_status()
        if name == "patchbay_worker_status":
            action_map[name] = "codex_worker_status"
        action = action_map.get(name)
        if not action:
            raise ValueError(f"Unknown hub tool: {name}")
        machine_id = str(arguments.get("machine_id") or "")
        if not machine_id or machine_id == "all":
            raise ValueError("machine_id is required for routed worker commands")
        routed_args = {key: value for key, value in arguments.items() if key not in {"machine_id", "refresh"} and value not in (None, "")}
        command = self.runtime.create_command(machine_id=machine_id, action=action, arguments=routed_args)
        command["accepted"] = True
        command["note"] = "Command queued for the selected PatchBay Edge machine."
        return command

    def _text(self, payload: Mapping[str, Any], tool_name: str) -> str:
        if tool_name == "patchbay_fleet_status":
            return str(payload.get("summary") or "Fleet status ready")
        if tool_name == "patchbay_machine_recommend":
            if not payload.get("enabled"):
                return "Hub availability routing is disabled. Use explicit machine_id."
            selected = payload.get("selected_machine_id") or "none"
            return f"Availability recommendation ready. Selected machine: {selected}."
        if tool_name == "patchbay_worker_start_auto" and not payload.get("accepted"):
            return str(payload.get("recommended_next_action") or payload.get("error") or "Auto-routing did not queue a worker.")
        if "command_id" in payload:
            return f"{payload.get('action')} queued on {payload.get('machine_id')} as {payload.get('command_id')} ({payload.get('state')})."
        rendered = json.dumps(payload, indent=2)
        if len(rendered) < 1000:
            return rendered
        return f"{tool_name} returned structuredContent with fields: {', '.join(payload.keys())}."
