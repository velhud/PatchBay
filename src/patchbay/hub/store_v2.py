"""Transactional SQLite foundation for the opt-in Hub V2 control plane.

The V1 JSON store remains available for compatibility. ``hub.control_plane:
v2`` selects this store through the production Hub V2 composition.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from patchbay.connector.profiles import resolve_runtime_path
from patchbay.hub.sqlite_schema_contract import (
    schema_contract_difference,
    schema_contract_snapshot,
)


SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000

_SCHEMA_OBJECTS_BY_VERSION: dict[int, frozenset[tuple[str, str]]] = {
    1: frozenset(
        {
            ("table", "schema_metadata"),
            ("table", "principals"),
            ("table", "hub_identity"),
            ("table", "legacy_imports"),
            ("table", "entity_records"),
            ("table", "operations"),
            ("table", "attempts"),
            ("table", "events"),
            ("table", "payload_metadata"),
            ("index", "one_operator_principal"),
            ("index", "entity_records_import_idx"),
            ("index", "operations_parent_idx"),
            ("index", "attempts_operation_idx"),
            ("index", "events_operation_idx"),
            ("index", "events_entity_idx"),
            ("index", "payload_metadata_operation_idx"),
        }
    ),
    2: frozenset(
        {
            ("table", "entity_control_index"),
            ("index", "entity_control_route_status_idx"),
            ("index", "entity_control_type_order_idx"),
            ("index", "operations_state_created_idx"),
        }
    ),
    3: frozenset(
        {
            ("table", "operation_group_index"),
            ("index", "operation_group_index_group_operation_idx"),
        }
    ),
}

OPERATION_GROUP_ASSOCIATION_ENTITY_TYPE = "hub.operation_group"
FLEET_WORKER_ENTITY_TYPE = "hub.fleet_worker"
WORKER_PROJECTION_ENTITY_TYPE = "hub.worker_projection"
WORK_GROUP_ENTITY_TYPE = "hub.work_group"

CONTROL_INDEX_ENTITY_TYPES = frozenset(
    {
        "hub.edge_dispatch",
        "hub.edge_receipt",
    }
)


@dataclass
class _BatchOperationGroupAssociationIntent:
    """One adapter-declared relation set to persist during a broker batch write."""

    logical_target: str
    idempotency_key: str
    principal_ref: str
    work_group_id: str
    child_item_ids: frozenset[str]
    parent_operation_id: str = ""
    associated_operation_ids: set[str] = field(default_factory=set)


@dataclass
class _OperationGroupAssociationIntent:
    """One adapter-declared relation to persist with one operation insert."""

    tool: str
    logical_target: str
    idempotency_key: str
    principal_ref: str
    work_group_id: str
    associated_operation_id: str = ""


_OPERATION_GROUP_ASSOCIATION_INTENTS: ContextVar[
    tuple[_OperationGroupAssociationIntent, ...]
] = ContextVar("hub_v2_operation_group_association_intents", default=())

_BATCH_OPERATION_GROUP_ASSOCIATION_INTENTS: ContextVar[
    tuple[_BatchOperationGroupAssociationIntent, ...]
] = ContextVar("hub_v2_batch_operation_group_association_intents", default=())

OPERATION_STATES = frozenset(
    {
        "created",
        "payload_ready",
        "dispatchable",
        "running",
        "reconciling",
        "outcome_unknown",
        "succeeded",
        "blocked",
        "failed",
        "cancelled",
    }
)
TERMINAL_OPERATION_STATES = frozenset({"succeeded", "blocked", "failed", "cancelled"})
ATTEMPT_STATES = frozenset(
    {
        "offered",
        "claimed",
        "executing",
        "effect_recorded",
        "result_ready",
        "acknowledged",
        "lease_expired",
        "reconciling",
        "retryable",
        "manual_recovery",
    }
)

LEGACY_ENTITY_TYPES = {
    "enrollment_codes": "legacy.enrollment_code",
    "machines": "legacy.machine",
    "commands": "legacy.command",
    "work_groups": "legacy.work_group",
    "current_work_group_by_manager": "legacy.current_work_group_pointer",
}
LEGACY_CLASSIFICATIONS = {
    "enrollment_codes": "legacy_enrollment_code",
    "machines": "legacy_machine",
    "commands": "legacy_command",
    "work_groups": "legacy_work_group",
    "current_work_group_by_manager": "legacy_current_work_group_pointer",
}
ACTIVE_LEGACY_COMMAND_STATES = frozenset({"queued", "running"})
LEGACY_RECOVERY_REQUIRED = "legacy_recovery_required"

_OPERATION_TRANSITIONS = {
    "created": {"payload_ready"},
    "payload_ready": {"dispatchable"},
    "dispatchable": {"running"},
    "running": {"reconciling", "outcome_unknown", *TERMINAL_OPERATION_STATES},
    "reconciling": set(TERMINAL_OPERATION_STATES),
    "outcome_unknown": {"reconciling", *TERMINAL_OPERATION_STATES},
}
_ATTEMPT_TRANSITIONS = {
    "offered": {"claimed"},
    "claimed": {"executing", "lease_expired"},
    "executing": {"effect_recorded", "lease_expired"},
    "effect_recorded": {"result_ready"},
    "result_ready": {"acknowledged"},
    "lease_expired": {"reconciling"},
    "reconciling": {"result_ready", "retryable", "manual_recovery"},
}


class HubStoreV2Error(RuntimeError):
    """Base error for the Hub V2 store."""


class HubStoreV2Corrupt(HubStoreV2Error):
    """Raised when durable V2 data or a V1 import cannot be decoded safely."""


class HubStoreV2Conflict(HubStoreV2Error):
    """Raised for an idempotency, revision, or migration ownership conflict."""


class HubStoreV2StateError(HubStoreV2Error):
    """Raised when a state transition violates the resolved V2 contract."""


def hub_state_v2_path(config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> Path:
    """Resolve an opt-in V2 database path without changing the V1 state path."""

    hub_config = config.get("hub") if isinstance(config.get("hub"), Mapping) else {}
    configured = hub_config.get("state_db") or hub_config.get("sqlite_file")
    if configured:
        return resolve_runtime_path(configured, "hub", "hub-state-v2.sqlite3", environ=environ)
    if hub_config.get("state_file"):
        v1_path = resolve_runtime_path(hub_config["state_file"], "hub", "hub-state.json", environ=environ)
        stem = v1_path.name.removesuffix(v1_path.suffix)
        return v1_path.with_name(f"{stem}-v2.sqlite3")
    return resolve_runtime_path(None, "hub", "hub-state-v2.sqlite3", environ=environ)


def assert_v2_activation_safe(
    config: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> None:
    """Refuse V2 activation when current V1 state lacks migration proof."""

    from patchbay.hub.store import hub_state_path

    legacy_path = hub_state_path(config, environ=environ)
    if not legacy_path.is_file():
        return
    database_path = hub_state_v2_path(config, environ=environ)
    if not database_path.is_file():
        raise HubStoreV2Conflict(
            "Existing Hub V1 state has not been imported into V2. Run the "
            "V1-to-V2 migration before selecting hub.control_plane: v2."
        )
    try:
        checksum = hashlib.sha256(legacy_path.read_bytes()).hexdigest()
        uri = f"{database_path.resolve(strict=False).as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            matched = connection.execute(
                """
                SELECT 1
                FROM legacy_imports
                WHERE source_checksum = ? AND status = 'complete'
                LIMIT 1
                """,
                (checksum,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        raise HubStoreV2Conflict(
            "Existing Hub V1 state cannot be matched to a completed V2 import. "
            "Keep V1 active until migration verification succeeds."
        ) from error
    if matched is None:
        raise HubStoreV2Conflict(
            "Existing Hub V1 state differs from every completed V2 import. "
            "Keep V1 active and migrate the current source before starting V2."
        )


def semantic_payload_hash(payload: Mapping[str, Any]) -> str:
    """Return the canonical semantic hash used by idempotent operations."""

    encoded = _encode_json_object(payload, field="payload")
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class HubStoreV2:
    """Versioned, multi-instance-safe SQLite storage for the Hub V2 foundation."""

    def __init__(
        self,
        path_or_config: str | Path | Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
        busy_timeout_ms: int | None = None,
        pre_migration_backup_marker: str | Path | None = None,
    ):
        if isinstance(path_or_config, Mapping):
            self.config = dict(path_or_config)
            assert_v2_activation_safe(path_or_config, environ=environ)
            self.path = hub_state_v2_path(path_or_config, environ=environ)
            hub_config = path_or_config.get("hub") if isinstance(path_or_config.get("hub"), Mapping) else {}
            configured_timeout = hub_config.get("sqlite_busy_timeout_ms", hub_config.get("busy_timeout_ms"))
            configured_migration_marker = hub_config.get(
                "pre_migration_backup_marker"
            )
            require_existing_state = bool(hub_config.get("require_existing_state", False))
            expected_hub_id = str(hub_config.get("expected_hub_id") or "").strip()
        else:
            self.config = {}
            self.path = Path(path_or_config).expanduser()
            configured_timeout = None
            configured_migration_marker = None
            require_existing_state = False
            expected_hub_id = ""

        requested_timeout = busy_timeout_ms if busy_timeout_ms is not None else configured_timeout
        try:
            self.busy_timeout_ms = max(1, int(requested_timeout or DEFAULT_BUSY_TIMEOUT_MS))
        except (TypeError, ValueError) as exc:
            raise ValueError("busy_timeout_ms must be an integer") from exc
        marker_value = (
            pre_migration_backup_marker
            if pre_migration_backup_marker is not None
            else configured_migration_marker
        )
        self.pre_migration_backup_marker = (
            Path(marker_value).expanduser().resolve(strict=False)
            if marker_value
            else None
        )
        self.pre_migration_backup_report: dict[str, Any] = {
            "status": "not_checked",
            "required": False,
            "valid": True,
        }
        self._preopen_schema_version: int | None = None
        self.require_existing_state = require_existing_state
        self.expected_hub_id = expected_hub_id

        if (self.require_existing_state or self.expected_hub_id) and not self.path.is_file():
            raise HubStoreV2Conflict(
                f"Configured Hub V2 state database is missing: {self.path}"
            )

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._validate_pre_migration_backup_gate()
            self._connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._configure_connection()
            self._migrate()
            self._validate_expected_identity()
            self._harden_permissions()
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise HubStoreV2Corrupt(f"Hub V2 database cannot be opened safely: {self.path}") from exc
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for narrow administrative inspection and tests."""

        self._require_open()
        return self._connection

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def principal_ref(self) -> str:
        self._require_open()
        row = self._connection.execute("SELECT principal_ref FROM hub_identity WHERE singleton = 1").fetchone()
        if row is None:
            raise HubStoreV2Corrupt("Hub V2 identity record is missing")
        return str(row["principal_ref"])

    def _configure_connection(self) -> None:
        journal_mode = str(self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise HubStoreV2Error(f"Hub V2 requires WAL journal mode, got {journal_mode!r}")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        if int(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise HubStoreV2Error("Hub V2 requires SQLite foreign keys")

    def _harden_permissions(self) -> None:
        """Keep the private database and live WAL sidecars owner-only."""

        os.chmod(self.path.parent, 0o700)
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                continue

    def _validate_expected_identity(self) -> None:
        if not self.expected_hub_id:
            return
        row = self._connection.execute(
            "SELECT hub_id FROM hub_identity WHERE singleton = 1"
        ).fetchone()
        if row is None or str(row["hub_id"]) != self.expected_hub_id:
            raise HubStoreV2Conflict(
                "Configured Hub V2 identity does not match the opened database"
            )

    def _migrate(self) -> None:
        migration_owner = f"migration_{secrets.token_hex(16)}"
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                allow_identity_bootstrap = self._database_is_provably_new()
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL CHECK (schema_version >= 0),
                        migration_lock TEXT,
                        migration_started_at REAL,
                        updated_at REAL NOT NULL,
                        v2_mutation_count INTEGER NOT NULL DEFAULT 0 CHECK (v2_mutation_count >= 0)
                    )
                    """
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_metadata
                        (singleton, schema_version, migration_lock, migration_started_at, updated_at)
                    VALUES (1, 0, NULL, NULL, ?)
                    """,
                    (now,),
                )
                metadata = self._connection.execute(
                    "SELECT schema_version, migration_lock FROM schema_metadata WHERE singleton = 1"
                ).fetchone()
                if metadata is None:
                    raise HubStoreV2Corrupt("Hub V2 schema metadata is missing")
                if metadata["migration_lock"]:
                    raise HubStoreV2Conflict("Another or incomplete Hub V2 migration owns the migration lock")
                current_version = int(metadata["schema_version"])
                if current_version > SCHEMA_VERSION:
                    raise HubStoreV2Corrupt(
                        f"Hub V2 schema version {current_version} is newer than supported version {SCHEMA_VERSION}"
                    )
                if (
                    current_version < SCHEMA_VERSION
                    and self._preopen_schema_version is not None
                    and not allow_identity_bootstrap
                ):
                    # Revalidate under the SQLite write reservation so another
                    # process cannot change the approved source before migration.
                    self._validate_pre_migration_backup_gate(force=True)
                self._connection.execute(
                    """
                    UPDATE schema_metadata
                    SET migration_lock = ?, migration_started_at = ?, updated_at = ?
                    WHERE singleton = 1 AND migration_lock IS NULL
                    """,
                    (migration_owner, now, now),
                )
                if current_version < 1:
                    self._apply_schema_v1()
                    current_version = 1
                if current_version < 2:
                    self._apply_schema_v2()
                    current_version = 2
                if current_version < 3:
                    self._apply_schema_v3()
                    current_version = 3
                self._connection.execute(f"PRAGMA user_version={current_version}")
                self._assert_schema_contract(self._connection, current_version)
                self._ensure_identity(allow_bootstrap=allow_identity_bootstrap)
                self._connection.execute(
                    """
                    UPDATE schema_metadata
                    SET schema_version = ?, migration_lock = NULL, migration_started_at = NULL, updated_at = ?
                    WHERE singleton = 1 AND migration_lock = ?
                    """,
                    (current_version, time.time(), migration_owner),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _validate_pre_migration_backup_gate(self, *, force: bool = False) -> None:
        """Require an exact validated backup before mutating an older store."""

        if not force and self._database_needs_no_migration_gate():
            self.pre_migration_backup_report = {
                "status": "not_required",
                "required": False,
                "valid": True,
            }
            return
        from patchbay.hub.backup_v2 import (
            BackupV2ValidationError,
            require_pre_migration_validated_backup,
        )

        try:
            report = require_pre_migration_validated_backup(
                self.path,
                database_kind="hub_v2",
                target_schema_version=SCHEMA_VERSION,
                marker_path=self.pre_migration_backup_marker,
                busy_timeout_ms=self.busy_timeout_ms,
            )
        except BackupV2ValidationError as exc:
            self.pre_migration_backup_report = dict(exc.report)
            raise HubStoreV2Conflict(
                "Hub V2 migration is blocked until a validated pre-migration "
                "backup marker proves the exact current database state"
            ) from exc
        self.pre_migration_backup_report = dict(report)

    def _database_needs_no_migration_gate(self) -> bool:
        """Inspect existing schema without creating or mutating SQLite state."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            self._preopen_schema_version = None
            return True
        uri = f"file:{self.path.resolve(strict=False).as_posix()}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True, isolation_level=None)
            table = connection.execute(
                """
                SELECT 1
                FROM sqlite_schema
                WHERE type = 'table' AND name = 'schema_metadata'
                """
            ).fetchone()
            if table is None:
                self._preopen_schema_version = None
                # This is not a supported older Hub schema. Let the normal
                # bootstrap/identity checks reject persisted or malformed state;
                # a backup marker cannot make an unknown schema migratable.
                return True
            row = connection.execute(
                "SELECT schema_version, migration_lock FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                self._preopen_schema_version = None
                return True
            if row[1]:
                raise HubStoreV2Conflict(
                    "Another or incomplete Hub V2 migration owns the migration lock"
                )
            self._preopen_schema_version = int(row[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if user_version != self._preopen_schema_version:
                raise HubStoreV2Corrupt(
                    "Hub V2 schema_metadata and PRAGMA user_version disagree"
                )
            self._assert_schema_contract(connection, self._preopen_schema_version)
            identity = connection.execute(
                "SELECT hub_id, principal_ref FROM hub_identity WHERE singleton = 1"
            ).fetchone()
            if identity is None:
                raise HubStoreV2Corrupt("Hub V2 identity record is missing")
            if self.expected_hub_id and str(identity[0]) != self.expected_hub_id:
                raise HubStoreV2Conflict(
                    "Configured Hub V2 identity does not match the opened database"
                )
            return self._preopen_schema_version >= SCHEMA_VERSION
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return False
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def _assert_schema_contract(
        cls, connection: sqlite3.Connection, schema_version: int
    ) -> None:
        if schema_version not in _SCHEMA_OBJECTS_BY_VERSION:
            raise HubStoreV2Corrupt(
                f"Unsupported Hub V2 schema contract: {schema_version}"
            )
        expected_connection = sqlite3.connect(":memory:", isolation_level=None)
        try:
            expected_connection.execute("PRAGMA foreign_keys=ON")
            expected_connection.execute(
                """
                CREATE TABLE schema_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version >= 0),
                    migration_lock TEXT,
                    migration_started_at REAL,
                    updated_at REAL NOT NULL,
                    v2_mutation_count INTEGER NOT NULL DEFAULT 0 CHECK (v2_mutation_count >= 0)
                )
                """
            )
            fixture = object.__new__(cls)
            fixture._connection = expected_connection
            fixture._apply_schema_v1()
            if schema_version >= 2:
                fixture._apply_schema_v2()
            if schema_version >= 3:
                fixture._apply_schema_v3()
            expected = schema_contract_snapshot(expected_connection)
        finally:
            expected_connection.close()
        actual = schema_contract_snapshot(connection)
        if actual["sha256"] != expected["sha256"]:
            difference = schema_contract_difference(actual, expected)
            raise HubStoreV2Corrupt(
                f"Hub V2 schema {schema_version} definition mismatch: {difference!r}"
            )

    def _database_is_provably_new(self) -> bool:
        """Return true only when SQLite contains no evidence of prior state."""

        schema_object = self._connection.execute(
            "SELECT 1 FROM sqlite_schema LIMIT 1"
        ).fetchone()
        persisted_markers = (
            int(self._connection.execute("PRAGMA schema_version").fetchone()[0]),
            int(self._connection.execute("PRAGMA user_version").fetchone()[0]),
            int(self._connection.execute("PRAGMA application_id").fetchone()[0]),
        )
        return schema_object is None and persisted_markers == (0, 0, 0)

    def _apply_schema_v1(self) -> None:
        statements = (
            """
            CREATE TABLE principals (
                principal_ref TEXT PRIMARY KEY,
                principal_kind TEXT NOT NULL CHECK (principal_kind = 'operator'),
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                record_json TEXT NOT NULL CHECK (json_valid(record_json) AND json_type(record_json) = 'object'),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            "CREATE UNIQUE INDEX one_operator_principal ON principals(principal_kind)",
            """
            CREATE TABLE hub_identity (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                hub_id TEXT NOT NULL UNIQUE,
                principal_ref TEXT NOT NULL REFERENCES principals(principal_ref) ON DELETE RESTRICT,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE legacy_imports (
                import_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_checksum TEXT NOT NULL UNIQUE,
                source_size_bytes INTEGER NOT NULL CHECK (source_size_bytes >= 0),
                source_mtime_ns INTEGER NOT NULL,
                source_version INTEGER,
                source_hub_id TEXT NOT NULL DEFAULT '',
                source_created_at REAL,
                counts_json TEXT NOT NULL CHECK (json_valid(counts_json) AND json_type(counts_json) = 'object'),
                recovery_required_count INTEGER NOT NULL DEFAULT 0 CHECK (recovery_required_count >= 0),
                imported_at REAL NOT NULL,
                status TEXT NOT NULL CHECK (status = 'complete')
            )
            """,
            """
            CREATE TABLE entity_records (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                record_json TEXT NOT NULL CHECK (json_valid(record_json) AND json_type(record_json) = 'object'),
                legacy_classification TEXT NOT NULL DEFAULT '',
                source_import_id TEXT REFERENCES legacy_imports(import_id) ON DELETE RESTRICT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (entity_type, entity_id)
            )
            """,
            "CREATE INDEX entity_records_import_idx ON entity_records(source_import_id, entity_type)",
            f"""
            CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                principal_ref TEXT NOT NULL REFERENCES principals(principal_ref) ON DELETE RESTRICT,
                tool TEXT NOT NULL,
                logical_target TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                semantic_payload_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ({_sql_values(OPERATION_STATES)})),
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                parent_operation_id TEXT REFERENCES operations(operation_id) ON DELETE RESTRICT,
                item_id TEXT NOT NULL DEFAULT '',
                result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND json_type(result_json) = 'object')),
                error_json TEXT CHECK (error_json IS NULL OR (json_valid(error_json) AND json_type(error_json) = 'object')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (principal_ref, tool, logical_target, idempotency_key)
            )
            """,
            "CREATE INDEX operations_parent_idx ON operations(parent_operation_id, item_id)",
            f"""
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                machine_id TEXT NOT NULL,
                edge_generation INTEGER NOT NULL CHECK (edge_generation >= 0),
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                state TEXT NOT NULL CHECK (state IN ({_sql_values(ATTEMPT_STATES)})),
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                lease_expires_at REAL,
                result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND json_type(result_json) = 'object')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (operation_id, fencing_token)
            )
            """,
            "CREATE INDEX attempts_operation_idx ON attempts(operation_id, state)",
            """
            CREATE TABLE events (
                event_revision INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                operation_id TEXT REFERENCES operations(operation_id) ON DELETE RESTRICT,
                entity_type TEXT NOT NULL DEFAULT '',
                entity_id TEXT NOT NULL DEFAULT '',
                entity_revision INTEGER CHECK (entity_revision IS NULL OR entity_revision >= 1),
                data_json TEXT NOT NULL CHECK (json_valid(data_json) AND json_type(data_json) = 'object'),
                legacy_classification TEXT NOT NULL DEFAULT '',
                source_import_id TEXT REFERENCES legacy_imports(import_id) ON DELETE RESTRICT,
                source_ordinal INTEGER CHECK (source_ordinal IS NULL OR source_ordinal >= 0),
                created_at REAL NOT NULL,
                UNIQUE (source_import_id, source_ordinal)
            )
            """,
            "CREATE INDEX events_operation_idx ON events(operation_id, event_revision)",
            "CREATE INDEX events_entity_idx ON events(entity_type, entity_id, event_revision)",
            """
            CREATE TABLE payload_metadata (
                payload_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
                payload_kind TEXT NOT NULL,
                checksum_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                storage_ref TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ready', 'acknowledged', 'expired', 'deleted')),
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                expires_at REAL,
                acknowledged_at REAL,
                metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            "CREATE INDEX payload_metadata_operation_idx ON payload_metadata(operation_id, status)",
        )
        for statement in statements:
            self._connection.execute(statement)

    def _apply_schema_v2(self) -> None:
        """Add bounded relational lookup projections for Edge control history."""

        self._connection.execute(
            """
            CREATE TABLE entity_control_index (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                machine_id TEXT NOT NULL DEFAULT '',
                edge_generation TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                sort_created_at REAL NOT NULL,
                PRIMARY KEY (entity_type, entity_id),
                FOREIGN KEY (entity_type, entity_id)
                    REFERENCES entity_records(entity_type, entity_id) ON DELETE CASCADE
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX entity_control_route_status_idx
            ON entity_control_index(
                entity_type, machine_id, edge_generation, status,
                sort_created_at, entity_id
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX entity_control_type_order_idx
            ON entity_control_index(entity_type, sort_created_at, entity_id)
            """
        )
        self._connection.execute(
            """
            CREATE INDEX operations_state_created_idx
            ON operations(state, created_at, operation_id)
            """
        )
        self._connection.execute(
            """
            INSERT INTO entity_control_index
                (entity_type, entity_id, machine_id, edge_generation, status,
                 sort_created_at)
            SELECT entity_type,
                   entity_id,
                   COALESCE(json_extract(record_json, '$.machine_id'), ''),
                   COALESCE(json_extract(record_json, '$.edge_generation'), ''),
                   CASE
                       WHEN entity_type = 'hub.edge_receipt'
                           THEN COALESCE(json_extract(record_json, '$.status'), 'pending')
                       ELSE COALESCE(json_extract(record_json, '$.status'), '')
                   END,
                   COALESCE(
                       CAST(json_extract(record_json, '$.created_at') AS REAL),
                       created_at
                   )
            FROM entity_records
            WHERE entity_type IN ('hub.edge_dispatch', 'hub.edge_receipt')
            """
        )

    def _apply_schema_v3(self) -> None:
        """Index explicit operation-to-group authority without decoding all history."""

        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_group_index (
                operation_id TEXT PRIMARY KEY
                    REFERENCES operations(operation_id) ON DELETE CASCADE,
                work_group_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS operation_group_index_group_operation_idx
            ON operation_group_index(work_group_id, operation_id)
            """
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO operation_group_index(operation_id, work_group_id, kind)
            SELECT association.entity_id,
                   COALESCE(json_extract(association.record_json, '$.work_group_id'), ''),
                   COALESCE(json_extract(association.record_json, '$.kind'), '')
            FROM entity_records AS association
            JOIN operations AS operation
              ON operation.operation_id = association.entity_id
            WHERE association.entity_type = 'hub.operation_group'
              AND COALESCE(json_extract(association.record_json, '$.work_group_id'), '') != ''
            """
        )

    def _ensure_identity(self, *, allow_bootstrap: bool) -> None:
        row = self._connection.execute(
            "SELECT principal_ref FROM hub_identity WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            return
        if not allow_bootstrap:
            raise HubStoreV2Corrupt(
                "Hub V2 identity record is missing from an existing database; "
                "automatic identity regeneration is refused"
            )
        now = time.time()
        principal_ref = f"principal_{secrets.token_hex(16)}"
        hub_id = f"hub-{secrets.token_hex(10)}"
        self._connection.execute(
            """
            INSERT INTO principals
                (principal_ref, principal_kind, revision, record_json, created_at, updated_at)
            VALUES (?, 'operator', 1, ?, ?, ?)
            """,
            (
                principal_ref,
                _encode_json_object({"trust_domain": "single_operator"}, field="principal"),
                now,
                now,
            ),
        )
        self._connection.execute(
            "INSERT INTO hub_identity(singleton, hub_id, principal_ref, created_at) VALUES (1, ?, ?, ?)",
            (hub_id, principal_ref, now),
        )

    def schema_info(self) -> dict[str, Any]:
        self._require_open()
        metadata = self._connection.execute("SELECT * FROM schema_metadata WHERE singleton = 1").fetchone()
        if metadata is None:
            raise HubStoreV2Corrupt("Hub V2 schema metadata is missing")
        return {
            "schema_version": int(metadata["schema_version"]),
            "migration_lock": metadata["migration_lock"],
            "v2_mutation_count": int(metadata["v2_mutation_count"]),
            "journal_mode": str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "foreign_keys": bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            "busy_timeout_ms": int(self._connection.execute("PRAGMA busy_timeout").fetchone()[0]),
        }

    def get_principal(self) -> dict[str, Any]:
        self._require_open()
        row = self._connection.execute(
            "SELECT * FROM principals WHERE principal_ref = ?", (self.principal_ref,)
        ).fetchone()
        if row is None:
            raise HubStoreV2Corrupt("Hub V2 operator principal is missing")
        return {
            "principal_ref": str(row["principal_ref"]),
            "principal_kind": str(row["principal_kind"]),
            "revision": int(row["revision"]),
            "record": _decode_json_object(row["record_json"], context="operator principal"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @contextmanager
    def immediate_transaction(self, *, mark_mutation: bool = True) -> Iterator[sqlite3.Connection]:
        """Serialize a conflicting update with ``BEGIN IMMEDIATE``.

        Callers must perform only bounded local database work in this context;
        network and model calls do not belong inside a transaction.
        """

        self._require_open()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                if mark_mutation:
                    self._connection.execute(
                        """
                        UPDATE schema_metadata
                        SET v2_mutation_count = v2_mutation_count + 1, updated_at = ?
                        WHERE singleton = 1
                        """,
                        (time.time(),),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    transaction = immediate_transaction

    @contextmanager
    def operation_group_association_scope(
        self,
        *,
        tool: str,
        logical_target: str,
        idempotency_key: str,
        principal_ref: str,
        work_group_id: str,
    ) -> Iterator[None]:
        """Bind one operation's group relation to its create transaction."""

        self._require_open()
        intent = _OperationGroupAssociationIntent(
            tool=_clean_key(tool, "tool"),
            logical_target=_clean_key(logical_target, "logical_target"),
            idempotency_key=_clean_key(idempotency_key, "idempotency_key"),
            principal_ref=_clean_key(
                principal_ref or self.principal_ref, "principal_ref"
            ),
            work_group_id=_clean_key(work_group_id, "work_group_id"),
        )
        token = _OPERATION_GROUP_ASSOCIATION_INTENTS.set(
            _OPERATION_GROUP_ASSOCIATION_INTENTS.get() + (intent,)
        )
        try:
            yield
        finally:
            _OPERATION_GROUP_ASSOCIATION_INTENTS.reset(token)

    def assert_operation_group_association(
        self,
        *,
        operation_id: str,
        work_group_id: str,
    ) -> None:
        """Fail closed before dispatch when one grouped operation lacks authority."""

        operation_value = _clean_key(operation_id, "operation_id")
        group_value = _clean_key(work_group_id, "work_group_id")
        row = self._connection.execute(
            """
            SELECT work_group_id
            FROM operation_group_index
            WHERE operation_id = ?
            """,
            (operation_value,),
        ).fetchone()
        if row is None or str(row["work_group_id"]) != group_value:
            raise HubStoreV2Conflict("operation_group_association_recovery_required")

    @contextmanager
    def batch_operation_group_association_scope(
        self,
        *,
        logical_target: str,
        idempotency_key: str,
        principal_ref: str,
        work_group_id: str,
        child_item_ids: list[str] | tuple[str, ...],
    ) -> Iterator[None]:
        """Bind one batch's explicit associations to its broker transaction.

        ``OperationBroker.create_batch_operation`` owns the ``BEGIN IMMEDIATE``
        boundary. The worker adapter opens this scope before calling it; the
        store observes each newly inserted batch operation and writes its
        association through the same SQLite connection before that transaction
        can commit.
        """

        self._require_open()
        normalized_items = frozenset(
            _clean_key(str(item_id), "child item_id") for item_id in child_item_ids
        )
        if not normalized_items:
            raise ValueError("child_item_ids must not be empty")
        intent = _BatchOperationGroupAssociationIntent(
            logical_target=_clean_key(logical_target, "logical_target"),
            idempotency_key=_clean_key(idempotency_key, "idempotency_key"),
            principal_ref=_clean_key(
                principal_ref or self.principal_ref, "principal_ref"
            ),
            work_group_id=_clean_key(work_group_id, "work_group_id"),
            child_item_ids=normalized_items,
        )
        token = _BATCH_OPERATION_GROUP_ASSOCIATION_INTENTS.set(
            _BATCH_OPERATION_GROUP_ASSOCIATION_INTENTS.get() + (intent,)
        )
        try:
            yield
        finally:
            _BATCH_OPERATION_GROUP_ASSOCIATION_INTENTS.reset(token)

    def assert_batch_operation_group_associations(
        self,
        *,
        parent_operation_id: str,
        child_operation_ids: list[str] | tuple[str, ...],
        work_group_id: str,
    ) -> None:
        """Fail closed before dispatch if a historical batch lacks authority."""

        self._require_open()
        operation_ids = [_clean_key(parent_operation_id, "parent_operation_id")]
        operation_ids.extend(
            _clean_key(str(operation_id), "child_operation_id")
            for operation_id in child_operation_ids
        )
        if len(operation_ids) != len(set(operation_ids)):
            raise HubStoreV2Conflict("batch_operation_group_association_recovery_required")
        placeholders = ",".join("?" for _ in operation_ids)
        rows = self._connection.execute(
            f"""
            SELECT operation_id
            FROM operation_group_index
            WHERE work_group_id = ?
              AND operation_id IN ({placeholders})
            """,
            [_clean_key(work_group_id, "work_group_id"), *operation_ids],
        ).fetchall()
        indexed_ids = {str(row["operation_id"]) for row in rows}
        if indexed_ids != set(operation_ids):
            raise HubStoreV2Conflict("batch_operation_group_association_recovery_required")

    def operation_ids_for_work_group(self, work_group_id: str) -> list[str]:
        """Return explicitly associated operation ids without decoding JSON records."""

        self._require_open()
        rows = self._connection.execute(
            """
            SELECT operation_id
            FROM operation_group_index
            WHERE work_group_id = ?
            ORDER BY operation_id
            """,
            (_clean_key(work_group_id, "work_group_id"),),
        ).fetchall()
        return [str(row["operation_id"]) for row in rows]

    def worker_refs_for_work_group(self, work_group_id: str) -> list[str]:
        """Filter durable worker identities before decoding their records."""

        self._require_open()
        rows = self._connection.execute(
            """
            SELECT entity_id
            FROM entity_records
            WHERE entity_type = ?
              AND COALESCE(json_extract(record_json, '$.work_group_id'), '') = ?
            ORDER BY COALESCE(json_extract(record_json, '$.lane_id'), 'main'),
                     LOWER(COALESCE(json_extract(record_json, '$.name'), entity_id)),
                     entity_id
            """,
            (
                FLEET_WORKER_ENTITY_TYPE,
                _clean_key(work_group_id, "work_group_id"),
            ),
        ).fetchall()
        return [str(row["entity_id"]) for row in rows]

    def work_group_status_revision(self, work_group_id: str) -> int:
        """Return the compact status token without materializing detail records."""

        self._require_open()
        group_id = _clean_key(work_group_id, "work_group_id")
        row = self._connection.execute(
            """
            WITH group_operations AS (
                SELECT operation.revision
                FROM operation_group_index AS association
                JOIN operations AS operation
                  ON operation.operation_id = association.operation_id
                WHERE association.work_group_id = ?
                UNION ALL
                SELECT operation.revision
                FROM operations AS operation
                WHERE operation.logical_target = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM operation_group_index AS indexed_association
                      WHERE indexed_association.operation_id = operation.operation_id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM entity_records AS association
                      WHERE association.entity_type = ?
                        AND association.entity_id = operation.operation_id
                  )
            ),
            group_workers AS (
                SELECT COALESCE(projection.revision, identity.revision) AS revision
                FROM entity_records AS identity
                LEFT JOIN entity_records AS projection
                  ON projection.entity_type = ?
                 AND projection.entity_id = identity.entity_id
                WHERE identity.entity_type = ?
                  AND COALESCE(
                      json_extract(identity.record_json, '$.work_group_id'), ''
                  ) = ?
            )
            SELECT
                COALESCE((
                    SELECT revision
                    FROM entity_records
                    WHERE entity_type = ? AND entity_id = ?
                ), 0)
                + COALESCE((SELECT SUM(revision) FROM group_operations), 0)
                + COALESCE((SELECT SUM(revision) FROM group_workers), 0)
                AS status_revision
            """,
            (
                group_id,
                group_id,
                OPERATION_GROUP_ASSOCIATION_ENTITY_TYPE,
                WORKER_PROJECTION_ENTITY_TYPE,
                FLEET_WORKER_ENTITY_TYPE,
                group_id,
                WORK_GROUP_ENTITY_TYPE,
                group_id,
            ),
        ).fetchone()
        return int(row["status_revision"] if row is not None else 0)

    def work_group_status_projection(
        self,
        work_group_id: str,
        *,
        operation_offset: int = 0,
        operation_limit: int = 100,
        worker_offset: int = 0,
        worker_limit: int = 100,
        integration_offset: int = 0,
        integration_limit: int = 100,
    ) -> dict[str, Any]:
        """Build one bounded, snapshot-consistent group status projection.

        Aggregate counts and the status token stay exact for the whole group.
        Only explicitly requested operation, worker, and integration pages are
        decoded into Python objects.
        """

        self._require_open()
        group_id = _clean_key(work_group_id, "work_group_id")
        operation_offset = max(0, int(operation_offset))
        worker_offset = max(0, int(worker_offset))
        integration_offset = max(0, int(integration_offset))
        operation_limit = max(0, min(int(operation_limit), 500))
        worker_limit = max(0, min(int(worker_limit), 500))
        integration_limit = max(0, min(int(integration_limit), 500))

        operation_cte = """
            WITH group_operations AS (
                SELECT operation.operation_id,
                       operation.parent_operation_id,
                       operation.tool,
                       operation.state,
                       operation.idempotency_key,
                       operation.semantic_payload_hash,
                       operation.revision,
                       operation.created_at,
                       operation.updated_at,
                       association.kind,
                       'indexed' AS association_source
                FROM operation_group_index AS association
                JOIN operations AS operation
                  ON operation.operation_id = association.operation_id
                WHERE association.work_group_id = ?
                UNION ALL
                SELECT operation.operation_id,
                       operation.parent_operation_id,
                       operation.tool,
                       operation.state,
                       operation.idempotency_key,
                       operation.semantic_payload_hash,
                       operation.revision,
                       operation.created_at,
                       operation.updated_at,
                       '',
                       'legacy_logical_target'
                FROM operations AS operation
                WHERE operation.logical_target = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM operation_group_index AS indexed_association
                      WHERE indexed_association.operation_id = operation.operation_id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM entity_records AS association
                      WHERE association.entity_type = ?
                        AND association.entity_id = operation.operation_id
                  )
            )
        """
        operation_parameters = (
            group_id,
            group_id,
            OPERATION_GROUP_ASSOCIATION_ENTITY_TYPE,
        )
        worker_cte = """
            WITH worker_records AS (
                SELECT identity.entity_id AS fleet_worker_ref,
                       COALESCE(
                           json_extract(identity.record_json, '$.lane_id'), 'main'
                       ) AS lane_id,
                       LOWER(COALESCE(
                           json_extract(identity.record_json, '$.name'),
                           identity.entity_id
                       )) AS sort_name,
                       COALESCE(projection.record_json, identity.record_json) AS record_json,
                       COALESCE(projection.revision, identity.revision) AS revision
                FROM entity_records AS identity
                LEFT JOIN entity_records AS projection
                  ON projection.entity_type = ?
                 AND projection.entity_id = identity.entity_id
                WHERE identity.entity_type = ?
                  AND COALESCE(
                      json_extract(identity.record_json, '$.work_group_id'), ''
                  ) = ?
            ),
            group_workers AS (
                SELECT fleet_worker_ref,
                       lane_id,
                       sort_name,
                       record_json,
                       revision,
                       COALESCE(json_extract(record_json, '$.turn_state'), 'none') AS turn_state,
                       COALESCE(json_extract(record_json, '$.liveness'), 'lost') AS liveness,
                       COALESCE(json_extract(record_json, '$.integration_state'), 'uncertain') AS integration_state,
                       COALESCE(json_extract(record_json, '$.review_disposition'), 'unreviewed') AS review_disposition
                FROM worker_records
            )
        """
        worker_parameters = (
            WORKER_PROJECTION_ENTITY_TYPE,
            FLEET_WORKER_ENTITY_TYPE,
            group_id,
        )

        with self._lock:
            owns_transaction = not self._connection.in_transaction
            if owns_transaction:
                self._connection.execute("BEGIN")
            try:
                operation_row = self._connection.execute(
                    operation_cte
                    + """
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(revision), 0) AS revision_sum,
                           COALESCE(MAX(updated_at), 0) AS latest_updated_at,
                           SUM(state = 'created') AS created_count,
                           SUM(state = 'payload_ready') AS payload_ready_count,
                           SUM(state = 'dispatchable') AS dispatchable_count,
                           SUM(state = 'running') AS running_count,
                           SUM(state = 'reconciling') AS reconciling_count,
                           SUM(state = 'outcome_unknown') AS outcome_unknown_count,
                           SUM(state = 'succeeded') AS succeeded_count,
                           SUM(state = 'blocked') AS blocked_count,
                           SUM(state = 'failed') AS failed_count,
                           SUM(state = 'cancelled') AS cancelled_count,
                           SUM(tool = 'patchbay_worker_integrate') AS integration_operation_count,
                           SUM(association_source = 'legacy_logical_target') AS legacy_count
                    FROM group_operations
                    """,
                    operation_parameters,
                ).fetchone()
                operation_rows = []
                if operation_limit:
                    operation_rows = self._connection.execute(
                        operation_cte
                        + """
                        SELECT *
                        FROM group_operations
                        ORDER BY created_at DESC, operation_id DESC
                        LIMIT ? OFFSET ?
                        """,
                        (*operation_parameters, operation_limit, operation_offset),
                    ).fetchall()

                worker_row = self._connection.execute(
                    worker_cte
                    + """
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(revision), 0) AS revision_sum,
                           SUM(turn_state IN ('queued', 'starting', 'working')) AS active_count,
                           SUM(liveness = 'quiet') AS quiet_count,
                           SUM(liveness = 'stale') AS stale_count,
                           SUM(liveness = 'lost') AS lost_count,
                           SUM(turn_state = 'failed') AS failed_count,
                           SUM(integration_state IN ('not_integrated', 'uncertain')) AS unintegrated_count
                    FROM group_workers
                    """,
                    worker_parameters,
                ).fetchone()
                worker_rows = []
                if worker_limit:
                    worker_rows = self._connection.execute(
                        worker_cte
                        + """
                        SELECT fleet_worker_ref, lane_id, record_json, revision
                        FROM group_workers
                        ORDER BY lane_id, sort_name, fleet_worker_ref
                        LIMIT ? OFFSET ?
                        """,
                        (*worker_parameters, worker_limit, worker_offset),
                    ).fetchall()
                lane_rows = self._connection.execute(
                    worker_cte
                    + """
                    SELECT lane_id,
                           COUNT(*) AS worker_count,
                           SUM(turn_state IN ('queued', 'starting', 'working')) AS active_count,
                           SUM(liveness = 'quiet') AS quiet_count,
                           SUM(liveness = 'stale') AS stale_count,
                           SUM(liveness = 'lost') AS lost_count,
                           SUM(turn_state = 'failed') AS failed_count,
                           SUM(integration_state IN ('not_integrated', 'uncertain')) AS unintegrated_count
                    FROM group_workers
                    GROUP BY lane_id
                    ORDER BY lane_id
                    """,
                    worker_parameters,
                ).fetchall()
                integration_state_rows = self._connection.execute(
                    worker_cte
                    + """
                    SELECT integration_state AS value, COUNT(*) AS count
                    FROM group_workers
                    GROUP BY integration_state
                    ORDER BY integration_state
                    """,
                    worker_parameters,
                ).fetchall()
                review_state_rows = self._connection.execute(
                    worker_cte
                    + """
                    SELECT review_disposition AS value, COUNT(*) AS count
                    FROM group_workers
                    GROUP BY review_disposition
                    ORDER BY review_disposition
                    """,
                    worker_parameters,
                ).fetchall()
                integration_rows = []
                if integration_limit:
                    integration_rows = self._connection.execute(
                        worker_cte
                        + """
                        SELECT fleet_worker_ref,
                               lane_id,
                               integration_state,
                               review_disposition,
                               turn_state,
                               liveness
                        FROM group_workers
                        ORDER BY lane_id, sort_name, fleet_worker_ref
                        LIMIT ? OFFSET ?
                        """,
                        (*worker_parameters, integration_limit, integration_offset),
                    ).fetchall()
                group_row = self._connection.execute(
                    """
                    SELECT revision
                    FROM entity_records
                    WHERE entity_type = ? AND entity_id = ?
                    """,
                    (WORK_GROUP_ENTITY_TYPE, group_id),
                ).fetchone()
                if owns_transaction:
                    self._connection.commit()
            except Exception:
                if owns_transaction:
                    self._connection.rollback()
                raise

        operation_states = {
            state: int(operation_row[f"{state}_count"] or 0)
            for state in OPERATION_STATES
        }
        operation_summary = {
            "total": int(operation_row["total"] or 0),
            "revision_sum": int(operation_row["revision_sum"] or 0),
            "latest_updated_at": float(operation_row["latest_updated_at"] or 0),
            "state_counts": operation_states,
            "active": sum(
                operation_states[state]
                for state in OPERATION_STATES
                if state not in TERMINAL_OPERATION_STATES
                and state not in {"outcome_unknown", "reconciling"}
            ),
            "uncertain": operation_states["outcome_unknown"]
            + operation_states["reconciling"],
            "terminal": sum(
                operation_states[state] for state in TERMINAL_OPERATION_STATES
            ),
            "integration_operations": int(
                operation_row["integration_operation_count"] or 0
            ),
            "legacy_logical_target": int(operation_row["legacy_count"] or 0),
        }
        worker_summary = {
            "total": int(worker_row["total"] or 0),
            "revision_sum": int(worker_row["revision_sum"] or 0),
            "active": int(worker_row["active_count"] or 0),
            "quiet": int(worker_row["quiet_count"] or 0),
            "stale": int(worker_row["stale_count"] or 0),
            "lost": int(worker_row["lost_count"] or 0),
            "failed": int(worker_row["failed_count"] or 0),
            "unintegrated": int(worker_row["unintegrated_count"] or 0),
        }
        workers = []
        for row in worker_rows:
            record = _decode_json_object(
                row["record_json"],
                context=f"worker projection {row['fleet_worker_ref']}",
            )
            record.setdefault("fleet_worker_ref", str(row["fleet_worker_ref"]))
            record.setdefault("lane_id", str(row["lane_id"] or "main"))
            record.setdefault("projection_revision", int(row["revision"]))
            record["store_revision"] = int(row["revision"])
            workers.append(record)
        operations = [
            {
                "operation_id": str(row["operation_id"]),
                "parent_operation_id": str(row["parent_operation_id"] or ""),
                "tool": str(row["tool"]),
                "state": str(row["state"]),
                "idempotency_key": str(row["idempotency_key"]),
                "semantic_payload_hash": str(row["semantic_payload_hash"]),
                "revision": int(row["revision"]),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
                "association_kind": str(row["kind"] or ""),
                "association_source": str(row["association_source"]),
            }
            for row in operation_rows
        ]
        lanes = [
            {
                "lane_id": str(row["lane_id"] or "main"),
                "worker_count": int(row["worker_count"] or 0),
                "active": int(row["active_count"] or 0),
                "quiet": int(row["quiet_count"] or 0),
                "stale": int(row["stale_count"] or 0),
                "lost": int(row["lost_count"] or 0),
                "failed": int(row["failed_count"] or 0),
                "unintegrated": int(row["unintegrated_count"] or 0),
            }
            for row in lane_rows
        ]
        integrations = [
            {
                "fleet_worker_ref": str(row["fleet_worker_ref"]),
                "lane_id": str(row["lane_id"] or "main"),
                "integration_state": str(row["integration_state"]),
                "review_disposition": str(row["review_disposition"]),
                "turn_state": str(row["turn_state"]),
                "liveness": str(row["liveness"]),
            }
            for row in integration_rows
        ]
        group_revision = int(group_row["revision"] if group_row is not None else 0)
        return {
            "status_revision": group_revision
            + operation_summary["revision_sum"]
            + worker_summary["revision_sum"],
            "operation_summary": operation_summary,
            "operations": operations,
            "worker_summary": worker_summary,
            "workers": workers,
            "lane_summaries": lanes,
            "integration_summary": {
                "total": worker_summary["total"],
                "state_counts": {
                    str(row["value"]): int(row["count"])
                    for row in integration_state_rows
                },
                "review_disposition_counts": {
                    str(row["value"]): int(row["count"])
                    for row in review_state_rows
                },
            },
            "integrations": integrations,
        }

    def put_entity(
        self,
        entity_type: str,
        entity_id: str,
        record: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        legacy_classification: str = "",
    ) -> dict[str, Any]:
        """Insert or update a typed JSON entity, optionally guarded by revision."""

        with self.immediate_transaction() as connection:
            result = self._put_entity_in_transaction(
                connection,
                entity_type,
                entity_id,
                record,
                expected_revision=expected_revision,
                legacy_classification=legacy_classification,
            )
            if result is None:
                actual = self._entity_revision(connection, entity_type, entity_id)
                raise HubStoreV2Conflict(
                    f"Entity revision conflict for {entity_type}/{entity_id}: expected {expected_revision}, actual {actual}"
                )
            return result

    def cas_entity(
        self,
        entity_type: str,
        entity_id: str,
        expected_revision: int,
        record: Mapping[str, Any],
        *,
        legacy_classification: str | None = None,
    ) -> dict[str, Any] | None:
        """Compare and swap an entity, returning ``None`` for a stale revision."""

        with self.immediate_transaction() as connection:
            return self._put_entity_in_transaction(
                connection,
                entity_type,
                entity_id,
                record,
                expected_revision=expected_revision,
                legacy_classification=legacy_classification,
            )

    def compare_and_swap_entity(
        self,
        entity_type: str,
        entity_id: str,
        expected_revision: int,
        record: Mapping[str, Any],
    ) -> bool:
        return self.cas_entity(entity_type, entity_id, expected_revision, record) is not None

    def update_entity(
        self,
        entity_type: str,
        entity_id: str,
        mutator: Callable[[dict[str, Any]], Mapping[str, Any] | None],
    ) -> dict[str, Any]:
        """Read-modify-write one entity while holding the immediate write lock."""

        with self.immediate_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM entity_records WHERE entity_type = ? AND entity_id = ?",
                (_clean_key(entity_type, "entity_type"), _clean_key(entity_id, "entity_id")),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown entity: {entity_type}/{entity_id}")
            current = _decode_json_object(row["record_json"], context=f"entity {entity_type}/{entity_id}")
            working = deepcopy(current)
            changed = mutator(working)
            replacement = working if changed is None else changed
            result = self._put_entity_in_transaction(
                connection,
                entity_type,
                entity_id,
                replacement,
                expected_revision=int(row["revision"]),
                legacy_classification=str(row["legacy_classification"]),
            )
            if result is None:  # BEGIN IMMEDIATE makes this an internal invariant failure.
                raise HubStoreV2Conflict(f"Entity changed during locked update: {entity_type}/{entity_id}")
            return result

    def _put_entity_in_transaction(
        self,
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        record: Mapping[str, Any],
        *,
        expected_revision: int | None,
        legacy_classification: str | None,
        source_import_id: str | None = None,
    ) -> dict[str, Any] | None:
        type_value = _clean_key(entity_type, "entity_type")
        id_value = _clean_key(entity_id, "entity_id")
        encoded = _encode_json_object(record, field="record")
        now = time.time()
        row = connection.execute(
            "SELECT * FROM entity_records WHERE entity_type = ? AND entity_id = ?",
            (type_value, id_value),
        ).fetchone()
        if row is None:
            if expected_revision not in (None, 0):
                return None
            connection.execute(
                """
                INSERT INTO entity_records
                    (entity_type, entity_id, revision, record_json, legacy_classification,
                     source_import_id, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (type_value, id_value, encoded, legacy_classification or "", source_import_id, now, now),
            )
        else:
            current_revision = int(row["revision"])
            if expected_revision is not None and current_revision != expected_revision:
                return None
            classification = (
                str(row["legacy_classification"])
                if legacy_classification is None
                else str(legacy_classification)
            )
            cursor = connection.execute(
                """
                UPDATE entity_records
                SET record_json = ?, revision = revision + 1, legacy_classification = ?, updated_at = ?
                WHERE entity_type = ? AND entity_id = ? AND revision = ?
                """,
                (encoded, classification, now, type_value, id_value, current_revision),
            )
            if cursor.rowcount != 1:
                return None
        saved = connection.execute(
            "SELECT * FROM entity_records WHERE entity_type = ? AND entity_id = ?", (type_value, id_value)
        ).fetchone()
        if saved is None:
            raise HubStoreV2Corrupt(f"Entity write disappeared: {type_value}/{id_value}")
        self._sync_control_index(
            connection,
            entity_type=type_value,
            entity_id=id_value,
            record=record,
            created_at=float(saved["created_at"]),
        )
        self._sync_operation_group_index(
            connection,
            entity_type=type_value,
            entity_id=id_value,
            record=record,
        )
        return self._entity_from_row(saved)

    @staticmethod
    def _sync_control_index(
        connection: sqlite3.Connection,
        *,
        entity_type: str,
        entity_id: str,
        record: Mapping[str, Any],
        created_at: float,
    ) -> None:
        if entity_type not in CONTROL_INDEX_ENTITY_TYPES:
            return
        status_default = "pending" if entity_type == "hub.edge_receipt" else ""
        try:
            sort_created_at = float(record.get("created_at") or created_at)
        except (TypeError, ValueError):
            sort_created_at = float(created_at)
        connection.execute(
            """
            INSERT INTO entity_control_index
                (entity_type, entity_id, machine_id, edge_generation, status,
                 sort_created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                machine_id = excluded.machine_id,
                edge_generation = excluded.edge_generation,
                status = excluded.status,
                sort_created_at = excluded.sort_created_at
            """,
            (
                entity_type,
                entity_id,
                str(record.get("machine_id") or ""),
                str(record.get("edge_generation") or ""),
                str(record.get("status") or status_default),
                sort_created_at,
            ),
        )

    @staticmethod
    def _sync_operation_group_index(
        connection: sqlite3.Connection,
        *,
        entity_type: str,
        entity_id: str,
        record: Mapping[str, Any],
    ) -> None:
        if entity_type != OPERATION_GROUP_ASSOCIATION_ENTITY_TYPE:
            return
        work_group_id = str(record.get("work_group_id") or "")
        if not work_group_id or connection.execute(
            "SELECT 1 FROM operations WHERE operation_id = ?", (entity_id,)
        ).fetchone() is None:
            connection.execute(
                "DELETE FROM operation_group_index WHERE operation_id = ?", (entity_id,)
            )
            return
        connection.execute(
            """
            INSERT INTO operation_group_index(operation_id, work_group_id, kind)
            VALUES (?, ?, ?)
            ON CONFLICT(operation_id) DO UPDATE SET
                work_group_id = excluded.work_group_id,
                kind = excluded.kind
            """,
            (entity_id, work_group_id, str(record.get("kind") or "")),
        )

    def get_entity(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT * FROM entity_records WHERE entity_type = ? AND entity_id = ?",
            (_clean_key(entity_type, "entity_type"), _clean_key(entity_id, "entity_id")),
        ).fetchone()
        return self._entity_from_row(row) if row is not None else None

    def list_entities(self, entity_type: str, *, legacy_classification: str | None = None) -> list[dict[str, Any]]:
        self._require_open()
        parameters: list[Any] = [_clean_key(entity_type, "entity_type")]
        sql = "SELECT * FROM entity_records WHERE entity_type = ?"
        if legacy_classification is not None:
            sql += " AND legacy_classification = ?"
            parameters.append(legacy_classification)
        sql += " ORDER BY entity_id"
        return [self._entity_from_row(row) for row in self._connection.execute(sql, parameters).fetchall()]

    def query_control_entities(
        self,
        entity_type: str,
        *,
        machine_id: str | None = None,
        edge_generation: str | None = None,
        statuses: tuple[str, ...] = (),
        operation_states: tuple[str, ...] = (),
        latest_attempt_states: tuple[str, ...] = (),
        attempt_machine_id: str | None = None,
        attempt_edge_generation: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Filter and limit control entities relationally before JSON decoding."""

        self._require_open()
        type_value = _clean_key(entity_type, "entity_type")
        if type_value not in CONTROL_INDEX_ENTITY_TYPES:
            raise ValueError(f"Entity type is not control-indexed: {type_value}")
        bounded_limit = max(1, min(int(limit), 10_000))
        joins = [
            """
            JOIN entity_records AS records
              ON records.entity_type = control.entity_type
             AND records.entity_id = control.entity_id
            """
        ]
        where = ["control.entity_type = ?"]
        parameters: list[Any] = [type_value]
        if operation_states:
            joins.append(
                "JOIN operations AS operation ON operation.operation_id = control.entity_id"
            )
            where.append(
                f"operation.state IN ({','.join('?' for _ in operation_states)})"
            )
            parameters.extend(operation_states)
        if latest_attempt_states:
            if not operation_states:
                raise ValueError("latest_attempt_states requires operation_states")
            joins.append(
                """
                JOIN attempts AS attempt ON attempt.operation_id = operation.operation_id
                """
            )
            where.extend(
                (
                    f"attempt.state IN ({','.join('?' for _ in latest_attempt_states)})",
                    """
                    NOT EXISTS (
                        SELECT 1 FROM attempts AS newer
                        WHERE newer.operation_id = attempt.operation_id
                          AND newer.fencing_token > attempt.fencing_token
                    )
                    """,
                )
            )
            parameters.extend(latest_attempt_states)
            if attempt_machine_id is not None:
                where.append("attempt.machine_id = ?")
                parameters.append(str(attempt_machine_id))
            if attempt_edge_generation is not None:
                where.append("attempt.edge_generation = ?")
                parameters.append(int(attempt_edge_generation))
        if machine_id is not None:
            where.append("control.machine_id = ?")
            parameters.append(str(machine_id))
        if edge_generation is not None:
            where.append("control.edge_generation = ?")
            parameters.append(str(edge_generation))
        if statuses:
            where.append(f"control.status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(statuses)
        sql = f"""
            SELECT records.*
            FROM entity_control_index AS control
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            ORDER BY control.sort_created_at, control.entity_id
            LIMIT ?
        """
        parameters.append(bounded_limit)
        rows = self._connection.execute(sql, parameters).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def _entity_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "entity_type": str(row["entity_type"]),
            "entity_id": str(row["entity_id"]),
            "revision": int(row["revision"]),
            "record": _decode_json_object(
                row["record_json"], context=f"entity {row['entity_type']}/{row['entity_id']}"
            ),
            "legacy_classification": str(row["legacy_classification"]),
            "source_import_id": row["source_import_id"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _entity_revision(connection: sqlite3.Connection, entity_type: str, entity_id: str) -> int | None:
        row = connection.execute(
            "SELECT revision FROM entity_records WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        ).fetchone()
        return int(row["revision"]) if row is not None else None

    def create_operation(
        self,
        *,
        tool: str,
        logical_target: str,
        idempotency_key: str,
        payload: Mapping[str, Any] | None = None,
        payload_hash: str = "",
        operation_id: str = "",
        principal_ref: str = "",
        parent_operation_id: str | None = None,
        item_id: str = "",
        state: str = "created",
    ) -> dict[str, Any]:
        """Create or replay an idempotently scoped operation."""

        tool_value = _clean_key(tool, "tool")
        target_value = _clean_key(logical_target, "logical_target")
        key_value = _clean_key(idempotency_key, "idempotency_key")
        state_value = _validate_state(state, OPERATION_STATES, "operation")
        principal_value = principal_ref or self.principal_ref
        semantic_hash = payload_hash or semantic_payload_hash(payload or {})
        operation_value = operation_id or f"op_{secrets.token_hex(16)}"
        now = time.time()
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM operations
                WHERE principal_ref = ? AND tool = ? AND logical_target = ? AND idempotency_key = ?
                """,
                (principal_value, tool_value, target_value, key_value),
            ).fetchone()
            if existing is not None:
                if str(existing["semantic_payload_hash"]) != semantic_hash:
                    raise HubStoreV2Conflict("idempotency_payload_conflict")
                replay = self._operation_from_row(existing)
                replay["idempotent_replay"] = True
                return replay
            connection.execute(
                """
                INSERT INTO operations
                    (operation_id, principal_ref, tool, logical_target, idempotency_key,
                     semantic_payload_hash, state, revision, parent_operation_id, item_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    operation_value,
                    principal_value,
                    tool_value,
                    target_value,
                    key_value,
                    semantic_hash,
                    state_value,
                    parent_operation_id,
                    item_id,
                    now,
                    now,
                ),
            )
            self._append_event_in_transaction(
                connection,
                "operation.created",
                {"state": state_value},
                operation_id=operation_value,
            )
            row = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_value,)).fetchone()
            result = self._operation_from_row(row)
            result["idempotent_replay"] = False
            return result

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (_clean_key(operation_id, "operation_id"),)
        ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    def get_operation_by_idempotency(
        self,
        *,
        tool: str,
        logical_target: str,
        idempotency_key: str,
        principal_ref: str = "",
    ) -> dict[str, Any] | None:
        """Look up an idempotency-scoped operation without creating one."""

        self._require_open()
        row = self._connection.execute(
            """
            SELECT * FROM operations
            WHERE principal_ref = ? AND tool = ?
              AND logical_target = ? AND idempotency_key = ?
            """,
            (
                principal_ref or self.principal_ref,
                _clean_key(tool, "tool"),
                _clean_key(logical_target, "logical_target"),
                _clean_key(idempotency_key, "idempotency_key"),
            ),
        ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    def cas_operation_state(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        state: str,
        expected_state: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        target_state = _validate_state(state, OPERATION_STATES, "operation")
        operation_value = _clean_key(operation_id, "operation_id")
        with self.immediate_transaction() as connection:
            row = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_value,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown operation: {operation_value}")
            current_state = str(row["state"])
            if int(row["revision"]) != expected_revision or (expected_state and current_state != expected_state):
                return None
            _require_transition(current_state, target_state, _OPERATION_TRANSITIONS, "operation")
            result_json = row["result_json"] if result is None else _encode_json_object(result, field="result")
            error_json = row["error_json"] if error is None else _encode_json_object(error, field="error")
            if current_state in TERMINAL_OPERATION_STATES:
                conflict = result_json != row["result_json"] or error_json != row["error_json"]
                self._append_event_in_transaction(
                    connection,
                    "operation.terminal_receipt_conflict" if conflict else "operation.terminal_receipt_confirmed",
                    {
                        "state": current_state,
                        "stored_result_hash": _stored_json_hash(row["result_json"]),
                        "received_result_hash": _stored_json_hash(result_json),
                        "stored_error_hash": _stored_json_hash(row["error_json"]),
                        "received_error_hash": _stored_json_hash(error_json),
                    },
                    operation_id=operation_value,
                    entity_revision=expected_revision,
                )
                terminal = self._operation_from_row(row)
                terminal["late_receipt_conflict"] = conflict
                return terminal
            now = time.time()
            cursor = connection.execute(
                """
                UPDATE operations
                SET state = ?, revision = revision + 1, result_json = ?, error_json = ?, updated_at = ?
                WHERE operation_id = ? AND revision = ? AND state = ?
                """,
                (target_state, result_json, error_json, now, operation_value, expected_revision, current_state),
            )
            if cursor.rowcount != 1:
                return None
            self._append_event_in_transaction(
                connection,
                "operation.state_changed",
                {"from": current_state, "to": target_state},
                operation_id=operation_value,
                entity_revision=expected_revision + 1,
            )
            saved = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_value,)).fetchone()
            return self._operation_from_row(saved)

    def compare_and_swap_operation(
        self, operation_id: str, expected_revision: int, state: str, *, expected_state: str | None = None
    ) -> bool:
        return (
            self.cas_operation_state(
                operation_id,
                expected_revision=expected_revision,
                expected_state=expected_state,
                state=state,
            )
            is not None
        )

    def _operation_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": str(row["operation_id"]),
            "principal_ref": str(row["principal_ref"]),
            "tool": str(row["tool"]),
            "logical_target": str(row["logical_target"]),
            "idempotency_key": str(row["idempotency_key"]),
            "semantic_payload_hash": str(row["semantic_payload_hash"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "parent_operation_id": row["parent_operation_id"],
            "item_id": str(row["item_id"]),
            "result": _decode_optional_json(row["result_json"], context=f"operation {row['operation_id']} result"),
            "error": _decode_optional_json(row["error_json"], context=f"operation {row['operation_id']} error"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def create_attempt(
        self,
        operation_id: str,
        *,
        machine_id: str,
        edge_generation: int,
        attempt_id: str = "",
        state: str = "offered",
        lease_expires_at: float | None = None,
    ) -> dict[str, Any]:
        operation_value = _clean_key(operation_id, "operation_id")
        machine_value = _clean_key(machine_id, "machine_id")
        generation_value = int(edge_generation)
        if generation_value < 0:
            raise ValueError("edge_generation must be non-negative")
        state_value = _validate_state(state, ATTEMPT_STATES, "attempt")
        attempt_value = attempt_id or f"attempt_{secrets.token_hex(16)}"
        now = time.time()
        with self.immediate_transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM operations WHERE operation_id = ?", (operation_value,)
            ).fetchone() is None:
                raise KeyError(f"Unknown operation: {operation_value}")
            token = int(
                connection.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) + 1 FROM attempts WHERE operation_id = ?",
                    (operation_value,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO attempts
                    (attempt_id, operation_id, machine_id, edge_generation, fencing_token,
                     state, revision, lease_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    attempt_value,
                    operation_value,
                    machine_value,
                    generation_value,
                    token,
                    state_value,
                    lease_expires_at,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_value,)).fetchone()
            return self._attempt_from_row(row)

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (_clean_key(attempt_id, "attempt_id"),)
        ).fetchone()
        return self._attempt_from_row(row) if row is not None else None

    def cas_attempt_state(
        self,
        attempt_id: str,
        *,
        expected_revision: int,
        expected_fencing_token: int,
        state: str,
        expected_operation_id: str | None = None,
        expected_machine_id: str | None = None,
        expected_edge_generation: int | None = None,
        result: Mapping[str, Any] | None = None,
        lease_expires_at: float | None = None,
    ) -> dict[str, Any] | None:
        attempt_value = _clean_key(attempt_id, "attempt_id")
        target_state = _validate_state(state, ATTEMPT_STATES, "attempt")
        with self.immediate_transaction() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_value,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown attempt: {attempt_value}")
            if int(row["revision"]) != expected_revision or int(row["fencing_token"]) != expected_fencing_token:
                return None
            if expected_operation_id is not None and str(row["operation_id"]) != expected_operation_id:
                return None
            if expected_machine_id is not None and str(row["machine_id"]) != expected_machine_id:
                return None
            if expected_edge_generation is not None and int(row["edge_generation"]) != expected_edge_generation:
                return None
            current_state = str(row["state"])
            _require_transition(current_state, target_state, _ATTEMPT_TRANSITIONS, "attempt")
            result_json = row["result_json"] if result is None else _encode_json_object(result, field="result")
            new_lease = row["lease_expires_at"] if lease_expires_at is None else lease_expires_at
            cursor = connection.execute(
                """
                UPDATE attempts
                SET state = ?, revision = revision + 1, result_json = ?, lease_expires_at = ?, updated_at = ?
                WHERE attempt_id = ? AND revision = ? AND fencing_token = ?
                """,
                (
                    target_state,
                    result_json,
                    new_lease,
                    time.time(),
                    attempt_value,
                    expected_revision,
                    expected_fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                return None
            saved = connection.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_value,)).fetchone()
            return self._attempt_from_row(saved)

    def _attempt_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": str(row["attempt_id"]),
            "operation_id": str(row["operation_id"]),
            "machine_id": str(row["machine_id"]),
            "edge_generation": int(row["edge_generation"]),
            "fencing_token": int(row["fencing_token"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "lease_expires_at": row["lease_expires_at"],
            "result": _decode_optional_json(row["result_json"], context=f"attempt {row['attempt_id']} result"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def append_event(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        entity_type: str = "",
        entity_id: str = "",
        entity_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.immediate_transaction() as connection:
            return self._append_event_in_transaction(
                connection,
                event_type,
                data,
                operation_id=operation_id,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_revision=entity_revision,
            )

    def _append_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        data: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        entity_type: str = "",
        entity_id: str = "",
        entity_revision: int | None = None,
        legacy_classification: str = "",
        source_import_id: str | None = None,
        source_ordinal: int | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        event_id = f"evt_{secrets.token_hex(16)}"
        cursor = connection.execute(
            """
            INSERT INTO events
                (event_id, event_type, operation_id, entity_type, entity_id, entity_revision,
                 data_json, legacy_classification, source_import_id, source_ordinal, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                _clean_key(event_type, "event_type"),
                operation_id,
                entity_type,
                entity_id,
                entity_revision,
                _encode_json_object(data, field="event data"),
                legacy_classification,
                source_import_id,
                source_ordinal,
                float(created_at if created_at is not None else time.time()),
            ),
        )
        row = connection.execute(
            "SELECT * FROM events WHERE event_revision = ?", (int(cursor.lastrowid),)
        ).fetchone()
        event = self._event_from_row(row)
        self._materialize_scoped_operation_group_association(
            connection, event_type=str(event_type), operation_id=operation_id
        )
        self._materialize_scoped_batch_operation_group_association(
            connection, event_type=str(event_type), operation_id=operation_id
        )
        return event

    def _materialize_scoped_operation_group_association(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        operation_id: str | None,
    ) -> None:
        if event_type != "operation.created" or not operation_id:
            return
        intents = _OPERATION_GROUP_ASSOCIATION_INTENTS.get()
        if not intents:
            return
        operation = connection.execute(
            """
            SELECT operation_id, principal_ref, tool, logical_target,
                   idempotency_key, parent_operation_id
            FROM operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise HubStoreV2Corrupt(
                f"Operation disappeared while materializing group association: {operation_id}"
            )
        for intent in reversed(intents):
            matches = (
                operation["parent_operation_id"] is None
                and str(operation["tool"]) == intent.tool
                and str(operation["logical_target"]) == intent.logical_target
                and str(operation["idempotency_key"]) == intent.idempotency_key
                and str(operation["principal_ref"]) == intent.principal_ref
            )
            if not matches:
                continue
            operation_value = str(operation["operation_id"])
            self._put_scoped_operation_group_association_in_transaction(
                connection,
                operation_id=operation_value,
                work_group_id=intent.work_group_id,
            )
            intent.associated_operation_id = operation_value
            return

    def _materialize_scoped_batch_operation_group_association(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        operation_id: str | None,
    ) -> None:
        if event_type != "operation.created" or not operation_id:
            return
        intents = _BATCH_OPERATION_GROUP_ASSOCIATION_INTENTS.get()
        if not intents:
            return
        operation = connection.execute(
            """
            SELECT operation_id, principal_ref, tool, logical_target,
                   idempotency_key, parent_operation_id, item_id
            FROM operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise HubStoreV2Corrupt(
                f"Operation disappeared while materializing batch association: {operation_id}"
            )
        for intent in reversed(intents):
            is_parent = (
                operation["parent_operation_id"] is None
                and str(operation["tool"]) == "patchbay_worker_start_batch"
                and str(operation["logical_target"]) == intent.logical_target
                and str(operation["idempotency_key"]) == intent.idempotency_key
                and str(operation["principal_ref"]) == intent.principal_ref
            )
            is_child = (
                bool(intent.parent_operation_id)
                and str(operation["parent_operation_id"] or "")
                == intent.parent_operation_id
                and str(operation["item_id"]) in intent.child_item_ids
            )
            if not is_parent and not is_child:
                continue
            operation_value = str(operation["operation_id"])
            if is_parent:
                intent.parent_operation_id = operation_value
            self._put_scoped_operation_group_association_in_transaction(
                connection,
                operation_id=operation_value,
                work_group_id=intent.work_group_id,
            )
            intent.associated_operation_ids.add(operation_value)
            return

    def _put_scoped_operation_group_association_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        work_group_id: str,
    ) -> None:
        existing = connection.execute(
            """
            SELECT * FROM entity_records
            WHERE entity_type = ? AND entity_id = ?
            """,
            (OPERATION_GROUP_ASSOCIATION_ENTITY_TYPE, operation_id),
        ).fetchone()
        if existing is not None:
            record = self._entity_from_row(existing)["record"]
            if str(record.get("work_group_id") or "") != work_group_id:
                raise HubStoreV2Conflict("operation_work_group_conflict")
            return
        saved = self._put_entity_in_transaction(
            connection,
            OPERATION_GROUP_ASSOCIATION_ENTITY_TYPE,
            operation_id,
            {
                "operation_id": operation_id,
                "work_group_id": work_group_id,
                "kind": "worker",
            },
            expected_revision=0,
            legacy_classification="",
        )
        if saved is None:
            raise HubStoreV2Conflict("operation_work_group_conflict")

    def list_events(self, *, after_revision: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self._require_open()
        bounded_limit = max(1, min(int(limit), 1_000))
        rows = self._connection.execute(
            "SELECT * FROM events WHERE event_revision > ? ORDER BY event_revision ASC LIMIT ?",
            (max(0, int(after_revision)), bounded_limit),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def _event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_revision": int(row["event_revision"]),
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "operation_id": row["operation_id"],
            "entity_type": str(row["entity_type"]),
            "entity_id": str(row["entity_id"]),
            "entity_revision": row["entity_revision"],
            "data": _decode_json_object(row["data_json"], context=f"event {row['event_id']}"),
            "legacy_classification": str(row["legacy_classification"]),
            "source_import_id": row["source_import_id"],
            "source_ordinal": row["source_ordinal"],
            "created_at": float(row["created_at"]),
        }

    def create_payload_metadata(
        self,
        operation_id: str,
        *,
        payload_kind: str,
        checksum_sha256: str,
        size_bytes: int,
        storage_ref: str,
        expires_at: float | None,
        metadata: Mapping[str, Any] | None = None,
        payload_id: str = "",
    ) -> dict[str, Any]:
        payload_value = payload_id or f"payload_{secrets.token_hex(16)}"
        now = time.time()
        with self.immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO payload_metadata
                    (payload_id, operation_id, payload_kind, checksum_sha256, size_bytes,
                     storage_ref, status, revision, expires_at, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ready', 1, ?, ?, ?, ?)
                """,
                (
                    payload_value,
                    _clean_key(operation_id, "operation_id"),
                    _clean_key(payload_kind, "payload_kind"),
                    _clean_key(checksum_sha256, "checksum_sha256"),
                    int(size_bytes),
                    storage_ref,
                    expires_at,
                    _encode_json_object(metadata or {}, field="payload metadata"),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM payload_metadata WHERE payload_id = ?", (payload_value,)
            ).fetchone()
            return self._payload_metadata_from_row(row)

    def get_payload_metadata(self, payload_id: str) -> dict[str, Any] | None:
        self._require_open()
        row = self._connection.execute(
            "SELECT * FROM payload_metadata WHERE payload_id = ?", (_clean_key(payload_id, "payload_id"),)
        ).fetchone()
        return self._payload_metadata_from_row(row) if row is not None else None

    def _payload_metadata_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "payload_id": str(row["payload_id"]),
            "operation_id": str(row["operation_id"]),
            "payload_kind": str(row["payload_kind"]),
            "checksum_sha256": str(row["checksum_sha256"]),
            "size_bytes": int(row["size_bytes"]),
            "storage_ref": str(row["storage_ref"]),
            "status": str(row["status"]),
            "revision": int(row["revision"]),
            "expires_at": row["expires_at"],
            "acknowledged_at": row["acknowledged_at"],
            "metadata": _decode_json_object(row["metadata_json"], context=f"payload {row['payload_id']}"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def import_v1_json(self, source: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
        """Import one V1 JSON snapshot without modifying or replaying the source."""

        source_path = Path(source).expanduser()
        try:
            source_stat = source_path.stat()
            raw = source_path.read_bytes()
        except OSError as exc:
            raise HubStoreV2Corrupt(f"Cannot read V1 Hub state: {source_path}") from exc
        checksum = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HubStoreV2Corrupt(f"V1 Hub state is corrupt JSON: {source_path}") from exc
        if not isinstance(payload, dict):
            raise HubStoreV2Corrupt("V1 Hub state is corrupt: root payload is not an object")

        typed_records, legacy_events, counts, recovery_count = _classify_v1_payload(payload)
        import_id = f"legacy_{checksum}"
        report = {
            "import_id": import_id,
            "source_path": str(source_path.resolve(strict=False)),
            "checksum_sha256": checksum,
            "source_size_bytes": len(raw),
            "source_mtime_ns": int(source_stat.st_mtime_ns),
            "source_version": _optional_int(payload.get("version")),
            "counts": counts,
            "legacy_recovery_required_count": recovery_count,
            "source_unchanged": True,
            "dry_run": bool(dry_run),
            "already_imported": False,
        }
        if dry_run:
            return report

        with self.immediate_transaction(mark_mutation=False) as connection:
            existing = connection.execute(
                "SELECT * FROM legacy_imports WHERE source_checksum = ?", (checksum,)
            ).fetchone()
            if existing is not None:
                return self._legacy_import_report(existing, already_imported=True)

            imported_at = time.time()
            connection.execute(
                """
                INSERT INTO legacy_imports
                    (import_id, source_path, source_checksum, source_size_bytes, source_mtime_ns,
                     source_version, source_hub_id, source_created_at, counts_json,
                     recovery_required_count, imported_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete')
                """,
                (
                    import_id,
                    report["source_path"],
                    checksum,
                    len(raw),
                    source_stat.st_mtime_ns,
                    report["source_version"],
                    str(payload.get("hub_id") or ""),
                    _optional_float(payload.get("created_at")),
                    _encode_json_object(counts, field="legacy counts"),
                    recovery_count,
                    imported_at,
                ),
            )
            for typed in typed_records:
                saved = self._put_entity_in_transaction(
                    connection,
                    typed["entity_type"],
                    typed["entity_id"],
                    typed["record"],
                    expected_revision=0,
                    legacy_classification=typed["legacy_classification"],
                    source_import_id=import_id,
                )
                if saved is None:
                    raise HubStoreV2Conflict(
                        f"Legacy entity already exists from another import: {typed['entity_type']}/{typed['entity_id']}"
                    )
            for ordinal, event in enumerate(legacy_events):
                event_created_at = _optional_float(event.get("created_at"))
                self._append_event_in_transaction(
                    connection,
                    str(event.get("type") or "legacy.event"),
                    event,
                    legacy_classification="legacy_event",
                    source_import_id=import_id,
                    source_ordinal=ordinal,
                    created_at=imported_at if event_created_at is None else event_created_at,
                )
            entity_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM entity_records WHERE source_import_id = ?", (import_id,)
                ).fetchone()[0]
            )
            event_count = int(
                connection.execute("SELECT COUNT(*) FROM events WHERE source_import_id = ?", (import_id,)).fetchone()[0]
            )
            if entity_count != len(typed_records) or event_count != len(legacy_events):
                raise HubStoreV2Corrupt("V1 import count validation failed")
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise HubStoreV2Corrupt("V1 import referential-integrity validation failed")
        return report

    import_v1 = import_v1_json

    def list_legacy_imports(self) -> list[dict[str, Any]]:
        self._require_open()
        rows = self._connection.execute("SELECT * FROM legacy_imports ORDER BY imported_at, import_id").fetchall()
        return [self._legacy_import_report(row, already_imported=True) for row in rows]

    @staticmethod
    def _legacy_import_report(row: sqlite3.Row, *, already_imported: bool) -> dict[str, Any]:
        return {
            "import_id": str(row["import_id"]),
            "source_path": str(row["source_path"]),
            "checksum_sha256": str(row["source_checksum"]),
            "source_size_bytes": int(row["source_size_bytes"]),
            "source_mtime_ns": int(row["source_mtime_ns"]),
            "source_version": row["source_version"],
            "counts": _decode_json_object(row["counts_json"], context=f"legacy import {row['import_id']}"),
            "legacy_recovery_required_count": int(row["recovery_required_count"]),
            "source_unchanged": True,
            "dry_run": False,
            "already_imported": already_imported,
            "imported_at": float(row["imported_at"]),
        }

    def close(self) -> None:
        """Rollback unfinished work and close safely; repeated calls are harmless."""

        with self._lock:
            if self._closed:
                return
            try:
                if self._connection.in_transaction:
                    self._connection.rollback()
            finally:
                self._connection.close()
                self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise HubStoreV2Error("Hub V2 store is closed")

    def __enter__(self) -> HubStoreV2:
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _classify_v1_payload(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], int]:
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    recovery_count = 0
    for source_key in ("enrollment_codes", "machines", "commands", "work_groups"):
        source_records = payload.get(source_key, {})
        if not isinstance(source_records, Mapping):
            raise HubStoreV2Corrupt(f"V1 Hub state is corrupt: {source_key} is not an object")
        counts[source_key] = len(source_records)
        for entity_id, record in source_records.items():
            if not isinstance(record, Mapping):
                raise HubStoreV2Corrupt(
                    f"V1 Hub state is corrupt: {source_key}/{entity_id} is not an object"
                )
            classification = LEGACY_CLASSIFICATIONS[source_key]
            if source_key == "commands" and str(record.get("state") or "").lower() in ACTIVE_LEGACY_COMMAND_STATES:
                classification = LEGACY_RECOVERY_REQUIRED
                recovery_count += 1
            records.append(
                {
                    "entity_type": LEGACY_ENTITY_TYPES[source_key],
                    "entity_id": str(entity_id),
                    "record": deepcopy(dict(record)),
                    "legacy_classification": classification,
                }
            )

    pointers = payload.get("current_work_group_by_manager", {})
    if not isinstance(pointers, Mapping):
        raise HubStoreV2Corrupt(
            "V1 Hub state is corrupt: current_work_group_by_manager is not an object"
        )
    counts["current_work_group_by_manager"] = len(pointers)
    for manager_ref, work_group_id in pointers.items():
        records.append(
            {
                "entity_type": LEGACY_ENTITY_TYPES["current_work_group_by_manager"],
                "entity_id": str(manager_ref),
                "record": {"manager_ref": manager_ref, "work_group_id": work_group_id},
                "legacy_classification": LEGACY_CLASSIFICATIONS["current_work_group_by_manager"],
            }
        )

    events = payload.get("events", [])
    if not isinstance(events, list):
        raise HubStoreV2Corrupt("V1 Hub state is corrupt: events is not an array")
    legacy_events: list[dict[str, Any]] = []
    for ordinal, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise HubStoreV2Corrupt(f"V1 Hub state is corrupt: event {ordinal} is not an object")
        legacy_events.append(deepcopy(dict(event)))
    counts["events"] = len(legacy_events)
    return records, legacy_events, counts, recovery_count


def _encode_json_object(value: Mapping[str, Any], *, field: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field} must contain valid JSON values") from exc


def _decode_json_object(raw: str, *, context: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HubStoreV2Corrupt(f"Stored JSON is corrupt for {context}") from exc
    if not isinstance(value, dict):
        raise HubStoreV2Corrupt(f"Stored JSON is not an object for {context}")
    return value


def _decode_optional_json(raw: str | None, *, context: str) -> dict[str, Any] | None:
    return None if raw is None else _decode_json_object(raw, context=context)


def _clean_key(value: Any, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _validate_state(value: str, allowed: frozenset[str], kind: str) -> str:
    state = _clean_key(value, f"{kind} state")
    if state not in allowed:
        raise HubStoreV2StateError(f"Unknown {kind} state: {state}")
    return state


def _require_transition(
    current: str, target: str, transitions: Mapping[str, set[str]], kind: str
) -> None:
    if target == current:
        return
    if target not in transitions.get(current, set()):
        raise HubStoreV2StateError(f"Invalid {kind} transition: {current} -> {target}")


def _sql_values(values: frozenset[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stored_json_hash(raw: str | None) -> str:
    return "" if raw is None else hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


# Compatibility aliases for stable call-site naming.
HubSQLiteStore = HubStoreV2
HubStoreCorrupt = HubStoreV2Corrupt
hub_v2_state_path = hub_state_v2_path
