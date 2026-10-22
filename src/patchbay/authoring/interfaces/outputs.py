"""Versioned model-visible result contracts, validated before MCP serialization.

    Extensible diagnostic dictionaries are confined to named policy/evidence
    fields. Every result declares its identity and lifecycle/coverage fields.
"""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())


class Operation(Output):
    id: str
    task_id: str
    kind: str
    revision: int | None
    state: str
    phase: str
    created: float
    updated: float
    cancel_requested: int
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    request_operation_id: str | None = None
    recovery: str | None = None
    request_id: str | None = None
    admission: dict[str, Any] | None = None
    result_partial: bool = False
    error_partial: bool = False
    result_bytes: int | None = None
    error_bytes: int | None = None
    detail_read: dict[str, Any] | None = None


class Task(Output):
    id: str
    repository_id: str
    goal: str
    origin_commit: str
    current_commit: str
    current_revision: int
    policy_digest: str
    policy: dict[str, Any]
    dependencies: list[dict[str, Any]]
    status: str
    revision: dict[str, Any]
    created: float
    updated: float
    recovery: dict[str, Any] | None = None


class TaskSummary(Output):
    id: str
    repository_id: str
    goal: str
    current_commit: str
    current_revision: int
    status: str
    updated: float
    goal_truncated: bool = False
    goal_bytes: int | None = None


class TaskPage(Output):
    tasks: list[TaskSummary]
    next_offset: int | None
    cursor: str
    watermark: int


class Capabilities(Output):
    name: str
    version: str
    schema_version: int
    artifact_round_trip: bool
    immutable_candidates: bool
    durable_checks: bool
    execution_platform: str
    strict_backend: str = Field(description="Supported strict backend, not evidence that it is installed or was used. Inspect the exact check receipt environment for the executed backend.")
    model_delegation: bool
    mcp_write_actions: bool
    qualification: dict[str, Any] | None
    catalog: dict[str, str]
    trusted_local_is_a_sandbox: bool
    account_usage_visibility: str
    client_write_qualification: str
    default_branch_mutation: bool
    automatic_merge: bool
    pull_request_creation: bool
    recognized_package_inbox: bool
    watched_folder: str
    recovery_contract: int
    controller_continuation: str
    unsupported: list[str]


class Source(Output):
    path: str
    commit: str
    sha256: str
    text: str | None = None
    binary: bool = False
    context_id: str | None = None
    draft_id: str | None = None
    version: int | None = None
    start_line: int | None = None
    total_lines: int | None = None
    next_start_line: int | None = None
    bytes: int | None = None
    next_action: str | None = None
    tree: str | None = None
    blob_oid: str | None = None
    mode: str | None = None
    producer_complete: bool | None = None
    next_read: dict[str, Any] | None = None
    content_encoding: str | None = None
    data_base64: str | None = None
    byte_offset: int | None = None
    returned_bytes: int | None = None
    chunk_sha256: str | None = None
    next_byte_offset: int | None = None
    has_more: bool | None = None
    window_covers_full_file: bool | None = None


class Match(Output):
    path: str
    line: int
    text: str
    truncated: bool


class Search(Output):
    context_id: str
    commit: str
    matches: list[Match]
    scanned_files: int
    scanned_bytes: int
    partial: bool
    next_offset: int | None
    cursor_contract: str


class Feedback(Output):
    task_id: str
    revision: int
    commit: str
    tree: str
    policy_digest: str
    classification: str
    receipt_sha: str | None
    receipt: dict[str, Any] | None
    verification_receipt: dict[str, Any]
    evidence_provenance: dict[str, Any]
    next_action: str
    operation: Operation | None = None
    check_results: list[dict[str, Any]] | None = None
    next_offset: int | None = None
    total_check_results: int | None = None
    historical_classification: str | None = None
    evidence_current: bool | None = None


class Lease(Output):
    task_id: str
    token: str
    epoch: int
    expires: float
    label: str
    request_operation_id: str


class Candidate(Output):
    task_id: str
    revision: int
    commit: str
    tree: str
    operation_id: str
    duplicate: bool
    checks_operation_id: str | None = None


class Draft(Output):
    task_id: str
    draft_id: str
    base_revision: int
    version: int
    commit: str
    tree: str
    accepted_revision: bool


class Sealed(Candidate):
    draft_id: str
    version: int


class Context(Output):
    context_id: str
    revision: int
    blob_sha: str
    inventory_sha: str
    download: str
    request_operation_id: str
    identity_only: bool = False
    inventory_format_version: int = 1


class Log(Output):
    offset: int
    next_offset: int
    has_more: bool
    bytes: int
    text: str
    sha256: str | None = None
    observed_bytes: int | None = None
    truncated: bool | None = None


class RecoveryCandidate(Output):
    revision: int
    commit: str
    tree: str
    base_commit: str
    context_id: str | None = Field(description="Deprecated provenance alias for source_context_id; never use it as the current candidate source selector.")
    source_context_id: str | None = Field(description="Context against which this revision was authored; may pin an older revision.")
    source_context: dict[str, Any] | None
    candidate_context_id: str | None = Field(description="Existing context pinned to this exact candidate revision and commit. Null means an authorized writer must export it; recovery never creates one.")
    candidate_context: dict[str, Any] | None


class Recovery(Output):
    format_version: int
    workflow: str
    task_id: str
    repository_id: str
    goal: str
    policy_digest: str
    candidate: RecoveryCandidate
    freshness: dict[str, Any]
    operations: list[Operation]
    active_operations: list[Operation]
    operation_count: int
    operations_partial: bool
    active_operation_count: int
    active_operations_partial: bool
    state: str
    required_judgment: str
    safe_reads: list[dict[str, Any]]
    automatic_model_continuation: bool
    merge_authorized: bool
    scope: dict[str, list[str]]
    policy: dict[str, Any]
    dependencies: list[dict[str, Any]]
    lease: dict[str, Any] | None
    contexts: list[dict[str, Any]]
    drafts: list[dict[str, Any]]
    verification: dict[str, Any] | None
    approvals: list[dict[str, Any]]
    publications: list[dict[str, Any]]
    controls: dict[str, Any]


class Operations(Output):
    task_id: str
    operations: list[Operation]
    total: int
    partial: bool
    next_offset: int | None
    cursor: str
    watermark: int
    traversal: str


class Preview(Output):
    task_id: str
    revision: int
    commit: str
    tree: str
    policy_digest: str
    receipt_sha: str
    branch: str
    base_branch: str
    publication_ready: bool
    merge_authorized: bool
    remote_head: str | None
    target_head: str | None
    remote_effects: str | None = None


class Published(Output):
    task_id: str
    revision: int
    commit: str
    tree: str
    branch: str
    pushed: bool
    ci: dict[str, Any]
    merged: bool
    receipt_sha: str
    pull_request: dict[str, Any] | None
    reconciled_previous_attempt: bool = False
    historical: bool = False


class Approval(Preview):
    approval_id: str
    actor: str


class Export(Output):
    task_id: str
    revision: int
    classification: str
    blob_sha: str
    context_id: str
    continuation: str
    request_operation_id: str
    download: str | None = None


CONTRACTS = {
    "author_capabilities": Capabilities, "author_list_tasks": TaskPage,
    "author_get_task": Task, "author_read_file": Source, "author_search": Search,
    "author_get_feedback": Feedback, "author_operation_status": Operation,
    "author_create_task": Task, "author_claim_task": Lease, "author_submit_package": Candidate,
    "author_apply_edits": Draft, "author_seal_draft": Sealed, "author_run_checks": Operation,
    "author_cancel_operation": Operation, "author_export_context": Context,
    "author_recover_task": Recovery, "author_recent_operations": Operations,
    "author_read_log": Log, "author_export_feedback": Export,
    "author_publication_preview": Preview, "author_approve_publication": Approval,
    "author_publish": Published,
}
