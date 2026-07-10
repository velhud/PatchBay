from __future__ import annotations

import asyncio
import inspect
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

import pytest

from patchbay.hub.adapters.pro_requests import (
    FleetHubProRequestAdapterV2,
    PRO_REQUEST_ASSOCIATION_ENTITY,
    HubProRequestAdapterV2,
)
from patchbay.hub.edge import build_capabilities, build_workspaces
from patchbay.hub.edge_client_v2 import DEFAULT_ENDPOINTS, EdgeV2Profile, EdgeV2Runner
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.edge_v2 import EdgeExecutionService
from patchbay.hub.protocol_v2 import validate_hub_v2_tool_output
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.tool_surface import HUB_V2_EXPECTED_TOOL_COUNT, HUB_V2_TOOL_NAMES
from patchbay.hub.transport_v2 import create_production_hub_v2_app
from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.pro_requests import ProRequestStore
from patchbay.protocol.context import RequestContext
from patchbay.tools.handler import ToolHandler


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Hub V2 Pro Requests\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Pro Adapter Test",
            "-c",
            "user.email=pro-adapter@example.invalid",
            "commit",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def _config(tmp_path: Path, repo: Path) -> dict[str, Any]:
    return {
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {"require_git_repo": False, "blocked_globs": []},
        "pro_requests": {
            "root": str(tmp_path / "runtime" / "pro-requests"),
            "mirror_enabled": False,
            "max_report_bytes": 20_000,
            "max_response_bytes": 20_000,
        },
    }


def _context(name: str, *, group: str = "group_alpha", lane: str = "analysis") -> RequestContext:
    return RequestContext(
        client_ref=f"client_{name}",
        chatgpt_session_ref=f"conversation_{name}",
        work_run_ref=f"run_{name}",
        work_group_id=group,
        lane_id=lane,
    )


class RecordingCanonicalDispatch:
    def __init__(self, store: ProRequestStore):
        self.store = store
        self.calls: list[dict[str, Any]] = []

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        assert name == "codex_pro_request_dispatch"
        args = dict(arguments)
        self.calls.append(args)
        target = str(args.get("target") or "origin_worker")
        manifest, refusal = self.store.mark_dispatch_requested(
            request_id=args["request_id"],
            target=target,
            request_context=context,
            takeover=bool(args.get("takeover", False)),
        )
        if refusal:
            return {"accepted": False, "request_id": manifest["id"], **refusal}
        worker = {
            "accepted": True,
            "name": "Origin Worker" if target == "origin_worker" else args.get("new_worker_name"),
            "target": target,
        }
        request = self.store.finish_dispatch(
            request_id=args["request_id"],
            accepted=True,
            target=target,
            dispatch_result=worker,
            request_context=context,
        )
        return {
            "accepted": True,
            "dispatched": True,
            "request": request,
            "dispatch_result": worker,
            "note": "Explicit dispatch only; no integration or commit.",
        }


def _fixture(tmp_path: Path, *, visibility: str = "private"):
    repo = _init_repo(tmp_path / "repo")
    config = _config(tmp_path, repo)
    canonical = ProRequestStore(config)
    report = tmp_path / "report.md"
    report.write_text("# Escalation\n\nNeed a bounded architecture decision.\n", encoding="utf-8")
    created = canonical.create_request(
        repo_path=str(repo),
        title="Architecture blocked",
        origin_kind="terminal_codex",
        origin_worker="Origin Worker",
        report_path=str(report),
        desired_output="Plan and verification",
    )
    hub = HubStoreV2(tmp_path / "hub.sqlite3")
    dispatcher = RecordingCanonicalDispatch(canonical)
    adapter = HubProRequestAdapterV2(
        hub,
        canonical,
        machine_id="machine_alpha",
        edge_generation="edgegen_alpha",
        workspace_ref="workspace_patchbay",
        work_group_id="group_alpha",
        lane="analysis",
        visibility=visibility,
        origin_operation_id="op_origin",
        dispatch_executor=dispatcher,
        reference_salt="test-salt",
    )
    return canonical, hub, dispatcher, adapter, created["id"]


def _run(awaitable):
    return asyncio.run(awaitable)


def test_all_six_actions_preserve_canonical_fields_and_semantic_envelopes(tmp_path):
    canonical, hub, dispatcher, adapter, request_id = _fixture(tmp_path)
    context = _context("owner")

    listed = _run(
        adapter.handle_tool_call(
            "patchbay_pro_request_list",
            {"work_group_id": "group_alpha", "limit": 10},
            context=context,
        )
    )
    assert set(listed) == {"status", "result", "operation", "warnings", "next_actions"}
    assert listed["status"] == "ok"
    item = listed["result"]["requests"][0]
    request_ref = item["request_ref"]
    assert item["id"] == request_id
    assert item["title"] == "Architecture blocked"
    assert request_ref.startswith("proreq_") and request_ref != request_id
    assert item["machine_id"] == "machine_alpha"
    assert item["edge_generation"] == "edgegen_alpha"
    assert item["workspace_ref"] == "workspace_patchbay"
    assert item["work_group_id"] == "group_alpha"
    assert item["lane"] == "analysis"
    assert item["origin_operation_id"] == "op_origin"
    assert listed["operation"] == {}
    validate_hub_v2_tool_output("patchbay_pro_request_list", listed)

    read = _run(
        adapter.handle_tool_call(
            "patchbay_pro_request_read",
            {"request_id": request_ref, "include_report": True},
            context=context,
        )
    )
    assert read["status"] == "ok"
    assert "bounded architecture decision" in read["result"]["report_markdown"]
    assert read["result"]["request"]["id"] == request_id
    assert read["result"]["request_ref"] == request_ref
    assert read["operation"] == {}
    validate_hub_v2_tool_output("patchbay_pro_request_read", read)

    claimed = _run(
        adapter.handle_tool_call(
            "patchbay_pro_request_claim",
            {
                "request_id": request_ref,
                "expected_revision": 1,
                "note": "Owner is investigating",
                "idempotency_key": "claim-owner-1",
            },
            context=context,
        )
    )
    assert claimed["status"] == "ok"
    assert claimed["result"]["accepted"] is True
    assert claimed["result"]["request"]["revision"] == 2
    assert claimed["result"]["request"]["claim_lease"]["participant_ref"] == "conversation_owner"
    assert claimed["operation"]["tool_name"] == "patchbay_pro_request_claim"
    validate_hub_v2_tool_output("patchbay_pro_request_claim", claimed)

    responded = _run(
        adapter.handle_tool_call(
            "patchbay_pro_request_respond",
            {
                "request_id": request_ref,
                "expected_revision": 2,
                "response_kind": "architecture_plan",
                "response_markdown": "# Response\n\nUse the injected boundary.",
                "worker_message_markdown": "Implement through the explicit boundary.",
                "recommended_next_action": "dispatch_to_origin_worker",
                "idempotency_key": "respond-owner-1",
            },
            context=context,
        )
    )
    assert responded["status"] == "ok"
    assert responded["result"]["response_stored"] is True
    assert responded["result"]["dispatched"] is False
    assert responded["result"]["applied"] is False
    assert responded["result"]["committed"] is False
    assert dispatcher.calls == []
    assert canonical.response_text(request_id)["response_markdown"].startswith("# Response")
    validate_hub_v2_tool_output("patchbay_pro_request_respond", responded)

    dispatched = _run(
        adapter.handle_tool_call(
            "patchbay_pro_request_dispatch",
            {
                "request_id": request_ref,
                "expected_revision": 3,
                "target": "origin_worker",
                "message_source": "worker_message_markdown",
                "idempotency_key": "dispatch-owner-1",
            },
            context=context,
        )
    )
    assert dispatched["status"] == "ok"
    assert dispatched["result"]["dispatched"] is True
    assert dispatched["result"]["dispatch_target"] == "origin_worker"
    assert dispatched["result"]["applied"] is False
    assert dispatched["result"]["committed"] is False
    assert dispatched["result"]["hidden_queueing"] is False
    assert dispatcher.calls == [
        {
            "request_id": request_id,
            "target": "origin_worker",
            "message_source": "worker_message_markdown",
        }
    ]
    child = dispatched["result"]["dispatch_operation"]
    assert child["tool_name"] == "patchbay_worker_message"
    assert child["parent_operation_id"] == dispatched["operation"]["operation_id"]
    assert dispatched["operation"]["item_results"][0]["operation_id"] == child["operation_id"]
    validate_hub_v2_tool_output("patchbay_pro_request_dispatch", dispatched)

    closed = _run(
        adapter.handle_tool_call(
            "patchbay_pro_request_close",
            {
                "request_id": request_ref,
                "expected_revision": 5,
                "status": "closed",
                "reason": "Response dispatched",
                "idempotency_key": "close-owner-1",
            },
            context=context,
        )
    )
    assert closed["status"] == "ok"
    assert closed["result"]["request"]["status"] == "closed"
    assert closed["result"]["dispatched"] is False
    assert closed["result"]["applied"] is False
    assert closed["result"]["committed"] is False
    validate_hub_v2_tool_output("patchbay_pro_request_close", closed)

    association = hub.get_entity(PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)["record"]
    assert association["principal_ref"] == hub.principal_ref
    assert association["workspace_ref"] == "workspace_patchbay"
    assert association["work_group_id"] == "group_alpha"
    assert association["lane"] == "analysis"
    assert association["origin_operation_id"] == "op_origin"
    assert set(association["action_operation_ids"]) == {
        "claim",
        "respond",
        "dispatch",
        "dispatch_target",
        "close",
    }


def test_concurrent_claim_cas_has_one_winner_and_stale_claim_is_semantic_block(tmp_path):
    canonical, first_hub, _dispatcher, first, request_id = _fixture(tmp_path)
    owner = _context("owner")
    request_ref = _run(first.list_requests(context=owner))["result"]["requests"][0]["request_ref"]

    second_hub = HubStoreV2(tmp_path / "hub.sqlite3", busy_timeout_ms=10_000)
    second = HubProRequestAdapterV2(
        second_hub,
        ProRequestStore(canonical.config),
        machine_id="machine_alpha",
        edge_generation="edgegen_alpha",
        workspace_ref="workspace_patchbay",
        work_group_id="group_alpha",
        lane="analysis",
        dispatch_executor=RecordingCanonicalDispatch(canonical),
        reference_salt="test-salt",
    )

    def claim(adapter: HubProRequestAdapterV2, name: str) -> dict[str, Any]:
        return _run(
            adapter.claim_request(
                {
                    "request_id": request_ref,
                    "expected_revision": 1,
                    "idempotency_key": f"claim-{name}",
                },
                context=_context(name),
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, adapter, name) for adapter, name in ((first, "left"), (second, "right"))]
        results = [future.result() for future in futures]

    assert sorted(result["status"] for result in results) == ["blocked", "ok"]
    blocked = next(result for result in results if result["status"] == "blocked")
    winner = next(result for result in results if result["status"] == "ok")
    assert blocked["result"]["reason"] == "stale_revision"
    assert blocked["result"]["actual_revision"] == 2
    assert winner["result"]["request"]["revision"] == 2
    assert canonical.read_request(request_id=request_id)["request"]["revision"] == 2

    stale = _run(
        first.claim_request(
            {
                "request_id": request_ref,
                "expected_revision": 1,
                "idempotency_key": "claim-explicitly-stale",
            },
            context=owner,
        )
    )
    assert stale["status"] == "blocked"
    assert stale["result"]["reason"] == "stale_revision"


def test_mutation_retry_is_stable_and_payload_conflict_does_not_repeat_storage(tmp_path):
    canonical, _hub, _dispatcher, adapter, request_id = _fixture(tmp_path)
    context = _context("owner")
    request_ref = _run(adapter.list_requests(context=context))["result"]["requests"][0]["request_ref"]
    claim = _run(
        adapter.claim_request(
            {
                "request_id": request_ref,
                "expected_revision": 1,
                "idempotency_key": "claim-retry-base",
            },
            context=context,
        )
    )
    assert claim["status"] == "ok"

    arguments = {
        "request_id": request_ref,
        "expected_revision": 2,
        "response_markdown": "Stable answer",
        "response_kind": "analysis",
        "idempotency_key": "respond-stable-key",
    }
    first = _run(adapter.respond_request(arguments, context=context))
    replay = _run(adapter.respond_request(dict(reversed(list(arguments.items()))), context=context))

    assert replay == first
    assert canonical.read_request(request_id=request_id)["request"]["revision"] == 3
    assert canonical.response_text(request_id)["response_markdown"] == "Stable answer"

    conflict = _run(
        adapter.respond_request(
            {**arguments, "response_markdown": "Different answer"},
            context=context,
        )
    )
    assert conflict["status"] == "blocked"
    assert conflict["result"]["reason"] == "idempotency_payload_conflict"
    assert canonical.read_request(request_id=request_id)["request"]["revision"] == 3
    assert canonical.response_text(request_id)["response_markdown"] == "Stable answer"


def test_dispatch_retry_targets_once_and_respond_never_crosses_dispatch_boundary(tmp_path):
    canonical, _hub, dispatcher, adapter, _request_id = _fixture(tmp_path)
    context = _context("owner")
    request_ref = _run(adapter.list_requests(context=context))["result"]["requests"][0]["request_ref"]
    _run(
        adapter.claim_request(
            {"request_id": request_ref, "expected_revision": 1, "idempotency_key": "claim-dispatch"},
            context=context,
        )
    )
    _run(
        adapter.respond_request(
            {
                "request_id": request_ref,
                "expected_revision": 2,
                "response_markdown": "Send this only when dispatch is explicit.",
                "worker_message_markdown": "Worker-specific message.",
                "idempotency_key": "respond-dispatch",
            },
            context=context,
        )
    )
    assert dispatcher.calls == []

    arguments = {
        "request_id": request_ref,
        "expected_revision": 3,
        "target": "new_worker",
        "new_worker_name": "Pro Implementer",
        "workspace_mode": "read_only",
        "message_source": "response_markdown",
        "idempotency_key": "dispatch-stable-key",
    }
    first = _run(adapter.dispatch_request(arguments, context=context))
    retry = _run(adapter.dispatch_request(arguments, context=context))

    assert retry == first
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["target"] == "new_worker"
    assert dispatcher.calls[0]["new_worker_name"] == "Pro Implementer"
    assert dispatcher.calls[0]["workspace_mode"] == "read_only"
    assert first["result"]["dispatch_operation"]["tool_name"] == "patchbay_worker_start"
    assert canonical.read_request(request_id=dispatcher.calls[0]["request_id"])["request"]["revision"] == 5


def test_private_and_shared_visibility_coordinate_by_participant_and_group(tmp_path):
    _canonical, _hub, _dispatcher, private, _request_id = _fixture(tmp_path / "private")
    owner = _context("owner", group="group_alpha")
    same_group = _context("peer", group="group_alpha")
    other_group = _context("other", group="group_beta")

    owner_list = _run(private.list_requests(context=owner))
    request_ref = owner_list["result"]["requests"][0]["request_ref"]
    peer_list = _run(private.list_requests(context=same_group))
    hidden_list = _run(private.list_requests(context=other_group))
    hidden_read = _run(private.read_request({"request_id": request_ref}, context=other_group))

    assert peer_list["result"]["requests"][0]["request_ref"] == request_ref
    assert hidden_list["result"]["requests"] == []
    assert hidden_list["result"]["hidden_count"] == 1
    assert hidden_read["status"] == "not_found"

    _canonical, _hub, _dispatcher, shared, _request_id = _fixture(tmp_path / "shared", visibility="shared")
    shared_ref = _run(shared.list_requests(context=owner))["result"]["requests"][0]["request_ref"]
    shared_list = _run(shared.list_requests(context=other_group))
    shared_read = _run(shared.read_request({"request_id": shared_ref}, context=other_group))

    assert shared_list["result"]["requests"][0]["request_ref"] == shared_ref
    assert shared_read["status"] == "ok"


def test_machine_generation_qualification_and_missing_dispatch_executor(tmp_path):
    canonical, hub, _dispatcher, first, _request_id = _fixture(tmp_path)
    context = _context("owner")
    first_ref = _run(first.list_requests(context=context))["result"]["requests"][0]["request_ref"]
    second = HubProRequestAdapterV2(
        hub,
        canonical,
        machine_id="machine_alpha",
        edge_generation="edgegen_second",
        workspace_ref="workspace_patchbay",
        work_group_id="group_alpha",
        lane="analysis",
        visibility="shared",
        reference_salt="test-salt",
    )
    second_ref = _run(second.list_requests(context=context))["result"]["requests"][0]["request_ref"]
    assert second_ref != first_ref

    blocked = _run(
        second.dispatch_request(
            {
                "request_id": second_ref,
                "target": "origin_worker",
                "idempotency_key": "dispatch-without-executor",
            },
            context=context,
        )
    )
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "dispatch_executor_unavailable"
    assert blocked["result"]["dispatched"] is False


def test_dispatch_exception_is_unknown_and_same_key_never_reexecutes(tmp_path):
    canonical, hub, _dispatcher, adapter, _request_id = _fixture(tmp_path)
    context = _context("owner")
    request_ref = _run(adapter.list_requests(context=context))["result"]["requests"][0]["request_ref"]
    calls: list[dict[str, Any]] = []

    async def uncertain(arguments: Mapping[str, Any], **_kwargs: Any) -> Mapping[str, Any]:
        calls.append(dict(arguments))
        raise RuntimeError("connection lost after send")

    failing = HubProRequestAdapterV2(
        hub,
        canonical,
        machine_id="machine_alpha",
        edge_generation="edgegen_alpha",
        workspace_ref="workspace_patchbay",
        work_group_id="group_alpha",
        lane="analysis",
        dispatch_executor=uncertain,
        reference_salt="test-salt",
    )
    arguments = {
        "request_id": request_ref,
        "target": "origin_worker",
        "idempotency_key": "uncertain-dispatch",
    }
    first = _run(failing.dispatch_request(arguments, context=context))
    replay = _run(failing.dispatch_request(arguments, context=context))

    assert first["status"] == "pending"
    assert first["operation"]["state"] == "outcome_unknown"
    assert replay["status"] == "pending"
    assert replay["operation"]["operation_id"] == first["operation"]["operation_id"]
    assert len(calls) == 1
    assert first["next_actions"][0]["tool"] == "patchbay_operation_status"


def test_adapter_is_adjacent_and_not_wired_into_hub_package() -> None:
    import patchbay.hub as hub_package

    assert not hasattr(hub_package, "HubProRequestAdapterV2")


class _InProcessHubTransport:
    def __init__(self, bridge: Any):
        self.bridge = bridge
        self.methods = {
            DEFAULT_ENDPOINTS.heartbeat: "edge_heartbeat",
            DEFAULT_ENDPOINTS.claim: "edge_claim",
            DEFAULT_ENDPOINTS.renew_lease: "edge_lease",
            DEFAULT_ENDPOINTS.result: "edge_result",
            DEFAULT_ENDPOINTS.outbox_ack: "edge_outbox_ack",
            DEFAULT_ENDPOINTS.projection: "edge_projection",
            DEFAULT_ENDPOINTS.reconcile: "edge_reconcile",
        }

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str = "",
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        del timeout_seconds
        callback = getattr(self.bridge, self.methods[path])
        result = callback(payload, token=token)
        return await result if inspect.isawaitable(result) else result


class _DispatchOnlyWorkerRuntime:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def projection_snapshot(
        self, *, previous_edge_worker_ids: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "snapshot_version": 2,
            "snapshot_kind": "full",
            "workers": [],
            "tombstones": [
                {"edge_worker_id": worker_id}
                for worker_id in previous_edge_worker_ids or []
            ],
            "present_edge_worker_ids": [],
        }

    async def message_worker(self, **kwargs: Any) -> dict[str, Any]:
        self.messages.append(dict(kwargs))
        return {
            "accepted": True,
            "name": str(kwargs.get("worker") or ""),
            "message_delivered": True,
        }


async def _wait_for_terminal_operation(app: Any, operation_id: str) -> None:
    async def wait() -> None:
        while True:
            operation = app.store.get_operation(operation_id)
            if operation and operation["state"] in {
                "succeeded",
                "blocked",
                "failed",
                "cancelled",
            }:
                return
            await asyncio.sleep(0.005)

    # The production-shaped Edge loop may share a small CI/VM CPU. Keep this
    # bounded, but do not turn scheduler contention into a false failure.
    await asyncio.wait_for(wait(), timeout=15)


@pytest.mark.asyncio
async def test_production_hub_routes_remote_edge_pro_requests_end_to_end(tmp_path):
    repo = _init_repo(tmp_path / "edge-repo")
    edge_config = _config(tmp_path / "edge", repo)
    edge_config.update(
        {
            "server": {
                "max_concurrent_jobs": 2,
                "job_timeout_seconds": 30,
                "job_cleanup_after_hours": 24,
            },
            "logging": {
                "job_logs_dir": str(tmp_path / "edge-logs" / "jobs"),
                "job_state_dir": str(tmp_path / "edge-logs" / "state"),
            },
            "locks": {"root": str(tmp_path / "edge-locks")},
            "power_tools": {
                "direct_write": False,
                "bash_mode": "off",
                "bash_transcript": "compact",
                "bash_session_id": "",
                "require_bash_session": False,
                "bash_timeout_ms": 30_000,
                "bash_max_output_bytes": 20_000,
            },
        }
    )
    edge_config["security"].update(
        {
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": [],
        }
    )
    edge_config["hub"] = {
        "control_plane": "v2",
        "edge": {"journal_file": str(tmp_path / "edge-journal.sqlite3")},
    }
    hub_config = {
        "hub": {
            "control_plane": "v2",
            "state_db": str(tmp_path / "hub.sqlite3"),
                "semantic_wait_seconds": 0.0,
        },
        "auth": {"enabled": False},
    }
    bridge = create_production_hub_v2_app(hub_config)
    app = bridge.app
    assert isinstance(app.pro_request_adapter, FleetHubProRequestAdapterV2)
    assert app.pro_store_bridge is None
    assert app.registered_tools == HUB_V2_TOOL_NAMES
    assert len(app.registered_tools) == HUB_V2_EXPECTED_TOOL_COUNT == 31

    machine_id = "edge-pro-requests"
    generation = "edgegen-pro-requests"
    enrollment = app.runtime.create_enrollment_code(name="Pro Request Edge")
    enrolled = bridge.edge_enroll(
        {
            "code": enrollment["code"],
            "machine_id": machine_id,
            "display_name": "Pro Request Edge",
            "edge_generation": generation,
            "capabilities": build_capabilities(edge_config),
            "workspaces": build_workspaces(edge_config),
        }
    )

    manager = JobManager(edge_config)
    handler = ToolHandler(edge_config, manager, JobExecutor(edge_config, manager))
    workers = _DispatchOnlyWorkerRuntime()
    handler.worker_runtime = workers
    report = tmp_path / "private-report.md"
    report.write_text(
        "# Private escalation\n\nSECRET-PROJECTION-SENTINEL needs a fleet answer.\n",
        encoding="utf-8",
    )
    created = handler.pro_request_store.create_request(
        repo_path=str(repo),
        title="PRIVATE-TITLE-SENTINEL",
        origin_kind="terminal_codex",
        origin_worker="Origin Worker",
        report_path=str(report),
        desired_output="PRIVATE-DESIRED-OUTPUT-SENTINEL",
    )

    journal = EdgeJournal(tmp_path / "edge-journal.sqlite3", edge_generation=generation)
    execution = EdgeExecutionService(
        handler,
        journal,
        machine_id=machine_id,
        edge_generation=generation,
        config=edge_config,
    )
    runner = EdgeV2Runner(
        execution,
        config=edge_config,
        profile=EdgeV2Profile(
            hub_url="https://in-process.invalid",
            machine_id=machine_id,
            node_token=enrolled["node_token"],
            edge_generation=generation,
        ),
        transport=_InProcessHubTransport(bridge),
        heartbeat_interval_seconds=0.01,
        claim_interval_seconds=0.005,
        result_retry_seconds=0.005,
        reconciliation_interval_seconds=0.02,
        lease_renewal_seconds=0.01,
        shutdown_timeout_seconds=1,
    )
    context = _context("fleet-owner", group="", lane="")
    run_task = runner.start()
    try:
        await runner.projection_once()
        associations = app.store.list_entities(PRO_REQUEST_ASSOCIATION_ENTITY)
        assert len(associations) == 1
        assert associations[0]["record"]["workspace_ref"].startswith("workspace_")
        projected_json = str(associations[0]["record"])
        assert "PRIVATE-TITLE-SENTINEL" not in projected_json
        assert "SECRET-PROJECTION-SENTINEL" not in projected_json
        assert "PRIVATE-DESIRED-OUTPUT-SENTINEL" not in projected_json
        assert "report_markdown" not in projected_json
        assert "response_markdown" not in projected_json

        group_args = {
            "title": "Remote Pro Request",
            "goal": "Resolve the Edge-local blocked request.",
            "workspace_ref": associations[0]["record"]["workspace_ref"],
            "machine_id": machine_id,
            "idempotency_key": "fleet-pro-group",
        }
        group = await app.handle_tool_call(
            "patchbay_work_group_create", group_args, context=context
        )
        await _wait_for_terminal_operation(app, group["operation"]["operation_id"])
        group = await app.handle_tool_call(
            "patchbay_work_group_create", group_args, context=context
        )
        group_id = group["result"]["work_group"]["work_group_id"]

        listed = await app.handle_tool_call(
            "patchbay_pro_request_list",
            {"work_group_id": group_id, "limit": 10},
            context=context,
        )
        assert listed["status"] == "ok"
        assert listed["result"]["private_content_projected"] is False
        validate_hub_v2_tool_output("patchbay_pro_request_list", listed)
        request_ref = listed["result"]["requests"][0]["request_ref"]
        assert request_ref != created["id"]

        read_args = {
            "request_id": request_ref,
            "work_group_id": group_id,
            "include_report": True,
        }
        read = await app.handle_tool_call(
            "patchbay_pro_request_read", read_args, context=context
        )
        if read["status"] == "pending":
            await asyncio.sleep(0)
            assert not run_task.done(), repr(run_task.exception())
            await _wait_for_terminal_operation(app, read["operation"]["operation_id"])
            completed_read = await app.handle_tool_call(
                "patchbay_operation_status",
                {
                    "operation_id": read["operation"]["operation_id"],
                    "include_result": True,
                },
                context=context,
            )
            assert completed_read["status"] == "ok"
            read_result = completed_read["result"]["domain_result"]
            validate_hub_v2_tool_output("patchbay_operation_status", completed_read)
        else:
            read_result = read["result"]
            validate_hub_v2_tool_output("patchbay_pro_request_read", read)
        assert "SECRET-PROJECTION-SENTINEL" in read_result["report_markdown"]
        assert read_result["machine_id"] == machine_id
        assert read_result["edge_generation"] == generation

        async def mutate(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            arguments = {**arguments, "work_group_id": group_id}
            first = await app.handle_tool_call(tool, arguments, context=context)
            operation_id = first["operation"]["operation_id"]
            await _wait_for_terminal_operation(app, operation_id)
            return await app.handle_tool_call(tool, arguments, context=context)

        claimed = await mutate(
            "patchbay_pro_request_claim",
            {
                "request_id": request_ref,
                "expected_revision": 1,
                "idempotency_key": "fleet-claim",
            },
        )
        responded = await mutate(
            "patchbay_pro_request_respond",
            {
                "request_id": request_ref,
                "expected_revision": 2,
                "response_markdown": "PRIVATE-RESPONSE-SENTINEL",
                "worker_message_markdown": "Proceed from the fleet response.",
                "idempotency_key": "fleet-respond",
            },
        )
        dispatched = await mutate(
            "patchbay_pro_request_dispatch",
            {
                "request_id": request_ref,
                "expected_revision": 3,
                "target": "origin_worker",
                "idempotency_key": "fleet-dispatch",
            },
        )
        closed = await mutate(
            "patchbay_pro_request_close",
            {
                "request_id": request_ref,
                "expected_revision": 5,
                "status": "closed",
                "reason": "Handled on owning Edge",
                "idempotency_key": "fleet-close",
            },
        )

        assert claimed["status"] == "ok"
        assert responded["result"]["response_stored"] is True
        assert responded["result"]["dispatched"] is False
        assert dispatched["result"]["dispatched"] is True
        assert dispatched["result"]["applied"] is False
        assert dispatched["result"]["committed"] is False
        assert workers.messages[0]["worker"] == "Origin Worker"
        assert workers.messages[0]["message"] == "Proceed from the fleet response."
        assert closed["result"]["request"]["status"] == "closed"
        validate_hub_v2_tool_output("patchbay_pro_request_claim", claimed)
        validate_hub_v2_tool_output("patchbay_pro_request_respond", responded)
        validate_hub_v2_tool_output("patchbay_pro_request_dispatch", dispatched)
        validate_hub_v2_tool_output("patchbay_pro_request_close", closed)
        assert handler.pro_request_store.read_request(
            request_id=created["id"], include_report=False
        )["request"]["revision"] == 6

        await runner.projection_once()
        final_projection = app.store.list_entities(PRO_REQUEST_ASSOCIATION_ENTITY)[0][
            "record"
        ]
        assert final_projection["status"] == "closed"
        assert "PRIVATE-RESPONSE-SENTINEL" not in str(final_projection)
    finally:
        await runner.shutdown(cancel_active=True)
        await run_task
        bridge.close()
