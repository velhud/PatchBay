#!/usr/bin/env python3
"""Install a per-user macOS LaunchAgent for a local PatchBay server.

The installer deliberately manages only a user LaunchAgent.  It never uses
sudo, never creates a system daemon, and never changes the private PatchBay
configuration or target allowlist. The generated plist uses a private wrapper
around the current checkout's Python interpreter so launchd can restart the
local MCP listener after an unexpected process exit without bypassing a
virtual environment.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_LABEL = "com.patchbay.local"
DEFAULT_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
_COMMON_PATH_ENTRIES = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


class LaunchAgentError(ValueError):
    """Raised when a launch-agent input is unsafe or cannot be installed."""


def _absolute_path(value: str | os.PathLike[str], *, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise LaunchAgentError(f"{name} must be an absolute path")
    return path


def _private_regular_file(value: str | os.PathLike[str], *, name: str) -> Path:
    path = _absolute_path(value, name=name)
    if not path.is_file():
        raise LaunchAgentError(f"{name} must name an existing regular file")
    if path.is_symlink():
        raise LaunchAgentError(f"{name} must not be a symlink")
    if path.stat().st_mode & 0o077:
        raise LaunchAgentError(f"{name} must be private (mode 0600 or stricter)")
    return path.resolve()


def _executable_file(value: str | os.PathLike[str], *, name: str) -> Path:
    path = _absolute_path(value, name=name)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise LaunchAgentError(f"{name} must name an executable file")
    # Preserve a virtualenv's lexical path.  macOS launchd may resolve a
    # symlinked venv interpreter when it is the plist's ProgramArguments[0],
    # which would bypass the venv site-packages.  The installer uses a small
    # shell wrapper for the real service and keeps this path intact there.
    return path.absolute()


def _owned_directory(value: str | os.PathLike[str], *, name: str, create: bool = False) -> Path:
    path = _absolute_path(value, name=name)
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise LaunchAgentError(f"{name} must name a directory")
    if path.is_symlink():
        raise LaunchAgentError(f"{name} must not be a symlink")
    stat_result = path.stat()
    if stat_result.st_uid != os.getuid():
        raise LaunchAgentError(f"{name} must be owned by the current user")
    if stat_result.st_mode & 0o002:
        raise LaunchAgentError(f"{name} must not be world-writable")
    return path.resolve()


def _launch_path(*, codex_bin: str | os.PathLike[str] | None = None) -> str:
    """Return a narrow executable search path suitable for launchd."""

    entries: list[str] = []
    if codex_bin:
        codex_path = Path(codex_bin).expanduser()
        if codex_path.is_absolute() and codex_path.parent.is_dir():
            entries.append(str(codex_path.parent))
    else:
        resolved = shutil.which("codex")
        if resolved:
            entries.append(str(Path(resolved).resolve().parent))
    entries.extend(_COMMON_PATH_ENTRIES)
    unique: list[str] = []
    for entry in entries:
        if entry not in unique:
            unique.append(entry)
    return os.pathsep.join(unique)


def build_launch_agent_plist(
    *,
    repo: str | os.PathLike[str],
    python_executable: str | os.PathLike[str],
    config: str | os.PathLike[str],
    patchbay_home: str | os.PathLike[str],
    log_dir: str | os.PathLike[str],
    label: str = DEFAULT_LABEL,
    codex_bin: str | os.PathLike[str] | None = None,
    server_launcher: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build a launchd plist dictionary without invoking launchctl."""

    if not label or any(char.isspace() for char in label) or "." not in label:
        raise LaunchAgentError("label must be a dotted launchd label without whitespace")
    repo_path = _owned_directory(repo, name="repo")
    python_path = _executable_file(python_executable, name="python_executable")
    config_path = _private_regular_file(config, name="config")
    home_path = _owned_directory(patchbay_home, name="patchbay_home")
    logs_path = _owned_directory(log_dir, name="log_dir", create=True)

    source_path = repo_path / "src"
    if not source_path.is_dir():
        raise LaunchAgentError("repo must contain a src directory")

    # Keep these values explicit and bounded.  In particular, do not copy the
    # caller's complete environment into a long-lived plist.
    environment = {
        "PATCHBAY_CONFIG": str(config_path),
        "PATCHBAY_HOME": str(home_path),
        "PYTHONPATH": str(source_path),
        "PYTHONUNBUFFERED": "1",
        "PATH": _launch_path(codex_bin=codex_bin),
    }
    program = _absolute_path(server_launcher, name="server_launcher") if server_launcher else python_path
    return {
        "Label": label,
        "ProgramArguments": ([str(program)] if server_launcher else [str(program), "-m", "patchbay.server"]),
        "WorkingDirectory": str(repo_path),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Umask": 0o077,
        "StandardOutPath": str(logs_path / "server.stdout.log"),
        "StandardErrorPath": str(logs_path / "server.stderr.log"),
        "LimitLoadToSessionType": "Aqua",
    }


def write_server_wrapper(path: str | os.PathLike[str], python_executable: str | os.PathLike[str]) -> Path:
    """Write a private wrapper that preserves a venv interpreter under launchd."""

    python_path = _executable_file(python_executable, name="python_executable")
    destination = _absolute_path(path, name="server_launcher")
    parent = _owned_directory(destination.parent, name="server_launcher_dir", create=True)
    if destination.exists() and destination.is_symlink():
        raise LaunchAgentError("server_launcher must not be a symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("#!/bin/sh\nset -eu\nexec ")
            stream.write(shlex.quote(str(python_path)))
            stream.write(" -m patchbay.server\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o700)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()


def _validate_launch_agents_directory(path: Path) -> Path:
    path = _owned_directory(path, name="launch_agents_dir", create=True)
    if path != DEFAULT_LAUNCH_AGENTS.resolve():
        # Keep normal installation confined to the current user's LaunchAgents
        # directory.  Unit tests can exercise plist generation without install.
        raise LaunchAgentError("launch_agents_dir must be the current user's ~/Library/LaunchAgents")
    return path


def _plist_path(label: str, launch_agents_dir: Path | None = None) -> Path:
    directory = _validate_launch_agents_directory(launch_agents_dir or DEFAULT_LAUNCH_AGENTS)
    return directory / f"{label}.plist"


def write_plist_atomic(path: str | os.PathLike[str], plist: Mapping[str, Any]) -> Path:
    """Write a private plist atomically and return its resolved path."""

    destination = _absolute_path(path, name="plist_path")
    parent = _validate_launch_agents_directory(destination.parent)
    if destination.exists() and destination.is_symlink():
        raise LaunchAgentError("plist_path must not be a symlink")
    parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(dict(plist), stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["launchctl", *args]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout or "launchctl failed").strip()
        raise LaunchAgentError(f"{' '.join(command)} failed: {detail[:400]}")
    return completed


def _wait_for_unload(service: str, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _launchctl("print", service, check=False).returncode:
            return
        time.sleep(0.1)
    raise LaunchAgentError(f"launchctl did not unload {service} before reinstall")


def install_launch_agent(plist_path: Path, *, label: str) -> None:
    if platform.system() != "Darwin":
        raise LaunchAgentError("macOS LaunchAgents are available only on Darwin")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    # Boot out only this installer-owned label.  A missing service is expected.
    _launchctl("bootout", service, check=False)
    _wait_for_unload(service)
    _launchctl("bootstrap", domain, str(plist_path))
    _launchctl("kickstart", "-k", service)


def uninstall_launch_agent(*, label: str, plist_path: Path | None = None) -> None:
    if platform.system() != "Darwin":
        raise LaunchAgentError("macOS LaunchAgents are available only on Darwin")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    _launchctl("bootout", service, check=False)
    _wait_for_unload(service)
    destination = plist_path or _plist_path(label)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise LaunchAgentError("plist_path must not be a symlink")
        destination.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Absolute private PatchBay runtime config (mode 0600).")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]), help="PatchBay checkout root.")
    parser.add_argument("--python", dest="python_executable", default=sys.executable, help="Absolute Python executable.")
    parser.add_argument("--patchbay-home", required=True, help="Absolute PATCHBAY_HOME runtime directory.")
    parser.add_argument("--log-dir", required=True, help="Private directory for stable stdout/stderr logs.")
    parser.add_argument("--codex-bin", default="", help="Optional absolute Codex executable path used to seed PATH.")
    parser.add_argument("--label", default=DEFAULT_LABEL, help=f"LaunchAgent label (default: {DEFAULT_LABEL}).")
    parser.add_argument("--dry-run", action="store_true", help="Write no plist and do not call launchctl.")
    parser.add_argument("--uninstall", action="store_true", help="Unload and remove the LaunchAgent.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        plist_path = _plist_path(args.label)
        if args.uninstall:
            if args.dry_run:
                print(f"would uninstall {args.label}")
            else:
                uninstall_launch_agent(label=args.label, plist_path=plist_path)
                print(f"uninstalled {args.label}")
            return 0

        plist = build_launch_agent_plist(
            repo=args.repo,
            python_executable=args.python_executable,
            config=args.config,
            patchbay_home=args.patchbay_home,
            log_dir=args.log_dir,
            label=args.label,
            codex_bin=args.codex_bin or None,
            server_launcher=str(Path(args.patchbay_home).expanduser().absolute() / "runtime" / "launchd" / "patchbay-server"),
        )
        if args.dry_run:
            plistlib.dump(plist, sys.stdout.buffer, sort_keys=False)
            return 0
        wrapper = write_server_wrapper(
            Path(args.patchbay_home).expanduser().absolute() / "runtime" / "launchd" / "patchbay-server",
            args.python_executable,
        )
        plist["ProgramArguments"] = [str(wrapper)]
        destination = write_plist_atomic(plist_path, plist)
        install_launch_agent(destination, label=args.label)
        print(f"installed {args.label}; launchd will keep the local PatchBay listener running")
        return 0
    except (LaunchAgentError, OSError, plistlib.InvalidFileException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
