from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from pathlib import Path

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.packages.builder import _write_zip, build, prepare
from patchbay.authoring.packages.inbox import PackageInbox
from patchbay.authoring.service import AuthorService
from patchbay.authoring.util import canonical


def package_for(service, task, tmp_path, changes):
    context = service.snapshots.export(task["id"])
    area = tmp_path / ("inbox-package-" + uuid.uuid4().hex)
    area.mkdir()
    with zipfile.ZipFile(service.store.blobs.path(context["bundle_sha"])) as source:
        source.extractall(area / "context")
    edited = area / "edited"
    prepare(area / "context", edited)
    for name, data in changes.items():
        target = edited / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
    output = area / "changes.zip"
    build(area / "context", edited, output, summary="Inbox fixture")
    return output


def rewrite_manifest(source: Path, destination: Path, mutate):
    with zipfile.ZipFile(source) as archive:
        members = {name: (archive.read(name), 0o644) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"][0])
    mutate(manifest)
    members["manifest.json"] = (canonical(manifest), 0o644)
    _write_zip(destination, members)
    return destination


def test_explicit_grant_imports_through_materializer_into_git(setup_author, tmp_path):
    service, source, repo, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"app.py": "def add(a, b):\n    return a + b\n"})
    inbox = PackageInbox(service.store, service.materializer)
    grant = inbox.grant(task["id"], lease["token"], lease["epoch"], auto_validate=True, key="grant-1")
    assert grant == {"task_id": task["id"], "auto_validate": True, "granted": True}
    assert "token" not in json.dumps(grant)

    accepted = inbox.ingest(package)
    assert accepted["state"] == "completed"
    result = accepted["result"]
    assert result["revision"] == 1
    assert service.repositories.git(repo["id"]).read_file(result["commit"], "app.py").endswith(b"return a + b\n")
    assert (source / "app.py").read_text().endswith("return a - b\n")
    assert service.tasks.get(task["id"])["current_revision"] == 1


def test_pending_package_survives_restart_and_requires_current_lease(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "durable\n"})
    inbox = PackageInbox(service.store, service.materializer)
    pending = inbox.ingest(package)
    assert pending["state"] == "awaiting_approval"
    assert inbox.pending()["operations"][0]["id"] == pending["id"]

    reopened = AuthorService(service.store.home)
    resumed = PackageInbox(reopened.store, reopened.materializer)
    assert resumed.store.operation(pending["id"])["state"] == "awaiting_approval"
    with pytest.raises(AuthorError) as error:
        resumed.approve(pending["id"], token="x" * 48, epoch=lease["epoch"], key="approve-denied")
    assert error.value.code == "LEASE_LOST"
    accepted = resumed.approve(pending["id"], token=lease["token"], epoch=lease["epoch"], key="approve-1")
    assert accepted["state"] == "completed"
    assert reopened.tasks.get(task["id"])["current_revision"] == 1
    replay = resumed.approve(pending["id"], token=lease["token"], epoch=lease["epoch"], key="approve-1")
    assert replay["id"] == accepted["id"] and replay["state"] == "completed"


def test_semantic_duplicate_across_repacked_files_has_one_inbox_record_and_revision(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "same\n"})
    repacked = tmp_path / "same.pbpkG"
    with zipfile.ZipFile(package) as original, zipfile.ZipFile(repacked, "w", compression=zipfile.ZIP_STORED) as target:
        for name in reversed(original.namelist()):
            target.writestr(name, original.read(name))
    inbox = PackageInbox(service.store, service.materializer)
    inbox.grant(task["id"], lease["token"], lease["epoch"], auto_validate=True, key="grant-auto")
    one = inbox.ingest(package)
    reopened = PackageInbox(AuthorService(service.store.home).store)
    two = reopened.ingest(repacked)
    assert one["id"] == two["id"]
    assert service.tasks.get(task["id"])["current_revision"] == 1
    assert len(service.store.rows("SELECT id FROM operations WHERE kind='package.inbox'")) == 1


def test_scan_ignores_partial_suffix_and_waits_for_stable_regular_file(setup_author, tmp_path, monkeypatch):
    service, _, _, task, _ = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "pending\n"})
    folder = tmp_path / "selected-downloads"
    folder.mkdir()
    completed = folder / "finished.zip"
    shutil.copyfile(package, completed)
    shutil.copyfile(package, folder / "still.zip.crdownload")
    inbox = PackageInbox(service.store)

    original = inbox._regular_stat
    calls = {"n": 0}

    def moving(path):
        info = original(path)
        if path == completed:
            calls["n"] += 1
            if calls["n"] == 2:
                class Changed:
                    pass
                changed = Changed()
                changed.st_dev, changed.st_ino = info.st_dev, info.st_ino
                changed.st_size, changed.st_mtime_ns = info.st_size, info.st_mtime_ns + 1
                changed.st_ctime_ns = info.st_ctime_ns
                return changed
        return info

    monkeypatch.setattr(inbox, "_regular_stat", moving)
    first = inbox.scan(folder, stability_seconds=0)
    assert [item["state"] for item in first["items"]] == ["in_progress"]
    monkeypatch.setattr(inbox, "_regular_stat", original)
    second = inbox.scan(folder, stability_seconds=0)
    assert len(second["items"]) == 1
    assert second["items"][0]["operation"]["state"] == "awaiting_approval"
    assert (folder / "still.zip.crdownload").exists()


@pytest.mark.parametrize("kind", ["truncated", "forged-task"])
def test_invalid_or_forged_packages_are_durably_rejected(setup_author, tmp_path, kind):
    service, _, _, task, _ = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "safe\n"})
    if kind == "truncated":
        broken = tmp_path / "truncated.zip"
        broken.write_bytes(package.read_bytes()[:50])
    else:
        broken = rewrite_manifest(package, tmp_path / "forged.zip",
                                  lambda manifest: manifest.update(task_id="task_" + "0" * 32))
    operation = PackageInbox(service.store).ingest(broken)
    assert operation["state"] == "failed"
    assert operation["error"]
    assert PackageInbox(service.store).store.operation(operation["id"])["error"] == operation["error"]
    assert service.tasks.get(task["id"])["current_revision"] == 0


def test_symlink_input_and_racing_copy_are_refused_and_recorded(setup_author, tmp_path, monkeypatch):
    service, _, _, task, _ = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "safe\n"})
    link = tmp_path / "link.zip"
    link.symlink_to(package)
    inbox = PackageInbox(service.store)
    rejected = inbox.ingest(link)
    assert rejected["state"] == "failed"
    assert rejected["error"]["code"] == "INVALID_PACKAGE"

    changing = tmp_path / "changing.zip"
    changing.write_bytes(package.read_bytes())
    put_stream = service.store.blobs.put_stream

    def mutate_during_read(stream, *, limit):
        sha = put_stream(stream, limit=limit)
        data = package.read_bytes()
        changing.write_bytes(data + b"changed")
        return sha

    monkeypatch.setattr(service.store.blobs, "put_stream", mutate_during_read)
    raced = inbox.ingest(changing)
    assert raced["state"] == "retryable"
    assert raced["error"]["code"] == "PACKAGE_IN_PROGRESS"
    monkeypatch.setattr(service.store.blobs, "put_stream", put_stream)
    assert service.tasks.get(task["id"])["current_revision"] == 0


def test_expired_or_superseded_grant_waits_without_lease_takeover(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "safe\n"})
    inbox = PackageInbox(service.store)
    inbox.grant(task["id"], lease["token"], lease["epoch"], auto_validate=True, key="grant-before-takeover")
    replacement = service.leases.claim(task["id"], label="explicit-operator", takeover=True)
    pending = inbox.ingest(package)
    assert pending["state"] == "awaiting_approval"
    assert service.tasks.get(task["id"])["current_revision"] == 0
    assert service.store.one("SELECT epoch FROM leases WHERE task_id=?", (task["id"],))["epoch"] == replacement["epoch"]


def test_symlink_in_selected_folder_is_reported_without_following(setup_author, tmp_path):
    service, _, _, _, _ = setup_author
    folder = tmp_path / "selected"
    folder.mkdir()
    target = tmp_path / "outside.zip"
    target.write_bytes(b"not read")
    (folder / "escape.zip").symlink_to(target)
    result = PackageInbox(service.store).scan(folder, stability_seconds=0)
    assert result["items"][0]["error"]["code"] == "INVALID_PACKAGE"
    assert target.read_bytes() == b"not read"


def test_bounded_scan_continues_beyond_first_batch(setup_author, tmp_path):
    service, _, _, task, _ = setup_author
    package = package_for(service, task, tmp_path, {"added.txt": "scan continuation"})
    folder = tmp_path / "batched-inbox"
    folder.mkdir()
    for number in range(3):
        shutil.copyfile(package, folder / f"{number}.zip")
    first = service.inbox.scan(folder, stability_seconds=0, max_files=2)
    assert first["next_offset"] == 2 and first["more_available"]
    second = service.inbox.scan(folder, stability_seconds=0, max_files=2, offset=first["next_offset"])
    assert [item["name"] for item in second["items"]] == ["2.zip"]
    assert second["next_offset"] is None and not second["more_available"]
    assert second["items"][0]["operation"]["id"] == first["items"][0]["operation"]["id"]
