"""One durable Linux supervisor per admitted run, independent of the HTTP process."""
from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import time
from pathlib import Path

from ..errors import AuthorError, require
from ..models import Profile
from ..persistence.locks import file_lock
from ..persistence.store import Store
from ..repositories import Repositories
from ..tasks import Tasks
from ..util import atomic_write, canonical, safe_id, strict_json, utc_now
from .environment import qualify
from .identity import enable_subreaper, identity, same_process, cleanup_owned
from .process import execute
from .receipts import new_receipt, classify, record_receipt
from .reports import evaluate, prepare_report
from .source import expected_source, source_changes


def await_launch(root: Path, operation_id: str, *, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gate = root / "launch.json"
        if gate.is_file():
            value = strict_json(gate.read_bytes())
            record = value.get("supervisor")
            require(value.get("operation_id") == operation_id and record and record.get("pid") == os.getpid()
                    and same_process(record), "EXECUTION_UNKNOWN", "Supervisor launch ownership did not match")
            return
        time.sleep(.05)
    raise AuthorError("BLOCKED_ENVIRONMENT", "Supervisor launch gate was never durably released; candidate code was not started")


def run(home: Path, operation_id: str) -> int:
    safe_id(operation_id)
    store = Store(home)
    root = store.home / "runs" / operation_id
    require(root.is_dir() and not root.is_symlink(), "EXECUTION_UNKNOWN", "Execution reservation directory is unavailable")
    with file_lock(store.home / "locks/maintenance", shared=True), file_lock(root / "supervisor.lock"):
        row = store.one("SELECT * FROM runs WHERE id=?", (operation_id,))
        if row["state"] not in {"launching", "running", "cancelling"}:
            return 0
        task = Tasks(store).get(row["task_id"])
        admitted = json.loads(store.one("SELECT payload FROM operations WHERE id=?", (operation_id,))["payload"])
        receipt = new_receipt(store, row, admitted.get("policy", task["policy_digest"]))
        profile = Profile.model_validate(json.loads(row["profile"]))
        required = {check.id for check in profile.checks if check.required}
        workspace = root / "source"
        owned = {}
        subreaper_ready = False
        try:
            await_launch(root, operation_id)
            enable_subreaper()
            subreaper_ready = True
            profile, environment = qualify(profile, root)
            receipt.update(environment=environment, environment_digest=environment["environment_digest"])
            git = Repositories(store).git(task["repository_id"])
            expected = expected_source(git, row["commit_oid"])
            git.checkout(row["commit_oid"], workspace)
            changes = source_changes(workspace, expected, profile.allowed_outputs, commit=row["commit_oid"])
            require(not changes, "BLOCKED_ENVIRONMENT", "Checkout filters or source modes did not preserve the pinned bytes", source_changes=changes)
            with store.transaction() as conn:
                conn.execute("UPDATE runs SET state=CASE WHEN state='cancelling' THEN state ELSE 'running' END,environment_digest=?,updated=? WHERE id=?",
                             (environment["environment_digest"], utc_now(), operation_id))
                conn.execute("UPDATE operations SET phase=CASE WHEN cancel_requested=1 THEN 'cancelling' ELSE 'verifying' END,updated=? WHERE id=?",
                             (utc_now(), operation_id))
                current = conn.execute("SELECT phase,cancel_requested FROM operations WHERE id=?", (operation_id,)).fetchone()
                store.event(conn, "checks.supervisor_started", row["task_id"],
                            {"operation_id": operation_id, "phase": current["phase"],
                             "cancel_requested": bool(current["cancel_requested"])})
            selected = set(json.loads(store.one("SELECT payload FROM operations WHERE id=?", (operation_id,))["payload"])["selected"])
            def cancelled():
                return bool(store.one("SELECT cancel_requested FROM operations WHERE id=?", (operation_id,))["cancel_requested"])
            def child_identity(record, container):
                if record:
                    owned[record["pid"]] = record
                atomic_write(root / "child.json", canonical({"process": record, "container": container, "operation_id": operation_id}))
            sequence = 0
            stop = False
            for stage, checks in (("setup", profile.setup), ("checks", [check for check in profile.checks if check.id in selected])):
                for check in checks:
                    if cancelled():
                        receipt[stage].append({"id": check.id, "required": check.required, "state": "cancelled",
                                               "verification": "incomplete", "cleanup": {"state": "not_required"}})
                        stop = True
                        break
                    prepare_report(workspace, check)
                    raw = execute(profile, check, workspace, root, stage=stage, sequence=sequence,
                                  cancelled=cancelled, identity_callback=child_identity)
                    sequence += 1
                    for name in ("stdout", "stderr"):
                        raw[name]["sha256"] = store.blobs.put_file(root / raw[name]["file"], limit=profile.log_bytes)
                    result = evaluate(store, profile, check, workspace, raw)
                    result["stage"] = stage
                    receipt[stage].append(result)
                    run_usage = receipt["resource_usage"]
                    check_usage = raw.get("resource_usage", {})
                    run_usage["samples"] += int(check_usage.get("samples") or 0)
                    methods = set(filter(None, (run_usage.get("method") or "").split(";")))
                    methods.update(filter(None, (check_usage.get("method") or "").split(";")))
                    run_usage["method"] = ";".join(sorted(methods)) or None
                    rss_methods = set(filter(None, (run_usage.get("rss_method") or "").split(";")))
                    rss_methods.update(filter(None, (check_usage.get("rss_method") or "").split(";")))
                    run_usage["rss_method"] = ";".join(sorted(rss_methods)) or None
                    for field, label in (("cpu_cores_peak", "CPU"), ("rss_bytes_peak", "RSS"),
                                         ("pids_peak", "PID"), ("disk_bytes_peak", "disk")):
                        value = check_usage.get(field)
                        if value is not None:
                            previous = run_usage.get(field)
                            run_usage[field] = value if previous is None else max(previous, value)
                            if label in run_usage["unavailable"]:
                                run_usage["unavailable"].remove(label)
                    run_usage["observation_state"] = "observed" if not run_usage["unavailable"] else (
                        "partial" if run_usage["samples"] else "unavailable")
                    observed = source_changes(workspace, expected, profile.allowed_outputs, commit=row["commit_oid"])
                    receipt["source_changes_during_checks"] = observed
                    # Preserve the first mutation: later checks cannot restore bytes and erase its evidence.
                    if observed or raw["cleanup"]["state"] == "unresolved" or raw["state"] in {"cancelled", "timed_out"}:
                        stop = True
                        break
                    if stage == "setup" and result["verification"] != "passed":
                        stop = True
                        break
                if stop:
                    break
        except BaseException as exc:
            code = getattr(exc, "code", None)
            if isinstance(exc, FileNotFoundError) or (isinstance(exc, OSError) and exc.errno == errno.ENOENT):
                code = "MISSING_EXECUTABLE"
            elif isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
                code = "DISK_EXHAUSTED"
            receipt["error"] = {"code": code or "EXECUTOR_ERROR", "message": str(exc)[:4000], "type": type(exc).__name__}
        finally:
            if subreaper_ready:
                try:
                    cleanup = cleanup_owned(owned)
                    receipt["cleanup"] = cleanup["state"]
                    receipt["supervisor_cleanup"] = cleanup
                except BaseException as exc:
                    receipt["cleanup"] = "unresolved"
                    receipt["supervisor_cleanup"] = {"state": "unresolved", "error": str(exc)[:2000]}
            else:
                # The launch gate or subreaper check failed before any candidate process started.
                receipt["cleanup"] = "proven"
            if any(check.get("cleanup", {}).get("state") == "unresolved" for check in receipt["setup"] + receipt["checks"]):
                receipt["cleanup"] = "unresolved"
            classify(receipt, required)
            receipt["completed"] = utc_now()
            atomic_write(root / "receipt.pending.json", canonical(receipt))
            terminal = record_receipt(store, operation_id, receipt)
            final_sha = terminal.get("result", {}).get("receipt_sha") if terminal.get("result") else None
            if final_sha:
                atomic_write(root / "receipt.json", store.blobs.read(final_sha))
                (root / "receipt.pending.json").unlink(missing_ok=True)
        return 0 if receipt["classification"] == "READY_FOR_REVIEW" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.home, args.operation)
    except Exception as exc:
        print(json.dumps({"code": getattr(exc, "code", "SUPERVISOR_ERROR"), "message": str(exc)[:4000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
