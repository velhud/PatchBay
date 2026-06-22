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


def codex_mcp_home(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    configured = env.get("CODEX_MCP_HOME") or env.get("CODEX_WRAPPER_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".codex-mcp-wrapper"


def profile_dir(environ: Mapping[str, str] | None = None) -> Path:
    return codex_mcp_home(environ) / "profiles"


def runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
    return codex_mcp_home(environ) / "runtime"


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
