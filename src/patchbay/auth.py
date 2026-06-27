"""HTTP authentication policy for the MCP connector."""
from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass, field
from typing import Mapping, Sequence


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off", ""}
LOCAL_TUNNEL_VALUES = {"", "none", "local", "false", "0"}
DEFAULT_QUERY_TOKEN_NAMES = ("patchbay_token", "token")
DEFAULT_TOKEN_ENVS = (
    "PATCHBAY_HTTP_TOKEN",
    "PATCHBAY_TOKEN",
)


class AuthConfigurationError(RuntimeError):
    """Raised when the server is configured to require auth but has no token."""


@dataclass(frozen=True)
class AuthPolicy:
    enabled: bool
    token: str | None = field(default=None, repr=False)
    allow_query_token: bool = True
    query_token_names: tuple[str, ...] = DEFAULT_QUERY_TOKEN_NAMES
    required_reasons: tuple[str, ...] = ()

    @property
    def token_configured(self) -> bool:
        return bool(self.token)


def build_auth_policy(config: Mapping[str, object], environ: Mapping[str, str] | None = None) -> AuthPolicy:
    """Build runtime auth policy from committed config plus environment."""
    env = environ if environ is not None else os.environ
    server_config = _mapping(config.get("server"))
    auth_config = _mapping(config.get("auth"))

    host = str(server_config.get("host") or "127.0.0.1")
    tunnel_mode = str(env.get("PATCHBAY_TUNNEL_MODE") or auth_config.get("tunnel_mode") or "none")
    token = _token_from_environment(auth_config, env)
    query_token_names = _query_token_names(auth_config)

    reasons: list[str] = []
    if _bool_from(auth_config.get("enabled"), False):
        reasons.append("auth.enabled")
    if _bool_from(env.get("PATCHBAY_REQUIRE_HTTP_TOKEN"), False):
        reasons.append("env.require_http_token")
    if token:
        reasons.append("token_configured")
    if _bool_from(auth_config.get("require_for_non_loopback"), True) and not is_loopback_host(host):
        reasons.append("non_loopback_bind")
    if _bool_from(auth_config.get("require_for_tunnel"), True) and tunnel_mode.lower() not in LOCAL_TUNNEL_VALUES:
        reasons.append("tunnel_mode")

    if reasons and not token:
        raise AuthConfigurationError(
            "HTTP token is required for this MCP binding but no token is configured. "
            "Set PATCHBAY_HTTP_TOKEN or bind to trusted loopback/local-only mode."
        )

    return AuthPolicy(
        enabled=bool(reasons),
        token=token if reasons else None,
        allow_query_token=_bool_from(auth_config.get("allow_query_token"), True),
        query_token_names=query_token_names,
        required_reasons=tuple(reasons),
    )


def is_loopback_host(host: str) -> bool:
    value = host.strip().lower().strip("[]")
    if value in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def request_is_authorized(
    policy: AuthPolicy,
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
) -> bool:
    if not policy.enabled:
        return True
    token = request_token(headers, query_params, policy)
    return token_matches(policy, token)


def request_token(
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
    policy: AuthPolicy,
) -> str | None:
    authorization = _header_get(headers, "authorization")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :]

    if policy.allow_query_token:
        for name in policy.query_token_names:
            value = query_params.get(name)
            if isinstance(value, str) and value:
                return value
    return None


def token_matches(policy: AuthPolicy, candidate: str | None) -> bool:
    if not policy.token or not candidate:
        return False
    return hmac.compare_digest(policy.token.encode("utf-8"), candidate.encode("utf-8"))


def auth_public_metadata(policy: AuthPolicy) -> dict[str, object]:
    return {
        "enabled": policy.enabled,
        "token_configured": policy.token_configured,
        "query_token_supported": policy.allow_query_token,
        "query_token_names": list(policy.query_token_names),
        "required_reasons": list(policy.required_reasons),
        "token_returned": False,
    }


def _token_from_environment(auth_config: Mapping[str, object], env: Mapping[str, str]) -> str | None:
    configured_env = auth_config.get("token_env")
    token_envs = [str(configured_env)] if isinstance(configured_env, str) and configured_env else []
    token_envs.extend(name for name in DEFAULT_TOKEN_ENVS if name not in token_envs)
    for name in token_envs:
        value = env.get(name)
        if value:
            return value
    return None


def _query_token_names(auth_config: Mapping[str, object]) -> tuple[str, ...]:
    raw = auth_config.get("query_token_names")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        names = tuple(str(item) for item in raw if str(item))
        if names:
            return names
    return DEFAULT_QUERY_TOKEN_NAMES


def _bool_from(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return default


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _header_get(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lowered:
            return candidate
    return ""
