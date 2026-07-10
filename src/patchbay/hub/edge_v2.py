"""Hub V2 Edge execution, projection, and recovery service.

The outbound runner passes claimed operation attempts here, uploads the returned
outbox records, and feed Hub receipt acknowledgements back without weakening
the journal ordering enforced below.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, AsyncIterator, Iterable, Mapping, Protocol, Sequence

from patchbay.hub.edge import build_capabilities, build_workspaces
from patchbay.hub.edge_journal import (
    RECOVERY_EXECUTE_INTENT,
    RECOVERY_MANUAL,
    RECOVERY_RECONCILE_EFFECT,
    RECOVERY_UPLOAD_RESULT,
    EdgeJournal,
)
from patchbay.hub.operations import normalize_domain_result, semantic_payload_hash
from patchbay.hub.tool_surface import (
    HUB_V2_ACTION_MAP,
    HUB_V2_EDGE_ACTION_MAP,
    HUB_V2_WORKSPACE_CHANGES_ACTION_MAP,
)
from patchbay.protocol.context import RequestContext
from patchbay.security import public_error_message, redact_sensitive_output


_OUTCOME_BY_PUBLIC_STATUS = {
    "ok": "succeeded",
    "partial": "succeeded",
    "not_found": "succeeded",
    "blocked": "blocked",
    "failed": "failed",
    "pending": "outcome_unknown",
}
_KNOWN_EDGE_ACTIONS = frozenset(
    {
        *HUB_V2_EDGE_ACTION_MAP.values(),
        *HUB_V2_WORKSPACE_CHANGES_ACTION_MAP.values(),
    }
)
_RESULT_EVIDENCE_FIELDS = (
    "worker_id",
    "edge_worker_id",
    "job_id",
    "artifact_id",
    "request_id",
    "session_id",
    "applied",
    "accepted",
)


class EdgeToolHandler(Protocol):
    """Narrow ToolHandler projection used by the V2 execution service."""

    worker_runtime: Any

    async def handle_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> dict[str, Any]: ...


class EdgeExecutionError(RuntimeError):
    """Base error for a V2 Edge execution request."""


class EdgeAttemptFenceError(EdgeExecutionError):
    """Raised when an attempt cannot cross the Edge execution fence."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = str(reason or "edge_attempt_rejected")
        self.detail = str(detail or "")
        message = self.reason if not self.detail else f"{self.reason}: {self.detail}"
        super().__init__(message)


class EdgeExecutionService:
    """Execute fenced canonical actions through one durable Edge journal.

    The service has no network behavior.  Successful, blocked, failed, and
    uncertain domain outcomes are returned as durable outbox receipt records.
    Callers may replay :meth:`pending_results` until the Hub explicitly returns
    the receipt ids through :meth:`acknowledge_receipts`.
    """

    def __init__(
        self,
        handler: EdgeToolHandler,
        journal: EdgeJournal,
        *,
        machine_id: str = "edge-local",
        edge_generation: str = "",
        capabilities: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
    ):
        self.handler = handler
        self.journal = journal
        self.machine_id = _required_text(machine_id, "machine_id")
        journal_generation = journal.edge_generation
        requested_generation = str(edge_generation or journal_generation).strip()
        if requested_generation != journal_generation:
            raise EdgeAttemptFenceError(
                "edge_generation_conflict",
                f"service {requested_generation!r}, journal {journal_generation!r}",
            )
        self.edge_generation = journal_generation
        handler_config = getattr(handler, "config", {})
        capability_config = config if config is not None else handler_config
        self.config = dict(capability_config or {})
        self.capabilities = dict(capabilities or build_capabilities(capability_config or {}))
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._target_lock_users: dict[str, int] = {}
        self._last_projection_worker_ids: list[str] = []
        self._last_projection_pro_request_ids: list[str] = []

    @property
    def target_lock_count(self) -> int:
        return len(self._target_locks)

    def validate_attempt(self, attempt: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize one immutable broker claim without side effects."""

        if not isinstance(attempt, Mapping):
            raise ValueError("attempt must be an object")
        root = dict(attempt)
        operation = _optional_object(root.get("operation"), "operation")
        claim = _optional_object(root.get("attempt"), "attempt")
        sources = (root, claim, operation)

        operation_id = _consistent_text(sources, ("operation_id",), "operation_id")
        attempt_id = _consistent_text(sources, ("attempt_id",), "attempt_id")
        fencing_token = _consistent_positive_int(sources, ("fencing_token",), "fencing_token")

        payload = _first_object(sources, ("payload", "operation_payload"))
        arguments = _first_object(sources, ("arguments",))
        if not arguments and isinstance(payload.get("arguments"), Mapping):
            arguments = dict(payload["arguments"])
        if not payload:
            payload = dict(arguments)
        if not arguments:
            arguments = dict(payload)

        public_tool = _first_text(sources, ("tool", "tool_name", "public_tool"))
        explicit_action = _first_text(sources, ("action", "edge_action"))
        action, public_tool = self._canonical_action(
            explicit_action=explicit_action,
            public_tool=public_tool,
            arguments=arguments,
        )

        requirement_maps = _requirement_maps(sources)
        self._validate_generation_fence(sources, requirement_maps)
        self._validate_machine_fence(sources)
        self._validate_contract_fences(sources, requirement_maps)
        self._validate_action_capability_fence(
            action=action,
            sources=sources,
            requirement_maps=requirement_maps,
        )

        public_context = self._public_context(sources, arguments)
        correlation = _first_object(sources, ("correlation",))
        correlation.update(
            {
                "action": action,
                "context": public_context,
            }
        )
        if public_tool:
            correlation["public_tool"] = public_tool
        for field in ("parent_operation_id", "item_id", "work_group_id", "lane_id"):
            value = _first_text(sources, (field,))
            if value:
                correlation[field] = value

        target_key = self._target_key(
            sources=sources,
            action=action,
            arguments=arguments,
            operation_id=operation_id,
        )
        payload_hash = _first_text(
            sources,
            ("operation_payload_hash", "semantic_payload_hash", "payload_hash"),
        )
        idempotency_key = _first_text(sources, ("idempotency_key",))
        return {
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "fencing_token": fencing_token,
            "edge_generation": self.edge_generation,
            "action": action,
            "public_tool": public_tool,
            "target_key": target_key,
            "payload": payload,
            "payload_hash": payload_hash,
            "arguments": arguments,
            "idempotency_key": idempotency_key,
            "correlation": correlation,
            "public_context": public_context,
        }

    async def execute_attempt(self, attempt: Mapping[str, Any]) -> dict[str, Any]:
        """Execute or recover one fenced attempt under its operation target lock."""

        plan = self.validate_attempt(attempt)
        async with self._target_lock(plan["target_key"]):
            recorded = self.journal.record_intent(
                operation_id=plan["operation_id"],
                attempt_id=plan["attempt_id"],
                fencing_token=plan["fencing_token"],
                action=plan["action"],
                target_key=plan["target_key"],
                payload=plan["payload"],
                payload_hash=plan["payload_hash"],
                edge_generation=self.edge_generation,
                idempotency_key=plan["idempotency_key"],
                correlation=plan["correlation"],
            )
            if recorded.get("idempotent_replay"):
                replay = self._resume_duplicate(recorded)
                if replay is not None:
                    return replay

            self.journal.mark_attempt_executing(
                plan["operation_id"],
                plan["attempt_id"],
                plan["fencing_token"],
                edge_generation=self.edge_generation,
            )
            context = RequestContext.from_public_metadata(plan["public_context"])
            try:
                arguments = deepcopy(plan["arguments"])
                revision_refusal = await self._pro_request_revision_refusal(
                    plan["action"], arguments, context
                )
                if revision_refusal is not None:
                    domain_result = revision_refusal
                else:
                    domain_result = await self.handler.handle_tool_call(
                        plan["action"], arguments, context=context
                    )
                if not isinstance(domain_result, Mapping):
                    raise TypeError("ToolHandler results must be objects")
                domain_result = redact_sensitive_output(dict(domain_result))
            except Exception as error:
                return self._record_uncertain_handler_outcome(plan, error)

            semantic = normalize_domain_result(domain_result)
            public_status = str(semantic["status"])
            outcome = _OUTCOME_BY_PUBLIC_STATUS[public_status]
            effect = {
                "action": plan["action"],
                "public_status": public_status,
                "domain_result_hash": semantic_payload_hash(domain_result),
                "correlation": {
                    key: domain_result[key]
                    for key in _RESULT_EVIDENCE_FIELDS
                    if key in domain_result
                },
            }
            self.journal.mark_effect_recorded(
                plan["operation_id"],
                plan["attempt_id"],
                plan["fencing_token"],
                effect=effect,
                edge_generation=self.edge_generation,
            )
            return self.journal.record_result(
                operation_id=plan["operation_id"],
                attempt_id=plan["attempt_id"],
                fencing_token=plan["fencing_token"],
                outcome=outcome,
                result=domain_result,
                uncertain=public_status == "pending",
                edge_generation=self.edge_generation,
            )

    async def _pro_request_revision_refusal(
        self,
        action: str,
        arguments: dict[str, Any],
        context: RequestContext,
    ) -> dict[str, Any] | None:
        """Apply the Hub CAS contract atomically under the Edge request lock."""

        if action not in {
            "codex_pro_request_claim",
            "codex_pro_request_respond",
            "codex_pro_request_dispatch",
            "codex_pro_request_close",
        }:
            return None
        expected = arguments.pop("expected_revision", None)
        if expected is None:
            return {
                "accepted": False,
                "reason": "expected_revision_required",
            }
        current = await self.handler.handle_tool_call(
            "codex_pro_request_read",
            {
                "request_id": arguments.get("request_id"),
                "include_report": False,
                "include_response": False,
                "include_events": False,
            },
            context=context,
        )
        request = current.get("request") if isinstance(current, Mapping) else None
        actual = int(request.get("revision") or 0) if isinstance(request, Mapping) else 0
        if int(expected) == actual:
            return None
        return {
            "accepted": False,
            "reason": "stale_revision",
            "expected_revision": int(expected),
            "actual_revision": actual,
            "request": deepcopy(dict(request or {})),
        }

    execute = execute_attempt
    handle_attempt = execute_attempt

    def pending_results(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return durable outbox receipts that still require Hub acknowledgement."""

        return self.journal.list_pending_outbox(limit=limit)

    pending_outbox = pending_results

    def acknowledge_receipt(self, receipt: str | Mapping[str, Any]) -> dict[str, Any]:
        """Apply one explicit Hub acknowledgement with all supplied fences."""

        if isinstance(receipt, str):
            return self.journal.acknowledge_outbox(receipt)
        if not isinstance(receipt, Mapping):
            raise ValueError("receipt acknowledgement must be a string or object")
        receipt_id = _required_text(receipt.get("receipt_id"), "receipt_id")
        fencing_token = receipt.get("fencing_token")
        return self.journal.acknowledge_outbox(
            receipt_id,
            operation_id=str(receipt.get("operation_id") or ""),
            attempt_id=str(receipt.get("attempt_id") or ""),
            fencing_token=fencing_token if fencing_token is not None else None,
            edge_generation=str(receipt.get("edge_generation") or ""),
        )

    def acknowledge_receipts(
        self,
        receipts: Sequence[str | Mapping[str, Any]] | Mapping[str, Any] | str,
    ) -> list[dict[str, Any]]:
        """Apply receipt acknowledgements returned by a Hub control exchange."""

        if isinstance(receipts, Mapping):
            nested: Any = None
            nested_present = False
            for key in (
                "receipt_acknowledgements",
                "acknowledged_receipts",
                "receipt_ids",
                "receipts",
            ):
                if key in receipts:
                    nested = receipts[key]
                    nested_present = True
                    break
            if not nested_present and receipts.get("receipt_id"):
                items: Iterable[str | Mapping[str, Any]] = (receipts,)
            elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                items = nested
            else:
                raise ValueError("Hub acknowledgement object does not contain receipts")
        elif isinstance(receipts, str):
            items = (receipts,)
        else:
            items = receipts
        return [self.acknowledge_receipt(receipt) for receipt in items]

    acknowledge_hub_receipts = acknowledge_receipts

    def projection_snapshot(
        self,
        *,
        previous_edge_worker_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Emit one full worker snapshot under a durable monotonic revision."""

        runtime = getattr(self.handler, "worker_runtime", None)
        snapshot_builder = getattr(runtime, "projection_snapshot", None)
        if not callable(snapshot_builder):
            raise EdgeExecutionError("ToolHandler has no WorkerRuntime projection API")
        if isinstance(previous_edge_worker_ids, str):
            previous = [previous_edge_worker_ids]
        elif previous_edge_worker_ids is not None:
            previous = list(previous_edge_worker_ids)
        else:
            previous = list(self._last_projection_worker_ids)
        raw = snapshot_builder(previous_edge_worker_ids=previous)
        if not isinstance(raw, Mapping):
            raise EdgeExecutionError("WorkerRuntime projection snapshot must be an object")
        snapshot = deepcopy(dict(raw))
        workers = snapshot.get("workers")
        if not isinstance(workers, list):
            raise EdgeExecutionError("WorkerRuntime full snapshot must contain workers")
        present = snapshot.get("present_edge_worker_ids")
        if not isinstance(present, list):
            present = [
                str(worker.get("edge_worker_id") or "")
                for worker in workers
                if isinstance(worker, Mapping) and worker.get("edge_worker_id")
            ]
            snapshot["present_edge_worker_ids"] = present
        self._last_projection_worker_ids = [str(worker_id) for worker_id in present]

        pro_requests, pro_requests_complete = self._pro_request_projection()
        present_pro_request_ids = [str(item["request_id"]) for item in pro_requests]
        pro_request_tombstones = [
            {"request_id": request_id}
            for request_id in sorted(
                set(self._last_projection_pro_request_ids) - set(present_pro_request_ids)
            )
        ]
        self._last_projection_pro_request_ids = present_pro_request_ids

        revision = self.journal.advance_projection_revision()
        snapshot.update(
            {
                "snapshot_version": int(snapshot.get("snapshot_version") or 2),
                "snapshot_kind": "full",
                "full_history": True,
                "complete_worker_set": True,
                "omission_means_tombstone": True,
                "machine_id": self.machine_id,
                "edge_generation": self.edge_generation,
                "projection_revision": revision,
                "projection_identity": {
                    "machine_id": self.machine_id,
                    "edge_generation": self.edge_generation,
                    "projection_revision": revision,
                },
                "pro_requests": pro_requests,
                "pro_request_tombstones": pro_request_tombstones,
                "complete_pro_request_set": pro_requests_complete,
            }
        )
        return snapshot

    def _pro_request_projection(self) -> tuple[list[dict[str, Any]], bool]:
        """Return an explicit metadata-only index of Edge-local Pro Requests."""

        store = getattr(self.handler, "pro_request_store", None)
        list_requests = getattr(store, "list_requests", None)
        if not callable(list_requests):
            return [], True

        result = list_requests(include_closed=True, limit=100, request_context=None)
        if not isinstance(result, Mapping):
            raise EdgeExecutionError("ProRequestStore list_requests must return an object")
        workspace_refs = self._workspace_refs_by_repo_name()
        projected: list[dict[str, Any]] = []
        for value in result.get("requests") or []:
            if not isinstance(value, Mapping):
                continue
            request_id = str(value.get("id") or "").strip()
            if not request_id:
                continue
            repo_name = str(value.get("repo_name") or "").strip()
            response = value.get("response") if isinstance(value.get("response"), Mapping) else {}
            origin = value.get("origin") if isinstance(value.get("origin"), Mapping) else {}
            projected.append(
                {
                    "request_id": request_id,
                    "status": str(value.get("status") or ""),
                    "revision": int(value.get("revision") or 0),
                    "created_at": value.get("created_at"),
                    "updated_at": value.get("updated_at"),
                    "workspace_id": str(value.get("workspace_id") or ""),
                    "workspace_ref": workspace_refs.get(repo_name.casefold(), ""),
                    "repo_name": repo_name,
                    "priority": str(value.get("priority") or ""),
                    "kind": str(value.get("kind") or ""),
                    "response_exists": bool(response.get("exists")),
                    "origin_available_for_dispatch": bool(
                        origin.get("origin_available_for_dispatch")
                    ),
                    "attachment_count": int(value.get("attachment_count") or 0),
                }
            )
        projected.sort(
            key=lambda item: (float(item.get("updated_at") or 0), item["request_id"]),
            reverse=True,
        )
        return projected, not bool(result.get("truncated"))

    def _workspace_refs_by_repo_name(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for workspace in build_workspaces(self.config):
            alias = str(workspace.get("alias") or "").strip()
            if alias:
                result[alias.casefold()] = alias
        return result

    full_projection_snapshot = projection_snapshot

    def reconciliation_lookup(
        self,
        *,
        operation_id: str = "",
        attempt_id: str = "",
    ) -> dict[str, Any]:
        """Return exact local recovery state without executing or uploading work."""

        operation_value = str(operation_id or "").strip()
        attempt_value = str(attempt_id or "").strip()
        if not operation_value and not attempt_value:
            return self.journal.recovery_snapshot()

        if attempt_value:
            attempt = self.journal.get_attempt(attempt_value)
            if attempt is None:
                return {
                    "found": False,
                    "operation_id": operation_value,
                    "attempt_id": attempt_value,
                }
            if operation_value and attempt["operation_id"] != operation_value:
                return {
                    "found": False,
                    "operation_id": operation_value,
                    "attempt_id": attempt_value,
                    "reason": "attempt_operation_mismatch",
                }
            return self._reconciliation_record(attempt)

        intent = self.journal.get_intent(operation_value)
        if intent is None:
            return {"found": False, "operation_id": operation_value, "attempt_id": ""}
        rows = self.journal.connection.execute(
            """
            SELECT attempt_id FROM operation_attempts
            WHERE operation_id = ? ORDER BY fencing_token, created_at, attempt_id
            """,
            (operation_value,),
        ).fetchall()
        attempts = [
            self._reconciliation_record(saved)
            for row in rows
            if (saved := self.journal.get_attempt(str(row["attempt_id"]))) is not None
        ]
        if not attempts:
            return {
                "found": True,
                "operation_id": operation_value,
                "attempt_id": "",
                "intent": intent,
                "attempts": [],
                "recovery_action": RECOVERY_MANUAL,
            }
        current = attempts[-1]
        return {**current, "intent": intent, "attempts": attempts}

    lookup_reconciliation = reconciliation_lookup

    def _canonical_action(
        self,
        *,
        explicit_action: str,
        public_tool: str,
        arguments: Mapping[str, Any],
    ) -> tuple[str, str]:
        if explicit_action in HUB_V2_ACTION_MAP:
            if public_tool and public_tool != explicit_action:
                raise EdgeAttemptFenceError("edge_action_conflict")
            public_tool = explicit_action
            explicit_action = ""

        mapped_action = ""
        if public_tool:
            mapped_action = str(HUB_V2_ACTION_MAP.get(public_tool) or "")
            if not mapped_action:
                raise EdgeAttemptFenceError("unknown_hub_v2_tool", public_tool)
            if public_tool == "patchbay_workspace_changes":
                view = str(arguments.get("view") or "").strip()
                mapped_action = str(HUB_V2_WORKSPACE_CHANGES_ACTION_MAP.get(view) or "")
                if not mapped_action:
                    raise EdgeAttemptFenceError("unsupported_workspace_changes_view", view)
        if explicit_action and mapped_action and explicit_action != mapped_action:
            raise EdgeAttemptFenceError(
                "edge_action_conflict",
                f"attempt {explicit_action!r}, tool maps to {mapped_action!r}",
            )
        action = explicit_action or mapped_action
        if not action:
            raise ValueError("action or tool_name is required")
        if action not in _KNOWN_EDGE_ACTIONS:
            raise EdgeAttemptFenceError("unsupported_edge_action", action)
        return action, public_tool

    def _validate_generation_fence(
        self,
        sources: Sequence[Mapping[str, Any]],
        requirement_maps: Sequence[Mapping[str, Any]],
    ) -> None:
        values = _all_text(sources, ("edge_generation", "required_edge_generation", "required_generation"))
        values.extend(
            _all_text(requirement_maps, ("edge_generation", "required_edge_generation", "generation"))
        )
        if not values:
            raise EdgeAttemptFenceError("missing_edge_generation_fence")
        mismatch = next((value for value in values if value != self.edge_generation), "")
        if mismatch:
            raise EdgeAttemptFenceError(
                "edge_generation_mismatch",
                f"attempt {mismatch!r}, edge {self.edge_generation!r}",
            )

    def _validate_machine_fence(self, sources: Sequence[Mapping[str, Any]]) -> None:
        values = _all_text(sources, ("machine_id", "required_machine_id"))
        mismatch = next((value for value in values if value != self.machine_id), "")
        if mismatch:
            raise EdgeAttemptFenceError(
                "edge_machine_mismatch",
                f"attempt {mismatch!r}, edge {self.machine_id!r}",
            )

    def _validate_contract_fences(
        self,
        sources: Sequence[Mapping[str, Any]],
        requirement_maps: Sequence[Mapping[str, Any]],
    ) -> None:
        all_sources = (*sources, *requirement_maps)
        contract_hashes = _all_text(
            all_sources,
            ("required_contract_hash", "contract_hash", "required_hash"),
        )
        if not contract_hashes:
            raise EdgeAttemptFenceError("missing_contract_hash_fence")
        actual_contract_hash = str(self.capabilities.get("contract_hash") or "")
        mismatch = next((value for value in contract_hashes if value != actual_contract_hash), "")
        if mismatch or not actual_contract_hash:
            raise EdgeAttemptFenceError(
                "edge_contract_mismatch",
                f"attempt {(mismatch or contract_hashes[0])!r}, edge {actual_contract_hash!r}",
            )

        optional_fences = (
            (("required_protocol_version", "protocol_version", "required_protocol"), "protocol_version"),
            (("required_contract_version", "contract_version"), "contract_version"),
            (("required_manifest_hash", "manifest_hash"), "manifest_hash"),
            (("required_schema_hash", "schema_hash"), "schema_hash"),
        )
        for aliases, capability_field in optional_fences:
            expected_values = _all_text(all_sources, aliases)
            actual = str(self.capabilities.get(capability_field) or "")
            mismatch = next((value for value in expected_values if value != actual), "")
            if mismatch:
                raise EdgeAttemptFenceError(
                    f"edge_{capability_field}_mismatch",
                    f"attempt {mismatch!r}, edge {actual!r}",
                )

    def _validate_action_capability_fence(
        self,
        *,
        action: str,
        sources: Sequence[Mapping[str, Any]],
        requirement_maps: Sequence[Mapping[str, Any]],
    ) -> None:
        all_sources = (*sources, *requirement_maps)
        expected = _all_text(
            all_sources,
            ("required_action_capability_version", "action_capability_version"),
        )
        for source in all_sources:
            for key in ("required_action_capabilities", "action_capabilities", "action_capability_versions"):
                versions = source.get(key)
                if isinstance(versions, Mapping) and versions.get(action) not in (None, ""):
                    expected.append(str(versions[action]).strip())
        if not expected:
            raise EdgeAttemptFenceError("missing_action_capability_fence", action)
        advertised = self.capabilities.get("action_capabilities")
        actual = str(advertised.get(action) or "") if isinstance(advertised, Mapping) else ""
        mismatch = next((value for value in expected if value != actual), "")
        if mismatch or not actual:
            raise EdgeAttemptFenceError(
                "edge_action_capability_mismatch",
                f"{action!r} requires {(mismatch or expected[0])!r}, edge has {actual!r}",
            )

    def _public_context(
        self,
        sources: Sequence[Mapping[str, Any]],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        public_context: dict[str, Any] = {}
        for source in reversed(sources):
            context = source.get("context")
            if isinstance(context, Mapping):
                public_context.update(dict(context))
        work_group_id = _first_text(sources, ("work_group_id",)) or str(
            arguments.get("work_group_id") or ""
        )
        lane_id = _first_text(sources, ("lane_id",)) or str(
            arguments.get("lane_id") or arguments.get("lane") or ""
        )
        if work_group_id:
            public_context["work_group_id"] = work_group_id
        if lane_id:
            public_context["lane_id"] = lane_id
        return public_context

    def _target_key(
        self,
        *,
        sources: Sequence[Mapping[str, Any]],
        action: str,
        arguments: Mapping[str, Any],
        operation_id: str,
    ) -> str:
        explicit = _first_text(sources, ("target_key",))
        if explicit:
            return explicit
        direct = _first_text(sources, ("target_ref", "operation_target", "fleet_worker_ref"))
        if direct:
            return f"target:{direct}"
        for source in sources:
            target = source.get("target")
            if isinstance(target, Mapping):
                for key in (
                    "fleet_worker_ref",
                    "worker",
                    "worker_id",
                    "name",
                    "workspace_projection_ref",
                    "workspace_ref",
                    "repo_path",
                ):
                    if target.get(key):
                        return f"{key}:{target[key]}"
            elif target:
                return f"target:{target}"

        if action in {
            "codex_worker_message",
            "codex_worker_stop",
            "codex_worker_integrate",
        }:
            for key in ("fleet_worker_ref", "worker", "worker_id"):
                if arguments.get(key):
                    return f"{key}:{arguments[key]}"
        if action == "codex_worker_start" and arguments.get("name"):
            repo = arguments.get("repo_path") or arguments.get("repo") or ""
            return f"worker_name:{repo}:{arguments['name']}"
        for key in ("request_id", "artifact_id", "workspace_projection_ref", "workspace_ref", "repo_path", "repo"):
            if arguments.get(key):
                return f"{key}:{arguments[key]}"
        return f"operation:{operation_id}"

    def _resume_duplicate(self, attempt: Mapping[str, Any]) -> dict[str, Any] | None:
        if attempt.get("result_hash") or attempt.get("receipt_id"):
            return self.journal.record_result(
                operation_id=str(attempt["operation_id"]),
                attempt_id=str(attempt["attempt_id"]),
                fencing_token=int(attempt["fencing_token"]),
                outcome=str(attempt.get("outcome") or "outcome_unknown"),
                result=attempt.get("result") if isinstance(attempt.get("result"), Mapping) else {},
                error=str(attempt.get("error") or ""),
                uncertain=bool(attempt.get("uncertain")),
                receipt_id=str(attempt.get("receipt_id") or ""),
                edge_generation=self.edge_generation,
            )
        if str(attempt.get("state") or "") == "intent_recorded":
            return None
        return self.reconciliation_lookup(attempt_id=str(attempt["attempt_id"]))

    def _record_uncertain_handler_outcome(
        self,
        plan: Mapping[str, Any],
        error: Exception,
    ) -> dict[str, Any]:
        safe_error = public_error_message(error)
        self.journal.mark_outcome_unknown(
            str(plan["operation_id"]),
            str(plan["attempt_id"]),
            int(plan["fencing_token"]),
            edge_generation=self.edge_generation,
        )
        return self.journal.record_result(
            operation_id=str(plan["operation_id"]),
            attempt_id=str(plan["attempt_id"]),
            fencing_token=int(plan["fencing_token"]),
            outcome="outcome_unknown",
            result={
                "reason": "outcome_unknown",
                "last_known_phase": "handler_execution",
            },
            error=safe_error,
            uncertain=True,
            edge_generation=self.edge_generation,
        )

    def _reconciliation_record(self, attempt: Mapping[str, Any]) -> dict[str, Any]:
        attempt_id = str(attempt["attempt_id"])
        recovery = next(
            (
                item
                for item in self.journal.list_restart_recovery()
                if item["attempt_id"] == attempt_id
            ),
            None,
        )
        if recovery is None:
            state = str(attempt.get("state") or "")
            recovery_action = {
                "intent_recorded": RECOVERY_EXECUTE_INTENT,
                "executing": RECOVERY_RECONCILE_EFFECT,
                "effect_recorded": RECOVERY_RECONCILE_EFFECT,
                "result_ready": RECOVERY_UPLOAD_RESULT,
                "outcome_unknown": RECOVERY_MANUAL,
                "manual_recovery": RECOVERY_MANUAL,
                "acknowledged": "none",
            }.get(state, RECOVERY_MANUAL)
            recovery = {
                **dict(attempt),
                "recovery_action": recovery_action,
                "needs_upload": state == "result_ready",
                "needs_reconciliation": recovery_action
                in {RECOVERY_RECONCILE_EFFECT, RECOVERY_MANUAL},
            }
        receipt_id = str(attempt.get("receipt_id") or "")
        receipt = self.journal.get_outbox(receipt_id) if receipt_id else None
        return {
            **dict(recovery),
            "found": True,
            "attempt": dict(attempt),
            "receipt": receipt or {},
        }

    @asynccontextmanager
    async def _target_lock(self, target_key: str) -> AsyncIterator[None]:
        lock = self._target_locks.setdefault(target_key, asyncio.Lock())
        self._target_lock_users[target_key] = self._target_lock_users.get(target_key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._target_lock_users.get(target_key, 1) - 1
            if remaining > 0:
                self._target_lock_users[target_key] = remaining
            else:
                self._target_lock_users.pop(target_key, None)
                if self._target_locks.get(target_key) is lock:
                    self._target_locks.pop(target_key, None)


def _optional_object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _required_text(value: Any, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _all_text(
    sources: Iterable[Mapping[str, Any]],
    aliases: Sequence[str],
) -> list[str]:
    values: list[str] = []
    for source in sources:
        for key in aliases:
            value = source.get(key)
            if value not in (None, ""):
                cleaned = str(value).strip()
                if cleaned and cleaned not in values:
                    values.append(cleaned)
    return values


def _first_text(
    sources: Iterable[Mapping[str, Any]],
    aliases: Sequence[str],
) -> str:
    values = _all_text(sources, aliases)
    return values[0] if values else ""


def _consistent_text(
    sources: Iterable[Mapping[str, Any]],
    aliases: Sequence[str],
    field: str,
) -> str:
    values = _all_text(sources, aliases)
    if not values:
        raise ValueError(f"{field} is required")
    if len(values) != 1:
        raise EdgeAttemptFenceError(f"{field}_conflict")
    return values[0]


def _consistent_positive_int(
    sources: Iterable[Mapping[str, Any]],
    aliases: Sequence[str],
    field: str,
) -> int:
    raw = _all_text(sources, aliases)
    if not raw:
        raise ValueError(f"{field} is required")
    values: list[int] = []
    for value in raw:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a positive integer") from exc
        if parsed < 1:
            raise ValueError(f"{field} must be a positive integer")
        if parsed not in values:
            values.append(parsed)
    if len(values) != 1:
        raise EdgeAttemptFenceError(f"{field}_conflict")
    return values[0]


def _first_object(
    sources: Iterable[Mapping[str, Any]],
    aliases: Sequence[str],
) -> dict[str, Any]:
    for source in sources:
        for key in aliases:
            value = source.get(key)
            if value is None:
                continue
            if not isinstance(value, Mapping):
                raise ValueError(f"{key} must be an object")
            return dict(value)
    return {}


def _requirement_maps(
    sources: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for source in sources:
        for key in ("requirements", "required_contract"):
            value = source.get(key)
            if value is None:
                continue
            if not isinstance(value, Mapping):
                raise ValueError(f"{key} must be an object")
            requirements.append(dict(value))
    return requirements


__all__ = [
    "EdgeAttemptFenceError",
    "EdgeExecutionError",
    "EdgeExecutionService",
    "EdgeToolHandler",
]
