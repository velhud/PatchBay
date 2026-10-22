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
from ..persistence.control import Control
from ..persistence.store import Store, TERMINAL
from ..publication.git import Publisher
from ..publication.integration import Integration
from ..service import AuthorService
from ..util import atomic_write, canonical, strict_json
from .auth import Access


DEFAULT_WAIT_SECONDS = 3600
MAX_WAIT_SECONDS = 86400


def _wait_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("wait must be a whole number of seconds") from exc
    if not 1 <= seconds <= MAX_WAIT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"wait must be between 1 and {MAX_WAIT_SECONDS} seconds"
        )
    return seconds


def _add_wait_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--wait",
        nargs="?",
        const=DEFAULT_WAIT_SECONDS,
        default=0,
        type=_wait_seconds,
        metavar="SECONDS",
        help=(
            f"Wait up to SECONDS for a terminal result; --wait without a value uses "
            f"{DEFAULT_WAIT_SECONDS} seconds (maximum {MAX_WAIT_SECONDS})."
        ),
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="patchbay author", description=__doc__)
    p.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("PATCHBAY_AUTHOR_HOME", "~/.patchbay-author")).expanduser(),
    )
    p.add_argument("--expected-instance")
    sub = p.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("init")
    cmd.add_argument("--limits", type=Path)
    cmd = sub.add_parser("register")
    cmd.add_argument("name")
    cmd.add_argument("source", type=Path)
    cmd.add_argument("--policy", type=Path, required=True)
    cmd.add_argument("--remote")
    cmd.add_argument("--default-branch")
    cmd.add_argument("--key")
    sub.add_parser("repositories")
    cmd = sub.add_parser("refresh")
    cmd.add_argument("repository")
    cmd.add_argument("--key", required=True)
    cmd.add_argument("--from-remote", action="store_true")
    cmd = sub.add_parser("task")
    cmd.add_argument("repository")
    cmd.add_argument("--goal", required=True)
    cmd.add_argument("--base", default="HEAD")
    cmd.add_argument("--dirty-path", action="append", help="Explicitly capture only these dirty paths without changing the source checkout")
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("tasks")
    cmd.add_argument("--offset", type=int, default=0)
    cmd = sub.add_parser("claim")
    cmd.add_argument("task_id")
    cmd.add_argument("--label", default="operator")
    cmd.add_argument("--takeover", action="store_true")
    cmd.add_argument("--key")
    cmd = sub.add_parser("context")
    cmd.add_argument("task_id")
    cmd.add_argument("--output", type=Path, required=True)
    selection = cmd.add_mutually_exclusive_group()
    selection.add_argument("--path", action="append")
    selection.add_argument('--identity-only', action='store_true', help='Complete inventory without source payloads, for new-file-only work.')
    cmd.add_argument("--revision", type=int)
    cmd.add_argument("--key")
    cmd = sub.add_parser("apply")
    cmd.add_argument("task_id")
    cmd.add_argument("package", type=Path)
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("check")
    cmd.add_argument("task_id")
    cmd.add_argument("--key", required=True)
    cmd.add_argument("--check", action="append")
    _add_wait_argument(cmd)
    cmd = sub.add_parser("status")
    cmd.add_argument("operation_id")
    _add_wait_argument(cmd)
    cmd = sub.add_parser("cancel")
    cmd.add_argument("operation_id")
    cmd = sub.add_parser("feedback")
    cmd.add_argument("task_id")
    cmd.add_argument("--output", type=Path)
    cmd.add_argument("--markdown", action="store_true")
    cmd.add_argument("--key")
    cmd = sub.add_parser("diff")
    cmd.add_argument("task_id")
    cmd.add_argument("--revision", type=int)
    cmd.add_argument("--path")
    cmd = sub.add_parser("logs")
    cmd.add_argument("operation_id")
    cmd.add_argument("filename")
    cmd.add_argument("--offset", type=int, default=0)
    cmd.add_argument("--limit", type=int, default=65536)
    sub.add_parser("dispatch")
    sub.add_parser("optimize-admission", help="Install additive admission indexes on quiescent schema-1 state")
    cmd = sub.add_parser("recover")
    cmd.add_argument("operation_id")
    cmd = sub.add_parser(
        "resolve-unknown-execution",
        help="Locally attest exact quiescence for one immutable unknown Author execution",
    )
    cmd.add_argument("operation_id")
    cmd.add_argument("--operator", required=True, help="Named operator identity recorded in private audit evidence.")
    cmd.add_argument("--reason", required=True, help="Brief manual reason for closing the admission fence; historical outcome remains unknown.")
    cmd.add_argument("--apply", action="store_true", help="Record the explicit operator disposition after all checks pass; default is assessment only.")
    from ..operator_resolution import ATTESTATION, LEGACY_CONTINUITY_ATTESTATION
    cmd.add_argument(
        "--attest-quiescence", default="",
        help=("Required with --apply; provide the exact statement matching the evidence source. "
              f"Native lifecycle: {ATTESTATION} Legacy continuity: {LEGACY_CONTINUITY_ATTESTATION}"),
    )
    cmd = sub.add_parser("approve")
    cmd.add_argument("task_id")
    cmd.add_argument("--actor", required=True)
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("publish")
    cmd.add_argument("approval_id")
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("publications")
    cmd.add_argument("task_id")
    cmd = sub.add_parser("integrate")
    cmd.add_argument("repository")
    cmd.add_argument("--sources", type=Path, required=True)
    cmd.add_argument("--base", required=True)
    cmd.add_argument("--goal", required=True)
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("backup")
    cmd.add_argument("output", type=Path)
    cmd.add_argument("--key")
    cmd = sub.add_parser("restore")
    cmd.add_argument("archive", type=Path)
    cmd = sub.add_parser("gc")
    cmd.add_argument("--minimum-age", type=int, default=3600)
    cmd.add_argument("--key")
    sub.add_parser("doctor")
    cmd = sub.add_parser("token")
    cmd.add_argument("--role", choices=["operator", "author", "reader"], required=True)
    cmd = sub.add_parser("token-issue")
    cmd.add_argument("principal_id")
    cmd.add_argument("--role", choices=["operator", "author", "reader"], required=True)
    cmd.add_argument("--task", action="append", dest="task_ids")
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("token-revoke")
    cmd.add_argument("credential_id")
    cmd.add_argument("--key", required=True)
    sub.add_parser("upgrade-state")
    cmd = sub.add_parser("qualify-client")
    cmd.add_argument("evidence", type=Path)
    cmd.add_argument("--actor", required=True)
    cmd.add_argument("--key")
    cmd = sub.add_parser("serve")
    cmd.add_argument("--host", default="127.0.0.1")
    cmd.add_argument("--port", type=int, default=8788)
    cmd.add_argument("--run-scheduler", action="store_true")
    cmd.add_argument("--allow-origin", action="append", default=[])
    cmd.add_argument("--output-profile", choices=["compat-full-v1", "structured-summary-v2"], default="compat-full-v1")
    cmd = sub.add_parser("recovery")
    cmd.add_argument("task_id")
    cmd = sub.add_parser("operations")
    cmd.add_argument("task_id")
    cmd.add_argument("--offset", type=int, default=0)
    cmd.add_argument("--limit", type=int, default=50)
    cmd.add_argument("--cursor")
    sub.add_parser("inbox")
    cmd = sub.add_parser("inbox-import")
    cmd.add_argument("package", type=Path)
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("inbox-approve")
    cmd.add_argument("operation_id")
    cmd.add_argument("--key", required=True)
    cmd = sub.add_parser("inbox-grant")
    cmd.add_argument("task_id")
    cmd.add_argument("--key", required=True)
    cmd.add_argument("--auto-validate", action="store_true")
    cmd = sub.add_parser("inbox-revoke")
    cmd.add_argument("task_id")
    cmd.add_argument("--key", required=True)
    for name in ("inbox-scan", "inbox-watch"):
        cmd = sub.add_parser(name)
        cmd.add_argument("folder", type=Path)
        cmd.add_argument("--stability-seconds", type=float, default=1.0)
        cmd.add_argument("--max-files", type=int, default=25)
        if name == "inbox-watch":
            cmd.add_argument("--poll-seconds", type=float, default=2.0)
            cmd.add_argument("--cycles", type=int, default=0, help="Zero watches until explicitly stopped")
    sub.add_parser("control-status")
    cmd = sub.add_parser("policy")
    cmd.add_argument("task_id")
    cmd.add_argument("--policy", type=Path, required=True)
    cmd.add_argument("--expected-digest", required=True)
    cmd.add_argument("--actor", required=True)
    cmd.add_argument("--key", required=True)
    for name in ("pause", "resume", "revoke-task", "restore-task"):
        cmd = sub.add_parser(name)
        if name.endswith("-task"):
            cmd.add_argument("task_id")
        cmd.add_argument("--key", required=True)
        cmd.add_argument("--actor", required=True)
    return p


def local_lease(
    service: AuthorService,
    task_id: str,
    *,
    label: str = "operator",
    takeover: bool = False,
    key: str | None = None,
) -> dict:
    service.tasks.get(task_id)
    path = service.store.home / "operator-leases" / (task_id + ".json")
    require(
        not path.is_symlink(),
        "UNSAFE_LEASE_CACHE",
        "Operator lease cache must not be a symbolic link",
    )
    old = strict_json(path.read_bytes()) if path.is_file() else {}
    if key:
        prior = service.store.one("SELECT payload,result FROM operations WHERE task_id=? AND kind='lease.claim' AND idempotency_key=? AND state='completed'", (task_id, service.store.scoped_key(key)), required=False)
        if prior:
            request = json.loads(prior["payload"])
            require(request["label"] == label and request["takeover"] == takeover, "IDEMPOTENCY_CONFLICT", "Lease request key has different semantics")
            saved = json.loads(prior["result"])
            atomic_write(path, canonical(saved))
            return {"token": saved["token"], "epoch": saved["epoch"]}
    lease = service.leases.claim(
        task_id,
        label=label,
        token=old.get("token"),
        takeover=takeover,
        key=key,
    )
    atomic_write(path, canonical(lease))
    return {"token": lease["token"], "epoch": lease["epoch"]}


def copy_blob(store: Store, sha: str, output: Path) -> dict:
    output = Path(output).expanduser().absolute()
    require(
        not output.resolve().is_relative_to(store.home),
        "INVALID_REQUEST",
        "Export outside the private author runtime",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    require(
        not output.exists(),
        "OUTPUT_EXISTS",
        "Export destination already exists; choose a new filename",
    )
    return store.blobs.export(sha, output)


def wait_for(
    service: AuthorService,
    operation: dict,
    *,
    seconds: int,
    dispatch: bool = False,
) -> dict:
    require(
        0 <= seconds <= MAX_WAIT_SECONDS,
        "INVALID_REQUEST",
        f"Wait must be between zero and {MAX_WAIT_SECONDS} seconds",
    )
    deadline = time.monotonic() + seconds
    while operation["state"] not in TERMINAL and time.monotonic() < deadline:
        if dispatch and operation["state"] == "queued":
            Checks(service.store).dispatch(operation_ids=[operation["id"]])
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
        operation = service.store.operation(operation["id"])
    return operation


def run(args) -> object:
    if args.command == "init":
        limits = (
            RuntimeLimits.model_validate(strict_json(args.limits.read_bytes()))
            if args.limits
            else RuntimeLimits()
        )
        store = Store.initialize(args.home, limits)
        return {
            "home": str(store.home),
            "instance_id": store.identity["instance_id"],
            "initialized": True,
        }
    if args.command == "restore":
        store = Maintenance.restore(args.archive, args.home)
        return {"restored": True, **store.integrity()}

    if args.command == "resolve-unknown-execution":
        # This local recovery boundary needs only the Author store. Do not
        # initialize the broader service graph or any HTTP/MCP interface.
        from ..operator_resolution import OperatorResolution
        store = Store(args.home, expected_instance=args.expected_instance)
        return OperatorResolution(store).resolve(
            args.operation_id,
            operator=args.operator,
            reason=args.reason,
            apply=args.apply,
            attestation=args.attest_quiescence,
        )

    service = AuthorService(args.home, expected_instance=args.expected_instance)
    store, command = service.store, args.command
    if command == "register":
        result = service.repositories.register(
            args.name,
            args.source,
            TaskPolicy.model_validate(strict_json(args.policy.read_bytes())),
            remote=args.remote,
            default_branch=args.default_branch,
            key=args.key,
        )
        return {
            key: result[key]
            for key in ("id", "name", "default_branch", "object_format")
        }
    if command == "repositories":
        return service.repositories.list()
    if command == "refresh":
        return service.repositories.refresh(
            args.repository,
            key=args.key,
            from_remote=args.from_remote,
        )
    if command == "task":
        base = args.base
        if args.dirty_path:
            require(base == "HEAD", "INVALID_REQUEST", "Explicit dirty capture starts from the source HEAD")
            from ..dirty import DirtySnapshots
            base = DirtySnapshots(store).capture(args.repository, args.dirty_path, key="dirty-" + args.key)["commit"]
        return service.tasks.create(
            args.repository,
            args.goal,
            base=base,
            key=args.key,
        )
    if command == "tasks":
        return service.tasks.list(offset=args.offset)
    if command == "claim":
        local_lease(
            service,
            args.task_id,
            label=args.label,
            takeover=args.takeover,
            key=args.key,
        )
        return {"task_id": args.task_id, "lease_saved": True}
    if command == "context":
        context = service.snapshots.export(
            args.task_id,
            revision=args.revision,
            paths=args.path,
            identity_only=args.identity_only,
            key=args.key,
        )
        return {
            "context_id": context["id"],
            **copy_blob(store, context["bundle_sha"], args.output),
        }
    if command == "apply":
        return service.materializer.submit(
            args.task_id,
            args.package,
            key=args.key,
            **local_lease(service, args.task_id),
        )
    if command == "check":
        task = service.tasks.get(args.task_id)
        operation = Checks(store).submit(
            args.task_id,
            revision=task["current_revision"],
            key=args.key,
            requested=args.check,
            **local_lease(service, args.task_id),
        )
        return (
            wait_for(service, operation, seconds=args.wait, dispatch=True)
            if args.wait
            else operation
        )
    if command == "status":
        operation = store.operation(args.operation_id)
        return (
            wait_for(service, operation, seconds=args.wait)
            if args.wait
            else operation
        )
    if command == "cancel":
        operation = store.operation(args.operation_id)
        return Checks(store).cancel(
            args.operation_id,
            **local_lease(service, operation["task_id"]),
        )
    if command == "feedback":
        feedback = Feedback(store)
        if args.output:
            result = feedback.export(args.task_id, key=args.key)
            return {
                **result,
                **copy_blob(store, result["blob_sha"], args.output),
            }
        result = feedback.get(args.task_id)
        return feedback.markdown(result) if args.markdown else result
    if command == "diff":
        return Feedback(store).diff(
            args.task_id,
            revision=args.revision,
            path=args.path,
        )
    if command == "logs":
        return Checks(store).log(
            args.operation_id,
            args.filename,
            offset=args.offset,
            limit=args.limit,
        )
    if command == "dispatch":
        return Checks(store).dispatch()
    if command == "recover":
        operation = store.operation(args.operation_id)
        if operation["kind"] == "package.submit":
            return service.materializer.recover(
                args.operation_id,
                **local_lease(service, operation["task_id"]),
            )
        require(
            operation["kind"] == "checks.run",
            "INVALID_REQUEST",
            "Use the original publish/integrate command and idempotency key to reconcile that operation",
        )
        return Checks(store).recover(args.operation_id)
    if command == "approve":
        task = service.tasks.get(args.task_id)
        return Publisher(store).approve(
            args.task_id,
            task["current_revision"],
            actor=args.actor,
            key=args.key,
            **local_lease(service, args.task_id),
        )
    if command == "publish":
        approval = store.one(
            "SELECT task_id FROM approvals WHERE id=?",
            (args.approval_id,),
        )
        return Publisher(store).apply(
            args.approval_id,
            key=args.key,
            **local_lease(service, approval["task_id"]),
        )
    if command == "publications":
        service.tasks.get(args.task_id)
        return store.rows(
            "SELECT id,commit_oid,branch,state,pull_request,updated "
            "FROM publications WHERE task_id=? ORDER BY created DESC LIMIT 50",
            (args.task_id,),
        )
    if command == "integrate":
        result = Integration(store).create(
            args.repository,
            strict_json(args.sources.read_bytes()),
            base=args.base,
            goal=args.goal,
            key=args.key,
            label="operator",
        )
        lease = result.pop("lease")
        lease_path = store.home / "operator-leases" / (result["task_id"] + ".json")
        require(
            not lease_path.is_symlink(),
            "UNSAFE_LEASE_CACHE",
            "Operator lease cache must not be a symbolic link",
        )
        atomic_write(lease_path, canonical(lease))
        return result
    if command == "backup":
        return Maintenance(store).backup(args.output, key=args.key)
    if command == "gc":
        return Maintenance(store).gc(minimum_age_seconds=args.minimum_age, key=args.key)
    if command == "doctor":
        return {**service.capabilities(), "integrity": store.integrity()}
    if command == "token":
        return strict_json((store.home / "tokens.json").read_bytes())[args.role]
    if command == "token-issue":
        return Access(store).issue(args.principal_id, args.role, args.task_ids, key=args.key)
    if command == "token-revoke":
        return Access(store).revoke(args.credential_id, key=args.key)
    if command == "optimize-admission":
        from ..persistence.admission_indexes import install
        return install(store)
    if command == "upgrade-state":
        from ..persistence.state_clock import install
        return install(store)
    if command == "qualify-client":
        return Access(store).qualify(
            strict_json(args.evidence.read_bytes()),
            actor=args.actor,
            key=args.key,
        )
    if command == "recovery":
        return service.recovery.get(args.task_id)
    if command == "operations":
        return service.recovery.operations(args.task_id, limit=args.limit, offset=args.offset, cursor=args.cursor)
    if command == "inbox":
        return service.inbox.pending()
    if command == "inbox-import":
        return service.inbox.ingest(args.package, key=args.key)
    if command == "inbox-approve":
        operation = store.operation(args.operation_id)
        return service.inbox.approve(args.operation_id, key=args.key, **local_lease(service, operation["task_id"]))
    if command == "inbox-grant":
        return service.inbox.grant(args.task_id, auto_validate=args.auto_validate, key=args.key, **local_lease(service, args.task_id))
    if command == "inbox-revoke":
        return service.inbox.revoke(args.task_id, key=args.key)
    if command in {"inbox-scan", "inbox-watch"}:
        require(0 <= args.stability_seconds <= 30, "INVALID_REQUEST", "Completion interval must be between zero and 30 seconds")
        if command == "inbox-scan":
            return service.inbox.scan(args.folder, stability_seconds=args.stability_seconds, max_files=args.max_files)
        require(1 <= args.poll_seconds <= 60 and args.cycles >= 0, "INVALID_REQUEST", "Watch interval/cycle count is invalid")
        # Only this explicit command opts in. Durable inbox operations/grants
        # survive this watcher stopping; nothing controls a model or a UI.
        cycles, scan_offset = 0, 0
        while args.cycles == 0 or cycles < args.cycles:
            result = service.inbox.scan(args.folder, stability_seconds=args.stability_seconds, max_files=args.max_files, offset=scan_offset)
            scan_offset = result["next_offset"] or 0
            cycles += 1
            print(json.dumps({"watch_cycle": cycles, **result}), flush=True)
            if args.cycles == 0 or cycles < args.cycles:
                time.sleep(args.poll_seconds)
        return {"watch_cycles": cycles, "stopped": True}
    if command == "control-status":
        return Control(store).status()
    if command == "policy":
        from ..policy import Policies
        return Policies(store).update(args.task_id, strict_json(args.policy.read_bytes()),
            expected_digest=args.expected_digest, actor=args.actor, key=args.key)
    if command in {"pause", "resume", "revoke-task", "restore-task"}:
        control = Control(store)
        method = {"pause": control.pause, "resume": control.resume, "revoke-task": control.revoke,
                  "restore-task": control.restore_grant}[command]
        values = {"key": args.key, "actor": args.actor}
        if command.endswith("-task"):
            values["task_id"] = args.task_id
        return method(**values)
    if command == "serve":
        import uvicorn

        from .http import create_app

        uvicorn.run(
            create_app(
                args.home,
                expected_instance=args.expected_instance,
                scheduler=args.run_scheduler,
                allowed_origins=tuple(args.allow_origin),
                output_profile=args.output_profile,
            ),
            host=args.host,
            port=args.port,
            access_log=False,
        )
        return {"stopped": True}
    raise AuthorError("INVALID_REQUEST", "Unknown author command")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, indent=2)
        )
        if isinstance(result, dict) and result.get("state") in {
            "failed",
            "blocked",
            "timed_out",
            "cancelled",
            "unknown_execution",
            "publication_unknown",
        }:
            return 1
        return 0
    except (AuthorError, ValidationError, OSError, ValueError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, AuthorError)
            else {"code": "INVALID_REQUEST", "message": str(exc)[:4000]}
        )
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Local waiting stopped. Authorized durable checks remain owned by their "
            "supervisors; use cancel explicitly.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
