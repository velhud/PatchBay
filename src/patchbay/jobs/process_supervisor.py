"""Per-job POSIX process supervisor used by :mod:`patchbay.jobs.executor`.

The supervisor is deliberately tiny and dependency-free.  PatchBay starts it
behind a parent-controlled launch gate, records the supervisor PID/PGID, and
only then lets it fork the real Codex command.  Linux child-subreaper semantics
and Darwin kqueue process tracking keep detached descendants attributable even
when they create a new session and remove PatchBay's environment marker.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable


_PR_SET_CHILD_SUBREAPER = 36
_TERM_GRACE_SECONDS = 0.25
_KILL_GRACE_SECONDS = 1.0
_DISCOVERY_COMMAND_TIMEOUT_SECONDS = 2.0
_DARWIN_DISCOVERY_SOURCES = 2
_KQ_EV_RECEIPT = 0x0040
_KQ_EV_ERROR = 0x4000
_JOB_PROCESS_MARKER_ENV = "PATCHBAY_JOB_MARKER"
_QUIESCENT_SCANS = 3
_QUIESCENT_SCAN_SECONDS = 0.02


def cleanup_proof_budget_seconds(platform_name: str = sys.platform) -> float:
    """Return the supervisor's bounded proof budget for one host platform."""

    if platform_name != "darwin":
        return 6.0
    # Worst case: a TERM scan sees work, the first KILL scan sees it gone,
    # then all quiescent scans prove continued absence. Each Darwin ownership
    # scan may use both bounded ``ps`` discovery sources sequentially.
    ownership_scans = 2 + _QUIESCENT_SCANS
    discovery_budget = (
        ownership_scans
        * _DARWIN_DISCOVERY_SOURCES
        * _DISCOVERY_COMMAND_TIMEOUT_SECONDS
    )
    return (
        discovery_budget
        + _TERM_GRACE_SECONDS
        + _KILL_GRACE_SECONDS
        + (_QUIESCENT_SCANS * _QUIESCENT_SCAN_SECONDS)
        + 1.0
    )


class _DarwinBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _process_identity(pid: int) -> str:
    """Return an exact process-start identity so recycled PIDs are never signalled."""

    if pid <= 0:
        return ""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        text = proc_stat.read_text(encoding="utf-8")
        _, separator, suffix = text.rpartition(")")
        fields = suffix.strip().split() if separator else []
        if len(fields) > 19:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="utf-8"
            ).strip()
            return f"linux:{boot_id}:{fields[19]}"
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            library = ctypes.CDLL(
                ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
            )
            function = library.proc_pidinfo
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            function.restype = ctypes.c_int
            value = _DarwinBSDInfo()
            size = ctypes.sizeof(value)
            written = int(function(pid, 3, 0, ctypes.byref(value), size))
        except (AttributeError, OSError, TypeError, ValueError):
            return ""
        if written == size and int(value.pbi_pid) == pid:
            return (
                f"darwin:{int(value.pbi_start_tvsec)}:"
                f"{int(value.pbi_start_tvusec)}"
            )
    return ""


class _StopRequested(BaseException):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = int(signum)


def _install_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (AttributeError, OSError):
        return False
    return result == 0


def _parent_snapshot() -> dict[int, int] | None:
    proc = Path("/proc")
    parents: dict[int, int] = {}
    complete = True
    if proc.is_dir():
        try:
            entries = list(proc.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat_text = (entry / "stat").read_text(encoding="utf-8")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                complete = False
                continue
            _, separator, suffix = stat_text.rpartition(")")
            fields = suffix.strip().split() if separator else []
            if len(fields) < 2 or fields[0] == "Z":
                continue
            try:
                parents[int(entry.name)] = int(fields[1])
            except ValueError:
                complete = False
        return parents if complete else None

    try:
        observed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,stat="],
            capture_output=True,
            text=True,
            timeout=_DISCOVERY_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if observed.returncode != 0:
        return None
    for line in observed.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            complete = False
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
        except ValueError:
            complete = False
            continue
        if not fields[2].startswith("Z"):
            parents[pid] = ppid
    return parents if complete else None


def _descendants(root_pid: int) -> set[int] | None:
    parents = _parent_snapshot()
    if parents is None:
        return None
    found: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid
            for pid, parent in parents.items()
            if parent in frontier and pid not in found and pid != root_pid
        }
        if not children:
            break
        found.update(children)
        frontier = children
    return found


def _pid_live(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _, separator, suffix = stat_text.rpartition(")")
        fields = suffix.strip().split() if separator else []
        if fields and fields[0] == "Z":
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _DarwinTracker:
    def __init__(self, root_pid: int):
        self._queue = None
        self._native_child_tracking: bool | None = None
        self.tracking_uncertain = False
        self.fork_seen = False
        self.job_marker = str(os.environ.get(_JOB_PROCESS_MARKER_ENV) or "").strip()
        root_identity = _process_identity(root_pid)
        self.tracked: dict[int, str] = (
            {root_pid: root_identity} if root_identity else {}
        )
        if not hasattr(select, "kqueue"):
            if sys.platform == "darwin":
                self.tracking_uncertain = True
            return
        try:
            queue = select.kqueue()
            self._queue = queue
            self._register(root_pid, require_native_tracking=True)
        except (AttributeError, OSError):
            if sys.platform == "darwin":
                self.tracking_uncertain = True
            if self._queue is not None:
                self._queue.close()
                self._queue = None
            return

    def _register(self, pid: int, *, require_native_tracking: bool = False) -> None:
        queue = self._queue
        if queue is None:
            return
        native_flags = (
            select.KQ_NOTE_EXIT
            | select.KQ_NOTE_FORK
            | getattr(select, "KQ_NOTE_TRACK", 0)
            | getattr(select, "KQ_NOTE_TRACKERR", 0)
        )
        if require_native_tracking:
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=(
                    select.KQ_EV_ADD
                    | select.KQ_EV_ENABLE
                    | _KQ_EV_RECEIPT
                ),
                fflags=native_flags,
            )
            try:
                receipts = queue.control([event], 1, 0)
            except OSError:
                receipts = []
            self._native_child_tracking = bool(
                getattr(select, "KQ_NOTE_TRACK", 0)
                and receipts
                and int(receipts[0].flags) & _KQ_EV_ERROR
                and int(receipts[0].data) == 0
            )
            if self._native_child_tracking:
                return

        if not self._native_child_tracking:
            # Darwin currently exposes the NOTE_TRACK constants through
            # Python even on kernels that reject them. Basic NOTE_FORK is
            # sufficient until a fork is actually observed; only a missed or
            # ambiguous fork event makes the cleanup proof uncertain.
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_EXIT | select.KQ_NOTE_FORK,
            )
            queue.control([event], 0, 0)

    @staticmethod
    def _children(pid: int) -> set[int] | None:
        if sys.platform != "darwin":
            return set()
        try:
            library = ctypes.CDLL(
                ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
            )
            function = library.proc_listchildpids
            function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            function.restype = ctypes.c_int
            capacity = 4096
            values = (ctypes.c_int * capacity)()
            count = int(function(int(pid), values, ctypes.sizeof(values)))
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        if count < 0:
            return None
        if count == 0:
            return set()
        return {int(values[index]) for index in range(min(count, capacity))}

    def poll(self, timeout: float = 0.0) -> None:
        queue = self._queue
        if queue is None:
            if timeout:
                time.sleep(timeout)
            return
        try:
            events = queue.control(None, 256, max(0.0, timeout))
        except (OSError, ValueError):
            self.tracking_uncertain = True
            return
        for event in events:
            pid = int(event.ident)
            flags = int(event.fflags)
            if flags & getattr(select, "KQ_NOTE_TRACKERR", 0):
                self.tracking_uncertain = True
            if flags & getattr(select, "KQ_NOTE_CHILD", 0):
                # NOTE_TRACK installs the same event on descendants before the
                # child can execute user code. This is the lossless Darwin
                # ownership boundary; ``event.ident`` is the child PID.
                identity = _process_identity(pid)
                if identity:
                    self.tracked[pid] = identity
                else:
                    self.tracking_uncertain = True
            if flags & select.KQ_NOTE_FORK:
                self.fork_seen = True
                if not self._native_child_tracking:
                    # Darwin does not provide NOTE_TRACK on supported PatchBay
                    # hosts. Register every child that is still attributable and
                    # use the per-job environment marker as the supplementary
                    # detached-process boundary. Full-access workers are trusted
                    # processes, not hostile code expected to erase that marker.
                    children = self._children(pid)
                    if children is None:
                        self.tracking_uncertain = True
                    for child_pid in children or set():
                        identity = _process_identity(child_pid)
                        if not identity:
                            self.tracking_uncertain = True
                            continue
                        if self.tracked.get(child_pid) == identity:
                            continue
                        self.tracked[child_pid] = identity
                        try:
                            self._register(child_pid)
                        except OSError:
                            self.tracking_uncertain = True
            if flags & select.KQ_NOTE_EXIT:
                self.tracked.pop(pid, None)

    def close(self) -> None:
        if self._queue is not None:
            self._queue.close()


def _signal_pids(pids: Iterable[int], sig: signal.Signals) -> None:
    for pid in sorted(set(pids), reverse=True):
        if not _pid_live(pid):
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            continue


def _reap_children() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if pid <= 0:
            return


def _owned_pids(supervisor_pid: int, tracker: _DarwinTracker) -> set[int]:
    tracker.poll(0)
    observed = _descendants(supervisor_pid)
    if observed is None:
        tracker.tracking_uncertain = True
    owned = {
        pid
        for pid, identity in tracker.tracked.items()
        if identity and _process_identity(pid) == identity
    }
    if observed is not None:
        owned.update(observed)
        for pid in observed:
            identity = _process_identity(pid)
            if identity:
                tracker.tracked[pid] = identity
    marked = _marker_pids(tracker.job_marker)
    if marked is None:
        if sys.platform == "darwin" and tracker.fork_seen:
            tracker.tracking_uncertain = True
    else:
        owned.update(marked)
        for pid in marked:
            identity = _process_identity(pid)
            if identity:
                tracker.tracked[pid] = identity
    owned.discard(supervisor_pid)
    return {pid for pid in owned if _pid_live(pid)}


def _marker_pids(marker: str) -> set[int] | None:
    """Return live same-user processes retaining this job's exact marker."""

    value = str(marker or "").strip()
    if not value:
        return set()
    token = f"{_JOB_PROCESS_MARKER_ENV}={value}".encode("utf-8")
    proc = Path("/proc")
    if proc.is_dir():
        result: set[int] = set()
        complete = True
        try:
            entries = list(proc.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                environ = (entry / "environ").read_bytes()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            except OSError:
                complete = False
                continue
            if token in environ.split(b"\0"):
                result.add(int(entry.name))
        return result if complete else None

    try:
        observed = subprocess.run(
            ["ps", "eww", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=_DISCOVERY_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if observed.returncode != 0:
        return None
    text_token = token.decode("utf-8")
    result = set()
    for line in observed.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or text_token not in fields[1]:
            continue
        try:
            result.add(int(fields[0]))
        except ValueError:
            return None
    return result


def _terminate_owned(supervisor_pid: int, tracker: _DarwinTracker) -> bool:
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while True:
        owned = _owned_pids(supervisor_pid, tracker)
        if not owned:
            absence_stable = True
            for _ in range(_QUIESCENT_SCANS):
                tracker.poll(_QUIESCENT_SCAN_SECONDS)
                _reap_children()
                if _owned_pids(supervisor_pid, tracker):
                    absence_stable = False
                    break
            if absence_stable:
                return not tracker.tracking_uncertain
            continue
        _signal_pids(owned, signal.SIGTERM)
        if time.monotonic() >= deadline:
            break
        tracker.poll(min(0.02, max(0.0, deadline - time.monotonic())))
        _reap_children()

    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while True:
        owned = _owned_pids(supervisor_pid, tracker)
        if not owned:
            absence_stable = True
            for _ in range(_QUIESCENT_SCANS):
                tracker.poll(_QUIESCENT_SCAN_SECONDS)
                _reap_children()
                if _owned_pids(supervisor_pid, tracker):
                    absence_stable = False
                    break
            if absence_stable:
                return not tracker.tracking_uncertain
            continue
        _signal_pids(owned, signal.SIGKILL)
        if time.monotonic() >= deadline:
            return False
        tracker.poll(min(0.02, max(0.0, deadline - time.monotonic())))
        _reap_children()


def _fork_target(command: list[str]) -> tuple[int, _DarwinTracker, int]:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(write_fd)
            gate = os.read(read_fd, 1)
            os.close(read_fd)
            if gate != b"1":
                os._exit(125)
            os.execvpe(command[0], command, os.environ)
        except BaseException as error:
            try:
                os.write(2, f"PatchBay process supervisor exec failed: {error}\n".encode())
            except OSError:
                pass
            os._exit(127)
    os.close(read_fd)
    try:
        tracker = _DarwinTracker(pid)
    except BaseException:
        os.close(write_fd)
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, InterruptedError):
            pass
        raise
    return pid, tracker, write_fd


def _exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _write_cleanup_proof(path_value: str, supervisor_pid: int) -> None:
    _write_supervisor_state(
        path_value,
        f"patchbay-supervisor-cleanup-v2:{supervisor_pid}",
    )


def _write_supervisor_state(path_value: str, record: str) -> None:
    """Atomically publish one durable supervisor lifecycle record."""

    if not path_value:
        return
    path = Path(path_value).expanduser().resolve(strict=False)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{record}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_cleanup_uncertainty(
    path_value: str, supervisor_pid: int, sentinel_pid: int
) -> None:
    """Persist a fail-closed terminal record without claiming descendants are gone."""

    if not path_value:
        return
    path = Path(path_value).expanduser().resolve(strict=False)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(
                "patchbay-supervisor-cleanup-unproven-v2:"
                f"{supervisor_pid}:{sentinel_pid}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _spawn_cleanup_uncertainty_sentinel(
    path_value: str,
    supervisor_pid: int,
    *,
    repo_lock_fd: int = -1,
) -> int:
    """Fail closed while descendant ownership cannot be proven.

    Darwin cannot reliably rediscover a marker-stripped rapid double-fork from
    an unprivileged process after the ancestry edge has disappeared.  Exiting
    here would both erase the live ownership witness and, after a PatchBay
    crash, release the repository flock inherited by this supervisor.  Keep a
    low-cost sentinel alive until an operator performs explicit recovery (or
    the machine is restarted), and make ordinary TERM/INT cleanup attempts
    incapable of silently converting uncertainty into an absence claim.
    """

    sentinel_pid = os.fork()
    if sentinel_pid != 0:
        _write_cleanup_uncertainty(path_value, supervisor_pid, sentinel_pid)
        return sentinel_pid

    # The sentinel must not keep the supervisor's stdout/stderr pipes open;
    # PatchBay needs to observe wrapper exit while the separate lock witness
    # remains alive. It retains only ordinary inherited descriptors such as the
    # repository lock and sleeps without consuming CPU.
    try:
        os.setsid()
    except OSError:
        pass
    devnull = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        try:
            os.dup2(devnull, descriptor)
        except OSError:
            pass
    if devnull > 2:
        os.close(devnull)
    if repo_lock_fd >= 0:
        try:
            os.set_inheritable(repo_lock_fd, False)
        except OSError:
            pass
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        signal.pause()


def supervise(
    gate_fd: int,
    command: list[str],
    *,
    ready_fd: int = -1,
    cleanup_proof_path: str = "",
    repo_lock_fd: int = -1,
) -> int:
    if os.name != "posix":
        raise RuntimeError("PatchBay process supervision requires POSIX")
    if not command:
        raise ValueError("supervised command is required")

    supervisor_pid = os.getpid()
    subreaper_installed = _install_subreaper()
    if repo_lock_fd >= 0:
        # The supervisor retains the descriptor, while FD_CLOEXEC prevents the
        # actual Codex command from inheriting repository-lock ownership.
        os.set_inheritable(repo_lock_fd, False)

    def stop(signum: int, _frame: object) -> None:
        # PatchBay can deliberately reach the supervisor through both its
        # dedicated process group and its exact job marker. Make the first
        # signal edge-triggered so a near-simultaneous duplicate cannot reenter
        # exception unwinding and bypass cleanup-proof publication.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise _StopRequested(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if ready_fd >= 0:
        # Publish the pre-target state before readiness. If PatchBay disappears
        # or cancellation kills this interpreter while it is still blocked on
        # the outer launch gate, a later reconciler can prove that no target was
        # ever released. Legacy/direct callers without the readiness protocol
        # retain the old contract where this path contains terminal state only.
        _write_supervisor_state(
            cleanup_proof_path,
            f"patchbay-supervisor-gated-v3:{supervisor_pid}",
        )
        try:
            os.write(ready_fd, b"1")
        finally:
            os.close(ready_fd)
    tracker: _DarwinTracker | None = None
    try:
        gate = os.read(gate_fd, 1)
        os.close(gate_fd)
        if gate != b"1":
            _write_cleanup_proof(cleanup_proof_path, supervisor_pid)
            return 125
        if ready_fd >= 0:
            # Gated is an absence proof; launching is deliberately not. Commit
            # the fail-closed transition before any target can be forked.
            _write_supervisor_state(
                cleanup_proof_path,
                f"patchbay-supervisor-launching-v3:{supervisor_pid}",
            )
        previous_mask = None
        target_gate_fd = -1
        if hasattr(signal, "pthread_sigmask"):
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGTERM, signal.SIGINT},
            )
        try:
            # The target remains blocked on its private gate until the outer
            # tracker assignment is complete. A pending TERM/INT is delivered
            # only after cleanup ownership is unambiguous.
            target_pid, tracker, target_gate_fd = _fork_target(command)
            os.write(target_gate_fd, b"1")
            os.close(target_gate_fd)
            target_gate_fd = -1
        finally:
            if target_gate_fd >= 0:
                os.close(target_gate_fd)
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if sys.platform.startswith("linux") and not subreaper_installed:
            tracker.tracking_uncertain = True
        while True:
            tracker.poll(0.02)
            try:
                waited_pid, status = os.waitpid(target_pid, os.WNOHANG)
            except InterruptedError:
                continue
            if waited_pid == target_pid:
                if _terminate_owned(supervisor_pid, tracker):
                    _write_cleanup_proof(cleanup_proof_path, supervisor_pid)
                else:
                    _spawn_cleanup_uncertainty_sentinel(
                        cleanup_proof_path,
                        supervisor_pid,
                        repo_lock_fd=repo_lock_fd,
                    )
                return _exit_code(status)
    except _StopRequested as requested:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if tracker is not None:
            if _terminate_owned(supervisor_pid, tracker):
                _write_cleanup_proof(cleanup_proof_path, supervisor_pid)
            else:
                _spawn_cleanup_uncertainty_sentinel(
                    cleanup_proof_path,
                    supervisor_pid,
                    repo_lock_fd=repo_lock_fd,
                )
        else:
            _write_cleanup_proof(cleanup_proof_path, supervisor_pid)
        return 128 + requested.signum
    except BaseException:
        if tracker is not None:
            if _terminate_owned(supervisor_pid, tracker):
                _write_cleanup_proof(cleanup_proof_path, supervisor_pid)
            else:
                _spawn_cleanup_uncertainty_sentinel(
                    cleanup_proof_path,
                    supervisor_pid,
                    repo_lock_fd=repo_lock_fd,
                )
        else:
            # The target was never released (fork/tracker construction failed),
            # so no descendant can exist and an exact absence proof is valid.
            _write_cleanup_proof(cleanup_proof_path, supervisor_pid)
        return 1
    finally:
        try:
            os.close(gate_fd)
        except OSError:
            pass
        if ready_fd >= 0:
            try:
                os.close(ready_fd)
            except OSError:
                pass
        if tracker is not None:
            tracker.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--gate-fd", required=True, type=int)
    parser.add_argument("--ready-fd", default=-1, type=int)
    parser.add_argument("--cleanup-proof-path", default="")
    parser.add_argument("--repo-lock-fd", default=-1, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return supervise(
        args.gate_fd,
        command,
        ready_fd=args.ready_fd,
        cleanup_proof_path=args.cleanup_proof_path,
        repo_lock_fd=args.repo_lock_fd,
    )


if __name__ == "__main__":
    raise SystemExit(main())
