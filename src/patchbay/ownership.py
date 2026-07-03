"""Shared owner metadata helpers for MCP coordination.

Ownership here is accidental-interference protection, not authentication. The
HTTP bearer/query token is still the authentication boundary; these helpers only
let shared MCP clients see whether a worker, job, or artifact was last controlled
by the current coordination owner.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from patchbay.protocol.context import ANONYMOUS_CLIENT_REF, RequestContext


OWNER_SESSION_HASH_OPTION = "_mcp_owner_session_hash"
OWNER_CLIENT_REF_OPTION = "_mcp_owner_client_ref"
OWNER_LABEL_OPTION = "_mcp_owner_label"
OWNER_CREATED_AT_OPTION = "_mcp_owner_created_at"
OWNER_LAST_SEEN_AT_OPTION = "_mcp_owner_last_seen_at"
OWNER_SCOPE_OPTION = "_mcp_owner_scope"
OWNER_SCHEMA_OPTION = "_mcp_owner_schema"
CURRENT_OWNER_SCHEMA = "patchbay-owner-v2"

OWNER_METADATA_KEYS = {
    OWNER_SESSION_HASH_OPTION,
    OWNER_CLIENT_REF_OPTION,
    OWNER_LABEL_OPTION,
    OWNER_CREATED_AT_OPTION,
    OWNER_LAST_SEEN_AT_OPTION,
    OWNER_SCOPE_OPTION,
    OWNER_SCHEMA_OPTION,
}
MAX_TAKEOVER_REASON_CHARS = 500


def _clean_label(value: Any) -> str:
    label = " ".join(str(value or "").split())
    return label[:80]


def owner_hash_for_context(context: RequestContext | None) -> str:
    """Return the private comparable owner hash for a request context."""
    if context is None:
        return ""
    if context.owner_ref and context.owner_ref != ANONYMOUS_CLIENT_REF:
        return context.owner_ref
    if not context.has_transport_session or context.client_ref == ANONYMOUS_CLIENT_REF:
        return ""
    return context.client_ref


def owner_scope_for_context(context: RequestContext | None) -> str:
    """Return the comparable owner scope for a request context."""
    if context is None:
        return ""
    if context.owner_scope:
        return context.owner_scope
    if context.has_transport_session and context.client_ref != ANONYMOUS_CLIENT_REF:
        return "transport_session"
    return ""


def owner_metadata_from_context(
    context: RequestContext | None,
    *,
    existing: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build private owner fields for durable job/artifact metadata."""
    owner_hash = owner_hash_for_context(context)
    if not owner_hash:
        return {}

    existing = existing or {}
    timestamp = float(now if now is not None else time.time())
    created_at = existing.get(OWNER_CREATED_AT_OPTION) or timestamp
    return {
        OWNER_SESSION_HASH_OPTION: owner_hash,
        OWNER_CLIENT_REF_OPTION: context.client_ref if context else "",
        OWNER_SCOPE_OPTION: owner_scope_for_context(context),
        OWNER_SCHEMA_OPTION: CURRENT_OWNER_SCHEMA,
        OWNER_LABEL_OPTION: _clean_label(context.client_label if context else ""),
        OWNER_CREATED_AT_OPTION: float(created_at),
        OWNER_LAST_SEEN_AT_OPTION: timestamp,
    }


def merge_owner_metadata(
    metadata: Mapping[str, Any] | None,
    context: RequestContext | None,
    *,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return metadata plus current private owner fields when context is known."""
    merged = dict(metadata or {})
    owner = owner_metadata_from_context(context, existing=existing or merged)
    if owner:
        merged.update(owner)
    return merged


def stored_owner_hash(metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return ""
    return str(metadata.get(OWNER_SESSION_HASH_OPTION) or metadata.get(OWNER_CLIENT_REF_OPTION) or "")


def stored_owner_scope(metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return ""
    return str(metadata.get(OWNER_SCOPE_OPTION) or "")


def public_ownership(
    metadata: Mapping[str, Any] | None,
    context: RequestContext | None,
    *,
    mutation_name: str = "mutating this item",
) -> dict[str, Any]:
    """Return safe ownership flags without exposing raw/private owner fields."""
    owner_hash = stored_owner_hash(metadata)
    current_hash = owner_hash_for_context(context)
    owner_scope = stored_owner_scope(metadata)
    current_scope = owner_scope_for_context(context)
    owner_label = _clean_label((metadata or {}).get(OWNER_LABEL_OPTION))

    result: dict[str, Any] = {}
    if context and context.owner_scope:
        result["ownership_scope"] = context.owner_scope
    if not owner_hash:
        result["owned_by_current_client"] = None
        result["ownership_status"] = "unknown_previous_connection"
        result["ownership_note"] = (
            "This item has no MCP owner metadata. In shared-client use, "
            f"{mutation_name} may require takeover=true after user confirmation."
        )
    elif current_hash and owner_hash == current_hash:
        result["owned_by_current_client"] = True
        result["ownership_status"] = "current_client"
    else:
        result["owned_by_current_client"] = False if current_hash else None
        if not current_hash:
            result["ownership_status"] = "unknown_current_connection"
            result["ownership_note"] = (
                "PatchBay cannot identify the current MCP coordination owner. "
                f"{mutation_name} may require takeover=true after user confirmation."
            )
        elif not owner_scope:
            result["ownership_status"] = "legacy_connection"
            result["ownership_note"] = (
                "This item has owner metadata from an older PatchBay version that did not record whether the "
                "owner was a transport session, token, or server-scoped owner. It may be from the same ChatGPT "
                "workflow, an older short-lived MCP session, or an earlier token. "
                f"Use takeover=true only after the user confirms {mutation_name} is intentional; that will "
                "rewrite the item's owner metadata using the current scoped owner model."
            )
        elif owner_scope != current_scope:
            result["ownership_status"] = "different_owner_scope"
            result["ownership_note"] = (
                f"This item was last controlled under {owner_scope!r} ownership, while the current request uses "
                f"{current_scope!r} ownership. This usually means PatchBay configuration or authentication changed. "
                f"Use takeover=true only after the user confirms {mutation_name} is intentional."
            )
        elif owner_scope == "token":
            result["ownership_status"] = "other_token_owner"
            result["ownership_note"] = (
                "This item was last controlled by a different token-scoped PatchBay owner. Short-lived MCP "
                "transport sessions using the same copied Server URL should normally share one token owner, so "
                "this usually means a different tokenized URL or a token rotation. "
                f"Use takeover=true only after the user confirms {mutation_name} is intentional."
            )
        else:
            result["ownership_status"] = "other_connection"
            result["ownership_note"] = (
                "This item was created or last controlled by a different PatchBay coordination owner. "
                f"Use takeover=true only after the user confirms {mutation_name} is intentional."
            )
    if owner_label:
        result["owner_label"] = owner_label
    return result


def takeover_required(metadata: Mapping[str, Any] | None, context: RequestContext | None) -> bool:
    """Return whether a known MCP caller must explicitly take over before mutation."""
    current_hash = owner_hash_for_context(context)
    if not current_hash:
        return False
    owner_hash = stored_owner_hash(metadata)
    return not owner_hash or owner_hash != current_hash


def clean_takeover_reason(value: Any) -> str:
    reason = " ".join(str(value or "").split())
    if len(reason) > MAX_TAKEOVER_REASON_CHARS:
        reason = reason[:MAX_TAKEOVER_REASON_CHARS].rstrip()
    return reason


def takeover_refusal(
    metadata: Mapping[str, Any] | None,
    context: RequestContext | None,
    *,
    mutation_name: str,
) -> dict[str, Any]:
    """Return a safe public refusal payload for cross-owner mutation."""
    payload = public_ownership(metadata, context, mutation_name=mutation_name)
    payload.update(
        {
            "takeover_required": True,
            "takeover_performed": False,
            "required_action": "call again with takeover=true after user confirms this is intentional",
            "note": (
                "This item appears to belong to another MCP connection or legacy unknown owner. "
                "Read/list/inspect are allowed, but mutation requires explicit user-confirmed takeover."
            ),
        }
    )
    return payload
