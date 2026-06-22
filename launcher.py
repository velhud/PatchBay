"""CodexPro-style launcher helpers for codex-mcp-wrapper."""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import yaml

from connector import connector_status
from profile_store import (
    normalize_root,
    read_workspace_profile,
    sanitize_workspace_profile,
    save_workspace_profile,
    write_runtime_config,
)


BASH_MODES = {"off", "safe", "full"}
TUNNEL_MODES = {"none", "local", "custom", "cloudflare", "cloudflare-named", "ngrok"}
TOOL_MODES = {"minimal", "standard", "full"}


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("Config file must contain a YAML object")
    return payload


def prepare_start(
    base_config: Mapping[str, Any],
    *,
    root: str | None = None,
    allow_roots: Sequence[str] = (),
    host: str | None = None,
    port: int | None = None,
    public_base_url: str | None = None,
    tunnel_mode: str | None = None,
    hostname: str | None = None,
    tunnel_name: str | None = None,
    cloudflared: str | None = None,
    ngrok: str | None = None,
    cloudflare_config: str | None = None,
    cloudflare_token_file: str | None = None,
    cloudflare_token_env: str | None = None,
    ngrok_config: str | None = None,
    tunnel_timeout_seconds: int | None = None,
    use_profile: bool = True,
    save_profile: bool = False,
    direct_write: bool | None = None,
    bash_mode: str | None = None,
    bash_session_id: str | None = None,
    require_bash_session: bool | None = None,
    codex_session_read: bool | None = None,
    widget_domain: str | None = None,
    tool_mode: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return runtime config, profile metadata, and connector status for launcher use."""
    env = environ if environ is not None else os.environ
    config = deepcopy(dict(base_config))
    requested_root = normalize_root(root or _default_root(config))

    profile: dict[str, Any] = {}
    if use_profile:
        profile = read_workspace_profile(requested_root, env)
        _apply_profile(config, profile)

    _apply_cli_overrides(
        config,
        root=requested_root,
        allow_roots=allow_roots,
        host=host,
        port=port,
        public_base_url=public_base_url,
        tunnel_mode=tunnel_mode,
        hostname=hostname,
        tunnel_name=tunnel_name,
        cloudflared=cloudflared,
        ngrok=ngrok,
        cloudflare_config=cloudflare_config,
        cloudflare_token_file=cloudflare_token_file,
        cloudflare_token_env=cloudflare_token_env,
        ngrok_config=ngrok_config,
        tunnel_timeout_seconds=tunnel_timeout_seconds,
        direct_write=direct_write,
        bash_mode=bash_mode,
        bash_session_id=bash_session_id,
        require_bash_session=require_bash_session,
        codex_session_read=codex_session_read,
        widget_domain=widget_domain,
        tool_mode=tool_mode,
    )

    effective_root = normalize_root(config["repositories"]["default"])
    effective_public_base_url = public_base_url if public_base_url is not None else profile.get("public_base_url")

    profile_path = None
    if save_profile:
        profile_path = save_workspace_profile(effective_root, _profile_from_config(config, effective_public_base_url), env)

    runtime_config_path = write_runtime_config(effective_root, config, env)
    status = connector_status(config, environ=env, public_base_url=effective_public_base_url)

    return {
        "runtime_config": config,
        "runtime_config_path": runtime_config_path,
        "profile": {
            "used": bool(profile),
            "saved": bool(profile_path),
            "profile_path": profile_path or profile.get("profile_path"),
            "public_base_url": effective_public_base_url,
        },
        "status": status,
    }


def launcher_json_payload(prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded JSON payload suitable for CLI output."""
    status = dict(prepared["status"])
    return {
        "name": "codex-mcp-wrapper",
        "ready": status.get("ready"),
        "runtime_config_path": prepared.get("runtime_config_path"),
        "profile": sanitize_workspace_profile(prepared.get("profile", {})),
        "connection": status.get("connection", {}),
        "auth": status.get("auth", {}),
        "power_tools": status.get("power_tools", {}),
        "checks": status.get("checks", []),
    }


def _default_root(config: Mapping[str, Any]) -> str:
    repositories = _section(config, "repositories")
    return str(repositories.get("default") or ".")


def _apply_profile(config: dict[str, Any], profile: Mapping[str, Any]) -> None:
    if not profile:
        return
    _merge_known(config, profile, "server", {"host", "port"})
    _merge_known(config, profile, "auth", {"tunnel_mode", "allow_query_token"})
    _merge_known(
        config,
        profile,
        "tunnel",
        {
            "hostname",
            "tunnel_name",
            "cloudflared",
            "ngrok",
            "cloudflare_config",
            "cloudflare_token_file",
            "cloudflare_token_env",
            "ngrok_config",
            "timeout_seconds",
        },
    )
    _merge_known(config, profile, "app", {"widget_domain", "tool_mode"})
    _merge_known(
        config,
        profile,
        "power_tools",
        {
            "direct_write",
            "bash_mode",
            "bash_transcript",
            "bash_session_id",
            "require_bash_session",
            "codex_session_read",
            "codex_home",
        },
    )


def _apply_cli_overrides(
    config: dict[str, Any],
    *,
    root: str,
    allow_roots: Sequence[str],
    host: str | None,
    port: int | None,
    public_base_url: str | None,
    tunnel_mode: str | None,
    hostname: str | None,
    tunnel_name: str | None,
    cloudflared: str | None,
    ngrok: str | None,
    cloudflare_config: str | None,
    cloudflare_token_file: str | None,
    cloudflare_token_env: str | None,
    ngrok_config: str | None,
    tunnel_timeout_seconds: int | None,
    direct_write: bool | None,
    bash_mode: str | None,
    bash_session_id: str | None,
    require_bash_session: bool | None,
    codex_session_read: bool | None,
    widget_domain: str | None,
    tool_mode: str | None,
) -> None:
    repositories = config.setdefault("repositories", {})
    repositories["default"] = root
    allowed = [root]
    allowed.extend(normalize_root(item) for item in allow_roots)
    repositories["allowed"] = _unique(allowed)

    server = config.setdefault("server", {})
    if host:
        server["host"] = host
    if port is not None:
        server["port"] = _validate_port(port)

    auth = config.setdefault("auth", {})
    effective_tunnel_mode = tunnel_mode
    if public_base_url and not effective_tunnel_mode:
        effective_tunnel_mode = "custom"
    if effective_tunnel_mode:
        auth["tunnel_mode"] = _validate_tunnel_mode(effective_tunnel_mode)

    tunnel = config.setdefault("tunnel", {})
    if hostname is not None:
        tunnel["hostname"] = hostname
    if tunnel_name is not None:
        tunnel["tunnel_name"] = tunnel_name
    if cloudflared is not None:
        tunnel["cloudflared"] = cloudflared
    if ngrok is not None:
        tunnel["ngrok"] = ngrok
    if cloudflare_config is not None:
        tunnel["cloudflare_config"] = cloudflare_config
    if cloudflare_token_file is not None:
        tunnel["cloudflare_token_file"] = cloudflare_token_file
    if cloudflare_token_env is not None:
        tunnel["cloudflare_token_env"] = cloudflare_token_env
    if ngrok_config is not None:
        tunnel["ngrok_config"] = ngrok_config
    if tunnel_timeout_seconds is not None:
        tunnel["timeout_seconds"] = _validate_timeout(tunnel_timeout_seconds)

    if widget_domain:
        config.setdefault("app", {})["widget_domain"] = _validate_widget_domain(widget_domain)
    if tool_mode:
        config.setdefault("app", {})["tool_mode"] = _validate_tool_mode(tool_mode)

    power = config.setdefault("power_tools", {})
    if direct_write is not None:
        power["direct_write"] = direct_write
    if bash_mode is not None:
        power["bash_mode"] = _validate_bash_mode(bash_mode)
    if bash_session_id is not None:
        power["bash_session_id"] = _validate_bash_session_id(bash_session_id)
    if require_bash_session is not None:
        power["require_bash_session"] = require_bash_session
    if codex_session_read is not None:
        power["codex_session_read"] = codex_session_read


def _profile_from_config(config: Mapping[str, Any], public_base_url: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "public_base_url": public_base_url,
        "server": _select_keys(_section(config, "server"), {"host", "port"}),
        "auth": _select_keys(_section(config, "auth"), {"tunnel_mode", "allow_query_token"}),
        "tunnel": _select_keys(
            _section(config, "tunnel"),
            {
                "hostname",
                "tunnel_name",
                "cloudflared",
                "ngrok",
                "cloudflare_config",
                "cloudflare_token_file",
                "cloudflare_token_env",
                "ngrok_config",
                "timeout_seconds",
            },
        ),
        "app": _select_keys(_section(config, "app"), {"widget_domain", "tool_mode"}),
        "power_tools": _select_keys(
            _section(config, "power_tools"),
            {
                "direct_write",
                "bash_mode",
                "bash_transcript",
                "bash_session_id",
                "require_bash_session",
                "codex_session_read",
                "codex_home",
            },
        ),
    }
    return {key: value for key, value in payload.items() if value not in ({}, None)}


def _merge_known(config: dict[str, Any], profile: Mapping[str, Any], section: str, keys: set[str]) -> None:
    source = profile.get(section)
    if not isinstance(source, Mapping):
        return
    target = config.setdefault(section, {})
    for key in keys:
        if key in source:
            target[key] = source[key]


def _section(config: Mapping[str, Any], section: str) -> Mapping[str, Any]:
    value = config.get(section)
    return value if isinstance(value, Mapping) else {}


def _select_keys(source: Mapping[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


def _unique(paths: Sequence[str]) -> list[str]:
    result = []
    seen = set()
    for item in paths:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _validate_port(port: int) -> int:
    if not 1 <= int(port) <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return int(port)


def _validate_bash_mode(mode: str) -> str:
    if mode not in BASH_MODES:
        raise ValueError(f"bash mode must be one of: {', '.join(sorted(BASH_MODES))}")
    return mode


def _validate_tunnel_mode(mode: str) -> str:
    if mode not in TUNNEL_MODES:
        raise ValueError(f"tunnel mode must be one of: {', '.join(sorted(TUNNEL_MODES))}")
    return mode


def _validate_tool_mode(mode: str) -> str:
    if mode not in TOOL_MODES:
        raise ValueError(f"tool mode must be one of: {', '.join(sorted(TOOL_MODES))}")
    return mode


def _validate_timeout(value: int) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 300:
        raise ValueError("tunnel timeout must be between 1 and 300 seconds")
    return parsed


def _validate_widget_domain(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("widget domain must be an https origin, for example https://widgets.example.com")
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_bash_session_id(value: str) -> str:
    if not value:
        return value
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ValueError("bash session id must be 1-64 characters and start with a letter or number")
    return value
