from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

from patchbay.hub.edge import build_capabilities
from patchbay.hub.edge_client_v2 import (
    DEFAULT_ENDPOINTS,
    EdgeV2Profile,
    EdgeV2Runner,
    edge_contract_metadata,
)
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.edge_v2 import EdgeExecutionService
from patchbay.protocol.context import RequestContext


MACHINE_ID = "edge-client-test"
EDGE_GENERATION = "edgegen_client_test"
ACTION = "codex_worker_start"


class FakeWorkerRuntime:
    def __init__(self) -> None:
        self.workers: list[dict[str, Any]] = []

    def projection_snapshot(
        self,
        *,
        previous_edge_worker_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        previous = list(previous_edge_worker_ids or [])
        present = [str(worker["edge_worker_id"]) for worker in self.workers]
        return {
            "snapshot_version": 2,
            "snapshot_kind": "full",
            "full_history": True,
            "complete_worker_set": True,
            "omission_means_tombstone": True,
            "previous_edge_worker_ids": previous,
            "present_edge_worker_ids": present,
            "workers": deepcopy(self.workers),
            "tombstones": [
                {"edge_worker_id": worker_id}
                for worker_id in previous
                if worker_id not in present
            ],
        }


class FakeToolHandler:
    def __init__(self, *, block: bool = False) -> None:
        self.config: dict[str, Any] = {}
        self.worker_runtime = FakeWorkerRuntime()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.effects = 0
        self.active = 0
        self.maximum_active = 0
        self.calls: list[dict[str, Any]] = []

    async def handle_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            self.calls.append(
                {
                    "tool_name": tool_name,
                    "arguments": deepcopy(arguments),
                    "context": context,
                }
            )
            self.started.set()
            await self.release.wait()
            self.effects += 1
            worker_id = f"worker-{self.effects}"
            self.worker_runtime.workers = [
                {"edge_worker_id": worker_id, "name": arguments.get("name", "Reader")}
            ]
            return {"accepted": True, "worker_id": worker_id}
        finally:
            self.active -= 1


class FakeHttpTransport:
    def __init__(self) -> None:
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.claims: list[dict[str, Any]] = []
        self.repeat_claim: dict[str, Any] | None = None
        self.result_failures = 0
        self.result_acknowledgements = True
        self.reconciliation_responses: list[dict[str, Any]] = []

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        del timeout_seconds
        saved = deepcopy(dict(payload))
        saved["_token"] = token
        self.calls[path].append(saved)
        required = {
            DEFAULT_ENDPOINTS.heartbeat: (
                "machine_id",
                "edge_generation",
                "projection_revision",
            ),
            DEFAULT_ENDPOINTS.claim: (
                "machine_id",
                "edge_generation",
                "contract_hash",
            ),
            DEFAULT_ENDPOINTS.renew_lease: (
                "machine_id",
                "edge_generation",
                "operation_id",
                "attempt_id",
                "contract_hash",
                "fencing_token",
                "expected_revision",
            ),
            DEFAULT_ENDPOINTS.result: ("machine_id", "edge_generation", "receipt"),
            DEFAULT_ENDPOINTS.outbox_ack: (
                "machine_id",
                "edge_generation",
                "receipt_ids",
            ),
            DEFAULT_ENDPOINTS.projection: (
                "machine_id",
                "edge_generation",
                "projection_revision",
                "projection",
            ),
            DEFAULT_ENDPOINTS.reconcile: (
                "machine_id",
                "edge_generation",
                "operation_id",
                "attempt_id",
            ),
        }.get(path, ())
        for field in required:
            assert payload.get(field) not in (None, ""), f"{path} missing {field}"
        if path == DEFAULT_ENDPOINTS.heartbeat:
            return {"accepted": True}
        if path == DEFAULT_ENDPOINTS.projection:
            return {
                "accepted": True,
                "projection_accepted": True,
                "current_projection_revision": payload["projection_revision"],
            }
        if path == DEFAULT_ENDPOINTS.claim:
            if self.claims:
                return {"attempt": deepcopy(self.claims.pop(0))}
            if self.repeat_claim is not None:
                return {"attempt": deepcopy(self.repeat_claim)}
            return {"attempt": None}
        if path == DEFAULT_ENDPOINTS.renew_lease:
            return {
                "accepted": True,
                "attempt": {
                    "revision": int(payload.get("expected_revision") or 1) + 1,
                    "lease_expires_at": 0,
                    "state": "executing",
                },
            }
        if path == DEFAULT_ENDPOINTS.result:
            if self.result_failures:
                self.result_failures -= 1
                raise OSError("simulated lost result response")
            if not self.result_acknowledgements:
                return {"accepted": True}
            receipt = payload["receipt"]
            return {
                "accepted": True,
                "receipt_acknowledgements": [
                    {
                        key: receipt[key]
                        for key in (
                            "receipt_id",
                            "operation_id",
                            "attempt_id",
                            "fencing_token",
                            "edge_generation",
                        )
                    }
                ],
            }
        if path == DEFAULT_ENDPOINTS.outbox_ack:
            return {
                "accepted": True,
                "acknowledged_receipts": list(payload["receipt_ids"]),
            }
        if path == DEFAULT_ENDPOINTS.reconcile:
            response = {"found": True}
            self.reconciliation_responses.append(
                {
                    "request": saved,
                    "response": response,
                }
            )
            return response
        raise AssertionError(f"Unexpected fake HTTP path: {path}")


def _capabilities() -> dict[str, Any]:
    capabilities = build_capabilities({})
    assert capabilities["contract_hash"]
    assert capabilities["action_capabilities"].get(ACTION)
    return capabilities


def _attempt(
    capabilities: Mapping[str, Any],
    *,
    operation_id: str = "op-client-1",
    attempt_id: str = "attempt-client-1",
    contract_hash: str | None = None,
) -> dict[str, Any]:
    metadata = edge_contract_metadata(
        capabilities,
        edge_generation=EDGE_GENERATION,
    )
    arguments = {
        "name": "Reader",
        "brief": "Inspect the repository",
        "repo_path": "repo",
        "work_group_id": "group-client",
        "lane": "research",
    }
    required_hash = metadata["contract_hash"] if contract_hash is None else contract_hash
    return {
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "fencing_token": 1,
        "revision": 1,
        "lease_expires_at": 0,
        "machine_id": MACHINE_ID,
        "edge_generation": EDGE_GENERATION,
        "tool_name": "patchbay_worker_start",
        "action": ACTION,
        "target_key": "worker_name:repo:Reader",
        "arguments": arguments,
        "payload": arguments,
        "idempotency_key": f"key-{operation_id}",
        "contract_hash": required_hash,
        "required_contract_hash": required_hash,
        "required_action_capability_version": metadata["action_capabilities"][ACTION],
        "requirements": metadata,
        "context": {
            "owner_ref": "owner-client",
            "chatgpt_session_ref": "conversation-client",
            "work_run_ref": "run-client",
        },
        "work_group_id": "group-client",
        "lane_id": "lane-research",
    }


def _service(
    path: Path,
    handler: FakeToolHandler,
    capabilities: Mapping[str, Any],
) -> tuple[EdgeJournal, EdgeExecutionService]:
    journal = EdgeJournal(path, edge_generation=EDGE_GENERATION)
    execution = EdgeExecutionService(
        handler,
        journal,
        machine_id=MACHINE_ID,
        capabilities=capabilities,
    )
    return journal, execution


def _runner(
    execution: EdgeExecutionService,
    transport: FakeHttpTransport,
    **overrides: Any,
) -> EdgeV2Runner:
    options = {
        "heartbeat_interval_seconds": 0.01,
        "claim_interval_seconds": 0.01,
        "result_retry_seconds": 0.01,
        "reconciliation_interval_seconds": 0.02,
        "lease_renewal_seconds": 0.01,
        "shutdown_timeout_seconds": 1,
        "max_concurrent_tasks": 2,
    }
    options.update(overrides)
    return EdgeV2Runner(
        execution,
        config={},
        profile=EdgeV2Profile(
            hub_url="https://hub.example",
            machine_id=MACHINE_ID,
            node_token="node-test-token",
            edge_generation=EDGE_GENERATION,
        ),
        transport=transport,
        **options,
    )


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_long_task_keeps_heartbeat_projection_and_lease_renewal_independent(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler(block=True)
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    transport.claims.append(_attempt(capabilities))
    runner = _runner(execution, transport)

    run_task = runner.start()
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    await asyncio.sleep(0.07)

    assert len(transport.calls[DEFAULT_ENDPOINTS.heartbeat]) >= 3
    assert len(transport.calls[DEFAULT_ENDPOINTS.renew_lease]) >= 2
    assert transport.calls[DEFAULT_ENDPOINTS.claim][0]["lease_seconds"] >= 30
    assert transport.calls[DEFAULT_ENDPOINTS.renew_lease][0]["lease_seconds"] >= 30
    assert handler.effects == 0
    heartbeat = transport.calls[DEFAULT_ENDPOINTS.heartbeat][-1]
    assert heartbeat["contract_hash"] == capabilities["contract_hash"]
    assert heartbeat["edge_generation"] == EDGE_GENERATION
    assert heartbeat["projection_revision"] >= 2
    assert len(transport.calls[DEFAULT_ENDPOINTS.projection]) >= 3

    handler.release.set()
    await _wait_until(lambda: bool(transport.calls[DEFAULT_ENDPOINTS.result]))
    await runner.shutdown()
    await asyncio.wait_for(run_task, timeout=1)

    assert handler.effects == 1
    assert journal.list_pending_outbox() == []
    assert runner.active_task_count == 0
    journal.close()


@pytest.mark.asyncio
async def test_cancelled_projection_child_is_supervised_and_restarted(tmp_path: Path) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    runner = _runner(execution, transport)

    run_task = runner.start()
    await _wait_until(lambda: len(transport.calls[DEFAULT_ENDPOINTS.projection]) >= 2)
    projection_child = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "patchbay-edge-v2-projection"
    )
    projection_child.cancel()
    before = len(transport.calls[DEFAULT_ENDPOINTS.projection])

    await _wait_until(lambda: len(transport.calls[DEFAULT_ENDPOINTS.projection]) > before)
    health = journal.control_loop_health("projection")
    assert health["restart_count"] >= 1
    assert health["last_success_revision"] >= 1
    assert len(transport.calls[DEFAULT_ENDPOINTS.heartbeat]) >= 1

    await runner.shutdown()
    await asyncio.wait_for(run_task, timeout=1)
    journal.close()


@pytest.mark.asyncio
async def test_lost_result_is_replayed_from_outbox_after_restart(tmp_path: Path) -> None:
    capabilities = _capabilities()
    path = tmp_path / "edge.sqlite3"
    first_handler = FakeToolHandler()
    first_journal, first_execution = _service(path, first_handler, capabilities)
    first_transport = FakeHttpTransport()
    first_transport.claims.append(_attempt(capabilities))
    first_transport.result_failures = 20
    first_runner = _runner(first_execution, first_transport)

    await first_runner.run_once()
    assert first_handler.effects == 1
    pending = first_journal.list_pending_outbox()
    assert len(pending) == 1
    receipt_id = pending[0]["receipt_id"]
    await first_runner.shutdown()
    first_journal.close()

    restarted_handler = FakeToolHandler()
    restarted_journal, restarted_execution = _service(
        path, restarted_handler, capabilities
    )
    restarted_transport = FakeHttpTransport()
    restarted_runner = _runner(restarted_execution, restarted_transport)

    await restarted_runner.run_once()

    assert restarted_handler.effects == 0
    assert restarted_journal.list_pending_outbox() == []
    uploaded = restarted_transport.calls[DEFAULT_ENDPOINTS.result]
    assert uploaded[0]["receipt_id"] == receipt_id
    await restarted_runner.shutdown()
    restarted_journal.close()


@pytest.mark.asyncio
async def test_execution_service_tasks_are_bounded_while_claim_loop_stays_live(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler(block=True)
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    for index, name in enumerate(("Reader", "Writer", "Reviewer"), start=1):
        attempt = _attempt(
            capabilities,
            operation_id=f"op-bounded-{index}",
            attempt_id=f"attempt-bounded-{index}",
        )
        attempt["arguments"]["name"] = name
        attempt["target_key"] = f"worker_name:repo:{name}"
        transport.claims.append(attempt)
    runner = _runner(execution, transport, max_concurrent_tasks=2)

    run_task = runner.start()
    await _wait_until(lambda: len(handler.calls) == 2)
    await asyncio.sleep(0.04)

    assert handler.maximum_active == 2
    assert runner.active_task_count == 2
    assert len(transport.calls[DEFAULT_ENDPOINTS.claim]) == 2
    assert len(transport.calls[DEFAULT_ENDPOINTS.heartbeat]) >= 2

    handler.release.set()
    await _wait_until(lambda: handler.effects == 3)
    await runner.shutdown()
    await asyncio.wait_for(run_task, timeout=1)

    assert handler.maximum_active == 2
    assert runner.active_task_count == 0
    journal.close()


@pytest.mark.asyncio
async def test_duplicate_claim_never_duplicates_local_toolhandler_effect(tmp_path: Path) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler(block=True)
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    transport.repeat_claim = _attempt(capabilities)
    runner = _runner(execution, transport, max_concurrent_tasks=4)

    run_task = runner.start()
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    await asyncio.sleep(0.06)

    assert len(transport.calls[DEFAULT_ENDPOINTS.claim]) >= 3
    assert len(handler.calls) == 1
    assert runner.active_task_count == 1

    handler.release.set()
    await _wait_until(lambda: bool(transport.calls[DEFAULT_ENDPOINTS.result]))
    await runner.shutdown()
    await asyncio.wait_for(run_task, timeout=1)

    assert handler.effects == 1
    journal.close()


@pytest.mark.asyncio
async def test_restart_resumes_safe_intent_but_reconciles_effect_boundary(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()

    safe_path = tmp_path / "safe.sqlite3"
    original_handler = FakeToolHandler()
    original_journal, original_execution = _service(
        safe_path, original_handler, capabilities
    )
    attempt = _attempt(capabilities, operation_id="op-safe", attempt_id="attempt-safe")
    plan = original_execution.validate_attempt(attempt)
    original_journal.record_intent(
        operation_id=plan["operation_id"],
        attempt_id=plan["attempt_id"],
        fencing_token=plan["fencing_token"],
        action=plan["action"],
        target_key=plan["target_key"],
        payload=plan["payload"],
        payload_hash=plan["payload_hash"],
        edge_generation=EDGE_GENERATION,
        idempotency_key=plan["idempotency_key"],
        correlation=plan["correlation"],
    )
    original_journal.close()

    recovered_handler = FakeToolHandler()
    recovered_journal, recovered_execution = _service(
        safe_path, recovered_handler, capabilities
    )
    recovered_transport = FakeHttpTransport()
    recovered_runner = _runner(recovered_execution, recovered_transport)

    await recovered_runner.run_once()

    assert recovered_handler.effects == 1
    assert recovered_journal.get_attempt("attempt-safe")["state"] == "acknowledged"
    await recovered_runner.shutdown()
    recovered_journal.close()

    uncertain_path = tmp_path / "uncertain.sqlite3"
    uncertain_handler = FakeToolHandler()
    uncertain_journal, uncertain_execution = _service(
        uncertain_path, uncertain_handler, capabilities
    )
    uncertain_attempt = _attempt(
        capabilities,
        operation_id="op-uncertain",
        attempt_id="attempt-uncertain",
    )
    uncertain_plan = uncertain_execution.validate_attempt(uncertain_attempt)
    uncertain_journal.record_intent(
        operation_id=uncertain_plan["operation_id"],
        attempt_id=uncertain_plan["attempt_id"],
        fencing_token=uncertain_plan["fencing_token"],
        action=uncertain_plan["action"],
        target_key=uncertain_plan["target_key"],
        payload=uncertain_plan["payload"],
        edge_generation=EDGE_GENERATION,
        idempotency_key=uncertain_plan["idempotency_key"],
        correlation=uncertain_plan["correlation"],
    )
    uncertain_journal.mark_attempt_executing(
        uncertain_plan["operation_id"],
        uncertain_plan["attempt_id"],
        uncertain_plan["fencing_token"],
        edge_generation=EDGE_GENERATION,
    )
    uncertain_journal.close()

    no_replay_handler = FakeToolHandler()
    restarted_journal, restarted_execution = _service(
        uncertain_path, no_replay_handler, capabilities
    )
    reconciliation_transport = FakeHttpTransport()
    restarted_runner = _runner(restarted_execution, reconciliation_transport)

    await restarted_runner.run_once()

    assert no_replay_handler.effects == 0
    reports = reconciliation_transport.calls[DEFAULT_ENDPOINTS.reconcile]
    assert any(
        report.get("attempt_id") == "attempt-uncertain"
        and report["local_recovery"].get("needs_reconciliation") is True
        for report in reports
    )
    await restarted_runner.shutdown()
    restarted_journal.close()


@pytest.mark.asyncio
async def test_incompatible_contract_is_rejected_before_intent_or_execution(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    transport.claims.append(_attempt(capabilities, contract_hash="wrong-contract"))
    runner = _runner(execution, transport)

    result = await runner.run_once()

    assert result["claim"]["rejected_attempts"] == 1
    assert handler.effects == 0
    assert journal.get_intent("op-client-1") is None
    reports = transport.calls[DEFAULT_ENDPOINTS.reconcile]
    assert any(
        report["local_recovery"].get("reason") == "edge_contract_mismatch"
        for report in reports
    )
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_clean_shutdown_stops_intake_drains_task_and_leaves_no_runner_tasks(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler(block=True)
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    transport.claims.append(_attempt(capabilities))
    runner = _runner(execution, transport)

    run_task = runner.start()
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    shutdown_task = asyncio.create_task(runner.shutdown(cancel_active=False))
    await asyncio.sleep(0.03)

    assert not shutdown_task.done()
    claims_before_release = len(transport.calls[DEFAULT_ENDPOINTS.claim])
    handler.release.set()
    await asyncio.wait_for(shutdown_task, timeout=1)
    await asyncio.wait_for(run_task, timeout=1)

    assert handler.effects == 1
    assert runner.closed is True
    assert runner.active_task_count == 0
    assert runner.control_task_count == 0
    assert runner._lease_tasks == {}
    assert len(transport.calls[DEFAULT_ENDPOINTS.claim]) == claims_before_release
    assert journal.list_pending_outbox() == []
    journal.close()


def test_enrollment_profile_and_contract_metadata_are_generation_compatible() -> None:
    normalized = EdgeV2Profile.from_mapping(
        {
            "profile": {
                "hub_url": "https://hub.example/mcp",
                "machine_id": MACHINE_ID,
                "node_token": "node-test",
            },
            "machine": {
                "machine_id": MACHINE_ID,
                "edge_generation": EDGE_GENERATION,
            },
        }
    )
    capabilities = _capabilities()
    metadata = edge_contract_metadata(
        capabilities,
        edge_generation=normalized.edge_generation,
    )

    assert normalized.hub_url == "https://hub.example"
    assert normalized.edge_generation == EDGE_GENERATION
    assert metadata["contract_hash"] == capabilities["contract_hash"]
    assert metadata["action_capabilities"] == metadata[
        "action_capability_versions"
    ]
