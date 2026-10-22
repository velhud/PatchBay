"""Git-native candidate snapshots for PatchBay worker integration.

This module deliberately uses an isolated temporary index. It can describe the
entire worker workspace (committed, staged, unstaged, untracked, binary, modes,
and symlinks) without changing the worker's real index or branch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
import stat
import selectors
import signal
import time
from pathlib import Path
import subprocess
import tempfile
from typing import Iterable, Mapping, Sequence


class CandidateSnapshotError(RuntimeError):
    """Raised when Git cannot construct a faithful worker candidate."""

    def __init__(self, message, *, context_paths=(), reason='source_observation_failed'):
        super().__init__(message)
        self.context_paths = list(context_paths)
        self.reason = reason


@dataclass(frozen=True)
class CandidateSnapshot:
    baseline_revision: str
    tree_sha: str
    changed_files: tuple[str, ...]
    patch: bytes
    entries: dict = field(default_factory=dict)
    object_format: str = "sha1"
    patch_complete: bool = True

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch).hexdigest() if self.patch else ""


def _run_git(
    root: str | Path, args: Sequence[str], *, env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None, timeout: int = 30,
    ok_returncodes: Iterable[int] = (0,), max_output_bytes: int = 32 * 1024**2,
    truncate_stdout: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Finite memory and deadline, complete producer exit, explicit prefixes.

    Oversized stdout is allowed only for a display/patch consumer that checks
    stdout_truncated. Identity inventories refuse rather than certify a prefix.
    """
    command = ["git", *args]
    process = None
    complete = False
    try:
        with tempfile.TemporaryFile() as source, selectors.DefaultSelector() as selector:
            if input_bytes is not None:
                source.write(input_bytes)
            source.seek(0)
            process = subprocess.Popen(command, cwd=str(root), env=dict(env) if env is not None else None,
                                       stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            parts = {"stdout": bytearray(), "stderr": bytearray()}
            truncated = False
            for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CandidateSnapshotError("Git observation exceeded its deadline")
                for key, _ in selector.select(min(.1, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    label = key.data
                    limit = max_output_bytes if label == "stdout" else 65536
                    available = max(0, limit - len(parts[label]))
                    parts[label].extend(chunk[:available])
                    if len(chunk) > available:
                        if label == "stdout" and truncate_stdout:
                            truncated = True
                        else:
                            raise CandidateSnapshotError("Git observation exceeds its output byte budget", reason="source_output_limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CandidateSnapshotError("Git observation exceeded its deadline")
            result = subprocess.CompletedProcess(command, process.wait(timeout=remaining),
                                                  bytes(parts["stdout"]), bytes(parts["stderr"]))
            result.stdout_truncated = truncated
            # wait() has reaped the leader. Never signal its numeric group ID
            # after this point: a future unrelated process could reuse it.
            complete = True
            if result.returncode not in set(ok_returncodes):
                detail = (result.stderr or result.stdout)[:4000].decode("utf-8", errors="replace").strip()
                raise CandidateSnapshotError(f"Git observation failed with exit code {result.returncode}: {detail}")
            return result
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CandidateSnapshotError("Git observation failed or exceeded its deadline") from error
    finally:
        if process is not None:
            if not complete:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


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
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"],
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
        value = str(raw or "")
        if Path(value).is_absolute() or ".." in Path(value).parts or "\0" in value:
            raise CandidateSnapshotError("Excluded context path must be a literal repository-relative path")
        if value and value != "." and value not in normalized:
            normalized.append(value)
    return normalized


def build_candidate_snapshot(
    root: str | Path,
    baseline_revision: str,
    *,
    excluded_paths: Iterable[str] = (),
    timeout: int = 60,
    include_patch: bool = True,
    max_patch_bytes: int = 2_000_000,
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

    # Observe the real index before replacing GIT_INDEX_FILE. Never refresh it.
    real_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    stages = _run_git(root_path, ["ls-files", "--stage", "-z"], env=real_env).stdout
    tracked = []
    index_entries = {}
    baseline_entries = tree_entries(root_path, baseline)
    for record in stages.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        if metadata.split()[2] != b"0":
            raise CandidateSnapshotError("Unmerged worker index is unsupported; preserve and resolve it explicitly", reason='unsupported_index_state')
        decoded = os.fsdecode(path)
        index_entries[decoded] = {'mode': metadata.split()[0].decode(), 'blob_oid': metadata.split()[1].decode()}
        if metadata.split()[0] == b'160000' and baseline_entries.get(decoded, {}).get('blob_oid') != metadata.split()[1].decode():
            raise CandidateSnapshotError(f"Unsupported changed gitlink at {decoded!r}", reason='unsupported_gitlink')
        tracked.append(path)
    flags = _run_git(root_path, ["ls-files", "-v", "-z"], env=real_env).stdout
    if any(record[:1] in {b"S", b"s"} for record in flags.split(b"\0") if record):
        raise CandidateSnapshotError("Sparse/skip-worktree worker indexes are unsupported for complete capture", reason='unsupported_index_state')
    head_entries = tree_entries(root_path, "HEAD")
    for path, entry in head_entries.items():
        if entry['mode'] == '160000' and baseline_entries.get(path) != entry:
            raise CandidateSnapshotError(f"Unsupported changed gitlink at {path!r}", reason='unsupported_gitlink')
    gitlinks = {path for entries in (baseline_entries, head_entries, index_entries) for path, entry in entries.items() if entry['mode'] == '160000'}
    if gitlinks:
        dirty_paths = set(porcelain_paths(root_path))
        for path in gitlinks:
            original = baseline_entries.get(path)
            indexed = index_entries.get(path)
            if (not original or not indexed or original['mode'] != indexed['mode'] or original['blob_oid'] != indexed['blob_oid']
                    or head_entries.get(path) != original or path in dirty_paths):
                raise CandidateSnapshotError('Unsupported changed or dirty gitlink; preserve the worker workspace', reason='unsupported_gitlink')
    tracked.extend(path.encode("utf-8", "surrogateescape") for path in head_entries)

    with tempfile.TemporaryDirectory(
        prefix="patchbay-candidate-index-"
    ) as temporary:
        index_path = Path(temporary) / "index"
        env = {**real_env, "GIT_LITERAL_PATHSPECS": "1"}
        env["GIT_INDEX_FILE"] = str(index_path)

        _run_git(root_path, ["read-tree", baseline], env=env, timeout=timeout)
        _run_git(root_path, ["add", "-A", "--", "."], env=env, timeout=timeout)
        # Force only explicitly tracked paths. Untracked ignored scratch stays
        # excluded; working-tree bytes take precedence over staged/HEAD bytes.
        present = [name for name in sorted(set(tracked))
                   if os.path.lexists(root_path / os.fsdecode(name))
                   and (not (root_path / os.fsdecode(name)).is_dir() or (root_path / os.fsdecode(name)).is_symlink())]
        if present:
            _run_git(root_path, ["add", "-A", "-f", "--pathspec-from-file=-", "--pathspec-file-nul"],
                     env=env, input_bytes=b"\0".join(present)+b"\0", timeout=timeout)

        for path in _normalize_exclusions(excluded_paths):
            _run_git(root_path, ["reset", "-q", baseline, "--", path], env=env, timeout=timeout)

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
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=none",
                baseline,
                tree_sha,
                "--",
            ],
            env=env,
            timeout=timeout,
        )
        patch_complete = False
        patch = b""
        if include_patch:
            patch_result = _run_git(
                root_path,
                [
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--ignore-submodules=none",
                    baseline,
                    tree_sha,
                    "--",
                ],
                env=env,
                timeout=timeout, max_output_bytes=max_patch_bytes, truncate_stdout=True,
            )
            patch_complete = not patch_result.stdout_truncated
            patch = patch_result.stdout if patch_complete else b""


    changed_paths = tuple(sorted(dict.fromkeys(decode_nul_paths(changed.stdout))))
    before_entries, after_entries = tree_entries(root_path, baseline), tree_entries(root_path, tree_sha)
    for path in changed_paths:
        for entry in (before_entries.get(path), after_entries.get(path)):
            if entry and entry['mode'] not in {'100644', '100755', '120000'}:
                raise CandidateSnapshotError(f"Unsupported changed Git entry at {path!r}; workspace preserved", reason='unsupported_git_entry')
    object_format = _run_git(root_path, ["rev-parse", "--show-object-format"]).stdout.decode().strip()
    expected = tree_fingerprints(after_entries, changed_paths)
    if filesystem_fingerprints(root_path, changed_paths, object_format=object_format) != expected:
        raise CandidateSnapshotError("Git conversion or concurrent source change prevents an exact materialized candidate; workspace preserved", reason='candidate_materialization_mismatch')
    return CandidateSnapshot(
        baseline_revision=baseline,
        tree_sha=tree_sha,
        changed_files=tuple(
            sorted(dict.fromkeys(decode_nul_paths(changed.stdout)))
        ),
        patch=patch,
        entries=after_entries,
        object_format=object_format,
        patch_complete=patch_complete,
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
    update_args.append(expected_old_revision or "0" * len(commit))
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


def tree_entries(root, revision):
    data = _run_git(root, ["ls-tree", "-rz", "--full-tree", revision]).stdout
    entries = {}
    for record in data.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        entries[os.fsdecode(path)] = {"mode": mode, "kind": kind, "blob_oid": oid}
    return entries


def tree_fingerprints(entries, paths):
    result = {}
    for path in paths:
        entry = entries.get(path)
        if entry:
            result[path] = {"kind": "symlink" if entry['mode']=='120000' else "file",
                            "mode": entry['mode'], "blob_oid": entry['blob_oid']}
        else:
            result[path] = {"kind": "directory" if any(p.startswith(path+'/') for p in entries) else "absent"}
    return result


def filesystem_fingerprints(root, paths, *, object_format=None):
    root = Path(root).resolve()
    object_format = object_format or _run_git(root, ["rev-parse", "--show-object-format"]).stdout.decode().strip()
    if object_format not in {'sha1', 'sha256'}:
        raise CandidateSnapshotError("Unsupported Git object format")
    result = {}
    for name in sorted(set(paths)):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise CandidateSnapshotError("Invalid affected repository path")
        target = root / relative
        if any(parent.is_symlink() for parent in target.parents if parent != root and root in parent.parents):
            raise CandidateSnapshotError("Affected path has a symlinked parent")
        try:
            info = target.lstat()
        except (FileNotFoundError, NotADirectoryError):
            result[name] = {"kind": "absent"}
            continue
        except OSError as error:
            raise CandidateSnapshotError("Affected entry cannot be observed") from error
        if stat.S_ISDIR(info.st_mode):
            result[name] = {"kind": "directory"}
            continue
        if stat.S_ISLNK(info.st_mode):
            data = os.fsencode(os.readlink(target))
            h = hashlib.new(object_format, b"blob "+str(len(data)).encode()+b"\0"+data)
            result[name] = {"kind": "symlink", "mode": "120000", "blob_oid": h.hexdigest()}
            continue
        if not stat.S_ISREG(info.st_mode):
            raise CandidateSnapshotError("Affected entry is not a supported regular file or link")
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                first = os.fstat(stream.fileno())
                h = hashlib.new(object_format, b"blob "+str(first.st_size).encode()+b"\0")
                count = 0
                for chunk in iter(lambda: stream.read(65536), b''):
                    h.update(chunk); count += len(chunk)
                last = os.fstat(stream.fileno())
            identity = lambda value: (value.st_dev,value.st_ino,value.st_mode,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
            if identity(info)!=identity(first) or identity(first)!=identity(last) or count!=first.st_size:
                raise CandidateSnapshotError("Affected file changed while being observed")
        except OSError as error:
            raise CandidateSnapshotError("Affected file cannot be read") from error
        result[name] = {"kind": "file", "mode": "100755" if info.st_mode & 0o111 else "100644", "blob_oid": h.hexdigest()}
    return result
