"""Pure V2 work-group lifecycle derivation.

Group status is derived from worker projections and operations. Transport
command completion is intentionally absent from this module.
"""
from __future__ import annotations

import secrets
import time
from copy import deepcopy
from typing import Any, Iterable, Mapping


TERMINAL_GROUP_STATES = frozenset({"complete", "abandoned", "superseded"})
ACTIVE_TURN_STATES = frozenset({"queued", "starting", "working"})
UNCERTAIN_LIVENESS = frozenset({"stale", "lost"})
TERMINAL_OPERATION_STATES = frozenset({"succeeded", "blocked", "failed", "cancelled"})
ACCEPTABLE_CLOSE_DISPOSITIONS = frozenset(
    {
        "integrated",
        "no_changes",
        "reviewed_failure",
        "stopped_preserved",
        "discarded_explicitly",
        "leave_running",
    }
)

GROUP_EXECUTION_MODES = frozenset({"end_to_end", "asynchronous_handoff"})


def derive_group_activity(
    workers: Iterable[Mapping[str, Any]],
    operations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    worker_list = [dict(worker) for worker in workers]
    operation_list = [dict(operation) for operation in operations]
    counts = {
        "workers": len(worker_list),
        "active": 0,
        "quiet": 0,
        "stale": 0,
        "lost": 0,
        "failed": 0,
        "unintegrated": 0,
        "uncertain_operations": 0,
        "active_operations": 0,
    }
    for worker in worker_list:
        turn_state = str(worker.get("turn_state") or "none")
        liveness = str(worker.get("liveness") or "terminal")
        integration_state = str(worker.get("integration_state") or "not_applicable")
        if turn_state in ACTIVE_TURN_STATES:
            counts["active"] += 1
        if liveness == "quiet":
            counts["quiet"] += 1
        if liveness == "stale":
            counts["stale"] += 1
        if liveness == "lost":
            counts["lost"] += 1
        if turn_state == "failed":
            counts["failed"] += 1
        if integration_state in {"not_integrated", "uncertain"}:
            counts["unintegrated"] += 1
    for operation in operation_list:
        state = str(operation.get("state") or "")
        if state in {"outcome_unknown", "reconciling"}:
            counts["uncertain_operations"] += 1
        elif state not in TERMINAL_OPERATION_STATES:
            counts["active_operations"] += 1

    if counts["lost"] or counts["uncertain_operations"]:
        activity = "recovery_required"
    elif counts["stale"]:
        activity = "degraded"
    elif counts["active"] or counts["active_operations"]:
        activity = "active"
    elif counts["workers"]:
        activity = "idle"
    else:
        activity = "planned"
    return {"activity": activity, "counts": counts}


def validate_source_contract(value):
    if value is None:
        return {}
    allowed = {"input_generation", "lane_relationships", "interface_owner", "join_condition", "required_combined_checks"}
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError("source_contract must declare input_generation, lane_relationships, interface_owner, join_condition and required_combined_checks")
    for key in allowed - {"required_combined_checks"}:
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 4000:
            raise ValueError("source_contract text must be explicit and bounded")
    checks = value["required_combined_checks"]
    if not isinstance(checks, list) or len(checks)>32 or any(not isinstance(x,str) or not x.strip() or len(x)>512 for x in checks) or len(set(checks)) != len(checks):
        raise ValueError("required_combined_checks must contain unique bounded check names")
    return deepcopy(value)


def combined_source_generation(workers, operations):
    import hashlib
    import json
    # Exact durable source/delivery facts; liveness and observer reads are not source generations.
    worker_fields = ('fleet_worker_ref', 'edge_worker_id', 'content_revision', 'latest_job_id', 'turn_count', 'integration_state')
    worker_facts = [{key: worker.get(key) for key in worker_fields} for worker in workers]
    delivery_facts = [{key: operation.get(key) for key in ('operation_id', 'revision', 'state', 'result')}
                      for operation in operations if operation.get('tool') == 'patchbay_worker_integrate']
    raw = json.dumps({'workers': sorted(worker_facts,key=lambda x:str(x)), 'deliveries': sorted(delivery_facts,key=lambda x:str(x))}, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def validate_combined_verification(contract, evidence, *, expected_generation=None):
    import re
    checks = contract.get("required_combined_checks") or []
    if not evidence:
        return {"satisfied": not checks, "scope": "controller_attestation", "reason": "not_required" if not checks else "combined_check_evidence_required"}
    if not isinstance(evidence, dict) or set(evidence) != {"input_generation", "combined_tree", "checks", "delivery_generation"}:
        raise ValueError("combined_verification requires input_generation, combined_tree, delivery_generation and checks")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(evidence["combined_tree"])):
        raise ValueError("combined_tree must identify the tested Git tree")
    if not isinstance(evidence["checks"], list) or len(evidence["checks"])>32:
        raise ValueError("checks must be a bounded list")
    observed = {}
    for check in evidence["checks"]:
        if not isinstance(check,dict) or set(check) != {"name", "tree", "outcome", "evidence_ref"}:
            raise ValueError("Each check requires name/tree/outcome/evidence_ref")
        if not isinstance(check['name'], str) or not 0 < len(check['name']) <= 512 or check["name"] in observed or check["outcome"] not in {"passed", "failed", "not_run"} or not isinstance(check["evidence_ref"],str) or not 0<len(check["evidence_ref"])<=512:
            raise ValueError("Check identity/outcome/evidence_ref is invalid")
        observed[check["name"]] = check
    satisfied = (expected_generation is None or evidence['delivery_generation'] == expected_generation) and evidence["input_generation"] == contract.get("input_generation", evidence["input_generation"]) and all(
        name in observed and observed[name]["outcome"] == "passed" and observed[name]["tree"] == evidence["combined_tree"] for name in checks)
    return {"satisfied": satisfied, "scope": "controller_attestation", "evidence": deepcopy(evidence),
            "canonical_execution_receipt": False}


def derive_completion_contract(
    group: Mapping[str, Any],
    workers: Iterable[Mapping[str, Any]],
    operations: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive the manager's continuation obligation from durable lifecycle facts.

    Semantic completion remains the manager's judgment. This contract only makes
    the declared execution mode and exact mechanical blockers unambiguous.
    """

    worker_list = [dict(worker) for worker in workers]
    operation_list = [dict(operation) for operation in operations]
    activity = derive_group_activity(worker_list, operation_list)
    mode = str(group.get("execution_mode") or "end_to_end")
    if mode not in GROUP_EXECUTION_MODES:
        mode = "end_to_end"
    group_open = str(group.get("status") or "open") == "open"
    counts = activity["counts"]

    work_group_id = str(group.get("work_group_id") or "")

    if not group_open:
        reason = "group_terminal"
        next_action: dict[str, Any] | str = (
            "The work group is terminal; no further PatchBay action is required."
        )
    elif counts["lost"] or counts["uncertain_operations"]:
        reason = "recovery_required"
        if work_group_id:
            next_action = {
                "tool": "patchbay_work_group_status",
                "reason": "Inspect authoritative recovery state before continuing.",
                "arguments": {
                    "work_group_id": work_group_id,
                    "include_workers": True,
                    "include_operations": True,
                    "include_integrations": True,
                },
            }
        else:
            next_action = (
                "List the manager's open work groups, select the affected durable group, "
                "then inspect its authoritative recovery state."
            )
    elif counts["active"]:
        reason = "workers_or_operations_active"
        if work_group_id:
            next_action = {
                "tool": "patchbay_worker_wait",
                "reason": "Required work is still active; wait and continue managing it.",
                "arguments": {
                    "work_group_id": work_group_id,
                    "wait_seconds": 30,
                },
            }
        else:
            next_action = (
                "List the manager's open work groups, select the affected durable group, "
                "then wait for its active workers."
            )
    elif counts["active_operations"]:
        reason = "operations_active"
        if work_group_id:
            next_action = {
                "tool": "patchbay_work_group_status",
                "reason": (
                    "A group-level operation such as repository preflight is still active. "
                    "Wait for authoritative group state; no worker exists to wait on yet."
                ),
                "arguments": {
                    "work_group_id": work_group_id,
                    "include_workers": True,
                    "include_operations": True,
                    "include_integrations": True,
                    "wait_for_change_seconds": 30,
                },
            }
        else:
            next_action = (
                "List the manager's open work groups, select the affected durable group, "
                "then wait on its group status until the active operation completes."
            )
    elif counts["unintegrated"]:
        reason = "unintegrated_worker_changes"
        unintegrated_worker = next(
            (
                worker
                for worker in worker_list
                if str(worker.get("integration_state") or "")
                in {"not_integrated", "uncertain"}
            ),
            {},
        )
        fleet_worker_ref = str(unintegrated_worker.get("fleet_worker_ref") or "")
        worker_name = str(unintegrated_worker.get("name") or "")
        unique_worker_name = bool(worker_name) and sum(
            str(worker.get("name") or "").casefold() == worker_name.casefold()
            for worker in worker_list
        ) == 1
        selector: dict[str, str] = {}
        if fleet_worker_ref:
            selector = {"fleet_worker_ref": fleet_worker_ref}
        elif unique_worker_name:
            selector = {"worker": worker_name}
        if work_group_id and selector:
            integration_state = str(unintegrated_worker.get("integration_state") or "")
            next_action = {
                "tool": "patchbay_worker_inspect",
                "reason": "Review this worker's evidence and integration state before deciding its disposition.",
                "arguments": {
                    "work_group_id": work_group_id,
                    **selector,
                    "view": (
                        "integration_preview"
                        if integration_state == "not_integrated"
                        else "diagnostics"
                    ),
                },
            }
        else:
            next_action = (
                "List the workers in this group, choose one with unintegrated or uncertain "
                "changes, and inspect that named worker before deciding whether to integrate, "
                "preserve, or explicitly discard its work."
            )
    elif not worker_list:
        reason = "workers_not_started"
        next_action = (
            "Define the team's shared brief and at least one worker-specific mission, "
            "generate fresh stable idempotency keys for the batch and each worker, then "
            f"call patchbay_worker_start_batch for work group {work_group_id or 'selected above'}."
        )
    else:
        reason = "manager_review_or_close_required"
        next_action = (
            "Review the worker reports and request corrections when needed. Once the "
            "definition of done is satisfied, choose the truthful outcome, write a durable "
            "summary, disposition every worker, generate a fresh stable idempotency key, "
            f"and call patchbay_work_group_close for work group {work_group_id or 'selected above'}."
        )

    final_response_allowed = not group_open or mode == "asynchronous_handoff"
    return {
        "execution_mode": mode,
        "definition_of_done": str(group.get("definition_of_done") or group.get("goal") or ""),
        "source_contract": deepcopy(group.get("source_contract") or {}),
        "combined_verification": deepcopy(group.get("combined_verification") or validate_combined_verification(group.get("source_contract") or {}, None)),
        "work_remaining": group_open,
        "manager_must_continue": group_open and mode == "end_to_end",
        "final_response_allowed": final_response_allowed,
        "reason": reason,
        "activity": activity["activity"],
        "activity_counts": counts,
        "recommended_next_action": next_action,
    }


def validate_close_dispositions(
    workers: Iterable[Mapping[str, Any]],
    dispositions: Mapping[str, str],
    *,
    outcome: str,
) -> dict[str, Any]:
    missing: list[str] = []
    invalid: dict[str, str] = {}
    blockers: list[dict[str, str]] = []
    normalized = {str(key): str(value) for key, value in dispositions.items()}
    for worker in workers:
        worker_ref = str(worker.get("fleet_worker_ref") or worker.get("worker_id") or "")
        if not worker_ref:
            blockers.append({"worker": "unknown", "reason": "missing_immutable_worker_ref"})
            continue
        disposition = normalized.get(worker_ref, "")
        if not disposition:
            missing.append(worker_ref)
            continue
        if disposition not in ACCEPTABLE_CLOSE_DISPOSITIONS:
            invalid[worker_ref] = disposition
            continue
        turn_state = str(worker.get("turn_state") or "none")
        liveness = str(worker.get("liveness") or "terminal")
        integration = str(worker.get("integration_state") or "not_applicable")
        if disposition == "integrated" and integration != "applied_to_checkout":
            blockers.append({"worker": worker_ref, "reason": "integrated_disposition_requires_delivery_evidence"})
        if turn_state in ACTIVE_TURN_STATES and disposition != "leave_running":
            blockers.append({"worker": worker_ref, "reason": "active_worker_requires_leave_running_or_stop"})
        if liveness in UNCERTAIN_LIVENESS:
            blockers.append({"worker": worker_ref, "reason": "uncertain_worker_requires_recovery"})
        if integration in {"not_integrated", "uncertain"} and disposition not in {
            "integrated",
            "discarded_explicitly",
            "stopped_preserved",
            "leave_running",
        }:
            blockers.append({"worker": worker_ref, "reason": "unintegrated_changes_need_disposition"})
        # ``reviewed_failure`` is itself the manager's explicit, durable review
        # decision in the close request. It must not depend on an Edge-private
        # projection field that no public manager tool can mutate.
    if str(outcome).lower() in {"complete", "completed", "success", "done"}:
        for worker_ref, disposition in normalized.items():
            if disposition == "stopped_preserved":
                blockers.append({"worker": worker_ref, "reason": "preserved_unfinished_work_blocks_complete"})
    return {
        "accepted": not missing and not invalid and not blockers,
        "missing_dispositions": missing,
        "invalid_dispositions": invalid,
        "blockers": blockers,
        "dispositions": normalized,
    }


def create_successor_group(
    predecessor: Mapping[str, Any],
    *,
    machine_id: str,
    edge_generation: str,
    reason: str,
    successor_id: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    predecessor_id = str(predecessor.get("work_group_id") or "")
    if not predecessor_id:
        raise ValueError("predecessor work_group_id is required")
    if not machine_id or not edge_generation:
        raise ValueError("successor machine_id and edge_generation are required")
    timestamp = float(now if now is not None else time.time())
    successor_id = str(successor_id or f"group_{secrets.token_hex(10)}")
    return {
        "work_group_id": successor_id,
        "title": str(predecessor.get("title") or "") + " (successor)",
        "goal": str(predecessor.get("goal") or ""),
        "status": "active",
        "visibility": str(predecessor.get("visibility") or "private"),
        "routing_policy": str(predecessor.get("routing_policy") or "keep_together"),
        "shared_write_policy": str(predecessor.get("shared_write_policy") or "serialized"),
        "execution_mode": str(predecessor.get("execution_mode") or "end_to_end"),
        "definition_of_done": str(
            predecessor.get("definition_of_done") or predecessor.get("goal") or ""
        ),
        "source_contract": deepcopy(predecessor.get("source_contract") or {}),
        "workspace_ref": str(predecessor.get("workspace_ref") or ""),
        "pinned_machine_id": machine_id,
        "pinned_edge_generation": edge_generation,
        "supersedes": predecessor_id,
        "reassignment_reason": str(reason or ""),
        "created_at": timestamp,
        "updated_at": timestamp,
        "lanes": {},
        "worker_refs": [],
        "predecessor_snapshot": {
            "pinned_machine_id": predecessor.get("pinned_machine_id"),
            "pinned_edge_generation": predecessor.get("pinned_edge_generation"),
            "worker_refs": deepcopy(list(predecessor.get("worker_refs") or [])),
        },
    }
