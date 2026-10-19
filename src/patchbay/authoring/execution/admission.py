"""Resource admission separates author concurrency from heavy verification slots."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..models import Profile


def _read_number(path: str) -> int | None:
    try:
        text = Path(path).read_text().strip()
        return int(text) if text != "max" else None
    except (OSError, ValueError):
        return None


def host_capacity() -> dict:
    cpu = float(os.cpu_count() or 1)
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpu = min(cpu, int(quota) / int(period))
    except (OSError, ValueError, ZeroDivisionError):
        quota = _read_number("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _read_number("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota is not None and period and quota > 0:
            cpu = min(cpu, quota / period)
    available = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    maximum = _read_number("/sys/fs/cgroup/memory.max")
    current = _read_number("/sys/fs/cgroup/memory.current")
    if maximum is not None and current is not None:
        remaining = max(0, maximum - current)
        available = min(available, remaining) if available is not None else remaining
    return {"cpu_cores": cpu, "available_memory_bytes": available,
            "memory_capacity_bytes": maximum, "cgroup_observed": maximum is not None}


def can_admit(store, profile: Profile, active: list[dict]) -> tuple[bool, dict]:
    limits = store.limits
    capacity = host_capacity()
    profiles = [Profile.model_validate(json.loads(row["profile"])) for row in active]
    allocated_cpu = sum(p.cpu for p in profiles)
    allocated_memory = sum(p.memory_mb for p in profiles)
    allocated_disk = sum(p.disk_mb for p in profiles) * 1024**2
    available = capacity["available_memory_bytes"]
    free_disk = shutil.disk_usage(store.home).free
    gates = {"job_slots": len(active) < limits.max_jobs,
             "configured_cpu": allocated_cpu + profile.cpu <= limits.cpu_cores,
             "observed_cpu": profile.cpu <= capacity["cpu_cores"],
             "configured_memory": allocated_memory + profile.memory_mb <= limits.memory_mb,
             "observed_memory": available is None or profile.memory_mb * 1024**2 <= available,
             "disk": free_disk - allocated_disk - profile.disk_mb * 1024**2 >= limits.minimum_free_bytes}
    return all(gates.values()), {"gates": gates, "active_jobs": len(active), "capacity": capacity,
                                "requested": {"cpu": profile.cpu, "memory_mb": profile.memory_mb, "disk_mb": profile.disk_mb}}
