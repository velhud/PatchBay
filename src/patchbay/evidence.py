"""Private runtime evidence recording for PatchBay.

The normal audit log is intentionally compact. This module stores the full
private work contract when explicitly enabled: MCP request/response bodies,
worker briefs, Codex prompts, and related metadata. These files live under the
runtime home, not the repository, and are not exposed through public tool
responses.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from patchbay.connector.profiles import resolve_runtime_path


def _utc_iso(ts: float | None = None) -> str:
    value = time.time() if ts is None else ts
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return cleaned[:120] or "unknown"


def evidence_root(config: Mapping[str, Any]) -> Path:
    logging_config = config.get("logging", {}) if isinstance(config.get("logging"), dict) else {}
    return resolve_runtime_path(
        logging_config.get("private_evidence_dir"),
        "logs",
        "private-evidence",
    )


def job_prompt_logging_enabled(config: Mapping[str, Any]) -> bool:
    logging_config = config.get("logging", {}) if isinstance(config.get("logging"), dict) else {}
    return bool(
        logging_config.get("private_evidence_log", False)
        or logging_config.get("store_job_prompts", False)
    )


def mcp_transcript_logging_enabled(config: Mapping[str, Any], *, direction: str | None = None) -> bool:
    logging_config = config.get("logging", {}) if isinstance(config.get("logging"), dict) else {}
    if bool(
        logging_config.get("private_evidence_log", False)
        or logging_config.get("store_mcp_transcripts", False)
    ):
        return True
    if direction == "request":
        return bool(logging_config.get("log_prompt_bodies", False))
    if direction == "response":
        return bool(logging_config.get("log_response_bodies", False))
    return bool(logging_config.get("log_prompt_bodies", False) or logging_config.get("log_response_bodies", False))


class EvidenceRecorder:
    """Append-only private runtime evidence writer."""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        self.root = evidence_root(config)
        self._lock = threading.Lock()

    def record_job_brief(
        self,
        *,
        job_id: str,
        mode: str,
        prompt: str,
        repo_path: str,
        options: Mapping[str, Any] | None = None,
        worktree_path: str | None = None,
        branch_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist a complete private job brief and return its index metadata."""
        if not job_prompt_logging_enabled(self.config):
            return None
        prompt_bytes = prompt.encode("utf-8")
        digest = hashlib.sha256(prompt_bytes).hexdigest()
        job_dir = self.root / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "kind": "job_brief",
            "recorded_at": _utc_iso(),
            "job_id": job_id,
            "mode": mode,
            "repo_path": repo_path,
            "worktree_path": worktree_path,
            "branch_name": branch_name,
            "prompt_sha256": digest,
            "prompt_bytes": len(prompt_bytes),
            "options": dict(options or {}),
            "prompt": prompt,
        }
        path = job_dir / "brief.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
        return {
            "prompt_artifact": str(path),
            "prompt_sha256": digest,
            "prompt_bytes": len(prompt_bytes),
            "prompt_recorded_at": payload["recorded_at"],
        }

    def record_mcp_event(
        self,
        *,
        client_ref: str,
        owner_ref: str | None,
        direction: str,
        message: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        """Append a full private MCP event when transcript logging is enabled."""
        if not mcp_transcript_logging_enabled(self.config, direction=direction):
            return
        now = time.time()
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        path = self.root / "mcp" / day / f"{_safe_name(client_ref)}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "kind": "mcp_event",
            "recorded_at": _utc_iso(now),
            "client_ref": client_ref,
            "owner_ref": owner_ref,
            "direction": direction,
        }
        if status_code is not None:
            payload["status_code"] = status_code
        if message is not None:
            payload["message"] = message
        if response is not None:
            payload["response"] = response
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
