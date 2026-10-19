"""Git-native candidate snapshots for PatchBay worker integration.

This module deliberately uses an isolated temporary index. It can describe the
entire worker workspace (committed, staged, unstaged, untracked, binary, modes,
and symlinks) without changing the worker's real index or branch.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Iterable, Mapping, Sequence


class CandidateSnapshotError(RuntimeError):
    """Raised when Git cannot construct a faithful worker candidate."""


@dataclass(frozen=True)
class CandidateSnapshot:
    baseline_revision: str
    tree_sha: str
    changed_files: tuple[str, ...]
    patch: bytes

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch).hexdigest() if self.patch else ""


def _run_git(
    root: str | Path,
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: int = 30,
    ok_returncodes: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    command = ["git", *args]
    result = subprocess.run(
        command,
        cwd=str(root),
        env=dict(env) if env is not None else None,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode not in set(ok_returncodes):
        detail = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise CandidateSnapshotError(
            f"{' '.join(command)} failed with exit code {result.returncode}: "
            f"{detail or 'no diagnostic'}"
        )
    return result


def decode_nul_paths(payload: bytes) -> list[str]:
    """Decode Git's NUL-delimited path output without C-style quoting."""
    return [
        value.decode("utf-8", errors="surrogateescape")
        for value in payload.split(b"\0")
        if value
    ]


def untracked_paths(root: str | Path) -> list[str]:
    """Return untracked, non-ignored paths using Git's unambiguous format."""
    result = _run_git(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z"],
        timeout=15,
    )
    return sorted(dict.fromkeys(decode_nul_paths(result.stdout)))


def porcelain_paths(root: str | Path) -> list[str]:
    """Return all changed paths, including both sides of a rename/copy."""
    result = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout=15,
    )
    fields = result.stdout.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field or len(field) < 3:
            continue
        status = field[:2]
        path = field[3:] if field[2:3] == b" " else field[2:]
        if path:
            paths.append(path.decode("utf-8", errors="surrogateescape"))
        if b"R" in status or b"C" in status:
            if index < len(fields) and fields[index]:
                paths.append(fields[index].decode("utf-8", errors="surrogateescape"))
            index += 1
    return sorted(dict.fromkeys(paths))


def _normalize_exclusions(paths: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for raw in paths:
        value = str(raw or "").replace("\\", "/").strip("/")
        if value and value != "." and value not in normalized:
            normalized.append(value)
    return normalized


def build_candidate_snapshot(
    root: str | Path,
    baseline_revision: str,
    *,
    excluded_paths: Iterable[str] = (),
    timeout: int = 60,
) -> CandidateSnapshot:
    """Build a complete candidate tree and binary patch from a workspace.

    Git's real index is never touched. ``git add -A`` runs against a temporary
    index initialized from ``baseline_revision``. Committed worker changes,
    index changes, working-tree changes, deletions, untracked files, executable
    bits, symlinks, binary files and missing final newlines are represented by
    Git itself.
    """
    root_path = Path(root).expanduser().resolve()
    baseline = str(baseline_revision or "").strip()
    if not baseline:
        raise CandidateSnapshotError("baseline_revision is required")

    with tempfile.TemporaryDirectory(
        prefix="patchbay-candidate-index-"
    ) as temporary:
        index_path = Path(temporary) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index_path)

        _run_git(root_path, ["read-tree", baseline], env=env, timeout=timeout)
        _run_git(root_path, ["add", "-A", "--", "."], env=env, timeout=timeout)

        for path in _normalize_exclusions(excluded_paths):
            result = subprocess.run(
                ["git", "reset", "-q", baseline, "--", path],
                cwd=str(root_path),
                env=env,
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).decode(
                    "utf-8", errors="replace"
                ).strip()
                raise CandidateSnapshotError(
                    f"git reset of excluded path {path!r} failed: "
                    f"{detail or 'no diagnostic'}"
                )

        tree_sha = _run_git(
            root_path, ["write-tree"], env=env, timeout=timeout
        ).stdout.decode().strip()
        changed = _run_git(
            root_path,
            [
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                baseline,
                tree_sha,
                "--",
            ],
            env=env,
            timeout=timeout,
        )
        patch = _run_git(
            root_path,
            [
                "diff",
                "--binary",
                "--full-index",
                "--no-renames",
                baseline,
                tree_sha,
                "--",
            ],
            env=env,
            timeout=timeout,
        ).stdout

    return CandidateSnapshot(
        baseline_revision=baseline,
        tree_sha=tree_sha,
        changed_files=tuple(
            sorted(dict.fromkeys(decode_nul_paths(changed.stdout)))
        ),
        patch=patch,
    )


def create_snapshot_commit(
    root: str | Path,
    *,
    tree_sha: str,
    parent_revision: str,
    ref_name: str,
    expected_old_revision: str = "",
    message: str = "PatchBay integrated worker snapshot",
) -> str:
    """Anchor a candidate tree behind a private ref for follow-up diffs."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "PatchBay",
            "GIT_AUTHOR_EMAIL": "patchbay@localhost.invalid",
            "GIT_COMMITTER_NAME": "PatchBay",
            "GIT_COMMITTER_EMAIL": "patchbay@localhost.invalid",
        }
    )
    args = ["commit-tree", tree_sha]
    if parent_revision:
        args.extend(["-p", parent_revision])
    commit = _run_git(
        root,
        args,
        env=env,
        input_bytes=(message.rstrip() + "\n").encode("utf-8"),
    ).stdout.decode().strip()
    update_args = ["update-ref", ref_name, commit]
    if expected_old_revision:
        update_args.append(expected_old_revision)
    _run_git(root, update_args)
    return commit


def delete_snapshot_ref(
    root: str | Path, ref_name: str, expected_revision: str = ""
) -> None:
    args = ["update-ref", "-d", ref_name]
    if expected_revision:
        args.append(expected_revision)
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True)
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise CandidateSnapshotError(
            f"git update-ref -d {ref_name!r} failed: "
            f"{detail or 'no diagnostic'}"
        )
