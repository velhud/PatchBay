"""Administrative functions for Hub V1-to-V2 migration and cutover.

These functions provide the fail-closed mechanics needed to rehearse and
verify migration before the atomic Hub V2 cutover.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from patchbay.hub.store import STORE_VERSION, hub_state_path
from patchbay.hub.store_v2 import (
    ACTIVE_LEGACY_COMMAND_STATES,
    LEGACY_ENTITY_TYPES,
    LEGACY_RECOVERY_REQUIRED,
    SCHEMA_VERSION,
    HubStoreV2,
    hub_state_v2_path,
)
from patchbay.hub.tool_surface import (
    HUB_V2_CAPABILITY_MANIFEST_HASH,
    HUB_V2_CAPABILITY_SCHEMA_HASH,
    HUB_V2_CONTRACT_HASH,
    HUB_V2_CONTRACT_VERSION,
    HUB_V2_EXPECTED_TOOL_COUNT,
    HUB_V2_MANIFEST_HASH,
    HUB_V2_SCHEMA_HASH,
    HUB_V2_TOOL_NAMES,
    compute_hub_v2_contract_hash,
    compute_hub_v2_manifest_hash,
    compute_hub_v2_schema_hash,
    compute_tool_manifest_hash,
    compute_tool_schema_hash,
    get_hub_v2_tools,
    hub_v2_contract_manifest as build_hub_v2_contract_manifest,
    validate_hub_v2_registry,
)


BACKUP_MANIFEST_VERSION = 1
BACKUP_MANIFEST_SUFFIX = ".manifest.json"
_BACKUP_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "created_at",
        "source_path",
        "source_checksum_sha256",
        "source_size_bytes",
        "source_mtime_ns",
        "backup_path",
        "backup_checksum_sha256",
        "backup_size_bytes",
        "database_path",
        "v1_schema_version",
        "v2_schema_version",
        "contract_version",
        "manifest_hash",
        "schema_hash",
        "contract_hash",
    }
)

_V1_TOP_LEVEL_FIELDS = frozenset(
    {
        "version",
        "hub_id",
        "created_at",
        "enrollment_codes",
        "machines",
        "commands",
        "work_groups",
        "current_work_group_by_manager",
        "events",
    }
)
_V1_OBJECT_COLLECTIONS = (
    "enrollment_codes",
    "machines",
    "commands",
    "work_groups",
    "current_work_group_by_manager",
)
_V2_TABLES = frozenset(
    {
        "schema_metadata",
        "principals",
        "hub_identity",
        "legacy_imports",
        "entity_records",
        "operations",
        "attempts",
        "events",
        "payload_metadata",
    }
)
_V2_TABLE_COLUMNS = {
    "schema_metadata": (
        "singleton",
        "schema_version",
        "migration_lock",
        "migration_started_at",
        "updated_at",
        "v2_mutation_count",
    ),
    "principals": (
        "principal_ref",
        "principal_kind",
        "revision",
        "record_json",
        "created_at",
        "updated_at",
    ),
    "hub_identity": ("singleton", "hub_id", "principal_ref", "created_at"),
    "legacy_imports": (
        "import_id",
        "source_path",
        "source_checksum",
        "source_size_bytes",
        "source_mtime_ns",
        "source_version",
        "source_hub_id",
        "source_created_at",
        "counts_json",
        "recovery_required_count",
        "imported_at",
        "status",
    ),
    "entity_records": (
        "entity_type",
        "entity_id",
        "revision",
        "record_json",
        "legacy_classification",
        "source_import_id",
        "created_at",
        "updated_at",
    ),
    "operations": (
        "operation_id",
        "principal_ref",
        "tool",
        "logical_target",
        "idempotency_key",
        "semantic_payload_hash",
        "state",
        "revision",
        "parent_operation_id",
        "item_id",
        "result_json",
        "error_json",
        "created_at",
        "updated_at",
    ),
    "attempts": (
        "attempt_id",
        "operation_id",
        "machine_id",
        "edge_generation",
        "fencing_token",
        "state",
        "revision",
        "lease_expires_at",
        "result_json",
        "created_at",
        "updated_at",
    ),
    "events": (
        "event_revision",
        "event_id",
        "event_type",
        "operation_id",
        "entity_type",
        "entity_id",
        "entity_revision",
        "data_json",
        "legacy_classification",
        "source_import_id",
        "source_ordinal",
        "created_at",
    ),
    "payload_metadata": (
        "payload_id",
        "operation_id",
        "payload_kind",
        "checksum_sha256",
        "size_bytes",
        "storage_ref",
        "status",
        "revision",
        "expires_at",
        "acknowledged_at",
        "metadata_json",
        "created_at",
        "updated_at",
    ),
}
_V2_EXPLICIT_INDEXES = frozenset(
    {
        "one_operator_principal",
        "entity_records_import_idx",
        "operations_parent_idx",
        "attempts_operation_idx",
        "events_operation_idx",
        "events_entity_idx",
        "payload_metadata_operation_idx",
    }
)


class HubV2CLIError(RuntimeError):
    """Base error for the Hub V2 administrative surface."""


class HubV2MigrationBlocked(HubV2CLIError):
    """Raised when a migration or rollback gate fails closed."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report or {})


def exact_contract_manifest() -> dict[str, Any]:
    """Return the exact, deterministic Hub V2 contract and all contract hashes."""

    validate_hub_v2_registry()
    tools = get_hub_v2_tools()
    contract = build_hub_v2_contract_manifest(tools)
    result = {
        "status": "ok",
        "contract_version": HUB_V2_CONTRACT_VERSION,
        "tool_count": len(tools),
        "tool_names": [tool["name"] for tool in tools],
        "manifest_hash": compute_tool_manifest_hash(tools),
        "schema_hash": compute_tool_schema_hash(tools),
        "capability_manifest_hash": compute_hub_v2_manifest_hash(tools),
        "capability_schema_hash": compute_hub_v2_schema_hash(tools),
        "contract_hash": compute_hub_v2_contract_hash(tools),
        "contract": contract,
    }
    expected = {
        "tool_count": HUB_V2_EXPECTED_TOOL_COUNT,
        "tool_names": list(HUB_V2_TOOL_NAMES),
        "manifest_hash": HUB_V2_MANIFEST_HASH,
        "schema_hash": HUB_V2_SCHEMA_HASH,
        "capability_manifest_hash": HUB_V2_CAPABILITY_MANIFEST_HASH,
        "capability_schema_hash": HUB_V2_CAPABILITY_SCHEMA_HASH,
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    mismatches = [field for field, value in expected.items() if result[field] != value]
    if mismatches:
        raise HubV2MigrationBlocked(
            f"Hub V2 contract constants do not match the canonical registry: {', '.join(mismatches)}",
            report=result,
        )
    return result


def migration_dry_run(
    source_or_config: str | Path | Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
    database_path: str | Path | None = None,
    backup_path: str | Path | None = None,
    expected_source_checksum: str = "",
    expected_contract_hash: str = HUB_V2_CONTRACT_HASH,
    expected_manifest_hash: str = HUB_V2_MANIFEST_HASH,
    expected_schema_hash: str = HUB_V2_SCHEMA_HASH,
) -> dict[str, Any]:
    """Inspect a V1 migration without creating a backup or V2 database."""

    source, database = _resolve_state_paths(
        source_or_config, source_path=source_path, database_path=database_path
    )
    source_report = _inspect_v1_source(source)
    backup = _resolve_backup_path(source, source_report["checksum_sha256"], backup_path)
    contract = _contract_gate(
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    blockers = list(contract["blockers"])
    if (
        expected_source_checksum
        and source_report["checksum_sha256"] != expected_source_checksum
    ):
        blockers.append(
            {
                "code": "source_checksum_mismatch",
                "expected": expected_source_checksum,
                "actual": source_report["checksum_sha256"],
            }
        )
    if source_report["active_legacy_commands"]:
        blockers.append(
            {
                "code": "active_legacy_commands",
                "commands": source_report["active_legacy_commands"],
            }
        )

    doctor = v2_store_doctor(
        source_or_config,
        database_path=database,
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    matching_import = False
    if doctor["exists"]:
        if not doctor["ready"]:
            blockers.append({"code": "v2_store_not_ready", "errors": doctor["errors"]})
        matching_import = any(
            item["checksum_sha256"] == source_report["checksum_sha256"]
            for item in doctor.get("legacy_imports", [])
        )
        conflicting_imports = [
            item["checksum_sha256"]
            for item in doctor.get("legacy_imports", [])
            if item["checksum_sha256"] != source_report["checksum_sha256"]
        ]
        if conflicting_imports:
            blockers.append(
                {
                    "code": "different_legacy_snapshot_already_imported",
                    "checksums": conflicting_imports,
                }
            )
        if doctor.get("v2_mutation_count", 0) > 0:
            blockers.append(
                {
                    "code": "cutover_already_committed",
                    "count": doctor["v2_mutation_count"],
                }
            )

    return {
        "status": "blocked"
        if blockers
        else ("already_applied" if matching_import else "ready"),
        "can_apply": not blockers,
        "already_applied": matching_import,
        "source": source_report,
        "database_path": str(database),
        "backup_path": str(backup),
        "backup_manifest_path": str(_backup_manifest_path(backup)),
        "contract": contract,
        "store": doctor,
        "blockers": blockers,
        "side_effects": {
            "source_mutated": False,
            "backup_created": False,
            "database_created": False,
        },
    }


def create_v1_backup(
    source_or_config: str | Path | Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
    database_path: str | Path | None = None,
    backup_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create an immutable checksum-named V1 snapshot and evidence manifest."""

    source, database = _resolve_state_paths(
        source_or_config, source_path=source_path, database_path=database_path
    )
    source_report = _inspect_v1_source(source, include_bytes=True)
    raw = source_report.pop("_raw")
    backup = _resolve_backup_path(source, source_report["checksum_sha256"], backup_path)
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    created = False
    if backup.exists():
        if _sha256_file(backup) != source_report["checksum_sha256"]:
            raise HubV2MigrationBlocked(
                f"Existing V1 backup checksum mismatch: {backup}"
            )
    else:
        _write_exclusive_snapshot(backup, raw)
        created = True

    manifest_path = _backup_manifest_path(backup)
    manifest = {
        "manifest_version": BACKUP_MANIFEST_VERSION,
        "created_at": time.time(),
        "source_path": str(source.resolve(strict=False)),
        "source_checksum_sha256": source_report["checksum_sha256"],
        "source_size_bytes": source_report["source_size_bytes"],
        "source_mtime_ns": source_report["source_mtime_ns"],
        "backup_path": str(backup.resolve(strict=False)),
        "backup_checksum_sha256": source_report["checksum_sha256"],
        "backup_size_bytes": len(raw),
        "database_path": str(database.resolve(strict=False)),
        "v1_schema_version": STORE_VERSION,
        "v2_schema_version": SCHEMA_VERSION,
        "contract_version": HUB_V2_CONTRACT_VERSION,
        "manifest_hash": HUB_V2_MANIFEST_HASH,
        "schema_hash": HUB_V2_SCHEMA_HASH,
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    if manifest_path.exists():
        existing = _read_backup_manifest(manifest_path)
        stable_fields = {
            key: value for key, value in manifest.items() if key != "created_at"
        }
        existing_stable = {
            key: value for key, value in existing.items() if key != "created_at"
        }
        if existing_stable != stable_fields:
            raise HubV2MigrationBlocked(
                f"Existing V1 backup manifest does not match the snapshot: {manifest_path}"
            )
        manifest = existing
    else:
        _write_exclusive_snapshot(
            manifest_path,
            (
                json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
            ).encode("utf-8"),
        )

    validation = validate_backup_checksum(
        source,
        backup_path=backup,
        database_path=database,
        expected_checksum=source_report["checksum_sha256"],
        expected_contract_hash=HUB_V2_CONTRACT_HASH,
        expected_manifest_hash=HUB_V2_MANIFEST_HASH,
        expected_schema_hash=HUB_V2_SCHEMA_HASH,
    )
    if not validation["valid"]:
        raise HubV2MigrationBlocked("V1 backup validation failed", report=validation)
    return {**validation, "created": created, "manifest": manifest}


def validate_backup_checksum(
    source_or_config: str | Path | Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
    backup_path: str | Path,
    database_path: str | Path | None = None,
    expected_checksum: str = "",
    expected_contract_hash: str = HUB_V2_CONTRACT_HASH,
    expected_manifest_hash: str = HUB_V2_MANIFEST_HASH,
    expected_schema_hash: str = HUB_V2_SCHEMA_HASH,
) -> dict[str, Any]:
    """Validate source, backup, evidence manifest, and exact contract checksums."""

    source, database = _resolve_state_paths(
        source_or_config, source_path=source_path, database_path=database_path
    )
    backup = Path(backup_path).expanduser()
    errors: list[dict[str, Any]] = []
    try:
        source_report = _inspect_v1_source(source)
    except HubV2CLIError as error:
        source_report = {
            "path": str(source),
            "checksum_sha256": "",
            "source_size_bytes": 0,
        }
        errors.append({"code": "source_invalid", "message": str(error)})

    backup_checksum = ""
    backup_size = 0
    if not backup.is_file():
        errors.append({"code": "backup_missing", "path": str(backup)})
    else:
        backup_size = backup.stat().st_size
        backup_checksum = _sha256_file(backup)

    source_checksum = str(source_report.get("checksum_sha256") or "")
    wanted_checksum = expected_checksum or source_checksum
    if wanted_checksum and source_checksum != wanted_checksum:
        errors.append(
            {
                "code": "source_checksum_mismatch",
                "expected": wanted_checksum,
                "actual": source_checksum,
            }
        )
    if wanted_checksum and backup_checksum != wanted_checksum:
        errors.append(
            {
                "code": "backup_checksum_mismatch",
                "expected": wanted_checksum,
                "actual": backup_checksum,
            }
        )
    if source_checksum and backup_checksum and source_checksum != backup_checksum:
        errors.append(
            {
                "code": "source_backup_checksum_mismatch",
                "source": source_checksum,
                "backup": backup_checksum,
            }
        )

    contract = _contract_gate(
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    errors.extend(contract["blockers"])
    manifest_path = _backup_manifest_path(backup)
    manifest: dict[str, Any] = {}
    if not manifest_path.is_file():
        errors.append({"code": "backup_manifest_missing", "path": str(manifest_path)})
    else:
        try:
            manifest = _read_backup_manifest(manifest_path)
        except HubV2CLIError as error:
            errors.append({"code": "backup_manifest_invalid", "message": str(error)})
        else:
            if set(manifest) != _BACKUP_MANIFEST_FIELDS:
                errors.append(
                    {
                        "code": "backup_manifest_field_mismatch",
                        "missing": sorted(_BACKUP_MANIFEST_FIELDS - set(manifest)),
                        "unexpected": sorted(set(manifest) - _BACKUP_MANIFEST_FIELDS),
                    }
                )
            expected_manifest_values = {
                "manifest_version": BACKUP_MANIFEST_VERSION,
                "source_path": str(source.resolve(strict=False)),
                "source_checksum_sha256": source_checksum,
                "source_size_bytes": int(source_report.get("source_size_bytes") or 0),
                "source_mtime_ns": int(source_report.get("source_mtime_ns") or 0),
                "backup_path": str(backup.resolve(strict=False)),
                "backup_checksum_sha256": backup_checksum,
                "backup_size_bytes": backup_size,
                "database_path": str(database.resolve(strict=False)),
                "v1_schema_version": STORE_VERSION,
                "v2_schema_version": SCHEMA_VERSION,
                "contract_version": HUB_V2_CONTRACT_VERSION,
                "manifest_hash": HUB_V2_MANIFEST_HASH,
                "schema_hash": HUB_V2_SCHEMA_HASH,
                "contract_hash": HUB_V2_CONTRACT_HASH,
            }
            for field, expected in expected_manifest_values.items():
                if manifest.get(field) != expected:
                    errors.append(
                        {
                            "code": "backup_manifest_mismatch",
                            "field": field,
                            "expected": expected,
                            "actual": manifest.get(field),
                        }
                    )
            if not isinstance(manifest.get("created_at"), (int, float)) or isinstance(
                manifest.get("created_at"), bool
            ):
                errors.append(
                    {"code": "backup_manifest_mismatch", "field": "created_at"}
                )

    return {
        "status": "ok" if not errors else "failed",
        "valid": not errors,
        "source_path": str(source.resolve(strict=False)),
        "backup_path": str(backup.resolve(strict=False)),
        "manifest_path": str(manifest_path.resolve(strict=False)),
        "source_checksum_sha256": source_checksum,
        "backup_checksum_sha256": backup_checksum,
        "expected_checksum_sha256": wanted_checksum,
        "source_size_bytes": int(source_report.get("source_size_bytes") or 0),
        "backup_size_bytes": backup_size,
        "contract": contract,
        "manifest": manifest,
        "errors": errors,
    }


def migration_apply(
    source_or_config: str | Path | Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
    database_path: str | Path | None = None,
    backup_path: str | Path | None = None,
    expected_source_checksum: str = "",
    expected_contract_hash: str = HUB_V2_CONTRACT_HASH,
    expected_manifest_hash: str = HUB_V2_MANIFEST_HASH,
    expected_schema_hash: str = HUB_V2_SCHEMA_HASH,
) -> dict[str, Any]:
    """Back up a quiescent V1 snapshot and import it into the V2 store."""

    dry_run = migration_dry_run(
        source_or_config,
        source_path=source_path,
        database_path=database_path,
        backup_path=backup_path,
        expected_source_checksum=expected_source_checksum,
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    if not dry_run["can_apply"]:
        raise HubV2MigrationBlocked("Hub V2 migration preflight failed", report=dry_run)

    source = Path(dry_run["source"]["path"])
    database = Path(dry_run["database_path"])
    backup = Path(dry_run["backup_path"])
    source_before = dry_run["source"]
    backup_report = create_v1_backup(
        source,
        database_path=database,
        backup_path=backup,
    )

    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with HubStoreV2(database) as store:
        import_report = store.import_v1_json(backup)
        schema = store.schema_info()
    if import_report["checksum_sha256"] != source_before["checksum_sha256"]:
        raise HubV2MigrationBlocked(
            "Imported V1 snapshot checksum differs from the approved source"
        )
    if import_report["legacy_recovery_required_count"]:
        raise HubV2MigrationBlocked(
            "Imported V1 snapshot unexpectedly contains active legacy commands",
            report=import_report,
        )
    if schema["v2_mutation_count"] != 0:
        raise HubV2MigrationBlocked(
            "Migration unexpectedly recorded a V2 domain mutation",
            report=schema,
        )

    source_after = _inspect_v1_source(source)
    if (
        source_after["checksum_sha256"] != source_before["checksum_sha256"]
        or source_after["source_size_bytes"] != source_before["source_size_bytes"]
        or source_after["source_mtime_ns"] != source_before["source_mtime_ns"]
    ):
        raise HubV2MigrationBlocked(
            "V1 source changed during migration; keep V1 stopped and investigate before cutover",
            report={"before": source_before, "after": source_after},
        )

    status = migration_status(
        source,
        database_path=database,
        backup_path=backup,
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    if status["status"] != "applied_ready_for_cutover":
        raise HubV2MigrationBlocked(
            "Hub V2 migration post-validation failed", report=status
        )
    return {
        "status": "applied",
        "source": source_after,
        "backup": backup_report,
        "import": import_report,
        "store": status["store"],
        "rollback": status["rollback"],
        "contract": status["contract"],
        "source_unchanged": True,
    }


def migration_status(
    source_or_config: str | Path | Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
    database_path: str | Path | None = None,
    backup_path: str | Path | None = None,
    expected_contract_hash: str = HUB_V2_CONTRACT_HASH,
    expected_manifest_hash: str = HUB_V2_MANIFEST_HASH,
    expected_schema_hash: str = HUB_V2_SCHEMA_HASH,
) -> dict[str, Any]:
    """Return non-mutating migration, cutover, and rollback status."""

    source, database = _resolve_state_paths(
        source_or_config, source_path=source_path, database_path=database_path
    )
    contract = _contract_gate(
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    errors = list(contract["blockers"])
    try:
        source_report = _inspect_v1_source(source)
    except HubV2CLIError as error:
        source_report = {
            "path": str(source),
            "checksum_sha256": "",
            "active_legacy_commands": [],
        }
        errors.append({"code": "source_invalid", "message": str(error)})

    checksum = str(source_report.get("checksum_sha256") or "")
    backup = _resolve_backup_path(source, checksum or "unknown", backup_path)
    doctor = v2_store_doctor(
        source_or_config,
        database_path=database,
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    matching_import = next(
        (
            item
            for item in doctor.get("legacy_imports", [])
            if item["checksum_sha256"] == checksum
        ),
        None,
    )
    if source_report.get("active_legacy_commands"):
        errors.append(
            {
                "code": "active_legacy_commands",
                "commands": source_report["active_legacy_commands"],
            }
        )
    if doctor["exists"] and not doctor["ready"]:
        errors.append({"code": "v2_store_not_ready", "errors": doctor["errors"]})
    if doctor.get("legacy_imports") and matching_import is None:
        errors.append({"code": "source_checksum_not_imported", "checksum": checksum})
    if matching_import and matching_import.get("counts") != source_report.get("counts"):
        errors.append(
            {
                "code": "source_import_count_metadata_mismatch",
                "source": source_report.get("counts"),
                "import": matching_import.get("counts"),
            }
        )
    if (
        doctor.get("exists")
        and doctor.get("v2_mutation_count", 0) > 0
        and matching_import is None
    ):
        errors.append(
            {
                "code": "v2_mutations_exist_before_legacy_import",
                "count": doctor["v2_mutation_count"],
            }
        )

    backup_validation: dict[str, Any] = {
        "status": "missing",
        "valid": False,
        "backup_path": str(backup),
        "errors": [{"code": "backup_missing"}],
    }
    if backup.is_file() or _backup_manifest_path(backup).is_file():
        backup_validation = validate_backup_checksum(
            source,
            backup_path=backup,
            database_path=database,
            expected_checksum=checksum,
            expected_contract_hash=expected_contract_hash,
            expected_manifest_hash=expected_manifest_hash,
            expected_schema_hash=expected_schema_hash,
        )
        if not backup_validation["valid"]:
            errors.append(
                {
                    "code": "backup_validation_failed",
                    "errors": backup_validation["errors"],
                }
            )

    rollback = rollback_eligibility(
        source,
        database_path=database,
        backup_path=backup,
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
        _doctor=doctor,
        _source_report=source_report,
        _backup_validation=backup_validation,
    )
    if errors:
        state = "blocked"
    elif matching_import and backup_validation["valid"]:
        state = (
            "cutover_committed"
            if int(doctor.get("v2_mutation_count") or 0) > 0
            else "applied_ready_for_cutover"
        )
    elif doctor["exists"]:
        state = "ready_to_apply"
    else:
        state = "not_started"
    return {
        "status": state,
        "source": source_report,
        "database_path": str(database),
        "backup": backup_validation,
        "store": doctor,
        "matching_import": matching_import or {},
        "contract": contract,
        "rollback": rollback,
        "errors": errors,
    }


def v2_store_doctor(
    database_or_config: str | Path | Mapping[str, Any],
    *,
    database_path: str | Path | None = None,
    expected_contract_hash: str = HUB_V2_CONTRACT_HASH,
    expected_manifest_hash: str = HUB_V2_MANIFEST_HASH,
    expected_schema_hash: str = HUB_V2_SCHEMA_HASH,
) -> dict[str, Any]:
    """Inspect a V2 SQLite store read-only and reject any schema drift."""

    database = _resolve_database_path(database_or_config, database_path=database_path)
    contract = _contract_gate(
        expected_contract_hash=expected_contract_hash,
        expected_manifest_hash=expected_manifest_hash,
        expected_schema_hash=expected_schema_hash,
    )
    errors = list(contract["blockers"])
    result: dict[str, Any] = {
        "status": "failed",
        "ready": False,
        "exists": database.is_file(),
        "database_path": str(database),
        "schema_version": None,
        "user_version": None,
        "migration_lock": None,
        "v2_mutation_count": 0,
        "journal_mode": "",
        "integrity_check": [],
        "foreign_key_errors": [],
        "tables": [],
        "indexes": [],
        "legacy_imports": [],
        "legacy_recovery_required_count": 0,
        "contract": contract,
        "errors": errors,
    }
    if not database.is_file():
        result["errors"] = [
            *errors,
            {"code": "database_missing", "path": str(database)},
        ]
        return result

    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.DatabaseError as error:
        result["errors"] = [
            *errors,
            {"code": "database_open_failed", "message": str(error)},
        ]
        return result
    try:
        result["journal_mode"] = str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        result["user_version"] = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        result["integrity_check"] = [
            str(row[0])
            for row in connection.execute("PRAGMA integrity_check").fetchall()
        ]
        result["foreign_key_errors"] = [
            list(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_")
        }
        result["tables"] = sorted(tables)
        if tables != _V2_TABLES:
            errors.append(
                {
                    "code": "schema_table_mismatch",
                    "missing": sorted(_V2_TABLES - tables),
                    "unexpected": sorted(tables - _V2_TABLES),
                }
            )
        for table in sorted(tables.intersection(_V2_TABLE_COLUMNS)):
            columns = tuple(
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            )
            if columns != _V2_TABLE_COLUMNS[table]:
                errors.append(
                    {
                        "code": "schema_column_mismatch",
                        "table": table,
                        "expected": list(_V2_TABLE_COLUMNS[table]),
                        "actual": list(columns),
                    }
                )
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_autoindex_")
        }
        result["indexes"] = sorted(indexes)
        if indexes != _V2_EXPLICIT_INDEXES:
            errors.append(
                {
                    "code": "schema_index_mismatch",
                    "missing": sorted(_V2_EXPLICIT_INDEXES - indexes),
                    "unexpected": sorted(indexes - _V2_EXPLICIT_INDEXES),
                }
            )
        if "schema_metadata" in tables:
            rows = connection.execute(
                "SELECT * FROM schema_metadata WHERE singleton = 1"
            ).fetchall()
            if len(rows) != 1:
                errors.append(
                    {"code": "schema_metadata_cardinality", "count": len(rows)}
                )
            else:
                metadata = rows[0]
                result["schema_version"] = int(metadata["schema_version"])
                result["migration_lock"] = metadata["migration_lock"]
                result["v2_mutation_count"] = int(metadata["v2_mutation_count"])
                if result["schema_version"] != SCHEMA_VERSION:
                    errors.append(
                        {
                            "code": "schema_version_mismatch",
                            "expected": SCHEMA_VERSION,
                            "actual": result["schema_version"],
                        }
                    )
                if result["migration_lock"]:
                    errors.append(
                        {
                            "code": "migration_lock_held",
                            "owner": result["migration_lock"],
                        }
                    )
        if result["user_version"] != SCHEMA_VERSION:
            errors.append(
                {
                    "code": "sqlite_user_version_mismatch",
                    "expected": SCHEMA_VERSION,
                    "actual": result["user_version"],
                }
            )
        if result["journal_mode"] != "wal":
            errors.append(
                {
                    "code": "journal_mode_mismatch",
                    "expected": "wal",
                    "actual": result["journal_mode"],
                }
            )
        if result["integrity_check"] != ["ok"]:
            errors.append(
                {"code": "integrity_check_failed", "details": result["integrity_check"]}
            )
        if result["foreign_key_errors"]:
            errors.append(
                {
                    "code": "foreign_key_check_failed",
                    "details": result["foreign_key_errors"],
                }
            )

        if {"principals", "hub_identity"}.issubset(tables):
            principals = int(
                connection.execute("SELECT COUNT(*) FROM principals").fetchone()[0]
            )
            identities = int(
                connection.execute(
                    "SELECT COUNT(*) FROM hub_identity WHERE singleton = 1"
                ).fetchone()[0]
            )
            if principals != 1 or identities != 1:
                errors.append(
                    {
                        "code": "operator_identity_mismatch",
                        "principal_count": principals,
                        "identity_count": identities,
                    }
                )

        if "legacy_imports" in tables:
            for row in connection.execute(
                "SELECT * FROM legacy_imports ORDER BY imported_at, import_id"
            ).fetchall():
                try:
                    counts = json.loads(row["counts_json"])
                except (TypeError, json.JSONDecodeError):
                    counts = None
                item = {
                    "import_id": str(row["import_id"]),
                    "source_path": str(row["source_path"]),
                    "checksum_sha256": str(row["source_checksum"]),
                    "source_size_bytes": int(row["source_size_bytes"]),
                    "counts": counts,
                    "legacy_recovery_required_count": int(
                        row["recovery_required_count"]
                    ),
                    "status": str(row["status"]),
                }
                result["legacy_imports"].append(item)
                result["legacy_recovery_required_count"] += item[
                    "legacy_recovery_required_count"
                ]
                if item["status"] != "complete" or not isinstance(counts, dict):
                    errors.append(
                        {
                            "code": "legacy_import_record_invalid",
                            "import_id": item["import_id"],
                        }
                    )
                elif {"entity_records", "events"}.issubset(tables):
                    actual_entities = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM entity_records WHERE source_import_id = ?",
                            (item["import_id"],),
                        ).fetchone()[0]
                    )
                    actual_events = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM events WHERE source_import_id = ?",
                            (item["import_id"],),
                        ).fetchone()[0]
                    )
                    expected_entities = sum(
                        int(counts.get(field) or 0) for field in _V1_OBJECT_COLLECTIONS
                    )
                    expected_events = int(counts.get("events") or 0)
                    if (
                        actual_entities != expected_entities
                        or actual_events != expected_events
                    ):
                        errors.append(
                            {
                                "code": "legacy_import_count_mismatch",
                                "import_id": item["import_id"],
                                "expected_entities": expected_entities,
                                "actual_entities": actual_entities,
                                "expected_events": expected_events,
                                "actual_events": actual_events,
                            }
                        )

        if "entity_records" in tables:
            active_rows = connection.execute(
                "SELECT entity_id, record_json, legacy_classification FROM entity_records WHERE entity_type = ?",
                (LEGACY_ENTITY_TYPES["commands"],),
            ).fetchall()
            active_classified = 0
            for row in active_rows:
                try:
                    record = json.loads(row["record_json"])
                except (TypeError, json.JSONDecodeError):
                    errors.append(
                        {
                            "code": "legacy_command_json_invalid",
                            "command_id": row["entity_id"],
                        }
                    )
                    continue
                is_active = (
                    str(record.get("state") or "").lower()
                    in ACTIVE_LEGACY_COMMAND_STATES
                )
                is_recovery = (
                    str(row["legacy_classification"]) == LEGACY_RECOVERY_REQUIRED
                )
                if is_recovery:
                    active_classified += 1
                if is_active != is_recovery:
                    errors.append(
                        {
                            "code": "legacy_command_classification_mismatch",
                            "command_id": row["entity_id"],
                        }
                    )
            if active_classified != result["legacy_recovery_required_count"]:
                errors.append(
                    {
                        "code": "legacy_recovery_count_mismatch",
                        "imports": result["legacy_recovery_required_count"],
                        "entities": active_classified,
                    }
                )
            if active_classified:
                errors.append(
                    {"code": "active_legacy_commands", "count": active_classified}
                )
    except sqlite3.DatabaseError as error:
        errors.append({"code": "database_query_failed", "message": str(error)})
    finally:
        connection.close()

    result["errors"] = errors
    result["ready"] = not errors
    result["status"] = "ok" if result["ready"] else "failed"
    return result


def rollback_eligibility(
    source_or_config: str | Path | Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
    database_path: str | Path | None = None,
    backup_path: str | Path | None = None,
    expected_contract_hash: str = HUB_V2_CONTRACT_HASH,
    expected_manifest_hash: str = HUB_V2_MANIFEST_HASH,
    expected_schema_hash: str = HUB_V2_SCHEMA_HASH,
    _doctor: Mapping[str, Any] | None = None,
    _source_report: Mapping[str, Any] | None = None,
    _backup_validation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Determine whether the unchanged V1 state may still be restarted safely."""

    source, database = _resolve_state_paths(
        source_or_config, source_path=source_path, database_path=database_path
    )
    reasons: list[dict[str, Any]] = []
    source_report = dict(_source_report or {})
    if not source_report:
        try:
            source_report = _inspect_v1_source(source)
        except HubV2CLIError as error:
            reasons.append({"code": "source_invalid", "message": str(error)})
            source_report = {"checksum_sha256": "", "active_legacy_commands": []}
    checksum = str(source_report.get("checksum_sha256") or "")
    backup = _resolve_backup_path(source, checksum or "unknown", backup_path)
    doctor = dict(
        _doctor
        or v2_store_doctor(
            source_or_config,
            database_path=database,
            expected_contract_hash=expected_contract_hash,
            expected_manifest_hash=expected_manifest_hash,
            expected_schema_hash=expected_schema_hash,
        )
    )
    if not doctor.get("exists"):
        reasons.append({"code": "migration_not_applied"})
    elif not doctor.get("ready"):
        reasons.append(
            {"code": "v2_store_not_ready", "errors": doctor.get("errors", [])}
        )
    if int(doctor.get("v2_mutation_count") or 0) > 0:
        reasons.append(
            {
                "code": "first_v2_mutation_already_recorded",
                "v2_mutation_count": int(doctor.get("v2_mutation_count") or 0),
            }
        )
    matching_import = any(
        item.get("checksum_sha256") == checksum
        for item in doctor.get("legacy_imports", [])
    )
    if doctor.get("exists") and not matching_import:
        reasons.append(
            {"code": "approved_v1_snapshot_not_imported", "checksum": checksum}
        )
    if source_report.get("active_legacy_commands"):
        reasons.append(
            {
                "code": "active_legacy_commands",
                "commands": source_report["active_legacy_commands"],
            }
        )

    backup_validation = dict(_backup_validation or {})
    if not backup_validation:
        backup_validation = validate_backup_checksum(
            source,
            backup_path=backup,
            database_path=database,
            expected_checksum=checksum,
            expected_contract_hash=expected_contract_hash,
            expected_manifest_hash=expected_manifest_hash,
            expected_schema_hash=expected_schema_hash,
        )
    if not backup_validation.get("valid"):
        reasons.append(
            {
                "code": "backup_validation_failed",
                "errors": backup_validation.get("errors", []),
            }
        )

    eligible = not reasons
    mutation_recorded = int(doctor.get("v2_mutation_count") or 0) > 0
    return {
        "status": "eligible" if eligible else "ineligible",
        "eligible": eligible,
        "database_path": str(database),
        "source_path": str(source),
        "backup_path": str(backup),
        "v2_mutation_count": int(doctor.get("v2_mutation_count") or 0),
        "matching_import": matching_import,
        "reasons": reasons,
        "rollback_mode": (
            "restart_unchanged_v1"
            if eligible
            else "restore_v2_database_or_use_separate_single_machine_endpoint"
            if mutation_recorded
            else "blocked_pending_repair"
        ),
    }


def _resolve_state_paths(
    source_or_config: str | Path | Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
    database_path: str | Path | None = None,
) -> tuple[Path, Path]:
    if isinstance(source_or_config, Mapping):
        source = (
            Path(source_path).expanduser()
            if source_path is not None
            else hub_state_path(source_or_config)
        )
        database = (
            Path(database_path).expanduser()
            if database_path is not None
            else hub_state_v2_path(source_or_config)
        )
    else:
        source = Path(source_path or source_or_config).expanduser()
        if database_path is not None:
            database = Path(database_path).expanduser()
        else:
            stem = source.name.removesuffix(source.suffix)
            database = source.with_name(f"{stem}-v2.sqlite3")
    return source.resolve(strict=False), database.resolve(strict=False)


def _resolve_database_path(
    database_or_config: str | Path | Mapping[str, Any],
    *,
    database_path: str | Path | None,
) -> Path:
    if database_path is not None:
        return Path(database_path).expanduser().resolve(strict=False)
    if isinstance(database_or_config, Mapping):
        return hub_state_v2_path(database_or_config).resolve(strict=False)
    return Path(database_or_config).expanduser().resolve(strict=False)


def _resolve_backup_path(
    source: Path, checksum: str, backup_path: str | Path | None
) -> Path:
    if backup_path is not None:
        return Path(backup_path).expanduser().resolve(strict=False)
    return source.with_name(f"{source.name}.pre-v2-{checksum}.bak").resolve(
        strict=False
    )


def _backup_manifest_path(backup: Path) -> Path:
    return Path(str(backup) + BACKUP_MANIFEST_SUFFIX)


def _contract_gate(
    *,
    expected_contract_hash: str,
    expected_manifest_hash: str,
    expected_schema_hash: str,
) -> dict[str, Any]:
    manifest = exact_contract_manifest()
    blockers: list[dict[str, Any]] = []
    expected = {
        "contract_hash": expected_contract_hash,
        "manifest_hash": expected_manifest_hash,
        "schema_hash": expected_schema_hash,
    }
    for field, value in expected.items():
        if not value or value != manifest[field]:
            blockers.append(
                {
                    "code": f"{field}_mismatch",
                    "expected": value,
                    "actual": manifest[field],
                }
            )
    return {
        "contract_version": manifest["contract_version"],
        "tool_count": manifest["tool_count"],
        "manifest_hash": manifest["manifest_hash"],
        "schema_hash": manifest["schema_hash"],
        "contract_hash": manifest["contract_hash"],
        "matches_expected": not blockers,
        "blockers": blockers,
    }


def _inspect_v1_source(path: Path, *, include_bytes: bool = False) -> dict[str, Any]:
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise HubV2CLIError(f"Cannot read V1 Hub state: {path}") from error
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise HubV2MigrationBlocked(
            f"V1 Hub state changed while it was being read: {path}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HubV2CLIError(f"V1 Hub state is corrupt JSON: {path}") from error
    if not isinstance(payload, dict):
        raise HubV2CLIError("V1 Hub state schema mismatch: root must be an object")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise HubV2CLIError(
            f"V1 Hub state contains non-finite or invalid JSON values: {path}"
        ) from error
    fields = set(payload)
    if fields != _V1_TOP_LEVEL_FIELDS:
        raise HubV2CLIError(
            "V1 Hub state schema mismatch: "
            f"missing={sorted(_V1_TOP_LEVEL_FIELDS - fields)!r}, "
            f"unexpected={sorted(fields - _V1_TOP_LEVEL_FIELDS)!r}"
        )
    if (
        not isinstance(payload.get("version"), int)
        or isinstance(payload["version"], bool)
        or payload["version"] != STORE_VERSION
    ):
        raise HubV2CLIError(
            f"V1 Hub state schema version mismatch: expected {STORE_VERSION}, got {payload.get('version')!r}"
        )
    if not isinstance(payload.get("hub_id"), str) or not payload["hub_id"].strip():
        raise HubV2CLIError(
            "V1 Hub state schema mismatch: hub_id must be a non-empty string"
        )
    if not isinstance(payload.get("created_at"), (int, float)) or isinstance(
        payload["created_at"], bool
    ):
        raise HubV2CLIError("V1 Hub state schema mismatch: created_at must be numeric")
    for field in _V1_OBJECT_COLLECTIONS:
        if not isinstance(payload.get(field), dict):
            raise HubV2CLIError(
                f"V1 Hub state schema mismatch: {field} must be an object"
            )
    if not isinstance(payload.get("events"), list):
        raise HubV2CLIError("V1 Hub state schema mismatch: events must be an array")
    for field in ("enrollment_codes", "machines", "commands", "work_groups"):
        for entity_id, record in payload[field].items():
            if not isinstance(record, dict):
                raise HubV2CLIError(
                    f"V1 Hub state schema mismatch: {field}/{entity_id} must be an object"
                )
    for ordinal, event in enumerate(payload["events"]):
        if not isinstance(event, dict):
            raise HubV2CLIError(
                f"V1 Hub state schema mismatch: event {ordinal} must be an object"
            )

    active = [
        {"command_id": str(command_id), "state": str(record.get("state") or "").lower()}
        for command_id, record in payload["commands"].items()
        if str(record.get("state") or "").lower() in ACTIVE_LEGACY_COMMAND_STATES
    ]
    active.sort(key=lambda item: item["command_id"])
    report: dict[str, Any] = {
        "path": str(path.resolve(strict=False)),
        "checksum_sha256": hashlib.sha256(raw).hexdigest(),
        "source_size_bytes": len(raw),
        "source_mtime_ns": int(after.st_mtime_ns),
        "schema_version": int(payload["version"]),
        "hub_id": payload["hub_id"],
        "counts": {
            "enrollment_codes": len(payload["enrollment_codes"]),
            "machines": len(payload["machines"]),
            "commands": len(payload["commands"]),
            "work_groups": len(payload["work_groups"]),
            "current_work_group_by_manager": len(
                payload["current_work_group_by_manager"]
            ),
            "events": len(payload["events"]),
        },
        "active_legacy_commands": active,
        "legacy_recovery_required_count": len(active),
    }
    if include_bytes:
        report["_raw"] = raw
    return report


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise HubV2CLIError(f"Cannot checksum file: {path}") from error
    return digest.hexdigest()


def _write_exclusive_snapshot(path: Path, raw: bytes) -> None:
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            if _sha256_file(path) != hashlib.sha256(raw).hexdigest():
                raise HubV2MigrationBlocked(
                    f"Existing snapshot differs from requested content: {path}"
                )
    except OSError as error:
        raise HubV2CLIError(f"Cannot create immutable snapshot: {path}") from error
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _read_backup_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HubV2CLIError(f"Backup manifest is invalid: {path}") from error
    if not isinstance(payload, dict):
        raise HubV2CLIError(f"Backup manifest root must be an object: {path}")
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


# Explicit aliases keep future parser wiring independent of internal naming.
hub_v2_migration_dry_run = migration_dry_run
hub_v2_migration_apply = migration_apply
hub_v2_migration_status = migration_status
hub_v2_backup_create = create_v1_backup
hub_v2_backup_validate = validate_backup_checksum
hub_v2_store_doctor = v2_store_doctor
hub_v2_contract_manifest = exact_contract_manifest
hub_v2_rollback_eligibility = rollback_eligibility
backup_checksum_validate = validate_backup_checksum
contract_manifest = exact_contract_manifest
store_doctor = v2_store_doctor


__all__ = [
    "BACKUP_MANIFEST_SUFFIX",
    "BACKUP_MANIFEST_VERSION",
    "HubV2CLIError",
    "HubV2MigrationBlocked",
    "backup_checksum_validate",
    "contract_manifest",
    "create_v1_backup",
    "exact_contract_manifest",
    "hub_v2_backup_create",
    "hub_v2_backup_validate",
    "hub_v2_contract_manifest",
    "hub_v2_migration_apply",
    "hub_v2_migration_dry_run",
    "hub_v2_migration_status",
    "hub_v2_rollback_eligibility",
    "hub_v2_store_doctor",
    "migration_apply",
    "migration_dry_run",
    "migration_status",
    "rollback_eligibility",
    "store_doctor",
    "v2_store_doctor",
    "validate_backup_checksum",
]
