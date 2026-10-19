"""Registered repositories have private mirrors; the operator checkout is read-only input."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from .errors import require
from .models import TaskPolicy
from .persistence.gates import mutation
from .persistence.locks import file_lock
from .persistence.storage import StorageBudget
from .revisions.git_objects import Git, invoke
from .util import canonical, fingerprint, safe_id, utc_now


class Repositories:
    def __init__(self, store):
        self.store = store

    def get(self, repository: str) -> dict:
        require(isinstance(repository, str) and 0 < len(repository) <= 200, "INVALID_REQUEST", "Repository identity is required")
        row = self.store.one("SELECT * FROM repositories WHERE id=? OR name=?", (repository, repository))
        row["policy"] = json.loads(row["policy"])
        return row

    def mirror(self, repository: str) -> Path:
        row = self.get(repository)
        return self.store.home / "repositories" / (row["id"] + ".git")

    def git(self, repository: str) -> Git:
        return Git(self.mirror(repository))

    def list(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        require(1 <= limit <= 200 and offset >= 0, "INVALID_REQUEST", "Invalid repository page")
        return self.store.rows("SELECT id,name,default_branch,object_format,created,updated FROM repositories ORDER BY created,id LIMIT ? OFFSET ?", (limit, offset))

    @mutation
    def register(self, name: str, source: Path, policy: TaskPolicy, *, remote: str | None = None,
                 default_branch: str | None = None) -> dict:
        safe_id(name)
        source = Path(source).expanduser().resolve()
        require(source.is_dir() and not source.is_relative_to(self.store.home), "INVALID_REPOSITORY", "Register a repository outside author state")
        policy = TaskPolicy.model_validate(policy)
        StorageBudget(self.store).check()
        with file_lock(self.store.home / "locks/repository-registration"):
            existing = self.store.one("SELECT * FROM repositories WHERE name=?", (name,), required=False)
            if existing:
                require(existing["source"] == str(source) and json.loads(existing["policy"]) == policy.wire()
                        and (remote is None or existing["remote"] == remote)
                        and (default_branch is None or existing["default_branch"] == default_branch),
                        "REGISTRATION_CONFLICT", "Repository name already has different configuration")
                return self.get(existing["id"])
            original = Git(source)
            original.resolve("HEAD")
            branch = default_branch or invoke(source, ["symbolic-ref", "--short", "HEAD"], check=False).stdout.decode().strip()
            require(branch and not branch.startswith("-") and invoke(source, ["check-ref-format", "--branch", branch], check=False).returncode == 0,
                    "INVALID_REPOSITORY", "An explicit default branch is required for detached source repositories")
            if remote is None:
                remote = invoke(source, ["config", "--get", "remote.origin.url"], check=False).stdout.decode().strip() or None
            require(remote is None or (isinstance(remote, str) and remote and not remote.startswith("-") and "\x00" not in remote),
                    "INVALID_REPOSITORY", "Invalid registered publication remote")
            repository_id, now = "repo_" + uuid.uuid4().hex, utc_now()
            mirror = self.store.home / "repositories" / (repository_id + ".git")
            try:
                invoke(None, ["clone", "--mirror", "--no-local", "--no-hardlinks", "--", str(source), str(mirror)], timeout=300)
                invoke(mirror, ["config", "--remove-section", "remote.origin"], check=False)
                with self.store.transaction() as conn:
                    conn.execute("INSERT INTO repositories VALUES(?,?,?,?,?,?,?,?,?)", (repository_id, name, str(source), remote,
                                 branch, original.object_format, canonical(policy.wire()).decode(), now, now))
                    self.store.event(conn, "repository.registered", "", {"repository_id": repository_id, "name": name})
            except BaseException:
                shutil.rmtree(mirror, ignore_errors=True)
                raise
            return self.get(repository_id)

    @mutation
    def refresh(self, repository: str, *, key: str, from_remote: bool = False) -> dict:
        row = self.get(repository)
        request = {"repository_id": row["id"], "from_remote": from_remote}
        op, _ = self.store.begin_operation(kind="repository.refresh", task_id=row["id"], key=key,
                                           fingerprint=fingerprint(request), payload=request)
        with file_lock(self.store.home / ("locks/git-" + row["id"])):
            observed = self.store.operation(op["id"])
            if observed["state"] == "completed":
                return observed["result"]
            require(observed["state"] not in {"failed", "blocked"}, "OPERATION_FAILED", "Refresh previously failed")
            remote = row["remote"] if from_remote else row["source"]
            require(remote is not None, "POLICY_DENIED", "No registered remote is available")
            git = self.git(row["id"])
            git.run(["fetch", "--no-recurse-submodules", "--prune", "--", remote, "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], timeout=300)
            commit = git.resolve("refs/heads/" + row["default_branch"])
            result = {"repository_id": row["id"], "commit": commit, "existing_tasks_changed": False}
            self.store.finish(op["id"], "completed", result=result)
            return result
