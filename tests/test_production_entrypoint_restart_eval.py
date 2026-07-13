from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from scripts.production_entrypoint_restart_eval import (
    FixturePaths,
    compare_restart_snapshots,
    run_production_entrypoint_restart_eval,
)


def _comparison_snapshot() -> dict:
    return {
        "database_files": ["/tmp/fixture/hub.sqlite3", "/tmp/fixture/edge.sqlite3"],
        "config_sha256": "config-hash",
        "profile": {
            "path": "/tmp/fixture/edge-profile.json",
            "sha256": "profile-hash",
            "machine_id": "edge-a",
            "edge_generation": "edgegen-a",
            "hub_url": "http://127.0.0.1:8000",
        },
        "generations": ["edgegen-a"],
        "hub": {
            "integrity_check": ["ok"],
            "hub_id": "hub-a",
            "principal_ref": "principal-a",
            "mutation_count": 10,
            "max_event_revision": 12,
            "entities": {
                "hub.machine": {
                    "edge-a": {
                        "revision": 2,
                        "record": {"edge_generation": "edgegen-a"},
                    }
                },
                "hub.work_group": {
                    "group-a": {"revision": 3, "record": {"work_group_id": "group-a"}}
                },
                "hub.fleet_worker": {
                    "worker-a": {
                        "revision": 4,
                        "record": {"fleet_worker_ref": "worker-a"},
                    }
                },
                "hub.worker_projection": {
                    "worker-a": {
                        "revision": 5,
                        "record": {"fleet_worker_ref": "worker-a"},
                    }
                },
            },
            "operations": {
                "operation-a": {
                    "tool": "patchbay_worker_start",
                    "state": "succeeded",
                    "revision": 5,
                }
            },
        },
        "edge": {
            "integrity_check": ["ok"],
            "edge_generation": "edgegen-a",
            "projection_revision": 6,
            "intents": {"operation-a": {"action": "codex_worker_start"}},
            "attempts": {
                "attempt-a": {
                    "operation_id": "operation-a",
                    "state": "acknowledged",
                    "revision": 5,
                }
            },
            "outbox": {},
        },
    }


def test_fixture_paths_are_absolute_and_confined(tmp_path):
    paths = FixturePaths.under(tmp_path)
    root = tmp_path.resolve()

    for raw in paths.public_mapping().values():
        path = Path(raw)
        assert path.is_absolute()
        path.relative_to(root)


def test_restart_comparison_rejects_new_identity_and_revision_regression():
    before = _comparison_snapshot()
    after = deepcopy(before)
    after["hub"]["entities"]["hub.worker_projection"]["worker-b"] = {
        "revision": 1,
        "record": {"fleet_worker_ref": "worker-b"},
    }
    after["hub"]["operations"]["operation-a"]["revision"] = 4

    comparison = compare_restart_snapshots(before, after)

    assert comparison["passed"] is False
    assert comparison["entity_identity_checks"]["hub.worker_projection"] is False
    assert comparison["checks"]["operation_revisions_monotonic"] is False


def test_restart_comparison_allows_only_monotonic_revision_progress():
    before = _comparison_snapshot()
    after = deepcopy(before)
    after["hub"]["mutation_count"] += 3
    after["hub"]["max_event_revision"] += 2
    after["hub"]["entities"]["hub.machine"]["edge-a"]["revision"] += 1
    after["hub"]["operations"]["operation-a"]["revision"] += 1
    after["edge"]["projection_revision"] += 1
    after["edge"]["attempts"]["attempt-a"]["revision"] += 1

    comparison = compare_restart_snapshots(before, after)

    assert comparison["passed"] is True
    assert all(comparison["checks"].values())


def test_production_entrypoint_restart_eval(tmp_path):
    report = run_production_entrypoint_restart_eval(
        tmp_path,
        rehearse_old_schema=True,
    )

    assert report["status"] == "passed", report
    assert report["fixture_retained"] is True
    assert report["entrypoints"] == {
        "hub": "patchbay hub start",
        "hub_enrollment": "patchbay hub enroll-code create",
        "edge_enrollment": "patchbay edge enroll",
        "edge_service": "patchbay edge start",
        "hub_backup": "patchbay hub backup create --prepare-migration",
        "hub_restore": "patchbay hub backup restore",
        "hub_factory": "create_production_hub_v2_app",
        "edge_factory": "create_edge_v2_runner",
        "manual_runtime_adapters": False,
    }
    assert all(check["passed"] for check in report["checks"]), report["checks"]
    assert report["comparison"]["passed"] is True
    assert all(report["comparison"]["checks"].values())
    assert (
        report["before_restart"]["database_files"]
        == report["after_restart"]["database_files"]
    )
    assert len(report["durable_state"]["database_files"]) == 2
    assert len(report["durable_state"]["generations"]) == 1
    assert len(report["durable_state"]["worker_refs"]) == 1
    assert len(report["durable_state"]["operation_ids"]) == 4
    assert report["shutdowns"]["before_restart"]["clean"] is True
    assert report["shutdowns"]["after_restart"]["clean"] is True
    upgrade = report["migration_rehearsal"]
    assert upgrade["status"] == "passed", upgrade
    assert upgrade["fixture_kind"] == "separate_disposable_schema_upgrade"
    assert upgrade["entrypoints"]["manual_runtime_adapters"] is False
    assert all(check["passed"] for check in upgrade["checks"]), upgrade["checks"]
    assert upgrade["startup_refusal"]["return_code"] != 0
    assert upgrade["startup_refusal"]["refused"] is True
    assert upgrade["startup_refusal"]["reason"] == (
        "missing_validated_pre_migration_backup_marker"
    )
    assert upgrade["edge_startup_refusal"]["refused"] is True
    assert upgrade["backup"]["source_unchanged"] is True
    assert upgrade["backup"]["pre_migration_backup"]["source_schema_version"] == 2
    assert upgrade["backup"]["pre_migration_backup"]["target_schema_version"] == 3
    assert upgrade["backup"]["immutable_before"] == upgrade["backup"]["immutable_after"]
    assert upgrade["older"]["schema_version"] == 2
    assert upgrade["older_edge"]["schema_version"] == 2
    assert upgrade["migrated"]["schema_version"] == 3
    assert upgrade["migrated_edge"]["schema_version"] == 3
    assert upgrade["restarted"]["schema_version"] == 3
    assert upgrade["restarted_edge"]["schema_version"] == 3
    assert upgrade["restored"]["schema_version"] == 2
    assert upgrade["restored_edge"]["schema_version"] == 2
    assert (
        upgrade["older"]["authoritative_state_sha256"]
        == upgrade["migrated_before_edge"]["authoritative_state_sha256"]
    )
    assert (
        upgrade["older"]["authoritative_state_sha256"]
        == upgrade["restored"]["authoritative_state_sha256"]
    )
    assert (
        upgrade["older"]["revisions"]
        == upgrade["migrated_before_edge"]["revisions"]
    )
    assert upgrade["older"]["revisions"] == upgrade["restored"]["revisions"]
    for table in ("operation_intents", "operation_attempts"):
        assert (
            upgrade["older_edge"]["tables"][table]
            == upgrade["migrated_edge"]["tables"][table]
            == upgrade["restarted_edge"]["tables"][table]
            == upgrade["restored_edge"]["tables"][table]
        )
    older_receipts = upgrade["older_edge"]["outbox_receipts"]
    migrated_receipts = upgrade["migrated_edge"]["outbox_receipts"]
    assert set(migrated_receipts).issubset(older_receipts)
    assert all(
        older_receipts[receipt_id]["acknowledged"] is True
        for receipt_id in set(older_receipts).difference(migrated_receipts)
    )
    assert (
        upgrade["older_edge"]["tables"]["result_outbox"]
        == upgrade["restored_edge"]["tables"]["result_outbox"]
    )
    assert upgrade["restore"] == {
        "status": "restored",
        "restored": True,
        "fresh_path": True,
        "pre_migration_backup_marker_valid": True,
    }
    assert upgrade["edge_restore"] == {
        "status": "restored",
        "restored": True,
        "fresh_path": True,
        "pre_migration_backup_marker_valid": True,
    }

    fixture_root = Path(report["paths"]["fixture_root"])
    for raw in report["paths"].values():
        Path(raw).relative_to(fixture_root)
    evidence = Path(report["paths"]["evidence"]).read_text(encoding="utf-8")
    assert '"code": "PB-' not in evidence
    assert "Complete one read-only disposable turn" not in evidence
    for raw in upgrade["paths"].values():
        Path(raw).relative_to(fixture_root)
