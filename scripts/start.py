#!/usr/bin/env python3
"""CodexPro-style launcher for codex-mcp-wrapper."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth import build_auth_policy  # noqa: E402
from connector import format_doctor_text  # noqa: E402
from launcher import launcher_json_payload, load_config, prepare_start, prepared_with_revealed_token  # noqa: E402
from profile_store import write_runtime_status  # noqa: E402
from tunnel_manager import (  # noqa: E402
    TunnelLaunchError,
    build_tunnel_spec,
    is_process_tunnel,
    mcp_url_from_public_base,
    spawn_logged,
    terminate_process,
    url_with_query_token,
    wait_for_cloudflare_url,
    wait_for_http_ready,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start codex-mcp-wrapper with a per-workspace runtime profile.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Base config.yaml path.")
    parser.add_argument("--root", help="Workspace root to expose.")
    parser.add_argument("--allow-root", action="append", default=[], help="Additional allowed repository root.")
    parser.add_argument("--host", help="HTTP bind host.")
    parser.add_argument("--port", type=int, help="HTTP bind port.")
    parser.add_argument("--public-base-url", help="Public tunnel base URL used for ChatGPT Server URL output.")
    parser.add_argument("--tunnel-mode", choices=["none", "local", "custom", "cloudflare", "cloudflare-named", "ngrok"])
    parser.add_argument("--hostname", help="Stable public hostname for ngrok or Cloudflare named tunnel.")
    parser.add_argument("--tunnel-name", help="Cloudflare named tunnel name.")
    parser.add_argument("--cloudflared", default="cloudflared", help="cloudflared executable path.")
    parser.add_argument("--ngrok", default="ngrok", help="ngrok executable path.")
    parser.add_argument("--cloudflare-config", help="cloudflared YAML config path.")
    parser.add_argument("--cloudflare-token-file", help="File containing a Cloudflare tunnel token.")
    parser.add_argument("--cloudflare-token-env", default="CLOUDFLARE_TUNNEL_TOKEN", help="Environment variable containing a Cloudflare tunnel token.")
    parser.add_argument("--ngrok-config", help="ngrok config file path.")
    parser.add_argument("--tunnel-timeout-seconds", type=int, default=45, help="Timeout while waiting for local server and tunnel readiness.")
    parser.add_argument("--save-profile", action="store_true", help="Save this workspace launch profile.")
    parser.add_argument("--no-profile", action="store_true", help="Ignore any saved profile for this workspace.")
    parser.add_argument("--direct-write", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--bash-mode", choices=["off", "safe", "full"])
    parser.add_argument("--bash-session-id", help="Require or label a bash session id.")
    parser.add_argument("--require-bash-session", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--codex-session-read", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--widget-domain", help="HTTPS origin for ChatGPT widget metadata.")
    parser.add_argument("--tool-mode", choices=["minimal", "standard", "full", "worker"], help="MCP tool surface exposed to ChatGPT.")
    parser.add_argument("--print-only", action="store_true", help="Print launch metadata without starting the server.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable launch metadata.")
    parser.add_argument("--reveal-token", action="store_true", help="Print a tokenized Server URL. Keep this local and private.")
    parser.add_argument("--verbose-logs", action="store_true", help="Print supervised server/tunnel output.")
    parser.add_argument("--force", action="store_true", help="Start even when readiness checks fail.")
    args = parser.parse_args()

    config = load_config(args.config)
    prepared = prepare_start(
        config,
        root=args.root,
        allow_roots=args.allow_root,
        host=args.host,
        port=args.port,
        public_base_url=args.public_base_url,
        tunnel_mode=args.tunnel_mode,
        hostname=args.hostname,
        tunnel_name=args.tunnel_name,
        cloudflared=args.cloudflared,
        ngrok=args.ngrok,
        cloudflare_config=args.cloudflare_config,
        cloudflare_token_file=args.cloudflare_token_file,
        cloudflare_token_env=args.cloudflare_token_env,
        ngrok_config=args.ngrok_config,
        tunnel_timeout_seconds=args.tunnel_timeout_seconds,
        use_profile=not args.no_profile,
        save_profile=args.save_profile,
        direct_write=args.direct_write,
        bash_mode=args.bash_mode,
        bash_session_id=args.bash_session_id,
        require_bash_session=args.require_bash_session,
        codex_session_read=args.codex_session_read,
        widget_domain=args.widget_domain,
        tool_mode=args.tool_mode,
    )

    output_prepared = (
        prepared_with_revealed_token(prepared, os.environ)
        if args.print_only and args.reveal_token
        else prepared
    )
    if args.print_only and output_prepared["status"].get("auth", {}).get("token_returned"):
        print("WARNING: printing a private tokenized ChatGPT Server URL. Do not paste it into logs or commits.", file=sys.stderr)

    payload = launcher_json_payload(output_prepared)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_doctor_text(output_prepared["status"]))
        print(f"Runtime config: {output_prepared['runtime_config_path']}")
        profile = output_prepared["profile"]
        if profile.get("saved"):
            print(f"Profile saved: {profile['profile_path']}")
        elif profile.get("used"):
            print(f"Profile used: {profile['profile_path']}")

    ready = bool(prepared["status"].get("ready"))
    if args.print_only:
        return 0 if ready else 1
    if not ready and not args.force:
        print("Refusing to start because readiness checks failed. Use --force only for a deliberate local debug run.", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["CODEX_MCP_CONFIG"] = str(prepared["runtime_config_path"])
    runtime_config = prepared["runtime_config"]
    auth_mode = runtime_config.get("auth", {}).get("tunnel_mode", "none")
    if is_process_tunnel(auth_mode):
        return _run_supervised_with_tunnel(prepared, env, reveal_token=args.reveal_token, verbose_logs=args.verbose_logs)
    os.execvpe(sys.executable, [sys.executable, str(ROOT / "server.py")], env)
    return 1


def _run_supervised_with_tunnel(prepared: dict, env: dict[str, str], *, reveal_token: bool, verbose_logs: bool) -> int:
    config = prepared["runtime_config"]
    root = str(Path(config["repositories"]["default"]).resolve())
    server_config = config.get("server", {})
    auth_config = config.get("auth", {})
    tunnel_config = config.get("tunnel", {})
    host = str(server_config.get("host") or "127.0.0.1")
    port = int(server_config.get("port") or 8000)
    local_base = f"http://{host}:{port}"
    mode = str(auth_config.get("tunnel_mode") or "none")
    timeout = float(tunnel_config.get("timeout_seconds") or 45)
    auth_policy = build_auth_policy(config, environ=env)
    token_name = auth_policy.query_token_names[0]

    spec = build_tunnel_spec(
        mode=mode,
        local_base_url=local_base,
        hostname=tunnel_config.get("hostname"),
        cloudflared=tunnel_config.get("cloudflared") or "cloudflared",
        ngrok=tunnel_config.get("ngrok") or "ngrok",
        tunnel_name=tunnel_config.get("tunnel_name"),
        cloudflare_config=tunnel_config.get("cloudflare_config"),
        cloudflare_token_file=tunnel_config.get("cloudflare_token_file"),
        cloudflare_token_env=tunnel_config.get("cloudflare_token_env") or "CLOUDFLARE_TUNNEL_TOKEN",
        ngrok_config=tunnel_config.get("ngrok_config"),
    )
    if spec is None:
        raise TunnelLaunchError(f"Tunnel mode {mode} does not require process supervision")

    server = None
    tunnel = None
    try:
        server, _server_tail = spawn_logged(
            "server",
            sys.executable,
            [str(ROOT / "server.py")],
            cwd=ROOT,
            env=env,
            verbose=verbose_logs,
        )
        wait_for_http_ready(f"{local_base}/", token=auth_policy.token, timeout_seconds=timeout)

        tunnel_env = dict(os.environ)
        tunnel_env.update(spec.env_overrides)
        tunnel, tunnel_tail = spawn_logged(
            spec.mode,
            spec.command,
            spec.args,
            cwd=root,
            env=tunnel_env,
            verbose=verbose_logs,
        )
        public_base = (
            wait_for_cloudflare_url(tunnel, tunnel_tail, timeout_seconds=timeout)
            if spec.discover_cloudflare_url
            else spec.public_base_url
        )
        if not public_base:
            raise TunnelLaunchError("Tunnel did not provide a public base URL")

        server_url = url_with_query_token(
            mcp_url_from_public_base(public_base),
            token_name,
            auth_policy.token,
            redact=not reveal_token,
        )
        status_path = write_runtime_status(
            root,
            {
                "mode": mode,
                "local_mcp_url": f"{local_base}/mcp",
                "public_base_url": public_base,
                "server_url": server_url,
                "token_returned": bool(reveal_token and auth_policy.token),
            },
        )
        if reveal_token and auth_policy.token:
            print("WARNING: printing a private tokenized ChatGPT Server URL. Do not paste it into logs or commits.", file=sys.stderr)
        print(
            json.dumps({"event": "tunnel_ready", "mode": mode, "server_url": server_url, "runtime_status_path": status_path}, indent=2),
            flush=True,
        )
        return _wait_supervised(server, tunnel)
    finally:
        terminate_process(tunnel)
        terminate_process(server)


def _wait_supervised(server: subprocess.Popen[str], tunnel: subprocess.Popen[str]) -> int:
    while True:
        server_code = server.poll()
        if server_code is not None:
            terminate_process(tunnel)
            return int(server_code)
        tunnel_code = tunnel.poll()
        if tunnel_code is not None:
            terminate_process(server)
            return int(tunnel_code or 1)
        try:
            time.sleep(0.25)
        except KeyboardInterrupt:
            terminate_process(tunnel)
            terminate_process(server)
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
