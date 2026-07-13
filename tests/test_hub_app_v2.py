from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Mapping

import pytest

from patchbay.hub.adapters.pro_requests import (
    FleetHubProRequestAdapterV2,
    HubProRequestAdapterV2,
)
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
from patchbay.hub.backup_v2 import AdmissionFreezeController
from patchbay.hub.operations import public_envelope
from patchbay.hub.protocol_v2 import HubProtocolV2
from patchbay.hub.runtime_v2 import (
    FLEET_WORKER_ENTITY,
    HubRuntimeV2,
    WORKER_PROJECTION_ENTITY,
    WORK_GROUP_ENTITY,
)
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.tool_surface import HUB_V2_CONTRACT_HASH, HUB_V2_TOOL_NAMES
from patchbay.protocol.context import RequestContext


MACHINE_ID = "machine_alpha"
EDGE_GENERATION = "edgegen_alpha"
WORKSPACE_REF = "workspace_patchbay"
REPO_PATH = "/srv/projects/PatchBay"


@pytest.mark.asyncio
async def test_admission_freeze_blocks_new_mutations_but_keeps_reads_available(
    tmp_path,
):
    gate = AdmissionFreezeController()
    app = HubAppV2(
        tmp_path / "frozen-hub.sqlite3",
        edge_delivery=FakeEdgeDelivery(),
        admission_gate=gate,
    )
    enroll_edge(app)
    lease = gate.freeze_admissions(reason="state-preserving rollout")
    try:
        fleet = await app.handle_tool_call("patchbay_fleet_status", {})
        blocked = await app.handle_tool_call(
            "patchbay_work_group_create",
            {
                "title": "Must not start",
                "goal": "Prove maintenance admission is closed.",
                "workspace_ref": WORKSPACE_REF,
                "lanes": [{"lane": "main", "title": "Main", "role": "Implement"}],
                "idempotency_key": "frozen-group-create",
            },
        )
    finally:
        lease.release()

    assert fleet["status"] == "ok"
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "hub_mutation_admission_frozen"
    assert app.store.list_entities(WORK_GROUP_ENTITY) == []

    created = await app.handle_tool_call(
        "patchbay_work_group_create",
        {
            "title": "Allowed after rollout",
            "goal": "Prove admission reopens after maintenance.",
            "workspace_ref": WORKSPACE_REF,
            "lanes": [{"lane": "main", "title": "Main", "role": "Implement"}],
            "idempotency_key": "unfrozen-group-create",
        },
    )
    assert created["status"] in {"ok", "pending"}


@pytest.mark.asyncio
async def test_read_tools_never_dispatch_pending_mutations_and_explicit_cycle_recovers_them(
    tmp_path,
):
    edge = FakeEdgeDelivery()
    app = HubAppV2(tmp_path / "read-is-not-dispatch.sqlite3", edge_delivery=edge)
    enroll_edge(app)

    created = app.runtime.create_work_group(
        title="Pending recovery",
        goal="Prove status reads cannot start unrelated effects.",
        machine_id=MACHINE_ID,
        lanes=[{"lane": "main", "title": "Main", "role": "Implement"}],
        idempotency_key="pending-recovery-group",
    )
    operation_id = str(created["result"]["readiness"]["operation_id"])
    before = app.store.get_operation(operation_id)
    assert before is not None and before["state"] == "dispatchable"
    assert edge.calls == []

    fleet = await app.handle_tool_call("patchbay_fleet_status", {})
    status = await app.handle_tool_call(
        "patchbay_operation_status",
        {"operation_id": operation_id, "include_result": True},
    )

    assert fleet["status"] == "ok"
    assert status["status"] == "pending"
    assert app.store.get_operation(operation_id)["state"] == "dispatchable"
    assert edge.calls == []

    delivered = await app.dispatch_pending_operations(max_operations=1)
    reconciled = await app.handle_tool_call(
        "patchbay_operation_status",
        {"operation_id": operation_id, "include_result": True},
    )

    assert delivered == [operation_id]
    assert edge.calls[-1]["action"] == "patchbay_edge_preflight"
    assert app.store.get_operation(operation_id)["state"] == "succeeded"
    assert reconciled["status"] == "ok"
    assert reconciled["result"]["outcome"]["terminal"] is True


@pytest.mark.asyncio
async def test_remote_read_dispatches_only_its_own_matching_operation(
    tmp_path, monkeypatch
):
    app = HubAppV2(
        tmp_path / "targeted-read.sqlite3",
        edge_delivery=FakeEdgeDelivery(),
    )
    unrelated = app.broker.create_operation(
        tool="patchbay_worker_stop",
        logical_target="worker:unrelated",
        idempotency_key="unrelated-stop",
        payload={"action": "codex_worker_stop"},
    )
    own = app.broker.create_operation(
        tool="codex_read_file",
        logical_target="workspace:file",
        idempotency_key="targeted-read",
        payload={"action": "codex_read_file"},
    )
    dispatched: list[str] = []

    async def matching_read(name, arguments, *, context=None):
        del arguments, context
        assert name == "patchbay_workspace_read_file"
        return public_envelope("pending", operation=own)

    async def targeted_dispatch(operation_id, *, context=None):
        del context
        dispatched.append(operation_id)
        return False

    monkeypatch.setattr(app.runtime, "handle_tool_call", matching_read)
    monkeypatch.setattr(app.dispatch_port, "dispatch_if_pending", targeted_dispatch)

    result = await app.handle_tool_call(
        "patchbay_workspace_read_file",
        {"workspace_ref": WORKSPACE_REF, "path": "README.md"},
    )

    assert result["status"] == "pending"
    assert dispatched == [own["operation_id"]]
    assert unrelated["operation_id"] not in dispatched


@pytest.mark.asyncio
async def test_mutation_dispatches_only_operations_returned_by_that_call(
    tmp_path, monkeypatch
):
    app = HubAppV2(
        tmp_path / "targeted-mutation.sqlite3",
        edge_delivery=FakeEdgeDelivery(),
    )
    unrelated = app.broker.create_operation(
        tool="patchbay_worker_stop",
        logical_target="worker:unrelated",
        idempotency_key="unrelated-stop-mutation",
        payload={"action": "codex_worker_stop"},
    )
    own = app.broker.create_operation(
        tool="patchbay_worker_stop",
        logical_target="worker:own",
        idempotency_key="own-stop-mutation",
        payload={"action": "codex_worker_stop"},
    )
    dispatched: list[str] = []

    async def matching_mutation(name, arguments, *, context=None):
        del arguments, context
        assert name == "patchbay_worker_stop"
        return public_envelope("pending", operation=own)

    async def targeted_dispatch(operation_id, *, context=None):
        del context
        dispatched.append(operation_id)
        return False

    monkeypatch.setattr(app.runtime, "handle_tool_call", matching_mutation)
    monkeypatch.setattr(app.dispatch_port, "dispatch_if_pending", targeted_dispatch)

    result = await app.handle_tool_call(
        "patchbay_worker_stop",
        {
            "work_group_id": "group_targeted",
            "fleet_worker_ref": "fworker_targeted",
            "idempotency_key": "targeted-stop",
        },
    )

    assert result["status"] == "pending"
    assert dispatched == [own["operation_id"]]
    assert unrelated["operation_id"] not in dispatched


class FakeEdgeDelivery:
    machine_id = MACHINE_ID
    edge_generation = EDGE_GENERATION
    workspace_ref = WORKSPACE_REF

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.workers: dict[str, dict[str, Any]] = {}
        self.preflight_pending = False

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
            if self.preflight_pending:
                return public_envelope("pending", result={"accepted": True})
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


async def complete_pending_preflight(app: HubAppV2, *, delay: float = 0.02) -> None:
    while True:
        for entity in app.store.list_entities(WORK_GROUP_ENTITY):
            group = entity["record"]
            readiness = group.get("readiness") or {}
            operation_id = str(readiness.get("operation_id") or "")
            operation = app.store.get_operation(operation_id) if operation_id else None
            if (
                readiness.get("status") == "pending"
                and (operation or {}).get("state") == "running"
            ):
                await asyncio.sleep(delay)
                app.runtime.record_preflight_result(
                    work_group_id=str(group["work_group_id"]),
                    operation_id=operation_id,
                    result={
                        "ok": True,
                        "accepted": True,
                        "repo_exists": True,
                        "repo_resolved": str(
                            group.get("resolved_repo_path") or REPO_PATH
                        ),
                        "repository_identity": "https://example.invalid/patchbay.git",
                        "disk_free_bytes": 10_000_000_000,
                        "free_worker_slots": 8,
                        "queue_enabled": False,
                    },
                )
                return
        await asyncio.sleep(0.005)


@pytest.mark.parametrize(
    "tool_name",
    ["patchbay_work_group_create", "patchbay_work_group_resume"],
    ids=["create", "resume"],
)
@pytest.mark.parametrize("complete_early", [False, True], ids=["timeout", "early"])
@pytest.mark.asyncio
async def test_group_preflight_wait_honors_timeout_and_early_completion(
    tmp_path,
    monkeypatch,
    tool_name,
    complete_early,
):
    edge = FakeEdgeDelivery()
    app = HubAppV2(tmp_path / f"{tool_name}-{complete_early}.sqlite3", edge_delivery=edge)
    enroll_edge(app)
    context = RequestContext(
        client_ref="client_preflight_wait",
        owner_ref="owner_preflight_wait",
        chatgpt_session_ref="conversation_preflight_wait",
        work_run_ref="run_preflight_wait",
    )
    client = ProtocolClient(app, context)
    group_id = ""
    if tool_name == "patchbay_work_group_resume":
        created = await client.call(
            "patchbay_work_group_create",
            {
                "title": "Resume preflight wait",
                "goal": "Create the group used by the resume wait regression.",
                "workspace_ref": WORKSPACE_REF,
                "idempotency_key": f"resume-wait-setup-{complete_early}",
            },
        )
        group_id = str(created["result"]["work_group"]["work_group_id"])

    edge.preflight_pending = True
    wait_calls: list[dict[str, Any]] = []
    original_handle_tool_call = app.runtime.handle_tool_call

    async def record_wait_and_warning(name, arguments, *, context=None):
        if name == "patchbay_work_group_status":
            wait_calls.append(deepcopy(dict(arguments)))
        response = deepcopy(
            dict(
                await original_handle_tool_call(
                    name,
                    arguments,
                    context=context,
                )
            )
        )
        if name == tool_name:
            response["warnings"] = list(response.get("warnings") or []) + [
                "Preserve the original operation warning."
            ]
        return response

    monkeypatch.setattr(app.runtime, "handle_tool_call", record_wait_and_warning)
    completion = (
        asyncio.create_task(complete_pending_preflight(app))
        if complete_early
        else None
    )
    if tool_name == "patchbay_work_group_create":
        arguments = {
            "title": "Create preflight wait",
            "goal": "Exercise the composition-layer preflight wait.",
            "workspace_ref": WORKSPACE_REF,
            "wait_for_preflight_seconds": 1,
            "idempotency_key": f"create-wait-{complete_early}",
        }
    else:
        arguments = {
            "work_group_id": group_id,
            "wait_for_preflight_seconds": 1,
            "idempotency_key": f"resume-wait-{complete_early}",
        }

    result = await client.call(tool_name, arguments)
    if completion is not None:
        await completion

    assert len(wait_calls) == 1
    assert wait_calls[0]["work_group_id"] == result["result"]["work_group"]["work_group_id"]
    assert wait_calls[0]["since_revision"] > 0
    assert wait_calls[0]["wait_for_change_seconds"] == 1
    assert result["operation"]["tool_name"] == tool_name
    assert result["warnings"] == ["Preserve the original operation warning."]
    assert result["result"]["changed"] is complete_early
    assert result["result"]["readiness"]["status"] == (
        "ready" if complete_early else "pending"
    )


@pytest.mark.parametrize(
    "tool_name",
    ["patchbay_work_group_create", "patchbay_work_group_resume"],
    ids=["create", "resume"],
)
@pytest.mark.asyncio
async def test_group_preflight_wait_returns_immediately_when_dispatch_is_ready(
    tmp_path,
    monkeypatch,
    tool_name,
):
    edge = FakeEdgeDelivery()
    app = HubAppV2(tmp_path / f"{tool_name}-ready.sqlite3", edge_delivery=edge)
    enroll_edge(app)
    context = RequestContext(
        client_ref="client_preflight_ready",
        owner_ref="owner_preflight_ready",
        chatgpt_session_ref="conversation_preflight_ready",
        work_run_ref="run_preflight_ready",
    )
    client = ProtocolClient(app, context)
    group_id = ""
    if tool_name == "patchbay_work_group_resume":
        created = await client.call(
            "patchbay_work_group_create",
            {
                "title": "Ready resume setup",
                "goal": "Create a ready group before the resume regression.",
                "workspace_ref": WORKSPACE_REF,
                "idempotency_key": "ready-resume-setup",
            },
        )
        group_id = str(created["result"]["work_group"]["work_group_id"])

    wait_calls: list[dict[str, Any]] = []
    original_handle_tool_call = app.runtime.handle_tool_call

    async def record_wait(name, arguments, *, context=None):
        if name == "patchbay_work_group_status":
            wait_calls.append(deepcopy(dict(arguments)))
        return await original_handle_tool_call(name, arguments, context=context)

    monkeypatch.setattr(app.runtime, "handle_tool_call", record_wait)
    if tool_name == "patchbay_work_group_create":
        arguments = {
            "title": "Already-ready create",
            "goal": "Return without entering the status wait.",
            "workspace_ref": WORKSPACE_REF,
            "wait_for_preflight_seconds": 1,
            "idempotency_key": "already-ready-create",
        }
    else:
        arguments = {
            "work_group_id": group_id,
            "wait_for_preflight_seconds": 1,
            "idempotency_key": "already-ready-resume",
        }

    result = await client.call(tool_name, arguments)

    assert result["result"]["readiness"]["status"] == "ready"
    assert wait_calls == []


@pytest.mark.asyncio
async def test_worker_wait_ignores_unchanged_worker_snapshots_on_new_heartbeats(
    tmp_path,
):
    app = HubAppV2(tmp_path / "hub-v2.sqlite3", edge_delivery=FakeEdgeDelivery())
    projection = HubWorkerProjectionPortV2(
        app.runtime,
        max_wait_seconds=0.05,
        minimum_poll_seconds=0.05,
        recommended_poll_seconds=0.05,
    )
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
    initial = projection.query(view="status", filters={}, route={})
    revision = initial["projection_revision"]

    app.runtime.heartbeat(
        machine_id=MACHINE_ID,
        token=enrolled["node_token"],
        edge_generation=EDGE_GENERATION,
        projection_revision=3,
        worker_projection={"snapshot_kind": "full", "workers": [worker]},
        resource_status={"cpu_percent": 75.0},
    )
    repeated = projection.query(view="status", filters={}, route={})
    waited = await projection.wait(
        filters={}, route={}, since_revision=revision, timeout_seconds=0.05
    )

    assert repeated["projection_revision"] == revision
    assert repeated["poll_too_early"] is True
    assert waited["changed"] is False
    assert waited["projection_revision"] == revision


def test_worker_list_and_status_share_cooldown_per_manager_and_group(tmp_path):
    app = HubAppV2(tmp_path / "hub-v2.sqlite3", edge_delivery=FakeEdgeDelivery())
    for name, state in (("Active", "working"), ("Complete", "completed")):
        app.store.put_entity(
            WORKER_PROJECTION_ENTITY,
            f"worker_poll_{name.casefold()}",
            {
                "fleet_worker_ref": f"worker_poll_{name.casefold()}",
                "edge_worker_id": f"edge_worker_poll_{name.casefold()}",
                "work_group_id": "group_poll",
                "name": name,
                "turn_state": state,
                "liveness": "active" if state == "working" else "completed",
            },
            expected_revision=0,
        )
    context_one = RequestContext(
        client_ref="client_poll_one",
        chatgpt_session_ref="conversation_poll_one",
    )
    context_two = RequestContext(
        client_ref="client_poll_two",
        chatgpt_session_ref="conversation_poll_two",
    )
    filters = {"work_group_id": "group_poll", "limit": 50}
    route = {"work_group_id": "group_poll"}

    listed = app.projection_port.query(
        view="list", filters=filters, route=route, context=context_one
    )
    cached_status = app.projection_port.query(
        view="status",
        filters={**filters, "active_only": True, "force_refresh": True},
        route=route,
        context=context_one,
    )
    other_manager = app.projection_port.query(
        view="status", filters=filters, route=route, context=context_two
    )
    other_group = app.projection_port.query(
        view="status",
        filters={"work_group_id": "group_other", "limit": 50},
        route={"work_group_id": "group_other"},
        context=context_one,
    )

    assert listed["poll_too_early"] is False
    assert listed["count"] == 2
    assert listed["minimum_next_poll_seconds"] == 20
    assert listed["recommended_next_poll_seconds"] == 30
    assert cached_status["view"] == "status"
    assert cached_status["poll_too_early"] is True
    assert cached_status["count"] == 1
    assert cached_status["workers"][0]["name"] == "Active"
    assert cached_status["status_current"] is False
    assert 1 <= cached_status["retry_after_seconds"] <= 20
    assert "same manager" not in cached_status["poll_guidance"]
    assert "This manager checked this work group" in cached_status["poll_guidance"]
    assert other_manager["poll_too_early"] is False
    assert other_group["poll_too_early"] is False


@pytest.mark.asyncio
async def test_worker_wait_clamps_too_small_timeout_and_refreshes_poll_cache(tmp_path):
    app = HubAppV2(tmp_path / "hub-v2.sqlite3", edge_delivery=FakeEdgeDelivery())
    context = RequestContext(
        client_ref="client_wait_clamp",
        chatgpt_session_ref="conversation_wait_clamp",
    )
    app.store.put_entity(
        WORKER_PROJECTION_ENTITY,
        "worker_wait_clamp",
        {
            "fleet_worker_ref": "worker_wait_clamp",
            "edge_worker_id": "edge_worker_wait_clamp",
            "work_group_id": "group_wait_clamp",
            "name": "Wait Clamp Worker",
            "turn_state": "working",
            "liveness": "active",
        },
        expected_revision=0,
    )
    filters = {"work_group_id": "group_wait_clamp"}
    route = {"work_group_id": "group_wait_clamp"}

    waited = await app.projection_port.wait(
        filters=filters,
        route=route,
        since_revision=0,
        timeout_seconds=0,
        context=context,
    )
    cached = app.projection_port.query(
        view="status", filters=filters, route=route, context=context
    )

    assert waited["changed"] is True
    assert waited["requested_wait_seconds"] == 0
    assert waited["effective_wait_seconds"] == 20
    assert waited["minimum_next_poll_seconds"] == 20
    assert waited["recommended_next_poll_seconds"] == 30
    assert cached["poll_too_early"] is True


def test_completion_contract_uses_all_workers_not_only_the_returned_page(tmp_path):
    app = HubAppV2(tmp_path / "hub-v2.sqlite3", edge_delivery=FakeEdgeDelivery())
    group_id = "group_many_workers"
    app.store.put_entity(
        WORK_GROUP_ENTITY,
        group_id,
        {
            "work_group_id": group_id,
            "status": "open",
            "execution_mode": "end_to_end",
            "definition_of_done": "Every worker is complete.",
        },
        expected_revision=0,
    )
    for index in range(51):
        app.store.put_entity(
            WORKER_PROJECTION_ENTITY,
            f"worker_{index:02d}",
            {
                "fleet_worker_ref": f"worker_{index:02d}",
                "edge_worker_id": f"edge_worker_{index:02d}",
                "work_group_id": group_id,
                "name": f"Worker {index:02d}",
                "turn_state": "working" if index == 50 else "completed",
                "liveness": "active" if index == 50 else "completed",
                "integration_state": "not_applicable",
                "worker_state": "available",
            },
            expected_revision=0,
        )

    status = app.projection_port._query_result(
        filters={"work_group_id": group_id, "limit": 50},
        route={"work_group_id": group_id},
    )

    assert len(status["workers"]) == 50
    assert status["total_known"] == 51
    assert status["completion_contract"]["reason"] == "workers_or_operations_active"
    assert status["completion_contract"]["activity_counts"]["active"] == 1
    assert status["completion_contract"]["final_response_allowed"] is False


@pytest.mark.asyncio
async def test_worker_target_falls_back_to_group_scoped_fleet_record_without_projection(tmp_path):
    edge = FakeEdgeDelivery()
    app = HubAppV2(tmp_path / "hub-v2.sqlite3", edge_delivery=edge)
    enroll_edge(app)
    context = RequestContext(
        client_ref="client_projection_fallback",
        owner_ref="owner_projection_fallback",
        chatgpt_session_ref="conversation_projection_fallback",
        work_run_ref="run_projection_fallback",
    )
    client = ProtocolClient(app, context)
    created = await client.call(
        "patchbay_work_group_create",
        {
            "title": "Projection fallback",
            "goal": "Route from durable fleet identity when projection is absent.",
            "workspace_ref": WORKSPACE_REF,
            "lanes": [{"lane": "main", "title": "Main", "role": "Implement"}],
            "idempotency_key": "projection-fallback-group",
        },
    )
    group_id = created["result"]["work_group"]["work_group_id"]
    fleet_ref = "fleet_projection_missing"
    edge_worker_id = "edge_projection_missing"
    fleet_record = {
        "fleet_worker_ref": fleet_ref,
        "machine_id": MACHINE_ID,
        "edge_generation": EDGE_GENERATION,
        "edge_worker_id": edge_worker_id,
        "work_group_id": group_id,
        "lane_id": "main",
        "workspace_ref": WORKSPACE_REF,
        "name": "Durable Worker",
        "created_at": 1.0,
    }
    app.store.put_entity(FLEET_WORKER_ENTITY, fleet_ref, fleet_record, expected_revision=0)
    app.store.put_entity(
        FLEET_WORKER_ENTITY,
        "fleet_wrong_group",
        {**fleet_record, "fleet_worker_ref": "fleet_wrong_group", "work_group_id": "other-group"},
        expected_revision=0,
    )
    app.store.put_entity(
        FLEET_WORKER_ENTITY,
        "fleet_wrong_generation",
        {
            **fleet_record,
            "fleet_worker_ref": "fleet_wrong_generation",
            "edge_worker_id": "edge_wrong_generation",
            "edge_generation": "stale-generation",
        },
        expected_revision=0,
    )
    edge.workers[edge_worker_id] = {
        "edge_worker_id": edge_worker_id,
        "worker_id": edge_worker_id,
        "name": "Durable Worker",
        "worker_state": "available",
        "turn_state": "completed",
        "liveness": "terminal",
        "integration_state": "no_changes",
        "review_disposition": "accepted",
        "report": "Durable worker remains authoritative at Edge.",
    }

    inspect_target = await app.runtime_port.resolve_target(
        tool_name="patchbay_worker_inspect",
        arguments={"work_group_id": group_id, "worker": "Durable Worker"},
        context=context,
    )
    message_target = await app.runtime_port.resolve_target(
        tool_name="patchbay_worker_message",
        arguments={"work_group_id": group_id, "fleet_worker_ref": fleet_ref},
        context=context,
    )
    inspected = await client.call(
        "patchbay_worker_inspect",
        {"work_group_id": group_id, "fleet_worker_ref": fleet_ref, "view": "report"},
    )
    messaged = await client.call(
        "patchbay_worker_message",
        {
            "work_group_id": group_id,
            "fleet_worker_ref": fleet_ref,
            "message": "Continue from durable Edge state.",
            "idempotency_key": "projection-fallback-message",
        },
    )

    assert inspect_target["edge_worker_id"] == edge_worker_id
    assert inspect_target["projection_missing"] is True
    assert inspect_target["worker"]["projection_missing"] is True
    assert message_target["edge_worker_id"] == edge_worker_id
    assert message_target["projection_missing"] is True
    assert inspected["status"] == "ok"
    assert inspected["result"]["report"] == "Durable worker remains authoritative at Edge."
    assert inspected["result"]["worker"]["projection_missing"] is True
    assert messaged["status"] == "ok"
    assert messaged["result"]["message_delivered"] is True


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

    preflight_call_count = sum(
        call["action"] == "patchbay_edge_preflight" for call in edge.calls
    )
    replayed = await client.call(
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
    assert replayed["result"]["work_group"]["work_group_id"] == group_id
    assert replayed["result"]["readiness"]["operation_id"] == preflight_id
    assert sum(
        call["action"] == "patchbay_edge_preflight" for call in edge.calls
    ) == preflight_call_count
    assert len(app.store.list_entities(WORK_GROUP_ENTITY)) == 1

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


@pytest.mark.asyncio
async def test_app_normalizes_unknown_direct_next_action_to_public_operation_status(
    tmp_path, monkeypatch
):
    app = HubAppV2(tmp_path / "unknown-next-action.sqlite3", edge_delivery=FakeEdgeDelivery())

    async def untrusted_handler(name, arguments, *, context=None):
        assert name == "patchbay_fleet_status"
        assert arguments == {}
        return public_envelope(
            "pending",
            operation={"operation_id": "op-untrusted-action", "state": "running"},
            next_actions=[
                {
                    "tool": "complete_reconciliation",
                    "arguments": {"attempt_id": "untrusted"},
                }
            ],
        )

    async def no_pending_delivery(*, context=None):
        return []

    monkeypatch.setattr(app.runtime, "handle_tool_call", untrusted_handler)
    monkeypatch.setattr(app.dispatch_port, "dispatch_pending", no_pending_delivery)

    result = await app.handle_tool_call("patchbay_fleet_status", {})

    assert result["next_actions"] == [
        {
            "tool": "patchbay_operation_status",
            "arguments": {"operation_id": "op-untrusted-action"},
            "reason": "Inspect this operation through Hub's public recovery tool.",
        }
    ]
    assert "complete_reconciliation" not in str(result)


def test_operation_status_decorates_canonical_pro_request_tool_names(
    tmp_path, monkeypatch
):
    app = HubAppV2(
        tmp_path / "canonical-pro-operation.sqlite3",
        edge_delivery=FakeEdgeDelivery(),
    )
    assert isinstance(app.pro_request_adapter, FleetHubProRequestAdapterV2)
    operation = {
        "operation_id": "op-canonical-pro-read",
        "tool": "codex_pro_request_read",
    }
    monkeypatch.setattr(
        app.store,
        "get_operation",
        lambda operation_id: operation
        if operation_id == operation["operation_id"]
        else None,
    )
    monkeypatch.setattr(
        app.pro_request_adapter,
        "operation_result",
        lambda value: public_envelope(
            "ok",
            result={
                "request": {"request_id": "request-1"},
                "machine_id": MACHINE_ID,
                "edge_generation": EDGE_GENERATION,
            },
            operation=value,
        ),
    )

    refreshed = app._refresh_operation_status_result(
        {"operation_id": operation["operation_id"]},
        public_envelope("ok", result={"outcome": {"terminal": True}}),
    )

    assert refreshed["result"]["domain_result"]["machine_id"] == MACHINE_ID
    assert (
        refreshed["result"]["domain_result"]["edge_generation"]
        == EDGE_GENERATION
    )
    app.close()
