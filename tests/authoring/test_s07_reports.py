"""PROPOSED DESIRED-BEHAVIOR REGRESSIONS; NOT RUN BY THIS AUDIT.
Scope: report parsing with controlled raw process outcomes, not real execution.
Acceptance references: S07-T01, T02, T03 and T05.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from patchbay.authoring.execution.reports import evaluate
from patchbay.authoring.models import Check, Profile


class _MemoryBlobs:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self.values[sha] = bytes(data)
        return sha


def _evaluate(tmp_path: Path, document: bytes) -> dict:
    check = Check(
        id="unit",
        argv=["python", "-m", "pytest"],  # Recorded metadata only; never executed here.
        report="junit",
        report_path=".author-reports/unit.xml",
        min_tests=1,
    )
    profile = Profile(id="s07-parser", backend="trusted-local", checks=[check])
    path = tmp_path / ".author-reports" / "unit.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(document)
    store = SimpleNamespace(blobs=_MemoryBlobs())
    raw = {"state": "completed", "exit_code": 0, "cleanup": {"state": "proven"}}
    return evaluate(store, profile, check, tmp_path, raw)


def test_supported_consistent_report_remains_passing(tmp_path: Path) -> None:
    result = _evaluate(
        tmp_path,
        b'<testsuite tests="1" failures="0" errors="0" skipped="0">'
        b'<testcase name="addition"/></testsuite>',
    )
    assert result["verification"] == "passed"
    assert result["test_counts"]["tests"] == 1


@pytest.mark.parametrize(
    "document",
    [
        b'<testsuite tests="1" failures="1"><testcase name="looks-green"/></testsuite>',
        b'<testsuite tests="1" errors="1"><testcase name="looks-green"/></testsuite>',
        b'<testsuite><error message="suite setup failed"/><testcase name="other"/></testsuite>',
    ],
)
def test_contradictory_or_unsupported_failure_cannot_be_green(
    tmp_path: Path, document: bytes
) -> None:
    result = _evaluate(tmp_path, document)
    assert result["verification"] != "passed"
    assert result.get("reason") or result.get("report_error")


def test_declared_no_dtd_rule_applies_to_utf16(tmp_path: Path) -> None:
    # A tiny benign entity, not an expansion/XXE stress fixture.
    document = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE testsuite [<!ENTITY label "small-fixture">]>'
        '<testsuite><testcase name="&label;"/></testsuite>'
    ).encode("utf-16")
    result = _evaluate(tmp_path, document)
    assert result["verification"] != "passed"
    assert result.get("reason") or result.get("report_error")
