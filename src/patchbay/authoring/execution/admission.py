"""Measured, conservative resource admission for Author execution."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..errors import AuthorError
from ..models import Profile

_MIB = 1024**2
_HISTORY_LIMIT = 20
_HISTORY_BYTES = 2 * _MIB
_CGROUP_ROOT = Path("/sys/fs/cgroup")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _number(path: Path) -> int | None:
    value = _read_text(path)
    if value is None or value == "max":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _cgroup_dirs() -> tuple[Path | None, Path | None, Path | None]:
    """Return current process v2 and v1 controller directories when discoverable."""
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return None, None, None
    v2 = v1_cpu = v1_memory = None
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, relative = parts
        rel = Path(relative.lstrip("/"))
        if ".." in rel.parts:
            continue
        if not controllers:
            candidate = _CGROUP_ROOT / rel
            if candidate.is_dir():
                v2 = candidate
        else:
            names = set(controllers.split(","))
            if "cpu" in names:
                candidate = _CGROUP_ROOT / "cpu" / rel
                if candidate.is_dir():
                    v1_cpu = candidate
            if "memory" in names:
                candidate = _CGROUP_ROOT / "memory" / rel
                if candidate.is_dir():
                    v1_memory = candidate
    return v2, v1_cpu, v1_memory


def _hierarchy(directory: Path | None, mount: Path):
    """Include inherited limits, stopping at the controller's visible mount.

    An unlimited child remains subject to every ancestor. Ancestor usage also
    includes siblings, so remaining capacity must use each limit's own usage.
    Never walk into ordinary filesystem parents outside the controller mount.
    """
    if directory is None or not directory.is_relative_to(mount):
        return
    while True:
        yield directory
        if directory == mount:
            return
        directory = directory.parent


def host_capacity() -> dict:
    """Observe effective CPU and memory capacity, including cgroup limits.

    Missing kernel observations remain ``None`` and are reported as unavailable;
    callers must not treat an unavailable observation as a measured zero.
    """
    cpu_values, cpu_sources = [], []
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity:
        try:
            allowed = len(affinity(0))
            if allowed > 0:
                cpu_values.append(float(allowed))
                cpu_sources.append("sched_getaffinity")
        except OSError:
            pass
    if os.cpu_count():
        cpu_values.append(float(os.cpu_count()))
        cpu_sources.append("os.cpu_count")

    v2, v1_cpu, v1_memory = _cgroup_dirs()
    quota_info = None
    cpu_mount = _CGROUP_ROOT if v2 else _CGROUP_ROOT / "cpu"
    quota_capacity = None
    for directory in _hierarchy(v2 or v1_cpu, cpu_mount):
        if v2:
            raw = _read_text(directory / "cpu.max")
            try:
                quota, period = raw.split()
                info = {"quota": quota, "period_us": int(period), "source": "cgroup-v2"}
                if quota_info is None:
                    quota_info = info
                if quota != "max" and int(quota) > 0 and int(period) > 0:
                    capacity = int(quota) / int(period)
                    if quota_capacity is None or capacity < quota_capacity:
                        quota_capacity, quota_info = capacity, info
            except (AttributeError, ValueError):
                pass
        else:
            quota, period = _number(directory / "cpu.cfs_quota_us"), _number(directory / "cpu.cfs_period_us")
            if quota is not None and period and quota > 0:
                capacity = quota / period
                if quota_capacity is None or capacity < quota_capacity:
                    quota_capacity = capacity
                    quota_info = {"quota_us": quota, "period_us": period, "source": "cgroup-v1"}
    if quota_capacity is not None:
        cpu_values.append(quota_capacity)
        cpu_sources.append("cgroup-v2-cpu.max" if v2 else "cgroup-v1-cpu.cfs_quota_us")

    memory_available = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                memory_available = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    memory_limit = memory_current = cgroup_remaining = None
    memory_mount = _CGROUP_ROOT if v2 else _CGROUP_ROOT / "memory"
    for directory in _hierarchy(v2 or v1_memory, memory_mount):
        limit = _number(directory / ("memory.max" if v2 else "memory.limit_in_bytes"))
        current = _number(directory / ("memory.current" if v2 else "memory.usage_in_bytes"))
        if memory_current is None:
            memory_current = current
        # v1 commonly uses a huge sentinel for an unlimited limit.
        if not v2 and limit is not None and limit >= 2**60:
            limit = None
        if limit is not None and current is not None:
            remaining = max(0, limit - current)
            if cgroup_remaining is None or remaining < cgroup_remaining:
                cgroup_remaining, memory_limit, memory_current = remaining, limit, current
    memory_sources = ["proc-meminfo-MemAvailable"] if memory_available is not None else []
    if cgroup_remaining is not None:
        memory_available = min(memory_available, cgroup_remaining) if memory_available is not None else cgroup_remaining
        memory_sources.append("cgroup-memory-limit-minus-current")

    pids_current = pids_limit = pids_remaining = None
    for directory in _hierarchy(v2, _CGROUP_ROOT):
        limit, current = _number(directory / "pids.max"), _number(directory / "pids.current")
        if pids_current is None:
            pids_current = current
        if limit is not None and current is not None:
            remaining = max(0, limit - current)
            if pids_remaining is None or remaining < pids_remaining:
                pids_remaining, pids_limit, pids_current = remaining, limit, current
    cpu_capacity = min(cpu_values) if cpu_values else None
    return {"cpu_cores": cpu_capacity, "available_memory_bytes": memory_available,
            "memory_capacity_bytes": memory_limit, "memory_current_bytes": memory_current,
            "cgroup_memory_remaining_bytes": cgroup_remaining,
            "cpu_quota": quota_info, "cpu_source": cpu_sources or ["unavailable"],
            "memory_source": memory_sources or ["unavailable"],
            "cgroup_observed": bool(v2 or v1_memory or v1_cpu),
            "pids_current": pids_current, "pids_capacity": pids_limit}


def _history_estimates(store, profile: Profile, *, limit: int = _HISTORY_LIMIT) -> dict:
    """Read a small, bounded sample of prior receipts for this exact profile."""
    empty = {"observations": 0, "cpu_cores_peak": 0.0, "rss_bytes_peak": 0,
             "pids_peak": 0, "disk_bytes_peak": 0}
    if not callable(getattr(store, "rows", None)) or not getattr(store, "blobs", None):
        return {**empty, "history_state": "unavailable"}
    try:
        rows = store.rows("SELECT receipt_sha FROM runs WHERE profile_digest=? AND receipt_sha IS NOT NULL "
                          "ORDER BY updated DESC LIMIT ?", (profile_digest(profile), max(0, limit)))
    except (AttributeError, OSError, TypeError, ValueError):
        return {**empty, "history_state": "unavailable"}
    cpu_peak = memory_peak = pids_peak = disk_peak = 0
    observations = examined = 0
    for row in rows:
        examined += 1
        try:
            raw = store.blobs.read(row["receipt_sha"], limit=_HISTORY_BYTES)
            receipt = json.loads(raw)
            usage = receipt.get("resource_usage", {})
            if not isinstance(usage, dict) or not usage.get("samples"):
                continue
            row_observed = False
            if usage.get("cpu_cores_peak") is not None:
                cpu_peak = max(cpu_peak, float(usage["cpu_cores_peak"]))
                row_observed = True
            if usage.get("rss_bytes_peak") is not None:
                memory_peak = max(memory_peak, int(usage["rss_bytes_peak"]))
                row_observed = True
            if usage.get("pids_peak") is not None:
                pids_peak = max(pids_peak, int(usage["pids_peak"]))
                row_observed = True
            if usage.get("disk_bytes_peak") is not None:
                disk_peak = max(disk_peak, int(usage["disk_bytes_peak"]))
                row_observed = True
            observations += int(row_observed)
        except AuthorError as exc:
            if exc.code == "RESOURCE_LIMIT":
                # Oversized historical receipts are outside this bounded scan.
                continue
            # Missing, corrupt, or incompatible authoritative evidence cannot
            # be treated as absent because that would understate reservations.
            raise
        except OSError as exc:
            raise AuthorError("STATE_UNAVAILABLE", "Historical execution receipt could not be read") from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise AuthorError("CORRUPT_STATE", "Historical execution receipt is malformed") from exc
    return {"observations": observations, "receipts_examined": examined,
            "history_state": "observed" if observations else "no_qualifying_history",
            "cpu_cores_peak": cpu_peak,
            "rss_bytes_peak": memory_peak, "pids_peak": pids_peak, "disk_bytes_peak": disk_peak}


def profile_digest(profile: Profile) -> str:
    # Kept local to avoid broad model/API changes; the profile's stable wire form
    # is used as the exact key, as in execution admission.
    from ..util import fingerprint
    return fingerprint(profile.wire())


def can_admit(store, profile: Profile, active: list[dict]) -> tuple[bool, dict]:
    limits = store.limits
    capacity = host_capacity()
    profiles = [Profile.model_validate(json.loads(row["profile"])) for row in active]
    estimate = _history_estimates(store, profile)
    estimate_by_digest = {profile_digest(profile): estimate}
    remaining_history = max(0, _HISTORY_LIMIT - estimate.get("receipts_examined", 0))
    active_reservations = []
    for active_row, active_profile in zip(active, profiles):
        key = active_row.get("profile_digest") or profile_digest(active_profile)
        if key not in estimate_by_digest and remaining_history:
            active_estimate = _history_estimates(store, active_profile, limit=remaining_history)
            remaining_history -= active_estimate.get("receipts_examined", 0)
            estimate_by_digest[key] = active_estimate
        active_estimate = estimate_by_digest.get(key, {"cpu_cores_peak": 0.0, "rss_bytes_peak": 0,
                                                         "pids_peak": 0, "disk_bytes_peak": 0})
        active_reservations.append((
            max(active_profile.cpu, active_estimate["cpu_cores_peak"] * 1.25),
            max(active_profile.memory_mb * _MIB, int(active_estimate["rss_bytes_peak"] * 1.25)),
            max(active_profile.pids, int(active_estimate["pids_peak"] * 1.25)),
            max(active_profile.disk_mb * _MIB, int(active_estimate["disk_bytes_peak"] * 1.25))))
    allocated_cpu = sum(reservation[0] for reservation in active_reservations)
    allocated_memory = sum(reservation[1] for reservation in active_reservations)
    allocated_pids = sum(reservation[2] for reservation in active_reservations)
    allocated_disk = sum(reservation[3] for reservation in active_reservations)
    # Prior peaks increase reservations with a 25% measurement margin. Declared
    # profile limits remain the floor; a noisy or short run never reduces them.
    reserve_cpu = max(profile.cpu, estimate["cpu_cores_peak"] * 1.25)
    reserve_memory = max(profile.memory_mb * _MIB, int(estimate["rss_bytes_peak"] * 1.25))
    reserve_pids = max(profile.pids, int(estimate["pids_peak"] * 1.25))
    reserve_disk = max(profile.disk_mb * _MIB, int(estimate["disk_bytes_peak"] * 1.25))
    observed_cpu = capacity["cpu_cores"]
    # Keep at least half a core or 20% of observed capacity available for the
    # supervisor and local control-plane work.
    cpu_reserve = min(observed_cpu * .5, max(.5, observed_cpu * .2)) if observed_cpu is not None else None
    available = capacity["available_memory_bytes"]
    memory_reserve = min(available, max(256 * _MIB, int(available * .15))) if available is not None else None
    pids_capacity = capacity["pids_capacity"]
    pids_current = capacity["pids_current"]
    pids_remaining = max(0, pids_capacity - pids_current) if pids_capacity is not None and pids_current is not None else None
    try:
        disk_available = shutil.disk_usage(store.home).free
    except OSError:
        disk_available = None
    gates = {"job_slots": len(active) < limits.max_jobs,
             "configured_cpu": allocated_cpu + reserve_cpu <= limits.cpu_cores,
             "observed_cpu": observed_cpu is not None and allocated_cpu + reserve_cpu <= max(0.0, observed_cpu - cpu_reserve),
             "configured_memory": allocated_memory + reserve_memory <= limits.memory_mb * _MIB,
             "observed_memory": available is not None and allocated_memory + reserve_memory <= max(0, available - memory_reserve),
             "observed_pids": pids_remaining is None or allocated_pids + reserve_pids <= pids_remaining,
             "disk": disk_available is not None and disk_available - allocated_disk - reserve_disk >= limits.minimum_free_bytes}
    return all(gates.values()), {"gates": gates, "active_jobs": len(active), "capacity": capacity,
                                "control_reserve": {"cpu_cores": cpu_reserve, "memory_bytes": memory_reserve},
                                "historical_peak_estimate": estimate,
                                "active_reserved": {"cpu_cores": allocated_cpu, "memory_bytes": allocated_memory,
                                                    "pids": allocated_pids, "disk_bytes": allocated_disk},
                                "disk_available_bytes": disk_available,
                                "reservation": {"cpu_cores": reserve_cpu, "memory_bytes": reserve_memory,
                                                "pids": reserve_pids, "disk_bytes": reserve_disk},
                                "requested": {"cpu": profile.cpu, "memory_mb": profile.memory_mb,
                                              "pids": profile.pids, "disk_mb": profile.disk_mb}}
