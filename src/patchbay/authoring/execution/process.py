"""Bounded command streams and exact cleanup inside one detached subreaper."""
from __future__ import annotations

import os
import errno
import resource
import selectors
import subprocess
import sys
import time
from pathlib import Path

from ..models import Check, Profile
from ..persistence.storage import tree_usage
from . import identity
from .environment import base_environment, cleanup_container, command, inspect_container
from .logs import LogSpool


def _proc_usage(known: dict[int, dict], previous: dict[int, float], elapsed: float) -> tuple[dict, dict]:
    """Read aggregate owned-process CPU and RSS from Linux procfs."""
    if sys.platform != "linux":
        return {}, previous
    ticks = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    total_cpu, rss, pids, next_cpu = 0.0, 0, 0, {}
    for pid, identity in tuple(known.items()):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if len(fields) < 22 or fields[19] != str(identity.get("start")):
                continue
            cpu = (int(fields[11]) + int(fields[12])) / ticks
            rss_bytes = max(0, int(fields[21])) * page
            next_cpu[pid] = cpu
            rss += rss_bytes
            pids += 1
            if pid in previous and elapsed > 0:
                total_cpu += max(0.0, cpu - previous[pid]) / elapsed
        except (OSError, ValueError, IndexError):
            continue
    return {"cpu_cores": total_cpu if previous else None, "rss_bytes": rss,
            "pids": pids, "method": "linux-procfs-owned-process-tree"}, next_cpu


def _empty_usage() -> dict:
    return {"observation_state": "unavailable", "method": None, "rss_method": None,
            "cpu_cores_peak": None, "rss_bytes_peak": None, "pids_peak": None,
            "disk_bytes_peak": None, "samples": 0,
            "unavailable": ["CPU", "RSS", "PID", "disk"]}


def _record_usage(usage: dict, *, cpu=None, rss=None, pids=None, disk=None, method=None):
    usage["samples"] += 1
    if method:
        usage["method"] = method
    for field, value, key in (("cpu_cores_peak", cpu, "CPU"), ("rss_bytes_peak", rss, "RSS"),
                              ("pids_peak", pids, "PID"), ("disk_bytes_peak", disk, "disk")):
        if value is None:
            continue
        usage[field] = value if usage[field] is None else max(usage[field], value)
        if key in usage["unavailable"]:
            usage["unavailable"].remove(key)
    usage["observation_state"] = "observed" if not usage["unavailable"] else ("partial" if usage["samples"] else "unavailable")
    if rss is not None and method:
        usage["rss_method"] = {"linux-procfs-owned-process-tree": "summed resident pages from /proc/<pid>/stat",
                                "docker-stats-no-stream": "Docker memory usage; not per-process RSS"}.get(method, method)


def _docker_usage(container_name: str) -> dict | None:
    try:
        result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}|{{.PIDs}}", container_name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=base_environment(Path("/nonexistent")), timeout=5)
        if result.returncode:
            return None
        cpu_text, memory_text, pids_text = result.stdout.decode("ascii", "replace").strip().split("|", 2)
        cpu = float(cpu_text.rstrip("%")) / 100.0
        memory_text = memory_text.split("/", 1)[0].strip()
        import re
        match = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]+)", memory_text)
        if not match:
            return None
        unit = match.group(2).lower()
        scale = {"b": 1, "kib": 1024, "kb": 1000, "mib": 1024**2, "mb": 1000**2,
                 "gib": 1024**3, "gb": 1000**3, "tib": 1024**4, "tb": 1000**4}.get(unit)
        if scale is None:
            return None
        return {"cpu_cores": cpu, "rss_bytes": int(float(match.group(1)) * scale),
                "pids": int(pids_text), "method": "docker-stats-no-stream"}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


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
    usage = _empty_usage()
    previous_cpu, previous_sample = {}, start
    docker_state = {"state": "unavailable", "reason": "not_applicable"}
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
        last_discovery, last_disk_probe, last_usage_probe, ended_at = 0.0, 0.0, 0.0, None
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
                    error = {"code": "PID_LIMIT_EXCEEDED", "message": "Owned process count exceeded the check profile"}
                    break
            if now - last_disk_probe >= 5.0:
                observed_disk = tree_usage(run_root, stop_above=profile.disk_mb * 1024 * 1024)
                last_disk_probe = now
                _record_usage(usage, disk=observed_disk["bytes"], method="bounded-run-root-tree-scan")
                if not observed_disk["complete"] and observed_disk["bytes"] <= profile.disk_mb * 1024 * 1024:
                    terminal = "failed"
                    error = {"code": "DISK_ACCOUNTING_INCOMPLETE", "message": "Execution workspace disk use could not be accounted completely", "observation": observed_disk}
                    break
                if observed_disk["bytes"] > profile.disk_mb * 1024 * 1024:
                    terminal = "failed"
                    error = {"code": "DISK_BUDGET_EXCEEDED", "message": "Execution workspace exceeded its disk budget", "observation": observed_disk}
                    break
            if now - last_usage_probe >= 1.0:
                elapsed = now - previous_sample
                if profile.backend == "trusted-local":
                    sample, previous_cpu = _proc_usage(known, previous_cpu, elapsed)
                else:
                    sample = _docker_usage(container_name) or {}
                _record_usage(usage, cpu=sample.get("cpu_cores"), rss=sample.get("rss_bytes"),
                              pids=sample.get("pids"), method=sample.get("method"))
                previous_sample, last_usage_probe = now, now
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
        code = getattr(exc, "code", None)
        if isinstance(exc, FileNotFoundError) or (isinstance(exc, OSError) and exc.errno == errno.ENOENT):
            code = "MISSING_EXECUTABLE"
        elif isinstance(exc, PermissionError) or (isinstance(exc, OSError) and exc.errno == errno.EACCES):
            code = "EXECUTABLE_NOT_RUNNABLE"
        elif isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
            code = "DISK_EXHAUSTED"
        error = {"code": code or "EXECUTOR_ERROR", "message": str(exc)[:2000], "type": type(exc).__name__}
    finally:
        if process is not None:
            now = time.monotonic()
            if profile.backend == "trusted-local":
                try:
                    known.update(identity.descendants(os.getpid()))
                except BaseException:
                    pass
                sample, _ = _proc_usage(known, previous_cpu, max(now - previous_sample, .001))
            else:
                sample = _docker_usage(container_name) or {}
            _record_usage(usage, cpu=sample.get("cpu_cores"), rss=sample.get("rss_bytes"),
                          pids=sample.get("pids"), method=sample.get("method"))
            try:
                observed_disk = tree_usage(run_root, stop_above=profile.disk_mb * 1024 * 1024)
                _record_usage(usage, disk=observed_disk["bytes"], method="bounded-run-root-tree-scan")
            except OSError:
                pass
        if process is not None:
            if profile.backend == "docker":
                docker_state = inspect_container(container_name)
                if docker_state.get("state") == "observed" and docker_state.get("oom_killed"):
                    terminal = "failed"
                    error = {"code": "OOM_KILLED", "message": "Docker reports that the check was killed by its memory limit", "observation": docker_state}
                if process.returncode == 127:
                    terminal = "failed"
                    error = {"code": "MISSING_EXECUTABLE", "message": "Docker could not find the approved command in the prepared image"}
                elif process.returncode == 126:
                    terminal = "failed"
                    error = {"code": "EXECUTABLE_NOT_RUNNABLE", "message": "Docker could not execute the approved command in the prepared image"}
                elif process.returncode == 125 and not error:
                    terminal = "failed"
                    error = {"code": "DOCKER_UNAVAILABLE", "message": "Docker could not start the prepared container"}
                elif process.returncode == 137 and not (docker_state.get("state") == "observed" and docker_state.get("oom_killed")):
                    terminal = "failed"
                    error = {"code": "TERMINATED_137_CAUSE_UNKNOWN", "message": "Container exited 137 but Docker did not confirm an OOM kill", "observation": docker_state}
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
                code = "DISK_EXHAUSTED" if isinstance(exc, OSError) and exc.errno in {
                    errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)} else "EVIDENCE_WRITE_FAILED"
                error = {"code": code, "message": str(exc)[:2000]}
                terminal = "failed"
            finally:
                key.fileobj.close()
        selector.close()
        out, err = stdout.close(), stderr.close()
    if usage["samples"] == 0:
        usage["method"] = "unavailable-before-first-sample"
    return {"state": terminal, "exit_code": process.returncode if process else None,
            "signal": -process.returncode if process and process.returncode is not None and process.returncode < 0 else None,
            "duration_seconds": round(time.monotonic() - start, 4), "cleanup": cleanup,
            "resource_usage": usage, "docker_state": docker_state,
            "stdout": out, "stderr": err, **({"error": error} if error else {})}
