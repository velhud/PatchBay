"""Shared local security helpers for codex-mcp-wrapper."""
from pathlib import Path
from typing import Any
import re


SENSITIVE_CONFIG_KEYS = ("token", "key", "secret", "password", "auth", "credential")

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED_POSSIBLE_SECRET]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_POSSIBLE_SECRET]"),
    (
        re.compile(r"(?i)(OPENAI_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|GROQ_API_KEY|GEMINI_API_KEY)\s*=\s*[^\s]+"),
        "[REDACTED_POSSIBLE_SECRET]",
    ),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+"), r"\1[REDACTED_POSSIBLE_SECRET]"),
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:token|secret|password|credential|auth)[A-Za-z0-9_]*[\"']?\s*[:=]\s*)"
            r"(?!true\b|false\b|null\b)[^\"'\s,}&]+"
        ),
        r"\1[REDACTED_POSSIBLE_SECRET]",
    ),
]


def validate_allowed_path(path: str, allowed_roots: list[str]) -> Path:
    """Return a resolved path only when it is inside configured allowed roots."""
    if not allowed_roots:
        raise ValueError("No allowed repository roots configured")

    candidate = Path(path).expanduser().resolve()
    for root in allowed_roots:
        root_path = Path(root).expanduser().resolve()
        if candidate == root_path or root_path in candidate.parents:
            return candidate

    raise ValueError(f"Path is outside configured allowed roots: {path}")


def redact_text(value: str) -> str:
    """Redact likely secrets from text."""
    redacted = value
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_sensitive_output(data: Any) -> Any:
    """Recursively redact likely secrets from output."""
    if isinstance(data, str):
        return redact_text(data)
    if isinstance(data, dict):
        return {k: redact_sensitive_output(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_sensitive_output(v) for v in data]
    return data


def redact_config_value(key: str, value: Any) -> Any:
    """Redact config values whose keys look credential-related."""
    if any(part in key.lower() for part in SENSITIVE_CONFIG_KEYS):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    return value
