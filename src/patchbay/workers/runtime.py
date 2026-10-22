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


class WorkerRuntime(RuntimeViewFixes, RuntimeIntegrationFixes, _legacy.WorkerRuntime):
    """Original worker runtime plus Git-native integration and lossless views."""

    async def start_worker(self, *, idempotency_key=None, **kwargs):
        kwargs["model"], kwargs["reasoning_effort"] = worker_execution_policy(
            kwargs.get("model"), kwargs.get("reasoning_effort"))
        return await admitted_call(self, super().start_worker, kwargs, idempotency_key)

    async def message_worker(self, *, idempotency_key=None, **kwargs):
        return await admitted_call(self, super().message_worker, kwargs, idempotency_key)

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
