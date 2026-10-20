"""Bounded command streams and exact cleanup inside one detached subreaper."""
from __future__ import annotations

import os
import resource
import selectors
import subprocess
import time
from pathlib import Path

from ..models import Check, Profile
from ..persistence.storage import tree_usage
from . import identity
from .environment import cleanup_container, command
from .logs import LogSpool


def _limits(profile: Profile, check: Check):
    def apply():
        memory = profile.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (profile.disk_mb * 1024 * 1024, profile.disk_mb * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (check.timeout_seconds, check.timeout_seconds + 1))
    return apply


def execute(profile: Profile, check: Check, workspace: Path, run_root: Path, *, stage: str,
            sequence: int, cancelled, identity_callback=None) -> dict:
    container_name = "pba-" + run_root.name.replace("_", "-") + "-" + str(sequence)
    argv, cwd, env = command(profile, check, workspace, run_root, container_name=container_name, stage=stage)
    stdout = LogSpool(run_root / f"{sequence:03d}-stdout.log", profile.log_bytes // 2)
    stderr = LogSpool(run_root / f"{sequence:03d}-stderr.log", profile.log_bytes // 2)
    start = time.monotonic()
    process = None
    known, terminal = {}, "failed"
    cleanup, error = {"state": "not_required"}, None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                   preexec_fn=_limits(profile, check) if profile.backend == "trusted-local" else None)
        record = identity.identity(process.pid)
        if record:
            known[process.pid] = record
        if identity_callback:
            identity_callback(record, container_name if profile.backend == "docker" else None)
        for stream, spool in ((process.stdout, stdout), (process.stderr, stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, spool)
        last_discovery, last_disk_probe, ended_at = 0.0, 0.0, None
        while True:
            now = time.monotonic()
            if now - last_discovery >= identity.discovery_interval():
                known.update(identity.descendants(os.getpid()))
                last_discovery = now
                if cancelled():
                    terminal = "cancelled"
                    break
                if now - start > check.timeout_seconds:
                    terminal = "timed_out"
                    break
                if profile.backend == "trusted-local" and len([p for p in known.values() if identity.same_process(p)]) > profile.pids:
                    terminal = "failed"
                    error = {"code": "RESOURCE_LIMIT", "message": "Owned process count exceeded the check profile"}
                    break
            if now - last_disk_probe >= 5.0:
                observed_disk = tree_usage(run_root, stop_above=profile.disk_mb * 1024 * 1024)
                last_disk_probe = now
                if not observed_disk["complete"] or observed_disk["bytes"] > profile.disk_mb * 1024 * 1024:
                    terminal = "failed"
                    error = {"code": "RESOURCE_LIMIT", "message": "Execution workspace exceeded its disk budget or accounting was incomplete", "observation": observed_disk}
                    break
            for key, _ in selector.select(timeout=0.1):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if chunk:
                    key.data.write(chunk)
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
            if process.poll() is not None:
                ended_at = ended_at or now
                if not selector.get_map() or now - ended_at > 0.2:
                    terminal = "completed"
                    break
    except BaseException as exc:
        terminal = "failed"
        error = {"code": getattr(exc, "code", "EXECUTOR_ERROR"), "message": str(exc)[:2000], "type": type(exc).__name__}
    finally:
        if process is not None:
            try:
                cleanup = identity.cleanup_owned(known, root_process=process)
            except BaseException as exc:
                cleanup = {"state": "unresolved", "error": str(exc)[:1000]}
            if profile.backend == "docker":
                container_cleanup = cleanup_container(container_name)
                if container_cleanup["state"] != "proven":
                    cleanup["state"] = "unresolved"
                cleanup["container"] = container_cleanup
        for key in list(selector.get_map().values()):
            try:
                while True:
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except (BlockingIOError, OSError):
                        break
                    if not chunk:
                        break
                    key.data.write(chunk)
            except BaseException as exc:
                error = {"code": "EVIDENCE_WRITE_FAILED", "message": str(exc)[:2000]}
                terminal = "failed"
            finally:
                key.fileobj.close()
        selector.close()
        out, err = stdout.close(), stderr.close()
    return {"state": terminal, "exit_code": process.returncode if process else None,
            "signal": -process.returncode if process and process.returncode is not None and process.returncode < 0 else None,
            "duration_seconds": round(time.monotonic() - start, 4), "cleanup": cleanup,
            "stdout": out, "stderr": err, **({"error": error} if error else {})}
