# Consolidated Implementation Strategy

Build V2 alongside V1-compatible fleet behavior, using one canonical internal
registry and version-gated Edge protocol. Preserve existing WorkerRuntime and
ToolHandler mechanics; add durable operation correlation, projection, and Hub
adapters around them rather than duplicating Codex behavior in Hub.

The implementation is divided by architectural ownership, not by public tool
name. Shared foundations land first. Tool families land only when they can
return truthful domain results. Group close/reassign and integration are late
because they depend on authoritative projections and mutation reconciliation.

Verification is continuous after every WorkPacket and culminates in real local
MCP, real EdgeRunner, real WorkerRuntime/Codex, restart/failure injection, and
two-Edge execution. Synthetic result posting remains smoke evidence only.
