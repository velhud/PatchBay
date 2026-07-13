"""Private, WAL-consistent backup and restore support for Hub V2 state.

The V1 JSON migration snapshot in :mod:`patchbay.hub.cli_v2` is deliberately
separate from this module.  Hub V2 and Edge state are live SQLite databases;
copying their main files while a WAL is active does not preserve committed
state.  These helpers always use :meth:`sqlite3.Connection.backup` instead.

``AdmissionFreezeController`` is shared by the Hub runtime and backup CLI. Its
private lock directory lets a separate operator process stop new mutation
admissions, drain already-admitted dispatches, and snapshot the live database
without interrupting read/status traffic.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager, Iterator, Mapping, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the documented offline path.
    fcntl = None

from patchbay.hub.edge_journal import SCHEMA_VERSION as EDGE_JOURNAL_SCHEMA_VERSION
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.store_v2 import SCHEMA_VERSION as HUB_STORE_SCHEMA_VERSION
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.tool_surface import (
    HUB_V2_CONTRACT_HASH,
    HUB_V2_CONTRACT_VERSION,
    HUB_V2_MANIFEST_HASH,
    HUB_V2_SCHEMA_HASH,
)


DATABASE_KIND_HUB_V2 = "hub_v2"
DATABASE_KIND_EDGE_V2 = "edge_v2"
DATABASE_KINDS = frozenset({DATABASE_KIND_HUB_V2, DATABASE_KIND_EDGE_V2})
V2_BACKUP_MANIFEST_VERSION = 2
V2_BACKUP_MANIFEST_SUFFIX = ".manifest.json"
PRE_MIGRATION_BACKUP_MARKER_VERSION = 1
PRE_MIGRATION_BACKUP_MARKER_SUFFIX = ".pre-migration-backup.json"
DEFAULT_BACKUP_BUSY_TIMEOUT_MS = 30_000

_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "created_at",
        "database",
        "source",
        "backup",
        "deployed_contract",
        "state_proof",
        "manifest_sha256",
    }
)
_DATABASE_FIELDS = frozenset(
    {"kind", "generation", "schema_version", "user_version", "integrity_check"}
)
_SOURCE_FIELDS = frozenset({"path", "sha256", "size_bytes", "mtime_ns"})
_BACKUP_FIELDS = frozenset({"sha256", "size_bytes"})
_DEPLOYED_CONTRACT_FIELDS = frozenset(
    {
        "deployed_revision",
        "contract_version",
        "contract_hash",
        "manifest_hash",
        "schema_hash",
    }
)
_PROOF_FIELDS = frozenset({"count", "sha256"})
_STATE_PROOF_SCOPE = "complete_durable_sqlite_state_v1"
_HUB_STATE_PROOF_FIELDS = frozenset(
    {
        "scope",
        "database",
        "schema",
        "identity",
        "tables",
        "entity_types",
        "groups",
        "operations",
        "receipts",
        "attempts",
    }
)
_EDGE_STATE_PROOF_FIELDS = frozenset(
    {
        "scope",
        "database",
        "schema",
        "tables",
        "groups",
        "operations",
        "receipts",
        "attempts",
    }
)
_PRE_MIGRATION_MARKER_FIELDS = frozenset(
    {
        "marker_version",
        "created_at",
        "database_kind",
        "database_generation",
        "source_path",
        "source_schema_version",
        "target_schema_version",
        "source_state_sha256",
        "backup_path",
        "backup_manifest_path",
        "backup_manifest_sha256",
        "marker_sha256",
    }
)
_HUB_REQUIRED_TABLES = frozenset(
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
_EDGE_REQUIRED_TABLES = frozenset(
    {
        "schema_metadata",
        "edge_state",
        "operation_intents",
        "operation_attempts",
        "result_outbox",
    }
)
_HUB_SCHEMA_TABLES = {
    2: frozenset({"entity_control_index"}),
    3: frozenset({"operation_group_index"}),
}
_EDGE_SCHEMA_TABLES = {
    2: frozenset({"control_loop_health"}),
}
_HUB_AUTHORITATIVE_TABLES = frozenset(
    {
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
_EDGE_AUTHORITATIVE_TABLES = frozenset(
    {"edge_state", "operation_intents", "operation_attempts", "result_outbox"}
)


class BackupV2Error(RuntimeError):
    """Base error for Hub V2/Edge SQLite backup operations."""


class BackupV2ValidationError(BackupV2Error):
    """Raised when a backup or its manifest cannot be validated."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report or {})


class BackupV2RestoreError(BackupV2Error):
    """Raised when a restore target is unsafe or post-restore checks fail."""


class AdmissionFrozenError(BackupV2Error):
    """Raised when dispatch attempts to enter a deliberately frozen admission gate."""


class AdmissionFreezeLease(Protocol):
    """A held admission freeze that can wait for currently admitted work."""

    def wait_for_drain(self, timeout_seconds: float | None = None) -> bool:
        """Return whether all mutation tickets completed before the deadline."""

    def release(self) -> None:
        """Reopen admission. Repeated calls must be harmless."""


class AdmissionFreezeGate(Protocol):
    """Runtime contract for wiring Hub mutation dispatch to backup coordination.

    The Hub runtime should wrap each *new mutating* dispatch in
    ``with gate.admit_mutation():``.  A backup coordinator can then call
    ``freeze_admissions`` before it snapshots durable state.  Existing work is
    not cancelled; the returned lease merely blocks future admission and can
    wait for already-admitted dispatches to leave their critical section.
    """

    def admit_mutation(self) -> ContextManager[None]:
        """Enter one short mutation-admission critical section."""

    def freeze_admissions(self, *, reason: str) -> AdmissionFreezeLease:
        """Block new mutation admission and return the owning lease."""


@dataclass
class _AdmissionLease:
    controller: "AdmissionFreezeController"
    freeze_id: int
    released: bool = False

    def wait_for_drain(self, timeout_seconds: float | None = None) -> bool:
        return self.controller._wait_for_drain(self.freeze_id, timeout_seconds)

    def release(self) -> None:
        if not self.released:
            self.controller._release(self.freeze_id)
            self.released = True


class AdmissionFreezeController:
    """Process-local and cross-process Hub mutation admission gate.

    A coordination directory beside the Hub database uses one short intent lock,
    one shared/exclusive admission lock, and a private owner marker. The marker
    blocks new admissions before the exclusive lock waits for already-admitted
    mutations to drain. Kernel locks are released automatically on process
    death; a PID/start-identity check clears a stale marker on the next request.
    """

    def __init__(self, coordination_path: str | Path | None = None) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._active_admissions = 0
        self._freeze_id = 0
        self._active_freeze_id: int | None = None
        self._freeze_reason = ""
        self._freeze_started_at: float | None = None
        self.coordination_path = (
            Path(coordination_path).expanduser().resolve(strict=False)
            if coordination_path is not None
            else None
        )
        self._freeze_file_handle: Any = None
        self._freeze_file_locked = False
        self._freeze_marker: dict[str, Any] = {}
        if self.coordination_path is not None:
            if fcntl is None:
                raise BackupV2Error(
                    "Cross-process Hub admission freeze is unavailable on this platform; use an offline service pause."
                )
            _prepare_private_directory(self.coordination_path)
            self._ensure_coordination_files()

    @contextmanager
    def admit_mutation(self) -> Iterator[None]:
        with self._condition:
            if self._active_freeze_id is not None:
                raise AdmissionFrozenError(
                    f"Hub mutation admission is frozen: {self._freeze_reason or 'maintenance'}"
                )
            self._active_admissions += 1
        file_handle = None
        try:
            if self.coordination_path is not None:
                file_handle = self._acquire_shared_admission()
            yield
        finally:
            if file_handle is not None:
                self._unlock_close(file_handle)
            with self._condition:
                self._active_admissions -= 1
                self._condition.notify_all()

    def freeze_admissions(self, *, reason: str) -> AdmissionFreezeLease:
        value = " ".join(str(reason or "").split())
        if not value:
            raise ValueError("admission freeze reason is required")
        with self._condition:
            if self._active_freeze_id is not None:
                raise AdmissionFrozenError("Hub mutation admission is already frozen")
            self._freeze_id += 1
            self._active_freeze_id = self._freeze_id
            self._freeze_reason = value
            self._freeze_started_at = time.time()
            freeze_id = self._freeze_id
        try:
            if self.coordination_path is not None:
                self._begin_cross_process_freeze(freeze_id, value)
            return _AdmissionLease(self, freeze_id)
        except Exception:
            with self._condition:
                self._active_freeze_id = None
                self._freeze_reason = ""
                self._freeze_started_at = None
                self._condition.notify_all()
            raise

    def state(self) -> dict[str, Any]:
        external = self._read_marker() if self.coordination_path is not None else {}
        with self._condition:
            return {
                "frozen": self._active_freeze_id is not None or bool(external),
                "freeze_id": self._active_freeze_id or external.get("freeze_id"),
                "reason": self._freeze_reason or str(external.get("reason") or ""),
                "started_at": self._freeze_started_at or external.get("started_at"),
                "active_admissions": self._active_admissions,
                "cross_process": self.coordination_path is not None,
            }

    def _wait_for_drain(self, freeze_id: int, timeout_seconds: float | None) -> bool:
        if timeout_seconds is not None and float(timeout_seconds) < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + float(timeout_seconds)
        )
        with self._condition:
            self._require_lease(freeze_id)
            while self._active_admissions:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
                self._require_lease(freeze_id)
        if self.coordination_path is None or self._freeze_file_locked:
            return True
        handle = self._freeze_file_handle
        if handle is None:
            raise AdmissionFrozenError("Admission freeze file lease is missing")
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._freeze_file_locked = True
                return True
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                time.sleep(
                    0.05
                    if deadline is None
                    else min(0.05, max(0.0, deadline - time.monotonic()))
                )

    def _release(self, freeze_id: int) -> None:
        marker = dict(self._freeze_marker)
        if self.coordination_path is not None:
            with self._intent_lock():
                current = self._read_marker()
                if current and self._same_marker(current, marker):
                    _unlink_if_exists(self._marker_path)
            handle = self._freeze_file_handle
            self._freeze_file_handle = None
            self._freeze_file_locked = False
            self._freeze_marker = {}
            if handle is not None:
                self._unlock_close(handle)
        with self._condition:
            self._require_lease(freeze_id)
            self._active_freeze_id = None
            self._freeze_reason = ""
            self._freeze_started_at = None
            self._condition.notify_all()

    def _require_lease(self, freeze_id: int) -> None:
        if self._active_freeze_id != freeze_id:
            raise AdmissionFrozenError("Admission freeze lease is no longer active")

    @property
    def _intent_path(self) -> Path:
        assert self.coordination_path is not None
        return self.coordination_path / "intent.lock"

    @property
    def _active_path(self) -> Path:
        assert self.coordination_path is not None
        return self.coordination_path / "active.lock"

    @property
    def _marker_path(self) -> Path:
        assert self.coordination_path is not None
        return self.coordination_path / "freeze.json"

    def _ensure_coordination_files(self) -> None:
        for path in (self._intent_path, self._active_path):
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(descriptor)
            _chmod_private_file(path)

    @contextmanager
    def _intent_lock(self) -> Iterator[None]:
        handle = self._intent_path.open("a+b", buffering=0)
        _chmod_private_file(self._intent_path)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            self._unlock_close(handle)

    def _acquire_shared_admission(self) -> Any:
        with self._intent_lock():
            marker = self._read_marker()
            if marker and self._marker_owner_live(marker):
                raise AdmissionFrozenError(
                    f"Hub mutation admission is frozen: {marker.get('reason') or 'maintenance'}"
                )
            if marker:
                _unlink_if_exists(self._marker_path)
            handle = self._active_path.open("a+b", buffering=0)
            _chmod_private_file(self._active_path)
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            return handle

    def _begin_cross_process_freeze(self, freeze_id: int, reason: str) -> None:
        identity = _process_start_identity(os.getpid())
        marker = {
            "freeze_id": f"{os.getpid()}-{freeze_id}-{secrets.token_hex(8)}",
            "pid": os.getpid(),
            "process_identity": identity,
            "reason": reason,
            "started_at": time.time(),
        }
        with self._intent_lock():
            current = self._read_marker()
            if current and self._marker_owner_live(current):
                raise AdmissionFrozenError(
                    f"Hub mutation admission is already frozen: {current.get('reason') or 'maintenance'}"
                )
            if current:
                _unlink_if_exists(self._marker_path)
            _write_private_json_exclusive(self._marker_path, marker)
        self._freeze_file_handle = self._active_path.open("a+b", buffering=0)
        _chmod_private_file(self._active_path)
        self._freeze_marker = marker

    def _read_marker(self) -> dict[str, Any]:
        if self.coordination_path is None or not self._marker_path.is_file():
            return {}
        try:
            payload = json.loads(self._marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"reason": "invalid maintenance marker", "pid": -1}
        return dict(payload) if isinstance(payload, Mapping) else {}

    @staticmethod
    def _same_marker(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
        return bool(first) and str(first.get("freeze_id") or "") == str(
            second.get("freeze_id") or ""
        )

    @staticmethod
    def _marker_owner_live(marker: Mapping[str, Any]) -> bool:
        try:
            pid = int(marker.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        expected = str(marker.get("process_identity") or "")
        return bool(pid > 0 and expected and _process_start_identity(pid) == expected)

    @staticmethod
    def _unlock_close(handle: Any) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def admission_coordination_path(database_path: str | Path) -> Path:
    """Return the private cross-process admission directory for one Hub DB."""

    database = Path(database_path).expanduser().resolve(strict=False)
    return database.with_name(f".{database.name}.admission")


def _process_start_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        text = proc_stat.read_text(encoding="utf-8")
        _, separator, suffix = text.rpartition(")")
        fields = suffix.strip().split() if separator else []
        if len(fields) > 19:
            boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="utf-8")
                .strip()
            )
            return f"linux:{boot_id}:{fields[19]}"
    except OSError:
        pass
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    started = " ".join(completed.stdout.split())
    return f"ps:{started}" if completed.returncode == 0 and started else ""


def v2_backup_manifest_path(backup_path: str | Path) -> Path:
    """Return the sidecar manifest path for one V2 SQLite snapshot."""

    return Path(f"{Path(backup_path).expanduser()}{V2_BACKUP_MANIFEST_SUFFIX}")


def pre_migration_backup_marker_path(database_path: str | Path) -> Path:
    """Return the private marker path checked before an in-place migration."""

    database = Path(database_path).expanduser().resolve(strict=False)
    return database.with_name(f".{database.name}{PRE_MIGRATION_BACKUP_MARKER_SUFFIX}")


def create_pre_migration_backup_marker(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    database_kind: str = DATABASE_KIND_HUB_V2,
    target_schema_version: int | None = None,
    marker_path: str | Path | None = None,
    expected_generation: str = "",
    expected_deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Bind a validated backup to the exact source state before migration.

    Callers must keep mutation admission offline or frozen across backup and
    marker creation. The marker is deliberately separate from store startup so
    an integrator can require it before constructing a migration-capable store.
    """

    kind = _database_kind(database_kind)
    timeout = _busy_timeout(busy_timeout_ms)
    source = _existing_database_path(source_path, label="pre-migration source database")
    backup = _existing_database_path(backup_path, label="pre-migration backup database")
    marker = (
        Path(marker_path).expanduser().resolve(strict=False)
        if marker_path is not None
        else pre_migration_backup_marker_path(source)
    )
    _prepare_private_directory(marker.parent)

    source_snapshot = _inspect_database(
        source,
        expected_kind=kind,
        timeout=timeout,
        allow_supported_older=True,
    )
    target = _target_schema_version(kind, target_schema_version)
    source_schema = int(source_snapshot["database"]["schema_version"])
    if source_schema >= target:
        raise BackupV2Error(
            "pre-migration backup marker requires an older source schema: "
            f"source={source_schema}, target={target}"
        )

    validation = validate_v2_backup(
        backup,
        expected_kind=kind,
        expected_generation=expected_generation,
        expected_deployed_revision=expected_deployed_revision,
        busy_timeout_ms=timeout,
    )
    errors = _source_backup_binding_errors(source, source_snapshot, validation)
    if errors:
        report = {
            "status": "failed",
            "valid": False,
            "source_path": str(source),
            "backup_path": str(backup),
            "marker_path": str(marker),
            "errors": errors,
            "backup_validation": validation,
        }
        raise BackupV2ValidationError(
            "pre-migration backup does not match the current source",
            report=report,
        )

    manifest_path = v2_backup_manifest_path(backup).resolve(strict=False)
    marker_payload: dict[str, Any] = {
        "marker_version": PRE_MIGRATION_BACKUP_MARKER_VERSION,
        "created_at": time.time(),
        "database_kind": kind,
        "database_generation": str(source_snapshot["database"]["generation"]),
        "source_path": str(source),
        "source_schema_version": source_schema,
        "target_schema_version": target,
        "source_state_sha256": _state_proof_checksum(source_snapshot["state_proof"]),
        "backup_path": str(backup),
        "backup_manifest_path": str(manifest_path),
        "backup_manifest_sha256": _sha256_file(manifest_path),
    }
    marker_payload["marker_sha256"] = _marker_checksum(marker_payload)
    marker_publication = "created"
    try:
        _write_private_json_exclusive(marker, marker_payload)
    except BackupV2Error:
        if not marker.exists():
            raise
        existing = validate_pre_migration_backup_marker(
            source,
            database_kind=kind,
            target_schema_version=target,
            marker_path=marker,
            expected_generation=expected_generation,
            expected_deployed_revision=expected_deployed_revision,
            busy_timeout_ms=timeout,
        )
        if not existing["valid"] or str(existing.get("backup_path") or "") != str(
            backup
        ):
            raise BackupV2ValidationError(
                "existing pre-migration marker is invalid or belongs to another artifact; preserved in place",
                report=existing,
            )
        marker_payload = dict(existing["marker"])
        marker_publication = "reused"
    return {
        "status": marker_publication,
        "created": marker_publication == "created",
        "reused": marker_publication == "reused",
        "publication": {"marker": marker_publication},
        "valid": True,
        "required": True,
        "source_path": str(source),
        "backup_path": str(backup),
        "marker_path": str(marker),
        "source_schema_version": source_schema,
        "target_schema_version": target,
        "database_generation": str(source_snapshot["database"]["generation"]),
        "source_state_sha256": marker_payload["source_state_sha256"],
        "backup_validation": validation,
    }


def validate_pre_migration_backup_marker(
    source_path: str | Path,
    *,
    database_kind: str = DATABASE_KIND_HUB_V2,
    target_schema_version: int | None = None,
    marker_path: str | Path | None = None,
    expected_generation: str = "",
    expected_deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Validate a marker, its backup bundle, and the still-current source."""

    kind = _database_kind(database_kind)
    timeout = _busy_timeout(busy_timeout_ms)
    target = _target_schema_version(kind, target_schema_version)
    source = Path(source_path).expanduser().resolve(strict=False)
    marker = (
        Path(marker_path).expanduser().resolve(strict=False)
        if marker_path is not None
        else pre_migration_backup_marker_path(source)
    )
    errors: list[dict[str, Any]] = []
    marker_payload: dict[str, Any] = {}
    backup_validation: dict[str, Any] = {}
    source_snapshot: dict[str, Any] = {}

    if not source.is_file():
        errors.append({"code": "pre_migration_source_missing", "path": str(source)})
    if not marker.is_file():
        errors.append({"code": "pre_migration_marker_missing", "path": str(marker)})
    else:
        _validate_private_file(marker, "pre_migration_marker", errors)
        try:
            marker_payload = _read_private_json(
                marker, label="pre-migration backup marker"
            )
        except BackupV2ValidationError as error:
            errors.append(
                {"code": "pre_migration_marker_invalid", "message": str(error)}
            )

    backup = Path(".")
    if marker_payload:
        errors.extend(_validate_pre_migration_marker_shape(marker_payload))
        if str(marker_payload.get("marker_sha256") or "") != _marker_checksum(
            marker_payload
        ):
            errors.append({"code": "pre_migration_marker_checksum_mismatch"})
        if str(marker_payload.get("database_kind") or "") != kind:
            errors.append(
                {
                    "code": "pre_migration_marker_kind_mismatch",
                    "expected": kind,
                    "actual": str(marker_payload.get("database_kind") or ""),
                }
            )
        if str(marker_payload.get("source_path") or "") != str(source):
            errors.append({"code": "pre_migration_marker_source_mismatch"})
        if marker_payload.get("target_schema_version") != target:
            errors.append(
                {
                    "code": "pre_migration_marker_target_schema_mismatch",
                    "expected": target,
                    "actual": marker_payload.get("target_schema_version"),
                }
            )
        backup_text = str(marker_payload.get("backup_path") or "")
        if backup_text:
            backup = Path(backup_text).expanduser().resolve(strict=False)
            expected_manifest = v2_backup_manifest_path(backup).resolve(strict=False)
            if str(marker_payload.get("backup_manifest_path") or "") != str(
                expected_manifest
            ):
                errors.append({"code": "pre_migration_marker_manifest_path_mismatch"})
            if expected_manifest.is_file():
                try:
                    actual_manifest_checksum = _sha256_file(expected_manifest)
                except BackupV2Error as error:
                    errors.append(
                        {
                            "code": "pre_migration_manifest_unreadable",
                            "message": str(error),
                        }
                    )
                else:
                    if (
                        marker_payload.get("backup_manifest_sha256")
                        != actual_manifest_checksum
                    ):
                        errors.append(
                            {"code": "pre_migration_manifest_checksum_mismatch"}
                        )
            backup_validation = validate_v2_backup(
                backup,
                expected_kind=kind,
                expected_generation=(
                    expected_generation
                    or str(marker_payload.get("database_generation") or "")
                ),
                expected_deployed_revision=expected_deployed_revision,
                busy_timeout_ms=timeout,
            )
            if not backup_validation["valid"]:
                errors.append({"code": "pre_migration_backup_invalid"})
        else:
            errors.append({"code": "pre_migration_marker_backup_path_invalid"})

    if source.is_file():
        try:
            source_snapshot = _inspect_database(
                source,
                expected_kind=kind,
                timeout=timeout,
                allow_supported_older=True,
            )
        except BackupV2Error as error:
            errors.append(
                {"code": "pre_migration_source_invalid", "message": str(error)}
            )
        else:
            if marker_payload:
                if str(marker_payload.get("database_generation") or "") != str(
                    source_snapshot["database"]["generation"]
                ):
                    errors.append({"code": "pre_migration_source_generation_mismatch"})
                if (
                    marker_payload.get("source_schema_version")
                    != source_snapshot["database"]["schema_version"]
                ):
                    errors.append({"code": "pre_migration_source_schema_mismatch"})
                if marker_payload.get("source_state_sha256") != _state_proof_checksum(
                    source_snapshot["state_proof"]
                ):
                    errors.append({"code": "pre_migration_source_state_mismatch"})
            if backup_validation:
                errors.extend(
                    _source_backup_binding_errors(
                        source, source_snapshot, backup_validation
                    )
                )

    return {
        "status": "ok" if not errors else "failed",
        "valid": not errors,
        "required": True,
        "source_path": str(source),
        "backup_path": str(backup) if marker_payload else "",
        "marker_path": str(marker),
        "source_schema_version": source_snapshot.get("database", {}).get(
            "schema_version"
        ),
        "target_schema_version": target,
        "database_generation": source_snapshot.get("database", {}).get(
            "generation", ""
        ),
        "marker": marker_payload,
        "backup_validation": backup_validation,
        "errors": errors,
    }


def require_pre_migration_validated_backup(
    source_path: str | Path,
    *,
    database_kind: str = DATABASE_KIND_HUB_V2,
    target_schema_version: int | None = None,
    marker_path: str | Path | None = None,
    expected_generation: str = "",
    expected_deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Fail closed unless an existing older database has a current backup marker."""

    kind = _database_kind(database_kind)
    timeout = _busy_timeout(busy_timeout_ms)
    target = _target_schema_version(kind, target_schema_version)
    source = Path(source_path).expanduser().resolve(strict=False)
    if not source.exists():
        return {
            "status": "not_required",
            "valid": True,
            "required": False,
            "reason": "database_does_not_exist",
            "source_path": str(source),
            "target_schema_version": target,
        }
    try:
        snapshot = _inspect_database(
            source,
            expected_kind=kind,
            timeout=timeout,
            allow_supported_older=True,
        )
    except BackupV2Error as error:
        report = {
            "status": "failed",
            "valid": False,
            "required": True,
            "source_path": str(source),
            "target_schema_version": target,
            "errors": [{"code": "pre_migration_source_invalid", "message": str(error)}],
        }
        raise BackupV2ValidationError(
            "cannot assess pre-migration backup requirement",
            report=report,
        ) from error
    source_schema = int(snapshot["database"]["schema_version"])
    if source_schema >= target:
        return {
            "status": "not_required",
            "valid": True,
            "required": False,
            "reason": "schema_is_current",
            "source_path": str(source),
            "source_schema_version": source_schema,
            "target_schema_version": target,
        }
    report = validate_pre_migration_backup_marker(
        source,
        database_kind=kind,
        target_schema_version=target,
        marker_path=marker_path,
        expected_generation=expected_generation,
        expected_deployed_revision=expected_deployed_revision,
        busy_timeout_ms=timeout,
    )
    if not report["valid"]:
        raise BackupV2ValidationError(
            "validated pre-migration backup marker is required",
            report=report,
        )
    return report


def create_hub_v2_backup(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    expected_generation: str = "",
    deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
    admission_freeze: AdmissionFreezeGate | None = None,
    drain_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Create one verified Hub V2 SQLite snapshot without touching live state."""

    return create_v2_sqlite_backup(
        source_path,
        backup_path,
        database_kind=DATABASE_KIND_HUB_V2,
        expected_generation=expected_generation,
        deployed_revision=deployed_revision,
        busy_timeout_ms=busy_timeout_ms,
        admission_freeze=admission_freeze,
        drain_timeout_seconds=drain_timeout_seconds,
    )


def create_edge_v2_backup(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    expected_generation: str = "",
    deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Create one verified Edge journal SQLite snapshot without touching it."""

    return create_v2_sqlite_backup(
        source_path,
        backup_path,
        database_kind=DATABASE_KIND_EDGE_V2,
        expected_generation=expected_generation,
        deployed_revision=deployed_revision,
        busy_timeout_ms=busy_timeout_ms,
    )


def create_v2_sqlite_backup(
    source_path: str | Path,
    backup_path: str | Path,
    *,
    database_kind: str,
    expected_generation: str = "",
    deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
    admission_freeze: AdmissionFreezeGate | None = None,
    drain_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Snapshot a live Hub V2 or Edge SQLite database through SQLite's API.

    The containing directory is created/hardened to ``0700`` and both output
    files to ``0600``. An exact complete bundle is reusable, and an exact
    database-only crash orphan can receive its missing manifest. Conflicting or
    invalid artifacts are preserved and diagnosed. A supplied admission gate
    is optional because this module must also support offline Edge backup, but
    when supplied it blocks new Hub dispatches and waits for short admission
    sections to drain first.
    """

    kind = _database_kind(database_kind)
    timeout = _busy_timeout(busy_timeout_ms)
    source = _existing_database_path(source_path, label="source database")
    backup = _output_path(backup_path, label="backup database")
    if backup == source:
        raise BackupV2Error("backup database path must differ from the source database")
    manifest_path = v2_backup_manifest_path(backup)
    source_before: dict[str, Any] = {}
    lease: AdmissionFreezeLease | None = None
    temporary_path: Path | None = None
    database_publication = "created"
    manifest_publication = "created"
    try:
        if admission_freeze is not None:
            lease = admission_freeze.freeze_admissions(reason=f"backup:{kind}")
            if not lease.wait_for_drain(drain_timeout_seconds):
                raise BackupV2Error(
                    "Hub mutation admission did not drain before backup"
                )

        source_before = _file_metadata(source)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{backup.name}.", suffix=".tmp", dir=backup.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        _chmod_private_file(temporary_path)

        source_connection = _open_readonly(source, timeout)
        destination_connection: sqlite3.Connection | None = None
        try:
            destination_connection = sqlite3.connect(
                temporary_path, timeout=timeout / 1_000, isolation_level=None
            )
            destination_connection.execute(f"PRAGMA busy_timeout={timeout}")
            destination_connection.execute("PRAGMA synchronous=FULL")
            source_connection.backup(destination_connection, pages=256, sleep=0.05)
            integrity = _integrity_check(destination_connection)
            if integrity != ["ok"]:
                raise BackupV2Error(
                    f"SQLite backup integrity_check failed: {integrity!r}"
                )
        except sqlite3.Error as error:
            raise BackupV2Error(f"SQLite backup failed: {error}") from error
        finally:
            if destination_connection is not None:
                destination_connection.close()
            source_connection.close()

        _chmod_private_file(temporary_path)
        snapshot = _inspect_database(
            temporary_path,
            expected_kind=kind,
            timeout=timeout,
            allow_supported_older=True,
        )
        generation = snapshot["database"]["generation"]
        _require_generation(generation, expected_generation)
        try:
            _publish_exclusive(temporary_path, backup)
            temporary_path = None
        except BackupV2RestoreError:
            if not backup.is_file():
                raise
            _require_matching_database_artifact(
                backup,
                expected_snapshot=snapshot,
                expected_kind=kind,
                timeout=timeout,
                label="existing backup database",
            )
            database_publication = "reused"

        manifest = {
            "manifest_version": V2_BACKUP_MANIFEST_VERSION,
            "created_at": time.time(),
            "database": snapshot["database"],
            "source": source_before,
            "backup": {
                "sha256": _sha256_file(backup),
                "size_bytes": int(backup.stat().st_size),
            },
            "deployed_contract": _deployed_contract_metadata(deployed_revision),
            "state_proof": snapshot["state_proof"],
        }
        manifest["manifest_sha256"] = _manifest_checksum(manifest)
        try:
            _write_private_json_exclusive(manifest_path, manifest)
        except BackupV2Error:
            if not manifest_path.is_file():
                raise
            report = _require_matching_backup_bundle(
                backup,
                expected_manifest=manifest,
                expected_kind=kind,
                expected_generation=expected_generation,
                timeout=timeout,
            )
            manifest_publication = "reused"
        else:
            report = validate_v2_backup(
                backup,
                expected_kind=kind,
                expected_generation=expected_generation,
            )
        if not report["valid"]:
            raise BackupV2ValidationError(
                "new SQLite backup did not validate", report=report
            )
        source_after = _file_metadata(source)
        created = database_publication == manifest_publication == "created"
        reused = database_publication == manifest_publication == "reused"
        return {
            "status": "created" if created else "reused" if reused else "recovered",
            "created": created,
            "reused": reused,
            "recovered_orphan": not created and not reused,
            "publication": {
                "database": database_publication,
                "manifest": manifest_publication,
            },
            "valid": True,
            "backup_path": str(backup),
            "manifest_path": str(manifest_path),
            "database_kind": kind,
            "database_generation": generation,
            "state_proof": snapshot["state_proof"],
            "source": source_before,
            "source_unchanged": source_before == source_after,
            "validation": report,
        }
    finally:
        if temporary_path is not None:
            _unlink_if_exists(temporary_path)
        if lease is not None:
            lease.release()


def validate_v2_backup(
    backup_path: str | Path,
    *,
    expected_kind: str = "",
    expected_generation: str = "",
    expected_deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Validate the immutable backup bundle without mutating its database."""

    timeout = _busy_timeout(busy_timeout_ms)
    backup = Path(backup_path).expanduser().resolve(strict=False)
    manifest_path = v2_backup_manifest_path(backup)
    errors: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {}
    snapshot: dict[str, Any] = {}

    if not backup.is_file():
        errors.append({"code": "backup_missing", "path": str(backup)})
    else:
        _validate_private_file(backup, "backup", errors)
        _validate_private_directory(backup.parent, errors)
    if not manifest_path.is_file():
        errors.append({"code": "backup_manifest_missing", "path": str(manifest_path)})
    else:
        _validate_private_file(manifest_path, "manifest", errors)
        try:
            manifest = _read_manifest(manifest_path)
        except BackupV2ValidationError as error:
            errors.append({"code": "backup_manifest_invalid", "message": str(error)})

    if manifest:
        errors.extend(_validate_manifest_shape(manifest))
        expected_manifest_checksum = str(manifest.get("manifest_sha256") or "")
        if expected_manifest_checksum != _manifest_checksum(manifest):
            errors.append({"code": "backup_manifest_checksum_mismatch"})
        database = manifest.get("database")
        if isinstance(database, Mapping):
            manifest_kind = str(database.get("kind") or "")
            if expected_kind and manifest_kind != _database_kind(expected_kind):
                errors.append(
                    {
                        "code": "database_kind_mismatch",
                        "expected": _database_kind(expected_kind),
                        "actual": manifest_kind,
                    }
                )
            manifest_generation = str(database.get("generation") or "")
            if expected_generation and manifest_generation != _generation_value(
                expected_generation
            ):
                errors.append(
                    {
                        "code": "database_generation_mismatch",
                        "expected": _generation_value(expected_generation),
                        "actual": manifest_generation,
                    }
                )
        deployed = manifest.get("deployed_contract")
        if expected_deployed_revision and isinstance(deployed, Mapping):
            actual_revision = str(deployed.get("deployed_revision") or "")
            if actual_revision != str(expected_deployed_revision).strip():
                errors.append(
                    {
                        "code": "deployed_revision_mismatch",
                        "expected": str(expected_deployed_revision).strip(),
                        "actual": actual_revision,
                    }
                )

    if backup.is_file():
        try:
            actual_checksum = _sha256_file(backup)
            actual_size = int(backup.stat().st_size)
        except OSError as error:
            errors.append({"code": "backup_unreadable", "message": str(error)})
            actual_checksum = ""
            actual_size = 0
        backup_metadata = (
            manifest.get("backup")
            if isinstance(manifest.get("backup"), Mapping)
            else {}
        )
        if backup_metadata:
            if backup_metadata.get("sha256") != actual_checksum:
                errors.append({"code": "backup_checksum_mismatch"})
            if backup_metadata.get("size_bytes") != actual_size:
                errors.append({"code": "backup_size_mismatch"})
        try:
            manifest_database = (
                manifest.get("database")
                if isinstance(manifest.get("database"), Mapping)
                else {}
            )
            manifest_schema_version = manifest_database.get("schema_version")
            snapshot = _inspect_database(
                backup,
                expected_kind=str(expected_kind or _manifest_kind(manifest)),
                timeout=timeout,
                expected_schema_version=(
                    int(manifest_schema_version)
                    if isinstance(manifest_schema_version, int)
                    else None
                ),
                allow_supported_older=True,
            )
        except (BackupV2Error, ValueError) as error:
            errors.append({"code": "backup_database_invalid", "message": str(error)})
        else:
            integrity = snapshot["database"]["integrity_check"]
            if integrity != ["ok"]:
                errors.append(
                    {"code": "backup_integrity_check_failed", "actual": integrity}
                )
            if isinstance(manifest.get("database"), Mapping):
                if dict(manifest["database"]) != snapshot["database"]:
                    errors.append({"code": "backup_database_metadata_mismatch"})
            if isinstance(manifest.get("state_proof"), Mapping):
                if dict(manifest["state_proof"]) != snapshot["state_proof"]:
                    errors.append({"code": "backup_state_proof_mismatch"})

    return {
        "status": "ok" if not errors else "failed",
        "valid": not errors,
        "backup_path": str(backup),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "database": snapshot.get("database", {}),
        "state_proof": snapshot.get("state_proof", {}),
        "errors": errors,
    }


def restore_hub_v2_backup(
    backup_path: str | Path,
    restore_path: str | Path,
    *,
    expected_generation: str = "",
    expected_deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Restore a Hub V2 snapshot to a fresh or exactly matching crash artifact."""

    return restore_v2_sqlite_backup(
        backup_path,
        restore_path,
        expected_kind=DATABASE_KIND_HUB_V2,
        expected_generation=expected_generation,
        expected_deployed_revision=expected_deployed_revision,
        busy_timeout_ms=busy_timeout_ms,
    )


def restore_edge_v2_backup(
    backup_path: str | Path,
    restore_path: str | Path,
    *,
    expected_generation: str = "",
    expected_deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Restore an Edge snapshot to a fresh or exactly matching crash artifact."""

    return restore_v2_sqlite_backup(
        backup_path,
        restore_path,
        expected_kind=DATABASE_KIND_EDGE_V2,
        expected_generation=expected_generation,
        expected_deployed_revision=expected_deployed_revision,
        busy_timeout_ms=busy_timeout_ms,
    )


def restore_v2_sqlite_backup(
    backup_path: str | Path,
    restore_path: str | Path,
    *,
    expected_kind: str,
    expected_generation: str = "",
    expected_deployed_revision: str = "",
    busy_timeout_ms: int = DEFAULT_BACKUP_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Restore a validated snapshot into a fresh or exactly matching private file."""

    kind = _database_kind(expected_kind)
    timeout = _busy_timeout(busy_timeout_ms)
    validation = validate_v2_backup(
        backup_path,
        expected_kind=kind,
        expected_generation=expected_generation,
        expected_deployed_revision=expected_deployed_revision,
        busy_timeout_ms=timeout,
    )
    if not validation["valid"]:
        raise BackupV2ValidationError(
            "backup validation failed before restore", report=validation
        )

    backup = _existing_database_path(backup_path, label="backup database")
    restore = _output_path(restore_path, label="restore database")
    if restore == backup:
        raise BackupV2RestoreError(
            "restore database path must differ from the backup database"
        )
    temporary_path: Path | None = None
    restore_publication = "created"
    restore_marker_report: dict[str, Any] = {}
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{restore.name}.", suffix=".tmp", dir=restore.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        _chmod_private_file(temporary_path)

        source_connection = _open_readonly(backup, timeout)
        destination_connection: sqlite3.Connection | None = None
        try:
            destination_connection = sqlite3.connect(
                temporary_path, timeout=timeout / 1_000, isolation_level=None
            )
            destination_connection.execute(f"PRAGMA busy_timeout={timeout}")
            destination_connection.execute("PRAGMA synchronous=FULL")
            source_connection.backup(destination_connection, pages=256, sleep=0.05)
            integrity = _integrity_check(destination_connection)
            if integrity != ["ok"]:
                raise BackupV2RestoreError(
                    f"restored SQLite integrity_check failed: {integrity!r}"
                )
        except sqlite3.Error as error:
            raise BackupV2RestoreError(f"SQLite restore failed: {error}") from error
        finally:
            if destination_connection is not None:
                destination_connection.close()
            source_connection.close()

        _chmod_private_file(temporary_path)
        manifest = validation["manifest"]
        expected_snapshot = {
            "database": dict(manifest["database"]),
            "state_proof": dict(manifest["state_proof"]),
        }
        try:
            _publish_exclusive(temporary_path, restore)
            temporary_path = None
        except BackupV2RestoreError:
            if not restore.is_file():
                raise
            _require_matching_database_artifact(
                restore,
                expected_snapshot=expected_snapshot,
                expected_kind=kind,
                timeout=timeout,
                label="existing restore database",
            )
            restore_publication = "reused"
        restored_snapshot = _inspect_database(
            restore,
            expected_kind=kind,
            timeout=timeout,
            expected_schema_version=int(manifest["database"]["schema_version"]),
            allow_supported_older=True,
        )
        if restored_snapshot["database"] != manifest["database"]:
            raise BackupV2RestoreError(
                "restored database metadata differs from backup manifest"
            )
        if restored_snapshot["state_proof"] != manifest["state_proof"]:
            raise BackupV2RestoreError("restored durable state differs from backup")
        current_schema_version = (
            HUB_STORE_SCHEMA_VERSION
            if kind == DATABASE_KIND_HUB_V2
            else EDGE_JOURNAL_SCHEMA_VERSION
        )
        if int(manifest["database"]["schema_version"]) < current_schema_version:
            restore_marker_report = create_pre_migration_backup_marker(
                restore,
                backup,
                database_kind=kind,
                target_schema_version=current_schema_version,
                expected_generation=str(manifest["database"]["generation"]),
                expected_deployed_revision=expected_deployed_revision,
                busy_timeout_ms=timeout,
            )
        open_verification = _verify_restored_store(
            restore,
            backup_path=backup,
            database_kind=kind,
            expected_generation=str(manifest["database"]["generation"]),
            expected_schema_version=int(manifest["database"]["schema_version"]),
            expected_state_proof=manifest["state_proof"],
            timeout=timeout,
        )
        return {
            "status": "restored" if restore_publication == "created" else "reused",
            "restored": True,
            "reused": restore_publication == "reused",
            "publication": {"database": restore_publication},
            "backup_path": str(backup),
            "restore_path": str(restore),
            "database_kind": kind,
            "database_generation": str(manifest["database"]["generation"]),
            "state_proof": restored_snapshot["state_proof"],
            "pre_migration_backup_marker": restore_marker_report,
            "open_verification": open_verification,
            "validation": validation,
        }
    finally:
        if temporary_path is not None:
            _unlink_if_exists(temporary_path)


def _verify_restored_store(
    path: Path,
    *,
    backup_path: Path,
    database_kind: str,
    expected_generation: str,
    expected_schema_version: int,
    expected_state_proof: Mapping[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """Open an ephemeral clone through the real wrapper without changing restore output."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.wrapper-check.", suffix=".sqlite3", dir=path.parent
    )
    os.close(descriptor)
    verification_path = Path(temporary_name)
    verification_marker: Path | None = None
    compatibility_verification: dict[str, Any] = {}
    schema_compatible_tables: set[str] = set()
    _chmod_private_file(verification_path)
    try:
        _copy_sqlite_database(path, verification_path, timeout=timeout)
        if database_kind == DATABASE_KIND_HUB_V2:
            try:
                if expected_schema_version < HUB_STORE_SCHEMA_VERSION:
                    marker_report = create_pre_migration_backup_marker(
                        verification_path,
                        backup_path,
                        database_kind=DATABASE_KIND_HUB_V2,
                        target_schema_version=HUB_STORE_SCHEMA_VERSION,
                        busy_timeout_ms=timeout,
                    )
                    verification_marker = Path(marker_report["marker_path"])
                with HubStoreV2(
                    verification_path,
                    busy_timeout_ms=timeout,
                    pre_migration_backup_marker=verification_marker,
                ) as store:
                    schema = store.schema_info()
                    row = store.connection.execute(
                        "SELECT hub_id FROM hub_identity WHERE singleton = 1"
                    ).fetchone()
                    generation = str(row["hub_id"]) if row is not None else ""
            except Exception as error:
                raise BackupV2RestoreError(
                    f"restored HubStoreV2 cannot be opened: {error}"
                ) from error
            wrapper = "HubStoreV2"
        elif database_kind == DATABASE_KIND_EDGE_V2:
            try:
                result_outbox_baseline: dict[str, Any] = {}
                if expected_schema_version < EDGE_JOURNAL_SCHEMA_VERSION:
                    result_outbox_baseline = _edge_result_outbox_upgrade_baseline(
                        verification_path,
                        expected_state_proof=expected_state_proof,
                        timeout=timeout,
                    )
                    marker_report = create_pre_migration_backup_marker(
                        verification_path,
                        backup_path,
                        database_kind=DATABASE_KIND_EDGE_V2,
                        target_schema_version=EDGE_JOURNAL_SCHEMA_VERSION,
                        busy_timeout_ms=timeout,
                    )
                    verification_marker = Path(marker_report["marker_path"])
                with EdgeJournal(
                    verification_path,
                    edge_generation=expected_generation,
                    busy_timeout_ms=timeout,
                    pre_migration_backup_marker=verification_marker,
                ) as journal:
                    schema = journal.schema_info()
                    generation = journal.edge_generation
                if result_outbox_baseline:
                    compatibility_verification = _verify_edge_result_outbox_upgrade(
                        verification_path,
                        baseline=result_outbox_baseline,
                        timeout=timeout,
                    )
                    schema_compatible_tables.add("result_outbox")
            except Exception as error:
                raise BackupV2RestoreError(
                    f"restored EdgeJournal cannot be opened: {error}"
                ) from error
            wrapper = "EdgeJournal"
        else:
            raise BackupV2RestoreError(
                f"unknown restored database kind: {database_kind!r}"
            )

        if generation != expected_generation:
            raise BackupV2RestoreError(
                "restored database generation mismatch: "
                f"expected {expected_generation!r}, got {generation!r}"
            )
        snapshot = _inspect_database(
            verification_path,
            expected_kind=database_kind,
            timeout=timeout,
        )
        verified_tables = _verify_wrapper_preserved_authoritative_state(
            database_kind=database_kind,
            source_schema_version=expected_schema_version,
            expected_state_proof=expected_state_proof,
            actual_state_proof=snapshot["state_proof"],
            schema_compatible_tables=schema_compatible_tables,
        )
        return {
            "wrapper": wrapper,
            "schema_version": int(schema["schema_version"]),
            "database_generation": generation,
            "verification_copy": "ephemeral",
            "restore_output_unchanged": True,
            "authoritative_tables_verified": verified_tables,
            "compatibility_verification": compatibility_verification,
        }
    finally:
        _unlink_if_exists(verification_path)
        _unlink_if_exists(Path(f"{verification_path}-wal"))
        _unlink_if_exists(Path(f"{verification_path}-shm"))
        if verification_marker is not None:
            _unlink_if_exists(verification_marker)


def _copy_sqlite_database(source: Path, destination: Path, *, timeout: int) -> None:
    source_connection = _open_readonly(source, timeout)
    destination_connection: sqlite3.Connection | None = None
    try:
        destination_connection = sqlite3.connect(
            destination,
            timeout=timeout / 1_000,
            isolation_level=None,
        )
        destination_connection.execute(f"PRAGMA busy_timeout={timeout}")
        destination_connection.execute("PRAGMA synchronous=FULL")
        source_connection.backup(destination_connection, pages=256, sleep=0.05)
        integrity = _integrity_check(destination_connection)
        if integrity != ["ok"]:
            raise BackupV2RestoreError(
                f"wrapper verification copy integrity_check failed: {integrity!r}"
            )
    except sqlite3.Error as error:
        raise BackupV2RestoreError(
            f"cannot create wrapper verification copy: {error}"
        ) from error
    finally:
        if destination_connection is not None:
            destination_connection.close()
        source_connection.close()


def _edge_result_outbox_upgrade_baseline(
    path: Path,
    *,
    expected_state_proof: Mapping[str, Any],
    timeout: int,
) -> dict[str, Any]:
    expected_tables = expected_state_proof.get("tables")
    expected_proof = (
        expected_tables.get("result_outbox")
        if isinstance(expected_tables, Mapping)
        else None
    )
    if not isinstance(expected_proof, Mapping):
        raise BackupV2RestoreError("older Edge restore proof is missing result_outbox")
    connection = _open_readonly(path, timeout)
    try:
        columns = _table_column_contract(connection, "result_outbox")
        actual_proof = _proof_for_table(connection, "result_outbox")
    finally:
        connection.close()
    if dict(actual_proof) != dict(expected_proof):
        raise BackupV2RestoreError(
            "older Edge restore result_outbox baseline differs from backup proof"
        )
    return {"columns": columns, "proof": dict(expected_proof)}


def _verify_edge_result_outbox_upgrade(
    path: Path,
    *,
    baseline: Mapping[str, Any],
    timeout: int,
) -> dict[str, Any]:
    expected_columns = baseline.get("columns")
    expected_proof = baseline.get("proof")
    if not isinstance(expected_columns, list) or not isinstance(
        expected_proof, Mapping
    ):
        raise BackupV2RestoreError("older Edge result_outbox baseline is incomplete")

    connection = _open_readonly(path, timeout)
    try:
        actual_columns = _table_column_contract(connection, "result_outbox")
        old_names = [str(column["name"]) for column in expected_columns]
        if actual_columns[:-1] != expected_columns:
            raise BackupV2RestoreError(
                "Edge migration changed a pre-existing result_outbox column"
            )
        added_columns = actual_columns[len(expected_columns) :]
        expected_added = {
            "name": "hub_confirmed_at",
            "type": "REAL",
            "notnull": 0,
            "default": None,
            "primary_key": 0,
            "hidden": 0,
        }
        if added_columns != [expected_added]:
            raise BackupV2RestoreError(
                "Edge migration added an unexpected result_outbox column or default"
            )
        projected_proof = _proof_for_table_columns(
            connection,
            "result_outbox",
            old_names,
        )
        if dict(projected_proof) != dict(expected_proof):
            raise BackupV2RestoreError(
                "Edge migration changed pre-existing result_outbox row content"
            )
        nondefault_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM result_outbox WHERE hub_confirmed_at IS NOT NULL"
            ).fetchone()[0]
        )
        if nondefault_count:
            raise BackupV2RestoreError(
                "Edge migration changed the default for existing result_outbox rows"
            )
    finally:
        connection.close()
    return {
        "table": "result_outbox",
        "preexisting_columns_verified": old_names,
        "preexisting_column_count": len(old_names),
        "preexisting_rows": int(expected_proof["count"]),
        "preexisting_state_sha256": str(expected_proof["sha256"]),
        "added_columns": ["hub_confirmed_at"],
        "added_column_default": None,
    }


def _table_column_contract(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"PRAGMA table_xinfo({_quote_identifier(table_name)})"
    ).fetchall()
    return [
        {
            "name": str(row[1]),
            "type": str(row[2]),
            "notnull": int(row[3]),
            "default": row[4],
            "primary_key": int(row[5]),
            "hidden": int(row[6]),
        }
        for row in rows
    ]


def _verify_wrapper_preserved_authoritative_state(
    *,
    database_kind: str,
    source_schema_version: int,
    expected_state_proof: Mapping[str, Any],
    actual_state_proof: Mapping[str, Any],
    schema_compatible_tables: set[str] | None = None,
) -> list[str]:
    expected_tables = expected_state_proof.get("tables")
    actual_tables = actual_state_proof.get("tables")
    if not isinstance(expected_tables, Mapping) or not isinstance(
        actual_tables, Mapping
    ):
        raise BackupV2RestoreError("wrapper verification state proof is incomplete")
    table_names = (
        _HUB_AUTHORITATIVE_TABLES
        if database_kind == DATABASE_KIND_HUB_V2
        else _EDGE_AUTHORITATIVE_TABLES
    )
    verified: list[str] = []
    for table_name in sorted(table_names):
        expected = expected_tables.get(table_name)
        actual = actual_tables.get(table_name)
        if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
            raise BackupV2RestoreError(
                f"wrapper verification lost authoritative table {table_name!r}"
            )
        if int(expected.get("count", -1)) != int(actual.get("count", -2)):
            raise BackupV2RestoreError(
                f"wrapper verification changed row count for {table_name!r}"
            )
        schema_alters_rows = table_name in (schema_compatible_tables or set())
        if schema_alters_rows and not (
            database_kind == DATABASE_KIND_EDGE_V2
            and table_name == "result_outbox"
            and source_schema_version < EDGE_JOURNAL_SCHEMA_VERSION
        ):
            raise BackupV2RestoreError(
                f"unsupported wrapper compatibility exception for {table_name!r}"
            )
        if not schema_alters_rows and dict(expected) != dict(actual):
            raise BackupV2RestoreError(
                f"wrapper verification changed authoritative table {table_name!r}"
            )
        verified.append(table_name)
    return verified


def _inspect_database(
    path: Path,
    *,
    expected_kind: str,
    timeout: int,
    expected_schema_version: int | None = None,
    allow_supported_older: bool = False,
) -> dict[str, Any]:
    kind = _database_kind(expected_kind)
    connection = _open_readonly(path, timeout)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = (
            _HUB_REQUIRED_TABLES
            if kind == DATABASE_KIND_HUB_V2
            else _EDGE_REQUIRED_TABLES
        )
        missing = sorted(required - tables)
        if missing:
            raise BackupV2Error(
                f"SQLite database is not {kind}: missing required tables {missing!r}"
            )
        integrity = _integrity_check(connection)
        if kind == DATABASE_KIND_HUB_V2:
            snapshot = _inspect_hub_database(
                connection,
                expected_schema_version=expected_schema_version,
                allow_supported_older=allow_supported_older,
            )
        else:
            snapshot = _inspect_edge_database(
                connection,
                expected_schema_version=expected_schema_version,
                allow_supported_older=allow_supported_older,
            )
        snapshot["database"]["integrity_check"] = integrity
        return snapshot
    except sqlite3.Error as error:
        raise BackupV2Error(
            f"cannot inspect SQLite database {path}: {error}"
        ) from error
    finally:
        connection.close()


def _inspect_hub_database(
    connection: sqlite3.Connection,
    *,
    expected_schema_version: int | None = None,
    allow_supported_older: bool = False,
) -> dict[str, Any]:
    metadata = connection.execute(
        "SELECT schema_version, migration_lock FROM schema_metadata WHERE singleton = 1"
    ).fetchone()
    identity = connection.execute(
        "SELECT hub_id FROM hub_identity WHERE singleton = 1"
    ).fetchone()
    if metadata is None or identity is None:
        raise BackupV2Error("Hub V2 schema metadata or hub identity is missing")
    schema_version = int(metadata["schema_version"])
    _require_schema_version(
        schema_version,
        current=HUB_STORE_SCHEMA_VERSION,
        expected=expected_schema_version,
        allow_supported_older=allow_supported_older,
        label="Hub V2",
    )
    if metadata["migration_lock"]:
        raise BackupV2Error("Hub V2 database has an active migration lock")
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version != schema_version:
        raise BackupV2Error(
            "Hub V2 schema metadata and PRAGMA user_version disagree: "
            f"{schema_version} != {user_version}"
        )
    _require_version_tables(
        connection,
        schema_version=schema_version,
        base_tables=_HUB_REQUIRED_TABLES,
        version_tables=_HUB_SCHEMA_TABLES,
        label="Hub V2",
    )
    generation = str(identity["hub_id"] or "").strip()
    if not generation:
        raise BackupV2Error("Hub V2 hub identity is empty")
    state_proof = _complete_state_proof(
        connection,
        database_kind=DATABASE_KIND_HUB_V2,
    )
    return {
        "database": {
            "kind": DATABASE_KIND_HUB_V2,
            "generation": generation,
            "schema_version": schema_version,
            "user_version": user_version,
        },
        "state_proof": state_proof,
    }


def _inspect_edge_database(
    connection: sqlite3.Connection,
    *,
    expected_schema_version: int | None = None,
    allow_supported_older: bool = False,
) -> dict[str, Any]:
    metadata = connection.execute(
        "SELECT schema_version, migration_lock FROM schema_metadata WHERE singleton = 1"
    ).fetchone()
    state = connection.execute(
        "SELECT edge_generation FROM edge_state WHERE singleton = 1"
    ).fetchone()
    if metadata is None or state is None:
        raise BackupV2Error("Edge journal schema metadata or generation is missing")
    schema_version = int(metadata["schema_version"])
    _require_schema_version(
        schema_version,
        current=EDGE_JOURNAL_SCHEMA_VERSION,
        expected=expected_schema_version,
        allow_supported_older=allow_supported_older,
        label="Edge journal",
    )
    if metadata["migration_lock"]:
        raise BackupV2Error("Edge journal has an active migration lock")
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version != schema_version:
        raise BackupV2Error(
            "Edge journal schema metadata and PRAGMA user_version disagree: "
            f"{schema_version} != {user_version}"
        )
    _require_version_tables(
        connection,
        schema_version=schema_version,
        base_tables=_EDGE_REQUIRED_TABLES,
        version_tables=_EDGE_SCHEMA_TABLES,
        label="Edge journal",
    )
    generation = str(state["edge_generation"] or "").strip()
    if not generation:
        raise BackupV2Error("Edge journal generation is empty")
    return {
        "database": {
            "kind": DATABASE_KIND_EDGE_V2,
            "generation": generation,
            "schema_version": schema_version,
            "user_version": user_version,
        },
        "state_proof": _complete_state_proof(
            connection,
            database_kind=DATABASE_KIND_EDGE_V2,
        ),
    }


def _require_version_tables(
    connection: sqlite3.Connection,
    *,
    schema_version: int,
    base_tables: frozenset[str],
    version_tables: Mapping[int, frozenset[str]],
    label: str,
) -> None:
    actual = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required = set(base_tables)
    for introduced, names in version_tables.items():
        if schema_version >= introduced:
            required.update(names)
    missing = sorted(required - actual)
    if missing:
        raise BackupV2Error(
            f"{label} schema {schema_version} is missing required tables {missing!r}"
        )


def _complete_state_proof(
    connection: sqlite3.Connection,
    *,
    database_kind: str,
) -> dict[str, Any]:
    table_names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    ]
    table_proofs = {
        table_name: _proof_for_table(connection, table_name)
        for table_name in table_names
    }
    schema_proof = _schema_proof(connection)
    aggregate = _proof_for_values([{"schema": schema_proof, "tables": table_proofs}])
    aggregate["count"] = sum(int(proof["count"]) for proof in table_proofs.values())
    proof: dict[str, Any] = {
        "scope": _STATE_PROOF_SCOPE,
        "database": aggregate,
        "schema": schema_proof,
        "tables": table_proofs,
    }
    if database_kind == DATABASE_KIND_HUB_V2:
        proof["identity"] = dict(table_proofs["hub_identity"])
        entity_types: dict[str, dict[str, Any]] = {}
        rows = connection.execute(
            "SELECT DISTINCT entity_type FROM entity_records ORDER BY entity_type"
        ).fetchall()
        for row in rows:
            entity_type = str(row[0])
            cursor = connection.execute(
                """
                SELECT * FROM entity_records
                WHERE entity_type = ?
                ORDER BY entity_type, entity_id
                """,
                (entity_type,),
            )
            entity_types[entity_type] = _proof_for_cursor(cursor)
        proof["entity_types"] = entity_types
        proof.update(
            {
                "groups": _proof_for_cursor(
                    connection.execute(
                        """
                        SELECT * FROM entity_records
                        WHERE entity_type IN ('hub.work_group', 'legacy.work_group')
                        ORDER BY entity_type, entity_id
                        """
                    )
                ),
                "operations": dict(table_proofs["operations"]),
                "receipts": dict(
                    entity_types.get("hub.edge_receipt") or _proof_for_values([])
                ),
                "attempts": dict(table_proofs["attempts"]),
            }
        )
    else:
        group_values: set[str] = set()
        for row in connection.execute(
            "SELECT correlation_json FROM operation_intents ORDER BY operation_id"
        ):
            try:
                correlation = json.loads(str(row[0]))
            except (TypeError, json.JSONDecodeError) as error:
                raise BackupV2Error(
                    "Edge journal correlation JSON is invalid"
                ) from error
            work_group_id = str(correlation.get("work_group_id") or "").strip()
            if work_group_id:
                group_values.add(work_group_id)
        proof.update(
            {
                "groups": _proof_for_values(sorted(group_values)),
                "operations": dict(table_proofs["operation_intents"]),
                "attempts": dict(table_proofs["operation_attempts"]),
                "receipts": dict(table_proofs["result_outbox"]),
            }
        )
    return proof


def _schema_proof(connection: sqlite3.Connection) -> dict[str, Any]:
    values: list[Any] = [
        {
            "pragma": "application_id",
            "value": int(connection.execute("PRAGMA application_id").fetchone()[0]),
        },
        {
            "pragma": "encoding",
            "value": str(connection.execute("PRAGMA encoding").fetchone()[0]),
        },
        {
            "pragma": "user_version",
            "value": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        },
    ]
    values.extend(
        {
            "type": str(row["type"]),
            "name": str(row["name"]),
            "table": str(row["tbl_name"]),
            "sql": row["sql"],
        }
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            ORDER BY type, name, tbl_name
            """
        ).fetchall()
    )
    return _proof_for_values(values)


def _proof_for_table(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, Any]:
    quoted_table = _quote_identifier(table_name)
    column_rows = connection.execute(f"PRAGMA table_xinfo({quoted_table})").fetchall()
    columns = [str(row[1]) for row in column_rows if int(row[6]) != 1]
    if not columns:
        raise BackupV2Error(f"SQLite table has no readable columns: {table_name}")
    return _proof_for_table_columns(connection, table_name, columns)


def _proof_for_table_columns(
    connection: sqlite3.Connection,
    table_name: str,
    columns: list[str],
) -> dict[str, Any]:
    if not columns:
        raise BackupV2Error(f"SQLite table has no proof columns: {table_name}")
    quoted_table = _quote_identifier(table_name)
    quoted_columns = [_quote_identifier(column) for column in columns]
    ordering = ", ".join(
        part
        for column in quoted_columns
        for part in (
            f"typeof({column}) COLLATE BINARY",
            f"quote({column}) COLLATE BINARY",
        )
    )
    cursor = connection.execute(
        f"SELECT {', '.join(quoted_columns)} FROM {quoted_table} ORDER BY {ordering}"
    )
    return _proof_for_cursor(cursor)


def _proof_for_cursor(cursor: sqlite3.Cursor) -> dict[str, Any]:
    columns = [str(item[0]) for item in (cursor.description or ())]
    digest = hashlib.sha256()
    _update_stable_hash(digest, {"columns": columns})
    count = 0
    for row in cursor:
        _update_stable_hash(digest, tuple(row))
        count += 1
    return {"count": count, "sha256": digest.hexdigest()}


def _require_schema_version(
    actual: int,
    *,
    current: int,
    expected: int | None,
    allow_supported_older: bool,
    label: str,
) -> None:
    if expected is not None and actual != expected:
        raise BackupV2Error(
            f"{label} schema version mismatch: expected {expected}, got {actual}"
        )
    if allow_supported_older and 1 <= actual <= current:
        return
    if actual != current:
        raise BackupV2Error(
            f"{label} schema version mismatch: expected {current}, got {actual}"
        )


def _proof_for_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    values = [dict(row) for row in rows]
    return _proof_for_values(values)


def _proof_for_values(values: list[Any]) -> dict[str, Any]:
    digest = hashlib.sha256()
    for value in values:
        _update_stable_hash(digest, value)
    return {"count": len(values), "sha256": digest.hexdigest()}


def _update_stable_hash(digest: Any, value: Any) -> None:
    if value is None:
        _update_hash_token(digest, b"null", b"")
    elif isinstance(value, bool):
        _update_hash_token(digest, b"bool", b"1" if value else b"0")
    elif isinstance(value, int):
        _update_hash_token(digest, b"integer", str(value).encode("ascii"))
    elif isinstance(value, float):
        _update_hash_token(digest, b"real", value.hex().encode("ascii"))
    elif isinstance(value, str):
        _update_hash_token(digest, b"text", value.encode("utf-8"))
    elif isinstance(value, (bytes, bytearray, memoryview)):
        _update_hash_token(digest, b"blob", bytes(value))
    elif isinstance(value, Mapping):
        _update_hash_token(digest, b"mapping", str(len(value)).encode("ascii"))
        for key in sorted(value, key=lambda item: str(item)):
            _update_stable_hash(digest, str(key))
            _update_stable_hash(digest, value[key])
    elif isinstance(value, (list, tuple)):
        _update_hash_token(digest, b"sequence", str(len(value)).encode("ascii"))
        for item in value:
            _update_stable_hash(digest, item)
    else:
        raise BackupV2Error(
            f"unsupported SQLite proof value type: {type(value).__name__}"
        )


def _update_hash_token(digest: Any, tag: bytes, payload: bytes) -> None:
    digest.update(len(tag).to_bytes(2, "big"))
    digest.update(tag)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _integrity_check(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()
    ]


def _deployed_contract_metadata(deployed_revision: str) -> dict[str, str]:
    return {
        "deployed_revision": str(
            deployed_revision or os.environ.get("PATCHBAY_DEPLOYED_REVISION") or ""
        ).strip(),
        "contract_version": HUB_V2_CONTRACT_VERSION,
        "contract_hash": HUB_V2_CONTRACT_HASH,
        "manifest_hash": HUB_V2_MANIFEST_HASH,
        "schema_hash": HUB_V2_SCHEMA_HASH,
    }


def _validate_manifest_shape(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if set(manifest) != _MANIFEST_FIELDS:
        errors.append(
            {
                "code": "backup_manifest_fields_mismatch",
                "missing": sorted(_MANIFEST_FIELDS - set(manifest)),
                "unexpected": sorted(set(manifest) - _MANIFEST_FIELDS),
            }
        )
    if manifest.get("manifest_version") != V2_BACKUP_MANIFEST_VERSION:
        errors.append({"code": "backup_manifest_version_mismatch"})
    if not _is_number(manifest.get("created_at")):
        errors.append({"code": "backup_manifest_created_at_invalid"})
    for name, fields in (
        ("database", _DATABASE_FIELDS),
        ("source", _SOURCE_FIELDS),
        ("backup", _BACKUP_FIELDS),
        ("deployed_contract", _DEPLOYED_CONTRACT_FIELDS),
    ):
        value = manifest.get(name)
        if not isinstance(value, Mapping) or set(value) != fields:
            errors.append({"code": "backup_manifest_section_invalid", "section": name})
    database = manifest.get("database")
    database_kind = (
        str(database.get("kind") or "") if isinstance(database, Mapping) else ""
    )
    errors.extend(
        _validate_state_proof_shape(manifest.get("state_proof"), database_kind)
    )
    if not _sha256_text(manifest.get("manifest_sha256")):
        errors.append({"code": "backup_manifest_checksum_invalid"})
    return errors


def _validate_state_proof_shape(value: Any, database_kind: str) -> list[dict[str, Any]]:
    error = {"code": "backup_manifest_state_proof_invalid"}
    if not isinstance(value, Mapping):
        return [error]
    expected_fields = (
        _HUB_STATE_PROOF_FIELDS
        if database_kind == DATABASE_KIND_HUB_V2
        else _EDGE_STATE_PROOF_FIELDS
        if database_kind == DATABASE_KIND_EDGE_V2
        else frozenset()
    )
    if not expected_fields or set(value) != expected_fields:
        return [error]
    if value.get("scope") != _STATE_PROOF_SCOPE:
        return [error]
    for name in (
        "database",
        "schema",
        "groups",
        "operations",
        "receipts",
        "attempts",
    ):
        if not _valid_proof(value.get(name)):
            return [error]
    tables = value.get("tables")
    if not isinstance(tables, Mapping) or not tables:
        return [error]
    if any(not str(name) or not _valid_proof(proof) for name, proof in tables.items()):
        return [error]
    if database_kind == DATABASE_KIND_HUB_V2:
        if not _valid_proof(value.get("identity")):
            return [error]
        if dict(value["identity"]) != dict(tables.get("hub_identity") or {}):
            return [error]
        entity_types = value.get("entity_types")
        if not isinstance(entity_types, Mapping):
            return [error]
        if any(
            not str(entity_type) or not _valid_proof(proof)
            for entity_type, proof in entity_types.items()
        ):
            return [error]
    return []


def _valid_proof(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _PROOF_FIELDS
        and isinstance(value.get("count"), int)
        and not isinstance(value.get("count"), bool)
        and int(value["count"]) >= 0
        and _sha256_text(value.get("sha256"))
    )


def _validate_pre_migration_marker_shape(
    marker: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if set(marker) != _PRE_MIGRATION_MARKER_FIELDS:
        return [{"code": "pre_migration_marker_fields_mismatch"}]
    valid = (
        marker.get("marker_version") == PRE_MIGRATION_BACKUP_MARKER_VERSION
        and _is_number(marker.get("created_at"))
        and str(marker.get("database_kind") or "") in DATABASE_KINDS
        and bool(str(marker.get("database_generation") or ""))
        and bool(str(marker.get("source_path") or ""))
        and isinstance(marker.get("source_schema_version"), int)
        and not isinstance(marker.get("source_schema_version"), bool)
        and int(marker["source_schema_version"]) >= 1
        and isinstance(marker.get("target_schema_version"), int)
        and not isinstance(marker.get("target_schema_version"), bool)
        and int(marker["target_schema_version"]) > int(marker["source_schema_version"])
        and _sha256_text(marker.get("source_state_sha256"))
        and bool(str(marker.get("backup_path") or ""))
        and bool(str(marker.get("backup_manifest_path") or ""))
        and _sha256_text(marker.get("backup_manifest_sha256"))
        and _sha256_text(marker.get("marker_sha256"))
    )
    return [] if valid else [{"code": "pre_migration_marker_fields_invalid"}]


def _source_backup_binding_errors(
    source: Path,
    source_snapshot: Mapping[str, Any],
    backup_validation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    del source
    errors: list[dict[str, Any]] = []
    if not backup_validation.get("valid"):
        return [{"code": "pre_migration_backup_invalid"}]
    if dict(backup_validation.get("database") or {}) != dict(
        source_snapshot.get("database") or {}
    ):
        errors.append({"code": "pre_migration_backup_source_database_mismatch"})
    if dict(backup_validation.get("state_proof") or {}) != dict(
        source_snapshot.get("state_proof") or {}
    ):
        errors.append({"code": "pre_migration_backup_source_state_mismatch"})
    return errors


def _target_schema_version(database_kind: str, value: int | None) -> int:
    current = (
        HUB_STORE_SCHEMA_VERSION
        if database_kind == DATABASE_KIND_HUB_V2
        else EDGE_JOURNAL_SCHEMA_VERSION
    )
    target = current if value is None else int(value)
    if target < 1 or target > current:
        raise ValueError(
            f"target_schema_version must be between 1 and {current} for {database_kind}"
        )
    return target


def _state_proof_checksum(state_proof: Mapping[str, Any]) -> str:
    database = state_proof.get("database")
    if not _valid_proof(database):
        raise BackupV2Error("complete state proof database digest is missing")
    return str(database["sha256"])


def _marker_checksum(marker: Mapping[str, Any]) -> str:
    value = {key: item for key, item in marker.items() if key != "marker_sha256"}
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    return _read_private_json(path, label="backup manifest")


def _read_private_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupV2ValidationError(f"{label} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise BackupV2ValidationError(f"{label} root must be an object: {path}")
    return value


def _manifest_checksum(manifest: Mapping[str, Any]) -> str:
    value = {key: item for key, item in manifest.items() if key != "manifest_sha256"}
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _manifest_kind(manifest: Mapping[str, Any]) -> str:
    database = manifest.get("database")
    if isinstance(database, Mapping):
        return str(database.get("kind") or "")
    return ""


def _open_readonly(path: Path, timeout: int) -> sqlite3.Connection:
    try:
        uri = f"{path.resolve(strict=False).as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri, uri=True, timeout=timeout / 1_000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={timeout}")
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as error:
        raise BackupV2Error(f"cannot open SQLite database read-only: {path}") from error


def _existing_database_path(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_file():
        raise BackupV2Error(f"{label} does not exist: {path}")
    return path


def _require_matching_database_artifact(
    path: Path,
    *,
    expected_snapshot: Mapping[str, Any],
    expected_kind: str,
    timeout: int,
    label: str,
) -> dict[str, Any]:
    permission_errors: list[dict[str, Any]] = []
    _validate_private_file(path, label.replace(" ", "_"), permission_errors)
    if permission_errors:
        raise BackupV2RestoreError(
            f"{label} is not a private reusable artifact and was preserved: {permission_errors!r}"
        )
    try:
        actual = _inspect_database(
            path,
            expected_kind=expected_kind,
            timeout=timeout,
            expected_schema_version=int(
                expected_snapshot["database"]["schema_version"]
            ),
            allow_supported_older=True,
        )
    except (BackupV2Error, KeyError, TypeError, ValueError) as error:
        raise BackupV2RestoreError(
            f"{label} is partial or invalid and was preserved in place: {error}"
        ) from error
    if actual["database"] != dict(expected_snapshot.get("database") or {}):
        raise BackupV2RestoreError(
            f"{label} belongs to a different database state and was preserved in place"
        )
    if actual["state_proof"] != dict(expected_snapshot.get("state_proof") or {}):
        raise BackupV2RestoreError(
            f"{label} has different durable row content and was preserved in place"
        )
    return actual


def _require_matching_backup_bundle(
    backup: Path,
    *,
    expected_manifest: Mapping[str, Any],
    expected_kind: str,
    expected_generation: str,
    timeout: int,
) -> dict[str, Any]:
    report = validate_v2_backup(
        backup,
        expected_kind=expected_kind,
        expected_generation=expected_generation,
        busy_timeout_ms=timeout,
    )
    if not report["valid"]:
        raise BackupV2ValidationError(
            "existing backup bundle is partial or invalid and was preserved in place",
            report=report,
        )
    actual_manifest = report["manifest"]
    matching_sections = ("database", "backup", "deployed_contract", "state_proof")
    if any(
        actual_manifest.get(section) != expected_manifest.get(section)
        for section in matching_sections
    ) or str(actual_manifest.get("source", {}).get("path") or "") != str(
        expected_manifest.get("source", {}).get("path") or ""
    ):
        raise BackupV2RestoreError(
            "existing backup bundle belongs to another publication and was preserved in place"
        )
    return report


def _output_path(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() and not path.is_file():
            raise BackupV2RestoreError(
                f"{label} has an existing SQLite sidecar and was preserved: {sidecar}"
            )
    _prepare_private_directory(path.parent)
    return path


def _fresh_output_path(value: str | Path, *, label: str) -> Path:
    path = _output_path(value, label=label)
    if path.exists():
        raise BackupV2RestoreError(
            f"{label} already exists and will not be overwritten: {path}"
        )
    return path


def _prepare_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as error:
        raise BackupV2Error(
            f"cannot create private backup directory: {path}"
        ) from error
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o700:
        raise BackupV2Error(f"backup destination directory must be mode 0700: {path}")


def _publish_exclusive(
    temporary_path: Path,
    destination: Path,
) -> None:
    _fsync_file(temporary_path)
    try:
        os.link(temporary_path, destination)
    except FileExistsError as error:
        raise BackupV2RestoreError(
            f"destination appeared during backup/restore and was not overwritten: {destination}"
        ) from error
    except OSError as error:
        raise BackupV2Error(
            f"cannot publish private SQLite snapshot: {destination}"
        ) from error
    _chmod_private_file(destination)
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    try:
        temporary_path.unlink()
        _fsync_directory(temporary_path.parent)
    except OSError as error:
        raise BackupV2Error(
            f"published artifact is durable but temporary link cleanup failed: {temporary_path}"
        ) from error


def _write_private_json_exclusive(
    path: Path,
    value: Mapping[str, Any],
) -> None:
    data = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_private_file(temporary_path)
        _publish_exclusive(temporary_path, path)
    finally:
        _unlink_if_exists(temporary_path)


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise BackupV2Error(f"cannot fsync published artifact: {path}") from error


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise BackupV2Error(f"cannot fsync publication directory: {path}") from error


def _chmod_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as error:
        raise BackupV2Error(
            f"cannot harden private file permissions: {path}"
        ) from error
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise BackupV2Error(f"private backup file must be mode 0600: {path}")


def _validate_private_file(
    path: Path, label: str, errors: list[dict[str, Any]]
) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        errors.append({"code": f"{label}_stat_failed", "message": str(error)})
        return
    if mode != 0o600:
        errors.append(
            {"code": f"{label}_permissions_not_private", "actual_mode": oct(mode)}
        )


def _validate_private_directory(path: Path, errors: list[dict[str, Any]]) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        errors.append({"code": "backup_directory_stat_failed", "message": str(error)})
        return
    if mode != 0o700:
        errors.append(
            {
                "code": "backup_directory_permissions_not_private",
                "actual_mode": oct(mode),
            }
        )


def _file_metadata(path: Path) -> dict[str, Any]:
    try:
        metadata = path.stat()
    except OSError as error:
        raise BackupV2Error(f"cannot stat source database: {path}") from error
    return {
        "path": str(path.resolve(strict=False)),
        "sha256": _sha256_file(path),
        "size_bytes": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise BackupV2Error(f"cannot checksum file: {path}") from error
    return digest.hexdigest()


def _require_generation(actual: str, expected: str) -> None:
    if expected and actual != _generation_value(expected):
        raise BackupV2Error(
            f"database generation mismatch: expected {_generation_value(expected)!r}, got {actual!r}"
        )


def _generation_value(value: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError("expected_generation must not be blank when supplied")
    return result


def _database_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    if kind not in DATABASE_KINDS:
        raise ValueError(f"database_kind must be one of {sorted(DATABASE_KINDS)!r}")
    return kind


def _busy_timeout(value: int) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("busy_timeout_ms must be an integer") from error
    if timeout < 1:
        raise ValueError("busy_timeout_ms must be positive")
    return timeout


def _sha256_text(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text.lower()
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "AdmissionFreezeController",
    "AdmissionFreezeGate",
    "AdmissionFreezeLease",
    "AdmissionFrozenError",
    "BackupV2Error",
    "BackupV2RestoreError",
    "BackupV2ValidationError",
    "DATABASE_KIND_EDGE_V2",
    "DATABASE_KIND_HUB_V2",
    "DEFAULT_BACKUP_BUSY_TIMEOUT_MS",
    "PRE_MIGRATION_BACKUP_MARKER_SUFFIX",
    "PRE_MIGRATION_BACKUP_MARKER_VERSION",
    "V2_BACKUP_MANIFEST_SUFFIX",
    "V2_BACKUP_MANIFEST_VERSION",
    "create_edge_v2_backup",
    "create_hub_v2_backup",
    "create_pre_migration_backup_marker",
    "create_v2_sqlite_backup",
    "pre_migration_backup_marker_path",
    "require_pre_migration_validated_backup",
    "restore_edge_v2_backup",
    "restore_hub_v2_backup",
    "restore_v2_sqlite_backup",
    "validate_pre_migration_backup_marker",
    "v2_backup_manifest_path",
    "validate_v2_backup",
]
