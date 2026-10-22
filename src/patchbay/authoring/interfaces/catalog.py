"""Capability qualification binds the actual schemas and installed Author code."""
from __future__ import annotations

import hashlib
from pathlib import Path
from functools import lru_cache

from ..util import fingerprint

CATALOG_VERSION = "author-v2"
OUTPUT_PROFILE = "compat-full-v1"
OUTPUT_PROFILES = {OUTPUT_PROFILE, "structured-summary-v2"}


@lru_cache(maxsize=8)
def _source_digest(stamps: tuple) -> str:
    root = Path(__file__).parents[1]
    h = hashlib.sha256()
    for name, *_ in stamps:
        h.update(name.encode())
        h.update((root / name).read_bytes())
    return h.hexdigest()


def identity(output_profile: str = OUTPUT_PROFILE) -> dict[str, str]:
    from .mcp_legacy import CATALOG
    from .outputs import CONTRACTS
    from .instructions import READ_ONLY, WRITABLE
    root = Path(__file__).parents[1]
    sources = [(p.relative_to(root).as_posix(), p) for p in sorted(root.rglob("*.py"))]
    # Bind the independent shared negotiation helper too; a change to it must
    # invalidate Author qualification just like a subtree runtime change.
    sources.append(("../mcp_versions.py", root.parent / "mcp_versions.py"))
    sources.append(("../mcp_framing.py", root.parent / "mcp_framing.py"))
    stamps = tuple((name, p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ctime_ns, p.stat().st_ino)
                   for name, p in sources)
    specs = [{"name": name, "description": description, "inputSchema": model.model_json_schema(),
              "outputSchema": CONTRACTS[name].model_json_schema(), "mutates": write}
             for name, model, description, write in CATALOG]
    if output_profile not in OUTPUT_PROFILES:
        raise ValueError("Unsupported Author output profile")
    return {"version": CATALOG_VERSION, "sha256": fingerprint({"tools": specs, "reader": READ_ONLY, "writer": WRITABLE, "output_profile": output_profile}),
            "runtime_sha256": _source_digest(stamps), "output_profile": output_profile}
