from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pytest

from patchbay.hub.adapters.pro_requests import HubProRequestAdapterV2
from patchbay.hub.adapters.worker import HubWorkerAdapterV2
from patchbay.hub.adapters.workspace import WorkspaceAdapter
from patchbay.hub.app_v2 import (
    CanonicalProRequestStoreBridgeV2,
    HubAppV2,
    HubBrokerEdgeDispatchPortV2,
    HubRuntimeTargetPortV2,
    HubWorkerProjectionPortV2,
)
from patchbay.hub.broker import OperationBroker
from patchbay.hub.protocol_v2 import HubProtocolV2
from patchbay.hub.runtime_v2 import HubRuntimeV2
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.tool_surface import HUB_V2_CONTRACT_HASH, HUB_V2_TOOL_NAMES
from patchbay.protocol.context import RequestContext


MACHINE_ID = "machine_alpha"
EDGE_GENERATION = "edgegen_alpha"
WORKSPACE_REF = "workspace_patchbay"
REPO_PATH = "/srv/projects/PatchBay"


class FakeEdgeDelivery:
    machine_id = MACHINE_ID
    edge_generation = EDGE_GENERATION
    workspace_ref = WORKSPACE_REF

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.workers: dict[str, dict[str, Any]] = {}

    async def execute(
        self,
        *,
        machine_id: str,
        edge_generation: str,
        action: str,
        arguments: Mapping[str, Any],
        target: Mapping[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        call = {
            "machine_id": machine_id,
            "edge_generation": edge_generation,
            "action": action,
            "arguments": deepcopy(dict(arguments)),
            "target": deepcopy(dict(target)),
            "context": context,
        }
        self.calls.append(call)
        assert machine_id == MACHINE_ID
        assert edge_generation == EDGE_GENERATION

        if action == "patchbay_edge_preflight":
            repo_path = str(arguments.get("repo_path") or REPO_PATH)
            return {
                "ok": True,
                "accepted": True,
                "repo_exists": True,
                "repo_resolved": repo_path,
                "repository_identity": "https://example.invalid/patchbay.git",
                "disk_free_bytes": 10_000_000_000,
                "free_worker_slots": 8,
                "queue_enabled": False,
            }
        if action == "codex_worker_options":
            return {
                "models": [{"id": "gpt-test", "reasoning_efforts": ["high"]}],
                "default_model": "gpt-test",
            }
        if action == "codex_list_workspaces":
            return {
                "workspaces": [
                    {
                        "name": "ArchiveMind",
                        "path": "/srv/projects/ArchiveMind",
                        "git": True,
                    }
                ],
                "truncated": False,
            }
        if action == "codex_worker_inbox":
            return {"accepted": True, "artifacts": [], "count": 0}
        if action == "codex_worker_start":
            name = str(arguments["name"])
            edge_worker_id = "worker_" + name.casefold().replace(" ", "_")
            worker = {
                "edge_worker_id": edge_worker_id,
                "worker_id": edge_worker_id,
                "name": name,
                "worker_state": "available",
                "turn_state": "completed",
                "liveness": "terminal",
                "integration_state": "no_changes",
                "review_disposition": "accepted",
                "report": f"{name} completed its mission.",
                "has_changes": False,
                "changed_files": [],
            }
            self.workers[edge_worker_id] = worker
            return {"accepted": True, "worker": deepcopy(worker)}
        if action == "codex_worker_message":
            edge_worker_id = str(arguments.get("worker") or target.get("edge_worker_id") or "")
            worker = deepcopy(self.workers[edge_worker_id])
            worker["turn_state"] = "completed"
            worker["report"] = "Follow-up completed."
            self.workers[edge_worker_id] = worker
            return {"accepted": True, "worker": deepcopy(worker), "message_delivered": True}
        if action == "codex_worker_inspect":
            edge_worker_id = str(arguments.get("worker") or target.get("edge_worker_id") or "")
            return {
                **deepcopy(self.workers[edge_worker_id]),
                "view": str(arguments.get("view") or "report"),
            }
        if action == "codex_worker_integrate":
            edge_worker_id = str(arguments.get("worker") or target.get("edge_worker_id") or "")
            worker = deepcopy(self.workers[edge_worker_id])
            worker["integration_state"] = "applied_to_checkout"
            self.workers[edge_worker_id] = worker
            return {"accepted": True, "applied": True, "worker": worker}
        if action == "codex_worker_stop":
            edge_worker_id = str(arguments.get("worker") or target.get("edge_worker_id") or "")
            worker = deepcopy(self.workers[edge_worker_id])
            worker.update(worker_state="stopped", turn_state="cancelled", liveness="terminal")
            self.workers[edge_worker_id] = worker
            return {"accepted": True, "stopped": True, "worker": worker}
        if action == "codex_open_workspace":
            return {"workspace_id": "ws_patchbay", "path": REPO_PATH, "exists": True}
        if action == "codex_repo_tree":
            return {"path": ".", "entries": [{"path": "README.md", "kind": "file"}]}
        if action == "codex_search_repo":
            return {"matches": [{"path": "README.md", "line": 1, "text": "PatchBay"}]}
        if action == "codex_read_file":
            return {
                "file_path": str(arguments["file_path"]),
                "text": "# PatchBay\n",
                "start_line": 1,
                "end_line": 1,
                "total_lines": 1,
                "truncated": False,
            }
        if action == "codex_git_status":
            return {"dirty": False, "changed_files": []}
        if action == "codex_show_changes":
            return {"dirty": False, "changed_files": [], "diff": ""}
        if action == "codex_git_diff":
            return {"diff": "", "truncated": False}
        raise AssertionError(f"Unhandled fake Edge action: {action}")


class FakeCanonicalProStore:
    def __init__(self):
        self.request = {
            "id": "pro_001",
            "title": "Review worker result",
            "status": "open",
            "revision": 1,
            "created_at": 1.0,
            "updated_at": 1.0,
            "origin": {
                "origin_kind": "terminal_codex",
                "worker_name": "Researcher",
                "origin_available_for_dispatch": True,
            },
            "response": {"exists": False},
            "routing": {"dispatch_status": "not_requested"},
        }
        self.response_markdown = ""
        self.dispatch_calls: list[dict[str, Any]] = []

    def _view(self) -> dict[str, Any]:
        return deepcopy(self.request)

    def _advance(self, status: str) -> None:
        self.request["status"] = status
        self.request["revision"] = int(self.request["revision"]) + 1
        self.request["updated_at"] = float(self.request["revision"])

    def list_requests(self, **kwargs: Any) -> dict[str, Any]:
        include_closed = bool(kwargs.get("include_closed"))
        visible = include_closed or self.request["status"] not in {"closed", "cancelled", "superseded"}
        requests = [self._view()] if visible else []
        return {"requests": requests, "count": len(requests), "total_known": len(requests)}

    def read_request(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("request_id") != self.request["id"]:
            raise ValueError("Pro Request not found")
        return {
            "request": self._view(),
            "report_markdown": "# Escalation\n\nPlease review the worker result.",
            "response_markdown": self.response_markdown or None,
            "events": [],
        }

    def claim_request(self, **kwargs: Any) -> dict[str, Any]:
        self._advance("claimed")
        return {"accepted": True, "request": self._view()}

    def respond_request(self, **kwargs: Any) -> dict[str, Any]:
        self.response_markdown = str(kwargs.get("response_markdown") or "")
        self._advance("answered")
        self.request["response"] = {
            "exists": True,
            "response_kind": str(kwargs.get("response_kind") or "analysis"),
        }
        return {
            "accepted": True,
            "response_stored": True,
            "dispatched": False,
            "request": self._view(),
        }

    def dispatch_request(self, **kwargs: Any) -> dict[str, Any]:
        self.dispatch_calls.append(deepcopy(kwargs))
        self._advance("dispatched_to_worker")
        self.request["routing"] = {
            "dispatch_status": "dispatched",
            "dispatch_target": str(kwargs.get("target") or "origin_worker"),
        }
        return {
            "accepted": True,
            "dispatched": True,
            "hidden_queueing": False,
            "applied": False,
            "committed": False,
            "request": self._view(),
        }

    def close_request(self, **kwargs: Any) -> dict[str, Any]:
        self._advance(str(kwargs.get("status") or "closed"))
        return {"accepted": True, "request": self._view()}


class ProtocolClient:
    def __init__(self, app: HubAppV2, context: RequestContext):
        self.app = app
        self.context = context
        self.request_id = 0

    async def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        response = await self.app.protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": deepcopy(dict(arguments))},
            },
            context=self.context,
        )
        assert response is not None and "error" not in response, response
        return deepcopy(response["result"]["structuredContent"])


def enroll_edge(app: HubAppV2) -> dict[str, Any]:
    code = app.runtime.create_enrollment_code(name="Alpha", tags=["linux", "codex"])["code"]
    enrolled = app.runtime.enroll_machine(
        code=code,
        machine_id=MACHINE_ID,
        edge_generation=EDGE_GENERATION,
        display_name="Alpha",
        tags=["linux", "codex"],
    )
    heartbeat = app.runtime.heartbeat(
        machine_id=MACHINE_ID,
        token=enrolled["node_token"],
        edge_generation=EDGE_GENERATION,
        projection_revision=1,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "max_concurrent_jobs": 8,
            "queue_enabled": False,
        },
        workspaces=[
            {
                "workspace_ref": WORKSPACE_REF,
                "alias": "PatchBay",
                "path": REPO_PATH,
                "exists": True,
                "git": True,
                "repository_identity": "https://example.invalid/patchbay.git",
            }
        ],
        worker_projection={
            "snapshot_kind": "full",
            "complete_worker_set": True,
            "workers": [],
            "tombstones": [],
        },
        resource_status={
            "active_workers": 0,
            "max_concurrent_jobs": 8,
            "free_worker_slots": 8,
            "queue_enabled": False,
            "disk_free_bytes": 10_000_000_000,
        },
    )
    assert heartbeat["projection_accepted"] is True
    return enrolled


@pytest.mark.asyncio
async def test_worker_wait_ignores_unchanged_worker_snapshots_on_new_heartbeats(
    tmp_path,
):
    app = HubAppV2(tmp_path / "hub-v2.sqlite3", edge_delivery=FakeEdgeDelivery())
    enrolled = enroll_edge(app)
    worker = {
        "edge_worker_id": "worker-stable",
        "name": "Stable Worker",
        "work_group_id": "",
        "worker_state": "available",
        "turn_state": "working",
        "liveness": "active",
        "content_revision": "sha256:stable",
        "content_sha256": "stable",
    }

    app.runtime.heartbeat(
        machine_id=MACHINE_ID,
        token=enrolled["node_token"],
        edge_generation=EDGE_GENERATION,
        projection_revision=2,
        worker_projection={"snapshot_kind": "full", "workers": [worker]},
    )
    initial = app.projection_port.query(view="status", filters={}, route={})
    revision = initial["projection_revision"]

    app.runtime.heartbeat(
        machine_id=MACHINE_ID,
        token=enrolled["node_token"],
        edge_generation=EDGE_GENERATION,
        projection_revision=3,
        worker_projection={"snapshot_kind": "full", "workers": [worker]},
        resource_status={"cpu_percent": 75.0},
    )
    repeated = app.projection_port.query(view="status", filters={}, route={})
    waited = await app.projection_port.wait(
        filters={}, route={}, since_revision=revision, timeout_seconds=0.05
    )

    assert repeated["projection_revision"] == revision
    assert waited["changed"] is False
    assert waited["projection_revision"] == revision


@pytest.mark.asyncio
async def test_runtime_port_wires_live_workspace_discovery(tmp_path):
    edge = FakeEdgeDelivery()
    app = HubAppV2(tmp_path / "hub-v2.sqlite3", edge_delivery=edge)
    enroll_edge(app)

    result = await app.workspace_adapter.workspace_list(
        {"query": "ArchiveMind", "discover": True, "max_depth": 3, "max_results": 10}
    )

    assert result["status"] == "ok"
    assert result["result"]["workspaces"][0]["display_name"] == "ArchiveMind"
    assert any(call["action"] == "codex_list_workspaces" for call in edge.calls)


@pytest.mark.asyncio
async def test_composition_root_executes_consequential_full_surface_sequence(tmp_path):
    edge = FakeEdgeDelivery()
    canonical_pro = FakeCanonicalProStore()
    app = HubAppV2(
        tmp_path / "hub-v2.sqlite3",
        edge_delivery=edge,
        canonical_pro_store=canonical_pro,
        pro_request_route={
            "machine_id": MACHINE_ID,
            "edge_generation": EDGE_GENERATION,
            "workspace_ref": WORKSPACE_REF,
        },
    )
    enroll_edge(app)
    context = RequestContext(
        client_ref="client_composition",
        owner_ref="owner_composition",
        chatgpt_session_ref="conversation_composition",
        work_run_ref="run_composition",
    )
    client = ProtocolClient(app, context)

    assert isinstance(app.store, HubStoreV2)
    assert isinstance(app.broker, OperationBroker)
    assert isinstance(app.runtime, HubRuntimeV2)
    assert isinstance(app.dispatch_port, HubBrokerEdgeDispatchPortV2)
    assert isinstance(app.runtime_port, HubRuntimeTargetPortV2)
    assert isinstance(app.projection_port, HubWorkerProjectionPortV2)
    assert isinstance(app.worker_adapter, HubWorkerAdapterV2)
    assert isinstance(app.workspace_adapter, WorkspaceAdapter)
    assert isinstance(app.pro_store_bridge, CanonicalProRequestStoreBridgeV2)
    assert isinstance(app.pro_request_adapter, HubProRequestAdapterV2)
    assert isinstance(app.protocol, HubProtocolV2)
    assert app.registered_tools == HUB_V2_TOOL_NAMES
    assert set(app.tool_bindings.values()) == {
        "runtime",
        "worker_adapter",
        "workspace_adapter",
        "pro_request_adapter",
    }

    listed = await app.protocol.handle_message(
        {"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}},
        context=context,
    )
    assert listed is not None
    assert tuple(tool["name"] for tool in listed["result"]["tools"]) == HUB_V2_TOOL_NAMES

    fleet = await client.call("patchbay_fleet_status", {"include_workspaces": True})
    workspaces = await client.call("patchbay_workspace_list", {"query": "PatchBay"})
    assert fleet["status"] == "ok"
    assert fleet["result"]["counts"]["online"] == 1
    assert workspaces["result"]["workspaces"][0]["workspace_ref"] == WORKSPACE_REF

    created = await client.call(
        "patchbay_work_group_create",
        {
            "title": "Compose Hub V2",
            "goal": "Exercise every composed boundary with real durable state.",
            "workspace_ref": WORKSPACE_REF,
            "lanes": [
                {"lane": "research", "title": "Research", "role": "Inspect"},
                {"lane": "implementation", "title": "Implementation", "role": "Build"},
            ],
            "idempotency_key": "group-create-001",
        },
    )
    group_id = created["result"]["work_group"]["work_group_id"]
    preflight_id = created["result"]["readiness"]["operation_id"]
    assert created["status"] == "ok"
    assert created["result"]["readiness"]["status"] == "ready"
    assert app.store.get_operation(preflight_id)["state"] == "succeeded"
    assert edge.calls[-1]["action"] == "patchbay_edge_preflight"

    groups = await client.call("patchbay_work_group_list", {"scope": "owned"})
    group_status = await client.call(
        "patchbay_work_group_status", {"work_group_id": group_id}
    )
    assert groups["result"]["work_groups"][0]["work_group_id"] == group_id
    assert group_status["result"]["work_group"]["activity"] == "planned"

    options = await client.call(
        "patchbay_worker_options", {"work_group_id": group_id, "max_models": 5}
    )
    assert options["status"] == "ok"
    batch = await client.call(
        "patchbay_worker_start_batch",
        {
            "work_group_id": group_id,
            "shared_brief": "Finish the assigned lane and report exact evidence.",
            "workers": [
                {
                    "item_id": "researcher",
                    "idempotency_key": "worker-researcher-001",
                    "name": "Researcher",
                    "lane": "research",
                    "mission": "Inspect the current implementation.",
                },
                {
                    "item_id": "implementer",
                    "idempotency_key": "worker-implementer-001",
                    "name": "Implementer",
                    "lane": "implementation",
                    "mission": "Build and verify the implementation.",
                },
            ],
            "idempotency_key": "batch-001",
        },
    )
    batch_operation_id = batch["operation"]["operation_id"]
    assert batch["status"] == "ok"
    assert app.store.get_operation(batch_operation_id)["state"] == "succeeded"
    start_calls = [call for call in edge.calls if call["action"] == "codex_worker_start"]
    assert len(start_calls) == 2
    assert {
        call["context"].chatgpt_session_ref for call in start_calls if call["context"] is not None
    } == {"conversation_composition"}

    worker_list = await client.call(
        "patchbay_worker_list", {"work_group_id": group_id, "include_stopped": True}
    )
    projection_revision = worker_list["result"]["projection_revision"]
    assert {worker["name"] for worker in worker_list["result"]["workers"]} == {
        "Researcher",
        "Implementer",
    }
    status = await client.call(
        "patchbay_worker_status", {"work_group_id": group_id, "since_revision": 0}
    )
    waited = await client.call(
        "patchbay_worker_wait",
        {"work_group_id": group_id, "since_revision": projection_revision - 1, "wait_seconds": 0},
    )
    assert status["result"]["counts"]["completed"] == 2
    assert waited["result"]["changed"] is True

    researcher = next(
        worker for worker in worker_list["result"]["workers"] if worker["name"] == "Researcher"
    )
    inspected = await client.call(
        "patchbay_worker_inspect",
        {
            "work_group_id": group_id,
            "fleet_worker_ref": researcher["fleet_worker_ref"],
            "view": "report",
        },
    )
    messaged = await client.call(
        "patchbay_worker_message",
        {
            "work_group_id": group_id,
            "fleet_worker_ref": researcher["fleet_worker_ref"],
            "message": "Confirm the final evidence.",
            "idempotency_key": "message-researcher-001",
        },
    )
    assert inspected["result"]["report"].endswith("completed its mission.")
    assert messaged["status"] == "ok"
    assert messaged["result"]["message_delivered"] is True

    opened = await client.call("patchbay_workspace_open", {"work_group_id": group_id})
    tree = await client.call("patchbay_workspace_tree", {"work_group_id": group_id})
    searched = await client.call(
        "patchbay_workspace_search", {"work_group_id": group_id, "query": "PatchBay"}
    )
    read = await client.call(
        "patchbay_workspace_read_file",
        {"work_group_id": group_id, "file_path": "README.md"},
    )
    changes = await client.call(
        "patchbay_workspace_changes", {"work_group_id": group_id, "view": "status"}
    )
    assert opened["result"]["exists"] is True
    assert tree["result"]["entries"][0]["path"] == "README.md"
    assert searched["result"]["matches"][0]["path"] == "README.md"
    assert read["result"]["text"] == "# PatchBay\n"
    assert changes["result"]["dirty"] is False

    pro_list = await client.call(
        "patchbay_pro_request_list", {"work_group_id": group_id, "limit": 10}
    )
    request_ref = pro_list["result"]["requests"][0]["request_ref"]
    pro_read = await client.call(
        "patchbay_pro_request_read", {"request_id": request_ref, "work_group_id": group_id}
    )
    assert pro_read["result"]["request"]["revision"] == 1
    claimed = await client.call(
        "patchbay_pro_request_claim",
        {
            "request_id": request_ref,
            "work_group_id": group_id,
            "expected_revision": 1,
            "idempotency_key": "pro-claim-001",
        },
    )
    responded = await client.call(
        "patchbay_pro_request_respond",
        {
            "request_id": request_ref,
            "work_group_id": group_id,
            "expected_revision": 2,
            "response_markdown": "Use the reviewed worker result.",
            "worker_message_markdown": "Proceed with the reviewed result.",
            "idempotency_key": "pro-respond-001",
        },
    )
    dispatched = await client.call(
        "patchbay_pro_request_dispatch",
        {
            "request_id": request_ref,
            "work_group_id": group_id,
            "expected_revision": 3,
            "target": "origin_worker",
            "idempotency_key": "pro-dispatch-001",
        },
    )
    closed_pro = await client.call(
        "patchbay_pro_request_close",
        {
            "request_id": request_ref,
            "work_group_id": group_id,
            "expected_revision": 4,
            "reason": "Handled",
            "status": "closed",
            "idempotency_key": "pro-close-001",
        },
    )
    assert claimed["status"] == "ok"
    assert responded["result"]["response_stored"] is True
    assert responded["result"]["dispatched"] is False
    assert dispatched["result"]["dispatched"] is True
    assert dispatched["result"]["applied"] is False
    assert closed_pro["result"]["request"]["status"] == "closed"
    assert len(canonical_pro.dispatch_calls) == 1

    operation = await client.call(
        "patchbay_operation_status",
        {"operation_id": batch_operation_id, "include_result": True},
    )
    assert operation["status"] == "ok"
    assert operation["result"]["outcome"]["terminal"] is True
    assert operation["result"]["domain_result"]["total"] == 2

    final_workers = await client.call(
        "patchbay_worker_list", {"work_group_id": group_id, "include_stopped": True}
    )
    closure = await client.call(
        "patchbay_work_group_close",
        {
            "work_group_id": group_id,
            "outcome": "complete",
            "summary": "Both lanes completed and the Pro Request was handled.",
            "worker_dispositions": [
                {
                    "fleet_worker_ref": worker["fleet_worker_ref"],
                    "disposition": "no_changes",
                }
                for worker in final_workers["result"]["workers"]
            ],
            "idempotency_key": "group-close-001",
        },
    )
    assert closure["status"] == "ok"
    assert closure["result"]["work_group"]["status"] == "closed"
    assert closure["result"]["work_group"]["outcome"] == "complete"

    app.close()
    assert app.store.closed is True
    app.close()
