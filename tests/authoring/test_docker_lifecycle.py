from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from patchbay.authoring.execution import process
from patchbay.authoring.execution.logs import LogSpool
from patchbay.authoring.models import Check, Profile


def _container(sequence=0):
    return {"sequence": sequence, "container_name": f"pba-test-{sequence}",
            "container_id": None, "image_id": "sha256:" + "a" * 64,
            "source_mount_path": "/fixture/source", "state": "reserved", "stages": [],
            "cleanup": {"state": "not_started", "container_id": None,
                        "rm_exit_code": None, "absent_confirmations": 0, "last_error": None}}


def test_unacknowledged_create_that_finishes_late_stays_unknown(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    run_root.mkdir()
    cidfile = run_root / "container.cid"
    marker = run_root / "late-container-created"
    container = _container()
    writer = (
        "import pathlib,time; time.sleep(1); "
        f"pathlib.Path({str(cidfile)!r}).write_text({'1'*64!r}); "
        f"pathlib.Path({str(marker)!r}).write_text('created')"
    )
    launcher = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{writer!r}],start_new_session=True,"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "time.sleep(30)"
    )
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    script = fakebin / "docker"
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      "if sys.argv[1]=='info':\n print('daemon-fixture-id')\n"
                      "else:\n " + launcher.replace("\n", "\n ") + "\n")
    script.chmod(0o700)
    original_run = process.subprocess.run

    def fast_daemon_info(args, **kwargs):
        if args[1:3] == ["info", "--format"]:
            return process.subprocess.CompletedProcess(args, 0, b"daemon-fixture-id\n", b"")
        return original_run(args, **kwargs)

    monkeypatch.setattr(process.subprocess, "run", fast_daemon_info)
    stdout, stderr = LogSpool(run_root / "out", 4096), LogSpool(run_root / "err", 4096)

    child, container_id, error = process._docker_create(
        ["docker", "create", "--cidfile", str(cidfile)], cwd=tmp_path,
        env={"PATH": str(fakebin) + ":" + os.environ.get("PATH", "")}, run_root=run_root,
        operation_id="operation-test", container_name="pba-test-0", container=container,
        known={}, identity_callback=None, stdout=stdout, stderr=stderr, timeout=.5)
    stdout.close()
    stderr.close()
    assert child is not None
    assert container_id is None
    assert error["code"] == "DOCKER_CREATE_UNKNOWN"
    assert container["cleanup"]["state"] == "unresolved"
    assert container["stages"][0]["state"] == "unknown"

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(.02)
    assert marker.read_text() == "created"
    saved = json.loads((run_root / "docker-lifecycle.json").read_text())
    assert saved["containers"][0]["container_id"] is None
    assert saved["containers"][0]["cleanup"]["state"] == "unresolved"


def test_create_stdout_is_bounded_and_oversized_ack_is_rejected(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    cidfile = run_root / "container.cid"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        "if sys.argv[1]=='info': print('daemon-fixture-id')\n"
        "else:\n"
        f" pathlib.Path({str(cidfile)!r}).write_text({'4'*64!r})\n"
        " print('x'*5000)\n"
    )
    docker.chmod(0o700)
    container = _container()
    stdout, stderr = LogSpool(run_root / "out", 4096), LogSpool(run_root / "err", 4096)

    _, container_id, error = process._docker_create(
        ["docker", "create", "--cidfile", str(cidfile)], cwd=tmp_path,
        env={"PATH": str(fakebin)}, run_root=run_root, operation_id="operation-test",
        container_name="pba-test-0", container=container, known={}, identity_callback=None,
        stdout=stdout, stderr=stderr, timeout=2)
    stdout.close()
    stderr.close()

    assert container_id is None
    assert error["code"] == "DOCKER_CREATE_UNKNOWN"
    assert container["stages"][0]["stdout_truncated"] is True
    assert container["cleanup"]["state"] == "unresolved"


def test_daemon_info_output_is_bounded_before_create(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    marker = tmp_path / "create-was-called"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        "if sys.argv[1]=='info': print('x'*10000)\n"
        f"else: pathlib.Path({str(marker)!r}).write_text('create')\n"
    )
    docker.chmod(0o700)
    container = _container()
    stdout, stderr = LogSpool(run_root / "out", 4096), LogSpool(run_root / "err", 4096)

    _, container_id, error = process._docker_create(
        ["docker", "create"], cwd=tmp_path, env={"PATH": str(fakebin)}, run_root=run_root,
        operation_id="operation-test", container_name="pba-test-0", container=container,
        known={}, identity_callback=None, stdout=stdout, stderr=stderr, timeout=2)
    stdout.close()
    stderr.close()

    assert container_id is None
    assert error["code"] == "DOCKER_DAEMON_ID_UNAVAILABLE"
    assert container["cleanup"]["state"] == "not_started"
    assert not marker.exists()


def test_cancel_after_ack_skips_start_and_cleans_by_immutable_id(tmp_path, monkeypatch):
    docker = tmp_path / "docker"
    start_marker = tmp_path / "start-was-called"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "if args[0]=='info':\n"
        " print('daemon-fixture-id')\n"
        "elif args[0]=='create':\n"
        " p=pathlib.Path(args[args.index('--cidfile')+1]); p.write_text('" + "2"*64 + "')\n"
        " print('" + "2"*64 + "')\n"
        "elif args[0]=='start':\n"
        f" pathlib.Path({str(start_marker)!r}).write_text('started')\n"
        " sys.exit(99)\n"
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ.get("PATH", ""))
    workspace, run_root = tmp_path / "source", tmp_path / "run"
    workspace.mkdir()
    run_root.mkdir()
    profile = Profile(id="docker-lifecycle", backend="docker", image="sha256:" + "a" * 64,
                      checks=[Check(id="unit", argv=["python", "-m", "unittest"])],
                      memory_mb=128, disk_mb=16, pids=8, log_bytes=1024)
    check = profile.checks[0]
    cleaned = []
    monkeypatch.setattr(process, "cleanup_container", lambda identity, **kwargs: cleaned.append(identity) or {
        "state": "proven", "remove_exit_code": 0, "absence_confirmed": True})
    monkeypatch.setattr(process, "inspect_container", lambda identity, **kwargs: {"state": "observed", "oom_killed": False})
    monkeypatch.setattr(process.identity, "cleanup_owned", lambda *args, **kwargs: {"state": "proven"})
    result = process.execute(profile, check, workspace, run_root, stage="checks", sequence=0,
                             cancelled=lambda: True)

    expected_id = "2" * 64
    assert result["state"] == "cancelled"
    assert result["cleanup"]["state"] == "proven"
    assert cleaned == [expected_id]
    assert not start_marker.exists()
    assert expected_id.encode() not in (run_root / "000-stdout.log").read_bytes()
    saved = json.loads((run_root / "docker-lifecycle.json").read_text())
    record = saved["containers"][0]
    assert record["container_id"] == expected_id
    assert [stage["name"] for stage in record["stages"]] == ["create"]
    assert record["cleanup"]["state"] == "proven_absent"


def test_create_that_consumes_check_budget_never_starts_container(tmp_path, monkeypatch):
    docker = tmp_path / "docker"
    start_marker = tmp_path / "start-was-called"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "if args[0]=='info': print('daemon-fixture-id')\n"
        "elif args[0]=='create':\n"
        " p=pathlib.Path(args[args.index('--cidfile')+1]); p.write_text('" + "5"*64 + "')\n"
        " print('" + "5"*64 + "')\n"
        "elif args[0]=='start':\n"
        f" pathlib.Path({str(start_marker)!r}).write_text('started')\n"
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ.get("PATH", ""))
    original_create = process._docker_create

    def delayed_ack(*args, **kwargs):
        result = original_create(*args, **kwargs)
        time.sleep(1.05)
        return result

    monkeypatch.setattr(process, "_docker_create", delayed_ack)
    workspace, run_root = tmp_path / "source", tmp_path / "run"
    workspace.mkdir()
    run_root.mkdir()
    profile = Profile(id="docker-create-budget", backend="docker", image="sha256:" + "a" * 64,
                      checks=[Check(id="unit", argv=["python", "-m", "unittest"], timeout_seconds=1)],
                      memory_mb=128, disk_mb=16, pids=8, log_bytes=1024)
    cleaned = []
    monkeypatch.setattr(process, "cleanup_container", lambda identity, **kwargs: cleaned.append(identity) or {
        "state": "proven", "remove_exit_code": 0, "absence_confirmed": True})
    monkeypatch.setattr(process, "inspect_container", lambda identity, **kwargs: {"state": "observed", "oom_killed": False})
    monkeypatch.setattr(process.identity, "cleanup_owned", lambda *args, **kwargs: {"state": "proven"})

    result = process.execute(profile, profile.checks[0], workspace, run_root,
                             stage="checks", sequence=0, cancelled=lambda: False)

    expected_id = "5" * 64
    assert result["state"] == "timed_out"
    assert result["error"]["code"] == "CHECK_TIMEOUT"
    assert cleaned == [expected_id]
    assert not start_marker.exists()
    saved = json.loads((run_root / "docker-lifecycle.json").read_text())
    assert saved["containers"][0]["container_id"] == expected_id
    assert [stage["name"] for stage in saved["containers"][0]["stages"]] == ["create"]


@pytest.mark.skipif(sys.platform != "linux", reason="The Author execution supervisor uses Linux procfs identities")
def test_cancel_during_attached_start_cleans_acknowledged_container(tmp_path, monkeypatch):
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys,time\n"
        "args=sys.argv[1:]\n"
        "if args[0]=='info': print('daemon-fixture-id')\n"
        "elif args[0]=='create':\n"
        " p=pathlib.Path(args[args.index('--cidfile')+1]); p.write_text('" + "3"*64 + "')\n"
        " print('" + "3"*64 + "')\n"
        "elif args[0]=='start':\n"
        " print('running',flush=True)\n"
        " time.sleep(30)\n"
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ.get("PATH", ""))
    workspace, run_root = tmp_path / "source", tmp_path / "run"
    workspace.mkdir()
    run_root.mkdir()
    profile = Profile(id="docker-running-cancel", backend="docker", image="sha256:" + "a" * 64,
                      checks=[Check(id="unit", argv=["python", "-m", "unittest"])],
                      memory_mb=128, disk_mb=16, pids=8, log_bytes=1024)
    cleaned = []
    monkeypatch.setattr(process, "cleanup_container", lambda identity, **kwargs: cleaned.append(identity) or {
        "state": "proven", "remove_exit_code": 0, "absence_confirmed": True})
    monkeypatch.setattr(process, "inspect_container", lambda identity, **kwargs: {"state": "observed", "oom_killed": False})
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 1  # First check is after create; second is inside the attached start loop.

    result = process.execute(profile, profile.checks[0], workspace, run_root,
                             stage="checks", sequence=0, cancelled=cancelled)

    expected_id = "3" * 64
    assert result["state"] == "cancelled"
    assert result["cleanup"]["state"] == "proven"
    assert cleaned == [expected_id]
    saved = json.loads((run_root / "docker-lifecycle.json").read_text())
    record = saved["containers"][0]
    assert record["container_id"] == expected_id
    assert [stage["name"] for stage in record["stages"]] == ["create", "start"]
    assert record["stages"][1]["identity"]
    assert record["cleanup"]["state"] == "proven_absent"


@pytest.mark.skipif(sys.platform != "linux", reason="The Author execution supervisor uses Linux procfs identities")
def test_trusted_local_cancellation_cleans_spawned_child(tmp_path):
    workspace, run_root = tmp_path / "source", tmp_path / "run"
    workspace.mkdir()
    run_root.mkdir()
    pidfile = tmp_path / "child.pid"
    child_code = "import os,pathlib,time; pathlib.Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]); time.sleep(30)"
    )
    profile = Profile(id="trusted-child-cancel", backend="trusted-local",
                      checks=[Check(id="unit", argv=[sys.executable, "-c", parent_code], timeout_seconds=20)],
                      memory_mb=128, disk_mb=16, pids=8, log_bytes=1024)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 1

    result = process.execute(profile, profile.checks[0], workspace, run_root,
                             stage="checks", sequence=0, cancelled=cancelled)

    assert result["state"] == "cancelled"
    assert result["cleanup"]["state"] == "proven"
    child_pid = int(pidfile.read_text())
    try:
        stat_fields = Path(f"/proc/{child_pid}/stat").read_text()
        child_state = stat_fields[stat_fields.rfind(")") + 2:].split()[0]
        assert child_state in {"Z", "X"}
    except FileNotFoundError:
        pass
