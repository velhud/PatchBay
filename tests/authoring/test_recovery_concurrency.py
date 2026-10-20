"""Recovered acceptance, crash, publication-continuation, and concurrency regressions."""
from __future__ import annotations

import sys

import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.persistence.maintenance import Maintenance
from patchbay.authoring.publication.integration import Integration
from patchbay.authoring.revisions.git_objects import Git
from patchbay.authoring.service import AuthorService
from patchbay.authoring.util import canonical, utc_now
from conftest import git
from test_core import package_for, submit
from test_execution import wait_for


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_checks_replay_survives_later_candidate_but_new_stale_run_fails(setup_author, tmp_path):
    s, _, _, task, lease = setup_author
    op = Checks(s.store).submit(task["id"], revision=0, key="old", token=lease["token"], epoch=lease["epoch"], dispatch=False)
    submit(s, task, package_for(s, task, tmp_path, {"new.txt": "new"}), lease)
    replay = Checks(s.store).submit(task["id"], revision=0, key="old", token=lease["token"], epoch=lease["epoch"], dispatch=False)
    assert replay["id"] == op["id"]
    with pytest.raises(AuthorError) as error:
        Checks(s.store).submit(task["id"], revision=0, key="another", token=lease["token"], epoch=lease["epoch"], dispatch=False)
    assert error.value.code == "STALE_BASE"


def test_two_concurrent_submissions_admit_one_revision(setup_author, tmp_path):
    s, source, _, task, lease = setup_author
    packages = [package_for(s, task, tmp_path, {"a.txt": "a"}), package_for(s, task, tmp_path, {"b.txt": "b"})]
    def apply(number):
        try: return submit(s, task, packages[number], lease, str(number))
        except AuthorError as error: return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, (0, 1)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "STALE_BASE" in results
    assert s.tasks.get(task["id"])["current_revision"] == 1
    assert not (source / "a.txt").exists() and not (source / "b.txt").exists()


def test_crash_after_candidate_ref_reuses_pinned_content(setup_author, tmp_path, monkeypatch):
    s, _, repo, task, lease = setup_author
    package = package_for(s, task, tmp_path, {"added.txt": "durable"})
    original = Git.create_candidate
    saved = {}
    def interrupted(self, *args, **kwargs):
        saved["candidate"] = original(self, *args, **kwargs)
        raise RuntimeError("simulated process loss after pin")
    monkeypatch.setattr(Git, "create_candidate", interrupted)
    with pytest.raises(RuntimeError): submit(s, task, package, lease, "interrupted")
    assert s.tasks.get(task["id"])["current_revision"] == 0
    operation = s.store.one("SELECT * FROM operations WHERE task_id=? AND kind='package.submit'", (task["id"],))
    monkeypatch.setattr(Git, "create_candidate", original)
    result = s.materializer.recover(operation["id"], token=lease["token"], epoch=lease["epoch"])
    assert result["commit"] == saved["candidate"][0] and result["revision"] == 1
    assert s.materializer.recover(operation["id"], token=lease["token"], epoch=lease["epoch"])["commit"] == result["commit"]


def test_repository_refresh_is_explicit_and_idempotent(setup_author):
    s, source, repo, task, _ = setup_author
    initial = task["origin_commit"]
    (source / "README.md").write_text("new original commit\n")
    git(source, "add", "README.md"); git(source, "commit", "-m", "advance source")
    advanced = git(source, "rev-parse", "HEAD")
    assert s.tasks.create(repo["id"], "still pinned", key="pinned")["origin_commit"] == initial
    result = s.repositories.refresh(repo["id"], key="refresh")
    assert result["commit"] == advanced
    assert s.tasks.get(task["id"])["origin_commit"] == initial
    (source / "README.md").write_text("another original commit\n")
    git(source, "add", "README.md"); git(source, "commit", "-m", "advance source again")
    assert s.repositories.refresh(repo["id"], key="refresh")["commit"] == advanced


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_auto_check_acceptance_is_atomic_and_semantically_idempotent(setup_author, tmp_path):
    s, _, repo, _, _ = setup_author
    policy = repo["policy"]; policy["auto_check"] = True
    with s.store.transaction() as conn:
        conn.execute("UPDATE repositories SET policy=? WHERE id=?", (canonical(policy).decode(), repo["id"]))
    task = s.tasks.create(repo["id"], "auto checks", key="auto")
    lease = s.leases.claim(task["id"])
    package = package_for(s, task, tmp_path, {"app.py": "def add(a,b): return a+b\n"})
    accepted = submit(s, task, package, lease, "first")
    replay = submit(s, task, package, lease, "recompressed-semantic-repeat")
    assert replay["checks_operation_id"] == accepted["checks_operation_id"]
    assert len(s.store.rows("SELECT * FROM runs WHERE task_id=?", (task["id"],))) == 1
    assert not (s.store.home / "runs" / accepted["checks_operation_id"]).exists()
    Checks(s.store).dispatch(operation_ids=[accepted["checks_operation_id"]])
    assert wait_for(s, accepted["checks_operation_id"])["state"] == "completed"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_backup_and_gc_preserve_immutable_receipts(setup_author, tmp_path):
    s, _, _, task, lease = setup_author
    package = package_for(s, task, tmp_path, {"app.py": "def add(a,b): return a+b\n"})
    accepted = submit(s, task, package, lease)
    run = Checks(s.store).submit(task["id"], revision=1, key="run", token=lease["token"], epoch=lease["epoch"])
    assert wait_for(s, run)["state"] == "completed"
    receipt = Feedback(s.store).get(task["id"])["receipt_sha"]
    archive = tmp_path / "backup.zip"
    Maintenance(s.store).backup(archive)
    with zipfile.ZipFile(archive) as z:
        assert not any("/source/" in name for name in z.namelist())
    restored = Maintenance.restore(archive, tmp_path / "restored")
    assert Feedback(restored).get(task["id"])["receipt_sha"] == receipt
    with s.store.transaction() as conn:
        conn.execute("UPDATE runs SET updated=? WHERE id=?", (utc_now() - 3600, run["id"]))
    assert Maintenance(s.store).gc(minimum_age_seconds=60)["execution_directories_removed"] == 1
    assert not (s.store.home / "runs" / run["id"]).exists()
    assert Feedback(s.store).get(task["id"])["receipt_sha"] == receipt
    assert Checks(s.store).recover(run["id"])["state"] == "completed"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_integration_recovery_does_not_reapply_accepted_child(setup_author, tmp_path, monkeypatch):
    s, _, repo, first_task, first_lease = setup_author
    sources = []
    for number in (1, 2):
        task = first_task if number == 1 else s.tasks.create(repo["id"], "other source", key="other")
        lease = first_lease if number == 1 else s.leases.claim(task["id"])
        package = package_for(s, task, tmp_path, {"app.py": "def add(a,b): return a+b\n", f"feature-{number}.txt": str(number)})
        submit(s, task, package, lease)
        run = Checks(s.store).submit(task["id"], revision=1, key="checks", token=lease["token"], epoch=lease["epoch"])
        assert wait_for(s, run)["state"] == "completed"
        sources.append({"task_id": task["id"], "revision": 1})
    original = s.store.finish
    def interrupted(operation_id, state, **kwargs):
        if s.store.one("SELECT kind FROM operations WHERE id=?", (operation_id,))["kind"] == "integration.create":
            raise RuntimeError("lost parent terminal response")
        return original(operation_id, state, **kwargs)
    monkeypatch.setattr(s.store, "finish", interrupted)
    with pytest.raises(RuntimeError):
        Integration(s.store).create(repo["id"], sources, base=first_task["origin_commit"], goal="Combine both features", key="combine", label="integrator")
    monkeypatch.setattr(s.store, "finish", original)
    result = Integration(s.store).create(repo["id"], sources, base=first_task["origin_commit"], goal="Combine both features", key="combine", label="integrator")
    assert result["revision"] == 1 and result["classification"] == "INTEGRATION_NEEDS_CHECKS"
    assert Feedback(s.store).get(result["task_id"])["classification"] == "CHECKS_INCOMPLETE"
    assert len(s.store.rows("SELECT * FROM revisions WHERE task_id=?", (result["task_id"],))) == 2
    lease = result["lease"]
    run = Checks(s.store).submit(result["task_id"], revision=1, key="combined-checks", token=lease["token"], epoch=lease["epoch"])
    assert wait_for(s, run)["state"] == "completed"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_lost_launch_ownership_stays_fenced_and_is_not_restarted(setup_author, tmp_path):
    s, _, _, task, lease = setup_author
    op = Checks(s.store).submit(task["id"], revision=0, key="unknown", token=lease["token"], epoch=lease["epoch"], dispatch=False)
    with s.store.transaction() as conn:
        conn.execute("UPDATE runs SET state='launching',cleanup='pending' WHERE id=?", (op["id"],))
        conn.execute("UPDATE operations SET state='running',phase='launching' WHERE id=?", (op["id"],))
    assert Checks(s.store).recover(op["id"])["state"] == "unknown_execution"
    assert Checks(s.store).dispatch(operation_ids=[op["id"]])["launched"] == []
    with pytest.raises(AuthorError) as error: Maintenance(s.store).backup(tmp_path / "unsafe-backup.zip")
    assert error.value.code == "MAINTENANCE_BLOCKED"
