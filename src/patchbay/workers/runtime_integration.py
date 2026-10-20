"""Git-native integration and revision fixes for :mod:`patchbay.workers.runtime`."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from . import runtime_legacy as _legacy
from .integration_state import (
    CandidateSnapshot,
    CandidateSnapshotError,
    build_candidate_snapshot,
    create_snapshot_commit,
    delete_snapshot_ref,
    porcelain_paths,
    untracked_paths,
)

WORKER_INTEGRATED_SNAPSHOT_OPTION = "_worker_integrated_snapshot_revision"
WORKER_INTEGRATED_TREE_OPTION = "_worker_integrated_worker_tree"
WORKER_INTEGRATED_REF_OPTION = "_worker_integrated_snapshot_ref"


class RuntimeIntegrationFixes:
    """Mixin binding worker acceptance to an exact, complete Git tree."""

    def _worker_options(
        self,
        *,
        worker_id: str,
        worker_name: str,
        workspace_mode: str,
        workspace: Dict[str, Any],
        model: str = "",
        reasoning_effort: str = "",
        allow_concurrent_shared_write: bool = False,
        request_context: Optional[Any] = None,
        existing_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        options = super()._worker_options(
            worker_id=worker_id,
            worker_name=worker_name,
            workspace_mode=workspace_mode,
            workspace=workspace,
            model=model,
            reasoning_effort=reasoning_effort,
            allow_concurrent_shared_write=allow_concurrent_shared_write,
            request_context=request_context,
            existing_options=existing_options,
        )
        if workspace_mode == "read_only":
            options["sandbox"] = "read-only"
            options["full_auto"] = False
            options.pop("dangerously_bypass", None)
        existing = existing_options or {}
        for key in (
            WORKER_INTEGRATED_SNAPSHOT_OPTION,
            WORKER_INTEGRATED_TREE_OPTION,
            WORKER_INTEGRATED_REF_OPTION,
        ):
            if existing.get(key):
                options[key] = existing[key]
        return options

    def _integration_baseline_revision(self, jobs: list[Any]) -> str:
        for job in reversed(jobs):
            value = str(
                (job.options or {}).get(WORKER_INTEGRATED_SNAPSHOT_OPTION) or ""
            ).strip()
            if value:
                return value
        workspace = self._workspace_for_jobs(jobs)
        return str(workspace.get("base_revision") or "").strip()

    def _integration_ref_name(self, jobs: list[Any]) -> str:
        worker_id, _ = self._worker_identity(jobs)
        digest = hashlib.sha256(
            worker_id.encode("utf-8", errors="replace")
        ).hexdigest()[:24]
        return f"refs/patchbay/workers/{digest}/integrated"

    def _candidate_exclusions(self, jobs: list[Any], root: str) -> list[str]:
        exclusions = [_legacy.ARTIFACT_CONTEXT_DIR]
        for rel_path, expected in self._included_untracked_base_digests(jobs).items():
            actual = self._file_sha256(Path(root) / rel_path)
            if actual and actual == expected:
                exclusions.append(rel_path)
        return exclusions

    def _candidate_snapshot(self, jobs: list[Any]) -> CandidateSnapshot:
        workspace = self._workspace_for_jobs(jobs)
        baseline = self._integration_baseline_revision(jobs)
        if workspace["mode"] == "read_only" or not workspace["available"]:
            return CandidateSnapshot(baseline, baseline, (), b"")
        if not baseline:
            raise CandidateSnapshotError("worker integration baseline is unavailable")
        root = self._execution_path_for_workspace(workspace)
        return build_candidate_snapshot(
            root,
            baseline,
            excluded_paths=self._candidate_exclusions(jobs, root),
        )

    def _changed_files(self, jobs: list[Any]) -> list[str]:
        workspace = self._workspace_for_jobs(jobs)
        if workspace["mode"] == "read_only" or not workspace["available"]:
            return []
        if not self._integration_baseline_revision(jobs):
            # Legacy records can expose honest current dirt for inspection, but
            # cannot certify integration: _candidate_snapshot still rejects a
            # missing delivery baseline rather than guessing from current HEAD.
            return porcelain_paths(self._execution_path_for_workspace(workspace))
        return list(self._candidate_snapshot(jobs).changed_files)

    def _git_untracked_files(self, base_repo: str) -> list[str]:
        try:
            return untracked_paths(base_repo)
        except CandidateSnapshotError:
            return []

    def _base_changed_files(self, base_repo: str) -> list[str]:
        try:
            return porcelain_paths(base_repo)
        except CandidateSnapshotError:
            return []

    def _is_untracked(self, root: str, rel_path: str) -> bool:
        try:
            return rel_path in set(untracked_paths(root))
        except CandidateSnapshotError:
            return False

    def _integration_patch(self, jobs: list[Any]) -> tuple[str, Dict[str, Any]]:
        snapshot = self._candidate_snapshot(jobs)
        patch_bytes = snapshot.patch
        skipped: list[str] = []
        truncated = False
        if len(patch_bytes) > _legacy.MAX_INTEGRATION_PATCH_BYTES:
            skipped = list(snapshot.changed_files)
            patch_bytes = b""
            truncated = True
        try:
            patch = patch_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CandidateSnapshotError(
                "Git produced a non-UTF-8 patch; the candidate remains preserved"
            ) from error
        return patch, {
            "patch_sha256": hashlib.sha256(patch_bytes).hexdigest()
            if patch_bytes
            else "",
            "patch_bytes": len(patch_bytes),
            "patch_truncated": truncated,
            "skipped_files": skipped,
            "worker_tree_sha": snapshot.tree_sha,
            "worker_baseline_revision": snapshot.baseline_revision,
        }

    def _applied_integration_result(
        self,
        jobs: list[Any],
        *,
        preview: Dict[str, Any],
        patch_info: Dict[str, Any],
        base_repo: str,
        request_context: Optional[Any],
        takeover: bool,
    ) -> Dict[str, Any]:
        result = super()._applied_integration_result(
            jobs,
            preview=preview,
            patch_info=patch_info,
            base_repo=base_repo,
            request_context=request_context,
            takeover=takeover,
        )
        result["worker_tree_sha"] = str(patch_info.get("worker_tree_sha") or "")
        result["worker_baseline_revision"] = str(
            patch_info.get("worker_baseline_revision") or ""
        )
        return result

    def _persist_integration_token_state(
        self,
        jobs: list[Any],
        token_state: Optional[Dict[str, Any]],
        *,
        request_context: Optional[Any] = None,
        takeover: bool = False,
        takeover_reason: str = "",
        integrated_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        snapshot_revision = ""
        snapshot_ref = ""
        worker_tree = ""
        if integrated_result is not None:
            worker_tree = str(integrated_result.get("worker_tree_sha") or "")
            parent = str(integrated_result.get("worker_baseline_revision") or "")
            if worker_tree:
                workspace = self._workspace_for_jobs(jobs)
                root = self._execution_path_for_workspace(workspace)
                snapshot_ref = self._integration_ref_name(jobs)
                old_snapshot = ""
                for job in reversed(jobs):
                    old_snapshot = str(
                        (job.options or {}).get(WORKER_INTEGRATED_SNAPSHOT_OPTION)
                        or ""
                    ).strip()
                    if old_snapshot:
                        break
                try:
                    snapshot_revision = create_snapshot_commit(
                        root,
                        tree_sha=worker_tree,
                        parent_revision=parent,
                        ref_name=snapshot_ref,
                        expected_old_revision=old_snapshot,
                    )
                except CandidateSnapshotError as error:
                    snapshot_revision = worker_tree
                    snapshot_ref = ""
                    integrated_result["snapshot_anchor_warning"] = str(error)
                integrated_result["integrated_snapshot_revision"] = snapshot_revision
                integrated_result["integrated_snapshot_ref"] = snapshot_ref

        super()._persist_integration_token_state(
            jobs,
            token_state,
            request_context=request_context,
            takeover=takeover,
            takeover_reason=takeover_reason,
            integrated_result=integrated_result,
        )
        if integrated_result is not None and snapshot_revision:
            latest = jobs[-1]

            def record_snapshot(options: Dict[str, Any]) -> Dict[str, Any]:
                options[WORKER_INTEGRATED_SNAPSHOT_OPTION] = snapshot_revision
                options[WORKER_INTEGRATED_TREE_OPTION] = worker_tree
                if snapshot_ref:
                    options[WORKER_INTEGRATED_REF_OPTION] = snapshot_ref
                else:
                    options.pop(WORKER_INTEGRATED_REF_OPTION, None)
                return options

            self.job_manager.mutate_job_options(latest.job_id, record_snapshot)

    def _unintegrated_changed_files(self, jobs: list[Any]) -> list[str]:
        try:
            return self._changed_files(jobs)
        except Exception as error:
            _legacy.logger.warning("Could not prove worker integration state: %s", error)
            return ["<integration-state-unavailable>"]

    def _integration_state_for_jobs(self, jobs: list[Any]) -> str:
        integrated = any(
            (job.options or {}).get(WORKER_INTEGRATED_SNAPSHOT_OPTION)
            or (job.options or {}).get("_worker_integrated_at")
            for job in jobs
        )
        if not integrated:
            return "not_integrated"
        workspace = self._workspace_for_jobs(jobs)
        if not workspace["available"] or workspace["discarded"]:
            return "applied_to_checkout"
        try:
            return (
                "applied_to_checkout"
                if not self._candidate_snapshot(jobs).changed_files
                else "not_integrated"
            )
        except Exception:
            return "not_integrated"

    def _projection_review_disposition(
        self,
        jobs: list[Any],
        *,
        turn_state: str,
        integration_state: str,
    ) -> str:
        explicit = ""
        for job in reversed(jobs):
            value = str(
                (job.options or {}).get(_legacy.WORKER_REVIEW_DISPOSITION_OPTION)
                or ""
            ).strip()
            if value in _legacy.WORKER_REVIEW_DISPOSITIONS:
                explicit = value
                break
        if integration_state == "applied_to_checkout":
            return "accepted"
        if integration_state in {"not_integrated", "uncertain"}:
            return "rejected" if explicit == "rejected" else "unreviewed"
        if explicit:
            return explicit
        if turn_state == "completed" and integration_state in {
            "not_applicable",
            "no_changes",
        }:
            return "not_required"
        return "unreviewed"

    def _cleanup_worker_workspace(self, jobs: list[Any]) -> bool:
        workspace = self._workspace_for_jobs(jobs)
        ref_name = ""
        revision = ""
        for job in reversed(jobs):
            options = job.options or {}
            ref_name = str(options.get(WORKER_INTEGRATED_REF_OPTION) or "").strip()
            revision = str(
                options.get(WORKER_INTEGRATED_SNAPSHOT_OPTION) or ""
            ).strip()
            if ref_name:
                break
        cleaned = super()._cleanup_worker_workspace(jobs)
        if cleaned and ref_name:
            try:
                delete_snapshot_ref(workspace["base_repo_path"], ref_name, revision)
            except CandidateSnapshotError as error:
                _legacy.logger.warning(
                    "Failed to remove worker snapshot ref %s: %s", ref_name, error
                )
        return cleaned
