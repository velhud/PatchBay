"""Shared constants and helpers for Pro Escalation requests."""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
REQUEST_ID_RE = re.compile(r"^proreq_[0-9]{8}_[0-9]{6}_[0-9a-f]{6,12}$")
DEFAULT_REPORT_BYTES = 200_000
DEFAULT_RESPONSE_BYTES = 200_000
DEFAULT_ATTACHMENT_BYTES = 2_000_000
DEFAULT_MAX_ATTACHMENTS = 10

STATUSES = {
    "open",
    "claimed",
    "needs_context",
    "answered",
    "dispatch_requested",
    "dispatched_to_worker",
    "dispatch_blocked",
    "closed",
    "cancelled",
    "stale",
    "superseded",
}

EVENT_TYPES = {
    "created",
    "read",
    "claimed",
    "responded",
    "dispatch_requested",
    "dispatched_to_worker",
    "dispatch_blocked",
    "closed",
    "cancelled",
    "stale_detected",
    "superseded",
}


def now_ts() -> float:
    return time.time()


def new_request_id(now: float | None = None) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(now_ts() if now is None else now))
    return f"proreq_{ts}_{uuid.uuid4().hex[:8]}"


def validate_request_id(request_id: str) -> str:
    request_id = str(request_id or "").strip()
    if not REQUEST_ID_RE.match(request_id):
        raise ValueError("Invalid Pro Request id")
    return request_id


def clean_short_text(value: Any, *, field: str, max_chars: int = 500) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError(f"{field} is required")
    return text[:max_chars]


def optional_short_text(value: Any, *, max_chars: int = 500) -> str:
    return " ".join(str(value or "").split())[:max_chars]


def bounded_text(value: str, max_bytes: int) -> tuple[str, bool, int]:
    raw = str(value or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(value or ""), False, len(raw)
    trimmed = raw[:max_bytes].decode("utf-8", errors="ignore")
    return trimmed, True, len(raw)


def safe_attachment_name(path: str | Path) -> str:
    raw = str(Path(path).name or "").strip()
    if not raw:
        raise ValueError("Attachment filename is required")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or ".." in pure.parts or "/" in raw or "\\" in raw:
        raise ValueError("Attachment filename is not safe")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not cleaned:
        raise ValueError("Attachment filename is not safe")
    return cleaned[:120]


def request_summary_from_report(report: str, max_chars: int = 300) -> str:
    for line in report.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:max_chars]
    return ""
