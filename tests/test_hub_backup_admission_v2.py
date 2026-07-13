from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest
import yaml

from patchbay.cli import hub_enroll_code_main
from patchbay.hub.backup_v2 import (
    AdmissionFreezeController,
    AdmissionFrozenError,
    admission_coordination_path,
)
from patchbay.hub.broker import OperationBroker
from patchbay.hub.runtime_v2 import MACHINE_ENTITY, HubRuntimeV2
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.tool_surface import HUB_V2_CONTRACT_HASH
from patchbay.hub.transport_v2 import HubPullTransportBridgeV2


class _RuntimeApp:
    def __init__(self, path: Path) -> None:
        self.store = HubStoreV2(path)
        self.broker = OperationBroker(self.store)
        self.runtime = HubRuntimeV2(self.store, broker=self.broker)
        self.admission_gate = AdmissionFreezeController(
            admission_coordination_path(path)
        )

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: Any = None,
    ) -> Mapping[str, Any]:
        return await self.runtime.handle_tool_call(name, arguments, context=context)

    def close(self) -> None:
        self.store.close()


def _bridge(path: Path) -> tuple[_RuntimeApp, HubPullTransportBridgeV2]:
    app = _RuntimeApp(path)
    return app, HubPullTransportBridgeV2(app)


def test_process_shared_freeze_blocks_official_admin_cli_mutation(
    tmp_path: Path,
    capsys,
) -> None:
    state_path = tmp_path / "admin-cli.sqlite3"
    with HubStoreV2(state_path):
        pass
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "hub": {
                    "control_plane": "v2",
                    "state_db": str(state_path),
                },
                "repositories": {
                    "default": str(tmp_path),
                    "allowed": [str(tmp_path)],
                },
                "server": {"max_concurrent_jobs": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    gate = AdmissionFreezeController(admission_coordination_path(state_path))
    freeze = gate.freeze_admissions(reason="admin CLI snapshot")
    assert freeze.wait_for_drain(timeout_seconds=1)
    try:
        with pytest.raises(AdmissionFrozenError):
            hub_enroll_code_main(
                [
                    "create",
                    "--config",
                    str(config_path),
                    "--name",
                    "Blocked Edge",
                    "--json",
                ]
            )
        with HubStoreV2(state_path) as store:
            assert store.list_entities("hub.enrollment_code") == []
    finally:
        freeze.release()

    assert (
        hub_enroll_code_main(
            [
                "create",
                "--config",
                str(config_path),
                "--name",
                "Allowed Edge",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with HubStoreV2(state_path) as store:
        assert len(store.list_entities("hub.enrollment_code")) == 1


def _enrollment_payload(
    app: _RuntimeApp,
    *,
    machine_id: str,
    edge_generation: str,
) -> dict[str, Any]:
    code = app.runtime.create_enrollment_code(name=machine_id)["code"]
    return {
        "code": code,
        "machine_id": machine_id,
        "edge_generation": edge_generation,
        "display_name": machine_id,
        "capabilities": {"contract_hash": HUB_V2_CONTRACT_HASH},
    }


def _online_edge(
    app: _RuntimeApp,
    transport: HubPullTransportBridgeV2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    enrolled = transport.edge_enroll(
        _enrollment_payload(
            app,
            machine_id="online-edge",
            edge_generation="online-generation",
        )
    )
    identity = {
        "machine_id": "online-edge",
        "edge_generation": "online-generation",
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    return enrolled, identity


@pytest.mark.parametrize("mutation", ["enrollment", "heartbeat", "projection"])
def test_process_shared_freeze_blocks_edge_mutation_and_retry_resumes(
    tmp_path: Path,
    mutation: str,
) -> None:
    state_path = tmp_path / f"{mutation}.sqlite3"
    app, transport = _bridge(state_path)
    try:
        if mutation == "enrollment":
            payload = _enrollment_payload(
                app,
                machine_id="new-edge",
                edge_generation="new-generation",
            )

            def mutate() -> Mapping[str, Any]:
                return transport.edge_enroll(payload)

            def assert_unchanged() -> None:
                assert app.store.get_entity(MACHINE_ENTITY, "new-edge") is None

            def assert_resumed(result: Mapping[str, Any]) -> None:
                assert result["machine"]["machine_id"] == "new-edge"
        else:
            enrolled, identity = _online_edge(app, transport)
            token = str(enrolled["node_token"])
            if mutation == "heartbeat":
                payload = {
                    **identity,
                    "projection_revision": 1,
                    "resource_status": {"free_worker_slots": 3},
                }

                def mutate() -> Mapping[str, Any]:
                    return transport.edge_heartbeat(payload, token=token)
            else:
                payload = {
                    **identity,
                    "projection_revision": 1,
                    "projection": {
                        "snapshot_kind": "full",
                        "workers": [],
                        "tombstones": [],
                    },
                }

                def mutate() -> Mapping[str, Any]:
                    return transport.edge_projection(payload, token=token)

            def assert_unchanged() -> None:
                machine = app.store.get_entity(MACHINE_ENTITY, "online-edge")
                assert machine is not None
                assert machine["record"]["projection_revision"] == 0

            def assert_resumed(result: Mapping[str, Any]) -> None:
                assert result["projection_accepted"] is True

        backup_gate = AdmissionFreezeController(admission_coordination_path(state_path))
        freeze = backup_gate.freeze_admissions(reason="backup:hub_v2")
        try:
            assert freeze.wait_for_drain(timeout_seconds=2) is True
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(mutate)
                with pytest.raises(AdmissionFrozenError, match="backup:hub_v2"):
                    future.result(timeout=2)
            assert_unchanged()
        finally:
            freeze.release()

        assert_resumed(mutate())
    finally:
        app.close()


def test_read_status_and_receipt_ack_remain_available_during_freeze(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "allowed-traffic.sqlite3"
    app, transport = _bridge(state_path)
    try:
        enrolled, identity = _online_edge(app, transport)
        freeze = AdmissionFreezeController(
            admission_coordination_path(state_path)
        ).freeze_admissions(reason="backup:hub_v2")
        try:
            assert freeze.wait_for_drain(timeout_seconds=2) is True
            status = asyncio.run(
                transport.handle_tool_call(
                    "patchbay_fleet_status",
                    {"include_offline": True},
                )
            )
            acknowledged = transport.edge_outbox_ack(
                {**identity, "receipt_ids": []},
                token=str(enrolled["node_token"]),
            )
        finally:
            freeze.release()

        assert status["status"] == "ok"
        assert acknowledged["accepted"] is True
    finally:
        app.close()


class _NonReentrantGate:
    def __init__(self) -> None:
        self.depth = 0

    @contextmanager
    def admit_mutation(self) -> Iterator[None]:
        if self.depth:
            raise AssertionError("nested admission")
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


def test_app_admitted_dispatch_does_not_reenter_edge_admission(tmp_path: Path) -> None:
    app, transport = _bridge(tmp_path / "non-reentrant.sqlite3")
    enrollment = _enrollment_payload(
        app,
        machine_id="edge-for-dispatch",
        edge_generation="dispatch-generation",
    )
    enrollment["capabilities"] = {
        "contract_hash": HUB_V2_CONTRACT_HASH,
        "action_capabilities": {"codex_worker_stop": "2"},
    }
    transport.edge_enroll(enrollment)
    gate = _NonReentrantGate()
    app.admission_gate = gate
    try:
        operation = app.broker.create_operation(
            tool="patchbay_worker_stop",
            logical_target="worker:backup-test",
            idempotency_key="backup-admission-nesting",
            payload={"worker": "backup-test"},
        )
        operation = app.broker.prepare_operation(
            str(operation["operation_id"]),
            expected_revision=int(operation["revision"]),
        )
        assert operation is not None
        operation = app.broker.make_dispatchable(
            str(operation["operation_id"]),
            expected_revision=int(operation["revision"]),
        )
        assert operation is not None
        payload = {
            "action": "codex_worker_stop",
            "arguments": {"worker": "backup-test"},
            "machine_id": "edge-for-dispatch",
            "edge_generation": "dispatch-generation",
            "target": {
                "machine_id": "edge-for-dispatch",
                "edge_generation": "dispatch-generation",
            },
        }

        with gate.admit_mutation():
            result = asyncio.run(
                transport.dispatch_operation(operation=operation, payload=payload)
            )

        assert result["status"] == "pending"
    finally:
        app.close()
