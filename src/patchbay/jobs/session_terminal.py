"""Observe authoritative terminal events for one exact Codex session."""
from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from patchbay.codex_home import resolve_codex_home

MAX_SESSION_RECORD_BYTES = 4 * 1024 * 1024
MAX_POLL_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SessionTerminalSnapshot:
    """Bounded semantic completion evidence from a Codex session JSONL."""

    completed: bool = False
    observed_at: float | None = None
    final_message: str = ""
    source: str = ""
    report_truncated: bool = False
    original_characters: int = 0
    original_bytes: int = 0
    report_sha256: str = ""
    retained_ranges: tuple = ()
    oversized_records: int = 0
    source_status: str = "unresolved"


class CodexSessionTerminalObserver:
    """Incrementally tail one exact Codex session without exposing its path."""

    def __init__(
        self,
        config: dict[str, Any],
        session_id: str,
        *,
        not_before: float,
        initial_offset: int = 0,
        max_final_message_chars: int = 200_000,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.session_id = str(session_id or "").strip()
        self.not_before = float(not_before)
        self.max_final_message_chars = max(1, int(max_final_message_chars))
        self._source: Path | None = None
        self._offset = max(0, int(initial_offset))
        self._pending = b""
        self._discarding_record = False
        self.oversized_records = 0
        self._latest_agent_message = ""
        self._snapshot = SessionTerminalSnapshot()
        self._initial_offset = self._offset
        self._record_boundary = self._offset
        self._record_range = (0, 0)
        self._message_range = None
        self._final_range = None
        self._file_token = ""
        self._checkpoint = dict(checkpoint or {})
        self._source_error = ""
        self._anchor_sha256 = ''
        self._observed_size = None
        self._observed_mtime = None

    def _identity(self, handle) -> str:
        stat = os.fstat(handle.fileno())
        handle.seek(0)
        header = handle.readline(MAX_SESSION_RECORD_BYTES + 1)
        return hashlib.sha256(repr((stat.st_dev, stat.st_ino,
            getattr(stat, 'st_birthtime', None), hashlib.sha256(header).hexdigest())).encode()).hexdigest()

    def checkpoint(self) -> dict[str, Any]:
        """Persist ranges, not report bodies or a partially decoded suffix.

        Restart reconstructs at most one bounded record and one report record
        from the same source generation before the next bounded poll.
        """
        if self._source is None or not self._file_token:
            return {}
        return dict(version=1, session_id=self.session_id, initial_offset=self._initial_offset,
            source_token=self._file_token, offset=self._offset, record_boundary=self._record_boundary,
            discarding_record=self._discarding_record, message_range=self._message_range,
            anchor_sha256=self._anchor_sha256, observed_size=self._observed_size,
            observed_mtime=self._observed_mtime,
            oversized_records=self.oversized_records)

    @property
    def has_unread_bytes(self) -> bool:
        try:
            return not self._source_error and self._source is not None and self._source.stat().st_size > self._offset
        except OSError:
            return False

    def _open_source(self):
        if self._source_error:
            return None
        if self._source is None:
            self._source = self._resolve_exact_session_file()
        if self._source is None:
            return None
        handle = self._source.open('rb')
        token = self._identity(handle)
        checkpoint = self._checkpoint
        if not self._file_token:
            self._file_token = token
            if checkpoint:
                if (checkpoint.get('version') != 1 or checkpoint.get('session_id') != self.session_id
                        or checkpoint.get('initial_offset') != self._initial_offset
                        or checkpoint.get('source_token') != token):
                    self._source_error = 'source_checkpoint_mismatch'
                else:
                    offset = checkpoint.get('offset', self._offset)
                    boundary = checkpoint.get('record_boundary', offset)
                    if not (isinstance(offset, int) and isinstance(boundary, int)
                            and self._initial_offset <= boundary <= offset
                            and offset-boundary <= MAX_SESSION_RECORD_BYTES):
                        self._source_error = 'source_checkpoint_invalid'
                    elif os.fstat(handle.fileno()).st_size < offset:
                        self._source_error = 'source_truncated'
                    else:
                        self._offset, self._record_boundary = offset, boundary
                        self._anchor_sha256 = str(checkpoint.get('anchor_sha256') or '')
                        self._observed_size = checkpoint.get('observed_size')
                        self._observed_mtime = checkpoint.get('observed_mtime')
                        self._discarding_record = bool(checkpoint.get('discarding_record'))
                        if not self._discarding_record:
                            handle.seek(boundary)
                            self._pending = handle.read(offset-boundary)
                        self.oversized_records = int(checkpoint.get('oversized_records') or 0)
                        span = checkpoint.get('message_range')
                        if isinstance(span, (list, tuple)) and len(span) == 2:
                            raw = self._read_record(handle, span)
                            record = self._decode_line(raw)
                            if record:
                                self._latest_agent_message = self._agent_message(str(record.get('type') or ''), record.get('payload') or {})
                                self._message_range = tuple(span)
                self._checkpoint = {}
        elif token != self._file_token:
            self._source_error = 'source_replaced'
        if os.fstat(handle.fileno()).st_size < self._offset:
            self._source_error = 'source_truncated'
        info = os.fstat(handle.fileno())
        if (self._observed_size == info.st_size and self._observed_mtime is not None
                and self._observed_mtime != info.st_mtime_ns):
            self._source_error = 'source_modified_without_append'
        if self._anchor_sha256 and self._anchor(handle) != self._anchor_sha256:
            self._source_error = 'source_cursor_content_changed'
        if self._source_error:
            handle.close()
            return None
        return handle

    def _anchor(self, handle):
        start = max(0, self._offset-256)
        handle.seek(start)
        return hashlib.sha256(handle.read(self._offset-start)).hexdigest()

    def _read_record(self, handle, span) -> bytes:
        start, end = span
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end
                and end-start <= MAX_SESSION_RECORD_BYTES+1):
            return b''
        handle.seek(start)
        return handle.read(end-start)

    def complete_report(self) -> str | None:
        """Retrieve one exact bounded source record, never a guessed session."""
        if self._final_range is None:
            return None
        try:
            handle = self._open_source()
            if handle is None:
                return None
            with handle:
                value = self._decode_line(self._read_record(handle, self._final_range))
            if not value:
                return None
            payload = value.get('payload') or {}
            message = (payload.get('last_agent_message') if value.get('type') == 'event_msg'
                       and payload.get('type') == 'task_complete' else None)
            message = str(message or self._agent_message(str(value.get('type') or ''), payload) or '')
            return message if hashlib.sha256(message.encode()).hexdigest() == self._snapshot.report_sha256 else None
        except OSError:
            return None

    def prime_to_end(self) -> int:
        """Ignore existing records and observe only later appends for this session."""

        if not self.session_id:
            return 0
        try:
            handle = self._open_source()
            if handle is None:
                return 0
            with handle:
                self._offset = max(0, int(os.fstat(handle.fileno()).st_size))
                self._anchor_sha256 = self._anchor(handle)
                info = os.fstat(handle.fileno())
                self._observed_size, self._observed_mtime = info.st_size, info.st_mtime_ns
        except OSError:
            return self._offset
        self._pending = b""
        self._discarding_record = False
        self._initial_offset = self._record_boundary = self._offset
        return self._offset

    def poll(self) -> SessionTerminalSnapshot:
        """Read newly appended records and return the latest terminal snapshot."""
        if self._snapshot.completed:
            return self._snapshot
        if not self.session_id:
            return self._snapshot
        try:
            handle = self._open_source()
            if handle is None:
                return replace(self._snapshot, source_status=self._source_error or 'source_unavailable')
            with handle:
                handle.seek(self._offset)
                chunk = handle.read(MAX_POLL_BYTES)
                self._offset = handle.tell()
                self._anchor_sha256 = self._anchor(handle)
                info = os.fstat(handle.fileno())
                self._observed_size, self._observed_mtime = info.st_size, info.st_mtime_ns
        except OSError:
            return replace(self._snapshot, source_status='source_unavailable')
        if not chunk:
            return self._snapshot

        pieces = chunk.split(b"\n")
        physical = self._offset-len(chunk)
        for index, piece in enumerate(pieces):
            terminated = index < len(pieces) - 1
            physical += len(piece) + int(terminated)
            if self._discarding_record:
                if terminated:
                    self._discarding_record = False
                self._record_boundary = physical
                continue
            if len(self._pending) + len(piece) > MAX_SESSION_RECORD_BYTES:
                self._pending = b""
                self.oversized_records += 1
                self._discarding_record = not terminated
                self._record_boundary = physical
                continue
            self._pending += piece
            if not terminated:
                continue
            self._record_range = (self._record_boundary, physical)
            self._observe_line(self._pending)
            self._pending = b""
            self._record_boundary = physical
            if self._snapshot.completed:
                break
        if not self._snapshot.completed and self._pending and self._decode_line(self._pending) is not None:
            complete_line = self._pending
            self._pending = b""
            self._record_range = (self._record_boundary, self._offset)
            self._observe_line(complete_line)
            self._record_boundary = self._offset
        return replace(self._snapshot, oversized_records=self.oversized_records,
                       source_status=self._source_error or 'current')

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
                for index in range(80):
                    raw_line = handle.readline(MAX_SESSION_RECORD_BYTES + 1)
                    if not raw_line or len(raw_line) > MAX_SESSION_RECORD_BYTES:
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
            self._latest_agent_message = message
            self._message_range = self._record_range

        if record_type == "event_msg" and str(payload.get("type") or "") == "task_complete":
            final_message = str(payload.get("last_agent_message") or self._latest_agent_message or "")
            self._final_range = (self._record_range if payload.get('last_agent_message') else self._message_range)
            size = len(final_message)
            clipped = size > self.max_final_message_chars
            marker = '\n...[middle omitted; retrieve complete report]...\n'
            budget = max(0, self.max_final_message_chars-len(marker))
            first, last = budget//2, budget-budget//2
            if clipped and budget:
                preview = final_message[:first]+marker+final_message[-last:]
                ranges = ((0,first),(size-last,size))
            elif clipped:
                preview = final_message[:self.max_final_message_chars]
                ranges = ((0,len(preview)),)
            else:
                preview, ranges = final_message, ((0,size),)
            self._snapshot = SessionTerminalSnapshot(
                completed=True,
                observed_at=timestamp,
                final_message=preview,
                source="session_task_complete",
                report_truncated=clipped, original_characters=size, original_bytes=len(final_message.encode()),
                report_sha256=hashlib.sha256(final_message.encode()).hexdigest(),
                retained_ranges=ranges,
                oversized_records=self.oversized_records, source_status='current',
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
