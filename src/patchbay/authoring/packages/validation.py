"""Whole-operation validation against exact task, context, authority, and Git bytes."""
from __future__ import annotations

import fnmatch
from ..errors import require
from ..models import TaskPolicy
from ..util import digest, safe_path


def matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.endswith("/**") and path == pattern[:-3]:
        return True
    if not any(c in pattern for c in "*?["):
        return path.startswith(pattern.rstrip("/") + "/")
    return False


def writable(path: str, policy: TaskPolicy) -> None:
    safe_path(path)
    require(any(matches(path, p) for p in policy.writable_paths)
            and not any(matches(path, p) for p in policy.blocked_paths),
            "POLICY_DENIED", "Package changes a path outside its task scope", path=path)


def validate(package, *, task: dict, revision: dict, context: dict, git, read_payload) -> dict:
    manifest = package.manifest
    require(manifest.repository_id == task["repository_id"] and manifest.task_id == task["id"],
            "INVALID_PACKAGE", "Package repository/task identity mismatch")
    require(manifest.expected_revision == task["current_revision"] and manifest.expected_base_commit == task["current_commit"],
            "STALE_BASE", "Package was authored against an obsolete task candidate", current_revision=task["current_revision"], current_commit=task["current_commit"])
    require(context["task_id"] == task["id"] and context["revision"] == manifest.expected_revision
            and context["commit_oid"] == manifest.expected_base_commit and context["tree_oid"] == revision["tree_oid"],
            "STALE_BASE", "Context does not identify the expected task revision")
    require(manifest.git_object_format == git.object_format, "INVALID_PACKAGE", "Git object format mismatch")
    policy = TaskPolicy.model_validate(task["policy"])
    require(set(manifest.requested_checks) <= {check.id for check in policy.profile.checks},
            "POLICY_DENIED", "Package requests an unknown check ID")
    entries = git.entries(task["current_commit"])
    cache = {}

    def before(path, expected_sha, expected_mode):
        entry = entries.get(path)
        require(entry is not None, "STALE_BASE", "Required source file is absent", path=path)
        require(entry["mode"] in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported existing file mode", path=path)
        if path not in cache:
            cache[path] = git.blob(entry["oid"])
        require(entry["mode"] == expected_mode and digest(cache[path]) == expected_sha,
                "STALE_BASE", "Existing-file bytes or mode do not match the package", path=path)
        return cache[path]

    for dependency in manifest.read_dependencies:
        before(dependency.path, dependency.sha256, dependency.mode)
    affected = set()
    for operation in manifest.operations:
        paths = [operation.path] + ([operation.destination] if operation.destination is not None else [])
        for path in paths:
            writable(path, policy)
            require(path not in affected, "INVALID_PACKAGE", "Operation set touches a path more than once", path=path)
            require(not any(path.startswith(other + "/") or other.startswith(path + "/") for other in affected),
                    "INVALID_PACKAGE", "Operations contain ancestor/descendant conflicts", path=path)
            affected.add(path)
    changes = {}
    final_paths = set(entries)
    for operation in manifest.operations:
        path = operation.path
        if operation.op == "create":
            require(path not in entries, "STALE_BASE", "Create target already exists", path=path)
        else:
            content = before(path, operation.before_sha256, operation.before_mode)
        if operation.op in {"create", "replace"}:
            changes[path] = (read_payload(package.payloads[operation.payload]), operation.mode)
            final_paths.add(path)
        elif operation.op == "delete":
            changes[path] = None
            final_paths.remove(path)
        elif operation.op == "chmod":
            changes[path] = (content, operation.mode)
        elif operation.op == "move":
            require(operation.destination not in entries, "STALE_BASE", "Move destination already exists", path=operation.destination)
            changes[path] = None
            changes[operation.destination] = (content, operation.before_mode)
            final_paths.remove(path)
            final_paths.add(operation.destination)
    folded = {}
    for path in sorted(final_paths):
        require(path.casefold() not in folded, "INVALID_PACKAGE", "Resulting source has case-colliding paths", path=path)
        folded[path.casefold()] = path
        parts = path.split("/")
        require(not any("/".join(parts[:i]) in final_paths for i in range(1, len(parts))),
                "INVALID_PACKAGE", "Resulting source contains a file/directory collision", path=path)
    return changes
