"""Consistent offline backups and conservative, reference-aware local cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path

from ..errors import AuthorError, require
from ..repositories import Repositories
from ..util import atomic_write, canonical, digest, fsync_dir, safe_id, safe_path, strict_json, utc_now
from .locks import file_lock
from .store import Store, TERMINAL
from .schema import SCHEMA_VERSION


class Maintenance:
    def __init__(self, store: Store):
        self.store = store

    def _quiescent(self) -> None:
        active = self.store.one("SELECT 1 FROM runs WHERE state NOT IN ('completed','failed','blocked','timed_out','cancelled') OR cleanup IN ('pending','unresolved') LIMIT 1", required=False)
        require(not active, "MAINTENANCE_BLOCKED", "Resolve or finish active/uncertain checks before state maintenance")

    def backup(self, output: Path) -> dict:
        output = Path(output).expanduser().absolute()
        require(not output.resolve().is_relative_to(self.store.home), "INVALID_REQUEST", "Backups must be outside author runtime state")
        self._quiescent()
        with file_lock(self.store.home / "locks/maintenance", blocking=False):
            self._quiescent()
            self.store.integrity()
            with tempfile.TemporaryDirectory(prefix=".author-backup-", dir=self.store.home.parent) as temporary:
                temporary = Path(temporary)
                database = temporary / "author.sqlite3"
                with self.store.connect() as source:
                    destination = sqlite3.connect(database)
                    try:
                        source.backup(destination)
                    finally:
                        destination.close()
                files = {"author.sqlite3": database}
                for name in ("identity.json", "tokens.json", "client-qualification.json"):
                    path = self.store.home / name
                    if path.exists():
                        files[name] = path
                for subtree in ("blobs", "repositories", "operator-leases"):
                    root = self.store.home / subtree
                    for directory, dirs, names in os.walk(root, followlinks=False):
                        for name in dirs:
                            require(not (Path(directory) / name).is_symlink(), "CORRUPT_STATE", "Runtime backup encountered a directory symlink")
                        for name in names:
                            path = Path(directory) / name
                            require(not path.is_symlink() and path.is_file(), "CORRUPT_STATE", "Runtime backup encountered an unsupported file")
                            # Partial orphan blobs are not referenced state.
                            if subtree == "blobs" and path.name.startswith(".blob-"):
                                continue
                            files[path.relative_to(self.store.home).as_posix()] = path
                require(len(files) <= 500000, "RESOURCE_LIMIT", "Backup inventory is too large")
                manifest = {"format_version": 1, "schema_version": SCHEMA_VERSION,
                            "instance_id": self.store.identity["instance_id"], "created": utc_now(), "files": {}}
                total = 0
                archive_path = temporary / "backup.zip"
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for name, path in sorted(files.items()):
                        safe_path(name)
                        info = path.stat()
                        total += info.st_size
                        require(total <= self.store.limits.total_storage_bytes, "RESOURCE_LIMIT", "Backup exceeds the authorized state budget")
                        h = hashlib.sha256()
                        with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as target:
                            for chunk in iter(lambda: source.read(65536), b""):
                                target.write(chunk); h.update(chunk)
                        manifest["files"][name] = {"sha256": h.hexdigest(), "bytes": info.st_size,
                                                     "mode": 0o700 if info.st_mode & 0o111 else 0o600}
                    archive.writestr("BACKUP.json", canonical(manifest))
                output.parent.mkdir(parents=True, exist_ok=True)
                require(not output.is_symlink(), "INVALID_REQUEST", "Backup destination cannot be a symlink")
                partial = output.with_name(output.name + ".part")
                require(not partial.exists(), "INVALID_REQUEST", "A partial backup already occupies the destination")
                with archive_path.open("rb") as source, partial.open("xb") as target:
                    shutil.copyfileobj(source, target, 65536)
                    target.flush(); os.fsync(target.fileno())
                partial.chmod(0o600)
                os.replace(partial, output); fsync_dir(output.parent)
                return {"output": str(output), "files": len(files), "uncompressed_bytes": total,
                        "instance_id": manifest["instance_id"], "execution_workspaces_included": False}

    @staticmethod
    def restore(archive_path: Path, destination: Path) -> Store:
        destination = Path(destination).expanduser().absolute()
        require(not destination.exists() and not destination.is_symlink(), "ALREADY_INITIALIZED", "Restore destination must be new")
        require(Path(archive_path).is_file() and not Path(archive_path).is_symlink(), "INVALID_BACKUP", "Backup must be a regular archive")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".author-restore-", dir=destination.parent))
        try:
            temporary.chmod(0o700)
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                require(len(infos) <= 500001, "INVALID_BACKUP", "Backup has too many members")
                names, folded = {}, set()
                for info in infos:
                    require(info.filename == info.orig_filename and not info.is_dir(), "INVALID_BACKUP", "Backup members must be unambiguous files")
                    name = safe_path(info.filename)
                    require(name not in names and name.casefold() not in folded and not info.flag_bits & 1,
                            "INVALID_BACKUP", "Duplicate, encrypted, or colliding backup member")
                    require(stat.S_IFMT(info.external_attr >> 16) in {0, stat.S_IFREG}, "INVALID_BACKUP", "Backup links and devices are not supported")
                    require(info.compress_type in {zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED}, "INVALID_BACKUP", "Unsupported backup compression")
                    names[name] = info; folded.add(name.casefold())
                require("BACKUP.json" in names and names["BACKUP.json"].file_size <= 128 * 1024**2,
                        "INVALID_BACKUP", "Backup manifest is missing or too large")
                manifest = strict_json(archive.read("BACKUP.json"))
                require(manifest.get("format_version") == 1 and manifest.get("schema_version") == SCHEMA_VERSION
                        and isinstance(manifest.get("files"), dict), "INCOMPATIBLE_STATE", "Unsupported backup format")
                require(set(names) == set(manifest["files"]) | {"BACKUP.json"}, "INVALID_BACKUP", "Backup inventory does not exactly match its members")
                required = {"identity.json", "tokens.json", "author.sqlite3"}
                require(required <= set(names), "INVALID_BACKUP", "Backup is missing required deployment identity/state")
                require(names["identity.json"].file_size <= 1048576, "INVALID_BACKUP", "Runtime identity is too large")
                identity = strict_json(archive.read("identity.json"))
                from ..models import RuntimeLimits
                limits = RuntimeLimits.model_validate(identity["limits"])
                total = sum(info.file_size for name, info in names.items() if name != "BACKUP.json")
                require(total <= limits.total_storage_bytes, "RESOURCE_LIMIT", "Backup expands beyond its declared runtime budget")
                require(shutil.disk_usage(destination.parent).free - total >= limits.minimum_free_bytes,
                        "RESOURCE_LIMIT", "Insufficient disk to restore author state")
                for name, metadata in sorted(manifest["files"].items()):
                    parts = name.split("/")
                    require(not any("/".join(parts[:i]) in names for i in range(1, len(parts))), "INVALID_BACKUP", "Backup has a file/directory collision")
                    require(name in required or name == "client-qualification.json" or name.startswith(("blobs/", "repositories/", "operator-leases/")),
                            "INVALID_BACKUP", "Backup contains an unsupported runtime path")
                    info = names[name]
                    require(type(metadata.get("bytes")) is int and info.file_size == metadata["bytes"]
                            and metadata.get("mode") in {0o600, 0o700}, "INVALID_BACKUP", "Backup member metadata mismatch")
                    target = temporary / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    h, size = hashlib.sha256(), 0
                    with archive.open(info) as source, target.open("xb") as output:
                        for chunk in iter(lambda: source.read(65536), b""):
                            size += len(chunk)
                            require(size <= metadata["bytes"], "INVALID_BACKUP", "Backup member exceeded its declared length")
                            h.update(chunk); output.write(chunk)
                        output.flush(); os.fsync(output.fileno())
                    target.chmod(metadata["mode"])
                    require(size == metadata["bytes"] and h.hexdigest() == metadata.get("sha256"), "INVALID_BACKUP", "Backup content checksum failed", path=name)
            for name in ("blobs", "repositories", "locks", "uploads", "staging", "runs", "operator-leases"):
                (temporary / name).mkdir(exist_ok=True, mode=0o700)
            store = Store(temporary, expected_instance=manifest["instance_id"])
            store.integrity()
            for repo in store.rows("SELECT id FROM repositories"):
                Repositories(store).git(repo["id"]).run(["fsck", "--full", "--no-reflogs"], timeout=300)
            os.rename(temporary, destination); fsync_dir(destination.parent)
            return Store(destination, expected_instance=manifest["instance_id"])
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def gc(self, *, minimum_age_seconds: int = 3600) -> dict:
        require(type(minimum_age_seconds) is int and minimum_age_seconds >= 60, "INVALID_REQUEST", "Garbage collection needs at least a 60-second age guard")
        self._quiescent()
        removed_blobs, removed_runs, removed_temporary = 0, 0, 0
        cutoff = utc_now() - minimum_age_seconds
        with file_lock(self.store.home / "locks/maintenance", blocking=False):
            self._quiescent()
            referenced = {row["sha"] for row in self.store.rows("SELECT DISTINCT sha FROM blob_refs")}
            for row in self.store.rows("SELECT payload FROM operations WHERE state NOT IN ('completed','failed','blocked','cancelled','timed_out')"):
                payload = json.loads(row["payload"])
                if payload.get("archive_sha"):
                    referenced.add(payload["archive_sha"])
            for prefix in self.store.blobs.root.iterdir():
                if prefix.is_symlink() or not prefix.is_dir():
                    continue
                for path in prefix.iterdir():
                    sha = prefix.name + path.name
                    if path.is_file() and not path.is_symlink() and sha not in referenced and path.stat().st_mtime < cutoff:
                        path.unlink(); removed_blobs += 1
            for row in self.store.rows("SELECT id,receipt_sha FROM runs WHERE cleanup='proven' AND receipt_sha IS NOT NULL AND updated<?", (cutoff,)):
                safe_id(row["id"])
                require(row["receipt_sha"] in referenced and self.store.blobs.verify(row["receipt_sha"]), "CORRUPT_STATE", "Execution evidence must be retained before workspace cleanup")
                path = self.store.home / "runs" / row["id"]
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                else:
                    continue
                removed_runs += 1
            for name in ("staging", "uploads"):
                for path in (self.store.home / name).iterdir():
                    if path.lstat().st_mtime < cutoff:
                        if path.is_dir() and not path.is_symlink():
                            shutil.rmtree(path)
                        else:
                            path.unlink()
                        removed_temporary += 1
            with self.store.transaction() as conn:
                self.store.event(conn, "maintenance.gc", "", {"blobs": removed_blobs, "runs": removed_runs, "temporary": removed_temporary})
        return {"unreferenced_blobs_removed": removed_blobs, "execution_directories_removed": removed_runs,
                "temporary_entries_removed": removed_temporary, "accepted_candidates_removed": 0}
