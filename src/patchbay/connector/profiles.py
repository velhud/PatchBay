"""Per-workspace launcher profile storage.

This ports the useful CodexPro idea of remembered workspace connection
profiles, but keeps token values out of saved JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml


PROFILE_VERSION = 1
TOKEN_KEYS = {"token", "auth_token", "cloudflare_token", "ngrok_token", "password", "secret"}


def patchbay_home(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    configured = env.get("PATCHBAY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".patchbay"


def profile_dir(environ: Mapping[str, str] | None = None) -> Path:
    return patchbay_home(environ) / "profiles"


def runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
    return patchbay_home(environ) / "runtime"


def runtime_path(*parts: str, environ: Mapping[str, str] | None = None) -> Path:
    return runtime_dir(environ).joinpath(*parts).expanduser().resolve(strict=False)


def resolve_runtime_path(
    configured: str | Path | None,
    *default_parts: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if configured not in (None, ""):
        return Path(os.path.expandvars(str(configured))).expanduser().resolve(strict=False)
    return runtime_path(*default_parts, environ=environ)


def normalize_logging_paths(
    config: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    logging_config = config.setdefault("logging", {})
    job_logs_configured = logging_config.get("job_logs_dir")
    job_logs_dir = resolve_runtime_path(job_logs_configured, "logs", "jobs", environ=environ)

    logging_config["audit_file"] = str(
        resolve_runtime_path(logging_config.get("audit_file"), "logs", "audit.log", environ=environ)
    )
    logging_config["job_logs_dir"] = str(job_logs_dir)

    if logging_config.get("job_state_dir") not in (None, ""):
        job_state_dir = resolve_runtime_path(logging_config.get("job_state_dir"), environ=environ)
    elif job_logs_configured not in (None, ""):
        job_state_dir = (job_logs_dir / "state").resolve(strict=False)
    else:
        job_state_dir = runtime_path("logs", "jobs", "state", environ=environ)
    logging_config["job_state_dir"] = str(job_state_dir)

    if logging_config.get("worktrees_dir") not in (None, ""):
        worktrees_dir = resolve_runtime_path(logging_config.get("worktrees_dir"), environ=environ)
    else:
        worktrees_dir = runtime_path("worktrees", "jobs", environ=environ)
    logging_config["worktrees_dir"] = str(worktrees_dir)

    return config


def normalize_root(root: str | Path) -> str:
    return str(Path(root).expanduser().resolve(strict=False))


def profile_id_for_root(root: str | Path) -> str:
    digest = hashlib.sha256(normalize_root(root).encode("utf-8")).hexdigest()
    return digest[:24]


def profile_path_for_root(root: str | Path, environ: Mapping[str, str] | None = None) -> Path:
    return profile_dir(environ) / f"{profile_id_for_root(root)}.json"


def runtime_config_path_for_root(root: str | Path, environ: Mapping[str, str] | None = None) -> Path:
    return runtime_dir(environ) / f"{profile_id_for_root(root)}.yaml"


def runtime_status_path_for_root(root: str | Path, environ: Mapping[str, str] | None = None) -> Path:
    return runtime_dir(environ) / f"{profile_id_for_root(root)}.json"


def read_workspace_profile(root: str | Path, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    normalized_root = normalize_root(root)
    path = profile_path_for_root(normalized_root, environ)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    if payload.get("root") and normalize_root(str(payload["root"])) != normalized_root:
        return {}
    return {**payload, "profile_path": str(path)}


def save_workspace_profile(
    root: str | Path,
    profile: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> str:
    normalized_root = normalize_root(root)
    target = profile_path_for_root(normalized_root, environ)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = sanitize_workspace_profile(profile)
    payload.update(
        {
            "version": PROFILE_VERSION,
            "root": normalized_root,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_private_text(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return str(target)


def write_runtime_config(
    root: str | Path,
    config: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> str:
    normalized_root = normalize_root(root)
    target = runtime_config_path_for_root(normalized_root, environ)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private_text(target, yaml.safe_dump(dict(config), sort_keys=False))
    return str(target)


def write_runtime_status(
    root: str | Path,
    status: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> str:
    normalized_root = normalize_root(root)
    target = runtime_status_path_for_root(normalized_root, environ)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = sanitize_workspace_profile(status)
    payload.setdefault("root", normalized_root)
    payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    _write_private_text(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return str(target)


def sanitize_workspace_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    return _drop_sensitive_keys(deepcopy(dict(profile)))


def _drop_sensitive_keys(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            if _is_sensitive_key(str(key)):
                continue
            cleaned[key] = _drop_sensitive_keys(child)
        return cleaned
    if isinstance(value, list):
        return [_drop_sensitive_keys(child) for child in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(token_key in lowered for token_key in TOKEN_KEYS)


def _write_private_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
