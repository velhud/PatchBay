from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from patchbay.jobs.process_supervisor import cleanup_proof_budget_seconds

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only tests below are skipped.
    fcntl = None  # type: ignore[assignment]


SUPERVISOR_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "patchbay"
    / "jobs"
    / "process_supervisor.py"
)


def test_cleanup_proof_budget_covers_bounded_darwin_discovery_path():
    assert cleanup_proof_budget_seconds("darwin") >= 22.0
    assert cleanup_proof_budget_seconds("linux") == 6.0


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ps discovery path")
def test_darwin_supervisor_publishes_proof_after_slow_discovery(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ps = fake_bin / "ps"
    fake_ps.write_text("#!/bin/sh\nsleep 1.6\nexit 0\n", encoding="utf-8")
    fake_ps.chmod(0o755)
    proof_file = tmp_path / "slow-discovery.proof"
    read_fd, write_fd = os.pipe()
    started = time.monotonic()
    process = subprocess.Popen(
        [
            sys.executable,
            str(SUPERVISOR_SCRIPT),
            "--gate-fd",
            str(read_fd),
            "--cleanup-proof-path",
            str(proof_file),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        pass_fds=(read_fd,),
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
    )
    os.close(read_fd)
    os.write(write_fd, b"1")
    os.close(write_fd)

    assert process.wait(timeout=cleanup_proof_budget_seconds("darwin")) == 0
    elapsed = time.monotonic() - started
    assert elapsed >= 6.0
    assert elapsed < cleanup_proof_budget_seconds("darwin")
    assert proof_file.read_text(encoding="ascii").strip() == (
        f"patchbay-supervisor-cleanup-v2:{process.pid}"
    )


def _pid_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(os.name != "posix", reason="POSIX supervisor contract")
def test_simple_process_receives_verified_cleanup_proof(tmp_path):
    proof_file = tmp_path / "simple-cleanup.proof"
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            str(SUPERVISOR_SCRIPT),
            "--gate-fd",
            str(read_fd),
            "--cleanup-proof-path",
            str(proof_file),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    os.write(write_fd, b"1")
    os.close(write_fd)

    assert process.wait(timeout=5) == 0
    assert proof_file.read_text(encoding="ascii").strip() == (
        f"patchbay-supervisor-cleanup-v2:{process.pid}"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX supervisor contract")
def test_pre_target_fork_failure_publishes_no_target_cleanup_proof(
    tmp_path, monkeypatch
):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "patchbay_process_supervisor_fork_failure_test", SUPERVISOR_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_install_subreaper", lambda: True)
    monkeypatch.setattr(
        module,
        "_fork_target",
        lambda _command: (_ for _ in ()).throw(OSError("fork exhausted")),
    )
    proof_file = tmp_path / "fork-failure-cleanup.proof"
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"1")
    os.close(write_fd)
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        assert (
            module.supervise(
                read_fd,
                [sys.executable, "-c", "pass"],
                cleanup_proof_path=str(proof_file),
            )
            == 1
        )
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    assert proof_file.read_text(encoding="ascii").strip() == (
        f"patchbay-supervisor-cleanup-v2:{os.getpid()}"
    )


@pytest.mark.skipif(
    os.name != "posix" or fcntl is None,
    reason="POSIX supervisor flock inheritance contract",
)
def test_supervisor_retains_repo_lock_after_parent_descriptor_closes(tmp_path):
    lock_path = tmp_path / "repo.lock"
    parent_handle = lock_path.open("a+b")
    fcntl.flock(parent_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    inherited_fd = os.dup(parent_handle.fileno())
    os.set_inheritable(inherited_fd, True)
    proof_file = tmp_path / "parent-crash-cleanup.proof"
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            str(SUPERVISOR_SCRIPT),
            "--gate-fd",
            str(read_fd),
            "--cleanup-proof-path",
            str(proof_file),
            "--repo-lock-fd",
            str(inherited_fd),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(0.4)",
        ],
        pass_fds=(read_fd, inherited_fd),
    )
    os.close(read_fd)
    os.close(inherited_fd)
    # Simulate PatchBay disappearing without an explicit LOCK_UN. The
    # supervisor's descriptor must remain the owner until cleanup completes.
    parent_handle.close()
    os.write(write_fd, b"1")
    os.close(write_fd)

    contender = lock_path.open("a+b")
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert process.wait(timeout=5) == 0
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
    finally:
        contender.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX supervisor contract")
def test_rapid_detached_double_fork_never_receives_false_cleanup_proof(tmp_path):
    """Uncertain descendants retain a sentinel and repo lock, never a false proof."""

    if sys.platform == "darwin":
        pytest.skip(
            "Darwin full-access workers are trusted not to erase the per-job "
            "marker; Linux subreaper coverage proves the hostile double-fork case"
        )

    attempts = 20 if sys.platform == "darwin" else 8
    false_proofs: list[int] = []
    for index in range(attempts):
        child_pid_file = tmp_path / f"child-{index}.pid"
        proof_file = tmp_path / f"cleanup-{index}.proof"
        lock_path = tmp_path / f"repo-{index}.lock"
        lock_handle = lock_path.open("a+b")
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited_fd = os.dup(lock_handle.fileno())
        os.set_inheritable(inherited_fd, True)
        target = f"""
import os, pathlib, time
first = os.fork()
if first == 0:
    second = os.fork()
    if second == 0:
        os.setsid()
        os.environ.clear()
        pathlib.Path({str(child_pid_file)!r}).write_text(str(os.getpid()), encoding='ascii')
        time.sleep(30)
    os._exit(0)
os.waitpid(first, 0)
path = pathlib.Path({str(child_pid_file)!r})
deadline = time.time() + 1
while not path.exists() and time.time() < deadline:
    time.sleep(0.001)
"""
        read_fd, write_fd = os.pipe()
        process = subprocess.Popen(
            [
                sys.executable,
                str(SUPERVISOR_SCRIPT),
                "--gate-fd",
                str(read_fd),
                "--cleanup-proof-path",
                str(proof_file),
                "--repo-lock-fd",
                str(inherited_fd),
                "--",
                sys.executable,
                "-c",
                target,
            ],
            pass_fds=(read_fd, inherited_fd),
        )
        os.close(read_fd)
        os.close(inherited_fd)
        lock_handle.close()
        os.write(write_fd, b"1")
        os.close(write_fd)
        deadline = time.monotonic() + 5
        while not proof_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert proof_file.exists(), "supervisor did not publish cleanup disposition"
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 0.5
        while _pid_live(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        child_live = _pid_live(child_pid)
        proof_text = (
            proof_file.read_text(encoding="ascii").strip()
            if proof_file.exists()
            else ""
        )
        cleanup_proven = proof_text.startswith("patchbay-supervisor-cleanup-v2:")
        assert process.wait(timeout=5) == 0
        if child_live and cleanup_proven:
            false_proofs.append(child_pid)
        if not cleanup_proven:
            prefix = (
                f"patchbay-supervisor-cleanup-unproven-v2:{process.pid}:"
            )
            assert proof_text.startswith(prefix)
            sentinel_pid = int(proof_text.removeprefix(prefix))
            assert _pid_live(sentinel_pid)
            if fcntl is not None:
                contender = lock_path.open("a+b")
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(
                            contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                finally:
                    contender.close()
            os.kill(sentinel_pid, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while _pid_live(sentinel_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _pid_live(sentinel_pid)
            if fcntl is not None:
                released = lock_path.open("a+b")
                try:
                    fcntl.flock(
                        released.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    fcntl.flock(released.fileno(), fcntl.LOCK_UN)
                finally:
                    released.close()
        if child_live:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert false_proofs == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX supervisor contract")
def test_fork_target_stays_gated_until_tracker_is_returned(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "patchbay_process_supervisor_test", SUPERVISOR_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    started = tmp_path / "started"
    pid, tracker, gate_fd = module._fork_target(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(started)!r}).write_text('yes')",
        ]
    )
    try:
        time.sleep(0.05)
        assert not started.exists()
        os.write(gate_fd, b"1")
        os.close(gate_fd)
        gate_fd = -1
        waited, status = os.waitpid(pid, 0)
        assert waited == pid
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        assert started.read_text(encoding="utf-8") == "yes"
    finally:
        if gate_fd >= 0:
            os.close(gate_fd)
        tracker.close()
