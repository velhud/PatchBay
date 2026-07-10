"""Stable coordination identities for PatchBay Hub V2.

These identifiers are coordination references inside one authenticated operator
trust domain. They are not a multi-tenant authorization system.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from patchbay.protocol.context import ANONYMOUS_CLIENT_REF, RequestContext


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")


def new_ref(prefix: str) -> str:
    normalized = _normalize_prefix(prefix)
    return f"{normalized}_{secrets.token_hex(12)}"


def stable_ref(prefix: str, *parts: str, salt: str) -> str:
    normalized = _normalize_prefix(prefix)
    body = json.dumps([str(part or "") for part in parts], separators=(",", ":"), ensure_ascii=True)
    digest = hmac.new(salt.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{normalized}_{digest[:24]}"


def validate_ref(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if not _REF_RE.fullmatch(text):
        raise ValueError(f"Invalid {field}")
    return text


def _normalize_prefix(prefix: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", str(prefix or "").strip().lower()).strip("_")
    if not value or not value[0].isalpha():
        raise ValueError("Identity prefix must begin with a letter")
    return value[:24]


@dataclass(frozen=True)
class ManagerIdentity:
    principal_ref: str
    conversation_ref: str
    transport_ref: str
    work_run_ref: str

    @classmethod
    def from_request(cls, context: RequestContext | None, *, principal_ref: str) -> "ManagerIdentity":
        principal = validate_ref(principal_ref, field="principal_ref")
        request = context or RequestContext.anonymous()
        conversation = str(request.chatgpt_session_ref or "").strip()
        transport = str(request.client_ref or ANONYMOUS_CLIENT_REF).strip()
        work_run = str(request.work_run_ref or "").strip()
        return cls(
            principal_ref=principal,
            conversation_ref=conversation if conversation and conversation != ANONYMOUS_CLIENT_REF else "",
            transport_ref=transport if transport and transport != ANONYMOUS_CLIENT_REF else "",
            work_run_ref=work_run,
        )

    @property
    def participant_ref(self) -> str:
        return self.conversation_ref or self.transport_ref or self.principal_ref

    def public_metadata(self) -> dict[str, str]:
        result = {"principal_ref": self.principal_ref, "participant_ref": self.participant_ref}
        if self.conversation_ref:
            result["conversation_ref"] = self.conversation_ref
        if self.transport_ref:
            result["transport_ref"] = self.transport_ref
        if self.work_run_ref:
            result["work_run_ref"] = self.work_run_ref
        return result


@dataclass(frozen=True)
class EdgeIdentity:
    machine_id: str
    edge_generation: str

    def __post_init__(self) -> None:
        validate_ref(self.machine_id, field="machine_id")
        validate_ref(self.edge_generation, field="edge_generation")

    @property
    def ref(self) -> str:
        return f"{self.machine_id}@{self.edge_generation}"


@dataclass(frozen=True)
class WorkspaceProjectionIdentity:
    workspace_ref: str
    machine_id: str
    edge_generation: str
    projection_ref: str

    @classmethod
    def create(
        cls,
        *,
        workspace_ref: str,
        machine_id: str,
        edge_generation: str,
        local_identity: str,
        salt: str,
    ) -> "WorkspaceProjectionIdentity":
        workspace = validate_ref(workspace_ref, field="workspace_ref")
        machine = validate_ref(machine_id, field="machine_id")
        generation = validate_ref(edge_generation, field="edge_generation")
        projection = stable_ref("wsp", workspace, machine, generation, local_identity, salt=salt)
        return cls(workspace, machine, generation, projection)


@dataclass(frozen=True)
class FleetWorkerIdentity:
    machine_id: str
    edge_generation: str
    edge_worker_id: str
    fleet_worker_ref: str

    @classmethod
    def create(
        cls,
        *,
        machine_id: str,
        edge_generation: str,
        edge_worker_id: str,
        salt: str,
    ) -> "FleetWorkerIdentity":
        machine = validate_ref(machine_id, field="machine_id")
        generation = validate_ref(edge_generation, field="edge_generation")
        worker = str(edge_worker_id or "").strip()
        if not worker:
            raise ValueError("edge_worker_id is required")
        fleet_ref = stable_ref("fworker", machine, generation, worker, salt=salt)
        return cls(machine, generation, worker, fleet_ref)


def canonical_target_hash(target: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(target), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
