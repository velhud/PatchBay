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
    filesystem_fingerprints,
    tree_fingerprints,
    tree_entries,
    _run_git,
)

WORKER_INTEGRATED_SNAPSHOT_OPTION = "_worker_integrated_snapshot_revision"
WORKER_INTEGRATED_TREE_OPTION = "_worker_integrated_worker_tree"
WORKER_INTEGRATED_REF_OPTION = "_worker_integrated_snapshot_ref"


class RuntimeIntegrationFixes:
    """Mixin binding worker acceptance to an exact, complete Git tree."""

    def _public_view(self, jobs, **kwargs):
        view = super()._public_view(jobs, **kwargs)
        decision = (jobs[-1].options or {}).get('_worker_controller_review') or {}
        view['source_review'] = decision or {'disposition': 'unreviewed', 'scope': 'advisory_reports_only'}
        view['review_disposition_scope'] = 'controller_delivery_disposition; independent source review is separate'
        return view

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
        manifest_key = '_worker_included_untracked_base_manifest'
        if workspace.get('included_untracked_from_base'):
            root = self._execution_path_for_workspace(workspace)
            manifest = filesystem_fingerprints(root, workspace['included_untracked_from_base'])
            if any(entry['kind'] != 'file' for entry in manifest.values()):
                raise CandidateSnapshotError('Copied context must contain regular files')
            options[manifest_key] = {'format_version': 1, 'entries': manifest}
        elif manifest_key in existing:
            options[manifest_key] = existing[manifest_key]
        if existing.get('_worker_delivery_baseline_repair_required'):
            options['_worker_delivery_baseline_repair_required'] = True
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
        records = {}
        for job in jobs:
            manifest = (job.options or {}).get('_worker_included_untracked_base_manifest') or {}
            records.update(manifest.get('entries') or {})
        for rel_path, expected_sha in self._included_untracked_base_digests(jobs).items():
            expected = records.get(rel_path)
            if expected is None:
                # Old records have no historical kind/mode proof. Revalidate an
                # unchanged regular source against today's explicit base context.
                base = self._workspace_for_jobs(jobs)['base_repo_path']
                expected = filesystem_fingerprints(base, [rel_path])[rel_path]
                if expected['kind'] != 'file' or self._file_sha256(Path(base)/rel_path) != expected_sha:
                    raise CandidateSnapshotError('Legacy copied context needs explicit revalidation')
            actual = filesystem_fingerprints(root, [rel_path])[rel_path]
            if actual != expected:
                raise CandidateSnapshotError('Copied context kind/mode/content changed; preserve it and explicitly revise the source handoff', context_paths=[rel_path])
            exclusions.append(rel_path)
        return exclusions

    def _delivery_baseline_repair_required(self, jobs):
        options = jobs[-1].options or {}
        if options.get('_worker_delivery_baseline_repair_required'):
            return True
        baseline = options.get(WORKER_INTEGRATED_SNAPSHOT_OPTION)
        if not baseline:
            return False
        workspace = self._workspace_for_jobs(jobs)
        if not workspace['available']:
            return False
        try:
            observed = _run_git(workspace['base_repo_path'], ['cat-file', '-t', baseline], ok_returncodes=(0,128))
            return observed.returncode != 0 or observed.stdout.strip() != b'commit'
        except CandidateSnapshotError:
            return True

    def _candidate_snapshot(self, jobs: list[Any]) -> CandidateSnapshot:
        if self._delivery_baseline_repair_required(jobs):
            raise CandidateSnapshotError('Delivery is recorded but its baseline needs repair; preserve this workspace')
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
        return untracked_paths(base_repo)

    def _base_changed_files(self, base_repo: str) -> list[str]:
        return porcelain_paths(base_repo)

    def _is_untracked(self, root: str, rel_path: str) -> bool:
        try:
            return rel_path in set(untracked_paths(root))
        except CandidateSnapshotError:
            raise

    def _integration_patch(self, jobs: list[Any]) -> tuple[str, Dict[str, Any]]:
        return self._integration_patch_from_snapshot(self._candidate_snapshot(jobs))

    def _integration_patch_from_snapshot(self, snapshot):
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
            "expected_postimages": tree_fingerprints(snapshot.entries, snapshot.changed_files),
            "identity_representation": "raw_git_blob_mode_and_kind",
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
        result['controller_review'] = patch_info.get('controller_review') or {"disposition": "not_supplied", "scope": "delivery_acceptance_only"}
        result["worker_tree_sha"] = str(patch_info.get("worker_tree_sha") or "")
        result["worker_baseline_revision"] = str(
            patch_info.get("worker_baseline_revision") or ""
        )
        return result

    def _controller_review_decision(self, jobs, snapshot, decision, request_context):
        if decision is None:
            return {"disposition": "not_supplied", "scope": "delivery_acceptance_only"}
        if not isinstance(decision, dict) or set(decision) - {"candidate_id", "disposition", "evidence_refs"}:
            raise ValueError("review_decision has unsupported fields")
        if decision.get("disposition") not in {"accepted", "not_required"}:
            raise ValueError("review_decision must explicitly accept or declare separate review not required")
        _, target = self._review_target(jobs, decision.get("candidate_id"))
        if (target["tree_sha"] != snapshot.tree_sha or target["baseline_revision"] != snapshot.baseline_revision
                or target["turn_job_id"] != jobs[-1].job_id):
            raise ValueError("Review decision names a stale candidate or turn")
        refs = decision.get("evidence_refs") or []
        if not isinstance(refs, list) or len(refs) > 32 or any(not isinstance(ref, str) or not ref or len(ref)>512 for ref in refs):
            raise ValueError("Review evidence_refs must be bounded explicit references")
        principal, participant = self._integration_identity(request_context)
        return {**decision, "evidence_refs": refs, "actor": principal, "participant": participant,
                "scope": "controller_source_review_decision", "tree_sha": snapshot.tree_sha,
                "contract_sha256": target["contract_sha256"]}

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
        if integrated_result is not None:
            worker_tree = str(integrated_result.get('worker_tree_sha') or '')
            parent = str(integrated_result.get('worker_baseline_revision') or '')
            root = self._execution_path_for_workspace(self._workspace_for_jobs(jobs))
            ref = self._integration_ref_name(jobs)
            try:
                if not worker_tree or not parent:
                    raise CandidateSnapshotError('Delivery lacks its candidate tree or baseline')
                # A crash can leave this owned ref ahead of the authoritative
                # job record. Adopt only its exact intended tree and parent.
                retained = _run_git(root, ['rev-parse', '--verify', ref], ok_returncodes=(0,128))
                existing = retained.stdout.decode().strip() if retained.returncode == 0 else ''
                if existing and _run_git(root, ['rev-parse', existing+'^{tree}']).stdout.decode().strip() == worker_tree and _run_git(root, ['rev-parse', existing+'^']).stdout.decode().strip() == parent:
                    revision = existing
                else:
                    old = self._integration_baseline_revision(jobs)
                    if existing and existing != old:
                        raise CandidateSnapshotError('Delivery ref belongs to a different generation')
                    revision = create_snapshot_commit(root, tree_sha=worker_tree, parent_revision=parent,
                        ref_name=ref, expected_old_revision=existing)
                integrated_result.update(integrated_snapshot_revision=revision, integrated_snapshot_ref=ref,
                                         baseline_repair_required=False)
            except CandidateSnapshotError as error:
                integrated_result.update(baseline_repair_required=True, snapshot_anchor_warning='Candidate anchor unavailable; preserve workspace and recover delivery bookkeeping',
                                         integrated_snapshot_revision='', integrated_snapshot_ref='')
            if token_state is not None:
                for record in token_state.get('tokens', {}).values():
                    previous = record.get('result') or {}
                    if (record.get('disposition') == 'applied' and previous.get('worker_tree_sha') == worker_tree
                            and previous.get('worker_baseline_revision') == parent
                            and previous.get('patch_sha256') == integrated_result.get('patch_sha256')):
                        record['result'] = dict(integrated_result)
        super()._persist_integration_token_state(jobs, token_state,
            request_context=request_context, takeover=takeover, takeover_reason=takeover_reason,
            integrated_result=integrated_result)

    def _repair_applied_delivery(self, jobs, token_state, token_record):
        result = dict(token_record.get('result') or {})
        wanted = result.get('worker_tree_sha') or (token_record.get('patch_info') or {}).get('worker_tree_sha')
        latest = jobs[-1].options or {}
        baseline = latest.get(WORKER_INTEGRATED_SNAPSHOT_OPTION)
        workspace = self._workspace_for_jobs(jobs)
        if not workspace['available']:
            # Retired workspaces need historical readback, not a new baseline.
            return
        commit_baseline = False
        if baseline:
            try:
                observed = _run_git(workspace['base_repo_path'], ['cat-file', '-t', baseline], ok_returncodes=(0,128))
                commit_baseline = observed.returncode == 0 and observed.stdout.strip() == b'commit'
            except CandidateSnapshotError:
                pass
        if baseline and commit_baseline and latest.get(WORKER_INTEGRATED_TREE_OPTION) == wanted and not latest.get('_worker_delivery_baseline_repair_required'):
            return
        parent = result.get('worker_baseline_revision') or (token_record.get('patch_info') or {}).get('worker_baseline_revision')
        if baseline and baseline != parent and latest.get(WORKER_INTEGRATED_TREE_OPTION) != wanted:
            # An old receipt cannot rewind a different current delivery. Legacy
            # split repair is allowed only from that receipt's exact input base.
            result['baseline_repair_skipped'] = 'different_current_delivery_generation'
            token_record['result'] = result
            return
        if not wanted:
            result['baseline_repair_required'] = True
            token_record['result'] = result
            self.job_manager.mutate_job_options(jobs[-1].job_id, lambda options: {**options,
                '_worker_delivery_baseline_repair_required': True, _legacy.WORKER_INTEGRATION_TOKENS_OPTION: token_state})
            return
        result.setdefault('worker_tree_sha', wanted)
        result.setdefault('worker_baseline_revision', (token_record.get('patch_info') or {}).get('worker_baseline_revision'))
        workspace = self._workspace_for_jobs(jobs)
        root = self._execution_path_for_workspace(workspace)
        try:
            expected = tree_fingerprints(tree_entries(root, wanted), result.get('changed_files') or [])
            compatible = filesystem_fingerprints(workspace['base_repo_path'], expected) == expected
        except CandidateSnapshotError:
            compatible = False
        if not compatible:
            result['baseline_repair_required'] = True
            token_record['result'] = result
            self.job_manager.mutate_job_options(jobs[-1].job_id, lambda options: {**options,
                '_worker_delivery_baseline_repair_required': True, _legacy.WORKER_INTEGRATION_TOKENS_OPTION: token_state})
            return
        self._persist_integration_token_state(jobs, token_state, integrated_result=result)

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
        if (jobs[-1].options or {}).get('_worker_delivery_baseline_repair_required'):
            return False
        cleaned = super()._cleanup_worker_workspace(jobs)
        if cleaned and ref_name:
            try:
                delete_snapshot_ref(workspace["base_repo_path"], ref_name, revision)
            except CandidateSnapshotError as error:
                _legacy.logger.warning(
                    "Failed to remove worker snapshot ref %s: %s", ref_name, error
                )
        return cleaned
