"""Contract tests for the internal, not-yet-public Hub V2 tool registry."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from patchbay.hub.tool_surface import (
    HUB_V2_PUBLIC_STATUSES,
    HUB_V2_TOOL_DESCRIPTORS,
    HUB_V2_TOOL_FAMILIES,
    HUB_V2_TOOL_MANIFEST_HASH,
    HUB_V2_TOOL_NAMES,
    HUB_V2_TOOL_SCHEMA_HASH,
    HUB_V2_UNSUPPORTED_WORKER_COLLECTION_FIELDS,
    compute_tool_manifest_hash,
    compute_tool_schema_hash,
    normalize_hub_v2_next_action,
)
from patchbay.protocol.mcp import PUBLIC_TOOL_DESCRIPTORS_BY_NAME


EXPECTED_FAMILIES = {
    "fleet_and_discovery": (
        "patchbay_fleet_status",
        "patchbay_workspace_list",
    ),
    "work_groups": (
        "patchbay_work_group_create",
        "patchbay_work_group_list",
        "patchbay_work_group_status",
        "patchbay_work_group_resume",
        "patchbay_work_group_reassign",
        "patchbay_work_group_close",
    ),
    "workers_and_artifacts": (
        "patchbay_worker_options",
        "patchbay_worker_inbox",
        "patchbay_worker_start",
        "patchbay_worker_start_batch",
        "patchbay_worker_message",
        "patchbay_worker_list",
        "patchbay_worker_status",
        "patchbay_worker_wait",
        "patchbay_worker_inspect",
        "patchbay_worker_integrate",
        "patchbay_worker_stop",
    ),
    "exceptional_manager_workspace_inspection": (
        "patchbay_workspace_open",
        "patchbay_workspace_tree",
        "patchbay_workspace_search",
        "patchbay_workspace_read_file",
        "patchbay_workspace_changes",
    ),
    "pro_requests": (
        "patchbay_pro_request_list",
        "patchbay_pro_request_read",
        "patchbay_pro_request_claim",
        "patchbay_pro_request_respond",
        "patchbay_pro_request_dispatch",
        "patchbay_pro_request_close",
    ),
    "exceptional_operation_recovery": ("patchbay_operation_status",),
}
EXPECTED_FAMILY_COUNTS = {
    "fleet_and_discovery": 2,
    "work_groups": 6,
    "workers_and_artifacts": 11,
    "exceptional_manager_workspace_inspection": 5,
    "pro_requests": 6,
    "exceptional_operation_recovery": 1,
}
EXPECTED_TOOL_NAMES = tuple(
    tool_name
    for family_names in EXPECTED_FAMILIES.values()
    for tool_name in family_names
)
V1_ONLY_TOOL_NAMES = {
    "patchbay_machine_list",
    "patchbay_machine_workspaces",
    "patchbay_machine_recommend",
    "patchbay_worker_start_auto",
    "patchbay_command_status",
}
EXPECTED_PUBLIC_STATUSES = (
    "ok",
    "pending",
    "partial",
    "blocked",
    "failed",
    "not_found",
)

READ_ONLY_TOOL_NAMES = {
    "patchbay_fleet_status",
    "patchbay_workspace_list",
    "patchbay_work_group_list",
    "patchbay_work_group_status",
    "patchbay_worker_options",
    "patchbay_worker_list",
    "patchbay_worker_status",
    "patchbay_worker_wait",
    "patchbay_worker_inspect",
    "patchbay_workspace_open",
    "patchbay_workspace_tree",
    "patchbay_workspace_search",
    "patchbay_workspace_read_file",
    "patchbay_workspace_changes",
    "patchbay_pro_request_list",
    "patchbay_pro_request_read",
    "patchbay_operation_status",
}
MUTATING_TOOL_NAMES = set(EXPECTED_TOOL_NAMES) - READ_ONLY_TOOL_NAMES
DESTRUCTIVE_TOOL_NAMES = {
    "patchbay_work_group_reassign",
    "patchbay_work_group_close",
    "patchbay_worker_inbox",
    "patchbay_worker_integrate",
    "patchbay_worker_stop",
}
OPEN_WORLD_TOOL_NAMES = {
    "patchbay_worker_inbox",
    "patchbay_worker_start",
    "patchbay_worker_start_batch",
    "patchbay_worker_message",
    "patchbay_pro_request_dispatch",
}

CANONICAL_WORKER_TOOL_MAP = {
    "patchbay_worker_options": "codex_worker_options",
    "patchbay_worker_inbox": "codex_worker_inbox",
    "patchbay_worker_start": "codex_worker_start",
    "patchbay_worker_message": "codex_worker_message",
    "patchbay_worker_list": "codex_worker_list",
    "patchbay_worker_status": "codex_worker_status",
    "patchbay_worker_wait": "codex_worker_wait",
    "patchbay_worker_inspect": "codex_worker_inspect",
    "patchbay_worker_integrate": "codex_worker_integrate",
    "patchbay_worker_stop": "codex_worker_stop",
}
HUB_WORKER_ROUTING_FIELDS = {
    "patchbay_worker_options": {"work_group_id", "machine_id"},
    "patchbay_worker_inbox": {"work_group_id", "machine_id", "workspace_ref"},
    "patchbay_worker_start": {
        "work_group_id",
        "lane",
        "machine_id",
        "workspace_ref",
        "ungrouped_reason",
    },
    "patchbay_worker_message": {"work_group_id", "fleet_worker_ref"},
    "patchbay_worker_list": {"work_group_id", "lane"},
    "patchbay_worker_status": {"work_group_id", "lane", "since_revision"},
    "patchbay_worker_wait": {"work_group_id", "lane", "since_revision"},
    "patchbay_worker_inspect": {"work_group_id", "fleet_worker_ref"},
    "patchbay_worker_integrate": {"work_group_id", "fleet_worker_ref"},
    "patchbay_worker_stop": {"work_group_id", "fleet_worker_ref"},
}


def _descriptors_by_name() -> dict[str, Mapping[str, Any]]:
    return {descriptor["name"]: descriptor for descriptor in HUB_V2_TOOL_DESCRIPTORS}


def _strip_documentation(value: Any) -> Any:
    """Compare schema behavior while allowing Hub-specific field wording."""

    if isinstance(value, Mapping):
        return {
            key: _strip_documentation(item)
            for key, item in value.items()
            if key not in {"description", "title", "$comment", "examples"}
        }
    if isinstance(value, list):
        return [_strip_documentation(item) for item in value]
    return value


def _object_schema_paths(schema: Any, path: str = "$") -> list[tuple[str, Mapping[str, Any]]]:
    found: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(schema, Mapping):
        if schema.get("type") == "object":
            found.append((path, schema))
        for key, value in schema.items():
            found.extend(_object_schema_paths(value, f"{path}.{key}"))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found.extend(_object_schema_paths(value, f"{path}[{index}]"))
    return found


def _reverse_mapping_key_order(value: Any) -> Any:
    if isinstance(value, Mapping):
        keys = tuple(value)
        return {
            key: _reverse_mapping_key_order(value[key])
            for key in reversed(keys)
        }
    if isinstance(value, list):
        return [_reverse_mapping_key_order(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_reverse_mapping_key_order(item) for item in value)
    return value


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_schema_instance(instance: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate the closed JSON Schema subset used by the regression payload."""

    expected_type = schema.get("type")
    if expected_type == "object":
        assert isinstance(instance, Mapping), f"{path} must be an object"
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            assert required in instance, f"{path}.{required} is required"
        if schema.get("additionalProperties") is False:
            assert set(instance) <= set(properties), f"{path} has unknown properties"
        for key, value in instance.items():
            if key in properties:
                _validate_schema_instance(value, properties[key], f"{path}.{key}")
    elif expected_type == "array":
        assert isinstance(instance, list), f"{path} must be an array"
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, value in enumerate(instance):
                _validate_schema_instance(value, item_schema, f"{path}[{index}]")
    elif expected_type == "string":
        assert isinstance(instance, str), f"{path} must be a string"
    elif expected_type == "integer":
        assert isinstance(instance, int) and not isinstance(instance, bool), (
            f"{path} must be an integer"
        )
    elif expected_type == "number":
        assert isinstance(instance, (int, float)) and not isinstance(instance, bool), (
            f"{path} must be a number"
        )
    elif expected_type == "boolean":
        assert isinstance(instance, bool), f"{path} must be a boolean"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path} is outside the enum"


def test_exact_ordered_tool_manifest_and_family_counts():
    descriptor_names = tuple(
        descriptor["name"] for descriptor in HUB_V2_TOOL_DESCRIPTORS
    )

    assert tuple(HUB_V2_TOOL_FAMILIES) == tuple(EXPECTED_FAMILIES)
    assert {
        family: len(names) for family, names in HUB_V2_TOOL_FAMILIES.items()
    } == EXPECTED_FAMILY_COUNTS
    assert {
        family: tuple(names) for family, names in HUB_V2_TOOL_FAMILIES.items()
    } == EXPECTED_FAMILIES
    assert tuple(HUB_V2_TOOL_NAMES) == EXPECTED_TOOL_NAMES
    assert descriptor_names == EXPECTED_TOOL_NAMES
    assert len(descriptor_names) == 31


def test_tool_names_are_unique_and_v1_only_names_are_absent():
    names = tuple(descriptor["name"] for descriptor in HUB_V2_TOOL_DESCRIPTORS)

    assert len(names) == len(set(names))
    assert V1_ONLY_TOOL_NAMES.isdisjoint(names)


def test_every_input_and_output_envelope_is_closed():
    for descriptor in HUB_V2_TOOL_DESCRIPTORS:
        assert "inputSchema" in descriptor, descriptor["name"]
        assert "outputSchema" in descriptor, descriptor["name"]
        input_schema = descriptor["inputSchema"]
        output_schema = descriptor["outputSchema"]
        assert input_schema["type"] == "object", descriptor["name"]
        assert output_schema["type"] == "object", descriptor["name"]
        assert input_schema.get("additionalProperties") is False, descriptor["name"]
        assert output_schema.get("additionalProperties") is False, descriptor["name"]

        # The public call and result envelope are strict. Selected nested
        # transport-owned payloads remain extensible by contract: Apps file
        # parameters can gain metadata, and Edge domain results retain mature
        # worker fields without requiring a Hub release for every addition.
        for path, object_schema in _object_schema_paths(input_schema):
            if path == "$.properties.artifact_file":
                assert object_schema.get("additionalProperties") is True


def test_every_output_schema_uses_the_resolved_semantic_envelope():
    assert tuple(HUB_V2_PUBLIC_STATUSES) == EXPECTED_PUBLIC_STATUSES

    for descriptor in HUB_V2_TOOL_DESCRIPTORS:
        output_schema = descriptor["outputSchema"]
        properties = output_schema["properties"]
        assert {
            "status",
            "result",
            "operation",
            "warnings",
            "next_actions",
        } <= set(properties), descriptor["name"]
        assert set(output_schema["required"]) >= {
            "status",
            "result",
            "operation",
            "warnings",
            "next_actions",
        }, descriptor["name"]
        assert tuple(properties["status"]["enum"]) == EXPECTED_PUBLIC_STATUSES
        assert properties["status"]["type"] == "string"
        assert properties["result"]["type"] == "object"
        assert properties["operation"]["type"] == "object"
        assert properties["warnings"]["type"] == "array"
        assert properties["next_actions"]["type"] == "array"
        assert "operation_id" not in properties
        assert "next_action" not in properties

        action_schema = properties["next_actions"]["items"]["oneOf"][1]
        assert tuple(action_schema["properties"]["tool"]["enum"]) == HUB_V2_TOOL_NAMES


def test_next_action_normalization_validates_the_referenced_tool_input_schema():
    fallback = {
        "tool": "patchbay_operation_status",
        "arguments": {"operation_id": "op_invalid_action"},
        "reason": "Inspect this operation through Hub's public recovery tool.",
    }

    for invalid in (
        {"tool": "patchbay_worker_wait"},
        {
            "tool": "patchbay_worker_wait",
            "arguments": {"work_group_id": 7},
        },
        {
            "tool": "patchbay_worker_wait",
            "arguments": {"work_group_id": "group-a", "unsupported": True},
        },
    ):
        assert normalize_hub_v2_next_action(
            invalid, operation_id="op_invalid_action"
        ) == fallback

    valid = {
        "tool": "patchbay_worker_wait",
        "arguments": {
            "work_group_id": "group-a",
            "since_revision": 4,
            "wait_seconds": 20,
        },
        "reason": "Wait for the worker projection to change.",
    }
    assert normalize_hub_v2_next_action(
        valid, operation_id="op_invalid_action"
    ) == valid


def test_public_operation_output_schema_validates_integer_fencing_token():
    descriptor = _descriptors_by_name()["patchbay_operation_status"]
    output_schema = descriptor["outputSchema"]
    payload = {
        "status": "pending",
        "result": {},
        "operation": {
            "operation_id": "op_schema_regression",
            "state": "running",
            "fencing_token": 7,
        },
        "warnings": [],
        "next_actions": [],
    }

    _validate_schema_instance(payload, output_schema)
    assert output_schema["properties"]["operation"]["properties"]["fencing_token"] == {
        "type": "integer"
    }


def test_annotations_match_the_resolved_side_effect_contract():
    expected_annotation_keys = {
        "readOnlyHint",
        "destructiveHint",
        "openWorldHint",
        "idempotentHint",
    }

    for descriptor in HUB_V2_TOOL_DESCRIPTORS:
        name = descriptor["name"]
        annotations = descriptor["annotations"]
        assert set(annotations) == expected_annotation_keys, name
        assert all(isinstance(value, bool) for value in annotations.values()), name
        assert annotations == {
            "readOnlyHint": name in READ_ONLY_TOOL_NAMES,
            "destructiveHint": name in DESTRUCTIVE_TOOL_NAMES,
            "openWorldHint": name in OPEN_WORLD_TOOL_NAMES,
            # Required stable retry keys make repeated identical Hub calls
            # idempotent even when the Edge domain action is destructive.
            "idempotentHint": True,
        }, name


def test_mutations_require_idempotency_keys_and_reads_do_not_accept_them():
    assert READ_ONLY_TOOL_NAMES | MUTATING_TOOL_NAMES == set(EXPECTED_TOOL_NAMES)
    assert READ_ONLY_TOOL_NAMES.isdisjoint(MUTATING_TOOL_NAMES)

    for descriptor in HUB_V2_TOOL_DESCRIPTORS:
        name = descriptor["name"]
        properties = descriptor["inputSchema"]["properties"]
        required = set(descriptor["inputSchema"].get("required", []))
        if name in MUTATING_TOOL_NAMES:
            assert "idempotency_key" in properties, name
            assert properties["idempotency_key"]["type"] == "string", name
            assert "idempotency_key" in required, name
        else:
            assert "idempotency_key" not in properties, name
            assert "idempotency_key" not in required, name


def test_worker_tools_preserve_every_supported_canonical_input_field_and_schema():
    by_name = _descriptors_by_name()

    for hub_name, canonical_name in CANONICAL_WORKER_TOOL_MAP.items():
        hub_properties = by_name[hub_name]["inputSchema"]["properties"]
        canonical_properties = PUBLIC_TOOL_DESCRIPTORS_BY_NAME[canonical_name][
            "inputSchema"
        ]["properties"]
        supported_canonical_properties = {
            field_name: canonical_schema
            for field_name, canonical_schema in canonical_properties.items()
            if not (
                hub_name
                in {"patchbay_worker_list", "patchbay_worker_status", "patchbay_worker_wait"}
                and field_name in HUB_V2_UNSUPPORTED_WORKER_COLLECTION_FIELDS
            )
        }
        assert set(supported_canonical_properties) <= set(hub_properties), hub_name
        for field_name, canonical_schema in supported_canonical_properties.items():
            assert _strip_documentation(hub_properties[field_name]) == (
                _strip_documentation(canonical_schema)
            ), (hub_name, field_name)

    for name in ("patchbay_worker_list", "patchbay_worker_status", "patchbay_worker_wait"):
        assert "work_group_id" in by_name[name]["inputSchema"]["required"]
    assert by_name["patchbay_worker_status"]["outputSchema"]["properties"]["result"]["properties"]["workers"]["items"]["properties"]["last_activity_at"]["type"] == ["number", "null"]


def test_worker_tools_add_group_fleet_routing_without_dropping_parity_fields():
    by_name = _descriptors_by_name()

    for hub_name, expected_fields in HUB_WORKER_ROUTING_FIELDS.items():
        properties = by_name[hub_name]["inputSchema"]["properties"]
        assert expected_fields <= set(properties), hub_name

    integrate = by_name["patchbay_worker_integrate"]["inputSchema"]
    assert "preview_token" in integrate["properties"]
    assert "preview_token" in integrate["required"]

    stop = by_name["patchbay_worker_stop"]["inputSchema"]
    assert "discard_unintegrated_changes" in stop["properties"]
    assert stop["properties"]["discard_unintegrated_changes"]["type"] == "boolean"
    assert stop["properties"]["reason"]["type"] == "string"


def test_group_worker_monitoring_schema_exposes_only_supported_group_filters():
    by_name = _descriptors_by_name()
    expected_fields = {
        "patchbay_worker_list": {
            "work_group_id",
            "lane",
            "active_only",
            "include_stopped",
            "cursor",
            "limit",
        },
        "patchbay_worker_status": {
            "work_group_id",
            "lane",
            "active_only",
            "include_stopped",
            "cursor",
            "limit",
            "since_revision",
        },
        "patchbay_worker_wait": {
            "work_group_id",
            "lane",
            "active_only",
            "include_stopped",
            "cursor",
            "limit",
            "wait_seconds",
            "since_revision",
        },
    }

    for name, expected in expected_fields.items():
        schema = by_name[name]["inputSchema"]
        assert set(schema["properties"]) == expected
        assert schema["required"] == ["work_group_id"]
        assert {"scope", "owned_only", "created_after", "repo_path", "force_refresh"}.isdisjoint(
            schema["properties"]
        )
        assert "required work group" in by_name[name]["description"]


def test_group_close_schema_records_dispositions_without_stop_or_cleanup_controls():
    close = _descriptors_by_name()["patchbay_work_group_close"]["inputSchema"]
    properties = close["properties"]

    assert "active_work_disposition" not in properties
    assert "cleanup_completed_workspaces" not in properties
    dispositions = properties["worker_dispositions"]["items"]
    assert dispositions["properties"]["disposition"]["enum"] == [
        "integrated",
        "no_changes",
        "reviewed_failure",
        "stopped_preserved",
        "discarded",
        "leave_running",
    ]
    assert dispositions["allOf"][0]["then"] == {
        "properties": {"discard_unintegrated_changes": {"const": True}},
        "required": ["discard_unintegrated_changes"],
    }
    assert "never stops workers or cleans workspaces" in _descriptors_by_name()[
        "patchbay_work_group_close"
    ]["description"]


def test_group_tools_expose_execution_and_completion_contracts():
    by_name = _descriptors_by_name()
    create = by_name["patchbay_work_group_create"]["inputSchema"]
    status_input = by_name["patchbay_work_group_status"]["inputSchema"]
    status = by_name["patchbay_work_group_status"]["outputSchema"]

    assert create["properties"]["execution_mode"]["enum"] == [
        "end_to_end",
        "asynchronous_handoff",
    ]
    assert "definition_of_done" in create["properties"]
    assert {
        "worker_cursor",
        "worker_limit",
        "operation_cursor",
        "operation_limit",
        "integration_cursor",
        "integration_limit",
    } <= set(status_input["properties"])
    assert status_input["properties"]["worker_limit"]["maximum"] == 100
    assert status_input["properties"]["operation_limit"]["maximum"] == 100
    assert status_input["properties"]["integration_limit"]["maximum"] == 100
    result = status["properties"]["result"]["properties"]
    assert "completion_contract" in result
    assert "status_revision" in result
    assert "changed" in result


def test_worker_batch_schema_preserves_shared_and_per_worker_contracts():
    schema = _descriptors_by_name()["patchbay_worker_start_batch"]["inputSchema"]
    properties = schema["properties"]
    assert {
        "work_group_id",
        "shared_brief",
        "context_from_workers",
        "context_from_artifacts",
        "context_detail",
        "workers",
        "idempotency_key",
    } <= set(properties)
    assert {
        "work_group_id",
        "shared_brief",
        "workers",
        "idempotency_key",
    } <= set(schema["required"])

    worker_item = properties["workers"]["items"]
    assert worker_item["type"] == "object"
    assert worker_item["additionalProperties"] is False
    assert {
        "name",
        "lane",
        "mission",
        "workspace_mode",
        "model",
        "reasoning_effort",
        "context_from_workers",
        "context_from_artifacts",
        "include_untracked_from_base",
        "auto_suffix",
        "idempotency_key",
    } <= set(worker_item["properties"])
    assert {"name", "lane", "mission", "idempotency_key"} <= set(
        worker_item["required"]
    )


def test_worker_inbox_retains_chatgpt_apps_file_parameter_metadata():
    inbox = _descriptors_by_name()["patchbay_worker_inbox"]

    assert inbox["_meta"]["openai/fileParams"] == ["artifact_file"]
    assert "artifact_file" in inbox["inputSchema"]["properties"]


def test_manifest_and_schema_hashes_use_deterministic_canonical_json():
    descriptors = list(HUB_V2_TOOL_DESCRIPTORS)
    manifest_payload = [descriptor["name"] for descriptor in descriptors]
    schema_payload = [
        {
            "name": descriptor["name"],
            "inputSchema": descriptor["inputSchema"],
            "outputSchema": descriptor["outputSchema"],
        }
        for descriptor in descriptors
    ]
    expected_manifest_hash = _canonical_sha256(manifest_payload)
    expected_schema_hash = _canonical_sha256(schema_payload)

    assert HUB_V2_TOOL_MANIFEST_HASH == expected_manifest_hash
    assert HUB_V2_TOOL_SCHEMA_HASH == expected_schema_hash
    assert compute_tool_manifest_hash(descriptors) == expected_manifest_hash
    assert compute_tool_schema_hash(descriptors) == expected_schema_hash
    assert re.fullmatch(r"[0-9a-f]{64}", HUB_V2_TOOL_MANIFEST_HASH)
    assert re.fullmatch(r"[0-9a-f]{64}", HUB_V2_TOOL_SCHEMA_HASH)

    reordered_keys = _reverse_mapping_key_order(copy.deepcopy(descriptors))
    assert compute_tool_manifest_hash(reordered_keys) == expected_manifest_hash
    assert compute_tool_schema_hash(reordered_keys) == expected_schema_hash


def test_manifest_hash_tracks_order_and_schema_hash_tracks_schema_changes():
    descriptors = list(HUB_V2_TOOL_DESCRIPTORS)
    reversed_descriptors: Sequence[Mapping[str, Any]] = tuple(
        reversed(copy.deepcopy(descriptors))
    )
    assert compute_tool_manifest_hash(reversed_descriptors) != (
        HUB_V2_TOOL_MANIFEST_HASH
    )

    changed_schema = copy.deepcopy(descriptors)
    changed_schema[0]["inputSchema"]["properties"]["hash_probe"] = {
        "type": "string"
    }
    assert compute_tool_schema_hash(changed_schema) != HUB_V2_TOOL_SCHEMA_HASH
