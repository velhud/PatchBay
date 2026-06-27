"""Optional public tunnel process management for the launcher."""
from __future__ import annotations

import os
import platform
import re
import signal
import shutil
import subprocess
import tarfile
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
CLOUDFLARED_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"


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


@dataclass(frozen=True)
class BinaryCheck:
    command: str
    available: bool
    version: str = ""
    error: str = ""


@dataclass(frozen=True)
class CloudflaredAsset:
    file_name: str
    archive: bool = False


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


def local_cloudflared_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    home = Path(env.get("PATCHBAY_HOME") or Path.home() / ".patchbay").expanduser()
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    return (home / "bin" / name).resolve(strict=False)


def command_exists(command: str) -> bool:
    return bool(_resolve_executable(command))


def verify_binary(command: str, args: Sequence[str] = ("--version",), *, timeout_seconds: float = 5.0) -> BinaryCheck:
    resolved = _resolve_executable(command)
    if not resolved:
        return BinaryCheck(command=command, available=False, error=f"{command} was not found")
    try:
        completed = subprocess.run(
            [resolved, *args],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return BinaryCheck(command=resolved, available=False, error=str(error))
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return BinaryCheck(command=resolved, available=False, version=output, error=f"exit code {completed.returncode}")
    return BinaryCheck(command=resolved, available=True, version=output)


def verify_cloudflared(command: str) -> BinaryCheck:
    return verify_binary(command, ("--version",))


def verify_ngrok(command: str) -> BinaryCheck:
    return verify_binary(command, ("version",))


def resolve_cloudflared(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    allow_local: bool = True,
) -> str:
    env = environ if environ is not None else os.environ
    requested = explicit or env.get("CLOUDFLARED_BIN") or ""
    if requested:
        check = verify_cloudflared(requested)
        if not check.available:
            raise TunnelConfigurationError(
                f"cloudflared was not usable at {requested}: {check.error or check.version or 'version check failed'}"
            )
        return check.command
    check = verify_cloudflared("cloudflared")
    if check.available:
        return check.command
    local = local_cloudflared_path(env)
    if allow_local and local.exists():
        check = verify_cloudflared(str(local))
        if check.available:
            return check.command
        raise TunnelConfigurationError(
            f"local cloudflared exists but failed --version at {local}: {check.error or check.version or 'version check failed'}"
        )
    raise TunnelConfigurationError(
        "cloudflared was not found. Run `patchbay install-cloudflared`, install Cloudflare Tunnel, "
        "or pass --cloudflared <path>."
    )


def resolve_ngrok(explicit: str | None = None, *, environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    requested = explicit or env.get("NGROK_BIN") or ""
    if requested:
        check = verify_ngrok(requested)
        if not check.available:
            raise TunnelConfigurationError(f"ngrok was not usable at {requested}: {check.error or check.version or 'version check failed'}")
        return check.command
    check = verify_ngrok("ngrok")
    if check.available:
        return check.command
    raise TunnelConfigurationError(
        "ngrok was not found on PATH. Install ngrok and run `ngrok config add-authtoken <token>`, "
        "or pass --ngrok <path>."
    )


def cloudflared_release_asset(system: str | None = None, machine: str | None = None) -> CloudflaredAsset:
    platform_name = (system or platform.system()).lower()
    arch = (machine or platform.machine()).lower()
    if arch in {"aarch64", "arm64"}:
        normalized_arch = "arm64"
    elif arch in {"x86_64", "amd64"}:
        normalized_arch = "amd64"
    elif arch in {"i386", "i686", "x86"}:
        normalized_arch = "386"
    elif arch.startswith("arm"):
        normalized_arch = "arm"
    else:
        normalized_arch = arch

    if platform_name == "darwin":
        if normalized_arch == "arm64":
            return CloudflaredAsset("cloudflared-darwin-arm64.tgz", archive=True)
        if normalized_arch == "amd64":
            return CloudflaredAsset("cloudflared-darwin-amd64.tgz", archive=True)
    if platform_name == "linux":
        if normalized_arch in {"amd64", "arm64", "arm", "386"}:
            return CloudflaredAsset(f"cloudflared-linux-{normalized_arch}")
    if platform_name == "windows":
        if normalized_arch in {"amd64", "386"}:
            return CloudflaredAsset(f"cloudflared-windows-{normalized_arch}.exe")
    raise TunnelConfigurationError(
        f"Automatic cloudflared install is not supported on {platform_name}/{arch}. "
        "Install cloudflared manually or pass --cloudflared <path>."
    )


def install_cloudflared_local(
    *,
    environ: Mapping[str, str] | None = None,
    download_base_url: str = CLOUDFLARED_RELEASE_BASE,
) -> str:
    """Install cloudflared into PATCHBAY_HOME/bin from Cloudflare's release asset."""
    env = environ if environ is not None else os.environ
    asset = cloudflared_release_asset()
    install_path = local_cloudflared_path(env)
    install_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_root = install_path.parent / f".cloudflared-download-{int(time.time() * 1000)}"
    temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        download_path = temp_root / asset.file_name
        urllib.request.urlretrieve(f"{download_base_url.rstrip('/')}/{asset.file_name}", download_path)
        if asset.archive:
            extract_dir = temp_root / "extract"
            extract_dir.mkdir(mode=0o700)
            with tarfile.open(download_path, "r:gz") as archive:
                _safe_extract_tar(archive, extract_dir)
            extracted = _find_file_by_name(extract_dir, "cloudflared")
            if extracted is None:
                raise TunnelLaunchError(f"Could not find cloudflared inside {asset.file_name}")
            shutil.copyfile(extracted, install_path)
        else:
            shutil.copyfile(download_path, install_path)
        if os.name != "nt":
            install_path.chmod(0o755)
        check = verify_cloudflared(str(install_path))
        if not check.available:
            raise TunnelLaunchError(f"Downloaded cloudflared, but --version failed: {check.error or check.version}")
        return check.command
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


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
    verify_binaries: bool = False,
) -> TunnelSpec | None:
    if mode in {"none", "local", "custom"}:
        return None
    if mode == "cloudflare":
        if verify_binaries:
            cloudflared = resolve_cloudflared(cloudflared)
        return TunnelSpec(
            mode=mode,
            command=cloudflared,
            args=["tunnel", "--url", local_base_url],
            discover_cloudflare_url=True,
        )
    if mode == "ngrok":
        if verify_binaries:
            ngrok = resolve_ngrok(ngrok)
        public_base = public_base_from_hostname(hostname or "")
        args = ["http", local_base_url, "--url", public_base]
        if ngrok_config:
            args.extend(["--config", str(Path(ngrok_config).expanduser())])
        return TunnelSpec(mode=mode, command=ngrok, args=args, public_base_url=public_base)
    if mode == "cloudflare-named":
        if verify_binaries:
            cloudflared = resolve_cloudflared(cloudflared)
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


def _resolve_executable(command: str) -> str | None:
    if not command:
        return None
    expanded = str(Path(command).expanduser()) if any(sep in command for sep in ("/", "\\")) else command
    if any(sep in command for sep in ("/", "\\")):
        path = Path(expanded)
        return str(path) if path.exists() and os.access(path, os.X_OK) else None
    return shutil.which(command)


def _safe_extract_tar(archive: tarfile.TarFile, target: Path) -> None:
    target_resolved = target.resolve()
    for member in archive.getmembers():
        destination = (target / member.name).resolve()
        if target_resolved not in destination.parents and destination != target_resolved:
            raise TunnelLaunchError("Refusing to extract cloudflared archive outside the install directory")
    archive.extractall(target)


def _find_file_by_name(root: Path, name: str) -> Path | None:
    for path in root.rglob(name):
        if path.is_file():
            return path
    return None


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
