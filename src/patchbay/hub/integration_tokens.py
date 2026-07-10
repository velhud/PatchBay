"""Signed opaque handles for worker integration previews.

The token carries no claims. Claims and apply disposition remain in the Edge's
private durable worker record; the caller receives only a random identifier and
an HMAC signature that cannot be forged or moved between workers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Any, Mapping


INTEGRATION_PREVIEW_TOKEN_VERSION = 2
INTEGRATION_PREVIEW_TOKEN_PREFIX = "pit2"
_TOKEN_ID_BYTES = 24
_SIGNING_SECRET_BYTES = 32
_TOKEN_PATTERN = re.compile(r"^pit2\.([A-Za-z0-9_-]{24,96})\.([A-Za-z0-9_-]{32,96})$")


class IntegrationPreviewTokenError(ValueError):
    """Raised when an opaque preview token is malformed or unauthentic."""


def new_signing_secret() -> str:
    """Return a URL-safe signing secret suitable for private durable storage."""
    return _encode(secrets.token_bytes(_SIGNING_SECRET_BYTES))


def new_token_id() -> str:
    """Return a high-entropy opaque token identifier."""
    return _encode(secrets.token_bytes(_TOKEN_ID_BYTES))


def format_signed_token(signing_secret: str, token_id: str) -> str:
    """Sign one token identifier without embedding its claims."""
    identifier = _validated_token_id(token_id)
    signature = hmac.new(
        _decode_secret(signing_secret),
        f"{INTEGRATION_PREVIEW_TOKEN_PREFIX}.{identifier}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{INTEGRATION_PREVIEW_TOKEN_PREFIX}.{identifier}.{_encode(signature)}"


def issue_signed_token(signing_secret: str) -> tuple[str, str]:
    """Return ``(token, token_id)`` for a new opaque preview handle."""
    token_id = new_token_id()
    return format_signed_token(signing_secret, token_id), token_id


def verify_signed_token(token: str, signing_secret: str) -> str:
    """Verify a token and return its opaque identifier."""
    match = _TOKEN_PATTERN.fullmatch(str(token or "").strip())
    if match is None:
        raise IntegrationPreviewTokenError("invalid_preview_token")
    token_id, supplied_signature = match.groups()
    expected = format_signed_token(signing_secret, token_id).rsplit(".", 1)[1]
    if not hmac.compare_digest(supplied_signature, expected):
        raise IntegrationPreviewTokenError("invalid_preview_token")
    return token_id


def canonical_sha256(value: Mapping[str, Any]) -> str:
    """Hash a JSON-compatible mapping using the repository's canonical shape."""
    import json

    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_token_id(value: str) -> str:
    token_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,96}", token_id):
        raise IntegrationPreviewTokenError("invalid_preview_token")
    return token_id


def _decode_secret(value: str) -> bytes:
    try:
        decoded = _decode(str(value or "").strip())
    except Exception as error:
        raise IntegrationPreviewTokenError("invalid_preview_token_signing_secret") from error
    if len(decoded) < _SIGNING_SECRET_BYTES:
        raise IntegrationPreviewTokenError("invalid_preview_token_signing_secret")
    return decoded


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
