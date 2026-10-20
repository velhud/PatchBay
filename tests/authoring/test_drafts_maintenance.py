from __future__ import annotations

import json
import os
import time
import zipfile

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.revisions.drafts import Drafts
from patchbay.authoring.util import digest


def test_draft_exact_edits_are_isolated_and_seal_through_package_core(setup_author):
    s, source, repo, task, lease = setup_author
    drafts = Drafts(s.store)
    authority = {"token": lease["token"], "epoch": lease["epoch"]}
    old = b"def add(a, b):\n    return a - b\n"
    first = drafts.apply(task["id"], expected_revision=0, expected_version=0, key="draft-first", edits=[{
        "op": "edit", "path": "app.py", "before_sha256": digest(old), "old_text": "a - b", "new_text": "a + b"}], **authority)
    assert first["version"] == 1 and s.tasks.get(task["id"])["current_revision"] == 0
    assert "a + b" in drafts.read(first["draft_id"], 1, "app.py")["text"]
    assert "a - b" in (source / "app.py").read_text()
    sealed = drafts.seal(first["draft_id"], expected_version=1, key="seal", **authority)
    assert sealed["revision"] == 1
    assert drafts.seal(first["draft_id"], expected_version=1, key="seal", **authority)["commit"] == sealed["commit"]
    assert s.repositories.git(repo["id"]).read_file(sealed["commit"], "app.py") == old.replace(b"a - b", b"a + b")


def test_stale_draft_version_and_wrong_source_hash_are_rejected(setup_author):
    s, _, _, task, lease = setup_author
    authority = {"token": lease["token"], "epoch": lease["epoch"]}
    drafts = Drafts(s.store)
    first = drafts.apply(task["id"], expected_revision=0, expected_version=0, key="create", edits=[{"op": "create", "path": "new.txt", "content": "one"}], **authority)
    with pytest.raises(AuthorError) as error:
        drafts.apply(task["id"], draft_id=first["draft_id"], expected_revision=0, expected_version=0, key="stale", edits=[{"op": "create", "path": "other.txt", "content": "two"}], **authority)
    assert error.value.code == "STALE_DRAFT"
    with pytest.raises(AuthorError) as error:
        drafts.apply(task["id"], draft_id=first["draft_id"], expected_revision=0, expected_version=1, key="bad-hash", edits=[{"op": "replace", "path": "new.txt", "content": "two", "before_sha256": "0" * 64}], **authority)
    assert error.value.code == "STALE_DRAFT"


def test_backup_checksum_corruption_does_not_create_runtime(setup_author, tmp_path):
    s, _, _, _, _ = setup_author
    original = tmp_path / "backup.zip"; Maintenance(s.store).backup(original)
    corrupted = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(corrupted, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "tokens.json": content = b"{}"
            target.writestr(info, content)
    destination = tmp_path / "must-not-exist"
    with pytest.raises(AuthorError): Maintenance.restore(corrupted, destination)
    assert not destination.exists()


def test_gc_keeps_referenced_context_and_removes_only_old_unreferenced_blobs(setup_author):
    s, _, _, task, _ = setup_author
    context = s.snapshots.export(task["id"])
    old = s.store.blobs.put(b"unreferenced fixture bytes")
    os.utime(s.store.blobs.path(old), (time.time() - 3600, time.time() - 3600))
    os.utime(s.store.blobs.path(context["bundle_sha"]), (time.time() - 3600, time.time() - 3600))
    result = Maintenance(s.store).gc(minimum_age_seconds=60)
    assert result["unreferenced_blobs_removed"] == 1
    assert not s.store.blobs.path(old).exists()
    assert s.store.blobs.verify(context["bundle_sha"])
