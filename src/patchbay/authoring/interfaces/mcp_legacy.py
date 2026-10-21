"""Author-only MCP catalog and JSON-RPC dispatch, independent of legacy protocols."""
from __future__ import annotations

import json
from typing import Any
from pydantic import ValidationError

from ..errors import AuthorError, require
from ..execution.runner import Checks
from ..feedback.export import Feedback
from ..packages.ingestion import NativeDownloader
from ..revisions.drafts import Drafts
from ..service import AuthorService
from .auth import Access
from . import inputs as I
from .outputs import CONTRACTS
from .catalog import identity as catalog_identity, OUTPUT_PROFILE, OUTPUT_PROFILES
from ..publication.git import Publisher
from .instructions import READ_ONLY, WRITABLE

CATALOG = [
    ("author_capabilities", I.Empty, "Inspect Author Mode transport, execution, and qualification status. Does not start work.", False),
    ("author_list_tasks", I.Page, "List a bounded page of durable author tasks. Use returned next_offset for continuation.", False),
    ("author_get_task", I.TaskRef, "Inspect a task, its exact current revision, approved policy, and pinned dependencies. No execution is scheduled.", False),
    ("author_read_file", I.ReadFile, "Read a bounded file window from an exact context or draft version. Omitted source remains available at the same pinned commit.", False),
    ("author_search", I.Search, "Perform a bounded literal source search at an immutable context identity. Results explicitly indicate partial coverage.", False),
    ("author_get_feedback", I.FeedbackPage, "Read canonical check evidence for the requested task revision. Missing or unknown evidence is never a pass.", False),
    ("author_operation_status", I.OperationRef, "Observe one durable operation without launching, reconciling, cancelling, or retrying any work.", False),
    ("author_recover_task", I.RecoveryRef, "Recover a current factual task capsule, active and uncertain effects, ownership, evidence and required judgment. Recent history defaults to 20 operations; use author_recent_operations pages for the full history. Pure read; do not replace or replay existing work.", False),
    ("author_recent_operations", I.RecentOperations, "Discover recent accepted task operations after a lost response or operation ID. Continue with the returned versioned cursor; stale pages fail explicitly.", False),
    ("author_read_log", I.ReadLog, "Read a bounded page of an owned operation's retained log. Follow byte offsets; truncation never means complete original output.", False),
    ("author_publication_preview", I.PublicationPreview, "Read exact checked candidate and remote publication preconditions. Does not approve, push, create a PR or merge.", False),
    ("author_create_task", I.CreateTask, "Create an implementation task within an operator-registered repository and its existing policy. Cannot register a new repository or widen authority.", True),
    ("author_claim_task", I.Claim, "Claim or renew a task writer lease. Explicit takeover invalidates prior writer epochs.", True),
    ("author_submit_package", I.NativeSubmission, "Import a supported native file and atomically accept its validated implementation. Task policy may queue approved checks. Does not delegate inference or publish code.", True),
    ("author_apply_edits", I.DraftEdit, "Apply exact, bounded edits to a versioned isolated draft. A validation rejection is terminal for this idempotency key; correct the request and use a new key. The draft is not an accepted candidate until sealed.", True),
    ("author_seal_draft", I.DraftSeal, "Seal the exact current draft through standard package validation and candidate acceptance. A validation rejection is terminal for this idempotency key; correct the request and use a new key.", True),
    ("author_run_checks", I.CheckRequest, "Queue approved deterministic checks for an exact candidate. Required checks cannot be removed. Returns an operation ID, not a pass assertion.", True),
    ("author_cancel_operation", I.Cancel, "Request cancellation of an owned check run, retaining cleanup evidence. Never stop unrelated processes.", True),
    ("author_export_context", I.ExportContext, "Create a durable pinned context archive for the exact task revision and paths. Use its returned context ID for source reads and the standard package builder. This is an explicit write.", True),
    ("author_export_feedback", I.ExportFeedback, "Create a self-contained feedback and source continuation archive. Keeps exact revision/evidence identities for a fresh chat; never launches a model.", True),
    ("author_approve_publication", I.Approval, "Operator-only explicit review/approval of an exact verified candidate. Requires actor and current lease; no push, PR or merge.", True),
    ("author_publish", I.Publish, "Publish only an operator-approved exact candidate under its registered policy. No PR creation, default-branch mutation, merge or deployment.", True),
]


class MCP:
    def __init__(self, service: AuthorService, *, output_profile: str = OUTPUT_PROFILE):
        require(output_profile in OUTPUT_PROFILES, "INVALID_REQUEST", "Unknown output profile")
        self.service, self.access = service, Access(service.store)
        self.output_profile = output_profile

    def tools(self, role: str) -> list[dict]:
        writable = role in {"operator", "author"} and self.access.qualified(role, output_profile=self.output_profile) is not None
        catalog = catalog_identity(self.output_profile)
        result = []
        for name, contract, description, write in CATALOG:
            if write and not writable:
                continue
            if name in {"author_approve_publication", "author_publish"} and role != "operator":
                continue
            tool = {"name": name, "description": description, "inputSchema": contract.model_json_schema(),
                    "outputSchema": CONTRACTS[name].model_json_schema(),
                    "annotations": {"readOnlyHint": not write, "destructiveHint": write,
                                    "idempotentHint": True,
                                    "openWorldHint": name in {"author_submit_package", "author_run_checks"}}}
            tool["_meta"] = {"patchbay/catalog": catalog}
            if name == "author_submit_package":
                tool["_meta"]["openai/fileParams"] = ["artifact_file"]
            result.append(tool)
        return result

    def call(self, role: str, name: str, arguments: dict) -> dict:
        match = next((entry for entry in CATALOG if entry[0] == name), None)
        require(match is not None, "UNKNOWN_TOOL", "Unknown author tool")
        _, contract, _, write = match
        if write:
            self.access.require_mutation(role, mcp=True, output_profile=self.output_profile, operator_only=name in {"author_approve_publication", "author_publish"})
        params = contract.model_validate(arguments).wire()
        service = self.service
        if name == "author_capabilities":
            qualification = self.access.qualified(role, output_profile=self.output_profile)
            return {**service.capabilities(), "mcp_write_actions": role in {"operator", "author"} and qualification is not None,
                    "qualification": qualification, "catalog": catalog_identity(self.output_profile)}
        if name == "author_list_tasks": return service.tasks.list(**params)
        if name == "author_get_task": return {**service.tasks.get(**params), "recovery": service.recovery.get(**params)}
        if name == "author_recover_task": return service.recovery.get(**params)
        if name == "author_recent_operations": return service.recovery.operations(**params)
        if name == "author_read_log": return Checks(service.store).log(**params)
        if name == "author_publication_preview": return Publisher(service.store).preview(**params)
        if name == "author_approve_publication": return Publisher(service.store).approve(**params)
        if name == "author_publish": return Publisher(service.store).apply(**params)
        if name == "author_export_context":
            context = service.snapshots.export(**params)
            return {"context_id": context["id"], "revision": context["revision"], "blob_sha": context["bundle_sha"],
                    "inventory_sha": context["inventory_sha"], "download": "/api/blobs/" + context["bundle_sha"],
                    "request_operation_id": context["request_operation_id"]}
        if name == "author_export_feedback": return Feedback(service.store).export(**params)
        if name == "author_create_task": return service.tasks.create(**params)
        if name == "author_read_file":
            if params.get("draft_id"):
                params["version"] = params.pop("draft_version")
                params.pop("context_id", None)
                return Drafts(service.store).read(**params)
            params.pop("draft_id", None); params.pop("draft_version", None)
            return service.snapshots.read(**params)
        if name == "author_search": return service.snapshots.search(**params)
        if name == "author_get_feedback": return Feedback(service.store).page(**params)
        if name == "author_operation_status": return service.store.operation(**params)
        if name == "author_claim_task": return service.leases.claim(**params)
        if name == "author_apply_edits": return Drafts(service.store).apply(**params)
        if name == "author_seal_draft": return Drafts(service.store).seal(**params)
        if name == "author_run_checks": return Checks(service.store).submit(**params)
        if name == "author_cancel_operation": return Checks(service.store).cancel(**params)
        if name == "author_submit_package":
            file = params.pop("artifact_file")
            # Validate authority before performing any external file retrieval.
            service.leases.assert_owned(params["task_id"], params["token"], params["epoch"])
            path = NativeDownloader(service.store).download(file)
            try:
                return service.materializer.submit(path=path, **params)
            finally:
                path.unlink(missing_ok=True)
        raise AuthorError("UNKNOWN_TOOL", "Unknown author tool")

    def handle(self, role: str, message: dict) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Expected a JSON-RPC 2.0 request object"}}
        method, request_id = message["method"], message.get("id")
        if "id" not in message:
            # Notifications never start durable domain work, even with a forged tools/call.
            return None
        if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request ID"}}
        try:
            params = message.get("params") or {}
            require(isinstance(params, dict), "INVALID_REQUEST", "Expected object parameters")
            if method == "initialize":
                requested = params.get("protocolVersion", "2025-03-26")
                version = requested if requested in {"2025-03-26", "2025-06-18", "2025-11-25"} else "2025-03-26"
                writable = role in {"operator", "author"} and self.access.qualified(role, output_profile=self.output_profile) is not None
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "patchbay-author", "version": "0.1.0", "_meta": {"patchbay/catalog": catalog_identity(self.output_profile)}},
                          "instructions": WRITABLE if writable else READ_ONLY}
            elif method == "ping": result = {}
            elif method == "tools/list": result = {"tools": self.tools(role)}
            elif method == "tools/call":
                try:
                    payload = self.call(role, params.get("name"), params.get("arguments", {}))
                    # Validate the full result before exposing a successful tool call.
                    try:
                        CONTRACTS[params["name"]].model_validate(payload)
                    except ValidationError as exc:
                        raise AuthorError("INVALID_OUTPUT", "The server result violated its published output contract; no success is asserted") from exc
                    text = _summary_text(params["name"], payload) if self.output_profile == "structured-summary-v2" else payload
                    result = {"content": [{"type": "text", "text": json.dumps(text, ensure_ascii=False)}],
                              "structuredContent": payload, "isError": False}
                    require(len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 8 * 1024**2,
                            "RESOURCE_LIMIT", "Result exceeds the MCP response budget; request a smaller page or export the exact artifact through the operator route",
                            effect_returned=bool(next(entry[3] for entry in CATALOG if entry[0] == params["name"])),
                            result_identity={key: payload[key] for key in ("id", "task_id", "revision", "operation_id", "request_operation_id", "commit", "blob_sha", "bundle_sha", "context_id") if key in payload})
                except (AuthorError, ValidationError, UnicodeDecodeError) as exc:
                    error = exc.as_dict() if isinstance(exc, AuthorError) else {"code": "INVALID_REQUEST", "message": "Tool arguments or text content violate the contract"}
                    result = {"content": [{"type": "text", "text": json.dumps(error)}], "structuredContent": error, "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except AuthorError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": exc.message, "data": exc.as_dict()}}


_SUMMARY_FIELDS = (
    "task_id", "id", "operation_id", "request_operation_id", "revision", "version", "commit", "tree",
    "context_id", "draft_id", "path", "sha256", "blob_sha", "receipt_sha", "state", "phase", "status",
    "classification", "next_action", "required_judgment", "next_offset", "next_start_line", "start_line",
    "total_lines", "bytes", "observed_bytes", "has_more", "partial", "truncated", "result_partial",
    "error_partial", "result_bytes", "error_bytes", "operation_count", "operations_partial",
    "active_operation_count", "active_operations_partial", "total_check_results", "total", "cursor", "watermark",
)
_SUMMARY_TEXT_MAX = 512


def _bounded_summary_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str) and len(value.encode("utf-8")) > _SUMMARY_TEXT_MAX:
        raw = value.encode("utf-8")[:_SUMMARY_TEXT_MAX]
        while raw:
            try:
                return raw.decode("utf-8"), True
            except UnicodeDecodeError:
                raw = raw[:-1]
        return "", True
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value, False
    return None, False


def _summary_text(name: str, payload: dict) -> dict[str, Any]:
    """Expose bounded identity/progress metadata while keeping full content structured."""
    summary = {"tool": name}
    truncated = []
    for key in _SUMMARY_FIELDS:
        if key not in payload:
            continue
        value, was_truncated = _bounded_summary_value(payload[key])
        if isinstance(payload[key], (str, int, float, bool)) or payload[key] is None:
            summary[key] = value
            if was_truncated:
                truncated.append(key)
    error = payload.get("error")
    if isinstance(error, dict):
        error_summary = {}
        for key in ("code", "message"):
            if key in error and isinstance(error[key], str):
                error_summary[key], was_truncated = _bounded_summary_value(error[key])
                if was_truncated:
                    truncated.append("error." + key)
        if error_summary:
            summary["error_summary"] = error_summary
    recovery = payload if name == "author_recover_task" else payload.get("recovery") if name == "author_get_task" else None
    if isinstance(recovery, dict):
        references = {}
        for collection in ("operations", "active_operations"):
            for operation in recovery.get(collection, []):
                if not isinstance(operation, dict) or not (operation.get("result_partial") or operation.get("error_partial")):
                    continue
                operation_id = operation.get("id")
                if not isinstance(operation_id, str):
                    continue
                references[operation_id] = {
                    "operation_id": operation_id,
                    "result_partial": bool(operation.get("result_partial")),
                    "result_bytes": operation.get("result_bytes"),
                    "error_partial": bool(operation.get("error_partial")),
                    "error_bytes": operation.get("error_bytes"),
                    "read_tool": "author_operation_status",
                }
        refs = list(references.values())
        if refs:
            summary["operation_detail_reads"] = refs[:20]
            if len(refs) > 20:
                summary["operation_detail_reads_truncated"] = True
    if truncated:
        summary["summary_truncated_fields"] = truncated
    return summary
