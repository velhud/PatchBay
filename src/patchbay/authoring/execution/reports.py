"""Deterministic bounded report parsing; author claims never create pass receipts."""
from __future__ import annotations

import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

from ..errors import AuthorError, require
from ..models import Check, Profile
from ..util import safe_path


def report_path(workspace: Path, value: str) -> Path:
    safe_path(value)
    require(value.startswith(".author-reports/"), "INVALID_REQUEST", "Reports must be in the reserved report directory")
    path = workspace / value
    require(not any(parent.is_symlink() for parent in [path, *path.parents] if parent.is_relative_to(workspace)),
            "EVIDENCE_INVALID", "Report path contains a symlink")
    require(path.parent.resolve().is_relative_to(workspace.resolve()), "EVIDENCE_INVALID", "Report escaped its workspace")
    return path


def prepare_report(workspace: Path, check: Check) -> None:
    if check.report_path:
        path = report_path(workspace, check.report_path)
        if path.exists():
            require(path.is_file(), "EVIDENCE_INVALID", "Report destination is not a regular file")
            path.unlink()


def evaluate(store, profile: Profile, check: Check, workspace: Path, raw: dict) -> dict:
    result = {"id": check.id, "required": check.required, "argv": check.argv,
              "report_kind": check.report, **raw, "verification": "incomplete"}
    if raw.get("cleanup", {}).get("state") not in {"proven", "not_required"}:
        result.update(reason="cleanup_unresolved", failure_kind="EXECUTION_UNKNOWN")
        return result
    if raw["state"] != "completed":
        code = raw.get("error", {}).get("code")
        kind_map = {"MISSING_EXECUTABLE": "MISSING_EXECUTABLE", "DOCKER_UNAVAILABLE": "DOCKER_UNAVAILABLE",
                    "EXECUTABLE_NOT_RUNNABLE": "EXECUTABLE_NOT_RUNNABLE",
                    "PID_LIMIT_EXCEEDED": "PID_LIMIT_EXCEEDED",
                    "OOM_KILLED": "OOM_KILLED", "DISK_EXHAUSTED": "DISK_EXHAUSTED",
                    "TERMINATED_137_CAUSE_UNKNOWN": "TERMINATION_CAUSE_UNKNOWN",
                    "DISK_BUDGET_EXCEEDED": "DISK_BUDGET_EXCEEDED",
                    "DISK_ACCOUNTING_INCOMPLETE": "DISK_ACCOUNTING_INCOMPLETE"}
        result.update(reason=code or raw["state"], failure_kind=kind_map.get(code, "CHECK_EXECUTION_FAILED"))
        return result
    if raw["exit_code"] != 0:
        if raw.get("signal") == 9 and not raw.get("error"):
            result.update(verification="failed", reason="signal_9_cause_unknown",
                          failure_kind="TERMINATION_CAUSE_UNKNOWN")
        else:
            result.update(verification="failed", reason="nonzero_exit", failure_kind="CHECK_EXIT_NONZERO")
    elif check.report == "exit":
        result["verification"] = "passed"
        return result
    if check.report != "junit":
        return result
    try:
        path = report_path(workspace, check.report_path)
        require(path.is_file(), "EVIDENCE_MISSING", "Expected fresh test report was not produced")
        require(path.stat().st_size <= profile.report_bytes, "RESOURCE_LIMIT", "Test report exceeds the bounded parser budget")
        data = path.read_bytes()
        require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(), "EVIDENCE_INVALID", "DTD/entity declarations are not accepted in test evidence")
        root = ET.fromstring(data)
        cases = list(root.iter("testcase"))
        counts = {"tests": len(cases), "failed": sum(c.find("failure") is not None for c in cases),
                  "errors": sum(c.find("error") is not None for c in cases),
                  "skipped": sum(c.find("skipped") is not None for c in cases)}
        require(root.tag in {"testsuite", "testsuites"}, "EVIDENCE_INVALID", "Expected a JUnit testsuite root")
        result["test_counts"] = counts
        result["report_sha256"] = store.blobs.put(data)
        failures = []
        for case in cases:
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is not None and len(failures) < 30:
                    failures.append({"test": case.get("name", "")[:500], "class": case.get("classname", "")[:500],
                                     "kind": kind, "message": node.get("message", "")[:2000],
                                     "text": (node.text or "")[:6000]})
        result["failures"] = failures
        if raw["exit_code"] != 0 or counts["failed"] or counts["errors"]:
            result.update(verification="failed", reason="test_failure", failure_kind="TEST_FAILURE")
        elif counts["tests"] == 0:
            result.update(verification="incomplete", reason="zero_tests", failure_kind="ZERO_TESTS")
        elif counts["tests"] < check.min_tests:
            result.update(verification="incomplete", reason="insufficient_collected_tests", failure_kind="INSUFFICIENT_TESTS")
        elif counts["skipped"] and not check.allow_skipped:
            result.update(verification="incomplete", reason="skipped_tests_not_qualified", failure_kind="SKIPPED_TESTS")
        else:
            result["verification"] = "passed"
    except (AuthorError, OSError, ET.ParseError, ValueError) as exc:
        # Keep a nonzero exit as failed even when the report parser also fails.
        result["verification"] = "failed" if raw["exit_code"] != 0 else "incomplete"
        code = getattr(exc, "code", None)
        if isinstance(exc, ET.ParseError):
            result.update(reason="malformed_report", failure_kind="REPORT_PARSE_ERROR")
        elif code == "EVIDENCE_MISSING":
            result.update(reason="missing_report", failure_kind="REPORT_MISSING")
        elif code == "RESOURCE_LIMIT":
            result.update(reason="report_size_limit", failure_kind="REPORT_RESOURCE_LIMIT")
        else:
            result.update(reason=code or "EVIDENCE_INVALID", failure_kind="REPORT_INVALID")
        result["report_error"] = str(exc)[:2000]
    return result
