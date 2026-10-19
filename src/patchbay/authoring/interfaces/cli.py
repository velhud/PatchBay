"""Independent author CLI; no legacy worker imports, launches, or state recovery."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from pydantic import ValidationError
from ..errors import AuthorError, require
from ..execution.runner import Checks
from ..feedback.export import Feedback
from ..models import RuntimeLimits, TaskPolicy
from ..persistence.maintenance import Maintenance
from ..persistence.store import Store, TERMINAL
from ..publication.git import Publisher
from ..publication.integration import Integration
from ..service import AuthorService
from ..util import atomic_write, canonical, strict_json
from .auth import Access


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="patchbay author", description=__doc__)
    p.add_argument("--home", type=Path, default=Path(os.environ.get("PATCHBAY_AUTHOR_HOME", "~/.patchbay-author")).expanduser())
    p.add_argument("--expected-instance")
    sub = p.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("init"); cmd.add_argument("--limits", type=Path)
    cmd = sub.add_parser("register"); cmd.add_argument("name"); cmd.add_argument("source", type=Path)
    cmd.add_argument("--policy", type=Path, required=True); cmd.add_argument("--remote"); cmd.add_argument("--default-branch")
    sub.add_parser("repositories")
    cmd = sub.add_parser("refresh"); cmd.add_argument("repository"); cmd.add_argument("--key", required=True); cmd.add_argument("--from-remote", action="store_true")
    cmd = sub.add_parser("task"); cmd.add_argument("repository"); cmd.add_argument("--goal", required=True)
    cmd.add_argument("--base", default="HEAD"); cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("tasks"); cmd.add_argument("--offset", type=int, default=0)
    cmd = sub.add_parser("claim"); cmd.add_argument("task_id"); cmd.add_argument("--label", default="operator"); cmd.add_argument("--takeover", action="store_true")
    cmd = sub.add_parser("context"); cmd.add_argument("task_id"); cmd.add_argument("--output", type=Path, required=True)
    cmd.add_argument("--path", action="append"); cmd.add_argument("--revision", type=int)
    cmd = sub.add_parser("apply"); cmd.add_argument("task_id"); cmd.add_argument("package", type=Path); cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("check"); cmd.add_argument("task_id"); cmd.add_argument("--key", required=True)
    cmd.add_argument("--check", action="append"); cmd.add_argument("--wait", action="store_true")
    cmd = sub.add_parser("status"); cmd.add_argument("operation_id"); cmd.add_argument("--wait", action="store_true")
    cmd = sub.add_parser("cancel"); cmd.add_argument("operation_id")
    cmd = sub.add_parser("feedback"); cmd.add_argument("task_id"); cmd.add_argument("--output", type=Path); cmd.add_argument("--markdown", action="store_true")
    cmd = sub.add_parser("diff"); cmd.add_argument("task_id"); cmd.add_argument("--revision", type=int); cmd.add_argument("--path")
    cmd = sub.add_parser("logs"); cmd.add_argument("operation_id"); cmd.add_argument("filename")
    cmd.add_argument("--offset", type=int, default=0); cmd.add_argument("--limit", type=int, default=65536)
    sub.add_parser("dispatch")
    cmd = sub.add_parser("recover"); cmd.add_argument("operation_id")
    cmd = sub.add_parser("approve"); cmd.add_argument("task_id"); cmd.add_argument("--actor", default="operator"); cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("publish"); cmd.add_argument("approval_id"); cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("publications"); cmd.add_argument("task_id")
    cmd = sub.add_parser("integrate"); cmd.add_argument("repository"); cmd.add_argument("--sources", type=Path, required=True)
    cmd.add_argument("--base", required=True); cmd.add_argument("--goal", required=True); cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("backup"); cmd.add_argument("output", type=Path)
    cmd = sub.add_parser("restore"); cmd.add_argument("archive", type=Path)
    cmd = sub.add_parser("gc"); cmd.add_argument("--minimum-age", type=int, default=3600)
    sub.add_parser("doctor")
    cmd = sub.add_parser("token"); cmd.add_argument("--role", choices=["operator", "author", "reader"], default="operator")
    cmd = sub.add_parser("qualify-client"); cmd.add_argument("evidence", type=Path); cmd.add_argument("--actor", required=True)
    cmd = sub.add_parser("serve"); cmd.add_argument("--host", default="127.0.0.1"); cmd.add_argument("--port", type=int, default=8788)
    cmd.add_argument("--run-scheduler", action="store_true"); cmd.add_argument("--allow-origin", action="append", default=[])
    return p


def local_lease(service, task_id: str, *, label: str = "operator", takeover: bool = False) -> dict:
    service.tasks.get(task_id)
    path = service.store.home / "operator-leases" / (task_id + ".json")
    old = strict_json(path.read_bytes()) if path.is_file() else {}
    lease = service.leases.claim(task_id, label=label, token=old.get("token"), takeover=takeover)
    atomic_write(path, canonical(lease))
    return {"token": lease["token"], "epoch": lease["epoch"]}


def copy_blob(store, sha: str, output: Path) -> dict:
    output = Path(output).expanduser().absolute()
    require(not output.resolve().is_relative_to(store.home), "INVALID_REQUEST", "Export outside the private author runtime")
    output.parent.mkdir(parents=True, exist_ok=True)
    require(not output.exists(), "OUTPUT_EXISTS", "Export destination already exists; choose a new filename")
    with store.blobs.path(sha).open("rb") as source, output.open("xb") as target:
        shutil.copyfileobj(source, target, 65536)
        target.flush(); os.fsync(target.fileno())
    return {"output": str(output), "sha256": sha}


def wait_for(service, operation: dict, *, dispatch: bool = False) -> dict:
    while operation["state"] not in TERMINAL:
        if dispatch and operation["state"] == "queued":
            Checks(service.store).dispatch(operation_ids=[operation["id"]])
        time.sleep(1)
        operation = service.store.operation(operation["id"])
    return operation


def run(args) -> object:
    if args.command == "init":
        limits = RuntimeLimits.model_validate(strict_json(args.limits.read_bytes())) if args.limits else RuntimeLimits()
        store = Store.initialize(args.home, limits)
        return {"home": str(store.home), "instance_id": store.identity["instance_id"], "initialized": True}
    if args.command == "restore":
        store = Maintenance.restore(args.archive, args.home)
        return {"restored": True, **store.integrity()}
    service = AuthorService(args.home, expected_instance=args.expected_instance)
    store, command = service.store, args.command
    if command == "register":
        result = service.repositories.register(args.name, args.source, TaskPolicy.model_validate(strict_json(args.policy.read_bytes())),
                                               remote=args.remote, default_branch=args.default_branch)
        return {key: result[key] for key in ("id", "name", "default_branch", "object_format")}
    if command == "repositories": return service.repositories.list()
    if command == "refresh": return service.repositories.refresh(args.repository, key=args.key, from_remote=args.from_remote)
    if command == "task": return service.tasks.create(args.repository, args.goal, base=args.base, key=args.key)
    if command == "tasks": return service.tasks.list(offset=args.offset)
    if command == "claim":
        local_lease(service, args.task_id, label=args.label, takeover=args.takeover)
        return {"task_id": args.task_id, "lease_saved": True}
    if command == "context":
        context = service.snapshots.export(args.task_id, revision=args.revision, paths=args.path)
        return {"context_id": context["id"], **copy_blob(store, context["bundle_sha"], args.output)}
    if command == "apply":
        return service.materializer.submit(args.task_id, args.package, key=args.key, **local_lease(service, args.task_id))
    if command == "check":
        task = service.tasks.get(args.task_id)
        operation = Checks(store).submit(args.task_id, revision=task["current_revision"], key=args.key,
                                         requested=args.check, **local_lease(service, args.task_id))
        return wait_for(service, operation, dispatch=True) if args.wait else operation
    if command == "status":
        operation = store.operation(args.operation_id)
        return wait_for(service, operation) if args.wait else operation
    if command == "cancel":
        operation = store.operation(args.operation_id)
        return Checks(store).cancel(args.operation_id, **local_lease(service, operation["task_id"]))
    if command == "feedback":
        feedback = Feedback(store)
        if args.output:
            result = feedback.export(args.task_id)
            return {**result, **copy_blob(store, result["blob_sha"], args.output)}
        result = feedback.get(args.task_id)
        return feedback.markdown(result) if args.markdown else result
    if command == "diff": return Feedback(store).diff(args.task_id, revision=args.revision, path=args.path)
    if command == "logs": return Checks(store).log(args.operation_id, args.filename, offset=args.offset, limit=args.limit)
    if command == "dispatch": return Checks(store).dispatch()
    if command == "recover":
        operation = store.operation(args.operation_id)
        if operation["kind"] == "package.submit":
            return service.materializer.recover(args.operation_id, **local_lease(service, operation["task_id"]))
        require(operation["kind"] == "checks.run", "INVALID_REQUEST", "Use the original publish/integrate command and idempotency key to reconcile that operation")
        return Checks(store).recover(args.operation_id)
    if command == "approve":
        task = service.tasks.get(args.task_id)
        return Publisher(store).approve(args.task_id, task["current_revision"], actor=args.actor, key=args.key, **local_lease(service, args.task_id))
    if command == "publish":
        approval = store.one("SELECT task_id FROM approvals WHERE id=?", (args.approval_id,))
        return Publisher(store).apply(args.approval_id, key=args.key, **local_lease(service, approval["task_id"]))
    if command == "publications":
        service.tasks.get(args.task_id)
        return store.rows("SELECT id,commit_oid,branch,state,pull_request,updated FROM publications WHERE task_id=? ORDER BY created DESC LIMIT 50", (args.task_id,))
    if command == "integrate":
        result = Integration(store).create(args.repository, strict_json(args.sources.read_bytes()), base=args.base, goal=args.goal, key=args.key, label="operator")
        lease = result.pop("lease")
        atomic_write(store.home / "operator-leases" / (result["task_id"] + ".json"), canonical(lease))
        return result
    if command == "backup": return Maintenance(store).backup(args.output)
    if command == "gc": return Maintenance(store).gc(minimum_age_seconds=args.minimum_age)
    if command == "doctor": return {**service.capabilities(), "integrity": store.integrity()}
    if command == "token": return strict_json((store.home / "tokens.json").read_bytes())[args.role]
    if command == "qualify-client": return Access(store).qualify(strict_json(args.evidence.read_bytes()), actor=args.actor)
    if command == "serve":
        import uvicorn
        from .http import create_app
        uvicorn.run(create_app(args.home, expected_instance=args.expected_instance, scheduler=args.run_scheduler,
                               allowed_origins=tuple(args.allow_origin)), host=args.host, port=args.port)
        return {"stopped": True}
    raise AuthorError("INVALID_REQUEST", "Unknown author command")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("state") in {"failed", "blocked", "timed_out", "cancelled", "unknown_execution", "publication_unknown"}:
            return 1
        return 0
    except (AuthorError, ValidationError, OSError, ValueError) as exc:
        error = exc.as_dict() if isinstance(exc, AuthorError) else {"code": "INVALID_REQUEST", "message": str(exc)[:4000]}
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Local waiting stopped. Authorized durable checks remain owned by their supervisors; use cancel explicitly.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
