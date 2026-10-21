"""Bounded public input diagnostics. Never serialize Pydantic input or context."""
from __future__ import annotations

from pydantic import BaseModel, ValidationError

MESSAGES = {
    "missing": "Required field is missing",
    "extra_forbidden": "An undeclared field is not allowed",
    "string_type": "Expected a string",
    "int_type": "Expected a strict integer; booleans are not integers",
    "bool_type": "Expected a boolean",
    "list_type": "Expected an array",
    "model_type": "Expected an object",
    "string_pattern_mismatch": "Value must match the field's published ID or hash pattern",
    "string_too_short": "Value is shorter than the published minimum",
    "string_too_long": "Value exceeds the published maximum",
    "greater_than_equal": "Value is below the published minimum",
    "less_than_equal": "Value exceeds the published maximum",
    "too_long": "Collection exceeds the published maximum",
    "too_short": "Collection is below the published minimum",
    "literal_error": "Use one of the published literal values",
    "source_selector": "Select non-null context_id alone, or non-null draft_id with exact positive draft_version",
}


def argument_error(contract: type[BaseModel], error: ValidationError) -> dict:
    schema = contract.model_json_schema()
    fields = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        fields.update(definition.get("properties", {}))
    errors = error.errors(include_input=False, include_url=False, include_context=False)
    projected = []
    for item in errors[:8]:
        kind = item["type"] if item["type"] in MESSAGES else "invalid_value"
        # Unknown property names are caller data too: do not echo them.
        location = [part if isinstance(part, str) and part in fields else "<item>" for part in item["loc"][:6]]
        detail = {"location": location, "constraint": kind,
                  "message": MESSAGES.get(kind, "Value violates the published field contract")}
        if kind == "source_selector":
            detail["fields"] = ["context_id", "draft_id", "draft_version"]
            detail["repair"] = "Read author_recover_task for the known task_id; contexts and drafts provide exact IDs and drafts[].version. Do not guess a version or create an export through a reader role."
        projected.append(detail)
    return {"code": "INVALID_REQUEST", "message": "Tool arguments violate the published contract",
            "details": {"boundary": "arguments_rejected_before_effect", "field_errors": projected,
                        "omitted_error_count": max(0, len(errors) - len(projected))}}
