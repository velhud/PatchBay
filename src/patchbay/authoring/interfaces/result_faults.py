"""Read existing admission state after a response fault; never journal or retry."""
from __future__ import annotations

KINDS = {
    "author_create_task": "task.create", "author_claim_task": "lease.claim",
    "author_submit_package": "package.submit", "author_apply_edits": "draft.apply",
    "author_seal_draft": "draft.seal", "author_run_checks": "checks.run",
    "author_cancel_operation": "checks.cancel", "author_export_context": "context.export",
    "author_export_feedback": "feedback.export", "author_approve_publication": "publication.approve",
    "author_publish": "publication.apply",
}


def result_fault(service, name: str, arguments: dict, *, write: bool, too_large: bool = False) -> dict:
    """Identities come from validated arguments and authorized durable readback.

    In particular, no field from the invalid/oversized result is trusted.
    Failure to resolve an identity leaves a bounded task-discovery route.
    """
    details = {"boundary": ("effect_result_too_large" if too_large else "effect_may_exist_result_invalid")
               if write else ("read_result_too_large" if too_large else "read_result_invalid"),
               "effect_may_exist": write, "success_asserted": False}
    if write and "key" in arguments:
        details["original_key"] = arguments["key"]
    task_id = arguments.get("task_id")
    operation_id = arguments.get("operation_id")
    actions = []
    try:
        if not task_id and operation_id:
            task_id = service.store.operation(operation_id).get("task_id")
        if not task_id and arguments.get("draft_id"):
            row = service.store.one("SELECT task_id FROM drafts WHERE id=?", (arguments["draft_id"],))
            task_id = service.tasks.get(row["task_id"])["id"]
        if not task_id and arguments.get("approval_id"):
            row = service.store.one("SELECT task_id FROM approvals WHERE id=?", (arguments["approval_id"],))
            task_id = service.tasks.get(row["task_id"])["id"]
        target = task_id
        if name == "author_create_task":
            target = service.repositories.get(arguments["repository"])["id"]
        if write and target and name in KINDS:
            row = service.store.one("SELECT id FROM operations WHERE task_id=? AND kind=? AND idempotency_key=?",
                (target, KINDS[name], service.store.scoped_key(arguments["key"])), required=False)
            if row:
                operation_id = service.store.operation(row["id"])["id"]
                if name == "author_create_task":
                    task_id = service.tasks.get("task_" + operation_id[3:])["id"]
        if task_id:
            task_id = service.tasks.get(task_id)["id"]
            details["task_id"] = task_id
            actions.append({"tool": "author_recover_task", "arguments": {"task_id": task_id}})
        if operation_id:
            operation_id = service.store.operation(operation_id)["id"]
            details["operation_id"] = operation_id
            actions.insert(0, {"tool": "author_operation_status", "arguments": {"operation_id": operation_id}})
    except Exception:
        # No invented identity and no extra operation when readback is unavailable.
        details["recovery_lookup_unavailable"] = True
    # Retain the older size-error fields, now derived only from trusted reads.
    details["effect_returned"] = write
    identity = {field: details[field] for field in ("task_id", "operation_id") if field in details}
    if name == "author_operation_status" and "operation_id" in identity:
        identity["id"] = identity["operation_id"]
    elif name in {"author_create_task", "author_get_task"} and "task_id" in identity:
        identity["id"] = identity["task_id"]
    details["result_identity"] = identity
    details["recovery_actions"] = actions or [{"tool": "author_list_tasks", "arguments": {"limit": 25}}]
    details["guidance"] = ("Do not infer success or absence of an effect. Read the original admission before another mutation. An identical retry uses the original key and payload; a changed request is a separate decision."
                           if write else "Read result invalid or too large; no mutation attempted. Recover exact state or request a smaller page.")
    return details
