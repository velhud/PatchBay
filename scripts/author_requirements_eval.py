#!/usr/bin/env python3
"""Evaluate evidence against the source-pinned 52-case reliability register.

This tool preserves source IDs, titles, status, and ordinary-Chat requirement
verbatim. ``evaluated_status`` is this evaluator's evidence disposition; it is
not a replacement for the register's ``status`` field.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "docs/authoring/reliability-acceptance-tests.json"
REGISTER_SHA256 = "f05b0ec4c533f5c119237ad05ad9d96d16506014b19a35abad9b6b574ab58600"
STATUSES = {"PASS", "FAIL", "NOT_RUN", "BLOCKED_ENVIRONMENT"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_commit() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def load_register() -> tuple[list[dict[str, Any]], str]:
    raw = REGISTER.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != REGISTER_SHA256:
        raise ValueError(f"source-pinned reliability register hash mismatch: {digest}")
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) != 52:
        raise ValueError("source-pinned reliability register must contain 52 cases")
    expected_ids = [f"T{i:02}" for i in range(1, 53)]
    if [row.get("id") for row in data] != expected_ids:
        raise ValueError("reliability case IDs differ from the source-pinned T01-T52 sequence")
    for row in data:
        if not isinstance(row.get("title"), str) or not isinstance(row.get("requires_actual_ordinary_chat"), bool):
            raise ValueError(f"malformed source case {row.get('id')}")
        if row.get("status") != "NOT_RUN_IN_THIS_RESEARCH":
            raise ValueError(f"unexpected source status for {row['id']}: {row.get('status')!r}")
    return data, digest


def evaluate(evidence_path: Path | None) -> dict[str, Any]:
    register, register_digest = load_register()
    commit = current_commit()
    supplied: dict[str, Any] = {}
    errors: list[str] = []
    if evidence_path:
        try:
            raw = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("runs"), dict):
                raise ValueError("expected schema_version=1 and a runs object")
            supplied = raw["runs"]
            known = {row["id"] for row in register}
            unknown = sorted(set(supplied) - known)
            if unknown:
                errors.append("unknown source case IDs: " + ", ".join(unknown))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"invalid evidence file: {exc}")
            supplied = {}

    results: list[dict[str, Any]] = []
    for source in register:
        case_id = source["id"]
        run = supplied.get(case_id)
        evaluated_status = "NOT_RUN"
        reasons = ["no evidence record supplied"]
        if run is not None:
            if not isinstance(run, dict):
                evaluated_status, reasons = "FAIL", ["run record must be an object"]
            else:
                requested = run.get("status")
                if requested not in STATUSES:
                    evaluated_status, reasons = "FAIL", ["status must be PASS, FAIL, NOT_RUN, or BLOCKED_ENVIRONMENT"]
                else:
                    evaluated_status = requested
                    reasons = []
                if evaluated_status == "PASS":
                    if run.get("commit") != commit or commit == "UNKNOWN":
                        evaluated_status = "FAIL"
                        reasons.append("PASS evidence is not pinned to current Git HEAD")
                    if not str(run.get("command") or "").strip():
                        evaluated_status = "FAIL"
                        reasons.append("PASS evidence requires the exact command")
                    if source["requires_actual_ordinary_chat"]:
                        if run.get("execution_surface") != "ordinary_chat":
                            evaluated_status = "FAIL"
                            reasons.append("source case requires observed ordinary_chat execution_surface")
                        if not str(run.get("profile_id") or "").strip():
                            evaluated_status = "FAIL"
                            reasons.append("source case requires an explicit profile_id")
                    artifacts = run.get("artifacts")
                    if not isinstance(artifacts, list) or not artifacts:
                        evaluated_status = "FAIL"
                        reasons.append("PASS evidence requires at least one artifact with verified SHA-256")
                    else:
                        for artifact in artifacts:
                            if not isinstance(artifact, dict) or not artifact.get("path"):
                                evaluated_status = "FAIL"
                                reasons.append("artifact entries require a path and sha256")
                                continue
                            path = Path(str(artifact["path"]))
                            if not path.is_absolute():
                                path = evidence_path.parent / path if evidence_path else ROOT / path
                            try:
                                actual = sha256_file(path)
                            except OSError:
                                actual = ""
                            if not actual or actual != artifact.get("sha256"):
                                evaluated_status = "FAIL"
                                reasons.append(f"artifact missing or hash mismatch: {artifact['path']}")
                elif evaluated_status in {"FAIL", "BLOCKED_ENVIRONMENT", "NOT_RUN"}:
                    reasons = [str(run.get("reason") or "recorded outcome")]
        results.append({**source, "evaluated_status": evaluated_status,
                        "evaluated_status_scope": "evidence_record_validation",
                        "evaluation_reasons": reasons})

    counts = {name: sum(row["evaluated_status"] == name for row in results) for name in STATUSES}
    overall = "FAIL" if errors or counts["FAIL"] else (
        "INCOMPLETE" if counts["NOT_RUN"] or counts["BLOCKED_ENVIRONMENT"] else "PASS"
    )
    return {
        "schema_version": 2,
        "evaluator": "patchbay.author-requirements.v2",
        "candidate_commit": commit,
        "validation_scope": "record_structure_commit_pin_artifact_hashes",
        "candidate_binding": "git_head_only",
        "independent_semantic_acceptance": False,
        "claim_relevance_review": "required_separately",
        "installed_or_loaded_bytes_observed": False,
        "client_observation_performed_by_evaluator": False,
        "register_path": REGISTER.relative_to(ROOT).as_posix(),
        "register_sha256": register_digest,
        "overall": overall,
        "counts": counts,
        "errors": errors,
        "scenarios": results,
        "limitations": [
            "The source case status and title are preserved verbatim; evaluated_status is a separate evidence disposition.",
            "A PASS validates evidence fields, current commit pin, and artifact hashes; it does not independently reproduce external execution.",
            "Ordinary Chat cases require actual ordinary Chat evidence. Static rules and byte counts do not test model judgment.",
            "A missing evidence record remains NOT_RUN; an environment blocker is not a pass.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="JSON evidence record to audit; omitted means all unrun")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    try:
        report = evaluate(args.evidence)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"requirements evaluation failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['evaluator']}: {report['overall']} ({report['candidate_commit']})")
        for item in report["scenarios"]:
            print(f"[{item['evaluated_status']}] {item['id']} {item['title']}: {'; '.join(item['evaluation_reasons'])}")
        for error in report["errors"]:
            print(f"ERROR: {error}")
    return 1 if report["overall"] in {"FAIL", "INCOMPLETE"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
