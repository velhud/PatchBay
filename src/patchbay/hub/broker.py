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
from patchbay.hub.tool_surface import normalize_hub_v2_next_action


ATTEMPT_CONTRACT_ENTITY_TYPE = "hub.operation_attempt_contract"
OPERATION_GROUP_ENTITY_TYPE = "hub.operation_group"
BATCH_CHILD_MANIFEST_ENTITY_TYPE = "hub.operation_batch_child_manifest"
EDGE_DISPATCH_ENTITY_TYPE = "hub.edge_dispatch"
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
_LATE_RESULT_REPAIRABLE_BLOCKERS = frozenset(
    {
        "edge_attempt_history_unavailable",
        "edge_outcome_unknown_requires_manual_recovery",
    }
)

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
            if (
                concurrent is not None
                and str(concurrent["record"].get("work_group_id") or "") == group_value
            ):
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
            expected_item_ids = self._batch_manifest_item_ids_in_transaction(
                connection, parent_id
            )
            if expected_item_ids is not None and item not in expected_item_ids:
                raise HubStoreV2Conflict("child_operation_not_declared_in_manifest")

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

    def create_batch_operation(
        self,
        *,
        logical_target: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        child_specs: list[Mapping[str, Any]],
        child_dispatch_specs: list[Mapping[str, Any]] | None = None,
        operation_id: str = "",
        principal_ref: str = "",
    ) -> dict[str, Any]:
        """Atomically create or replay a batch parent, manifest, children, and dispatches."""

        target = _clean(logical_target, "logical_target")
        key = _clean(idempotency_key, "idempotency_key")
        principal = principal_ref or self.store.principal_ref
        parent_hash = semantic_payload_hash(payload)
        requested_parent_id = operation_id or f"op_{secrets.token_hex(16)}"
        normalized_specs = self._normalize_batch_child_specs(child_specs)
        normalized_dispatches = self._normalize_batch_child_dispatch_specs(
            child_dispatch_specs, normalized_specs
        )
        dispatches_by_item_id = {
            dispatch["item_id"]: dispatch for dispatch in normalized_dispatches
        }

        with self.store.immediate_transaction() as connection:
            parent = connection.execute(
                """
                SELECT * FROM operations
                WHERE principal_ref = ? AND tool = 'patchbay_worker_start_batch'
                  AND logical_target = ? AND idempotency_key = ?
                """,
                (principal, target, key),
            ).fetchone()
            parent_replay = parent is not None
            if parent is not None:
                if str(parent["semantic_payload_hash"]) != parent_hash:
                    raise HubStoreV2Conflict("idempotency_payload_conflict")
                parent_id = str(parent["operation_id"])
            else:
                parent_id = requested_parent_id
                now = self._clock()
                connection.execute(
                    """
                    INSERT INTO operations
                        (operation_id, principal_ref, tool, logical_target,
                         idempotency_key, semantic_payload_hash, state, revision,
                         parent_operation_id, item_id, created_at, updated_at)
                    VALUES (?, ?, 'patchbay_worker_start_batch', ?, ?, ?,
                            'created', 1, NULL, '', ?, ?)
                    """,
                    (parent_id, principal, target, key, parent_hash, now, now),
                )
                self.store._append_event_in_transaction(
                    connection,
                    "operation.created",
                    {"state": "created"},
                    operation_id=parent_id,
                )
                parent = self._operation_row(connection, parent_id)

            manifest_record = self._batch_manifest_record(parent_id, normalized_specs)
            existing_item_ids = [
                str(row["item_id"])
                for row in connection.execute(
                    """
                    SELECT item_id FROM operations
                    WHERE parent_operation_id = ? ORDER BY item_id, operation_id
                    """,
                    (parent_id,),
                ).fetchall()
            ]
            expected_item_ids = [spec["item_id"] for spec in normalized_specs]
            if len(existing_item_ids) != len(set(existing_item_ids)) or any(
                item_id not in set(expected_item_ids) for item_id in existing_item_ids
            ):
                raise HubStoreV2Conflict("batch_existing_child_set_conflict")
            manifest_row = connection.execute(
                """
                SELECT * FROM entity_records
                WHERE entity_type = ? AND entity_id = ?
                """,
                (BATCH_CHILD_MANIFEST_ENTITY_TYPE, parent_id),
            ).fetchone()
            manifest_replay = manifest_row is not None
            if parent_replay:
                if manifest_row is None:
                    raise HubStoreV2StateError(
                        "legacy_batch_missing_manifest_recovery_required"
                    )
                existing_manifest = self.store._entity_from_row(manifest_row)["record"]
                version = existing_manifest.get("version")
                if version in {1, 2}:
                    if existing_manifest.get("expected_item_ids") != [
                        spec["item_id"] for spec in normalized_specs
                    ]:
                        raise HubStoreV2Conflict("batch_child_manifest_conflict")
                elif existing_manifest != manifest_record:
                    raise HubStoreV2Conflict("batch_child_manifest_conflict")
                if len(existing_item_ids) != len(expected_item_ids) or set(
                    existing_item_ids
                ) != set(expected_item_ids):
                    raise HubStoreV2StateError(
                        "legacy_batch_incomplete_child_set_recovery_required"
                    )
            elif manifest_row is not None:
                raise HubStoreV2Conflict("batch_child_manifest_conflict")
            else:
                saved_manifest = self.store._put_entity_in_transaction(
                    connection,
                    BATCH_CHILD_MANIFEST_ENTITY_TYPE,
                    parent_id,
                    manifest_record,
                    expected_revision=0,
                    legacy_classification="",
                )
                if saved_manifest is None:
                    raise HubStoreV2Conflict("batch_child_manifest_conflict")
                self.store._append_event_in_transaction(
                    connection,
                    "operation.batch_child_manifest_declared",
                    {
                        "expected_item_ids": manifest_record["expected_item_ids"],
                        "expected_child_count": manifest_record["expected_child_count"],
                        "manifest_hash": manifest_record["manifest_hash"],
                        "version": 3,
                    },
                    operation_id=parent_id,
                    entity_revision=int(parent["revision"]),
                )

            children: list[dict[str, Any]] = []
            all_children_replayed = True
            all_dispatches_replayed = True
            existing_child_count = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE parent_operation_id = ?",
                (parent_id,),
            ).fetchone()[0]
            if str(parent["state"]) in TERMINAL_OPERATION_STATES and int(
                existing_child_count
            ) != len(normalized_specs):
                raise HubStoreV2StateError(
                    "terminal_batch_has_incomplete_child_set_recovery_required"
                )
            for spec in normalized_specs:
                child, replayed = self._create_batch_child_in_transaction(
                    connection, parent_id=parent_id, principal=principal, spec=spec
                )
                children.append(child)
                all_children_replayed = all_children_replayed and replayed
                dispatch_spec = dispatches_by_item_id.get(str(spec["item_id"]))
                if dispatch_spec is not None:
                    dispatch_replayed = self._persist_batch_child_dispatch_in_transaction(
                        connection,
                        child=child,
                        dispatch_spec=dispatch_spec,
                        parent_replay=parent_replay,
                    )
                    all_dispatches_replayed = (
                        all_dispatches_replayed and dispatch_replayed
                    )

            result = {
                "parent": self._operation_from_row(
                    self._operation_row(connection, parent_id)
                ),
                "manifest": manifest_record,
                "children": children,
                "idempotent_replay": (
                    parent_replay
                    and manifest_replay
                    and all_children_replayed
                    and all_dispatches_replayed
                ),
            }
            result["parent"]["idempotent_replay"] = parent_replay
            return result

    def _normalize_batch_child_specs(
        self, child_specs: list[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if not child_specs:
            raise ValueError("child_specs must not be empty")
        normalized: list[dict[str, Any]] = []
        for raw_spec in child_specs:
            item_id = _clean(str(raw_spec.get("item_id") or ""), "child item_id")
            tool = _clean(str(raw_spec.get("tool") or ""), "child tool")
            logical_target = _clean(
                str(raw_spec.get("logical_target") or ""), "child logical_target"
            )
            raw_payload = raw_spec.get("payload")
            if not isinstance(raw_payload, Mapping):
                raise ValueError(f"child payload must be an object: {item_id}")
            child_payload = deepcopy(dict(raw_payload))
            normalized.append(
                {
                    "item_id": item_id,
                    "tool": tool,
                    "logical_target": logical_target,
                    "payload": child_payload,
                    "semantic_payload_hash": semantic_payload_hash(child_payload),
                }
            )
        item_ids = [spec["item_id"] for spec in normalized]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("child item_ids must be unique")
        return normalized

    def _normalize_batch_child_dispatch_specs(
        self,
        child_dispatch_specs: list[Mapping[str, Any]] | None,
        child_specs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if child_dispatch_specs is None:
            return []
        child_specs_by_item_id = {spec["item_id"]: spec for spec in child_specs}
        if len(child_dispatch_specs) != len(child_specs_by_item_id):
            raise ValueError("child_dispatch_specs must cover every batch child")
        normalized: list[dict[str, Any]] = []
        for raw_spec in child_dispatch_specs:
            item_id = _clean(
                str(raw_spec.get("item_id") or ""), "dispatch child item_id"
            )
            child_spec = child_specs_by_item_id.get(item_id)
            if child_spec is None:
                raise ValueError(f"dispatch child is not declared in batch: {item_id}")
            raw_payload = raw_spec.get("payload")
            if not isinstance(raw_payload, Mapping):
                raise ValueError(f"dispatch payload must be an object: {item_id}")
            dispatch_payload = deepcopy(dict(raw_payload))
            source_payload_hash = semantic_payload_hash(dispatch_payload)
            if source_payload_hash != child_spec["semantic_payload_hash"]:
                raise HubStoreV2Conflict("batch_child_dispatch_source_payload_conflict")
            action = _clean(
                str(raw_spec.get("action") or dispatch_payload.get("action") or ""),
                "dispatch action",
            )
            normalized.append(
                {
                    "item_id": item_id,
                    "action": action,
                    "payload": dispatch_payload,
                    "payload_hash": semantic_payload_hash(dispatch_payload),
                    "source_payload_hash": source_payload_hash,
                }
            )
        item_ids = [dispatch["item_id"] for dispatch in normalized]
        if len(set(item_ids)) != len(item_ids) or set(item_ids) != set(
            child_specs_by_item_id
        ):
            raise ValueError("child_dispatch_specs must uniquely cover every batch child")
        return normalized

    @staticmethod
    def _batch_manifest_record(
        parent_id: str, child_specs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        child_hashes = [
            {
                "item_id": str(spec["item_id"]),
                "semantic_hash": str(spec["semantic_payload_hash"]),
            }
            for spec in child_specs
        ]
        expected_item_ids = [child["item_id"] for child in child_hashes]
        manifest_hash = semantic_payload_hash(
            {
                "expected_item_ids": expected_item_ids,
                "child_hashes": child_hashes,
            }
        )
        return {
            "version": 3,
            "operation_id": parent_id,
            "expected_item_ids": expected_item_ids,
            "expected_child_count": len(child_hashes),
            "child_hashes": child_hashes,
            "manifest_hash": manifest_hash,
        }

    def _create_batch_child_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        parent_id: str,
        principal: str,
        spec: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        item_id = str(spec["item_id"])
        existing = connection.execute(
            "SELECT * FROM operations WHERE parent_operation_id = ? AND item_id = ?",
            (parent_id, item_id),
        ).fetchone()
        if existing is not None:
            equivalent = (
                str(existing["principal_ref"]) == principal
                and str(existing["tool"]) == spec["tool"]
                and str(existing["logical_target"]) == spec["logical_target"]
                and str(existing["semantic_payload_hash"])
                == spec["semantic_payload_hash"]
            )
            if not equivalent:
                raise HubStoreV2Conflict("child_operation_payload_conflict")
            child = self._operation_from_row(existing)
            child["idempotent_replay"] = True
            return child, True

        child_id = f"op_{secrets.token_hex(16)}"
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
                child_id,
                principal,
                spec["tool"],
                spec["logical_target"],
                self.child_idempotency_key(parent_id, item_id),
                spec["semantic_payload_hash"],
                parent_id,
                item_id,
                now,
                now,
            ),
        )
        self.store._append_event_in_transaction(
            connection,
            "operation.created",
            {"state": "created", "parent_operation_id": parent_id, "item_id": item_id},
            operation_id=child_id,
        )
        child = self._operation_from_row(self._operation_row(connection, child_id))
        child["idempotent_replay"] = False
        return child, False

    def _persist_batch_child_dispatch_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        child: Mapping[str, Any],
        dispatch_spec: Mapping[str, Any],
        parent_replay: bool,
    ) -> bool:
        operation_id = str(child["operation_id"])
        existing = connection.execute(
            """
            SELECT * FROM entity_records
            WHERE entity_type = ? AND entity_id = ?
            """,
            (EDGE_DISPATCH_ENTITY_TYPE, operation_id),
        ).fetchone()
        if existing is not None:
            record = self.store._entity_from_row(existing)["record"]
            stored_payload = record.get("payload")
            equivalent = (
                record.get("operation_id") == operation_id
                and record.get("action") == dispatch_spec["action"]
                and isinstance(stored_payload, Mapping)
                and semantic_payload_hash(stored_payload)
                == dispatch_spec["payload_hash"]
                and record.get("payload_hash") == dispatch_spec["payload_hash"]
                and record.get("source_payload_hash")
                == dispatch_spec["source_payload_hash"]
            )
            if not equivalent:
                raise HubStoreV2Conflict("batch_child_dispatch_payload_conflict")
            return True
        if parent_replay:
            raise HubStoreV2StateError(
                "legacy_batch_missing_child_dispatch_recovery_required"
            )
        record = {
            "operation_id": operation_id,
            "action": dispatch_spec["action"],
            "payload": deepcopy(dict(dispatch_spec["payload"])),
            "payload_hash": dispatch_spec["payload_hash"],
            "source_payload_hash": dispatch_spec["source_payload_hash"],
            "status": "pending",
            "created_at": child.get("created_at") or self._clock(),
        }
        saved = self.store._put_entity_in_transaction(
            connection,
            EDGE_DISPATCH_ENTITY_TYPE,
            operation_id,
            record,
            expected_revision=0,
            legacy_classification="",
        )
        if saved is None:
            raise HubStoreV2Conflict("batch_child_dispatch_payload_conflict")
        return False

    def declare_child_manifest(
        self,
        parent_operation_id: str,
        *,
        expected_item_ids: list[str],
        principal_ref: str = "",
    ) -> dict[str, Any]:
        """Persist one immutable expected-child manifest for a compound parent."""

        parent_id = _clean(parent_operation_id, "parent_operation_id")
        principal = principal_ref or self.store.principal_ref
        normalized_item_ids = [
            _clean(item_id, "expected child item_id") for item_id in expected_item_ids
        ]
        if not normalized_item_ids:
            raise ValueError("expected_item_ids must not be empty")
        if len(set(normalized_item_ids)) != len(normalized_item_ids):
            raise ValueError("expected_item_ids must be unique")
        manifest_hash = semantic_payload_hash(
            {"expected_item_ids": normalized_item_ids}
        )
        record = {
            "version": 1,
            "operation_id": parent_id,
            "expected_item_ids": normalized_item_ids,
            "expected_child_count": len(normalized_item_ids),
            "manifest_hash": manifest_hash,
        }

        with self.store.immediate_transaction() as connection:
            parent = self._visible_operation_in_transaction(
                connection, parent_id, principal
            )
            if str(parent["tool"]) != "patchbay_worker_start_batch":
                raise HubStoreV2StateError(
                    "Child manifests are only valid for batch worker starts"
                )
            existing = connection.execute(
                """
                SELECT * FROM entity_records
                WHERE entity_type = ? AND entity_id = ?
                """,
                (BATCH_CHILD_MANIFEST_ENTITY_TYPE, parent_id),
            ).fetchone()
            if existing is not None:
                saved = self.store._entity_from_row(existing)
                if saved["record"] != record:
                    raise HubStoreV2Conflict("batch_child_manifest_conflict")
                saved["idempotent_replay"] = True
                return saved
            if str(parent["state"]) in TERMINAL_OPERATION_STATES:
                raise HubStoreV2StateError(
                    "Cannot declare children for a terminal operation"
                )

            saved = self.store._put_entity_in_transaction(
                connection,
                BATCH_CHILD_MANIFEST_ENTITY_TYPE,
                parent_id,
                record,
                expected_revision=0,
                legacy_classification="",
            )
            if saved is None:  # BEGIN IMMEDIATE makes this unreachable.
                raise HubStoreV2Conflict("batch_child_manifest_conflict")
            self.store._append_event_in_transaction(
                connection,
                "operation.batch_child_manifest_declared",
                {
                    "expected_item_ids": normalized_item_ids,
                    "expected_child_count": len(normalized_item_ids),
                    "manifest_hash": manifest_hash,
                },
                operation_id=parent_id,
                entity_revision=int(parent["revision"]),
            )
            saved["idempotent_replay"] = False
            return saved

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
                if (
                    str(active["state"]) == "offered"
                    and str(active["machine_id"]) == machine
                    and int(active["edge_generation"]) == generation
                ):
                    # No Edge has claimed this attempt, so no effect can have
                    # crossed the durable intent boundary. A rolling contract
                    # change may therefore fence the stale offer and replace it
                    # without manager intervention or duplicate execution.
                    now = self._clock()
                    connection.execute(
                        """
                        UPDATE attempts
                        SET state = 'retryable', revision = revision + 1,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE attempt_id = ? AND revision = ? AND state = 'offered'
                        """,
                        (now, active["attempt_id"], active["revision"]),
                    )
                    self.store._append_event_in_transaction(
                        connection,
                        "operation.attempt_unclaimed_contract_superseded",
                        {
                            "attempt_id": str(active["attempt_id"]),
                            "previous_required_contract_hash": str(
                                contract.get("required_contract_hash") or ""
                            ),
                            "successor_required_contract_hash": contract_hash,
                            "fencing_token": int(active["fencing_token"]),
                        },
                        operation_id=operation_value,
                        entity_revision=int(active["revision"]) + 1,
                    )
                else:
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
                if equivalent:
                    self.store._append_event_in_transaction(
                        connection,
                        "operation.terminal_receipt_confirmed",
                        {
                            "attempt_id": str(attempt["attempt_id"]),
                            "stored_result_hash": stored_hash,
                            "received_result_hash": normalized_hash,
                        },
                        operation_id=str(operation["operation_id"]),
                        entity_revision=int(operation["revision"]),
                    )
                    response = self._operation_from_row(operation)
                    response["idempotent_replay"] = True
                    response["receipt_duplicate"] = True
                elif self._late_result_can_repair_manual_recovery(
                    operation, attempt, target_state=target_state
                ):
                    if int(operation["revision"]) != operation_revision:
                        return None
                    if expected_attempt_revision is not None and int(
                        attempt["revision"]
                    ) != int(expected_attempt_revision):
                        return None
                    response = self._repair_manual_recovery_in_transaction(
                        connection,
                        operation,
                        attempt,
                        target_state=target_state,
                        normalized=normalized,
                        normalized_hash=normalized_hash,
                        public_status=public_status,
                        fencing_token=fencing_token,
                    )
                else:
                    self.store._append_event_in_transaction(
                        connection,
                        "operation.terminal_receipt_conflict",
                        {
                            "attempt_id": str(attempt["attempt_id"]),
                            "stored_result_hash": stored_hash,
                            "received_result_hash": normalized_hash,
                        },
                        operation_id=str(operation["operation_id"]),
                        entity_revision=int(operation["revision"]),
                    )
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

    @staticmethod
    def _late_result_can_repair_manual_recovery(
        operation: sqlite3.Row,
        attempt: sqlite3.Row,
        *,
        target_state: str,
    ) -> bool:
        """Return whether an exact fenced receipt outranks an absence-based blocker."""

        if (
            str(operation["state"]) != "blocked"
            or str(attempt["state"]) != "manual_recovery"
            or target_state == "outcome_unknown"
        ):
            return False
        normalized = _decode_optional_json(
            operation["result_json"],
            f"operation {operation['operation_id']} result",
        )
        result = normalized.get("result") if isinstance(normalized, Mapping) else None
        reason = str(result.get("reason") or "") if isinstance(result, Mapping) else ""
        return reason in _LATE_RESULT_REPAIRABLE_BLOCKERS

    def _repair_manual_recovery_in_transaction(
        self,
        connection: sqlite3.Connection,
        operation: sqlite3.Row,
        attempt: sqlite3.Row,
        *,
        target_state: str,
        normalized: Mapping[str, Any],
        normalized_hash: str,
        public_status: str,
        fencing_token: int,
    ) -> dict[str, Any]:
        """Atomically replace an absence-based blocker with exact late evidence."""

        now = self._clock()
        encoded = _encode_json(normalized, "normalized result")
        attempt_cursor = connection.execute(
            """
            UPDATE attempts
            SET state = 'result_ready', revision = revision + 1,
                result_json = ?, updated_at = ?
            WHERE attempt_id = ? AND revision = ? AND fencing_token = ?
              AND state = 'manual_recovery'
            """,
            (
                encoded,
                now,
                attempt["attempt_id"],
                attempt["revision"],
                fencing_token,
            ),
        )
        if attempt_cursor.rowcount != 1:
            raise HubStoreV2Conflict("attempt_revision_conflict")
        operation_cursor = connection.execute(
            """
            UPDATE operations
            SET state = ?, revision = revision + 1, result_json = ?,
                error_json = NULL, updated_at = ?
            WHERE operation_id = ? AND revision = ? AND state = 'blocked'
            """,
            (
                target_state,
                encoded,
                now,
                operation["operation_id"],
                operation["revision"],
            ),
        )
        if operation_cursor.rowcount != 1:
            raise HubStoreV2Conflict("operation_revision_conflict")
        self.store._append_event_in_transaction(
            connection,
            "operation.attempt_state_changed",
            {
                "attempt_id": str(attempt["attempt_id"]),
                "from": "manual_recovery",
                "to": "result_ready",
            },
            operation_id=str(operation["operation_id"]),
            entity_revision=int(attempt["revision"]) + 1,
        )
        self.store._append_event_in_transaction(
            connection,
            "operation.manual_recovery_late_result_accepted",
            {
                "attempt_id": str(attempt["attempt_id"]),
                "fencing_token": int(fencing_token),
                "public_status": public_status,
                "result_hash": normalized_hash,
            },
            operation_id=str(operation["operation_id"]),
            entity_revision=int(operation["revision"]) + 1,
        )
        saved = self._operation_row(connection, str(operation["operation_id"]))
        if saved["parent_operation_id"]:
            self._aggregate_parent_in_transaction(
                connection,
                str(saved["parent_operation_id"]),
                allow_terminal_refresh=True,
            )
        response = self._operation_from_row(saved)
        response["idempotent_replay"] = False
        response["receipt_duplicate"] = False
        response["manual_recovery_repaired"] = True
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
            expected_item_ids = self._batch_manifest_item_ids_in_transaction(
                connection, parent_id
            )
            actual_item_ids = [str(child["item_id"]) for child in children]
            exact_child_set = (
                bool(children)
                if expected_item_ids is None
                else len(actual_item_ids) == len(expected_item_ids)
                and set(actual_item_ids) == set(expected_item_ids)
            )
            response["children_terminal"] = exact_child_set and all(
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

        if (
            operation["tool"] == "patchbay_worker_start_batch"
            and operation["state"] not in TERMINAL_OPERATION_STATES
        ):
            aggregated = self.aggregate_parent(operation_value, principal_ref=principal)
            if aggregated is not None:
                operation = aggregated

        batch_recovery = (
            self._batch_recovery_required(operation_value)
            if operation["tool"] == "patchbay_worker_start_batch"
            else None
        )

        latest_event = self._latest_event_revision(operation_value)
        if (
            wait_seconds
            and batch_recovery is None
            and latest_event <= max(0, int(since_revision))
        ):
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
            if operation["tool"] == "patchbay_worker_start_batch":
                batch_recovery = self._batch_recovery_required(operation_value)

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
        if batch_recovery is not None:
            public_status = "blocked"
        aggregate_pending = (
            operation["tool"] == "patchbay_worker_start_batch"
            and operation["state"] not in TERMINAL_OPERATION_STATES
            and batch_recovery is None
        )
        safe_next_action = (
            "inspect_and_replace_batch"
            if batch_recovery is not None
            else (
                "wait_for_child_operations"
                if aggregate_pending
                else self._safe_next_action(str(operation["state"]))
            )
        )
        operation_summary = {
            "operation_id": operation_value,
            "parent_operation_id": str(operation["parent_operation_id"] or ""),
            "item_id": operation["item_id"],
            "state": operation["state"],
            "revision": operation["revision"],
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
                "state": (
                    "recovery_required"
                    if batch_recovery is not None
                    else (
                        "aggregate_running"
                        if aggregate_pending
                        else self._dispatch_state(str(operation["state"]))
                    )
                ),
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
        stale_preview_recovery = self._integration_preview_recovery_action(
            operation, normalized
        )
        if isinstance(normalized, Mapping):
            if include_result:
                domain_result = deepcopy(dict(normalized.get("result") or {}))
                if stale_preview_recovery is not None:
                    # Edge-local single-machine tool names are implementation
                    # details. Hub callers receive only the exact public action.
                    domain_result.pop("next_tool", None)
                    domain_result.pop("next_arguments", None)
                status_result["domain_result"] = domain_result
            warnings = [
                self._public_warning(warning)
                for warning in list(normalized.get("warnings") or [])
            ]
            next_actions = [
                self._public_next_action(action, operation_value)
                for action in list(normalized.get("next_actions") or [])
            ]
        if stale_preview_recovery is not None:
            next_actions = [stale_preview_recovery]
        if batch_recovery is not None:
            warnings.append(batch_recovery)
            status_result["recovery"] = deepcopy(batch_recovery["details"])
            next_actions = [
                {
                    "tool": "patchbay_operation_status",
                    "arguments": {"operation_id": operation_value},
                    "reason": (
                        "This historical batch has incomplete atomic durable state and cannot be "
                        "repaired from its minimized manifest. Inspect the recorded children, do "
                        "not retry this idempotency key, and submit a replacement batch with a "
                        "new idempotency key if the missing work is still required."
                    ),
                }
            ]
        if not next_actions and operation["state"] not in TERMINAL_OPERATION_STATES:
            next_actions = [
                {
                    "tool": "patchbay_operation_status",
                    "arguments": {
                        "operation_id": operation_value,
                        "wait_seconds": 20,
                        "since_revision": latest_event,
                    },
                    "reason": safe_next_action,
                }
            ]
        return public_envelope(
            public_status,
            result=status_result,
            operation=operation_summary,
            warnings=warnings,
            next_actions=next_actions,
        )

    def _integration_preview_recovery_action(
        self,
        operation: Mapping[str, Any],
        normalized: Any,
    ) -> dict[str, Any] | None:
        """Translate one Edge-local stale-preview hint into a callable Hub action."""

        if str(operation.get("tool") or "") != "patchbay_worker_integrate":
            return None
        if not isinstance(normalized, Mapping):
            return None
        domain = normalized.get("result")
        if not isinstance(domain, Mapping) or str(domain.get("reason") or "") not in {
            "stale_preview_token",
            "preview_token_expired",
        }:
            return None
        operation_id = str(operation.get("operation_id") or "")
        row = self.store.connection.execute(
            """
            SELECT work_group_id
            FROM operation_group_index
            WHERE operation_id = ?
            ORDER BY work_group_id
            LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        work_group_id = str(row["work_group_id"]) if row is not None else ""
        fleet_worker_ref = str(operation.get("logical_target") or "")
        if work_group_id and fleet_worker_ref.startswith("fworker_"):
            return {
                "tool": "patchbay_worker_inspect",
                "arguments": {
                    "work_group_id": work_group_id,
                    "fleet_worker_ref": fleet_worker_ref,
                    "view": "integration_preview",
                },
                "reason": (
                    "The signed preview became stale. Review the authoritative replacement "
                    "preview, then submit a new integration mutation with its token and a fresh "
                    "idempotency key."
                ),
            }
        if work_group_id:
            return {
                "tool": "patchbay_work_group_status",
                "arguments": {
                    "work_group_id": work_group_id,
                    "include_workers": True,
                    "include_integrations": True,
                },
                "reason": (
                    "The signed preview became stale and the worker selector could not be "
                    "reconstructed. Inspect authoritative group integration state."
                ),
            }
        return {
            "tool": "patchbay_operation_status",
            "arguments": {"operation_id": operation_id, "include_result": True},
            "reason": "Inspect the stale integration result before selecting its worker again.",
        }

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
        self,
        connection: sqlite3.Connection,
        parent_operation_id: str,
        *,
        allow_terminal_refresh: bool = False,
    ) -> sqlite3.Row:
        parent = self._operation_row(connection, parent_operation_id)
        parent_was_terminal = str(parent["state"]) in TERMINAL_OPERATION_STATES
        if parent_was_terminal and not allow_terminal_refresh:
            return parent
        children = connection.execute(
            """
            SELECT * FROM operations WHERE parent_operation_id = ?
            ORDER BY item_id, operation_id
            """,
            (parent_operation_id,),
        ).fetchall()
        if not children:
            return parent

        expected_item_ids = self._batch_manifest_item_ids_in_transaction(
            connection, parent_operation_id
        )
        if (
            expected_item_ids is None
            and str(parent["tool"]) == "patchbay_worker_start_batch"
        ):
            return parent
        if expected_item_ids is not None:
            children_by_item_id = {str(child["item_id"]): child for child in children}
            if len(children) != len(expected_item_ids) or set(
                children_by_item_id
            ) != set(expected_item_ids):
                return parent
            children = [children_by_item_id[item_id] for item_id in expected_item_ids]

        # Aggregate parents are Hub-owned lifecycle records. Advance them
        # through the normal operation state machine without creating an Edge
        # attempt, then let child completion drive the terminal transition.
        while str(parent["state"]) in {"created", "payload_ready", "dispatchable"}:
            next_state = {
                "created": "payload_ready",
                "payload_ready": "dispatchable",
                "dispatchable": "running",
            }[str(parent["state"])]
            parent = self._transition_operation_in_transaction(
                connection, parent, next_state
            )
        if any(
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

        if parent_was_terminal:
            encoded_parent = _encode_json(normalized_parent, "parent operation result")
            cursor = connection.execute(
                """
                UPDATE operations
                SET state = ?, revision = revision + 1, result_json = ?,
                    error_json = NULL, updated_at = ?
                WHERE operation_id = ? AND revision = ? AND state = ?
                """,
                (
                    terminal_state,
                    encoded_parent,
                    self._clock(),
                    parent["operation_id"],
                    parent["revision"],
                    parent["state"],
                ),
            )
            if cursor.rowcount != 1:
                raise HubStoreV2Conflict("operation_revision_conflict")
            parent = self._operation_row(connection, parent_operation_id)
            event_name = "operation.parent_reconciled"
        elif str(parent["state"]) == "outcome_unknown":
            parent = self._transition_operation_in_transaction(
                connection, parent, "reconciling"
            )
            event_name = "operation.parent_aggregated"
        else:
            event_name = "operation.parent_aggregated"
        if not parent_was_terminal:
            if str(parent["state"]) not in {"running", "reconciling"}:
                raise HubStoreV2StateError(
                    f"Cannot aggregate parent in state {parent['state']}"
                )
            parent = self._transition_operation_in_transaction(
                connection, parent, terminal_state, result=normalized_parent
            )
        self.store._append_event_in_transaction(
            connection,
            event_name,
            {
                "child_count": len(items),
                "public_status": aggregate_status,
                "child_operation_ids": [item["operation_id"] for item in items],
            },
            operation_id=parent_operation_id,
            entity_revision=int(parent["revision"]),
        )
        if allow_terminal_refresh and parent["parent_operation_id"]:
            self._aggregate_parent_in_transaction(
                connection,
                str(parent["parent_operation_id"]),
                allow_terminal_refresh=True,
            )
        return parent

    def _batch_manifest_item_ids_in_transaction(
        self, connection: sqlite3.Connection, parent_operation_id: str
    ) -> list[str] | None:
        row = connection.execute(
            """
            SELECT record_json FROM entity_records
            WHERE entity_type = ? AND entity_id = ?
            """,
            (BATCH_CHILD_MANIFEST_ENTITY_TYPE, parent_operation_id),
        ).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(str(row["record_json"]))
            item_ids = record["expected_item_ids"]
            normalized = [str(item_id) for item_id in item_ids]
            version = record.get("version")
            valid = (
                isinstance(record, dict)
                and version in {1, 2, 3}
                and record.get("operation_id") == parent_operation_id
                and isinstance(item_ids, list)
                and bool(normalized)
                and all(
                    item_id and item_id == item_id.strip() for item_id in normalized
                )
                and len(set(normalized)) == len(normalized)
                and record.get("expected_child_count") == len(normalized)
            )
            if valid and version == 1:
                valid = record.get("manifest_hash") == semantic_payload_hash(
                    {"expected_item_ids": normalized}
                )
            elif valid and version == 2:
                child_specs = record.get("child_specs")
                valid = (
                    isinstance(child_specs, list)
                    and len(child_specs) == len(normalized)
                    and [spec.get("item_id") for spec in child_specs] == normalized
                    and all(
                        isinstance(spec, dict)
                        and isinstance(spec.get("payload"), dict)
                        and spec.get("semantic_payload_hash")
                        == semantic_payload_hash(spec["payload"])
                        for spec in child_specs
                    )
                    and record.get("manifest_hash")
                    == semantic_payload_hash({"child_specs": child_specs})
                )
            elif valid and version == 3:
                child_hashes = record.get("child_hashes")
                valid = (
                    isinstance(child_hashes, list)
                    and len(child_hashes) == len(normalized)
                    and all(
                        isinstance(child, dict)
                        and set(child) == {"item_id", "semantic_hash"}
                        and child.get("item_id") == normalized[index]
                        and isinstance(child.get("semantic_hash"), str)
                        and bool(child["semantic_hash"])
                        for index, child in enumerate(child_hashes)
                    )
                    and record.get("manifest_hash")
                    == semantic_payload_hash(
                        {
                            "expected_item_ids": normalized,
                            "child_hashes": child_hashes,
                        }
                    )
                )
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            valid = False
            normalized = []
        if not valid:
            raise HubStoreV2Corrupt(
                f"Invalid batch child manifest for operation {parent_operation_id}"
            )
        return normalized

    def _batch_recovery_required(
        self, parent_operation_id: str
    ) -> dict[str, Any] | None:
        connection = self.store.connection
        manifest = connection.execute(
            """
            SELECT record_json FROM entity_records
            WHERE entity_type = ? AND entity_id = ?
            """,
            (BATCH_CHILD_MANIFEST_ENTITY_TYPE, parent_operation_id),
        ).fetchone()
        child_rows = connection.execute(
            """
            SELECT operation_id, item_id FROM operations
            WHERE parent_operation_id = ? ORDER BY item_id, operation_id
            """,
            (parent_operation_id,),
        ).fetchall()
        actual_item_ids = [str(row["item_id"]) for row in child_rows]
        if manifest is None:
            return {
                "code": "batch_recovery_required",
                "message": "The historical batch is missing its atomic child manifest.",
                "details": {
                    "reason": "missing_atomic_child_manifest",
                    "actual_child_count": len(actual_item_ids),
                },
            }
        expected_item_ids = self._batch_manifest_item_ids_in_transaction(
            connection, parent_operation_id
        )
        exact_child_set = (
            expected_item_ids is not None
            and len(actual_item_ids) == len(expected_item_ids)
            and set(actual_item_ids) == set(expected_item_ids)
        )
        if not exact_child_set:
            return {
                "code": "batch_recovery_required",
                "message": "The historical batch has an incomplete durable child set.",
                "details": {
                    "reason": "incomplete_atomic_child_set",
                    "expected_item_ids": expected_item_ids or [],
                    "actual_item_ids": actual_item_ids,
                    "missing_item_ids": [
                        item_id
                        for item_id in (expected_item_ids or [])
                        if item_id not in set(actual_item_ids)
                    ],
                },
            }

        dispatched_item_ids = {
            str(row["item_id"])
            for row in connection.execute(
                """
                SELECT operations.item_id
                FROM operations
                JOIN entity_records
                  ON entity_records.entity_id = operations.operation_id
                 AND entity_records.entity_type = ?
                WHERE operations.parent_operation_id = ?
                """,
                (EDGE_DISPATCH_ENTITY_TYPE, parent_operation_id),
            ).fetchall()
        }
        missing_dispatch_item_ids = [
            item_id
            for item_id in (expected_item_ids or [])
            if item_id not in dispatched_item_ids
        ]
        if missing_dispatch_item_ids:
            return {
                "code": "batch_recovery_required",
                "message": (
                    "The historical batch has children without durable Edge dispatch records."
                ),
                "details": {
                    "reason": "incomplete_atomic_child_dispatch_set",
                    "expected_item_ids": expected_item_ids or [],
                    "actual_item_ids": actual_item_ids,
                    "dispatched_item_ids": [
                        item_id
                        for item_id in (expected_item_ids or [])
                        if item_id in dispatched_item_ids
                    ],
                    "missing_item_ids": missing_dispatch_item_ids,
                },
            }
        return None

    @staticmethod
    def _public_warning(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            code = str(value.get("code") or "operation_warning")
            message = str(value.get("message") or code)
            supplied_details = value.get("details")
            details = {
                str(key): deepcopy(item)
                for key, item in value.items()
                if key not in {"code", "message", "details"}
            }
            if isinstance(supplied_details, Mapping):
                details = {**deepcopy(dict(supplied_details)), **details}
            warning: dict[str, Any] = {"code": code, "message": message}
            if details:
                warning["details"] = details
            return warning
        return {"code": "operation_warning", "message": str(value)}

    @staticmethod
    def _public_next_action(value: Any, operation_id: str) -> dict[str, Any]:
        normalized = normalize_hub_v2_next_action(value, operation_id=operation_id)
        if isinstance(normalized, Mapping):
            return deepcopy(dict(normalized))
        return {
            "tool": "patchbay_operation_status",
            "arguments": {"operation_id": operation_id},
            "reason": str(normalized),
        }

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
            "reconciling": "wait_for_edge_reconciliation",
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
