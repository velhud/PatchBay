"""Bounded recovery and useful structured-profile text summaries."""
from __future__ import annotations

import json
import pytest

from patchbay.authoring.interfaces.mcp import MCP
from patchbay.authoring.util import fingerprint


def _call(protocol, name, arguments):
    response = protocol.handle("reader", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})["result"]
    return response


def test_recovery_defaults_to_twenty_but_keeps_old_uncertainty_and_paged_history(setup_author):
    service, _, _, task, _ = setup_author
    protocol = MCP(service)

    uncertain, _ = service.store.begin_operation(kind="fixture.history", task_id=task["id"],
        key="old-uncertain", fingerprint=fingerprint({"fixture": "old-uncertain"}), payload={})
    with service.store.transaction() as conn:
        conn.execute("UPDATE operations SET state='unknown_execution',phase='ownership_unresolved',error=? WHERE id=?",
                     (json.dumps({"code": "EXECUTION_UNKNOWN", "message": "Keep this effect fenced"}), uncertain["id"]))

    for index in range(45):
        operation, _ = service.store.begin_operation(kind="fixture.history", task_id=task["id"],
            key=f"history-{index:03d}", fingerprint=fingerprint({"fixture": index}), payload={})
        service.store.finish(operation["id"], "completed", result={"ordinal": index})

    before = service.store.one("SELECT COUNT(*) AS n FROM events")["n"]
    recovered = _call(protocol, "author_recover_task", {"task_id": task["id"]})
    assert not recovered["isError"]
    capsule = recovered["structuredContent"]
    assert len(capsule["operations"]) == 20
    assert capsule["operations_partial"] and capsule["operation_count"] > 20
    assert capsule["active_operation_count"] == 1
    assert [op["id"] for op in capsule["active_operations"]] == [uncertain["id"]]
    assert capsule["active_operations"][0]["state"] == "unknown_execution"

    found = False
    offset, cursor = 0, None
    while True:
        page_response = _call(protocol, "author_recent_operations", {
            "task_id": task["id"], "limit": 20, "offset": offset, "cursor": cursor,
        })
        assert not page_response["isError"]
        page = page_response["structuredContent"]
        found |= any(op["id"] == uncertain["id"] for op in page["operations"])
        if page["next_offset"] is None:
            break
        offset, cursor = page["next_offset"], page["cursor"]
    assert found
    assert service.store.one("SELECT state FROM operations WHERE id=?", (uncertain["id"],))["state"] == "unknown_execution"
    assert service.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


def test_recovery_operation_limit_is_strict_and_bounded_without_side_effects(setup_author):
    service, _, _, task, _ = setup_author
    protocol = MCP(service)
    before = service.store.one("SELECT COUNT(*) AS n FROM events")["n"]

    for value in (0, True, 201):
        response = _call(protocol, "author_recover_task", {"task_id": task["id"], "operation_limit": value})
        assert response["isError"]
        assert response["structuredContent"]["code"] == "INVALID_REQUEST"

    response = _call(protocol, "author_recover_task", {"task_id": task["id"], "operation_limit": 1})
    assert not response["isError"]
    assert len(response["structuredContent"]["operations"]) <= 1
    assert service.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


def test_compatibility_profile_remains_the_default_and_keeps_full_text(setup_author):
    service, _, _, task, _ = setup_author
    protocol = MCP(service)
    assert protocol.output_profile == "compat-full-v1"
    response = _call(protocol, "author_get_feedback", {"task_id": task["id"]})
    assert not response["isError"]
    assert json.loads(response["content"][0]["text"]) == response["structuredContent"]


def test_author_guidance_is_initialized_without_reinserting_it_in_tool_results(setup_author):
    service, _, _, task, _ = setup_author
    protocol = MCP(service)
    initialized = protocol.handle("reader", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    instructions = initialized["result"]["instructions"]
    assert "This connection is read-only" in instructions
    for _ in range(2):
        response = _call(protocol, "author_recover_task", {"task_id": task["id"]})
        assert not response["isError"]
        assert "instructions" not in response
        assert instructions not in json.dumps(response)


def test_structured_summary_keeps_source_structured_and_surfaces_read_identity(setup_author):
    service, _, _, task, _ = setup_author
    protocol = MCP(service, output_profile="structured-summary-v2")
    context = service.snapshots.export(task["id"], key="summary-source-context")
    expected = service.snapshots.read(context["id"], "app.py")
    before = service.store.one("SELECT COUNT(*) AS n FROM events")["n"]

    response = _call(protocol, "author_read_file", {"context_id": context["id"], "path": "app.py"})
    assert not response["isError"]
    text = json.loads(response["content"][0]["text"])
    assert text["tool"] == "author_read_file"
    assert text["context_id"] == context["id"] and text["path"] == "app.py"
    assert text["commit"] == expected["commit"] and text["sha256"] == expected["sha256"]
    assert text["start_line"] == expected["start_line"] and text["total_lines"] == expected["total_lines"]
    assert "text" not in text
    assert response["structuredContent"] == expected
    assert service.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


def test_structured_summary_surfaces_bounded_error_and_operation_progress(setup_author):
    service, _, _, task, _ = setup_author
    operation, _ = service.store.begin_operation(kind="fixture.failure", task_id=task["id"], key="failed-op",
        fingerprint=fingerprint({"fixture": "failed"}), payload={})
    message = "actionable failure λ " * 60
    service.store.finish(operation["id"], "failed", error={"code": "CHECK_FAILED", "message": message,
        "detail": "full details remain in structured content"})
    protocol = MCP(service, output_profile="structured-summary-v2")

    response = _call(protocol, "author_operation_status", {"operation_id": operation["id"]})
    assert not response["isError"]
    text = json.loads(response["content"][0]["text"])
    assert text["id"] == operation["id"] and text["state"] == "failed" and text["phase"] == "terminal"
    assert text["error_summary"]["code"] == "CHECK_FAILED"
    assert text["error_summary"]["message"].encode("utf-8")
    assert len(text["error_summary"]["message"].encode("utf-8")) <= 512
    assert "error.message" in text["summary_truncated_fields"]
    assert "detail" not in text["error_summary"]
    assert response["structuredContent"]["error"]["message"] == message
    assert response["structuredContent"]["error"]["detail"] == "full details remain in structured content"


def test_structured_recovery_summary_points_to_paged_large_operation_details(setup_author):
    service, _, _, task, _ = setup_author
    operation, _ = service.store.begin_operation(kind="fixture.large-history", task_id=task["id"], key="large-history-error",
        fingerprint=fingerprint({"fixture": "large-history-error"}), payload={})
    service.store.finish(operation["id"], "failed", error={"code": "CHECK_FAILED", "message": "bounded summary",
        "detail": "diagnostic " * 1200})
    before = service.store.one("SELECT COUNT(*) AS n FROM events")["n"]
    protocol = MCP(service, output_profile="structured-summary-v2")

    response = _call(protocol, "author_recover_task", {"task_id": task["id"]})
    assert not response["isError"]
    text = json.loads(response["content"][0]["text"])
    details = next(ref for ref in text["operation_detail_reads"] if ref["operation_id"] == operation["id"])
    assert details["error_partial"] and details["error_bytes"] > 8192
    assert details["read_tool"] == "author_operation_status"
    exact = next(row for row in response["structuredContent"]["operations"] if row["id"] == operation["id"])
    assert exact["error_partial"] and exact["error_bytes"] == details["error_bytes"]
    assert exact["detail_read"] == {"tool": "author_operation_status", "arguments": {"operation_id": operation["id"]}}
    assert service.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


@pytest.mark.parametrize("uncertain_state", ["unknown_execution", "publication_unknown"])
def test_recovery_prioritizes_uncertainty_beyond_the_active_projection_cap(setup_author, uncertain_state):
    service, _, _, task, _ = setup_author
    for index in range(205):
        service.store.begin_operation(kind="fixture.queued", task_id=task["id"],
            key=f"active-backlog-{index}", fingerprint=fingerprint({"queued": index}), payload={})
    uncertain, _ = service.store.begin_operation(kind="fixture.uncertain", task_id=task["id"],
        key="late-uncertainty", fingerprint=fingerprint({"uncertain": uncertain_state}), payload={})
    with service.store.transaction() as conn:
        conn.execute("UPDATE operations SET state=?,created=4102444800.0 WHERE id=?",
                     (uncertain_state, uncertain["id"]))
    before = service.store.one("SELECT COUNT(*) AS n FROM events")["n"]
    response = _call(MCP(service), "author_recover_task", {"task_id": task["id"], "operation_limit": 1})
    assert not response["isError"]
    recovery = response["structuredContent"]
    assert recovery["state"] == "awaiting_controller"
    assert "Reconcile the existing uncertain operation" in recovery["required_judgment"]
    assert recovery["active_operations_partial"] and recovery["active_operation_count"] == 206
    assert len(recovery["active_operations"]) == 200
    assert any(row["id"] == uncertain["id"] for row in recovery["active_operations"])
    assert service.store.one("SELECT state FROM operations WHERE id=?", (uncertain["id"],))["state"] == uncertain_state
    assert service.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before


@pytest.mark.parametrize(("check_state", "task_state"), [("queued", "awaiting_resources"), ("running", "executing")])
def test_recovery_classifies_checks_outside_the_active_projection(setup_author, check_state, task_state):
    service, _, _, task, _ = setup_author
    for index in range(205):
        service.store.begin_operation(kind="fixture.queued", task_id=task["id"],
            key=f"active-backlog-{index}", fingerprint=fingerprint({"queued": index}), payload={})
    check, _ = service.store.begin_operation(kind="checks.run", task_id=task["id"], key="late-check",
        fingerprint=fingerprint({"check": check_state}), payload={})
    with service.store.transaction() as conn:
        conn.execute("UPDATE operations SET state=?,created=4102444800.0 WHERE id=?", (check_state, check["id"]))
    before = service.store.one("SELECT COUNT(*) AS n FROM events")["n"]
    recovery = service.recovery.get(task["id"], operation_limit=1)
    assert recovery["state"] == task_state
    assert recovery["active_operations_partial"]
    assert any(row["id"] == check["id"] for row in recovery["active_operations"])
    assert service.store.one("SELECT COUNT(*) AS n FROM events")["n"] == before
