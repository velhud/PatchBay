"""Disposable Git/application fixtures. No Codex, model API, or real GitHub writes."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from patchbay.authoring.models import Check, Profile, RuntimeLimits, TaskPolicy
from patchbay.authoring.persistence.store import Store
from patchbay.authoring.service import AuthorService


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return result.stdout.decode().strip()


@pytest.fixture
def setup_author(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Author Tests")
    git(source, "config", "user.email", "author-tests@localhost")
    (source / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (source / "tests").mkdir()
    (source / "tests/test_app.py").write_text(
        "import unittest\nfrom app import add\n\nclass AddTest(unittest.TestCase):\n"
        "    def test_addition(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (source / "README.md").write_text("Disposable application with an intentional addition defect.\n")
    git(source, "add", "."); git(source, "commit", "-m", "fixture baseline")
    (source / "local-only.txt").write_text("Uncommitted operator data must remain untouched.\n")
    Store.initialize(tmp_path / "state", RuntimeLimits(minimum_free_bytes=0, max_jobs=2, cpu_cores=2, memory_mb=4096))
    service = AuthorService(tmp_path / "state")
    profile = Profile(id="fixture", backend="trusted-local", memory_mb=256, disk_mb=128, cpu=.25,
                      checks=[Check(id="unit", argv=[sys.executable, "-m", "unittest", "discover", "-s", "tests"], timeout_seconds=30)])
    repo = service.repositories.register("fixture", source, TaskPolicy(profile=profile))
    task = service.tasks.create(repo["id"], "Implement correct addition without unrelated changes", key="task")
    lease = service.leases.claim(task["id"], label="fixture-author")
    yield service, source, repo, task, lease
    # Normal test failures should still stop only their owned disposable checks.
    from patchbay.authoring.execution.runner import Checks
    for row in service.store.rows("SELECT id,task_id,state FROM runs WHERE state IN ('queued','launching','running','cancelling')"):
        try:
            authority = service.leases.claim(row["task_id"], label="fixture-cleanup", takeover=True)
            Checks(service.store).cancel(row["id"], token=authority["token"], epoch=authority["epoch"])
        except Exception:
            continue
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not service.store.one("SELECT 1 FROM runs WHERE state IN ('launching','running','cancelling') LIMIT 1", required=False):
            break
        time.sleep(.1)
