"""Typed tool refusals that are safe to return through remote control planes."""

from __future__ import annotations


class PublicToolRefusal(ValueError):
    """A deterministic refusal known to have occurred before a tool side effect."""

    def __init__(self, reason: str, message: str):
        self.reason = str(reason or "tool_refused")
        self.public_message = str(message or "The tool request was refused.")
        super().__init__(self.public_message)


class WorkerNameConflict(PublicToolRefusal):
    """A worker name already exists in the selected repository."""

    def __init__(self, name: str):
        super().__init__(
            "worker_name_conflict",
            (
                f"A worker named {name!r} already exists in this workspace. Continue it with "
                "patchbay_worker_message, pass auto_suffix=true, or choose another human-readable name."
            ),
        )
