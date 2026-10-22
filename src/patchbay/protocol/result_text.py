"""Small deterministic decision metadata; bulk evidence remains structured."""
from __future__ import annotations

import json
from collections.abc import Mapping

FIELDS = (
    "worker_id", "name", "worker", "task_id", "work_group_id", "operation_id", "request_operation_id",
    "state", "status", "accepted", "request_replayed", "request_ref", "applied", "blocked", "reason", "conflict_summary", "error",
    "stopped", "stop_confirmation_required", "discard_confirmation_required", "workspace_cleaned",
    "cleanup_pending", "cleanup_unresolved", "cleanup_recovery_required", "integration_state", "apply_disposition",
    "preview_token_id", "patch_sha256", "candidate_hash", "base_revision", "worker_base_revision", "revision",
    "commit", "tree", "file_path", "path", "source", "sha256", "start_line", "end_line", "total_lines",
    "truncated", "partial", "has_more", "next_start_line", "next_offset", "next_cursor", "cursor",
    "patch_truncated", "context_truncated", "result_partial", "error_partial", "operations_partial",
    "active_operations_partial", "changed", "poll_too_early", "status_current", "seconds_since_last_poll",
    "recommended_next_poll_seconds", "retry_after_seconds", "suggested_action", "required_action",
    "final_response_allowed", "manager_must_continue", "work_remaining", "projection_revision",
    "operation_count", "active_operation_count", "uncertain_operation_count", "as_of", "last_activity_at",
    "consistent_snapshot", "fresh", "age_seconds",
)


def decision_metadata(payload: Mapping) -> dict:
    """Keep outcome flags even when prose is identical or arbitrarily long.

    Oversized scalar values are omitted explicitly, never turned into a usable
    truncated identity/cursor. The original value stays in structuredContent.
    """
    result, omitted = {}, []
    for key in FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or type(value) in {bool, int, float, str}:
            if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= 128:
                result[key] = value
            else:
                omitted.append(key)
        elif key == "error" and isinstance(value, Mapping):
            result[key] = {k: v for k in ("code", "message") if isinstance((v := value.get(k)), str) and len(v.encode("utf-8")) <= 128}
    if omitted:
        result["summary_omitted_fields"] = omitted
    return result


def decision_text(payload: Mapping) -> str:
    return json.dumps(decision_metadata(payload), ensure_ascii=False, separators=(",", ":"))
