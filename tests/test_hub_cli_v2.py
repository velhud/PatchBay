import json
import sqlite3
from pathlib import Path

import pytest

from patchbay.hub.cli_v2 import (
    HubV2CLIError,
    HubV2MigrationBlocked,
    create_v1_backup,
    exact_contract_manifest,
    migration_apply,
    migration_dry_run,
    migration_status,
    rollback_eligibility,
    v2_store_doctor,
    validate_backup_checksum,
)
from patchbay.hub.store_v2 import SCHEMA_VERSION, HubStoreV2
from patchbay.hub.tool_surface import (
    HUB_V2_CONTRACT_HASH,
    HUB_V2_MANIFEST_HASH,
    HUB_V2_SCHEMA_HASH,
    HUB_V2_TOOL_NAMES,
)


def _v1_payload() -> dict:
    return {
        "version": 2,
        "hub_id": "hub-legacy",
        "created_at": 100.0,
        "enrollment_codes": {
            "PB-AAAA-BBBB": {
                "code": "PB-AAAA-BBBB",
                "display_name": "Legacy Edge",
                "created_at": 101.0,
                "expires_at": 999.0,
                "used_at": 102.0,
            }
        },
        "machines": {
            "edge-a": {
                "machine_id": "edge-a",
                "display_name": "Edge A",
                "token_hash": "sha256-token-hash",
                "created_at": 102.0,
            }
        },
        "commands": {
            "cmd-done": {
                "command_id": "cmd-done",
                "machine_id": "edge-a",
                "action": "codex_worker_start",
                "state": "completed",
            }
        },
        "work_groups": {
            "grp-a": {
                "work_group_id": "grp-a",
                "pinned_machine_id": "edge-a",
                "reassignment_history": [{"from": "edge-old", "to": "edge-a"}],
            }
        },
        "current_work_group_by_manager": {"manager-a": "grp-a"},
        "events": [
            {
                "type": "machine.enrolled",
                "created_at": 103.0,
                "data": {"machine_id": "edge-a"},
            },
        ],
    }


def _write_v1(path: Path, payload: dict | None = None) -> None:
    path.write_text(
        json.dumps(payload or _v1_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_exact_contract_manifest_is_complete_and_deterministic():
    first = exact_contract_manifest()
    second = exact_contract_manifest()

    assert first == second
    assert first["status"] == "ok"
    assert first["tool_count"] == 31
    assert first["tool_names"] == list(HUB_V2_TOOL_NAMES)
    assert first["manifest_hash"] == HUB_V2_MANIFEST_HASH
    assert first["schema_hash"] == HUB_V2_SCHEMA_HASH
    assert first["contract_hash"] == HUB_V2_CONTRACT_HASH
    assert len(first["contract"]["descriptors"]) == 31


def test_migration_dry_run_has_no_filesystem_side_effects(tmp_path):
    source = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    _write_v1(source)
    before = source.read_bytes()
    before_mtime = source.stat().st_mtime_ns

    report = migration_dry_run(source, database_path=database)

    assert report["status"] == "ready"
    assert report["can_apply"] is True
    assert report["side_effects"] == {
        "source_mutated": False,
        "backup_created": False,
        "database_created": False,
    }
    assert source.read_bytes() == before
    assert source.stat().st_mtime_ns == before_mtime
    assert not database.exists()
    assert not Path(report["backup_path"]).exists()
    assert not Path(report["backup_manifest_path"]).exists()


def test_backup_creation_and_checksum_validation_detect_tampering(tmp_path):
    source = tmp_path / "hub-state.json"
    backup = tmp_path / "hub-state.v1.bak"
    _write_v1(source)

    created = create_v1_backup(source, backup_path=backup)
    valid = validate_backup_checksum(source, backup_path=backup)

    assert created["created"] is True
    assert valid["valid"] is True
    assert valid["source_checksum_sha256"] == valid["backup_checksum_sha256"]
    assert Path(valid["manifest_path"]).is_file()

    backup.write_bytes(backup.read_bytes() + b"\n")
    tampered = validate_backup_checksum(source, backup_path=backup)

    assert tampered["valid"] is False
    assert {error["code"] for error in tampered["errors"]} >= {
        "backup_checksum_mismatch",
        "source_backup_checksum_mismatch",
        "backup_manifest_mismatch",
    }


def test_migration_apply_is_idempotent_and_never_mutates_v1_source(tmp_path):
    source = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    backup = tmp_path / "hub-state.v1.bak"
    _write_v1(source)
    before = source.read_bytes()
    before_mtime = source.stat().st_mtime_ns

    first = migration_apply(source, database_path=database, backup_path=backup)
    second = migration_apply(source, database_path=database, backup_path=backup)
    status = migration_status(source, database_path=database, backup_path=backup)

    assert first["status"] == "applied"
    assert first["import"]["already_imported"] is False
    assert second["import"]["already_imported"] is True
    assert first["store"]["v2_mutation_count"] == 0
    assert status["status"] == "applied_ready_for_cutover"
    assert status["rollback"]["eligible"] is True
    assert source.read_bytes() == before
    assert source.stat().st_mtime_ns == before_mtime
    with HubStoreV2(database) as store:
        assert len(store.list_legacy_imports()) == 1


def test_active_legacy_commands_block_dry_run_and_apply_before_backup(tmp_path):
    source = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    backup = tmp_path / "hub-state.v1.bak"
    payload = _v1_payload()
    payload["commands"] = {
        "cmd-queued": {"command_id": "cmd-queued", "state": "queued"},
        "cmd-running": {"command_id": "cmd-running", "state": "running"},
    }
    _write_v1(source, payload)

    dry_run = migration_dry_run(source, database_path=database, backup_path=backup)

    assert dry_run["can_apply"] is False
    assert dry_run["source"]["legacy_recovery_required_count"] == 2
    assert "active_legacy_commands" in {item["code"] for item in dry_run["blockers"]}
    with pytest.raises(HubV2MigrationBlocked, match="preflight failed"):
        migration_apply(source, database_path=database, backup_path=backup)
    assert not backup.exists()
    assert not database.exists()


def test_source_schema_and_contract_mismatches_fail_closed(tmp_path):
    source = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    payload = _v1_payload()
    payload["version"] = 1
    _write_v1(source, payload)

    with pytest.raises(HubV2CLIError, match="schema version mismatch"):
        migration_dry_run(source, database_path=database)

    _write_v1(source)
    report = migration_dry_run(
        source,
        database_path=database,
        expected_contract_hash="0" * 64,
    )
    assert report["can_apply"] is False
    assert "contract_hash_mismatch" in {item["code"] for item in report["blockers"]}
    with pytest.raises(HubV2MigrationBlocked, match="preflight failed"):
        migration_apply(
            source,
            database_path=database,
            expected_contract_hash="0" * 64,
        )
    assert not database.exists()


def test_store_doctor_is_read_only_and_rejects_schema_drift(tmp_path):
    database = tmp_path / "hub-state-v2.sqlite3"
    with HubStoreV2(database):
        pass
    before = database.stat().st_mtime_ns

    healthy = v2_store_doctor(database)

    assert healthy["ready"] is True
    assert healthy["schema_version"] == SCHEMA_VERSION
    assert healthy["integrity_check"] == ["ok"]
    assert database.stat().st_mtime_ns == before

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=99")
        connection.execute("ALTER TABLE principals ADD COLUMN unexpected TEXT")
    drifted = v2_store_doctor(database)

    assert drifted["ready"] is False
    codes = {error["code"] for error in drifted["errors"]}
    assert "sqlite_user_version_mismatch" in codes
    assert "schema_column_mismatch" in codes


def test_store_doctor_reconciles_legacy_import_counts(tmp_path):
    source = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    _write_v1(source)
    with HubStoreV2(database) as store:
        store.import_v1_json(source)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM events WHERE source_import_id IS NOT NULL")

    doctor = v2_store_doctor(database)

    assert doctor["ready"] is False
    assert "legacy_import_count_mismatch" in {
        error["code"] for error in doctor["errors"]
    }


def test_first_v2_domain_mutation_permanently_blocks_v1_rollback(tmp_path):
    source = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    backup = tmp_path / "hub-state.v1.bak"
    _write_v1(source)
    migration_apply(source, database_path=database, backup_path=backup)

    before = rollback_eligibility(source, database_path=database, backup_path=backup)
    assert before["eligible"] is True
    assert before["rollback_mode"] == "restart_unchanged_v1"

    with HubStoreV2(database) as store:
        store.put_entity("hub.cutover_probe", "first-mutation", {"accepted": True})

    after = rollback_eligibility(source, database_path=database, backup_path=backup)
    status = migration_status(source, database_path=database, backup_path=backup)
    dry_run = migration_dry_run(source, database_path=database, backup_path=backup)
    assert after["eligible"] is False
    assert after["v2_mutation_count"] == 1
    assert "first_v2_mutation_already_recorded" in {
        reason["code"] for reason in after["reasons"]
    }
    assert (
        after["rollback_mode"]
        == "restore_v2_database_or_use_separate_single_machine_endpoint"
    )
    assert status["status"] == "cutover_committed"
    assert dry_run["can_apply"] is False
    assert "cutover_already_committed" in {
        blocker["code"] for blocker in dry_run["blockers"]
    }
    with pytest.raises(HubV2MigrationBlocked, match="preflight failed"):
        migration_apply(source, database_path=database, backup_path=backup)


def test_imported_active_commands_make_store_doctor_fail_closed(tmp_path):
    source = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    payload = _v1_payload()
    payload["commands"] = {
        "cmd-running": {"command_id": "cmd-running", "state": "running"}
    }
    _write_v1(source, payload)
    with HubStoreV2(database) as store:
        store.import_v1_json(source)

    doctor = v2_store_doctor(database)

    assert doctor["ready"] is False
    assert doctor["legacy_recovery_required_count"] == 1
    assert "active_legacy_commands" in {error["code"] for error in doctor["errors"]}
