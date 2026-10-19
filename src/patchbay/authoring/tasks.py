"""Task origin, authority, and current revisions are durable and explicit."""
from __future__ import annotations

import json
from .errors import require
from .persistence.gates import mutation
from .persistence.locks import file_lock
from .repositories import Repositories
from .util import canonical, fingerprint, safe_id, utc_now


class Tasks:
    def __init__(self, store):
        self.store = store
        self.repositories = Repositories(store)

    def revision(self, task_id: str, revision: int) -> dict:
        safe_id(task_id)
        require(type(revision) is int and revision >= 0, "INVALID_REQUEST", "Invalid revision number")
        return self.store.one("SELECT * FROM revisions WHERE task_id=? AND number=?", (task_id, revision))

    def get(self, task_id: str) -> dict:
        safe_id(task_id)
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

    def list(self, *, limit: int = 25, offset: int = 0) -> dict:
        require(1 <= limit <= 200 and offset >= 0, "INVALID_REQUEST", "Invalid task page")
        rows = self.store.rows("SELECT id,repository_id,goal,current_commit,current_revision,status,updated FROM tasks ORDER BY created,id LIMIT ? OFFSET ?", (limit + 1, offset))
        return {"tasks": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    @mutation
    def create(self, repository: str, goal: str, *, base: str = "HEAD", key: str, dependencies: list[dict] | None = None) -> dict:
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
        op, _ = self.store.begin_operation(kind="task.create", task_id=repo["id"], key=key,
                                           fingerprint=fingerprint(request), payload=payload)
        task_id = "task_" + op["id"][3:]
        with file_lock(self.store.home / ("locks/task-" + task_id)):
            if self.store.one("SELECT 1 FROM tasks WHERE id=?", (task_id,), required=False):
                return self.get(task_id)
            # A replay uses its original captured commit and policy, not a newer HEAD.
            retained = json.loads(op["payload"])
            with file_lock(self.store.home / ("locks/git-" + repo["id"])):
                git.run(["update-ref", "refs/author/tasks/" + task_id + "/origin", retained["commit"]])
            now = op["created"]
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,0,?,?,'waiting_for_package',?,?,?)",
                             (task_id, repo["id"], goal, retained["commit"], retained["commit"],
                              canonical(retained["policy"]).decode(), fingerprint(retained["policy"]),
                              canonical(dependencies).decode(), now, now))
                conn.execute("INSERT INTO revisions VALUES(?,0,NULL,?,?,?,NULL,NULL,NULL,?)",
                             (task_id, retained["commit"], retained["commit"], retained["tree"], now))
                conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                             (canonical({"task_id": task_id}).decode(), utc_now(), op["id"]))
                self.store.event(conn, "task.created", task_id, {"revision": 0, "commit": retained["commit"]})
            return self.get(task_id)
