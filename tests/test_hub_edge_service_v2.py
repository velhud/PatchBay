from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

from patchbay.hub.edge_journal import (
    RECOVERY_RECONCILE_EFFECT,
    EdgeJournal,
)
from patchbay.hub.edge_v2 import EdgeAttemptFenceError, EdgeExecutionService
from patchbay.protocol.context import RequestContext
from patchbay.tools.errors import WorkerNameConflict


EDGE_GENERATION = "edgegen-service-test"
MACHINE_ID = "edge-service-test"
CONTRACT_HASH = "contract-service-v2"
ACTION_VERSION = "2"
ACTION = "codex_worker_start"


class SimulatedProcessCrash(BaseException):
    pass


class FakeWorkerRuntime:
    def __init__(self) -> None:
        self.workers = [{"edge_worker_id": "worker-1", "name": "Reader"}]
        self.previous_worker_ids: list[list[str]] = []

    def projection_snapshot(
        self,
        *,
        previous_edge_worker_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        previous = sorted(previous_edge_worker_ids or [])
        self.previous_worker_ids.append(previous)
        present = sorted(str(worker["edge_worker_id"]) for worker in self.workers)
        return {
            "snapshot_version": 2,
            "snapshot_kind": "full",
            "full_history": True,
            "complete_worker_set": True,
            "omission_means_tombstone": True,
            "previous_edge_worker_ids": previous,
            "present_edge_worker_ids": present,
            "tombstones": [
                {"edge_worker_id": worker_id}
                for worker_id in sorted(set(previous) - set(present))
            ],
            "workers": deepcopy(self.workers),
            "content_revision": "sha256:fake-projection",
            "content_sha256": "fake-projection",
        }

class FakeToolHandler:
    def __init__(
        self,
        *,
        result: Mapping[str, Any] | None = None,
        crash_after_effect: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.result = dict(result or {"accepted": True, "worker_id": "worker-1"})
        self.crash_after_effect = crash_after_effect
        self.delay = delay
        self.worker_runtime = FakeWorkerRuntime()
        self.calls: list[dict[str, Any]] = []
        self.effects = 0
        self.active = 0
        self.maximum_active = 0
        self.refusal: Exception | None = None

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
            if self.delay:
                await asyncio.sleep(self.delay)
            self.effects += 1
            if self.refusal is not None:
                self.effects -= 1
                raise self.refusal
            if self.crash_after_effect:
                raise SimulatedProcessCrash("process stopped after domain effect")
            return deepcopy(self.result)
        finally:
            self.active -= 1


def capabilities() -> dict[str, Any]:
    return {
        "protocol_version": "2",
        "contract_version": "HUB-MANAGER-CONTROL-PLANE-V2",
        "contract_hash": CONTRACT_HASH,
        "manifest_hash": "manifest-service-v2",
        "schema_hash": "schema-service-v2",
        "action_capability_version": ACTION_VERSION,
        "action_capabilities": {ACTION: ACTION_VERSION},
        "action_capability_versions": {ACTION: ACTION_VERSION},
    }


def operation_attempt(
    *,
    operation_id: str = "op-service-1",
    attempt_id: str = "attempt-service-1",
    fencing_token: int = 1,
    name: str = "Reader",
    target_key: str = "worker-name:repo:Reader",
) -> dict[str, Any]:
    arguments = {
        "name": name,
        "brief": f"Run {name}",
        "repo_path": "repo",
        "work_group_id": "group-service",
        "lane": "research",
    }
    return {
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "fencing_token": fencing_token,
        "machine_id": MACHINE_ID,
        "edge_generation": EDGE_GENERATION,
        "tool_name": "patchbay_worker_start",
        "action": ACTION,
        "target_key": target_key,
        "arguments": arguments,
        "payload": arguments,
        "idempotency_key": f"key-{operation_id}",
        "required_contract_hash": CONTRACT_HASH,
        "required_action_capability_version": ACTION_VERSION,
        "requirements": {
            "protocol_version": "2",
            "contract_version": "HUB-MANAGER-CONTROL-PLANE-V2",
            "manifest_hash": "manifest-service-v2",
            "schema_hash": "schema-service-v2",
            "edge_generation": EDGE_GENERATION,
            "action_capabilities": {ACTION: ACTION_VERSION},
        },
        "context": {
            "client_ref": "client-service",
            "owner_ref": "owner-service",
            "owner_scope": "owner-scope-service",
            "chatgpt_session_ref": "conversation-service",
            "work_run_ref": "run-service",
        },
        "work_group_id": "group-service",
        "lane_id": "lane-research",
    }


def service(
    path: Path,
    handler: FakeToolHandler,
) -> tuple[EdgeJournal, EdgeExecutionService]:
    journal = EdgeJournal(path, edge_generation=EDGE_GENERATION)
    execution = EdgeExecutionService(
        handler,
        journal,
        machine_id=MACHINE_ID,
        capabilities=capabilities(),
    )
    return journal, execution


@pytest.mark.asyncio
async def test_intent_precedes_effect_context_is_reconstructed_and_duplicate_replays_result(
    tmp_path: Path,
) -> None:
    handler = FakeToolHandler()
    journal, execution = service(tmp_path / "edge.sqlite3", handler)
    attempt = operation_attempt()

    first = await execution.execute_attempt(attempt)
    duplicate = await execution.execute_attempt(deepcopy(attempt))

    assert handler.effects == 1
    assert len(handler.calls) == 1
    call = handler.calls[0]
    assert call["tool_name"] == ACTION
    assert call["arguments"] == attempt["arguments"]
    context = call["context"]
    assert isinstance(context, RequestContext)
    assert context.transport_session_id is None
    assert context.client_ref == "client-service"
    assert context.owner_ref == "owner-service"
    assert context.owner_scope == "owner-scope-service"
    assert context.chatgpt_session_ref == "conversation-service"
    assert context.work_run_ref == "run-service"
    assert context.work_group_id == "group-service"
    assert context.lane_id == "lane-research"

    intent = journal.get_intent(attempt["operation_id"])
    assert intent is not None
    assert intent["action"] == ACTION
    assert intent["target_key"] == attempt["target_key"]
    assert first["outcome"] == "succeeded"
    assert first["acknowledged_at"] is None
    assert duplicate["receipt_id"] == first["receipt_id"]
    assert duplicate["idempotent_replay"] is True
    assert journal.get_attempt(attempt["attempt_id"])["state"] == "result_ready"
    journal.close()


@pytest.mark.asyncio
async def test_crash_before_effect_leaves_replayable_intent(tmp_path: Path, monkeypatch) -> None:
    handler = FakeToolHandler()
    journal, execution = service(tmp_path / "edge.sqlite3", handler)
    attempt = operation_attempt()
    mark_executing = journal.mark_attempt_executing

    def crash_before_effect(*args, **kwargs):
        raise SimulatedProcessCrash("process stopped after intent commit")

    monkeypatch.setattr(journal, "mark_attempt_executing", crash_before_effect)
    with pytest.raises(SimulatedProcessCrash):
        await execution.execute_attempt(attempt)

    assert handler.calls == []
    assert journal.get_attempt(attempt["attempt_id"])["state"] == "intent_recorded"

    monkeypatch.setattr(journal, "mark_attempt_executing", mark_executing)
    recovered = await execution.execute_attempt(attempt)

    assert recovered["outcome"] == "succeeded"
    assert handler.effects == 1
    journal.close()


@pytest.mark.asyncio
async def test_crash_after_effect_is_not_blindly_reexecuted_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "edge.sqlite3"
    handler = FakeToolHandler(crash_after_effect=True)
    journal, execution = service(path, handler)
    attempt = operation_attempt()

    with pytest.raises(SimulatedProcessCrash):
        await execution.execute_attempt(attempt)

    assert handler.effects == 1
    assert journal.get_attempt(attempt["attempt_id"])["state"] == "executing"
    journal.close()

    restarted_journal, restarted = service(path, handler)
    recovery = await restarted.execute_attempt(attempt)

    assert handler.effects == 1
    assert recovery["found"] is True
    assert recovery["recovery_action"] == RECOVERY_RECONCILE_EFFECT
    assert recovery["needs_reconciliation"] is True
    assert restarted.reconciliation_lookup(attempt_id=attempt["attempt_id"])[
        "recovery_action"
    ] == RECOVERY_RECONCILE_EFFECT
    restarted_journal.close()


@pytest.mark.asyncio
async def test_result_outbox_replays_across_acknowledgement_and_pruning(tmp_path: Path) -> None:
    path = tmp_path / "edge.sqlite3"
    handler = FakeToolHandler()
    journal, execution = service(path, handler)
    attempt = operation_attempt()
    receipt = await execution.execute_attempt(attempt)

    assert [item["receipt_id"] for item in execution.pending_results()] == [
        receipt["receipt_id"]
    ]
    acknowledged = execution.acknowledge_receipts(
        {"receipt_acknowledgements": [receipt]}
    )[0]
    assert acknowledged["acknowledged_at"] is not None
    assert execution.pending_results() == []
    assert execution.acknowledge_receipts({"receipt_ids": []}) == []
    assert journal.confirm_outbox_deliveries([receipt["receipt_id"]]) == 1
    assert journal.prune_acknowledged() == 1
    journal.close()

    restarted_journal, restarted = service(path, handler)
    replay = await restarted.execute_attempt(attempt)

    assert handler.effects == 1
    assert replay["receipt_id"] == receipt["receipt_id"]
    assert replay["idempotent_replay"] is True
    assert replay["pruned"] is True
    assert restarted.pending_results() == []
    restarted_journal.close()


@pytest.mark.asyncio
async def test_blocked_domain_result_is_a_durable_semantic_receipt(tmp_path: Path) -> None:
    handler = FakeToolHandler(
        result={
            "accepted": False,
            "reason": "repo_busy",
            "conflict": {"repository": "repo"},
        }
    )
    journal, execution = service(tmp_path / "edge.sqlite3", handler)

    receipt = await execution.execute_attempt(operation_attempt())

    assert receipt["outcome"] == "blocked"
    assert receipt["result"] == {
        "accepted": False,
        "reason": "repo_busy",
        "conflict": {"repository": "repo"},
    }
    assert receipt["error"] == ""
    assert journal.get_attempt("attempt-service-1")["state"] == "result_ready"
    assert execution.pending_results()[0]["result"]["reason"] == "repo_busy"
    journal.close()


@pytest.mark.asyncio
async def test_known_pre_effect_refusal_is_blocked_not_outcome_unknown(
    tmp_path: Path,
) -> None:
    handler = FakeToolHandler()
    handler.refusal = WorkerNameConflict("Reader")
    journal, execution = service(tmp_path / "edge.sqlite3", handler)

    receipt = await execution.execute_attempt(operation_attempt())

    assert handler.effects == 0
    assert receipt["outcome"] == "blocked"
    assert receipt["uncertain"] is False
    assert receipt["error"] == ""
    assert receipt["result"] == {
        "accepted": False,
        "reason": "worker_name_conflict",
        "message": (
            "A worker named 'Reader' already exists in this workspace. Continue it with "
            "patchbay_worker_message, pass auto_suffix=true, or choose another human-readable name."
        ),
    }
    assert journal.get_attempt("attempt-service-1")["state"] == "result_ready"
    journal.close()


@pytest.mark.asyncio
async def test_contract_generation_and_action_capability_fences_precede_intent(
    tmp_path: Path,
) -> None:
    handler = FakeToolHandler()
    journal, execution = service(tmp_path / "edge.sqlite3", handler)
    mutations = (
        ("required_contract_hash", "old-contract", "edge_contract_mismatch"),
        ("edge_generation", "edgegen-replaced", "edge_generation_mismatch"),
        (
            "required_action_capability_version",
            "1",
            "edge_action_capability_mismatch",
        ),
    )

    for index, (field, value, reason) in enumerate(mutations):
        attempt = operation_attempt(
            operation_id=f"op-fence-{index}",
            attempt_id=f"attempt-fence-{index}",
        )
        attempt[field] = value
        with pytest.raises(EdgeAttemptFenceError, match=reason):
            await execution.execute_attempt(attempt)
        assert journal.get_intent(attempt["operation_id"]) is None

    assert handler.calls == []
    journal.close()


@pytest.mark.asyncio
async def test_same_target_attempts_are_serialized(tmp_path: Path) -> None:
    handler = FakeToolHandler(delay=0.02)
    journal, execution = service(tmp_path / "edge.sqlite3", handler)
    reader = operation_attempt(
        operation_id="op-reader",
        attempt_id="attempt-reader",
        name="Reader",
        target_key="worker:shared",
    )
    writer = operation_attempt(
        operation_id="op-writer",
        attempt_id="attempt-writer",
        name="Writer",
        target_key="worker:shared",
    )

    results = await asyncio.gather(
        execution.execute_attempt(reader),
        execution.execute_attempt(writer),
    )

    assert [result["outcome"] for result in results] == ["succeeded", "succeeded"]
    assert handler.maximum_active == 1
    assert handler.effects == 2
    assert execution.target_lock_count == 0
    journal.close()


def test_projection_full_snapshots_use_durable_generation_and_revisions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge.sqlite3"
    handler = FakeToolHandler()
    journal, execution = service(path, handler)

    first = execution.projection_snapshot()
    handler.worker_runtime.workers = []
    second = execution.projection_snapshot()

    assert first["snapshot_kind"] == "full"
    assert first["machine_id"] == MACHINE_ID
    assert first["edge_generation"] == EDGE_GENERATION
    assert first["projection_revision"] == 1
    assert first["projection_identity"] == {
        "machine_id": MACHINE_ID,
        "edge_generation": EDGE_GENERATION,
        "projection_revision": 1,
    }
    assert second["projection_revision"] == 2
    assert second["tombstones"] == [{"edge_worker_id": "worker-1"}]
    assert handler.worker_runtime.previous_worker_ids == [[], ["worker-1"]]
    journal.close()

    restarted_journal, restarted = service(path, handler)
    third = restarted.projection_snapshot(previous_edge_worker_ids="worker-prior")

    assert third["projection_revision"] == 3
    assert handler.worker_runtime.previous_worker_ids[-1] == ["worker-prior"]
    assert restarted_journal.projection_revision == 3
    restarted_journal.close()
