"""Compatibility-preserving Hub Pro Request projection corrections."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .pro_requests_legacy import *  # noqa: F401,F403
from . import pro_requests_legacy as _legacy


class FleetHubProRequestAdapterV2(_legacy.FleetHubProRequestAdapterV2):
    """Keep current projection metadata monotonic across historical reads."""

    def _refresh_from_canonical(
        self, association: Mapping[str, Any], request: Mapping[str, Any]
    ) -> None:
        request_ref = str(association.get("request_ref") or "")
        entity = self.hub_store.get_entity(_legacy.PRO_REQUEST_ASSOCIATION_ENTITY, request_ref)
        if entity is None:
            return
        record = deepcopy(dict(entity["record"]))
        current_revision = int(record.get("revision") or 0)
        incoming_revision = int(request.get("revision") or 0)
        if incoming_revision <= current_revision:
            # Operation-result reads are historical evidence. They must not
            # overwrite a newer canonical status, or churn an equal revision.
            return
        for key in (
            "status",
            "revision",
            "created_at",
            "updated_at",
            "workspace_id",
            "repo_name",
            "priority",
            "kind",
            "attachment_count",
        ):
            if key in request:
                record[key] = deepcopy(request[key])
        response = request.get("response") if isinstance(request.get("response"), Mapping) else {}
        origin = request.get("origin") if isinstance(request.get("origin"), Mapping) else {}
        record["response_exists"] = bool(response.get("exists"))
        record["origin_available_for_dispatch"] = bool(
            origin.get("origin_available_for_dispatch")
        )
        self.hub_store.put_entity(
            _legacy.PRO_REQUEST_ASSOCIATION_ENTITY,
            request_ref,
            record,
            expected_revision=entity["revision"],
        )
