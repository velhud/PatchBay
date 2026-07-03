"""Shared local security helpers for patchbay."""
from pathlib import Path
from typing import Any
import re


SENSITIVE_CONFIG_KEYS = ("token", "key", "secret", "password", "auth", "credential")

PUBLIC_ERROR_MAX_CHARS = 240

ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w.-])(?:"
    r"/(?:Users|Volumes|private|tmp|var|home|mnt|opt|Applications|Library|System|workspace|repo)\S*"
    r"|[A-Za-z]:\\[^\s\"'`<>|;]+"
    r")"
)

PATH_DETAIL_PREFIXES = (
    "Path is outside configured allowed roots",
    "Workspace root does not exist",
    "Workspace root is not a directory",
    "Not a git repository",
    "Job worktree could not be created",
    "Worker worktree already exists",
    "Worker worktree could not be created",
    "Worker worktree path is outside worker root",
    "cwd is not a directory",
)

SAFE_VALIDATION_PREFIXES = (
    "Missing required argument",
    "Invalid type for argument",
    "Invalid value for argument",
    "Unknown argument",
    "Unknown or unavailable tool",
    "Tool arguments must be an object",
    "Tool is unavailable",
    "resources/read requires",
    "Unknown resource URI",
    "Invalid tool mode",
    "No allowed repository roots configured",
    "No workspace path provided",
    "Maximum active jobs",
    "Job worktree could not be created",
    "Worker worktree could not be created",
    "dangerously_bypass is disabled",
    "config_overrides are disabled",
    "Config override is not allowed",
    "codex_review accepts either",
    "codex_write_file is disabled",
    "codex_edit_file is disabled",
    "codex_run_command is disabled",
    "codex_read_session is disabled",
    "session_id is required",
    "Missing session reference",
    "Missing session_ref/session_id",
    "model must be",
    "reasoning_effort must be",
    "workspace_mode must be",
    "context_detail must be",
    "file_path is required",
    "file_path is blocked",
    "file_path must",
    "worker is required",
    "Unknown worker",
    "Worker name is ambiguous",
    "view must be",
    "name is required",
    "brief is required",
    "message is required",
    "query is required",
    "plan is required",
    "old_text must not be empty",
    "old_text was not found",
    "old_text matched",
    "Expected ",
    "File already exists",
    "Parent directory does not exist",
    "Path is blocked by safety rules",
    "Write content is too large",
    "Edited file is too large",
    "File is too large",
    "Refusing to read binary file",
    "Secret-looking content is blocked",
)

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


def redact_local_paths(value: str) -> str:
    """Redact common absolute local filesystem paths from text."""
    return ABSOLUTE_PATH_PATTERN.sub("[REDACTED_PATH]", value)


def public_error_message(
    error: Any,
    *,
    default: str = "Operation could not be completed.",
    allow_details: bool = False,
) -> str:
    """Return a bounded, ChatGPT-safe error string.

    Expected validation failures stay actionable. Unexpected failures should be
    summarized generically because raw exception text can contain local paths,
    command fragments, prompts, tokens, or implementation details.
    """
    if error is None:
        return default
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return default
    text = redact_local_paths(redact_text(text))
    text = re.sub(r"\s+", " ", text).strip()
    for prefix in PATH_DETAIL_PREFIXES:
        if text.startswith(prefix):
            text = prefix
            break
    if len(text) > PUBLIC_ERROR_MAX_CHARS:
        text = text[: PUBLIC_ERROR_MAX_CHARS - 3].rstrip() + "..."
    if allow_details or any(text.startswith(prefix) for prefix in SAFE_VALIDATION_PREFIXES):
        return text
    return default


def internal_log_error(error: Any) -> str:
    """Return redacted exception text for server logs."""
    if error is None:
        return ""
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    text = redact_local_paths(redact_text(text))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 500:
        return text[:497].rstrip() + "..."
    return text


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
