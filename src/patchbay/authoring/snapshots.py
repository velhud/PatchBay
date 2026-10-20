"""Reproducible source bundles and bounded reads from immutable commit objects."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .errors import require
from .packages.builder import _write_zip
from .persistence.gates import mutation
from .persistence.locks import file_lock
from .persistence.storage import StorageBudget
from .repositories import Repositories
from .tasks import Tasks
from .util import canonical, digest, fingerprint, safe_path, utc_now

AUTHOR_INSTRUCTION = """You are the implementation author. Read the pinned task context and applicable repository instructions. Modify actual source and tests in an edited copy. Do not substitute another plan or delegate implementation to Codex or another inference service. PatchBay performs deterministic validation, test execution, and authorized Git operations. Use tools/build_change_package.py to prepare a working copy and construct the final package. Do not modify source/ or calculate hashes manually. Author-environment checks are not canonical executor receipts. Ask for precise additional source when required. Return the implementation ZIP, changed behavior, tests actually run, and remaining uncertainty.\n"""


class Snapshots:
    def __init__(self, store):
        self.store = store
        self.tasks, self.repositories = Tasks(store), Repositories(store)

    @mutation
    def export(self, task_id: str, *, revision: int | None = None, paths: list[str] | None = None) -> dict:
        task = self.tasks.get(task_id)
        revision = task["current_revision"] if revision is None else revision
        candidate = self.tasks.revision(task_id, revision)
        selection = sorted(set(paths or []))
        for path in selection:
            safe_path(path)
        identity = {"repository_id": task["repository_id"], "task_id": task_id, "revision": revision,
                    "commit": candidate["commit_oid"], "selection": selection, "policy_digest": task["policy_digest"]}
        context_id = "ctx_" + fingerprint(identity)[:40]
        with file_lock(self.store.home / ("locks/context-" + context_id)):
            previous = self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,), required=False)
            if previous:
                return previous
            StorageBudget(self.store).check()
            git = self.repositories.git(task["repository_id"])
            entries = git.entries(candidate["commit_oid"])
            require(len(entries) <= self.store.limits.archive.members - 6, "RESOURCE_LIMIT", "Context inventory exceeds the member budget")
            for requested in selection:
                require(requested in entries or any(p.startswith(requested + "/") for p in entries), "NEEDS_CONTEXT", "Requested source does not exist", path=requested)
            files, members, total = {}, {}, 0
            for path, entry in sorted(entries.items()):
                regular = entry["mode"] in {"100644", "100755"}
                included = regular and (not selection or any(path == item or path.startswith(item + "/") for item in selection))
                data = git.blob(entry["oid"], limit=self.store.limits.archive.member_bytes) if entry["type"] == "blob" else b""
                total += len(data) if included else 0
                require(total <= self.store.limits.snapshot_bytes, "RESOURCE_LIMIT", "Context exceeds the source-byte budget")
                files[path] = {**entry, "sha256": digest(data), "bytes": len(data), "included": included,
                               "reason": "included" if included else "outside_selection" if regular else "unsupported_mode"}
                if included:
                    members["source/" + path] = (data, 0o755 if entry["mode"] == "100755" else 0o644)
            metadata = {**identity, "context_id": context_id, "tree": candidate["tree_oid"],
                        "git_object_format": git.object_format, "format_version": 1}
            inventory = {"files": files, "commit": candidate["commit_oid"], "omission_is_not_absence": True}
            instruction = AUTHOR_INSTRUCTION + "\nTask: " + task["goal"] + "\n\nPrepare:\npython tools/build_change_package.py --context . --prepare edited\n\nBuild after editing:\npython tools/build_change_package.py --context . --edited edited --output ../changes.zip\n"
            acceptance = "# Approved task policy\n\nThe package cannot change these required checks or scope.\n\n```json\n" + json.dumps(task["policy"], indent=2) + "\n```\n"
            builder = Path(__file__).parent / "packages/builder.py"
            members.update({"TASK.md": (instruction.encode(), 0o644), "CONTEXT.json": (canonical(metadata), 0o644),
                            "INVENTORY.json": (canonical(inventory), 0o644), "ACCEPTANCE.md": (acceptance.encode(), 0o644),
                            "tools/build_change_package.py": (builder.read_bytes(), 0o755),
                            "tools/PACKAGING.md": (b"Keep source/ unchanged. Prepare a separate edited/ tree with the included helper. Only included files can be deleted or replaced. Request a supplemental pinned context for omitted files. Output the change ZIP outside edited/.\n", 0o644)})
            with tempfile.TemporaryDirectory(prefix="context-", dir=self.store.home / "staging") as temporary:
                archive = Path(temporary) / "context.zip"
                _write_zip(archive, members)
                bundle_sha = self.store.blobs.put_file(archive, limit=self.store.limits.archive.expanded_bytes)
            inventory_sha = self.store.blobs.put(canonical(inventory))
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO contexts VALUES(?,?,?,?,?,?,?,?,?)", (context_id, task_id, revision,
                             candidate["commit_oid"], candidate["tree_oid"], bundle_sha, inventory_sha, canonical(selection).decode(), utc_now()))
                self.store.reference(conn, "context:" + context_id, bundle_sha, inventory_sha)
                self.store.event(conn, "context.exported", task_id, {"context_id": context_id, "revision": revision})
            return self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,))

    def read(self, context_id: str, path: str, *, start_line: int = 1, max_lines: int = 200, max_bytes: int = 65536) -> dict:
        require(start_line >= 1 and 1 <= max_lines <= 1000 and 1 <= max_bytes <= 262144, "INVALID_REQUEST", "Invalid file window")
        context = self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,))
        task = self.tasks.get(context["task_id"])
        data = self.repositories.git(task["repository_id"]).read_file(context["commit_oid"], safe_path(path))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"context_id": context_id, "path": path, "binary": True, "sha256": digest(data), "bytes": len(data), "next_action": "Use the pinned context archive for binary bytes"}
        lines = text.splitlines(keepends=True)
        selected, size, position = [], 0, start_line - 1
        while position < len(lines) and len(selected) < max_lines:
            line = lines[position]
            if size + len(line.encode()) > max_bytes:
                require(selected, "RESOURCE_LIMIT", "One source line exceeds the response budget; use the context archive")
                break
            selected.append(line)
            size += len(line.encode())
            position += 1
        return {"context_id": context_id, "commit": context["commit_oid"], "path": path, "sha256": digest(data),
                "start_line": start_line, "text": "".join(selected), "total_lines": len(lines),
                "next_start_line": position + 1 if position < len(lines) else None}

    def search(self, context_id: str, query: str, *, limit: int = 50, offset: int = 0) -> dict:
        require(isinstance(query, str) and 0 < len(query) <= 1000 and 1 <= limit <= 200 and offset >= 0,
                "INVALID_REQUEST", "Invalid literal search request")
        context = self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,))
        task = self.tasks.get(context["task_id"])
        git = self.repositories.git(task["repository_id"])
        entries = sorted(git.entries(context["commit_oid"]).items())
        matches, scanned, scanned_bytes = [], offset, 0
        for position in range(offset, len(entries)):
            path, entry = entries[position]
            if entry["mode"] not in {"100644", "100755"}:
                scanned = position + 1
                continue
            data = git.blob(entry["oid"])
            scanned_bytes += len(data)
            if scanned_bytes > 16 * 1024**2 or position - offset >= 500:
                break
            try:
                lines = data.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                scanned = position + 1
                continue
            for number, line in enumerate(lines, 1):
                if query in line and len(matches) < limit:
                    matches.append({"path": path, "line": number, "text": line[:2000]})
            scanned = position + 1
            if len(matches) >= limit:
                break
        return {"context_id": context_id, "matches": matches, "scanned_files": scanned - offset,
                "partial": scanned < len(entries), "next_offset": scanned if scanned < len(entries) else None}
