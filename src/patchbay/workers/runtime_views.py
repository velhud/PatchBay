"""Lossless worker source/diff views with public response-contract fixes."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .runtime_views_legacy import RuntimeViewFixes as _RuntimeViewFixes


class RuntimeViewFixes(_RuntimeViewFixes):
    """Retain the critical runtime implementation and complete its cap notice."""

    def _file_view(
        self,
        jobs: list[Any],
        *,
        file_path: Optional[str],
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        max_bytes: Optional[int] = None,
        request_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        view = super()._file_view(
            jobs,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            max_bytes=max_bytes,
            request_context=request_context,
        )
        if max_bytes is None or not view.get("exists"):
            return view
        requested = int(max_bytes)
        applied = int(view.get("max_bytes_applied") or requested)
        if requested > applied:
            note = str(view.get("note") or "").rstrip()
            suffix = f"Requested max_bytes was capped to {applied} bytes by server policy."
            view["note"] = f"{note} {suffix}".strip()
        return view
