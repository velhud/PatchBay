"""Observe authoritative terminal events for one exact Codex session."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from patchbay.codex_home import resolve_codex_home


@dataclass(frozen=True)
class SessionTerminalSnapshot:
    """Bounded semantic completion evidence from a Codex session JSONL."""

    completed: bool = False
    observed_at: float | None = None
    final_message: str = ""
    source: str = ""


class CodexSessionTerminalObserver:
    """Incrementally tail one exact Codex session without exposing its path."""

    def __init__(
        self,
        config: dict[str, Any],
        session_id: str,
        *,
        not_before: float,
        max_final_message_chars: int = 200_000,
    ) -> None:
        self.config = config
        self.session_id = str(session_id or "").strip()
        self.not_before = float(not_before)
        self.max_final_message_chars = max(1, int(max_final_message_chars))
        self._source: Path | None = None
        self._offset = 0
        self._pending = b""
        self._latest_agent_message = ""
        self._snapshot = SessionTerminalSnapshot()

    def poll(self) -> SessionTerminalSnapshot:
        """Read newly appended records and return the latest terminal snapshot."""
        if self._snapshot.completed:
            return self._snapshot
        if not self.session_id:
            return self._snapshot
        if self._source is None:
            self._source = self._resolve_exact_session_file()
            if self._source is None:
                return self._snapshot
        try:
            with self._source.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return self._snapshot
        if not chunk:
            return self._snapshot

        data = self._pending + chunk
        lines = data.split(b"\n")
        self._pending = lines.pop() if lines else b""
        for raw_line in lines:
            self._observe_line(raw_line)
            if self._snapshot.completed:
                break
        if not self._snapshot.completed and self._pending and self._decode_line(self._pending) is not None:
            complete_line = self._pending
            self._pending = b""
            self._observe_line(complete_line)
        return self._snapshot

    def _resolve_exact_session_file(self) -> Path | None:
        home = resolve_codex_home(self.config)
        candidates: list[Path] = []
        for root in (home / "sessions", home / "archived_sessions"):
            if not root.exists():
                continue
            resolved_root = root.resolve(strict=False)
            for candidate in root.rglob(f"*{self.session_id}*.jsonl"):
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if resolved_root not in resolved.parents or not resolved.is_file():
                    continue
                if self._file_declares_session(resolved):
                    candidates.append(resolved)
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _file_declares_session(self, source: Path) -> bool:
        try:
            with source.open("rb") as handle:
                for index, raw_line in enumerate(handle):
                    if index >= 80:
                        break
                    value = self._decode_line(raw_line)
                    if not isinstance(value, dict) or value.get("type") != "session_meta":
                        continue
                    payload = value.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    declared = str(payload.get("id") or payload.get("session_id") or payload.get("sessionId") or "").strip()
                    return declared == self.session_id
        except OSError:
            return False
        return False

    def _observe_line(self, raw_line: bytes) -> None:
        value = self._decode_line(raw_line)
        if not isinstance(value, dict):
            return
        timestamp = self._timestamp(value.get("timestamp"))
        if timestamp is None or timestamp + 1.0 < self.not_before:
            return

        record_type = str(value.get("type") or "")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        message = self._agent_message(record_type, payload)
        if message:
            self._latest_agent_message = message[-self.max_final_message_chars :]

        if record_type == "event_msg" and str(payload.get("type") or "") == "task_complete":
            final_message = str(payload.get("last_agent_message") or self._latest_agent_message or "")
            self._snapshot = SessionTerminalSnapshot(
                completed=True,
                observed_at=timestamp,
                final_message=final_message[-self.max_final_message_chars :],
                source="session_task_complete",
            )

    def _agent_message(self, record_type: str, payload: dict[str, Any]) -> str:
        if record_type == "event_msg" and str(payload.get("type") or "") == "agent_message":
            return str(payload.get("message") or payload.get("text") or "")
        if record_type != "response_item" or str(payload.get("type") or "") != "message":
            return ""
        if str(payload.get("role") or "") != "assistant":
            return ""
        return self._extract_content_text(payload.get("content"))

    def _extract_content_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, Iterable) or isinstance(content, (bytes, bytearray, dict)):
            return ""
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("value")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    def _decode_line(self, raw_line: bytes) -> dict[str, Any] | None:
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _timestamp(self, value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
