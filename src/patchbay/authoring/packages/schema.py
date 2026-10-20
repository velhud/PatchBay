"""Version 1 implementation manifest. Shape validation is not semantic validation."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, StrictInt, field_validator, model_validator
from ..models import Contract
from ..util import safe_path

Hash = str
Mode = Literal["100644", "100755"]


class Operation(Contract):
    op: Literal["create", "replace", "delete", "move", "chmod"]
    path: str
    destination: str | None = None
    before_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    before_mode: Mode | None = None
    payload: str | None = None
    after_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    mode: Mode | None = None

    @model_validator(mode="after")
    def contract(self):
        safe_path(self.path)
        if self.op != "create":
            if self.before_sha256 is None or self.before_mode is None:
                raise ValueError("Existing-file operations need exact byte and mode preconditions")
        elif self.before_sha256 is not None or self.before_mode is not None:
            raise ValueError("Create requires absence, not existing-file preconditions")
        if self.op in {"create", "replace"}:
            if self.payload is None or self.after_sha256 is None or self.mode is None:
                raise ValueError("Create/replace requires payload, byte hash, and mode")
            safe_path(self.payload)
            if not self.payload.startswith("payload/"):
                raise ValueError("Payload members must be below payload/")
        elif self.payload is not None or self.after_sha256 is not None:
            raise ValueError("Only create/replace may carry replacement payloads")
        if self.op == "chmod":
            if self.mode is None or self.mode == self.before_mode:
                raise ValueError("chmod requires a different supported mode")
        elif self.op not in {"create", "replace"} and self.mode is not None:
            raise ValueError("Unexpected output mode for this operation")
        if self.op == "move":
            if self.destination is None:
                raise ValueError("Move requires destination")
            safe_path(self.destination)
            if self.destination == self.path:
                raise ValueError("Move must change path")
        elif self.destination is not None:
            raise ValueError("Only move may specify a destination")
        return self


class ReadDependency(Contract):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Mode

    @field_validator("path")
    @classmethod
    def path_contract(cls, value):
        return safe_path(value)


class Manifest(Contract):
    format_version: StrictInt = Field(1, ge=1, le=1)
    repository_id: str = Field(pattern=r"^repo_[0-9a-f]{32}$")
    task_id: str = Field(pattern=r"^task_[0-9a-f]{32}$")
    submission_id: str = Field(min_length=1, max_length=200)
    context_id: str = Field(pattern=r"^ctx_[0-9a-f]{40}$")
    expected_revision: StrictInt = Field(ge=0)
    expected_base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    git_object_format: Literal["sha1", "sha256"]
    operations: list[Operation] = Field(min_length=1, max_length=20000)
    read_dependencies: list[ReadDependency] = Field(default_factory=list, max_length=20000)
    requested_checks: list[str] = Field(default_factory=list, max_length=100)
    summary: str = Field("", max_length=20000)
    author_notes: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def identities(self):
        if len(self.expected_base_commit) != (40 if self.git_object_format == "sha1" else 64):
            raise ValueError("Base commit length does not match the declared object format")
        if len({d.path for d in self.read_dependencies}) != len(self.read_dependencies):
            raise ValueError("Read dependencies must be unique")
        if any(len(note) > 20000 for note in self.author_notes):
            raise ValueError("Author notes must remain bounded")
        return self

    def semantics(self) -> dict:
        value = self.wire()
        for field in ("submission_id", "summary", "author_notes"):
            value.pop(field, None)
        value["operations"] = sorted(value["operations"], key=lambda op: (op["path"], op["op"]))
        value["read_dependencies"] = sorted(value["read_dependencies"], key=lambda d: d["path"])
        value["requested_checks"] = sorted(set(value["requested_checks"]))
        return value
