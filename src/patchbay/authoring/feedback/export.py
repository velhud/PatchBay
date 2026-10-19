"""Canonical execution feedback, never a model-authored success assertion."""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

from ..errors import require
from ..packages.builder import _write_zip
from ..persistence.gates import mutation
from ..repositories import Repositories
from ..snapshots import Snapshots
from ..tasks import Tasks
from ..util import canonical


class Feedback:
    def __init__(self, store):
        self.store = store
        self.tasks = Tasks(store)

    def get(self, task_id: str, *, revision: int | None = None) -> dict:
        task = self.tasks.get(task_id)
        revision = task["current_revision"] if revision is None else revision
        candidate = self.tasks.revision(task_id, revision)
        runs = self.store.rows("SELECT * FROM runs WHERE task_id=? AND revision=? ORDER BY created DESC,id DESC LIMIT 1", (task_id, revision))
        result = {"task_id": task_id, "revision": revision, "commit": candidate["commit_oid"],
                  "tree": candidate["tree_oid"], "policy_digest": task["policy_digest"],
                  "classification": "CHECKS_INCOMPLETE", "receipt_sha": None, "receipt": None,
                  "verification_receipt": {"outcome": "incomplete", "source_unchanged": None},
                  "next_action": "Run the approved checks on this exact revision"}
        if not runs:
            return result
        run = runs[0]
        result["operation"] = self.store.operation(run["id"])
        if not run["receipt_sha"]:
            result["classification"] = {"unknown_execution": "EXECUTION_UNKNOWN", "cancelled": "CANCELLED", "blocked": "BLOCKED_ENVIRONMENT"}.get(run["state"], "CHECKS_INCOMPLETE")
            result["next_action"] = "Inspect the existing operation; do not infer success or relaunch an uncertain execution"
            return result
        receipt = json.loads(self.store.blobs.read(run["receipt_sha"]))
        require(receipt["commit"] == candidate["commit_oid"] and receipt["tree"] == candidate["tree_oid"]
                and receipt["policy_digest"] == task["policy_digest"], "EVIDENCE_INVALID", "Stored feedback does not match the requested candidate")
        result.update(classification=receipt["classification"], receipt_sha=run["receipt_sha"], receipt=receipt,
                      verification_receipt=receipt["verification_receipt"],
                      next_action="Review this exact candidate for publication" if receipt["classification"] == "READY_FOR_REVIEW" else "Implement corrections against the current pinned context and return a new change package")
        return result

    def page(self, task_id: str, *, revision: int | None = None, offset: int = 0, limit: int = 10) -> dict:
        require(offset >= 0 and 1 <= limit <= 50, "INVALID_REQUEST", "Invalid feedback page")
        result = self.get(task_id, revision=revision)
        receipt = result.get("receipt")
        if receipt is None:
            return result
        combined = [("setup", check) for check in receipt["setup"]] + [("checks", check) for check in receipt["checks"]]
        result["receipt"] = {k: v for k, v in receipt.items() if k not in {"setup", "checks"}}
        result["check_results"] = [{"stage": stage, **check} for stage, check in combined[offset:offset + limit]]
        result["next_offset"] = offset + limit if offset + limit < len(combined) else None
        result["total_check_results"] = len(combined)
        return result

    @staticmethod
    def markdown(result: dict) -> str:
        lines = ["# Author implementation feedback", "", "Status: **" + result["classification"] + "**", "",
                 "Task: `" + result["task_id"] + "`", "Revision: " + str(result["revision"]),
                 "Commit: `" + result["commit"] + "`", "Tree: `" + result["tree"] + "`", "",
                 "These results were recorded by the deterministic executor, not supplied by the implementation author.", ""]
        receipt = result.get("receipt") or {}
        for stage in ("setup", "checks"):
            for check in receipt.get(stage, []):
                lines.extend(["## " + stage + ": " + check["id"],
                              "Outcome: " + check.get("verification", "incomplete") + "; process: " + check["state"], ""])
                for failure in check.get("failures", []):
                    lines.extend(["Test: " + failure["test"], "```text", failure["message"], failure["text"], "```", ""])
                if check.get("verification") != "passed":
                    for stream in ("stdout", "stderr"):
                        text = check.get(stream, {}).get("tail", "")
                        if text:
                            lines.extend([stream + " tail:", "```text", text, "```", ""])
        if receipt.get("source_changes_during_checks"):
            lines.extend(["## Source was modified during verification", "```json", json.dumps(receipt["source_changes_during_checks"], indent=2), "```"])
        if receipt.get("error"):
            lines.extend(["## Execution problem", "```json", json.dumps(receipt["error"], indent=2), "```"])
        lines.extend(["", "## Next action", result["next_action"], "",
                      "Implement the correction yourself. Do not return another plan for a Codex worker. Use the bundled current context and package builder."])
        return "\n".join(lines) + "\n"

    @mutation
    def export(self, task_id: str, *, revision: int | None = None) -> dict:
        result = self.get(task_id, revision=revision)
        context = Snapshots(self.store).export(task_id, revision=result["revision"])
        continuation = ("Continue implementation for task " + task_id + ". Read FEEDBACK.md and RESULT.json, then task-context/TASK.md. "
                        "Use context " + context["id"] + " at revision " + str(result["revision"]) + ", commit " + result["commit"] + ". "
                        "Prepare an edited copy with the included builder, correct source/tests, and return the new change ZIP. "
                        "Do not modify the immutable originals or delegate to another inference worker.\n")
        members = {"RESULT.json": (canonical(result), 0o644), "FEEDBACK.md": (self.markdown(result).encode(), 0o644),
                   "CONTINUE.md": (continuation.encode(), 0o644)}
        # Include source as a nested archive to keep export memory bounded for large contexts.
        members["task-context.zip"] = (self.store.blobs.read(context["bundle_sha"]), 0o644)
        with tempfile.TemporaryDirectory(prefix="feedback-", dir=self.store.home / "staging") as temporary:
            path = Path(temporary) / "feedback.zip"
            _write_zip(path, members)
            sha = self.store.blobs.put_file(path, limit=self.store.limits.archive.expanded_bytes + 32 * 1024**2)
        with self.store.transaction() as conn:
            self.store.reference(conn, "feedback:" + task_id + ":" + str(result["revision"]) + ":" + sha, sha, context["bundle_sha"])
            self.store.event(conn, "feedback.exported", task_id, {"revision": result["revision"], "sha256": sha})
        return {"task_id": task_id, "revision": result["revision"], "classification": result["classification"],
                "blob_sha": sha, "context_id": context["id"], "continuation": continuation}

    def diff(self, task_id: str, *, revision: int | None = None, path: str | None = None) -> dict:
        task = self.tasks.get(task_id)
        revision = task["current_revision"] if revision is None else revision
        candidate = self.tasks.revision(task_id, revision)
        return {"task_id": task_id, "revision": revision, "commit": candidate["commit_oid"],
                **Repositories(self.store).git(task["repository_id"]).diff(task["origin_commit"], candidate["commit_oid"], path=path)}
