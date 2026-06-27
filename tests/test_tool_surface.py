import pytest

from mcp_protocol import (
    APP_SECURITY_SCHEMES,
    PUBLIC_TOOL_DESCRIPTORS,
    PUBLIC_TOOL_NAMES,
    TOOLS,
    resolve_public_tool_name,
    tool_descriptors_for_mode,
    validate_public_tool_arguments,
)
from tool_resources import TOOL_CARD_URI


def test_public_tool_names_are_codex_specific():
    expected = {
        "codex_open_workspace",
        "codex_repo_tree",
        "codex_read_file",
        "codex_search_repo",
        "codex_load_context",
        "codex_export_context",
        "codex_list_skills",
        "codex_load_skill",
        "codex_write_handoff",
        "codex_get_handoff_status",
        "codex_get_handoff_diff",
        "codex_list_workspaces",
        "codex_workspace_snapshot",
        "codex_inventory",
        "codex_git_status",
        "codex_git_diff",
        "codex_show_changes",
        "codex_write_file",
        "codex_edit_file",
        "codex_run_command",
        "codex_plan_job",
        "codex_apply_job",
        "codex_get_status",
        "codex_get_result",
        "codex_get_diff",
        "codex_cancel_job",
        "codex_review",
        "codex_list_sessions",
        "codex_read_session",
        "codex_resume",
        "codex_interactive",
        "codex_interactive_reply",
        "codex_worker_options",
        "codex_worker_start",
        "codex_worker_message",
        "codex_worker_list",
        "codex_worker_inspect",
        "codex_worker_integrate",
        "codex_worker_stop",
        "codex_self_test",
        "codex_get_config",
        "codex_tool_mode_info",
        "codex_tool_mode_switch",
    }

    assert expected <= PUBLIC_TOOL_NAMES


def test_no_enterprise_tool_names_are_primary_public_surface():
    forbidden = {
        "query_text_analytics",
        "update_content_record",
        "fetch_record_delta",
        "apply_remote_delta",
        "submit_remote_task",
        "codex_sandbox",
        "codex_cloud_exec",
        "codex_cloud_diff",
    }

    assert not (forbidden & PUBLIC_TOOL_NAMES)


def test_unknown_and_internal_tools_are_rejected():
    for name in ["codex_sandbox", "codex_cloud_exec", "string_transform", "unknown_tool"]:
        with pytest.raises(ValueError):
            resolve_public_tool_name(name)


def test_deprecated_aliases_are_resolved_but_not_advertised():
    assert resolve_public_tool_name("query_text_analytics") == "codex_plan_job"
    assert "query_text_analytics" not in PUBLIC_TOOL_NAMES


def test_codexpro_aliases_are_advertised_and_resolve_to_canonical_tools():
    assert resolve_public_tool_name("read") == "codex_read_file"
    assert resolve_public_tool_name("edit") == "codex_edit_file"
    assert resolve_public_tool_name("show_changes") == "codex_show_changes"
    assert resolve_public_tool_name("handoff_to_agent") == "codex_write_handoff"
    assert {"read", "edit", "show_changes", "handoff_to_agent"} <= PUBLIC_TOOL_NAMES


def test_tool_modes_filter_advertised_surface():
    minimal = {tool["name"] for tool in tool_descriptors_for_mode({"app": {"tool_mode": "minimal"}})}
    standard = {tool["name"] for tool in tool_descriptors_for_mode({"app": {"tool_mode": "standard"}})}
    full = {tool["name"] for tool in tool_descriptors_for_mode({"app": {"tool_mode": "full"}})}
    worker = {tool["name"] for tool in tool_descriptors_for_mode({"app": {"tool_mode": "worker"}})}

    assert {"read", "edit", "show_changes", "codex_read_file", "codex_show_changes"} <= minimal
    assert {"codex_tool_mode_info", "codex_tool_mode_switch"} <= minimal
    assert "codex_plan_job" not in minimal
    assert {"codex_plan_job", "codex_workspace_snapshot", "handoff_to_agent", "codex_worker_start"} <= standard
    assert {"codex_tool_mode_info", "codex_tool_mode_switch"} <= standard
    assert "codex_read_session" not in standard
    assert {"codex_read_session", "read_codex_session"} <= full
    assert {"codex_tool_mode_info", "codex_tool_mode_switch"} <= full
    assert {"codex_worker_options", "codex_worker_start", "codex_worker_message", "codex_worker_list", "codex_worker_inspect", "codex_worker_stop"} <= worker
    assert {"codex_tool_mode_info", "codex_tool_mode_switch"} <= worker
    assert "codex_plan_job" not in worker
    assert "codex_get_status" not in worker
    assert "read" not in worker
    assert "show_changes" not in worker


def test_mutating_tools_are_not_readonly():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert by_name["codex_plan_job"]["readOnlyHint"] is False
    assert by_name["codex_apply_job"]["readOnlyHint"] is False
    assert by_name["codex_cancel_job"]["readOnlyHint"] is False
    assert by_name["codex_write_file"]["readOnlyHint"] is False
    assert by_name["codex_edit_file"]["readOnlyHint"] is False
    assert by_name["codex_run_command"]["readOnlyHint"] is False
    assert by_name["codex_export_context"]["readOnlyHint"] is False
    assert by_name["codex_write_handoff"]["readOnlyHint"] is False
    assert by_name["codex_resume"]["readOnlyHint"] is False
    assert by_name["codex_interactive"]["readOnlyHint"] is False
    assert by_name["codex_interactive_reply"]["readOnlyHint"] is False
    assert by_name["codex_worker_start"]["readOnlyHint"] is False
    assert by_name["codex_worker_message"]["readOnlyHint"] is False
    assert by_name["codex_worker_stop"]["readOnlyHint"] is False
    assert by_name["codex_tool_mode_switch"]["readOnlyHint"] is False
    assert by_name["codex_tool_mode_info"]["readOnlyHint"] is True


def test_readonly_tools_are_marked_readonly():
    readonly = PUBLIC_TOOL_NAMES - {
        "codex_plan_job",
        "codex_apply_job",
        "codex_cancel_job",
        "codex_export_context",
        "codex_write_handoff",
        "codex_write_file",
        "codex_edit_file",
        "codex_run_command",
        "codex_resume",
        "codex_interactive",
        "codex_interactive_reply",
        "codex_worker_start",
        "codex_worker_message",
        "codex_worker_integrate",
        "codex_worker_stop",
        "codex_tool_mode_switch",
        "write",
        "edit",
        "bash",
        "export_pro_context",
        "handoff_to_agent",
        "handoff_to_codex",
    }
    by_name = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}
    for name in readonly:
        assert by_name[name]["readOnlyHint"] is True


def test_public_tool_descriptors_have_chatgpt_app_metadata():
    assert len(PUBLIC_TOOL_DESCRIPTORS) > len(TOOLS)

    for descriptor in PUBLIC_TOOL_DESCRIPTORS:
        assert descriptor["title"].startswith(("Codex ", "CodexPro "))
        assert descriptor["outputSchema"]["type"] == "object"
        assert descriptor["securitySchemes"] == APP_SECURITY_SCHEMES
        assert descriptor["_meta"]["securitySchemes"] == APP_SECURITY_SCHEMES

        annotations = descriptor["annotations"]
        assert set(annotations) == {
            "readOnlyHint",
            "destructiveHint",
            "openWorldHint",
            "idempotentHint",
        }
        assert annotations["readOnlyHint"] is descriptor["readOnlyHint"]
        assert isinstance(annotations["idempotentHint"], bool)

        invoking = descriptor["_meta"]["openai/toolInvocation/invoking"]
        invoked = descriptor["_meta"]["openai/toolInvocation/invoked"]
        assert isinstance(invoking, str)
        assert isinstance(invoked, str)
        assert 0 < len(invoking) <= 64
        assert 0 < len(invoked) <= 64
        assert descriptor["_meta"]["ui"]["resourceUri"] == TOOL_CARD_URI
        assert descriptor["_meta"]["openai/outputTemplate"] == TOOL_CARD_URI


def test_high_value_output_schemas_describe_structured_results():
    by_name = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}

    assert "workspace_id" in by_name["codex_open_workspace"]["outputSchema"]["properties"]
    assert "matches" in by_name["codex_search_repo"]["outputSchema"]["properties"]
    assert "skill_inventory" in by_name["codex_list_skills"]["outputSchema"]["properties"]
    assert "skill" in by_name["codex_load_skill"]["outputSchema"]["properties"]
    assert "diff" in by_name["codex_write_file"]["outputSchema"]["properties"]
    assert "job_id" in by_name["codex_apply_job"]["outputSchema"]["properties"]
    assert "sessions" in by_name["codex_list_sessions"]["outputSchema"]["properties"]
    assert "messages" in by_name["codex_read_session"]["outputSchema"]["properties"]
    assert "job_id" in by_name["codex_resume"]["outputSchema"]["properties"]
    assert "job_id" in by_name["codex_interactive_reply"]["outputSchema"]["properties"]
    assert "models" in by_name["codex_worker_options"]["outputSchema"]["properties"]
    assert "report" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "workers" in by_name["codex_worker_list"]["outputSchema"]["properties"]
    assert "diff" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "can_apply" in by_name["codex_worker_integrate"]["outputSchema"]["properties"]
    assert "connection" in by_name["codex_self_test"]["outputSchema"]["properties"]
    assert "wrapper_config" in by_name["codex_get_config"]["outputSchema"]["properties"]
    assert "modes" in by_name["codex_tool_mode_info"]["outputSchema"]["properties"]
    assert "current_mode" in by_name["codex_tool_mode_switch"]["outputSchema"]["properties"]


def test_public_tool_descriptor_power_classifications_are_explicit():
    by_name = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}

    assert by_name["codex_read_file"]["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": True,
    }
    assert by_name["codex_write_file"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": False,
        "idempotentHint": False,
    }
    assert by_name["codex_run_command"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name["codex_plan_job"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name["codex_apply_job"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name["codex_interactive"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name["codex_resume"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
        "idempotentHint": False,
    }


def test_public_tool_schema_rejects_missing_required_argument():
    with pytest.raises(ValueError, match="Missing required argument 'spec'"):
        validate_public_tool_arguments("codex_plan_job", {})


def test_public_tool_schema_rejects_unknown_argument():
    with pytest.raises(ValueError, match="Unknown argument 'unexpected'"):
        validate_public_tool_arguments("codex_plan_job", {"spec": "inspect", "unexpected": True})


def test_public_tool_schema_rejects_wrong_type():
    with pytest.raises(ValueError, match="Invalid type for argument 'spec'"):
        validate_public_tool_arguments("codex_plan_job", {"spec": 123})


def test_public_tool_schema_accepts_danger_full_access_sandbox():
    validate_public_tool_arguments("codex_plan_job", {"spec": "inspect", "sandbox": "danger-full-access"})


def test_public_tool_schema_validates_nested_objects():
    validate_public_tool_arguments(
        "codex_plan_job",
        {"spec": "inspect", "features": {"enable": ["json"], "disable": ["skills"]}},
    )

    with pytest.raises(ValueError, match="Unknown argument 'features.extra'"):
        validate_public_tool_arguments("codex_plan_job", {"spec": "inspect", "features": {"extra": True}})
