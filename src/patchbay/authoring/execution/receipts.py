"""Execution-owned receipts bind every result to one immutable code/profile identity."""
from __future__ import annotations

import json
from copy import deepcopy

from ..errors import require
from ..persistence.store import TERMINAL
from ..util import atomic_write, canonical, fingerprint, utc_now


def new_receipt(store, run: dict, policy_digest: str) -> dict:
    return {"format_version": 1, "issuer": "patchbay-author-executor",
            "instance_id": store.identity["instance_id"], "operation_id": run["id"],
            "task_id": run["task_id"], "revision": run["revision"],
            "commit": run["commit_oid"], "tree": run["tree_oid"],
            "profile_digest": run["profile_digest"], "policy_digest": policy_digest,
            "environment": None, "environment_digest": None, "setup": [], "checks": [],
            "source_changes_during_checks": [], "cleanup": "not_required",
            "classification": "CHECKS_INCOMPLETE", "state": "failed",
            "resource_usage": {"observation_state": "unavailable", "method": None,
                               "rss_method": None,
                               "cpu_cores_peak": None, "rss_bytes_peak": None,
                               "pids_peak": None, "disk_bytes_peak": None,
                               "samples": 0, "unavailable": ["CPU", "RSS", "PID", "disk"]},
            "failure_causes": [],
            "started": utc_now(), "completed": None,
            "delegated_inference_by_author_service": False,
            "verification_receipt": {"outcome": "incomplete", "source_unchanged": False}}


def classify(receipt: dict, required: set[str]) -> None:
    all_results = receipt["setup"] + receipt["checks"]
    causes = []
    if receipt.get("error"):
        causes.append({"stage": "supervisor", "code": receipt["error"].get("code", "EXECUTOR_ERROR"),
                       "message": receipt["error"].get("message", "")[:1000]})
    for row in all_results:
        if row.get("failure_kind"):
            causes.append({"stage": row.get("stage", "check"), "check_id": row.get("id"),
                           "code": row["failure_kind"], "reason": row.get("reason")})
        elif row.get("reason"):
            causes.append({"stage": row.get("stage", "check"), "check_id": row.get("id"),
                           "code": row.get("reason"), "reason": row.get("reason")})
    recorded_required = {row.get("id") for row in receipt["checks"] if row.get("id")}
    for check_id in sorted(required - recorded_required):
        causes.append({"stage": "checks", "check_id": check_id, "code": "REQUIRED_CHECK_NOT_RECORDED"})
    if receipt.get("cleanup") == "unresolved":
        causes.append({"stage": "cleanup", "code": "EXECUTION_CLEANUP_UNRESOLVED"})
    if receipt.get("source_changes_during_checks"):
        causes.append({"stage": "source_integrity", "code": "SOURCE_CHANGED_DURING_CHECKS"})
    receipt["failure_causes"] = causes
    environment_codes = {"BLOCKED_ENVIRONMENT", "MISSING_EXECUTABLE", "EXECUTABLE_NOT_RUNNABLE", "DOCKER_UNAVAILABLE",
                         "PID_LIMIT_EXCEEDED",
                         "OOM_KILLED", "DISK_EXHAUSTED", "DISK_BUDGET_EXCEEDED",
                         "DISK_ACCOUNTING_INCOMPLETE"}
    environment_failure = any(item.get("code") in environment_codes for item in causes)
    if receipt["cleanup"] == "unresolved":
        receipt.update(classification="EXECUTION_UNKNOWN", state="unknown_execution")
    elif any(row["state"] == "cancelled" for row in all_results):
        receipt.update(classification="CANCELLED", state="cancelled")
    elif any(row["state"] == "timed_out" for row in all_results):
        receipt.update(classification="TIMED_OUT", state="timed_out")
    elif any(row.get("verification") != "passed" for row in receipt["setup"]):
        receipt.update(classification="BLOCKED_ENVIRONMENT", state="blocked")
    elif receipt.get("error", {}).get("code") == "BLOCKED_ENVIRONMENT":
        receipt.update(classification="BLOCKED_ENVIRONMENT", state="blocked")
    elif environment_failure:
        receipt.update(classification="BLOCKED_ENVIRONMENT", state="blocked")
    elif receipt.get("source_changes_during_checks"):
        receipt.update(classification="CHECKS_INCOMPLETE", state="failed")
    elif any(row.get("verification") == "failed" and row.get("required") for row in receipt["checks"]):
        receipt.update(classification="CHECKS_FAILED", state="failed")
    elif receipt.get("error") or not required <= {row["id"] for row in receipt["checks"] if row.get("verification") == "passed"}:
        receipt.update(classification="CHECKS_INCOMPLETE", state="failed")
    else:
        receipt.update(classification="READY_FOR_REVIEW", state="completed")
    receipt["verification_receipt"] = {
        "outcome": "passed" if receipt["classification"] == "READY_FOR_REVIEW" else "incomplete" if receipt["classification"] != "CHECKS_FAILED" else "failed",
        "source_unchanged": not bool(receipt["source_changes_during_checks"]),
        "required_checks": sorted(required), "commit": receipt["commit"], "tree": receipt["tree"],
        "profile_digest": receipt["profile_digest"], "environment_digest": receipt["environment_digest"]}


def referenced_hashes(receipt: dict) -> list[str]:
    hashes = []
    for check in receipt.get("setup", []) + receipt.get("checks", []):
        for stream in ("stdout", "stderr"):
            if check.get(stream, {}).get("sha256"):
                hashes.append(check[stream]["sha256"])
        if check.get("report_sha256"):
            hashes.append(check["report_sha256"])
    return sorted(set(hashes))


def record_receipt(store, operation_id: str, receipt: dict) -> dict:
    """Persist immutable evidence before its short authoritative transaction.

    The maintenance read lock protects referenced bytes against GC. A cancel
    racing serialization retries preparation; hashing never holds the SQLite
    writer lock. Old-policy receipts remain historical evidence, not current
    task qualification.
    """
    from ..persistence.locks import file_lock
    original = deepcopy(receipt)
    original["completed"] = original.get("completed") or utc_now()
    with file_lock(store.home / "locks/maintenance", shared=True):
        for _ in range(3):
            run = store.one("SELECT * FROM runs WHERE id=?", (operation_id,))
            operation = store.one("SELECT * FROM operations WHERE id=?", (operation_id,))
            require(original.get("issuer") == "patchbay-author-executor"
                    and original.get("instance_id") == store.identity["instance_id"]
                    and original.get("operation_id") == operation_id
                    and original.get("task_id") == run["task_id"] and original.get("revision") == run["revision"]
                    and original.get("commit") == run["commit_oid"] and original.get("tree") == run["tree_oid"]
                    and original.get("profile_digest") == run["profile_digest"],
                    "EVIDENCE_INVALID", "Receipt identity does not match its admitted execution")
            task = store.one("SELECT * FROM tasks WHERE id=?", (run["task_id"],))
            admitted_policy = json.loads(operation["payload"]).get("policy", task["policy_digest"])
            require(original.get("policy_digest") == admitted_policy, "EVIDENCE_INVALID", "Receipt policy differs from admitted execution")
            if operation["state"] in TERMINAL and operation["state"] != "unknown_execution":
                return store.public_operation(operation)
            prepared = deepcopy(original)
            cancelled = bool(operation["cancel_requested"])
            if cancelled and prepared["cleanup"] != "unresolved":
                prepared.update(classification="CANCELLED", state="cancelled")
                prepared["verification_receipt"]["outcome"] = "incomplete"
            require(prepared["state"] in TERMINAL, "EVIDENCE_INVALID", "Receipt is not terminal")
            require(prepared["classification"] != "READY_FOR_REVIEW" or (
                        prepared["cleanup"] == "proven" and not prepared["source_changes_during_checks"]
                        and prepared.get("environment_digest") and prepared["verification_receipt"]["outcome"] == "passed"),
                    "EVIDENCE_INVALID", "A pass is missing source, environment, or cleanup proof")
            hashes = referenced_hashes(prepared)
            for sha in hashes:
                require(store.blobs.verify(sha), "EVIDENCE_INVALID", "Receipt references unavailable or corrupt log/report bytes", sha256=sha)
            data = canonical(prepared)
            require(len(data) <= 32 * 1024**2, "RESOURCE_LIMIT", "Execution receipt exceeds its size contract")
            sha = store.blobs.put(data)
            with store.transaction() as conn:
                current = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
                if current["state"] in TERMINAL and current["state"] != "unknown_execution":
                    return store.public_operation(dict(current))
                if bool(current["cancel_requested"]) != cancelled:
                    continue
                store.reference(conn, "receipt:" + operation_id, sha, *hashes)
                result = {"operation_id": operation_id, "task_id": run["task_id"], "revision": run["revision"],
                          "classification": prepared["classification"], "receipt_sha": sha,
                          "commit": run["commit_oid"], "tree": run["tree_oid"], "cleanup": prepared["cleanup"]}
                conn.execute("UPDATE runs SET state=?,cleanup=?,receipt_sha=?,environment_digest=?,updated=? WHERE id=?",
                             (prepared["state"], prepared["cleanup"], sha, prepared.get("environment_digest"), utc_now(), operation_id))
                conn.execute("UPDATE operations SET state=?,phase='terminal',result=?,error=?,updated=? WHERE id=?",
                             (prepared["state"], canonical(result).decode(), canonical(prepared["error"]).decode() if prepared.get("error") else None,
                              utc_now(), operation_id))
                conn.execute("UPDATE tasks SET status=?,updated=? WHERE id=? AND current_revision=? AND current_commit=? AND policy_digest=?",
                             ("ready_for_review" if prepared["classification"] == "READY_FOR_REVIEW" else "needs_correction",
                              utc_now(), run["task_id"], run["revision"], run["commit_oid"], admitted_policy))
                store.event(conn, "checks.terminal", run["task_id"], result)
            return store.operation(operation_id)
    require(False, "EXECUTION_UNKNOWN", "Cancellation changed repeatedly during receipt persistence; retain ownership fencing and reconcile")
