from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from patchbay.hub.backup_v2 import (
    create_edge_v2_backup,
    create_pre_migration_backup_marker,
)
from patchbay.hub.edge_journal import (
    RECOVERY_EXECUTE_INTENT,
    RECOVERY_MANUAL,
    RECOVERY_RECONCILE_EFFECT,
    RECOVERY_UPLOAD_RESULT,
    SCHEMA_VERSION,
    EdgeJournal,
    EdgeJournalConflict,
    EdgeJournalCorrupt,
)


def _intent(
    journal: EdgeJournal,
    *,
    operation_id: str = "op-1",
    attempt_id: str = "attempt-1",
    fencing_token: int = 1,
    payload: dict | None = None,
    target_key: str = "worker:Reader",
    correlation: dict | None = None,
) -> dict:
    return journal.record_intent(
        operation_id=operation_id,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        action="codex_worker_start",
        target_key=target_key,
        payload=payload or {"name": "Reader", "brief": "Inspect the repository"},
        correlation=correlation or {"work_group_id": "group-1", "item_id": "item-1"},
    )


def test_private_sqlite_schema_pragmas_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "private" / "edge-journal.sqlite3"
    journal = EdgeJournal(path, edge_generation="edgegen-test", busy_timeout_ms=7_500)

    tables = {
        str(row[0])
        for row in journal.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "schema_metadata",
        "edge_state",
        "operation_intents",
        "operation_attempts",
        "result_outbox",
    } <= tables
    assert journal.schema_info() == {
        "schema_version": SCHEMA_VERSION,
        "migration_lock": None,
        "journal_mode": "wal",
        "foreign_keys": True,
        "busy_timeout_ms": 7_500,
        "synchronous": 2,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with pytest.raises(sqlite3.IntegrityError):
        with journal.immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO operation_attempts
                    (attempt_id, operation_id, edge_generation, fencing_token, state,
                     revision, created_at, updated_at)
                VALUES ('missing-attempt', 'missing-operation', 'edgegen-test', 1,
                        'intent_recorded', 1, 1, 1)
                """
            )
    journal.close()


def test_existing_empty_sqlite_database_bootstraps_edge_state(tmp_path: Path) -> None:
    path = tmp_path / "empty-edge-journal.sqlite3"
    sqlite3.connect(path).close()

    journal = EdgeJournal(path, edge_generation="edgegen-empty")

    assert journal.projection_identity() == {
        "edge_generation": "edgegen-empty",
        "projection_revision": 0,
    }
    journal.close()


@pytest.mark.parametrize(
    ("pragma", "value"),
    (
        ("schema_version", 7),
        ("user_version", 1),
        ("application_id", 1734437990),
    ),
)
def test_persisted_empty_sqlite_state_never_bootstraps_edge_identity(
    tmp_path: Path, pragma: str, value: int
) -> None:
    path = tmp_path / f"persisted-{pragma}.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA {pragma}={value}")

    with pytest.raises(EdgeJournalCorrupt, match="persisted SQLite state"):
        EdgeJournal(path, edge_generation="edgegen-must-not-appear")

    with sqlite3.connect(path) as connection:
        assert connection.execute(f"PRAGMA {pragma}").fetchone()[0] == value
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            == 0
        )


def test_current_edge_schema_metadata_mismatch_fails_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-user-version-mismatch.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-current"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=2")

    with pytest.raises(EdgeJournalCorrupt, match="user_version disagree"):
        EdgeJournal(path, edge_generation="edgegen-current")

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_current_edge_schema_missing_required_object_fails_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-missing-index.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-current"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")

    with pytest.raises(EdgeJournalCorrupt, match="definition mismatch"):
        EdgeJournal(path, edge_generation="edgegen-current")

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE type = 'index' AND name = 'result_outbox_confirmation_pending_idx'
            """
            ).fetchone()[0]
            == 0
        )


def test_current_edge_schema_missing_required_table_fails_without_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-missing-table.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-current"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE control_loop_health")

    with pytest.raises(EdgeJournalCorrupt, match="definition mismatch"):
        EdgeJournal(path, edge_generation="edgegen-current")

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM sqlite_schema
            WHERE type = 'table' AND name = 'control_loop_health'
            """
            ).fetchone()[0]
            == 0
        )


def test_current_edge_schema_rejects_wrong_partial_index_definition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-wrong-index.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-current"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute(
            """
            CREATE INDEX result_outbox_confirmation_pending_idx
            ON result_outbox(receipt_id)
            WHERE hub_confirmed_at IS NULL
            """
        )

    with pytest.raises(EdgeJournalCorrupt, match="definition mismatch"):
        EdgeJournal(path, edge_generation="edgegen-current")


def test_older_edge_schema_rejects_corrupt_index_even_with_valid_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-schema-two-corrupt-index.sqlite3"
    backup = tmp_path / "backups" / "edge-schema-two-corrupt-index.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-corrupt"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute("ALTER TABLE result_outbox DROP COLUMN hub_confirmed_at")
        connection.execute("DROP INDEX result_outbox_pending_idx")
        connection.execute(
            "CREATE INDEX result_outbox_pending_idx ON result_outbox(receipt_id)"
        )
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")
    create_edge_v2_backup(
        path,
        backup,
        expected_generation="edgegen-corrupt",
    )
    marker = create_pre_migration_backup_marker(
        path,
        backup,
        database_kind="edge_v2",
        expected_generation="edgegen-corrupt",
    )

    with pytest.raises(EdgeJournalCorrupt, match="definition mismatch"):
        EdgeJournal(
            path,
            edge_generation="edgegen-corrupt",
            pre_migration_backup_marker=marker["marker_path"],
        )


def test_older_edge_schema_requires_exact_validated_backup_before_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-schema-two.sqlite3"
    backup = tmp_path / "backups" / "edge-schema-two.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-schema-two") as journal:
        _intent(journal)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute("ALTER TABLE result_outbox DROP COLUMN hub_confirmed_at")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")

    with pytest.raises(EdgeJournalConflict, match="pre-migration backup"):
        EdgeJournal(path, edge_generation="edgegen-schema-two")

    created = create_edge_v2_backup(
        path, backup, expected_generation="edgegen-schema-two"
    )
    assert created["validation"]["valid"] is True
    marker = create_pre_migration_backup_marker(
        path,
        backup,
        database_kind="edge_v2",
        expected_generation="edgegen-schema-two",
    )
    with EdgeJournal(
        path,
        edge_generation="edgegen-schema-two",
        pre_migration_backup_marker=marker["marker_path"],
    ) as migrated:
        assert migrated.schema_info()["schema_version"] == SCHEMA_VERSION
        assert migrated.get_intent("op-1") is not None


def test_older_edge_schema_rejects_stale_pre_migration_marker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-schema-two-stale.sqlite3"
    backup = tmp_path / "backups" / "edge-schema-two-stale.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-schema-two"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute("ALTER TABLE result_outbox DROP COLUMN hub_confirmed_at")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")
    create_edge_v2_backup(path, backup, expected_generation="edgegen-schema-two")
    marker = create_pre_migration_backup_marker(
        path,
        backup,
        database_kind="edge_v2",
        expected_generation="edgegen-schema-two",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE edge_state SET projection_revision = 1 WHERE singleton = 1"
        )

    with pytest.raises(EdgeJournalConflict, match="pre-migration backup"):
        EdgeJournal(
            path,
            edge_generation="edgegen-schema-two",
            pre_migration_backup_marker=marker["marker_path"],
        )

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_older_edge_schema_rejects_corrupt_pre_migration_marker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-schema-two-corrupt.sqlite3"
    backup = tmp_path / "backups" / "edge-schema-two-corrupt.sqlite3"
    with EdgeJournal(path, edge_generation="edgegen-schema-two"):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX result_outbox_confirmation_pending_idx")
        connection.execute("ALTER TABLE result_outbox DROP COLUMN hub_confirmed_at")
        connection.execute(
            "UPDATE schema_metadata SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version=2")
    create_edge_v2_backup(path, backup, expected_generation="edgegen-schema-two")
    marker = create_pre_migration_backup_marker(
        path,
        backup,
        database_kind="edge_v2",
        expected_generation="edgegen-schema-two",
    )
    Path(marker["marker_path"]).write_text("{not-json", encoding="utf-8")

    with pytest.raises(EdgeJournalConflict, match="pre-migration backup"):
        EdgeJournal(
            path,
            edge_generation="edgegen-schema-two",
            pre_migration_backup_marker=marker["marker_path"],
        )

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_nonempty_journal_missing_edge_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "damaged-edge-journal.sqlite3"
    journal = EdgeJournal(path, edge_generation="edgegen-existing")
    _intent(journal)
    journal.persist_projection_revision(7)
    journal.close()

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM edge_state WHERE singleton = 1")

    with pytest.raises(EdgeJournalCorrupt) as raised:
        EdgeJournal(path, edge_generation="edgegen-existing")

    message = str(raised.value)
    assert "missing its singleton edge_state record" in message
    assert "refusing to reset edge_generation or projection_revision to zero" in message
    assert "Restore" in message
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM edge_state").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM operation_intents").fetchone()[0]
            == 1
        )


def test_intent_is_durable_before_effect_and_restart_classifies_it_safe_to_execute(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-journal.sqlite3"
    first = EdgeJournal(path, edge_generation="edgegen-test")

    recorded = _intent(first)

    assert recorded["state"] == "intent_recorded"
    assert recorded["payload_hash"]
    assert recorded["target_key"] == "worker:Reader"
    assert recorded["correlation"] == {"item_id": "item-1", "work_group_id": "group-1"}
    assert first.list_pending_outbox() == []
    first.close()  # Crash boundary: committed intent, no domain effect.

    with EdgeJournal(path, edge_generation="edgegen-test") as restarted:
        recovery = restarted.list_restart_recovery()

        assert len(recovery) == 1
        assert recovery[0]["operation_id"] == "op-1"
        assert recovery[0]["recovery_action"] == RECOVERY_EXECUTE_INTENT
        assert recovery[0]["needs_reconciliation"] is False


def test_duplicate_equivalent_intent_is_idempotent_and_conflicting_payload_is_rejected(
    tmp_path: Path,
) -> None:
    journal = EdgeJournal(
        tmp_path / "edge-journal.sqlite3", edge_generation="edgegen-test"
    )
    original = _intent(
        journal,
        payload={"brief": "Inspect the repository", "name": "Reader"},
    )

    replay = _intent(
        journal,
        payload={"name": "Reader", "brief": "Inspect the repository"},
    )

    assert replay["idempotent_replay"] is True
    assert replay["attempt_id"] == original["attempt_id"]
    assert replay["payload_hash"] == original["payload_hash"]
    with pytest.raises(EdgeJournalConflict, match="idempotency_payload_conflict"):
        _intent(journal, payload={"name": "Writer", "brief": "Change the repository"})
    assert (
        journal.connection.execute("SELECT COUNT(*) FROM operation_intents").fetchone()[
            0
        ]
        == 1
    )
    assert (
        journal.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts"
        ).fetchone()[0]
        == 1
    )
    journal.close()


def test_attempt_fence_blocks_stale_effect_and_result_writes(tmp_path: Path) -> None:
    journal = EdgeJournal(
        tmp_path / "edge-journal.sqlite3", edge_generation="edgegen-test"
    )
    _intent(journal, attempt_id="attempt-old", fencing_token=1)
    journal.mark_attempt_executing("op-1", "attempt-old", 1)
    _intent(journal, attempt_id="attempt-current", fencing_token=2)

    with pytest.raises(EdgeJournalConflict, match="stale_fencing_token"):
        journal.mark_effect_recorded(
            "op-1", "attempt-old", 1, effect={"job_id": "job-stale"}
        )
    with pytest.raises(EdgeJournalConflict, match="stale_fencing_token"):
        journal.record_result(
            operation_id="op-1",
            attempt_id="attempt-old",
            fencing_token=1,
            outcome="succeeded",
            result={"job_id": "job-stale"},
        )

    current = journal.mark_attempt_executing("op-1", "attempt-current", 2)
    assert current["state"] == "executing"
    recovery = {item["attempt_id"]: item for item in journal.list_restart_recovery()}
    assert recovery["attempt-old"]["recovery_action"] == RECOVERY_MANUAL
    assert recovery["attempt-current"]["recovery_action"] == RECOVERY_RECONCILE_EFFECT
    journal.close()


def test_result_survives_crash_in_outbox_until_hub_ack_then_prunes_safely(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-journal.sqlite3"
    first = EdgeJournal(path, edge_generation="edgegen-test")
    intent = _intent(first)
    first.mark_attempt_executing("op-1", "attempt-1", 1)
    first.mark_effect_recorded("op-1", "attempt-1", 1, effect={"job_id": "job-1"})

    receipt = first.record_result(
        operation_id="op-1",
        attempt_id="attempt-1",
        fencing_token=1,
        outcome="succeeded",
        result={"job_id": "job-1", "accepted": True},
    )

    assert receipt["operation_payload_hash"] == intent["payload_hash"]
    assert receipt["target_key"] == "worker:Reader"
    assert receipt["outcome"] == "succeeded"
    assert receipt["acknowledged_at"] is None
    assert first.get_attempt("attempt-1")["state"] == "result_ready"
    first.close()  # Crash boundary: local result committed, Hub has not acknowledged it.

    second = EdgeJournal(path, edge_generation="edgegen-test")
    assert [row["receipt_id"] for row in second.list_pending_outbox()] == [
        receipt["receipt_id"]
    ]
    recovery = second.list_restart_recovery()
    assert recovery[0]["recovery_action"] == RECOVERY_UPLOAD_RESULT
    assert recovery[0]["needs_upload"] is True

    acknowledged = second.acknowledge_outbox(
        receipt["receipt_id"],
        operation_id="op-1",
        attempt_id="attempt-1",
        fencing_token=1,
        edge_generation="edgegen-test",
    )
    assert acknowledged["acknowledged_at"] is not None
    assert second.list_pending_outbox() == []
    assert second.confirm_outbox_deliveries([receipt["receipt_id"]]) == 1
    assert second.prune_acknowledged() == 1
    assert second.get_outbox(receipt["receipt_id"]) is None

    replay = second.record_result(
        operation_id="op-1",
        attempt_id="attempt-1",
        fencing_token=1,
        outcome="succeeded",
        result={"accepted": True, "job_id": "job-1"},
    )
    assert replay["idempotent_replay"] is True
    assert replay["pruned"] is True
    assert replay["receipt_id"] == receipt["receipt_id"]
    assert second.list_pending_outbox() == []
    with pytest.raises(EdgeJournalConflict, match="receipt_operation_id_conflict"):
        second.acknowledge_outbox(receipt["receipt_id"], operation_id="op-other")
    second.close()


def test_result_receipt_preserves_attempt_contract_and_pages_past_old_failures(
    tmp_path: Path,
) -> None:
    journal = EdgeJournal(
        tmp_path / "edge-journal.sqlite3", edge_generation="edgegen-test"
    )
    for index in range(3):
        _intent(
            journal,
            operation_id=f"op-{index}",
            attempt_id=f"attempt-{index}",
            correlation={
                "edge_transport": {"contract_hash": f"contract-{index}"},
            },
        )
        journal.record_result(
            operation_id=f"op-{index}",
            attempt_id=f"attempt-{index}",
            fencing_token=1,
            outcome="succeeded",
            result={"index": index},
        )

    first = journal.list_pending_outbox(limit=1)
    second = journal.list_pending_outbox(
        limit=1,
        after=(first[0]["created_at"], first[0]["receipt_id"]),
    )

    assert first[0]["contract_hash"] == "contract-0"
    assert second[0]["contract_hash"] == "contract-1"
    assert second[0]["receipt_id"] != first[0]["receipt_id"]
    journal.close()


def test_schema_v2_derives_receipt_contract_without_a_schema_bump(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-journal.sqlite3"
    journal = EdgeJournal(path, edge_generation="edgegen-test")
    _intent(
        journal,
        correlation={"edge_transport": {"contract_hash": "historical-contract"}},
    )
    receipt = journal.record_result(
        operation_id="op-1",
        attempt_id="attempt-1",
        fencing_token=1,
        outcome="succeeded",
        result={"accepted": True},
    )
    columns = {
        str(row[1])
        for row in journal.connection.execute("PRAGMA table_info(result_outbox)")
    }
    assert SCHEMA_VERSION == 3
    assert "contract_hash" not in columns
    assert "hub_confirmed_at" in columns
    assert journal.schema_info()["schema_version"] == SCHEMA_VERSION
    assert journal.get_outbox(receipt["receipt_id"])["contract_hash"] == (
        "historical-contract"
    )
    journal.close()


def test_successor_attempts_preserve_immutable_intent_and_attempt_contracts(
    tmp_path: Path,
) -> None:
    journal = EdgeJournal(
        tmp_path / "edge-journal.sqlite3", edge_generation="edgegen-test"
    )
    payload = {"name": "Reader", "brief": "Inspect the repository"}
    first = _intent(
        journal,
        attempt_id="attempt-old",
        fencing_token=1,
        payload=payload,
        correlation={
            "work_group_id": "group-1",
            "edge_transport": {
                "contract_hash": "contract-v2",
                "attempt_revision": 2,
            },
        },
    )
    old_receipt = journal.record_result(
        operation_id="op-1",
        attempt_id="attempt-old",
        fencing_token=1,
        outcome="succeeded",
        result={"worker_id": "worker-old"},
    )

    successor = _intent(
        journal,
        attempt_id="attempt-new",
        fencing_token=2,
        payload=payload,
        correlation={
            "work_group_id": "group-1",
            "edge_transport": {
                "contract_hash": "contract-v3",
                "attempt_revision": 1,
            },
        },
    )
    new_receipt = journal.record_result(
        operation_id="op-1",
        attempt_id="attempt-new",
        fencing_token=2,
        outcome="succeeded",
        result={"worker_id": "worker-new"},
    )

    correlation = successor["correlation"]
    assert first["payload_hash"] == successor["payload_hash"]
    assert correlation["edge_transport"]["contract_hash"] == "contract-v2"
    assert correlation["edge_transport"]["attempts"] == {
        "attempt-old": {
            "attempt_revision": 2,
            "contract_hash": "contract-v2",
        },
        "attempt-new": {
            "attempt_revision": 1,
            "contract_hash": "contract-v3",
        },
    }
    assert (
        journal.get_outbox(old_receipt["receipt_id"])["contract_hash"] == "contract-v2"
    )
    assert (
        journal.get_outbox(new_receipt["receipt_id"])["contract_hash"] == "contract-v3"
    )
    assert journal.schema_info()["schema_version"] == 3
    journal.close()


def test_retention_never_prunes_unacked_or_uncertain_receipts(tmp_path: Path) -> None:
    current_time = [100.0]
    journal = EdgeJournal(
        tmp_path / "edge-journal.sqlite3",
        edge_generation="edgegen-test",
        clock=lambda: current_time[0],
    )
    _intent(journal, operation_id="op-certain", attempt_id="attempt-certain")
    certain = journal.record_result(
        operation_id="op-certain",
        attempt_id="attempt-certain",
        fencing_token=1,
        outcome="succeeded",
        result={"ok": True},
    )
    _intent(journal, operation_id="op-uncertain", attempt_id="attempt-uncertain")
    uncertain = journal.record_result(
        operation_id="op-uncertain",
        attempt_id="attempt-uncertain",
        fencing_token=1,
        outcome="outcome_unknown",
        result={"last_known_phase": "apply"},
        uncertain=True,
    )
    journal.acknowledge_outbox(uncertain["receipt_id"])

    current_time[0] = 10_000.0
    assert journal.prune_acknowledged(retention_seconds=60) == 0
    assert journal.get_outbox(certain["receipt_id"]) is not None
    assert journal.get_outbox(uncertain["receipt_id"]) is not None

    journal.acknowledge_outbox(certain["receipt_id"])
    assert journal.confirm_outbox_deliveries([certain["receipt_id"]]) == 1
    current_time[0] += 61.0
    assert journal.prune_acknowledged(retention_seconds=60) == 1
    assert journal.get_outbox(certain["receipt_id"]) is None
    assert [row["receipt_id"] for row in journal.list_uncertain_receipts()] == [
        uncertain["receipt_id"]
    ]
    assert journal.get_attempt("attempt-uncertain")["state"] == "manual_recovery"
    assert journal.list_restart_recovery() == []
    journal.close()


def test_restart_recovery_distinguishes_intent_effect_and_upload_boundaries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-journal.sqlite3"
    journal = EdgeJournal(path, edge_generation="edgegen-test")
    _intent(journal, operation_id="op-intent", attempt_id="attempt-intent")
    _intent(journal, operation_id="op-effect", attempt_id="attempt-effect")
    journal.mark_attempt_executing("op-effect", "attempt-effect", 1)
    _intent(journal, operation_id="op-result", attempt_id="attempt-result")
    journal.record_result(
        operation_id="op-result",
        attempt_id="attempt-result",
        fencing_token=1,
        outcome="blocked",
        result={"reason": "repo_busy"},
    )
    journal.close()

    with EdgeJournal(path, edge_generation="edgegen-test") as restarted:
        recovery = {
            item["attempt_id"]: item["recovery_action"]
            for item in restarted.list_restart_recovery()
        }

        assert recovery == {
            "attempt-intent": RECOVERY_EXECUTE_INTENT,
            "attempt-effect": RECOVERY_RECONCILE_EFFECT,
            "attempt-result": RECOVERY_UPLOAD_RESULT,
        }
        assert len(restarted.recovery_snapshot()["pending_outbox"]) == 1


@pytest.mark.parametrize("effect_state", ["executing", "effect_recorded"])
def test_manual_recovery_transition_is_terminal_and_idempotent(
    tmp_path: Path,
    effect_state: str,
) -> None:
    journal = EdgeJournal(
        tmp_path / f"{effect_state}.sqlite3", edge_generation="edgegen-test"
    )
    _intent(journal, operation_id="op-effect", attempt_id="attempt-effect")
    journal.mark_attempt_executing("op-effect", "attempt-effect", 1)
    if effect_state == "effect_recorded":
        journal.mark_effect_recorded(
            "op-effect", "attempt-effect", 1, effect={"result_hash": "hash-only"}
        )

    first = journal.mark_manual_recovery("op-effect", "attempt-effect", 1)
    second = journal.mark_manual_recovery("op-effect", "attempt-effect", 1)

    assert first["state"] == "manual_recovery"
    assert first["idempotent_replay"] is False
    assert second["state"] == "manual_recovery"
    assert second["idempotent_replay"] is True
    assert journal.list_restart_recovery() == []
    journal.close()


def test_edge_generation_and_projection_revision_persist_and_never_regress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "edge-journal.sqlite3"
    first = EdgeJournal(path, edge_generation="edgegen-stable")

    assert first.projection_identity() == {
        "edge_generation": "edgegen-stable",
        "projection_revision": 0,
    }
    assert first.advance_projection_revision(expected_revision=0) == 1
    first.close()

    left = EdgeJournal(path, edge_generation="edgegen-stable")
    right = EdgeJournal(path, edge_generation="edgegen-stable")
    assert left.projection_revision == 1
    assert left.persist_projection_revision(4) == 4
    assert right.persist_projection_revision(4) == 4
    assert left.next_projection_revision() == 5
    assert right.next_projection_revision() == 6
    with pytest.raises(EdgeJournalConflict, match="projection_revision_regression"):
        left.persist_projection_revision(5)
    with pytest.raises(EdgeJournalConflict, match="projection_revision_conflict"):
        right.advance_projection_revision(expected_revision=5)
    left.close()
    right.close()

    with pytest.raises(EdgeJournalConflict, match="edge_generation_conflict"):
        EdgeJournal(path, edge_generation="edgegen-replaced")
