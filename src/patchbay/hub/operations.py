"""Pure Hub V2 operation state and semantic-result contracts."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping


PUBLIC_STATUSES = ("ok", "pending", "partial", "blocked", "failed", "not_found")
TERMINAL_OPERATION_STATES = frozenset({"succeeded", "blocked", "failed", "cancelled"})
OPERATION_TRANSITIONS = {
    "created": frozenset({"payload_ready", "cancelled", "failed"}),
    "payload_ready": frozenset({"dispatchable", "cancelled", "failed"}),
    "dispatchable": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"reconciling", "outcome_unknown", *TERMINAL_OPERATION_STATES}),
    "outcome_unknown": frozenset({"reconciling"}),
    "reconciling": frozenset(TERMINAL_OPERATION_STATES),
    "succeeded": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TERMINAL_ATTEMPT_STATES = frozenset({"acknowledged", "retryable", "manual_recovery"})
ATTEMPT_TRANSITIONS = {
    "offered": frozenset({"claimed", "retryable"}),
    "claimed": frozenset({"executing", "lease_expired", "retryable"}),
    "executing": frozenset({"effect_recorded", "result_ready", "lease_expired", "manual_recovery"}),
    "effect_recorded": frozenset({"result_ready", "lease_expired", "manual_recovery"}),
    "result_ready": frozenset({"acknowledged"}),
    "lease_expired": frozenset({"reconciling"}),
    "reconciling": frozenset({"result_ready", "retryable", "manual_recovery"}),
    "acknowledged": frozenset(),
    "retryable": frozenset(),
    "manual_recovery": frozenset(),
}


def can_transition_operation(current: str, target: str) -> bool:
    return target in OPERATION_TRANSITIONS.get(current, frozenset())


def require_operation_transition(current: str, target: str) -> None:
    if not can_transition_operation(current, target):
        raise ValueError(f"Invalid operation transition: {current} -> {target}")


def can_transition_attempt(current: str, target: str) -> bool:
    return target in ATTEMPT_TRANSITIONS.get(current, frozenset())


def require_attempt_transition(current: str, target: str) -> None:
    if not can_transition_attempt(current, target):
        raise ValueError(f"Invalid attempt transition: {current} -> {target}")


def semantic_payload_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def idempotency_scope(*, principal_ref: str, tool_name: str, logical_target: str, key: str) -> str:
    if not key.strip():
        raise ValueError("idempotency_key is required for mutating Hub tools")
    return semantic_payload_hash(
        {
            "principal_ref": principal_ref,
            "tool_name": tool_name,
            "logical_target": logical_target,
            "key": key,
        }
    )


def public_envelope(
    status: str,
    *,
    result: Mapping[str, Any] | None = None,
    operation: Mapping[str, Any] | None = None,
    warnings: list[Any] | None = None,
    next_actions: list[Any] | None = None,
) -> dict[str, Any]:
    if status not in PUBLIC_STATUSES:
        raise ValueError(f"Invalid public Hub status: {status}")
    return {
        "status": status,
        "result": deepcopy(dict(result or {})),
        "operation": deepcopy(dict(operation or {})),
        "warnings": deepcopy(list(warnings or [])),
        "next_actions": deepcopy(list(next_actions or [])),
    }


def normalize_domain_result(
    value: Mapping[str, Any] | None,
    *,
    transport_error: str = "",
    pending_operation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(value or {})
    if transport_error:
        return public_envelope(
            "failed",
            result={"error": transport_error, "reason": "transport_error"},
            operation=pending_operation,
        )
    if not value and pending_operation:
        return public_envelope("pending", operation=pending_operation)
    if payload.get("found") is False:
        return public_envelope("not_found", result=payload, operation=pending_operation)
    if payload.get("partial") is True or str(payload.get("status") or "") == "partial":
        return public_envelope("partial", result=payload, operation=pending_operation)
    blocked_reason = _blocked_reason(payload)
    if blocked_reason:
        payload.setdefault("reason", blocked_reason)
        return public_envelope("blocked", result=payload, operation=pending_operation)
    if payload.get("failed") is True or str(payload.get("status") or "") in {"failed", "error"}:
        return public_envelope("failed", result=payload, operation=pending_operation)
    return public_envelope("ok", result=payload, operation=pending_operation)


def _blocked_reason(payload: Mapping[str, Any]) -> str:
    if payload.get("accepted") is False:
        return str(payload.get("reason") or payload.get("error") or "domain_refused")
    if payload.get("applied") is False:
        can_apply = payload.get("can_apply")
        conflict = str(payload.get("conflict_summary") or "").strip()
        integration_state = str(payload.get("integration_state") or "").strip()
        if can_apply is False or conflict or integration_state in {
            "blocked",
            "conflicted",
            "uncertain",
        }:
            return str(payload.get("reason") or conflict or "integration_blocked")
    if payload.get("stop_confirmation_required") or payload.get("force_required"):
        return "confirmation_required"
    status = str(payload.get("status") or "")
    if status in {"blocked", "refused", "repo_busy", "capacity_blocked", "needs_confirmation"}:
        return status
    return ""
