"""Durable store for optional PatchBay hub state."""
from __future__ import annotations

import json
import os
import secrets
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from patchbay.connector.profiles import resolve_runtime_path


STORE_VERSION = 1


def hub_state_path(config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> Path:
    configured = (config.get("hub") or {}).get("state_file") if isinstance(config.get("hub"), dict) else None
    return resolve_runtime_path(configured, "hub", "hub-state.json", environ=environ)


def generate_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class HubStore:
    """Small JSON-backed hub store.

    This is intentionally simple for the first hub release. It is private
    runtime state, not repository data.
    """

    def __init__(self, config: Mapping[str, Any], *, environ: Mapping[str, str] | None = None):
        self.config = dict(config)
        self.path = hub_state_path(config, environ=environ)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(self._empty())

    def _empty(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "hub_id": generate_secret("hub").replace("hub_", "hub-", 1)[:28],
            "created_at": time.time(),
            "enrollment_codes": {},
            "machines": {},
            "commands": {},
            "events": [],
        }

    def read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = self._empty()
        if not isinstance(payload, dict):
            payload = self._empty()
        payload.setdefault("version", STORE_VERSION)
        payload.setdefault("enrollment_codes", {})
        payload.setdefault("machines", {})
        payload.setdefault("commands", {})
        payload.setdefault("events", [])
        return payload

    def update(self, mutator) -> dict[str, Any]:
        payload = self.read()
        result = mutator(payload)
        self._write(payload)
        return result if result is not None else payload

    def _write(self, payload: Mapping[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def append_event(self, payload: dict[str, Any], event_type: str, data: Mapping[str, Any]) -> None:
        events = payload.setdefault("events", [])
        events.append(
            {
                "type": event_type,
                "created_at": time.time(),
                "data": deepcopy(dict(data)),
            }
        )
        if len(events) > int((self.config.get("hub") or {}).get("max_events", 1000)):
            del events[:-int((self.config.get("hub") or {}).get("max_events", 1000))]
