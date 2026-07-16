from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pytest

import patchbay.hub.edge_client_v2 as edge_client_v2
from patchbay.connector.launcher import load_config
from patchbay.hub.edge import build_capabilities, load_edge_profile, save_edge_profile
from patchbay.hub.edge_client_v2 import (
    DEFAULT_ENDPOINTS,
    EdgeV2HttpError,
    EdgeV2Profile,
    EdgeV2Runner,
    PersistentEdgeV2Transport,
    create_edge_v2_runner,
    edge_contract_metadata,
)
from patchbay.hub.edge_journal import EdgeJournal, EdgeJournalConflict
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

    async def projection_snapshot_async(
        self,
        *,
        previous_edge_worker_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.projection_snapshot,
            previous_edge_worker_ids=previous_edge_worker_ids,
        )


class TokenWorkerRuntime(FakeWorkerRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.state_token = 0

    def projection_state_token(self) -> tuple[int, int]:
        return self.state_token, 0

    async def projection_state_token_async(self) -> tuple[int, int]:
        return self.projection_state_token()


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
            local = payload.get("local_recovery") or {}
            recovery_action = str(local.get("recovery_action") or "")
            if recovery_action in {"reconcile_effect", "manual_recovery"}:
                response = {
                    "accepted": True,
                    "found": True,
                    "disposition": "manual_recovery",
                    "attempt": {"state": "manual_recovery"},
                    "operation": {"state": "blocked"},
                }
            elif (
                recovery_action == "lease_reconciliation"
                and local.get("effect_started") is False
            ):
                response = {
                    "accepted": True,
                    "found": True,
                    "disposition": "retryable",
                    "retry_attempts": [{"attempt_id": f"retry-{payload['attempt_id']}"}],
                }
            else:
                response = {"accepted": True, "found": True}
            self.reconciliation_responses.append(
                {
                    "request": saved,
                    "response": response,
                }
            )
            return response
        raise AssertionError(f"Unexpected fake HTTP path: {path}")


class FakePersistentResponse:
    def __init__(self, payload: Mapping[str, Any], *, will_close: bool = False) -> None:
        self.status = 200
        self.will_close = will_close
        self._payload = dict(payload)

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class FakePersistentConnection:
    def __init__(self, *, fail_request: bool = False) -> None:
        self.sock = None
        self.fail_request = fail_request
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False
        self.timeout: float | None = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, path, body, dict(headers)))
        if self.fail_request:
            raise OSError("simulated stale keep-alive connection")

    def getresponse(self) -> FakePersistentResponse:
        return FakePersistentResponse({"accepted": True})

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_persistent_transport_reuses_connection_and_closes_pool() -> None:
    connections: list[FakePersistentConnection] = []

    def create_connection() -> FakePersistentConnection:
        connection = FakePersistentConnection()
        connections.append(connection)
        return connection

    transport = PersistentEdgeV2Transport(
        "https://hub.example/control",
        connection_factory=create_connection,
    )

    assert await transport.post_json("/heartbeat", {"revision": 1}) == {
        "accepted": True
    }
    assert await transport.post_json("/claim", {"slots": 4}) == {"accepted": True}

    assert len(connections) == 1
    assert [request[1] for request in connections[0].requests] == [
        "/control/heartbeat",
        "/control/claim",
    ]
    await transport.aclose()
    assert connections[0].closed is True


@pytest.mark.asyncio
async def test_persistent_transport_discards_failure_without_hidden_retry() -> None:
    connections: list[FakePersistentConnection] = []

    def create_connection() -> FakePersistentConnection:
        connection = FakePersistentConnection(fail_request=not connections)
        connections.append(connection)
        return connection

    transport = PersistentEdgeV2Transport(
        "https://hub.example",
        connection_factory=create_connection,
    )

    with pytest.raises(EdgeV2HttpError, match="simulated stale"):
        await transport.post_json("/claim", {"slots": 1})

    assert len(connections) == 1
    assert len(connections[0].requests) == 1
    assert connections[0].closed is True
    assert await transport.post_json("/claim", {"slots": 1}) == {"accepted": True}
    assert len(connections) == 2
    await transport.aclose()


@pytest.mark.asyncio
async def test_stable_projection_is_not_rebuilt_or_embedded_in_heartbeat(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    handler.worker_runtime = TokenWorkerRuntime()
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    runner = _runner(execution, transport)

    first = await runner.projection_once()
    unchanged = await runner.projection_once()

    assert first["projection_accepted"] is True
    assert unchanged["projection_unchanged"] is True
    assert len(transport.calls[DEFAULT_ENDPOINTS.projection]) == 1
    assert journal.projection_revision == 1

    handler.worker_runtime.state_token += 1
    handler.worker_runtime.workers = [
        {
            "edge_worker_id": "worker-stable",
            "turn_state": "completed",
            "liveness": "terminal",
            "integration_state": "not_integrated",
        }
    ]
    await runner.projection_once()
    await runner.heartbeat_once()

    assert len(transport.calls[DEFAULT_ENDPOINTS.projection]) == 2
    heartbeat_status = transport.calls[DEFAULT_ENDPOINTS.heartbeat][-1][
        "worker_status"
    ]
    assert "workers" not in heartbeat_status
    assert heartbeat_status["counts"] == {
        "total": 1,
        "active": 0,
        "quiet": 0,
        "stale": 0,
        "lost": 0,
        "completed": 1,
        "failed": 0,
        "unintegrated": 1,
    }
    journal.close()


class FirstReceiptFailsTransport(FakeHttpTransport):
    def __init__(self) -> None:
        super().__init__()
        self.poison_receipt_id = ""

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if (
            path == DEFAULT_ENDPOINTS.result
            and str((payload.get("receipt") or {}).get("receipt_id") or "")
            == self.poison_receipt_id
        ):
            saved = deepcopy(dict(payload))
            saved["_token"] = token
            self.calls[path].append(saved)
            raise OSError("permanent first-receipt failure")
        return await super().post_json(
            path,
            payload,
            token=token,
            timeout_seconds=timeout_seconds,
        )


class FirstReconciliationFailsTransport(FakeHttpTransport):
    def __init__(self) -> None:
        super().__init__()
        self.poison_attempt_id = ""

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        token: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if (
            path == DEFAULT_ENDPOINTS.reconcile
            and str(payload.get("attempt_id") or "") == self.poison_attempt_id
        ):
            saved = deepcopy(dict(payload))
            saved["_token"] = token
            self.calls[path].append(saved)
            raise OSError("permanent first-reconciliation failure")
        return await super().post_json(
            path,
            payload,
            token=token,
            timeout_seconds=timeout_seconds,
        )


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
        "heartbeat_interval_seconds": 0.05,
        "claim_interval_seconds": 0.02,
        "result_retry_seconds": 0.02,
        "reconciliation_interval_seconds": 0.05,
        "lease_renewal_seconds": 0.05,
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


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(wait(), timeout=timeout)


def test_production_factory_persists_legacy_generation_before_journal_across_two_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patchbay_home = tmp_path / "patchbay-home"
    monkeypatch.setenv("PATCHBAY_HOME", str(patchbay_home))
    save_edge_profile(
        {
            "hub_url": "https://hub.example",
            "machine_id": MACHINE_ID,
            "node_token": "node-test-token",
        }
    )
    config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    config["repositories"]["default"] = str(tmp_path)
    config["repositories"]["allowed"] = [str(tmp_path)]

    journal_paths: list[Path] = []
    generations: list[str] = []
    for expected_revision in range(3):
        runner = create_edge_v2_runner(config)
        journal = runner.execution.journal
        journal_paths.append(journal.path)
        generations.append(runner.edge_generation)
        assert journal.projection_revision == expected_revision
        if expected_revision < 2:
            assert journal.advance_projection_revision() == expected_revision + 1
        journal.close()

    persisted = load_edge_profile()
    assert persisted["edge_generation"] == generations[0]
    assert len(set(generations)) == 1
    assert len(set(journal_paths)) == 1
    assert journal_paths[0].name == f"edge-v2-journal-{generations[0]}.sqlite3"
    assert sorted(journal_paths[0].parent.glob("edge-v2-journal-*.sqlite3")) == [
        journal_paths[0]
    ]


def test_production_factory_fails_closed_when_generation_cannot_be_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patchbay_home = tmp_path / "patchbay-home"
    monkeypatch.setenv("PATCHBAY_HOME", str(patchbay_home))
    save_edge_profile(
        {
            "hub_url": "https://hub.example",
            "machine_id": MACHINE_ID,
            "node_token": "node-test-token",
        }
    )
    config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    config["repositories"]["default"] = str(tmp_path)
    config["repositories"]["allowed"] = [str(tmp_path)]

    def fail_profile_write(path: Path, profile: Mapping[str, Any]) -> None:
        del path, profile
        raise OSError("simulated profile persistence failure")

    monkeypatch.setattr(edge_client_v2, "_atomic_write_edge_profile", fail_profile_write)

    with pytest.raises(RuntimeError, match="refusing to select or open an Edge journal"):
        create_edge_v2_runner(config)

    assert list((patchbay_home / "runtime" / "hub").glob("edge-v2-journal-*.sqlite3")) == []


def test_production_factory_deployment_mode_refuses_missing_edge_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patchbay_home = tmp_path / "patchbay-home"
    monkeypatch.setenv("PATCHBAY_HOME", str(patchbay_home))
    save_edge_profile(
        {
            "hub_url": "https://hub.example",
            "machine_id": MACHINE_ID,
            "node_token": "node-test-token",
            "edge_generation": "edgegen-required-existing",
        }
    )
    config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    config["repositories"]["default"] = str(tmp_path)
    config["repositories"]["allowed"] = [str(tmp_path)]
    config["hub"].setdefault("edge", {})["require_existing_journal"] = True

    with pytest.raises(RuntimeError, match="Configured Edge journal is missing"):
        create_edge_v2_runner(config)

    assert list((patchbay_home / "runtime" / "hub").glob("*.sqlite3")) == []


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
    await asyncio.wait_for(handler.started.wait(), timeout=5)
    await _wait_until(
        lambda: (
            len(transport.calls[DEFAULT_ENDPOINTS.heartbeat]) >= 3
            and len(transport.calls[DEFAULT_ENDPOINTS.renew_lease]) >= 2
            and len(transport.calls[DEFAULT_ENDPOINTS.projection]) >= 3
            and transport.calls[DEFAULT_ENDPOINTS.heartbeat][-1][
                "projection_revision"
            ]
            >= 2
        )
    )

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
    await asyncio.wait_for(run_task, timeout=5)

    assert handler.effects == 1
    assert journal.list_pending_outbox() == []
    assert runner.active_task_count == 0
    journal.close()


@pytest.mark.asyncio
async def test_projection_completion_wakes_heartbeat_with_new_revision(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FakeHttpTransport()
    runner = _runner(execution, transport, heartbeat_interval_seconds=60)

    run_task = runner.start()
    await _wait_until(
        lambda: (
            bool(transport.calls[DEFAULT_ENDPOINTS.projection])
            and bool(transport.calls[DEFAULT_ENDPOINTS.heartbeat])
            and transport.calls[DEFAULT_ENDPOINTS.heartbeat][-1][
                "projection_revision"
            ]
            == journal.projection_revision
        )
    )
    heartbeat_count = len(transport.calls[DEFAULT_ENDPOINTS.heartbeat])
    previous_revision = journal.projection_revision

    await runner.projection_once()
    published_revision = journal.projection_revision
    await _wait_until(
        lambda: (
            len(transport.calls[DEFAULT_ENDPOINTS.heartbeat]) > heartbeat_count
            and transport.calls[DEFAULT_ENDPOINTS.heartbeat][-1][
                "projection_revision"
            ]
            == published_revision
        )
    )

    assert published_revision == previous_revision + 1
    await runner.shutdown()
    await asyncio.wait_for(run_task, timeout=5)
    journal.close()


@pytest.mark.asyncio
async def test_blocking_projection_snapshot_does_not_stall_event_loop(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    started = threading.Event()
    release = threading.Event()
    original_snapshot = handler.worker_runtime.projection_snapshot

    def blocking_snapshot(*, previous_edge_worker_ids=None):
        started.set()
        assert release.wait(timeout=5)
        return original_snapshot(
            previous_edge_worker_ids=previous_edge_worker_ids
        )

    async def blocking_snapshot_async(*, previous_edge_worker_ids=None):
        return await asyncio.to_thread(
            blocking_snapshot,
            previous_edge_worker_ids=previous_edge_worker_ids,
        )

    def unexpected_sync_snapshot(*, previous_edge_worker_ids=None):
        raise AssertionError("Edge projection used the synchronous runtime path")

    handler.worker_runtime.projection_snapshot = unexpected_sync_snapshot
    handler.worker_runtime.projection_snapshot_async = blocking_snapshot_async
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    runner = _runner(execution, FakeHttpTransport())
    projection_task = asyncio.create_task(runner.projection_once())

    assert await asyncio.to_thread(started.wait, 5)
    loop_progressed = asyncio.Event()

    async def tick() -> None:
        await asyncio.sleep(0.01)
        loop_progressed.set()

    await asyncio.wait_for(tick(), timeout=0.2)
    assert loop_progressed.is_set()
    release.set()
    await asyncio.wait_for(projection_task, timeout=5)
    await runner.shutdown()
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
    await asyncio.wait_for(run_task, timeout=5)
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
async def test_outbox_rotates_past_failed_oldest_receipt_and_uses_attempt_contract(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    receipts = []
    for index in range(2):
        attempt = _attempt(
            capabilities,
            operation_id=f"op-fair-{index}",
            attempt_id=f"attempt-fair-{index}",
        )
        attempt["contract_hash"] = f"attempt-contract-{index}"
        attempt["required_contract_hash"] = f"attempt-contract-{index}"
        execution.journal.record_intent(
            operation_id=attempt["operation_id"],
            attempt_id=attempt["attempt_id"],
            fencing_token=1,
            action=ACTION,
            target_key=attempt["target_key"],
            payload=attempt["payload"],
            correlation={
                "edge_transport": {
                    "contract_hash": attempt["contract_hash"],
                }
            },
        )
        receipts.append(
            execution.journal.record_result(
                operation_id=attempt["operation_id"],
                attempt_id=attempt["attempt_id"],
                fencing_token=1,
                outcome="succeeded",
                result={"index": index},
            )
        )

    transport = FirstReceiptFailsTransport()
    transport.poison_receipt_id = receipts[0]["receipt_id"]
    runner = _runner(execution, transport, outbox_batch_size=1)

    first = await runner.upload_outbox_once()
    second = await runner.upload_outbox_once()

    assert first["failures"] == 1
    assert second["acknowledged"] == 1
    uploaded = transport.calls[DEFAULT_ENDPOINTS.result]
    assert uploaded[0]["contract_hash"] == runner.contract_metadata["contract_hash"]
    assert uploaded[1]["contract_hash"] == runner.contract_metadata["contract_hash"]
    assert uploaded[0]["receipt"]["contract_hash"] == "attempt-contract-0"
    assert uploaded[1]["receipt"]["contract_hash"] == "attempt-contract-1"
    assert [item["receipt_id"] for item in journal.list_pending_outbox()] == [
        receipts[0]["receipt_id"]
    ]
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_due_outbox_retry_is_not_starved_by_continuous_new_receipts(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )

    def record_receipt(index: int) -> dict[str, Any]:
        attempt = _attempt(
            capabilities,
            operation_id=f"op-continuous-{index}",
            attempt_id=f"attempt-continuous-{index}",
        )
        journal.record_intent(
            operation_id=attempt["operation_id"],
            attempt_id=attempt["attempt_id"],
            fencing_token=1,
            action=ACTION,
            target_key=attempt["target_key"],
            payload=attempt["payload"],
        )
        return journal.record_result(
            operation_id=attempt["operation_id"],
            attempt_id=attempt["attempt_id"],
            fencing_token=1,
            outcome="succeeded",
            result={"index": index},
        )

    poison = record_receipt(0)
    transport = FirstReceiptFailsTransport()
    transport.poison_receipt_id = poison["receipt_id"]
    runner = _runner(execution, transport, outbox_batch_size=1)

    first = await runner.upload_outbox_once()
    assert first["failures"] == 1

    for index in range(1, 5):
        healthy = record_receipt(index)
        runner._outbox_retry_state[poison["receipt_id"]]["next_retry_at"] = 0.0

        result = await runner.upload_outbox_once()

        assert result["retry_candidates"] == 1
        assert result["failures"] == 1
        assert result["acknowledged"] == 1
        assert healthy["receipt_id"] not in {
            item["receipt_id"] for item in journal.list_pending_outbox()
        }

    uploaded_ids = [
        call["receipt"]["receipt_id"]
        for call in transport.calls[DEFAULT_ENDPOINTS.result]
    ]
    assert uploaded_ids.count(poison["receipt_id"]) == 5
    assert len(uploaded_ids) == 9
    assert [item["receipt_id"] for item in journal.list_pending_outbox()] == [
        poison["receipt_id"]
    ]
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_unacknowledged_result_response_enters_due_retry_lane(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    attempt = _attempt(
        capabilities,
        operation_id="op-missing-ack",
        attempt_id="attempt-missing-ack",
    )
    journal.record_intent(
        operation_id=attempt["operation_id"],
        attempt_id=attempt["attempt_id"],
        fencing_token=1,
        action=ACTION,
        target_key=attempt["target_key"],
        payload=attempt["payload"],
    )
    receipt = journal.record_result(
        operation_id=attempt["operation_id"],
        attempt_id=attempt["attempt_id"],
        fencing_token=1,
        outcome="succeeded",
        result={"accepted": True},
    )
    transport = FakeHttpTransport()
    transport.result_acknowledgements = False
    runner = _runner(execution, transport)

    first = await runner.upload_outbox_once()
    transport.result_acknowledgements = True
    runner._outbox_retry_state[receipt["receipt_id"]]["next_retry_at"] = 0.0
    second = await runner.upload_outbox_once()

    assert first["uploaded"] == 1
    assert first["acknowledged"] == 0
    assert first["failures"] == 1
    assert second["retry_candidates"] == 1
    assert second["acknowledged"] == 1
    assert receipt["receipt_id"] not in runner._outbox_retry_state
    assert journal.list_pending_outbox() == []
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_uncertain_receipt_confirmation_is_retained_without_repeat_upload(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    attempt = _attempt(
        capabilities,
        operation_id="op-uncertain-confirmed",
        attempt_id="attempt-uncertain-confirmed",
    )
    journal.record_intent(
        operation_id=attempt["operation_id"],
        attempt_id=attempt["attempt_id"],
        fencing_token=1,
        action=ACTION,
        target_key=attempt["target_key"],
        payload=attempt["payload"],
    )
    receipt = journal.record_result(
        operation_id=attempt["operation_id"],
        attempt_id=attempt["attempt_id"],
        fencing_token=1,
        outcome="outcome_unknown",
        result={"last_known_phase": "worker_start"},
        uncertain=True,
    )
    transport = FakeHttpTransport()
    runner = _runner(execution, transport)

    first = await runner.upload_outbox_once()
    second = await runner.upload_outbox_once()

    retained = journal.get_outbox(receipt["receipt_id"])
    assert first["acknowledged"] == 1
    assert first["confirmed"] == 1
    assert second["pending"] == 0
    assert second["confirmed"] == 0
    assert retained is not None
    assert retained["uncertain"] is True
    assert retained["acknowledged_at"] is not None
    assert journal.get_attempt(attempt["attempt_id"])["state"] == "manual_recovery"
    assert len(transport.calls[DEFAULT_ENDPOINTS.result]) == 1
    assert len(transport.calls[DEFAULT_ENDPOINTS.outbox_ack]) == 1
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_twenty_thousand_acknowledged_receipts_confirm_in_bounded_pages(
    tmp_path: Path,
) -> None:
    history_size = 20_000
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    payload_hash = "0" * 64
    intent_rows = []
    attempt_rows = []
    outbox_rows = []
    for index in range(history_size):
        operation_id = f"op-confirm-{index:05d}"
        attempt_id = f"attempt-confirm-{index:05d}"
        receipt_id = f"receipt-confirm-{index:05d}"
        created_at = float(index)
        intent_rows.append(
            (
                operation_id,
                EDGE_GENERATION,
                ACTION,
                f"target:{index}",
                "",
                payload_hash,
                "{}",
                "{}",
                created_at,
                created_at,
            )
        )
        attempt_rows.append(
            (
                attempt_id,
                operation_id,
                EDGE_GENERATION,
                1,
                "manual_recovery",
                1,
                payload_hash,
                "outcome_unknown",
                "{}",
                "",
                1,
                receipt_id,
                created_at,
                created_at,
                created_at,
                created_at,
            )
        )
        outbox_rows.append(
            (
                receipt_id,
                operation_id,
                attempt_id,
                EDGE_GENERATION,
                1,
                payload_hash,
                f"target:{index}",
                "outcome_unknown",
                payload_hash,
                "{}",
                "",
                1,
                created_at,
                created_at,
            )
        )
    with journal.immediate_transaction() as connection:
        connection.executemany(
            """
            INSERT INTO operation_intents
                (operation_id, edge_generation, action, target_key, idempotency_key,
                 payload_hash, payload_json, correlation_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            intent_rows,
        )
        connection.executemany(
            """
            INSERT INTO operation_attempts
                (attempt_id, operation_id, edge_generation, fencing_token, state,
                 revision, result_hash, outcome, result_json, result_error,
                 result_uncertain, receipt_id, result_recorded_at, acknowledged_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            attempt_rows,
        )
        connection.executemany(
            """
            INSERT INTO result_outbox
                (receipt_id, operation_id, attempt_id, edge_generation, fencing_token,
                 operation_payload_hash, target_key, outcome, result_hash, result_json,
                 error, uncertain, created_at, acknowledged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            outbox_rows,
        )

    transport = FakeHttpTransport()
    runner = _runner(execution, transport, outbox_batch_size=64)
    confirmed = 0
    while confirmed < history_size:
        confirmed += await runner._confirm_outbox_acknowledgements()

    requests = transport.calls[DEFAULT_ENDPOINTS.outbox_ack]
    assert len(requests) == (history_size + 63) // 64
    assert all(0 < len(request["receipt_ids"]) <= 64 for request in requests)
    assert sum(len(request["receipt_ids"]) for request in requests) == history_size
    assert journal.list_outbox_pending_confirmation(limit=1) == []
    assert journal.connection.execute("SELECT COUNT(*) FROM result_outbox").fetchone()[0] == history_size
    assert await runner._confirm_outbox_acknowledgements() == 0
    assert len(transport.calls[DEFAULT_ENDPOINTS.outbox_ack]) == len(requests)
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_successor_receipts_upload_with_their_attempt_specific_contracts(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    payload = {"name": "Reader", "brief": "Inspect", "repo_path": "repo"}
    journal.record_intent(
        operation_id="op-successor",
        attempt_id="attempt-v2",
        fencing_token=1,
        action=ACTION,
        target_key="worker_name:repo:Reader",
        payload=payload,
        correlation={"edge_transport": {"contract_hash": "contract-v2"}},
    )
    journal.record_result(
        operation_id="op-successor",
        attempt_id="attempt-v2",
        fencing_token=1,
        outcome="succeeded",
        result={"worker_id": "worker-v2"},
    )
    journal.record_intent(
        operation_id="op-successor",
        attempt_id="attempt-v3",
        fencing_token=2,
        action=ACTION,
        target_key="worker_name:repo:Reader",
        payload=payload,
        correlation={"edge_transport": {"contract_hash": "contract-v3"}},
    )
    journal.record_result(
        operation_id="op-successor",
        attempt_id="attempt-v3",
        fencing_token=2,
        outcome="succeeded",
        result={"worker_id": "worker-v3"},
    )
    transport = FakeHttpTransport()
    runner = _runner(execution, transport)

    uploaded = await runner.upload_outbox_once()

    receipts = transport.calls[DEFAULT_ENDPOINTS.result]
    assert uploaded["acknowledged"] == 2
    assert [call["receipt"]["contract_hash"] for call in receipts] == [
        "contract-v2",
        "contract-v3",
    ]
    assert all(
        call["contract_hash"] == runner.contract_metadata["contract_hash"]
        for call in receipts
    )
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_reconciliation_failure_is_isolated_and_later_record_completes(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    transport = FirstReconciliationFailsTransport()
    transport.poison_attempt_id = "attempt-poison"
    runner = _runner(execution, transport)
    records = [
        {
            "operation_id": "op-poison",
            "attempt_id": "attempt-poison",
            "fencing_token": 1,
            "contract_hash": capabilities["contract_hash"],
            "recovery_action": "lease_reconciliation",
            "found": False,
            "effect_started": False,
        },
        {
            "operation_id": "op-healthy",
            "attempt_id": "attempt-healthy",
            "fencing_token": 1,
            "contract_hash": capabilities["contract_hash"],
            "recovery_action": "lease_reconciliation",
            "found": False,
            "effect_started": False,
        },
    ]
    for record in records:
        runner._enqueue_reconciliation(record)

    result = await runner.reconcile_once()

    assert result["failed_records"] == 1
    assert result["reported_records"] == 1
    assert [
        call["attempt_id"] for call in transport.calls[DEFAULT_ENDPOINTS.reconcile]
    ] == ["attempt-poison", "attempt-healthy"]
    assert [
        record["attempt_id"] for record in runner._reconciliation_queue.values()
    ] == ["attempt-poison"]
    assert runner._reconciliation_retry_state
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
async def test_no_progress_reconciliation_response_stays_pending(tmp_path: Path) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    attempt = _attempt(
        capabilities,
        operation_id="op-no-progress",
        attempt_id="attempt-no-progress",
    )
    plan = execution.validate_attempt(attempt)
    journal.record_intent(
        operation_id=plan["operation_id"],
        attempt_id=plan["attempt_id"],
        fencing_token=plan["fencing_token"],
        action=plan["action"],
        target_key=plan["target_key"],
        payload=plan["payload"],
        correlation=plan["correlation"],
    )
    journal.mark_attempt_executing(
        plan["operation_id"], plan["attempt_id"], plan["fencing_token"]
    )
    transport = FakeHttpTransport()
    runner = _runner(execution, transport)

    original_post = transport.post_json

    async def no_progress(path, payload, **kwargs):
        if path == DEFAULT_ENDPOINTS.reconcile:
            transport.calls[path].append(deepcopy(dict(payload)))
            return {"accepted": True, "found": True}
        return await original_post(path, payload, **kwargs)

    transport.post_json = no_progress
    first = await runner.reconcile_once()
    runner._reconciliation_retry_state.clear()
    second = await runner.reconcile_once()

    assert first["accepted"] is False
    assert first["reported_records"] == 0
    assert second["reported_records"] == 0
    assert journal.get_attempt(plan["attempt_id"])["state"] == "executing"
    assert len(transport.calls[DEFAULT_ENDPOINTS.reconcile]) == 2
    await runner.shutdown()
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_state", ["executing", "effect_recorded"])
async def test_effect_boundary_restart_becomes_idempotent_manual_recovery_without_replay(
    tmp_path: Path,
    restart_state: str,
) -> None:
    capabilities = _capabilities()
    path = tmp_path / f"{restart_state}.sqlite3"
    journal, execution = _service(path, FakeToolHandler(), capabilities)
    attempt = _attempt(
        capabilities,
        operation_id=f"op-{restart_state}",
        attempt_id=f"attempt-{restart_state}",
    )
    plan = execution.validate_attempt(attempt)
    journal.record_intent(
        operation_id=plan["operation_id"],
        attempt_id=plan["attempt_id"],
        fencing_token=plan["fencing_token"],
        action=plan["action"],
        target_key=plan["target_key"],
        payload=plan["payload"],
        correlation=plan["correlation"],
    )
    journal.mark_attempt_executing(
        plan["operation_id"], plan["attempt_id"], plan["fencing_token"]
    )
    if restart_state == "effect_recorded":
        journal.mark_effect_recorded(
            plan["operation_id"],
            plan["attempt_id"],
            plan["fencing_token"],
            effect={"action": ACTION, "domain_result_hash": "durable-hash-only"},
        )
    journal.close()

    recovered_handler = FakeToolHandler()
    recovered_journal, recovered_execution = _service(
        path, recovered_handler, capabilities
    )
    transport = FakeHttpTransport()
    runner = _runner(recovered_execution, transport)

    first = await runner.reconcile_once()
    second = await runner.reconcile_once()

    assert first["accepted"] is True
    assert first["reported_records"] == 1
    assert second["reported_records"] == 0
    assert recovered_handler.effects == 0
    assert recovered_journal.get_attempt(plan["attempt_id"])["state"] == (
        "manual_recovery"
    )
    assert len(transport.calls[DEFAULT_ENDPOINTS.reconcile]) == 1
    await runner.shutdown()
    recovered_journal.close()

    final_handler = FakeToolHandler()
    final_journal, final_execution = _service(path, final_handler, capabilities)
    final_transport = FakeHttpTransport()
    final_runner = _runner(final_execution, final_transport)

    repeated = await final_runner.reconcile_once()

    assert repeated["reported_records"] == 0
    assert final_handler.effects == 0
    assert final_journal.get_attempt(plan["attempt_id"])["state"] == "manual_recovery"
    assert len(final_transport.calls[DEFAULT_ENDPOINTS.reconcile]) == 0

    final_runner._queue_response_work(
        {
            "reconciliation_requests": [
                {
                    "operation_id": plan["operation_id"],
                    "attempt_id": plan["attempt_id"],
                    "fencing_token": plan["fencing_token"],
                    "edge_generation": EDGE_GENERATION,
                    "contract_hash": capabilities["contract_hash"],
                    "required_contract_hash": capabilities["contract_hash"],
                }
            ]
        }
    )
    explicitly_requested = await final_runner.reconcile_once()

    assert explicitly_requested["reported_records"] == 1
    assert len(final_transport.calls[DEFAULT_ENDPOINTS.reconcile]) == 1
    await final_runner.shutdown()
    final_journal.close()


def test_hub_reconciliation_request_preserves_historical_fences_when_journal_is_missing(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    runner = _runner(execution, FakeHttpTransport())

    runner._queue_response_work(
        {
            "reconciliation_requests": [
                {
                    "operation_id": "op-missing-local",
                    "attempt_id": "attempt-missing-local",
                    "fencing_token": 7,
                    "state": "reconciling",
                    "contract_hash": "historical-attempt-contract",
                    "required_contract_hash": "historical-attempt-contract",
                }
            ]
        }
    )

    compact = runner._reconciliation_queue.popitem(last=False)[1]
    record = runner._hydrate_reconciliation_record(compact)
    assert record["found"] is False
    assert record["contract_hash"] == "historical-attempt-contract"
    assert record["required_contract_hash"] == "historical-attempt-contract"
    journal.close()


def test_repeated_reconciliation_pages_retain_one_compact_record_per_attempt(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    runner = _runner(execution, FakeHttpTransport(), outbox_batch_size=8)
    requests = []
    report = "r" * (64 * 1024)
    for index in range(8):
        attempt = _attempt(
            capabilities,
            operation_id=f"op-memory-{index}",
            attempt_id=f"attempt-memory-{index}",
        )
        journal.record_intent(
            operation_id=attempt["operation_id"],
            attempt_id=attempt["attempt_id"],
            fencing_token=1,
            action=ACTION,
            target_key=attempt["target_key"],
            payload=attempt["payload"],
            correlation={
                "edge_transport": {"contract_hash": capabilities["contract_hash"]}
            },
        )
        journal.record_result(
            operation_id=attempt["operation_id"],
            attempt_id=attempt["attempt_id"],
            fencing_token=1,
            outcome="succeeded",
            result={"summary": f"report-{index}", "detailed_report": report},
        )
        requests.append(
            {
                "operation_id": attempt["operation_id"],
                "attempt_id": attempt["attempt_id"],
                "fencing_token": 1,
                "state": "reconciling",
                "contract_hash": capabilities["contract_hash"],
                "required_contract_hash": capabilities["contract_hash"],
            }
        )

    response = {"reconciliation_requests": requests}
    for _ in range(64):
        runner._queue_response_work(response)

    assert len(runner._reconciliation_queue) == len(requests)
    retained = list(runner._reconciliation_queue.values())
    assert {record["attempt_id"] for record in retained} == {
        request["attempt_id"] for request in requests
    }
    assert all(
        not {"attempt", "receipt", "result", "payload"}.intersection(record)
        for record in retained
    )
    assert all(report not in repr(record) for record in retained)
    hydrated = runner._hydrate_reconciliation_record(retained[0])
    assert hydrated["receipt"]["result"]["detailed_report"] == report
    identity = next(iter(runner._reconciliation_queue))
    runner._reconciliation_retry_state[identity] = {
        "failures": 1,
        "next_retry_at": 1.0,
    }
    runner._apply_receipt_acknowledgements(
        {"receipt_acknowledgements": [hydrated["receipt"]]}
    )
    assert identity not in runner._reconciliation_queue
    assert identity not in runner._reconciliation_retry_state
    acknowledged = journal.get_outbox(hydrated["receipt"]["receipt_id"])
    assert acknowledged is not None
    assert acknowledged["result"]["detailed_report"] == report
    assert acknowledged["acknowledged_at"] is not None
    assert len(journal.list_pending_outbox()) == len(requests) - 1
    journal.close()


def test_reconciliation_queue_stays_constant_over_ten_thousand_control_cycles(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    runner = _runner(execution, FakeHttpTransport())
    requests = [
        {
            "operation_id": f"op-missing-{index}",
            "attempt_id": f"attempt-missing-{index}",
            "fencing_token": 1,
            "state": "reconciling",
            "contract_hash": "historical-contract",
            "required_contract_hash": "historical-contract",
        }
        for index in range(4)
    ]

    for _ in range(10_000):
        runner._queue_response_work({"reconciliation_requests": requests})

    assert len(runner._reconciliation_queue) == len(requests)
    assert {
        record["attempt_id"] for record in runner._reconciliation_queue.values()
    } == {request["attempt_id"] for request in requests}
    journal.close()


def test_recovery_queue_coalesces_repeated_attempts_without_losing_payload(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    runner = _runner(execution, FakeHttpTransport())
    attempts = [
        _attempt(
            capabilities,
            operation_id=f"op-recovery-{index}",
            attempt_id=f"attempt-recovery-{index}",
        )
        for index in range(4)
    ]

    for _ in range(10_000):
        runner._queue_response_work({"retry_attempts": attempts})

    assert len(runner._recovery_queue) == len(attempts)
    assert list(runner._recovery_queue) == [
        attempt["attempt_id"] for attempt in attempts
    ]
    for attempt in attempts:
        retained = runner._recovery_queue[attempt["attempt_id"]]
        assert retained["payload"] == attempt["payload"]
        assert retained["required_contract_hash"] == attempt[
            "required_contract_hash"
        ]
    journal.close()


def test_fifty_thousand_recovery_identities_are_lazy_bounded_and_fair(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge-50k.sqlite3", FakeToolHandler(), capabilities
    )
    identity_count = 50_000
    with journal.immediate_transaction() as connection:
        connection.executemany(
            """
            INSERT INTO operation_intents
                (operation_id, edge_generation, action, target_key,
                 idempotency_key, payload_hash, payload_json, correlation_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, '', ?, '{}', '{}', ?, ?)
            """,
            (
                (
                    f"op-lazy-{index:05d}",
                    EDGE_GENERATION,
                    ACTION,
                    f"lazy:{index:05d}",
                    "0" * 64,
                    float(index),
                    float(index),
                )
                for index in range(identity_count)
            ),
        )
        connection.executemany(
            """
            INSERT INTO operation_attempts
                (attempt_id, operation_id, edge_generation, fencing_token,
                 state, revision, created_at, updated_at)
            VALUES (?, ?, ?, 1, 'intent_recorded', 1, ?, ?)
            """,
            (
                (
                    f"attempt-lazy-{index:05d}",
                    f"op-lazy-{index:05d}",
                    EDGE_GENERATION,
                    float(index),
                    float(index),
                )
                for index in range(identity_count)
            ),
        )

    runner = _runner(
        execution,
        FakeHttpTransport(),
        outbox_batch_size=32,
    )
    hydration_count = 0
    original_hydrator = journal.get_restart_recovery

    def counted_hydrator(attempt_id: str):
        nonlocal hydration_count
        hydration_count += 1
        return original_hydrator(attempt_id)

    journal.get_restart_recovery = counted_hydrator  # type: ignore[method-assign]
    seen: set[str] = set()
    maximum_recovery_cardinality = 0
    while len(seen) < identity_count:
        before = hydration_count
        runner._fill_recovery_queue_from_journal()
        hydrated_this_page = hydration_count - before
        maximum_recovery_cardinality = max(
            maximum_recovery_cardinality, len(runner._recovery_queue)
        )
        page = list(runner._recovery_queue)
        assert page
        assert hydrated_this_page <= runner.outbox_batch_size
        assert not seen.intersection(page)
        seen.update(page)
        runner._recovery_queue.clear()

    assert len(seen) == identity_count
    assert hydration_count == identity_count
    assert maximum_recovery_cardinality <= runner.hot_queue_identity_limit
    assert maximum_recovery_cardinality <= 32

    reconciled: set[str] = set()
    maximum_reconciliation_cardinality = 0

    def drain_reconciliation_queue() -> None:
        nonlocal maximum_reconciliation_cardinality
        maximum_reconciliation_cardinality = max(
            maximum_reconciliation_cardinality,
            len(runner._reconciliation_queue),
        )
        while runner._reconciliation_queue:
            identity, _ = runner._reconciliation_queue.popitem(last=False)
            reconciled.add(identity[1])

    for index in range(identity_count):
        record = {
            "operation_id": f"op-reconcile-{index:05d}",
            "attempt_id": f"attempt-reconcile-{index:05d}",
            "fencing_token": 1,
            "state": "reconciling",
            "found": False,
        }
        if not runner._enqueue_reconciliation(record):
            drain_reconciliation_queue()
            assert runner._enqueue_reconciliation(record) is True
    drain_reconciliation_queue()

    assert len(reconciled) == identity_count
    assert maximum_reconciliation_cardinality <= runner.hot_queue_identity_limit
    assert not runner._recovery_queue
    assert not runner._reconciliation_queue
    journal.close()


def test_hub_reconciliation_queue_cannot_starve_journal_traversal(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge-reconciliation-fairness.sqlite3",
        FakeToolHandler(),
        capabilities,
    )
    attempt = _attempt(
        capabilities,
        operation_id="op-journal-pending",
        attempt_id="attempt-journal-pending",
    )
    journal.record_intent(
        operation_id=attempt["operation_id"],
        attempt_id=attempt["attempt_id"],
        fencing_token=1,
        action=ACTION,
        target_key=attempt["target_key"],
        payload=attempt["payload"],
        correlation={
            "edge_transport": {"contract_hash": capabilities["contract_hash"]}
        },
    )
    journal.mark_attempt_executing(
        attempt["operation_id"], attempt["attempt_id"], 1
    )
    runner = _runner(
        execution,
        FakeHttpTransport(),
        outbox_batch_size=2,
    )
    for index in range(8):
        runner._enqueue_reconciliation(
            {
                "operation_id": f"op-hub-{index}",
                "attempt_id": f"attempt-hub-{index}",
                "fencing_token": 1,
                "state": "reconciling",
                "found": False,
            }
        )

    records = runner._reconciliation_records()
    attempt_ids = {str(record["attempt_id"]) for record in records}

    assert len(records) == 2
    assert "attempt-journal-pending" in attempt_ids
    assert any(attempt_id.startswith("attempt-hub-") for attempt_id in attempt_ids)
    journal.close()


def test_settled_manual_recovery_never_replays_after_cache_eviction_and_cursor_wrap(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities()
    journal, execution = _service(
        tmp_path / "edge.sqlite3", FakeToolHandler(), capabilities
    )
    settled_count = 4_097
    with journal.immediate_transaction() as connection:
        connection.executemany(
            """
            INSERT INTO operation_intents
                (operation_id, edge_generation, action, target_key,
                 idempotency_key, payload_hash, payload_json, correlation_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, '', ?, '{}', '{}', ?, ?)
            """,
            [
                (
                    f"op-manual-{index:05d}",
                    EDGE_GENERATION,
                    ACTION,
                    f"manual:{index:05d}",
                    "0" * 64,
                    float(index),
                    float(index),
                )
                for index in range(settled_count)
            ],
        )
        connection.executemany(
            """
            INSERT INTO operation_attempts
                (attempt_id, operation_id, edge_generation, fencing_token,
                 state, revision, created_at, updated_at)
            VALUES (?, ?, ?, 1, 'manual_recovery', 3, ?, ?)
            """,
            [
                (
                    f"attempt-manual-{index:05d}",
                    f"op-manual-{index:05d}",
                    EDGE_GENERATION,
                    float(index),
                    float(index),
                )
                for index in range(settled_count)
            ],
        )

    pending_states = (
        "executing",
        "intent_recorded",
        "result_ready",
        "effect_recorded",
        "outcome_unknown",
    )
    expected_reconciliation: set[str] = set()
    for index in range(15):
        state = pending_states[index % len(pending_states)]
        attempt = _attempt(
            capabilities,
            operation_id=f"op-pending-{index:02d}",
            attempt_id=f"attempt-pending-{index:02d}-{state}",
        )
        journal.record_intent(
            operation_id=attempt["operation_id"],
            attempt_id=attempt["attempt_id"],
            fencing_token=1,
            action=ACTION,
            target_key=attempt["target_key"],
            payload=attempt["payload"],
            correlation={
                "edge_transport": {"contract_hash": capabilities["contract_hash"]}
            },
        )
        if state != "intent_recorded":
            journal.mark_attempt_executing(
                attempt["operation_id"], attempt["attempt_id"], 1
            )
        if state == "result_ready":
            journal.record_result(
                operation_id=attempt["operation_id"],
                attempt_id=attempt["attempt_id"],
                fencing_token=1,
                outcome="succeeded",
                result={"summary": f"pending result {index}"},
            )
        elif state == "effect_recorded":
            journal.mark_effect_recorded(
                attempt["operation_id"],
                attempt["attempt_id"],
                1,
                effect={"action": ACTION, "index": index},
            )
            expected_reconciliation.add(attempt["attempt_id"])
        elif state == "outcome_unknown":
            journal.mark_outcome_unknown(
                attempt["operation_id"], attempt["attempt_id"], 1
            )
            expected_reconciliation.add(attempt["attempt_id"])
        elif state == "executing":
            expected_reconciliation.add(attempt["attempt_id"])

    runner = _runner(
        execution,
        FakeHttpTransport(),
        outbox_batch_size=2,
    )
    for index in range(settled_count):
        runner._record_reported_recovery(
            (f"op-manual-{index:05d}", f"attempt-manual-{index:05d}", 1)
        )
    assert (
        "op-manual-00000",
        "attempt-manual-00000",
        1,
    ) not in runner._reported_recovery

    seen: set[str] = set()
    previous_cursor: tuple[float, str] | None = None
    wrapped = False
    for _ in range(20):
        records = runner._reconciliation_records()
        seen.update(str(record["attempt_id"]) for record in records)
        cursor = runner._reconciliation_cursor
        if previous_cursor is not None and cursor is not None and cursor < previous_cursor:
            wrapped = True
        previous_cursor = cursor
        if wrapped and expected_reconciliation <= seen:
            break

    assert wrapped is True
    assert expected_reconciliation <= seen
    assert all(not attempt_id.startswith("attempt-manual-") for attempt_id in seen)
    references = journal.list_restart_recovery_references(limit=10_000)
    assert len(references) == len(pending_states) * 3
    assert all(
        not reference["attempt_id"].startswith("attempt-manual-")
        for reference in references
    )
    journal.close()


def test_repeated_background_error_tracebacks_are_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    capabilities = _capabilities()
    handler = FakeToolHandler()
    journal, execution = _service(tmp_path / "edge.sqlite3", handler, capabilities)
    runner = _runner(execution, FakeHttpTransport())
    exceptions: list[tuple[Any, ...]] = []
    warnings: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "patchbay.hub.edge_client_v2.logger.error",
        lambda *args, **kwargs: exceptions.append(args),
    )
    monkeypatch.setattr(
        "patchbay.hub.edge_client_v2.logger.warning",
        lambda *args, **kwargs: warnings.append(args),
    )
    error = OSError("same deterministic conflict")

    runner._record_background_error("result_upload", error)
    runner._record_background_error("result_upload", error)
    key = ("result_upload", "OSError", "runtime")
    runner._error_log_state[key]["last_logged_at"] = 0.0
    runner._record_background_error("result_upload", error)

    assert len(exceptions) == 2
    assert len(warnings) == 1
    assert warnings[0][2] == 1
    for index in range(300):
        runner._record_background_error(
            f"source-{index}", OSError(f"private response body {index}")
        )
    assert len(runner._error_log_state) <= 128
    assert all("private response body" not in part for key in runner._error_log_state for part in key)
    assert all("private response body" not in item for item in runner.background_errors)

    runner._record_background_error(
        "execution", EdgeJournalConflict("attempt_transport_contract_conflict")
    )
    assert runner.background_errors[-1].endswith(
        ":attempt_transport_contract_conflict"
    )
    journal.close()


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
    await asyncio.wait_for(run_task, timeout=5)

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
    await asyncio.wait_for(handler.started.wait(), timeout=5)
    await asyncio.sleep(0.06)

    assert len(transport.calls[DEFAULT_ENDPOINTS.claim]) >= 3
    assert len(handler.calls) == 1
    assert runner.active_task_count == 1

    handler.release.set()
    await _wait_until(lambda: bool(transport.calls[DEFAULT_ENDPOINTS.result]))
    await runner.shutdown()
    await asyncio.wait_for(run_task, timeout=5)

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
    await asyncio.wait_for(handler.started.wait(), timeout=5)
    shutdown_task = asyncio.create_task(runner.shutdown(cancel_active=False))
    await asyncio.sleep(0.03)

    assert not shutdown_task.done()
    claims_before_release = len(transport.calls[DEFAULT_ENDPOINTS.claim])
    handler.release.set()
    await asyncio.wait_for(shutdown_task, timeout=5)
    await asyncio.wait_for(run_task, timeout=5)

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
