"""Implemented Hub V2 manager-tool contract registry.

The production Hub protocol imports this exact 31-tool catalog.  The registry
is the public descriptor, schema, annotation, authentication-metadata, and
routing contract for every manager action exposed by Hub V2.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from patchbay.pro_requests.tool_surface import PRO_REQUEST_OUTPUT_SCHEMA, PRO_REQUEST_TOOLS
from patchbay.workers.tool_surface import (
    WORKER_INBOX_SCHEMA,
    WORKER_LIST_SCHEMA,
    WORKER_OPTIONS_SCHEMA,
    WORKER_STATUS_SCHEMA,
    WORKER_TOOLS,
    WORKER_VIEW_SCHEMA,
)


HUB_V2_CONTRACT_VERSION = "HUB-MANAGER-CONTROL-PLANE-V2"
HUB_V2_ACTION_CAPABILITY_VERSION = "2"
HUB_V2_EXPECTED_TOOL_COUNT = 31
HUB_V2_SECURITY_SCHEMES: list[dict[str, str]] = [{"type": "noauth"}]

HUB_V2_PUBLIC_STATUSES = (
    "ok",
    "pending",
    "partial",
    "blocked",
    "failed",
    "not_found",
)

HUB_V2_TOOL_NAMES = (
    "patchbay_fleet_status",
    "patchbay_workspace_list",
    "patchbay_work_group_create",
    "patchbay_work_group_list",
    "patchbay_work_group_status",
    "patchbay_work_group_resume",
    "patchbay_work_group_reassign",
    "patchbay_work_group_close",
    "patchbay_worker_options",
    "patchbay_worker_inbox",
    "patchbay_worker_start",
    "patchbay_worker_start_batch",
    "patchbay_worker_message",
    "patchbay_worker_list",
    "patchbay_worker_status",
    "patchbay_worker_wait",
    "patchbay_worker_inspect",
    "patchbay_worker_integrate",
    "patchbay_worker_stop",
    "patchbay_workspace_open",
    "patchbay_workspace_tree",
    "patchbay_workspace_search",
    "patchbay_workspace_read_file",
    "patchbay_workspace_changes",
    "patchbay_pro_request_list",
    "patchbay_pro_request_read",
    "patchbay_pro_request_claim",
    "patchbay_pro_request_respond",
    "patchbay_pro_request_dispatch",
    "patchbay_pro_request_close",
    "patchbay_operation_status",
)
HUB_V2_TOOL_NAME_SET = frozenset(HUB_V2_TOOL_NAMES)

# These inputs describe single-machine history or refresh behavior that the Hub
# projection surface does not implement. The Hub contract is intentionally
# group-scoped instead of accepting filters that would silently do nothing.
HUB_V2_UNSUPPORTED_WORKER_COLLECTION_FIELDS = frozenset(
    {"repo_path", "scope", "owned_only", "created_after", "force_refresh"}
)

HUB_V2_TOOL_FAMILIES: dict[str, tuple[str, ...]] = {
    "fleet_and_discovery": HUB_V2_TOOL_NAMES[0:2],
    "work_groups": HUB_V2_TOOL_NAMES[2:8],
    "workers_and_artifacts": HUB_V2_TOOL_NAMES[8:19],
    "exceptional_manager_workspace_inspection": HUB_V2_TOOL_NAMES[19:24],
    "pro_requests": HUB_V2_TOOL_NAMES[24:30],
    "exceptional_operation_recovery": HUB_V2_TOOL_NAMES[30:31],
}

# These are the five V1-only manager tools replaced by the V2 contract.  Keep
# them explicit so catalog regression tests cannot accidentally reintroduce
# transport- or machine-administration concepts into the manager surface.
HUB_V1_ONLY_TOOL_NAMES = (
    "patchbay_machine_list",
    "patchbay_machine_workspaces",
    "patchbay_machine_recommend",
    "patchbay_worker_start_auto",
    "patchbay_command_status",
)
REMOVED_HUB_V1_TOOL_NAMES = HUB_V1_ONLY_TOOL_NAMES
TARGET_HUB_V2_TOOL_NAMES = HUB_V2_TOOL_NAMES


def _string(description: str = "", *, enum: Sequence[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if description:
        schema["description"] = description
    if enum is not None:
        schema["enum"] = list(enum)
    return schema


def _integer(
    description: str = "",
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer"}
    if description:
        schema["description"] = description
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _boolean(description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "boolean"}
    if description:
        schema["description"] = description
    return schema


def _string_array(description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    if description:
        schema["description"] = description
    return schema


def _input_schema(
    properties: Mapping[str, Any],
    *,
    required: Sequence[str] = (),
    all_of: Sequence[Mapping[str, Any]] = (),
    any_of: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": deepcopy(dict(properties)),
        "required": list(required),
    }
    if all_of:
        schema["allOf"] = deepcopy(list(all_of))
    if any_of:
        schema["anyOf"] = deepcopy(list(any_of))
    return schema


IDEMPOTENCY_KEY_SCHEMA = _string(
    "Required opaque stable retry key for this mutation. Generate it before the first call and reuse it after interruption."
)

GROUP_ROUTE_PROPERTIES: dict[str, Any] = {
    "work_group_id": _string("Durable Hub work-group id. Omit only where the current group is explicitly supported."),
    "lane": _string("Human lane label inside the work group."),
}

EXPLICIT_ROUTE_PROPERTIES: dict[str, Any] = {
    "machine_id": _string("Explicit machine id for an exceptional ungrouped route."),
    "workspace_ref": _string("Logical workspace reference resolved to an authorized projection on the selected Edge."),
    "repo_path": _string("Compatibility repository name or machine-local path hint; Edge path guards remain authoritative."),
    "ungrouped_reason": _string(
        "Required for exceptional ungrouped work.",
        enum=("tiny_check", "operator_requested", "legacy_compat"),
    ),
}

WORKER_SELECTOR_PROPERTIES: dict[str, Any] = {
    "worker": _string("Worker name inside the work group."),
    "fleet_worker_ref": _string("Immutable machine-generation-qualified fleet worker reference."),
}

TAKEOVER_PROPERTIES: dict[str, Any] = {
    "takeover": _boolean("Use only after user confirmation to take over cross-participant mutation."),
    "takeover_reason": _string("Short audit reason for an explicit takeover."),
}

PAGINATION_PROPERTIES: dict[str, Any] = {
    "cursor": _string("Opaque pagination cursor returned by the previous page."),
    "limit": _integer("Maximum records to return, capped by server policy.", minimum=1),
}

ROUTING_RESULT_PROPERTIES: dict[str, Any] = {
    "work_group": {"type": "object", "additionalProperties": True},
    "lane": {"type": "object", "additionalProperties": True},
    "worker": {"type": "object", "additionalProperties": True},
    "machine": {"type": "object", "additionalProperties": True},
    "workspace": {"type": "object", "additionalProperties": True},
    "fleet_worker_ref": {"type": "string"},
    "edge_generation": {"type": "string"},
}


def _result_schema(properties: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": deepcopy(dict(properties or {})),
    }


def _routed_result_schema(base: Mapping[str, Any]) -> dict[str, Any]:
    schema = deepcopy(dict(base))
    schema.setdefault("type", "object")
    schema.setdefault("additionalProperties", True)
    properties = schema.setdefault("properties", {})
    for name, property_schema in ROUTING_RESULT_PROPERTIES.items():
        properties.setdefault(name, deepcopy(property_schema))
    return schema


HUB_V2_WORKER_RESULT_SCHEMA = _routed_result_schema(WORKER_VIEW_SCHEMA)
_worker_result_properties = HUB_V2_WORKER_RESULT_SCHEMA["properties"]
if "liveness" in _worker_result_properties:
    _worker_result_properties["liveness_detail"] = deepcopy(_worker_result_properties["liveness"])
_worker_result_properties.update(
    {
        "worker_state": {
            "type": "string",
            "enum": ["available", "stopped", "workspace_missing"],
        },
        "turn_state": {
            "type": "string",
            "enum": ["none", "queued", "starting", "working", "completed", "failed", "cancelled"],
        },
        "liveness": {
            "type": "string",
            "enum": ["starting", "active", "quiet", "stale", "lost", "terminal"],
        },
        "integration_state": {
            "type": "string",
            "enum": [
                "not_applicable",
                "no_changes",
                "not_integrated",
                "applied_to_checkout",
                "discarded",
                "uncertain",
            ],
        },
        "review_disposition": {
            "type": "string",
            "enum": ["unreviewed", "accepted", "rejected", "not_required"],
        },
        "projection_revision": {"type": "integer"},
        "workspace_ref": {"type": "string"},
        "workspace_projection_ref": {"type": "string"},
        "preview_token": {"type": "string"},
        "preview_token_expires_at": {"type": "number"},
    }
)

_COMPLETION_RECOMMENDATION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "string",
            "description": (
                "Natural-language manager guidance when PatchBay cannot truthfully infer "
                "the judgment, brief, selector, dispositions, or idempotency keys required "
                "for a valid tool call."
            ),
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tool": _string(enum=HUB_V2_TOOL_NAMES),
                "reason": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": True},
            },
            "required": ["tool", "reason", "arguments"],
        },
    ]
}


HUB_V2_COMPLETION_CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "execution_mode": {
            "type": "string",
            "enum": ["end_to_end", "asynchronous_handoff"],
        },
        "definition_of_done": {"type": "string"},
        "work_remaining": {"type": "boolean"},
        "manager_must_continue": {"type": "boolean"},
        "final_response_allowed": {"type": "boolean"},
        "reason": {"type": "string"},
        "activity": {"type": "string"},
        "activity_counts": {"type": "object", "additionalProperties": True},
        "recommended_next_action": deepcopy(_COMPLETION_RECOMMENDATION_SCHEMA),
    },
}

HUB_V2_WORKER_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "fleet_worker_ref": {"type": "string"},
        "worker_id": {"type": "string"},
        "name": {"type": "string"},
        "work_group_id": {"type": "string"},
        "lane_id": {"type": "string"},
        "machine_id": {"type": "string"},
        "workspace_mode": {"type": "string"},
        "worker_state": {"type": "string"},
        "turn_state": {"type": "string"},
        "liveness": {"type": "string"},
        "current_phase": {"type": "string"},
        "last_activity_at": {"type": ["number", "null"]},
        "status_line": {"type": "string"},
        "latest_partial_note": {"type": "object", "additionalProperties": True},
        "has_changes": {"type": "boolean"},
        "integration_state": {"type": "string"},
        "review_disposition": {"type": "string"},
        "can_message_now": {"type": "boolean"},
        "projection_revision": {"type": "integer"},
    },
}


def _worker_list_result_schema() -> dict[str, Any]:
    schema = _routed_result_schema(WORKER_LIST_SCHEMA)
    schema["properties"]["workers"] = {
        "type": "array",
        "items": deepcopy(HUB_V2_WORKER_SUMMARY_SCHEMA),
    }
    return schema


def _worker_status_result_schema() -> dict[str, Any]:
    schema = _routed_result_schema(WORKER_STATUS_SCHEMA)
    schema["properties"]["workers"] = {
        "type": "array",
        "items": deepcopy(HUB_V2_WORKER_SUMMARY_SCHEMA),
    }
    schema["properties"]["projection_revision"] = {"type": "integer"}
    schema["properties"]["projection_freshness"] = {"type": "object", "additionalProperties": True}
    schema["properties"]["completion_contract"] = deepcopy(HUB_V2_COMPLETION_CONTRACT_SCHEMA)
    schema["properties"]["work_remaining"] = {"type": "boolean"}
    schema["properties"]["final_response_allowed"] = {"type": "boolean"}
    schema["properties"]["changed"] = {"type": "boolean"}
    return schema


HUB_V2_OPERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "operation_id": {"type": "string"},
        "parent_operation_id": {"type": "string"},
        "item_id": {"type": "string"},
        "tool_name": {"type": "string"},
        "state": {
            "type": "string",
            "enum": [
                "created",
                "payload_ready",
                "dispatchable",
                "running",
                "reconciling",
                "outcome_unknown",
                "succeeded",
                "blocked",
                "failed",
                "cancelled",
            ],
        },
        "attempt_id": {"type": "string"},
        "attempt_state": {
            "type": "string",
            "enum": [
                "offered",
                "claimed",
                "executing",
                "effect_recorded",
                "result_ready",
                "acknowledged",
                "lease_expired",
                "reconciling",
                "retryable",
                "manual_recovery",
            ],
        },
        "machine_id": {"type": "string"},
        "edge_generation": {"type": "string"},
        "fencing_token": {"type": "integer"},
        "idempotency_key": {"type": "string"},
        "semantic_payload_hash": {"type": "string"},
        "revision": {"type": "integer"},
        "created_at": {"type": "number"},
        "updated_at": {"type": "number"},
        "retryable": {"type": "boolean"},
        "reconciliation_state": {"type": "string"},
        "item_results": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
}

_WARNING_ITEM_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "string"},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "details": {"type": "object", "additionalProperties": True},
            },
            "required": ["code", "message"],
        },
    ]
}

_NEXT_ACTION_ITEM_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "string"},
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tool": _string(enum=HUB_V2_TOOL_NAMES),
                "reason": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": True},
            },
            "required": ["tool"],
        },
    ]
}


def normalize_hub_v2_next_action(value: Any, *, operation_id: str = "") -> dict[str, Any] | str:
    """Return one public manager action without exposing internal transitions.

    Stored Edge results can outlive a contract upgrade. Unknown action names must
    therefore be converted at the public boundary rather than merely rejected by
    output validation after they have already reached the MCP response path.
    """

    fallback: dict[str, Any] | str
    if operation_id:
        fallback = {
            "tool": "patchbay_operation_status",
            "arguments": {"operation_id": operation_id},
            "reason": "Inspect this operation through Hub's public recovery tool.",
        }
    else:
        fallback = "Inspect the current manager-visible operation or work-group state before continuing."

    if isinstance(value, Mapping):
        tool = str(value.get("tool") or "").strip()
        if tool not in HUB_V2_TOOL_NAME_SET:
            return fallback
        supplied_arguments = value.get("arguments")
        if supplied_arguments is None:
            arguments: dict[str, Any] = {}
        elif isinstance(supplied_arguments, Mapping):
            arguments = deepcopy(dict(supplied_arguments))
        else:
            return fallback
        # Import lazily to keep the descriptor module importable by the protocol
        # validator while reusing its complete Hub V2 schema implementation.
        from patchbay.hub.protocol_v2 import validate_hub_v2_tool_arguments

        try:
            validate_hub_v2_tool_arguments(tool, arguments)
        except ValueError:
            return fallback
        action: dict[str, Any] = {"tool": tool}
        if supplied_arguments is not None:
            action["arguments"] = arguments
        reason = value.get("reason")
        if isinstance(reason, str) and reason.strip():
            action["reason"] = reason.strip()
        return action

    if isinstance(value, str):
        guidance = value.strip()
        if (
            guidance
            and "_" in guidance
            and all(character.isalnum() or character in "_-" for character in guidance)
        ):
            return fallback
        return guidance or fallback
    return fallback


def normalize_hub_v2_next_actions(
    values: Any, *, operation_id: str = ""
) -> list[dict[str, Any] | str]:
    """Normalize a public next-action list against the exact Hub V2 registry."""

    if not isinstance(values, list):
        return []
    return [normalize_hub_v2_next_action(value, operation_id=operation_id) for value in values]


def output_envelope_schema(result_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return the strict canonical public envelope around an action result."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": list(HUB_V2_PUBLIC_STATUSES)},
            "result": deepcopy(dict(result_schema)),
            "operation": deepcopy(HUB_V2_OPERATION_SCHEMA),
            "warnings": {"type": "array", "items": deepcopy(_WARNING_ITEM_SCHEMA)},
            "next_actions": {"type": "array", "items": deepcopy(_NEXT_ACTION_ITEM_SCHEMA)},
        },
        "required": ["status", "result", "operation", "warnings", "next_actions"],
    }


HUB_V2_MUTATING_TOOL_NAMES = frozenset(
    {
        "patchbay_work_group_create",
        "patchbay_work_group_resume",
        "patchbay_work_group_reassign",
        "patchbay_work_group_close",
        "patchbay_worker_inbox",
        "patchbay_worker_start",
        "patchbay_worker_start_batch",
        "patchbay_worker_message",
        "patchbay_worker_integrate",
        "patchbay_worker_stop",
        "patchbay_pro_request_claim",
        "patchbay_pro_request_respond",
        "patchbay_pro_request_dispatch",
        "patchbay_pro_request_close",
    }
)

HUB_V2_DESTRUCTIVE_TOOL_NAMES = frozenset(
    {
        "patchbay_work_group_reassign",
        "patchbay_work_group_close",
        "patchbay_worker_inbox",
        "patchbay_worker_integrate",
        "patchbay_worker_stop",
    }
)

HUB_V2_OPEN_WORLD_TOOL_NAMES = frozenset(
    {
        "patchbay_worker_inbox",
        "patchbay_worker_start",
        "patchbay_worker_start_batch",
        "patchbay_worker_message",
        "patchbay_pro_request_dispatch",
    }
)


def _annotations(name: str) -> dict[str, bool]:
    read_only = name not in HUB_V2_MUTATING_TOOL_NAMES
    return {
        "readOnlyHint": read_only,
        "destructiveHint": name in HUB_V2_DESTRUCTIVE_TOOL_NAMES,
        "openWorldHint": name in HUB_V2_OPEN_WORLD_TOOL_NAMES,
        # Every V2 mutation is protected by its required stable key. Read-only
        # calls are naturally repeatable, so the public call boundary is
        # idempotent even where the underlying domain effect is not.
        "idempotentHint": True,
    }


def _title(name: str) -> str:
    return "PatchBay " + " ".join(word.capitalize() for word in name.removeprefix("patchbay_").split("_"))


def _descriptor(
    name: str,
    description: str,
    input_schema: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    descriptor = deepcopy(dict(source or {}))
    meta = deepcopy(dict(descriptor.get("_meta") or {}))
    meta["securitySchemes"] = deepcopy(HUB_V2_SECURITY_SCHEMES)
    descriptor.update(
        {
            "name": name,
            "title": _title(name),
            "description": description,
            "inputSchema": deepcopy(dict(input_schema)),
            "outputSchema": output_envelope_schema(result_schema),
            "readOnlyHint": name not in HUB_V2_MUTATING_TOOL_NAMES,
            "annotations": _annotations(name),
            "securitySchemes": deepcopy(HUB_V2_SECURITY_SCHEMES),
            "_meta": meta,
        }
    )
    return descriptor


_CANONICAL_NAME_REPLACEMENTS = {
    "codex_self_test": "patchbay_fleet_status",
    "codex_list_workspaces": "patchbay_workspace_list",
    "codex_open_workspace": "patchbay_workspace_open",
    "codex_repo_tree": "patchbay_workspace_tree",
    "codex_search_repo": "patchbay_workspace_search",
    "codex_read_file": "patchbay_workspace_read_file",
    "codex_git_status": "patchbay_workspace_changes",
    "codex_git_diff": "patchbay_workspace_changes",
    "codex_show_changes": "patchbay_workspace_changes",
    **{f"codex_worker_{suffix}": f"patchbay_worker_{suffix}" for suffix in (
        "options",
        "inbox",
        "start",
        "message",
        "list",
        "status",
        "wait",
        "inspect",
        "integrate",
        "stop",
    )},
    **{f"codex_pro_request_{suffix}": f"patchbay_pro_request_{suffix}" for suffix in (
        "list",
        "read",
        "claim",
        "respond",
        "dispatch",
        "close",
    )},
}


def _target_description(source_description: str, routing_note: str) -> str:
    description = source_description
    for old_name, new_name in sorted(_CANONICAL_NAME_REPLACEMENTS.items(), key=lambda item: -len(item[0])):
        description = description.replace(old_name, new_name)
    return f"{description} {routing_note}".strip()


_WORKER_SOURCES = {tool["name"]: tool for tool in WORKER_TOOLS}
_PRO_REQUEST_SOURCES = {tool["name"]: tool for tool in PRO_REQUEST_TOOLS}


def _canonical_properties(source: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(dict(source["inputSchema"]["properties"]))


def _fleet_status_descriptor() -> dict[str, Any]:
    workspace_summary = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "workspace_projection_ref": {"type": ["string", "null"]},
            "workspace_ref": {"type": ["string", "null"]},
            "aliases": {"type": ["array", "null"], "items": {"type": "string"}},
            "repository_identity": {"type": ["string", "null"]},
            "local_path": {"type": ["string", "null"]},
            "exists": {"type": ["boolean", "null"]},
            "git": {"type": ["object", "boolean", "null"], "additionalProperties": True},
            "active": {"type": ["boolean", "null"]},
            "projection_revision": {"type": ["integer", "null"]},
            "received_at": {"type": ["number", "null"]},
        },
    }
    machine_summary = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "machine_id": {"type": "string"},
            "display_name": {"type": "string"},
            "edge_generation": {"type": "string"},
            "status": {"type": "string"},
            "compatibility": {"type": "string"},
            "contract_hash": {"type": "string"},
            "tags": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
            "role": {"type": "string"},
            "last_seen_at": {"type": ["number", "null"]},
            "last_seen_age_seconds": {"type": ["number", "null"]},
            "projection_revision": {"type": "integer"},
            "resource_status": {"type": "object", "additionalProperties": True},
            "worker_projection_status": {"type": "string"},
            "projection_health": {"type": "object", "additionalProperties": True},
            "retired_at": {"type": ["number", "null"]},
            "capabilities": {"type": "object", "additionalProperties": True},
            "worker_summary": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "projection_revision": {"type": "integer"},
                    "last_received_projection_revision": {"type": "integer"},
                    "resync_required": {"type": "boolean"},
                    "tombstone_count": {"type": "integer"},
                    "counts": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            key: {"type": "integer"}
                            for key in (
                                "total",
                                "active",
                                "quiet",
                                "stale",
                                "lost",
                                "failed",
                                "completed",
                                "unintegrated",
                            )
                        },
                    },
                },
                "required": [
                    "projection_revision",
                    "last_received_projection_revision",
                    "resync_required",
                    "tombstone_count",
                    "counts",
                ],
            },
            "workspaces": {
                "type": "array",
                "maxItems": 10,
                "items": workspace_summary,
            },
            "workspace_count": {"type": "integer"},
            "hidden_workspace_count": {"type": "integer"},
        },
    }
    return _descriptor(
        "patchbay_fleet_status",
        "Return one compact Hub V2 operational view of current usable fleet capacity, compatibility, workspace summaries, current-group context, and recovery warnings. Retired machines are audit-only.",
        _input_schema(
            {
                "include_offline": _boolean("Include current offline machines. Default true."),
                "include_retired": _boolean("Include retired or superseded generations for audit only. Default false."),
                "query": _string("Optional machine name, alias, or tag query."),
                "tags": _string_array("Require all listed machine tags."),
                "include_workspaces": _boolean("Include bounded compact workspace summaries."),
                "since_revision": _integer("Return state at or after this Hub revision when available.", minimum=0),
            }
        ),
        _result_schema(
            {
                "hub": {"type": "object", "additionalProperties": True},
                "contract_version": {"type": "string"},
                "manifest_hash": {"type": "string"},
                "schema_hash": {"type": "string"},
                "routing_enabled": {"type": "boolean"},
                "counts": {"type": "object", "additionalProperties": True},
                "machines": {"type": "array", "maxItems": 20, "items": machine_summary},
                "machine_count": {"type": "integer"},
                "hidden_machine_count": {"type": "integer"},
                "current_work_group": {"type": "object", "additionalProperties": True},
                "owned_active_groups": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "object", "additionalProperties": True},
                },
                "owned_active_group_count": {"type": "integer"},
                "hidden_owned_active_group_count": {"type": "integer"},
            }
        ),
    )


def _workspace_list_descriptor() -> dict[str, Any]:
    return _descriptor(
        "patchbay_workspace_list",
        "Find logical repositories and authorized machine projections across eligible PatchBay Edges without guessing absolute paths. Results distinguish ready, stale, offline, and preflight-required projections.",
        _input_schema(
            {
                "query": _string("Optional workspace name, alias, identity, or path query."),
                "discover": _boolean("Ask eligible Edges for bounded discovery in addition to known projections."),
                "machine_ids": _string_array("Optional eligible machine ids."),
                "required_tags": _string_array("Require all listed machine tags."),
                "include_offline": _boolean("Include known projections on current offline machines."),
                "max_depth": _integer("Maximum discovery depth, capped by server policy.", minimum=0),
                "max_results": _integer("Maximum logical workspaces to return, capped by server policy.", minimum=1),
            }
        ),
        _result_schema(
            {
                "workspaces": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "count": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "next_cursor": {"type": "string"},
                "query": {"type": "string"},
            }
        ),
    )


def _work_group_descriptors() -> list[dict[str, Any]]:
    lane_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "lane": _string("Stable human lane label."),
            "title": _string("Optional human lane title."),
            "role": _string("Optional responsibility or role description."),
        },
        "required": ["lane"],
    }
    disposition_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **deepcopy(WORKER_SELECTOR_PROPERTIES),
            "disposition": _string(
                "Required close disposition for this worker.",
                enum=(
                    "integrated",
                    "no_changes",
                    "reviewed_failure",
                    "stopped_preserved",
                    "discarded",
                    "leave_running",
                ),
            ),
            "discard_unintegrated_changes": _boolean(
                "Must be true when disposition=discarded; cleanup alone never authorizes discarding changes."
            ),
            "note": _string("Optional disposition evidence or manager note."),
        },
        "required": ["disposition"],
        "anyOf": [{"required": ["worker"]}, {"required": ["fleet_worker_ref"]}],
        "allOf": [
            {
                "if": {
                    "properties": {"disposition": {"const": "discarded"}},
                    "required": ["disposition"],
                },
                "then": {
                    "properties": {
                        "discard_unintegrated_changes": {"const": True},
                    },
                    "required": ["discard_unintegrated_changes"],
                },
            }
        ],
    }
    group_result = _result_schema(
        {
            "work_group": {"type": "object", "additionalProperties": True},
            "lanes": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "workers": {"type": "array", "items": deepcopy(HUB_V2_WORKER_SUMMARY_SCHEMA)},
            "readiness": {"type": "object", "additionalProperties": True},
            "routing": {"type": "object", "additionalProperties": True},
            "completion_contract": deepcopy(HUB_V2_COMPLETION_CONTRACT_SCHEMA),
            "status_revision": {"type": "integer"},
            "waited_seconds": {"type": "number"},
            "requested_wait_seconds": {"type": "integer"},
            "changed": {"type": "boolean"},
            "candidate_summary": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "rejection_summary": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        }
    )
    return [
        _descriptor(
            "patchbay_work_group_create",
            "Use this to create one durable task and pin its worker team to one available machine. Default end_to_end mode keeps the manager in the tool loop until the group is terminal; asynchronous_handoff must be an explicit choice. Group creation and workspace readiness are reported separately.",
            _input_schema(
                {
                    "title": _string("Short human task title."),
                    "goal": _string("Natural-language outcome the worker team must achieve."),
                    "workspace_ref": deepcopy(EXPLICIT_ROUTE_PROPERTIES["workspace_ref"]),
                    "repo_path": deepcopy(EXPLICIT_ROUTE_PROPERTIES["repo_path"]),
                    "machine_id": _string("Optional explicit machine id; otherwise availability-only placement applies."),
                    "allowed_machine_ids": _string_array("Optional placement allow-list."),
                    "required_tags": _string_array("Require all listed machine tags."),
                    "lanes": {"type": "array", "items": lane_schema},
                    "visibility": _string("Coordination visibility inside the operator trust domain.", enum=("private", "shared")),
                    "shared_write_policy": _string(
                        "Architect-selected policy for workers that directly share the base checkout. serialized keeps the repository lock; manager_controlled permits deliberate concurrent shared writers and reports the conflict risk.",
                        enum=("serialized", "manager_controlled"),
                    ),
                    "execution_mode": _string(
                        "Manager completion policy. end_to_end forbids a voluntary final response while the group is open; asynchronous_handoff explicitly permits reporting active durable work.",
                        enum=("end_to_end", "asynchronous_handoff"),
                    ),
                    "definition_of_done": _string(
                        "Natural-language result and verification criteria. Defaults to goal when omitted."
                    ),
                    "wait_for_preflight_seconds": _integer("Bounded synchronous preflight wait.", minimum=0),
                    "idempotency_key": deepcopy(IDEMPOTENCY_KEY_SCHEMA),
                },
                required=("title", "goal", "idempotency_key"),
            ),
            group_result,
        ),
        _descriptor(
            "patchbay_work_group_list",
            "List the current, owned, recent, or historical work groups without dumping all history. The default scope returns the current conversation group plus owned open groups and reports hidden counts.",
            _input_schema(
                {
                    "scope": _string("Group visibility scope.", enum=("current", "owned", "recent", "history")),
                    "status": _string("Optional persistent lifecycle, readiness, activity, or outcome filter."),
                    "workspace_ref": deepcopy(EXPLICIT_ROUTE_PROPERTIES["workspace_ref"]),
                    "machine_id": deepcopy(EXPLICIT_ROUTE_PROPERTIES["machine_id"]),
                    "query": _string("Optional title, goal, workspace, or machine query."),
                    "include_closed": _boolean("Include closed or superseded groups."),
                    **deepcopy(PAGINATION_PROPERTIES),
                }
            ),
            _result_schema(
                {
                    "work_groups": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                    "count": {"type": "integer"},
                    "hidden_counts": {"type": "object", "additionalProperties": True},
                    "next_cursor": {"type": "string"},
                }
            ),
        ),
        _descriptor(
            "patchbay_work_group_status",
            "Use this for the authoritative task overview and completion contract. When final_response_allowed is false, follow recommended_next_action instead of answering the user. wait_for_change_seconds performs a real bounded wait for group, worker, operation, or integration state change.",
            _input_schema(
                {
                    "work_group_id": deepcopy(GROUP_ROUTE_PROPERTIES["work_group_id"]),
                    "since_revision": _integer("Return changes after this group revision when possible.", minimum=0),
                    "wait_for_change_seconds": _integer("Bounded Hub-side wait for a projection change.", minimum=0),
                    "include_workers": _boolean("Include compact worker projections."),
                    "include_operations": _boolean("Include compact operation projections."),
                    "include_integrations": _boolean("Include integration/disposition projections."),
                    "worker_cursor": _string(
                        "Opaque worker-page cursor returned by the previous group status response."
                    ),
                    "worker_limit": _integer(
                        "Maximum compact worker projections returned in this page.",
                        minimum=1,
                        maximum=100,
                    ),
                    "operation_cursor": _string(
                        "Opaque operation-page cursor returned by the previous group status response."
                    ),
                    "operation_limit": _integer(
                        "Maximum compact operation projections returned in this page.",
                        minimum=1,
                        maximum=100,
                    ),
                    "integration_cursor": _string(
                        "Opaque integration-page cursor returned by the previous group status response."
                    ),
                    "integration_limit": _integer(
                        "Maximum integration/disposition projections returned in this page.",
                        minimum=1,
                        maximum=100,
                    ),
                }
            ),
            group_result,
        ),
        _descriptor(
            "patchbay_work_group_resume",
            "Make one owned open group current for this ChatGPT conversation, record participation, and refresh stale projections. Closed groups cannot be reopened.",
            _input_schema(
                {
                    "work_group_id": deepcopy(GROUP_ROUTE_PROPERTIES["work_group_id"]),
                    **deepcopy(TAKEOVER_PROPERTIES),
                    "wait_for_preflight_seconds": _integer("Optional bounded wait for refreshed preflight.", minimum=0),
                    "idempotency_key": deepcopy(IDEMPOTENCY_KEY_SCHEMA),
                },
                required=("work_group_id", "idempotency_key"),
            ),
            group_result,
        ),
        _descriptor(
            "patchbay_work_group_reassign",
            "Create a linked successor group on another machine generation. This never changes the predecessor pin and never claims to migrate live sessions, worktrees, or artifacts.",
            _input_schema(
                {
                    "work_group_id": deepcopy(GROUP_ROUTE_PROPERTIES["work_group_id"]),
                    "reason": _string("Required manager reason for successor placement."),
                    "machine_id": _string("Optional explicit successor machine id."),
                    "allowed_machine_ids": _string_array("Optional successor placement allow-list."),
                    "required_tags": _string_array("Require all listed successor machine tags."),
                    "carry_context": _string(
                        "Context staged into the successor; live sessions and worktrees are never moved.",
                        enum=("reports", "reports_and_changes", "none"),
                    ),
                    "idempotency_key": deepcopy(IDEMPOTENCY_KEY_SCHEMA),
                },
                required=("work_group_id", "reason", "idempotency_key"),
            ),
            group_result,
        ),
        _descriptor(
            "patchbay_work_group_close",
            "Record one group's final manager decision with an explicit outcome, summary, and disposition for every worker. This tool never stops workers or cleans workspaces: explicitly stop active workers and complete any workspace disposal with worker-specific controls before close. Complete refuses active, uncertain, unreviewed, or unintegrated work.",
            _input_schema(
                {
                    "work_group_id": deepcopy(GROUP_ROUTE_PROPERTIES["work_group_id"]),
                    "outcome": _string("Final manager outcome.", enum=("complete", "partial", "abandoned", "failed")),
                    "summary": _string("Durable manager closure summary."),
                    "worker_dispositions": {"type": "array", "items": disposition_schema},
                    "idempotency_key": deepcopy(IDEMPOTENCY_KEY_SCHEMA),
                },
                required=("work_group_id", "outcome", "summary", "worker_dispositions", "idempotency_key"),
            ),
            group_result,
        ),
    ]


def _worker_options_descriptor() -> dict[str, Any]:
    source = _WORKER_SOURCES["codex_worker_options"]
    properties = _canonical_properties(source)
    properties.update(
        {
            "work_group_id": deepcopy(GROUP_ROUTE_PROPERTIES["work_group_id"]),
            "machine_id": deepcopy(EXPLICIT_ROUTE_PROPERTIES["machine_id"]),
        }
    )
    return _descriptor(
        "patchbay_worker_options",
        _target_description(
            str(source["description"]),
            "Hub resolves the pinned Edge from work_group_id; machine_id is the explicit ungrouped alternative.",
        ),
        _input_schema(properties, any_of=({"required": ["work_group_id"]}, {"required": ["machine_id"]})),
        _routed_result_schema(WORKER_OPTIONS_SCHEMA),
        source=source,
    )


def _worker_inbox_descriptor() -> dict[str, Any]:
    source = _WORKER_SOURCES["codex_worker_inbox"]
    properties = _canonical_properties(source)
    properties.update(deepcopy(GROUP_ROUTE_PROPERTIES))
    properties.update(deepcopy(EXPLICIT_ROUTE_PROPERTIES))
    properties["idempotency_key"] = deepcopy(IDEMPOTENCY_KEY_SCHEMA)
    return _descriptor(
        "patchbay_worker_inbox",
        _target_description(
            str(source["description"]),
            "Artifacts remain machine-affine. This mixed read/mutation tool requires idempotency_key at the public V2 boundary so every import or cleanup retry is stable.",
        ),
        _input_schema(properties, required=("action", "idempotency_key")),
        _routed_result_schema(WORKER_INBOX_SCHEMA),
        source=source,
    )


def _worker_start_descriptor() -> dict[str, Any]:
    source = _WORKER_SOURCES["codex_worker_start"]
    properties = _canonical_properties(source)
    properties.update(deepcopy(GROUP_ROUTE_PROPERTIES))
    properties.update(deepcopy(EXPLICIT_ROUTE_PROPERTIES))
    properties["idempotency_key"] = deepcopy(IDEMPOTENCY_KEY_SCHEMA)
    return _descriptor(
        "patchbay_worker_start",
        "Use this when appointing one durable Codex colleague inside a work-group lane. Give a colleague-quality natural-language brief with outcome, context, boundaries, deliverable, and verification; use batch start when several independent lanes can begin together. Normal starts route to the group's pinned Edge.",
        _input_schema(
            properties,
            required=("name", "brief", "idempotency_key"),
            any_of=(
                {"required": ["work_group_id", "lane"]},
                {
                    "required": ["machine_id", "ungrouped_reason"],
                    "anyOf": [{"required": ["workspace_ref"]}, {"required": ["repo_path"]}],
                },
            ),
        ),
        HUB_V2_WORKER_RESULT_SCHEMA,
        source=source,
    )


def _worker_start_batch_descriptor() -> dict[str, Any]:
    start_source = _WORKER_SOURCES["codex_worker_start"]
    start_properties = _canonical_properties(start_source)
    worker_item_properties = {
        "item_id": _string("Caller-stable child item id used to correlate partial results and retries."),
        "idempotency_key": _string(
            "Opaque stable retry key for this worker item. Reuse it with the same parent and item payload."
        ),
        "name": deepcopy(start_properties["name"]),
        "lane": deepcopy(GROUP_ROUTE_PROPERTIES["lane"]),
        "mission": _string(
            "Worker-specific natural-language responsibility appended to shared_brief. State its lane outcome, boundaries, "
            "coordination role, deliverable, and evidence/verification expectations."
        ),
        "workspace_mode": deepcopy(start_properties["workspace_mode"]),
        "model": deepcopy(start_properties["model"]),
        "reasoning_effort": deepcopy(start_properties["reasoning_effort"]),
        "context_from_workers": deepcopy(start_properties["context_from_workers"]),
        "context_from_artifacts": deepcopy(start_properties["context_from_artifacts"]),
        "include_untracked_from_base": deepcopy(start_properties["include_untracked_from_base"]),
        "auto_suffix": deepcopy(start_properties["auto_suffix"]),
    }
    worker_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": worker_item_properties,
        "required": ["item_id", "idempotency_key", "name", "lane", "mission"],
    }
    return _descriptor(
        "patchbay_worker_start_batch",
        "Appoint a parallel team on one work group's pinned Edge. The whole batch is validated before dispatch. isolated_write remains the recommended parallel default; multiple shared_write workers are accepted only when the group's architect-selected shared_write_policy is manager_controlled, in which case PatchBay reports but does not prevent checkout conflicts. Each child has stable identity and idempotency, successful children are never rolled back, and retries resume unfinished items without duplicate workers.",
        _input_schema(
            {
                "work_group_id": deepcopy(GROUP_ROUTE_PROPERTIES["work_group_id"]),
                "shared_brief": _string(
                    "Common task/product purpose, current context and authority, desired outcome, constraints and non-goals, "
                    "team coordination assumptions, and shared evidence/verification requirements."
                ),
                "context_from_workers": deepcopy(start_properties["context_from_workers"]),
                "context_from_artifacts": deepcopy(start_properties["context_from_artifacts"]),
                "context_detail": deepcopy(start_properties["context_detail"]),
                "workers": {"type": "array", "minItems": 1, "items": worker_item_schema},
                "idempotency_key": deepcopy(IDEMPOTENCY_KEY_SCHEMA),
            },
            required=("work_group_id", "shared_brief", "workers", "idempotency_key"),
        ),
        _result_schema(
            {
                "work_group": {"type": "object", "additionalProperties": True},
                "items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "counts": {"type": "object", "additionalProperties": True},
            }
        ),
    )


def _worker_message_descriptor() -> dict[str, Any]:
    source = _WORKER_SOURCES["codex_worker_message"]
    properties = _canonical_properties(source)
    properties.update(deepcopy(GROUP_ROUTE_PROPERTIES))
    properties.update(deepcopy(WORKER_SELECTOR_PROPERTIES))
    properties["idempotency_key"] = deepcopy(IDEMPOTENCY_KEY_SCHEMA)
    return _descriptor(
        "patchbay_worker_message",
        "Use this when the same named worker should clarify, correct, deepen, review, or continue its work in natural language. Ask follow-up before manually redoing a worker's investigation. Active turns return active_turn_in_progress; wait for completion and then continue the same session.",
        _input_schema(
            properties,
            required=("work_group_id", "message", "idempotency_key"),
            any_of=({"required": ["worker"]}, {"required": ["fleet_worker_ref"]}),
        ),
        HUB_V2_WORKER_RESULT_SCHEMA,
        source=source,
    )


def _worker_collection_descriptor(canonical_name: str, target_name: str) -> dict[str, Any]:
    source = _WORKER_SOURCES[canonical_name]
    properties = _canonical_properties(source)
    for field in HUB_V2_UNSUPPORTED_WORKER_COLLECTION_FIELDS:
        properties.pop(field, None)
    properties.update(
        {
            "work_group_id": deepcopy(GROUP_ROUTE_PROPERTIES["work_group_id"]),
            "lane": deepcopy(GROUP_ROUTE_PROPERTIES["lane"]),
            "cursor": deepcopy(PAGINATION_PROPERTIES["cursor"]),
            "limit": deepcopy(PAGINATION_PROPERTIES["limit"]),
        }
    )
    if target_name in {"patchbay_worker_status", "patchbay_worker_wait"}:
        properties["since_revision"] = _integer("Return projection changes after this revision.", minimum=0)
    result_schema = (
        _worker_list_result_schema()
        if target_name == "patchbay_worker_list"
        else _worker_status_result_schema()
    )
    descriptions = {
        "patchbay_worker_list": "Use this to list the named workers in one required work group or lane before choosing a management action. It returns compact authoritative projections, not raw transcripts; Hub has no current/history or machine-wide worker scope for this tool. Normal Hub monitoring has a 20-second minimum and 30-second recommended cadence; an earlier repeat returns cached poll_too_early guidance scoped to this manager and group.",
        "patchbay_worker_status": "Use this for a compact authoritative status check for one required work group or lane. Follow completion_contract and recommended_next_action; active or quiet workers are not failures. Hub has no current/history or machine-wide worker scope for this tool. Normal Hub monitoring has a 20-second minimum and 30-second recommended cadence; an earlier repeat returns cached poll_too_early guidance scoped to this manager and group.",
        "patchbay_worker_wait": "Use this when workers in one required work group or lane are active or quiet and the manager's correct action is patience. It waits for a worker projection change or a bounded timeout, uses a 30-second default, clamps smaller waits to the 20-second minimum, does not interrupt workers, and returns the next management action. Hub has no current/history or machine-wide worker scope for this tool. A timeout is not completion or failure.",
    }
    return _descriptor(
        target_name,
        descriptions[target_name],
        _input_schema(properties, required=("work_group_id",)),
        result_schema,
        source=source,
    )


def _worker_inspect_descriptor() -> dict[str, Any]:
    source = _WORKER_SOURCES["codex_worker_inspect"]
    properties = _canonical_properties(source)
    properties.update(deepcopy(GROUP_ROUTE_PROPERTIES))
    properties.update(deepcopy(WORKER_SELECTOR_PROPERTIES))
    return _descriptor(
        "patchbay_worker_inspect",
        "Use this to read one worker's report or investigate a concrete concern. Prefer report/compact/status for normal management; use diagnostics, files, diffs, or integration_preview only when needed. integration_preview returns the signed token required for Hub integration.",
        _input_schema(
            properties,
            required=("work_group_id",),
            any_of=({"required": ["worker"]}, {"required": ["fleet_worker_ref"]}),
        ),
        HUB_V2_WORKER_RESULT_SCHEMA,
        source=source,
    )


def _worker_integrate_descriptor() -> dict[str, Any]:
    source = _WORKER_SOURCES["codex_worker_integrate"]
    properties = _canonical_properties(source)
    properties.update(deepcopy(GROUP_ROUTE_PROPERTIES))
    properties.update(deepcopy(WORKER_SELECTOR_PROPERTIES))
    properties["preview_token"] = _string("Required signed opaque token returned by integration_preview.")
    properties["idempotency_key"] = deepcopy(IDEMPOTENCY_KEY_SCHEMA)
    return _descriptor(
        "patchbay_worker_integrate",
        "Use this after accepting an isolated worker result and obtaining its signed integration_preview token. Hub revalidates the worker, patch, base, and repository state, applies without committing, and makes identical retries idempotent.",
        _input_schema(
            properties,
            required=("work_group_id", "preview_token", "idempotency_key"),
            any_of=({"required": ["worker"]}, {"required": ["fleet_worker_ref"]}),
        ),
        HUB_V2_WORKER_RESULT_SCHEMA,
        source=source,
    )


def _worker_stop_descriptor() -> dict[str, Any]:
    source = _WORKER_SOURCES["codex_worker_stop"]
    properties = _canonical_properties(source)
    properties.update(deepcopy(GROUP_ROUTE_PROPERTIES))
    properties.update(deepcopy(WORKER_SELECTOR_PROPERTIES))
    properties["discard_unintegrated_changes"] = _boolean(
        "Explicit consent required in addition to cleanup_workspace when unintegrated changes would be discarded."
    )
    properties["idempotency_key"] = deepcopy(IDEMPOTENCY_KEY_SCHEMA)
    cleanup_rule = {
        "if": {
            "properties": {"cleanup_workspace": {"const": True}},
            "required": ["cleanup_workspace"],
        },
        "then": {
            "required": ["discard_unintegrated_changes"],
            "properties": {"discard_unintegrated_changes": {"const": True}},
        },
    }
    return _descriptor(
        "patchbay_worker_stop",
        "Use this only after a deliberate decision to interrupt or retire a worker, not because it is merely quiet. Workspace cleanup never authorizes loss by itself; discarding unintegrated changes requires explicit discard_unintegrated_changes=true.",
        _input_schema(
            properties,
            required=("work_group_id", "idempotency_key"),
            all_of=(cleanup_rule,),
            any_of=({"required": ["worker"]}, {"required": ["fleet_worker_ref"]}),
        ),
        HUB_V2_WORKER_RESULT_SCHEMA,
        source=source,
    )


def _workspace_target_properties() -> dict[str, Any]:
    properties = deepcopy(GROUP_ROUTE_PROPERTIES)
    properties.update(deepcopy(EXPLICIT_ROUTE_PROPERTIES))
    return properties


def _workspace_descriptors() -> list[dict[str, Any]]:
    route_note = (
        "Use work_group_id for the pinned workspace. An explicit machine/workspace route is exceptional and must include ungrouped_reason."
    )
    open_properties = _workspace_target_properties()
    open_properties.update(
        {
            "include_tree": _boolean("Include a bounded repository tree."),
            "max_depth": _integer("Maximum tree depth, capped by server policy.", minimum=0),
            "max_entries": _integer("Maximum tree entries, capped by server policy.", minimum=1),
            "include_hidden": _boolean("Include non-blocked hidden paths."),
        }
    )
    tree_properties = _workspace_target_properties()
    tree_properties.update(
        {
            "path": _string("Workspace-relative directory path. Defaults to repository root."),
            "max_depth": _integer("Maximum tree depth, capped by server policy.", minimum=0),
            "max_entries": _integer("Maximum tree entries, capped by server policy.", minimum=1),
            "include_hidden": _boolean("Include non-blocked hidden paths."),
        }
    )
    search_properties = _workspace_target_properties()
    search_properties.update(
        {
            "query": _string("Focused search query."),
            "path": _string("Workspace-relative file or directory to search."),
            "glob": _string("Optional file glob such as **/*.py."),
            "regex": _boolean("Treat query as a regular expression."),
            "include_hidden": _boolean("Include non-blocked hidden paths."),
            "max_results": _integer("Maximum matches, capped by server policy.", minimum=1),
            "timeout_ms": _integer("Search timeout; timeout returns structured partial recovery.", minimum=1),
        }
    )
    read_properties = _workspace_target_properties()
    read_properties.update(
        {
            "file_path": _string("Workspace-relative base-checkout file path."),
            "start_line": _integer("1-based start line.", minimum=1),
            "end_line": _integer("1-based inclusive end line.", minimum=1),
            "max_bytes": _integer("Maximum bytes for this response page, capped by server policy.", minimum=1),
        }
    )
    change_properties = _workspace_target_properties()
    change_properties.update(
        {
            "view": _string("Requested git view.", enum=("status", "summary", "diff")),
            "file_path": _string("Optional workspace-relative file scope."),
            "staged": _boolean("Inspect staged changes."),
            "include_diff": _boolean("Include bounded diff text in summary view."),
            "max_bytes": _integer("Maximum returned diff bytes, capped by server policy.", minimum=1),
            "porcelain": _boolean("Return porcelain-form status details for status view."),
        }
    )
    common_any_of = (
        {"required": ["work_group_id"]},
        {
            "required": ["machine_id", "ungrouped_reason"],
            "anyOf": [{"required": ["workspace_ref"]}, {"required": ["repo_path"]}],
        },
    )
    workspace_result = _result_schema(
        {
            **deepcopy(ROUTING_RESULT_PROPERTIES),
            "workspace_id": {"type": "string"},
            "path": {"type": "string"},
            "text": {"type": "string"},
            "tree": {"type": ["object", "null"], "additionalProperties": True},
            "git": {"type": "object", "additionalProperties": True},
            "matches": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "timed_out": {"type": "boolean"},
            "truncated": {"type": "boolean"},
            "next_start_line": {"type": "integer"},
            "diff": {"type": "string"},
            "files_changed": {"type": "array", "items": {"type": "string"}},
        }
    )
    return [
        _descriptor(
            "patchbay_workspace_open",
            f"Open a bounded manager orientation for an authorized base checkout, including git state, instruction summary, and optional tree. Skill administration is intentionally not exposed. {route_note}",
            _input_schema(open_properties, any_of=common_any_of),
            workspace_result,
        ),
        _descriptor(
            "patchbay_workspace_tree",
            f"Return a bounded tree for focused manager orientation or verification while preserving Edge path guards. {route_note}",
            _input_schema(tree_properties, any_of=common_any_of),
            workspace_result,
        ),
        _descriptor(
            "patchbay_workspace_search",
            f"Search an authorized base checkout for a focused manager question with bounded structured timeout recovery. {route_note}",
            _input_schema(search_properties, required=("query",), any_of=common_any_of),
            workspace_result,
        ),
        _descriptor(
            "patchbay_workspace_read_file",
            f"Read one paged text slice from an authorized base checkout. Worker-created files remain under patchbay_worker_inspect before integration. {route_note}",
            _input_schema(read_properties, required=("file_path",), any_of=common_any_of),
            workspace_result,
        ),
        _descriptor(
            "patchbay_workspace_changes",
            f"Return strict status, summary, or diff views for an authorized base checkout, combining the overlapping V1 git tools. {route_note}",
            _input_schema(change_properties, required=("view",), any_of=common_any_of),
            workspace_result,
        ),
    ]


def _pro_request_descriptor(canonical_name: str, target_name: str) -> dict[str, Any]:
    source = _PRO_REQUEST_SOURCES[canonical_name]
    properties = _canonical_properties(source)
    properties.update(deepcopy(GROUP_ROUTE_PROPERTIES))
    properties.update(
        {
            "machine_id": deepcopy(EXPLICIT_ROUTE_PROPERTIES["machine_id"]),
            "workspace_ref": deepcopy(EXPLICIT_ROUTE_PROPERTIES["workspace_ref"]),
            "repo_path": deepcopy(EXPLICIT_ROUTE_PROPERTIES["repo_path"]),
        }
    )
    required = list(source["inputSchema"].get("required", []))
    if target_name in HUB_V2_MUTATING_TOOL_NAMES:
        properties["expected_revision"] = _integer(
            "Expected Pro Request revision for compare-and-set mutation.",
            minimum=0,
        )
        properties["idempotency_key"] = deepcopy(IDEMPOTENCY_KEY_SCHEMA)
        required.append("idempotency_key")
    description = _target_description(
        str(source["description"]),
        "Hub applies principal, participant, group, machine-generation, workspace, and revision consistency before returning or mutating the machine-affine request.",
    )
    return _descriptor(
        target_name,
        description,
        _input_schema(properties, required=required),
        _routed_result_schema(PRO_REQUEST_OUTPUT_SCHEMA),
        source=source,
    )


def _operation_status_descriptor() -> dict[str, Any]:
    return _descriptor(
        "patchbay_operation_status",
        "Recover a routed call that returned pending or whose outcome is still reconciling. This exceptional tool returns semantic state and safe next action without exposing another group or principal's raw queue data.",
        _input_schema(
            {
                "operation_id": _string("Opaque operation id returned by a prior V2 tool call."),
                "wait_seconds": _integer("Optional bounded Hub-side wait for operation revision change.", minimum=0),
                "include_result": _boolean("Include the action result when it is available and visible."),
                "since_revision": _integer("Return changes after this operation revision.", minimum=0),
            },
            required=("operation_id",),
        ),
        _result_schema(
            {
                "dispatch": {"type": "object", "additionalProperties": True},
                "outcome": {"type": "object", "additionalProperties": True},
                "attempt": {"type": "object", "additionalProperties": True},
                "receipt": {"type": "object", "additionalProperties": True},
                "domain_result": {"type": "object", "additionalProperties": True},
                "safe_next_action": {"type": "string"},
            }
        ),
    )


def _build_registry() -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = [
        _fleet_status_descriptor(),
        _workspace_list_descriptor(),
        *_work_group_descriptors(),
        _worker_options_descriptor(),
        _worker_inbox_descriptor(),
        _worker_start_descriptor(),
        _worker_start_batch_descriptor(),
        _worker_message_descriptor(),
        _worker_collection_descriptor("codex_worker_list", "patchbay_worker_list"),
        _worker_collection_descriptor("codex_worker_status", "patchbay_worker_status"),
        _worker_collection_descriptor("codex_worker_wait", "patchbay_worker_wait"),
        _worker_inspect_descriptor(),
        _worker_integrate_descriptor(),
        _worker_stop_descriptor(),
        *_workspace_descriptors(),
    ]
    descriptors.extend(
        _pro_request_descriptor(f"codex_pro_request_{suffix}", f"patchbay_pro_request_{suffix}")
        for suffix in ("list", "read", "claim", "respond", "dispatch", "close")
    )
    descriptors.append(_operation_status_descriptor())
    return descriptors


# The values are execution identities, not handlers. Hub-local projection and
# lifecycle actions deliberately do not masquerade as Edge calls.
HUB_V2_ACTION_MAP: dict[str, str] = {
    "patchbay_fleet_status": "hub.fleet_status",
    "patchbay_workspace_list": "hub.workspace_list",
    "patchbay_work_group_create": "hub.work_group_create",
    "patchbay_work_group_list": "hub.work_group_list",
    "patchbay_work_group_status": "hub.work_group_status",
    "patchbay_work_group_resume": "hub.work_group_resume",
    "patchbay_work_group_reassign": "hub.work_group_reassign",
    "patchbay_work_group_close": "hub.work_group_close",
    "patchbay_worker_options": "codex_worker_options",
    "patchbay_worker_inbox": "codex_worker_inbox",
    "patchbay_worker_start": "codex_worker_start",
    "patchbay_worker_start_batch": "codex_worker_start",
    "patchbay_worker_message": "codex_worker_message",
    "patchbay_worker_list": "hub.worker_projection_list",
    "patchbay_worker_status": "hub.worker_projection_status",
    "patchbay_worker_wait": "hub.worker_projection_wait",
    "patchbay_worker_inspect": "codex_worker_inspect",
    "patchbay_worker_integrate": "codex_worker_integrate",
    "patchbay_worker_stop": "codex_worker_stop",
    "patchbay_workspace_open": "codex_open_workspace",
    "patchbay_workspace_tree": "codex_repo_tree",
    "patchbay_workspace_search": "codex_search_repo",
    "patchbay_workspace_read_file": "codex_read_file",
    "patchbay_workspace_changes": "hub.workspace_changes_by_view",
    "patchbay_pro_request_list": "codex_pro_request_list",
    "patchbay_pro_request_read": "codex_pro_request_read",
    "patchbay_pro_request_claim": "codex_pro_request_claim",
    "patchbay_pro_request_respond": "codex_pro_request_respond",
    "patchbay_pro_request_dispatch": "codex_pro_request_dispatch",
    "patchbay_pro_request_close": "codex_pro_request_close",
    "patchbay_operation_status": "hub.operation_status",
}

HUB_V2_WORKSPACE_CHANGES_ACTION_MAP: dict[str, str] = {
    "status": "codex_git_status",
    "summary": "codex_show_changes",
    "diff": "codex_git_diff",
}

HUB_V2_ACTION_SPECS: dict[str, dict[str, Any]] = {
    name: {
        "executor": (
            "edge_batch"
            if name == "patchbay_worker_start_batch"
            else "edge_by_view"
            if name == "patchbay_workspace_changes"
            else "edge"
            if action.startswith("codex_")
            else "hub"
        ),
        "action": action,
        "capability_version": HUB_V2_ACTION_CAPABILITY_VERSION,
        **(
            {"view_actions": deepcopy(HUB_V2_WORKSPACE_CHANGES_ACTION_MAP)}
            if name == "patchbay_workspace_changes"
            else {}
        ),
    }
    for name, action in HUB_V2_ACTION_MAP.items()
}

HUB_V2_EDGE_ACTION_MAP = {
    name: action
    for name, action in HUB_V2_ACTION_MAP.items()
    if action.startswith("codex_")
}

HUB_V2_TOOL_REGISTRY = tuple(_build_registry())
HUB_V2_TOOLS = HUB_V2_TOOL_REGISTRY
HUB_V2_TOOL_DESCRIPTORS = HUB_V2_TOOL_REGISTRY
HUB_V2_TOOLS_BY_NAME = {tool["name"]: tool for tool in HUB_V2_TOOL_REGISTRY}


def get_hub_v2_tools() -> list[dict[str, Any]]:
    """Return a defensive copy of the ordered internal catalog."""
    return deepcopy(list(HUB_V2_TOOL_REGISTRY))


def get_hub_v2_tool(name: str) -> dict[str, Any]:
    """Return one defensive descriptor copy or raise ``KeyError``."""
    return deepcopy(HUB_V2_TOOLS_BY_NAME[name])


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_hash(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def compute_tool_manifest_hash(descriptors: Sequence[Mapping[str, Any]]) -> str:
    """Hash only the ordered public names that make up the catalog manifest."""
    return _json_hash([str(descriptor["name"]) for descriptor in descriptors])


def compute_tool_schema_hash(descriptors: Sequence[Mapping[str, Any]]) -> str:
    """Hash ordered input/output schemas while ignoring mapping key order."""
    return _json_hash(
        [
            {
                "name": str(descriptor["name"]),
                "inputSchema": descriptor["inputSchema"],
                "outputSchema": descriptor["outputSchema"],
            }
            for descriptor in descriptors
        ]
    )


def hub_v2_manifest(registry: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build the ordered capability manifest used for Hub/Edge negotiation."""
    tools = registry if registry is not None else HUB_V2_TOOL_REGISTRY
    return {
        "contract_version": HUB_V2_CONTRACT_VERSION,
        "action_capability_version": HUB_V2_ACTION_CAPABILITY_VERSION,
        "tool_count": len(tools),
        "tools": [
            {
                "name": str(tool["name"]),
                "annotations": deepcopy(dict(tool["annotations"])),
                "action": deepcopy(HUB_V2_ACTION_SPECS[str(tool["name"])]),
            }
            for tool in tools
        ],
    }


def hub_v2_schema_manifest(registry: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build the ordered input/output schema manifest."""
    tools = registry if registry is not None else HUB_V2_TOOL_REGISTRY
    return {
        "contract_version": HUB_V2_CONTRACT_VERSION,
        "schemas": [
            {
                "name": str(tool["name"]),
                "inputSchema": deepcopy(dict(tool["inputSchema"])),
                "outputSchema": deepcopy(dict(tool["outputSchema"])),
            }
            for tool in tools
        ],
    }


def hub_v2_contract_manifest(registry: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build the complete descriptor contract, including Apps metadata."""
    tools = registry if registry is not None else HUB_V2_TOOL_REGISTRY
    return {
        "contract_version": HUB_V2_CONTRACT_VERSION,
        "manifest": hub_v2_manifest(tools),
        "schemas": hub_v2_schema_manifest(tools),
        "descriptors": deepcopy([dict(tool) for tool in tools]),
    }


def compute_hub_v2_manifest_hash(registry: Sequence[Mapping[str, Any]] | None = None) -> str:
    return _json_hash(hub_v2_manifest(registry))


def compute_hub_v2_schema_hash(registry: Sequence[Mapping[str, Any]] | None = None) -> str:
    return _json_hash(hub_v2_schema_manifest(registry))


def compute_hub_v2_contract_hash(registry: Sequence[Mapping[str, Any]] | None = None) -> str:
    return _json_hash(hub_v2_contract_manifest(registry))


# Short aliases make the helpers convenient to use from future handshake code
# without binding that code to constants computed at import time.
tool_manifest_hash = compute_hub_v2_manifest_hash
tool_schema_hash = compute_hub_v2_schema_hash
tool_contract_hash = compute_hub_v2_contract_hash


def validate_hub_v2_registry(registry: Sequence[Mapping[str, Any]] | None = None) -> None:
    """Fail fast on drift in the frozen WP-00 machine contract."""
    tools = registry if registry is not None else HUB_V2_TOOL_REGISTRY
    names = tuple(str(tool.get("name") or "") for tool in tools)
    if names != HUB_V2_TOOL_NAMES:
        raise ValueError(f"Hub V2 tool order mismatch: {names!r}")
    if len(names) != HUB_V2_EXPECTED_TOOL_COUNT or len(set(names)) != len(names):
        raise ValueError("Hub V2 registry must contain exactly 31 unique tools")
    removed = set(names).intersection(HUB_V1_ONLY_TOOL_NAMES)
    if removed:
        raise ValueError(f"V1-only tools present in Hub V2 registry: {sorted(removed)!r}")
    if set(HUB_V2_ACTION_MAP) != set(names) or set(HUB_V2_ACTION_SPECS) != set(names):
        raise ValueError("Every Hub V2 tool must have exactly one action mapping")

    expected_envelope_keys = {"status", "result", "operation", "warnings", "next_actions"}
    for tool in tools:
        name = str(tool["name"])
        input_schema = tool.get("inputSchema")
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            raise ValueError(f"{name} lacks an object input schema")
        if input_schema.get("additionalProperties") is not False:
            raise ValueError(f"{name} input schema is not strict")
        output_schema = tool.get("outputSchema")
        if not isinstance(output_schema, dict) or output_schema.get("additionalProperties") is not False:
            raise ValueError(f"{name} output envelope is not strict")
        if set(output_schema.get("required", [])) != expected_envelope_keys:
            raise ValueError(f"{name} output envelope required fields drifted")
        output_properties = output_schema.get("properties", {})
        if set(output_properties) != expected_envelope_keys:
            raise ValueError(f"{name} output envelope fields drifted")
        if tuple(output_properties["status"].get("enum", ())) != HUB_V2_PUBLIC_STATUSES:
            raise ValueError(f"{name} uses noncanonical public statuses")
        next_action_items = output_properties["next_actions"].get("items", {})
        action_branches = next_action_items.get("oneOf", []) if isinstance(next_action_items, Mapping) else []
        action_schema = action_branches[1] if len(action_branches) == 2 else {}
        action_tool = action_schema.get("properties", {}).get("tool", {}) if isinstance(action_schema, Mapping) else {}
        if tuple(action_tool.get("enum", ())) != HUB_V2_TOOL_NAMES:
            raise ValueError(f"{name} next actions are not bound to the exact Hub V2 tool registry")
        annotations = tool.get("annotations")
        if annotations != _annotations(name):
            raise ValueError(f"{name} annotations are not truthful for the frozen contract")
        if tool.get("readOnlyHint") is not annotations["readOnlyHint"]:
            raise ValueError(f"{name} legacy readOnlyHint disagrees with annotations")
        if tool.get("securitySchemes") != HUB_V2_SECURITY_SCHEMES:
            raise ValueError(f"{name} lacks the canonical top-level securitySchemes")
        meta = tool.get("_meta")
        if not isinstance(meta, Mapping) or meta.get("securitySchemes") != HUB_V2_SECURITY_SCHEMES:
            raise ValueError(f"{name} lacks the canonical _meta.securitySchemes")
        if name in HUB_V2_MUTATING_TOOL_NAMES and "idempotency_key" not in input_schema.get("properties", {}):
            raise ValueError(f"{name} mutation lacks idempotency_key")
        if "handler" in tool:
            raise ValueError(f"{name} must not advertise a fake handler")

    inbox_meta = HUB_V2_TOOLS_BY_NAME["patchbay_worker_inbox"].get("_meta", {})
    if inbox_meta.get("openai/fileParams") != ["artifact_file"]:
        raise ValueError("patchbay_worker_inbox lost Apps file parameter metadata")


validate_hub_v2_registry()

HUB_V2_TOOL_MANIFEST_HASH = compute_tool_manifest_hash(HUB_V2_TOOL_DESCRIPTORS)
HUB_V2_TOOL_SCHEMA_HASH = compute_tool_schema_hash(HUB_V2_TOOL_DESCRIPTORS)
HUB_V2_CAPABILITY_MANIFEST_HASH = compute_hub_v2_manifest_hash()
HUB_V2_CAPABILITY_SCHEMA_HASH = compute_hub_v2_schema_hash()
HUB_V2_MANIFEST_HASH = HUB_V2_TOOL_MANIFEST_HASH
HUB_V2_SCHEMA_HASH = HUB_V2_TOOL_SCHEMA_HASH
HUB_V2_CONTRACT_HASH = compute_hub_v2_contract_hash()


__all__ = [
    "HUB_V1_ONLY_TOOL_NAMES",
    "HUB_V2_ACTION_CAPABILITY_VERSION",
    "HUB_V2_ACTION_MAP",
    "HUB_V2_ACTION_SPECS",
    "HUB_V2_CAPABILITY_MANIFEST_HASH",
    "HUB_V2_CAPABILITY_SCHEMA_HASH",
    "HUB_V2_CONTRACT_HASH",
    "HUB_V2_CONTRACT_VERSION",
    "HUB_V2_DESTRUCTIVE_TOOL_NAMES",
    "HUB_V2_EDGE_ACTION_MAP",
    "HUB_V2_EXPECTED_TOOL_COUNT",
    "HUB_V2_MANIFEST_HASH",
    "HUB_V2_MUTATING_TOOL_NAMES",
    "HUB_V2_OPEN_WORLD_TOOL_NAMES",
    "HUB_V2_OPERATION_SCHEMA",
    "HUB_V2_PUBLIC_STATUSES",
    "HUB_V2_SCHEMA_HASH",
    "HUB_V2_SECURITY_SCHEMES",
    "HUB_V2_TOOLS",
    "HUB_V2_TOOLS_BY_NAME",
    "HUB_V2_TOOL_NAMES",
    "HUB_V2_TOOL_NAME_SET",
    "HUB_V2_TOOL_DESCRIPTORS",
    "HUB_V2_TOOL_FAMILIES",
    "HUB_V2_TOOL_MANIFEST_HASH",
    "HUB_V2_TOOL_REGISTRY",
    "HUB_V2_TOOL_SCHEMA_HASH",
    "HUB_V2_WORKSPACE_CHANGES_ACTION_MAP",
    "HUB_V2_WORKER_RESULT_SCHEMA",
    "HUB_V2_UNSUPPORTED_WORKER_COLLECTION_FIELDS",
    "REMOVED_HUB_V1_TOOL_NAMES",
    "TARGET_HUB_V2_TOOL_NAMES",
    "compute_hub_v2_contract_hash",
    "compute_hub_v2_manifest_hash",
    "compute_hub_v2_schema_hash",
    "compute_tool_manifest_hash",
    "compute_tool_schema_hash",
    "get_hub_v2_tool",
    "get_hub_v2_tools",
    "hub_v2_contract_manifest",
    "normalize_hub_v2_next_action",
    "normalize_hub_v2_next_actions",
    "hub_v2_manifest",
    "hub_v2_schema_manifest",
    "output_envelope_schema",
    "tool_contract_hash",
    "tool_manifest_hash",
    "tool_schema_hash",
    "validate_hub_v2_registry",
]
