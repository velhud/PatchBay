#!/usr/bin/env python3
"""Run the opt-in Author storage lab inside a private, bounded tmpfs mount."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TEST = "tests/authoring/test_storage_lab.py"
ADMIN = "/usr/local/bin/codex-host-admin"
RAW_DEFAULT = REPO / ".local/validation/storage-lab"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-c", f"safe.directory={REPO}", *args], cwd=REPO, text=True).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(raw_dir: Path, stem: str, meta: dict, output: str, status: int) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{stem}.log").write_text(output, encoding="utf-8")
    meta.update(exit_code=status, log=f"{stem}.log")
    (raw_dir / f"{stem}.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _inner(mountpoint: Path, raw_dir: Path, stem: str, expected_parent_namespace: int,
           namespace_launcher: str) -> int:
    if mountpoint.is_symlink() or not mountpoint.is_dir() or any(mountpoint.iterdir()):
        raise RuntimeError("mountpoint must be an existing, empty, non-symlink directory")
    if os.stat("/proc/self/ns/mnt").st_ino == expected_parent_namespace:
        raise RuntimeError("refusing to mount: private mount namespace was not established")
    resolved_mount = mountpoint.resolve()
    resolved_raw = raw_dir.resolve()
    if resolved_raw == resolved_mount or resolved_mount in resolved_raw.parents:
        raise RuntimeError("raw evidence directory must be outside the disposable mount")
    mounted = False
    try:
        subprocess.run(["mount", "-t", "tmpfs", "-o", "size=64m,nr_inodes=2048,mode=700", "tmpfs", str(mountpoint)], check=True)
        mounted = True
        usage = shutil.disk_usage(mountpoint)
        stat = os.statvfs(mountpoint)
        info = {
            "schema_version": 1,
            "commit": _git("rev-parse", "HEAD"),
            "git_status": _git("status", "--short"),
            "command": f"sudo [{namespace_launcher}] unshare --mount --propagation private python scripts/author_storage_lab.py",
            "namespace_launcher": namespace_launcher,
            "parent_mount_namespace_inode": expected_parent_namespace,
            "lab_mount_namespace_inode": os.stat("/proc/self/ns/mnt").st_ino,
            "source_sha256": {
                "scripts/author_storage_lab.py": _sha256(REPO / "scripts/author_storage_lab.py"),
                "tests/authoring/test_storage_lab.py": _sha256(REPO / TEST),
                "src/patchbay/authoring/persistence/maintenance.py": _sha256(
                    REPO / "src/patchbay/authoring/persistence/maintenance.py"),
            },
            "python": sys.version,
            "platform": platform.platform(),
            "kernel": platform.release(),
            "mount_type": "tmpfs",
            "mount_size_limit_bytes": 64 * 1024**2,
            "mount_inode_limit": 2048,
            "mount_observed_total_bytes": usage.total,
            "mount_observed_free_bytes_before": usage.free,
            "mount_observed_inode_count": stat.f_files,
            "mount_observed_free_inodes_before": stat.f_ffree,
            "explicit_mount": "size=64m,nr_inodes=2048,mode=700",
            "evidence_outside_mount": True,
        }
        (raw_dir / f"{stem}.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        env = {**os.environ, "PATCHBAY_STORAGE_LAB": "1"}
        command = [sys.executable, "-m", "pytest", TEST, "-q", "-p", "no:cacheprovider",
                   "--basetemp", str(mountpoint / "pytest-tmp")]
        result = subprocess.run(command, cwd=REPO, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _record(raw_dir, stem, info, result.stdout, result.returncode)
        print(result.stdout, end="")
        return result.returncode
    finally:
        if mounted:
            subprocess.run(["umount", str(mountpoint)], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW_DEFAULT,
                        help="private evidence directory outside the temporary mount")
    parser.add_argument("--host-admin", help="optional host admin executable used to invoke unshare")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mountpoint", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--stem", help=argparse.SUPPRESS)
    parser.add_argument("--parent-mnt-namespace", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--namespace-launcher", default="unshare", help=argparse.SUPPRESS)
    args = parser.parse_args()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = args.stem or f"storage-lab-{stamp}"
    raw_dir = args.raw_dir.expanduser().absolute()
    if args.inside:
        if not args.mountpoint or args.parent_mnt_namespace is None:
            parser.error("--inside requires mountpoint and parent namespace identity")
        return _inner(args.mountpoint.absolute(), raw_dir, stem, args.parent_mnt_namespace,
                      args.namespace_launcher)

    raw_dir.mkdir(parents=True, exist_ok=True)
    launcher = args.host_admin or (ADMIN if shutil.which(ADMIN) else None)
    mountpoint = Path(tempfile.mkdtemp(prefix="patchbay-storage-lab-")).absolute()
    parent_namespace = os.stat("/proc/self/ns/mnt").st_ino
    unshare = ["sudo", launcher, "unshare"] if launcher else ["sudo", "unshare"]
    launcher_name = "host-admin" if launcher else "unshare"
    command = [*unshare, "--mount", "--propagation", "private",
               sys.executable, str(Path(__file__).resolve()), "--inside", "--mountpoint", str(mountpoint),
               "--raw-dir", str(raw_dir), "--stem", stem,
               "--parent-mnt-namespace", str(parent_namespace),
               "--namespace-launcher", launcher_name]
    try:
        result = subprocess.run(command, cwd=REPO, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = result.stdout
        log_path = raw_dir / f"{stem}.log"
        if not log_path.exists():
            meta = {
                "schema_version": 1,
                "commit": _git("rev-parse", "HEAD"),
                "command": f"sudo [{launcher_name}] unshare --mount --propagation private python scripts/author_storage_lab.py",
                "namespace_launcher": launcher_name,
                "mount_size_limit_bytes": 64 * 1024**2,
                "mount_inode_limit": 2048,
                "evidence_outside_mount": True,
            }
            _record(raw_dir, stem, meta, output, result.returncode)
        if output:
            print(output, end="")
        print(f"private storage-lab evidence: {raw_dir / (stem + '.json')}")
        return result.returncode
    finally:
        try:
            mountpoint.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
