from __future__ import annotations

import json
import os
import sys
import time
import zipfile

import pytest
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.models import Check, Profile, TaskPolicy
from patchbay.authoring.persistence.store import TERMINAL
from patchbay.authoring.util import canonical
from test_core import package_for, submit


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")

def wait_for(service, operation, *, timeout=40):
    operation_id = operation["id"] if isinstance(operation, dict) else operation
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = service.store.operation(operation_id)
        if observed["state"] in TERMINAL:
            return observed
        time.sleep(.05)
    root = service.store.home / "runs" / operation_id
    log = (root / "supervisor.log").read_text(errors="replace")[-5000:] if (root / "supervisor.log").exists() else "No supervisor log"
    pytest.fail(f"Check did not finish: {service.store.operation(operation_id)}\n{log}")


def test_real_failure_feedback_correction_pass_and_isolated_candidate(setup_author, tmp_path):
    service, source, repo, task, lease = setup_author
    first = Checks(service.store).submit(task["id"], revision=0, key="first-run", token=lease["token"], epoch=lease["epoch"])
    failed = wait_for(service, first)
    assert failed["state"] == "failed", failed
    feedback = Feedback(service.store).get(task["id"])
    assert feedback["classification"] == "CHECKS_FAILED"
    assert "AssertionError" in Feedback(service.store).markdown(feedback)
    package = package_for(service, task, tmp_path, {"app.py": "def add(a, b):\n    return a + b\n"})
    accepted = submit(service, task, package, lease)
    second = Checks(service.store).submit(task["id"], revision=accepted["revision"], key="corrected-run", token=lease["token"], epoch=lease["epoch"])
    passed = wait_for(service, second)
    assert passed["state"] == "completed", passed
    feedback = Feedback(service.store).get(task["id"])
    assert feedback["classification"] == "READY_FOR_REVIEW"
    assert feedback["receipt"]["commit"] == accepted["commit"]
    assert feedback["receipt"]["cleanup"] == "proven"
    assert feedback["receipt"]["delegated_inference_by_author_service"] is False
    assert "a - b" in (source / "app.py").read_text()
    exported = Feedback(service.store).export(task["id"])
    with zipfile.ZipFile(service.store.blobs.path(exported["blob_sha"])) as archive:
        assert {"CONTINUE.md", "FEEDBACK.md", "RESULT.json", "task-context.zip"} <= set(archive.namelist())
        assert json.loads(archive.read("RESULT.json"))["commit"] == accepted["commit"]


def test_read_only_queries_never_launch_queued_checks(setup_author):
    service, _, _, task, lease = setup_author
    op = Checks(service.store).submit(task["id"], revision=0, key="queued", token=lease["token"], epoch=lease["epoch"], dispatch=False)
    for _ in range(20):
        service.tasks.get(task["id"]); service.store.operation(op["id"]); Feedback(service.store).get(task["id"])
    assert not (service.store.home / "runs" / op["id"]).exists()
    assert service.store.operation(op["id"])["state"] == "queued"


@pytest.mark.skipif(os.environ.get("PATCHBAY_AUTHOR_LONG_TESTS") != "1", reason="Explicit 185-second execution qualification; enable PATCHBAY_AUTHOR_LONG_TESTS=1")
def test_check_longer_than_the_previous_three_minute_limit(setup_author):
    service, source, _, _, _ = setup_author
    profile = Profile(id="long-run", backend="trusted-local", memory_mb=256, disk_mb=128, cpu=.25,
                      checks=[Check(id="long", argv=[sys.executable, "-c", "import time; time.sleep(185); print('finished')"], timeout_seconds=210)])
    repo = service.repositories.register("long-run", source, TaskPolicy(profile=profile))
    task = service.tasks.create(repo["id"], "Qualify an actual command longer than three minutes", key="long")
    lease = service.leases.claim(task["id"])
    op = Checks(service.store).submit(task["id"], revision=0, key="run", token=lease["token"], epoch=lease["epoch"])
    completed = wait_for(service, op, timeout=240)
    assert completed["state"] == "completed"
    receipt = Feedback(service.store).get(task["id"])["receipt"]
    assert receipt["checks"][0]["duration_seconds"] >= 185


def test_queued_cancel_has_no_process_effect(setup_author):
    service, _, _, task, lease = setup_author
    op = Checks(service.store).submit(task["id"], revision=0, key="cancel-queued", token=lease["token"], epoch=lease["epoch"], dispatch=False)
    result = Checks(service.store).cancel(op["id"], token=lease["token"], epoch=lease["epoch"])
    assert result["state"] == "cancelled"
    assert Checks(service.store).dispatch(operation_ids=[op["id"]])["launched"] == []
    assert not (service.store.home / "runs" / op["id"]).exists()
