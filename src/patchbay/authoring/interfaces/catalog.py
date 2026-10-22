"""Capability qualification binds the actual schemas and installed Author code."""
from __future__ import annotations

import hashlib
import os
import sys
import importlib.metadata
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


def observed_source_digest() -> str:
    root = Path(__file__).parents[1]
    sources = [(p.relative_to(root).as_posix(), p) for p in sorted(root.rglob("*.py"))]
    # Bind the independent shared negotiation helper too; a change to it must
    # invalidate Author qualification just like a subtree runtime change.
    sources.append(("../mcp_versions.py", root.parent / "mcp_versions.py"))
    sources.append(("../mcp_framing.py", root.parent / "mcp_framing.py"))
    stamps = tuple((name, p.stat().st_mtime_ns, p.stat().st_size, p.stat().st_ctime_ns, p.stat().st_ino)
                   for name, p in sources)
    return _source_digest(stamps)


# Capture once when the independent Author runtime loads. Disk hashing remains
# a drift detector; it can never relabel these already loaded objects as B.
STARTUP_SOURCE_SHA256 = observed_source_digest()
# Editable metadata may appear both on the source path and in site-packages.
# Select all available RECORD identities deterministically, never whichever
# distribution happens to occur first in sys.path for this entrypoint.
_installation_records = sorted({hashlib.sha256(record.encode()).hexdigest()
    for distribution in importlib.metadata.distributions()
    if distribution.metadata.get("Name", "").lower() == "patchbay"
    for record in [distribution.read_text("RECORD")] if record})
_STARTUP_RUNTIME_OBSERVATION = {
    "source_manifest": STARTUP_SOURCE_SHA256,
    "installation_record": _installation_records or "unavailable",
    "interpreter": sys.version, "executable": str(Path(sys.executable).resolve()), "prefix": sys.prefix,
    "dependencies": sorted({(d.metadata.get("Name", "").lower(), d.version) for d in importlib.metadata.distributions()}),
}
STARTUP_RUNTIME_SHA256 = fingerprint(_STARTUP_RUNTIME_OBSERVATION)
DEPLOYMENT_SHA256 = os.environ.get("PATCHBAY_AUTHOR_DEPLOYMENT_SHA256", "not_recorded")
if DEPLOYMENT_SHA256 != "not_recorded":
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", DEPLOYMENT_SHA256):
        raise RuntimeError("Deployment binding must be an opaque SHA-256 digest")


def assert_current_release() -> str:
    from ..errors import require
    require(observed_source_digest() == STARTUP_SOURCE_SHA256, "RELEASE_DRIFT",
            "Author source changed in place; drain and restart an immutable release before new writes")
    return STARTUP_RUNTIME_SHA256


def identity(output_profile: str = OUTPUT_PROFILE) -> dict[str, str]:
    from .mcp_legacy import CATALOG
    from .outputs import CONTRACTS
    from .instructions import READ_ONLY, WRITABLE
    observed = observed_source_digest()
    specs = [{"name": name, "description": description, "inputSchema": model.model_json_schema(),
              "outputSchema": CONTRACTS[name].model_json_schema(), "mutates": write}
             for name, model, description, write in CATALOG]
    if output_profile not in OUTPUT_PROFILES:
        raise ValueError("Unsupported Author output profile")
    return {"version": CATALOG_VERSION, "sha256": fingerprint({"tools": specs, "reader": READ_ONLY, "writer": WRITABLE, "output_profile": output_profile}),
            "runtime_sha256": fingerprint({"loaded": STARTUP_RUNTIME_SHA256, "observed_source": observed,
                                            "deployment": DEPLOYMENT_SHA256}),
            "loaded_runtime_sha256": STARTUP_RUNTIME_SHA256,
            "startup_source_sha256": STARTUP_SOURCE_SHA256, "observed_source_sha256": observed,
            "source_drift": "unchanged" if observed == STARTUP_SOURCE_SHA256 else "detected",
            "deployment_sha256": DEPLOYMENT_SHA256, "output_profile": output_profile}
