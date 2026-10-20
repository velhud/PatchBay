"""Authenticated Author principals and request correlation context.

Role tokens predate principal identities and remain a compatibility surface.
New principal tokens are mapped to a stable principal ID in the private Author
database; the bearer value is never part of an identity or journal payload.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re

from .errors import require
from .util import safe_id


@dataclass(frozen=True)
class AuthIdentity:
    principal_id: str
    role: str
    task_ids: tuple[str, ...] | None = None
    legacy: bool = False


_LOCAL_OPERATOR = AuthIdentity("local-operator", "operator", None, legacy=True)
_identity: ContextVar[AuthIdentity] = ContextVar("patchbay_author_identity", default=_LOCAL_OPERATOR)
_request_id: ContextVar[str | None] = ContextVar("patchbay_author_request_id", default=None)


def current_identity() -> AuthIdentity:
    """Return the request principal, or the trusted local CLI principal."""
    return _identity.get()


def assert_task_scope(task_id: str | None, identity: AuthIdentity | None = None) -> None:
    """Enforce an identity's exact task allowlist, including global records."""
    identity = identity or current_identity()
    if identity.task_ids is None:
        return
    require(isinstance(task_id, str) and task_id in identity.task_ids, "FORBIDDEN",
            "This principal is not granted access to the requested task", status=403)
    safe_id(task_id)


def assert_operator(task_id: str | None = None, identity: AuthIdentity | None = None) -> None:
    """Require operator role and either global or exact-task authorization."""
    identity = identity or current_identity()
    require(identity.role == "operator", "FORBIDDEN", "An operator principal is required", status=403)
    if task_id is None:
        require(identity.task_ids is None, "FORBIDDEN", "An unscoped operator principal is required", status=403)
    else:
        assert_task_scope(task_id, identity)


@contextmanager
def identity_context(identity: AuthIdentity):
    require(isinstance(identity, AuthIdentity), "UNAUTHORIZED", "A verified Author identity is required", status=401)
    token = _identity.set(identity)
    try:
        yield identity
    finally:
        _identity.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_context(request_id: str | None):
    """Set safe transport correlation metadata without changing logical keys."""
    if request_id is not None:
        require(isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id) is not None,
                "INVALID_REQUEST", "Request correlation ID is invalid")
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _request_id.reset(token)
