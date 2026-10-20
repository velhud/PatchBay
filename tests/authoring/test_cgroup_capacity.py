"""Inherited limits constrain admission even when the leaf is unlimited."""
from pathlib import Path

from patchbay.authoring.execution import admission

MIB = 1024**2


def write(directory, **values):
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (directory / name.replace('_', '.', 1)).write_text(str(value))


def test_v2_parent_quota_and_sibling_usage_constrain_unlimited_leaf(tmp_path, monkeypatch):
    root, parent = tmp_path / "cgroup", tmp_path / "cgroup/limited"
    leaf = parent / "leaf"
    write(parent, cpu_max="50000 100000", memory_max=512*MIB, memory_current=384*MIB,
          pids_max=64, pids_current=60)
    write(leaf, cpu_max="max 100000", memory_max="max", memory_current=16*MIB,
          pids_max="max", pids_current=1)
    monkeypatch.setattr(admission, "_CGROUP_ROOT", root, raising=False)
    monkeypatch.setattr(admission, "_cgroup_dirs", lambda: (leaf, None, None))
    capacity = admission.host_capacity()
    assert capacity["cpu_cores"] <= .5
    assert capacity["available_memory_bytes"] <= 128*MIB
    assert capacity["cgroup_memory_remaining_bytes"] == 128*MIB
    assert capacity["memory_capacity_bytes"] == 512*MIB
    assert capacity["memory_current_bytes"] == 384*MIB
    assert capacity["pids_capacity"] - capacity["pids_current"] == 4


def test_v2_different_ancestors_limit_cpu_memory_and_pids(tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    parent, leaf = root / "limited", root / "limited/leaf"
    write(root, cpu_max="100000 100000", memory_max=1024*MIB, memory_current=1000*MIB,
          pids_max=128, pids_current=127)
    write(parent, cpu_max="50000 100000", memory_max=512*MIB, memory_current=16*MIB,
          pids_max=64, pids_current=2)
    write(leaf, cpu_max="max 100000", memory_max="max", memory_current=8*MIB,
          pids_max="max", pids_current=1)
    monkeypatch.setattr(admission, "_CGROUP_ROOT", root, raising=False)
    monkeypatch.setattr(admission, "_cgroup_dirs", lambda: (leaf, None, None))
    capacity = admission.host_capacity()
    assert capacity["cpu_cores"] <= .5
    assert capacity["cgroup_memory_remaining_bytes"] == 24*MIB
    assert capacity["pids_capacity"] - capacity["pids_current"] == 1


def test_v1_parent_limits_are_not_replaced_by_unlimited_leaf(tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    cpu, memory = root / "cpu/limited/leaf", root / "memory/limited/leaf"
    write(cpu.parent, cpu_cfs_quota_us=25000, cpu_cfs_period_us=100000)
    write(cpu, cpu_cfs_quota_us=-1, cpu_cfs_period_us=100000)
    write(memory.parent, memory_limit_in_bytes=256*MIB, memory_usage_in_bytes=192*MIB)
    write(memory, memory_limit_in_bytes=2**62, memory_usage_in_bytes=8*MIB)
    monkeypatch.setattr(admission, "_CGROUP_ROOT", root, raising=False)
    monkeypatch.setattr(admission, "_cgroup_dirs", lambda: (None, cpu, memory))
    capacity = admission.host_capacity()
    assert capacity["cpu_cores"] <= .25
    assert capacity["cgroup_memory_remaining_bytes"] == 64*MIB


def test_hierarchy_does_not_read_limits_outside_the_controller_mount(tmp_path, monkeypatch):
    root, leaf = tmp_path / "cgroup", tmp_path / "cgroup/leaf"
    write(tmp_path, cpu_max="1 100000", memory_max=1, memory_current=1)
    write(leaf, cpu_max="100000 100000", memory_max="max", pids_max="max")
    monkeypatch.setattr(admission, "_CGROUP_ROOT", root, raising=False)
    monkeypatch.setattr(admission, "_cgroup_dirs", lambda: (leaf, None, None))
    capacity = admission.host_capacity()
    assert capacity["cpu_cores"] > .00001
    assert capacity["cgroup_memory_remaining_bytes"] is None
