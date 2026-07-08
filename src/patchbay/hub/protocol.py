"""MCP protocol surface for PatchBay Hub mode."""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Mapping

from patchbay.hub.runtime import HubRuntime
from patchbay.hub.store import HubStoreCorrupt
from patchbay.protocol.context import RequestContext
from patchbay.security import public_error_message, redact_sensitive_output

logger = logging.getLogger(__name__)


HUB_INSTRUCTIONS = """
PatchBay Hub is a fleet-level ChatGPT-to-Codex control plane. ChatGPT is the
manager of multiple PatchBay machines. Each machine runs its own local Codex
workers, local repositories, local worktrees, local credentials, and local
authority policy.

For any non-trivial Hub task, use the group-first lifecycle:
patchbay_fleet_status -> patchbay_work_group_list -> resume an existing group
or create one group -> start workers inside named lanes -> monitor
patchbay_work_group_status -> close the group or explicitly leave it active.

Hard rule: one user task equals one work group. Do not create one group per
worker. Do not use patchbay_worker_start_auto before a group exists. Do not use
patchbay_machine_recommend as permission to scatter workers across machines.
If the user asks for the same repository/task to use multiple machines, create
separate explicit groups/branches/integration owners instead of one mixed group.

A work group is the durable task object. Use one group for one task. Lanes are
parallel worker responsibilities inside that task. Hub auto-routing is
availability-only and only chooses a machine when the group is created. After
that, the group stays pinned to that machine. Do not scatter ordinary workers
from one task across machines. Moving work requires patchbay_work_group_reassign;
it creates successor work and does not move live Codex processes.

patchbay_worker_start_auto requires work_group_id, lane, and
auto_routing_ok=true. Use it only inside a work group. Explicit machine_id
worker starts remain available for tiny checks, operator-requested work, and
legacy compatibility, but ungrouped mutating starts must explain that reason.

After creating or resuming a group, wait for patchbay_work_group_status to show
preflight ok before starting grouped workers. If preflight is pending, wait. If
it failed, fix/reassign/override only as an operator recovery action. When the
pinned machine is busy or offline, wait, queue there if policy permits, or ask
for explicit reassign; do not silently choose another machine.

Queued Hub command means accepted by Hub, not completed by Edge or Codex. Before
final answers, check work-group status and either close the group or report what
remains active. Direct manual file reading is not the default manager behavior;
delegate repository work to workers and use direct inspection only for focused
verification, escalation, or tiny checks.
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
        "Recommend an eligible machine. With work_group_id, returns the pinned machine or a blocked reason; without a group, read-only availability advice.",
        {
            "work_group_id": _string_schema("Optional work group id. When present, returns that group's pinned machine."),
            "required_tags": _string_array_schema("Optional required machine tags. All listed tags must match."),
            "allowed_machine_ids": _string_array_schema("Optional eligible machine ids."),
            "repo_path": _string_schema("Optional repo path or alias to match advertised workspace projections."),
        },
    ),
    _tool(
        "patchbay_work_group_create",
        "Create one durable Hub work group for one non-trivial task, choose/pin one machine, and queue edge preflight before workers start.",
        {
            "title": _string_schema("Short human title for the task."),
            "goal": _string_schema("Goal the worker team must achieve."),
            "repo_path": _string_schema("Optional machine-local repo path or advertised alias."),
            "machine_id": _string_schema("Optional explicit machine id. If omitted and routing is enabled, Hub chooses one eligible machine."),
            "allowed_machine_ids": _string_array_schema("Optional machine allow-list."),
            "required_tags": _string_array_schema("Optional required machine tags."),
            "lanes": {"type": "array", "items": {"type": "string"}, "description": "Optional initial lane names."},
            "visibility": _string_schema("private or shared. Defaults to private."),
            "idempotency_key": _string_schema("Optional caller-chosen key to safely retry creation."),
            "routing_policy": _string_schema("Defaults to keep_together."),
            "make_current": {"type": "boolean", "description": "Defaults to true."},
        },
        ["title", "goal"],
        read_only=False,
    ),
    _tool(
        "patchbay_work_group_list",
        "List current/owned/recent/history work groups. Hides stale closed history by default and reports hidden counts.",
        {
            "scope": _string_schema("current, owned, recent, or history. Defaults to current."),
            "status": _string_schema("Optional group status filter."),
            "repo_path": _string_schema("Optional repo path filter."),
            "machine_id": _string_schema("Optional pinned machine filter."),
            "include_closed": {"type": "boolean"},
            "query": _string_schema("Optional title/goal/repo search."),
            "limit": {"type": "integer", "minimum": 1},
        },
    ),
    _tool(
        "patchbay_work_group_status",
        "Show group state, pinned machine, lanes, commands, preflight, active counts, and next recommended action.",
        {"work_group_id": _string_schema("Optional group id. Defaults to current group for this manager/session.")},
    ),
    _tool(
        "patchbay_work_group_resume",
        "Make an existing work group current for this manager/session and queue a fresh edge preflight.",
        {
            "work_group_id": _string_schema("Group id to resume."),
            "takeover": {"type": "boolean"},
            "takeover_reason": _string_schema("Reason when resuming a private or closed group from another manager/session."),
        },
        ["work_group_id"],
        read_only=False,
    ),
    _tool(
        "patchbay_work_group_close",
        "Close a work group with an outcome and summary. Refuses by default while active commands remain.",
        {
            "work_group_id": _string_schema("Group id to close."),
            "outcome": _string_schema("Completion outcome."),
            "summary": _string_schema("Manager summary."),
            "force": {"type": "boolean"},
        },
        ["work_group_id", "outcome", "summary"],
        read_only=False,
    ),
    _tool(
        "patchbay_work_group_reassign",
        "Explicitly move future successor work to another machine. Does not migrate live Codex workers.",
        {
            "work_group_id": _string_schema("Group id to reassign."),
            "machine_id": _string_schema("Optional explicit new machine id. If omitted, availability routing chooses an eligible machine."),
            "allowed_machine_ids": _string_array_schema("Optional new machine allow-list."),
            "required_tags": _string_array_schema("Optional required tags for the new machine."),
            "reason": _string_schema("Required reason for reassignment."),
        },
        ["work_group_id", "reason"],
        read_only=False,
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
            "work_group_id": _string_schema("Optional work group id for grouped Hub starts."),
            "lane": _string_schema("Required when work_group_id is supplied."),
            "ungrouped_reason": _string_schema("Required for ungrouped Hub starts: tiny_check, operator_requested, or legacy_compat."),
            "name": _string_schema("Human worker name."),
            "brief": _string_schema("Natural-language worker brief."),
            "repo_path": _string_schema("Optional machine-local repo path or configured alias."),
            "workspace_mode": _string_schema("isolated_write, read_only, or shared_write."),
            "model": _string_schema("Optional Codex model."),
            "reasoning_effort": _string_schema("Optional reasoning effort."),
            "preflight_override": {"type": "boolean", "description": "Operator recovery only; bypasses failed/pending group preflight for starts."},
        },
        ["name", "brief"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_start_auto",
        "Start a named Codex worker inside an existing work group on that group's pinned machine. Requires explicit grouped auto-routing confirmation.",
        {
            "work_group_id": _string_schema("Required work group id."),
            "lane": _string_schema("Required lane inside the work group."),
            "auto_routing_ok": {"type": "boolean", "description": "Must be true to confirm grouped availability routing."},
            "name": _string_schema("Human worker name."),
            "brief": _string_schema("Natural-language worker brief."),
            "repo_path": _string_schema("Optional machine-local repo path or configured alias."),
            "workspace_mode": _string_schema("isolated_write, read_only, or shared_write."),
            "model": _string_schema("Optional Codex model."),
            "reasoning_effort": _string_schema("Optional reasoning effort."),
            "required_tags": _string_array_schema("Optional required machine tags. All listed tags must match."),
            "preflight_override": {"type": "boolean", "description": "Operator recovery only; bypasses failed/pending group preflight for starts."},
        },
        ["work_group_id", "lane", "auto_routing_ok", "name", "brief"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_message",
        "Continue a named worker on its machine with a natural-language follow-up.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "work_group_id": _string_schema("Optional group id. If supplied, machine_id may be omitted and the pinned machine is used."),
            "lane": _string_schema("Optional lane id."),
            "worker": _string_schema("Worker name or id on that machine."),
            "message": _string_schema("Natural-language follow-up."),
            "repo_path": _string_schema("Optional repo disambiguation."),
        },
        ["worker", "message"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_status",
        "Return known fleet worker status, or queue a refresh command for a selected machine.",
        {
            "machine_id": _string_schema("Optional machine id; omit or use all for fleet status."),
            "work_group_id": _string_schema("Optional group id for grouped status refresh."),
            "lane": _string_schema("Optional lane id."),
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
            "work_group_id": _string_schema("Optional group id. If supplied, machine_id may be omitted and the pinned machine is used."),
            "lane": _string_schema("Optional lane id."),
            "wait_seconds": {"type": "integer", "minimum": 0},
            "repo_path": _string_schema("Optional repo filter."),
        },
        [],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_inspect",
        "Inspect one worker on one machine by routing a bounded inspect request to that machine.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "work_group_id": _string_schema("Optional group id. If supplied, machine_id may be omitted and the pinned machine is used."),
            "lane": _string_schema("Optional lane id."),
            "worker": _string_schema("Worker name or id."),
            "view": _string_schema("report, compact, status, diagnostics, changes, diff, file, or integration_preview."),
            "repo_path": _string_schema("Optional repo disambiguation."),
            "file_path": _string_schema("Optional worker file/diff path."),
            "max_bytes": {"type": "integer", "minimum": 1},
        },
        ["worker"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_stop",
        "Stop a worker turn on one machine. This is an interruption and may require confirmation on the edge.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "work_group_id": _string_schema("Optional group id. If supplied, machine_id may be omitted and the pinned machine is used."),
            "lane": _string_schema("Optional lane id."),
            "worker": _string_schema("Worker name or id."),
            "force": {"type": "boolean"},
            "repo_path": _string_schema("Optional repo disambiguation."),
        },
        ["worker"],
        read_only=False,
    ),
    _tool(
        "patchbay_worker_integrate",
        "Apply an accepted isolated worker result on the same machine where that worker ran. Does not commit.",
        {
            "machine_id": _string_schema("Target PatchBay machine id."),
            "work_group_id": _string_schema("Optional group id. If supplied, machine_id may be omitted and the pinned machine is used."),
            "lane": _string_schema("Optional lane id."),
            "worker": _string_schema("Worker name or id."),
            "repo_path": _string_schema("Optional repo disambiguation."),
            "allow_dirty_base": {"type": "boolean"},
        },
        ["worker"],
        read_only=False,
    ),
    _tool(
        "patchbay_command_status",
        "Inspect hub-routed command state. Useful when an edge is offline or a command is still queued.",
        {
            "command_id": _string_schema("Optional command id."),
            "machine_id": _string_schema("Optional machine id."),
            "work_group_id": _string_schema("Optional group id."),
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
                result = await self._tool_call(params, context=context)
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
        except HubStoreCorrupt as error:
            if msg_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32050, "message": public_error_message(error, allow_details=True)},
            }
        except Exception as error:
            logger.exception("Hub protocol error: %s", error)
            if msg_id is None:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": "Internal processing error"}}

    async def _tool_call(self, params: Mapping[str, Any], *, context: RequestContext | None = None) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        payload = await self._dispatch(name, arguments, context=context)
        payload = redact_sensitive_output(payload)
        return {
            "structuredContent": payload,
            "content": [{"type": "text", "text": self._text(payload, name)}],
            "_meta": {"patchbay/tool_name": name, "patchbay/tool_id": name.replace("patchbay_", "")},
        }

    async def _dispatch(self, name: str, arguments: Mapping[str, Any], *, context: RequestContext | None = None) -> dict[str, Any]:
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
            return self.runtime.recommend_machine(
                work_group_id=str(arguments.get("work_group_id") or ""),
                required_tags=arguments.get("required_tags") or [],
                allowed_machine_ids=arguments.get("allowed_machine_ids") or [],
                repo_path=str(arguments.get("repo_path") or ""),
            )
        if name == "patchbay_work_group_create":
            return self.runtime.create_work_group(
                title=str(arguments.get("title") or ""),
                goal=str(arguments.get("goal") or ""),
                repo_path=str(arguments.get("repo_path") or ""),
                machine_id=str(arguments.get("machine_id") or ""),
                allowed_machine_ids=arguments.get("allowed_machine_ids") or [],
                required_tags=arguments.get("required_tags") or [],
                lanes=arguments.get("lanes") or [],
                visibility=str(arguments.get("visibility") or ""),
                idempotency_key=str(arguments.get("idempotency_key") or ""),
                routing_policy=str(arguments.get("routing_policy") or ""),
                make_current=bool(arguments.get("make_current", True)),
                context=context,
            )
        if name == "patchbay_work_group_list":
            return self.runtime.list_work_groups(
                scope=str(arguments.get("scope") or "current"),
                status=str(arguments.get("status") or ""),
                repo_path=str(arguments.get("repo_path") or ""),
                machine_id=str(arguments.get("machine_id") or ""),
                include_closed=bool(arguments.get("include_closed", False)),
                query=str(arguments.get("query") or ""),
                limit=int(arguments.get("limit") or 20),
                context=context,
            )
        if name == "patchbay_work_group_status":
            return self.runtime.work_group_status(work_group_id=str(arguments.get("work_group_id") or ""), context=context)
        if name == "patchbay_work_group_resume":
            return self.runtime.resume_work_group(
                work_group_id=str(arguments.get("work_group_id") or ""),
                takeover=bool(arguments.get("takeover", False)),
                takeover_reason=str(arguments.get("takeover_reason") or ""),
                context=context,
            )
        if name == "patchbay_work_group_close":
            return self.runtime.close_work_group(
                work_group_id=str(arguments.get("work_group_id") or ""),
                outcome=str(arguments.get("outcome") or ""),
                summary=str(arguments.get("summary") or ""),
                force=bool(arguments.get("force", False)),
                context=context,
            )
        if name == "patchbay_work_group_reassign":
            return self.runtime.reassign_work_group(
                work_group_id=str(arguments.get("work_group_id") or ""),
                machine_id=str(arguments.get("machine_id") or ""),
                allowed_machine_ids=arguments.get("allowed_machine_ids") or [],
                required_tags=arguments.get("required_tags") or [],
                reason=str(arguments.get("reason") or ""),
                context=context,
            )
        if name == "patchbay_command_status":
            return self.runtime.command_status(
                command_id=str(arguments.get("command_id") or ""),
                machine_id=str(arguments.get("machine_id") or ""),
                work_group_id=str(arguments.get("work_group_id") or ""),
                state=str(arguments.get("state") or ""),
            )
        if name == "patchbay_worker_start_auto":
            return self.runtime.queue_auto_worker_start(arguments=arguments, context=context)
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
            if arguments.get("work_group_id"):
                return self.runtime.work_group_status(work_group_id=str(arguments.get("work_group_id") or ""), context=context)
            return self.runtime.fleet_status()
        if name == "patchbay_worker_status":
            action_map[name] = "codex_worker_status"
        action = action_map.get(name)
        if not action:
            raise ValueError(f"Unknown hub tool: {name}")
        command = self.runtime.queue_worker_command(
            machine_id=str(arguments.get("machine_id") or ""),
            action=action,
            arguments=arguments,
            context=context,
            work_group_id=str(arguments.get("work_group_id") or ""),
            lane=str(arguments.get("lane") or ""),
            ungrouped_reason=str(arguments.get("ungrouped_reason") or ""),
            required_tags=arguments.get("required_tags") or [],
        )
        command["accepted"] = True
        command["note"] = "Command queued for the selected or pinned PatchBay Edge machine."
        return command

    def _text(self, payload: Mapping[str, Any], tool_name: str) -> str:
        if tool_name == "patchbay_fleet_status":
            return str(payload.get("summary") or "Fleet status ready")
        if tool_name == "patchbay_machine_recommend":
            if not payload.get("enabled"):
                return "Hub availability routing is disabled. Use explicit machine_id."
            selected = payload.get("selected_machine_id") or "none"
            return f"Availability recommendation ready. Selected machine: {selected}."
        if tool_name.startswith("patchbay_work_group_"):
            group = payload.get("work_group") if isinstance(payload.get("work_group"), dict) else payload
            group_id = group.get("work_group_id") if isinstance(group, dict) else payload.get("current_work_group_id")
            if payload.get("accepted") is False:
                return str(payload.get("recommended_next_action") or payload.get("error") or "Work group operation did not complete.")
            return f"Work group result ready: {group_id or 'no current group'}."
        if tool_name == "patchbay_worker_start_auto" and not payload.get("accepted"):
            return str(payload.get("recommended_next_action") or payload.get("error") or "Auto-routing did not queue a worker.")
        if "command_id" in payload:
            return f"{payload.get('action')} queued on {payload.get('machine_id')} as {payload.get('command_id')} ({payload.get('state')})."
        rendered = json.dumps(payload, indent=2)
        if len(rendered) < 1000:
            return rendered
        return f"{tool_name} returned structuredContent with fields: {', '.join(payload.keys())}."
