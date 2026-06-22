"""Bounded Codex session transcript reader."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from security import redact_text


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class CodexSessionReader:
    """Read local Codex JSONL sessions only when explicitly enabled."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def read_session(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        max_messages = self._bounded_int(
            args.get("max_messages"),
            default=int(self.power_config().get("codex_session_max_messages", 80)),
            minimum=1,
            maximum=400,
        )
        max_total_bytes = self._bounded_int(
            args.get("max_total_bytes"),
            default=int(self.power_config().get("codex_session_max_bytes", 80_000)),
            minimum=4_000,
            maximum=400_000,
        )

        source = self._resolve_session_file(session_id)
        stat = source.stat()
        max_file_bytes = int(self.power_config().get("codex_session_max_file_bytes", 20_000_000))
        if stat.st_size > max_file_bytes:
            raise ValueError(f"Codex session file is too large ({stat.st_size} bytes)")

        meta = self._parse_session_meta(source, expected_session_id=session_id)
        messages, truncated = self._load_messages(source, max_messages=max_messages, max_total_bytes=max_total_bytes)
        text = self._format_transcript(meta, messages, truncated)
        return {
            "session": meta,
            "messages": messages,
            "message_count": len(messages),
            "truncated": truncated,
            "text": text,
            "transcript_returned": True,
            "paths_returned": False,
            "source_path_returned": False,
        }

    def power_config(self) -> dict[str, Any]:
        value = self.config.get("power_tools")
        return value if isinstance(value, dict) else {}

    def _require_enabled(self) -> None:
        if not bool(self.power_config().get("codex_session_read", False)):
            raise ValueError("codex_read_session is disabled. Set power_tools.codex_session_read to true.")

    def _codex_home(self) -> Path:
        configured = str(self.power_config().get("codex_home") or "").strip()
        return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()

    def _session_roots(self) -> list[Path]:
        home = self._codex_home()
        return [home / "sessions", home / "archived_sessions"]

    def _resolve_session_file(self, session_id: str) -> Path:
        for file_path in self._iter_session_files():
            meta = self._parse_session_meta(file_path, expected_session_id=None)
            if meta.get("session_id") == session_id:
                return file_path
        raise ValueError(f"Codex session not found: {session_id}")

    def _iter_session_files(self) -> Iterable[Path]:
        max_files = int(self.power_config().get("codex_session_max_scan_files", 3000))
        max_depth = int(self.power_config().get("codex_session_max_scan_depth", 6))
        seen = 0
        for root in self._session_roots():
            if not root.exists():
                continue
            root = root.resolve()
            for file_path in sorted(root.rglob("*.jsonl")):
                if seen >= max_files:
                    return
                try:
                    resolved = file_path.resolve(strict=True)
                except OSError:
                    continue
                if not self._is_under_root(resolved, root):
                    continue
                depth = len(resolved.relative_to(root).parts) - 1
                if depth > max_depth:
                    continue
                if resolved.is_file():
                    seen += 1
                    yield resolved

    def _parse_session_meta(self, file_path: Path, expected_session_id: Optional[str]) -> dict[str, Any]:
        session_id = self._infer_session_id_from_filename(file_path)
        title = ""
        summary = ""
        created_at = None
        last_active_at = None

        for value in self._iter_json_lines(file_path, max_lines=600):
            timestamp = self._parse_timestamp(value.get("timestamp")) if isinstance(value, dict) else None
            created_at = created_at if created_at is not None else timestamp
            last_active_at = timestamp if timestamp is not None else last_active_at

            if value.get("type") == "session_meta" and isinstance(value.get("payload"), dict):
                payload = value["payload"]
                session_id = str(
                    payload.get("id") or payload.get("session_id") or payload.get("sessionId") or session_id or ""
                ).strip()

            if value.get("type") == "response_item" and isinstance(value.get("payload"), dict):
                payload = value["payload"]
                if payload.get("type") == "message":
                    role = str(payload.get("role") or "")
                    text = self._extract_text(payload.get("content"))
                    if role == "user" and not title and text.strip():
                        title = self._truncate(redact_text(text), 96)
                    if text.strip():
                        summary = self._truncate(redact_text(text), 180)

        if expected_session_id and session_id != expected_session_id:
            raise ValueError("Codex session id does not match session file")
        if not session_id:
            raise ValueError("Unable to determine Codex session id")
        return {
            "provider_id": "codex",
            "session_id": session_id,
            "title": title,
            "summary": summary,
            "created_at": created_at,
            "last_active_at": last_active_at,
            "resume_command": f"codex exec resume {session_id}",
            "source_path_returned": False,
        }

    def _load_messages(self, file_path: Path, *, max_messages: int, max_total_bytes: int) -> tuple[list[dict[str, Any]], bool]:
        messages: list[dict[str, Any]] = []
        used_bytes = 0
        truncated = False
        for value in self._iter_json_lines(file_path):
            if value.get("type") != "response_item" or not isinstance(value.get("payload"), dict):
                continue
            payload = value["payload"]
            role = ""
            content = ""
            if payload.get("type") == "message":
                role = str(payload.get("role") or "unknown")
                content = self._extract_text(payload.get("content"))
            elif payload.get("type") == "function_call":
                role = "assistant"
                content = f"[Tool: {payload.get('name') or 'unknown'}]"
            elif payload.get("type") == "function_call_output":
                role = "tool"
                content = str(payload.get("output") or "")
            if not content.strip():
                continue

            safe_content = redact_text(content)
            next_bytes = len(safe_content.encode("utf-8"))
            if len(messages) >= max_messages or used_bytes + next_bytes > max_total_bytes:
                truncated = True
                break
            used_bytes += next_bytes
            message: dict[str, Any] = {
                "role": role,
                "content": safe_content,
            }
            timestamp = self._parse_timestamp(value.get("timestamp"))
            if timestamp is not None:
                message["ts"] = timestamp
            messages.append(message)
        return messages, truncated

    def _iter_json_lines(self, file_path: Path, max_lines: Optional[int] = None) -> Iterable[dict[str, Any]]:
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if max_lines is not None and index >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value

    def _extract_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if text is None:
                        text = item.get("input_text")
                    if text is not None:
                        parts.append(str(text))
            return "\n".join(part for part in parts if part)
        if isinstance(value, dict) and value.get("text") is not None:
            return str(value["text"])
        return ""

    def _format_transcript(self, meta: dict[str, Any], messages: list[dict[str, Any]], truncated: bool) -> str:
        sections = [
            "# Codex Session",
            "",
            f"Session: {meta['session_id']}",
        ]
        if meta.get("title"):
            sections.extend(["", f"Title: {meta['title']}"])
        if truncated:
            sections.extend(["", "Transcript truncated by configured limits."])
        sections.extend(["", "## Transcript", ""])
        if not messages:
            sections.append("No readable transcript messages found.")
        else:
            for message in messages:
                when = f" {datetime.fromtimestamp(message['ts']).isoformat()}" if message.get("ts") else ""
                sections.extend([f"### {message['role']}{when}", "", message["content"], ""])
        return "\n".join(sections).strip() + "\n"

    def _parse_timestamp(self, value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        return None

    def _infer_session_id_from_filename(self, file_path: Path) -> str:
        match = UUID_RE.search(file_path.name)
        return match.group(0) if match else ""

    def _truncate(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 1)].rstrip() + "..."

    def _bounded_int(self, value: Any, *, default: int, minimum: int, maximum: int) -> int:
        if value is None:
            number = default
        else:
            number = int(value)
        return max(minimum, min(number, maximum))

    def _is_under_root(self, child: Path, root: Path) -> bool:
        return child == root or root in child.parents
