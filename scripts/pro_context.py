#!/usr/bin/env python3
"""Bundle or apply planning-model context through .ai-bridge."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from patchbay.workspace.context import WorkspaceContext  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="CodexPro-style Pro context bundle/apply for patchbay.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bundle = subparsers.add_parser("bundle", help="Write .ai-bridge/pro-context.md.")
    add_common(bundle)
    bundle.add_argument("--path", action="append", default=[], help="Workspace-relative file to include.")
    bundle.add_argument("--title", default="PatchBay Pro Context", help="Context title.")
    bundle.add_argument("--include-diff", action=argparse.BooleanOptionalAction, default=True)
    bundle.add_argument("--include-ai-bridge", action=argparse.BooleanOptionalAction, default=True)
    bundle.add_argument("--copy", action="store_true", help="Copy generated markdown to macOS clipboard when pbcopy exists.")

    apply = subparsers.add_parser("apply", help="Write a planning-model response to .ai-bridge/current-plan.md.")
    add_common(apply)
    source = apply.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Markdown plan file to read.")
    source.add_argument("--stdin", action="store_true", help="Read plan from stdin.")
    apply.add_argument("--title", default="Planning Model Handoff", help="Plan heading when input has none.")
    apply.add_argument("--append", action="store_true", help="Append to current-plan.md instead of overwriting.")
    apply.add_argument("--agent", default="codex", help="Target local agent name. Default: codex.")
    apply.add_argument("--model", default="", help="Optional model hint.")

    args = parser.parse_args()
    config = load_config(args.config, args.root)
    workspace_context = WorkspaceContext(config)
    workspace = workspace_context.open_workspace(args.root)
    workspace_context.ensure_ai_bridge(workspace)

    if args.command == "bundle":
        return run_bundle(workspace_context, workspace, args)
    return run_apply(workspace_context, workspace, args)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Base config.yaml path.")
    parser.add_argument("--root", default=".", help="Workspace root. Default: current directory.")


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


def run_bundle(workspace_context: WorkspaceContext, workspace, args: argparse.Namespace) -> int:
    result = workspace_context.export_context(
        {
            "repo": str(workspace.root),
            "title": args.title,
            "selected_paths": args.path,
            "include_diff": args.include_diff,
            "include_ai_bridge": args.include_ai_bridge,
        }
    )
    output_path = workspace.root / result["path"]
    markdown = output_path.read_text(encoding="utf-8")
    if args.copy:
        copied = subprocess.run(["pbcopy"], input=markdown, text=True, capture_output=True)
        if copied.returncode != 0:
            raise SystemExit(f"pbcopy failed: {copied.stderr}")
    print(f"Wrote {output_path}")
    print(f"Bytes: {result['bytes']}")
    print(f"Selected files: {len(result['selected_files'])}")
    print(f"Skipped files: {len(result['skipped_files'])}")
    return 0


def run_apply(workspace_context: WorkspaceContext, workspace, args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if args.stdin else Path(args.file).read_text(encoding="utf-8")
    plan = normalize_plan(raw, args.title)
    result = workspace_context.write_handoff(
        {
            "repo": str(workspace.root),
            "plan": plan,
            "title": args.title,
            "append": args.append,
            "agent": args.agent,
            "model": args.model,
        }
    )
    print(f"Wrote {workspace.root / result['path']}")
    print(f"Bytes: {result['bytes']}")
    print(f"Status: {workspace.root / result['status_path']}")
    return 0


def normalize_plan(raw: str, title: str) -> str:
    text = raw.strip()
    if not text:
        raise ValueError("Plan is empty.")
    if text.startswith("#"):
        return text + "\n"
    return f"# {title}\n\n{text}\n"


if __name__ == "__main__":
    raise SystemExit(main())
