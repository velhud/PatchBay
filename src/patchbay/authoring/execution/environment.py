"""Explicit execution backends: isolated offline Docker or operator-trusted local code."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..errors import AuthorError, require
from ..models import Check, Profile
from ..util import fingerprint, safe_path


def base_environment(home: Path) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(home),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0"}


def qualify(profile: Profile, run_root: Path) -> tuple[Profile, dict]:
    require(sys.platform == "linux", "BLOCKED_ENVIRONMENT", "This execution supervisor requires Linux/WSL2")
    information = {"backend": profile.backend, "platform": platform.platform(),
                   "python": sys.version, "profile_id": profile.id,
                   "network_denied_during_checks": profile.backend == "docker",
                   "provider_credentials_mounted": False if profile.backend == "docker" else "not_isolated",
                   "delegated_inference_by_author_service": False}
    if profile.backend == "trusted-local":
        information["execution_trust"] = "operator-trusted source; not an adversarial sandbox or network-denial guarantee"
        information["environment_digest"] = fingerprint(information)
        return profile, information
    require(profile.image, "BLOCKED_ENVIRONMENT", "Docker backend needs an explicitly prepared image")
    require(shutil.which("docker"), "MISSING_EXECUTABLE", "Docker executable is unavailable")
    require("," not in str(run_root) and "\n" not in str(run_root), "BLOCKED_ENVIRONMENT", "Docker bind paths contain unsupported characters")
    try:
        probe = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", profile.image],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=base_environment(run_root), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuthorError("BLOCKED_ENVIRONMENT", "Docker image qualification could not complete") from exc
    require(probe.returncode == 0, "BLOCKED_ENVIRONMENT", "Prepared Docker image is unavailable; images are not pulled automatically")
    image_id = probe.stdout.decode().strip()
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image_id), "BLOCKED_ENVIRONMENT", "Docker did not identify exact image content")
    information.update(image_id=image_id, execution_trust="isolated-container", setup_network=profile.setup_network)
    information["environment_digest"] = fingerprint(information)
    return profile.model_copy(update={"image": image_id}), information


def command(profile: Profile, check: Check, workspace: Path, run_root: Path, *,
            container_name: str, stage: str) -> tuple[list[str], Path, dict]:
    safe_path(check.cwd, allow_dot=True)
    cwd = workspace / check.cwd
    require(cwd.is_dir() and cwd.resolve().is_relative_to(workspace.resolve()),
            "BLOCKED_ENVIRONMENT", "Approved check directory is unavailable or leaves its workspace")
    home = run_root / "home"
    home.mkdir(exist_ok=True, mode=0o700)
    env = base_environment(home)
    if profile.backend == "trusted-local":
        env.update(profile.environment)
        executable = check.argv[0]
        found = shutil.which(executable, path=env.get("PATH")) if "/" not in executable else executable
        if not found or ("/" in executable and not os.path.exists(found)):
            raise AuthorError("MISSING_EXECUTABLE", f"Approved check executable is unavailable: {executable[:300]}")
        if "/" in executable and not os.access(found, os.X_OK):
            raise AuthorError("EXECUTABLE_NOT_RUNNABLE", f"Approved check executable lacks execute permission: {executable[:300]}")
        return list(check.argv), cwd, env
    if not shutil.which("docker", path=env.get("PATH")):
        raise AuthorError("MISSING_EXECUTABLE", "Docker executable is unavailable")
    require(profile.image is not None, "BLOCKED_ENVIRONMENT", "Docker image is not configured")
    cidfile = run_root / f"{container_name}.cid"
    argv = ["docker", "create", "--cidfile", str(cidfile), "--name", container_name, "--pull=never", "--read-only",
            "--network", "bridge" if stage == "setup" and profile.setup_network else "none",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit", str(profile.pids),
            "--memory", str(profile.memory_mb) + "m", "--memory-swap", str(profile.memory_mb) + "m",
            "--cpus", str(profile.cpu), "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            "--workdir", "/workspace" + ("/" + check.cwd if check.cwd != "." else ""),
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_CONFIG_GLOBAL=/dev/null"]
    for key, value in sorted(profile.environment.items()):
        argv.extend(["--env", key + "=" + value])
    argv.append(profile.image)
    argv.extend(check.argv)
    return argv, run_root, env


def start_command(container_id: str) -> list[str]:
    """Start and attach to one container by its acknowledged immutable ID."""
    require(re.fullmatch(r"[0-9a-f]{64}", container_id), "INVALID_REQUEST", "Invalid owned container ID")
    return ["docker", "start", "--attach", container_id]


def _daemon_id(env: dict) -> str | None:
    try:
        result = subprocess.run(["docker", "info", "--format", "{{.ID}}"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.decode("utf-8", "replace").strip()
    return value[:256] if result.returncode == 0 and value else None


def inspect_container(name: str, *, expected_daemon_id: str | None = None) -> dict:
    """Read bounded Docker state before cleanup, preserving honest absence."""
    require(re.fullmatch(r"(?:pba-[a-z0-9-]+|[0-9a-f]{64})", name), "INVALID_REQUEST", "Invalid owned container identity")
    env = base_environment(Path("/nonexistent"))
    if expected_daemon_id and _daemon_id(env) != expected_daemon_id:
        return {"state": "unavailable", "reason": "docker_daemon_identity_mismatch"}
    try:
        result = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", name],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "unavailable", "reason": type(exc).__name__}
    if result.returncode:
        return {"state": "unavailable", "reason": "inspect_failed", "exit_code": result.returncode}
    try:
        state = json.loads(result.stdout[:65536])
    except (ValueError, UnicodeDecodeError):
        return {"state": "unavailable", "reason": "invalid_inspect_output"}
    return {"state": "observed", "oom_killed": bool(state.get("OOMKilled")),
            "exit_code": state.get("ExitCode"), "status": state.get("Status"),
            "error": str(state.get("Error") or "")[:1000]}


def cleanup_container(container_id: str, *, expected_daemon_id: str | None = None) -> dict:
    require(re.fullmatch(r"[0-9a-f]{64}", container_id), "INVALID_REQUEST", "Invalid owned container ID")
    env = base_environment(Path("/nonexistent"))
    if not expected_daemon_id or _daemon_id(env) != expected_daemon_id:
        return {"state": "unresolved", "message": "Docker daemon identity could not be matched before cleanup"}
    try:
        removed = subprocess.run(["docker", "rm", "--force", container_id], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env, timeout=20)
        observed = subprocess.run(["docker", "container", "inspect", container_id], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, timeout=10)
        daemon_after = _daemon_id(env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "unresolved", "message": str(exc)[:1000]}
    if daemon_after != expected_daemon_id:
        return {"state": "unresolved", "message": "Docker daemon identity changed during cleanup"}
    absent = observed.returncode != 0 and b"No such" in observed.stderr
    return {"state": "proven" if absent else "unresolved", "remove_exit_code": removed.returncode,
            "absence_confirmed": absent}
