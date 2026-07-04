"""Connector readiness and ChatGPT Server URL helpers."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from patchbay.auth import AuthConfigurationError, AuthPolicy, auth_public_metadata, build_auth_policy, is_loopback_host
from patchbay.security import public_error_message


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
        auth_error = public_error_message(error, default="Authentication configuration error.", allow_details=True)
        policy = AuthPolicy(enabled=True, token=None, required_reasons=("configuration_error",))

    checks: list[dict[str, str]] = []
    _check(checks, "python", "pass", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    path_env = str(_mapping(environ).get("PATH", "")) if environ is not None else None
    _binary_check(checks, "codex_cli", "codex", path=path_env, missing_status="warn")
    _binary_check(checks, "git", "git", path=path_env, missing_status="warn")
    _binary_check(checks, "bash", "bash", path=path_env, missing_status="warn")
    _binary_check(checks, "ripgrep", "rg", path=path_env, missing_status="warn")
    _binary_check(checks, "python3", "python3", path=path_env, missing_status="warn")

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

    auth_metadata = auth_public_metadata(policy)
    if reveal_token and policy.enabled and policy.allow_query_token and policy.token:
        auth_metadata["token_returned"] = True

    return {
        "name": "patchbay",
        "ready": not any(check["status"] == "fail" for check in checks),
        "checks": checks,
        "connection": {
            "local_mcp_url": f"{local_base}/mcp",
            "server_url": server_url,
            "public_base_url": public_base_url,
            "chatgpt_authentication": "No Authentication / None when using a query-token Server URL; Bearer token when custom headers are supported.",
            "query_token_url_redacted": policy.enabled and policy.allow_query_token and not reveal_token,
            "tool_mode": app_config.get("tool_mode", "worker"),
        },
        "auth": auth_metadata,
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


def connector_setup_guide(
    config: Mapping[str, Any],
    status: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return structured first-run guidance for ChatGPT connector setup."""
    repo_config = _mapping(config.get("repositories"))
    auth_config = _mapping(config.get("auth"))
    tunnel_config = _mapping(config.get("tunnel"))
    app_config = _mapping(config.get("app"))
    connection = _mapping(status.get("connection"))
    auth = _mapping(status.get("auth"))
    profile = _mapping(profile)

    server_url = str(connection.get("server_url") or "")
    query_redacted = bool(connection.get("query_token_url_redacted"))
    token_note = (
        "Use --reveal-token only when privately copying the Server URL into ChatGPT; keep logs redacted."
        if query_redacted
        else "Use the displayed Server URL exactly as shown for this run."
    )
    tunnel_mode = str(auth_config.get("tunnel_mode") or "none")
    default_root = str(repo_config.get("default") or "")
    allowed_roots = [str(path) for path in repo_config.get("allowed") or []]

    return {
        "purpose": "Connect ChatGPT Developer Mode to this local PatchBay server.",
        "server_url": server_url,
        "local_mcp_url": connection.get("local_mcp_url"),
        "authentication": connection.get("chatgpt_authentication"),
        "tool_mode": app_config.get("tool_mode", connection.get("tool_mode", "worker")),
        "tunnel_mode": tunnel_mode,
        "default_root": default_root,
        "allowed_roots": allowed_roots,
        "profile": {
            "used": bool(profile.get("used")),
            "saved": bool(profile.get("saved")),
            "profile_path": profile.get("profile_path"),
            "public_base_url": profile.get("public_base_url"),
        },
        "chatgpt_steps": [
            "Open ChatGPT Settings -> Apps & Connectors -> Advanced settings.",
            "Enable Developer mode and keep Enforce CSP in developer mode enabled.",
            "Create a connector/app named PatchBay.",
            "Paste the Server URL from this guide.",
            "Use No Authentication / None for a query-token Server URL, or Bearer token only when your ChatGPT UI supports custom headers.",
            "Open a new chat, add the PatchBay connector, then start with codex_self_test and codex_open_workspace.",
            "Act as manager of local Codex workers, not as the primary file reader; after self-test/orientation, appoint workers for non-trivial repository work.",
            "If a repo name is unclear, use codex_list_workspaces with query/discover instead of guessing paths.",
        ],
        "operator_commands": [
            "patchbay doctor --json",
            "patchbay start --root <repo> --tool-mode worker --print-only --json",
            "patchbay setup",
            "patchbay start --root <repo> --tool-mode worker --save-profile",
            "patchbay start --root <repo> --public-base-url https://your-tunnel.example --reveal-token --print-only",
            "python scripts/start.py --root <repo> --tool-mode worker --print-only --json",
        ],
        "controls": [
            "--tool-mode worker is the recommended first ChatGPT validation surface.",
            "--allow-root must be repeated for every additional repository ChatGPT may access.",
            "--save-profile stores reusable non-secret launch settings for this workspace.",
            "--no-profile ignores saved settings for a deliberate one-off run.",
            "--tunnel-mode none/local/custom/ngrok/cloudflare/cloudflare-named selects how ChatGPT reaches /mcp.",
            "--reveal-token is explicit because tokenized Server URLs are private copy/paste material.",
        ],
        "warnings": [
            token_note,
            "Public or tunnel URLs must be token protected.",
            "Quick tunnel URLs can change on restart; stable hostnames avoid repeated ChatGPT connector edits.",
            "Tunnel binaries are installed only by explicit commands such as patchbay install-cloudflared.",
        ],
        "runtime": {
            "ready": bool(status.get("ready")),
            "checks": status.get("checks", []),
            "auth_enabled": bool(auth.get("enabled")),
            "query_token_url_redacted": query_redacted,
            "hostname": tunnel_config.get("hostname"),
        },
    }


def format_setup_guide_text(guide: Mapping[str, Any]) -> str:
    """Render structured setup guidance as concise terminal text."""
    lines = [
        "ChatGPT setup",
        f"Server URL: {guide.get('server_url')}",
        f"Authentication: {guide.get('authentication')}",
        f"Tool mode: {guide.get('tool_mode')}",
        f"Tunnel mode: {guide.get('tunnel_mode')}",
        "",
        "Steps:",
    ]
    lines.extend(f"{index}. {step}" for index, step in enumerate(guide.get("chatgpt_steps", []), start=1))
    lines.extend(["", "Useful commands:"])
    lines.extend(f"- {command}" for command in guide.get("operator_commands", []))
    warnings = list(guide.get("warnings", []))
    if warnings:
        lines.extend(["", "Notes:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


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
    lines = ["PatchBay doctor", ""]
    for check in status.get("checks", []):
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    lines.extend(
        [
            "",
            f"Ready: {'yes' if status.get('ready') else 'no'}",
            f"Local MCP URL: {status['connection']['local_mcp_url']}",
            f"ChatGPT Server URL: {status['connection']['server_url']}",
            f"Authentication: {status['connection']['chatgpt_authentication']}",
            "",
            "Next: run patchbay start --root <repo> --tool-mode worker --print-only for a ChatGPT setup guide.",
        ]
    )
    return "\n".join(lines)


def format_doctor_json(status: Mapping[str, Any]) -> str:
    return json.dumps(status, indent=2, sort_keys=True)


def _check(checks: list[dict[str, str]], name: str, status: str, detail: str) -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def _binary_check(
    checks: list[dict[str, str]],
    name: str,
    binary: str,
    *,
    path: str | None,
    missing_status: str = "warn",
) -> None:
    resolved = shutil.which(binary, path=path)
    _check(
        checks,
        name,
        "pass" if resolved else missing_status,
        f"available on PATH: {resolved}" if resolved else f"{binary} not found on PATH",
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
