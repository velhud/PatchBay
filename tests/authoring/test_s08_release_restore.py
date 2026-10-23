"""S08 source identity and restore ordering, using disposable owned state."""
import json
import os
import sys
from pathlib import Path

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.interfaces import catalog
from patchbay.authoring.persistence.maintenance import Maintenance
from test_core import package_for, submit


def test_loaded_release_stays_pinned_when_disk_observation_drifts(monkeypatch):
    loaded = catalog.STARTUP_RUNTIME_SHA256
    before = catalog.identity()
    monkeypatch.setattr(catalog, "observed_source_digest", lambda: "f" * 64)
    observed = catalog.identity()
    assert observed["loaded_runtime_sha256"] == loaded == before["loaded_runtime_sha256"]
    assert observed["runtime_sha256"] != before["runtime_sha256"] and observed["source_drift"] == "detected"
    with pytest.raises(AuthorError) as error: catalog.assert_current_release()
    assert error.value.code == "RELEASE_DRIFT"


def test_loaded_runtime_includes_environment_and_manifest_identity():
    basis = catalog._STARTUP_RUNTIME_OBSERVATION
    from patchbay.authoring.util import fingerprint
    assert basis["interpreter"] and basis["dependencies"]
    changed = {**basis, "interpreter": "different-runtime"}
    assert fingerprint(changed) != catalog.STARTUP_RUNTIME_SHA256
    assert fingerprint({**basis, "dependencies": []}) != catalog.STARTUP_RUNTIME_SHA256


def test_capability_reads_reuse_source_hash_cache(monkeypatch):
    catalog.identity(); hits = catalog._source_digest.cache_info().hits
    catalog.identity(); catalog.identity()
    assert catalog._source_digest.cache_info().hits >= hits + 2


def test_restore_sync_order_and_old_logical_history(setup_author, tmp_path, monkeypatch):
    from patchbay.authoring.persistence import maintenance
    service, _, _, task, lease = setup_author
    submit(service, task, package_for(service, task, tmp_path, {"nested/a/b.txt": "exact payload\n"}), lease)
    archive = tmp_path / "backup.zip"; Maintenance(service.store).backup(archive, key="ordered-backup")
    order = []; sync = maintenance.fsync_dir; rename = maintenance.os.rename
    def observed_sync(path):
        order.append(("dir", Path(path))); return sync(path)
    def observed_rename(source, destination):
        order.append(("publish", Path(source))); return rename(source, destination)
    monkeypatch.setattr(maintenance, "fsync_dir", observed_sync)
    monkeypatch.setattr(maintenance.os, "rename", observed_rename)
    destination = tmp_path / "restored"
    restored = Maintenance.restore(archive, destination)
    publish = next(i for i, item in enumerate(order) if item[0] == "publish")
    staging = order[publish][1]
    assert ("dir", staging) in order[:publish]
    assert any(kind == "dir" and p.parent != tmp_path for kind, p in order[:publish])
    assert order[publish + 1] == ("dir", tmp_path)
    assert restored.identity["instance_id"] == service.store.identity["instance_id"]
    assert restored.source_integrity() == service.store.source_integrity()
    assert restored.one("SELECT COUNT(*) AS n FROM runs")["n"] == 0


@pytest.mark.parametrize("after_rename", [False, True])
def test_restore_sync_failure_never_reports_success_or_removes_published_target(setup_author, tmp_path, monkeypatch, after_rename):
    from patchbay.authoring.persistence import maintenance
    service, _, _, _, _ = setup_author
    archive = tmp_path / "backup.zip"; Maintenance(service.store).backup(archive, key="sync-fault-backup")
    original = archive.read_bytes(); destination = tmp_path / "restored"
    sync = maintenance.fsync_dir
    def fail(path):
        path = Path(path)
        if (after_rename and path == tmp_path) or (not after_rename and path.name == "objects"):
            raise OSError("controlled directory synchronization failure")
        return sync(path)
    monkeypatch.setattr(maintenance, "fsync_dir", fail)
    with pytest.raises((AuthorError, OSError)) as error:
        Maintenance.restore(archive, destination)
    assert destination.exists() is after_rename
    if after_rename: assert error.value.code == "RESTORE_PUBLICATION_UNCERTAIN"
    assert archive.read_bytes() == original


def test_missing_private_retention_anchor_blocks_backup(setup_author, tmp_path):
    service, _, repo, task, _ = setup_author
    git = service.repositories.git(repo["id"])
    git.run(["update-ref", "-d", "refs/author/tasks/" + task["id"] + "/origin"])
    assert git.tree(task["current_commit"])  # Object exists in ordinary mirror heads.
    with pytest.raises(AuthorError) as error:
        Maintenance(service.store).backup(tmp_path / "not-published.zip", key="missing-anchor")
    assert error.value.code == "SOURCE_STATE_INCONSISTENT"
    assert not (tmp_path / "not-published.zip").exists()


def test_relation_scan_deduplicates_many_contexts_and_preserves_historical_source(setup_author, tmp_path, monkeypatch):
    service, _, repo, task, lease = setup_author
    service.snapshots.export(task["id"])
    original = service.store.one("SELECT * FROM contexts WHERE task_id=?", (task["id"],))
    with service.store.transaction() as conn:
        for n in range(50):
            row = {**original, "id": "ctx_duplicate_" + str(n)}
            conn.execute("INSERT INTO contexts VALUES(?,?,?,?,?,?,?,?,?)", tuple(row.values()))
    submit(service, task, package_for(service, task, tmp_path, {"new.txt": "new candidate"}), lease)
    from patchbay.authoring.revisions.git_objects import Git
    calls = []; run = Git.run
    def observe(self, args, **kwargs):
        calls.append(args); return run(self, args, **kwargs)
    monkeypatch.setattr(Git, "run", observe)
    result = service.store.source_integrity()
    assert result["references"] > 50 and result["unique_commits"] == 2
    assert sum(args[0] == "log" for args in calls) == 1


def test_candidate_git_hardening_is_effective(setup_author, tmp_path):
    service, _, repo, task, lease = setup_author
    submit(service, task, package_for(service, task, tmp_path, {"new.txt": "durable source"}), lease)
    git = service.repositories.git(repo["id"])
    assert git.text(["config", "--get", "core.fsync"]) == "committed"
    assert git.text(["config", "--get", "core.fsyncMethod"]) == "fsync"


def test_drift_invalidates_existing_qualification_and_refuses_refresh(setup_author, monkeypatch):
    from patchbay.authoring.interfaces.auth import Access
    from conftest import qualification_profile
    service, *_ = setup_author
    access = Access(service.store)
    evidence = {**qualification_profile(), "model": "test-double", "client": "unit-test",
                "ordinary_chat": True, "write_supported": True,
                "probe_description": "Synthetic fixture only", "observed_at": "fixture"}
    access.qualify(evidence, actor="fixture", key="before-drift")
    assert access.qualified("author") is not None
    monkeypatch.setattr(catalog, "observed_source_digest", lambda: "f" * 64)
    assert access.qualified("author") is None
    with pytest.raises(AuthorError) as error:
        access.qualify(evidence, actor="fixture", key="after-drift")
    assert error.value.code == "RELEASE_DRIFT"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux/WSL2 execution admission")
def test_supervisor_release_mismatch_never_launches_candidate(setup_author, monkeypatch):
    from patchbay.authoring.execution import worker
    from patchbay.authoring.execution.runner import Checks
    from patchbay.authoring.feedback.export import Feedback
    service, _, _, task, lease = setup_author
    op = Checks(service.store).submit(task["id"], revision=0, key="release-mismatch", token=lease["token"], epoch=lease["epoch"], dispatch=False)
    (service.store.home / "runs" / op["id"]).mkdir()
    with service.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='launching' WHERE id=?", (op["id"],))
    monkeypatch.setattr(worker, "await_launch", lambda *args: None)
    monkeypatch.setenv("PATCHBAY_AUTHOR_EXPECTED_RELEASE", "f" * 64)
    def forbidden(*args, **kwargs): raise AssertionError("Candidate path must not be reached")
    monkeypatch.setattr(worker, "qualify", forbidden)
    assert worker.run(service.store.home, op["id"]) == 1
    receipt = Feedback(service.store).get(task["id"])["receipt"]
    assert not receipt["checks"] and receipt["error"]["code"] == "BLOCKED_ENVIRONMENT"


def test_restore_rejects_self_consistent_archive_with_false_sql_tree(setup_author, tmp_path):
    import hashlib
    import sqlite3
    import zipfile
    service, _, _, task, _ = setup_author
    original = tmp_path / "good.zip"
    Maintenance(service.store).backup(original, key="archive-corruption")
    with zipfile.ZipFile(original) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    database = tmp_path / "corrupt.sqlite3"; database.write_bytes(members["author.sqlite3"])
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE revisions SET tree_oid=? WHERE task_id=?", ("f" * 40, task["id"]))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    members["author.sqlite3"] = database.read_bytes()
    manifest = json.loads(members["BACKUP.json"])
    manifest.pop("source_relations")  # Older format-1 archive still requires the join.
    manifest["files"]["author.sqlite3"].update(sha256=hashlib.sha256(members["author.sqlite3"]).hexdigest(), bytes=len(members["author.sqlite3"]))
    members["BACKUP.json"] = json.dumps(manifest).encode()
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as archive:
        for name, data in members.items(): archive.writestr(name, data)
    target = tmp_path / "must-not-publish"
    with pytest.raises(AuthorError) as error: Maintenance.restore(corrupt, target)
    assert error.value.code == "SOURCE_STATE_INCONSISTENT" and not target.exists()


@pytest.mark.parametrize("field", ["commit", "base_commit", "resolved_base", "origin_commit"])
def test_pending_source_intent_cannot_hide_missing_commit(setup_author, tmp_path, field):
    service, _, repo, task, _ = setup_author
    service.store.begin_operation(kind="fixture.pending-source", task_id=task["id"], key="pending-" + field,
        fingerprint="fixture", payload={"repository_id": repo["id"], field: "f" * 40})
    target = tmp_path / "must-not-hide-intent.zip"
    with pytest.raises(AuthorError) as error:
        Maintenance(service.store).backup(target, key="pending-backup-" + field)
    assert error.value.code == "SOURCE_STATE_INCONSISTENT" and not target.exists()


def _second_repository(service, tmp_path, object_format):
    import subprocess
    from patchbay.authoring.models import TaskPolicy
    source = tmp_path / ("source-" + object_format); source.mkdir()
    def git(*args): return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    git("init", "--object-format=" + object_format, "-b", "main")
    git("config", "user.name", "Fixture"); git("config", "user.email", "fixture@localhost")
    (source / "unique.txt").write_text("independent source\n")
    git("add", "."); git("commit", "-m", "independent fixture")
    policy = service.repositories.get("fixture")["policy"]
    repo = service.repositories.register("second-" + object_format, source, TaskPolicy.model_validate(policy))
    task = service.tasks.create(repo["id"], "second source", key="second-" + object_format)
    return repo, task


def test_source_relation_round_trip_handles_sha256_objects(setup_author, tmp_path):
    service, *_ = setup_author
    repo, task = _second_repository(service, tmp_path, "sha256")
    assert len(task["current_commit"]) == 64
    archive = tmp_path / "sha256-backup.zip"
    Maintenance(service.store).backup(archive, key="sha256-backup")
    restored = Maintenance.restore(archive, tmp_path / "sha256-restored")
    assert restored.source_integrity() == service.store.source_integrity()
    assert restored.one("SELECT object_format FROM repositories WHERE id=?", (repo["id"],))["object_format"] == "sha256"


def test_commit_in_another_mirror_does_not_satisfy_source_custody(setup_author, tmp_path):
    service, _, _, task, _ = setup_author
    repo2, task2 = _second_repository(service, tmp_path, "sha1")
    tree2 = service.repositories.git(repo2["id"]).tree(task2["current_commit"])
    with service.store.transaction() as conn:
        conn.execute("UPDATE tasks SET current_commit=? WHERE id=?", (task2["current_commit"], task["id"]))
        conn.execute("UPDATE revisions SET commit_oid=?,tree_oid=? WHERE task_id=?", (task2["current_commit"], tree2, task["id"]))
    with pytest.raises(AuthorError) as error: service.store.source_integrity()
    assert error.value.code == "SOURCE_STATE_INCONSISTENT"
