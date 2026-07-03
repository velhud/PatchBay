"""Public MCP descriptors for the natural-language worker facade."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


WORKER_VIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "worker_id": {"type": "string"},
        "name": {"type": "string"},
        "workspace_id": {"type": "string"},
        "workspace_name": {"type": "string"},
        "workspace_mode": {"type": "string"},
        "workspace_available": {"type": "boolean"},
        "workspace_location": {"type": "string"},
        "state": {"type": "string"},
        "report": {"type": "string"},
        "worker_report_files": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "has_changes": {"type": "boolean"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "change_count": {"type": "integer"},
        "file_path": {"type": "string"},
        "source": {"type": "string"},
        "exists": {"type": "boolean"},
        "text": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "next_start_line": {"type": "integer"},
        "total_lines": {"type": "integer"},
        "bytes": {"type": "integer"},
        "max_bytes_applied": {"type": "integer"},
        "sha256": {"type": "string"},
        "diff": {"type": "string"},
        "truncated": {"type": "boolean"},
        "has_session": {"type": "boolean"},
        "can_message": {"type": "boolean"},
        "last_activity_at": {"type": "number"},
        "accepted": {"type": "boolean"},
        "stopped": {"type": "boolean"},
        "workspace_cleaned": {"type": "boolean"},
        "note": {"type": "string"},
        "context_sources": {"type": "array", "items": {"type": "string"}},
        "context_detail": {"type": "string"},
        "context_truncated": {"type": "boolean"},
        "integration_state": {"type": "string"},
        "can_apply": {"type": "boolean"},
        "applied": {"type": "boolean"},
        "apply_check": {"type": "string"},
        "base_dirty": {"type": "boolean"},
        "base_moved": {"type": "boolean"},
        "base_changed_files": {"type": "array", "items": {"type": "string"}},
        "main_changed_files": {"type": "array", "items": {"type": "string"}},
        "blocked_files": {"type": "array", "items": {"type": "string"}},
        "skipped_files": {"type": "array", "items": {"type": "string"}},
        "conflict_summary": {"type": "string"},
        "patch_sha256": {"type": "string"},
        "patch_bytes": {"type": "integer"},
        "patch_truncated": {"type": "boolean"},
        "base_revision": {"type": "string"},
        "worker_base_revision": {"type": "string"},
        "model": {"type": "string"},
        "reasoning_effort": {"type": "string"},
        "owned_by_current_client": {"type": ["boolean", "null"]},
        "ownership_status": {"type": "string"},
        "owner_label": {"type": "string"},
        "ownership_note": {"type": "string"},
        "takeover_required": {"type": "boolean"},
        "takeover_performed": {"type": "boolean"},
        "required_action": {"type": "string"},
        "latest_turn": {"type": "object", "additionalProperties": True},
    },
}

WORKER_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "workers": {"type": "array", "items": WORKER_VIEW_SCHEMA},
        "count": {"type": "integer"},
        "active": {"type": "integer"},
        "team_report": {"type": "string"},
    },
}

WORKER_OPTIONS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "source": {"type": "string"},
        "codex_version": {"type": "string"},
        "default_model": {"type": "string"},
        "default_reasoning_effort": {"type": "string"},
        "selected_model": {"type": "object", "additionalProperties": True},
        "selected_reasoning_effort": {"type": "string"},
        "reasoning_efforts": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "models": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "model_count": {"type": "integer"},
        "models_truncated": {"type": "boolean"},
        "allows_custom_model_string": {"type": "boolean"},
        "worker_start_fields": {"type": "object", "additionalProperties": True},
        "next_step": {"type": "string"},
        "note": {"type": "string"},
    },
}

WORKER_INBOX_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "artifact_id": {"type": "string"},
        "workspace_id": {"type": "string"},
        "label": {"type": "string"},
        "kind": {"type": "string"},
        "original_file_name": {"type": "string"},
        "mime_type": {"type": "string"},
        "sha256": {"type": "string"},
        "total_bytes": {"type": "integer"},
        "unpacked_bytes": {"type": "integer"},
        "file_count": {"type": "integer"},
        "top_level_entries": {"type": "array", "items": {"type": "string"}},
        "artifacts": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "count": {"type": "integer"},
        "view": {"type": "string"},
        "entries": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "file_path": {"type": "string"},
        "exists": {"type": "boolean"},
        "text": {"type": "string"},
        "bytes": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "removed": {"type": "boolean"},
        "owned_by_current_client": {"type": ["boolean", "null"]},
        "ownership_status": {"type": "string"},
        "owner_label": {"type": "string"},
        "ownership_note": {"type": "string"},
        "takeover_required": {"type": "boolean"},
        "takeover_performed": {"type": "boolean"},
        "required_action": {"type": "string"},
        "next_step": {"type": "string"},
        "note": {"type": "string"},
    },
}

WORKER_TAKEOVER_PROPERTIES: Dict[str, Any] = {
    "takeover": {
        "type": "boolean",
        "description": (
            "Use only when the user explicitly wants this chat to take control of a worker or artifact "
            "created by another MCP connection. Default: false."
        ),
    },
    "takeover_reason": {
        "type": "string",
        "description": "Optional short reason for takeover, bounded by server policy and not used for authentication.",
    },
}

WORKER_EXECUTION_OPTION_PROPERTIES: Dict[str, Any] = {
    "model": {
        "type": "string",
        "description": (
            "Optional Codex model id for this worker, such as a model returned by codex_worker_options. "
            "Omit to use the configured Codex default."
        ),
    },
    "reasoning_effort": {
        "type": "string",
        "enum": ["minimal", "low", "medium", "high", "xhigh"],
        "description": (
            "Optional Codex reasoning effort for supported models. Omit to use the selected model or Codex default."
        ),
    },
}

WORKER_TOOLS = [
    {
        "name": "codex_worker_options",
        "description": (
            "Read-only progressive setup menu for worker execution choices. Call this when ChatGPT needs to "
            "choose a Codex model or reasoning effort before starting or continuing a worker. It loads bounded "
            "model metadata from the installed Codex runtime/catalog and explains which fields to pass to "
            "codex_worker_start or codex_worker_message."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Optional model id to focus the returned reasoning options.",
                },
                "max_models": {
                    "type": "integer",
                    "description": "Maximum model menu entries to return. Default 12; capped by server policy.",
                },
                "include_model_details": {
                    "type": "boolean",
                    "description": "When true, include extra bounded service-tier details if Codex exposes them.",
                },
            },
            "required": [],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_worker_inbox",
        "description": (
            "Use this when ChatGPT needs to send a generated file or zip package to local Codex workers. "
            "action=import_file downloads the provided Apps file into a local artifact inbox without editing "
            "the repo. action=list and action=inspect help choose artifact ids. Then pass artifact ids in "
            "context_from_artifacts on codex_worker_start or codex_worker_message so an isolated worker can "
            "read them. action=cleanup removes local inbox copies only; if the artifact belongs to another "
            "MCP connection, retry cleanup with takeover=true only after user confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["import_file", "list", "inspect", "cleanup"],
                    "description": "Inbox operation. Use import_file to receive a ChatGPT file, list to see recent artifacts, inspect for tree/file details, or cleanup to remove a local artifact.",
                },
                "artifact_file": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "download_url": {"type": "string"},
                        "file_id": {"type": "string"},
                        "mime_type": {"type": "string"},
                        "file_name": {"type": "string"},
                    },
                    "description": "Apps SDK file parameter for action=import_file. ChatGPT supplies download_url, file_id, mime_type, and file_name.",
                },
                "artifact_id": {
                    "type": "string",
                    "description": "Artifact id returned by import_file; required for inspect or cleanup.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short human label for action=import_file.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to scope the inbox. Defaults to the configured workspace.",
                },
                "view": {
                    "type": "string",
                    "enum": ["summary", "tree", "file", "raw_manifest"],
                    "description": "Inspect view. Use tree or raw_manifest before reading specific files; use file with file_path only when contents are needed.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Artifact-relative file path required for action=inspect with view=file.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes returned for view=file. This limits tool output, not artifact import size.",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries returned for view=tree or view=raw_manifest. This limits tool output, not artifact import size.",
                },
                **WORKER_TAKEOVER_PROPERTIES,
            },
            "required": ["action"],
        },
        "readOnlyHint": False,
        "_meta": {"openai/fileParams": ["artifact_file"]},
    },
    {
        "name": "codex_worker_start",
        "description": (
            "Appoint a durable named Codex colleague for autonomous investigation, implementation, review, "
            "or verification. Use this as the normal path for non-trivial repository work so ChatGPT can lead "
            "instead of micromanaging code. Give goals, context, constraints, deliverables, and expected report "
            "in the natural-language brief; do not spell out every file or line unless truly necessary. "
            "For consequential audits or implementation, ask the worker to write a durable report file or "
            "changed-file evidence in its worker workspace, not only a brief chat summary. "
            "Defaults to an isolated writing worktree; choose workspace_mode=read_only for advisory work. "
            "For larger tasks, start multiple workers with separate responsibilities and reconcile their reports. "
            "Can include bounded context from other workers for review, alternatives, or handoff. When a "
            "specific model or reasoning depth matters, call codex_worker_options first, then pass model and/or "
            "reasoning_effort here. Worker names are scoped to the target workspace, so the same name can be reused "
            "safely in another repo."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable worker name, such as Connector Investigator.",
                },
                "brief": {
                    "type": "string",
                    "description": "Natural-language assignment. Put goals, context, and expected report here.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Optional owned or authorized repository path. Defaults to the configured workspace.",
                },
                "workspace_mode": {
                    "type": "string",
                    "enum": ["isolated_write", "read_only", "shared_write"],
                    "description": "Worker workspace mode. Default: isolated_write.",
                },
                "context_from_workers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional worker names or ids whose reports/changes/diffs should be included as "
                        "natural-language peer context for this worker turn. Capped by server policy."
                    ),
                },
                "context_from_artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional artifact ids from codex_worker_inbox. Selected artifacts are copied into "
                        "the isolated worker worktree as source material and are excluded from integration."
                    ),
                },
                "context_detail": {
                    "type": "string",
                    "enum": ["report", "changes", "diff"],
                    "description": "How much peer-worker context to include. Default: report.",
                },
                **WORKER_EXECUTION_OPTION_PROPERTIES,
            },
            "required": ["name", "brief"],
        },
        "readOnlyHint": False,
    },
    {
        "name": "codex_worker_message",
        "description": (
            "Continue, question, or redirect an existing named Codex worker in natural language, preserving its "
            "session and worker worktree when available. Use this for follow-up, review feedback, clarification, "
            "or revision of the same worker's task, not for a new independent colleague. Prefer asking the worker "
            "to investigate or adjust over ChatGPT doing a manual file-by-file implementation loop. Use follow-up "
            "messages when a report is thin, contradictory, missing evidence, lacks a durable report file, or needs "
            "another worker's findings. Can include bounded peer report/change/diff context without exposing backend ids. "
            "By default the worker keeps its prior model/reasoning choices; "
            "pass model or reasoning_effort only to intentionally change them for this continuation. If the worker "
            "belongs to another MCP connection, retry with takeover=true only after user confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "worker": {"type": "string", "description": "Worker name or worker id."},
                "message": {"type": "string", "description": "Natural-language follow-up or correction."},
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to resolve a worker name in that workspace. Worker ids are globally unique.",
                },
                "context_from_workers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional worker names or ids whose reports/changes/diffs should be included as "
                        "natural-language peer context for this worker turn. Capped by server policy."
                    ),
                },
                "context_from_artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional artifact ids from codex_worker_inbox to add to this isolated worker turn. "
                        "Use when ChatGPT imported a later file or zip for the same worker."
                    ),
                },
                "context_detail": {
                    "type": "string",
                    "enum": ["report", "changes", "diff"],
                    "description": "How much peer-worker context to include. Default: report.",
                },
                **WORKER_TAKEOVER_PROPERTIES,
                **WORKER_EXECUTION_OPTION_PROPERTIES,
            },
            "required": ["worker", "message"],
        },
        "readOnlyHint": False,
    },
    {
        "name": "codex_worker_list",
        "description": (
            "List durable Codex workers as an engineering lead would want to see them: names, human-readable "
            "state, latest report, team summary, and whether each worker can receive a follow-up. Use this to manage "
            "a small team of worker threads, after restart, or before choosing which worker to inspect, message, stop, or integrate. "
            "Use active_only, owned_only, include_stopped=false, or created_after to reduce historical worker clutter during a specific task. "
            "If the team report shows thin, failed, stale, or conflicting work, continue the relevant named worker instead "
            "of treating first reports as final. By default ChatGPT "
            "sees workers for the current workspace, so old workers from other repos do not steal the same name."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to filter workers.",
                },
                "active_only": {
                    "type": "boolean",
                    "description": "When true, return only workers whose latest turn is starting or working.",
                },
                "include_stopped": {
                    "type": "boolean",
                    "description": "When false, omit stopped workers from the list. Default: true.",
                },
                "owned_only": {
                    "type": "boolean",
                    "description": "When true, return only workers owned by the current coordination owner.",
                },
                "created_after": {
                    "type": "number",
                    "description": "Optional Unix timestamp; return workers first created at or after this time.",
                },
            },
            "required": [],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_worker_inspect",
        "description": (
            "Read one worker's current state and latest natural-language report. Optionally wait briefly for the "
            "current turn; this does not expose private repo paths, job ids, session ids, or raw transcripts. "
            "Use the report as the normal management signal, but question the worker again with codex_worker_message "
            "when evidence is missing, output is too compressed, or another worker disagrees. Use view=changes, view=diff with file_path, view=file with file_path for worker-created files before "
            "integration, or view=integration_preview when verifying an accepted worker result. codex_read_file reads the "
            "base checkout, not an isolated worker worktree."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "worker": {"type": "string", "description": "Worker name or worker id."},
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to resolve a worker name in that workspace. Worker ids are globally unique.",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "Optional brief wait for completion, capped at 30 seconds.",
                },
                "view": {
                    "type": "string",
                    "enum": ["report", "status", "changes", "diff", "file", "integration_preview"],
                    "description": "Report/status by default. Use changes for file inventory, diff with file_path, file with file_path for worker-created file content, or integration_preview before accepting a worker result.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Workspace-relative path required for view=diff or view=file.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based start line for view=file. Default: 1.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-based inclusive end line for view=file. Default: file end.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes for view=file, capped by server policy. Use start_line/end_line for pagination when a file is larger than the cap.",
                },
            },
            "required": ["worker"],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_worker_integrate",
        "description": (
            "Apply an explicitly accepted isolated writing worker result to the base checkout after inspecting "
            "the report/diff and preferably view=integration_preview. This is the human-level act: use this "
            "colleague's result. It does not commit, does not delete the worker worktree, and refuses dirty-base "
            "or conflicted application unless an expert override is supplied. If the worker belongs to another "
            "MCP connection, retry with takeover=true only after user confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "worker": {"type": "string", "description": "Worker name or worker id."},
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to resolve a worker name in that workspace. Worker ids are globally unique.",
                },
                "allow_dirty_base": {
                    "type": "boolean",
                    "description": "Expert override allowing integration into a dirty base checkout when git apply still succeeds. Default: false.",
                },
                **WORKER_TAKEOVER_PROPERTIES,
            },
            "required": ["worker"],
        },
        "readOnlyHint": False,
    },
    {
        "name": "codex_worker_stop",
        "description": (
            "Stop a named worker's active Codex turn. The durable worker identity and Codex conversation are "
            "preserved so ChatGPT can continue the colleague later. Set cleanup_workspace=true only when the "
            "user intentionally wants to discard that worker's isolated worktree. If the worker belongs to "
            "another MCP connection, retry with takeover=true only after user confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "worker": {"type": "string", "description": "Worker name or worker id."},
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to resolve a worker name in that workspace. Worker ids are globally unique.",
                },
                "cleanup_workspace": {
                    "type": "boolean",
                    "description": "When true, discard the isolated worker worktree after stopping active work.",
                },
                **WORKER_TAKEOVER_PROPERTIES,
            },
            "required": ["worker"],
        },
        "readOnlyHint": False,
    },
]

WORKER_TOOL_NAMES = {tool["name"] for tool in WORKER_TOOLS}
WORKER_NON_IDEMPOTENT_TOOLS = {"codex_worker_inbox", "codex_worker_start", "codex_worker_message", "codex_worker_integrate", "codex_worker_stop"}
WORKER_OPEN_WORLD_TOOLS = {"codex_worker_inbox", "codex_worker_start", "codex_worker_message"}
WORKER_DESTRUCTIVE_TOOLS = {"codex_worker_inbox", "codex_worker_integrate", "codex_worker_stop"}

WORKER_MODE_TOOLS = {
    "codex_get_config",
    "codex_tool_mode_info",
    "codex_tool_mode_switch",
    "codex_self_test",
    "codex_open_workspace",
    "codex_repo_tree",
    "codex_read_file",
    "codex_search_repo",
    "codex_load_context",
    "codex_list_skills",
    "codex_load_skill",
    "codex_git_status",
    "codex_git_diff",
    "codex_show_changes",
    *WORKER_TOOL_NAMES,
}


def install_worker_tool_surface(
    *,
    tools: list[Dict[str, Any]],
    tools_by_name: Dict[str, Dict[str, Any]],
    public_tool_names: set[str],
    tool_modes: Dict[str, set[str]],
    destructive_tools: set[str],
    open_world_tools: set[str],
    non_idempotent_tools: set[str],
    invocation_status: Dict[str, tuple[str, str]],
    output_schemas: Dict[str, Dict[str, Any]],
) -> None:
    """Install worker descriptors before public descriptors are materialized."""
    for descriptor in WORKER_TOOLS:
        name = descriptor["name"]
        if name not in tools_by_name:
            copied = deepcopy(descriptor)
            tools.append(copied)
            tools_by_name[name] = copied
            public_tool_names.add(name)

    tool_modes.setdefault("standard", set()).update(WORKER_TOOL_NAMES)
    tool_modes.setdefault("full", set()).update(WORKER_TOOL_NAMES)
    tool_modes["worker"] = set(WORKER_MODE_TOOLS)

    destructive_tools.update(WORKER_DESTRUCTIVE_TOOLS)
    open_world_tools.update(WORKER_OPEN_WORLD_TOOLS)
    non_idempotent_tools.update(WORKER_NON_IDEMPOTENT_TOOLS)

    invocation_status.update(
        {
            "codex_worker_start": ("Starting worker", "Worker started"),
            "codex_worker_options": ("Loading worker options", "Worker options ready"),
            "codex_worker_inbox": ("Updating worker inbox", "Worker inbox ready"),
            "codex_worker_message": ("Messaging worker", "Message delivered"),
            "codex_worker_list": ("Listing workers", "Workers ready"),
            "codex_worker_inspect": ("Checking worker", "Worker report ready"),
            "codex_worker_integrate": ("Integrating worker", "Worker result applied"),
            "codex_worker_stop": ("Stopping worker", "Worker stopped"),
        }
    )

    output_schemas.update(
        {
            "codex_worker_options": deepcopy(WORKER_OPTIONS_SCHEMA),
            "codex_worker_inbox": deepcopy(WORKER_INBOX_SCHEMA),
            "codex_worker_start": deepcopy(WORKER_VIEW_SCHEMA),
            "codex_worker_message": deepcopy(WORKER_VIEW_SCHEMA),
            "codex_worker_list": deepcopy(WORKER_LIST_SCHEMA),
            "codex_worker_inspect": deepcopy(WORKER_VIEW_SCHEMA),
            "codex_worker_integrate": deepcopy(WORKER_VIEW_SCHEMA),
            "codex_worker_stop": deepcopy(WORKER_VIEW_SCHEMA),
        }
    )
