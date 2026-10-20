"""Operator, author, and reader authority; MCP writes require recorded client qualification."""
from __future__ import annotations

import hmac
import uuid
from ..errors import require
from ..persistence.gates import mutation
from ..util import atomic_write, canonical, strict_json, utc_now


class Access:
    def __init__(self, store):
        self.store = store

    def role(self, authorization: str | None) -> str:
        require(isinstance(authorization, str) and authorization.startswith("Bearer ") and len(authorization) <= 1024,
                "UNAUTHORIZED", "Author runtime authorization is required", status=401)
        candidate = authorization[7:]
        tokens = strict_json((self.store.home / "tokens.json").read_bytes())
        for role in ("operator", "author", "reader"):
            if isinstance(tokens.get(role), str) and hmac.compare_digest(tokens[role], candidate):
                return role
        require(False, "UNAUTHORIZED", "Author runtime authorization was not accepted", status=401)

    def require_mutation(self, role: str, *, operator_only: bool = False, mcp: bool = False) -> None:
        require(role in ({"operator"} if operator_only else {"operator", "author"}),
                "FORBIDDEN", "This action exceeds the current runtime role", status=403)
        if mcp:
            require(self.qualified() is not None, "QUALIFICATION_REQUIRED",
                    "Live MCP writes are not qualified for this runtime; use the artifact import workflow", status=403)

    def qualified(self) -> dict | None:
        path = self.store.home / "client-qualification.json"
        if not path.is_file() or path.is_symlink():
            return None
        record = strict_json(path.read_bytes())
        if (record.get("instance_id") != self.store.identity["instance_id"] or record.get("expires", 0) <= utc_now()
                or record.get("evidence", {}).get("ordinary_chat") is not True
                or record.get("evidence", {}).get("write_supported") is not True):
            return None
        return record

    @mutation
    def qualify(self, evidence: dict, *, actor: str) -> dict:
        allowed = {"model", "client", "account_label", "ordinary_chat", "write_supported", "probe_description", "observed_at"}
        require(isinstance(evidence, dict) and set(evidence) <= allowed
                and evidence.get("ordinary_chat") is True and evidence.get("write_supported") is True,
                "INVALID_QUALIFICATION", "Qualification must describe an observed ordinary-Chat write trial")
        for name in ("model", "client", "probe_description", "observed_at"):
            require(isinstance(evidence.get(name), str) and 0 < len(evidence[name]) <= 4000,
                    "INVALID_QUALIFICATION", "Qualification is missing an explicit observation", field=name)
        require(isinstance(actor, str) and 0 < len(actor) <= 200, "INVALID_QUALIFICATION", "Operator identity is required")
        record = {"qualification_id": "qualification_" + uuid.uuid4().hex,
                  "instance_id": self.store.identity["instance_id"], "actor": actor,
                  "created": utc_now(), "expires": utc_now() + 30 * 86400, "evidence": evidence,
                  "billing_verified": False, "interpretation": "operator-recorded capability, not a universal account entitlement"}
        atomic_write(self.store.home / "client-qualification.json", canonical(record))
        with self.store.transaction() as conn:
            self.store.event(conn, "client.qualified", "", {"qualification_id": record["qualification_id"], "actor": actor})
        return record
