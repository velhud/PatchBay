"""Pure MCP version selection and HTTP header checks; no runtime imports.

These versions describe the existing JSON tools/resource transport subset.
Negotiation does not advertise optional tasks, sampling, or subscriptions.
"""
from __future__ import annotations

SUPPORTED_MCP_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
PREFERRED_MCP_VERSION = "2025-11-25"


def select_mcp_version(requested: object, *, preferred: str = PREFERRED_MCP_VERSION) -> str:
    if not isinstance(requested, str) or not requested.strip():
        raise ValueError("initialize.protocolVersion must be a non-empty string")
    if preferred not in SUPPORTED_MCP_VERSIONS:
        raise ValueError("Unsupported server MCP preference")
    return requested if requested in SUPPORTED_MCP_VERSIONS else preferred


def validate_mcp_version_header(header: str | None, *, negotiated: str | None = None) -> None:
    # Missing headers retain compatibility with existing clients. A stateful
    # transport uses its negotiated version; stateless clients use March 2025.
    version = header if header is not None else negotiated or "2025-03-26"
    if version not in SUPPORTED_MCP_VERSIONS:
        raise ValueError("Unsupported MCP-Protocol-Version header")
    if negotiated is not None and version != negotiated:
        raise ValueError("MCP-Protocol-Version header differs from the negotiated version")
