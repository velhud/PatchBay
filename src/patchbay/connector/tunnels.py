"""Optional public tunnel process management for the launcher."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


PROCESS_TUNNEL_MODES = {"cloudflare", "cloudflare-named", "ngrok"}
CLOUDFLARE_QUICK_RE = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com")


class TunnelConfigurationError(ValueError):
    """Raised when tunnel mode is missing required runtime settings."""


class TunnelLaunchError(RuntimeError):
    """Raised when a tunnel process fails to become usable."""


@dataclass(frozen=True)
class TunnelSpec:
    mode: str
    command: str
    args: list[str]
    public_base_url: str | None = None
    discover_cloudflare_url: bool = False
    env_overrides: dict[str, str] = field(default_factory=dict)


class ProcessLogTail:
    """Bounded stdout/stderr collector for supervised child processes."""

    def __init__(self, label: str, max_lines: int = 120, verbose: bool = False) -> None:
        self.label = label
        self.max_lines = max(1, max_lines)
        self.verbose = verbose
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._buffer = ""

    def attach(self, process: subprocess.Popen[str]) -> None:
        for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                continue
            thread = threading.Thread(target=self._reader, args=(stream_name, stream), daemon=True)
            thread.start()

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def contains_cloudflare_url(self) -> str | None:
        with self._lock:
            match = CLOUDFLARE_QUICK_RE.search("\n".join(self._lines))
            return match.group(0) if match else None

    def _reader(self, stream_name: str, stream) -> None:
        while True:
            chunk = stream.readline()
            if chunk == "":
                return
            self.record(stream_name, chunk.rstrip("\n"))

    def record(self, stream_name: str, line: str) -> None:
        if not line:
            return
        entry = f"[{self.label}:{stream_name}] {line}"
        with self._lock:
            self._lines.append(entry)
            if len(self._lines) > self.max_lines:
                self._lines = self._lines[-self.max_lines :]
        if self.verbose:
            print(entry, flush=True)


def is_process_tunnel(mode: str | None) -> bool:
    return (mode or "none") in PROCESS_TUNNEL_MODES


def public_base_from_hostname(hostname: str) -> str:
    """Normalize a hostname or URL to an HTTPS origin/base path without /mcp."""
    raw = hostname.strip()
    if not raw:
        raise TunnelConfigurationError("hostname is required for this tunnel mode")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme != "https" or not parsed.netloc:
        raise TunnelConfigurationError("public tunnel hostname must resolve to an https URL")
    path = parsed.path.rstrip("/")
    if path == "/mcp":
        path = ""
    elif path.endswith("/mcp"):
        path = path[: -len("/mcp")].rstrip("/")
    return urlunparse(("https", parsed.netloc, path, "", "", "")).rstrip("/")


def mcp_url_from_public_base(public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}/mcp"


def url_with_query_token(url: str, token_name: str, token: str | None, *, redact: bool = False) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[token_name] = "<redacted>" if redact else token
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_tunnel_spec(
    *,
    mode: str,
    local_base_url: str,
    hostname: str | None = None,
    cloudflared: str = "cloudflared",
    ngrok: str = "ngrok",
    tunnel_name: str | None = None,
    cloudflare_config: str | None = None,
    cloudflare_token_file: str | None = None,
    cloudflare_token_env: str = "CLOUDFLARE_TUNNEL_TOKEN",
    ngrok_config: str | None = None,
) -> TunnelSpec | None:
    if mode in {"none", "local", "custom"}:
        return None
    if mode == "cloudflare":
        return TunnelSpec(
            mode=mode,
            command=cloudflared,
            args=["tunnel", "--url", local_base_url],
            discover_cloudflare_url=True,
        )
    if mode == "ngrok":
        public_base = public_base_from_hostname(hostname or "")
        args = ["http", local_base_url, "--url", public_base]
        if ngrok_config:
            args.extend(["--config", str(Path(ngrok_config).expanduser())])
        return TunnelSpec(mode=mode, command=ngrok, args=args, public_base_url=public_base)
    if mode == "cloudflare-named":
        public_base = public_base_from_hostname(hostname or "")
        env_overrides = {}
        args = ["tunnel"]
        if cloudflare_config:
            args.extend(["--config", str(Path(cloudflare_config).expanduser()), "run"])
            if tunnel_name:
                args.append(tunnel_name)
        else:
            args.extend(["run", "--url", local_base_url])
            if cloudflare_token_file:
                args.extend(["--token-file", str(Path(cloudflare_token_file).expanduser())])
            elif os.environ.get(cloudflare_token_env):
                env_overrides["TUNNEL_TOKEN"] = os.environ[cloudflare_token_env]
            else:
                if not tunnel_name:
                    raise TunnelConfigurationError(
                        "cloudflare-named requires tunnel_name, cloudflare_config, "
                        "cloudflare_token_file, or a Cloudflare tunnel token environment variable"
                    )
                args.append(tunnel_name)
        return TunnelSpec(mode=mode, command=cloudflared, args=args, public_base_url=public_base, env_overrides=env_overrides)
    raise TunnelConfigurationError(f"Unsupported tunnel mode: {mode}")


def spawn_logged(
    label: str,
    command: str,
    args: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str] | None = None,
    verbose: bool = False,
) -> tuple[subprocess.Popen[str], ProcessLogTail]:
    process = subprocess.Popen(
        [command, *args],
        cwd=str(cwd),
        env=dict(env or os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    tail = ProcessLogTail(label, verbose=verbose)
    tail.attach(process)
    return process, tail


def wait_for_cloudflare_url(process: subprocess.Popen[str], tail: ProcessLogTail, timeout_seconds: float = 45.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        found = tail.contains_cloudflare_url()
        if found:
            return found
        exit_code = process.poll()
        if exit_code is not None:
            raise TunnelLaunchError(f"cloudflared exited before a public URL was found, code={exit_code}")
        time.sleep(0.1)
    raise TunnelLaunchError("Timed out waiting for cloudflared public URL")


def wait_for_http_ready(url: str, *, token: str | None = None, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url)
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(request, timeout=1.0) as response:
                if 200 <= response.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
        time.sleep(0.15)
    detail = f": {last_error}" if last_error else ""
    raise TunnelLaunchError(f"Timed out waiting for {url}{detail}")


def terminate_process(process: subprocess.Popen[str] | None, timeout_seconds: float = 3.0) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)
