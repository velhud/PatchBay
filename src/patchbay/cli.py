"""Public PatchBay command line interface."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from patchbay.auth import build_auth_policy
from patchbay.connector.launcher import (
    launcher_json_payload,
    load_config,
    prepare_start,
    prepared_with_revealed_token,
)
from patchbay.connector.profiles import (
    delete_workspace_profile,
    list_workspace_profiles,
    normalize_root,
    read_workspace_profile,
    save_workspace_profile,
    write_runtime_status,
)
from patchbay.connector.status import (
    connector_status,
    format_doctor_json,
    format_doctor_text,
    format_setup_guide_text,
)
from patchbay.connector.tunnels import (
    TunnelLaunchError,
    build_tunnel_spec,
    install_cloudflared_local,
    is_process_tunnel,
    mcp_url_from_public_base,
    resolve_cloudflared,
    resolve_ngrok,
    terminate_process,
    url_with_query_token,
    wait_for_cloudflare_url,
    wait_for_http_ready,
    spawn_logged,
)
from patchbay.pro_requests import ProRequestStore
from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.tools.handler import ToolHandler


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PACKAGE_ROOT / "src"


def default_config_path() -> str:
    configured = os.environ.get("PATCHBAY_CONFIG")
    if configured:
        return configured
    candidates = [Path.cwd() / "config.yaml", PACKAGE_ROOT / "config.yaml"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0])


def main(argv: Iterable[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_top_help())
        return 0
    command, rest = args[0], args[1:]
    handlers = {
        "start": start_main,
        "doctor": doctor_main,
        "setup": setup_main,
        "settings": settings_main,
        "ngrok": ngrok_main,
        "stable": stable_main,
        "install-cloudflared": install_cloudflared_main,
        "stdio": stdio_main,
        "pro-request": pro_request_main,
    }
    handler = handlers.get(command)
    if handler is None:
        print(f"Unknown command: {command}\n\n{_top_help()}", file=sys.stderr)
        return 2
    try:
        return handler(rest)
    except (ValueError, OSError, TunnelLaunchError) as error:
        print(f"patchbay {command}: {error}", file=sys.stderr)
        return 2


def start_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start PatchBay with a per-workspace runtime profile.")
    _add_start_args(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = load_config(args.config)
    prepared = _prepare_from_args(args, config)
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
        print()
        print(format_setup_guide_text(payload["setup_guide"]))
        print(f"Runtime config: {output_prepared['runtime_config_path']}")
        profile = output_prepared["profile"]
        if profile.get("saved"):
            print(f"Profile saved: {profile['profile_path']}")
        elif profile.get("used"):
            print(f"Profile used: {profile['profile_path']}")

    _post_connection_actions(
        payload["connection"].get("server_url") or payload["setup_guide"].get("server_url"),
        copy_url=args.copy_url,
        open_chatgpt=args.open_chatgpt,
    )

    ready = bool(prepared["status"].get("ready"))
    if args.print_only:
        return 0 if ready else 1
    if not ready and not args.force:
        print("Refusing to start because readiness checks failed. Use --force only for a deliberate local debug run.", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env["PATCHBAY_CONFIG"] = str(prepared["runtime_config_path"])
    _prepend_source_path(env)
    runtime_config = prepared["runtime_config"]
    auth_mode = runtime_config.get("auth", {}).get("tunnel_mode", "none")
    if is_process_tunnel(auth_mode):
        return _run_supervised_with_tunnel(
            prepared,
            env,
            reveal_token=args.reveal_token,
            verbose_logs=args.verbose_logs,
            copy_url=args.copy_url,
            open_chatgpt=args.open_chatgpt,
        )
    os.execvpe(sys.executable, [sys.executable, "-m", "patchbay.server"], env)
    return 1


def doctor_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check PatchBay connector readiness.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--config", default=default_config_path(), help="Path to config.yaml.")
    parser.add_argument("--public-base-url", help="Optional public tunnel base URL for Server URL preview.")
    parser.add_argument("--reveal-token", action="store_true", help="Include the configured token in the Server URL.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = load_config(args.config)
    status = connector_status(config, public_base_url=args.public_base_url, reveal_token=args.reveal_token)
    print(format_doctor_json(status) if args.json else format_doctor_text(status))
    return 0 if status["ready"] else 1


def setup_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an interactive PatchBay first-run setup.")
    parser.add_argument("--config", default=default_config_path(), help="Path to config.yaml.")
    parser.add_argument("--no-start", action="store_true", help="Save/print settings without starting the server.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable launch metadata.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not sys.stdin.isatty():
        print(
            "patchbay setup is interactive. Use `patchbay start --root <repo> --tool-mode worker --print-only --json` "
            "or run setup in a terminal.",
            file=sys.stderr,
        )
        return 2

    config = load_config(args.config)
    default_root = str(config.get("repositories", {}).get("default") or Path.cwd())
    root = _ask("Repository root", default_root)
    port = int(_ask("Local HTTP port", str(config.get("server", {}).get("port") or 8000)))
    tool_mode = _ask_choice("Tool mode", ["worker", "standard", "full", "minimal"], "worker")
    public_choice = _ask_choice("Public access", ["local", "quick", "stable", "ngrok"], "local")

    start_args = [
        "--config",
        args.config,
        "--root",
        root,
        "--port",
        str(port),
        "--tool-mode",
        tool_mode,
    ]
    if public_choice == "quick":
        start_args.extend(["--tunnel-mode", "cloudflare"])
    elif public_choice == "stable":
        start_args.extend(["--tunnel-mode", "cloudflare-named"])
        start_args.extend(["--hostname", _ask("Stable HTTPS hostname", "")])
        tunnel_name = _ask("Cloudflare tunnel name/token config label", "")
        if tunnel_name:
            start_args.extend(["--tunnel-name", tunnel_name])
    elif public_choice == "ngrok":
        start_args.extend(["--tunnel-mode", "ngrok"])
        start_args.extend(["--hostname", _ask("Ngrok reserved HTTPS hostname", "")])
    else:
        start_args.extend(["--tunnel-mode", "none"])

    if _ask_yes_no("Save this workspace profile", True):
        start_args.append("--save-profile")
    else:
        start_args.append("--no-profile")

    start_now = False if args.no_start else _ask_yes_no("Start PatchBay now", False)
    if not start_now:
        start_args.extend(["--print-only"])
    if args.json:
        start_args.append("--json")
    return start_main(start_args)


def settings_main(argv: Iterable[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print(_settings_help())
        return 0
    command, rest = args[0], args[1:]
    if command == "list":
        parser = argparse.ArgumentParser(description="List saved PatchBay workspace profiles.")
        parser.add_argument("--json", action="store_true")
        parsed = parser.parse_args(rest)
        profiles = list_workspace_profiles()
        if parsed.json:
            print(json.dumps({"profiles": profiles}, indent=2, sort_keys=True))
        else:
            if not profiles:
                print("No saved PatchBay profiles.")
            for profile in profiles:
                print(f"{profile.get('root', '<unknown>')}  {profile.get('auth', {}).get('tunnel_mode', 'none')}  {profile.get('profile_path')}")
        return 0
    if command == "show":
        parser = argparse.ArgumentParser(description="Show one saved PatchBay workspace profile.")
        parser.add_argument("--root", default=str(Path.cwd()))
        parser.add_argument("--json", action="store_true")
        parsed = parser.parse_args(rest)
        profile = read_workspace_profile(parsed.root)
        if not profile:
            print(f"No saved PatchBay profile for {normalize_root(parsed.root)}", file=sys.stderr)
            return 1
        print(json.dumps(profile, indent=2, sort_keys=True) if parsed.json else yaml.safe_dump(profile, sort_keys=False))
        return 0
    if command == "delete":
        parser = argparse.ArgumentParser(description="Delete one saved PatchBay workspace profile.")
        parser.add_argument("--root", default=str(Path.cwd()))
        parsed = parser.parse_args(rest)
        removed = delete_workspace_profile(parsed.root)
        print(f"Deleted profile for {normalize_root(parsed.root)}" if removed else f"No profile found for {normalize_root(parsed.root)}")
        return 0 if removed else 1
    if command in {"set", "use"}:
        parser = argparse.ArgumentParser(description=f"{command.capitalize()} PatchBay profile settings.")
        _add_start_args(parser, include_runtime_flags=False)
        parsed = parser.parse_args(rest)
        config = load_config(parsed.config)
        prepared = _prepare_from_args(parsed, config, save_profile=(command == "set"))
        payload = launcher_json_payload(prepared)
        print(json.dumps(payload, indent=2, sort_keys=True) if parsed.json else format_setup_guide_text(payload["setup_guide"]))
        if command == "set":
            print(f"Profile saved: {prepared['profile']['profile_path']}")
        return 0 if prepared["status"].get("ready") else 1
    print(f"Unknown settings command: {command}\n\n{_settings_help()}", file=sys.stderr)
    return 2


def ngrok_main(argv: Iterable[str] | None = None) -> int:
    return start_main(["--tunnel-mode", "ngrok", *(list(argv) if argv is not None else [])])


def stable_main(argv: Iterable[str] | None = None) -> int:
    return start_main(["--tunnel-mode", "cloudflare-named", *(list(argv) if argv is not None else [])])


def install_cloudflared_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install cloudflared into PATCHBAY_HOME/bin.")
    parser.add_argument("--download-base-url", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    installed = install_cloudflared_local(download_base_url=args.download_base_url) if args.download_base_url else install_cloudflared_local()
    print(f"cloudflared ready: {installed}")
    return 0


def stdio_main(argv: Iterable[str] | None = None) -> int:
    from patchbay.stdio import main as run_stdio_main

    return run_stdio_main(argv)


def pro_request_main(argv: Iterable[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print(_pro_request_help())
        return 0
    command, rest = args[0], args[1:]
    if command == "create":
        parser = argparse.ArgumentParser(description="Create a PatchBay Pro Escalation Request.")
        _add_pro_request_common(parser)
        parser.add_argument("--repo", required=True, help="Authorized repository path.")
        parser.add_argument("--title", required=True)
        parser.add_argument("--kind", default="debugging")
        parser.add_argument("--priority", default="normal")
        parser.add_argument("--origin-kind", default="human")
        parser.add_argument("--origin-worker", default="")
        parser.add_argument("--report", required=True, help="Markdown report path.")
        parser.add_argument("--attach", action="append", default=[], help="Bounded text/log/diff attachment. Repeat as needed.")
        parser.add_argument("--desired-output", default="")
        parsed = parser.parse_args(rest)
        store = ProRequestStore(load_config(parsed.config))
        result = store.create_request(
            repo_path=parsed.repo,
            title=parsed.title,
            kind=parsed.kind,
            priority=parsed.priority,
            origin_kind=parsed.origin_kind,
            origin_worker=parsed.origin_worker,
            report_path=parsed.report,
            attachments=parsed.attach,
            desired_output=parsed.desired_output,
        )
        _print_pro_request_result(result, json_output=parsed.json)
        return 0
    if command == "list":
        parser = argparse.ArgumentParser(description="List PatchBay Pro Escalation Requests.")
        _add_pro_request_common(parser)
        parser.add_argument("--repo", help="Authorized repository path filter.")
        parser.add_argument("--status", action="append", default=[], help="Status filter. Repeat as needed.")
        parser.add_argument("--include-closed", action="store_true")
        parser.add_argument("--limit", type=int, default=10)
        parsed = parser.parse_args(rest)
        store = ProRequestStore(load_config(parsed.config))
        result = store.list_requests(
            repo_path=parsed.repo,
            statuses=parsed.status,
            include_closed=parsed.include_closed,
            limit=parsed.limit,
        )
        _print_pro_request_result(result, json_output=parsed.json)
        return 0
    if command == "show":
        parser = argparse.ArgumentParser(description="Show one PatchBay Pro Escalation Request.")
        _add_pro_request_common(parser)
        parser.add_argument("request_id")
        parser.add_argument("--no-report", action="store_true")
        parser.add_argument("--include-events", action="store_true")
        parsed = parser.parse_args(rest)
        store = ProRequestStore(load_config(parsed.config))
        result = store.read_request(
            request_id=parsed.request_id,
            include_report=not parsed.no_report,
            include_events=parsed.include_events,
        )
        _print_pro_request_result(result, json_output=parsed.json)
        return 0
    if command == "response":
        parser = argparse.ArgumentParser(description="Read the stored response for one Pro Request.")
        _add_pro_request_common(parser)
        parser.add_argument("request_id")
        parsed = parser.parse_args(rest)
        store = ProRequestStore(load_config(parsed.config))
        result = store.response_text(parsed.request_id)
        _print_pro_request_result(result, json_output=parsed.json)
        return 0 if result.get("exists") else 1
    if command == "dispatch":
        parser = argparse.ArgumentParser(description="Dispatch a stored Pro response through the MCP server or ToolHandler path.")
        _add_pro_request_common(parser)
        parser.add_argument("request_id")
        parser.add_argument("--target", choices=["origin_worker", "new_worker"], default="origin_worker")
        parser.add_argument("--new-worker-name", default="Pro Solution Implementer")
        parser.add_argument("--message-source", choices=["worker_message_markdown", "response_markdown"], default="worker_message_markdown")
        parser.add_argument("--workspace-mode", choices=["isolated_write", "read_only"], default="isolated_write")
        parsed = parser.parse_args(rest)
        config = load_config(parsed.config)
        manager = JobManager(config)
        executor = JobExecutor(config, manager)
        handler = ToolHandler(config, manager, executor)
        result = asyncio.run(
            handler.handle_tool_call(
                "codex_pro_request_dispatch",
                {
                    "request_id": parsed.request_id,
                    "target": parsed.target,
                    "new_worker_name": parsed.new_worker_name,
                    "message_source": parsed.message_source,
                    "workspace_mode": parsed.workspace_mode,
                },
            )
        )
        _print_pro_request_result(result, json_output=parsed.json)
        return 0 if result.get("accepted") else 1
    if command == "close":
        parser = argparse.ArgumentParser(description="Close, cancel, or supersede a Pro Request.")
        _add_pro_request_common(parser)
        parser.add_argument("request_id")
        parser.add_argument("--reason", default="")
        parser.add_argument("--status", choices=["closed", "cancelled", "superseded"], default="closed")
        parsed = parser.parse_args(rest)
        store = ProRequestStore(load_config(parsed.config))
        result = store.close_request(request_id=parsed.request_id, reason=parsed.reason, status=parsed.status)
        _print_pro_request_result(result, json_output=parsed.json)
        return 0 if result.get("accepted") else 1
    print(f"Unknown pro-request command: {command}\n\n{_pro_request_help()}", file=sys.stderr)
    return 2


def _add_pro_request_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=default_config_path(), help="Path to config.yaml.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def _print_pro_request_result(result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if "requests" in result:
        for request in result["requests"]:
            print(f"{request['id']}  {request['status']}  {request.get('repo_name', '')}  {request.get('title', '')}")
        if not result["requests"]:
            print("No Pro Escalation Requests found.")
        return
    request = result.get("request") or result
    if request.get("id"):
        print(f"Pro Request: {request['id']}")
        print(f"Status: {request.get('status')}")
        print(f"Title: {request.get('title', '')}")
        print(f"Repo: {request.get('repo_name', '')}")
    if result.get("report_markdown"):
        print("\n--- report.md ---")
        print(result["report_markdown"])
    if result.get("response_markdown"):
        print("\n--- response.md ---")
        print(result["response_markdown"])
    if result.get("note"):
        print(result["note"])


def _add_start_args(parser: argparse.ArgumentParser, *, include_runtime_flags: bool = True) -> None:
    parser.add_argument("--config", default=default_config_path(), help="Base config.yaml path.")
    parser.add_argument(
        "--root",
        help="Default workspace root. When supplied, allowed roots are reset to this root plus any --allow-root values.",
    )
    parser.add_argument("--allow-root", action="append", default=[], help="Additional allowed repository root. Repeat as needed.")
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
    if include_runtime_flags:
        parser.add_argument("--print-only", action="store_true", help="Print launch metadata without starting the server.")
        parser.add_argument("--reveal-token", action="store_true", help="Print a tokenized Server URL. Keep this local and private.")
        parser.add_argument("--verbose-logs", action="store_true", help="Print supervised server/tunnel output.")
        parser.add_argument("--force", action="store_true", help="Start even when readiness checks fail.")
        parser.add_argument("--copy-url", dest="copy_url", action="store_true", default=None, help="Copy the ChatGPT Server URL to the clipboard.")
        parser.add_argument("--no-copy-url", dest="copy_url", action="store_false", help="Do not copy the ChatGPT Server URL.")
        parser.add_argument("--open-chatgpt", action="store_true", help="Open ChatGPT connector settings in the default browser.")
    else:
        parser.add_argument("--json", action="store_true", help="Print machine-readable launch metadata.")
        parser.set_defaults(print_only=True, reveal_token=False, verbose_logs=False, force=False, copy_url=None, open_chatgpt=False)
    if include_runtime_flags:
        parser.add_argument("--json", action="store_true", help="Print machine-readable launch metadata.")


def _prepare_from_args(args: argparse.Namespace, config: Mapping[str, Any], *, save_profile: bool | None = None) -> dict[str, Any]:
    return prepare_start(
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
        save_profile=args.save_profile if save_profile is None else save_profile,
        direct_write=args.direct_write,
        bash_mode=args.bash_mode,
        bash_session_id=args.bash_session_id,
        require_bash_session=args.require_bash_session,
        codex_session_read=args.codex_session_read,
        widget_domain=args.widget_domain,
        tool_mode=args.tool_mode,
    )


def _run_supervised_with_tunnel(
    prepared: dict[str, Any],
    env: dict[str, str],
    *,
    reveal_token: bool,
    verbose_logs: bool,
    copy_url: bool | None,
    open_chatgpt: bool,
) -> int:
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

    if mode in {"cloudflare", "cloudflare-named"}:
        resolve_cloudflared(tunnel_config.get("cloudflared") or "cloudflared")
    if mode == "ngrok":
        resolve_ngrok(tunnel_config.get("ngrok") or "ngrok")

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
    tunnel_tail = None
    try:
        server, _server_tail = spawn_logged(
            "server",
            sys.executable,
            ["-m", "patchbay.server"],
            cwd=root,
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
        _post_connection_actions(server_url, copy_url=copy_url, open_chatgpt=open_chatgpt)
        return _wait_supervised(server, tunnel, server_url=server_url)
    except TunnelLaunchError as error:
        detail = str(error)
        if tunnel_tail:
            tail_text = tunnel_tail.text()
            if tail_text:
                detail = f"{detail}\n\nRecent {mode} output:\n{tail_text}"
        raise TunnelLaunchError(f"{detail}{_tunnel_failure_hint(mode)}") from error
    finally:
        terminate_process(tunnel)
        terminate_process(server)


def _wait_supervised(server: subprocess.Popen[str], tunnel: subprocess.Popen[str], *, server_url: str) -> int:
    _print_controls()
    while True:
        server_code = server.poll()
        if server_code is not None:
            terminate_process(tunnel)
            return int(server_code)
        tunnel_code = tunnel.poll()
        if tunnel_code is not None:
            terminate_process(server)
            return int(tunnel_code or 1)
        control = _read_control_key()
        if control == "q":
            terminate_process(tunnel)
            terminate_process(server)
            return 0
        if control == "u":
            print(server_url, flush=True)
        elif control == "c":
            _copy_text_to_clipboard(server_url)
        elif control == "o":
            _open_chatgpt_settings()
        elif control == "h":
            _print_controls()
        try:
            time.sleep(0.25)
        except KeyboardInterrupt:
            terminate_process(tunnel)
            terminate_process(server)
            return 130


def _prepend_source_path(env: dict[str, str]) -> None:
    if not SOURCE_ROOT.exists():
        return
    entries = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry]
    source = str(SOURCE_ROOT)
    if source not in entries:
        entries.insert(0, source)
    env["PYTHONPATH"] = os.pathsep.join(entries)


def _post_connection_actions(server_url: str | None, *, copy_url: bool | None, open_chatgpt: bool) -> None:
    if not server_url:
        return
    if copy_url:
        _copy_text_to_clipboard(server_url)
    if open_chatgpt:
        _open_chatgpt_settings()


def _copy_text_to_clipboard(text: str) -> bool:
    commands: list[list[str]]
    if sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif os.name == "nt":
        commands = [["clip"]]
    else:
        commands = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for command in commands:
        if shutil.which(command[0]):
            subprocess.run(command, input=text, text=True, check=False)
            print("Copied ChatGPT Server URL to clipboard.", flush=True)
            return True
    print("Clipboard helper not found. Copy the ChatGPT Server URL from the output above.", file=sys.stderr)
    return False


def _open_chatgpt_settings() -> None:
    webbrowser.open("https://chatgpt.com/#settings/Connectors")


def _print_controls() -> None:
    if sys.stdin.isatty():
        print("Controls: u print URL | c copy URL | o open ChatGPT settings | h help | q quit", flush=True)


def _read_control_key() -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
        return sys.stdin.readline().strip().lower()[:1] or None
    except (OSError, ImportError):
        return None


def _tunnel_failure_hint(mode: str) -> str:
    if mode == "ngrok":
        return (
            "\n\nNgrok hints:\n"
            "- run `ngrok config add-authtoken <token>`\n"
            "- pass `--hostname <reserved-domain.ngrok-free.dev>`\n"
            "- stop any other process using the same reserved domain"
        )
    if mode == "cloudflare-named":
        return (
            "\n\nCloudflare named tunnel hints:\n"
            "- run `patchbay install-cloudflared` if cloudflared is missing\n"
            "- run `cloudflared tunnel login`, create a tunnel, and route DNS\n"
            "- pass `--hostname <host>` plus `--tunnel-name`, `--cloudflare-config`, or a tunnel token"
        )
    if mode == "cloudflare":
        return "\n\nCloudflare quick tunnel hints:\n- run `patchbay install-cloudflared` if cloudflared is missing\n- quick tunnel URLs change on restart"
    return ""


def _ask(prompt: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        value = _ask(f"{prompt} ({'/'.join(choices)})", default).lower()
        if value in choices:
            return value
        print(f"Choose one of: {', '.join(choices)}")


def _ask_yes_no(prompt: str, default: bool) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{marker}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Answer yes or no.")


def _top_help() -> str:
    return """PatchBay command line

Usage:
  patchbay setup
  patchbay start --root <repo> --tool-mode worker
  patchbay doctor --json
  patchbay settings list
  patchbay pro-request list
  patchbay stdio --config config.yaml
  patchbay ngrok --root <repo> --hostname <reserved-domain>
  patchbay stable --root <repo> --hostname <host> --tunnel-name <name>
  patchbay install-cloudflared

Run `patchbay <command> --help` for command-specific options."""


def _pro_request_help() -> str:
    return """PatchBay Pro Escalation Requests

Usage:
  patchbay pro-request create --repo <repo> --title <title> --report <report.md>
  patchbay pro-request list [--repo <repo>] [--status open] [--json]
  patchbay pro-request show <proreq_id> [--json]
  patchbay pro-request response <proreq_id> [--json]
  patchbay pro-request dispatch <proreq_id> [--target origin_worker]
  patchbay pro-request close <proreq_id> [--reason <text>]

Worker dispatch uses the same ToolHandler/WorkerRuntime path as codex_pro_request_dispatch."""


def _settings_help() -> str:
    return """PatchBay settings

Usage:
  patchbay settings list [--json]
  patchbay settings show --root <repo> [--json]
  patchbay settings set --root <repo> [start options]
  patchbay settings use --root <repo> [--json]
  patchbay settings delete --root <repo>"""


if __name__ == "__main__":
    raise SystemExit(main())
