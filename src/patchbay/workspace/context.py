"""Compatibility-preserving workspace reader with lossless line pagination.

The original implementation is retained in :mod:`context_legacy`. This module
keeps the public API stable while refusing to silently clip or skip source
lines that do not fit in a requested response page.
"""
from __future__ import annotations

from typing import Any, Dict

from .context_legacy import *  # noqa: F401,F403 - preserve public module API
from . import context_legacy as _legacy


class WorkspaceContext(_legacy.WorkspaceContext):
    """Workspace context whose text pagination preserves source coordinates."""

    def read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        workspace = self.open_workspace(args.get("repo"))
        file_path = args.get("file_path")
        if not file_path:
            raise ValueError("file_path is required")
        target, rel = self.resolve_path(workspace, str(file_path))
        self._assert_text_file(target)

        requested_max = self._bounded_int(
            args.get("max_bytes"), self.max_read_bytes, 1, self.max_read_bytes
        )
        size = target.stat().st_size
        digest, total_lines = self._text_file_digest_and_line_count(target)
        start_line = max(1, int(args.get("start_line") or 1))
        end_line = min(total_lines, int(args.get("end_line") or total_lines))
        if end_line < start_line:
            raise ValueError(
                f"end_line ({end_line}) must be >= start_line ({start_line})"
            )

        width = len(str(max(end_line, start_line)))
        selected: list[tuple[int, str]] = []
        used_bytes = 0
        next_start_line: int | None = None
        oversized_line: int | None = None
        required_bytes: int | None = None

        from patchbay.text_pages import logical_lines
        for line_number, clean, source_bytes in logical_lines(
            target, self.max_read_bytes, total_lines=total_lines
        ):
            if line_number < start_line:
                continue
            if line_number > end_line:
                break
            if clean is None:
                next_start_line = line_number
                if not selected:
                    required_bytes = source_bytes + width + 3
                break
            clean = clean[:-1] if clean.endswith("\r") else clean
            rendered = (
                f"{str(line_number).rjust(width)} | {_legacy.redact_text(clean)}"
            )
            encoded = rendered.encode("utf-8")
            separator_bytes = 1 if selected else 0
            if used_bytes + separator_bytes + len(encoded) > requested_max:
                if not selected and len(encoded) <= self.max_read_bytes:
                    selected.append((line_number, rendered))
                    used_bytes = len(encoded)
                    oversized_line = line_number
                    next_start_line = line_number + 1
                else:
                    next_start_line = line_number
                    if not selected:
                        required_bytes = len(encoded)
                break
            selected.append((line_number, rendered))
            used_bytes += separator_bytes + len(encoded)

        returned_end_line = selected[-1][0] if selected else start_line - 1
        result: Dict[str, Any] = {
            "workspace_id": workspace.id,
            "path": rel,
            "text": "\n".join(rendered for _, rendered in selected),
            "start_line": start_line,
            "end_line": returned_end_line,
            "requested_end_line": end_line,
            "total_lines": total_lines,
            "bytes": size,
            "sha256": digest,
            "max_bytes_applied": requested_max,
            "truncated": bool(
                start_line > 1
                or returned_end_line < total_lines
                or next_start_line is not None
            ),
        }
        if next_start_line is not None and next_start_line <= total_lines:
            result["next_start_line"] = next_start_line
        if oversized_line is not None:
            result["line_exceeded_requested_max_bytes"] = oversized_line
            result["actual_page_bytes"] = used_bytes
            result["note"] = (
                "One complete source line exceeded max_bytes and was returned "
                "intact; source text was not clipped or skipped."
            )
        if required_bytes is not None:
            result["line_too_large"] = start_line
            result["required_bytes"] = required_bytes
            result["note"] = (
                "The next complete source line exceeds the configured maximum "
                "response size. No partial line was returned because this "
                "line-oriented API cannot resume its suffix."
            )
        return result
