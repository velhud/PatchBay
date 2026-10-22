"""Explicitly selected dirty overlays become immutable, author-owned source.

No stash, reset, add or commit is run against the operator checkout. Selection
is an operator/controller decision; this module only enforces exact paths.
"""
from __future__ import annotations

import json
import os
import stat
from .authority import assert_operator

from .errors import require
from .models import TaskPolicy
from .packages.validation import writable
from .persistence.control import Control
from .persistence.gates import mutation
from .persistence.locks import file_lock
from .repositories import Repositories
from .revisions.git_objects import Git, invoke
from .util import canonical, fingerprint, safe_path, utc_now


class DirtySnapshots:
    def __init__(self, store):
        self.store = store

    @mutation
    def capture(self, repository: str, paths: list[str], *, key: str) -> dict:
        assert_operator()
        from pathlib import Path
        repositories = Repositories(self.store)
        repo = repositories.get(repository)
        require(isinstance(paths, list) and 1 <= len(paths) <= 20000, "INVALID_REQUEST", "Dirty snapshot needs explicit selected file paths")
        paths = sorted({safe_path(path) for path in paths})
        policy = TaskPolicy.model_validate(repo["policy"])
        for path in paths:
            writable(path, policy)
        request = {"repository_id": repo["id"], "paths": paths}
        source = Path(repo["source"])
        original = Git(source)
        commit = original.resolve("HEAD")
        op, _ = self.store.begin_operation(kind="source.dirty_snapshot", task_id=repo["id"], key=key,
            fingerprint=fingerprint(request), payload={**request, "base_commit": commit})
        with file_lock(self.store.home / ("locks/git-" + repo["id"])):
            current = self.store.operation(op["id"])
            if current["state"] == "completed":
                return current["result"]
            Control(self.store).assert_admission()
            base = json.loads(op["payload"])["base_commit"]
            git = repositories.git(repo["id"])
            ref = "refs/author/source/" + op["id"]
            retained = invoke(git.root, ["show-ref", "--verify", "--hash", ref], check=False)
            if retained.returncode == 0:
                snapshot = retained.stdout.decode().strip()
                require(git.text(["rev-parse", snapshot + "^"]) == base,
                        "CORRUPT_STATE", "Retained dirty snapshot has an unexpected parent")
                return self._record(op, repo["id"], paths, base, snapshot, git.tree(snapshot))
            git.run(["fetch", "--no-tags", "--no-recurse-submodules", "--", str(source), base])
            before = git.entries(base)
            replacements, total = {}, 0
            for path in paths:
                target = source / path
                require(not any(parent.is_symlink() for parent in [target, *target.parents] if parent.is_relative_to(source)),
                        "UNSUPPORTED_FILE", "Dirty snapshot paths cannot include links", path=path)
                old = before.get(path)
                require(old is None or old["mode"] in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported dirty source mode", path=path)
                if not target.exists():
                    require(old is not None, "NOT_FOUND", "Selected dirty path is neither present nor tracked", path=path)
                    replacements[path] = None
                    continue
                descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    first = os.fstat(stream.fileno())
                    require(stat.S_ISREG(first.st_mode) and first.st_size <= self.store.limits.archive.member_bytes,
                            "UNSUPPORTED_FILE", "Dirty source must be a bounded regular file", path=path)
                    data = stream.read(self.store.limits.archive.member_bytes + 1)
                    last = os.fstat(stream.fileno())
                    require((first.st_ino, first.st_size, first.st_mtime_ns, first.st_ctime_ns) ==
                            (last.st_ino, last.st_size, last.st_mtime_ns, last.st_ctime_ns) and len(data) == first.st_size,
                            "SOURCE_CHANGED", "Dirty source changed during snapshot capture", path=path)
                total += len(data)
                require(total <= self.store.limits.snapshot_bytes, "RESOURCE_LIMIT", "Dirty overlay exceeds its source budget")
                replacements[path] = (data, "100755" if first.st_mode & 0o111 else "100644")
            require(original.resolve("HEAD") == base, "SOURCE_CHANGED", "Source HEAD changed during dirty capture")
            final_paths = (set(before) - {path for path, value in replacements.items() if value is None}) | {
                path for path, value in replacements.items() if value is not None}
            require(len(final_paths) == len({path.casefold() for path in final_paths}),
                    "UNSUPPORTED_FILE", "Dirty snapshot would contain case-colliding paths")
            for path in final_paths:
                pieces = path.split("/")
                require(not any("/".join(pieces[:index]) in final_paths for index in range(1, len(pieces))),
                        "UNSUPPORTED_FILE", "Dirty snapshot would contain a file/directory collision", path=path)
            snapshot, tree = git.create_candidate(base, replacements, operation_id=op["id"], created=op["created"],
                ref=ref, staging=self.store.home / "staging")
            return self._record(op, repo["id"], paths, base, snapshot, tree)

    def _record(self, op, repository_id, paths, base, snapshot, tree):
        result = {"repository_id": repository_id, "commit": snapshot, "tree": tree, "upstream_commit": base,
                  "selected_paths": paths, "uncommitted_source": True, "source_checkout_modified": False,
                  "capture_consistency": "per_file_stable_reads_and_unchanged_head",
                  "globally_atomic": False,
                  "consistency_requirement": "Keep selected files and their parent paths quiescent during capture for a coherent coupled edit"}
        with self.store.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", ("source-snapshot:" + snapshot, canonical(result).decode()))
            conn.execute("UPDATE operations SET state='completed',phase='terminal',result=?,updated=? WHERE id=?",
                         (canonical(result).decode(), utc_now(), op["id"]))
            self.store.event(conn, "source.dirty_snapshot", "", {"repository_id": repository_id, "commit": snapshot})
        return result
