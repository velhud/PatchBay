"""Exact Git object operations on author-owned repositories, never the user's index."""
from __future__ import annotations

import os
import math
import re
import subprocess
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from ..errors import AuthorError, require
from ..util import safe_path


def git_environment(home: Path | None = None) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home or Path.home()),
            "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


def invoke(root: Path | None, args: list[str], *, data: bytes | None = None, env: dict | None = None,
           check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    if args and args[0] in {"clone", "fetch", "update-ref", "hash-object", "write-tree", "commit-tree"}:
        ensure_durable_git()
    command = ["git", "-c", "core.fsync=committed", "-c", "core.fsyncMethod=fsync"] + (["-C", str(root)] if root else []) + args
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**git_environment(root), **(env or {})}, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthorError("GIT_ERROR", "Git operation could not complete", operation=args[0] if args else "git",
                          failure_kind='timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'io') from exc
    if check and result.returncode:
        raise AuthorError("GIT_ERROR", "Git rejected the requested operation", operation=args[0],
                          exit_code=result.returncode, stderr=result.stderr.decode("utf-8", "replace")[-4000:])
    return result


@lru_cache(maxsize=1)
def ensure_durable_git() -> None:
    """Immutable installed Git must support acknowledged source/ref fsync."""
    version = invoke(None, ["version"]).stdout.decode("ascii", "replace")
    match = re.search(r"git version (\d+)\.(\d+)", version)
    require(match and tuple(map(int, match.groups())) >= (2, 36), "BLOCKED_ENVIRONMENT",
            "Durable Author source writes require Git 2.36 or newer")


class Git:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.object_format = self.text(["rev-parse", "--show-object-format"])
        require(self.object_format in {"sha1", "sha256"}, "UNSUPPORTED_REPOSITORY", "Unsupported Git object format")
        self.oid_length = 40 if self.object_format == "sha1" else 64

    def run(self, args: list[str], *, data: bytes | None = None, env: dict | None = None, check: bool = True, timeout: int = 120) -> bytes:
        return invoke(self.root, args, data=data, env=env, check=check, timeout=timeout).stdout

    def text(self, args: list[str], **kwargs) -> str:
        return self.run(args, **kwargs).decode("utf-8", "strict").strip()

    def resolve(self, ref: str) -> str:
        require(isinstance(ref, str) and ref and not ref.startswith("-") and "\x00" not in ref and len(ref) <= 500,
                "INVALID_REQUEST", "Invalid Git reference")
        oid = self.text(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"])
        require(re.fullmatch(r"[0-9a-f]{" + str(self.oid_length) + r"}", oid), "GIT_ERROR", "Git returned an invalid commit")
        return oid

    def tree(self, commit: str) -> str:
        return self.text(["rev-parse", "--verify", "--end-of-options", self.resolve(commit) + "^{tree}"])

    def entries(self, commit: str) -> dict[str, dict]:
        raw = self.run(["ls-tree", "-rz", "--full-tree", self.resolve(commit)])
        require(len(raw) <= 64 * 1024**2, "RESOURCE_LIMIT", "Git path inventory is too large")
        result = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            metadata, name = item.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            try:
                path = name.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AuthorError("UNSUPPORTED_REPOSITORY", "Repository contains non-UTF-8 path names") from exc
            safe_path(path)
            result[path] = {"mode": mode, "type": kind, "oid": oid}
        return result

    def blob(self, oid: str, *, limit: int = 128 * 1024**2) -> bytes:
        with self.open_blob(oid, limit=limit) as stream:
            return stream.read()

    @contextmanager
    def open_blob(self, oid: str, *, limit: int = 128 * 1024**2, mode: str = 'complete', timeout: float = 30):
        """Complete consumers require exact size/exit; prefix consumers declare it."""
        from .streams import GitReadStream
        require(isinstance(timeout, (int, float)) and math.isfinite(timeout) and 0 < timeout <= 120,
                'INVALID_REQUEST', 'Git read deadline must be finite and within 120 seconds')
        require(mode in {'complete', 'prefix'}, 'INVALID_REQUEST', 'Invalid Git consumption mode')
        require(re.fullmatch(r'[0-9a-f]{' + str(self.oid_length) + r'}', oid), 'INVALID_REQUEST', 'Invalid Git blob identity')
        size = int(self.text(['cat-file', '-s', oid]))
        require(size <= limit, 'RESOURCE_LIMIT', 'Git object exceeds the content budget')
        try:
            process = subprocess.Popen(['git', '-C', str(self.root), 'cat-file', 'blob', oid],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_environment(self.root))
        except OSError as exc:
            raise AuthorError('SOURCE_READ_FAILED', 'Git blob producer could not start') from exc
        stream = GitReadStream(process, expected_bytes=size, timeout=timeout)
        try:
            yield stream
            if mode == 'complete' or stream.eof:
                stream.finish()
        finally:
            stream.close()

    def read_file(self, commit: str, path: str) -> bytes:
        entry = self.entries(commit).get(safe_path(path))
        require(entry is not None, "NOT_FOUND", "File is absent from the pinned revision", path=path)
        require(entry["mode"] in {"100644", "100755"}, "UNSUPPORTED_FILE", "Pinned object is not a supported regular file", path=path)
        return self.blob(entry["oid"])

    def create_candidate(self, parent: str, replacements: dict, *, operation_id: str, created: float,
                         ref: str, staging: Path) -> tuple[str, str]:
        # Git 2.36 introduced component-based object/ref fsync. Never rely on
        # an ambient repository default before SQL can acknowledge this source.
        ensure_durable_git()
        self.run(["config", "core.fsync", "committed"])
        self.run(["config", "core.fsyncMethod", "fsync"])
        parent = self.resolve(parent)
        require(ref.startswith("refs/author/") and not any(c.isspace() for c in ref), "INVALID_REQUEST", "Candidate refs must stay in the author namespace")
        staging.mkdir(parents=True, exist_ok=True)
        descriptor, index_name = tempfile.mkstemp(prefix="index-", dir=staging)
        os.close(descriptor)
        Path(index_name).unlink()
        env = {"GIT_INDEX_FILE": index_name}
        try:
            original_entries = self.entries(parent) if replacements else {}
            for path in replacements:
                old = original_entries.get(path)
                if old and old["mode"] in {"100644", "100755"}:
                    with self.open_blob(old["oid"], mode="prefix") as stream:
                        require(not stream.read(64).startswith(b"version https://git-lfs.github.com/spec/v1\n"),
                                "UNSUPPORTED_FILE", "LFS pointer mutation requires an LFS-aware workflow", path=path)
            self.run(["read-tree", parent], env=env)
            updates = []
            for path, replacement in sorted(replacements.items()):
                safe_path(path)
                if replacement is None:
                    updates.append(("0 " + "0" * self.oid_length + "\t" + path).encode() + b"\0")
                else:
                    content, mode = replacement
                    require(mode in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported candidate mode")
                    require(not content.startswith(b"version https://git-lfs.github.com/spec/v1\n"),
                            "UNSUPPORTED_FILE", "LFS pointer mutation requires an LFS-aware workflow", path=path)
                    oid = self.text(["hash-object", "-w", "--stdin"], data=content)
                    updates.append((mode + " " + oid + "\t" + path).encode() + b"\0")
            if updates:
                self.run(["update-index", "-z", "--index-info"], data=b"".join(updates), env=env)
            tree = self.text(["write-tree"], env=env)
            if replacements:
                # Inspect both source and result attributes: changing attributes
                # in the same package cannot silently bypass LFS rejection.
                names = b"".join(path.encode() + b"\0" for path in sorted(replacements))
                for source_tree in (parent, tree):
                    self.run(["read-tree", source_tree], env=env)
                    attrs = self.run(["check-attr", "--cached", "-z", "--stdin", "filter"], data=names, env=env)
                    records = attrs.split(b"\0")
                    for index in range(0, len(records) - 2, 3):
                        require(records[index + 2] != b"lfs", "UNSUPPORTED_FILE",
                                "LFS-managed file mutation requires an LFS-aware workflow", path=records[index].decode())
            metadata = {"GIT_AUTHOR_NAME": "PatchBay Author", "GIT_AUTHOR_EMAIL": "patchbay-author@localhost",
                        "GIT_COMMITTER_NAME": "PatchBay Author", "GIT_COMMITTER_EMAIL": "patchbay-author@localhost",
                        "GIT_AUTHOR_DATE": f"{int(created)} +0000", "GIT_COMMITTER_DATE": f"{int(created)} +0000"}
            commit = self.text(["commit-tree", tree, "-p", parent], data=("Author candidate " + operation_id + "\n").encode(), env=metadata)
            existing = invoke(self.root, ["show-ref", "--verify", "--hash", ref], check=False)
            if existing.returncode == 0:
                require(existing.stdout.decode().strip() == commit, "CANDIDATE_CONFLICT", "A retained candidate ref has different content")
            else:
                self.run(["update-ref", ref, commit, "0" * self.oid_length])
            return commit, tree
        finally:
            Path(index_name).unlink(missing_ok=True)
            Path(index_name + ".lock").unlink(missing_ok=True)

    def checkout(self, commit: str, destination: Path) -> None:
        """Independent object copy and index; no hard links or shared metadata."""
        commit = self.resolve(commit)
        require(not destination.exists(), "WORKSPACE_CONFLICT", "Execution destination already exists")
        invoke(None, ["clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(self.root), str(destination)], timeout=300)
        # Internal author refs need not be among clone's default heads/tags.
        invoke(destination, ["fetch", "--no-tags", "--no-recurse-submodules", "--", str(self.root), commit], timeout=300)
        invoke(destination, ["config", "--remove-section", "remote.origin"], check=False)
        invoke(destination, ["config", "core.autocrlf", "false"])
        invoke(destination, ["config", "core.filemode", "true"])
        invoke(destination, ["checkout", "--detach", "--force", commit])
        require(Git(destination).resolve("HEAD") == commit, "GIT_ERROR", "Execution checkout is not the pinned candidate")

    def diff(self, base: str, commit: str, *, path: str | None = None, limit: int = 512000) -> dict:
        require(1 <= limit <= 5 * 1024**2, "INVALID_REQUEST", "Invalid diff byte budget")
        args = ["git", "-C", str(self.root), "diff", "--no-ext-diff", "--no-textconv", "--binary", self.resolve(base), self.resolve(commit), "--"]
        if path:
            args.append(safe_path(path))
        from .streams import GitReadStream
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_environment(self.root))
        except OSError as exc:
            raise AuthorError('SOURCE_READ_FAILED', 'Git diff producer could not start') from exc
        stream = GitReadStream(process, timeout=30)
        data, truncated, timed_out = bytearray(), False, False
        try:
            while len(data) <= limit:
                chunk = stream.read1(min(65536, limit+1-len(data)))
                if not chunk:
                    stream.finish()
                    break
                data.extend(chunk)
                if len(data) > limit:
                    truncated = True
                    break
        except AuthorError as error:
            if error.code != 'SOURCE_READ_TIMEOUT':
                raise
            timed_out = True
        finally:
            stream.close()
        return {'diff': bytes(data[:limit]).decode('utf-8', 'replace'), 'truncated': truncated or timed_out,
                'partial': truncated or timed_out, 'complete': not truncated and not timed_out,
                'returned_bytes': min(len(data), limit), 'timed_out': timed_out,
                'next_action': 'Request a narrower path or a larger bounded diff limit' if truncated or timed_out else None}
