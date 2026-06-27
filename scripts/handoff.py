#!/usr/bin/env python3
"""Execute or watch .ai-bridge handoff plans with a local implementation agent."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from patchbay.workspace.context import WorkspaceContext  # noqa: E402


DEFAULT_TIMEOUT_MS = 600_000
DEFAULT_MAX_OUTPUT_BYTES = 120_000


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute or watch patchbay .ai-bridge handoffs.")
    parser.add_argument("command", choices=["execute", "watch"], help="Run one plan or watch for new plans.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Base config.yaml path.")
    parser.add_argument("--root", default=".", help="Workspace root. Default: current directory.")
    parser.add_argument("--agent", default="custom", help="Agent adapter name: custom, codex, opencode, pi.")
    parser.add_argument("--model", default="", help="Optional model string passed to command templates.")
    parser.add_argument("--command-template", help="Custom command template. Supports {{model}}, {{plan_file}}, {{plan_text}}, {{root}}.")
    parser.add_argument("--dry-run", action="store_true", help="Print the command without executing it.")
    parser.add_argument("--yes", action="store_true", help="Run without interactive confirmation.")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument("--once", action="store_true", help="For watch mode, check once and exit.")
    parser.add_argument("--poll-interval-ms", type=int, default=2000)
    parser.add_argument("--debounce-ms", type=int, default=500)
    parser.add_argument("--state-file", help="Watch state file. Default: .ai-bridge/watch-handoff-state.json.")
    args = parser.parse_args()

    config = load_config(args.config, args.root)
    workspace_context = WorkspaceContext(config)
    workspace = workspace_context.open_workspace(args.root)
    workspace_context.ensure_ai_bridge(workspace)

    if args.command == "execute":
        result = execute_handoff(workspace_context, workspace, args, event_name="execute_handoff")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["exit_code"] == 0 or args.dry_run else 1

    return watch_handoff(workspace_context, workspace, args)


def load_config(config_path: str, root: str) -> dict[str, Any]:
    with open(config_path, encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    resolved = str(Path(root).expanduser().resolve())
    config.setdefault("repositories", {})["default"] = resolved
    allowed = list(config["repositories"].get("allowed") or [])
    if resolved not in allowed:
        allowed.insert(0, resolved)
    config["repositories"]["allowed"] = allowed
    return config


def watch_handoff(workspace_context: WorkspaceContext, workspace, args: argparse.Namespace) -> int:
    state_path = Path(args.state_file) if args.state_file else workspace.root / workspace_context.context_dir / "watch-handoff-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        time.sleep(max(args.debounce_ms, 0) / 1000)
        plan_path = workspace.root / workspace_context.context_dir / "current-plan.md"
        plan_text = read_plan(plan_path)
        plan_hash = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
        state = read_json(state_path)
        if not is_real_plan(plan_text):
            print(json.dumps({"event": "watch_handoff_skip", "reason": "no real plan"}, sort_keys=True))
        elif state.get("lastPlanHash") == plan_hash:
            print(json.dumps({"event": "watch_handoff_skip", "reason": "duplicate plan"}, sort_keys=True))
        else:
            append_event(workspace_context, workspace, "watch_handoff_started", {"plan_hash": plan_hash})
            result = execute_handoff(workspace_context, workspace, args, event_name="watch_handoff")
            write_json(state_path, {"lastPlanHash": plan_hash, "updatedAt": now_iso(), "lastResult": result})
            append_event(workspace_context, workspace, "watch_handoff_finished", {"plan_hash": plan_hash, "exit_code": result["exit_code"]})
            print(json.dumps(result, indent=2, sort_keys=True))
            if result["exit_code"] != 0 and not args.dry_run:
                return 1
        if args.once:
            return 0
        time.sleep(max(args.poll_interval_ms, 250) / 1000)


def execute_handoff(workspace_context: WorkspaceContext, workspace, args: argparse.Namespace, *, event_name: str) -> dict[str, Any]:
    plan_path = workspace.root / workspace_context.context_dir / "current-plan.md"
    plan_text = read_plan(plan_path)
    if not is_real_plan(plan_text):
        raise SystemExit("No real handoff plan found in .ai-bridge/current-plan.md")

    command = build_command(args, workspace.root, plan_path, plan_text)
    if args.dry_run:
        append_event(workspace_context, workspace, f"{event_name}_dry_run", {"agent": args.agent, "command": command})
        return {"event": f"{event_name}_dry_run", "agent": args.agent, "command": command, "exit_code": 0}

    if not args.yes and sys.stdin.isatty():
        answer = input(f"Run local handoff command in {workspace.root}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise SystemExit("Cancelled.")

    started = time.time()
    completed = subprocess.run(
        command,
        cwd=workspace.root,
        shell=True,
        text=True,
        capture_output=True,
        timeout=max(args.timeout_ms, 1000) / 1000,
        env=build_env(),
    )
    stdout = trim(completed.stdout, args.max_output_bytes)
    stderr = trim(completed.stderr, args.max_output_bytes)
    duration_ms = int((time.time() - started) * 1000)

    diff_text = git_diff(workspace.root, args.max_output_bytes)
    write_ai_bridge_text(workspace_context, workspace, "implementation-diff.patch", diff_text)
    status = "\n".join(
        [
            "# Agent Execution Status",
            "",
            f"Updated: {now_iso()}",
            f"Agent: {args.agent}",
            f"Model: {args.model or 'unspecified'}",
            f"Exit code: {completed.returncode}",
            f"Duration ms: {duration_ms}",
            "",
            "## stdout",
            "",
            "```text",
            stdout,
            "```",
            "",
            "## stderr",
            "",
            "```text",
            stderr,
            "```",
        ]
    )
    write_ai_bridge_text(workspace_context, workspace, "agent-status.md", status)
    event = {
        "agent": args.agent,
        "model": args.model or "",
        "exit_code": completed.returncode,
        "duration_ms": duration_ms,
    }
    append_event(workspace_context, workspace, event_name, event)
    return {
        "event": event_name,
        "agent": args.agent,
        "exit_code": completed.returncode,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "status_path": f"{workspace_context.context_dir}/agent-status.md",
        "diff_path": f"{workspace_context.context_dir}/implementation-diff.patch",
    }


def build_command(args: argparse.Namespace, root: Path, plan_path: Path, plan_text: str) -> str:
    template = args.command_template or default_command_template(args.agent)
    if "{{plan_file}}" not in template and "{{plan_text}}" not in template:
        raise ValueError("command template must include {{plan_file}} or {{plan_text}}")
    replacements = {
        "model": shlex.quote(args.model),
        "plan_file": shlex.quote(str(plan_path)),
        "plan_text": shlex.quote(plan_text),
        "root": shlex.quote(str(root)),
    }
    command = template
    for key, value in replacements.items():
        command = command.replace("{{" + key + "}}", value)
    return command


def default_command_template(agent: str) -> str:
    if agent == "codex":
        return "codex exec --cd {{root}} - < {{plan_file}}"
    if agent == "opencode":
        return "opencode run --model {{model}} $(cat {{plan_file}})"
    if agent == "pi":
        return "pi run --model {{model}} $(cat {{plan_file}})"
    raise ValueError("--command-template is required for custom agents")


def read_plan(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def is_real_plan(plan_text: str) -> bool:
    stripped = plan_text.strip()
    return bool(stripped) and "No plan written yet" not in stripped


def write_ai_bridge_text(workspace_context: WorkspaceContext, workspace, name: str, text: str) -> None:
    workspace_context._write_ai_bridge_file(workspace, name, text, append=False)


def append_event(workspace_context: WorkspaceContext, workspace, event: str, data: dict[str, Any]) -> None:
    line = json.dumps({"ts": now_iso(), "event": event, **data}, sort_keys=True) + "\n"
    path, _rel = workspace_context._ai_bridge_path(workspace, "execution-log.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


def git_diff(root: Path, max_bytes: int) -> str:
    completed = subprocess.run(["git", "diff", "--", "."], cwd=root, text=True, capture_output=True, timeout=20)
    output = completed.stdout if completed.returncode == 0 else completed.stderr
    return trim(output, max_bytes)


def build_env() -> dict[str, str]:
    allowed = {"PATH", "HOME", "USER", "SHELL", "TMPDIR", "OPENAI_API_KEY"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
    env["NO_COLOR"] = "1"
    return env


def trim(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + f"\n...[truncated to {max_bytes} bytes]"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
