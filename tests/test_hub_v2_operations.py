from __future__ import annotations

import pytest

from patchbay.hub.operations import (
    PUBLIC_STATUSES,
    can_transition_attempt,
    can_transition_operation,
    idempotency_scope,
    normalize_domain_result,
    public_envelope,
    require_attempt_transition,
    require_operation_transition,
    semantic_payload_hash,
)


def test_unknown_outcome_reconciles_before_terminal_result() -> None:
    assert can_transition_operation("running", "outcome_unknown")
    assert can_transition_operation("outcome_unknown", "reconciling")
    assert not can_transition_operation("outcome_unknown", "succeeded")
    assert can_transition_operation("reconciling", "succeeded")


def test_terminal_operation_state_cannot_be_overwritten() -> None:
    assert not can_transition_operation("succeeded", "failed")
    with pytest.raises(ValueError):
        require_operation_transition("succeeded", "failed")


def test_attempt_lease_expiry_requires_reconciliation() -> None:
    assert can_transition_attempt("executing", "lease_expired")
    assert can_transition_attempt("lease_expired", "reconciling")
    assert not can_transition_attempt("lease_expired", "retryable")
    with pytest.raises(ValueError):
        require_attempt_transition("lease_expired", "retryable")


def test_semantic_payload_hash_ignores_mapping_order() -> None:
    assert semantic_payload_hash({"b": 2, "a": 1}) == semantic_payload_hash({"a": 1, "b": 2})


def test_idempotency_scope_requires_caller_key_and_binds_target() -> None:
    first = idempotency_scope(principal_ref="p", tool_name="start", logical_target="group-a", key="retry-1")
    second = idempotency_scope(principal_ref="p", tool_name="start", logical_target="group-b", key="retry-1")
    assert first != second
    with pytest.raises(ValueError):
        idempotency_scope(principal_ref="p", tool_name="start", logical_target="g", key="")


def test_public_envelope_has_one_canonical_shape() -> None:
    envelope = public_envelope("pending", operation={"operation_id": "op_1"})
    assert tuple(envelope) == ("status", "result", "operation", "warnings", "next_actions")
    assert set(PUBLIC_STATUSES) == {"ok", "pending", "partial", "blocked", "failed", "not_found"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"accepted": False, "error": "repo busy"}, "blocked"),
        ({"stop_confirmation_required": True}, "blocked"),
        ({"status": "refused"}, "blocked"),
        ({"found": False}, "not_found"),
        ({"partial": True, "items": []}, "partial"),
        ({"failed": True}, "failed"),
        ({"accepted": True, "worker_id": "w"}, "ok"),
        ({"applied": False, "can_apply": True, "preview_token": "pit2.x"}, "ok"),
        ({"applied": False, "can_apply": False}, "blocked"),
    ],
)
def test_domain_result_normalization(payload: dict[str, object], expected: str) -> None:
    assert normalize_domain_result(payload)["status"] == expected


def test_no_result_with_operation_is_pending() -> None:
    result = normalize_domain_result(None, pending_operation={"operation_id": "op_1"})
    assert result["status"] == "pending"


def test_transport_error_is_failed_not_domain_blocked() -> None:
    result = normalize_domain_result(None, transport_error="connection lost")
    assert result["status"] == "failed"
    assert result["result"]["reason"] == "transport_error"
