"""Journaled whole-candidate construction and revision compare-and-swap."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from ..errors import AuthorError, require
from ..models import TaskPolicy
from ..packages.archive import ImportedPackage, PackageReader
from ..packages.validation import validate
from ..persistence.locks import file_lock
from ..persistence.store import Store
from ..persistence.storage import StorageBudget
from ..repositories import Repositories
from ..tasks import Tasks
from ..util import canonical, utc_now, fingerprint
from .leases import Leases
from ..persistence.control import Control
from ..authority import AuthIdentity


class Materializer:
    def __init__(self, store: Store):
        self.store = store
        self.tasks, self.repositories = Tasks(store), Repositories(store)
        self.leases = Leases(store)

    def submit(self, task_id: str, path: Path, *, key: str, token: str, epoch: int) -> dict:
        with file_lock(self.store.home / "locks/maintenance", shared=True):
            self.leases.assert_owned(task_id, token, epoch)
            StorageBudget(self.store).check(min(path.stat().st_size * 4, self.store.limits.archive.expanded_bytes))
            package = PackageReader(self.store).read(path)
            return self.submit_imported(task_id, package, key=key, token=token, epoch=epoch)

    def submit_imported(self, task_id: str, package: ImportedPackage, *, key: str, token: str, epoch: int,
                        _operation: dict | None = None) -> dict:
        self.leases.assert_owned(task_id, token, epoch)
        manifest = package.manifest
        require(manifest.task_id == task_id, "INVALID_PACKAGE", "Package targets a different task")
        if _operation is None:
            op, _ = self.store.begin_operation(kind="package.submit", task_id=task_id, key=key,
                                               fingerprint=package.fingerprint,
                                               payload={"archive_sha": package.archive_sha, "epoch": epoch},
                                               revision=manifest.expected_revision)
        else:
            op = self.store.one("SELECT * FROM operations WHERE id=?", (_operation["id"],))
            retained = json.loads(op["payload"])
            principal = retained.get("_principal_id")
            expected = self.store.scoped_fingerprint(package.fingerprint, identity=AuthIdentity(principal, "author")) if principal else package.fingerprint
            require(op["task_id"] == task_id and op["kind"] == "package.submit"
                    and op["fingerprint"] == expected and op["revision"] == manifest.expected_revision,
                    "IDEMPOTENCY_CONFLICT", "Recovery bytes differ from the accepted package intent")
        with file_lock(self.store.home / "locks/maintenance", shared=True), file_lock(self.store.home / ("locks/task-" + task_id)):
            try:
                observed = self.store.operation(op["id"])
                if observed["state"] == "completed":
                    return observed["result"]
                require(observed["state"] not in {"failed", "blocked"}, "OPERATION_FAILED",
                        "This submission previously failed; use a new idempotency key after correcting the cause",
                        previous=observed["error"])
                previous = self.store.one("SELECT * FROM revisions WHERE task_id=? AND fingerprint=?",
                                          (task_id, package.fingerprint), required=False)
                if previous:
                    result = {"operation_id": op["id"], "task_id": task_id, "revision": previous["number"],
                              "commit": previous["commit_oid"], "tree": previous["tree_oid"], "duplicate": True}
                    original = self.store.one("SELECT result FROM operations WHERE task_id=? AND kind='package.submit' AND fingerprint=? AND state='completed' ORDER BY created LIMIT 1",
                                              (task_id, self.store.scoped_fingerprint(package.fingerprint)), required=False)
                    if original and original["result"]:
                        checks_id = json.loads(original["result"]).get("checks_operation_id")
                        if checks_id:
                            result["checks_operation_id"] = checks_id
                    self.store.finish(op["id"], "completed", result=result)
                    return result
                Control(self.store).assert_admission(task_id)
                task = self.tasks.get(task_id)
                require(task["repository_id"] == manifest.repository_id, "INVALID_PACKAGE", "Repository identity mismatch")
                context = self.store.one("SELECT * FROM contexts WHERE id=?", (manifest.context_id,))
                git = self.repositories.git(task["repository_id"])
                replacements = validate(package, task=task, revision=task["revision"], context=context, git=git,
                                        read_payload=self.store.blobs.read)
                with self.store.transaction() as conn:
                    self.leases.assert_owned(task_id, token, epoch, conn=conn)
                    conn.execute("INSERT OR IGNORE INTO packages VALUES(?,?,?,?)",
                                 (package.fingerprint, package.archive_sha, canonical(manifest.wire()).decode(), utc_now()))
                    self.store.reference(conn, "package:" + package.fingerprint, package.archive_sha, *package.payloads.values())
                    conn.execute("UPDATE operations SET state='running',phase='constructing',updated=? WHERE id=?",
                                 (utc_now(), op["id"]))
                    self.store.event(conn, "package.constructing", task_id,
                                     {"operation_id": op["id"], "phase": "constructing"})
                with file_lock(self.store.home / ("locks/git-" + task["repository_id"])):
                    commit, tree = git.create_candidate(task["current_commit"], replacements, operation_id=op["id"],
                                                        created=op["created"], ref="refs/author/candidates/" + op["id"],
                                                        staging=self.store.home / "staging")
                retained = json.loads(op["payload"])
                candidate = {**{key: retained[key] for key in ("_request_id", "_principal_id") if key in retained},
                             "commit": commit, "tree": tree, "epoch": epoch, "archive_sha": package.archive_sha}
                with self.store.transaction() as conn:
                    conn.execute("UPDATE operations SET phase='candidate_pinned',payload=?,updated=? WHERE id=?",
                                 (canonical(candidate).decode(), utc_now(), op["id"]))
                    self.store.event(conn, "package.candidate_pinned", task_id,
                                     {"operation_id": op["id"], "phase": "candidate_pinned"})
                return self._accept(op, task, package, commit, tree, token, epoch)
            except AuthorError as exc:
                self.store.finish(op["id"], "failed", error=exc.as_dict())
                raise
            # Unexpected interruption remains nonterminal for explicit recovery.

    def _accept(self, op: dict, task: dict, package: ImportedPackage, commit: str, tree: str,
                token: str, epoch: int) -> dict:
        revision = task["current_revision"] + 1
        result = {"operation_id": op["id"], "task_id": task["id"], "revision": revision,
                  "commit": commit, "tree": tree, "duplicate": False}
        with self.store.transaction() as conn:
            self.leases.assert_owned(task["id"], token, epoch, conn=conn)
            Control(self.store).assert_admission(task["id"], conn=conn)
            current_policy = conn.execute('SELECT policy_digest FROM tasks WHERE id=?', (task['id'],)).fetchone()
            require(current_policy['policy_digest'] == task['policy_digest'], 'STALE_POLICY',
                    'Approved policy changed before candidate and automatic-check admission')
            changed = conn.execute("UPDATE tasks SET current_revision=?,current_commit=?,status='candidate_ready',updated=? WHERE id=? AND current_revision=? AND current_commit=?",
                                   (revision, commit, utc_now(), task["id"], task["current_revision"], task["current_commit"]))
            require(changed.rowcount == 1, "STALE_BASE", "A newer candidate won the revision race")
            conn.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (task["id"], revision, task["current_revision"], task["current_commit"], commit, tree,
                          package.fingerprint, package.archive_sha, package.manifest.context_id, utc_now()))
            policy = TaskPolicy.model_validate(task["policy"])
            if policy.auto_check:
                check_id = "op_" + uuid.uuid5(uuid.NAMESPACE_URL, "patchbay-author-auto-check:" + op["id"]).hex
                request = {"revision": revision, "commit": commit, "policy": task["policy_digest"],
                           "selected": [c.id for c in policy.profile.checks]}
                now = utc_now()
                conn.execute("""INSERT INTO operations
                    (id,task_id,kind,idempotency_key,fingerprint,revision,state,phase,payload,created,updated)
                    VALUES(?,?,'checks.run',?,?,?,'queued','accepted',?,?,?)""",
                    (check_id, task["id"], "auto-" + op["id"], fingerprint(request), revision, canonical(request).decode(), now, now))
                conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,'queued','not_required',NULL,?,?)",
                    (check_id, task["id"], revision, commit, tree, canonical(policy.profile.wire()).decode(),
                     fingerprint(policy.profile.wire()), None, now, now))
                result["checks_operation_id"] = check_id
                self.store.event(conn, "checks.auto_queued", task["id"], {"operation_id": check_id, "revision": revision})
            conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                         (canonical(result).decode(), utc_now(), op["id"]))
            self.store.reference(conn, "revision:" + task["id"] + ":" + str(revision), package.archive_sha)
            self.store.event(conn, "revision.accepted", task["id"], result)
        return result

    def recover(self, operation_id: str, *, token: str, epoch: int) -> dict:
        op = self.store.one("SELECT * FROM operations WHERE id=?", (operation_id,))
        require(op["kind"] == "package.submit", "INVALID_REQUEST", "Not a package operation")
        self.leases.assert_owned(op["task_id"], token, epoch)
        if op["state"] == "completed":
            return json.loads(op["result"])
        payload = json.loads(op["payload"])
        require(op["state"] not in {"failed", "blocked"}, "OPERATION_FAILED", "Terminal rejected submissions cannot be resumed")
        path = self.store.blobs.path(payload["archive_sha"])
        with file_lock(self.store.home / "locks/maintenance", shared=True):
            package = PackageReader(self.store).read(path)
            return self.submit_imported(op["task_id"], package,
                key=op["idempotency_key"], token=token, epoch=epoch, _operation=op)
