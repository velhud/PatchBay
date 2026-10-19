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


class WorkerRuntime(RuntimeViewFixes, RuntimeIntegrationFixes, _legacy.WorkerRuntime):
    """Original worker runtime plus Git-native integration and lossless views."""

    pass
