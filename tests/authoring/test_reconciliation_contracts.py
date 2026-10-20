from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from patchbay.authoring.errors import AuthorError
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.interfaces.auth import Access
from patchbay.authoring.interfaces.cli import local_lease, parser, run
from patchbay.authoring.interfaces.mcp import MCP
from patchbay.authoring.models import Check, Profile, TaskPolicy
from test_core import package_for, submit
from test_execution import wait_for


def test_cli_waits_are_bounded_and_sensitive_roles_are_explicit():
    command = parser().parse_args(["check", "task", "--key", "key", "--wait"])
    assert command.wait == 3600
    assert parser().parse_args(
        ["status", "operation", "--wait", "17"]
    ).wait == 17

    with pytest.raises(SystemExit):
        parser().parse_args(["check", "task", "--key", "key", "--wait", "0"])
    with pytest.raises(SystemExit):
        parser().parse_args(["status", "operation", "--wait", "86401"])
    with pytest.raises(SystemExit):
        parser().parse_args(["approve", "task", "--key", "key"])
    with pytest.raises(SystemExit):
        parser().parse_args(["token"])


def test_operator_lease_cache_rejects_symlink(setup_author, tmp_path):
    service, _, _, task, _ = setup_author
    cache = service.store.home / "operator-leases" / f"{task['id']}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside-lease.json"
    target.write_text("{}", encoding="utf-8")
    cache.unlink(missing_ok=True)
    cache.symlink_to(target)

    with pytest.raises(AuthorError) as error:
        local_lease(service, task["id"])
    assert error.value.code == "UNSAFE_LEASE_CACHE"
    assert target.read_text(encoding="utf-8") == "{}"


def test_author_capabilities_do_not_project_private_instance_id(setup_author):
    service, _, _, _, _ = setup_author
    Access(service.store).qualify(
        {
            "model": "test-double",
            "client": "unit-test",
            "ordinary_chat": True,
            "write_supported": True,
            "probe_description": "Disposable unit-test qualification",
            "observed_at": "fixture",
        },
        actor="test-operator",
    )
    result = MCP(service).call("reader", "author_capabilities", {})
    assert result["qualification"]["qualification_id"].startswith("qualification_")
    assert "instance_id" not in result["qualification"]
    assert service.store.identity["instance_id"] not in json.dumps(result)


def test_author_cli_import_does_not_initialize_worker_runtime():
    code = (
        "import sys; import patchbay.authoring.interfaces.cli; "
        "assert 'patchbay.workers.runtime' not in sys.modules; "
        "assert 'patchbay.workers.runtime_legacy' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_author_serve_disables_access_log(setup_author, monkeypatch):
    service, _, _, _, _ = setup_author
    observed = {}

    def fake_run(app, **kwargs):
        observed.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    args = parser().parse_args(
        ["--home", str(service.store.home), "serve", "--port", "8899"]
    )
    assert run(args) == {"stopped": True}
    assert observed["port"] == 8899
    assert observed["access_log"] is False


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_replayed_stale_check_keeps_one_run(setup_author, tmp_path):
    service, _, _, task, lease = setup_author
    checks = Checks(service.store)
    original = checks.submit(
        task["id"],
        revision=0,
        key="durable-check",
        token=lease["token"],
        epoch=lease["epoch"],
        dispatch=False,
    )
    package = package_for(service, task, tmp_path, {"added.txt": "new candidate"})
    submit(service, task, package, lease, "candidate")
    replay = checks.submit(
        task["id"],
        revision=0,
        key="durable-check",
        token=lease["token"],
        epoch=lease["epoch"],
        dispatch=False,
    )
    assert replay["id"] == original["id"]
    assert len(service.store.rows("SELECT id FROM runs")) == 1


def _configured_check(setup_author, name: str, check: Check, **profile_options):
    service, source, _, _, _ = setup_author
    profile = Profile(
        id=name,
        backend="trusted-local",
        checks=[check],
        memory_mb=256,
        disk_mb=128,
        cpu=0.25,
        **profile_options,
    )
    repository = service.repositories.register(name, source, TaskPolicy(profile=profile))
    task = service.tasks.create(repository["id"], name, key=name)
    lease = service.leases.claim(task["id"])
    operation = Checks(service.store).submit(
        task["id"],
        revision=0,
        key="run",
        token=lease["token"],
        epoch=lease["epoch"],
    )
    return service, task, lease, operation


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_cancelled_check_keeps_cancelled_feedback_classification(setup_author):
    service, task, lease, operation = _configured_check(
        setup_author,
        "reconciled-cancel",
        Check(
            id="sleep",
            argv=[sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
            timeout_seconds=40,
        ),
    )
    marker = service.store.home / "runs" / operation["id"] / "child.json"
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    Checks(service.store).cancel(
        operation["id"], token=lease["token"], epoch=lease["epoch"]
    )
    assert wait_for(service, operation)["state"] == "cancelled"
    assert Feedback(service.store).get(task["id"])["classification"] == "CANCELLED"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_noisy_log_page_retains_truncation_and_hash(setup_author):
    program = "import os\nfor _ in range(64): os.write(1, b'x' * 65536)\n"
    service, task, _, operation = _configured_check(
        setup_author,
        "reconciled-noisy",
        Check(id="noise", argv=[sys.executable, "-c", program]),
        log_bytes=4096,
    )
    done = wait_for(service, operation)
    assert done["state"] == "completed"
    output = Feedback(service.store).get(task["id"])["receipt"]["checks"][0]["stdout"]
    page = Checks(service.store).log(done["id"], output["file"])
    assert page["truncated"] is True
    assert page["sha256"] == output["sha256"]


@pytest.mark.skipif(sys.platform != "linux", reason="Requires the Linux/WSL2 Author execution supervisor")
def test_exact_git_context_is_ready_for_review(setup_author):
    service, source, _, original, _ = setup_author
    expected = original["current_commit"]
    program = (
        "import subprocess\n"
        f"assert subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip() == {expected!r}\n"
        "assert not subprocess.check_output(['git','remote'], text=True).strip()\n"
        "assert not subprocess.check_output(['git','status','--porcelain'], text=True).strip()\n"
    )
    service, task, _, operation = _configured_check(
        setup_author,
        "reconciled-git-context",
        Check(id="git", argv=[sys.executable, "-c", program]),
    )
    assert wait_for(service, operation)["state"] == "completed"
    assert Feedback(service.store).get(task["id"])["classification"] == "READY_FOR_REVIEW"
