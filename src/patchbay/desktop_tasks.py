"""Opt-in durable bridge for continuing a handed-off Codex Desktop task.

The public API uses a private operator allowlist and human aliases. The actual
Codex session id, execution paths, and job id remain private runtime state.
Desktop owns archive/unarchive and writer handoff; this module only starts and
reads bounded local CLI jobs through PatchBay's durable executor.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from patchbay.jobs.manager import (
    JobInfo,
    JobState,
    terminal_cleanup_pending,
    terminal_cleanup_recovery_required,
)
from patchbay.desktop_task_handoff import (
    DEFAULT_HANDOFF_TIMEOUT_MS,
    DesktopHandoffError,
    prepare_desktop_task,
)
from patchbay.security import redact_local_paths, redact_text
from patchbay.workers.model_options import build_reasoning_config_override


MAX_ALIAS_LENGTH = 80
MAX_RECEIPT_LENGTH = 128
# The MCP schema advertises the absolute cap so a target-specific private
# limit cannot reject a human-readable brief before PatchBay sees it. Each
# alias may choose a lower limit in the private targets file.
MAX_PROMPT_LENGTH_CAP = 16_000
DEFAULT_PROMPT_LENGTH = 12_000
# Compatibility name for callers that imported the old limit. It now means
# the default per-target limit; MAX_PROMPT_LENGTH_CAP is the hard public cap.
MAX_PROMPT_LENGTH = DEFAULT_PROMPT_LENGTH
MIN_PROMPT_LENGTH = 1_000
MAX_ANSWER_LENGTH = 12_000
# Reports are persisted after sanitization.  The cap is deliberately larger
# than the default response chunk so callers can reassemble a useful report
# without allowing an unbounded model response into durable job state.
MAX_REPORT_LENGTH = 200_000
MAX_REPORT_CHUNK_LENGTH = 12_000
DEFAULT_REPORT_CHUNK_LENGTH = MAX_ANSWER_LENGTH
MAX_TIMEOUT_MS = 24 * 60 * 60 * 1_000
DEFAULT_TIMEOUT_MS = 30 * 60 * 1_000
DEFAULT_RETENTION_HOURS = 24
MAX_RETENTION_HOURS = 7 * 24
DEFAULT_START_HANDSHAKE_MS = 3_000
MAX_START_HANDSHAKE_MS = 10_000
MIN_START_HANDSHAKE_MS = 100

DESKTOP_TASK_MARKER = "_desktop_task"
DESKTOP_TASK_ALIAS_OPTION = "_desktop_task_alias"
DESKTOP_TASK_RECEIPT_OPTION = "_desktop_task_receipt_id"
DESKTOP_TASK_DIGEST_OPTION = "_desktop_task_request_digest"
DESKTOP_TASK_TIMEOUT_OPTION = "_desktop_task_timeout_ms"
DESKTOP_TASK_CODEX_BIN_OPTION = "_desktop_task_codex_bin"
DESKTOP_TASK_OUTPUT_FORMAT_OPTION = "_desktop_task_output_format"
DESKTOP_TASK_PERMISSION_REQUESTED_OPTION = "_desktop_task_permission_mode_requested"
DESKTOP_TASK_PERMISSION_EFFECTIVE_OPTION = "_desktop_task_permission_mode_effective"
DESKTOP_TASK_HANDOFF_MODE_OPTION = "_desktop_task_handoff_mode"
DESKTOP_TASK_HANDOFF_SOCKET_OPTION = "_desktop_task_handoff_socket"
DESKTOP_TASK_HANDOFF_STATE_OPTION = "_desktop_task_handoff_state"
DESKTOP_TASK_SCHEDULED_OPTION = "_desktop_task_scheduled"

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.: -]{0,79}$")
_RECEIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,159}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_CODEX_BIN_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._/+:\-]{0,255}|/[A-Za-z0-9._/+:\-]{1,255})$")
_SESSION_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![A-Za-z0-9])"
)
_REASONING = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})
_OUTPUT_FORMATS = frozenset({"structured", "markdown"})
_HANDOFF_MODES = frozenset({"manual", "app_server"})


class DesktopTaskError(ValueError):
    """Validation/configuration error safe to report to the MCP caller."""


@dataclasses.dataclass(frozen=True)
class DesktopTaskTarget:
    alias: str
    thread_id: str
    cwd: str = ""
    model: str = ""
    reasoning_effort: str = ""
    sandbox: str = ""
    profile: str = ""
    skip_git_repo_check: bool = False
    output_format: str = "structured"
    max_prompt_length: int = DEFAULT_PROMPT_LENGTH
    handoff_mode: str = "manual"
    app_server_socket: str = ""
    allowed_permission_modes: tuple[str, ...] = ()
    default_permission_mode: str = ""


@dataclasses.dataclass(frozen=True)
class DesktopTaskOptions:
    cwd: str = ""
    model: str = ""
    reasoning_effort: str = ""
    sandbox: str = ""
    profile: str = ""
    skip_git_repo_check: bool = False
    output_format: str = "structured"
    handoff_mode: str = "manual"
    app_server_socket: str = ""
    permission_mode_requested: str = ""
    permission_mode_effective: str = ""


def _text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise DesktopTaskError(f"{field} must be text")
    if required and not value:
        raise DesktopTaskError(f"{field} is required")
    if len(value) > maximum:
        raise DesktopTaskError(f"{field} is too long")
    return value


def _alias(value: Any) -> str:
    result = " ".join(_text(value, field="target", maximum=MAX_ALIAS_LENGTH).split())
    if not _ALIAS_RE.fullmatch(result) or _THREAD_ID_RE.fullmatch(result):
        raise DesktopTaskError("target must be an allowlisted logical alias")
    return result


def _receipt(value: Any) -> str:
    result = _text(value, field="receipt_id", maximum=MAX_RECEIPT_LENGTH)
    if not _RECEIPT_RE.fullmatch(result):
        raise DesktopTaskError("receipt_id contains unsupported characters")
    return result


def _thread_id(value: Any) -> str:
    result = _text(value, field="thread id", maximum=160)
    if not _THREAD_ID_RE.fullmatch(result):
        raise DesktopTaskError("allowlist contains an invalid thread id")
    return result


def _model(value: Any, field: str = "model") -> str:
    result = _text(value, field=field, maximum=160, required=False).strip()
    if result and not _MODEL_RE.fullmatch(result):
        raise DesktopTaskError(f"{field} contains unsupported characters")
    return result


def _reasoning(value: Any) -> str:
    result = _text(value, field="reasoning_effort", maximum=16, required=False).strip().lower()
    if result and result not in _REASONING:
        raise DesktopTaskError("reasoning_effort is unsupported")
    return result


def _sandbox(value: Any) -> str:
    result = _text(value, field="sandbox", maximum=32, required=False).strip().lower()
    if result and result not in _SANDBOXES:
        raise DesktopTaskError("sandbox is unsupported")
    return result


def _permission_mode(value: Any, *, field: str = "permission_mode") -> str:
    """Validate one Codex sandbox mode used for a Desktop turn."""
    result = _text(value, field=field, maximum=32, required=False).strip().lower()
    if result and result not in _SANDBOXES:
        raise DesktopTaskError(f"{field} is unsupported")
    return result


def _permission_allowlist(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise DesktopTaskError("allowed_permission_modes must be a non-empty list")
    if len(value) > len(_SANDBOXES):
        raise DesktopTaskError("allowed_permission_modes contains too many values")
    modes = tuple(_permission_mode(item, field="allowed_permission_modes item") for item in value)
    if any(not mode for mode in modes):
        raise DesktopTaskError("allowed_permission_modes cannot contain an empty value")
    if len(set(modes)) != len(modes):
        raise DesktopTaskError("allowed_permission_modes contains a duplicate")
    return modes


def _requested_permission_mode(value: Any) -> str:
    if value is None:
        return ""
    return _permission_mode(value)


def _target_permission_defaults(value: Mapping[str, Any], sandbox: str) -> tuple[tuple[str, ...], str]:
    """Parse the private per-alias permission allowlist and default.

    Legacy targets without the new fields retain their existing sandbox as a
    one-value default. New targets must declare both the allowlist and its
    default, which prevents a private operator from accidentally making a
    newly permitted mode the implicit mode.
    """
    raw_allowed = value.get("allowed_permission_modes")
    raw_default = value.get("default_permission_mode")
    if raw_allowed is None:
        default = _permission_mode(
            sandbox if raw_default is None else raw_default,
            field="default_permission_mode",
        )
        return ((default,) if default else ()), default
    if raw_default is None:
        raise DesktopTaskError(
            "default_permission_mode is required when allowed_permission_modes is configured"
        )
    allowed = _permission_allowlist(raw_allowed)
    default = _permission_mode(raw_default, field="default_permission_mode")
    if not default or default not in allowed:
        raise DesktopTaskError("default_permission_mode must be in allowed_permission_modes")
    return allowed, default


def _effective_permission_mode(target: DesktopTaskTarget, requested: str) -> str:
    allowed = target.allowed_permission_modes
    default = target.default_permission_mode or target.sandbox
    if requested:
        if not allowed or requested not in allowed:
            raise DesktopTaskError("permission_mode is not allowed for this target")
        return requested
    if default and allowed and default not in allowed:
        raise DesktopTaskError("target permission configuration is invalid")
    return default


def _profile(value: Any) -> str:
    result = _text(value, field="profile", maximum=120, required=False).strip()
    if result and not _PROFILE_RE.fullmatch(result):
        raise DesktopTaskError("profile contains unsupported characters")
    return result


def _output_format(value: Any) -> str:
    result = _text(value, field="output_format", maximum=16, required=False).strip().lower()
    if not result:
        return "structured"
    if result not in _OUTPUT_FORMATS:
        raise DesktopTaskError("output_format is unsupported")
    return result


def _handoff_mode(value: Any) -> str:
    result = _text(value, field="handoff_mode", maximum=16, required=False).strip().lower()
    if not result:
        return "manual"
    if result not in _HANDOFF_MODES:
        raise DesktopTaskError("handoff_mode is unsupported")
    return result


def _app_server_socket(value: Any, *, required: bool = False) -> str:
    result = _text(value, field="app_server_socket", maximum=1_024, required=required).strip()
    if not result:
        return ""
    path = Path(result).expanduser()
    if not path.is_absolute():
        raise DesktopTaskError("app_server_socket must be an absolute path")
    if "\x00" in str(path):
        raise DesktopTaskError("app_server_socket contains unsupported characters")
    return str(path)


def _prompt_length(value: Any) -> int:
    return _bounded_int(
        value,
        field="max_prompt_length",
        default=DEFAULT_PROMPT_LENGTH,
        minimum=MIN_PROMPT_LENGTH,
        maximum=MAX_PROMPT_LENGTH_CAP,
    )


def _cwd(value: Any) -> str:
    result = _text(value, field="cwd", maximum=1_024, required=False).strip()
    if not result:
        return ""
    path = Path(result).expanduser()
    if not path.is_absolute():
        raise DesktopTaskError("cwd must be an absolute path")
    return str(path)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DesktopTaskError(f"{field} must be a boolean")
    return value


def _codex_bin(value: Any) -> str:
    result = _text(value, field="codex_bin", maximum=256, required=False).strip()
    if not result:
        return "codex"
    if not _CODEX_BIN_RE.fullmatch(result) or ".." in Path(result).parts:
        raise DesktopTaskError("codex_bin contains unsupported characters")
    return result


def _private_path(value: Any, *, field: str) -> Path:
    """Resolve a private configuration path without accepting cwd-relative data."""
    text = _text(value, field=field, maximum=1_024).strip()
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise DesktopTaskError(f"{field} must be an absolute path")
    return path


def _validated_targets_path(value: Any) -> Path:
    """Validate the private targets file before catalog or runtime use."""
    if isinstance(value, Path):
        value = str(value)
    path = _private_path(value, field="desktop_tasks.targets_file")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise DesktopTaskError("desktop task targets file must exist") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise DesktopTaskError("desktop task targets file must be a regular file")
    if metadata.st_mode & 0o077:
        raise DesktopTaskError("desktop task targets file must be private (mode 0600 or stricter)")
    return path


def _target(alias: str, value: Any) -> DesktopTaskTarget:
    if isinstance(value, str):
        return DesktopTaskTarget(alias=alias, thread_id=_thread_id(value))
    if not isinstance(value, Mapping):
        raise DesktopTaskError("allowlist targets must be thread ids or objects")
    sandbox = _sandbox(value.get("sandbox", ""))
    allowed_permission_modes, default_permission_mode = _target_permission_defaults(value, sandbox)
    target = DesktopTaskTarget(
        alias=alias,
        thread_id=_thread_id(value.get("thread_id", value.get("session_id"))),
        cwd=_cwd(value.get("cwd", "")),
        model=_model(value.get("model", "")),
        reasoning_effort=_reasoning(value.get("reasoning_effort", "")),
        sandbox=sandbox,
        profile=_profile(value.get("profile", "")),
        skip_git_repo_check=_bool(value.get("skip_git_repo_check", False), "skip_git_repo_check"),
        output_format=_output_format(value.get("output_format", "structured")),
        max_prompt_length=_prompt_length(value.get("max_prompt_length")),
        handoff_mode=_handoff_mode(value.get("handoff_mode", "manual")),
        app_server_socket=_app_server_socket(value.get("app_server_socket", "")),
        allowed_permission_modes=allowed_permission_modes,
        default_permission_mode=default_permission_mode,
    )
    if target.handoff_mode == "app_server" and not target.app_server_socket:
        raise DesktopTaskError("app_server_socket is required for app_server handoff_mode")
    if target.handoff_mode == "manual" and target.app_server_socket:
        # Retain strict private configuration: a socket should never be
        # silently ignored because an operator mistyped the handoff mode.
        raise DesktopTaskError("app_server_socket requires app_server handoff_mode")
    return target


def load_desktop_targets(path: Path) -> dict[str, DesktopTaskTarget]:
    path = _validated_targets_path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except DesktopTaskError:
        raise
    except (OSError, ValueError) as exc:
        raise DesktopTaskError("unable to read desktop task targets file") from exc
    raw_targets = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise DesktopTaskError("desktop task targets file must contain targets")
    targets: dict[str, DesktopTaskTarget] = {}
    thread_ids: set[str] = set()
    for raw_alias, raw_target in raw_targets.items():
        alias = _alias(raw_alias)
        if alias in targets:
            raise DesktopTaskError("desktop task targets contain a duplicate alias")
        target = _target(alias, raw_target)
        if target.thread_id in thread_ids:
            raise DesktopTaskError("desktop task targets contain a duplicate thread id")
        thread_ids.add(target.thread_id)
        targets[alias] = target
    return targets


def desktop_tasks_enabled(config: Mapping[str, Any]) -> bool:
    settings = config.get("desktop_tasks")
    if not isinstance(settings, Mapping) or settings.get("enabled") is not True:
        return False
    try:
        _bounded_int(
            settings.get("timeout_ms"),
            field="desktop_tasks.timeout_ms",
            default=DEFAULT_TIMEOUT_MS,
            minimum=1_000,
            maximum=MAX_TIMEOUT_MS,
        )
        _bounded_int(
            settings.get("retention_hours"),
            field="desktop_tasks.retention_hours",
            default=DEFAULT_RETENTION_HOURS,
            minimum=1,
            maximum=MAX_RETENTION_HOURS,
        )
        _bounded_int(
            settings.get("startup_handshake_ms"),
            field="desktop_tasks.startup_handshake_ms",
            default=DEFAULT_START_HANDSHAKE_MS,
            minimum=MIN_START_HANDSHAKE_MS,
            maximum=MAX_START_HANDSHAKE_MS,
        )
        _codex_bin(settings.get("codex_bin", "codex"))
        load_desktop_targets(_validated_targets_path(settings.get("targets_file")))
    except DesktopTaskError:
        return False
    return True


def _bounded_int(value: Any, *, field: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise DesktopTaskError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DesktopTaskError(f"{field} must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def build_desktop_resume_command(
    codex_bin: str,
    target: DesktopTaskTarget,
    options: DesktopTaskOptions,
) -> list[str]:
    """Build the private CLI argv used by the durable executor."""
    command = [codex_bin, "exec"]
    if options.sandbox:
        command.extend(["--sandbox", options.sandbox])
    if options.cwd:
        command.extend(["--cd", options.cwd])
    if options.profile:
        command.extend(["--profile", options.profile])
    if options.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    # Desktop markdown mode intentionally leaves the final agent message
    # unconstrained so Codex can render normal Markdown in its own transcript.
    # The executor still requests JSON lifecycle events in both modes.
    command.append("--json")
    if options.model:
        command.extend(["--model", options.model])
    if options.reasoning_effort:
        command.extend(["-c", build_reasoning_config_override(options.reasoning_effort)])
    command.extend(["resume", target.thread_id, "-"])
    return command


def _request_digest(
    alias: str,
    prompt: str,
    options: DesktopTaskOptions,
    timeout_ms: int,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "target": alias,
                "prompt": prompt,
                "options": dataclasses.asdict(options),
                "timeout_ms": timeout_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _state_for_job(job: JobInfo) -> str:
    if job.state == JobState.PENDING:
        return "queued"
    if job.state == JobState.RUNNING:
        return "running"
    return "completed" if job.state == JobState.COMPLETED else "failed"


def _error_code(job: JobInfo) -> str:
    result = job.result if isinstance(job.result, dict) else {}
    diagnostic = result.get("failure_diagnostic") if isinstance(result.get("failure_diagnostic"), dict) else {}
    category = str(diagnostic.get("category") or "").strip()
    if category:
        return category
    message = str(job.error or "").lower()
    if "active writer" in message or "active_writer" in message:
        return "active_writer"
    if "archived" in message:
        return "archived_thread"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if job.state == JobState.CANCELLED:
        return "cancelled"
    return "desktop_task_failed"


def _private_answer(
    value: Any,
    target: DesktopTaskTarget,
    private_values: tuple[str, ...] = (),
) -> tuple[str, bool]:
    if isinstance(value, dict):
        selected = None
        for key in ("answer", "detailed_report", "summary", "message"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                selected = candidate
                break
        # Do not serialize an arbitrary result object: it may contain private
        # session/path fields that are not part of the public Desktop receipt.
        value = selected or ""
    answer = _sanitize_output_text(value, target, private_values)
    truncated = len(answer) > MAX_ANSWER_LENGTH
    return answer[:MAX_ANSWER_LENGTH], truncated


def _rewrite_target_cwd(value: str, target_cwd: str) -> str:
    """Turn paths below the private target workspace into useful relative paths."""
    if not target_cwd:
        return value
    root = str(Path(target_cwd).expanduser())
    if root == "/":
        return value
    root = root.rstrip("/")
    value = value.replace(root + "/", "")
    return re.sub(
        rf"(?<![A-Za-z0-9_.-]){re.escape(root)}(?![A-Za-z0-9_.-])",
        ".",
        value,
    )


def _sanitize_output_text(
    value: Any,
    target: DesktopTaskTarget,
    private_values: tuple[str, ...] = (),
    *,
    repo_relative: bool = True,
) -> str:
    """Sanitize one report string without changing its Markdown structure."""
    text = str(value or "")
    if repo_relative:
        text = _rewrite_target_cwd(text, target.cwd)
    for private_value in (
        target.thread_id,
        target.cwd,
        target.model,
        target.profile,
        *private_values,
    ):
        if private_value and len(private_value) > 2:
            text = text.replace(private_value, "[private]")
    text = redact_text(redact_local_paths(text))
    return _SESSION_ID_RE.sub("[private-session]", text)


_STRUCTURED_REPORT_FIELDS = (
    ("summary", "Summary"),
    ("detailed_report", "Detailed report"),
    ("evidence", "Evidence"),
    ("files_changed", "Files changed"),
    ("commands_run", "Commands run"),
    ("tests_run", "Tests run"),
    ("notes", "Notes"),
    ("risks", "Risks"),
    ("open_questions", "Open questions"),
    ("next_steps", "Next steps"),
)


def _render_structured_report(value: Any) -> str:
    """Render every user-facing structured result field into one report."""
    if not isinstance(value, Mapping):
        return str(value or "")
    sections: list[str] = []
    for key, heading in _STRUCTURED_REPORT_FIELDS:
        raw = value.get(key, "" if key in {"summary", "detailed_report", "notes"} else [])
        if isinstance(raw, list):
            body = "\n".join(f"- {item}" for item in raw) or "_None_"
        elif raw is None:
            body = ""
        else:
            body = str(raw)
        sections.append(f"## {heading}\n{body}")
    return "\n\n".join(sections)


def _report_source(value: Any, output_format: str) -> str:
    if output_format == "markdown":
        if isinstance(value, Mapping):
            for key in ("answer", "detailed_report", "summary", "message"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate
            return _render_structured_report(value)
        return str(value or "")
    return _render_structured_report(value)


def _bounded_report(report: str) -> tuple[str, int, bool]:
    """Return durable text, its stored length, and whether the hard cap hit."""
    safe = str(report or "")
    capped = len(safe) > MAX_REPORT_LENGTH
    if capped:
        safe = safe[:MAX_REPORT_LENGTH]
    return safe, len(safe), capped


def _public_error(code: str) -> str:
    """Return a bounded, path-free error for the public receipt surface."""
    messages = {
        "active_writer": (
            "Desktop still owns the task writer. Recover ownership in Desktop, "
            "then retry with a new receipt_id."
        ),
        "archived_thread": (
            "Desktop still indexes the task as archived. Recover archive state in Desktop, "
            "then retry with a new receipt_id."
        ),
        "timeout": "Codex did not complete before the configured timeout.",
        "cancelled": "PatchBay stopped the Desktop task before completion.",
        "codex_auth_refresh_failed": "Codex authentication failed before the Desktop task could run.",
        "codex_model_unavailable": "Codex rejected the selected model before the Desktop task could run.",
        "codex_workspace_trust_failed": "Codex rejected the Desktop task workspace trust configuration.",
        "desktop_task_not_found": (
            "The configured Desktop task was not found. Update the private alias registration to the current "
            "Desktop task, restart PatchBay, then retry with a new receipt_id."
        ),
        "desktop_handoff_unavailable": (
            "Automatic Desktop handoff is unavailable. Ensure the configured private Codex app-server is running, "
            "then retry with a new receipt_id."
        ),
        "desktop_handoff_failed": (
            "Automatic Desktop handoff failed. Inspect the local Codex app-server, then retry with a new receipt_id."
        ),
        "desktop_handoff_incomplete": (
            "The previous automatic Desktop handoff did not finish. Recover the task in Desktop or restart the local "
            "bridge, then retry with a new receipt_id."
        ),
        "codex_usage_limit": "Codex could not run the Desktop task because its current usage quota is exhausted.",
    }
    return messages.get(
        code,
        "The Desktop task did not complete; inspect the task in Desktop and local PatchBay diagnostics before retrying.",
    )


class DesktopTaskClient:
    """Durable alias-only facade over PatchBay's local Codex executor."""

    def __init__(
        self,
        config: Mapping[str, Any],
        job_manager: Any,
        job_executor: Any,
        targets: Mapping[str, DesktopTaskTarget],
        *,
        codex_bin: str = "codex",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        retention_hours: int = DEFAULT_RETENTION_HOURS,
        startup_handshake_ms: int = DEFAULT_START_HANDSHAKE_MS,
        handoff_runner: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ):
        self.config = config
        self.job_manager = job_manager
        self.job_executor = job_executor
        self.targets = dict(targets)
        thread_ids: set[str] = set()
        for target in self.targets.values():
            if target.thread_id in thread_ids:
                raise DesktopTaskError("desktop task targets contain a duplicate thread id")
            thread_ids.add(target.thread_id)
        self.codex_bin = _codex_bin(codex_bin)
        self.timeout_ms = _bounded_int(
            timeout_ms,
            field="desktop_tasks.timeout_ms",
            default=DEFAULT_TIMEOUT_MS,
            minimum=1_000,
            maximum=MAX_TIMEOUT_MS,
        )
        self.retention_hours = _bounded_int(
            retention_hours,
            field="desktop_tasks.retention_hours",
            default=DEFAULT_RETENTION_HOURS,
            minimum=1,
            maximum=MAX_RETENTION_HOURS,
        )
        self.startup_handshake_ms = _bounded_int(
            startup_handshake_ms,
            field="desktop_tasks.startup_handshake_ms",
            default=DEFAULT_START_HANDSHAKE_MS,
            minimum=MIN_START_HANDSHAKE_MS,
            maximum=MAX_START_HANDSHAKE_MS,
        )
        self._handoff_runner = handoff_runner or prepare_desktop_task
        self._start_lock = asyncio.Lock()
        configured_repo = (config.get("repositories") or {}).get("default")
        self._private_output_values = tuple(
            value
            for value in (
                str(configured_repo or ""),
                self.codex_bin if "/" in self.codex_bin else "",
            )
            if value
        )
        self._lock = threading.RLock()

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        job_manager: Any,
        job_executor: Any,
    ) -> Optional["DesktopTaskClient"]:
        settings = config.get("desktop_tasks")
        if not isinstance(settings, Mapping) or settings.get("enabled") is not True:
            return None
        timeout_ms = _bounded_int(
            settings.get("timeout_ms"),
            field="desktop_tasks.timeout_ms",
            default=DEFAULT_TIMEOUT_MS,
            minimum=1_000,
            maximum=MAX_TIMEOUT_MS,
        )
        retention_hours = _bounded_int(
            settings.get("retention_hours"),
            field="desktop_tasks.retention_hours",
            default=DEFAULT_RETENTION_HOURS,
            minimum=1,
            maximum=MAX_RETENTION_HOURS,
        )
        startup_handshake_ms = _bounded_int(
            settings.get("startup_handshake_ms"),
            field="desktop_tasks.startup_handshake_ms",
            default=DEFAULT_START_HANDSHAKE_MS,
            minimum=MIN_START_HANDSHAKE_MS,
            maximum=MAX_START_HANDSHAKE_MS,
        )
        client = cls(
            config,
            job_manager,
            job_executor,
            load_desktop_targets(_validated_targets_path(settings.get("targets_file"))),
            codex_bin=_codex_bin(settings.get("codex_bin", "codex")),
            timeout_ms=timeout_ms,
            retention_hours=retention_hours,
            startup_handshake_ms=startup_handshake_ms,
        )
        client.prune_expired()
        return client

    def _jobs(self) -> list[JobInfo]:
        lock = getattr(self.job_manager, "_state_lock", None)
        if lock is None:
            return [
                job
                for job in list(getattr(self.job_manager, "jobs", {}).values())
                if bool((job.options or {}).get(DESKTOP_TASK_MARKER))
            ]
        with lock:
            return [
                job
                for job in list(getattr(self.job_manager, "jobs", {}).values())
                if bool((job.options or {}).get(DESKTOP_TASK_MARKER))
            ]

    def _job_for_receipt(self, receipt_id: str) -> Optional[JobInfo]:
        matches = [
            job
            for job in self._jobs()
            if str((job.options or {}).get(DESKTOP_TASK_RECEIPT_OPTION) or "") == receipt_id
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: float(item.started_at or item.completed_at or 0))[-1]

    def _persist_report(self, job: JobInfo, payload: Mapping[str, Any]) -> None:
        """Persist one bounded sanitized report without changing job semantics."""
        lock = getattr(self.job_manager, "_state_lock", None)
        if lock is None:
            return
        with lock:
            current = self.job_manager.get_job(job.job_id)
            if current is None:
                return
            merged = dict(current.result or {})
            changed = False
            for key, value in payload.items():
                if merged.get(key) != value:
                    merged[key] = value
                    changed = True
            if not changed:
                return
            current.result = merged
            persist = getattr(self.job_manager, "_persist_job", None)
            if callable(persist):
                persist(current)
            job.result = merged

    def _report_for_job(self, job: JobInfo, target: DesktopTaskTarget) -> dict[str, Any]:
        result = job.result if isinstance(job.result, dict) else {}
        stored = result.get("desktop_report")
        stored_format = result.get("desktop_report_format")
        if isinstance(stored, str) and stored_format in _OUTPUT_FORMATS:
            return {
                "text": stored,
                "format": stored_format,
                "total_length": len(stored),
                "capped": bool(result.get("desktop_report_capped", False)),
            }
        report_format = target.output_format
        source = _report_source(result, report_format)
        sanitized = _sanitize_output_text(source, target, self._private_output_values)
        text, total_length, capped = _bounded_report(sanitized)
        payload = {
            "desktop_report": text,
            "desktop_report_format": report_format,
            "desktop_report_capped": capped,
        }
        self._persist_report(job, payload)
        return {
            "text": text,
            "format": report_format,
            "total_length": total_length,
            "capped": capped,
        }

    @staticmethod
    def _report_argument(value: Any, *, field: str, default: int, maximum: int, minimum: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise DesktopTaskError(f"{field} must be an integer")
        if value < minimum or value > maximum:
            raise DesktopTaskError(f"{field} must be between {minimum} and {maximum}")
        return value

    def _public(
        self,
        job: JobInfo,
        target: DesktopTaskTarget,
        *,
        report_offset: int = 0,
        report_limit: int = DEFAULT_REPORT_CHUNK_LENGTH,
    ) -> dict[str, Any]:
        state = _state_for_job(job)
        result: dict[str, Any] = {
            "ok": state == "completed",
            "target": target.alias,
            "receipt_id": str((job.options or {}).get(DESKTOP_TASK_RECEIPT_OPTION) or ""),
            "state": state,
            "permission_mode_requested": (
                str((job.options or {}).get(DESKTOP_TASK_PERMISSION_REQUESTED_OPTION) or "") or None
            ),
            "permission_mode_effective": (
                str(
                    (job.options or {}).get(DESKTOP_TASK_PERMISSION_EFFECTIVE_OPTION)
                    or (job.options or {}).get("sandbox")
                    or ""
                )
                or None
            ),
        }
        if job.event_count:
            result["event_count"] = int(job.event_count)
        if terminal_cleanup_pending(job.wrapper_cleanup_outcome):
            result["cleanup_pending"] = True
            if terminal_cleanup_recovery_required(job.wrapper_cleanup_outcome):
                result["cleanup_warning_code"] = str(job.wrapper_cleanup_outcome)
                result["cleanup_warning"] = (
                    "PatchBay retained a fail-closed cleanup barrier; recover local process ownership "
                    "before starting another Desktop task turn."
                )
        if state == "completed" and isinstance(job.result, dict):
            answer, truncated = _private_answer(job.result, target, self._private_output_values)
            if answer:
                result["answer"] = answer
                result["answer_truncated"] = truncated
            report = self._report_for_job(job, target)
            total_length = int(report["total_length"])
            if report_offset > total_length:
                raise DesktopTaskError("report_offset is beyond the durable report")
            end = min(total_length, report_offset + report_limit)
            result.update(
                {
                    "report_format": report["format"],
                    "report": report["text"][report_offset:end],
                    "report_total_length": total_length,
                    "report_offset": report_offset,
                    "report_next_offset": end if end < total_length else None,
                    "report_complete": end >= total_length,
                    "report_capped": bool(report["capped"]),
                }
            )
        if state == "failed":
            result["error_code"] = _error_code(job)
            result["error"] = _public_error(result["error_code"])
        if state == "completed" and job.exit_code not in (None, 0):
            result["warning_code"] = "wrapper_exit_after_answer"
            result["warning"] = "Codex persisted a final answer before its wrapper exited nonzero; the answer was retained."
        return result

    def _options(
        self,
        target: DesktopTaskTarget,
        alias: str,
        receipt_id: str,
        digest: str,
        timeout_ms: int,
        *,
        permission_mode_requested: str = "",
        permission_mode_effective: str = "",
    ) -> dict[str, Any]:
        overrides = []
        if target.reasoning_effort:
            overrides.append(build_reasoning_config_override(target.reasoning_effort))
        effective = permission_mode_effective or target.default_permission_mode or target.sandbox
        return {
            "resume_session_id": target.thread_id,
            "_codex_cwd": target.cwd,
            "model": target.model,
            "sandbox": effective,
            "profile": target.profile,
            "structured_output": True,
            "json_events": True,
            "skip_git_repo_check": target.skip_git_repo_check,
            DESKTOP_TASK_OUTPUT_FORMAT_OPTION: target.output_format,
            DESKTOP_TASK_PERMISSION_REQUESTED_OPTION: permission_mode_requested,
            DESKTOP_TASK_PERMISSION_EFFECTIVE_OPTION: effective,
            DESKTOP_TASK_HANDOFF_MODE_OPTION: target.handoff_mode,
            DESKTOP_TASK_HANDOFF_SOCKET_OPTION: target.app_server_socket,
            DESKTOP_TASK_HANDOFF_STATE_OPTION: (
                "not_started" if target.handoff_mode == "app_server" else "manual"
            ),
            DESKTOP_TASK_SCHEDULED_OPTION: False,
            "_desktop_task_max_prompt_length": target.max_prompt_length,
            "config_overrides": overrides,
            DESKTOP_TASK_MARKER: True,
            DESKTOP_TASK_ALIAS_OPTION: alias,
            DESKTOP_TASK_RECEIPT_OPTION: receipt_id,
            DESKTOP_TASK_DIGEST_OPTION: digest,
            DESKTOP_TASK_TIMEOUT_OPTION: timeout_ms,
            DESKTOP_TASK_CODEX_BIN_OPTION: self.codex_bin,
        }

    def _create_job(
        self,
        alias: str,
        receipt_id: str,
        prompt: str,
        target: DesktopTaskTarget,
        timeout_ms: int,
        *,
        permission_mode_requested: str = "",
        permission_mode_effective: str = "",
    ) -> JobInfo:
        effective = permission_mode_effective or target.default_permission_mode or target.sandbox
        options = DesktopTaskOptions(
            cwd=target.cwd,
            model=target.model,
            reasoning_effort=target.reasoning_effort,
            sandbox=effective,
            profile=target.profile,
            skip_git_repo_check=target.skip_git_repo_check,
            output_format=target.output_format,
            handoff_mode=target.handoff_mode,
            app_server_socket=target.app_server_socket,
            permission_mode_requested=permission_mode_requested,
            permission_mode_effective=effective,
        )
        digest = _request_digest(alias, prompt, options, timeout_ms)
        with self._lock:
            self.prune_expired()
            existing = self._job_for_receipt(receipt_id)
            if existing is not None:
                if str((existing.options or {}).get(DESKTOP_TASK_DIGEST_OPTION) or "") != digest:
                    raise DesktopTaskError("receipt_id was already used for another request")
                return existing
            for job in self._jobs():
                job_alias = str((job.options or {}).get(DESKTOP_TASK_ALIAS_OPTION) or "")
                job_thread_id = str((job.options or {}).get("resume_session_id") or "")
                active = job.state in {JobState.PENDING, JobState.RUNNING}
                active = active or terminal_cleanup_pending(job.wrapper_cleanup_outcome)
                if (job_alias == alias or job_thread_id == target.thread_id) and active:
                    reconcile = getattr(
                        self.job_executor,
                        "reconcile_stale_terminal_cleanup",
                        None,
                    )
                    if callable(reconcile) and reconcile(job.job_id):
                        refreshed = self.job_manager.get_job(job.job_id)
                        if refreshed is not None:
                            job = refreshed
                            active = job.state in {JobState.PENDING, JobState.RUNNING}
                            active = active or terminal_cleanup_pending(
                                job.wrapper_cleanup_outcome
                            )
                    if not active:
                        continue
                    raise DesktopTaskError("this Desktop task already has an active turn; wait for its receipt")
            repo_path = str((self.config.get("repositories") or {}).get("default") or "")
            if not repo_path:
                raise DesktopTaskError("PatchBay has no default workspace for the Desktop task job")
            try:
                job_id = self.job_manager.create_job(
                    "resume",
                    prompt,
                    repo_path,
                    self._options(
                        target,
                        alias,
                        receipt_id,
                        digest,
                        timeout_ms,
                        permission_mode_requested=permission_mode_requested,
                        permission_mode_effective=effective,
                    ),
                )
            except RuntimeError as exc:
                raise DesktopTaskError("PatchBay's local job capacity is full; wait and retry with a new receipt_id") from exc
            except ValueError as exc:
                raise DesktopTaskError("PatchBay could not accept the configured Desktop task workspace") from exc
            job = self.job_manager.get_job(job_id)
            if job is None:
                raise DesktopTaskError("PatchBay could not persist the Desktop task receipt")
            return job

    def _set_handoff_state(self, job_id: str, state: str) -> JobInfo:
        self.job_manager.mutate_job_options(
            job_id,
            lambda current: {
                **current,
                DESKTOP_TASK_HANDOFF_STATE_OPTION: state,
            },
        )
        return self.job_manager.get_job(job_id) or JobInfo(job_id=job_id, state=JobState.FAILED)

    def _fail_handoff(self, job: JobInfo, code: str) -> JobInfo:
        self.job_manager.update_job_state(
            job.job_id,
            JobState.FAILED,
            error=code,
            result={"failure_diagnostic": {"category": code}},
        )
        return self.job_manager.get_job(job.job_id) or job

    async def _prepare_handoff(self, job: JobInfo, target: DesktopTaskTarget) -> JobInfo:
        """Perform one durable, private app-server ownership handoff.

        ``preparing`` is written before the first remote mutation.  If the
        service dies after that point, a later retry with the same receipt is
        terminally blocked rather than guessing whether archive/unarchive
        completed.  A fresh receipt is required after operator recovery.
        """
        if target.handoff_mode != "app_server":
            return job
        state = str((job.options or {}).get(DESKTOP_TASK_HANDOFF_STATE_OPTION) or "not_started")
        if state == "released":
            return job
        if state in {"preparing", "archived"}:
            return self._fail_handoff(job, "desktop_handoff_incomplete")
        if state not in {"", "not_started"}:
            return self._fail_handoff(job, "desktop_handoff_incomplete")
        try:
            self._set_handoff_state(job.job_id, "preparing")
            await self._handoff_runner(target.app_server_socket, target.thread_id)
            return self._set_handoff_state(job.job_id, "released")
        except DesktopHandoffError as error:
            code = error.code if error.code in {
                "active_writer",
                "archived_thread",
                "desktop_task_not_found",
                "desktop_handoff_unavailable",
                "desktop_handoff_failed",
            } else "desktop_handoff_failed"
            return self._fail_handoff(job, code)
        except Exception:
            return self._fail_handoff(job, "desktop_handoff_failed")

    async def start(
        self,
        *,
        target: Any,
        receipt_id: Any,
        prompt: Any,
        timeout_ms: Any = None,
        permission_mode: Any = None,
    ) -> dict[str, Any]:
        async with self._start_lock:
            alias = _alias(target)
            receipt = _receipt(receipt_id)
            target_record = self.targets.get(alias)
            if target_record is None:
                raise DesktopTaskError("target alias is not allowlisted")
            requested_permission_mode = _requested_permission_mode(permission_mode)
            effective_permission_mode = _effective_permission_mode(
                target_record,
                requested_permission_mode,
            )
            message = _text(prompt, field="prompt", maximum=target_record.max_prompt_length)
            bounded_timeout = _bounded_int(
                timeout_ms,
                field="timeout_ms",
                default=self.timeout_ms,
                minimum=1_000,
                maximum=self.timeout_ms,
            )
            job = await asyncio.to_thread(
                self._create_job,
                alias,
                receipt,
                message,
                target_record,
                bounded_timeout,
                permission_mode_requested=requested_permission_mode,
                permission_mode_effective=effective_permission_mode,
            )
            # A durable terminal receipt is the idempotent result.  Never
            # repeat its handoff or schedule a second Desktop turn.
            if job.state not in {JobState.PENDING, JobState.RUNNING}:
                return self._public(job, target_record)
            if bool((job.options or {}).get(DESKTOP_TASK_SCHEDULED_OPTION)):
                return self._public(job, target_record)
            job = await self._prepare_handoff(job, target_record)
            if job.state not in {JobState.PENDING, JobState.RUNNING}:
                return self._public(job, target_record)
            self.job_manager.mutate_job_options(
                job.job_id,
                lambda current: {
                    **current,
                    DESKTOP_TASK_SCHEDULED_OPTION: True,
                },
            )
            try:
                scheduled = self.job_executor.schedule_job(job.job_id)
            except Exception:
                self.job_manager.update_job_state(job.job_id, JobState.FAILED, error="PatchBay could not schedule the Desktop task turn.")
                failed = self.job_manager.get_job(job.job_id) or job
                return self._public(failed, target_record)
            # Real JobExecutor.schedule_job returns an asyncio.Task. A small
            # bounded handshake lets immediate CLI failures (writer, archive,
            # missing task, auth, model) come back as a terminal receipt while a
            # genuinely running turn remains asynchronous. Test/dry-run
            # executors may return None and retain the immediate path.
            if isinstance(scheduled, asyncio.Future):
                job = await self._start_handshake(job, scheduled)
            return self._public(job, target_record)

    async def _start_handshake(self, job: JobInfo, scheduled: asyncio.Future) -> JobInfo:
        deadline = asyncio.get_running_loop().time() + self.startup_handshake_ms / 1_000
        while True:
            current = self.job_manager.get_job(job.job_id) or job
            if current.state not in {JobState.PENDING, JobState.RUNNING}:
                return current
            if scheduled.done():
                # Retrieve the exception so a crashed task is not left as an
                # unobserved future. A well-behaved executor has already
                # persisted a terminal state; only convert a task crash with
                # no durable transition into a receipt failure.
                try:
                    scheduled.exception()
                except BaseException:
                    pass
                current = self.job_manager.get_job(job.job_id) or current
                if current.state in {JobState.PENDING, JobState.RUNNING}:
                    self.job_manager.update_job_state(
                        job.job_id,
                        JobState.FAILED,
                        error="PatchBay's Desktop task executor stopped before a terminal result was persisted.",
                    )
                    current = self.job_manager.get_job(job.job_id) or current
                return current
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return current
            await asyncio.sleep(min(0.05, remaining))

    async def status(
        self,
        *,
        target: Any,
        receipt_id: Any,
        report_offset: Any = None,
        report_limit: Any = None,
    ) -> dict[str, Any]:
        alias = _alias(target)
        receipt = _receipt(receipt_id)
        target_record = self.targets.get(alias)
        if target_record is None:
            raise DesktopTaskError("target alias is not allowlisted")
        offset = self._report_argument(
            report_offset,
            field="report_offset",
            default=0,
            maximum=MAX_REPORT_LENGTH,
        )
        limit = self._report_argument(
            report_limit,
            field="report_limit",
            default=DEFAULT_REPORT_CHUNK_LENGTH,
            maximum=MAX_REPORT_CHUNK_LENGTH,
            minimum=1,
        )
        await asyncio.to_thread(self.prune_expired)
        job = self._job_for_receipt(receipt)
        if job is None or str((job.options or {}).get(DESKTOP_TASK_ALIAS_OPTION) or "") != alias:
            raise DesktopTaskError("receipt_id was not found for this target or has expired")
        return self._public(
            job,
            target_record,
            report_offset=offset,
            report_limit=limit,
        )

    def prune_expired(self) -> int:
        cutoff = time.time() - self.retention_hours * 3600
        removed = 0
        for job in self._jobs():
            if job.state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
                continue
            if terminal_cleanup_pending(job.wrapper_cleanup_outcome):
                continue
            if job.completed_at is None or float(job.completed_at) >= cutoff:
                continue
            try:
                if self.job_manager.cleanup_job(job.job_id):
                    removed += 1
            except Exception:
                continue
        return removed


__all__ = [
    "DESKTOP_TASK_MARKER",
    "DesktopTaskClient",
    "DesktopTaskError",
    "DesktopTaskOptions",
    "DesktopTaskTarget",
    "DESKTOP_TASK_PERMISSION_EFFECTIVE_OPTION",
    "DESKTOP_TASK_PERMISSION_REQUESTED_OPTION",
    "MAX_ANSWER_LENGTH",
    "MAX_REPORT_CHUNK_LENGTH",
    "MAX_REPORT_LENGTH",
    "MAX_ALIAS_LENGTH",
    "MAX_PROMPT_LENGTH_CAP",
    "DEFAULT_PROMPT_LENGTH",
    "DEFAULT_START_HANDSHAKE_MS",
    "DEFAULT_HANDOFF_TIMEOUT_MS",
    "MAX_START_HANDSHAKE_MS",
    "MAX_PROMPT_LENGTH",
    "MAX_RECEIPT_LENGTH",
    "build_desktop_resume_command",
    "desktop_tasks_enabled",
    "load_desktop_targets",
]
