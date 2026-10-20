"""Linux process-start identities and owned-descendant cleanup.

Each run has its own child-subreaper supervisor. Prefer the kernel child-link
index. Kernels without that procfs feature use a bounded PPID snapshot at a
lower cadence; missing ancestry is never interpreted as an empty process tree.
"""
from __future__ import annotations

import ctypes
import functools
import os
import signal
import sys
import time
from pathlib import Path

from ..errors import AuthorError, require


@functools.lru_cache(maxsize=1)
def boot_id() -> str:
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def identity(pid: int) -> dict | None:
    if sys.platform != 'linux':
        return None
    try:
        raw = Path(f'/proc/{pid}/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        return {'pid': pid, 'start': fields[19], 'boot': boot_id(), 'state': fields[0], 'ppid': int(fields[1])}
    except (OSError, IndexError, ValueError):
        return None


def same_process(record: dict | None, *, include_zombie: bool = False) -> bool:
    if not record:
        return False
    live = identity(record['pid'])
    return bool(live and live['boot'] == record['boot'] and live['start'] == record['start']
                and (include_zombie or live['state'] not in {'Z', 'X'}))


def enable_subreaper() -> None:
    require(sys.platform == 'linux', 'BLOCKED_ENVIRONMENT', 'Execution supervision currently requires Linux/WSL2')
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(36, 1, 0, 0, 0) == 0, 'BLOCKED_ENVIRONMENT', 'Could not establish Linux child-subreaper ownership')
    observed = ctypes.c_int()
    require(libc.prctl(37, ctypes.byref(observed), 0, 0, 0) == 0 and observed.value == 1,
            'BLOCKED_ENVIRONMENT', 'Child-subreaper ownership was not confirmed')


@functools.lru_cache(maxsize=1)
def child_index_available() -> bool:
    return Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children').is_file()


def discovery_interval() -> float:
    return .25 if child_index_available() else 1.0


def _proc_snapshot() -> dict[int, dict]:
    result = {}
    start = time.monotonic()
    try:
        with os.scandir('/proc') as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                require(len(result) < 50000 and time.monotonic() - start <= 2.0,
                        'EXECUTION_UNKNOWN', 'Process discovery budget exceeded; cleanup cannot be claimed')
                pid = int(entry.name)
                try:
                    raw = Path(entry.path, 'stat').read_text()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise AuthorError('EXECUTION_UNKNOWN', 'Process ancestry snapshot is incomplete') from exc
                fields = raw[raw.rfind(')') + 2:].split()
                require(len(fields) > 19, 'EXECUTION_UNKNOWN', 'Incomplete process identity')
                result[pid] = {'pid': pid, 'start': fields[19], 'boot': boot_id(),
                               'state': fields[0], 'ppid': int(fields[1])}
    except OSError as exc:
        raise AuthorError('EXECUTION_UNKNOWN', 'Process namespace is unavailable') from exc
    return result


def descendants(pid: int) -> dict[int, dict]:
    if not child_index_available():
        snapshot = _proc_snapshot()
        by_parent = {}
        for record in snapshot.values():
            by_parent.setdefault(record['ppid'], []).append(record)
        result, todo = {}, [pid]
        while todo:
            parent = todo.pop()
            for record in by_parent.get(parent, []):
                if record['pid'] in result or record['pid'] == pid:
                    continue
                parent_record = snapshot.get(parent)
                if parent_record and int(parent_record['start']) > int(record['start']):
                    continue
                result[record['pid']] = record
                todo.append(record['pid'])
        return result
    result, todo = {}, [pid]
    while todo:
        parent = todo.pop()
        try:
            tids = list(Path(f'/proc/{parent}/task').iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AuthorError('EXECUTION_UNKNOWN', 'Owned process child links are unavailable') from exc
        children = set()
        for tid in tids:
            try:
                children.update(int(x) for x in (tid / 'children').read_text().split())
            except FileNotFoundError:
                if not tid.exists():
                    continue
                raise AuthorError('EXECUTION_UNKNOWN', 'Expected child index disappeared')
            except OSError as exc:
                raise AuthorError('EXECUTION_UNKNOWN', 'Owned child index could not be read') from exc
        for child in children:
            if child in result:
                continue
            record = identity(child)
            if record:
                result[child] = record
                todo.append(child)
    return result


def signal_exact(record: dict, sig: int) -> bool:
    if not same_process(record):
        return False
    try:
        descriptor = os.pidfd_open(record['pid'], 0)
    except (ProcessLookupError, FileNotFoundError):
        return False
    except (AttributeError, OSError) as exc:
        raise AuthorError('EXECUTION_UNKNOWN', 'Exact process signalling is unavailable') from exc
    try:
        if not same_process(record):
            return False
        signal.pidfd_send_signal(descriptor, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        os.close(descriptor)


def cleanup_owned(known: dict[int, dict], *, grace: float = 1.5, root_process=None) -> dict:
    known.update(descendants(os.getpid()))
    signalled = set()
    empty_sweeps = 0
    for sig, duration in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        deadline = time.monotonic() + duration
        while True:
            known.update(descendants(os.getpid()))
            living = [r for r in known.values() if same_process(r)]
            empty_sweeps = empty_sweeps + 1 if not living else 0
            if empty_sweeps >= 2:
                break
            for record in living:
                marker = (record['pid'], record['start'], sig)
                if marker not in signalled:
                    signal_exact(record, sig)
                    signalled.add(marker)
            if root_process:
                root_process.poll()
            if time.monotonic() >= deadline:
                break
            time.sleep(.1)
        if empty_sweeps >= 2:
            break
    if root_process:
        try:
            root_process.wait(timeout=.2)
        except Exception:
            pass
    for pid in list(known):
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            pass
    known.update(descendants(os.getpid()))
    living = [r for r in known.values() if same_process(r)]
    return {'state': 'unresolved' if living else 'proven', 'owned_processes': len(known),
            'discovery': 'kernel-child-index' if child_index_available() else 'bounded-ppid-snapshot',
            'remaining': [{'pid': r['pid'], 'start': r['start']} for r in living]}
