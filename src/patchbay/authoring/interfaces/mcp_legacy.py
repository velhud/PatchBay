"""Author-only MCP catalog and JSON-RPC dispatch, independent of legacy protocols."""
from __future__ import annotations

import json
from pydantic import ValidationError

from ..errors import AuthorError, require
from ..execution.runner import Checks
from ..feedback.export import Feedback
from ..packages.ingestion import NativeDownloader
from ..revisions.drafts import Drafts
from ..service import AuthorService
from .auth import Access
from . import inputs as I
from .instructions import READ_ONLY, WRITABLE

CATALOG = [
    ("author_capabilities", I.Empty, "Inspect Author Mode transport, execution, and qualification status. Does not start work.", False),
    ("author_list_tasks", I.Page, "List a bounded page of durable author tasks. Use returned next_offset for continuation.", False),
    ("author_get_task", I.TaskRef, "Inspect a task, its exact current revision, approved policy, and pinned dependencies. No execution is scheduled.", False),
    ("author_read_file", I.ReadFile, "Read a bounded file window from an exact context or draft version. Omitted source remains available at the same pinned commit.", False),
    ("author_search", I.Search, "Perform a bounded literal source search at an immutable context identity. Results explicitly indicate partial coverage.", False),
    ("author_get_feedback", I.FeedbackPage, "Read canonical check evidence for the requested task revision. Missing or unknown evidence is never a pass.", False),
    ("author_operation_status", I.OperationRef, "Observe one durable operation without launching, reconciling, cancelling, or retrying any work.", False),
    ("author_create_task", I.CreateTask, "Create an implementation task within an operator-registered repository and its existing policy. Cannot register a new repository or widen authority.", True),
    ("author_claim_task", I.Claim, "Claim or renew a task writer lease. Explicit takeover invalidates prior writer epochs.", True),
    ("author_submit_package", I.NativeSubmission, "Import a supported native file and atomically accept its validated implementation. Task policy may queue approved checks. Does not delegate inference or publish code.", True),
    ("author_apply_edits", I.DraftEdit, "Apply exact, bounded edits to a versioned isolated draft. The draft is not an accepted candidate until sealed.", True),
    ("author_seal_draft", I.DraftSeal, "Seal the exact current draft through standard package validation and candidate acceptance.", True),
    ("author_run_checks", I.CheckRequest, "Queue approved deterministic checks for an exact candidate. Required checks cannot be removed. Returns an operation ID, not a pass assertion.", True),
    ("author_cancel_operation", I.Cancel, "Request cancellation of an owned check run, retaining cleanup evidence. Never stop unrelated processes.", True),
]


class MCP:
    def __init__(self, service: AuthorService):
        self.service, self.access = service, Access(service.store)

    def tools(self, role: str) -> list[dict]:
        writable = role in {"operator", "author"} and self.access.qualified() is not None
        result = []
        for name, contract, description, write in CATALOG:
            if write and not writable:
                continue
            tool = {"name": name, "description": description, "inputSchema": contract.model_json_schema(),
                    "outputSchema": {"type": "object", "additionalProperties": True},
                    "annotations": {"readOnlyHint": not write, "destructiveHint": write,
                                    "idempotentHint": not write or name in {"author_create_task", "author_submit_package", "author_run_checks", "author_apply_edits", "author_seal_draft"},
                                    "openWorldHint": name in {"author_submit_package", "author_run_checks"}}}
            if name == "author_submit_package":
                tool["_meta"] = {"openai/fileParams": ["artifact_file"]}
            result.append(tool)
        return result

    def call(self, role: str, name: str, arguments: dict) -> dict:
        match = next((entry for entry in CATALOG if entry[0] == name), None)
        require(match is not None, "UNKNOWN_TOOL", "Unknown author tool")
        _, contract, _, write = match
        if write:
            self.access.require_mutation(role, mcp=True)
        params = contract.model_validate(arguments).wire()
        service = self.service
        if name == "author_capabilities":
            qualification = self.access.qualified()
            return {**service.capabilities(), "mcp_write_actions": role in {"operator", "author"} and qualification is not None,
                    "qualification": qualification}
        if name == "author_list_tasks": return service.tasks.list(**params)
        if name == "author_get_task": return service.tasks.get(**params)
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
                writable = role in {"operator", "author"} and self.access.qualified() is not None
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "patchbay-author", "version": "0.1.0"},
                          "instructions": WRITABLE if writable else READ_ONLY}
            elif method == "ping": result = {}
            elif method == "tools/list": result = {"tools": self.tools(role)}
            elif method == "tools/call":
                try:
                    payload = self.call(role, params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                              "structuredContent": payload, "isError": False}
                except (AuthorError, ValidationError, UnicodeDecodeError) as exc:
                    error = exc.as_dict() if isinstance(exc, AuthorError) else {"code": "INVALID_REQUEST", "message": "Tool arguments or text content violate the contract"}
                    result = {"content": [{"type": "text", "text": json.dumps(error)}], "structuredContent": error, "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except AuthorError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": exc.message, "data": exc.as_dict()}}
