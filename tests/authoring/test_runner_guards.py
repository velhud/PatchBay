"""Recovered execution regressions, adapted to the completed independent core."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from patchbay.authoring.execution.identity import same_process
from patchbay.authoring.execution.runner import Checks
from patchbay.authoring.feedback.export import Feedback
from patchbay.authoring.models import Check, Profile, TaskPolicy
from test_execution import wait_for


def configured(setup_author, name, checks, *, setup=None, **profile_options):
    service, source, _, _, _ = setup_author
    options = {"memory_mb": 256, "disk_mb": 128, "cpu": .25, **profile_options}
    profile = Profile(id=name, backend="trusted-local", checks=checks, setup=setup or [], **options)
    repo = service.repositories.register(name, source, TaskPolicy(profile=profile))
    task = service.tasks.create(repo["id"], name, key=name)
    lease = service.leases.claim(task["id"])
    return service, source, task, lease


def launch(service, task, lease):
    return Checks(service.store).submit(task["id"], revision=0, key="run", token=lease["token"], epoch=lease["epoch"])


def test_timeout_has_real_terminal_and_cleanup_evidence(setup_author):
    s, _, t, lease = configured(setup_author, "timeout", [Check(id="sleep", argv=[sys.executable, "-c", "import time; time.sleep(15)"], timeout_seconds=1)])
    assert wait_for(s, launch(s, t, lease))["state"] == "timed_out"
    feedback = Feedback(s.store).get(t["id"])
    assert feedback["classification"] == "TIMED_OUT"
    assert feedback["receipt"]["cleanup"] == "proven"
    assert feedback["verification_receipt"]["outcome"] != "passed"


def test_cancel_waits_for_owned_process_cleanup(setup_author):
    s, _, t, lease = configured(setup_author, "cancel", [Check(id="sleep", argv=[sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=40)])
    op = launch(s, t, lease)
    child = s.store.home / "runs" / op["id"] / "child.json"
    deadline = time.monotonic() + 20
    while not child.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert child.exists()
    Checks(s.store).cancel(op["id"], token=lease["token"], epoch=lease["epoch"])
    assert wait_for(s, op)["state"] == "cancelled"
    receipt = Feedback(s.store).get(t["id"])["receipt"]
    assert receipt["cleanup"] == "proven"
    assert Checks(s.store).cancel(op["id"], token=lease["token"], epoch=lease["epoch"])["state"] == "cancelled"


def test_noisy_output_is_spooled_and_bounded(setup_author):
    program = "import os\nfor _ in range(256): os.write(1,b'x'*65536)\n"
    s, _, t, lease = configured(setup_author, "noisy", [Check(id="noise", argv=[sys.executable, "-c", program])], log_bytes=4096)
    assert wait_for(s, launch(s, t, lease))["state"] == "completed"
    output = Feedback(s.store).get(t["id"])["receipt"]["checks"][0]["stdout"]
    assert output["observed_bytes"] == 256 * 65536
    assert output["stored_bytes"] == 2048 and output["truncated"] is True
    assert len(output["tail"].encode()) <= 16384
    assert len(s.store.blobs.read(output["sha256"])) == 2048


def test_mutating_check_does_not_qualify_original_candidate(setup_author):
    program = "from pathlib import Path; Path('app.py').write_text('def add(a,b): return a+b\\n')"
    s, source, t, lease = configured(setup_author, "mutator", [Check(id="mutate", argv=[sys.executable, "-c", program])])
    wait_for(s, launch(s, t, lease))
    feedback = Feedback(s.store).get(t["id"])
    assert feedback["classification"] == "CHECKS_INCOMPLETE"
    assert feedback["receipt"]["source_changes_during_checks"] == [{"path": "app.py", "reason": "tracked_source_changed"}]
    assert feedback["verification_receipt"]["source_unchanged"] is False
    assert "a - b" in (source / "app.py").read_text()


def test_check_sees_exact_commit_no_remote_and_independent_objects(setup_author):
    s, source, repo, original, _ = setup_author
    commit = original["current_commit"]
    program = ("import subprocess,os\nfrom pathlib import Path\n"
               "assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==" + repr(commit) + "\n"
               "assert not subprocess.check_output(['git','remote'],text=True).strip()\n"
               "assert not subprocess.check_output(['git','status','--porcelain'],text=True).strip()\n")
    s, _, t, lease = configured(setup_author, "git-view", [Check(id="git", argv=[sys.executable, "-c", program])])
    op = launch(s, t, lease)
    assert wait_for(s, op)["state"] == "completed"
    copied = s.store.home / "runs" / op["id"] / "source"
    source_objects = {path.relative_to(source / ".git/objects").as_posix(): path for path in (source / ".git/objects").rglob("*") if path.is_file()}
    for path in (copied / ".git/objects").rglob("*"):
        relative = path.relative_to(copied / ".git/objects").as_posix()
        if path.is_file() and relative in source_objects:
            assert (path.stat().st_dev, path.stat().st_ino) != (source_objects[relative].stat().st_dev, source_objects[relative].stat().st_ino)


def test_detached_descendant_cleanup_does_not_touch_unrelated_process(setup_author):
    program = """import json,os,time
from pathlib import Path
Path('.author-reports').mkdir(exist_ok=True)
if os.fork() == 0:
    os.setsid()
    if os.fork() != 0: os._exit(0)
    pid=os.getpid()
    raw=Path(f'/proc/{pid}/stat').read_text()
    fields=raw[raw.rfind(')')+2:].split()
    record={'pid':pid,'start':fields[19],'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    Path('.author-reports/detached.json').write_text(json.dumps(record))
    time.sleep(30)
    os._exit(0)
time.sleep(.5)
"""
    s, _, t, lease = configured(setup_author, "detached", [Check(id="fork", argv=[sys.executable, "-c", program])])
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        op = launch(s, t, lease)
        assert wait_for(s, op)["state"] == "completed"
        child = json.loads((s.store.home / "runs" / op["id"] / "source/.author-reports/detached.json").read_text())
        assert not same_process(child)
        assert unrelated.poll() is None
        receipt = Feedback(s.store).get(t["id"])["receipt"]
        assert receipt["checks"][0]["cleanup"]["state"] == "proven"
        assert receipt["checks"][0]["cleanup"]["owned_processes"] >= 2
    finally:
        unrelated.terminate(); unrelated.wait(timeout=5)


def junit_check(name, test_name=None):
    if test_name is None:
        program = "pass"
    else:
        xml = '<testsuite tests="1"><testcase name="' + test_name + '"/></testsuite>'
        program = "from pathlib import Path; Path('.author-reports').mkdir(exist_ok=True); Path('.author-reports/shared.xml').write_text(" + repr(xml) + ")"
    return Check(id=name, argv=[sys.executable, "-c", program], report="junit", report_path=".author-reports/shared.xml")


def test_stale_report_is_removed_before_each_check(setup_author):
    s, _, t, lease = configured(setup_author, "stale-report", [junit_check("first", "first"), junit_check("second")])
    wait_for(s, launch(s, t, lease))
    feedback = Feedback(s.store).get(t["id"])
    assert feedback["classification"] == "CHECKS_INCOMPLETE"
    first, second = feedback["receipt"]["checks"]
    assert first["verification"] == "passed" and second["verification"] == "incomplete"
    assert s.store.blobs.read(first["report_sha256"])


def test_same_report_path_preserves_distinct_per_check_content(setup_author):
    s, _, t, lease = configured(setup_author, "separate-reports", [junit_check("first", "one"), junit_check("second", "two")])
    assert wait_for(s, launch(s, t, lease))["state"] == "completed"
    first, second = Feedback(s.store).get(t["id"])["receipt"]["checks"]
    assert first["report_sha256"] != second["report_sha256"]
    assert b'one' in s.store.blobs.read(first["report_sha256"])
    assert b'two' in s.store.blobs.read(second["report_sha256"])


def test_setup_failure_is_not_a_source_test_failure(setup_author):
    s, _, t, lease = configured(setup_author, "setup-failure", [Check(id="unit", argv=[sys.executable, "-c", "pass"])],
                               setup=[Check(id="setup", argv=[sys.executable, "-c", "raise SystemExit(7)"])])
    assert wait_for(s, launch(s, t, lease))["state"] == "blocked"
    feedback = Feedback(s.store).get(t["id"])
    assert feedback["classification"] == "BLOCKED_ENVIRONMENT" and feedback["receipt"]["checks"] == []
