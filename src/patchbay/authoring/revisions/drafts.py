"""Live author edits are versioned drafts; sealing uses the ordinary package validator."""
from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..errors import AuthorError, require
from ..models import TaskPolicy
from ..packages.diff import from_commit
from ..packages.validation import writable
from ..persistence.gates import mutation
from ..persistence.control import Control
from ..persistence.locks import file_lock
from ..persistence.store import TERMINAL
from ..repositories import Repositories
from ..tasks import Tasks
from ..util import canonical, digest, fingerprint, safe_id, safe_path, utc_now
from .leases import Leases
from .materialize import Materializer


class Drafts:
    def __init__(self, store):
        self.store = store
        self.tasks, self.repositories, self.leases = Tasks(store), Repositories(store), Leases(store)

    @staticmethod
    def _replay_result(observed: dict, *, message: str) -> dict | None:
        if observed["state"] == "completed":
            return observed["result"]
        require(observed["state"] not in TERMINAL, "OPERATION_FAILED", message,
                previous=observed.get("error"))
        return None

    @contextmanager
    def _durable_rejection(self, operation_id: str):
        try:
            yield
        except AuthorError as error:
            observed = self.store.operation(operation_id)
            if observed["state"] not in TERMINAL:
                self.store.finish(operation_id, "failed", error=error.as_dict())
            raise

    @mutation
    def apply(self, task_id: str, *, expected_revision: int, expected_version: int, edits: list[dict],
              key: str, token: str, epoch: int, draft_id: str | None = None) -> dict:
        self.leases.assert_owned(task_id, token, epoch)
        require(isinstance(edits, list) and 1 <= len(edits) <= 1000, "INVALID_REQUEST", "A draft update requires bounded explicit edits")
        request = {"task_id": task_id, "expected_revision": expected_revision, "expected_version": expected_version,
                   "draft_id": draft_id, "edits": edits}
        op, _ = self.store.begin_operation(kind="draft.apply", task_id=task_id, key=key, fingerprint=fingerprint(request), payload=request, revision=expected_revision)
        with file_lock(self.store.home / ("locks/task-" + task_id)), self._durable_rejection(op["id"]):
            observed = self.store.operation(op["id"])
            replay = self._replay_result(observed, message="This draft update was rejected; correct the cause and use a new idempotency key")
            if replay is not None:
                return replay
            Control(self.store).assert_admission(task_id)
            task = self.tasks.get(task_id)
            require(task["current_revision"] == expected_revision, "STALE_BASE", "The task candidate changed while this draft was being authored")
            if draft_id is None:
                require(expected_version == 0, "STALE_DRAFT", "New drafts start at version zero")
                draft_id = "draft_" + op["id"][3:]
                base_commit = task["current_commit"]
            else:
                safe_id(draft_id)
                draft = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
                require(draft["task_id"] == task_id and draft["base_revision"] == expected_revision and draft["state"] == "open"
                        and draft["version"] == expected_version, "STALE_DRAFT", "Draft identity/version changed or was already sealed")
                base_commit = draft["commit_oid"]
            policy = TaskPolicy.model_validate(task["policy"])
            git = self.repositories.git(task["repository_id"])
            entries = git.entries(base_commit)
            replacements = {}
            for edit in edits:
                require(isinstance(edit, dict) and set(edit) <= {"op", "path", "content", "before_sha256", "old_text", "new_text", "mode"},
                        "INVALID_REQUEST", "Unknown draft edit field")
                path = safe_path(edit.get("path", ""))
                writable(path, policy)
                require(path not in replacements, "INVALID_REQUEST", "An edit batch may touch each path only once")
                existing = entries.get(path)
                require(existing is None or existing["mode"] in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported draft file mode")
                old = git.blob(existing["oid"]) if existing else None
                kind = edit.get("op")
                if kind == "create":
                    require(existing is None, "STALE_DRAFT", "Draft create target already exists")
                else:
                    require(existing is not None, "STALE_DRAFT", "Draft source file is missing")
                    require(edit.get("before_sha256") == digest(old), "STALE_DRAFT", "Draft edit hash does not match its exact source version")
                if kind == "delete":
                    require(set(edit) <= {"op", "path", "before_sha256"}, "INVALID_REQUEST", "Delete cannot carry replacement text")
                    replacements[path] = None
                    continue
                if kind == "edit":
                    require(isinstance(edit.get("old_text"), str) and edit["old_text"], "INVALID_REQUEST", "Exact edit requires nonempty old_text")
                    require(isinstance(edit.get("new_text"), str), "INVALID_REQUEST", "Exact edit requires new_text")
                    text = old.decode("utf-8")
                    require(text.count(edit["old_text"]) == 1, "STALE_DRAFT", "Exact old_text must match precisely once")
                    content = text.replace(edit["old_text"], edit["new_text"], 1).encode()
                else:
                    require(kind in {"create", "replace"} and isinstance(edit.get("content"), str), "INVALID_REQUEST", "Draft edits require create/replace/edit/delete and explicit text")
                    content = edit["content"].encode()
                require(len(content) <= self.store.limits.archive.member_bytes, "RESOURCE_LIMIT", "Draft file is too large")
                mode = edit.get("mode") or (existing["mode"] if existing else "100644")
                require(mode in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported draft mode")
                replacements[path] = (content, mode)
            final_paths = (set(entries) - {p for p, v in replacements.items() if v is None}) | {p for p, v in replacements.items() if v is not None}
            require(len({p.casefold() for p in final_paths}) == len(final_paths), "INVALID_PATH", "Draft paths collide by case")
            for path in final_paths:
                parts = path.split("/")
                require(not any("/".join(parts[:i]) in final_paths for i in range(1, len(parts))), "INVALID_PATH", "Draft has a file/directory collision")
            with file_lock(self.store.home / ("locks/git-" + task["repository_id"])):
                commit, tree = git.create_candidate(base_commit, replacements, operation_id=op["id"], created=op["created"],
                                                    ref="refs/author/drafts/" + op["id"], staging=self.store.home / "staging")
            version = expected_version + 1
            result = {"draft_id": draft_id, "task_id": task_id, "base_revision": expected_revision, "version": version, "commit": commit, "tree": tree, "accepted_revision": False}
            with self.store.transaction() as conn:
                self.leases.assert_owned(task_id, token, epoch, conn=conn)
                Control(self.store).assert_granted(task_id, conn=conn)
                if expected_version == 0:
                    conn.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,'open',?,?)", (draft_id, task_id, expected_revision, version, tree, commit, op["created"], utc_now()))
                else:
                    changed = conn.execute("UPDATE drafts SET version=?,tree_oid=?,commit_oid=?,updated=? WHERE id=? AND version=? AND state='open'", (version, tree, commit, utc_now(), draft_id, expected_version))
                    require(changed.rowcount == 1, "STALE_DRAFT", "Another draft revision won the update")
                conn.execute("INSERT INTO draft_versions VALUES(?,?,?,?,?)", (draft_id, version, commit, tree, utc_now()))
                conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?", (canonical(result).decode(), utc_now(), op["id"]))
                self.store.event(conn, "draft.updated", task_id, result)
            return result

    def read(self, draft_id: str, version: int, path: str, *, start_line: int = 1, max_lines: int = 200, max_bytes: int = 65536,
             byte_offset: int | None = None) -> dict:
        from ..source_windows import read_window, bind_source_retrieval
        require(type(version) is int and version > 0, "INVALID_REQUEST", "Invalid draft version")
        draft = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        snapshot = self.store.one("SELECT * FROM draft_versions WHERE draft_id=? AND version=?", (draft_id, version))
        task = self.tasks.get(draft["task_id"])
        selector = {'draft_id': draft_id, 'draft_version': version}
        try:
            result = read_window(self.repositories.git(task['repository_id']), snapshot['commit_oid'], path,
                                 start_line=start_line, max_lines=max_lines, max_bytes=max_bytes, byte_offset=byte_offset)
        except AuthorError as error:
            bind_source_retrieval(error.details, selector)
            raise
        return {'draft_id': draft_id, 'version': version, **bind_source_retrieval(result, selector)}

    @mutation
    def seal(self, draft_id: str, *, expected_version: int, key: str, token: str, epoch: int) -> dict:
        draft = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        task_id = draft["task_id"]
        self.leases.assert_owned(task_id, token, epoch)
        request = {"draft_id": draft_id, "expected_version": expected_version}
        op, _ = self.store.begin_operation(kind="draft.seal", task_id=task_id, key=key, fingerprint=fingerprint(request), payload=request, revision=draft["base_revision"])
        with file_lock(self.store.home / ("locks/task-" + task_id)), self._durable_rejection(op["id"]):
            observed = self.store.operation(op["id"])
            replay = self._replay_result(observed, message="This draft seal was rejected; correct the cause and use a new idempotency key")
            if replay is not None:
                return replay
            child_key = "draft-seal-" + op["id"]
            child = self.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit' AND idempotency_key=?", (task_id, self.store.scoped_key(child_key)), required=False)
            if child and child["state"] == "completed":
                accepted = json.loads(child["result"])
            else:
                current = self.store.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
                require(current["version"] == expected_version and current["state"] == "open", "STALE_DRAFT", "Only the exact current open draft may be sealed")
                with tempfile.TemporaryDirectory(prefix="seal-", dir=self.store.home / "staging") as temporary:
                    package = Path(temporary) / "changes.zip"
                    from_commit(self.store, task_id, revision=draft["base_revision"], target_commit=current["commit_oid"], output=package, summary="Seal Pro-authored live draft")
                    accepted = Materializer(self.store).submit(task_id, package, key=child_key, token=token, epoch=epoch)
            with self.store.transaction() as conn:
                conn.execute("UPDATE drafts SET state='sealed',updated=? WHERE id=? AND version=?", (utc_now(), draft_id, expected_version))
            result = {"draft_id": draft_id, "version": expected_version, **accepted}
            self.store.finish(op["id"], "completed", result=result)
            return result
