from __future__ import annotations

import json
import os
import time
import zipfile

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.revisions.drafts import Drafts
from patchbay.authoring.util import canonical, digest, fingerprint


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


@pytest.mark.parametrize("case,expected_code", [
    ("stale_revision", "STALE_BASE"),
    ("stale_hash", "STALE_DRAFT"),
    ("outside_policy", "POLICY_DENIED"),
    ("unsafe_path", "INVALID_PATH"),
])
def test_rejected_draft_apply_is_terminal_and_idempotent(setup_author, case, expected_code):
    s, _, _, task, lease = setup_author
    if case == "outside_policy":
        policy = dict(task["policy"])
        policy["writable_paths"] = ["ledger.py"]
        with s.store.transaction() as conn:
            conn.execute("UPDATE tasks SET policy=?,policy_digest=? WHERE id=?",
                         (canonical(policy).decode(), fingerprint(policy), task["id"]))
    edits = [{"op": "create", "path": "added.txt", "content": "trial"}]
    expected_revision = 0
    if case == "stale_revision":
        expected_revision = 1
    elif case == "stale_hash":
        edits = [{"op": "replace", "path": "app.py", "before_sha256": "0" * 64, "content": "bad"}]
    elif case == "outside_policy":
        edits = [{"op": "create", "path": "tests/test_app.py", "content": "bad"}]
    elif case == "unsafe_path":
        edits = [{"op": "create", "path": "../outside", "content": "bad"}]

    drafts = Drafts(s.store)
    arguments = {"expected_revision": expected_revision, "expected_version": 0, "edits": edits,
                 "key": "rejected-" + case, "token": lease["token"], "epoch": lease["epoch"]}
    with pytest.raises(AuthorError) as rejected:
        drafts.apply(task["id"], **arguments)
    assert rejected.value.code == expected_code

    operation = s.store.one("SELECT * FROM operations WHERE task_id=? AND kind='draft.apply' AND idempotency_key=?",
                            (task["id"], s.store.scoped_key(arguments["key"])))
    assert operation["state"] == "failed" and operation["phase"] == "terminal"
    assert json.loads(operation["error"])["code"] == expected_code
    assert operation["result"] is None
    with pytest.raises(AuthorError) as replay:
        drafts.apply(task["id"], **arguments)
    assert replay.value.code == "OPERATION_FAILED"
    assert s.store.operation(operation["id"])["state"] == "failed"
    assert s.tasks.get(task["id"])["current_revision"] == 0
    assert s.store.rows("SELECT * FROM drafts WHERE task_id=?", (task["id"],)) == []


def test_preexisting_queued_apply_is_revalidated_and_terminalized(setup_author):
    s, _, _, task, lease = setup_author
    arguments = {"expected_revision": 1, "expected_version": 0,
                 "edits": [{"op": "create", "path": "added.txt", "content": "trial"}],
                 "key": "legacy-queued-rejection", "token": lease["token"], "epoch": lease["epoch"]}
    request = {"task_id": task["id"], "expected_revision": arguments["expected_revision"],
               "expected_version": arguments["expected_version"], "draft_id": None, "edits": arguments["edits"]}
    old_operation, created = s.store.begin_operation(kind="draft.apply", task_id=task["id"],
        key=arguments["key"], fingerprint=fingerprint(request), payload=request,
        revision=arguments["expected_revision"])
    assert created and old_operation["state"] == "queued"

    with pytest.raises(AuthorError) as rejected:
        Drafts(s.store).apply(task["id"], **arguments)
    assert rejected.value.code == "STALE_BASE"
    resolved = s.store.operation(old_operation["id"])
    assert resolved["state"] == "failed" and resolved["phase"] == "terminal"
    assert resolved["error"]["code"] == "STALE_BASE"
    assert resolved["result"] is None
    assert s.tasks.get(task["id"])["current_revision"] == 0
    assert s.store.rows("SELECT * FROM drafts WHERE task_id=?", (task["id"],)) == []


def test_rejected_draft_seal_is_terminal_and_idempotent(setup_author):
    s, _, _, task, lease = setup_author
    drafts = Drafts(s.store)
    created = drafts.apply(task["id"], expected_revision=0, expected_version=0, key="draft-for-bad-seal",
        edits=[{"op": "create", "path": "new.txt", "content": "one"}],
        token=lease["token"], epoch=lease["epoch"])
    arguments = {"draft_id": created["draft_id"], "expected_version": 2, "key": "bad-seal-version",
                 "token": lease["token"], "epoch": lease["epoch"]}
    with pytest.raises(AuthorError) as rejected:
        drafts.seal(**arguments)
    assert rejected.value.code == "STALE_DRAFT"
    operation = s.store.one("SELECT * FROM operations WHERE task_id=? AND kind='draft.seal' AND idempotency_key=?",
                            (task["id"], s.store.scoped_key(arguments["key"])))
    assert operation["state"] == "failed" and json.loads(operation["error"])["code"] == "STALE_DRAFT"
    with pytest.raises(AuthorError) as replay:
        drafts.seal(**arguments)
    assert replay.value.code == "OPERATION_FAILED"
    assert s.store.operation(operation["id"])["state"] == "failed"
    assert s.tasks.get(task["id"])["current_revision"] == 0


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
