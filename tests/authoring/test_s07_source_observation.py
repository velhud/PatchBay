"""PROPOSED DESIRED-BEHAVIOR REGRESSIONS; NOT RUN BY THIS AUDIT.
Scope: receipt classification only; no source/process/database is created.
Acceptance references: S07-T13 and T17. A full observation fixture is T16.
"""
from __future__ import annotations

from types import SimpleNamespace

from patchbay.authoring.execution.receipts import classify, new_receipt


def _receipt() -> dict:
    store = SimpleNamespace(identity={"instance_id": "fixture-instance"})
    run = {
        "id": "fixture-operation",
        "task_id": "fixture-task",
        "revision": 1,
        "commit_oid": "a" * 40,
        "tree_oid": "b" * 40,
        "profile_digest": "profile-fixture",
    }
    return new_receipt(store, run, "policy-fixture")


def test_pre_observation_environment_failure_cannot_assert_unchanged() -> None:
    receipt = _receipt()
    receipt["cleanup"] = "proven"
    receipt["error"] = {
        "code": "BLOCKED_ENVIRONMENT",
        "message": "Fixture failed before source materialization.",
    }
    classify(receipt, {"unit"})
    assert receipt["classification"] == "BLOCKED_ENVIRONMENT"
    assert receipt["verification_receipt"]["source_unchanged"] is not True


def test_observed_source_change_remains_negative_evidence() -> None:
    receipt = _receipt()
    receipt["cleanup"] = "proven"
    receipt["source_changes_during_checks"] = [
        {"path": "app.py", "reason": "tracked_source_changed"}
    ]
    classify(receipt, {"unit"})
    assert receipt["classification"] == "CHECKS_INCOMPLETE"
    assert receipt["verification_receipt"]["source_unchanged"] is False
