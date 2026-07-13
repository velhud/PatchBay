import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from patchbay.hub.backup_v2 import (
    create_hub_v2_backup,
    create_pre_migration_backup_marker,
)
from patchbay.hub.store_v2 import (
    LEGACY_ENTITY_TYPES,
    LEGACY_RECOVERY_REQUIRED,
    SCHEMA_VERSION,
    HubStoreV2,
    HubStoreV2Conflict,
    HubStoreV2Corrupt,
    HubStoreV2Error,
    assert_v2_activation_safe,
)


def _approve_schema_migration(path: Path, tmp_path: Path, label: str) -> Path:
    backup = tmp_path / f"{label}.backup.sqlite3"
    created = create_hub_v2_backup(path, backup)
    assert created["validation"]["valid"] is True
    marker = create_pre_migration_backup_marker(path, backup)
    assert marker["valid"] is True
    return Path(marker["marker_path"])


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
            {"type": "machine.enrolled", "created_at": 103.0, "data": {"machine_id": "edge-a"}},
            {"type": "work_group.reassigned", "created_at": 104.0, "data": {"work_group_id": "grp-a"}},
        ],
    }


def _write_v1(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_v2_activation_requires_completed_import_of_current_v1_state(tmp_path):
    legacy = tmp_path / "hub-state.json"
    database = tmp_path / "hub-state-v2.sqlite3"
    config = {
        "hub": {
            "control_plane": "v2",
            "state_file": str(legacy),
            "state_db": str(database),
        }
    }
    _write_v1(legacy, _v1_payload())

    with pytest.raises(HubStoreV2Conflict, match="has not been imported"):
        assert_v2_activation_safe(config)
    with pytest.raises(HubStoreV2Conflict, match="has not been imported"):
        HubStoreV2(config)

    with HubStoreV2(database) as migration_store:
        migration_store.import_v1_json(legacy)

    assert_v2_activation_safe(config) is None
    with HubStoreV2(config) as activated:
        assert activated.list_legacy_imports()[0]["already_imported"] is True

    changed = _v1_payload()
    changed["events"].append(
        {"type": "changed.after.import", "created_at": 105.0, "data": {}}
    )
    _write_v1(legacy, changed)
    with pytest.raises(HubStoreV2Conflict, match="differs"):
        assert_v2_activation_safe(config)


def test_deployment_continuity_refuses_missing_hub_database(tmp_path):
    path = tmp_path / "missing-hub.sqlite3"
    config = {
        "hub": {
            "control_plane": "v2",
            "state_db": str(path),
            "require_existing_state": True,
        }
    }

    with pytest.raises(HubStoreV2Conflict, match="state database is missing"):
        HubStoreV2(config)
    assert path.exists() is False


def test_deployment_continuity_refuses_wrong_hub_identity(tmp_path):
    path = tmp_path / "existing-hub.sqlite3"
    with HubStoreV2(path) as store:
        actual_hub_id = str(
            store.connection.execute(
                "SELECT hub_id FROM hub_identity WHERE singleton = 1"
            ).fetchone()[0]
        )
    config = {
        "hub": {
            "control_plane": "v2",
            "state_db": str(path),
            "require_existing_state": True,
            "expected_hub_id": f"{actual_hub_id}-wrong",
        }
    }

    with pytest.raises(HubStoreV2Conflict, match="identity does not match"):
        HubStoreV2(config)
    with HubStoreV2(path) as store:
        assert store.connection.execute(
            "SELECT hub_id FROM hub_identity WHERE singleton = 1"
        ).fetchone()[0] == actual_hub_id


@pytest.mark.parametrize("precreate_sqlite_file", [False, True])
def test_provably_new_empty_database_bootstraps_identity(
    tmp_path, precreate_sqlite_file
):
    path = tmp_path / "new-empty.sqlite3"
    if precreate_sqlite_file:
        sqlite3.connect(path).close()

    with HubStoreV2(path) as store:
        identity = store.connection.execute(
            "SELECT hub_id, principal_ref FROM hub_identity WHERE singleton = 1"
        ).fetchone()

        assert identity is not None
        assert str(identity["hub_id"]).startswith("hub-")
        assert str(identity["principal_ref"]).startswith("principal_")
        assert store.connection.execute("SELECT COUNT(*) FROM principals").fetchone()[0] == 1


def test_empty_database_with_persisted_sqlite_state_does_not_bootstrap_identity(
    tmp_path,
):
    path = tmp_path / "not-provably-new.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=1")
    connection.close()

    with pytest.raises(HubStoreV2Corrupt, match="identity record is missing"):
        HubStoreV2(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()[0] == 0
    finally:
        connection.close()


def test_hub_identity_never_changes_across_restart(tmp_path):
    path = tmp_path / "stable-identity.sqlite3"
    with HubStoreV2(path) as store:
        expected_identity = tuple(
            store.connection.execute(
                """
                SELECT hub_id, principal_ref, created_at
                FROM hub_identity
                WHERE singleton = 1
                """
            ).fetchone()
        )

    for _ in range(3):
        with HubStoreV2(path) as reopened:
            identity = tuple(
                reopened.connection.execute(
                    """
                    SELECT hub_id, principal_ref, created_at
                    FROM hub_identity
                    WHERE singleton = 1
                    """
                ).fetchone()
            )
            assert identity == expected_identity
            assert reopened.principal_ref == expected_identity[1]


def test_current_hub_schema_metadata_mismatch_fails_without_repair(tmp_path):
    path = tmp_path / "hub-user-version-mismatch.sqlite3"
    with HubStoreV2(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=2")

    with pytest.raises(HubStoreV2Corrupt, match="user_version disagree"):
        HubStoreV2(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_current_hub_schema_missing_required_object_fails_without_repair(tmp_path):
    path = tmp_path / "hub-missing-index.sqlite3"
    with HubStoreV2(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX operation_group_index_group_operation_idx")

    with pytest.raises(HubStoreV2Corrupt, match="definition mismatch"):
        HubStoreV2(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE type = 'index'
              AND name = 'operation_group_index_group_operation_idx'
            """
        ).fetchone()[0] == 0


def test_current_hub_schema_rejects_wrong_named_index_definition(tmp_path):
    path = tmp_path / "hub-wrong-index.sqlite3"
    with HubStoreV2(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX operations_state_created_idx")
        connection.execute(
            "CREATE INDEX operations_state_created_idx ON operations(tool)"
        )

    with pytest.raises(HubStoreV2Corrupt, match="definition mismatch"):
        HubStoreV2(path)


def test_nonempty_current_database_missing_identity_fails_without_state_loss(tmp_path):
    path = tmp_path / "current-missing-identity.sqlite3"
    with HubStoreV2(path) as store:
        original_principal = store.principal_ref
        store.put_entity(
            "hub.work_group",
            "group-existing",
            {"work_group_id": "group-existing", "status": "open"},
        )
        store.put_entity(
            "hub.fleet_worker",
            "worker-existing",
            {
                "fleet_worker_id": "worker-existing",
                "work_group_id": "group-existing",
            },
        )
        store.put_entity(
            "hub.current_work_group",
            "manager-existing",
            {
                "participant_ref": "manager-existing",
                "work_group_id": "group-existing",
            },
        )
        operation = store.create_operation(
            tool="patchbay_worker_start",
            logical_target="group-existing",
            idempotency_key="operation-existing",
            payload={"worker": "Existing"},
        )
        expected_entities = [
            tuple(row)
            for row in store.connection.execute(
                """
                SELECT entity_type, entity_id, revision, record_json
                FROM entity_records
                ORDER BY entity_type, entity_id
                """
            ).fetchall()
        ]
        expected_operation = tuple(
            store.connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation["operation_id"],),
            ).fetchone()
        )

    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM hub_identity")
    connection.commit()
    connection.close()

    with pytest.raises(HubStoreV2Corrupt, match="identity record is missing"):
        HubStoreV2(path)

    connection = sqlite3.connect(path)
    try:
        actual_entities = connection.execute(
            """
            SELECT entity_type, entity_id, revision, record_json
            FROM entity_records
            ORDER BY entity_type, entity_id
            """
        ).fetchall()
        actual_operation = tuple(
            connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation["operation_id"],),
            ).fetchone()
        )
        assert actual_entities == expected_entities
        assert actual_operation == expected_operation
        assert connection.execute("SELECT COUNT(*) FROM hub_identity").fetchone()[0] == 0
        assert connection.execute(
            "SELECT principal_ref FROM principals"
        ).fetchall() == [(original_principal,)]
    finally:
        connection.close()


def test_migrated_legacy_database_missing_identity_fails_without_state_loss(tmp_path):
    source = tmp_path / "hub-state.json"
    path = tmp_path / "migrated-missing-identity.sqlite3"
    _write_v1(source, _v1_payload())
    with HubStoreV2(path) as store:
        report = store.import_v1_json(source)
        original_principal = store.principal_ref

    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM hub_identity")
    connection.commit()
    connection.close()

    with pytest.raises(HubStoreV2Corrupt, match="identity record is missing"):
        HubStoreV2(path)

    connection = sqlite3.connect(path)
    try:
        imported = connection.execute(
            """
            SELECT source_checksum, source_hub_id, counts_json
            FROM legacy_imports
            WHERE status = 'complete'
            """
        ).fetchone()
        pointer = connection.execute(
            """
            SELECT record_json
            FROM entity_records
            WHERE entity_type = 'legacy.current_work_group_pointer'
              AND entity_id = 'manager-a'
            """
        ).fetchone()
        assert imported == (
            report["checksum_sha256"],
            "hub-legacy",
            json.dumps(report["counts"], separators=(",", ":"), sort_keys=True),
        )
        assert json.loads(pointer[0]) == {
            "manager_ref": "manager-a",
            "work_group_id": "grp-a",
        }
        assert connection.execute("SELECT COUNT(*) FROM entity_records").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM hub_identity").fetchone()[0] == 0
        assert connection.execute(
            "SELECT principal_ref FROM principals"
        ).fetchall() == [(original_principal,)]
    finally:
        connection.close()


def test_schema_pragmas_foreign_keys_and_principal_persist(tmp_path):
    path = tmp_path / "hub-v2.sqlite3"
    first = HubStoreV2(path, busy_timeout_ms=7_500)

    tables = {
        row[0]
        for row in first.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {
        "schema_metadata",
        "principals",
        "hub_identity",
        "entity_records",
        "operations",
        "attempts",
        "events",
        "payload_metadata",
        "legacy_imports",
    }.issubset(tables)
    assert first.schema_info() == {
        "schema_version": SCHEMA_VERSION,
        "migration_lock": None,
        "v2_mutation_count": 0,
        "journal_mode": "wal",
        "foreign_keys": True,
        "busy_timeout_ms": 7_500,
    }
    principal_ref = first.principal_ref
    assert first.get_principal()["record"] == {"trust_domain": "single_operator"}
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    with pytest.raises(sqlite3.IntegrityError):
        with first.immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO attempts
                    (attempt_id, operation_id, machine_id, edge_generation, fencing_token,
                     state, revision, created_at, updated_at)
                VALUES ('attempt-missing', 'missing-operation', 'edge', 1, 1, 'offered', 1, 1, 1)
                """
            )
    first.close()

    with HubStoreV2(path) as reopened:
        assert reopened.principal_ref == principal_ref
        assert reopened.schema_info()["schema_version"] == SCHEMA_VERSION


def test_schema_v2_backfills_control_index_without_rewriting_entities(tmp_path):
    path = tmp_path / "hub-v1-compatible.sqlite3"
    store = HubStoreV2(path)
    saved = store.put_entity(
        "hub.edge_receipt",
        "receipt-existing",
        {
            "receipt_id": "receipt-existing",
            "machine_id": "machine-existing",
            "edge_generation": "generation-existing",
            "status": "pending",
            "created_at": 10.0,
        },
    )
    store.connection.execute("DROP TABLE entity_control_index")
    store.connection.execute("DROP INDEX operations_state_created_idx")
    store.connection.execute("DROP TABLE operation_group_index")
    store.connection.execute(
        "UPDATE schema_metadata SET schema_version = 1 WHERE singleton = 1"
    )
    store.connection.execute("PRAGMA user_version=1")
    store.close()

    with pytest.raises(HubStoreV2Conflict, match="pre-migration backup"):
        HubStoreV2(path)
    marker = _approve_schema_migration(path, tmp_path, "schema-v1")
    reopened = HubStoreV2(path)
    indexed = reopened.query_control_entities(
        "hub.edge_receipt",
        machine_id="machine-existing",
        edge_generation="generation-existing",
        statuses=("pending",),
        limit=1,
    )

    assert reopened.schema_info()["schema_version"] == SCHEMA_VERSION
    assert indexed == [saved]
    assert reopened.get_entity("hub.edge_receipt", "receipt-existing") == saved
    assert reopened.pre_migration_backup_report["valid"] is True
    assert marker.exists()
    reopened.close()


def test_older_hub_schema_rejects_corrupt_index_even_with_valid_backup(tmp_path):
    path = tmp_path / "hub-schema-two-corrupt-index.sqlite3"
    with HubStoreV2(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE operation_group_index")
        connection.execute("DROP INDEX operations_state_created_idx")
        connection.execute(
            "CREATE INDEX operations_state_created_idx ON operations(tool)"
        )
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")
    marker = _approve_schema_migration(path, tmp_path, "corrupt-schema-two")

    with pytest.raises(HubStoreV2Corrupt, match="definition mismatch"):
        HubStoreV2(path, pre_migration_backup_marker=marker)


def test_schema_v3_backfills_operation_group_index_without_rewriting_entities(tmp_path):
    path = tmp_path / "hub-v2-operation-groups.sqlite3"
    store = HubStoreV2(path)
    operation = store.create_operation(
        tool="patchbay_worker_start",
        logical_target="group-existing",
        idempotency_key="operation-existing",
        payload={"worker": "Existing"},
    )
    association = store.put_entity(
        "hub.operation_group",
        operation["operation_id"],
        {
            "operation_id": operation["operation_id"],
            "work_group_id": "group-existing",
            "kind": "worker",
        },
        expected_revision=0,
    )
    store.connection.execute("DROP TABLE operation_group_index")
    store.connection.execute(
        "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
    )
    store.connection.execute("PRAGMA user_version=2")
    store.close()

    with pytest.raises(HubStoreV2Conflict, match="pre-migration backup"):
        HubStoreV2(path)
    marker = _approve_schema_migration(path, tmp_path, "schema-v2")
    reopened = HubStoreV2(path)

    assert reopened.schema_info()["schema_version"] == SCHEMA_VERSION
    assert reopened.operation_ids_for_work_group("group-existing") == [
        operation["operation_id"]
    ]
    assert reopened.get_entity("hub.operation_group", operation["operation_id"]) == association
    assert reopened.pre_migration_backup_report["valid"] is True
    assert marker.exists()
    reopened.close()


def test_multi_instance_immediate_updates_do_not_lose_writes(tmp_path):
    path = tmp_path / "hub-v2.sqlite3"
    left = HubStoreV2(path, busy_timeout_ms=10_000)
    right = HubStoreV2(path, busy_timeout_ms=10_000)
    left.put_entity("projection", "shared", {"counter": 0})

    def increment(store: HubStoreV2) -> None:
        for _ in range(75):
            store.update_entity(
                "projection",
                "shared",
                lambda record: record.__setitem__("counter", record["counter"] + 1),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(increment, store) for store in (left, right)]
        for future in futures:
            future.result()

    saved = left.get_entity("projection", "shared")
    assert saved["record"]["counter"] == 150
    assert saved["revision"] == 151
    left.close()
    right.close()


def test_entity_operation_and_attempt_cas_reject_stale_writers(tmp_path):
    path = tmp_path / "hub-v2.sqlite3"
    left = HubStoreV2(path)
    right = HubStoreV2(path)

    original = left.put_entity("participant", "conversation-a", {"state": "active"})
    changed = left.cas_entity(
        "participant", "conversation-a", original["revision"], {"state": "waiting"}
    )
    stale = right.cas_entity(
        "participant", "conversation-a", original["revision"], {"state": "closed"}
    )
    assert changed["revision"] == 2
    assert stale is None
    assert right.get_entity("participant", "conversation-a")["record"]["state"] == "waiting"

    operation = left.create_operation(
        tool="patchbay_worker_start",
        logical_target="workspace-a",
        idempotency_key="retry-key-a",
        payload={"name": "Reader"},
    )
    payload_ready = left.cas_operation_state(
        operation["operation_id"], expected_revision=1, expected_state="created", state="payload_ready"
    )
    assert payload_ready["revision"] == 2
    assert (
        right.cas_operation_state(
            operation["operation_id"], expected_revision=1, expected_state="created", state="payload_ready"
        )
        is None
    )

    attempt = left.create_attempt(
        operation["operation_id"], machine_id="edge-a", edge_generation=3
    )
    assert (
        right.cas_attempt_state(
            attempt["attempt_id"],
            expected_revision=1,
            expected_fencing_token=attempt["fencing_token"] + 1,
            state="claimed",
        )
        is None
    )
    claimed = left.cas_attempt_state(
        attempt["attempt_id"],
        expected_revision=1,
        expected_fencing_token=attempt["fencing_token"],
        expected_operation_id=operation["operation_id"],
        expected_machine_id="edge-a",
        expected_edge_generation=3,
        state="claimed",
    )
    assert claimed["state"] == "claimed"
    assert claimed["revision"] == 2
    left.close()
    right.close()


def test_operation_idempotency_replays_same_payload_and_blocks_conflict(tmp_path):
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")
    arguments = {
        "tool": "patchbay_worker_start",
        "logical_target": "workspace-a",
        "idempotency_key": "stable-retry-key",
    }

    first = store.create_operation(**arguments, payload={"name": "Reader"})
    replay = store.create_operation(**arguments, payload={"name": "Reader"})

    assert replay["operation_id"] == first["operation_id"]
    assert replay["idempotent_replay"] is True
    with pytest.raises(HubStoreV2Conflict, match="idempotency_payload_conflict"):
        store.create_operation(**arguments, payload={"name": "Writer"})
    assert store.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    store.close()


def test_events_have_stable_ordered_revisions_across_instances(tmp_path):
    path = tmp_path / "hub-v2.sqlite3"
    left = HubStoreV2(path)
    right = HubStoreV2(path)

    first = left.append_event("projection.received", {"projection_revision": 1})
    second = right.append_event("projection.received", {"projection_revision": 2})
    third = left.append_event("projection.received", {"projection_revision": 3})

    assert [first["event_revision"], second["event_revision"], third["event_revision"]] == [1, 2, 3]
    assert [event["data"]["projection_revision"] for event in right.list_events()] == [1, 2, 3]
    assert [event["event_revision"] for event in left.list_events(after_revision=1)] == [2, 3]
    left.close()
    right.close()


def test_conflicting_late_terminal_receipt_is_audited_without_overwrite(tmp_path):
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")
    operation = store.create_operation(
        tool="patchbay_worker_start",
        logical_target="workspace-a",
        idempotency_key="terminal-key",
        payload={"name": "Reader"},
    )
    for expected, state in enumerate(("payload_ready", "dispatchable", "running"), start=1):
        operation = store.cas_operation_state(
            operation["operation_id"], expected_revision=expected, state=state
        )
    terminal = store.cas_operation_state(
        operation["operation_id"],
        expected_revision=4,
        state="succeeded",
        result={"worker_id": "worker-original"},
    )

    late = store.cas_operation_state(
        operation["operation_id"],
        expected_revision=terminal["revision"],
        expected_state="succeeded",
        state="succeeded",
        result={"worker_id": "worker-conflicting"},
    )

    assert late["late_receipt_conflict"] is True
    assert late["revision"] == terminal["revision"]
    assert store.get_operation(operation["operation_id"])["result"] == {"worker_id": "worker-original"}
    assert store.list_events()[-1]["event_type"] == "operation.terminal_receipt_conflict"
    store.close()


def test_v1_import_is_idempotent_typed_and_preserves_source(tmp_path):
    source = tmp_path / "hub-state.json"
    _write_v1(source, _v1_payload())
    before = source.read_bytes()
    before_mtime = source.stat().st_mtime_ns
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")

    first = store.import_v1_json(source)
    event_revisions = [event["event_revision"] for event in store.list_events()]
    second = store.import_v1_json(source)

    assert first["already_imported"] is False
    assert second["already_imported"] is True
    assert second["checksum_sha256"] == first["checksum_sha256"]
    assert second["counts"] == first["counts"]
    assert len(store.list_legacy_imports()) == 1
    assert store.connection.execute("SELECT COUNT(*) FROM entity_records").fetchone()[0] == 5
    assert [event["event_revision"] for event in store.list_events()] == event_revisions == [1, 2]
    assert store.get_entity(LEGACY_ENTITY_TYPES["machines"], "edge-a")["record"]["token_hash"] == "sha256-token-hash"
    assert store.get_entity(LEGACY_ENTITY_TYPES["work_groups"], "grp-a")["record"][
        "reassignment_history"
    ] == [{"from": "edge-old", "to": "edge-a"}]
    assert all(event["legacy_classification"] == "legacy_event" for event in store.list_events())
    assert source.read_bytes() == before
    assert source.stat().st_mtime_ns == before_mtime
    store.close()


def test_v1_import_dry_run_does_not_persist_records(tmp_path):
    source = tmp_path / "hub-state.json"
    _write_v1(source, _v1_payload())
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")

    report = store.import_v1_json(source, dry_run=True)

    assert report["dry_run"] is True
    assert report["already_imported"] is False
    assert store.list_legacy_imports() == []
    assert store.connection.execute("SELECT COUNT(*) FROM entity_records").fetchone()[0] == 0
    store.close()


def test_v1_import_rejects_corrupt_json_without_reset_or_source_change(tmp_path):
    source = tmp_path / "hub-state.json"
    source.write_bytes(b"{not json")
    before = source.read_bytes()
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")

    with pytest.raises(HubStoreV2Corrupt, match="corrupt JSON"):
        store.import_v1_json(source)

    assert source.read_bytes() == before
    assert store.list_legacy_imports() == []
    assert store.connection.execute("SELECT COUNT(*) FROM entity_records").fetchone()[0] == 0
    store.close()


def test_v1_import_rejects_nonstandard_json_constants(tmp_path):
    source = tmp_path / "hub-state.json"
    source.write_bytes(b'{"commands": {"cmd": {"state": NaN}}}')
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")

    with pytest.raises(HubStoreV2Corrupt, match="corrupt JSON"):
        store.import_v1_json(source)

    assert store.list_legacy_imports() == []
    store.close()


def test_active_legacy_commands_require_recovery_and_are_never_requeued(tmp_path):
    payload = _v1_payload()
    payload["commands"] = {
        "cmd-queued": {"command_id": "cmd-queued", "state": "queued", "arguments": {"brief": "one"}},
        "cmd-running": {"command_id": "cmd-running", "state": "running", "arguments": {"brief": "two"}},
        "cmd-completed": {"command_id": "cmd-completed", "state": "completed", "result": {"ok": True}},
    }
    source = tmp_path / "hub-state.json"
    _write_v1(source, payload)
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")

    report = store.import_v1_json(source)
    commands = {
        record["entity_id"]: record
        for record in store.list_entities(LEGACY_ENTITY_TYPES["commands"])
    }

    assert report["legacy_recovery_required_count"] == 2
    assert commands["cmd-queued"]["legacy_classification"] == LEGACY_RECOVERY_REQUIRED
    assert commands["cmd-running"]["legacy_classification"] == LEGACY_RECOVERY_REQUIRED
    assert commands["cmd-completed"]["legacy_classification"] == "legacy_command"
    assert commands["cmd-queued"]["record"]["state"] == "queued"
    assert commands["cmd-running"]["record"]["state"] == "running"
    assert store.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert store.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    store.close()


def test_close_is_idempotent_and_rejects_later_use(tmp_path):
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")

    store.close()
    store.close()

    assert store.closed is True
    with pytest.raises(HubStoreV2Error, match="closed"):
        store.get_entity("machine", "edge-a")


def test_atomic_entity_batch_rolls_back_earlier_writes_on_late_conflict(tmp_path):
    store = HubStoreV2(tmp_path / "hub-v2.sqlite3")
    first = store.put_entity("test.entity", "first", {"value": 1})
    store.put_entity("test.entity", "second", {"value": 2})

    with pytest.raises(HubStoreV2Conflict, match="second"):
        store.put_entities_atomic(
            [
                {
                    "entity_type": "test.entity",
                    "entity_id": "first",
                    "record": {"value": 10},
                    "expected_revision": first["revision"],
                },
                {
                    "entity_type": "test.entity",
                    "entity_id": "second",
                    "record": {"value": 20},
                    "expected_revision": 99,
                },
            ]
        )

    assert store.get_entity("test.entity", "first")["record"] == {"value": 1}
    assert store.get_entity("test.entity", "first")["revision"] == 1
    assert store.get_entity("test.entity", "second")["record"] == {"value": 2}
    store.close()
