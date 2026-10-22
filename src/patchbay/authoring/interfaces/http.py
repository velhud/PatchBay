"""Independent HTTP app factory. Creating it never imports or starts legacy workers."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
import time
import zipfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from patchbay.mcp_versions import validate_mcp_version_header

from ..errors import AuthorError, require
from ..execution.runner import Checks
from ..feedback.export import Feedback
from ..publication.git import Publisher
from ..service import AuthorService
from ..util import strict_json
from .auth import Access
from .mcp import MCP
from . import inputs as I
from ..persistence.control import Control
from ..authority import current_identity, request_context
from .ingress import IngressAdmission


class VerifiedBlobResponse(StreamingResponse):
    """Own the preverified descriptor even if response iteration is cancelled."""
    def __init__(self, source, content, **kwargs):
        self.source = source
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.source.close()


async def open_delivery_source(blobs, sha):
    """Cancellation cannot abandon a descriptor returned by a verifier thread."""
    task = asyncio.create_task(asyncio.to_thread(blobs.open_verified, sha))
    try:
        return await asyncio.shield(task)
    except BaseException:
        def release(done):
            if not done.cancelled():
                try:
                    done.result().close()
                except BaseException:
                    pass
        task.add_done_callback(release)
        raise


class BodyLimit:
    def __init__(self, app, upload_limit: int):
        self.app, self.upload_limit = app, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.upload_limit if scope["path"].endswith("/packages") else 2 * 1024 * 1024
        seen = 0
        async def bounded_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise AuthorError("RESOURCE_LIMIT", "Request body exceeds the configured limit", status=413)
            return message
        await self.app(scope, bounded_receive, send)


def create_app(home: Path, *, expected_instance: str | None = None, scheduler: bool = False,
               allowed_origins: tuple[str, ...] = (), output_profile: str = "compat-full-v1") -> FastAPI:
    service = AuthorService(home, expected_instance=expected_instance)
    access, protocol = Access(service.store), MCP(service, output_profile=output_profile)
    scheduler_health = {"last_attempt": None, "last_success": None, "last_error": None}

    async def schedule():
        while True:
            scheduler_health["last_attempt"] = time.time()
            try:
                await asyncio.to_thread(Checks(service.store).dispatch)
                scheduler_health.update(last_success=time.time(), last_error=None)
            except Exception as exc:
                scheduler_health["last_error"] = {"code": getattr(exc, "code", "SCHEDULER_ERROR"), "message": str(exc)[:2000]}
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(schedule()) if scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Authorized detached checks are not stopped by an HTTP restart.

    app = FastAPI(title="PatchBay Author Mode", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit, upload_limit=service.store.limits.archive.compressed_bytes)
    app.state.author_service = service

    @app.exception_handler(AuthorError)
    async def author_error(request, exc):
        status = {"UNAUTHORIZED": 401, "FORBIDDEN": 403, "QUALIFICATION_REQUIRED": 403,
                  "NOT_FOUND": 404, "RESOURCE_LIMIT": 413}.get(exc.code, exc.status)
        return JSONResponse({"error": exc.as_dict()}, status_code=status)

    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Request violates the interface contract",
                                        "details": exc.errors(include_input=False, include_context=False)}}, status_code=422)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # Authorization must use the ASGI routing path, never a URL rebuilt
        # from a client-controlled Host header.
        if request.scope["path"].startswith(("/api/", "/mcp")):
            try:
                identity = access.authenticate(request.headers.get("authorization"))
                request.state.role = identity.role
                origin = request.headers.get("origin")
                own = str(request.base_url).rstrip("/")
                require(origin is None or origin in allowed_origins or origin == own,
                        "FORBIDDEN", "Request origin is not authorized")
            except AuthorError as exc:
                return JSONResponse({"error": exc.as_dict()}, status_code=401 if exc.code == "UNAUTHORIZED" else 403)
            with access.as_identity(identity), request_context(getattr(request.state, "request_id", None)):
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    async def body(request, model):
        value = strict_json(await request.body())
        # Legacy HTTP clients can omit a key; it is still journaled and returned
        # for discovery. New controllers supply Idempotency-Key or a body key.
        if "key" in model.model_fields and isinstance(value, dict) and "key" not in value:
            value["key"] = request.headers.get("idempotency-key") or "http-" + uuid.uuid4().hex
        return model.model_validate(value)

    def write(request, operator=False):
        access.require_mutation(request.state.role, operator_only=operator)

    async def invoke(method, **kwargs):
        return await asyncio.to_thread(method, **kwargs)

    def authorize_blob(sha):
        scope = current_identity().task_ids
        if scope is None:
            service.store.one("SELECT 1 FROM blob_refs WHERE sha=? LIMIT 1", (sha,))
            return
        marks = ",".join("?" for _ in scope)
        # Select only owners belonging to the granted tasks; a known digest is
        # not a cross-task download capability.
        sql = """SELECT 1 FROM blob_refs b WHERE b.sha=? AND b.owner IN (
            SELECT 'context:'||id FROM contexts WHERE task_id IN ({0})
            UNION SELECT 'receipt:'||id FROM runs WHERE task_id IN ({0})
            UNION SELECT 'approval:'||id FROM approvals WHERE task_id IN ({0})
            UNION SELECT 'package-inbox:'||id FROM operations WHERE task_id IN ({0})
            UNION SELECT 'revision:'||task_id||':'||number FROM revisions WHERE task_id IN ({0})
            UNION SELECT 'package:'||fingerprint FROM revisions WHERE task_id IN ({0})
        ) LIMIT 1""".format(marks)
        allowed = service.store.one(sql, (sha, *scope, *scope, *scope, *scope, *scope, *scope), required=False)
        if not allowed:
            for task_id in scope:
                allowed = service.store.one("SELECT 1 FROM blob_refs WHERE sha=? AND owner LIKE ? LIMIT 1",
                                            (sha, "feedback:" + task_id + ":%"), required=False)
                if allowed:
                    break
        require(allowed is not None, "FORBIDDEN", "Artifact is outside this principal's task grant", status=403)

    @app.get("/api/capabilities")
    async def capabilities(request: Request):
        public = {k: v for k, v in service.capabilities().items() if k != "instance_id"}
        return {**public, "role": request.state.role, "mcp_write_qualified": request.state.role in {"operator", "author"} and access.qualified(request.state.role, output_profile=output_profile) is not None,
                "scheduler_enabled": scheduler, "scheduler_health": scheduler_health, "limits": service.store.limits.wire()}

    @app.get("/api/repositories")
    async def repositories(request: Request, limit: int = 50, offset: int = 0):
        return {"repositories": await invoke(service.repositories.list, limit=limit, offset=offset)}

    @app.post("/api/repositories")
    async def register(request: Request):
        write(request, operator=True)
        access.assert_global_operator()
        params = await body(request, I.Register)
        values = params.wire(); values["source"], values["policy"] = Path(params.source), params.policy
        result = await invoke(service.repositories.register, **values)
        return {key: result[key] for key in ("id", "name", "default_branch", "object_format")}

    @app.post("/api/repositories/refresh")
    async def refresh_repository(request: Request):
        write(request, operator=True)
        access.assert_global_operator()
        params = await body(request, I.RefreshRepository)
        return await invoke(service.repositories.refresh, **params.wire())

    @app.get("/api/tasks")
    async def tasks(request: Request, limit: int = 25, offset: int = 0, cursor: str | None = None):
        return await invoke(service.tasks.list, limit=limit, offset=offset, cursor=cursor)

    @app.post("/api/tasks")
    async def create_task(request: Request):
        write(request)
        params = await body(request, I.CreateTask)
        return await invoke(service.tasks.create, **params.wire())

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        return await invoke(service.tasks.get, task_id=task_id)

    @app.get("/api/tasks/{task_id}/recovery")
    async def recovery(task_id: str, request: Request):
        return await invoke(service.recovery.get, task_id=task_id)

    @app.get("/api/tasks/{task_id}/operations")
    async def recent_operations(task_id: str, request: Request, limit: int = 50, offset: int = 0, cursor: str | None = None,
                                traversal: str = 'strict'):
        return await invoke(service.recovery.operations, task_id=task_id, limit=limit, offset=offset, cursor=cursor, traversal=traversal)

    @app.post("/api/claim")
    async def claim(request: Request):
        write(request)
        params = await body(request, I.Claim)
        return await invoke(service.leases.claim, **params.wire())

    @app.post("/api/context")
    async def context(request: Request):
        write(request)
        params = await body(request, I.ExportContext)
        result = await invoke(service.snapshots.export, **params.wire())
        return {"context_id": result["id"], "revision": result["revision"], "blob_sha": result["bundle_sha"],
                'identity_only': result.get('identity_only', False), 'inventory_format_version': result.get('inventory_format_version', 1),
                "request_operation_id": result["request_operation_id"],
                "download": "/api/blobs/" + result["bundle_sha"]}

    @app.post("/api/tasks/{task_id}/packages")
    async def package(task_id: str, request: Request):
        write(request)
        token, key = request.headers.get("x-author-lease"), request.headers.get("idempotency-key")
        try:
            epoch = int(request.headers.get("x-author-epoch", ""))
        except ValueError as exc:
            raise AuthorError("LEASE_REQUIRED", "X-Author-Epoch is required") from exc
        await invoke(service.leases.assert_owned, task_id=task_id, token=token, epoch=epoch)
        require(isinstance(key, str) and 0 < len(key) <= 200, "INVALID_REQUEST", "Idempotency-Key is required")
        fd, name = tempfile.mkstemp(prefix="upload-", suffix=".zip", dir=service.store.home / "uploads")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    require(size <= service.store.limits.archive.compressed_bytes, "RESOURCE_LIMIT", "Package upload is too large")
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            return await invoke(service.materializer.submit, task_id=task_id, path=path, key=key, token=token, epoch=epoch)
        finally:
            path.unlink(missing_ok=True)

    @app.post("/api/checks")
    async def checks(request: Request):
        write(request)
        params = await body(request, I.CheckRequest)
        return await invoke(Checks(service.store).submit, **params.wire())

    @app.post("/api/cancel")
    async def cancel(request: Request):
        write(request)
        params = await body(request, I.Cancel)
        return await invoke(Checks(service.store).cancel, **params.wire())

    @app.get("/api/operations/{operation_id}")
    async def operation(operation_id: str, request: Request):
        return await invoke(service.store.operation, operation_id=operation_id)

    @app.get("/api/operations/{operation_id}/logs/{filename}")
    async def logs(operation_id: str, filename: str, request: Request, offset: int = 0, limit: int = 65536):
        return await invoke(Checks(service.store).log, operation_id=operation_id, filename=filename, offset=offset, limit=limit)

    @app.get("/api/tasks/{task_id}/feedback")
    async def feedback(task_id: str, request: Request, revision: int | None = None, offset: int = 0, limit: int = 10):
        return await invoke(Feedback(service.store).page, task_id=task_id, revision=revision, offset=offset, limit=limit)

    @app.post("/api/tasks/{task_id}/feedback")
    async def feedback_export(task_id: str, request: Request):
        write(request)
        result = await invoke(Feedback(service.store).export, task_id=task_id,
                              key=request.headers.get("idempotency-key") or "http-" + uuid.uuid4().hex)
        return {**result, "download": "/api/blobs/" + result["blob_sha"]}

    @app.get("/api/tasks/{task_id}/diff")
    async def diff(task_id: str, request: Request, revision: int | None = None, path: str | None = None):
        return await invoke(Feedback(service.store).diff, task_id=task_id, revision=revision, path=path)

    @app.get("/api/blobs/{sha}")
    async def blob(sha: str, request: Request):
        await invoke(authorize_blob, sha=sha)
        source = await open_delivery_source(service.store.blobs, sha)
        try:
            is_zip = zipfile.is_zipfile(source)
            source.seek(0)
            size = os.fstat(source.fileno()).st_size
            filename = 'patchbay-author-' + sha[:12] + ('.zip' if is_zip else '.bin')
            return VerifiedBlobResponse(source, service.store.blobs.delivery_chunks(source, sha, size),
                media_type='application/zip' if is_zip else 'application/octet-stream',
                headers={'Content-Length': str(size), 'ETag': '"' + sha + '"', 'X-Content-SHA256': sha,
                         'Content-Disposition': 'attachment; filename="' + filename + '"'})
        except BaseException:
            source.close()
            raise

    @app.get("/api/tasks/{task_id}/publication-preview")
    async def publication_preview(task_id: str, request: Request, revision: int):
        return await invoke(Publisher(service.store).preview, task_id=task_id, revision=revision)

    @app.get("/api/tasks/{task_id}/publications")
    async def publications(task_id: str, request: Request):
        await invoke(service.tasks.get, task_id=task_id)
        return {"publications": await invoke(service.store.rows, sql="SELECT id,commit_oid,branch,state,pull_request,updated FROM publications WHERE task_id=? ORDER BY created DESC LIMIT 50", args=(task_id,))}

    @app.get("/api/inbox")
    async def inbox(request: Request, limit: int = 50, offset: int = 0):
        return await invoke(service.inbox.pending, limit=limit, offset=offset)

    @app.post("/api/inbox/scan")
    async def scan_intake(request: Request):
        write(request, operator=True)
        access.assert_global_operator()
        params = await body(request, I.InboxScan)
        values = params.wire()
        values["folder"] = Path(values["folder"])
        return await invoke(service.inbox.scan, **values)

    @app.post("/api/inbox/import")
    async def import_intake(request: Request):
        write(request, operator=True)
        access.assert_global_operator()
        params = await body(request, I.InboxImport)
        return await invoke(service.inbox.ingest, path=Path(params.path), key=params.key)

    @app.post("/api/inbox/grant")
    async def grant_intake(request: Request):
        write(request)
        params = await body(request, I.InboxGrant)
        return await invoke(service.inbox.grant, **params.wire())

    @app.post("/api/inbox/revoke")
    async def revoke_intake(request: Request):
        write(request, operator=True)
        params = await body(request, I.ExportFeedback)
        access.assert_task(params.task_id)
        return await invoke(service.inbox.revoke, task_id=params.task_id, key=params.key)

    @app.post("/api/inbox/{operation_id}/approve")
    async def approve_intake(operation_id: str, request: Request):
        write(request)
        params = await body(request, I.Cancel)
        require(params.operation_id == operation_id, "INVALID_REQUEST", "Inbox operation identity mismatch")
        return await invoke(service.inbox.approve, **params.wire())

    @app.get("/api/control")
    async def control_status(request: Request):
        require(current_identity().task_ids is None, "FORBIDDEN", "Global status requires an unscoped principal", status=403)
        return await invoke(Control(service.store).status)

    @app.post("/api/policy")
    async def update_policy(request: Request):
        write(request, operator=True)
        from ..policy import Policies
        params = await body(request, I.UpdatePolicy)
        access.assert_task(params.task_id)
        return await invoke(Policies(service.store).update, **params.wire())

    @app.post("/api/control/{action}")
    async def control(action: str, request: Request):
        write(request, operator=True)
        params = await body(request, I.Control)
        require(action in {"pause", "resume", "revoke", "restore_grant"}, "INVALID_REQUEST", "Unknown control action")
        method = getattr(Control(service.store), action)
        values = params.wire()
        if action in {"pause", "resume"}:
            access.assert_global_operator()
            require(params.task_id is None, "INVALID_REQUEST", "Global admission control does not take a task")
            values.pop("task_id")
        else:
            require(params.task_id is not None, "INVALID_REQUEST", "Task grant control requires a task")
            access.assert_task(params.task_id)
        return await invoke(method, **values)

    @app.post("/api/approvals")
    async def approval(request: Request):
        write(request, operator=True)
        params = await body(request, I.Approval)
        return await invoke(Publisher(service.store).approve, **params.wire())

    @app.post("/api/publications")
    async def publish(request: Request):
        write(request, operator=True)
        params = await body(request, I.Publish)
        return await invoke(Publisher(service.store).apply, **params.wire())

    @app.get("/api/events")
    async def events(request: Request, after: int = 0, limit: int = 100):
        require(after >= 0 and 1 <= limit <= 200, "INVALID_REQUEST", "Invalid event cursor")
        scope = current_identity().task_ids
        clause = " AND task_id IN (" + ",".join("?" for _ in scope) + ")" if scope is not None else ""
        rows = await invoke(service.store.rows, sql="SELECT * FROM events WHERE seq>?" + clause + " ORDER BY seq LIMIT ?", args=(after, *scope, limit) if scope is not None else (after, limit))
        first = await invoke(service.store.one, sql="SELECT MIN(seq) AS first FROM events")
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return {"events": rows, "next_cursor": rows[-1]["seq"] if rows else after,
                "history_gap": bool(after and first["first"] and after + 1 < first["first"])}

    @app.post("/mcp")
    async def mcp(request: Request):
        try:
            validate_mcp_version_header(request.headers.get("MCP-Protocol-Version"))
        except ValueError as error:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32602, "message": str(error)}}, status_code=400)
        result = await asyncio.to_thread(protocol.handle, request.state.role, strict_json(await request.body()))
        return Response(status_code=202) if result is None else JSONResponse(result)

    @app.get("/mcp")
    async def no_unsolicited_stream(request: Request):
        return JSONResponse({"message": "This stateless MCP server uses JSON POST responses; unsolicited SSE is unsupported"}, status_code=405)

    ui = Path(__file__).with_name("ui")
    app.mount("/assets", StaticFiles(directory=ui), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(ui / "index.html", media_type="text/html")

    def principal_for_scope(scope):
        authorization = next((value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == b"authorization"), None)
        return access.authenticate(authorization).principal_id

    def admission_kind(scope, raw):
        """Classify bounded, authorized intent; never dispatch or grant authority."""
        if not scope.get('path','').startswith(('/api/','/mcp')):
            return 'read' if scope.get('method','GET').upper() in {'GET','HEAD','OPTIONS'} else 'write'
        authorization = next((v.decode('latin-1') for k,v in scope.get('headers',[]) if k.lower()==b'authorization'), None)
        identity = access.authenticate(authorization)
        method, path = scope.get('method','GET').upper(), scope.get('path','')
        with access.as_identity(identity):
            if method == 'GET':
                recovery = re.fullmatch(r'/api/tasks/(task_[0-9a-f]{32})/recovery', path)
                if recovery:
                    service.tasks.get(recovery[1])
                    return 'control'
                return 'read'
            if method in {'HEAD','OPTIONS'}:
                return 'read'
            if method != 'POST' or raw is None:
                return 'write'
            value = strict_json(raw)
            if path == '/mcp':
                from patchbay.mcp_framing import request_identity
                version = next((v.decode('latin-1') for k,v in scope.get('headers',[]) if k.lower()==b'mcp-protocol-version'),None)
                validate_mcp_version_header(version)
                identified, _ = request_identity(value)
                if not identified:
                    return 'read'  # Notifications cannot dispatch domain work.
                if value['method'] in {'initialize','tools/list','ping'}:
                    return 'read'
                if value['method'] != 'tools/call' or not isinstance(value.get('params'),dict):
                    return 'write'
                params = value['params']
                entry, arguments = protocol._prepare_call(identity.role, params.get('name'),params.get('arguments',{}))
                if entry[0] == 'author_recover_task':
                    service.tasks.get(arguments['task_id'])
                    return 'control'
                if entry[0] == 'author_cancel_operation':
                    operation = service.store.operation(arguments['operation_id'])
                    require(operation['kind']=='checks.run','INVALID_REQUEST','Only check operations can be cancelled')
                    service.leases.assert_owned(operation['task_id'],arguments['token'],arguments['epoch'])
                    return 'control'
                return 'write' if entry[3] else 'read'
            if path == '/api/cancel':
                access.require_mutation(identity.role, output_profile=output_profile)
                params = I.Cancel.model_validate(value)
                operation = service.store.operation(params.operation_id)
                require(operation['kind']=='checks.run','INVALID_REQUEST','Only check operations can be cancelled')
                service.leases.assert_owned(operation['task_id'],params.token,params.epoch)
                return 'control'
            if path in {'/api/control/pause','/api/control/resume','/api/control/revoke','/api/control/restore_grant'}:
                access.require_mutation(identity.role,operator_only=True,output_profile=output_profile)
                params = I.Control.model_validate(value)
                if path in {'/api/control/pause','/api/control/resume'}:
                    access.assert_global_operator()
                    require(params.task_id is None,'INVALID_REQUEST','Global control does not take a task')
                else:
                    require(params.task_id is not None,'INVALID_REQUEST','Task control requires a task')
                    service.tasks.get(params.task_id)
                return 'control'
            return 'write'

    app.add_middleware(IngressAdmission, principal_getter=principal_for_scope, semantic_kind=admission_kind)
    return app
