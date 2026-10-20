from __future__ import annotations

from patchbay.hub.runtime_v2 import FLEET_WORKER_ENTITY
from patchbay.hub.tool_surface import HUB_V2_TOOLS_BY_NAME
from test_hub_runtime_v2 import (
    context,
    create_group,
    enroll_online,
    heartbeat_workers,
    make_runtime,
)


def test_reports_and_changes_reassignment_returns_exact_predecessor_read_actions(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    old_edge = enroll_online(runtime, machine_id="machine_old")
    enroll_online(runtime, machine_id="machine_new")
    caller = context("retrieval-contract")
    created = create_group(runtime, caller=caller, machine_id="machine_old")
    predecessor = created["result"]["work_group"]
    heartbeat_workers(
        runtime,
        old_edge,
        2,
        [
            {
                "edge_worker_id": "worker-old",
                "name": "Old Worker",
                "work_group_id": predecessor["work_group_id"],
                "lane_id": "implementation",
                "worker_state": "available",
                "turn_state": "completed",
                "liveness": "terminal",
                "integration_state": "not_integrated",
                "review_disposition": "unreviewed",
                "content_revision": "sha256:" + "a" * 64,
                "content_sha256": "a" * 64,
                "base_revision": "base-commit",
                "worker_base_revision": "worker-base-commit",
            }
        ],
    )
    worker_ref = store.list_entities(FLEET_WORKER_ENTITY)[0]["entity_id"]

    result = runtime.reassign_work_group(
        work_group_id=predecessor["work_group_id"],
        machine_id="machine_new",
        reason="The predecessor machine is being replaced.",
        carry_context="reports_and_changes",
        idempotency_key="retrieval-contract-reassign",
        context=caller,
    )
    handoff = result["result"]["work_group"]["handoff_context"]
    retrieval = handoff["required_changes_retrieval"]

    assert handoff["context_mode"] == "reports_and_changes"
    assert handoff["predecessor_work_group_id"] == predecessor["work_group_id"]
    assert handoff["predecessor_machine_id"] == "machine_old"
    assert handoff["predecessor_edge_generation"] == predecessor["pinned_edge_generation"]
    assert handoff["artifacts_transferred"] is False
    assert handoff["changes_transferred"] is False
    assert retrieval["content_transferred"] is False
    assert retrieval["predecessor_work_group_id"] == predecessor["work_group_id"]
    actions = {action["purpose"].split(";")[0]: action for action in retrieval["actions"]}
    enumerate_action = actions["enumerate all predecessor workers"]
    assert enumerate_action["tool_name"] == "patchbay_worker_list"
    assert enumerate_action["arguments"] == {"work_group_id": predecessor["work_group_id"], "limit": 50}
    report_action = actions["read one predecessor worker report"]
    changes_action = actions["read the predecessor worker changed-file inventory"]
    for action, view in ((report_action, "report"), (changes_action, "changes")):
        assert action["tool_name"] == "patchbay_worker_inspect"
        assert action["arguments_template"] == {
            "work_group_id": predecessor["work_group_id"],
            "fleet_worker_ref": "exact fleet_worker_ref from predecessor worker list",
            "view": view,
        }
    file_action = actions["read exact changed-file content in bounded line pages"]
    assert file_action["arguments_template"]["start_line"] == 1
    assert "next_start_line" in file_action["page_rule"]
    assert "sha256" in file_action["page_rule"]

    inspect = HUB_V2_TOOLS_BY_NAME["patchbay_worker_inspect"]["inputSchema"]
    list_schema = HUB_V2_TOOLS_BY_NAME["patchbay_worker_list"]["inputSchema"]
    assert {"work_group_id", "fleet_worker_ref"} <= set(inspect["properties"])
    assert {"work_group_id", "limit", "cursor"} <= set(list_schema["properties"])
    assert worker_ref == handoff["reports"][0]["fleet_worker_ref"]
    assert handoff["reports"][0]["content_sha256"] == "a" * 64
    assert handoff["reports"][0]["content_revision"] == "sha256:" + "a" * 64
    assert handoff["reports"][0]["base_revision"] == "base-commit"
    assert handoff["reports"][0]["worker_base_revision"] == "worker-base-commit"


def test_other_handoff_modes_do_not_claim_retrieval_or_transfer(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    old_edge = enroll_online(runtime, machine_id="machine_old")
    enroll_online(runtime, machine_id="machine_new")
    caller = context("reports-only-contract")
    created = create_group(runtime, caller=caller, machine_id="machine_old")
    predecessor = created["result"]["work_group"]
    heartbeat_workers(
        runtime,
        old_edge,
        2,
        [
            {
                "edge_worker_id": "worker-old",
                "name": "Old Worker",
                "work_group_id": predecessor["work_group_id"],
                "lane_id": "implementation",
                "worker_state": "available",
                "turn_state": "completed",
                "liveness": "terminal",
                "integration_state": "no_changes",
                "review_disposition": "not_required",
            }
        ],
    )
    result = runtime.reassign_work_group(
        work_group_id=predecessor["work_group_id"],
        machine_id="machine_new",
        reason="Use a different machine.",
        carry_context="reports",
        idempotency_key="reports-only-reassign",
        context=caller,
    )
    handoff = result["result"]["work_group"]["handoff_context"]
    assert handoff["context_mode"] == "reports"
    assert handoff["artifacts_transferred"] is False
    assert handoff["changes_transferred"] is False
    assert "required_changes_retrieval" not in handoff
