"""Bounded command streams and exact cleanup inside one detached subreaper."""
from __future__ import annotations

import os
import errno
import json
import re
import resource
import selectors
import subprocess
import sys
import time
from pathlib import Path

from ..models import Check, Profile
from ..persistence.storage import tree_usage
from . import identity
from ..util import atomic_write, canonical
from .environment import base_environment, cleanup_container, command, inspect_container, start_command
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


def _persist_lifecycle(run_root: Path, operation_id: str, container: dict) -> None:
    path = run_root / "docker-lifecycle.json"
    if path.exists():
        current = json.loads(path.read_text())
        valid = current.get("operation_id") == operation_id and isinstance(current.get("containers"), list)
        if not valid:
            raise RuntimeError("Docker lifecycle metadata does not belong to this operation")
        containers = current["containers"]
        existing = next((item for item in containers if item.get("sequence") == container["sequence"]), None)
        if existing is None:
            containers.append(container)
        else:
            existing.clear()
            existing.update(container)
    else:
        containers = [container]
    atomic_write(path, canonical({"format_version": 1, "operation_id": operation_id,
                                  "containers": containers}))


def _bounded_communicate(child: subprocess.Popen, timeout: float, *,
                         stdout_limit: int = 4096, stderr_limit: int = 65536) -> tuple[bytes, bytes, bool, bool]:
    """Drain create-client pipes while retaining only bounded output prefixes."""
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    overflow = {"stdout": False, "stderr": False}
    for name, stream in (("stdout", child.stdout), ("stderr", child.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(child.args, timeout,
                                                output=bytes(buffers["stdout"]),
                                                stderr=bytes(buffers["stderr"]))
            for key, _ in selector.select(min(0.1, remaining)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                available = limits[name] - len(buffers[name])
                if available > 0:
                    buffers[name].extend(chunk[:available])
                if len(chunk) > available:
                    overflow[name] = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(child.args, timeout,
                                            output=bytes(buffers["stdout"]),
                                            stderr=bytes(buffers["stderr"]))
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired(child.args, timeout,
                                            output=bytes(buffers["stdout"]),
                                            stderr=bytes(buffers["stderr"])) from exc
        return (bytes(buffers["stdout"]), bytes(buffers["stderr"]),
                overflow["stdout"], overflow["stderr"])
    finally:
        selector.close()
        for stream in (child.stdout, child.stderr):
            if stream and not stream.closed:
                stream.close()


def _docker_create(argv: list[str], *, cwd: Path, env: dict, run_root: Path,
                   operation_id: str, container_name: str, container: dict,
                   known: dict, identity_callback, stdout: LogSpool, stderr: LogSpool,
                   timeout: float) -> tuple[subprocess.Popen | None, str | None, dict | None]:
    """Conclude create before cancellation can decide whether start is safe.

    A timed-out or disconnected create RPC has unknown daemon-side effects. It
    deliberately returns no ID and no absence claim, even if a later inspect
    would momentarily find nothing.
    """
    create_deadline = time.monotonic() + timeout
    info = None
    daemon_id = ""
    info_stderr = b""
    try:
        info = subprocess.Popen(["docker", "info", "--format", "{{.ID}}"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        info_identity = identity.identity(info.pid)
        if info_identity:
            known[info.pid] = info_identity
        info_out, info_stderr, info_out_overflow, _ = _bounded_communicate(
            info, min(10.0, max(0.001, create_deadline - time.monotonic())),
            stdout_limit=512, stderr_limit=4096)
        daemon_id = info_out.decode("utf-8", "replace").strip() if not info_out_overflow else ""
    except (OSError, subprocess.TimeoutExpired):
        if info is not None and info.poll() is None:
            try:
                info.kill()
                info.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        daemon_id = ""
    if info is None or info.returncode != 0 or not daemon_id:
        container.update(state="not_started", cleanup={"state": "not_started", "container_id": None,
                        "rm_exit_code": None, "absent_confirmations": 0,
                        "last_error": "docker_daemon_identity_unavailable_before_create",
                        "diagnostic": info_stderr.decode("utf-8", "replace")[-2048:]})
        _persist_lifecycle(run_root, operation_id, container)
        return None, None, {"code": "DOCKER_DAEMON_ID_UNAVAILABLE", "message": "Docker daemon identity could not be verified before container creation"}
    container["daemon_id"] = daemon_id[:256]
    remaining = create_deadline - time.monotonic()
    if remaining <= 0:
        container.update(state="not_started", cleanup={"state": "not_started", "container_id": None,
                        "rm_exit_code": None, "absent_confirmations": 0,
                        "last_error": "check_budget_expired_before_create_rpc"})
        _persist_lifecycle(run_root, operation_id, container)
        return None, None, {"code": "CHECK_TIMEOUT", "message": "Check budget expired before Docker create"}
    container["state"] = "reserved"
    _persist_lifecycle(run_root, operation_id, container)
    stage = {"name": "create", "state": "launching", "identity": None,
             "exit_code": None, "signal": None, "started_at": time.time(), "finished_at": None}
    container["stages"].append(stage)
    container["state"] = "creating"
    _persist_lifecycle(run_root, operation_id, container)
    try:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        stage.update(state="not_started", finished_at=time.time())
        container.update(state="not_started", cleanup={"state": "not_started", "container_id": None,
                        "rm_exit_code": None, "absent_confirmations": 0, "last_error": type(exc).__name__})
        _persist_lifecycle(run_root, operation_id, container)
        return None, None, {"code": "DOCKER_CREATE_NOT_STARTED", "message": str(exc)[:1000]}
    record = identity.identity(child.pid)
    stage["identity"] = record
    if record:
        known[record["pid"]] = record
    if identity_callback:
        identity_callback(record, container_name)
    _persist_lifecycle(run_root, operation_id, container)
    try:
        out, err, stdout_overflow, stderr_overflow = _bounded_communicate(
            child, max(0.001, create_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # This is our exact Popen child, so terminating this client is safe.
        # It does not cancel a daemon-side create already accepted by Docker.
        try:
            child.kill()
            child.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        stage.update(state="unknown", exit_code=child.returncode, finished_at=time.time())
        container.update(state="unknown", cleanup={"state": "unresolved", "container_id": None,
                        "rm_exit_code": None, "absent_confirmations": 0,
                        "last_error": "create_rpc_timeout_without_acknowledged_id"})
        _persist_lifecycle(run_root, operation_id, container)
        return child, None, {"code": "DOCKER_CREATE_UNKNOWN", "message": "Docker create did not conclusively acknowledge an immutable container ID"}
    stage.update(state="exited", exit_code=child.returncode,
                 signal=(-child.returncode if child.returncode is not None and child.returncode < 0 else None),
                 finished_at=time.time(), stdout_truncated=stdout_overflow,
                 stderr_truncated=stderr_overflow,
                 diagnostic=err.decode("utf-8", "replace")[-4096:])
    cidfile = Path(argv[argv.index("--cidfile") + 1])
    raw_id = out.decode("ascii", "replace").strip()
    try:
        file_id = cidfile.read_text().strip()
    except OSError:
        file_id = ""
    if child.returncode != 0 or stdout_overflow or not re.fullmatch(r"[0-9a-f]{64}", raw_id) or raw_id != file_id:
        container.update(state="unknown", cleanup={"state": "unresolved", "container_id": None,
                        "rm_exit_code": None, "absent_confirmations": 0,
                        "last_error": "create_did_not_acknowledge_matching_id_and_cidfile"})
        _persist_lifecycle(run_root, operation_id, container)
        return child, None, {"code": "DOCKER_CREATE_UNKNOWN", "message": "Docker create did not return a matching immutable ID and cidfile"}
    container.update(container_id=raw_id, state="created",
                     cleanup={"state": "not_started", "container_id": raw_id,
                              "rm_exit_code": None, "absent_confirmations": 0, "last_error": None})
    _persist_lifecycle(run_root, operation_id, container)
    return child, raw_id, None


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
    container_id = None
    docker_record = None
    create_process = None
    operation_id = run_root.name
    selector = selectors.DefaultSelector()
    try:
        if profile.backend == "docker":
            docker_record = {"sequence": sequence, "container_name": container_name,
                             "container_id": None, "daemon_id": None, "image_id": profile.image,
                             "configured_image": profile.image, "command": list(check.argv),
                             "source_mount_path": str(workspace.resolve()), "state": "reserved",
                             "stages": [], "cleanup": {"state": "not_started", "container_id": None,
                             "rm_exit_code": None, "absent_confirmations": 0, "last_error": None}}
            remaining_budget = max(0.0, float(check.timeout_seconds) - (time.monotonic() - start))
            if remaining_budget <= 0:
                terminal = "timed_out"
                error = {"code": "CHECK_TIMEOUT", "message": "Check budget expired before Docker create"}
                create_error = error
            else:
                create_process, container_id, create_error = _docker_create(
                argv, cwd=cwd, env=env, run_root=run_root, operation_id=operation_id,
                container_name=container_name, container=docker_record, known=known,
                identity_callback=identity_callback, stdout=stdout, stderr=stderr,
                timeout=remaining_budget)
            process = None  # create client is concluded; its streams were drained above.
            if terminal == "timed_out":
                pass
            elif create_error:
                terminal = "failed"
                error = create_error
                cleanup = {"state": "not_required" if docker_record["cleanup"]["state"] == "not_started" else "unresolved",
                           "container": docker_record["cleanup"]}
            elif time.monotonic() - start >= check.timeout_seconds:
                terminal = "timed_out"
                error = {"code": "CHECK_TIMEOUT", "message": "Check budget expired during Docker create"}
            elif cancelled():
                terminal = "cancelled"
            else:
                stage_record = {"name": "start", "state": "launching", "identity": None,
                                "exit_code": None, "signal": None, "started_at": time.time(), "finished_at": None}
                docker_record["stages"].append(stage_record)
                docker_record["state"] = "starting"
                _persist_lifecycle(run_root, operation_id, docker_record)
                process = subprocess.Popen(start_command(container_id), cwd=cwd, env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, start_new_session=True)
                record = identity.identity(process.pid)
                stage_record["identity"] = record
                stage_record["state"] = "running" if record else "unknown"
                docker_record["state"] = "running" if record else "unknown"
                if record:
                    known[process.pid] = record
                if identity_callback:
                    identity_callback(record, container_name)
                _persist_lifecycle(run_root, operation_id, docker_record)
                for stream, spool in ((process.stdout, stdout), (process.stderr, stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, spool)
        else:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                       preexec_fn=_limits(profile, check))
            record = identity.identity(process.pid)
            if record:
                known[process.pid] = record
            if identity_callback:
                identity_callback(record, None)
            for stream, spool in ((process.stdout, stdout), (process.stderr, stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, spool)
        last_discovery, last_disk_probe, last_usage_probe, ended_at = 0.0, 0.0, 0.0, None
        while process is not None:
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
                    sample = _docker_usage(container_id) or {}
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
                sample = _docker_usage(container_id) or {} if container_id else {}
            _record_usage(usage, cpu=sample.get("cpu_cores"), rss=sample.get("rss_bytes"),
                          pids=sample.get("pids"), method=sample.get("method"))
            try:
                observed_disk = tree_usage(run_root, stop_above=profile.disk_mb * 1024 * 1024)
                _record_usage(usage, disk=observed_disk["bytes"], method="bounded-run-root-tree-scan")
            except OSError:
                pass
        if profile.backend == "docker":
            if container_id:
                docker_state = inspect_container(container_id, expected_daemon_id=docker_record["daemon_id"])
                if docker_state.get("state") == "observed" and docker_state.get("oom_killed"):
                    terminal = "failed"
                    error = {"code": "OOM_KILLED", "message": "Docker reports that the check was killed by its memory limit", "observation": docker_state}
                if process is not None and process.returncode == 127:
                    terminal = "failed"
                    error = {"code": "MISSING_EXECUTABLE", "message": "Docker could not find the approved command in the prepared image"}
                elif process is not None and process.returncode == 126:
                    terminal = "failed"
                    error = {"code": "EXECUTABLE_NOT_RUNNABLE", "message": "Docker could not execute the approved command in the prepared image"}
                elif process is not None and process.returncode == 125 and not error:
                    terminal = "failed"
                    error = {"code": "DOCKER_UNAVAILABLE", "message": "Docker could not start the prepared container"}
                elif process is not None and process.returncode == 137 and not (docker_state.get("state") == "observed" and docker_state.get("oom_killed")):
                    terminal = "failed"
                    error = {"code": "TERMINATED_137_CAUSE_UNKNOWN", "message": "Container exited 137 but Docker did not confirm an OOM kill", "observation": docker_state}
            try:
                host_cleanup = identity.cleanup_owned(known, root_process=process or create_process)
            except BaseException as exc:
                host_cleanup = {"state": "unresolved", "error": str(exc)[:1000]}
            if not container_id:
                observed_cleanup = docker_record.get("cleanup") if docker_record else {"state": "unresolved"}
                cleanup = {"state": "unresolved" if observed_cleanup.get("state") != "not_started" or host_cleanup.get("state") != "proven" else "not_required",
                           "host_processes": host_cleanup, "container": observed_cleanup}
            else:
                cleanup = host_cleanup
            if profile.backend == "docker" and container_id:
                container_cleanup = cleanup_container(container_id, expected_daemon_id=docker_record["daemon_id"])
                if container_cleanup["state"] != "proven":
                    cleanup["state"] = "unresolved"
                cleanup["container"] = container_cleanup
                if docker_record:
                    docker_record["state"] = "cleaned" if container_cleanup["state"] == "proven" else "cleanup_unresolved"
                    docker_record["cleanup"] = {"state": "proven_absent" if container_cleanup["state"] == "proven" else "unresolved",
                        "container_id": container_id, "rm_exit_code": container_cleanup.get("remove_exit_code"),
                        "absent_confirmations": 1 if container_cleanup.get("absence_confirmed") else 0,
                        "last_error": container_cleanup.get("message")}
                    _persist_lifecycle(run_root, operation_id, docker_record)
        elif process is not None:
            try:
                cleanup = identity.cleanup_owned(known, root_process=process)
            except BaseException as exc:
                cleanup = {"state": "unresolved", "error": str(exc)[:1000]}
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
    if docker_record and docker_record["stages"] and docker_record["stages"][-1]["name"] == "start":
        if docker_record["stages"][-1].get("identity") is None:
            docker_record["stages"][-1]["state"] = "unknown" if process is not None else "not_started"
        else:
            docker_record["stages"][-1]["state"] = "exited" if process and process.returncode is not None else docker_record["stages"][-1]["state"]
        if process and docker_record["stages"][-1].get("identity"):
            docker_record["stages"][-1]["exit_code"] = process.returncode
            docker_record["stages"][-1]["signal"] = -process.returncode if process.returncode is not None and process.returncode < 0 else None
        docker_record["stages"][-1]["finished_at"] = time.time()
        _persist_lifecycle(run_root, operation_id, docker_record)
    return {"state": terminal, "exit_code": process.returncode if process else None,
            "signal": -process.returncode if process and process.returncode is not None and process.returncode < 0 else None,
            "duration_seconds": round(time.monotonic() - start, 4), "cleanup": cleanup,
            "resource_usage": usage, "docker_state": docker_state,
            "stdout": out, "stderr": err, **({"error": error} if error else {})}
