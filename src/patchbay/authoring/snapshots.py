"""Reproducible source bundles and bounded reads from immutable commit objects."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .errors import AuthorError, require
from .packages.builder import _write_zip
from .persistence.gates import mutation
from .persistence.locks import file_lock
from .persistence.requests import replayable
from .persistence.storage import StorageBudget
from .repositories import Repositories
from .tasks import Tasks
from .util import canonical, digest, fingerprint, safe_path, utc_now

AUTHOR_INSTRUCTION = """You are the implementation author. Read the pinned task context and applicable repository instructions. Modify actual source and tests in an edited copy. Do not substitute another plan or delegate implementation to Codex or another inference service. PatchBay performs deterministic validation, test execution, and authorized Git operations. Use tools/build_change_package.py to prepare a working copy and construct the final package. Do not modify source/ or calculate hashes manually. Keep the completed preparation marker beside edited/. A manually assembled full tree requires --manual-tree and explicitly treats missing included files as deletions; a sparse replacement directory is not a complete tree. Author-environment checks are not canonical executor receipts. Ask for precise additional source when required. Return the implementation ZIP, changed behavior, tests actually run, and remaining uncertainty.\n"""


class Snapshots:
    def __init__(self, store):
        self.store = store
        self.tasks, self.repositories = Tasks(store), Repositories(store)

    @mutation
    @replayable("context.export", legacy_defaults={'identity_only': False})
    def export(self, task_id: str, *, revision: int | None = None, paths: list[str] | None = None,
               identity_only: bool = False) -> dict:
        task = self.tasks.get(task_id)
        revision = task["current_revision"] if revision is None else revision
        candidate = self.tasks.revision(task_id, revision)
        selection = sorted(set(paths or []))
        require(type(identity_only) is bool and not (identity_only and selection), 'INVALID_REQUEST',
                'Identity-only contexts cannot also select source payloads')
        for path in selection:
            safe_path(path)
        identity = {"repository_id": task["repository_id"], "task_id": task_id, "revision": revision,
                    "commit": candidate["commit_oid"], "selection": selection, "policy_digest": task["policy_digest"],
                    'identity_only': identity_only, 'inventory_format_version': 2}
        context_id = "ctx_" + fingerprint(identity)[:40]
        with file_lock(self.store.home / ("locks/context-" + context_id)):
            previous = self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,), required=False)
            if previous:
                return {**previous, 'identity_only': identity_only, 'inventory_format_version': 2}
            StorageBudget(self.store).check()
            git = self.repositories.git(task["repository_id"])
            entries = git.entries(candidate["commit_oid"])
            require(len(entries) <= self.store.limits.context_inventory_paths, 'RESOURCE_LIMIT', 'Context exceeds its inventory path budget')
            for requested in selection:
                require(requested in entries or any(p.startswith(requested + "/") for p in entries), "NEEDS_CONTEXT", "Requested source does not exist", path=requested)
            files, members, total = {}, {}, 0
            for path, entry in sorted(entries.items()):
                regular = entry["mode"] in {"100644", "100755"}
                included = regular and not identity_only and (not selection or any(path == item or path.startswith(item + "/") for item in selection))
                data = git.blob(entry["oid"], limit=self.store.limits.archive.member_bytes) if included else None
                total += len(data) if included else 0
                require(total <= self.store.limits.snapshot_bytes, "RESOURCE_LIMIT", "Context exceeds the source-byte budget")
                files[path] = {**entry, "sha256": digest(data) if included else None, "bytes": len(data) if included else None, "included": included,
                               'content_metadata': 'verified' if included else 'not_materialized',
                               "reason": "included" if included else "outside_selection" if regular else "unsupported_mode"}
                if included:
                    members["source/" + path] = (data, 0o755 if entry["mode"] == "100755" else 0o644)
            require(len(members) + 6 <= self.store.limits.archive.members, 'RESOURCE_LIMIT', 'Context selected payload exceeds its member budget')
            builder = Path(__file__).parent / "packages/builder.py"
            metadata = {**identity, "context_id": context_id, "tree": candidate["tree_oid"],
                        "git_object_format": git.object_format, "format_version": 2,
                        'helper_version': 2, 'builder_sha256': digest(builder.read_bytes())}
            inventory = {"files": files, "commit": candidate["commit_oid"], "omission_is_not_absence": True,
                         'format_version': 2, 'complete': True, 'identity_only': identity_only}
            require(len(canonical(inventory)) <= self.store.limits.context_inventory_bytes,
                    'RESOURCE_LIMIT', 'Context exceeds its inventory metadata byte budget')
            instruction = AUTHOR_INSTRUCTION + "\nTask: " + task["goal"] + "\n\nPrepare:\npython tools/build_change_package.py --context . --prepare edited\n\nBuild after editing:\npython tools/build_change_package.py --context . --edited edited --output ../changes.zip\n"
            acceptance = "# Approved task policy\n\nThe package cannot change these required checks or scope.\n\n```json\n" + json.dumps(task["policy"], indent=2) + "\n```\n"
            members.update({"TASK.md": (instruction.encode(), 0o644), "CONTEXT.json": (canonical(metadata), 0o644),
                            "INVENTORY.json": (canonical(inventory), 0o644), "ACCEPTANCE.md": (acceptance.encode(), 0o644),
                            "tools/build_change_package.py": (builder.read_bytes(), 0o755),
                            "tools/PACKAGING.md": (b"Keep source/ unchanged. Prepare a separate edited/ tree with the included helper. Keep the completed preparation marker beside edited/. Version 2 presence inventory includes omitted paths with null byte/hash metadata; omission is not absence. Only included files can be deleted or replaced. Request a supplemental pinned context for omitted files. --manual-tree explicitly acknowledges a complete manual tree including missing-file deletions; it is not sparse-edit mode. Output the change ZIP outside edited/.\n", 0o644)})
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
            return {**self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,)),
                    'identity_only': identity_only, 'inventory_format_version': 2}

    def read(self, context_id: str, path: str, *, start_line: int = 1, max_lines: int = 200, max_bytes: int = 65536,
             byte_offset: int | None = None) -> dict:
        from .source_windows import read_window, bind_source_retrieval
        context = self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,))
        task = self.tasks.get(context["task_id"])
        try:
            result = read_window(self.repositories.git(task['repository_id']), context['commit_oid'], path,
                                 start_line=start_line, max_lines=max_lines, max_bytes=max_bytes, byte_offset=byte_offset)
        except AuthorError as error:
            bind_source_retrieval(error.details, {'context_id': context_id})
            raise
        return {'context_id': context_id, **bind_source_retrieval(result, {'context_id': context_id})}

    def search(self, context_id: str, query: str, *, limit: int = 50, offset: int = 0) -> dict:
        from .source_windows import lines
        require(isinstance(query, str) and 0 < len(query) <= 1000 and 1 <= limit <= 200 and type(offset) is int and offset >= 0,
                "INVALID_REQUEST", "Invalid literal search request")
        context = self.store.one("SELECT * FROM contexts WHERE id=?", (context_id,))
        task = self.tasks.get(context["task_id"])
        git = self.repositories.git(task["repository_id"])
        entries = sorted(git.entries(context["commit_oid"]).items())
        # Offset is an opaque monotonic (file, source-line) coordinate. Keeping it
        # numeric preserves old clients while allowing continuation inside a file.
        stride = 2**32
        first, after_line = divmod(offset, stride)
        require(first <= len(entries), "INVALID_REQUEST", "Search cursor is outside the pinned inventory")
        matches, scanned, scanned_bytes, next_offset = [], 0, 0, None
        for position in range(first, len(entries)):
            if scanned and (scanned_bytes >= 16 * 1024**2 or scanned >= 500):
                next_offset = position * stride
                break
            path, entry = entries[position]
            scanned += 1
            if entry["mode"] not in {"100644", "100755"}:
                continue
            file_matches = []
            try:
                with git.open_blob(entry["oid"], mode="prefix") as stream:
                    for number, preview, size, found in lines(stream, preview_bytes=2000, query=query):
                        scanned_bytes += size
                        if position == first and number <= after_line:
                            continue
                        if found:
                            file_matches.append({"path": path, "line": number, "text": preview.rstrip("\r\n"),
                                                 "truncated": size > len(preview.encode("utf-8"))})
                            if len(matches) + len(file_matches) >= limit:
                                next_offset = position * stride + number
                                break
                matches.extend(file_matches)
            except UnicodeDecodeError:
                # No partial results from a file falsely represented as UTF-8.
                continue
            if next_offset is not None:
                break
        return {"context_id": context_id, "commit": context["commit_oid"], "matches": matches,
                "scanned_files": scanned, "scanned_bytes": scanned_bytes,
                "partial": next_offset is not None, "next_offset": next_offset,
                "cursor_contract": "pinned-file-line-v2"}
