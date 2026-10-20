"""Source identity checks use controller-owned hashes, not candidate-written Git status."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from pathlib import Path

from ..errors import require
from ..revisions.git_objects import Git
from ..util import digest


def expected_source(git: Git, commit: str) -> dict[str, dict]:
    result = {}
    for path, entry in git.entries(commit).items():
        require(entry["mode"] in {"100644", "100755"}, "BLOCKED_ENVIRONMENT",
                "Execution of symlink/submodule source is not qualified in this profile", path=path)
        result[path] = {"sha256": digest(git.blob(entry["oid"])), "mode": entry["mode"]}
    return result


def regular_file(workspace: Path, path: str):
    """Never follow candidate-created parent symlinks into controller files."""
    parts = path.split("/")
    current = workspace
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return None
    target = current / parts[-1]
    if target.is_symlink() or not target.is_file():
        return None
    return target


def source_changes(workspace: Path, expected: dict, allowed_outputs: list[str], *, commit: str) -> list[dict]:
    changes = []
    for path, entry in sorted(expected.items()):
        try:
            target = regular_file(workspace, path)
            if target is None:
                changes.append({"path": path, "reason": "tracked_source_changed"})
                continue
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            h = hashlib.sha256()
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Not a regular file")
                for chunk in iter(lambda: stream.read(65536), b""):
                    h.update(chunk)
            mode = "100755" if info.st_mode & 0o111 else "100644"
            if h.hexdigest() != entry["sha256"] or mode != entry["mode"]:
                changes.append({"path": path, "reason": "tracked_source_changed"})
        except OSError:
            changes.append({"path": path, "reason": "tracked_source_changed"})
    def ignored(path: str) -> bool:
        parts = path.split("/")
        return any(fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/")
                   or ("/" not in pattern and pattern in parts) for pattern in allowed_outputs)
    count = 0
    for directory, dirs, files in os.walk(workspace, followlinks=False):
        for name in list(dirs):
            target = Path(directory) / name
            path = target.relative_to(workspace).as_posix()
            if path == ".git" or ignored(path):
                dirs.remove(name)
            elif target.is_symlink():
                changes.append({"path": path, "reason": "unexpected_source_path"})
                dirs.remove(name)
        for name in files:
            count += 1
            require(count <= 1000000, "RESOURCE_LIMIT", "Post-check source inventory exceeded its bound")
            path = (Path(directory) / name).relative_to(workspace).as_posix()
            if path not in expected and not ignored(path):
                changes.append({"path": path, "reason": "unexpected_source_path"})
        require(len(changes) <= 20000, "RESOURCE_LIMIT", "Too many source changes to qualify")
    # Checkout is deliberately detached. Do not run Git against candidate-mutated
    # configuration after tests: the raw HEAD must still be that exact commit.
    try:
        head = regular_file(workspace, ".git/HEAD")
        if head is None or head.stat().st_size > 256 or head.read_bytes().strip() != commit.encode():
            changes.append({"path": ".git/HEAD", "reason": "candidate_identity_changed"})
    except OSError:
        changes.append({"path": ".git/HEAD", "reason": "candidate_identity_unavailable"})
    return changes
