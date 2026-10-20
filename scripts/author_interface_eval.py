#!/usr/bin/env python3
"""Measure deterministic Hub/Author interface baselines and captured A/B data.

Byte lengths are UTF-8 serialized source/capture sizes. They do not estimate
tokens, model cost, selection quality, or qualification. Synthetic Author
profiles exercise schema visibility only; they do not initialize an Author
service, worker, task, or live qualification state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from patchbay.hub.tool_surface import HUB_V2_TOOLS  # noqa: E402
from patchbay.authoring.interfaces.mcp_legacy import CATALOG as AUTHOR_CATALOG  # noqa: E402
from patchbay.authoring.interfaces.instructions import READ_ONLY, WRITABLE  # noqa: E402
from patchbay.authoring.interfaces.outputs import CONTRACTS as AUTHOR_OUTPUTS  # noqa: E402

INSTRUCTIONS = ROOT / "docs/user/chatgpt-instructions.md"
GOLDEN = ROOT / "docs/authoring/golden-prompts.json"
AUTHOR_CATALOG_VERSION = "author-v2"
AUTHOR_OUTPUT_PROFILE = "structured-summary-v2"
VARIANTS = {"full-text+structured", "concise+structured"}
AUTHOR_ROLE_VISIBILITY = {
    "reader": {"qualified": False, "operator_only": False},
    "operator": {"qualified": False, "operator_only": True},
    "author": {"qualified": False, "operator_only": False},
    "operator-qualified-synthetic": {"qualified": True, "operator_only": True},
    "author-qualified-synthetic": {"qualified": True, "operator_only": False},
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _author_tools(profile: str) -> list[dict[str, Any]]:
    rules = AUTHOR_ROLE_VISIBILITY[profile]
    result = []
    for name, input_model, description, write in AUTHOR_CATALOG:
        if write and (not rules["qualified"] or profile == "reader"):
            continue
        if name in {"author_approve_publication", "author_publish"} and not rules["operator_only"]:
            continue
        result.append({
            "name": name,
            "description": description,
            "inputSchema": input_model.model_json_schema(),
            "outputSchema": AUTHOR_OUTPUTS[name].model_json_schema(),
            "annotations": {
                "readOnlyHint": not write,
                "destructiveHint": write,
                "idempotentHint": True,
                "openWorldHint": name in {"author_submit_package", "author_run_checks"},
            },
        })
    return result


def author_profiles() -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    instruction_profiles = {"reader": READ_ONLY, "operator": READ_ONLY, "author": READ_ONLY,
                            "operator-qualified-synthetic": WRITABLE,
                            "author-qualified-synthetic": WRITABLE}
    for profile, instructions in instruction_profiles.items():
        tools = _author_tools(profile)
        list_response = {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}
        initialize_response = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "patchbay-author", "version": "0.1.0"},
                               "instructions": instructions}
        outputs = {tool["name"]: tool["outputSchema"] for tool in tools}
        # Explicit schema-only examples keep result-envelope measurement
        # deterministic without calling a tool or fabricating runtime results.
        result_profile = {"content": [{"type": "text", "text": "<runtime summary omitted>"}],
                          "structuredContent": "<runtime payload conforms to outputSchema>", "isError": False}
        request_profile = {"jsonrpc": "2.0", "id": "synthetic", "method": "tools/call",
                           "params": {"name": "<one listed tool>", "arguments": "<inputSchema>"}}
        profiles[profile] = {
            "profile_input": {"role": profile.split("-qualified")[0],
                              "qualification": "synthetic_fixture" if "synthetic" in profile else "none",
                              "live_qualified": False},
            "tool_list": {"count": len(tools), "serialized_utf8_bytes": len(canonical(list_response)),
                          "sha256": digest(canonical(list_response)), "tools": tools},
            "instructions": {"utf8_bytes": len(instructions.encode("utf-8")), "sha256": digest(instructions.encode("utf-8"))},
            "initialize_result": {"serialized_utf8_bytes": len(canonical(initialize_response)),
                                  "sha256": digest(canonical(initialize_response))},
            "request_profile": {"serialized_utf8_bytes": len(canonical(request_profile)),
                                "sha256": digest(canonical(request_profile))},
            "result_profile": {"serialized_utf8_bytes": len(canonical(result_profile)),
                               "sha256": digest(canonical(result_profile))},
            "output_schemas": {"count": len(outputs), "serialized_utf8_bytes": len(canonical(outputs)),
                               "sha256": digest(canonical(outputs))},
            "evidence_class": "deterministic_synthetic_schema_profile",
        }
    return profiles


def _load_capture(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    records = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"capture JSONL line {line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"capture JSONL line {line_no} must be an object")
        if not isinstance(row.get("case_id"), str) or not row["case_id"].strip():
            raise ValueError(f"capture JSONL line {line_no} requires case_id")
        if row.get("variant") not in VARIANTS:
            raise ValueError(f"capture JSONL line {line_no} variant must be one of {sorted(VARIANTS)}")
        if row.get("status") not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
            raise ValueError(f"capture JSONL line {line_no} requires an explicit trial status")
        for field in ("input_raw", "output_raw"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"capture JSONL line {line_no} requires raw UTF-8 string {field}")
        tool_ids = row.get("tool_ids", [])
        required = row.get("required_tool_ids")
        if not isinstance(tool_ids, list) or not all(isinstance(item, str) for item in tool_ids):
            raise ValueError(f"capture JSONL line {line_no} tool_ids must be a string list")
        if required is not None and (not isinstance(required, list) or not all(isinstance(item, str) for item in required)):
            raise ValueError(f"capture JSONL line {line_no} required_tool_ids must be a string list")
        records.append(row)
    return records


def compare_ab(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "NOT_RUN", "actual_chat_outcomes": "NOT_RUN", "records": 0, "cases": [],
                "limitations": ["No actual ordinary-Chat capture supplied."]}
    records = _load_capture(path)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    known_cases = {case["id"] for case in golden["cases"]}
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in records:
        if row["case_id"] not in known_cases:
            raise ValueError(f"unknown golden prompt case ID: {row['case_id']}")
        variant = row["variant"]
        case = grouped.setdefault(row["case_id"], {})
        if variant in case:
            raise ValueError(f"duplicate case/variant capture: {row['case_id']} {variant}")
        case[variant] = row
    cases = []
    for case_id, variants in sorted(grouped.items()):
        paired = all(variant in variants for variant in VARIANTS)
        result: dict[str, Any] = {"case_id": case_id, "paired": paired, "variants": {}}
        for variant in sorted(VARIANTS):
            row = variants.get(variant)
            if row is None:
                result["variants"][variant] = {"status": "NOT_RUN", "input_bytes": None, "output_bytes": None,
                                               "tool_ids": [], "coverage": None}
                continue
            expected = row.get("required_tool_ids")
            observed_ids = sorted(set(row.get("tool_ids", [])))
            result["variants"][variant] = {
                "status": row["status"],
                "input_bytes": len(row["input_raw"].encode("utf-8")),
                "output_bytes": len(row["output_raw"].encode("utf-8")),
                "tool_ids": observed_ids,
                "coverage": ({"required": sorted(set(expected)),
                              "observed_required": sorted(set(expected) & set(observed_ids)),
                              "missing_required": sorted(set(expected) - set(observed_ids)),
                              "fraction": (len(set(expected) & set(observed_ids)) / len(set(expected))) if expected else 1.0}
                             if expected is not None else None),
                "ordinary_chat": row.get("execution_surface") == "ordinary_chat",
                "profile_id": row.get("profile_id"),
            }
        a = result["variants"]["full-text+structured"]
        b = result["variants"]["concise+structured"]
        result["status_match"] = paired and a["status"] == b["status"]
        result["coverage_comparable"] = paired and a["coverage"] is not None and b["coverage"] is not None
        cases.append(result)
    actual = bool(cases) and all(
        case["paired"]
        and all(
            item["ordinary_chat"] and item["profile_id"]
            for item in case["variants"].values()
        )
        for case in cases
    )
    status = "CAPTURED" if actual else ("PARTIAL_CAPTURE" if records else "NOT_RUN")
    return {"status": status, "actual_chat_outcomes": "CAPTURED" if actual else "NOT_RUN",
            "records": len(records), "capture_sha256": digest(path.read_bytes()), "cases": cases,
            "limitations": ["Mechanical status, tool-ID coverage, and UTF-8 byte comparisons do not score model judgment.",
                            "Byte counts are not token counts, token costs, or evidence for model selection.",
                            "Every included case needs both variants observed in ordinary Chat with profile IDs; incomplete pairs remain NOT_RUN."]}


def evaluate(capture: Path | None) -> dict[str, Any]:
    hub_catalog = canonical({"jsonrpc": "2.0", "id": 1, "result": {"tools": HUB_V2_TOOLS}})
    output_contracts = {str(tool["name"]): tool.get("outputSchema", {}) for tool in HUB_V2_TOOLS}
    output_contract_bytes = canonical(output_contracts)
    instructions = INSTRUCTIONS.read_bytes()
    corpus = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return {
        "schema_version": 2,
        "evaluator": "patchbay.interface-baseline.v2",
        "measured": {
            "hub": {
                "catalog": {"profile": "hub_v2_complete", "tools": len(HUB_V2_TOOLS),
                            "serialized_utf8_bytes": len(hub_catalog), "sha256": digest(hub_catalog)},
                "instructions": {"path": INSTRUCTIONS.relative_to(ROOT).as_posix(),
                                 "utf8_bytes": len(instructions), "sha256": digest(instructions)},
                "output_contracts": {"tools": len(output_contracts), "serialized_utf8_bytes": len(output_contract_bytes),
                                     "sha256": digest(output_contract_bytes)},
            },
            "author": {"catalog_version": AUTHOR_CATALOG_VERSION, "output_profile": AUTHOR_OUTPUT_PROFILE,
                       "profiles": author_profiles(),
                       "source_paths": ["src/patchbay/authoring/interfaces/mcp_legacy.py",
                                        "src/patchbay/authoring/interfaces/instructions.py",
                                        "src/patchbay/authoring/interfaces/outputs.py"]},
            "ab_capture": compare_ab(capture),
            "golden_prompt_cases": len(corpus["cases"]),
            "golden_prompt_sha256": digest(canonical(corpus)),
        },
        "limitations": [
            "Hub catalog bytes are canonical JSON for the checked-in complete registry, not a host envelope or token count.",
            "Author profiles use deterministic synthetic role/qualification inputs; no live qualification, service, task, or worker is created.",
            "A/B input/output bytes measure supplied raw UTF-8 strings only; they do not prove token usage, cost, correctness, or model choice.",
            "Actual ordinary-Chat outcomes remain NOT_RUN unless all supplied records attest ordinary_chat and provide profile IDs.",
            "The frozen prompt corpus requires human semantic scoring; static rules and tool-ID coverage do not establish model judgment.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", "--traces", dest="capture", type=Path,
                        help="JSONL ordinary-Chat A/B capture with both variants and raw inputs/outputs")
    parser.add_argument("--json", action="store_true", help="Print JSON (always enabled)")
    args = parser.parse_args()
    try:
        report = evaluate(args.capture)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"interface evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
