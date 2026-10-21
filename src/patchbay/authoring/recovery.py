"""Continuously available factual recovery; never depends on a final model call."""
from __future__ import annotations

import json

from .errors import AuthorError, require
from .authority import assert_task_scope
from .persistence.store import TERMINAL
from .util import fingerprint, safe_id, utc_now
from .persistence.state_clock import watermark as operation_generation


class Recovery:
    def __init__(self, store):
        self.store = store

    def operations(self, task_id: str, *, limit: int = 50, offset: int = 0, cursor: str | None = None) -> dict:
        safe_id(task_id)
        assert_task_scope(task_id)
        require(1 <= limit <= 200 and offset >= 0, "INVALID_REQUEST", "Invalid operation page")
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            require(conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone(), "NOT_FOUND", "Task is unavailable")
            watermark = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
            state_watermark = self._operation_watermark(conn, task_id)
            cursor_value = fingerprint({"task_id": task_id, "watermark": watermark,
                                        "operation_state": state_watermark})
            if cursor:
                require(cursor == cursor_value,
                        "STALE_CURSOR", "Task changed between pages; recover current state and restart pagination", current_watermark=watermark)
            rows = conn.execute("SELECT " + self.store.operation_summary_columns() + " FROM operations WHERE task_id=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                                (task_id, limit + 1, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM operations WHERE task_id=?", (task_id,)).fetchone()[0]
        return {"task_id": task_id, "operations": [self.store.public_operation(dict(r), summary=True) for r in rows[:limit]],
                "total": total, "partial": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None,
                "cursor": cursor_value, "watermark": watermark}

    def _operation_watermark(self, conn, task_id: str) -> int:
        # Indexed, transaction-bound generation changes on every lifecycle write.
        return operation_generation(self.store, task_id, conn=conn)

    def get(self, task_id: str, *, operation_limit: int = 50) -> dict:
        safe_id(task_id)
        assert_task_scope(task_id)
        require(1 <= operation_limit <= 200, "INVALID_REQUEST", "Invalid recovery operation limit")
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            require(task is not None, "NOT_FOUND", "Task is unavailable")
            task = dict(task)
            revision = dict(conn.execute("SELECT * FROM revisions WHERE task_id=? AND number=?", (task_id, task["current_revision"])).fetchone())
            watermark = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
            recent = [dict(r) for r in conn.execute("SELECT " + self.store.operation_summary_columns() + " FROM operations WHERE task_id=? ORDER BY created DESC,id DESC LIMIT ?", (task_id, operation_limit))]
            # Prioritize unresolved effects and checks, even beyond a large backlog.
            # Classification below uses the entire active set, not this projection.
            active = [dict(r) for r in conn.execute("SELECT " + self.store.operation_summary_columns() + " FROM operations WHERE task_id=? AND state NOT IN ('completed','failed','blocked','timed_out','cancelled') ORDER BY CASE WHEN state IN ('unknown_execution','publication_unknown') THEN 0 WHEN kind='checks.run' AND state!='queued' THEN 1 WHEN kind='checks.run' THEN 2 ELSE 3 END,created,id LIMIT 200", (task_id,))]
            count = conn.execute("SELECT COUNT(*) FROM operations WHERE task_id=?", (task_id,)).fetchone()[0]
            active_counts = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(state IN ('unknown_execution','publication_unknown')) AS uncertain, "
                "SUM(kind='checks.run' AND state NOT IN ('unknown_execution','publication_unknown')) AS checks, "
                "SUM(kind='checks.run' AND state NOT IN ('queued','unknown_execution','publication_unknown')) AS started_checks "
                "FROM operations WHERE task_id=? AND state NOT IN ('completed','failed','blocked','timed_out','cancelled')",
                (task_id,)).fetchone()
            active_count = active_counts["total"]
            uncertain_count = active_counts["uncertain"] or 0
            check_count = active_counts["checks"] or 0
            started_check_count = active_counts["started_checks"] or 0
            lease = conn.execute("SELECT label,epoch,expires FROM leases WHERE task_id=?", (task_id,)).fetchone()
            contexts = [dict(r) for r in conn.execute("SELECT id,revision,commit_oid,bundle_sha,created FROM contexts WHERE task_id=? AND revision=? ORDER BY created DESC LIMIT 10", (task_id, task["current_revision"]))]
            run = conn.execute("SELECT id,state,cleanup,receipt_sha,profile_digest,environment_digest FROM runs WHERE task_id=? AND revision=? ORDER BY created DESC,id DESC LIMIT 1", (task_id, task["current_revision"])).fetchone()
            pending_inbox = conn.execute(
                "SELECT id FROM operations WHERE task_id=? AND kind='package.inbox' AND state='awaiting_approval' "
                "ORDER BY created,id LIMIT 1", (task_id,)).fetchone()
            approvals = [dict(r) for r in conn.execute("SELECT id,revision,commit_oid,actor,revoked,created FROM approvals WHERE task_id=? ORDER BY created DESC LIMIT 10", (task_id,))]
            publications = [dict(r) for r in conn.execute("SELECT id,commit_oid,branch,state,updated FROM publications WHERE task_id=? ORDER BY created DESC LIMIT 10", (task_id,))]
            drafts = [dict(r) for r in conn.execute("SELECT id,version,base_revision,commit_oid,state FROM drafts WHERE task_id=? AND state='open' ORDER BY updated DESC LIMIT 10", (task_id,))]
            controls = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key,value FROM metadata WHERE key IN ('author.control.state','author.control.task.'||?)", (task_id,))}
        policy = json.loads(task["policy"])
        run = dict(run) if run else None
        if uncertain_count or (run and run["cleanup"] == "unresolved"):
            state, judgment = "awaiting_controller", "Reconcile the existing uncertain operation; never relaunch or infer success"
        elif check_count:
            if not started_check_count:
                state, judgment = "awaiting_resources", "Checks are accepted and queued; inspect their cached admission blockers instead of resubmitting or claiming execution started"
            else:
                state, judgment = "executing", "Observe the existing accepted checks; no new model decision is running locally"
        elif pending_inbox:
            state, judgment = "awaiting_approval", "Review the recognized package and explicitly approve it under the current task lease"
        elif run and run["state"] == "completed" and run["receipt_sha"]:
            if self._receipt_is_current(run, task, revision):
                state, judgment = "awaiting_approval", "Review the exact candidate and canonical receipt before any authorized publication"
            else:
                state, judgment = "awaiting_controller", "The stored receipt does not prove the current candidate and policy; inspect exact feedback before treating checks as green"
        elif run and run["state"] in TERMINAL:
            state, judgment = "awaiting_controller", "Read exact feedback and implement a correction; no hidden author or worker will be started"
        else:
            state, judgment = "awaiting_input", "Obtain pinned context and deliver actual implementation or run approved checks on the current candidate"
        if controls.get("author.control.task." + task_id, {}).get("granted") is False:
            if uncertain_count or (run and run["cleanup"] == "unresolved"):
                judgment += "; the grant is also revoked, so restoring it alone does not resolve execution uncertainty"
            elif check_count:
                judgment += "; the grant is revoked, but this accepted execution remains owned and was not cancelled"
            elif pending_inbox:
                judgment += "; the task grant is revoked, so an operator must restore authority before package approval can take effect"
            elif state == "awaiting_controller":
                judgment += "; task grant is revoked; read and resolve this recorded evidence before any authorized successor effect"
            else:
                state, judgment = "awaiting_approval", "Task grant was revoked; an operator must explicitly restore authority before successor effects"
        now = utc_now()
        return {"format_version": 1, "workflow": "author", "task_id": task_id, "repository_id": task["repository_id"],
                "goal": task["goal"], "scope": {"writable_paths": policy["writable_paths"], "blocked_paths": policy["blocked_paths"]},
                "policy": policy, "policy_digest": task["policy_digest"], "dependencies": json.loads(task["dependencies"]),
                "candidate": {"revision": task["current_revision"], "commit": revision["commit_oid"], "tree": revision["tree_oid"],
                              "base_commit": task["origin_commit"], "context_id": revision["context_id"]},
                "lease": {**dict(lease), "expired": lease["expires"] <= now} if lease else None,
                "contexts": contexts, "drafts": drafts, "verification": run, "approvals": approvals, "publications": publications,
                "operations": [self.store.public_operation(r, summary=True) for r in recent],
                "active_operations": [self.store.public_operation(r, summary=True) for r in active[:200]],
                "operation_count": count, "operations_partial": count > len(recent),
                "active_operation_count": active_count, "active_operations_partial": active_count > 200,
                "state": state, "required_judgment": judgment, "controls": controls,
                "freshness": {"as_of": now, "watermark": watermark, "consistent_snapshot": True},
                "safe_reads": [{"tool": "author_get_task", "arguments": {"task_id": task_id}},
                               {"tool": "author_get_feedback", "arguments": {"task_id": task_id, "revision": task["current_revision"]}},
                               {"tool": "author_recent_operations", "arguments": {"task_id": task_id}}],
                "automatic_model_continuation": False, "merge_authorized": False}

    def _receipt_is_current(self, run: dict, task: dict, revision: dict) -> bool:
        """Only a receipt bound to today's exact candidate and task policy is green."""
        try:
            receipt = json.loads(self.store.blobs.read(run["receipt_sha"]))
        except (OSError, ValueError, UnicodeError, AuthorError):
            return False
        verification = receipt.get("verification_receipt")
        return bool(
            receipt.get("classification") == "READY_FOR_REVIEW"
            and receipt.get("state") == "completed"
            and receipt.get("task_id") == task["id"]
            and receipt.get("revision") == task["current_revision"]
            and receipt.get("commit") == revision["commit_oid"]
            and receipt.get("tree") == revision["tree_oid"]
            and receipt.get("policy_digest") == task["policy_digest"]
            and isinstance(verification, dict)
            and verification.get("outcome") == "passed"
            and verification.get("source_unchanged") is True
        )
