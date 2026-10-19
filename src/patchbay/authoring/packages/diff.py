"""Normalize immutable draft/integration trees to the ordinary package contract."""
from __future__ import annotations

from pathlib import Path
from ..errors import require
from ..repositories import Repositories
from ..snapshots import Snapshots
from ..tasks import Tasks
from ..util import canonical, digest
from .builder import _write_zip


def from_commit(store, task_id: str, *, revision: int, target_commit: str, output: Path, summary: str = "") -> dict:
    task = Tasks(store).get(task_id)
    base = Tasks(store).revision(task_id, revision)
    context = Snapshots(store).export(task_id, revision=revision)
    git = Repositories(store).git(task["repository_id"])
    original, target = git.entries(base["commit_oid"]), git.entries(target_commit)
    operations, members = [], {}
    for path in sorted(set(original) | set(target)):
        before, after = original.get(path), target.get(path)
        if before == after:
            continue
        require((before is None or before["mode"] in {"100644", "100755"})
                and (after is None or after["mode"] in {"100644", "100755"}),
                "UNSUPPORTED_FILE", "Internal adapters cannot change unsupported file modes", path=path)
        old = git.blob(before["oid"]) if before else None
        new = git.blob(after["oid"]) if after else None
        operation = {"op": "delete" if after is None else "create" if before is None else "chmod" if old == new else "replace", "path": path}
        if before:
            operation.update(before_sha256=digest(old), before_mode=before["mode"])
        if after:
            operation["mode"] = after["mode"]
            if operation["op"] != "chmod":
                sha = digest(new)
                payload = "payload/" + sha
                members[payload] = (new, 0o644)
                operation.update(payload=payload, after_sha256=sha)
        operations.append(operation)
    require(operations, "NO_CHANGES", "The proposed tree has no changes to accept")
    manifest = {"format_version": 1, "repository_id": task["repository_id"], "task_id": task_id,
                "context_id": context["id"], "expected_revision": revision, "expected_base_commit": base["commit_oid"],
                "git_object_format": git.object_format, "submission_id": "tree-" + target_commit,
                "operations": operations, "read_dependencies": [], "requested_checks": [],
                "summary": summary, "author_notes": []}
    members.update({"manifest.json": (canonical(manifest), 0o644), "REVIEW.md": (summary.encode(), 0o644)})
    _write_zip(output, members)
    return manifest
