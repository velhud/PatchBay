"""Thin application composition; legacy worker and provider code is never loaded."""
from __future__ import annotations

import sys
from pathlib import Path

from .persistence.store import Store
from .repositories import Repositories
from .tasks import Tasks
from .snapshots import Snapshots
from .revisions.leases import Leases
from .revisions.materialize import Materializer


class AuthorService:
    def __init__(self, home: Path, *, expected_instance: str | None = None):
        self.store = Store(home, expected_instance=expected_instance)
        self.repositories = Repositories(self.store)
        self.tasks = Tasks(self.store)
        self.leases = Leases(self.store)
        self.snapshots = Snapshots(self.store)
        self.materializer = Materializer(self.store)

    def capabilities(self) -> dict:
        return {"name": "patchbay-author", "version": "0.1.0", "schema_version": 1,
                "instance_id": self.store.identity["instance_id"], "artifact_round_trip": True,
                "immutable_candidates": True, "durable_checks": sys.platform == "linux",
                "execution_platform": "Linux/WSL2", "strict_backend": "docker-offline",
                "trusted_local_is_a_sandbox": False, "model_delegation": False,
                "account_usage_visibility": "unknown", "client_write_qualification": "explicit_record_required",
                "default_branch_mutation": False, "automatic_merge": False,
                "unsupported": ["native-Windows-execution", "macOS-execution-supervision", "LFS/submodule/symlink mutation", "automatic model continuation"]}
