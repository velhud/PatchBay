import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import patchbay.hub.backup_v2 as backup_module
from patchbay.hub.backup_v2 import (
    AdmissionFreezeController,
    AdmissionFrozenError,
    BackupV2ValidationError,
    admission_coordination_path,
    create_edge_v2_backup,
    create_hub_v2_backup,
    create_pre_migration_backup_marker,
    pre_migration_backup_marker_path,
    require_pre_migration_validated_backup,
    restore_edge_v2_backup,
    restore_hub_v2_backup,
    validate_pre_migration_backup_marker,
    validate_v2_backup,
    v2_backup_manifest_path,
)
from patchbay.hub.edge_journal import SCHEMA_VERSION as EDGE_SCHEMA_VERSION
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.store_v2 import SCHEMA_VERSION as HUB_SCHEMA_VERSION
from patchbay.hub.store_v2 import HubStoreV2


HUB_ENTITY_TYPES = {
    "hub.current_work_group",
    "hub.edge_dispatch",
    "hub.edge_receipt",
    "hub.fleet_worker",
    "hub.machine",
    "hub.machine_generation",
    "hub.operation_group",
    "hub.participant",
    "hub.pro_request_association",
    "hub.work_group",
    "hub.worker_projection",
    "hub.workspace",
    "hub.workspace_projection",
}


def _seed_hub(path: Path) -> tuple[HubStoreV2, dict]:
    store = HubStoreV2(path)
    entities = {
        "hub.work_group": (
            "group-1",
            {
                "work_group_id": "group-1",
                "definition_of_done": "restore complete durable state",
                "status": "open",
            },
        ),
        "hub.machine": (
            "machine-1",
            {"machine_id": "machine-1", "status": "online", "edge_generation": "7"},
        ),
        "hub.machine_generation": (
            "machine-1:7",
            {"machine_id": "machine-1", "edge_generation": "7", "status": "active"},
        ),
        "hub.workspace": (
            "workspace-1",
            {"workspace_ref": "workspace-1", "machine_id": "machine-1"},
        ),
        "hub.workspace_projection": (
            "workspace-1",
            {
                "workspace_ref": "workspace-1",
                "machine_id": "machine-1",
                "private_status": "private workspace projection payload",
            },
        ),
        "hub.participant": (
            "conversation-owner",
            {
                "participant_ref": "conversation-owner",
                "principal_ref": store.principal_ref,
                "work_group_ids": ["group-1"],
                "current_work_group_id": "group-1",
            },
        ),
        "hub.current_work_group": (
            "conversation-owner",
            {
                "participant_ref": "conversation-owner",
                "principal_ref": store.principal_ref,
                "work_group_id": "group-1",
            },
        ),
        "hub.fleet_worker": (
            "fworker-machine-1-reader",
            {
                "fleet_worker_ref": "fworker-machine-1-reader",
                "work_group_id": "group-1",
                "machine_id": "machine-1",
                "workspace_ref": "workspace-1",
                "name": "Reader",
            },
        ),
        "hub.worker_projection": (
            "fworker-machine-1-reader",
            {
                "fleet_worker_ref": "fworker-machine-1-reader",
                "work_group_id": "group-1",
                "machine_id": "machine-1",
                "status": "completed",
                "report": "private worker projection payload",
            },
        ),
    }
    for entity_type, (entity_id, record) in entities.items():
        store.put_entity(entity_type, entity_id, record)
    operation = store.create_operation(
        tool="patchbay_worker_start",
        logical_target="workspace-a",
        idempotency_key="backup-operation-1",
        payload={"name": "Reader", "brief": "Preserve durable state"},
    )
    for entity_type, entity_id, record in (
        (
            "hub.edge_dispatch",
            operation["operation_id"],
            {
                "operation_id": operation["operation_id"],
                "machine_id": "machine-1",
                "edge_generation": "7",
                "status": "claimed",
                "created_at": 100.0,
            },
        ),
        (
            "hub.edge_receipt",
            "receipt-1",
            {
                "receipt_id": "receipt-1",
                "operation_id": operation["operation_id"],
                "machine_id": "machine-1",
                "edge_generation": "7",
                "status": "pending",
                "created_at": 101.0,
            },
        ),
        (
            "hub.pro_request_association",
            "request-1",
            {
                "request_id": "request-1",
                "work_group_id": "group-1",
                "fleet_worker_ref": "fworker-machine-1-reader",
                "private_subject": "private Pro Request association payload",
            },
        ),
        (
            "hub.operation_group",
            operation["operation_id"],
            {
                "operation_id": operation["operation_id"],
                "work_group_id": "group-1",
                "kind": "worker",
            },
        ),
    ):
        store.put_entity(entity_type, entity_id, record)
    store.create_attempt(
        operation["operation_id"],
        machine_id="machine-1",
        edge_generation=7,
        attempt_id="attempt-1",
    )
    store.append_event(
        "backup.fixture.ready",
        {"private_detail": "private event payload"},
        operation_id=operation["operation_id"],
        entity_type="hub.worker_projection",
        entity_id="fworker-machine-1-reader",
        entity_revision=1,
    )
    store.create_payload_metadata(
        operation["operation_id"],
        payload_kind="worker_report",
        checksum_sha256="a" * 64,
        size_bytes=123,
        storage_ref="/private/runtime/worker-report.json",
        expires_at=None,
        metadata={"private_label": "private payload metadata"},
        payload_id="payload-1",
    )
    return store, operation


def _seed_edge(path: Path) -> tuple[EdgeJournal, dict]:
    journal = EdgeJournal(path, edge_generation="edgegen-backup-test")
    journal.record_intent(
        operation_id="op-edge-1",
        attempt_id="attempt-edge-1",
        fencing_token=1,
        action="codex_worker_start",
        target_key="worker:Reader",
        payload={"name": "Reader", "brief": "Preserve journal state"},
        correlation={"work_group_id": "group-1"},
    )
    receipt = journal.record_result(
        operation_id="op-edge-1",
        attempt_id="attempt-edge-1",
        fencing_token=1,
        outcome="succeeded",
        result={"worker_id": "Reader"},
    )
    return journal, receipt


def test_admission_freeze_contract_blocks_new_mutations_until_released() -> None:
    gate = AdmissionFreezeController()

    with gate.admit_mutation():
        assert gate.state()["active_admissions"] == 1
    lease = gate.freeze_admissions(reason="backup:hub_v2")
    assert lease.wait_for_drain(timeout_seconds=0) is True
    assert gate.state()["frozen"] is True
    with pytest.raises(AdmissionFrozenError, match="frozen"):
        with gate.admit_mutation():
            pass
    lease.release()
    assert gate.state()["frozen"] is False
    with gate.admit_mutation():
        pass


@pytest.mark.skipif(
    os.name != "posix", reason="cross-process admission uses POSIX flock"
)
def test_cross_process_gate_drains_existing_admission_and_blocks_new_ones(
    tmp_path: Path,
) -> None:
    coordination = admission_coordination_path(tmp_path / "hub.sqlite3")
    running_hub = AdmissionFreezeController(coordination)
    backup_cli = AdmissionFreezeController(coordination)
    another_hub_request = AdmissionFreezeController(coordination)
    admission_entered = threading.Event()
    release_admission = threading.Event()

    def hold_existing_admission() -> None:
        with running_hub.admit_mutation():
            admission_entered.set()
            assert release_admission.wait(timeout=5)

    holder = threading.Thread(target=hold_existing_admission, daemon=True)
    holder.start()
    assert admission_entered.wait(timeout=2)

    lease = backup_cli.freeze_admissions(reason="backup:hub_v2")
    try:
        assert lease.wait_for_drain(timeout_seconds=0.05) is False
        with pytest.raises(AdmissionFrozenError, match="backup:hub_v2"):
            with another_hub_request.admit_mutation():
                pass
        release_admission.set()
        assert lease.wait_for_drain(timeout_seconds=2) is True
    finally:
        release_admission.set()
        lease.release()
        holder.join(timeout=2)

    assert holder.is_alive() is False
    with another_hub_request.admit_mutation():
        pass


@pytest.mark.skipif(
    os.name != "posix", reason="cross-process admission uses POSIX flock"
)
def test_cross_process_gate_recovers_stale_owner_marker(tmp_path: Path) -> None:
    coordination = admission_coordination_path(tmp_path / "hub.sqlite3")
    gate = AdmissionFreezeController(coordination)
    marker = coordination / "freeze.json"
    marker.write_text(
        json.dumps(
            {
                "freeze_id": "dead-owner",
                "pid": 999_999_999,
                "process_identity": "linux:dead:0",
                "reason": "abandoned backup",
                "started_at": time.time() - 3600,
            }
        ),
        encoding="utf-8",
    )
    marker.chmod(0o600)

    with gate.admit_mutation():
        pass

    assert marker.exists() is False


@pytest.mark.skipif(
    os.name != "posix", reason="cross-process admission uses POSIX flock"
)
def test_backup_process_drains_a_separate_running_hub_process(tmp_path: Path) -> None:
    coordination = admission_coordination_path(tmp_path / "hub.sqlite3")
    ready = tmp_path / "admission-ready"
    release = tmp_path / "release-admission"
    code = """
import pathlib
import sys
import time
from patchbay.hub.backup_v2 import AdmissionFreezeController

coordination = pathlib.Path(sys.argv[1])
ready = pathlib.Path(sys.argv[2])
release = pathlib.Path(sys.argv[3])
gate = AdmissionFreezeController(coordination)
with gate.admit_mutation():
    ready.write_text('ready', encoding='ascii')
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            source_root,
            *filter(None, environment.get("PYTHONPATH", "").split(os.pathsep)),
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(coordination), str(ready), str(release)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists()

        backup_gate = AdmissionFreezeController(coordination)
        lease = backup_gate.freeze_admissions(reason="backup:hub_v2")
        try:
            assert lease.wait_for_drain(timeout_seconds=0.05) is False
            release.write_text("release", encoding="ascii")
            assert lease.wait_for_drain(timeout_seconds=2) is True
        finally:
            release.write_text("release", encoding="ascii")
            lease.release()
        assert child.wait(timeout=2) == 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=2)


def test_online_wal_hub_backup_preserves_state_without_mutating_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    store, operation = _seed_hub(source)
    try:
        assert (
            store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
            == "wal"
        )
        before_bytes = source.read_bytes()
        before_mtime = source.stat().st_mtime_ns

        gate = AdmissionFreezeController()
        created = create_hub_v2_backup(
            source,
            backup,
            deployed_revision="test-revision",
            admission_freeze=gate,
        )

        assert created["status"] == "created"
        assert created["source_unchanged"] is True
        assert created["state_proof"]["groups"]["count"] == 1
        assert created["state_proof"]["operations"]["count"] == 1
        assert created["state_proof"]["receipts"]["count"] == 1
        assert created["state_proof"]["attempts"]["count"] == 1
        assert created["state_proof"]["tables"]["entity_records"]["count"] == len(
            HUB_ENTITY_TYPES
        )
        assert created["state_proof"]["tables"]["events"]["count"] == 2
        assert created["state_proof"]["tables"]["payload_metadata"]["count"] == 1
        assert created["state_proof"]["tables"]["entity_control_index"]["count"] == 2
        assert created["state_proof"]["tables"]["operation_group_index"]["count"] == 1
        assert set(created["state_proof"]["entity_types"]) == HUB_ENTITY_TYPES
        assert all(
            proof["count"] == 1
            for proof in created["state_proof"]["entity_types"].values()
        )
        assert created["state_proof"]["identity"]["count"] == 1
        assert gate.state()["frozen"] is False
        assert source.read_bytes() == before_bytes
        assert source.stat().st_mtime_ns == before_mtime
        assert store.get_entity("hub.work_group", "group-1") is not None
        assert store.get_operation(operation["operation_id"]) is not None
        assert store.get_entity("hub.edge_receipt", "receipt-1") is not None

        manifest = json.loads(
            v2_backup_manifest_path(backup).read_text(encoding="utf-8")
        )
        assert manifest["database"]["kind"] == "hub_v2"
        assert manifest["database"]["generation"].startswith("hub-")
        assert manifest["database"]["user_version"] == HUB_SCHEMA_VERSION
        assert manifest["deployed_contract"]["deployed_revision"] == "test-revision"
        manifest_text = json.dumps(manifest, sort_keys=True)
        assert "private worker projection payload" not in manifest_text
        assert "private event payload" not in manifest_text
        assert "/private/runtime/worker-report.json" not in manifest_text
        assert (backup.parent.stat().st_mode & 0o777) == 0o700
        assert (backup.stat().st_mode & 0o777) == 0o600
        assert (v2_backup_manifest_path(backup).stat().st_mode & 0o777) == 0o600
    finally:
        store.close()


def test_manifest_validation_and_database_tampering_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    store, _operation = _seed_hub(source)
    try:
        create_hub_v2_backup(source, backup)
    finally:
        store.close()

    assert validate_v2_backup(backup, expected_kind="hub_v2")["valid"] is True
    manifest_path = v2_backup_manifest_path(backup)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["generation"] = "tampered-hub-id"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    tampered_manifest = validate_v2_backup(backup, expected_kind="hub_v2")
    assert tampered_manifest["valid"] is False
    assert {error["code"] for error in tampered_manifest["errors"]} >= {
        "backup_manifest_checksum_mismatch",
        "backup_database_metadata_mismatch",
    }

    source_two = tmp_path / "hub-two.sqlite3"
    backup_two = tmp_path / "private-backups-two" / "hub.sqlite3"
    store, _operation = _seed_hub(source_two)
    try:
        create_hub_v2_backup(source_two, backup_two)
    finally:
        store.close()
    backup_two.write_bytes(backup_two.read_bytes() + b"tampered")
    backup_two.chmod(0o600)

    tampered_database = validate_v2_backup(backup_two, expected_kind="hub_v2")
    assert tampered_database["valid"] is False
    assert "backup_checksum_mismatch" in {
        error["code"] for error in tampered_database["errors"]
    }


def test_complete_proof_detects_worker_projection_drop_old_proof_missed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    store, _operation = _seed_hub(source)
    try:
        create_hub_v2_backup(source, backup)
    finally:
        store.close()

    manifest_path = v2_backup_manifest_path(backup)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    compatibility_keys = ("groups", "operations", "receipts", "attempts")
    old_proof_before = {key: manifest["state_proof"][key] for key in compatibility_keys}

    with sqlite3.connect(backup) as connection:
        connection.execute(
            """
            DELETE FROM entity_records
            WHERE entity_type = 'hub.worker_projection'
              AND entity_id = 'fworker-machine-1-reader'
            """
        )
    backup.chmod(0o600)
    tampered_snapshot = backup_module._inspect_database(
        backup,
        expected_kind="hub_v2",
        timeout=30_000,
    )

    assert {
        key: tampered_snapshot["state_proof"][key] for key in compatibility_keys
    } == old_proof_before
    assert tampered_snapshot["state_proof"]["tables"]["entity_records"]["count"] == (
        len(HUB_ENTITY_TYPES) - 1
    )
    assert (
        "hub.worker_projection" not in tampered_snapshot["state_proof"]["entity_types"]
    )

    manifest["backup"]["sha256"] = backup_module._sha256_file(backup)
    manifest["backup"]["size_bytes"] = backup.stat().st_size
    manifest["manifest_sha256"] = backup_module._manifest_checksum(manifest)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    report = validate_v2_backup(backup, expected_kind="hub_v2")
    assert report["valid"] is False
    assert "backup_state_proof_mismatch" in {
        error["code"] for error in report["errors"]
    }
    assert "backup_checksum_mismatch" not in {
        error["code"] for error in report["errors"]
    }


def test_failed_publication_never_deletes_another_process_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    manifest_path = v2_backup_manifest_path(backup)
    store, _operation = _seed_hub(source)

    def competing_manifest(path: Path, payload: dict) -> None:
        del payload
        path.write_text('{"owner":"another-process"}\n', encoding="utf-8")
        path.chmod(0o600)
        raise FileExistsError("simulated competing manifest publication")

    monkeypatch.setattr(
        backup_module, "_write_private_json_exclusive", competing_manifest
    )
    try:
        with pytest.raises(FileExistsError, match="competing manifest"):
            create_hub_v2_backup(source, backup)
    finally:
        store.close()

    assert backup.exists() is True
    assert manifest_path.read_text(encoding="utf-8") == (
        '{"owner":"another-process"}\n'
    )


def test_backup_reuses_complete_bundle_without_replacing_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    store, _operation = _seed_hub(source)
    store.close()

    created = create_hub_v2_backup(source, backup, deployed_revision="same-revision")
    manifest_path = v2_backup_manifest_path(backup)
    identities = (backup.stat().st_ino, manifest_path.stat().st_ino)
    manifest_bytes = manifest_path.read_bytes()

    reused = create_hub_v2_backup(source, backup, deployed_revision="same-revision")

    assert created["created"] is True
    assert reused["status"] == "reused"
    assert reused["created"] is False
    assert reused["reused"] is True
    assert reused["publication"] == {"database": "reused", "manifest": "reused"}
    assert (backup.stat().st_ino, manifest_path.stat().st_ino) == identities
    assert manifest_path.read_bytes() == manifest_bytes
    assert reused["state_proof"] == created["state_proof"]
    assert {
        key: reused["state_proof"][key]
        for key in (
            "groups",
            "operations",
            "receipts",
            "attempts",
        )
    } == {
        key: created["state_proof"][key]
        for key in (
            "groups",
            "operations",
            "receipts",
            "attempts",
        )
    }


def test_backup_repairs_matching_database_orphan_after_process_crash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    store, _operation = _seed_hub(source)
    store.close()
    code = """
import os
import sys
import patchbay.hub.backup_v2 as backup_module

def crash_before_manifest(path, payload):
    del path, payload
    os._exit(91)

backup_module._write_private_json_exclusive = crash_before_manifest
backup_module.create_hub_v2_backup(sys.argv[1], sys.argv[2])
"""
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            source_root,
            *filter(None, environment.get("PYTHONPATH", "").split(os.pathsep)),
        ]
    )
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(source), str(backup)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert crashed.returncode == 91
    assert backup.is_file()
    assert not v2_backup_manifest_path(backup).exists()
    orphan_identity = backup.stat().st_ino

    recovered = create_hub_v2_backup(source, backup)

    assert recovered["status"] == "recovered"
    assert recovered["recovered_orphan"] is True
    assert recovered["publication"] == {"database": "reused", "manifest": "created"}
    assert backup.stat().st_ino == orphan_identity
    assert validate_v2_backup(backup, expected_kind="hub_v2")["valid"] is True


def test_mismatched_database_orphan_is_diagnosed_and_preserved(tmp_path: Path) -> None:
    source = tmp_path / "hub.sqlite3"
    other_source = tmp_path / "other-hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    store, _operation = _seed_hub(source)
    store.close()
    other_store, _operation = _seed_hub(other_source)
    other_store.put_entity("hub.work_group", "different", {"status": "different"})
    other_store.close()
    backup.parent.mkdir(mode=0o700)
    backup.write_bytes(other_source.read_bytes())
    backup.chmod(0o600)
    orphan_bytes = backup.read_bytes()

    with pytest.raises(
        Exception, match="different database state|different durable row content"
    ):
        create_hub_v2_backup(source, backup)

    assert backup.read_bytes() == orphan_bytes
    assert not v2_backup_manifest_path(backup).exists()


def test_edge_restore_rejects_wrong_kind_generation_and_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "edge.sqlite3"
    backup = tmp_path / "private-backups" / "edge.sqlite3"
    journal, receipt = _seed_edge(source)
    try:
        create_edge_v2_backup(source, backup, expected_generation="edgegen-backup-test")
    finally:
        journal.close()

    wrong_kind = validate_v2_backup(backup, expected_kind="hub_v2")
    assert wrong_kind["valid"] is False
    assert "database_kind_mismatch" in {error["code"] for error in wrong_kind["errors"]}
    wrong_generation = validate_v2_backup(
        backup, expected_kind="edge_v2", expected_generation="edgegen-other"
    )
    assert wrong_generation["valid"] is False
    assert "database_generation_mismatch" in {
        error["code"] for error in wrong_generation["errors"]
    }
    with pytest.raises(BackupV2ValidationError, match="validation failed"):
        restore_hub_v2_backup(backup, tmp_path / "wrong-kind.sqlite3")

    restored = tmp_path / "restored" / "edge.sqlite3"
    result = restore_edge_v2_backup(
        backup, restored, expected_generation="edgegen-backup-test"
    )
    assert result["open_verification"]["wrapper"] == "EdgeJournal"
    assert result["state_proof"]["groups"]["count"] == 1
    reused = restore_edge_v2_backup(
        backup, restored, expected_generation="edgegen-backup-test"
    )
    assert reused["status"] == "reused"
    assert reused["reused"] is True
    assert reused["publication"] == {"database": "reused"}
    with EdgeJournal(restored, edge_generation="edgegen-backup-test") as recovered:
        assert (
            recovered.get_outbox(receipt["receipt_id"])["operation_id"] == "op-edge-1"
        )
        assert recovered.get_attempt("attempt-edge-1")["state"] == "result_ready"
    with pytest.raises(Exception, match="different durable row content"):
        restore_edge_v2_backup(backup, restored)


def test_hub_restore_in_fresh_process_proves_groups_operations_and_receipts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    restored = tmp_path / "restored" / "hub.sqlite3"
    store, operation = _seed_hub(source)
    try:
        create_hub_v2_backup(source, backup)
    finally:
        store.close()

    code = """
import json
import sys
from patchbay.hub.backup_v2 import restore_hub_v2_backup
from patchbay.hub.store_v2 import HubStoreV2

report = restore_hub_v2_backup(sys.argv[1], sys.argv[2])
with HubStoreV2(sys.argv[2]) as store:
    assert store.get_entity('hub.work_group', 'group-1') is not None
    assert store.get_operation(sys.argv[3]) is not None
    assert store.get_entity('hub.edge_receipt', 'receipt-1') is not None
print(json.dumps({
    'status': report['status'],
    'wrapper': report['open_verification']['wrapper'],
    'groups': report['state_proof']['groups']['count'],
    'operations': report['state_proof']['operations']['count'],
    'receipts': report['state_proof']['receipts']['count'],
}))
"""
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            source_root,
            *filter(None, environment.get("PYTHONPATH", "").split(os.pathsep)),
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(backup),
            str(restored),
            operation["operation_id"],
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report == {
        "status": "restored",
        "wrapper": "HubStoreV2",
        "groups": 1,
        "operations": 1,
        "receipts": 1,
    }


def test_restore_reuses_database_published_before_process_crash(tmp_path: Path) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    restored = tmp_path / "restored" / "hub.sqlite3"
    store, _operation = _seed_hub(source)
    store.close()
    create_hub_v2_backup(source, backup)
    code = """
import os
import sys
import patchbay.hub.backup_v2 as backup_module

def crash_after_database_publication(*args, **kwargs):
    del args, kwargs
    os._exit(92)

backup_module._verify_restored_store = crash_after_database_publication
backup_module.restore_hub_v2_backup(sys.argv[1], sys.argv[2])
"""
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            source_root,
            *filter(None, environment.get("PYTHONPATH", "").split(os.pathsep)),
        ]
    )
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(backup), str(restored)],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert crashed.returncode == 92
    assert restored.is_file()
    restored_identity = restored.stat().st_ino

    recovered = restore_hub_v2_backup(backup, restored)

    assert recovered["status"] == "reused"
    assert recovered["publication"] == {"database": "reused"}
    assert restored.stat().st_ino == restored_identity
    assert recovered["state_proof"] == validate_v2_backup(backup)["state_proof"]


def test_hub_restore_preserves_exact_complete_state_before_wrapper_open(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    restored = tmp_path / "restored" / "hub.sqlite3"
    store, operation = _seed_hub(source)
    try:
        created = create_hub_v2_backup(source, backup)
    finally:
        store.close()

    result = restore_hub_v2_backup(backup, restored)
    raw_restored = backup_module._inspect_database(
        restored,
        expected_kind="hub_v2",
        timeout=30_000,
    )

    assert raw_restored["database"] == created["validation"]["database"]
    assert raw_restored["state_proof"] == created["state_proof"]
    assert result["state_proof"] == created["state_proof"]
    assert result["open_verification"]["verification_copy"] == "ephemeral"
    assert result["open_verification"]["restore_output_unchanged"] is True

    with HubStoreV2(restored) as reopened:
        assert reopened.get_operation(operation["operation_id"]) is not None
        assert (
            reopened.get_entity("hub.worker_projection", "fworker-machine-1-reader")[
                "record"
            ]["status"]
            == "completed"
        )
        assert (
            reopened.get_entity("hub.current_work_group", "conversation-owner")[
                "record"
            ]["work_group_id"]
            == "group-1"
        )
        assert reopened.get_payload_metadata("payload-1") is not None
        assert len(reopened.list_events(limit=10)) == 2


def test_database_manifest_and_marker_publication_fsync_parent_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "hub-schema-two.sqlite3"
    backup = tmp_path / "private-backups" / "hub-schema-two.sqlite3"
    store, _operation = _seed_hub(source)
    store.close()
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TABLE operation_group_index")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")

    directory_fsyncs: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            directory_fsyncs.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(backup_module.os, "fsync", recording_fsync)

    create_hub_v2_backup(source, backup)
    create_pre_migration_backup_marker(
        source,
        backup,
        target_schema_version=HUB_SCHEMA_VERSION,
    )

    backup_directory = backup.parent.stat()
    source_directory = source.parent.stat()
    assert (backup_directory.st_dev, backup_directory.st_ino) in directory_fsyncs
    assert (source_directory.st_dev, source_directory.st_ino) in directory_fsyncs


def test_pre_migration_marker_requires_current_validated_complete_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub-schema-two.sqlite3"
    backup = tmp_path / "private-backups" / "hub-schema-two.sqlite3"
    store, _operation = _seed_hub(source)
    store.close()
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TABLE operation_group_index")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")

    with pytest.raises(BackupV2ValidationError) as missing:
        require_pre_migration_validated_backup(
            source,
            target_schema_version=HUB_SCHEMA_VERSION,
        )
    assert "pre_migration_marker_missing" in {
        error["code"] for error in missing.value.report["errors"]
    }

    create_hub_v2_backup(source, backup)
    created = create_pre_migration_backup_marker(
        source,
        backup,
        target_schema_version=HUB_SCHEMA_VERSION,
    )
    marker = pre_migration_backup_marker_path(source)
    marker_identity = marker.stat().st_ino
    reused_marker = create_pre_migration_backup_marker(
        source,
        backup,
        target_schema_version=HUB_SCHEMA_VERSION,
    )

    assert created["valid"] is True
    assert reused_marker["status"] == "reused"
    assert reused_marker["created"] is False
    assert reused_marker["reused"] is True
    assert marker.stat().st_ino == marker_identity
    assert created["source_schema_version"] == 2
    assert created["target_schema_version"] == HUB_SCHEMA_VERSION
    assert marker.is_file()
    assert (marker.stat().st_mode & 0o777) == 0o600
    assert (
        validate_pre_migration_backup_marker(
            source,
            target_schema_version=HUB_SCHEMA_VERSION,
        )["valid"]
        is True
    )
    assert (
        require_pre_migration_validated_backup(
            source,
            target_schema_version=HUB_SCHEMA_VERSION,
        )["valid"]
        is True
    )

    with sqlite3.connect(source) as connection:
        connection.execute(
            """
            DELETE FROM entity_records
            WHERE entity_type = 'hub.worker_projection'
              AND entity_id = 'fworker-machine-1-reader'
            """
        )

    with pytest.raises(BackupV2ValidationError) as stale:
        require_pre_migration_validated_backup(
            source,
            target_schema_version=HUB_SCHEMA_VERSION,
        )
    assert {
        "pre_migration_source_state_mismatch",
        "pre_migration_backup_source_state_mismatch",
    } <= {error["code"] for error in stale.value.report["errors"]}


def test_marker_published_and_fsynced_before_process_crash_is_reused(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hub-schema-two.sqlite3"
    backup = tmp_path / "private-backups" / "hub-schema-two.sqlite3"
    store, _operation = _seed_hub(source)
    store.close()
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TABLE operation_group_index")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")
    create_hub_v2_backup(source, backup)
    marker = pre_migration_backup_marker_path(source)
    code = """
import os
import sys
import patchbay.hub.backup_v2 as backup_module

real_fsync_directory = backup_module._fsync_directory
def crash_after_directory_fsync(path):
    real_fsync_directory(path)
    os._exit(93)

backup_module._fsync_directory = crash_after_directory_fsync
backup_module.create_pre_migration_backup_marker(
    sys.argv[1],
    sys.argv[2],
    target_schema_version=int(sys.argv[3]),
)
"""
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            source_root,
            *filter(None, environment.get("PYTHONPATH", "").split(os.pathsep)),
        ]
    )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(source),
            str(backup),
            str(HUB_SCHEMA_VERSION),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert crashed.returncode == 93
    assert marker.is_file()
    marker_identity = marker.stat().st_ino

    recovered = create_pre_migration_backup_marker(
        source,
        backup,
        target_schema_version=HUB_SCHEMA_VERSION,
    )

    assert recovered["status"] == "reused"
    assert recovered["publication"] == {"marker": "reused"}
    assert marker.stat().st_ino == marker_identity


def test_backup_and_restore_migrate_supported_older_hub_schema(tmp_path: Path) -> None:
    source = tmp_path / "hub-schema-two.sqlite3"
    backup = tmp_path / "private-backups" / "hub-schema-two.sqlite3"
    restored = tmp_path / "restored" / "hub.sqlite3"
    store, operation = _seed_hub(source)
    store.close()
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TABLE operation_group_index")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")

    created = create_hub_v2_backup(source, backup)
    assert created["validation"]["valid"] is True
    assert created["validation"]["database"]["schema_version"] == 2
    recovered = restore_hub_v2_backup(backup, restored)

    assert recovered["open_verification"]["schema_version"] == HUB_SCHEMA_VERSION
    with HubStoreV2(restored) as reopened:
        assert reopened.get_operation(operation["operation_id"]) is not None
        assert reopened.get_entity("hub.work_group", "group-1") is not None


def test_backup_and_restore_migrate_supported_older_edge_schema(tmp_path: Path) -> None:
    source = tmp_path / "edge-schema-two.sqlite3"
    backup = tmp_path / "private-backups" / "edge-schema-two.sqlite3"
    restored = tmp_path / "restored" / "edge.sqlite3"
    journal, receipt = _seed_edge(source)
    journal.close()
    with sqlite3.connect(source) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute("ALTER TABLE result_outbox DROP COLUMN hub_confirmed_at")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")

    created = create_edge_v2_backup(
        source,
        backup,
        expected_generation="edgegen-backup-test",
    )
    assert created["validation"]["valid"] is True
    assert created["validation"]["database"]["schema_version"] == 2
    recovered = restore_edge_v2_backup(
        backup,
        restored,
        expected_generation="edgegen-backup-test",
    )

    assert recovered["open_verification"]["schema_version"] == EDGE_SCHEMA_VERSION
    compatibility = recovered["open_verification"]["compatibility_verification"]
    assert compatibility["table"] == "result_outbox"
    assert compatibility["preexisting_rows"] == 1
    assert "acknowledged_at" in compatibility["preexisting_columns_verified"]
    assert "hub_confirmed_at" not in compatibility["preexisting_columns_verified"]
    assert compatibility["added_columns"] == ["hub_confirmed_at"]
    assert compatibility["added_column_default"] is None
    with EdgeJournal(restored, edge_generation="edgegen-backup-test") as reopened:
        assert reopened.get_outbox(receipt["receipt_id"])["operation_id"] == "op-edge-1"


def test_older_edge_restore_rejects_corruption_in_any_preexisting_outbox_column(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "edge-schema-two.sqlite3"
    backup = tmp_path / "private-backups" / "edge-schema-two.sqlite3"
    restored = tmp_path / "restored" / "edge.sqlite3"
    journal, _receipt = _seed_edge(source)
    journal.close()
    with sqlite3.connect(source) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute("ALTER TABLE result_outbox DROP COLUMN hub_confirmed_at")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")
    create_edge_v2_backup(source, backup)

    class CorruptingEdgeJournal(EdgeJournal):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._connection.execute(
                "UPDATE result_outbox SET target_key = 'worker:Corrupted'"
            )

    monkeypatch.setattr(backup_module, "EdgeJournal", CorruptingEdgeJournal)

    with pytest.raises(Exception, match="pre-existing result_outbox row content"):
        restore_edge_v2_backup(backup, restored)

    assert restored.exists() is True
    raw_restored = backup_module._inspect_database(
        restored,
        expected_kind="edge_v2",
        timeout=30_000,
        expected_schema_version=2,
        allow_supported_older=True,
    )
    assert raw_restored["state_proof"] == validate_v2_backup(backup)["state_proof"]
