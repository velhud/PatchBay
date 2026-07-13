import json
from pathlib import Path

from patchbay.cli import edge_main, hub_main
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.store_v2 import HubStoreV2


def test_hub_backup_cli_create_validate_and_fresh_restore(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "hub.sqlite3"
    backup = tmp_path / "private-backups" / "hub.sqlite3"
    restored = tmp_path / "restored" / "hub.sqlite3"
    with HubStoreV2(source) as store:
        store.put_entity("hub.work_group", "group-cli", {"work_group_id": "group-cli"})

    assert (
        hub_main(
            [
                "backup",
                "create",
                "--database",
                str(source),
                "--backup",
                str(backup),
                "--drain-timeout-seconds",
                "2",
                "--json",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["database_kind"] == "hub_v2"
    assert created["state_proof"]["groups"]["count"] == 1
    assert created["state_proof"]["tables"]["entity_records"]["count"] == 1
    assert created["state_proof"]["entity_types"]["hub.work_group"]["count"] == 1
    assert created["state_proof"]["schema"]["count"] > 0
    assert created["state_proof"]["identity"]["count"] == 1

    assert hub_main(["backup", "validate", "--backup", str(backup), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert (
        hub_main(
            [
                "backup",
                "create",
                "--database",
                str(source),
                "--backup",
                str(backup),
                "--drain-timeout-seconds",
                "2",
                "--json",
            ]
        )
        == 0
    )
    reused = json.loads(capsys.readouterr().out)
    assert reused["status"] == "reused"
    assert reused["created"] is False
    assert reused["publication"] == {"database": "reused", "manifest": "reused"}

    assert (
        hub_main(
            [
                "backup",
                "restore",
                "--backup",
                str(backup),
                "--restore-to",
                str(restored),
                "--json",
            ]
        )
        == 0
    )
    restored_payload = json.loads(capsys.readouterr().out)
    assert restored_payload["open_verification"]["wrapper"] == "HubStoreV2"
    assert restored_payload["open_verification"]["verification_copy"] == "ephemeral"
    assert restored_payload["state_proof"] == created["state_proof"]
    with HubStoreV2(restored) as store:
        assert store.get_entity("hub.work_group", "group-cli") is not None


def test_edge_backup_cli_requires_explicit_database_and_preserves_generation(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "edge.sqlite3"
    backup = tmp_path / "private-backups" / "edge.sqlite3"
    with EdgeJournal(source, edge_generation="edgegen-cli") as journal:
        journal.record_intent(
            operation_id="op-cli",
            attempt_id="attempt-cli",
            fencing_token=1,
            action="codex_worker_start",
            target_key="worker:Reader",
            payload={"name": "Reader"},
            correlation={"work_group_id": "group-cli"},
        )

    assert (
        edge_main(
            [
                "backup",
                "create",
                "--database",
                str(source),
                "--backup",
                str(backup),
                "--expected-generation",
                "edgegen-cli",
                "--json",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["database_generation"] == "edgegen-cli"
    assert (
        edge_main(
            [
                "backup",
                "validate",
                "--backup",
                str(backup),
                "--expected-generation",
                "edgegen-cli",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_hub_backup_cli_prepares_exact_source_migration_marker(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "hub-schema-two.sqlite3"
    backup = tmp_path / "private-backups" / "hub-before-schema-three.sqlite3"
    with HubStoreV2(source) as store:
        store.put_entity(
            "hub.work_group",
            "group-before-migration",
            {"work_group_id": "group-before-migration"},
        )
        store.connection.execute("DROP TABLE operation_group_index")
        store.connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        store.connection.execute("PRAGMA user_version=2")

    assert (
        hub_main(
            [
                "backup",
                "create",
                "--database",
                str(source),
                "--backup",
                str(backup),
                "--prepare-migration",
                "--json",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    marker = created["pre_migration_backup"]
    assert marker["valid"] is True
    assert marker["source_schema_version"] == 2
    assert marker["target_schema_version"] == 3
    assert Path(marker["marker_path"]).is_file()

    with HubStoreV2(source) as migrated:
        assert migrated.schema_info()["schema_version"] == 3
        assert (
            migrated.get_entity("hub.work_group", "group-before-migration") is not None
        )
