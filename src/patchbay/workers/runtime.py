"""Compatibility-preserving worker runtime with audited correctness fixes.

The complete original implementation remains in :mod:`runtime_legacy`.
Focused mixins replace integration/state and source-view contracts while this
module preserves the long-standing public import surface.
"""
from __future__ import annotations

from .runtime_legacy import *  # noqa: F401,F403 - preserve public constants/API
from . import runtime_legacy as _legacy
from .runtime_integration import *  # noqa: F401,F403
from .runtime_integration import RuntimeIntegrationFixes
from .runtime_views import RuntimeViewFixes
from .model_options import worker_execution_policy
from .requests import admitted_call, request_metadata
from .integration_state import CandidateSnapshotError
from patchbay.repo_locks import RepoMutationBusy
from contextlib import asynccontextmanager


class WorkerRuntime(RuntimeViewFixes, RuntimeIntegrationFixes, _legacy.WorkerRuntime):
    """Original worker runtime plus Git-native integration and lossless views."""

    async def start_worker(self, *, idempotency_key=None, **kwargs):
        kwargs["model"], kwargs["reasoning_effort"] = worker_execution_policy(
            kwargs.get("model"), kwargs.get("reasoning_effort"))
        try:
            return await admitted_call(self, super().start_worker, kwargs, idempotency_key)
        except RepoMutationBusy as error:
            return {"accepted": False, **error.public_payload()}

    async def message_worker(self, *, idempotency_key=None, **kwargs):
        async with self._source_reservation(kwargs) as busy:
            if busy:
                return busy
            return await admitted_call(self, super().message_worker, kwargs, idempotency_key)

    @asynccontextmanager
    async def _source_reservation(self, kwargs):
        jobs = self._resolve_worker(kwargs["worker"], repo_path=kwargs.get("repo_path"))
        workspace = self._workspace_for_jobs(jobs)
        if workspace["mode"] != "isolated_write" or not workspace["available"]:
            yield None
            return
        # Fixed order: worker source, then destination, then short job mutation.
        try:
            lease = await self.repo_locks.acquire(self._execution_path_for_workspace(workspace),
                                                  operation="worker_source_reservation")
        except RepoMutationBusy as error:
            yield {"accepted": False, "applied": False, "workspace_cleaned": False,
                   **error.public_payload()}
            return
        try:
            yield None
        finally:
            lease.release()

    async def integrate_worker(self, **kwargs):
        async with self._source_reservation(kwargs) as busy:
            if busy:
                return busy
            try:
                return await super().integrate_worker(**kwargs)
            except CandidateSnapshotError as error:
                return {"applied": False, "can_apply": False, "status": "blocked",
                        "effect_may_exist": True, "apply_disposition": "unknown",
                        "reason": error.reason, "observation_complete": False,
                        "note": "Source or destination observation failed; preserve both workspaces and retry inspection."}

    async def stop_worker(self, **kwargs):
        async with self._source_reservation(kwargs) as busy:
            if busy:
                return busy
            return await super().stop_worker(**kwargs)

    async def inspect_worker(self, *, candidate_id=None, **kwargs):
        if candidate_id:
            jobs = self._resolve_worker(kwargs["worker"], repo_path=kwargs.get("repo_path"))
            return self._retained_candidate_view(jobs, candidate_id=candidate_id, **kwargs)
        if kwargs.get("view") == "integration_preview":
            async with self._source_reservation(kwargs) as busy:
                if busy:
                    return busy
                try:
                    return await super().inspect_worker(**kwargs)
                except CandidateSnapshotError as error:
                    return {"can_apply": False, "applied": False, "status": "blocked",
                            "reason": error.reason, "observation_complete": False,
                            "modified_included_untracked_base_files": error.context_paths,
                            "note": ("Worker changed files copied from accepted untracked base context; preserve them and revise the source handoff explicitly."
                                     if error.context_paths else {
                                         'unsupported_gitlink': 'Changed or dirty gitlinks are unsupported; preserve the candidate workspace.',
                                         'unsupported_git_entry': 'The candidate contains an unsupported Git entry; preserve the workspace.',
                                         'unsupported_index_state': 'Unmerged or sparse worker index state cannot certify a complete candidate.',
                                         'candidate_materialization_mismatch': 'Git conversion or concurrent source changes prevent exact materialized candidate identity.',
                                         'review_target_budget_exhausted': 'Retained review-target budget exhausted; preserve existing targets and resolve retention explicitly.',
                                     }.get(error.reason, "Candidate or destination observation failed; no clean-state claim is available."))}
        return await super().inspect_worker(**kwargs)

    def _worker_options(self, **kwargs):
        kwargs["model"], kwargs["reasoning_effort"] = worker_execution_policy(
            kwargs.get("model"), kwargs.get("reasoning_effort"))
        return {**super()._worker_options(**kwargs), **request_metadata()}

    def _prepare_prompt(self, *args, **kwargs):
        prompt = super()._prepare_prompt(*args, **kwargs)
        return prompt + (
            "\n\nDelegated-worker policy: all workers and nested helpers MUST explicitly use "
            "gpt-6-luna with high (default), xhigh, or max effort. Never use another family "
            "or older Luna, inherit an ambient model, or reroute on failure. If unavailable, "
            "report the blocker. Do not create full chats or alternate launch paths to evade "
            "this policy. Include this entire policy and the exact model in nested handoffs.\n"
        )
