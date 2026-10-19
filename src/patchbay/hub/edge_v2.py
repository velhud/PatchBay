"""Compatibility-preserving Edge V2 projection corrections."""
from __future__ import annotations

from .edge_v2_legacy import *  # noqa: F401,F403
from . import edge_v2_legacy as _legacy


class EdgeExecutionService(_legacy.EdgeExecutionService):
    """Do not tombstone Pro Requests merely displaced from a truncated page."""

    def _finalize_projection_snapshot(self, raw):
        previous = list(getattr(self, "_last_projection_pro_request_ids", []))
        snapshot = super()._finalize_projection_snapshot(raw)
        if not bool(snapshot.get("complete_pro_request_set")):
            current = [
                str(item.get("request_id") or "")
                for item in snapshot.get("pro_requests") or []
                if isinstance(item, dict) and item.get("request_id")
            ]
            snapshot["pro_request_tombstones"] = []
            # Preserve knowledge of requests outside this page so a truncated
            # view cannot turn ordinary pagination into deletion semantics.
            self._last_projection_pro_request_ids = list(
                dict.fromkeys([*previous, *current])
            )
        return snapshot
