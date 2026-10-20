"""Compatibility-preserving process supervisor signal-mask correction.

The original implementation is retained in :mod:`process_supervisor_legacy`.
The parent deliberately blocks TERM/INT across ``fork`` to close a launch race,
but an exec target must not inherit that temporary mask. This module replaces
the fork helper and patches the legacy supervisor's global reference so all
existing callers keep the same API and lifecycle machinery.
"""
from __future__ import annotations

import os
import signal

from .process_supervisor_legacy import *  # noqa: F401,F403
from . import process_supervisor_legacy as _legacy


def _restore_target_signal_mask() -> None:
    """Remove only the launch-race signals blocked by the supervisor."""
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(
            signal.SIG_UNBLOCK,
            {signal.SIGTERM, signal.SIGINT},
        )


def _fork_target(command: list[str]):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(write_fd)
            gate = os.read(read_fd, 1)
            os.close(read_fd)
            if gate != b"1":
                os._exit(125)
            _restore_target_signal_mask()
            os.execvpe(command[0], command, os.environ)
        except BaseException as error:
            try:
                os.write(
                    2,
                    f"PatchBay process supervisor exec failed: {error}\n".encode(),
                )
            except OSError:
                pass
            os._exit(127)
    os.close(read_fd)
    try:
        tracker = _legacy._DarwinTracker(pid)
    except BaseException:
        os.close(write_fd)
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, InterruptedError):
            pass
        raise
    return pid, tracker, write_fd


# ``supervise`` resolves this helper from the legacy module's globals.
_legacy._fork_target = _fork_target
