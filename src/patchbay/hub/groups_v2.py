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
        if state not in TERMINAL_OPERATION_STATES:
            counts["active_operations"] += 1
        if state in {"outcome_unknown", "reconciling"}:
            counts["uncertain_operations"] += 1

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
        review = str(worker.get("review_disposition") or "unreviewed")
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
        if turn_state == "failed" and review == "unreviewed" and disposition == "reviewed_failure":
            blockers.append({"worker": worker_ref, "reason": "failure_is_not_reviewed"})
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
    now: float | None = None,
) -> dict[str, Any]:
    predecessor_id = str(predecessor.get("work_group_id") or "")
    if not predecessor_id:
        raise ValueError("predecessor work_group_id is required")
    if not machine_id or not edge_generation:
        raise ValueError("successor machine_id and edge_generation are required")
    timestamp = float(now if now is not None else time.time())
    successor_id = f"group_{secrets.token_hex(10)}"
    return {
        "work_group_id": successor_id,
        "title": str(predecessor.get("title") or "") + " (successor)",
        "goal": str(predecessor.get("goal") or ""),
        "status": "active",
        "visibility": str(predecessor.get("visibility") or "private"),
        "routing_policy": str(predecessor.get("routing_policy") or "keep_together"),
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
