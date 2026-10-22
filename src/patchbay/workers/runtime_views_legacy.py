"""Lossless worker source/diff views for :mod:`patchbay.workers.runtime`."""
from __future__ import annotations

from pathlib import Path
import subprocess
import hashlib
import json
import re
from .integration_state import CandidateSnapshotError, _run_git, create_snapshot_commit
from typing import Any, Dict, Optional

from . import runtime_legacy as _legacy


class RuntimeViewFixes:
    def _retain_review_target(self, jobs, snapshot):
        worker_id, _ = self._worker_identity(jobs)
        descriptor = {
            "format_version": 1, "worker_id": worker_id, "turn_job_id": jobs[-1].job_id,
            "baseline_revision": snapshot.baseline_revision, "tree_sha": snapshot.tree_sha,
            "changed_files": list(snapshot.changed_files),
            "contract_sha256": hashlib.sha256(jobs[-1].prompt.encode()).hexdigest(),
            "evidence_refs": (jobs[-1].result or {}).get("report_source", {}),
        }
        candidate_id = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
        ref = self._integration_ref_name(jobs).rsplit("/", 1)[0] + "/reviews/" + candidate_id
        current = self.job_manager.get_job(jobs[-1].job_id)
        targets = (current.options or {}).get('_worker_review_targets') or {}
        if candidate_id not in targets and len(targets) >= 128:
            raise CandidateSnapshotError('Retained review target budget exhausted; preserve existing targets', reason='review_target_budget_exhausted')
        record = {**descriptor, "candidate_id": candidate_id, "snapshot_commit": "0"*len(snapshot.tree_sha), "snapshot_ref": ref,
                  "retrieval": {"view": "file", "candidate_id": candidate_id},
                  "scope": "source_review_target", "semantic_verdict": "unreviewed"}
        if len(json.dumps(record).encode()) > 2*1024*1024:
            raise CandidateSnapshotError('Review target inventory exceeds retained metadata policy', reason='review_target_budget_exhausted')
        root = self._execution_path_for_workspace(self._workspace_for_jobs(jobs))
        old = _run_git(root, ["rev-parse", "--verify", ref], ok_returncodes=(0,128))
        if old.returncode == 0:
            commit = old.stdout.decode().strip()
            if _run_git(root, ["rev-parse", commit+"^{tree}"]).stdout.decode().strip() != snapshot.tree_sha:
                raise CandidateSnapshotError("Retained review target identity conflict")
        else:
            commit = create_snapshot_commit(root, tree_sha=snapshot.tree_sha,
                parent_revision=snapshot.baseline_revision, ref_name=ref, expected_old_revision="")
        record['snapshot_commit'] = commit
        def retain(options):
            targets = dict(options.get("_worker_review_targets") or {})
            if candidate_id not in targets and len(targets) >= 128:
                raise CandidateSnapshotError("Retained review target budget exhausted; preserve existing targets", reason="review_target_budget_exhausted")
            if len(json.dumps(record).encode()) > 2*1024*1024:
                raise CandidateSnapshotError("Review target inventory exceeds retained metadata policy", reason="review_target_budget_exhausted")
            targets[candidate_id] = record
            options["_worker_review_targets"] = targets
            return options
        self.job_manager.mutate_job_options(jobs[-1].job_id, retain)
        return record

    def _review_target(self, jobs, candidate_id):
        if not re.fullmatch(r"[0-9a-f]{64}", str(candidate_id)):
            raise ValueError("candidate_id must identify a retained review target")
        record = next(((job.options or {}).get("_worker_review_targets", {}).get(candidate_id)
                       for job in reversed(jobs)
                       if candidate_id in (job.options or {}).get("_worker_review_targets", {})), None)
        if not record:
            raise ValueError("No retained candidate for this worker and candidate_id")
        root = self._workspace_for_jobs(jobs)["base_repo_path"]
        tree = _run_git(root, ["rev-parse", record["snapshot_commit"]+"^{tree}"]).stdout.decode().strip()
        if tree != record["tree_sha"]:
            raise CandidateSnapshotError("Retained review source is unavailable or changed")
        return root, record

    def _retained_candidate_view(self, jobs, *, candidate_id, view="file", file_path=None,
                                 start_line=None, end_line=None, max_bytes=None, **kwargs):
        from .integration_state import tree_entries
        root, record = self._review_target(jobs, candidate_id)
        result = {"view": view, "source": "retained_candidate", "review_target": record,
                  "candidate_id": candidate_id, "mutable_workspace": False}
        if view == "changes":
            return {**result, "changed_files": record["changed_files"], "complete": True}
        if view not in {"file", "diff"} or not file_path:
            raise ValueError("candidate_id supports changes, file or diff; file_path is required for file/diff")
        rel = str(file_path)
        if Path(rel).is_absolute() or ".." in Path(rel).parts or "\0" in rel:
            raise ValueError("file_path must be repository relative")
        if self._blocked_changed_files([rel]):
            raise ValueError("file_path is blocked by server policy")
        result["file_path"] = rel
        if view == "diff":
            from .integration_state import CandidateSnapshot
            snap = CandidateSnapshot(record["baseline_revision"], record["tree_sha"], tuple(record["changed_files"]), b"")
            member = self._candidate_diff_member(jobs, rel, snapshot=snap, root=root)
            return {**result, **member}
        entry = tree_entries(root, record["tree_sha"]).get(rel)
        if not entry:
            return {**result, "exists": False, "text": "", "complete": True}
        if entry["mode"] not in {"100644", "100755"}:
            return {**result, "exists": True, "entry": entry, "complete": False,
                    "reason": "unsupported_source_kind", "text": ""}
        cap = int(self.config.get("security", {}).get("max_read_bytes", 1024*1024))
        size = int(_run_git(root, ["cat-file", "-s", entry["blob_oid"]]).stdout)
        if size > cap:
            return {**result, "exists": True, "entry": entry, "bytes": size,
                    "complete": False, "reason": "source_exceeds_read_policy", "text": ""}
        raw = _run_git(root, ["cat-file", "blob", entry["blob_oid"]]).stdout
        try:
            if b"\0" in raw:
                raise UnicodeError()
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            return {**result, "exists": True, "entry": entry, "complete": False,
                    "reason": "binary_or_non_utf8", "text": ""}
        start = max(1, int(start_line or 1)); end = int(end_line or len(lines))
        if end < start and lines:
            raise ValueError("end_line must be >= start_line")
        limit = max(1, min(int(max_bytes or 8192), cap, 65536))
        selected = []; used = 0; next_line = None
        for number in range(start, min(end, len(lines))+1):
            line = self._safe_public_text(lines[number-1], {root}, max_chars=cap)
            cost = len(line.encode()) + bool(selected)
            if used + cost > limit:
                next_line = number; break
            selected.append(line); used += cost
        return {**result, "exists": True, "entry": entry, "text": "\n".join(selected),
                "start_line": start, "end_line": start+len(selected)-1,
                "next_start_line": next_line, "truncated": next_line is not None,
                "complete": next_line is None and start == 1 and end >= len(lines),
                "representation": "sanitized_utf8_lines", "raw_blob_oid": entry["blob_oid"]}

    def _candidate_diff_member(self, jobs, rel_path, *, snapshot, root=None):
        root = root or self._execution_path_for_workspace(self._workspace_for_jobs(jobs))
        if self._blocked_changed_files([rel_path]):
            return {"file_path": rel_path, "diff": "", "complete": False,
                    "truncated": False, "error": "protected_source_path", "representation": "withheld"}
        try:
            result = _run_git(root, ["--literal-pathspecs", "diff", "--binary", "--full-index",
                "--no-renames", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none",
                snapshot.baseline_revision, snapshot.tree_sha, "--", rel_path])
            text = result.stdout.decode("utf-8", errors="replace")
            safe = self._safe_public_text(text, {root})
            max_bytes = int(self.config.get("security", {}).get("max_diff_bytes", 200_000))
            encoded = safe.encode("utf-8")
            truncated = len(encoded) > max_bytes
            if truncated:
                safe = encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n[worker diff truncated]"
            return {"file_path": rel_path, "diff": safe, "truncated": truncated,
                    "complete": not truncated and safe == text, "representation": "sanitized_git_binary_diff",
                    "representation_changed": safe != text, "error": None}
        except CandidateSnapshotError:
            return {"file_path": rel_path, "diff": "", "complete": False,
                    "truncated": False, "error": "git_observation_failed"}

    def _candidate_diff_for_file(self, jobs, rel_path):
        member = self._candidate_diff_member(jobs, rel_path, snapshot=self._candidate_snapshot(jobs))
        if member["error"]:
            raise CandidateSnapshotError("Candidate member diff failed")
        return member["diff"]

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
        snapshot = self._candidate_snapshot(jobs)
        member = self._candidate_diff_member(jobs, rel_path, snapshot=snapshot)
        view.update(member)
        view['candidate_tree_sha'] = snapshot.tree_sha
        return view

    def _context_diff_capture(self, jobs, *, byte_budget, snapshot=None):
        snapshot = snapshot or self._candidate_snapshot(jobs)
        pieces = []; members = []; used = 0; complete = True
        for rel_path in snapshot.changed_files:
            if used >= byte_budget:
                members.append({"file_path": rel_path, "complete": False, "error": "budget_omitted"})
                complete = False
                continue
            member = self._candidate_diff_member(jobs, rel_path, snapshot=snapshot)
            chunk = member["diff"].encode("utf-8")
            remaining = max(0, byte_budget-used-2)
            if len(chunk) > remaining:
                member.update(truncated=True, complete=False)
                chunk = chunk[:remaining]
            text = chunk.decode("utf-8", errors="ignore")
            if text:
                pieces.append(text); used += len(text.encode())+2
            complete = complete and member["complete"]
            members.append({key: value for key,value in member.items() if key != "diff"})
        return {"diff": "\n\n".join(pieces), "complete": complete,
                "truncated": not complete, "members": members, "tree_sha": snapshot.tree_sha,
                "capture_count": 1, "retained_bytes": used}

    def _context_diff_for_jobs(self, jobs, *, byte_budget):
        workspace = self._workspace_for_jobs(jobs)
        if workspace["mode"] == "read_only" or not workspace["available"]:
            return "", False
        result = self._context_diff_capture(jobs, byte_budget=byte_budget)
        return result["diff"], not result["complete"]

    def _worker_context_prompt(self, workers, *, detail, repo_path=None):
        names = self._normalize_context_workers(workers)
        detail = self._validate_context_detail(detail)
        if not names:
            return self._empty_context(detail=detail)
        sections = []; targets = []; members = []; used = 0; incomplete = False; public_names = []
        for source in names:
            jobs = self._resolve_worker(source, repo_path=repo_path)
            view = self._public_view(jobs, include_change_state=False)
            public_names.append(view["name"])
            lines = ["## Context from worker: "+view["name"], "State: "+view["state"],
                     "Latest report (advisory):", self._clip_text(view.get("report") or "No report yet.", _legacy.MAX_CONTEXT_REPORT_CHARS)]
            workspace = self._workspace_for_jobs(jobs)
            if detail != "report" and workspace["mode"] != "read_only" and workspace["available"]:
                root = self._execution_path_for_workspace(workspace)
                with self.repo_locks.hold_sync(root, operation="peer_source_capture"):
                    snapshot = self._candidate_snapshot(jobs)
                    target = self._retain_review_target(jobs, snapshot)
                    targets.append(target)
                    lines += ["Candidate source identity:", "Immutable review target:", json.dumps(target, sort_keys=True),
                              "Changed files:", *snapshot.changed_files[:80]]
                    if len(snapshot.changed_files) > 80:
                        incomplete = True
                    if detail in {"review", "diff"}:
                        result = self._context_diff_capture(jobs, byte_budget=max(0, _legacy.MAX_CONTEXT_DIFF_BYTES-used), snapshot=snapshot)
                        members.extend(result["members"]); incomplete |= not result["complete"]
                        lines += ["Bounded diff:", result["diff"]]
                    lines += ["Read retained source with codex_worker_inspect using this worker and candidate_id; snapshot_commit identifies the retained Git source.",
                              "Review note: review source only. Do not run tests or edit the implementation workspace. Request a separate disposable writable check workspace for executable verification. Reports remain advisory; the controller records a target-bound acceptance decision."]
            block = "\n".join(lines)
            remaining = max(0, _legacy.MAX_CONTEXT_DIFF_BYTES-used)
            raw = block.encode()
            if len(raw) > remaining:
                block = raw[:remaining].decode("utf-8", errors="ignore"); incomplete = True
            sections.append(block); used += len(block.encode())
        return {"prompt": "\n\n".join(sections), "sources": public_names, "detail": detail,
                "truncated": incomplete, "review_targets": targets, "members": members}

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
            from patchbay.text_pages import bounded_lines
            for line_number, clean, source_bytes in bounded_lines(target, max_allowed):
                total_lines = line_number
                if line_number < start:
                    continue
                if requested_end is not None and line_number > requested_end:
                    continue
                if stopped:
                    continue
                if clean is None:
                    next_start_line = line_number
                    stopped = True
                    continue
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
