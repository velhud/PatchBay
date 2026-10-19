from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.packages.builder import build, prepare, _write_zip
from patchbay.authoring.packages.archive import PackageReader
from patchbay.authoring.persistence.store import Store
from patchbay.authoring.service import AuthorService
from patchbay.authoring.util import canonical, digest, strict_json


def package_for(service, task, tmp_path, changes, *, revision=None, paths=None):
    task_id = task["id"] if isinstance(task, dict) else task
    context = service.snapshots.export(task_id, revision=revision, paths=paths)
    area = tmp_path / ("package-" + uuid.uuid4().hex)
    area.mkdir()
    with zipfile.ZipFile(service.store.blobs.path(context["bundle_sha"])) as archive:
        archive.extractall(area / "context")
    edited = area / "edited"
    prepare(area / "context", edited)
    for path, value in changes.items():
        target = edited / path
        if value is None:
            target.unlink()
            continue
        mode = None
        if isinstance(value, tuple):
            value, mode = value
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode() if isinstance(value, str) else value)
        if mode is not None:
            target.chmod(mode)
    output = area / "changes.zip"
    build(area / "context", edited, output, summary="Actual implementation fixture")
    return output


def changed_archive(original, output, mutate):
    with zipfile.ZipFile(original) as archive:
        members = {name: (archive.read(name), 0o644) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"][0])
    mutate(manifest, members)
    members["manifest.json"] = (canonical(manifest), 0o644)
    _write_zip(output, members)
    return output


def submit(service, task, package, lease, key="implementation"):
    return service.materializer.submit(task["id"], package, key=key, token=lease["token"], epoch=lease["epoch"])


def test_context_reproducible_and_original_dirty_checkout_preserved(setup_author, tmp_path):
    service, source, repo, task, lease = setup_author
    first = service.snapshots.export(task["id"])
    second = service.snapshots.export(task["id"])
    assert first["bundle_sha"] == second["bundle_sha"] and first["id"] == second["id"]
    with zipfile.ZipFile(service.store.blobs.path(first["bundle_sha"])) as archive:
        names = archive.namelist()
        assert "source/app.py" in names and "source/local-only.txt" not in names
        assert "tools/build_change_package.py" in names
        inventory = json.loads(archive.read("INVENTORY.json"))
        assert inventory["omission_is_not_absence"] is True
    package = package_for(service, task, tmp_path, {"app.py": "def add(a, b):\n    return a + b\n"})
    result = submit(service, task, package, lease)
    assert result["revision"] == 1
    assert "a - b" in (source / "app.py").read_text()
    assert (source / "local-only.txt").read_text().startswith("Uncommitted")
    assert service.repositories.git(repo["id"]).read_file(result["commit"], "app.py").endswith(b"a + b\n")


def test_duplicate_bytes_and_recompression_are_one_semantic_revision(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "one\n"})
    first = submit(service, task, package, lease, "one")
    repacked = tmp_path / "repacked.zip"
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(repacked, "w", compression=zipfile.ZIP_STORED) as destination:
        for name in reversed(source.namelist()):
            destination.writestr(name, source.read(name))
    second = submit(service, task, repacked, lease, "two")
    assert first["commit"] == second["commit"] and second["duplicate"] is True
    assert service.tasks.get(task["id"])["current_revision"] == 1
    assert len(service.store.rows("SELECT * FROM revisions WHERE task_id=?", (task["id"],))) == 2


def test_idempotency_conflict_and_stale_submission_cannot_overwrite(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    first = package_for(service, task, tmp_path, {"added.txt": "one"})
    second = package_for(service, task, tmp_path, {"added.txt": "two"})
    accepted = submit(service, task, first, lease, "key")
    with pytest.raises(AuthorError) as error:
        submit(service, task, second, lease, "key")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(AuthorError) as error:
        submit(service, task, second, lease, "new-key")
    assert error.value.code == "STALE_BASE"
    assert service.tasks.get(task["id"])["current_commit"] == accepted["commit"]


@pytest.mark.parametrize("field,value", [("format_version", 2), ("format_version", True), ("expected_revision", "0"), ("unknown", 1)])
def test_strict_manifest_fields(setup_author, tmp_path, field, value):
    service, _, _, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "one"})
    broken = changed_archive(package, tmp_path / "broken.zip", lambda manifest, members: manifest.update({field: value}))
    with pytest.raises(AuthorError): submit(service, task, broken, lease)
    assert service.tasks.get(task["id"])["current_revision"] == 0


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/drive", "a\\b", ".git/config", "a/.GIT/config", "a//b", "x/../y", "a. /b", "trailing."])
def test_archive_rejects_unsafe_paths(setup_author, tmp_path, path):
    service, _, _, task, _ = setup_author
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(path, b"bad")
        output.writestr("manifest.json", b"{}")
    with pytest.raises(AuthorError): PackageReader(service.store).read(archive)
    assert service.tasks.get(task["id"])["current_revision"] == 0


def test_corruption_undeclared_members_and_duplicate_paths_fail(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "one"})
    def corrupt(manifest, members):
        members[manifest["operations"][0]["payload"]] = (b"different", 0o644)
    with pytest.raises(AuthorError):
        submit(service, task, changed_archive(package, tmp_path / "corrupt.zip", corrupt), lease)
    def extra(manifest, members): members["extra.dat"] = (b"undeclared", 0o644)
    with pytest.raises(AuthorError):
        submit(service, task, changed_archive(package, tmp_path / "extra.zip", extra), lease)
    def duplicate(manifest, members): manifest["operations"].append(dict(manifest["operations"][0]))
    with pytest.raises(AuthorError):
        submit(service, task, changed_archive(package, tmp_path / "duplicate.zip", duplicate), lease)
    assert service.tasks.get(task["id"])["current_revision"] == 0


def test_binary_empty_executable_delete_and_move_roundtrip(setup_author, tmp_path):
    service, _, repo, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"binary.bin": b"\x00\xff\r\n", "empty.txt": b"", "script.sh": (b"#!/bin/sh\necho OK\n", 0o755), "README.md": None})
    one = submit(service, task, package, lease, "first")
    git = service.repositories.git(repo["id"])
    assert git.read_file(one["commit"], "binary.bin") == b"\x00\xff\r\n"
    assert git.entries(one["commit"])["script.sh"]["mode"] == "100755"
    assert "README.md" not in git.entries(one["commit"])
    context = service.snapshots.export(task["id"])
    manifest = {"format_version": 1, "repository_id": repo["id"], "task_id": task["id"], "submission_id": "move",
                "context_id": context["id"], "expected_revision": 1, "expected_base_commit": one["commit"],
                "git_object_format": git.object_format,
                "operations": [{"op": "move", "path": "binary.bin", "destination": "renamed.bin", "before_sha256": digest(b"\x00\xff\r\n"), "before_mode": "100644"},
                               {"op": "chmod", "path": "script.sh", "before_sha256": digest(b"#!/bin/sh\necho OK\n"), "before_mode": "100755", "mode": "100644"}]}
    moved = tmp_path / "move.zip"; _write_zip(moved, {"manifest.json": (canonical(manifest), 0o644)})
    two = submit(service, task, moved, lease, "second")
    entries = git.entries(two["commit"])
    assert "binary.bin" not in entries and "renamed.bin" in entries
    assert entries["script.sh"]["mode"] == "100644"


def test_omitted_file_is_not_a_deletion_or_new_file(setup_author, tmp_path):
    service, _, repo, task, lease = setup_author
    context = service.snapshots.export(task["id"], paths=["app.py"])
    area = tmp_path / "partial"
    with zipfile.ZipFile(service.store.blobs.path(context["bundle_sha"])) as archive:
        archive.extractall(area)
    prepare(area, tmp_path / "edited")
    (tmp_path / "edited/app.py").write_text("def add(a,b): return a+b\n")
    manifest = build(area, tmp_path / "edited", tmp_path / "good.zip")
    assert all(op["path"] == "app.py" for op in manifest["operations"])
    (tmp_path / "edited/README.md").write_text("Invented replacement")
    with pytest.raises(ValueError, match="omitted"):
        build(area, tmp_path / "edited", tmp_path / "bad.zip")


def test_writer_takeover_fences_old_token(setup_author, tmp_path):
    service, _, _, task, old = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "one"})
    new = service.leases.claim(task["id"], label="new-author", takeover=True)
    assert new["epoch"] > old["epoch"]
    with pytest.raises(AuthorError) as error: submit(service, task, package, old)
    assert error.value.code == "LEASE_LOST"
    assert submit(service, task, package, new)["revision"] == 1


def test_reopen_missing_state_and_read_only_state_queries(setup_author):
    service, _, _, task, _ = setup_author
    before = service.store.one("SELECT COUNT(*) AS count FROM events")["count"]
    reopened = AuthorService(service.store.home)
    for _ in range(30):
        assert reopened.tasks.get(task["id"])["current_revision"] == 0
        reopened.capabilities(); reopened.tasks.list()
    assert service.store.one("SELECT COUNT(*) AS count FROM events")["count"] == before
    with pytest.raises(AuthorError) as error: Store(service.store.home.parent / "missing-state")
    assert error.value.code == "STATE_UNAVAILABLE"


def test_independent_import_graph(setup_author):
    service, _, _, task, _ = setup_author
    script = """import sys
from pathlib import Path
from patchbay.authoring.service import AuthorService
from patchbay.authoring.interfaces.cli import parser
from patchbay.authoring.interfaces.http import create_app
s=AuthorService(Path(sys.argv[1])); s.capabilities(); parser()
create_app(s.store.home)
for forbidden in ('patchbay.server','patchbay.tools.handler','patchbay.jobs','patchbay.workers','openai'):
    assert not any(name == forbidden or name.startswith(forbidden + '.') for name in sys.modules), forbidden
print('independent')
"""
    result = subprocess.run([sys.executable, "-c", script, str(service.store.home)], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
    assert result.returncode == 0, result.stderr
    assert "independent" in result.stdout
