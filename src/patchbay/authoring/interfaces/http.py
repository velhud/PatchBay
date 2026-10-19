"""Independent HTTP app factory. Creating it never imports or starts legacy workers."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..errors import AuthorError, require
from ..execution.runner import Checks
from ..feedback.export import Feedback
from ..publication.git import Publisher
from ..service import AuthorService
from ..util import strict_json
from .auth import Access
from .mcp import MCP
from . import inputs as I


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
               allowed_origins: tuple[str, ...] = ()) -> FastAPI:
    service = AuthorService(home, expected_instance=expected_instance)
    access, protocol = Access(service.store), MCP(service)
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
        if request.url.path.startswith(("/api/", "/mcp")):
            try:
                request.state.role = access.role(request.headers.get("authorization"))
                origin = request.headers.get("origin")
                own = str(request.base_url).rstrip("/")
                require(origin is None or origin in allowed_origins or origin == own,
                        "FORBIDDEN", "Request origin is not authorized")
            except AuthorError as exc:
                return JSONResponse({"error": exc.as_dict()}, status_code=401 if exc.code == "UNAUTHORIZED" else 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    async def body(request, model):
        return model.model_validate(strict_json(await request.body()))

    def write(request, operator=False):
        access.require_mutation(request.state.role, operator_only=operator)

    async def invoke(method, **kwargs):
        return await asyncio.to_thread(method, **kwargs)

    @app.get("/api/capabilities")
    async def capabilities(request: Request):
        return {**service.capabilities(), "role": request.state.role, "mcp_write_qualified": access.qualified() is not None,
                "scheduler_enabled": scheduler, "scheduler_health": scheduler_health, "limits": service.store.limits.wire()}

    @app.get("/api/repositories")
    async def repositories(request: Request, limit: int = 50, offset: int = 0):
        return {"repositories": await invoke(service.repositories.list, limit=limit, offset=offset)}

    @app.post("/api/repositories")
    async def register(request: Request):
        write(request, operator=True)
        params = await body(request, I.Register)
        values = params.wire(); values["source"], values["policy"] = Path(params.source), params.policy
        result = await invoke(service.repositories.register, **values)
        return {key: result[key] for key in ("id", "name", "default_branch", "object_format")}

    @app.post("/api/repositories/refresh")
    async def refresh_repository(request: Request):
        write(request, operator=True)
        params = await body(request, I.RefreshRepository)
        return await invoke(service.repositories.refresh, **params.wire())

    @app.get("/api/tasks")
    async def tasks(request: Request, limit: int = 25, offset: int = 0):
        return await invoke(service.tasks.list, limit=limit, offset=offset)

    @app.post("/api/tasks")
    async def create_task(request: Request):
        write(request)
        params = await body(request, I.CreateTask)
        return await invoke(service.tasks.create, **params.wire())

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        return await invoke(service.tasks.get, task_id=task_id)

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
        result = await invoke(Feedback(service.store).export, task_id=task_id)
        return {**result, "download": "/api/blobs/" + result["blob_sha"]}

    @app.get("/api/tasks/{task_id}/diff")
    async def diff(task_id: str, request: Request, revision: int | None = None, path: str | None = None):
        return await invoke(Feedback(service.store).diff, task_id=task_id, revision=revision, path=path)

    @app.get("/api/blobs/{sha}")
    async def blob(sha: str, request: Request):
        await invoke(service.store.one, sql="SELECT 1 FROM blob_refs WHERE sha=? LIMIT 1", args=(sha,))
        path = service.store.blobs.path(sha)
        require(path.is_file(), "NOT_FOUND", "Referenced artifact is unavailable")
        is_zip = await asyncio.to_thread(zipfile.is_zipfile, path)
        return FileResponse(path, media_type="application/zip" if is_zip else "application/octet-stream",
                            filename="patchbay-author-" + sha[:12] + (".zip" if is_zip else ".bin"))

    @app.get("/api/tasks/{task_id}/publication-preview")
    async def publication_preview(task_id: str, request: Request, revision: int):
        return await invoke(Publisher(service.store).preview, task_id=task_id, revision=revision)

    @app.get("/api/tasks/{task_id}/publications")
    async def publications(task_id: str, request: Request):
        await invoke(service.tasks.get, task_id=task_id)
        return {"publications": await invoke(service.store.rows, sql="SELECT id,commit_oid,branch,state,pull_request,updated FROM publications WHERE task_id=? ORDER BY created DESC LIMIT 50", args=(task_id,))}

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
        rows = await invoke(service.store.rows, sql="SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", args=(after, limit))
        first = await invoke(service.store.one, sql="SELECT MIN(seq) AS first FROM events")
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return {"events": rows, "next_cursor": rows[-1]["seq"] if rows else after,
                "history_gap": bool(after and first["first"] and after + 1 < first["first"])}

    @app.post("/mcp")
    async def mcp(request: Request):
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

    return app
