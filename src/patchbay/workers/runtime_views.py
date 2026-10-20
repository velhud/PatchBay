"""Lossless worker source/diff views for :mod:`patchbay.workers.runtime`."""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Dict, Optional

from . import runtime_legacy as _legacy


class RuntimeViewFixes:
    def _candidate_diff_for_file(self, jobs: list[Any], rel_path: str) -> str:
        workspace = self._workspace_for_jobs(jobs)
        root = self._execution_path_for_workspace(workspace)
        snapshot = self._candidate_snapshot(jobs)
        result = subprocess.run(
            [
                "git",
                "diff",
                "--binary",
                "--full-index",
                "--no-renames",
                snapshot.baseline_revision,
                snapshot.tree_sha,
                "--",
                rel_path,
            ],
            cwd=root,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return ""
        safe = self._safe_public_text(
            result.stdout.decode("utf-8", errors="replace"), {root}
        )
        max_bytes = int(
            self.config.get("security", {}).get("max_diff_bytes", 200_000)
        )
        encoded = safe.encode("utf-8")
        if len(encoded) > max_bytes:
            safe = encoded[:max_bytes].decode("utf-8", errors="replace").rstrip()
            safe += "\n[worker diff truncated]"
        return safe

    def _diff_view(
        self,
        jobs: list[Any],
        *,
        file_path: Optional[str],
        request_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if not file_path:
            raise ValueError("file_path is required for view=diff")
        view = self._public_view(
            jobs, request_context=request_context, include_change_state=False
        )
        workspace = self._workspace_for_jobs(jobs)
        if not workspace["available"]:
            view.update(
                {
                    "file_path": file_path,
                    "diff": "",
                    "note": "Worker workspace is unavailable.",
                }
            )
            return view
        if workspace["mode"] == "read_only":
            view.update(
                {
                    "file_path": file_path,
                    "diff": "",
                    "note": "Read-only workers do not expose change diffs.",
                }
            )
            return view
        root = self._execution_path_for_workspace(workspace)
        rel_path = self._safe_relative_path(root, file_path)
        diff = self._candidate_diff_for_file(jobs, rel_path)
        view.update(
            {
                "file_path": rel_path,
                "diff": diff,
                "truncated": diff.endswith("[worker diff truncated]"),
            }
        )
        return view

    def _context_diff_for_jobs(
        self, jobs: list[Any], *, byte_budget: int
    ) -> tuple[str, bool]:
        if byte_budget <= 0:
            return "", True
        workspace = self._workspace_for_jobs(jobs)
        if workspace["mode"] == "read_only" or not workspace["available"]:
            return "", False
        pieces: list[str] = []
        used = 0
        truncated = False
        for rel_path in self._changed_files(jobs):
            if used >= byte_budget:
                truncated = True
                break
            diff = self._candidate_diff_for_file(jobs, rel_path)
            if not diff:
                continue
            chunk = diff.strip()
            encoded = chunk.encode("utf-8", errors="replace")
            remaining = byte_budget - used
            if len(encoded) > remaining:
                chunk = encoded[:remaining].decode(
                    "utf-8", errors="replace"
                ).rstrip()
                chunk += "\n[peer worker diff truncated]"
                truncated = True
            pieces.append(chunk)
            used += len(chunk.encode("utf-8", errors="replace"))
        return "\n\n".join(pieces), truncated

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
        if not file_path:
            raise ValueError("file_path is required for view=file")
        view = self._public_view(
            jobs, request_context=request_context, include_change_state=False
        )
        workspace = self._workspace_for_jobs(jobs)
        if not workspace["available"]:
            view.update(
                {
                    "file_path": file_path,
                    "text": "",
                    "exists": False,
                    "note": "Worker workspace is unavailable.",
                }
            )
            return view

        root = self._execution_path_for_workspace(workspace)
        rel_path = self._safe_relative_path(root, file_path)
        if self._blocked_changed_files([rel_path]):
            raise ValueError("file_path is blocked by server policy")
        target = Path(root) / rel_path
        view.update(
            {
                "view": "file",
                "source": "worker_workspace",
                "file_path": rel_path,
                "text": "",
                "exists": False,
                "truncated": False,
                "note": (
                    "This reads the worker workspace before integration. "
                    "codex_read_file reads the base checkout."
                ),
            }
        )
        if not target.is_file():
            view["note"] = "File is not present in the worker workspace."
            return view

        max_allowed = int(
            self.config.get("security", {}).get(
                "max_read_bytes", _legacy.DEFAULT_WORKER_FILE_READ_BYTES
            )
        )
        public_cap = int(
            self.config.get("workers", {}).get(
                "file_response_max_bytes",
                _legacy.DEFAULT_WORKER_FILE_RESPONSE_BYTES,
            )
        )
        max_read = max(1, min(int(max_bytes or public_cap), max_allowed, public_cap))
        size = target.stat().st_size
        with target.open("rb") as sample_handle:
            if b"\0" in sample_handle.read(4096):
                view.update(
                    {
                        "bytes": size,
                        "note": "Refusing to read binary file from worker workspace.",
                    }
                )
                return view

        start = max(1, int(start_line or 1))
        requested_end = int(end_line) if end_line is not None else None
        if requested_end is not None and requested_end < start:
            raise ValueError(
                f"end_line ({requested_end}) must be >= start_line ({start})"
            )

        selected: list[tuple[int, str]] = []
        total_lines = 0
        bytes_used = 0
        next_start_line: int | None = None
        oversized_line: int | None = None
        stopped = False
        try:
            with target.open(
                "r", encoding="utf-8", errors="strict", newline=""
            ) as handle:
                for line_number, line in enumerate(handle, start=1):
                    total_lines = line_number
                    if line_number < start:
                        continue
                    if requested_end is not None and line_number > requested_end:
                        continue
                    if stopped:
                        continue
                    clean = line[:-1] if line.endswith("\n") else line
                    encoded = clean.encode("utf-8", errors="replace")
                    separator = 1 if selected else 0
                    if bytes_used + separator + len(encoded) > max_read:
                        if not selected and len(encoded) <= max_allowed:
                            selected.append((line_number, clean))
                            bytes_used = len(encoded)
                            oversized_line = line_number
                            next_start_line = line_number + 1
                        else:
                            next_start_line = line_number
                        stopped = True
                        continue
                    selected.append((line_number, clean))
                    bytes_used += separator + len(encoded)
        except UnicodeDecodeError:
            view.update(
                {
                    "bytes": size,
                    "note": "Refusing to read non-UTF-8 file from worker workspace.",
                }
            )
            return view

        returned_end = selected[-1][0] if selected else start - 1
        width = len(str(max(returned_end, start)))
        numbered = "\n".join(
            f"{str(number).rjust(width)} | {_legacy.redact_sensitive_output(line)}"
            for number, line in selected
        )
        numbered = self._safe_public_text(
            numbered,
            self._private_paths_for_jobs(jobs) | {root},
            max_chars=max(max_read, len(numbered)) if oversized_line else max_read,
            truncation_label="worker file",
        )
        view.update(
            {
                "exists": True,
                "text": numbered,
                "start_line": start,
                "end_line": returned_end,
                "total_lines": total_lines,
                "bytes": size,
                "sha256": self._file_sha256(target),
                "truncated": bool(
                    start > 1
                    or returned_end < total_lines
                    or next_start_line is not None
                    or numbered.endswith("...[worker file truncated]")
                ),
                "max_bytes_applied": max_read,
            }
        )
        if next_start_line is not None and next_start_line <= total_lines:
            view["next_start_line"] = next_start_line
        if oversized_line is not None:
            view["line_exceeded_requested_max_bytes"] = oversized_line
            view["note"] += (
                " One complete source line exceeded max_bytes and was returned "
                "intact so pagination does not omit or renumber source."
            )
        return view
