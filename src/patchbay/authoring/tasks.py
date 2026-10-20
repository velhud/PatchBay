"""Task origin, authority, and current revisions are durable and explicit."""
from __future__ import annotations

import json
from .authority import assert_task_scope, current_identity
from .errors import require
from .persistence.gates import mutation
from .persistence.control import Control
from .persistence.locks import file_lock
from .persistence.storage import StorageBudget
from .repositories import Repositories
from .util import canonical, fingerprint, safe_id, utc_now


class Tasks:
    def __init__(self, store):
        self.store = store
        self.repositories = Repositories(store)

    def revision(self, task_id: str, revision: int) -> dict:
        safe_id(task_id)
        assert_task_scope(task_id)
        require(type(revision) is int and revision >= 0, "INVALID_REQUEST", "Invalid revision number")
        return self.store.one("SELECT * FROM revisions WHERE task_id=? AND number=?", (task_id, revision))

    def get(self, task_id: str) -> dict:
        safe_id(task_id)
        assert_task_scope(task_id)
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            require(row is not None, "NOT_FOUND", "Task does not exist", status=404)
            value = dict(row)
            revision = conn.execute("SELECT * FROM revisions WHERE task_id=? AND number=?", (task_id, value["current_revision"])).fetchone()
            require(revision is not None, "CORRUPT_STATE", "Task revision is unavailable")
            value["revision"] = dict(revision)
        value["policy"] = json.loads(value["policy"])
        value["dependencies"] = json.loads(value["dependencies"])
        return value

    def list(self, *, limit: int = 25, offset: int = 0, cursor: str | None = None) -> dict:
        require(1 <= limit <= 200 and offset >= 0, "INVALID_REQUEST", "Invalid task page")
        scope = current_identity().task_ids
        where = " WHERE id IN (" + ",".join("?" for _ in scope) + ")" if scope is not None else ""
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            watermark = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            identity = fingerprint({"task_page_watermark": watermark, "task_scope": scope})
            require(cursor is None or cursor == identity, "STALE_CURSOR", "State changed between task pages; start a new page sequence", watermark=watermark)
            rows = [dict(r) for r in conn.execute("SELECT id,repository_id,substr(goal,1,2048) AS goal,length(goal)>2048 AS goal_truncated,length(CAST(goal AS BLOB)) AS goal_bytes,current_commit,current_revision,status,updated FROM tasks" + where + " ORDER BY created,id LIMIT ? OFFSET ?", (*scope, limit + 1, offset) if scope is not None else (limit + 1, offset))]
        for row in rows:
            row["goal_truncated"] = bool(row["goal_truncated"])
        return {"tasks": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None,
                "cursor": identity, "watermark": watermark}

    @mutation
    def create(self, repository: str, goal: str, *, base: str = "HEAD", key: str, dependencies: list[dict] | None = None) -> dict:
        require(current_identity().task_ids is None, "FORBIDDEN", "Creating a new task requires a principal with repository-wide task authority", status=403)
        require(isinstance(goal, str) and 0 < len(goal.encode()) <= 200000, "INVALID_REQUEST", "Task needs a bounded implementation goal")
        repo = self.repositories.get(repository)
        git = self.repositories.git(repo["id"])
        dependencies = dependencies or []
        require(len(dependencies) <= 100, "INVALID_REQUEST", "Too many dependencies")
        for dependency in dependencies:
            require(set(dependency) == {"task_id", "revision"}, "INVALID_REQUEST", "Dependencies need exact task and revision")
            self.revision(dependency["task_id"], dependency["revision"])
        request = {"repository_id": repo["id"], "goal": goal, "base": base, "dependencies": dependencies}
        commit = git.resolve(base)
        payload = {**request, "commit": commit, "tree": git.tree(commit), "policy": repo["policy"]}
        source_snapshot = self.store.one("SELECT value FROM metadata WHERE key=?", ("source-snapshot:" + commit,), required=False)
        payload["origin_commit"] = json.loads(source_snapshot["value"])["upstream_commit"] if source_snapshot else commit
        op, _ = self.store.begin_operation(kind="task.create", task_id=repo["id"], key=key,
                                           fingerprint=fingerprint(request), payload=payload)
        task_id = "task_" + op["id"][3:]
        with file_lock(self.store.home / ("locks/task-" + task_id)):
            if self.store.one("SELECT 1 FROM tasks WHERE id=?", (task_id,), required=False):
                return self.get(task_id)
            Control(self.store).assert_admission()
            StorageBudget(self.store).check(4096)
            # A replay uses its original captured commit and policy, not a newer HEAD.
            retained = json.loads(op["payload"])
            with file_lock(self.store.home / ("locks/git-" + repo["id"])):
                git.run(["update-ref", "refs/author/tasks/" + task_id + "/origin", retained["commit"]])
            now = op["created"]
            with self.store.transaction() as conn:
                Control(self.store).assert_admission(conn=conn)
                conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,0,?,?,'waiting_for_package',?,?,?)",
                             (task_id, repo["id"], goal, retained.get("origin_commit", retained["commit"]), retained["commit"],
                              canonical(retained["policy"]).decode(), fingerprint(retained["policy"]),
                              canonical(dependencies).decode(), now, now))
                conn.execute("INSERT INTO revisions VALUES(?,0,NULL,?,?,?,NULL,NULL,NULL,?)",
                             (task_id, retained["commit"], retained["commit"], retained["tree"], now))
                conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                             (canonical({"task_id": task_id}).decode(), utc_now(), op["id"]))
                self.store.event(conn, "task.created", task_id, {"revision": 0, "commit": retained["commit"]})
            return self.get(task_id)
