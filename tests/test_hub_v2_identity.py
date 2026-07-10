from __future__ import annotations

import pytest

from patchbay.hub.identity import (
    EdgeIdentity,
    FleetWorkerIdentity,
    ManagerIdentity,
    WorkspaceProjectionIdentity,
    canonical_target_hash,
    stable_ref,
    validate_ref,
)
from patchbay.protocol.context import RequestContext


def test_manager_identity_separates_principal_conversation_transport_and_run() -> None:
    context = RequestContext(
        client_ref="client_abc123",
        chatgpt_session_ref="chatgpt_abc123",
        work_run_ref="run_abc123",
    )
    identity = ManagerIdentity.from_request(context, principal_ref="principal_local")

    assert identity.principal_ref == "principal_local"
    assert identity.conversation_ref == "chatgpt_abc123"
    assert identity.transport_ref == "client_abc123"
    assert identity.work_run_ref == "run_abc123"
    assert identity.participant_ref == "chatgpt_abc123"


def test_manager_identity_degrades_to_transport_then_principal() -> None:
    transport = ManagerIdentity.from_request(RequestContext(client_ref="client_abc123"), principal_ref="principal_local")
    anonymous = ManagerIdentity.from_request(RequestContext.anonymous(), principal_ref="principal_local")

    assert transport.participant_ref == "client_abc123"
    assert anonymous.participant_ref == "principal_local"


def test_edge_generation_is_part_of_edge_identity() -> None:
    first = EdgeIdentity("machine_alpha", "edgegen_first")
    second = EdgeIdentity("machine_alpha", "edgegen_second")

    assert first.ref != second.ref


def test_fleet_worker_ref_changes_when_edge_generation_changes() -> None:
    first = FleetWorkerIdentity.create(
        machine_id="machine_alpha",
        edge_generation="edgegen_first",
        edge_worker_id="wrk_1",
        salt="test-salt",
    )
    second = FleetWorkerIdentity.create(
        machine_id="machine_alpha",
        edge_generation="edgegen_second",
        edge_worker_id="wrk_1",
        salt="test-salt",
    )

    assert first.fleet_worker_ref != second.fleet_worker_ref


def test_workspace_projection_ref_binds_machine_generation_and_local_identity() -> None:
    projection = WorkspaceProjectionIdentity.create(
        workspace_ref="workspace_retailmind",
        machine_id="machine_alpha",
        edge_generation="edgegen_first",
        local_identity="git:example/repo",
        salt="test-salt",
    )

    assert projection.projection_ref.startswith("wsp_")
    assert projection.workspace_ref == "workspace_retailmind"


def test_stable_ref_and_target_hash_are_deterministic() -> None:
    assert stable_ref("operation", "a", "b", salt="x") == stable_ref("operation", "a", "b", salt="x")
    assert canonical_target_hash({"b": 2, "a": 1}) == canonical_target_hash({"a": 1, "b": 2})


def test_validate_ref_rejects_unstructured_or_unsafe_values() -> None:
    with pytest.raises(ValueError):
        validate_ref("../machine", field="machine_id")
