"""Strict public input schemas; native file parameters are top-level declared objects."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, StrictInt, model_validator
from pydantic_core import PydanticCustomError
from ..models import Contract, TaskPolicy
from ..util import safe_path


class Empty(Contract):
    pass


class Page(Contract):
    limit: StrictInt = Field(25, ge=1, le=200)
    offset: StrictInt = Field(0, ge=0)
    cursor: str | None = Field(None, max_length=64)


class TaskRef(Contract):
    task_id: str = Field(pattern=r"^task_[0-9a-f]{32}$")


class RecoveryRef(TaskRef):
    """Bound recent-history size in a recovery read without hiding active work."""
    operation_limit: StrictInt = Field(20, ge=1, le=200)


class OperationRef(Contract):
    operation_id: str = Field(pattern=r"^op_[0-9a-f]{32}$")


class Register(Contract):
    name: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=4096)
    policy: TaskPolicy
    remote: str | None = Field(None, max_length=4096)
    default_branch: str | None = Field(None, max_length=500)
    key: str | None = Field(None, min_length=1, max_length=200)


class RefreshRepository(Contract):
    repository: str
    key: str = Field(min_length=1, max_length=200)
    from_remote: bool = False


class InboxScan(Contract):
    folder: str = Field(min_length=1, max_length=4096)
    stability_seconds: float = Field(1.0, ge=0, le=30)
    max_files: StrictInt = Field(25, ge=1, le=25)
    offset: StrictInt = Field(0, ge=0, le=10000)


class InboxImport(Contract):
    path: str = Field(min_length=1, max_length=4096)
    key: str = Field(min_length=1, max_length=200)


class InboxGrant(Contract):
    task_id: str = Field(pattern=r"^task_[0-9a-f]{32}$")
    token: str = Field(min_length=16, max_length=512)
    epoch: StrictInt = Field(ge=1)
    auto_validate: bool
    key: str = Field(min_length=1, max_length=200)


class CreateTask(Contract):
    repository: str
    goal: str = Field(min_length=1, max_length=200000)
    base: str = Field("HEAD", min_length=1, max_length=500)
    key: str = Field(min_length=1, max_length=200)


class Claim(TaskRef):
    label: str = Field("author", min_length=1, max_length=200)
    token: str | None = Field(None, min_length=16, max_length=512)
    takeover: bool = False
    key: str = Field(min_length=1, max_length=200)


class Leased(TaskRef):
    token: str = Field(min_length=16, max_length=512)
    epoch: StrictInt = Field(ge=1)


class ExportContext(TaskRef):
    revision: StrictInt | None = Field(None, ge=0)
    paths: list[str] | None = Field(None, max_length=20000)
    key: str = Field(min_length=1, max_length=200)


class ReadFile(Contract):
    model_config = {"json_schema_extra": {"oneOf": [
        {"required": ["context_id"], "properties": {
            "context_id": {"type": "string"}, "draft_id": {"type": "null"}, "draft_version": {"type": "null"}}},
        {"required": ["draft_id", "draft_version"], "properties": {
            "context_id": {"type": "null"}, "draft_id": {"type": "string"}, "draft_version": {"type": "integer"}}},
    ]}}
    context_id: str | None = Field(None, pattern=r"^ctx_[0-9a-f]{40}$",
        description="Immutable context ID from task recovery or an existing export; excludes draft_id and draft_version.")
    draft_id: str | None = Field(None, pattern=r"^draft_[0-9a-f]{32}$",
        description="Draft ID from task recovery or an edit result; requires its exact draft_version and no context_id.")
    draft_version: StrictInt | None = Field(None, ge=1,
        description="Exact positive draft version from task recovery drafts[].version; not the task revision.")
    path: str = Field(min_length=1, max_length=4096, description="Normalized repository-relative path in the selected source.")
    start_line: StrictInt = Field(1, ge=1)
    max_lines: StrictInt = Field(200, ge=1, le=1000)
    max_bytes: StrictInt = Field(65536, ge=1, le=262144)

    @model_validator(mode="after")
    def selector(self):
        if (self.context_id is None) == (self.draft_id is None):
            raise PydanticCustomError("source_selector", "Select context_id alone or draft_id with exact draft_version")
        if (self.draft_id is None) != (self.draft_version is None):
            raise PydanticCustomError("source_selector", "Select context_id alone or draft_id with exact draft_version")
        safe_path(self.path)
        return self


class Search(Contract):
    context_id: str = Field(pattern=r"^ctx_[0-9a-f]{40}$")
    query: str = Field(min_length=1, max_length=1000)
    limit: StrictInt = Field(50, ge=1, le=200)
    offset: StrictInt = Field(0, ge=0)


class FeedbackPage(TaskRef):
    revision: StrictInt | None = Field(None, ge=0)
    offset: StrictInt = Field(0, ge=0)
    limit: StrictInt = Field(10, ge=1, le=50)


class NativeFile(Contract):
    download_url: str = Field(min_length=1, max_length=32768)
    file_id: str = Field(min_length=1, max_length=500)
    mime_type: str | None = Field(None, max_length=200)
    file_name: str | None = Field(None, max_length=500)


class NativeSubmission(Leased):
    key: str = Field(min_length=1, max_length=200)
    artifact_file: NativeFile


class DraftChange(Contract):
    op: Literal["create", "replace", "edit", "delete"]
    path: str
    content: str | None = Field(None, max_length=1000000)
    before_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    old_text: str | None = Field(None, max_length=1000000)
    new_text: str | None = Field(None, max_length=1000000)
    mode: Literal["100644", "100755"] | None = None


class DraftEdit(Leased):
    expected_revision: StrictInt = Field(ge=0)
    expected_version: StrictInt = Field(ge=0)
    draft_id: str | None = Field(None, pattern=r"^draft_[0-9a-f]{32}$")
    edits: list[DraftChange] = Field(min_length=1, max_length=1000)
    key: str = Field(min_length=1, max_length=200)

    def wire(self) -> dict:
        value = super().wire()
        value["edits"] = [change.model_dump(mode="json", exclude_none=True) for change in self.edits]
        return value


class DraftSeal(Contract):
    draft_id: str = Field(pattern=r"^draft_[0-9a-f]{32}$")
    expected_version: StrictInt = Field(ge=1)
    key: str = Field(min_length=1, max_length=200)
    token: str = Field(min_length=16, max_length=512)
    epoch: StrictInt = Field(ge=1)


class CheckRequest(Leased):
    revision: StrictInt = Field(ge=0)
    key: str = Field(min_length=1, max_length=200)
    requested: list[str] | None = Field(None, max_length=100)


class Cancel(OperationRef):
    token: str = Field(min_length=16, max_length=512)
    epoch: StrictInt = Field(ge=1)
    key: str = Field(min_length=1, max_length=200)


class ExportFeedback(TaskRef):
    revision: StrictInt | None = Field(None, ge=0)
    key: str = Field(min_length=1, max_length=200)


class RecentOperations(TaskRef):
    limit: StrictInt = Field(50, ge=1, le=200)
    offset: StrictInt = Field(0, ge=0)
    cursor: str | None = Field(None, max_length=64)


class ReadLog(OperationRef):
    filename: str = Field(min_length=1, max_length=100)
    offset: StrictInt = Field(0, ge=0)
    limit: StrictInt = Field(65536, ge=1, le=262144)


class PublicationPreview(TaskRef):
    revision: StrictInt = Field(ge=0)


class Approval(Leased):
    revision: StrictInt = Field(ge=0)
    actor: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=200)


class Publish(Contract):
    approval_id: str = Field(pattern=r"^approval_[0-9a-f]{32}$")
    key: str = Field(min_length=1, max_length=200)
    token: str = Field(min_length=16, max_length=512)
    epoch: StrictInt = Field(ge=1)


class Control(Contract):
    task_id: str | None = Field(None, pattern=r"^task_[0-9a-f]{32}$")
    key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)


class UpdatePolicy(TaskRef):
    policy: TaskPolicy
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
