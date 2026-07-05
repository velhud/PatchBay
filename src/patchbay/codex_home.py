"""Shared Codex home resolution for PatchBay runtime services."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping


def resolve_codex_home(
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the effective Codex home PatchBay should use for CLI/auth/session state."""
    env = environ if environ is not None else os.environ
    configured = ""
    if isinstance(config, Mapping):
        power_tools = config.get("power_tools")
        if isinstance(power_tools, Mapping):
            configured = str(power_tools.get("codex_home") or "").strip()
    selected = configured or str(env.get("CODEX_HOME") or "").strip() or "~/.codex"
    return Path(os.path.expandvars(selected)).expanduser().resolve(strict=False)


def codex_home_path_hint(path: Path) -> str:
    """Return a human-safe path hint without raw config values."""
    home = Path.home().resolve(strict=False)
    try:
        relative = path.resolve(strict=False).relative_to(home)
    except ValueError:
        return "configured_codex_home/" + path.name
    return "~/" + str(relative)
