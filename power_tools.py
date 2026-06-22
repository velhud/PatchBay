"""Optional direct workspace power tools."""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from security import redact_text
from workspace_context import WorkspaceContext


SAFE_ALLOWED_PREFIXES = [
    "pwd",
    "ls",
    "find",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git branch",
    "git rev-parse",
    "git ls-files",
    "npm test",
    "npm run test",
    "npm run typecheck",
    "npm run lint",
    "npm run build",
    "npm run check",
    "pnpm test",
    "pnpm run test",
    "pnpm run typecheck",
    "pnpm run lint",
    "pnpm run build",
    "pnpm run check",
    "yarn test",
    "yarn run test",
    "yarn run typecheck",
    "yarn run lint",
    "yarn run build",
    "yarn run check",
    "bun test",
    "bun run test",
    "bun run typecheck",
    "bun run lint",
    "bun run build",
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "uv run pytest",
    "go test",
    "cargo test",
    "cargo check",
    "cargo clippy",
    "tsc",
    "npx tsc",
    "eslint",
    "npx eslint",
    "biome check",
    "npx biome check",
]


SAFE_BLOCKED_PATTERNS = [
    re.compile(r"(^|\s)rm\s+"),
    re.compile(r"(^|\s)mv\s+"),
    re.compile(r"(^|\s)cp\s+"),
    re.compile(r"(^|\s)dd\s+"),
    re.compile(r"(^|\s)sudo\s+"),
    re.compile(r"(^|\s)chmod\s+"),
    re.compile(r"(^|\s)chown\s+"),
    re.compile(r"(^|\s)kill\s+"),
    re.compile(r"(^|\s)pkill\s+"),
    re.compile(r"(^|\s)curl\s+"),
    re.compile(r"(^|\s)wget\s+"),
    re.compile(r"(^|\s)ssh\s+"),
    re.compile(r"(^|\s)scp\s+"),
    re.compile(r"(^|\s)rsync\s+"),
    re.compile(r"(^|\s)docker\s+"),
    re.compile(r"(^|\s)podman\s+"),
    re.compile(r"(^|\s)git\s+push\b"),
    re.compile(r"(^|\s)git\s+reset\b"),
    re.compile(r"(^|\s)git\s+clean\b"),
    re.compile(r"(^|\s)git\s+checkout\b"),
    re.compile(r"(^|\s)git\s+switch\b"),
    re.compile(r"(^|\s)git\s+restore\b"),
    re.compile(r"(^|\s)(npm|pnpm|yarn)\s+publish\b"),
    re.compile(r"(^|\s)--no-index\b"),
    re.compile(r"(^|\s)--fix\b"),
    re.compile(r"(^|\s)(/|~(?:/|\s|$))"),
    re.compile(r"(^|\s)\.\.(?:/|\s|$)"),
    re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{|\[)"),
    re.compile(r"(^|[\s:])(?:\.env(?:[./\s:]|$)|\.git(?:[/\s:]|$)|node_modules(?:[/\s:]|$)|\.ssh(?:[/\s:]|$)|id_rsa(?:[.\s:]|$)|id_ed25519(?:[.\s:]|$)|[^\s:]*\.(?:pem|key)(?:[\s:]|$))"),
    re.compile(r"(^|\s)-exec\b"),
    re.compile(r"(^|\s)-execdir\b"),
    re.compile(r"(^|\s)-delete\b"),
    re.compile(r"(^|\s)-ok\b"),
    re.compile(r"(^|\s)-okdir\b"),
    re.compile(r"(^|\s)-fprint\b"),
    re.compile(r"(^|\s)-fprintf\b"),
    re.compile(r"(^|\s)-fls\b"),
    re.compile(r"(^|\s)(sed|perl)\s+.*(^|\s)-i(\s|$)"),
    re.compile(r"(^|\s)(cat|grep|rg|head|tail|wc)\s+"),
    re.compile(r"[;&|<>`]"),
    re.compile(r"\$\("),
    re.compile(r"\n"),
]


def compact_command(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


def is_allowed_package_script(command: str) -> bool:
    return bool(
        re.match(
            r"^(?:npm|pnpm|yarn|bun)\s+run\s+(?:test|typecheck|lint|build|check)(?::[A-Za-z0-9._-]+)*(?:\s+--\s+[A-Za-z0-9._:= -]+)?$",
            command,
        )
    )


def starts_with_allowed_prefix(command: str) -> bool:
    normalized = compact_command(command)
    return is_allowed_package_script(normalized) or any(
        normalized == prefix or normalized.startswith(f"{prefix} ") for prefix in SAFE_ALLOWED_PREFIXES
    )


class PowerToolRunner:
    """Runs optional direct commands under configured power controls."""

    def __init__(self, config: Dict[str, Any], workspace_context: WorkspaceContext):
        self.config = config
        self.workspace_context = workspace_context

    def power_config(self) -> Dict[str, Any]:
        return self.config.get("power_tools", {})

    def write_enabled(self) -> bool:
        return bool(self.power_config().get("direct_write", False))

    def bash_mode(self) -> str:
        mode = str(self.power_config().get("bash_mode", "off")).strip().lower()
        return mode if mode in {"off", "safe", "full"} else "off"

    async def run_command(self, args: Dict[str, Any]) -> Dict[str, Any]:
        command = str(args.get("command") or "")
        if not command.strip():
            raise ValueError("command is required")
        self._assert_bash_allowed(command, args.get("session_id"))

        workspace = self.workspace_context.open_workspace(args.get("repo"))
        cwd_path, cwd_rel = self.workspace_context.resolve_path(workspace, args.get("cwd") or ".")
        if not cwd_path.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd_rel}")

        configured_timeout = int(self.power_config().get("bash_timeout_ms", 30_000))
        timeout_ms = max(1_000, min(int(args.get("timeout_ms") or configured_timeout), 180_000))
        max_output_bytes = max(1_000, min(int(self.power_config().get("bash_max_output_bytes", 60_000)), 500_000))

        started = time.time()
        process = await asyncio.create_subprocess_exec(
            self._bash_executable(),
            "-lc",
            command,
            cwd=str(cwd_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(),
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            timed_out = True
            process.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()

        stdout_text, stdout_truncated = self._trim_output(stdout.decode("utf-8", errors="replace"), max_output_bytes)
        stderr_text, stderr_truncated = self._trim_output(stderr.decode("utf-8", errors="replace"), max_output_bytes)
        if timed_out:
            stderr_text = (stderr_text + f"\n[codex-mcp-wrapper] Command timed out after {timeout_ms} ms.").strip()

        return {
            "command": command,
            "cwd": cwd_rel,
            "exit_code": process.returncode,
            "duration_ms": int((time.time() - started) * 1000),
            "stdout": redact_text(stdout_text),
            "stderr": redact_text(stderr_text),
            "truncated": stdout_truncated or stderr_truncated,
            "timed_out": timed_out,
            "bash_mode": self.bash_mode(),
            "bash_session_id": self.power_config().get("bash_session_id") or None,
        }

    def _assert_bash_allowed(self, command: str, session_id: Optional[str]) -> None:
        mode = self.bash_mode()
        if mode == "off":
            raise ValueError("codex_run_command is disabled. Set power_tools.bash_mode to safe or full.")
        self._assert_session(session_id)
        if mode == "full":
            return

        normalized = compact_command(command)
        for pattern in SAFE_BLOCKED_PATTERNS:
            if pattern.search(normalized):
                raise ValueError(
                    f"Command is blocked in safe bash mode: {normalized}. "
                    "Use read/search/git tools for inspection or enable full bash only for trusted repos."
                )
        if not starts_with_allowed_prefix(normalized):
            raise ValueError(
                f"Command is not in the safe bash allowlist: {normalized}. "
                "Allowed examples include pwd, ls, find, git status, git diff, npm test, pytest, go test, cargo test."
            )

    def _assert_session(self, session_id: Optional[str]) -> None:
        expected = str(self.power_config().get("bash_session_id") or "").strip()
        require = bool(self.power_config().get("require_bash_session", False))
        requested = str(session_id or "").strip()
        if not expected:
            if require:
                raise ValueError("bash session guard is enabled but no bash_session_id is configured")
            return
        if not requested:
            if require:
                raise ValueError(f"bash session id is required. Retry with session_id={expected!r}.")
            return
        if requested != expected:
            raise ValueError(f"bash session id mismatch. This server accepts session_id={expected!r}.")

    def _build_env(self) -> Dict[str, str]:
        allowed = set(
            self.config.get("security", {}).get("allowed_env_keys")
            or ["PATH", "HOME", "USER", "SHELL", "TMPDIR"]
        )
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        env["CI"] = os.environ.get("CI", "1")
        return env

    def _trim_output(self, text: str, max_bytes: int) -> tuple[str, bool]:
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text, False
        return encoded[:max_bytes].decode("utf-8", errors="replace") + f"\n...[output truncated to {max_bytes} bytes]", True

    def _bash_executable(self) -> str:
        return "/bin/bash" if Path("/bin/bash").exists() else "bash"
