"""Versioned task authority and execution profiles; candidate code cannot edit them."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .util import safe_path


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    def wire(self) -> dict:
        return self.model_dump(mode="json")


class ArchiveLimits(Contract):
    compressed_bytes: int = Field(128 * 1024 * 1024, ge=1024, le=2**34)
    expanded_bytes: int = Field(512 * 1024 * 1024, ge=1024, le=2**36)
    member_bytes: int = Field(128 * 1024 * 1024, ge=1024, le=2**34)
    manifest_bytes: int = Field(2 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    members: int = Field(20000, ge=1, le=1000000)


class RuntimeLimits(Contract):
    archive: ArchiveLimits = Field(default_factory=ArchiveLimits)
    max_jobs: int = Field(2, ge=1, le=128)
    cpu_cores: float = Field(2.0, gt=0, le=1024)
    memory_mb: int = Field(2048, ge=128, le=1048576)
    total_storage_bytes: int = Field(20 * 1024**3, ge=1024**2)
    minimum_free_bytes: int = Field(512 * 1024**2, ge=0)
    snapshot_bytes: int = Field(512 * 1024**2, ge=1024)
    context_inventory_paths: int = Field(100000, ge=1, le=1000000)
    context_inventory_bytes: int = Field(16 * 1024**2, ge=1024, le=128 * 1024**2)
    max_events: int = Field(10000, ge=100, le=1000000)
    lease_seconds: int = Field(86400, ge=60, le=604800)
    native_download_hosts: list[str] = Field(default_factory=lambda: ["files.oaiusercontent.com", "*.oaiusercontent.com"])


class Check(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    argv: list[str] = Field(min_length=1, max_length=1000)
    cwd: str = "."
    timeout_seconds: int = Field(3600, ge=1, le=86400)
    required: bool = True
    report: Literal["exit", "junit"] = "exit"
    report_path: str | None = None
    min_tests: int = Field(1, ge=0, le=10000000)
    min_executed: int = Field(0, ge=0, le=10000000)
    allow_skipped: bool = False

    @field_validator("argv")
    @classmethod
    def arguments(cls, values):
        if not values[0] or any("\x00" in value or len(value) > 131072 for value in values):
            raise ValueError("Invalid argument vector")
        return values

    @field_validator("cwd")
    @classmethod
    def directory(cls, value):
        return safe_path(value, allow_dot=True)

    @model_validator(mode="after")
    def reports(self):
        if self.report == "junit":
            if not self.report_path:
                raise ValueError("A JUnit check requires report_path")
            safe_path(self.report_path)
            if not self.report_path.startswith(".author-reports/"):
                raise ValueError("Reports must be under .author-reports/")
        elif self.report_path is not None:
            raise ValueError("report_path only applies to JUnit checks")
        if self.report != "junit" and self.min_executed:
            raise ValueError("min_executed requires a JUnit report")
        return self


class Profile(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    backend: Literal["docker", "trusted-local"] = "docker"
    image: str | None = None
    checks: list[Check] = Field(min_length=1, max_length=100)
    setup: list[Check] = Field(default_factory=list, max_length=100)
    setup_network: bool = False
    environment: dict[str, str] = Field(default_factory=dict)
    cpu: float = Field(1.0, gt=0, le=128)
    memory_mb: int = Field(1024, ge=64, le=1048576)
    disk_mb: int = Field(2048, ge=16, le=1048576)
    pids: int = Field(128, ge=8, le=65536)
    log_bytes: int = Field(4 * 1024**2, ge=1024, le=1024**3)
    report_bytes: int = Field(10 * 1024**2, ge=1024, le=128 * 1024**2)
    allowed_outputs: list[str] = Field(default_factory=lambda: [".author-reports", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"])

    @model_validator(mode="after")
    def coherent(self):
        ids = [check.id for check in self.checks + self.setup]
        if len(ids) != len(set(ids)):
            raise ValueError("Check IDs must be unique across setup and verification")
        if not any(check.required for check in self.checks):
            raise ValueError("At least one verification check must be required")
        for name, value in self.environment.items():
            if not name or "=" in name or "\x00" in name + value:
                raise ValueError("Invalid environment entry")
        for output in self.allowed_outputs:
            safe_path(output)
        return self


class PublicationPolicy(Contract):
    enabled: bool = False
    branch_prefix: str = "author/"
    github_repository: str | None = None
    draft_pr: bool = False
    require_target_unchanged: bool = True
    required_ci: list[str] = Field(default_factory=list)
    allow_external_ci: bool = False
    remote_effects: Literal["downstream_possible", "controlled_no_automation"] = "downstream_possible"
    signing_required: bool = False

    @model_validator(mode="after")
    def branch(self):
        if not self.branch_prefix.endswith("/") or any(c in self.branch_prefix for c in " ~^:?*[\\") or ".." in self.branch_prefix:
            raise ValueError("branch_prefix must be a valid feature-branch namespace ending in /")
        if self.draft_pr and not self.github_repository:
            raise ValueError("draft_pr needs an explicit owner/repository")
        if self.github_repository:
            import re
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repository):
                raise ValueError("Expected GitHub owner/repository")
        return self


class TaskPolicy(Contract):
    profile: Profile
    writable_paths: list[str] = Field(default_factory=lambda: ["**"], min_length=1, max_length=1000)
    blocked_paths: list[str] = Field(default_factory=lambda: [".author-reports/**"], max_length=1000)
    auto_check: bool = False
    allow_background_checks: bool = True
    publication: PublicationPolicy = Field(default_factory=PublicationPolicy)

    @model_validator(mode="after")
    def consistency(self):
        if self.auto_check and not self.allow_background_checks:
            raise ValueError("Auto validation requires durable check authority")
        for pattern in self.writable_paths + self.blocked_paths:
            if not pattern or pattern.startswith("/") or "\\" in pattern or ".." in pattern.split("/") or "\x00" in pattern:
                raise ValueError("Path scopes must be repository-relative patterns")
        return self
