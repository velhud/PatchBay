"""Durable check admission and supervisor dispatch; reads never execute work."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..errors import AuthorError, require
from ..models import Profile, TaskPolicy
from ..persistence.gates import mutation
from ..persistence.control import Control
from ..persistence.locks import file_lock
from ..persistence.requests import replayable
from ..persistence.store import Store, TERMINAL
from ..revisions.leases import Leases
from ..tasks import Tasks
from ..util import atomic_write, canonical, fingerprint, safe_id, utc_now
from .admission import can_admit
from .identity import identity, same_process
from .logs import read_page


class Checks:
    def __init__(self, store: Store):
        self.store = store
        self.tasks, self.leases = Tasks(store), Leases(store)

    @mutation
    def submit(self, task_id: str, *, revision: int, key: str, token: str, epoch: int,
               requested: list[str] | None = None, dispatch: bool = True) -> dict:
        require(sys.platform == "linux", "BLOCKED_ENVIRONMENT", "Execution supervisors require Linux/WSL2; no unsupported platform fallback")
        self.leases.assert_owned(task_id, token, epoch)
        task = self.tasks.get(task_id)
        exact_revision = self.tasks.revision(task_id, revision)
        policy = TaskPolicy.model_validate(task["policy"])
        require(policy.allow_background_checks, "POLICY_DENIED", "This task does not authorize durable background execution")
        requested = sorted(set(requested or []))
        require(set(requested) <= {c.id for c in policy.profile.checks}, "POLICY_DENIED", "Unknown check ID")
        selected = [c.id for c in policy.profile.checks if not requested or c.required or c.id in requested]
        request = {"revision": revision, "commit": exact_revision["commit_oid"], "policy": task["policy_digest"], "selected": selected}
        with file_lock(self.store.home / ("locks/task-" + task_id)):
            def initialize(conn, operation):
                self.leases.assert_owned(task_id, token, epoch, conn=conn)
                Control(self.store).assert_admission(task_id, conn=conn)
                current = conn.execute("SELECT current_revision FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
                require(current == revision, "STALE_BASE", "Task changed while checks were submitted")
                conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,'queued','not_required',NULL,?,?)",
                             (operation["id"], task_id, revision, exact_revision["commit_oid"], exact_revision["tree_oid"],
                              canonical(policy.profile.wire()).decode(), fingerprint(policy.profile.wire()), None,
                              operation["created"], operation["created"]))
            op, new = self.store.begin_operation(kind="checks.run", task_id=task_id, key=key,
                                                 fingerprint=fingerprint(request), payload=request, revision=revision,
                                                 initialize=initialize)
            if not new and op["state"] in TERMINAL:
                return self.store.operation(op["id"])
        if dispatch:
            self.dispatch(operation_ids=[op["id"]])
        return self.store.operation(op["id"])

    def dispatch(self, *, operation_ids: list[str] | None = None) -> dict:
        """Explicit scheduler action. It never retries an uncertain execution."""
        require(sys.platform == "linux", "BLOCKED_ENVIRONMENT", "Execution supervisors require Linux/WSL2; no unsupported platform fallback")
        launched, waiting = [], []
        if operation_ids is not None:
            require(1 <= len(operation_ids) <= 100, "INVALID_REQUEST", "Dispatch needs one to 100 existing operation IDs")
            for operation_id in operation_ids:
                safe_id(operation_id)
        with file_lock(self.store.home / "locks/maintenance", shared=True), file_lock(self.store.home / "locks/scheduler"):
            sql = "SELECT r.*,o.payload FROM runs r JOIN operations o ON r.id=o.id WHERE r.state='queued'"
            args = ()
            if operation_ids is not None:
                sql += " AND r.id IN (" + ",".join("?" for _ in operation_ids) + ")"
                args = tuple(operation_ids)
            pending = self.store.rows(sql + " ORDER BY r.created LIMIT 100", args)
            for row in pending:
                active = self.store.rows("SELECT * FROM runs WHERE state IN ('launching','running','cancelling','unknown_execution') OR cleanup='unresolved'")
                if any(other["task_id"] == row["task_id"] and (other["state"] == "unknown_execution" or other["cleanup"] == "unresolved") for other in active):
                    self._record_wait(row["id"], "task_execution_fenced")
                    waiting.append({"operation_id": row["id"], "reason": "task_execution_fenced"})
                    continue
                profile = Profile.model_validate(json.loads(row["profile"]))
                fits, admission = can_admit(self.store, profile, active)
                if not fits:
                    self._record_wait(row["id"], "resource_budget", admission)
                    waiting.append({"operation_id": row["id"], "reason": "resource_budget", "admission": admission})
                    continue
                root = self.store.home / "runs" / row["id"]
                require(not root.exists(), "EXECUTION_UNKNOWN", "Run directory already exists; reconcile before launch")
                root.mkdir(mode=0o700)
                with self.store.transaction() as conn:
                    reserved = conn.execute("UPDATE runs SET state='launching',cleanup='pending',updated=? WHERE id=? AND state='queued'", (utc_now(), row["id"]))
                    if reserved.rowcount != 1:
                        root.rmdir()
                        continue
                    conn.execute("UPDATE operations SET state='running',phase='launching',updated=? WHERE id=?", (utc_now(), row["id"]))
                    self.store.event(conn, "checks.launching", row["task_id"], {"operation_id": row["id"]})
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
                       "PYTHONPATH": str(Path(__file__).resolve().parents[3]), "PYTHONDONTWRITEBYTECODE": "1"}
                try:
                    with (root / "supervisor.log").open("xb") as log:
                        child = subprocess.Popen([sys.executable, "-m", "patchbay.authoring.execution.worker",
                                                  "--home", str(self.store.home), "--operation", row["id"]],
                                                 cwd=self.store.home, env=env, stdin=subprocess.DEVNULL,
                                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
                except OSError as exc:
                    with self.store.transaction() as conn:
                        conn.execute("UPDATE runs SET state='blocked',cleanup='proven',updated=? WHERE id=?", (utc_now(), row["id"]))
                    self.store.finish(row["id"], "blocked", error={"code": "BLOCKED_ENVIRONMENT", "message": "Supervisor could not be launched", "detail": str(exc)[:1000]})
                    continue
                record = identity(child.pid)
                require(record is not None, "EXECUTION_UNKNOWN", "Could not establish supervisor identity; launch gate remains closed")
                with self.store.transaction() as conn:
                    conn.execute("UPDATE operations SET owner_identity=?,updated=? WHERE id=?", (canonical(record).decode(), utc_now(), row["id"]))
                    self.store.event(conn, "checks.supervisor_owned", row["task_id"], {"operation_id": row["id"]})
                atomic_write(root / "launch.json", canonical({"operation_id": row["id"], "supervisor": record}))
                launched.append(row["id"])
        return {"launched": launched, "waiting": waiting}

    def _record_wait(self, operation_id: str, reason: str, admission: dict | None = None) -> None:
        """Cache meaningful admission blockers; unchanged polling writes nothing."""
        details = {"reason": reason, "admission": admission}
        # Free-memory/disk samples fluctuate; transitions depend on actual gates
        # and reservations, not on an unchanged scheduler heartbeat.
        identity = fingerprint({"reason": reason, "gates": (admission or {}).get("gates"),
                                "reservation": (admission or {}).get("reservation"),
                                "active_jobs": (admission or {}).get("active_jobs")})
        with self.store.transaction() as conn:
            row = conn.execute("SELECT task_id,state,payload FROM operations WHERE id=?", (operation_id,)).fetchone()
            require(row is not None, "NOT_FOUND", "Queued operation is unavailable")
            if row["state"] != "queued":
                return
            payload = json.loads(row["payload"])
            if payload.get("_admission", {}).get("identity") == identity:
                return
            payload["_admission"] = {**details, "identity": identity, "observed_at": utc_now()}
            conn.execute("UPDATE operations SET phase='waiting_for_admission',payload=?,updated=? WHERE id=? AND state='queued'",
                         (canonical(payload).decode(), utc_now(), operation_id))
            self.store.event(conn, "checks.admission_wait", row["task_id"], {"operation_id": operation_id, "reason": reason})

    @mutation
    @replayable("checks.cancel")
    def cancel(self, operation_id: str, *, token: str, epoch: int) -> dict:
        run = self.store.one("SELECT * FROM runs WHERE id=?", (operation_id,))
        self.leases.assert_owned(run["task_id"], token, epoch)
        with self.store.transaction() as conn:
            op = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op["state"] in TERMINAL:
                return self.store.public_operation(dict(op))
            current_run = conn.execute("SELECT state FROM runs WHERE id=?", (operation_id,)).fetchone()
            if current_run["state"] == "queued":
                conn.execute("UPDATE runs SET state='cancelled',cleanup='proven',updated=? WHERE id=?", (utc_now(), operation_id))
                conn.execute("UPDATE operations SET state='cancelled',phase='terminal',cancel_requested=1,updated=? WHERE id=?", (utc_now(), operation_id))
            else:
                conn.execute("UPDATE operations SET cancel_requested=1,phase='cancelling',updated=? WHERE id=?", (utc_now(), operation_id))
                conn.execute("UPDATE runs SET state='cancelling',updated=? WHERE id=?", (utc_now(), operation_id))
            self.store.event(conn, "checks.cancel_requested", run["task_id"], {"operation_id": operation_id})
        return self.store.operation(operation_id)

    @mutation
    def recover(self, operation_id: str) -> dict:
        safe_id(operation_id)
        op = self.store.one("SELECT * FROM operations WHERE id=? AND kind='checks.run'", (operation_id,))
        if op["state"] in TERMINAL and op["state"] != "unknown_execution":
            return self.store.public_operation(op)
        root = self.store.home / "runs" / operation_id
        receipt = root / "receipt.json"
        if not receipt.exists():
            receipt = root / "receipt.pending.json"
        if receipt.exists():
            from .receipts import record_receipt
            record_receipt(self.store, operation_id, json.loads(receipt.read_bytes()))
        elif op["owner_identity"] and same_process(json.loads(op["owner_identity"])):
            return {**self.store.public_operation(op), "recovery": "supervisor_still_owned"}
        else:
            with self.store.transaction() as conn:
                conn.execute("UPDATE runs SET state='unknown_execution',cleanup='unresolved',updated=? WHERE id=?", (utc_now(), operation_id))
                conn.execute("UPDATE operations SET state='unknown_execution',phase='ownership_unresolved',error=?,updated=? WHERE id=?",
                             (canonical({"code": "EXECUTION_UNKNOWN", "message": "Supervisor ownership or completion cannot be proven; no automatic rerun or PID kill"}).decode(), utc_now(), operation_id))
                self.store.event(conn, "checks.unknown_execution", op["task_id"], {"operation_id": operation_id})
        return self.store.operation(operation_id)

    def log(self, operation_id: str, filename: str, *, offset: int = 0, limit: int = 65536) -> dict:
        safe_id(operation_id)
        self.store.operation(operation_id)  # Enforce the authenticated task scope before reading bytes.
        require(filename == "supervisor.log" or __import__("re").fullmatch(r"[0-9]{3}-(stdout|stderr)\.log", filename),
                "INVALID_REQUEST", "Invalid log name")
        row = self.store.one("SELECT receipt_sha FROM runs WHERE id=?", (operation_id,))
        if row["receipt_sha"]:
            receipt = json.loads(self.store.blobs.read(row["receipt_sha"]))
            for check in receipt.get("setup", []) + receipt.get("checks", []):
                for stream in ("stdout", "stderr"):
                    metadata = check.get(stream, {})
                    if metadata.get("file") == filename:
                        page = read_page(self.store.blobs.path(metadata["sha256"]), offset=offset, limit=limit)
                        return {**page, "sha256": metadata["sha256"], "observed_bytes": metadata["observed_bytes"], "truncated": metadata["truncated"]}
        return read_page(self.store.home / "runs" / operation_id / filename, offset=offset, limit=limit)
