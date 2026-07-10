"""Private crash-safe SQLite journal for Hub V2 Edge mutations.

The journal is intentionally independent from the Hub database.  An Edge must
commit an intent here before invoking a mutating domain action, then commit the
result to the outbox before telling the Hub that the attempt completed.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from patchbay.hub.operations import semantic_payload_hash


SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000

ATTEMPT_STATES = frozenset(
    {
        "intent_recorded",
        "executing",
        "effect_recorded",
        "result_ready",
        "acknowledged",
        "outcome_unknown",
        "manual_recovery",
    }
)

RECOVERY_EXECUTE_INTENT = "execute_intent"
RECOVERY_RECONCILE_EFFECT = "reconcile_effect"
RECOVERY_UPLOAD_RESULT = "upload_result"
RECOVERY_MANUAL = "manual_recovery"


class EdgeJournalError(RuntimeError):
    """Base error for the private Edge journal."""


class EdgeJournalCorrupt(EdgeJournalError):
    """Raised when the journal cannot be opened or decoded safely."""


class EdgeJournalConflict(EdgeJournalError):
    """Raised for payload, generation, fencing, or revision conflicts."""


class EdgeJournalStateError(EdgeJournalError):
    """Raised when an attempt transition would violate the crash protocol."""


class EdgeJournalNotFound(EdgeJournalError):
    """Raised when an operation, attempt, or receipt is unknown."""


class EdgeJournal:
    """Durable intent, attempt, result-outbox, and projection state.

    The caller owns domain execution.  The required ordering is::

        record_intent() -> mark_attempt_executing() -> domain effect
        -> mark_effect_recorded() -> record_result() -> Hub upload
        -> acknowledge_outbox() -> prune_acknowledged()

    Recovery code must inspect :meth:`list_restart_recovery` before replaying an
    attempt.  In particular, ``executing`` and ``effect_recorded`` attempts are
    never classified as safe blind retries.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        edge_generation: str | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path).expanduser()
        try:
            self.busy_timeout_ms = int(busy_timeout_ms)
        except (TypeError, ValueError) as exc:
            raise ValueError("busy_timeout_ms must be an integer") from exc
        if self.busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")

        requested_generation = _optional_key(edge_generation, "edge_generation")
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._configure_connection()
            self._migrate(requested_generation)
            self._harden_permissions()
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise EdgeJournalCorrupt(f"Edge journal cannot be opened safely: {self.path}") from exc
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for narrow diagnostics and tests."""

        self._require_open()
        return self._connection

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def edge_generation(self) -> str:
        self._require_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT edge_generation FROM edge_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise EdgeJournalCorrupt("Edge generation record is missing")
        return str(row["edge_generation"])

    @property
    def projection_revision(self) -> int:
        self._require_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT projection_revision FROM edge_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise EdgeJournalCorrupt("Edge projection record is missing")
        return int(row["projection_revision"])

    def _configure_connection(self) -> None:
        journal_mode = str(self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise EdgeJournalError(f"Edge journal requires WAL mode, got {journal_mode!r}")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        self._connection.execute("PRAGMA synchronous=FULL")
        if int(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise EdgeJournalError("Edge journal requires SQLite foreign keys")

    def _migrate(self, requested_generation: str) -> None:
        migration_owner = f"edge_migration_{secrets.token_hex(16)}"
        now = self._clock()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_metadata (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL CHECK (schema_version >= 0),
                        migration_lock TEXT,
                        migration_started_at REAL,
                        updated_at REAL NOT NULL
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
                    raise EdgeJournalCorrupt("Edge journal schema metadata is missing")
                if metadata["migration_lock"]:
                    raise EdgeJournalConflict("Another or incomplete Edge journal migration owns the lock")
                current_version = int(metadata["schema_version"])
                if current_version > SCHEMA_VERSION:
                    raise EdgeJournalCorrupt(
                        f"Edge journal schema {current_version} is newer than supported {SCHEMA_VERSION}"
                    )
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

                state = self._connection.execute(
                    "SELECT edge_generation FROM edge_state WHERE singleton = 1"
                ).fetchone()
                if state is None:
                    generation = requested_generation or f"edgegen_{secrets.token_hex(16)}"
                    self._connection.execute(
                        """
                        INSERT INTO edge_state
                            (singleton, edge_generation, projection_revision, created_at, updated_at)
                        VALUES (1, ?, 0, ?, ?)
                        """,
                        (generation, now, now),
                    )
                elif requested_generation and str(state["edge_generation"]) != requested_generation:
                    raise EdgeJournalConflict("edge_generation_conflict")

                self._connection.execute(f"PRAGMA user_version={current_version}")
                self._connection.execute(
                    """
                    UPDATE schema_metadata
                    SET schema_version = ?, migration_lock = NULL, migration_started_at = NULL, updated_at = ?
                    WHERE singleton = 1 AND migration_lock = ?
                    """,
                    (current_version, self._clock(), migration_owner),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _apply_schema_v1(self) -> None:
        statements = (
            """
            CREATE TABLE edge_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                edge_generation TEXT NOT NULL CHECK (length(edge_generation) > 0),
                projection_revision INTEGER NOT NULL DEFAULT 0 CHECK (projection_revision >= 0),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE operation_intents (
                operation_id TEXT PRIMARY KEY,
                edge_generation TEXT NOT NULL,
                action TEXT NOT NULL CHECK (length(action) > 0),
                target_key TEXT NOT NULL CHECK (length(target_key) > 0),
                idempotency_key TEXT NOT NULL DEFAULT '',
                payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
                payload_json TEXT NOT NULL
                    CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
                correlation_json TEXT NOT NULL
                    CHECK (json_valid(correlation_json) AND json_type(correlation_json) = 'object'),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX operation_intents_idempotency_idx
            ON operation_intents(edge_generation, action, target_key, idempotency_key)
            WHERE idempotency_key <> ''
            """,
            f"""
            CREATE TABLE operation_attempts (
                attempt_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL
                    REFERENCES operation_intents(operation_id) ON DELETE RESTRICT,
                edge_generation TEXT NOT NULL,
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                state TEXT NOT NULL CHECK (state IN ({_sql_values(ATTEMPT_STATES)})),
                revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
                effect_json TEXT,
                result_hash TEXT CHECK (result_hash IS NULL OR length(result_hash) = 64),
                outcome TEXT,
                result_json TEXT,
                result_error TEXT NOT NULL DEFAULT '',
                result_uncertain INTEGER NOT NULL DEFAULT 0 CHECK (result_uncertain IN (0, 1)),
                receipt_id TEXT UNIQUE,
                effect_started_at REAL,
                effect_recorded_at REAL,
                result_recorded_at REAL,
                acknowledged_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (operation_id, fencing_token),
                CHECK (effect_json IS NULL OR (json_valid(effect_json) AND json_type(effect_json) = 'object')),
                CHECK (result_json IS NULL OR (json_valid(result_json) AND json_type(result_json) = 'object'))
            )
            """,
            "CREATE INDEX operation_attempts_recovery_idx ON operation_attempts(state, updated_at)",
            """
            CREATE TABLE result_outbox (
                receipt_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL
                    REFERENCES operation_intents(operation_id) ON DELETE RESTRICT,
                attempt_id TEXT NOT NULL UNIQUE
                    REFERENCES operation_attempts(attempt_id) ON DELETE RESTRICT,
                edge_generation TEXT NOT NULL,
                fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
                operation_payload_hash TEXT NOT NULL CHECK (length(operation_payload_hash) = 64),
                target_key TEXT NOT NULL CHECK (length(target_key) > 0),
                outcome TEXT NOT NULL CHECK (length(outcome) > 0),
                result_hash TEXT NOT NULL CHECK (length(result_hash) = 64),
                result_json TEXT NOT NULL
                    CHECK (json_valid(result_json) AND json_type(result_json) = 'object'),
                error TEXT NOT NULL DEFAULT '',
                uncertain INTEGER NOT NULL DEFAULT 0 CHECK (uncertain IN (0, 1)),
                created_at REAL NOT NULL,
                acknowledged_at REAL
            )
            """,
            "CREATE INDEX result_outbox_pending_idx ON result_outbox(acknowledged_at, created_at)",
            "CREATE INDEX result_outbox_uncertain_idx ON result_outbox(uncertain, created_at)",
        )
        for statement in statements:
            self._connection.execute(statement)

    @contextmanager
    def immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        """Run bounded local persistence under ``BEGIN IMMEDIATE``."""

        self._require_open()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._harden_permissions()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    transaction = immediate_transaction

    def schema_info(self) -> dict[str, Any]:
        self._require_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT schema_version, migration_lock FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise EdgeJournalCorrupt("Edge journal schema metadata is missing")
            return {
                "schema_version": int(row["schema_version"]),
                "migration_lock": row["migration_lock"],
                "journal_mode": str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "foreign_keys": bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout_ms": int(self._connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                "synchronous": int(self._connection.execute("PRAGMA synchronous").fetchone()[0]),
            }

    def projection_identity(self) -> dict[str, Any]:
        return {
            "edge_generation": self.edge_generation,
            "projection_revision": self.projection_revision,
        }

    def advance_projection_revision(self, *, expected_revision: int | None = None) -> int:
        """Atomically allocate the next revision inside the persisted generation."""

        with self.immediate_transaction() as connection:
            row = connection.execute(
                "SELECT projection_revision FROM edge_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise EdgeJournalCorrupt("Edge projection record is missing")
            current = int(row["projection_revision"])
            if expected_revision is not None and _revision(expected_revision) != current:
                raise EdgeJournalConflict(
                    f"projection_revision_conflict: expected {expected_revision}, actual {current}"
                )
            next_revision = current + 1
            connection.execute(
                """
                UPDATE edge_state
                SET projection_revision = ?, updated_at = ?
                WHERE singleton = 1 AND projection_revision = ?
                """,
                (next_revision, self._clock(), current),
            )
            return next_revision

    next_projection_revision = advance_projection_revision

    def persist_projection_revision(self, revision: int) -> int:
        """Persist an observed revision without permitting regression."""

        requested = _revision(revision)
        with self.immediate_transaction() as connection:
            row = connection.execute(
                "SELECT projection_revision FROM edge_state WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise EdgeJournalCorrupt("Edge projection record is missing")
            current = int(row["projection_revision"])
            if requested < current:
                raise EdgeJournalConflict(
                    f"projection_revision_regression: current {current}, requested {requested}"
                )
            if requested > current:
                connection.execute(
                    "UPDATE edge_state SET projection_revision = ?, updated_at = ? WHERE singleton = 1",
                    (requested, self._clock()),
                )
            return requested

    def record_intent(
        self,
        *,
        operation_id: str,
        attempt_id: str,
        fencing_token: int,
        action: str,
        target_key: str,
        payload: Mapping[str, Any],
        payload_hash: str = "",
        edge_generation: str = "",
        idempotency_key: str = "",
        correlation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist operation intent and its immutable attempt before any effect.

        Repeating an identical operation/attempt is idempotent.  Reusing the
        operation identity for different semantics fails closed.
        """

        operation_value = _key(operation_id, "operation_id")
        attempt_value = _key(attempt_id, "attempt_id")
        action_value = _key(action, "action")
        target_value = _key(target_key, "target_key")
        generation_value = _optional_key(edge_generation, "edge_generation") or self.edge_generation
        if generation_value != self.edge_generation:
            raise EdgeJournalConflict("edge_generation_conflict")
        token_value = _fencing_token(fencing_token)
        payload_value = _object(payload, "payload")
        payload_json = _encode_object(payload_value)
        computed_hash = semantic_payload_hash(payload_value)
        if payload_hash and str(payload_hash).strip().lower() != computed_hash:
            raise EdgeJournalConflict("operation_payload_hash_mismatch")
        correlation_json = _encode_object(_object(correlation or {}, "correlation"))
        idempotency_value = str(idempotency_key or "").strip()
        now = self._clock()

        with self.immediate_transaction() as connection:
            intent = connection.execute(
                "SELECT * FROM operation_intents WHERE operation_id = ?", (operation_value,)
            ).fetchone()
            if intent is None:
                if idempotency_value:
                    scoped = connection.execute(
                        """
                        SELECT operation_id FROM operation_intents
                        WHERE edge_generation = ? AND action = ? AND target_key = ? AND idempotency_key = ?
                        """,
                        (generation_value, action_value, target_value, idempotency_value),
                    ).fetchone()
                    if scoped is not None:
                        raise EdgeJournalConflict("idempotency_operation_conflict")
                connection.execute(
                    """
                    INSERT INTO operation_intents
                        (operation_id, edge_generation, action, target_key, idempotency_key,
                         payload_hash, payload_json, correlation_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_value,
                        generation_value,
                        action_value,
                        target_value,
                        idempotency_value,
                        computed_hash,
                        payload_json,
                        correlation_json,
                        now,
                        now,
                    ),
                )
            else:
                identity = (
                    str(intent["edge_generation"]),
                    str(intent["action"]),
                    str(intent["target_key"]),
                    str(intent["idempotency_key"]),
                    str(intent["payload_hash"]),
                    str(intent["payload_json"]),
                    str(intent["correlation_json"]),
                )
                requested = (
                    generation_value,
                    action_value,
                    target_value,
                    idempotency_value,
                    computed_hash,
                    payload_json,
                    correlation_json,
                )
                if identity != requested:
                    raise EdgeJournalConflict("idempotency_payload_conflict")

            attempt = connection.execute(
                "SELECT * FROM operation_attempts WHERE attempt_id = ?", (attempt_value,)
            ).fetchone()
            if attempt is not None:
                if (
                    str(attempt["operation_id"]) != operation_value
                    or str(attempt["edge_generation"]) != generation_value
                    or int(attempt["fencing_token"]) != token_value
                ):
                    raise EdgeJournalConflict("attempt_identity_conflict")
                return self._attempt_bundle(connection, attempt_value, idempotent_replay=True)

            maximum = connection.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) FROM operation_attempts WHERE operation_id = ?",
                (operation_value,),
            ).fetchone()
            max_fence = int(maximum[0]) if maximum is not None else 0
            if token_value <= max_fence:
                raise EdgeJournalConflict(
                    f"stale_fencing_token: current {max_fence}, received {token_value}"
                )
            connection.execute(
                """
                INSERT INTO operation_attempts
                    (attempt_id, operation_id, edge_generation, fencing_token, state,
                     revision, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'intent_recorded', 1, ?, ?)
                """,
                (attempt_value, operation_value, generation_value, token_value, now, now),
            )
            return self._attempt_bundle(connection, attempt_value, idempotent_replay=False)

    persist_intent = record_intent

    def mark_attempt_executing(
        self,
        operation_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        edge_generation: str = "",
    ) -> dict[str, Any]:
        """Commit the effect boundary immediately before domain execution."""

        return self._transition_attempt(
            operation_id=operation_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            edge_generation=edge_generation,
            target_state="executing",
        )

    mark_executing = mark_attempt_executing

    def mark_effect_recorded(
        self,
        operation_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        effect: Mapping[str, Any] | None = None,
        edge_generation: str = "",
    ) -> dict[str, Any]:
        """Persist action-specific effect evidence before result publication."""

        return self._transition_attempt(
            operation_id=operation_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            edge_generation=edge_generation,
            target_state="effect_recorded",
            effect=effect or {},
        )

    def mark_outcome_unknown(
        self,
        operation_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        edge_generation: str = "",
    ) -> dict[str, Any]:
        """Persist that an effect may have happened and requires reconciliation."""

        return self._transition_attempt(
            operation_id=operation_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            edge_generation=edge_generation,
            target_state="outcome_unknown",
        )

    def _transition_attempt(
        self,
        *,
        operation_id: str,
        attempt_id: str,
        fencing_token: int,
        edge_generation: str,
        target_state: str,
        effect: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation_value = _key(operation_id, "operation_id")
        attempt_value = _key(attempt_id, "attempt_id")
        token_value = _fencing_token(fencing_token)
        generation_value = _optional_key(edge_generation, "edge_generation") or self.edge_generation
        now = self._clock()
        effect_json = _encode_object(_object(effect or {}, "effect")) if effect is not None else None
        allowed = {
            "executing": frozenset({"intent_recorded"}),
            "effect_recorded": frozenset({"executing"}),
            "outcome_unknown": frozenset({"executing", "effect_recorded"}),
        }
        with self.immediate_transaction() as connection:
            attempt = self._fenced_attempt(
                connection,
                operation_id=operation_value,
                attempt_id=attempt_value,
                fencing_token=token_value,
                edge_generation=generation_value,
            )
            current = str(attempt["state"])
            if current == target_state:
                if effect_json is not None and str(attempt["effect_json"] or "") != effect_json:
                    raise EdgeJournalConflict("effect_record_conflict")
                return self._attempt_bundle(connection, attempt_value, idempotent_replay=True)
            if current not in allowed[target_state]:
                raise EdgeJournalStateError(f"Invalid Edge attempt transition: {current} -> {target_state}")

            assignments = ["state = ?", "revision = revision + 1", "updated_at = ?"]
            values: list[Any] = [target_state, now]
            if target_state == "executing":
                assignments.append("effect_started_at = ?")
                values.append(now)
            elif target_state == "effect_recorded":
                assignments.extend(("effect_json = ?", "effect_recorded_at = ?"))
                values.extend((effect_json, now))
            values.append(attempt_value)
            connection.execute(
                f"UPDATE operation_attempts SET {', '.join(assignments)} WHERE attempt_id = ?",
                tuple(values),
            )
            return self._attempt_bundle(connection, attempt_value, idempotent_replay=False)

    def record_result(
        self,
        *,
        operation_id: str,
        attempt_id: str,
        fencing_token: int,
        outcome: str,
        result: Mapping[str, Any] | None = None,
        error: str = "",
        uncertain: bool = False,
        receipt_id: str = "",
        edge_generation: str = "",
    ) -> dict[str, Any]:
        """Atomically persist an attempt outcome and its upload receipt."""

        operation_value = _key(operation_id, "operation_id")
        attempt_value = _key(attempt_id, "attempt_id")
        token_value = _fencing_token(fencing_token)
        outcome_value = _key(outcome, "outcome")
        generation_value = _optional_key(edge_generation, "edge_generation") or self.edge_generation
        result_value = _object(result or {}, "result")
        result_json = _encode_object(result_value)
        error_value = str(error or "")
        uncertain_value = bool(uncertain or outcome_value == "outcome_unknown")
        result_hash = semantic_payload_hash(
            {
                "outcome": outcome_value,
                "result": result_value,
                "error": error_value,
                "uncertain": uncertain_value,
            }
        )
        requested_receipt = _optional_key(receipt_id, "receipt_id")
        now = self._clock()

        with self.immediate_transaction() as connection:
            attempt = self._fenced_attempt(
                connection,
                operation_id=operation_value,
                attempt_id=attempt_value,
                fencing_token=token_value,
                edge_generation=generation_value,
            )
            current_result_hash = str(attempt["result_hash"] or "")
            if current_result_hash:
                if current_result_hash != result_hash:
                    raise EdgeJournalConflict("attempt_result_conflict")
                saved_receipt = str(attempt["receipt_id"] or "")
                if requested_receipt and requested_receipt != saved_receipt:
                    raise EdgeJournalConflict("receipt_identity_conflict")
                outbox = connection.execute(
                    "SELECT * FROM result_outbox WHERE attempt_id = ?", (attempt_value,)
                ).fetchone()
                if outbox is not None:
                    replay = self._outbox_from_row(outbox)
                    replay["idempotent_replay"] = True
                    replay["pruned"] = False
                    return replay
                replay = self._pruned_receipt_from_attempt(connection, attempt_value)
                replay["idempotent_replay"] = True
                return replay

            if str(attempt["state"]) in {"acknowledged", "manual_recovery", "result_ready"}:
                raise EdgeJournalStateError(
                    f"Attempt {attempt_value} cannot record a new result from {attempt['state']}"
                )
            receipt_value = requested_receipt or f"receipt_{secrets.token_hex(16)}"
            intent = connection.execute(
                "SELECT payload_hash, target_key FROM operation_intents WHERE operation_id = ?",
                (operation_value,),
            ).fetchone()
            if intent is None:
                raise EdgeJournalCorrupt(f"Intent {operation_value} is missing")

            connection.execute(
                """
                INSERT INTO result_outbox
                    (receipt_id, operation_id, attempt_id, edge_generation, fencing_token,
                     operation_payload_hash, target_key, outcome, result_hash, result_json,
                     error, uncertain, created_at, acknowledged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    receipt_value,
                    operation_value,
                    attempt_value,
                    generation_value,
                    token_value,
                    str(intent["payload_hash"]),
                    str(intent["target_key"]),
                    outcome_value,
                    result_hash,
                    result_json,
                    error_value,
                    int(uncertain_value),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE operation_attempts
                SET state = ?, revision = revision + 1, result_hash = ?, outcome = ?,
                    result_json = ?, result_error = ?, result_uncertain = ?, receipt_id = ?,
                    result_recorded_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    "outcome_unknown" if uncertain_value else "result_ready",
                    result_hash,
                    outcome_value,
                    result_json,
                    error_value,
                    int(uncertain_value),
                    receipt_value,
                    now,
                    now,
                    attempt_value,
                ),
            )
            outbox = connection.execute(
                "SELECT * FROM result_outbox WHERE receipt_id = ?", (receipt_value,)
            ).fetchone()
            if outbox is None:
                raise EdgeJournalCorrupt("Committed Edge result is missing from the outbox")
            saved = self._outbox_from_row(outbox)
            saved["idempotent_replay"] = False
            saved["pruned"] = False
            return saved

    persist_result = record_result

    def acknowledge_outbox(
        self,
        receipt_id: str,
        *,
        operation_id: str = "",
        attempt_id: str = "",
        fencing_token: int | None = None,
        edge_generation: str = "",
    ) -> dict[str, Any]:
        """Record explicit Hub receipt acknowledgement; never implies pruning."""

        receipt_value = _key(receipt_id, "receipt_id")
        now = self._clock()
        with self.immediate_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM result_outbox WHERE receipt_id = ?", (receipt_value,)
            ).fetchone()
            if row is None:
                attempt = connection.execute(
                    "SELECT attempt_id, acknowledged_at FROM operation_attempts WHERE receipt_id = ?",
                    (receipt_value,),
                ).fetchone()
                if attempt is not None and attempt["acknowledged_at"] is not None:
                    replay = self._pruned_receipt_from_attempt(connection, str(attempt["attempt_id"]))
                    self._validate_receipt_ack(
                        replay,
                        operation_id=operation_id,
                        attempt_id=attempt_id,
                        fencing_token=fencing_token,
                        edge_generation=edge_generation,
                    )
                    replay["idempotent_replay"] = True
                    return replay
                raise EdgeJournalNotFound(f"Unknown Edge outbox receipt: {receipt_value}")

            self._validate_receipt_ack(
                row,
                operation_id=operation_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                edge_generation=edge_generation,
            )
            if row["acknowledged_at"] is not None:
                replay = self._outbox_from_row(row)
                replay["idempotent_replay"] = True
                replay["pruned"] = False
                return replay

            connection.execute(
                "UPDATE result_outbox SET acknowledged_at = ? WHERE receipt_id = ?",
                (now, receipt_value),
            )
            connection.execute(
                """
                UPDATE operation_attempts
                SET state = CASE WHEN result_uncertain = 1 THEN 'manual_recovery' ELSE 'acknowledged' END,
                    revision = revision + 1, acknowledged_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (now, now, str(row["attempt_id"])),
            )
            saved = connection.execute(
                "SELECT * FROM result_outbox WHERE receipt_id = ?", (receipt_value,)
            ).fetchone()
            if saved is None:
                raise EdgeJournalCorrupt("Acknowledged Edge receipt disappeared")
            result = self._outbox_from_row(saved)
            result["idempotent_replay"] = False
            result["pruned"] = False
            return result

    acknowledge = acknowledge_outbox

    def acknowledge_many(self, receipt_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Acknowledge a Hub-supplied receipt list idempotently."""

        return [self.acknowledge_outbox(receipt_id) for receipt_id in receipt_ids]

    def prune_acknowledged(
        self,
        *,
        retention_seconds: float = 0.0,
        older_than_seconds: float | None = None,
        before: float | None = None,
        now: float | None = None,
    ) -> int:
        """Prune only certain, acknowledged delivery rows.

        Intent and attempt tombstones remain, preserving duplicate detection.
        Uncertain receipts remain even after Hub acknowledgement.
        """

        retention = retention_seconds if older_than_seconds is None else older_than_seconds
        try:
            retention_value = float(retention)
        except (TypeError, ValueError) as exc:
            raise ValueError("retention_seconds must be numeric") from exc
        if retention_value < 0:
            raise ValueError("retention_seconds must be non-negative")
        cutoff = (
            float(before)
            if before is not None
            else float(now if now is not None else self._clock()) - retention_value
        )
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM result_outbox
                WHERE acknowledged_at IS NOT NULL AND uncertain = 0 AND acknowledged_at <= ?
                """,
                (cutoff,),
            )
            return int(cursor.rowcount)

    def get_intent(self, operation_id: str) -> dict[str, Any] | None:
        operation_value = _key(operation_id, "operation_id")
        self._require_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM operation_intents WHERE operation_id = ?", (operation_value,)
            ).fetchone()
        return self._intent_from_row(row) if row is not None else None

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        attempt_value = _key(attempt_id, "attempt_id")
        self._require_open()
        with self._lock:
            row = self._connection.execute(
                """
                SELECT a.*, i.action, i.target_key, i.payload_hash, i.payload_json, i.correlation_json
                FROM operation_attempts AS a
                JOIN operation_intents AS i ON i.operation_id = a.operation_id
                WHERE a.attempt_id = ?
                """,
                (attempt_value,),
            ).fetchone()
        return self._attempt_from_row(row) if row is not None else None

    def get_outbox(self, receipt_id: str) -> dict[str, Any] | None:
        receipt_value = _key(receipt_id, "receipt_id")
        self._require_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM result_outbox WHERE receipt_id = ?", (receipt_value,)
            ).fetchone()
        return self._outbox_from_row(row) if row is not None else None

    def list_pending_outbox(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return durable receipts that still need Hub acknowledgement."""

        query = "SELECT * FROM result_outbox WHERE acknowledged_at IS NULL ORDER BY created_at, receipt_id"
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            limit_value = _limit(limit)
            query += " LIMIT ?"
            parameters = (limit_value,)
        self._require_open()
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    pending_outbox = list_pending_outbox

    def list_uncertain_receipts(self) -> list[dict[str, Any]]:
        """Return uncertain receipts regardless of acknowledgement age."""

        self._require_open()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM result_outbox WHERE uncertain = 1 ORDER BY created_at, receipt_id"
            ).fetchall()
        return [self._outbox_from_row(row) for row in rows]

    def list_restart_recovery(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Classify non-final attempts after process restart.

        ``execute_intent`` is the only class safe to execute without
        action-specific reconciliation.  Stale fenced attempts are retained as
        manual-recovery evidence and cannot write a result.
        """

        query = """
            SELECT a.*, i.action, i.target_key, i.payload_hash, i.payload_json, i.correlation_json,
                   o.receipt_id AS outbox_receipt_id,
                   o.acknowledged_at AS outbox_acknowledged_at,
                   o.uncertain AS outbox_uncertain,
                   (SELECT MAX(newer.fencing_token)
                    FROM operation_attempts AS newer
                    WHERE newer.operation_id = a.operation_id) AS current_fencing_token
            FROM operation_attempts AS a
            JOIN operation_intents AS i ON i.operation_id = a.operation_id
            LEFT JOIN result_outbox AS o ON o.attempt_id = a.attempt_id
            WHERE a.state <> 'acknowledged'
            ORDER BY a.created_at, a.attempt_id
        """
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (_limit(limit),)
        self._require_open()
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._recovery_from_row(row) for row in rows]

    list_recovery_attempts = list_restart_recovery
    list_unfinished_intents = list_restart_recovery

    def recovery_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        records = self.list_restart_recovery()
        return {
            "attempts": records,
            "pending_outbox": self.list_pending_outbox(),
            "uncertain_receipts": self.list_uncertain_receipts(),
        }

    def _fenced_attempt(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        attempt_id: str,
        fencing_token: int,
        edge_generation: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM operation_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise EdgeJournalNotFound(f"Unknown Edge attempt: {attempt_id}")
        if (
            str(row["operation_id"]) != operation_id
            or str(row["edge_generation"]) != edge_generation
            or int(row["fencing_token"]) != fencing_token
        ):
            raise EdgeJournalConflict("attempt_fence_conflict")
        maximum = connection.execute(
            "SELECT MAX(fencing_token) FROM operation_attempts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        current_fence = int(maximum[0]) if maximum is not None and maximum[0] is not None else 0
        if fencing_token != current_fence:
            raise EdgeJournalConflict(
                f"stale_fencing_token: current {current_fence}, received {fencing_token}"
            )
        return row

    def _attempt_bundle(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        *,
        idempotent_replay: bool,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT a.*, i.action, i.target_key, i.payload_hash, i.payload_json, i.correlation_json
            FROM operation_attempts AS a
            JOIN operation_intents AS i ON i.operation_id = a.operation_id
            WHERE a.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise EdgeJournalCorrupt(f"Attempt {attempt_id} disappeared")
        result = self._attempt_from_row(row)
        result["idempotent_replay"] = idempotent_replay
        return result

    def _pruned_receipt_from_attempt(
        self, connection: sqlite3.Connection, attempt_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT a.*, i.target_key, i.payload_hash
            FROM operation_attempts AS a
            JOIN operation_intents AS i ON i.operation_id = a.operation_id
            WHERE a.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None or not row["receipt_id"]:
            raise EdgeJournalCorrupt(f"Receipt tombstone for attempt {attempt_id} is missing")
        return {
            "receipt_id": str(row["receipt_id"]),
            "operation_id": str(row["operation_id"]),
            "attempt_id": str(row["attempt_id"]),
            "edge_generation": str(row["edge_generation"]),
            "fencing_token": int(row["fencing_token"]),
            "operation_payload_hash": str(row["payload_hash"]),
            "target_key": str(row["target_key"]),
            "outcome": str(row["outcome"] or ""),
            "result_hash": str(row["result_hash"] or ""),
            "result": _decode_object(row["result_json"], "attempt result"),
            "error": str(row["result_error"] or ""),
            "uncertain": bool(row["result_uncertain"]),
            "created_at": float(row["result_recorded_at"] or row["created_at"]),
            "acknowledged_at": (
                float(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None
            ),
            "pruned": True,
        }

    @staticmethod
    def _validate_receipt_ack(
        row: Mapping[str, Any] | sqlite3.Row,
        *,
        operation_id: str,
        attempt_id: str,
        fencing_token: int | None,
        edge_generation: str,
    ) -> None:
        comparisons = (
            (operation_id, str(row["operation_id"]), "operation_id"),
            (attempt_id, str(row["attempt_id"]), "attempt_id"),
            (edge_generation, str(row["edge_generation"]), "edge_generation"),
        )
        for expected, actual, field in comparisons:
            if expected and _key(expected, field) != actual:
                raise EdgeJournalConflict(f"receipt_{field}_conflict")
        if fencing_token is not None and _fencing_token(fencing_token) != int(row["fencing_token"]):
            raise EdgeJournalConflict("receipt_fencing_token_conflict")

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": str(row["operation_id"]),
            "edge_generation": str(row["edge_generation"]),
            "action": str(row["action"]),
            "target_key": str(row["target_key"]),
            "idempotency_key": str(row["idempotency_key"]),
            "payload_hash": str(row["payload_hash"]),
            "payload": _decode_object(row["payload_json"], "operation payload"),
            "correlation": _decode_object(row["correlation_json"], "operation correlation"),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": str(row["operation_id"]),
            "attempt_id": str(row["attempt_id"]),
            "edge_generation": str(row["edge_generation"]),
            "fencing_token": int(row["fencing_token"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "action": str(row["action"]),
            "target_key": str(row["target_key"]),
            "payload_hash": str(row["payload_hash"]),
            "payload": _decode_object(row["payload_json"], "operation payload"),
            "correlation": _decode_object(row["correlation_json"], "operation correlation"),
            "effect": _decode_object(row["effect_json"], "effect record"),
            "outcome": str(row["outcome"] or ""),
            "result": _decode_object(row["result_json"], "attempt result"),
            "error": str(row["result_error"] or ""),
            "uncertain": bool(row["result_uncertain"]),
            "receipt_id": str(row["receipt_id"] or ""),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "effect_started_at": (
                float(row["effect_started_at"]) if row["effect_started_at"] is not None else None
            ),
            "effect_recorded_at": (
                float(row["effect_recorded_at"]) if row["effect_recorded_at"] is not None else None
            ),
            "result_recorded_at": (
                float(row["result_recorded_at"]) if row["result_recorded_at"] is not None else None
            ),
            "acknowledged_at": (
                float(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None
            ),
        }

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "receipt_id": str(row["receipt_id"]),
            "operation_id": str(row["operation_id"]),
            "attempt_id": str(row["attempt_id"]),
            "edge_generation": str(row["edge_generation"]),
            "fencing_token": int(row["fencing_token"]),
            "operation_payload_hash": str(row["operation_payload_hash"]),
            "target_key": str(row["target_key"]),
            "outcome": str(row["outcome"]),
            "result_hash": str(row["result_hash"]),
            "result": _decode_object(row["result_json"], "outbox result"),
            "error": str(row["error"]),
            "uncertain": bool(row["uncertain"]),
            "created_at": float(row["created_at"]),
            "acknowledged_at": (
                float(row["acknowledged_at"]) if row["acknowledged_at"] is not None else None
            ),
        }

    def _recovery_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = self._attempt_from_row(row)
        current_fence = int(row["current_fencing_token"])
        is_current = int(row["fencing_token"]) == current_fence
        pending_upload = row["outbox_receipt_id"] is not None and row["outbox_acknowledged_at"] is None
        uncertain = bool(row["result_uncertain"] or row["outbox_uncertain"])
        state = str(row["state"])
        if not is_current:
            recovery_action = RECOVERY_MANUAL
        elif pending_upload:
            recovery_action = RECOVERY_UPLOAD_RESULT
        elif uncertain or state in {"outcome_unknown", "manual_recovery"}:
            recovery_action = RECOVERY_MANUAL
        elif state == "intent_recorded":
            recovery_action = RECOVERY_EXECUTE_INTENT
        elif state in {"executing", "effect_recorded"}:
            recovery_action = RECOVERY_RECONCILE_EFFECT
        else:
            recovery_action = RECOVERY_MANUAL
        result.update(
            {
                "current_fencing_token": current_fence,
                "is_current_attempt": is_current,
                "recovery_action": recovery_action,
                "needs_upload": pending_upload,
                "needs_reconciliation": uncertain
                or state in {"executing", "effect_recorded", "outcome_unknown", "manual_recovery"},
            }
        )
        return result

    def _harden_permissions(self) -> None:
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                continue

    def _require_open(self) -> None:
        if self._closed:
            raise EdgeJournalError("Edge journal is closed")

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "EdgeJournal":
        self._require_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _object(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _encode_object(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("journal values must be JSON serializable") from exc


def _decode_object(value: Any, context: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EdgeJournalCorrupt(f"Invalid JSON in {context}") from exc
    if not isinstance(decoded, dict):
        raise EdgeJournalCorrupt(f"Expected an object in {context}")
    return decoded


def _key(value: Any, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _optional_key(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    return _key(value, field)


def _fencing_token(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("fencing_token must be a positive integer")
    try:
        token = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("fencing_token must be a positive integer") from exc
    if token < 1:
        raise ValueError("fencing_token must be a positive integer")
    return token


def _revision(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("projection revision must be a non-negative integer")
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("projection revision must be a non-negative integer") from exc
    if revision < 0:
        raise ValueError("projection revision must be a non-negative integer")
    return revision


def _limit(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("limit must be a positive integer")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be a positive integer") from exc
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    return limit


def _sql_values(values: Sequence[str] | frozenset[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))
