"""Durable Hub V2 operation delivery and recovery broker."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

from patchbay.hub.operations import (
    TERMINAL_OPERATION_STATES,
    normalize_domain_result,
    public_envelope,
    require_attempt_transition,
    require_operation_transition,
)
from patchbay.hub.store_v2 import (
    HubStoreV2,
    HubStoreV2Conflict,
    HubStoreV2Corrupt,
    HubStoreV2StateError,
    semantic_payload_hash,
)


ATTEMPT_CONTRACT_ENTITY_TYPE = "hub.operation_attempt_contract"
OPERATION_GROUP_ENTITY_TYPE = "hub.operation_group"
ACTIVE_ATTEMPT_STATES = frozenset(
    {
        "offered",
        "claimed",
        "executing",
        "effect_recorded",
        "result_ready",
        "reconciling",
    }
)
LEASED_ATTEMPT_STATES = frozenset({"claimed", "executing", "effect_recorded"})
TERMINAL_ATTEMPT_STATES = frozenset({"acknowledged", "retryable", "manual_recovery"})

DEFAULT_LEASE_SECONDS = 30.0
MAX_LEASE_SECONDS = 15 * 60.0
MAX_WAIT_SECONDS = 30.0
DEFAULT_POLL_INTERVAL = 0.025


# These aliases make the broker contract explicit while preserving the store's
# established exception hierarchy for callers which already catch it.
OperationBrokerConflict = HubStoreV2Conflict
OperationBrokerStateError = HubStoreV2StateError


class OperationBroker:
    """Coordinate operations and fenced Edge attempts through ``HubStoreV2``.

    The class is intentionally not connected to the public protocol yet. Every
    method performs bounded local database work; waits poll committed event
    revisions and sleep without retaining a transaction or process lock.
    """

    def __init__(
        self,
        store: HubStoreV2,
        *,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        default_lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        self.store = store
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self.default_lease_seconds = _positive_duration(
            default_lease_seconds, "default_lease_seconds", maximum=MAX_LEASE_SECONDS
        )
        self.max_wait_seconds = _non_negative_duration(
            max_wait_seconds, "max_wait_seconds"
        )
        self.poll_interval = _positive_duration(poll_interval, "poll_interval")

    def create_operation(
        self,
        *,
        tool: str,
        logical_target: str,
        idempotency_key: str,
        payload: Mapping[str, Any] | None = None,
        operation_id: str = "",
        principal_ref: str = "",
        parent_operation_id: str | None = None,
        item_id: str = "",
    ) -> dict[str, Any]:
        """Create an operation or replay the existing semantically equal one."""

        if parent_operation_id:
            if not item_id:
                raise ValueError("item_id is required for a child operation")
            return self.create_child_operation(
                parent_operation_id,
                item_id=item_id,
                tool=tool,
                logical_target=logical_target,
                payload=payload,
                operation_id=operation_id,
                principal_ref=principal_ref,
            )
        if item_id:
            raise ValueError("parent_operation_id is required when item_id is set")
        return self.store.create_operation(
            tool=tool,
            logical_target=logical_target,
            idempotency_key=idempotency_key,
            payload=payload or {},
            operation_id=operation_id,
            principal_ref=principal_ref,
        )

    def associate_operation(
        self,
        operation_id: str,
        *,
        work_group_id: str,
        principal_ref: str = "",
        kind: str = "worker",
    ) -> dict[str, Any]:
        """Persist the durable group relation used by lifecycle accounting."""

        operation_value = _clean(operation_id, "operation_id")
        group_value = _clean(work_group_id, "work_group_id")
        principal = principal_ref or self.store.principal_ref
        operation = self.store.get_operation(operation_value)
        if operation is None or operation["principal_ref"] != principal:
            raise KeyError(f"Unknown operation: {operation_value}")
        existing = self.store.get_entity(OPERATION_GROUP_ENTITY_TYPE, operation_value)
        if existing is not None:
            if str(existing["record"].get("work_group_id") or "") != group_value:
                raise HubStoreV2Conflict("operation_work_group_conflict")
            return existing
        try:
            return self.store.put_entity(
                OPERATION_GROUP_ENTITY_TYPE,
                operation_value,
                {
                    "operation_id": operation_value,
                    "work_group_id": group_value,
                    "kind": str(kind or "worker"),
                },
                expected_revision=0,
            )
        except HubStoreV2Conflict:
            concurrent = self.store.get_entity(
                OPERATION_GROUP_ENTITY_TYPE, operation_value
            )
            if concurrent is not None and str(
                concurrent["record"].get("work_group_id") or ""
            ) == group_value:
                return concurrent
            raise

    @staticmethod
    def child_idempotency_key(parent_operation_id: str, item_id: str) -> str:
        parent = _clean(parent_operation_id, "parent_operation_id")
        item = _clean(item_id, "item_id")
        return "child_" + semantic_payload_hash(
            {"parent_operation_id": parent, "item_id": item}
        )

    def create_child_operation(
        self,
        parent_operation_id: str,
        *,
        item_id: str,
        tool: str,
        logical_target: str,
        payload: Mapping[str, Any] | None = None,
        operation_id: str = "",
        principal_ref: str = "",
    ) -> dict[str, Any]:
        """Create one stable child, unique by parent and item identifier."""

        parent_id = _clean(parent_operation_id, "parent_operation_id")
        item = _clean(item_id, "item_id")
        tool_value = _clean(tool, "tool")
        target = _clean(logical_target, "logical_target")
        requested_principal = principal_ref or self.store.principal_ref
        payload_hash = semantic_payload_hash(payload or {})
        key = self.child_idempotency_key(parent_id, item)
        requested_id = operation_id or f"op_{secrets.token_hex(16)}"

        with self.store.immediate_transaction() as connection:
            parent = self._visible_operation_in_transaction(
                connection, parent_id, requested_principal
            )
            if str(parent["state"]) in TERMINAL_OPERATION_STATES:
                raise HubStoreV2StateError("Cannot add a child to a terminal operation")

            existing = connection.execute(
                "SELECT * FROM operations WHERE parent_operation_id = ? AND item_id = ?",
                (parent_id, item),
            ).fetchone()
            if existing is not None:
                equivalent = (
                    str(existing["principal_ref"]) == requested_principal
                    and str(existing["tool"]) == tool_value
                    and str(existing["logical_target"]) == target
                    and str(existing["semantic_payload_hash"]) == payload_hash
                )
                if not equivalent:
                    raise HubStoreV2Conflict("child_operation_payload_conflict")
                replay = self._operation_from_row(existing)
                replay["idempotent_replay"] = True
                return replay

            scoped = connection.execute(
                """
                SELECT * FROM operations
                WHERE principal_ref = ? AND tool = ? AND logical_target = ? AND idempotency_key = ?
                """,
                (requested_principal, tool_value, target, key),
            ).fetchone()
            if scoped is not None:
                if (
                    scoped["parent_operation_id"] != parent_id
                    or str(scoped["item_id"]) != item
                    or str(scoped["semantic_payload_hash"]) != payload_hash
                ):
                    raise HubStoreV2Conflict("child_operation_payload_conflict")
                replay = self._operation_from_row(scoped)
                replay["idempotent_replay"] = True
                return replay

            now = self._clock()
            connection.execute(
                """
                INSERT INTO operations
                    (operation_id, principal_ref, tool, logical_target, idempotency_key,
                     semantic_payload_hash, state, revision, parent_operation_id, item_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'created', 1, ?, ?, ?, ?)
                """,
                (
                    requested_id,
                    requested_principal,
                    tool_value,
                    target,
                    key,
                    payload_hash,
                    parent_id,
                    item,
                    now,
                    now,
                ),
            )
            self.store._append_event_in_transaction(
                connection,
                "operation.created",
                {"state": "created", "parent_operation_id": parent_id, "item_id": item},
                operation_id=requested_id,
            )
            saved = self._operation_row(connection, requested_id)
            result = self._operation_from_row(saved)
            result["idempotent_replay"] = False
            return result

    def list_child_operations(
        self, parent_operation_id: str, *, principal_ref: str = ""
    ) -> list[dict[str, Any]]:
        principal = principal_ref or self.store.principal_ref
        parent_id = _clean(parent_operation_id, "parent_operation_id")
        parent = self.store.get_operation(parent_id)
        if parent is None or parent["principal_ref"] != principal:
            raise KeyError(f"Unknown operation: {parent_id}")
        rows = self.store.connection.execute(
            """
            SELECT * FROM operations
            WHERE parent_operation_id = ? AND principal_ref = ?
            ORDER BY item_id, created_at, operation_id
            """,
            (parent_id, principal),
        ).fetchall()
        return [self._operation_from_row(row) for row in rows]

    def transition_operation(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        state: str,
        principal_ref: str = "",
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """CAS one operation transition using the resolved V2 state machine."""

        principal = principal_ref or self.store.principal_ref
        operation_value = _clean(operation_id, "operation_id")
        with self.store.immediate_transaction() as connection:
            row = self._visible_operation_in_transaction(
                connection, operation_value, principal
            )
            if int(row["revision"]) != int(expected_revision):
                return None
            saved = self._transition_operation_in_transaction(
                connection,
                row,
                _clean(state, "operation state"),
                result=result,
                error=error,
            )
            if (
                str(saved["state"]) in TERMINAL_OPERATION_STATES
                and saved["parent_operation_id"]
            ):
                self._aggregate_parent_in_transaction(
                    connection, str(saved["parent_operation_id"])
                )
            return self._operation_from_row(saved)

    def prepare_operation(
        self, operation_id: str, *, expected_revision: int, principal_ref: str = ""
    ) -> dict[str, Any] | None:
        return self.transition_operation(
            operation_id,
            expected_revision=expected_revision,
            state="payload_ready",
            principal_ref=principal_ref,
        )

    def make_dispatchable(
        self, operation_id: str, *, expected_revision: int, principal_ref: str = ""
    ) -> dict[str, Any] | None:
        return self.transition_operation(
            operation_id,
            expected_revision=expected_revision,
            state="dispatchable",
            principal_ref=principal_ref,
        )

    def cancel_operation(
        self,
        operation_id: str,
        *,
        expected_revision: int,
        principal_ref: str = "",
        reason: str = "cancelled",
    ) -> dict[str, Any] | None:
        normalized = public_envelope(
            "blocked", result={"reason": reason or "cancelled"}
        )
        return self.transition_operation(
            operation_id,
            expected_revision=expected_revision,
            state="cancelled",
            principal_ref=principal_ref,
            result=normalized,
        )

    def register_payload(
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
        principal_ref: str = "",
    ) -> dict[str, Any]:
        """Persist retry-stable transient payload metadata and mark it ready."""

        operation_value = _clean(operation_id, "operation_id")
        principal = principal_ref or self.store.principal_ref
        kind = _clean(payload_kind, "payload_kind")
        checksum = _clean(checksum_sha256, "checksum_sha256")
        storage = _clean(storage_ref, "storage_ref")
        size = int(size_bytes)
        if size < 0:
            raise ValueError("size_bytes must be non-negative")
        payload_value = (
            payload_id
            or "payload_"
            + semantic_payload_hash(
                {
                    "operation_id": operation_value,
                    "payload_kind": kind,
                    "checksum_sha256": checksum,
                    "storage_ref": storage,
                }
            )[:32]
        )
        encoded_metadata = _encode_json(metadata or {}, "payload metadata")

        with self.store.immediate_transaction() as connection:
            operation = self._visible_operation_in_transaction(
                connection, operation_value, principal
            )
            existing = connection.execute(
                "SELECT * FROM payload_metadata WHERE payload_id = ?", (payload_value,)
            ).fetchone()
            if existing is not None:
                equivalent = (
                    str(existing["operation_id"]) == operation_value
                    and str(existing["payload_kind"]) == kind
                    and str(existing["checksum_sha256"]) == checksum
                    and int(existing["size_bytes"]) == size
                    and str(existing["storage_ref"]) == storage
                    and existing["expires_at"] == expires_at
                    and str(existing["metadata_json"]) == encoded_metadata
                )
                if not equivalent:
                    raise HubStoreV2Conflict("payload_metadata_conflict")
                replay = self._payload_from_row(existing)
                replay["idempotent_replay"] = True
                return replay

            now = self._clock()
            connection.execute(
                """
                INSERT INTO payload_metadata
                    (payload_id, operation_id, payload_kind, checksum_sha256, size_bytes,
                     storage_ref, status, revision, expires_at, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ready', 1, ?, ?, ?, ?)
                """,
                (
                    payload_value,
                    operation_value,
                    kind,
                    checksum,
                    size,
                    storage,
                    expires_at,
                    encoded_metadata,
                    now,
                    now,
                ),
            )
            if str(operation["state"]) == "created":
                operation = self._transition_operation_in_transaction(
                    connection, operation, "payload_ready"
                )
            elif str(operation["state"]) != "payload_ready":
                raise HubStoreV2StateError(
                    f"Cannot register payload while operation is {operation['state']}"
                )
            self.store._append_event_in_transaction(
                connection,
                "operation.payload_ready",
                {"payload_id": payload_value, "payload_kind": kind},
                operation_id=operation_value,
            )
            saved = connection.execute(
                "SELECT * FROM payload_metadata WHERE payload_id = ?", (payload_value,)
            ).fetchone()
            result = self._payload_from_row(saved)
            result["idempotent_replay"] = False
            return result

    def acknowledge_payload(
        self,
        payload_id: str,
        *,
        expected_revision: int,
        principal_ref: str = "",
    ) -> dict[str, Any] | None:
        principal = principal_ref or self.store.principal_ref
        payload_value = _clean(payload_id, "payload_id")
        with self.store.immediate_transaction() as connection:
            row = connection.execute(
                """
                SELECT p.*, o.principal_ref
                FROM payload_metadata AS p
                JOIN operations AS o ON o.operation_id = p.operation_id
                WHERE p.payload_id = ?
                """,
                (payload_value,),
            ).fetchone()
            if row is None or str(row["principal_ref"]) != principal:
                raise KeyError(f"Unknown payload: {payload_value}")
            if str(row["status"]) == "acknowledged":
                replay = self._payload_from_row(row)
                replay["idempotent_replay"] = True
                return replay
            if int(row["revision"]) != int(expected_revision):
                return None
            if str(row["status"]) != "ready":
                raise HubStoreV2StateError(
                    f"Cannot acknowledge payload in state {row['status']}"
                )
            now = self._clock()
            connection.execute(
                """
                UPDATE payload_metadata
                SET status = 'acknowledged', revision = revision + 1,
                    acknowledged_at = ?, updated_at = ?
                WHERE payload_id = ? AND revision = ? AND status = 'ready'
                """,
                (now, now, payload_value, expected_revision),
            )
            self.store._append_event_in_transaction(
                connection,
                "operation.payload_acknowledged",
                {"payload_id": payload_value},
                operation_id=str(row["operation_id"]),
            )
            saved = connection.execute(
                "SELECT * FROM payload_metadata WHERE payload_id = ?", (payload_value,)
            ).fetchone()
            result = self._payload_from_row(saved)
            result["idempotent_replay"] = False
            return result

    def expire_payloads(
        self, *, now: float | None = None, limit: int = 1_000
    ) -> list[dict[str, Any]]:
        cutoff = self._clock() if now is None else float(now)
        bounded_limit = max(1, min(int(limit), 10_000))
        expired: list[dict[str, Any]] = []
        with self.store.immediate_transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM payload_metadata
                WHERE status = 'ready' AND expires_at IS NOT NULL AND expires_at <= ?
                ORDER BY expires_at, payload_id LIMIT ?
                """,
                (cutoff, bounded_limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE payload_metadata
                    SET status = 'expired', revision = revision + 1, updated_at = ?
                    WHERE payload_id = ? AND revision = ? AND status = 'ready'
                    """,
                    (cutoff, row["payload_id"], row["revision"]),
                )
                self.store._append_event_in_transaction(
                    connection,
                    "operation.payload_expired",
                    {"payload_id": str(row["payload_id"])},
                    operation_id=str(row["operation_id"]),
                )
                saved = connection.execute(
                    "SELECT * FROM payload_metadata WHERE payload_id = ?",
                    (row["payload_id"],),
                ).fetchone()
                expired.append(self._payload_from_row(saved))
        return expired

    def offer_attempt(
        self,
        operation_id: str,
        *,
        machine_id: str,
        edge_generation: int,
        required_contract_hash: str,
        principal_ref: str = "",
        attempt_id: str = "",
    ) -> dict[str, Any]:
        """Offer one immutable attempt, reusing an equivalent active offer."""

        operation_value = _clean(operation_id, "operation_id")
        machine = _clean(machine_id, "machine_id")
        generation = _generation(edge_generation)
        contract_hash = _clean(required_contract_hash, "required_contract_hash")
        principal = principal_ref or self.store.principal_ref

        with self.store.immediate_transaction() as connection:
            operation = self._visible_operation_in_transaction(
                connection, operation_value, principal
            )
            if str(operation["state"]) not in {
                "dispatchable",
                "running",
                "reconciling",
            }:
                raise HubStoreV2StateError(
                    f"Cannot offer an attempt while operation is {operation['state']}"
                )

            active_rows = connection.execute(
                f"""
                SELECT * FROM attempts
                WHERE operation_id = ? AND state IN ({_sql_placeholders(ACTIVE_ATTEMPT_STATES)})
                ORDER BY fencing_token DESC
                """,
                (operation_value, *sorted(ACTIVE_ATTEMPT_STATES)),
            ).fetchall()
            if active_rows:
                active = active_rows[0]
                contract = self._attempt_contract_in_transaction(
                    connection, str(active["attempt_id"])
                )
                equivalent = (
                    str(active["machine_id"]) == machine
                    and int(active["edge_generation"]) == generation
                    and contract["required_contract_hash"] == contract_hash
                )
                if equivalent:
                    replay = self._attempt_with_contract(active, contract)
                    replay["idempotent_replay"] = True
                    return replay
                raise HubStoreV2Conflict("operation_attempt_already_active")

            attempt_value = attempt_id or f"attempt_{secrets.token_hex(16)}"
            token = int(
                connection.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) + 1 FROM attempts WHERE operation_id = ?",
                    (operation_value,),
                ).fetchone()[0]
            )
            now = self._clock()
            connection.execute(
                """
                INSERT INTO attempts
                    (attempt_id, operation_id, machine_id, edge_generation, fencing_token,
                     state, revision, lease_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'offered', 1, NULL, ?, ?)
                """,
                (attempt_value, operation_value, machine, generation, token, now, now),
            )
            contract = {
                "attempt_id": attempt_value,
                "operation_id": operation_value,
                "machine_id": machine,
                "edge_generation": generation,
                "required_contract_hash": contract_hash,
                "fencing_token": token,
            }
            connection.execute(
                """
                INSERT INTO entity_records
                    (entity_type, entity_id, revision, record_json, legacy_classification,
                     source_import_id, created_at, updated_at)
                VALUES (?, ?, 1, ?, '', NULL, ?, ?)
                """,
                (
                    ATTEMPT_CONTRACT_ENTITY_TYPE,
                    attempt_value,
                    _encode_json(contract, "attempt contract"),
                    now,
                    now,
                ),
            )
            self.store._append_event_in_transaction(
                connection,
                "operation.attempt_offered",
                {
                    "attempt_id": attempt_value,
                    "machine_id": machine,
                    "edge_generation": generation,
                    "required_contract_hash": contract_hash,
                    "fencing_token": token,
                },
                operation_id=operation_value,
                entity_revision=1,
            )
            row = self._attempt_row(connection, attempt_value)
            result = self._attempt_with_contract(row, contract)
            result["idempotent_replay"] = False
            return result

    def claim_attempt(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
        lease_seconds: float | None = None,
        principal_ref: str = "",
    ) -> dict[str, Any]:
        """Claim an offered attempt after repeating generation/contract fences."""

        principal = principal_ref or self.store.principal_ref
        lease_duration = self._lease_duration(lease_seconds)
        conflict: str | None = None
        result: dict[str, Any] | None = None
        with self.store.immediate_transaction() as connection:
            operation, attempt, contract = self._attempt_context_in_transaction(
                connection, operation_id, attempt_id, principal
            )
            self._require_attempt_identity(
                connection,
                operation,
                attempt,
                contract,
                machine_id=machine_id,
                edge_generation=edge_generation,
                contract_hash=contract_hash,
                fencing_token=fencing_token,
            )
            now = self._clock()
            state = str(attempt["state"])
            if state in LEASED_ATTEMPT_STATES and self._attempt_lease_expired(
                attempt, now
            ):
                self._expire_attempt_in_transaction(connection, operation, attempt, now)
                conflict = "attempt_lease_expired"
            elif state == "offered":
                if str(operation["state"]) not in {
                    "dispatchable",
                    "running",
                    "reconciling",
                }:
                    raise HubStoreV2StateError(
                        f"Cannot claim while operation is {operation['state']}"
                    )
                lease_expires_at = now + lease_duration
                connection.execute(
                    """
                    UPDATE attempts
                    SET state = 'claimed', revision = revision + 1,
                        lease_expires_at = ?, updated_at = ?
                    WHERE attempt_id = ? AND revision = ? AND state = 'offered'
                    """,
                    (lease_expires_at, now, attempt["attempt_id"], attempt["revision"]),
                )
                if str(operation["state"]) == "dispatchable":
                    operation = self._transition_operation_in_transaction(
                        connection, operation, "running"
                    )
                self.store._append_event_in_transaction(
                    connection,
                    "operation.attempt_claimed",
                    {
                        "attempt_id": str(attempt["attempt_id"]),
                        "fencing_token": int(attempt["fencing_token"]),
                        "lease_expires_at": lease_expires_at,
                    },
                    operation_id=str(operation["operation_id"]),
                    entity_revision=int(attempt["revision"]) + 1,
                )
                saved = self._attempt_row(connection, str(attempt["attempt_id"]))
                result = self._attempt_with_contract(saved, contract)
                result["idempotent_replay"] = False
                result["operation_revision"] = int(operation["revision"])
            elif state in {
                "claimed",
                "executing",
                "effect_recorded",
                "result_ready",
                "acknowledged",
            }:
                result = self._attempt_with_contract(attempt, contract)
                result["idempotent_replay"] = True
                result["operation_revision"] = int(operation["revision"])
            else:
                raise HubStoreV2StateError(f"Cannot claim attempt in state {state}")
        if conflict:
            raise HubStoreV2Conflict(conflict)
        if result is None:
            raise HubStoreV2Corrupt("Attempt claim completed without a durable result")
        return result

    def transition_attempt(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
        state: str,
        principal_ref: str = "",
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """CAS one attempt state change after all Edge identity fences pass."""

        principal = principal_ref or self.store.principal_ref
        with self.store.immediate_transaction() as connection:
            operation, attempt, contract = self._attempt_context_in_transaction(
                connection, operation_id, attempt_id, principal
            )
            self._require_attempt_identity(
                connection,
                operation,
                attempt,
                contract,
                machine_id=machine_id,
                edge_generation=edge_generation,
                contract_hash=contract_hash,
                fencing_token=fencing_token,
            )
            if int(attempt["revision"]) != int(expected_revision):
                return None
            target = _clean(state, "attempt state")
            current = str(attempt["state"])
            if target == current:
                replay = self._attempt_with_contract(attempt, contract)
                replay["idempotent_replay"] = True
                return replay
            try:
                require_attempt_transition(current, target)
            except ValueError as exc:
                raise HubStoreV2StateError(str(exc)) from exc
            if current in LEASED_ATTEMPT_STATES and self._attempt_lease_expired(
                attempt, self._clock()
            ):
                raise HubStoreV2StateError("Attempt lease has expired")
            result_json = (
                attempt["result_json"]
                if result is None
                else _encode_json(result, "attempt result")
            )
            now = self._clock()
            cursor = connection.execute(
                """
                UPDATE attempts
                SET state = ?, revision = revision + 1, result_json = ?, updated_at = ?
                WHERE attempt_id = ? AND revision = ? AND fencing_token = ? AND state = ?
                """,
                (
                    target,
                    result_json,
                    now,
                    attempt["attempt_id"],
                    expected_revision,
                    fencing_token,
                    current,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self.store._append_event_in_transaction(
                connection,
                "operation.attempt_state_changed",
                {
                    "attempt_id": str(attempt["attempt_id"]),
                    "from": current,
                    "to": target,
                },
                operation_id=str(operation["operation_id"]),
                entity_revision=int(expected_revision) + 1,
            )
            saved = self._attempt_row(connection, str(attempt["attempt_id"]))
            response = self._attempt_with_contract(saved, contract)
            response["idempotent_replay"] = False
            return response

    def mark_attempt_executing(
        self, operation_id: str, attempt_id: str, **fences: Any
    ) -> dict[str, Any] | None:
        return self.transition_attempt(
            operation_id, attempt_id, state="executing", **fences
        )

    def mark_effect_recorded(
        self, operation_id: str, attempt_id: str, **fences: Any
    ) -> dict[str, Any] | None:
        return self.transition_attempt(
            operation_id, attempt_id, state="effect_recorded", **fences
        )

    def renew_lease(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
        lease_seconds: float | None = None,
        principal_ref: str = "",
    ) -> dict[str, Any] | None:
        """CAS a lease renewal without changing attempt identity or state."""

        principal = principal_ref or self.store.principal_ref
        duration = self._lease_duration(lease_seconds)
        conflict: str | None = None
        response: dict[str, Any] | None = None
        with self.store.immediate_transaction() as connection:
            operation, attempt, contract = self._attempt_context_in_transaction(
                connection, operation_id, attempt_id, principal
            )
            self._require_attempt_identity(
                connection,
                operation,
                attempt,
                contract,
                machine_id=machine_id,
                edge_generation=edge_generation,
                contract_hash=contract_hash,
                fencing_token=fencing_token,
            )
            if int(attempt["revision"]) != int(expected_revision):
                return None
            if str(attempt["state"]) not in LEASED_ATTEMPT_STATES:
                raise HubStoreV2StateError(
                    f"Cannot renew lease in attempt state {attempt['state']}"
                )
            now = self._clock()
            if self._attempt_lease_expired(attempt, now):
                self._expire_attempt_in_transaction(connection, operation, attempt, now)
                conflict = "attempt_lease_expired"
            else:
                lease_expires_at = now + duration
                cursor = connection.execute(
                    """
                    UPDATE attempts
                    SET revision = revision + 1, lease_expires_at = ?, updated_at = ?
                    WHERE attempt_id = ? AND revision = ? AND fencing_token = ? AND state = ?
                    """,
                    (
                        lease_expires_at,
                        now,
                        attempt["attempt_id"],
                        expected_revision,
                        fencing_token,
                        attempt["state"],
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                self.store._append_event_in_transaction(
                    connection,
                    "operation.attempt_lease_renewed",
                    {
                        "attempt_id": str(attempt["attempt_id"]),
                        "lease_expires_at": lease_expires_at,
                    },
                    operation_id=str(operation["operation_id"]),
                    entity_revision=int(expected_revision) + 1,
                )
                saved = self._attempt_row(connection, str(attempt["attempt_id"]))
                response = self._attempt_with_contract(saved, contract)
        if conflict:
            raise HubStoreV2Conflict(conflict)
        return response

    def expire_leases(
        self,
        *,
        now: float | None = None,
        operation_id: str = "",
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        """Expire due active leases and conservatively mark outcomes unknown."""

        cutoff = self._clock() if now is None else float(now)
        bounded_limit = max(1, min(int(limit), 10_000))
        parameters: list[Any] = [*sorted(LEASED_ATTEMPT_STATES), cutoff]
        operation_clause = ""
        if operation_id:
            operation_clause = " AND operation_id = ?"
            parameters.append(_clean(operation_id, "operation_id"))
        parameters.append(bounded_limit)
        expired: list[dict[str, Any]] = []

        with self.store.immediate_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM attempts
                WHERE state IN ({_sql_placeholders(LEASED_ATTEMPT_STATES)})
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                  {operation_clause}
                ORDER BY lease_expires_at, attempt_id LIMIT ?
                """,
                parameters,
            ).fetchall()
            for attempt in rows:
                operation = self._operation_row(
                    connection, str(attempt["operation_id"])
                )
                saved = self._expire_attempt_in_transaction(
                    connection, operation, attempt, cutoff
                )
                contract = self._attempt_contract_in_transaction(
                    connection, str(attempt["attempt_id"])
                )
                expired.append(self._attempt_with_contract(saved, contract))
        return expired

    def begin_reconciliation(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
        principal_ref: str = "",
    ) -> dict[str, Any] | None:
        """Move one expired attempt and its unknown operation into reconciliation."""

        principal = principal_ref or self.store.principal_ref
        with self.store.immediate_transaction() as connection:
            operation, attempt, contract = self._attempt_context_in_transaction(
                connection, operation_id, attempt_id, principal
            )
            self._require_attempt_identity(
                connection,
                operation,
                attempt,
                contract,
                machine_id=machine_id,
                edge_generation=edge_generation,
                contract_hash=contract_hash,
                fencing_token=fencing_token,
            )
            if int(attempt["revision"]) != int(expected_revision):
                return None
            if str(attempt["state"]) == "reconciling":
                replay = self._attempt_with_contract(attempt, contract)
                replay["idempotent_replay"] = True
                return replay
            if str(attempt["state"]) != "lease_expired":
                raise HubStoreV2StateError(
                    f"Cannot reconcile attempt in state {attempt['state']}"
                )
            now = self._clock()
            connection.execute(
                """
                UPDATE attempts
                SET state = 'reconciling', revision = revision + 1, updated_at = ?
                WHERE attempt_id = ? AND revision = ? AND state = 'lease_expired'
                """,
                (now, attempt["attempt_id"], expected_revision),
            )
            if str(operation["state"]) in {"outcome_unknown", "running"}:
                operation = self._transition_operation_in_transaction(
                    connection, operation, "reconciling"
                )
            elif str(operation["state"]) != "reconciling":
                raise HubStoreV2StateError(
                    f"Cannot reconcile operation in state {operation['state']}"
                )
            self.store._append_event_in_transaction(
                connection,
                "operation.attempt_reconciling",
                {"attempt_id": str(attempt["attempt_id"])},
                operation_id=str(operation["operation_id"]),
                entity_revision=int(expected_revision) + 1,
            )
            saved = self._attempt_row(connection, str(attempt["attempt_id"]))
            response = self._attempt_with_contract(saved, contract)
            response["idempotent_replay"] = False
            response["operation_revision"] = int(operation["revision"])
            return response

    # A concise alias for callers which model reconciliation as the lease action.
    reconcile_attempt = begin_reconciliation

    def complete_reconciliation(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        disposition: str,
        expected_revision: int,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
        principal_ref: str = "",
    ) -> dict[str, Any] | None:
        if disposition not in {"retryable", "manual_recovery"}:
            raise ValueError("disposition must be retryable or manual_recovery")
        return self.transition_attempt(
            operation_id,
            attempt_id,
            expected_revision=expected_revision,
            machine_id=machine_id,
            edge_generation=edge_generation,
            contract_hash=contract_hash,
            fencing_token=fencing_token,
            state=disposition,
            principal_ref=principal_ref,
        )

    def finish_operation(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        expected_revision: int | None = None,
        expected_operation_revision: int | None = None,
        expected_attempt_revision: int | None = None,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
        result: Mapping[str, Any] | None = None,
        domain_result: Mapping[str, Any] | None = None,
        transport_error: str = "",
        principal_ref: str = "",
    ) -> dict[str, Any] | None:
        """CAS a normalized Edge receipt into immutable terminal operation state."""

        if result is not None and domain_result is not None:
            raise ValueError("Pass result or domain_result, not both")
        operation_revision = _coalesce_revision(
            expected_revision, expected_operation_revision
        )
        received = domain_result if domain_result is not None else result
        normalized = normalize_domain_result(received, transport_error=transport_error)
        public_status = str(normalized["status"])
        target_state = {
            "ok": "succeeded",
            "partial": "succeeded",
            "not_found": "succeeded",
            "blocked": "blocked",
            "failed": "failed",
            "pending": "outcome_unknown",
        }[public_status]
        normalized_hash = semantic_payload_hash(normalized)
        principal = principal_ref or self.store.principal_ref
        conflict: str | None = None
        response: dict[str, Any] | None = None

        with self.store.immediate_transaction() as connection:
            operation, attempt, contract = self._attempt_context_in_transaction(
                connection, operation_id, attempt_id, principal
            )
            identity_error = self._attempt_identity_error(
                connection,
                operation,
                attempt,
                contract,
                machine_id=machine_id,
                edge_generation=edge_generation,
                contract_hash=contract_hash,
                fencing_token=fencing_token,
            )
            if identity_error:
                self.store._append_event_in_transaction(
                    connection,
                    "operation.stale_receipt_rejected",
                    {
                        "attempt_id": str(attempt["attempt_id"]),
                        "reason": identity_error,
                        "received_result_hash": normalized_hash,
                    },
                    operation_id=str(operation["operation_id"]),
                    entity_revision=int(operation["revision"]),
                )
                conflict = identity_error
            elif str(operation["state"]) in TERMINAL_OPERATION_STATES:
                stored = _decode_optional_json(
                    operation["result_json"],
                    f"operation {operation['operation_id']} result",
                )
                stored_hash = semantic_payload_hash(stored or {})
                equivalent = stored is not None and stored_hash == normalized_hash
                self.store._append_event_in_transaction(
                    connection,
                    (
                        "operation.terminal_receipt_confirmed"
                        if equivalent
                        else "operation.terminal_receipt_conflict"
                    ),
                    {
                        "attempt_id": str(attempt["attempt_id"]),
                        "stored_result_hash": stored_hash,
                        "received_result_hash": normalized_hash,
                    },
                    operation_id=str(operation["operation_id"]),
                    entity_revision=int(operation["revision"]),
                )
                if equivalent:
                    response = self._operation_from_row(operation)
                    response["idempotent_replay"] = True
                    response["receipt_duplicate"] = True
                else:
                    conflict = "conflicting_terminal_receipt"
            elif int(operation["revision"]) != operation_revision:
                return None
            elif expected_attempt_revision is not None and int(
                attempt["revision"]
            ) != int(expected_attempt_revision):
                return None
            elif str(
                attempt["state"]
            ) in LEASED_ATTEMPT_STATES and self._attempt_lease_expired(
                attempt, self._clock()
            ):
                self._expire_attempt_in_transaction(
                    connection, operation, attempt, self._clock()
                )
                self.store._append_event_in_transaction(
                    connection,
                    "operation.stale_receipt_rejected",
                    {
                        "attempt_id": str(attempt["attempt_id"]),
                        "reason": "attempt_lease_expired",
                        "received_result_hash": normalized_hash,
                    },
                    operation_id=str(operation["operation_id"]),
                    entity_revision=int(operation["revision"]),
                )
                conflict = "attempt_lease_expired"
            else:
                attempt_state = str(attempt["state"])
                if attempt_state not in {
                    "executing",
                    "effect_recorded",
                    "reconciling",
                    "result_ready",
                }:
                    raise HubStoreV2StateError(
                        f"Cannot finish from attempt state {attempt_state}"
                    )
                if attempt_state != "result_ready":
                    try:
                        require_attempt_transition(attempt_state, "result_ready")
                    except ValueError as exc:
                        raise HubStoreV2StateError(str(exc)) from exc
                    attempt_cursor = connection.execute(
                        """
                        UPDATE attempts
                        SET state = 'result_ready', revision = revision + 1,
                            result_json = ?, updated_at = ?
                        WHERE attempt_id = ? AND revision = ? AND fencing_token = ? AND state = ?
                        """,
                        (
                            _encode_json(normalized, "normalized result"),
                            self._clock(),
                            attempt["attempt_id"],
                            attempt["revision"],
                            fencing_token,
                            attempt_state,
                        ),
                    )
                    if attempt_cursor.rowcount != 1:
                        return None
                else:
                    stored_attempt_result = _decode_optional_json(
                        attempt["result_json"],
                        f"attempt {attempt['attempt_id']} result",
                    )
                    if (
                        semantic_payload_hash(stored_attempt_result or {})
                        != normalized_hash
                    ):
                        self.store._append_event_in_transaction(
                            connection,
                            "operation.terminal_receipt_conflict",
                            {
                                "attempt_id": str(attempt["attempt_id"]),
                                "stored_result_hash": semantic_payload_hash(
                                    stored_attempt_result or {}
                                ),
                                "received_result_hash": normalized_hash,
                            },
                            operation_id=str(operation["operation_id"]),
                            entity_revision=int(operation["revision"]),
                        )
                        conflict = "conflicting_attempt_receipt"

                if not conflict:
                    try:
                        require_operation_transition(
                            str(operation["state"]), target_state
                        )
                    except ValueError as exc:
                        raise HubStoreV2StateError(str(exc)) from exc
                    now = self._clock()
                    operation_cursor = connection.execute(
                        """
                        UPDATE operations
                        SET state = ?, revision = revision + 1, result_json = ?, updated_at = ?
                        WHERE operation_id = ? AND revision = ? AND state = ?
                        """,
                        (
                            target_state,
                            _encode_json(normalized, "normalized result"),
                            now,
                            operation["operation_id"],
                            operation_revision,
                            operation["state"],
                        ),
                    )
                    if operation_cursor.rowcount != 1:
                        return None
                    self.store._append_event_in_transaction(
                        connection,
                        "operation.result_ready",
                        {
                            "attempt_id": str(attempt["attempt_id"]),
                            "fencing_token": int(fencing_token),
                            "public_status": public_status,
                            "result_hash": normalized_hash,
                        },
                        operation_id=str(operation["operation_id"]),
                        entity_revision=operation_revision + 1,
                    )
                    saved = self._operation_row(
                        connection, str(operation["operation_id"])
                    )
                    if saved["parent_operation_id"]:
                        self._aggregate_parent_in_transaction(
                            connection, str(saved["parent_operation_id"])
                        )
                    response = self._operation_from_row(saved)
                    response["idempotent_replay"] = False
                    response["receipt_duplicate"] = False

        if conflict:
            raise HubStoreV2Conflict(conflict)
        return response

    # ``finish`` is the concise Edge-facing spelling.
    finish = finish_operation

    def acknowledge_result(
        self,
        operation_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
        principal_ref: str = "",
    ) -> dict[str, Any] | None:
        return self.transition_attempt(
            operation_id,
            attempt_id,
            expected_revision=expected_revision,
            machine_id=machine_id,
            edge_generation=edge_generation,
            contract_hash=contract_hash,
            fencing_token=fencing_token,
            state="acknowledged",
            principal_ref=principal_ref,
        )

    def aggregate_parent(
        self,
        parent_operation_id: str,
        *,
        principal_ref: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        """CAS a compound parent once every child has a terminal result."""

        parent_id = _clean(parent_operation_id, "parent_operation_id")
        principal = principal_ref or self.store.principal_ref
        with self.store.immediate_transaction() as connection:
            parent = self._visible_operation_in_transaction(
                connection, parent_id, principal
            )
            if expected_revision is not None and int(parent["revision"]) != int(
                expected_revision
            ):
                return None
            saved = self._aggregate_parent_in_transaction(connection, parent_id)
            response = self._operation_from_row(saved)
            children = connection.execute(
                """
                SELECT operation_id, item_id, state FROM operations
                WHERE parent_operation_id = ? ORDER BY item_id, operation_id
                """,
                (parent_id,),
            ).fetchall()
            response["children_terminal"] = bool(children) and all(
                str(child["state"]) in TERMINAL_OPERATION_STATES for child in children
            )
            return response

    async def wait_for_event_revision(
        self,
        operation_id: str,
        *,
        after_revision: int,
        timeout_seconds: float,
        principal_ref: str = "",
    ) -> int | None:
        """Wait for a committed operation event without holding a transaction."""

        operation_value = _clean(operation_id, "operation_id")
        principal = principal_ref or self.store.principal_ref
        if not self._operation_visible(operation_value, principal):
            return None
        cursor = max(0, int(after_revision))
        timeout = min(
            _non_negative_duration(timeout_seconds, "timeout_seconds"),
            self.max_wait_seconds,
        )
        deadline = self._monotonic() + timeout
        while True:
            latest = self._latest_event_revision(operation_value)
            if latest > cursor:
                return latest
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            await self._sleep(min(self.poll_interval, remaining))

    async def operation_status(
        self,
        operation_id: str,
        *,
        principal_ref: str = "",
        wait_seconds: float = 0,
        include_result: bool = False,
        since_revision: int = 0,
    ) -> dict[str, Any]:
        """Return the canonical visible operation recovery envelope."""

        operation_value = _clean(operation_id, "operation_id")
        principal = principal_ref or self.store.principal_ref
        operation = self.store.get_operation(operation_value)
        if operation is None or operation["principal_ref"] != principal:
            return public_envelope(
                "not_found", result={"reason": "operation_not_found"}
            )

        latest_event = self._latest_event_revision(operation_value)
        if wait_seconds and latest_event <= max(0, int(since_revision)):
            await self.wait_for_event_revision(
                operation_value,
                after_revision=since_revision,
                timeout_seconds=wait_seconds,
                principal_ref=principal,
            )
            operation = self.store.get_operation(operation_value)
            if operation is None or operation["principal_ref"] != principal:
                return public_envelope(
                    "not_found", result={"reason": "operation_not_found"}
                )
            latest_event = self._latest_event_revision(operation_value)

        attempt_row = self.store.connection.execute(
            """
            SELECT * FROM attempts WHERE operation_id = ?
            ORDER BY fencing_token DESC, created_at DESC LIMIT 1
            """,
            (operation_value,),
        ).fetchone()
        attempt = self._public_attempt(attempt_row) if attempt_row is not None else {}
        children = self.store.connection.execute(
            """
            SELECT operation_id, item_id, state, revision, result_json
            FROM operations WHERE parent_operation_id = ?
            ORDER BY item_id, operation_id
            """,
            (operation_value,),
        ).fetchall()
        child_summaries = [
            {
                "operation_id": str(child["operation_id"]),
                "item_id": str(child["item_id"]),
                "state": str(child["state"]),
                "revision": int(child["revision"]),
                "status": self._public_status_for_row(child),
            }
            for child in children
        ]
        normalized = operation.get("result")
        public_status = self._public_status_for_operation(operation)
        safe_next_action = self._safe_next_action(str(operation["state"]))
        operation_summary = {
            "operation_id": operation_value,
            "parent_operation_id": operation["parent_operation_id"],
            "item_id": operation["item_id"],
            "state": operation["state"],
            "revision": operation["revision"],
            "event_revision": latest_event,
            "updated_at": operation["updated_at"],
        }
        receipt_state = "absent"
        if attempt:
            receipt_state = {
                "result_ready": "available",
                "acknowledged": "acknowledged",
                "lease_expired": "unknown",
                "reconciling": "reconciling",
            }.get(str(attempt["state"]), "pending")
        status_result: dict[str, Any] = {
            "dispatch": {
                "state": self._dispatch_state(str(operation["state"])),
                "event_revision": latest_event,
            },
            "outcome": {
                "state": operation["state"],
                "terminal": operation["state"] in TERMINAL_OPERATION_STATES,
                "status": public_status,
            },
            "attempt": attempt,
            "receipt": {"state": receipt_state},
            "domain_result": {},
            "safe_next_action": safe_next_action,
        }
        if child_summaries:
            status_result["children"] = child_summaries
        warnings: list[Any] = []
        next_actions: list[Any] = []
        if isinstance(normalized, Mapping):
            if include_result:
                status_result["domain_result"] = deepcopy(
                    dict(normalized.get("result") or {})
                )
            warnings = deepcopy(list(normalized.get("warnings") or []))
            next_actions = deepcopy(list(normalized.get("next_actions") or []))
        if not next_actions and safe_next_action:
            next_actions = [{"action": safe_next_action}]
        return public_envelope(
            public_status,
            result=status_result,
            operation=operation_summary,
            warnings=warnings,
            next_actions=next_actions,
        )

    status = operation_status

    def _visible_operation_in_transaction(
        self, connection: sqlite3.Connection, operation_id: str, principal_ref: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None or str(row["principal_ref"]) != principal_ref:
            raise KeyError(f"Unknown operation: {operation_id}")
        return row

    @staticmethod
    def _operation_row(
        connection: sqlite3.Connection, operation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise HubStoreV2Corrupt(f"Operation disappeared: {operation_id}")
        return row

    @staticmethod
    def _attempt_row(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise HubStoreV2Corrupt(f"Attempt disappeared: {attempt_id}")
        return row

    def _attempt_context_in_transaction(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        attempt_id: str,
        principal_ref: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any]]:
        operation_value = _clean(operation_id, "operation_id")
        attempt_value = _clean(attempt_id, "attempt_id")
        operation = self._visible_operation_in_transaction(
            connection, operation_value, principal_ref
        )
        attempt = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ? AND operation_id = ?",
            (attempt_value, operation_value),
        ).fetchone()
        if attempt is None:
            raise KeyError(f"Unknown attempt: {attempt_value}")
        contract = self._attempt_contract_in_transaction(connection, attempt_value)
        return operation, attempt, contract

    @staticmethod
    def _attempt_contract_in_transaction(
        connection: sqlite3.Connection, attempt_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT record_json FROM entity_records
            WHERE entity_type = ? AND entity_id = ?
            """,
            (ATTEMPT_CONTRACT_ENTITY_TYPE, attempt_id),
        ).fetchone()
        if row is None:
            raise HubStoreV2Corrupt(f"Attempt contract is missing: {attempt_id}")
        return _decode_json(str(row["record_json"]), f"attempt contract {attempt_id}")

    def _require_attempt_identity(
        self,
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
        attempt: sqlite3.Row,
        contract: Mapping[str, Any],
        *,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
    ) -> None:
        error = self._attempt_identity_error(
            connection,
            operation,
            attempt,
            contract,
            machine_id=machine_id,
            edge_generation=edge_generation,
            contract_hash=contract_hash,
            fencing_token=fencing_token,
        )
        if error:
            raise HubStoreV2Conflict(error)

    @staticmethod
    def _attempt_identity_error(
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
        attempt: sqlite3.Row,
        contract: Mapping[str, Any],
        *,
        machine_id: str,
        edge_generation: int,
        contract_hash: str,
        fencing_token: int,
    ) -> str:
        if str(attempt["operation_id"]) != str(operation["operation_id"]):
            return "attempt_operation_mismatch"
        if str(attempt["machine_id"]) != _clean(machine_id, "machine_id"):
            return "attempt_machine_mismatch"
        if int(attempt["edge_generation"]) != _generation(edge_generation):
            return "edge_generation_mismatch"
        if str(contract.get("required_contract_hash") or "") != _clean(
            contract_hash, "contract_hash"
        ):
            return "edge_contract_mismatch"
        if int(attempt["fencing_token"]) != int(fencing_token):
            return "stale_fencing_token"
        latest_token = int(
            connection.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) FROM attempts WHERE operation_id = ?",
                (operation["operation_id"],),
            ).fetchone()[0]
        )
        if int(fencing_token) != latest_token:
            return "stale_fencing_token"
        return ""

    def _transition_operation_in_transaction(
        self,
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
        target_state: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> sqlite3.Row:
        current = str(operation["state"])
        if target_state == current:
            return operation
        try:
            require_operation_transition(current, target_state)
        except ValueError as exc:
            raise HubStoreV2StateError(str(exc)) from exc
        result_json = (
            operation["result_json"]
            if result is None
            else _encode_json(result, "operation result")
        )
        error_json = (
            operation["error_json"]
            if error is None
            else _encode_json(error, "operation error")
        )
        cursor = connection.execute(
            """
            UPDATE operations
            SET state = ?, revision = revision + 1, result_json = ?, error_json = ?, updated_at = ?
            WHERE operation_id = ? AND revision = ? AND state = ?
            """,
            (
                target_state,
                result_json,
                error_json,
                self._clock(),
                operation["operation_id"],
                operation["revision"],
                current,
            ),
        )
        if cursor.rowcount != 1:
            raise HubStoreV2Conflict("operation_revision_conflict")
        self.store._append_event_in_transaction(
            connection,
            "operation.state_changed",
            {"from": current, "to": target_state},
            operation_id=str(operation["operation_id"]),
            entity_revision=int(operation["revision"]) + 1,
        )
        return self._operation_row(connection, str(operation["operation_id"]))

    def _expire_attempt_in_transaction(
        self,
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
        attempt: sqlite3.Row,
        now: float,
    ) -> sqlite3.Row:
        current = str(attempt["state"])
        if current == "lease_expired":
            return attempt
        if current not in LEASED_ATTEMPT_STATES:
            raise HubStoreV2StateError(f"Cannot expire attempt in state {current}")
        try:
            require_attempt_transition(current, "lease_expired")
        except ValueError as exc:
            raise HubStoreV2StateError(str(exc)) from exc
        cursor = connection.execute(
            """
            UPDATE attempts
            SET state = 'lease_expired', revision = revision + 1, updated_at = ?
            WHERE attempt_id = ? AND revision = ? AND state = ?
            """,
            (now, attempt["attempt_id"], attempt["revision"], current),
        )
        if cursor.rowcount != 1:
            raise HubStoreV2Conflict("attempt_revision_conflict")
        if str(operation["state"]) == "running":
            operation = self._transition_operation_in_transaction(
                connection, operation, "outcome_unknown"
            )
        self.store._append_event_in_transaction(
            connection,
            "operation.attempt_lease_expired",
            {
                "attempt_id": str(attempt["attempt_id"]),
                "fencing_token": int(attempt["fencing_token"]),
                "lease_expires_at": attempt["lease_expires_at"],
            },
            operation_id=str(operation["operation_id"]),
            entity_revision=int(attempt["revision"]) + 1,
        )
        return self._attempt_row(connection, str(attempt["attempt_id"]))

    @staticmethod
    def _attempt_lease_expired(attempt: Mapping[str, Any], now: float) -> bool:
        expires_at = attempt["lease_expires_at"]
        return expires_at is not None and float(expires_at) <= float(now)

    def _aggregate_parent_in_transaction(
        self, connection: sqlite3.Connection, parent_operation_id: str
    ) -> sqlite3.Row:
        parent = self._operation_row(connection, parent_operation_id)
        if str(parent["state"]) in TERMINAL_OPERATION_STATES:
            return parent
        children = connection.execute(
            """
            SELECT * FROM operations WHERE parent_operation_id = ?
            ORDER BY item_id, operation_id
            """,
            (parent_operation_id,),
        ).fetchall()
        if not children or any(
            str(child["state"]) not in TERMINAL_OPERATION_STATES for child in children
        ):
            return parent

        items: list[dict[str, Any]] = []
        statuses: list[str] = []
        for child in children:
            normalized = _decode_optional_json(
                child["result_json"], f"operation {child['operation_id']} result"
            )
            status = self._public_status_for_row(child)
            statuses.append(status)
            items.append(
                {
                    "item_id": str(child["item_id"]),
                    "operation_id": str(child["operation_id"]),
                    "state": str(child["state"]),
                    "status": status,
                    "result": (
                        deepcopy(dict(normalized.get("result") or {}))
                        if isinstance(normalized, Mapping)
                        else {}
                    ),
                }
            )
        aggregate_status = statuses[0] if len(set(statuses)) == 1 else "partial"
        if aggregate_status == "pending":
            aggregate_status = "partial"
        normalized_parent = public_envelope(
            aggregate_status,
            result={
                "items": items,
                "total": len(items),
                "succeeded": sum(status == "ok" for status in statuses),
            },
        )
        terminal_state = {
            "ok": "succeeded",
            "partial": "succeeded",
            "not_found": "succeeded",
            "blocked": "blocked",
            "failed": "failed",
        }[aggregate_status]

        while str(parent["state"]) in {"created", "payload_ready", "dispatchable"}:
            next_state = {
                "created": "payload_ready",
                "payload_ready": "dispatchable",
                "dispatchable": "running",
            }[str(parent["state"])]
            parent = self._transition_operation_in_transaction(
                connection, parent, next_state
            )
        if str(parent["state"]) == "outcome_unknown":
            parent = self._transition_operation_in_transaction(
                connection, parent, "reconciling"
            )
        if str(parent["state"]) not in {"running", "reconciling"}:
            raise HubStoreV2StateError(
                f"Cannot aggregate parent in state {parent['state']}"
            )
        parent = self._transition_operation_in_transaction(
            connection, parent, terminal_state, result=normalized_parent
        )
        self.store._append_event_in_transaction(
            connection,
            "operation.parent_aggregated",
            {
                "child_count": len(items),
                "public_status": aggregate_status,
                "child_operation_ids": [item["operation_id"] for item in items],
            },
            operation_id=parent_operation_id,
            entity_revision=int(parent["revision"]),
        )
        return parent

    def _operation_visible(self, operation_id: str, principal_ref: str) -> bool:
        row = self.store.connection.execute(
            "SELECT principal_ref FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return row is not None and str(row["principal_ref"]) == principal_ref

    def _latest_event_revision(self, operation_id: str) -> int:
        row = self.store.connection.execute(
            "SELECT COALESCE(MAX(event_revision), 0) FROM events WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return int(row[0])

    def _lease_duration(self, lease_seconds: float | None) -> float:
        requested = (
            self.default_lease_seconds if lease_seconds is None else lease_seconds
        )
        return _positive_duration(requested, "lease_seconds", maximum=MAX_LEASE_SECONDS)

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> dict[str, Any]:
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
            "result": _decode_optional_json(
                row["result_json"], f"operation {row['operation_id']} result"
            ),
            "error": _decode_optional_json(
                row["error_json"], f"operation {row['operation_id']} error"
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": str(row["attempt_id"]),
            "operation_id": str(row["operation_id"]),
            "machine_id": str(row["machine_id"]),
            "edge_generation": int(row["edge_generation"]),
            "fencing_token": int(row["fencing_token"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "lease_expires_at": row["lease_expires_at"],
            "result": _decode_optional_json(
                row["result_json"], f"attempt {row['attempt_id']} result"
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _attempt_with_contract(
        self, row: sqlite3.Row, contract: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = self._attempt_from_row(row)
        result["required_contract_hash"] = str(contract["required_contract_hash"])
        return result

    @staticmethod
    def _public_attempt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": str(row["attempt_id"]),
            "machine_id": str(row["machine_id"]),
            "edge_generation": int(row["edge_generation"]),
            "fencing_token": int(row["fencing_token"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "lease_expires_at": row["lease_expires_at"],
        }

    @staticmethod
    def _payload_from_row(row: sqlite3.Row) -> dict[str, Any]:
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
            "metadata": _decode_json(
                str(row["metadata_json"]), f"payload {row['payload_id']} metadata"
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _public_status_for_operation(operation: Mapping[str, Any]) -> str:
        state = str(operation["state"])
        normalized = operation.get("result")
        if isinstance(normalized, Mapping) and str(normalized.get("status") or "") in {
            "ok",
            "pending",
            "partial",
            "blocked",
            "failed",
            "not_found",
        }:
            return str(normalized["status"])
        return {
            "succeeded": "ok",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "blocked",
        }.get(state, "pending")

    @staticmethod
    def _public_status_for_row(operation: Mapping[str, Any]) -> str:
        state = str(operation["state"])
        raw = operation["result_json"] if "result_json" in operation.keys() else None
        normalized = _decode_optional_json(
            raw, f"operation {operation['operation_id']} result"
        )
        if isinstance(normalized, Mapping) and str(normalized.get("status") or "") in {
            "ok",
            "pending",
            "partial",
            "blocked",
            "failed",
            "not_found",
        }:
            return str(normalized["status"])
        return {
            "succeeded": "ok",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "blocked",
        }.get(state, "pending")

    @staticmethod
    def _dispatch_state(operation_state: str) -> str:
        return {
            "created": "preparing",
            "payload_ready": "preparing",
            "dispatchable": "offered",
            "running": "claimed",
            "outcome_unknown": "unknown",
            "reconciling": "reconciling",
            "succeeded": "complete",
            "blocked": "complete",
            "failed": "complete",
            "cancelled": "complete",
        }[operation_state]

    @staticmethod
    def _safe_next_action(operation_state: str) -> str:
        return {
            "created": "prepare_payload",
            "payload_ready": "make_dispatchable",
            "dispatchable": "wait_for_edge_claim",
            "running": "wait_for_edge_result",
            "outcome_unknown": "reconcile_before_retry",
            "reconciling": "complete_reconciliation",
            "succeeded": "use_domain_result",
            "blocked": "resolve_blocker_before_retry",
            "failed": "inspect_failure",
            "cancelled": "create_new_operation_if_needed",
        }[operation_state]


def _clean(value: Any, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _generation(value: Any) -> int:
    generation = int(value)
    if generation < 0:
        raise ValueError("edge_generation must be non-negative")
    return generation


def _positive_duration(
    value: Any, field: str, *, maximum: float | None = None
) -> float:
    duration = float(value)
    if duration <= 0:
        raise ValueError(f"{field} must be positive")
    if maximum is not None:
        duration = min(duration, maximum)
    return duration


def _non_negative_duration(value: Any, field: str) -> float:
    duration = float(value)
    if duration < 0:
        raise ValueError(f"{field} must be non-negative")
    return duration


def _coalesce_revision(first: int | None, second: int | None) -> int:
    if first is None and second is None:
        raise ValueError("expected_revision is required")
    if first is not None and second is not None and int(first) != int(second):
        raise ValueError("expected revision values disagree")
    return int(first if first is not None else second)


def _encode_json(value: Mapping[str, Any], field: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    try:
        return json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field} must contain valid JSON values") from exc


def _decode_json(raw: str, context: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HubStoreV2Corrupt(f"Stored JSON is corrupt for {context}") from exc
    if not isinstance(value, dict):
        raise HubStoreV2Corrupt(f"Stored JSON is not an object for {context}")
    return value


def _decode_optional_json(raw: str | None, context: str) -> dict[str, Any] | None:
    return None if raw is None else _decode_json(str(raw), context)


def _sql_placeholders(values: frozenset[str]) -> str:
    return ", ".join("?" for _ in values)


HubOperationBroker = OperationBroker
