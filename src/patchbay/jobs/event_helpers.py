"""Stateless parsing and formatting helpers for Codex JSONL events."""

import json
import re
from typing import Any, Dict, Optional

from patchbay.security import redact_text


def event_item_from_event(event: Dict[str, Any]) -> Dict[str, Any]:
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    if item:
        return item
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    data_item = data.get("item") if isinstance(data.get("item"), dict) else {}
    if data_item:
        return data_item
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    payload_item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    return payload_item or {}


def agent_item_from_event(event: Dict[str, Any], *, event_item: Any = event_item_from_event) -> Dict[str, Any]:
    item = event_item(event)
    return item if item and str(item.get("type") or "") in {"agent_message", "message"} else {}


def clip_status_text(value: str) -> str:
    text = redact_text(str(value or "").strip())
    text = " ".join(text.split())
    if len(text) <= 240:
        return text
    return text[:237].rstrip() + "..."


def command_preview_from_item(item: Dict[str, Any], *, clip_text: Any = clip_status_text) -> str:
    if not item:
        return ""
    for key in ("command", "cmd", "text", "description", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return clip_text(value)
    for key in ("input", "arguments", "params"):
        value = item.get(key)
        if isinstance(value, dict):
            for nested_key in ("command", "cmd", "text", "description"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return clip_text(nested)
            if value:
                return clip_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
        if isinstance(value, str) and value.strip():
            return clip_text(value)
    return ""


def text_from_agent_item(item: Dict[str, Any]) -> str:
    for key in ("text", "message", "content_text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    content = item.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for entry in content:
            if isinstance(entry, str) and entry.strip():
                parts.append(entry.strip())
                continue
            if not isinstance(entry, dict):
                continue
            for key in ("text", "content"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
                    break
        if parts:
            return "\n".join(parts).strip()
    return ""


def clip_checkpoint_text(value: str, *, max_chars: int) -> str:
    text = redact_text(str(value or "").strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[checkpoint truncated]"


def checkpoint_from_event(
    event: Dict[str, Any],
    *,
    clock: Any,
    agent_item: Any = agent_item_from_event,
    text_from_item: Any = text_from_agent_item,
    clip_text: Any,
) -> Optional[Dict[str, Any]]:
    item = agent_item(event)
    if not item:
        return None
    text = text_from_item(item)
    if not text:
        return None

    summary = text
    details: Dict[str, Any] = {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        candidate = parsed.get("summary") or parsed.get("detailed_report") or parsed.get("notes") or parsed.get("message")
        if isinstance(candidate, str) and candidate.strip():
            summary = candidate.strip()
        for key in ("evidence", "files_changed", "commands_run", "tests_run", "risks", "open_questions", "next_steps"):
            value = parsed.get(key)
            if isinstance(value, list):
                details[f"{key}_count"] = len(value)
        if isinstance(parsed.get("notes"), str) and parsed["notes"].strip() and parsed["notes"].strip() != summary:
            details["notes"] = clip_text(parsed["notes"])

    return {
        "kind": "agent_message",
        "event": str(event.get("type") or "json_event"),
        "item_status": str(item.get("status") or ""),
        "at": clock(),
        "summary": clip_text(summary),
        **details,
    }


def session_id_from_event(event: Dict[str, Any]) -> Optional[str]:
    if event.get("type") != "thread.started":
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    value = event.get("thread_id") or data.get("thread_id")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value) else None


def event_progress_label(event: Dict[str, Any]) -> str:
    event_type = str(event.get("type") or "json_event")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    if item:
        item_type = str(item.get("type") or "item")
        status = str(item.get("status") or "").strip()
        return f"Codex event: {event_type} ({item_type}{', ' + status if status else ''})."
    return f"Codex event: {event_type}."


def activity_updates_from_event(
    job: Any,
    *,
    chunk: bytes,
    now: float,
    event_label: str,
    item: Dict[str, Any],
    command_preview: Any = command_preview_from_item,
) -> Dict[str, Any]:
    updates: Dict[str, Any] = {
        "event_count": (int(getattr(job, "event_count", 0) or 0) + 1) if job else 1,
        "stdout_bytes_seen": (int(getattr(job, "stdout_bytes_seen", 0) or 0) + len(chunk)) if job else len(chunk),
        "last_stdout_at": now,
    }
    item_type = str(item.get("type") or "").strip()
    item_status = str(item.get("status") or "").strip()
    if item_type:
        updates["current_item_type"] = item_type
    if item_status:
        updates["current_item_status"] = item_status

    preview = command_preview(item)
    if item_type == "command_execution":
        if event_label == "item.started":
            updates.update(current_phase="command_running", current_command_preview=preview, current_command_started_at=now)
        elif event_label == "item.completed":
            updates.update(
                current_phase="command_completed_waiting_for_model",
                last_command_preview=preview or str(getattr(job, "current_command_preview", "") or ""),
                last_command_completed_at=now,
                current_command_preview=None,
                current_command_started_at=None,
            )
    elif item_type in {"agent_message", "message"}:
        updates["current_phase"] = "agent_message_emitted"
    elif event_label == "turn.completed":
        updates["current_phase"] = "finalizing_report"
    elif event_label in {"thread.started", "turn.started"}:
        updates["current_phase"] = "model_reasoning"
    elif event_label.startswith("item."):
        updates["current_phase"] = item_type or "item_activity"
    elif event_label == "stdout":
        updates["current_phase"] = "stdout_output"
    return updates
