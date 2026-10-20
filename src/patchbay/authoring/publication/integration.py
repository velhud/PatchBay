"""Conservative deterministic integration creates a new candidate requiring fresh checks."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..errors import AuthorError, require
from ..feedback.export import Feedback
from ..packages.diff import from_commit
from ..persistence.gates import mutation
from ..persistence.locks import file_lock
from ..repositories import Repositories
from ..revisions.leases import Leases
from ..revisions.materialize import Materializer
from ..tasks import Tasks
from ..util import canonical, fingerprint, utc_now


class Integration:
    def __init__(self, store):
        self.store = store
        self.repositories, self.tasks, self.leases = Repositories(store), Tasks(store), Leases(store)

    @mutation
    def create(self, repository: str, sources: list[dict], *, base: str, goal: str, key: str, label: str) -> dict:
        require(isinstance(sources, list) and 1 <= len(sources) <= 100, "INVALID_REQUEST", "Integration needs one to 100 exact source revisions")
        repo = self.repositories.get(repository)
        git = self.repositories.git(repo["id"])
        resolved_base = git.resolve(base)
        request = {"repository_id": repo["id"], "sources": sources, "base": base, "goal": goal}
        op, _ = self.store.begin_operation(kind="integration.create", task_id=repo["id"], key=key,
                                           fingerprint=fingerprint(request), payload={**request, "resolved_base": resolved_base})
        with file_lock(self.store.home / ("locks/integration-" + repo["id"])):
            observed = self.store.operation(op["id"])
            if observed["state"] == "completed":
                result = observed["result"]
                retained = json.loads(self.store.one("SELECT payload FROM operations WHERE id=?", (op["id"],))["payload"])
                result["lease"] = retained["lease"]
                return result
            require(observed["state"] not in {"failed", "blocked"}, "OPERATION_FAILED", "Integration was previously rejected")
            payload = json.loads(self.store.one("SELECT payload FROM operations WHERE id=?", (op["id"],))["payload"])
            resolved_base = payload["resolved_base"]
            for source in sources:
                require(set(source) == {"task_id", "revision"}, "INVALID_REQUEST", "Source dependencies must identify task and exact revision")
                task = self.tasks.get(source["task_id"])
                require(task["repository_id"] == repo["id"], "INVALID_REQUEST", "Cross-repository changes need a release manifest, not an atomic integration")
                require(Feedback(self.store).get(source["task_id"], revision=source["revision"])["classification"] == "READY_FOR_REVIEW",
                        "CHECKS_INCOMPLETE", "An integration source lacks passing canonical checks")
            task = self.tasks.create(repo["id"], goal, base=resolved_base, key="integration-" + op["id"], dependencies=sources)
            lease = payload.get("lease")
            lease = self.leases.claim(task["id"], label=label, token=lease.get("token") if lease else None)
            payload.update(task_id=task["id"], lease=lease)
            with self.store.transaction() as conn:
                conn.execute("UPDATE operations SET payload=?,state='running',phase='combining',updated=? WHERE id=?", (canonical(payload).decode(), utc_now(), op["id"]))
            child_key = "integration-package-" + op["id"]
            child = self.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit' AND idempotency_key=?",
                                   (task["id"], child_key), required=False)
            if child and child["state"] == "completed":
                accepted = json.loads(child["result"])
            else:
                base_entries = git.entries(resolved_base)
                combined = dict(base_entries)
                for source in sources:
                    source_task = self.tasks.get(source["task_id"])
                    revision = self.tasks.revision(source["task_id"], source["revision"])
                    original = git.entries(source_task["origin_commit"])
                    target = git.entries(revision["commit_oid"])
                    for path in sorted(set(original) | set(target)):
                        before, after, current = original.get(path), target.get(path), combined.get(path)
                        if before == after:
                            continue
                        require(current == before or current == after, "INTEGRATION_CONFLICT",
                                "Independent changes conflict at this file; Pro must author the combined correction", path=path)
                        if after is None:
                            combined.pop(path, None)
                        else:
                            require(after["mode"] in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported integration file mode", path=path)
                            combined[path] = after
                replacements = {}
                for path in sorted(set(base_entries) | set(combined)):
                    if base_entries.get(path) != combined.get(path):
                        entry = combined.get(path)
                        replacements[path] = None if entry is None else (git.blob(entry["oid"]), entry["mode"])
                require(replacements, "NO_CHANGES", "Selected sources add no new integration changes")
                with file_lock(self.store.home / ("locks/git-" + repo["id"])):
                    target_commit, _ = git.create_candidate(resolved_base, replacements, operation_id="integration-" + op["id"],
                                                           created=op["created"], ref="refs/author/integration/" + op["id"], staging=self.store.home / "staging")
                with tempfile.TemporaryDirectory(prefix="integration-", dir=self.store.home / "staging") as temporary:
                    archive = Path(temporary) / "integration.zip"
                    from_commit(self.store, task["id"], revision=0, target_commit=target_commit, output=archive, summary=goal)
                    accepted = Materializer(self.store).submit(task["id"], archive, key=child_key, token=lease["token"], epoch=lease["epoch"])
            result = {"task_id": task["id"], "revision": accepted["revision"], "commit": accepted["commit"],
                      "tree": accepted["tree"], "classification": "INTEGRATION_NEEDS_CHECKS", "sources": sources}
            self.store.finish(op["id"], "completed", result=result)
            return {**result, "lease": lease}
