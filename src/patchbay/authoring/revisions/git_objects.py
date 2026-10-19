"""Exact Git object operations on author-owned repositories, never the user's index."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import AuthorError, require
from ..util import safe_path


def git_environment(home: Path | None = None) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home or Path.home()),
            "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


def invoke(root: Path | None, args: list[str], *, data: bytes | None = None, env: dict | None = None,
           check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    command = ["git"] + (["-C", str(root)] if root else []) + args
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**git_environment(root), **(env or {})}, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthorError("GIT_ERROR", "Git operation could not complete", operation=args[0] if args else "git") from exc
    if check and result.returncode:
        raise AuthorError("GIT_ERROR", "Git rejected the requested operation", operation=args[0],
                          exit_code=result.returncode, stderr=result.stderr.decode("utf-8", "replace")[-4000:])
    return result


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
        require(re.fullmatch(r"[0-9a-f]{" + str(self.oid_length) + r"}", oid), "INVALID_REQUEST", "Invalid Git blob identity")
        size = int(self.text(["cat-file", "-s", oid]))
        require(size <= limit, "RESOURCE_LIMIT", "Git object exceeds the content budget")
        return self.run(["cat-file", "blob", oid])

    def read_file(self, commit: str, path: str) -> bytes:
        entry = self.entries(commit).get(safe_path(path))
        require(entry is not None, "NOT_FOUND", "File is absent from the pinned revision", path=path)
        require(entry["mode"] in {"100644", "100755"}, "UNSUPPORTED_FILE", "Pinned object is not a supported regular file", path=path)
        return self.blob(entry["oid"])

    def create_candidate(self, parent: str, replacements: dict, *, operation_id: str, created: float,
                         ref: str, staging: Path) -> tuple[str, str]:
        parent = self.resolve(parent)
        require(ref.startswith("refs/author/") and not any(c.isspace() for c in ref), "INVALID_REQUEST", "Candidate refs must stay in the author namespace")
        staging.mkdir(parents=True, exist_ok=True)
        descriptor, index_name = tempfile.mkstemp(prefix="index-", dir=staging)
        os.close(descriptor)
        Path(index_name).unlink()
        env = {"GIT_INDEX_FILE": index_name}
        try:
            self.run(["read-tree", parent], env=env)
            updates = []
            for path, replacement in sorted(replacements.items()):
                safe_path(path)
                if replacement is None:
                    updates.append(("0 " + "0" * self.oid_length + "\t" + path).encode() + b"\0")
                else:
                    content, mode = replacement
                    require(mode in {"100644", "100755"}, "UNSUPPORTED_FILE", "Unsupported candidate mode")
                    oid = self.text(["hash-object", "-w", "--stdin"], data=content)
                    updates.append((mode + " " + oid + "\t" + path).encode() + b"\0")
            if updates:
                self.run(["update-index", "-z", "--index-info"], data=b"".join(updates), env=env)
            tree = self.text(["write-tree"], env=env)
            identity = {"GIT_AUTHOR_NAME": "PatchBay Author", "GIT_AUTHOR_EMAIL": "patchbay-author@localhost",
                        "GIT_COMMITTER_NAME": "PatchBay Author", "GIT_COMMITTER_EMAIL": "patchbay-author@localhost",
                        "GIT_AUTHOR_DATE": f"{int(created)} +0000", "GIT_COMMITTER_DATE": f"{int(created)} +0000"}
            commit = self.text(["commit-tree", tree, "-p", parent], data=("Author candidate " + operation_id + "\n").encode(), env=identity)
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
        invoke(destination, ["config", "--remove-section", "remote.origin"], check=False)
        invoke(destination, ["config", "core.autocrlf", "false"])
        invoke(destination, ["config", "core.filemode", "true"])
        invoke(destination, ["checkout", "--detach", "--force", commit])
        require(Git(destination).resolve("HEAD") == commit, "GIT_ERROR", "Execution checkout is not the pinned candidate")

    def diff(self, base: str, commit: str, *, path: str | None = None, limit: int = 512000) -> dict:
        args = ["diff", "--no-ext-diff", "--no-textconv", "--binary", self.resolve(base), self.resolve(commit), "--"]
        if path:
            args.append(safe_path(path))
        raw = self.run(args)
        return {"diff": raw[:limit].decode("utf-8", "replace"), "truncated": len(raw) > limit, "bytes": len(raw)}
