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
        "chatgpt_session_ref": {"type": "string"},
        "work_run_ref": {"type": "string"},
        "work_run_started_at": {"type": "number"},
        "work_run_last_activity_at": {"type": "number"},
        "workspace_mode": {"type": "string"},
        "workspace_available": {"type": "boolean"},
        "workspace_location": {"type": "string"},
        "state": {"type": "string"},
        "view": {"type": "string"},
        "report": {"type": "string"},
        "status_line": {"type": "string"},
        "compact_status": {"type": "object", "additionalProperties": True},
        "activity_since_last_check": {"type": "object", "additionalProperties": True},
        "liveness": {"type": "object", "additionalProperties": True},
        "latest_partial_note": {"type": "object", "additionalProperties": True},
        "latest_checkpoints": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "checkpoint_count": {"type": "integer"},
        "report_artifacts": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "worker_report_files_note": {"type": "string"},
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
        "can_message_now": {"type": "boolean"},
        "can_queue_message": {"type": "boolean"},
        "queued_message_count": {"type": "integer"},
        "can_message_reason": {"type": "string"},
        "followup_mode": {"type": "string"},
        "active_steering_supported": {"type": "boolean"},
        "last_activity_at": {"type": "number"},
        "accepted": {"type": "boolean"},
        "stopped": {"type": "boolean"},
        "stop_confirmation_required": {"type": "boolean"},
        "force_required": {"type": "boolean"},
        "force_parameter": {"type": "string"},
        "force_value": {"type": "boolean"},
        "stop_confirmation_grace_seconds": {"type": "integer"},
        "active_turn_elapsed_seconds": {"type": ["integer", "null"]},
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
        "accepted_dirty_base": {"type": "array", "items": {"type": "string"}},
        "accepted_dirty_base_files": {"type": "array", "items": {"type": "string"}},
        "unexpected_base_changed_files": {"type": "array", "items": {"type": "string"}},
        "modified_included_untracked_base_files": {"type": "array", "items": {"type": "string"}},
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
        "ownership_scope": {"type": "string"},
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
        "scope": {"type": "object", "additionalProperties": True},
        "hidden_workers": {"type": "object", "additionalProperties": True},
        "team_status": {"type": "object", "additionalProperties": True},
        "team_report": {"type": "string"},
    },
}

WORKER_STATUS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "summary": {"type": "string"},
        "since_last_check": {"type": "object", "additionalProperties": True},
        "since_last_check_line": {"type": "string"},
        "suggested_action": {"type": "string"},
        "worker_lines": {"type": "array", "items": {"type": "string"}},
        "counts": {"type": "object", "additionalProperties": True},
        "minimum_next_poll_seconds": {"type": "integer"},
        "recommended_next_poll_seconds": {"type": "integer"},
        "poll_guidance": {"type": "string"},
        "poll_too_early": {"type": "boolean"},
        "status_current": {"type": "boolean"},
        "seconds_since_last_poll": {"type": ["integer", "null"]},
        "retry_after_seconds": {"type": "integer"},
        "waited_seconds": {"type": "integer"},
        "requested_wait_seconds": {"type": "integer"},
        "minimum_wait_seconds_applied": {"type": "integer"},
        "wait_cap_seconds": {"type": "integer"},
        "wait_guidance": {"type": "string"},
        "scope": {"type": "object", "additionalProperties": True},
        "hidden_workers": {"type": "object", "additionalProperties": True},
        "workers": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "count": {"type": "integer"},
        "active": {"type": "integer"},
        "active_turns": {"type": "integer"},
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
        "model_selection_guidance": {"type": "object", "additionalProperties": True},
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
        "ownership_scope": {"type": "string"},
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
            "model metadata from the installed Codex runtime/catalog, returns advisory model-selection guidance "
            "for Spark, GPT-5.4 Mini, GPT-5.4, and GPT-5.5, and explains which fields to pass to "
            "codex_worker_start or codex_worker_message. repo_path is accepted as a harmless compatibility field "
            "and ignored because this is a runtime/model menu, not a repository operation. The guidance is a judgment "
            "aid, not a hard router."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional compatibility field accepted and ignored; worker options are global runtime metadata.",
                },
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
            "Appoint a durable named Codex colleague for autonomous investigation, architecture analysis, planning, implementation, review, "
            "or verification. Use this as the normal path for non-trivial repository work so ChatGPT acts as "
            "manager instead of primary file reader, routine code reviewer, or line-by-line coder. Delegation is expected: when work can "
            "be split cleanly, start a small team of workers rather than one shallow worker or a long manual "
            "read/search loop. Give goals, context, constraints, deliverables, and expected report "
            "in the natural-language brief; let the worker find relevant files unless exact paths matter. "
            "For consequential audits or implementation, ask the worker to write a durable report file or "
            "changed-file evidence in its worker workspace when the workspace is writable; read-only workers still "
            "produce structured reports and live checkpoints through PatchBay. "
            "Defaults to an isolated writing worktree; choose workspace_mode=read_only for advisory work. "
            "For larger tasks, start multiple workers with separate responsibilities and reconcile their reports; "
            "up to 10 concurrent worker slots may be available depending on server config. "
            "Can include bounded context from other workers for review, alternatives, or handoff. When a "
            "specific model or reasoning depth matters, call codex_worker_options first, then pass model and/or "
            "reasoning_effort here. Worker names are scoped to the target workspace, so the same name can be reused "
            "safely in another repo. If rerunning a phase with the same name, pass auto_suffix=true. For isolated "
            "workers that need accepted untracked phase artifacts from the base checkout, pass explicit "
            "include_untracked_from_base glob patterns."
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
                "auto_suffix": {
                    "type": "boolean",
                    "description": "When true, append a short timestamp suffix if this worker name already exists in the same workspace.",
                },
                "include_untracked_from_base": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional workspace-relative glob patterns for selected untracked base-checkout files to copy into "
                        "a new isolated_write worker worktree, for example dev/big_update/00-*.md. This is for accepted "
                        "phase artifacts that are not committed yet; blocked/secret-like paths are not copied."
                    ),
                },
                "context_from_workers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional worker names or ids whose reports/changes/diffs should be included as "
                        "natural-language peer context for this worker turn. Up to 10 workers can be attached; "
                        "for more, split into batches or start a synthesis worker first."
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
                    "enum": ["report", "changes", "diff", "review"],
                    "description": (
                        "How much peer-worker context to include. Default: report. Use changes for changed-file inventory, "
                        "diff for bounded patch context, and review for review-grade report+changes+diff context with explicit "
                        "visibility notes."
                    ),
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
            "or revision of the same worker's task, not for a new independent colleague. Trust worker reports as competent employee reports by default; when uncertain, ask the worker "
            "to investigate, compare, verify, or adjust over ChatGPT doing a manual file-by-file implementation loop. "
            "A manager should keep talking to the worker until the evidence is usable instead of replacing worker "
            "conversation with direct reads. Use follow-up "
            "messages when a report is thin, contradictory, missing evidence, lacks a durable report file, or needs "
            "another worker's findings. Can include bounded peer report/changes/diff/review context without exposing backend ids. "
            "If the worker is still running, inspect view=status and latest_checkpoints instead of cancelling it only "
            "because the final report is not ready; active-turn steering is not yet exposed, so this tool continues "
            "the next turn after completion. "
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
                        "natural-language peer context for this worker turn. Up to 10 workers can be attached; "
                        "for more, split into batches or start a synthesis worker first."
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
                    "enum": ["report", "changes", "diff", "review"],
                    "description": (
                        "How much peer-worker context to include. Default: report. Use review when this continuation "
                        "should evaluate another worker's report, changed-file list, and bounded diff."
                    ),
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
            "state, compact liveness/status lines, activity deltas since the last status check, latest report/checkpoints, team summary, and whether each worker can receive a follow-up. Use this to manage "
            "a worker team, after restart, or before choosing which worker to inspect, message, stop, or integrate. "
            "For long-running teams, read team_status/status_line first: active or quiet workers with recent activity are not failed just because no final report is ready. "
            "Respect the returned polling guidance: normal worker monitoring means waiting about 20-30 seconds before the next status check, not polling every few seconds. "
            "By default, scope=current hides old completed/stopped historical workers and shows only the current work run plus live/problem workers; the response says how many historical workers were hidden to reduce historical worker clutter. "
            "Use scope=conversation to intentionally reuse workers from the same ChatGPT conversation, scope=recent for recently active workers, or scope=history when you deliberately need the durable archive. "
            "Use active_only, owned_only, include_stopped=false, or created_after for additional narrowing during a specific task. "
            "If the team report shows thin, failed, stale, or conflicting work, continue the relevant named worker instead "
            "of treating first reports as final. If repo_path is omitted, list workers across all allowed repositories; "
            "pass repo_path or a worker_id when a human name exists in more than one repo."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to filter workers. Omit it to list workers across all allowed repositories.",
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
                "scope": {
                    "type": "string",
                    "enum": ["current", "conversation", "recent", "history", "all"],
                    "description": "Visibility scope. Default current: current work run plus live/problem workers, with historical completed/stopped workers hidden. conversation shows this ChatGPT conversation when available; recent shows recently active workers; history/all shows every durable worker.",
                },
            },
            "required": [],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_worker_status",
        "description": (
            "Return the compact pull-based worker team status bar. Use this while workers are running to see "
            "active/quiet/stale/lost/completed/failed counts, deltas since the last check, and one short line per "
            "worker without raw logs or long reports. This is the default liveness check before stopping a worker: "
            "if events/output/partial notes are changing, wait; if a worker is stale or lost, inspect it deliberately. "
            "For normal monitoring, wait about 20-30 seconds between status calls and follow "
            "recommended_next_poll_seconds in the result; do not poll every few seconds unless the user explicitly "
            "asked for near-real-time monitoring or the last result needs immediate recovery. Default scope=current hides old historical completed/stopped workers, reports how many are hidden, and keeps current-run plus live/problem workers visible. Use scope=conversation to reuse earlier workers from the same ChatGPT conversation, or scope=history only when you deliberately need the archive. If repo_path is omitted, "
            "status covers workers across all allowed repositories so a manager does not miss active work in another repo."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to filter workers. Omit it to see workers across all allowed repositories.",
                },
                "active_only": {
                    "type": "boolean",
                    "description": "When true, return only workers whose latest turn is starting or working.",
                },
                "include_stopped": {
                    "type": "boolean",
                    "description": "When true, include stopped/cancelled workers. Default: false for compact status.",
                },
                "owned_only": {
                    "type": "boolean",
                    "description": "When true, return only workers owned by the current coordination owner.",
                },
                "created_after": {
                    "type": "number",
                    "description": "Optional Unix timestamp; return workers first created at or after this time.",
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": "When true, bypass the soft polling cooldown for deliberate recovery or user-requested near-real-time monitoring. Default: false.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["current", "conversation", "recent", "history", "all"],
                    "description": "Visibility scope. Default current: current work run plus live/problem workers, with historical completed/stopped workers hidden. conversation shows this ChatGPT conversation when available; recent shows recently active workers; history/all shows every durable worker.",
                },
            },
            "required": [],
        },
        "readOnlyHint": True,
    },
    {
        "name": "codex_worker_wait",
        "description": (
            "Wait once, then return a fresh compact worker team status. Use this instead of repeatedly calling "
            "codex_worker_status every few seconds while workers are active or quiet. The normal manager pattern "
            "is: assign workers, wait about 20-30 seconds, read compact status, then either wait again, ask a "
            "worker a natural-language follow-up after its turn completes, or inspect only when there is a real "
            "concern. If wait_seconds is lower than the configured minimum cadence, PatchBay raises it to that minimum. "
            "Default scope=current hides old completed/stopped historical workers while keeping current-run and live/problem workers visible; use scope=conversation or scope=history only when intentionally reusing older workers. If repo_path is omitted, status covers workers across all allowed repositories. This tool does not expose "
            "raw logs and does not interrupt workers."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional authorized repository path used to filter workers. Omit it to wait on workers across all allowed repositories.",
                },
                "active_only": {
                    "type": "boolean",
                    "description": "When true, return only workers whose latest turn is starting or working.",
                },
                "include_stopped": {
                    "type": "boolean",
                    "description": "When true, include stopped/cancelled workers. Default: false.",
                },
                "owned_only": {
                    "type": "boolean",
                    "description": "When true, return only workers owned by the current coordination owner.",
                },
                "created_after": {
                    "type": "number",
                    "description": "Optional Unix timestamp; return workers first created at or after this time.",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "Seconds to wait before refreshing status. Default follows recommended_next_poll_seconds; values below the configured minimum monitoring cadence are raised to that minimum, and values are capped by server policy.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["current", "conversation", "recent", "history", "all"],
                    "description": "Visibility scope. Default current: current work run plus live/problem workers, with historical completed/stopped workers hidden. conversation shows this ChatGPT conversation when available; recent shows recently active workers; history/all shows every durable worker.",
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
            "For running workers, use view=compact, view=status, or codex_worker_status to check active/quiet/stale/lost status, activity deltas, phase, latest_checkpoints, and latest partial note before "
            "assuming the worker is stuck. Use the report as the normal management signal. Managerial review means reading the report and asking follow-up questions, not routinely opening every changed file or diff; question the worker again with codex_worker_message "
            "when evidence is missing, output is too compressed, or another worker disagrees. view=report omits low-level lifecycle diagnostics; use view=diagnostics only for deliberate debugging of process/session/command state. Use view=changes, view=diff with file_path, view=file with file_path, or view=integration_preview only when there is a concrete escalation or integration need. codex_read_file reads the "
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
                    "enum": ["report", "compact", "status", "diagnostics", "changes", "diff", "file", "integration_preview"],
                    "description": "Report by default. Use compact for a tiny liveness snapshot, status for liveness and latest-turn diagnostics without the full answer, diagnostics for the full lifecycle payload, changes for file inventory, diff with file_path, file with file_path for worker-created file content, or integration_preview before accepting a worker result.",
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
                "accepted_dirty_base": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "For view=integration_preview, workspace-relative glob patterns for known accepted dirty "
                        "base-checkout files, such as previous phase docs, that should not block preview."
                    ),
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
                "accepted_dirty_base": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Workspace-relative glob patterns for known accepted dirty base-checkout files that may coexist "
                        "with this integration. Unexpected dirty files still block unless allow_dirty_base=true."
                    ),
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
            "preserved so ChatGPT can continue the colleague later, and PatchBay preserves any partial checkpoints "
            "or partial report it captured before cancellation. Stop is an escalation, not a liveness probe; inspect "
            "view=status first when the worker has recent heartbeat or checkpoints. If the worker still looks live or "
            "has been active for less than the configured confirmation grace window, the first stop call returns "
            "stop_confirmation_required instead of cancelling; wait longer or call again with force=true after a "
            "deliberate manager decision. Set cleanup_workspace=true only when the "
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
                "force": {
                    "type": "boolean",
                    "description": "When true, confirm that ChatGPT deliberately wants to interrupt a worker that still looks live or is inside the early-stop grace window.",
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
    "codex_list_workspaces",
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
            "codex_worker_status": ("Checking worker status", "Worker status ready"),
            "codex_worker_wait": ("Waiting on workers", "Worker status ready"),
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
            "codex_worker_status": deepcopy(WORKER_STATUS_SCHEMA),
            "codex_worker_wait": deepcopy(WORKER_STATUS_SCHEMA),
            "codex_worker_inspect": deepcopy(WORKER_VIEW_SCHEMA),
            "codex_worker_integrate": deepcopy(WORKER_VIEW_SCHEMA),
            "codex_worker_stop": deepcopy(WORKER_VIEW_SCHEMA),
        }
    )
