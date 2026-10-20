#!/usr/bin/env python3
"""Privately register or remap one alias in a Desktop-task targets file.

This is an operator-only helper. It is intentionally not an MCP tool and never
prints the raw task id. The targets file remains mode 0600 and is updated
atomically so a running PatchBay instance can be restarted after a Desktop
task is replaced.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from patchbay.desktop_tasks import _alias, _thread_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets-file", required=True, help="Private Desktop-task targets JSON file.")
    parser.add_argument("--alias", required=True, help="Human alias to create or remap.")
    parser.add_argument("--thread-id", required=True, help="Local Codex Desktop task id.")
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create the alias when it does not already exist (remapping is the default).",
    )
    return parser


def register(path_value: str, alias_value: str, thread_id_value: str, *, create: bool = False) -> None:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise ValueError("targets file must be an absolute path")
    metadata = path.stat()
    if not path.is_file():
        raise ValueError("targets file must be a regular file")
    if metadata.st_mode & 0o077:
        raise ValueError("targets file must be private (mode 0600 or stricter)")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("targets"), dict):
        raise ValueError("targets file must contain a targets object")

    alias = _alias(alias_value)
    thread_id = _thread_id(thread_id_value)
    targets = payload["targets"]
    if alias not in targets and not create:
        raise ValueError("alias is not registered; pass --create for a new alias")
    for other_alias, value in targets.items():
        if other_alias == alias:
            continue
        other_id = value.get("thread_id", value.get("session_id")) if isinstance(value, dict) else value
        if other_id == thread_id:
            raise ValueError("task id is already registered under another alias")

    current = targets.get(alias)
    if isinstance(current, dict):
        updated = dict(current)
        updated["thread_id"] = thread_id
        updated.pop("session_id", None)
        targets[alias] = updated
    else:
        targets[alias] = {"thread_id": thread_id}

    mode = metadata.st_mode & 0o777
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    args = _parser().parse_args()
    try:
        register(args.targets_file, args.alias, args.thread_id, create=args.create)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"register_desktop_task: {error}")
        return 2
    print(f"registered Desktop task alias {args.alias!r}; restart PatchBay before retrying")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
