"""Standard-library-only package builder, also copied into each context bundle.

Usage: python tools/build_change_package.py --context . --prepare edited
       python tools/build_change_package.py --context . --edited edited --output changes.zip
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import stat
import unicodedata
import uuid
import zipfile
from pathlib import Path

BUILDER_VERSION = 2


def _context(context: Path):
    metadata = json.loads((context / 'CONTEXT.json').read_text())
    inventory = json.loads((context / 'INVENTORY.json').read_text())
    if type(metadata.get('format_version', 1)) is not int or metadata.get('format_version', 1) not in {1, 2}:
        raise ValueError('Unsupported context format version')
    if type(inventory.get('format_version', 1)) is not int or inventory.get('format_version', 1) not in {1, 2}:
        raise ValueError('Unsupported inventory format version')
    if metadata.get('format_version') == 2 and (inventory.get('format_version') != 2 or inventory.get('complete') is not True):
        raise ValueError('Version 2 contexts require a complete version 2 presence inventory')
    return metadata, inventory


def _preparation_path(edited: Path) -> Path:
    return edited.parent / ('.' + edited.name + '.patchbay-preparation.json')


def _preparation_record(metadata, inventory, state):
    return {'format_version': BUILDER_VERSION, 'context_id': metadata['context_id'],
            'inventory_sha256': _sha(_json(inventory)), 'state': state}


def _write_preparation(path: Path, record, *, initial=False):
    if initial:
        with path.open('xb') as stream:
            stream.write(_json(record)); stream.flush(); os.fsync(stream.fileno())
        return
    temporary = path.with_name(path.name + '.part-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(_json(record)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _safe(path: str) -> str:
    parts = path.split("/")
    if (not path or path.startswith("/") or "\\" in path or ":" in path or
        unicodedata.normalize("NFC", path) != path or any(ord(c) < 32 or ord(c) == 127 for c in path) or
        any(x in {"", ".", ".."} or x.casefold() in {".git", ".hg", ".svn"} or x.endswith((".", " ")) for x in parts)):
        raise ValueError(f"Unsafe package path: {path!r}")
    return path


def _regular(path: Path) -> bytes:
    if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ValueError(f"Expected a regular, non-link file: {path}")
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError(f"File exceeds the builder's 128 MiB limit: {path}")
    return path.read_bytes()


def _write_zip(path: Path, members: dict[str, tuple[bytes, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part-" + uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, (data, mode) in sorted(members.items()):
                info = zipfile.ZipInfo(_safe(name), (2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(context: Path, destination: Path) -> None:
    metadata, inventory = _context(context)
    if destination.exists():
        raise ValueError("Preparation destination must not already exist")
    source = context / "source"
    destination.parent.mkdir(parents=True, exist_ok=True)
    marker = _preparation_path(destination)
    _write_preparation(marker, _preparation_record(metadata, inventory, 'incomplete'), initial=True)
    destination.mkdir(parents=True)
    for path, entry in inventory["files"].items():
        if not entry["included"]:
            continue
        original = source / _safe(path)
        data = _regular(original)
        if _sha(data) != entry["sha256"]:
            raise ValueError(f"Original source was modified: {path}")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if entry["mode"] == "100755" else 0o644)
        if _sha(_regular(target)) != entry['sha256']:
            raise ValueError(f'Prepared source does not match its inventory: {path}')
    _write_preparation(marker, _preparation_record(metadata, inventory, 'complete'))


def build(context: Path, edited: Path, output: Path, *, requested_checks: list[str] | None = None,
          summary: str = "", submission_id: str | None = None, manual_tree: bool = False) -> dict:
    metadata, complete_inventory = _context(context)
    inventory = complete_inventory['files']
    originals = context / "source"
    if edited.resolve() == originals.resolve() or not edited.is_dir() or edited.is_symlink():
        raise ValueError("Use a separate prepared working copy, not immutable source/")
    if output.absolute().is_relative_to(edited.absolute()) or output.resolve().is_relative_to(edited.resolve()):
        raise ValueError('Write the package outside the edited source tree, including temporary output')
    marker = _preparation_path(edited)
    if not manual_tree and (metadata.get('format_version') == 2 or marker.exists() or marker.is_symlink()):
        if not marker.is_file() or marker.is_symlink():
            raise ValueError('Use a completed prepared tree; --manual-tree explicitly declares a complete manual tree, including deletions')
        if json.loads(_regular(marker)) != _preparation_record(metadata, complete_inventory, 'complete'):
            raise ValueError('Preparation is incomplete or belongs to another context')
    included = {p: e for p, e in inventory.items() if e["included"]}
    present: dict[str, Path] = {}
    excluded = {".git", "__pycache__", ".pytest_cache", ".author-reports"}
    for directory, dirs, files in os.walk(edited, followlinks=False):
        for name in list(dirs):
            if (Path(directory) / name).is_symlink():
                raise ValueError("Symlink directories are unsupported")
            prefix = (Path(directory) / name).relative_to(edited).as_posix() + "/"
            # Runtime caches are ignorable only when they are not pinned source.
            # Pruning an inventoried directory would turn unchanged tracked bytes
            # into implicit delete operations below.
            if name in excluded and not any(p.startswith(prefix) for p in inventory):
                dirs.remove(name)
        for name in files:
            candidate = Path(directory) / name
            path = _safe(candidate.relative_to(edited).as_posix())
            if any(part in excluded for part in path.split("/")[:-1]) and path not in inventory:
                continue
            if candidate.resolve() == output.resolve():
                raise ValueError("Write the package outside the edited source tree")
            if candidate.is_symlink():
                raise ValueError(f"Symlinks are unsupported: {path}")
            present[path] = candidate
    operations, members, diff_parts = [], {}, []
    for path in sorted(set(included) | set(present)):
        before = included.get(path)
        after_path = present.get(path)
        if path in inventory and before is None:
            raise ValueError(f"File existed but was omitted from this context: {path}; request additional context")
        old = _regular(originals / path) if before else None
        if before and _sha(old) != before["sha256"]:
            raise ValueError(f"Original source was modified: {path}")
        if after_path is None:
            operations.append({"op": "delete", "path": path, "before_sha256": before["sha256"],
                               "before_mode": before["mode"]})
            new = b""
        else:
            new = _regular(after_path)
            mode = "100755" if after_path.stat().st_mode & 0o111 else "100644"
            sha = _sha(new)
            if before and new == old and mode == before["mode"]:
                continue
            if before and new == old:
                operations.append({"op": "chmod", "path": path, "before_sha256": before["sha256"],
                                   "before_mode": before["mode"], "mode": mode})
            else:
                payload = "payload/" + sha
                members[payload] = (new, 0o644)
                operation = {"op": "replace" if before else "create", "path": path, "payload": payload,
                             "after_sha256": sha, "mode": mode}
                if before:
                    operation.update(before_sha256=before["sha256"], before_mode=before["mode"])
                operations.append(operation)
        if len(old or b"") + len(new) <= 100000:
            try:
                diff_parts.extend(difflib.unified_diff((old or b"").decode().splitlines(True), new.decode().splitlines(True),
                                                     fromfile="a/" + path, tofile="b/" + path))
            except UnicodeDecodeError:
                diff_parts.append(f"Binary change: {path}\n")
    if not operations:
        raise ValueError("No source or mode changes were found")
    manifest = {"format_version": 1, "repository_id": metadata["repository_id"], "task_id": metadata["task_id"],
                "submission_id": submission_id or uuid.uuid4().hex, "context_id": metadata["context_id"],
                "expected_revision": metadata["revision"], "expected_base_commit": metadata["commit"],
                "git_object_format": metadata["git_object_format"], "operations": operations,
                "read_dependencies": [{"path": p, "sha256": e["sha256"], "mode": e["mode"]}
                                      for p, e in sorted(included.items())],
                "requested_checks": requested_checks or [], "summary": summary, "author_notes": []}
    members.update({"manifest.json": (_json(manifest), 0o644), "REVIEW.md": (summary.encode(), 0o644),
                    "changes.diff": ("".join(diff_parts).encode(), 0o644)})
    _write_zip(output, members)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--edited", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--summary", default="")
    parser.add_argument('--manual-tree', action='store_true', help='Explicitly treat a complete manually assembled tree as authoritative, including missing-file deletions. Not a sparse-edits mode.')
    args = parser.parse_args(argv)
    try:
        if args.prepare:
            prepare(args.context, args.prepare)
        else:
            if not args.edited or not args.output:
                parser.error("--edited and --output are required when not preparing a working copy")
            result = build(args.context, args.edited, args.output, requested_checks=args.check, summary=args.summary,
                           manual_tree=args.manual_tree)
            print(json.dumps({"output": str(args.output), "operations": len(result["operations"])}))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"Package not built: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
