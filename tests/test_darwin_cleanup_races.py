"""Dead children must not become false cleanup fences; live unknowns still do."""
import errno
import subprocess
import sys
from types import SimpleNamespace

import pytest

from patchbay.jobs import process_supervisor as supervisor
from patchbay.jobs.executor import JobExecutor


class EventQueue:
    def __init__(self, events):
        self.events = events

    def control(self, *_args):
        events, self.events = self.events, []
        return events


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("stage", ["tracked_child", "fork_identity", "registration"])
def test_child_exit_races_preserve_unknown_live_fence(monkeypatch, live, stage):
    for name, value in [("KQ_NOTE_CHILD", 1), ("KQ_NOTE_FORK", 2), ("KQ_NOTE_EXIT", 4), ("KQ_NOTE_TRACKERR", 8)]:
        monkeypatch.setattr(supervisor.select, name, value, raising=False)
    tracker = supervisor._DarwinTracker.__new__(supervisor._DarwinTracker)
    flag = supervisor.select.KQ_NOTE_CHILD if stage == "tracked_child" else supervisor.select.KQ_NOTE_FORK
    tracker._queue = EventQueue([SimpleNamespace(ident=123, fflags=flag)])
    tracker._native_child_tracking = False
    tracker.tracking_uncertain = False
    tracker.reason_codes = set()
    tracker.fork_seen = False
    tracker.tracked = {}
    monkeypatch.setattr(tracker, "_children", lambda _pid: {124})
    monkeypatch.setattr(supervisor, "_pid_live", lambda _pid: live)
    monkeypatch.setattr(supervisor, "_process_identity", lambda _pid: "birth:1" if stage == "registration" else "")

    def missing_at_registration(_pid):
        raise ProcessLookupError(errno.ESRCH, "child exited before registration")

    monkeypatch.setattr(tracker, "_register", missing_at_registration)
    tracker.poll()

    reason = {"tracked_child": "tracked_child_identity_unavailable", "fork_identity": "fork_child_identity_unavailable", "registration": "child_tracker_registration_failed"}[stage]
    assert tracker.tracking_uncertain is live
    assert tracker.reason_codes == ({reason} if live else set())


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Darwin process-birth identity")
def test_executor_uses_real_darwin_birth_identity():
    process = subprocess.Popen(["/bin/sleep", "5"])
    try:
        identity = supervisor._process_identity(process.pid)
        assert identity.startswith("darwin:")
        executor = JobExecutor.__new__(JobExecutor)
        assert executor._process_identity(process.pid) == identity
        process.terminate()
        process.wait(timeout=5)
        assert not supervisor._pid_live(process.pid)
        assert executor._process_identity(process.pid) is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
