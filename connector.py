"""Connector readiness and ChatGPT Server URL helpers."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from auth import AuthConfigurationError, AuthPolicy, auth_public_metadata, build_auth_policy, is_loopback_host


def connector_status(
    config: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    public_base_url: str | None = None,
    reveal_token: bool = False,
) -> dict[str, Any]:
    """Return bounded connector readiness and connection metadata."""
    server_config = _mapping(config.get("server"))
    logging_config = _mapping(config.get("logging"))
    security_config = _mapping(config.get("security"))
    repo_config = _mapping(config.get("repositories"))
    power_config = _mapping(config.get("power_tools"))
    app_config = _mapping(config.get("app"))

    auth_error = None
    try:
        policy = build_auth_policy(config, environ=environ)
    except AuthConfigurationError as error:
        auth_error = str(error)
        policy = AuthPolicy(enabled=True, token=None, required_reasons=("configuration_error",))

    checks: list[dict[str, str]] = []
    _check(checks, "python", "pass", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    codex_path = shutil.which("codex")
    _check(checks, "codex_cli", "pass" if codex_path else "warn", "available on PATH" if codex_path else "not found on PATH")

    host = str(server_config.get("host") or "127.0.0.1")
    if is_loopback_host(host):
        _check(checks, "host_binding", "pass", f"{host} is loopback")
    elif policy.enabled and policy.token_configured:
        _check(checks, "host_binding", "warn", f"{host} is non-loopback and token protected")
    else:
        _check(checks, "host_binding", "fail", f"{host} is non-loopback without token protection")

    if auth_error:
        _check(checks, "http_auth", "fail", auth_error)
    elif policy.enabled:
        _check(checks, "http_auth", "pass", "token required")
    else:
        _check(checks, "http_auth", "pass", "trusted loopback mode without token")

    _check(
        checks,
        "access_log",
        "pass" if not logging_config.get("access_log", False) else "warn",
        "disabled" if not logging_config.get("access_log", False) else "enabled; query-token URLs may appear in access logs",
    )
    _check(
        checks,
        "cors",
        "pass" if not server_config.get("enable_cors", False) else "warn",
        "disabled" if not server_config.get("enable_cors", False) else "enabled for configured origins",
    )
    _check(
        checks,
        "default_sandbox",
        "pass" if security_config.get("default_sandbox", "read-only") == "read-only" else "warn",
        str(security_config.get("default_sandbox", "read-only")),
    )
    _check(
        checks,
        "direct_write",
        "warn" if power_config.get("direct_write", False) else "pass",
        "enabled" if power_config.get("direct_write", False) else "disabled",
    )
    bash_mode = str(power_config.get("bash_mode", "off"))
    _check(
        checks,
        "bash_mode",
        "warn" if bash_mode == "full" else "pass",
        bash_mode,
    )

    allowed_roots = [Path(str(path)).expanduser() for path in repo_config.get("allowed") or []]
    existing = [path for path in allowed_roots if path.exists()]
    if not allowed_roots:
        _check(checks, "allowed_roots", "fail", "no allowed repositories configured")
    elif len(existing) == len(allowed_roots):
        _check(checks, "allowed_roots", "pass", f"{len(existing)} configured root(s) exist")
    else:
        _check(checks, "allowed_roots", "fail", f"{len(existing)}/{len(allowed_roots)} configured root(s) exist")

    local_base = f"http://{host}:{int(server_config.get('port') or 8000)}"
    server_url = mcp_server_url(config, policy, public_base_url=public_base_url, reveal_token=reveal_token)

    return {
        "name": "codex-mcp-wrapper",
        "ready": not any(check["status"] == "fail" for check in checks),
        "checks": checks,
        "connection": {
            "local_mcp_url": f"{local_base}/mcp",
            "server_url": server_url,
            "public_base_url": public_base_url,
            "chatgpt_authentication": "No Authentication / None when using a query-token Server URL; Bearer token when custom headers are supported.",
            "query_token_url_redacted": policy.enabled and policy.allow_query_token and not reveal_token,
            "tool_mode": app_config.get("tool_mode", "full"),
        },
        "auth": auth_public_metadata(policy),
        "power_tools": {
            "direct_write": bool(power_config.get("direct_write", False)),
            "bash_mode": bash_mode,
            "bash_session_id": power_config.get("bash_session_id") or None,
            "require_bash_session": bool(power_config.get("require_bash_session", False)),
            "bash_transcript": power_config.get("bash_transcript", "compact"),
            "codex_session_read": bool(power_config.get("codex_session_read", False)),
            "codex_home_configured": bool(power_config.get("codex_home")),
        },
    }


def mcp_server_url(
    config: Mapping[str, Any],
    policy: AuthPolicy,
    *,
    public_base_url: str | None = None,
    reveal_token: bool = False,
) -> str:
    server_config = _mapping(config.get("server"))
    base = (public_base_url or f"http://{server_config.get('host', '127.0.0.1')}:{int(server_config.get('port') or 8000)}").rstrip("/")
    url = f"{base}/mcp"
    if policy.enabled and policy.allow_query_token:
        token_display = policy.token if reveal_token and policy.token else "<redacted>"
        url = f"{url}?{policy.query_token_names[0]}={quote(token_display)}"
    return url


def format_doctor_text(status: Mapping[str, Any]) -> str:
    lines = ["Codex MCP Wrapper doctor", ""]
    for check in status.get("checks", []):
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    lines.extend(
        [
            "",
            f"Ready: {'yes' if status.get('ready') else 'no'}",
            f"Local MCP URL: {status['connection']['local_mcp_url']}",
            f"ChatGPT Server URL: {status['connection']['server_url']}",
            f"Authentication: {status['connection']['chatgpt_authentication']}",
        ]
    )
    return "\n".join(lines)


def format_doctor_json(status: Mapping[str, Any]) -> str:
    return json.dumps(status, indent=2, sort_keys=True)


def _check(checks: list[dict[str, str]], name: str, status: str, detail: str) -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
