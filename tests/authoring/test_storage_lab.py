"""Opt-in real tmpfs exhaustion checks for Author persistence boundaries.

These tests require the explicit storage-lab runner. They intentionally fail if
run outside its bounded private tmpfs mount.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.receipts import new_receipt, record_receipt
from patchbay.authoring.models import RuntimeLimits
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.persistence.store import Store
from patchbay.authoring.service import AuthorService
from patchbay.authoring.util import canonical, fingerprint

from test_core import package_for, submit


_MIB = 1024 * 1024


def _mount_for(path: Path) -> tuple[Path, int, int]:
    """Return mountpoint, size limit and inode limit for the path's mount."""
    target = path.resolve()
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, fs = left.split(), right.split()
        mountpoint = Path(fields[4].replace("\\040", " "))
        if target == mountpoint or mountpoint in target.parents:
            matches.append((len(str(mountpoint)), mountpoint, fs[0], fs[2]))
    assert matches, f"no mount found for {target}"
    _, mountpoint, filesystem, options = max(matches)
    assert filesystem == "tmpfs", f"storage lab path is on {filesystem}, expected tmpfs"
    assert "size=" in options and "nr_inodes=" in options, "tmpfs limits are not explicit"
    size = _option_bytes(options, "size")
    inodes = int(options.split("nr_inodes=", 1)[1].split(",", 1)[0])
    assert size <= 64 * _MIB, f"tmpfs size {size} exceeds 64 MiB"
    assert inodes <= 2048, f"tmpfs inode limit {inodes} exceeds 2048"
    return mountpoint, size, inodes


def _option_bytes(options: str, key: str) -> int:
    value = options.split(key + "=", 1)[1].split(",", 1)[0]
    suffix = value[-1].lower() if value[-1].isalpha() else ""
    scale = {"k": 1024, "m": _MIB, "g": 1024**3}.get(suffix, 1)
    return int(value[:-1] if suffix else value) * scale


def _assert_lab_mount(path: Path) -> None:
    mountpoint, size_limit, inode_limit = _mount_for(path)
    assert path.resolve().is_relative_to(mountpoint)
    assert shutil.disk_usage(path).total <= size_limit
    assert inode_limit <= 2048


def _fill_leave(path: Path, remaining: int) -> Path:
    """Create one owned file, leaving approximately `remaining` bytes free."""
    free = shutil.disk_usage(path.parent).free
    amount = max(0, free - remaining)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        block = b"F" * 65536
        while amount:
            piece = block[: min(len(block), amount)]
            try:
                written = os.write(fd, piece)
            except OSError as exc:
                assert exc.errno == errno.ENOSPC, f"fill stopped for errno={exc.errno}"
                break
            amount -= written
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _has_enospc(error: BaseException) -> bool:
    current = error
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, OSError) and current.errno == errno.ENOSPC:
            return True
        current = current.__cause__ or current.__context__
    return False


def _seed_receipt_run(service, task) -> tuple[dict, dict, dict]:
    current = service.tasks.get(task["id"])
    op, _ = service.store.begin_operation(
        kind="checks.run", task_id=task["id"], key="storage-lab-receipt",
        fingerprint=fingerprint({"storage-lab": "receipt"}), payload={"selected": []},
        revision=current["current_revision"],
    )
    now = time.time()
    with service.store.transaction() as conn:
        conn.execute(
            """INSERT INTO runs
            (id,task_id,revision,commit_oid,tree_oid,profile,profile_digest,state,cleanup,environment_digest,created,updated)
            VALUES(?,?,?,?,?,?,?,'running','proven',NULL,?,?)""",
            (op["id"], task["id"], current["current_revision"], current["current_commit"],
             current["revision"]["tree_oid"], canonical(current["policy"]["profile"]).decode(),
             fingerprint(current["policy"]["profile"]), now, now),
        )
        conn.execute("UPDATE operations SET state='running',phase='verifying',updated=? WHERE id=?",
                     (now, op["id"]))
    run = service.store.one("SELECT * FROM runs WHERE id=?", (op["id"],))
    from test_fault_boundaries import _synthetic_receipt
    receipt = _synthetic_receipt(service, current, run, passed=True)
    return run, current, receipt


@pytest.mark.skipif(
    os.environ.get("PATCHBAY_STORAGE_LAB") != "1",
    reason="requires the explicitly opt-in bounded tmpfs storage lab",
)
def test_real_storage_inode_backup_restore_and_retention_boundaries(setup_author, tmp_path):
    _assert_lab_mount(tmp_path)
    service, _, repo, task, lease = setup_author
    mountpoint, size_limit, inode_limit = _mount_for(tmp_path)
    assert service.store.home.resolve().is_relative_to(mountpoint)

    # Real block exhaustion before package acceptance. Free only our named fill
    # file and retry the identical request to prove it was not falsely accepted.
    # A highly compressible but expanded payload passes the authorized storage
    # preflight (based on archive bytes) while its extracted blob exceeds the
    # deliberately small remaining tmpfs capacity.
    package_path = package_for(service, task, tmp_path, {"fault.bin": b"P" * (8 * _MIB)})
    fill = _fill_leave(tmp_path / "package-fill.bin", 64 * 1024)
    try:
        with pytest.raises(Exception) as error:
            submit(service, task, package_path, lease, "storage-lab-package")
        assert _has_enospc(error.value), f"failure did not preserve actual ENOSPC: {error.value!r}"
        assert not service.store.rows("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'",
                                      (task["id"],))
        assert service.tasks.get(task["id"])["current_revision"] == 0
    finally:
        fill.unlink(missing_ok=True)
    accepted = submit(service, task, package_path, lease, "storage-lab-package")
    assert accepted["revision"] == 1
    assert service.tasks.get(task["id"])["current_revision"] == 1

    # Actual ENOSPC at receipt blob persistence must leave the run active and
    # task unaccepted. Releasing only this fixture file permits a clean retry.
    run, current, receipt = _seed_receipt_run(service, task)
    receipt["storage_lab_padding"] = "R" * (256 * 1024)
    fill = _fill_leave(tmp_path / "receipt-fill.bin", 64 * 1024)
    try:
        with pytest.raises(OSError) as error:
            record_receipt(service.store, run["id"], receipt)
        assert error.value.errno == errno.ENOSPC
        assert service.store.one("SELECT state,receipt_sha FROM runs WHERE id=?", (run["id"],)) == {
            "state": "running", "receipt_sha": None}
        assert service.tasks.get(task["id"])["status"] != "ready_for_review"
        assert not service.store.rows("SELECT * FROM blob_refs WHERE owner=?", ("receipt:" + run["id"],))
    finally:
        fill.unlink(missing_ok=True)
    recorded = record_receipt(service.store, run["id"], receipt)
    assert recorded["state"] == "completed"

    # A successful backup gives the actual archive size for calibrating a
    # second run: enough room for its staging archive, then ENOSPC while the
    # separate publication copy is written.
    maintenance = Maintenance(service.store)
    # A modest incompressible orphan makes archive/publication sizing robust
    # while remaining well inside the 64 MiB fixture budget.
    service.store.blobs.put(os.urandom(512 * 1024))
    backup = tmp_path / "good-backup.zip"
    maintenance.backup(backup)
    archive_bytes = backup.stat().st_size
    backup.unlink()
    target = tmp_path / "full-backup.zip"
    _fill_leave(tmp_path / "backup-fill.bin", max(4096, archive_bytes * 3 // 2))
    with pytest.raises(OSError) as error:
        maintenance.backup(target)
    assert error.value.errno == errno.ENOSPC
    partial = target.with_name(target.name + ".part")
    # Keep this assertion strict: an abandoned partial publication is a real
    # cleanup defect. Remove only this lab's output before continuing.
    partial_left = partial.exists()
    partial.unlink(missing_ok=True)
    (tmp_path / "backup-fill.bin").unlink()
    assert not target.exists()

    # Restore admission has enough bytes for the declared expanded state, but
    # no headroom for filesystem metadata. The restore must fail with ENOSPC
    # and remove its owned temporary directory.
    good_backup = tmp_path / "restore-source.zip"
    maintenance.backup(good_backup)
    with __import__("zipfile").ZipFile(good_backup) as archive:
        manifest = json.loads(archive.read("BACKUP.json"))
        restore_bytes = sum(item["bytes"] for item in manifest["files"].values())
    _fill_leave(tmp_path / "restore-fill.bin", restore_bytes + 16 * 1024)
    restored = tmp_path / "restored-state"
    with pytest.raises(OSError) as error:
        Maintenance.restore(good_backup, restored)
    assert error.value.errno == errno.ENOSPC
    assert not restored.exists()
    assert not list(tmp_path.glob(".author-restore-*")), "failed restore left a temporary state tree"
    (tmp_path / "restore-fill.bin").unlink()

    # Inode exhaustion is independent of block exhaustion. Remove exactly the
    # created files, then confirm service state remains readable.
    inode_dir = tmp_path / "inode-fixture"
    inode_dir.mkdir()
    created = []
    while True:
        path = inode_dir / f"i-{len(created):04d}"
        try:
            path.touch(exist_ok=False)
        except OSError as exc:
            assert exc.errno == errno.ENOSPC
            break
        created.append(path)
    assert created, "inode exhaustion did not occur"
    for path in created:
        path.unlink()
    inode_dir.rmdir()
    assert service.store.integrity()["database"] == "ok"

    # Successful backup/restore and GC replay: retained package and receipt
    # references survive, while only the aged unreferenced fixture blob is
    # collected. Replaying the accepted package remains one revision.
    final_backup = tmp_path / "final-backup.zip"
    maintenance.backup(final_backup)
    restored_store = Maintenance.restore(final_backup, tmp_path / "restored-final")
    reopened = AuthorService(restored_store.home)
    replayed = submit(reopened, task, package_path, lease, "storage-lab-package")
    assert replayed["commit"] == accepted["commit"]
    assert reopened.tasks.get(task["id"])["current_revision"] == 1
    referenced = {row["sha"] for row in reopened.store.rows("SELECT DISTINCT sha FROM blob_refs")}
    assert all(reopened.store.blobs.verify(sha) for sha in referenced)
    orphan = reopened.store.blobs.put(b"storage-lab old unreferenced blob")
    os.utime(reopened.store.blobs.path(orphan), (time.time() - 3600, time.time() - 3600))
    gc = Maintenance(reopened.store).gc(minimum_age_seconds=60)
    assert gc["unreferenced_blobs_removed"] == 1
    assert not reopened.store.blobs.path(orphan).exists()
    assert all(reopened.store.blobs.verify(sha) for sha in referenced)
    assert reopened.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"] == "completed"

    # Compact facts are written by pytest's caller into external raw evidence.
    assert size_limit <= 64 * _MIB and inode_limit <= 2048
    assert not partial_left, "failed backup publication left a partial destination"


@pytest.mark.parametrize("partial_kind", ["file", "symlink", "broken_symlink"])
def test_backup_preserves_preexisting_partial_path(setup_author, tmp_path, partial_kind):
    service, _, _, _, _ = setup_author
    output = tmp_path / "protected-backup.zip"
    partial = output.with_name(output.name + ".part")
    sentinel = tmp_path / "sentinel"
    if partial_kind == "file":
        partial.write_bytes(b"keep this preexisting partial")
        expected = partial.read_bytes()
    else:
        if partial_kind == "symlink":
            sentinel.write_bytes(b"keep target")
        partial.symlink_to(sentinel)
        expected = sentinel.read_bytes() if sentinel.exists() else None

    with pytest.raises(AuthorError, match="partial backup already occupies"):
        Maintenance(service.store).backup(output)

    if partial_kind == "file":
        assert partial.read_bytes() == expected
    else:
        assert partial.is_symlink()
        assert partial.resolve(strict=False) == sentinel.resolve(strict=False)
        assert sentinel.read_bytes() == expected if expected is not None else not sentinel.exists()


def test_backup_removes_owned_partial_after_publication_failure_and_can_retry(setup_author, tmp_path, monkeypatch):
    service, _, _, _, _ = setup_author
    maintenance = Maintenance(service.store)
    output = tmp_path / "retryable-backup.zip"
    partial = output.with_name(output.name + ".part")
    original_copy = shutil.copyfileobj

    def fail_during_copy(source, target, length=65536):
        target.write(b"owned partial bytes")
        raise OSError(errno.ENOSPC, "actual test fault during backup publication")

    monkeypatch.setattr(shutil, "copyfileobj", fail_during_copy)
    with pytest.raises(OSError) as error:
        maintenance.backup(output)
    assert error.value.errno == errno.ENOSPC
    assert not partial.exists()
    assert not output.exists()

    monkeypatch.setattr(shutil, "copyfileobj", original_copy)
    result = maintenance.backup(output)
    assert output.is_file() and result["output"] == str(output)


def test_backup_removes_owned_partial_after_rename_failure_without_replacing_output(setup_author, tmp_path, monkeypatch):
    service, _, _, _, _ = setup_author
    maintenance = Maintenance(service.store)
    output = tmp_path / "rename-failure-backup.zip"
    output.write_bytes(b"preserve existing backup")
    original_output = output.read_bytes()
    partial = output.with_name(output.name + ".part")
    original_replace = os.replace

    def fail_named_rename(source, destination):
        if Path(source) == partial and Path(destination) == output:
            raise OSError(errno.EACCES, "injected failure at backup publication rename")
        return original_replace(source, destination)

    monkeypatch.setattr("patchbay.authoring.persistence.maintenance.os.replace", fail_named_rename)
    with pytest.raises(OSError) as error:
        maintenance.backup(output)
    assert error.value.errno == errno.EACCES
    assert output.read_bytes() == original_output
    assert not partial.exists()

    monkeypatch.setattr("patchbay.authoring.persistence.maintenance.os.replace", original_replace)
    result = maintenance.backup(output)
    assert result["output"] == str(output)
    assert output.is_file() and output.read_bytes() != original_output
    assert not partial.exists()
