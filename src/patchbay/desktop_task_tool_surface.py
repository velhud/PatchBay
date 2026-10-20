"""Public descriptors for the opt-in Codex Desktop task bridge."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from patchbay.desktop_tasks import MAX_PROMPT_LENGTH_CAP, desktop_tasks_enabled


DESKTOP_TASK_START_TOOL_NAME = "codex_desktop_task_start"
DESKTOP_TASK_STATUS_TOOL_NAME = "codex_desktop_task_status"
DESKTOP_TASK_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "target": {"type": "string"},
        "receipt_id": {"type": "string"},
        "state": {"type": "string", "enum": ["queued", "running", "completed", "failed"]},
        "permission_mode_requested": {
            "type": ["string", "null"],
            "description": "The one-turn mode requested by the caller, or null when the alias default was used.",
        },
        "permission_mode_effective": {
            "type": ["string", "null"],
            "description": "The private allowlisted Codex sandbox mode applied to this receipt.",
        },
        "answer": {"type": "string"},
        "answer_truncated": {"type": "boolean"},
        "report_format": {"type": "string", "enum": ["structured", "markdown"]},
        "report": {"type": "string"},
        "report_total_length": {"type": "integer"},
        "report_offset": {"type": "integer"},
        "report_next_offset": {"type": ["integer", "null"]},
        "report_complete": {"type": "boolean"},
        "report_capped": {"type": "boolean"},
        "event_count": {"type": "integer"},
        "error_code": {"type": "string"},
        "error": {"type": "string"},
        "warning_code": {"type": "string"},
        "warning": {"type": "string"},
        "cleanup_pending": {"type": "boolean"},
        "cleanup_warning_code": {"type": "string"},
        "cleanup_warning": {"type": "string"},
    },
}


_COMMON_TARGET_PROPERTIES = {
    "target": {
        "type": "string",
        "description": "Allowlisted human alias for a pre-registered Desktop task. Raw Codex task/session ids are not accepted.",
    },
    "receipt_id": {
        "type": "string",
        "description": "Stable caller receipt id. Reusing it with different input is rejected; retention is bounded.",
    },
}


DESKTOP_TASK_START_TOOL: Dict[str, Any] = {
    "name": DESKTOP_TASK_START_TOOL_NAME,
    "description": (
        "Experimental mutating/open-world bridge for a pre-registered Codex Desktop task. "
        "Use only after explicit user intent. By default the operator must complete a manual Desktop handoff; "
        "a private alias may opt into the supported Codex app-server archive/unarchive handoff. Start returns a queued or running receipt "
        "for a healthy handoff, or surfaces an immediate startup failure (such as active_writer, archived_thread, "
        "missing task, auth, or model rejection) during a short bounded handshake; use codex_desktop_task_status "
        "for local progress and the bounded final answer. "
        "For manual aliases, the operator must archive the task in Desktop, unarchive it in Desktop, then leave it idle/unloaded before starting. "
        "For app-server aliases, PatchBay performs that sequence through the private configured Codex app-server and verifies idle/notLoaded readiness. "
        "Desktop remains the transcript viewer. PatchBay never edits session files or uses CLI archive/unarchive fallbacks. "
        "A private alias may allow one Codex sandbox mode for this receipt only; omission uses the alias default for this turn. "
        "For operator-configured MTP Luna Web flows, callers should omit sandbox overrides and let the local default apply. "
        "The response reports requested and effective modes; a request never changes the alias default. "
        "active_writer and archived_thread failures require Desktop recovery and a new receipt_id. "
        "The feature exists only when desktop_tasks.enabled=true and a private targets_file are configured."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **_COMMON_TARGET_PROPERTIES,
            "prompt": {
                "type": "string",
                "maxLength": MAX_PROMPT_LENGTH_CAP,
                "description": (
                    "Bounded natural-language prompt for the next Desktop task turn. "
                    "The private alias may impose a lower configured limit."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Optional bounded local execution timeout; capped by desktop_tasks.timeout_ms.",
            },
            "permission_mode": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": (
                    "Optional one-turn Codex sandbox mode. The private alias allowlist decides whether it is accepted; "
                    "omitting it uses the alias default."
                ),
            },
        },
        "required": ["target", "receipt_id", "prompt"],
    },
    "readOnlyHint": False,
}


DESKTOP_TASK_STATUS_TOOL: Dict[str, Any] = {
    "name": DESKTOP_TASK_STATUS_TOOL_NAME,
    "description": (
        "Read the local durable receipt for a pre-registered Codex Desktop task. "
        "Returns queued, running, completed, or failed and includes the bounded final answer only after completion. "
        "Completed reports are available through bounded report chunks; use report_offset and report_limit to retrieve later chunks. "
        "This is local process/job monitoring, not a model polling request. Pass only the human alias and receipt_id; "
        "raw task/session ids, paths, prompts, and unstructured CLI output are never returned. A completed receipt "
        "may include cleanup_pending when PatchBay retained a fail-closed process cleanup barrier. Retention is bounded."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            **deepcopy(_COMMON_TARGET_PROPERTIES),
            "report_offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Optional character offset into the sanitized durable report.",
            },
            "report_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 12000,
                "description": "Optional bounded report chunk size in characters; maximum 12000.",
            },
        },
        "required": ["target", "receipt_id"],
    },
    "readOnlyHint": True,
}


def install_desktop_task_tool_surface(
    *,
    tools: list[Dict[str, Any]],
    tools_by_name: Dict[str, Dict[str, Any]],
    public_tool_names: set[str],
    tool_modes: Dict[str, set[str]],
    open_world_tools: set[str],
    non_idempotent_tools: set[str],
    invocation_status: Dict[str, tuple[str, str]],
    output_schemas: Dict[str, Dict[str, Any]],
) -> None:
    for descriptor in (DESKTOP_TASK_START_TOOL, DESKTOP_TASK_STATUS_TOOL):
        name = descriptor["name"]
        if name not in tools_by_name:
            copied = deepcopy(descriptor)
            tools.append(copied)
            tools_by_name[name] = copied
            public_tool_names.add(name)
        for mode in ("worker", "standard", "full"):
            tool_modes.setdefault(mode, set()).add(name)
        output_schemas[name] = deepcopy(DESKTOP_TASK_OUTPUT_SCHEMA)

    open_world_tools.add(DESKTOP_TASK_START_TOOL_NAME)
    non_idempotent_tools.add(DESKTOP_TASK_START_TOOL_NAME)
    invocation_status[DESKTOP_TASK_START_TOOL_NAME] = (
        "Starting Desktop task",
        "Desktop task receipt created",
    )
    invocation_status[DESKTOP_TASK_STATUS_TOOL_NAME] = (
        "Reading Desktop task status",
        "Desktop task status ready",
    )


__all__ = [
    "DESKTOP_TASK_OUTPUT_SCHEMA",
    "DESKTOP_TASK_START_TOOL",
    "DESKTOP_TASK_START_TOOL_NAME",
    "DESKTOP_TASK_STATUS_TOOL",
    "DESKTOP_TASK_STATUS_TOOL_NAME",
    "desktop_tasks_enabled",
    "install_desktop_task_tool_surface",
]
