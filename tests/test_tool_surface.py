import pytest

from mcp_protocol import (
    PUBLIC_TOOL_NAMES,
    TOOLS,
    resolve_public_tool_name,
)


def test_public_tool_names_are_codex_specific():
    expected = {
        "codex_plan_job",
        "codex_apply_job",
        "codex_get_status",
        "codex_get_result",
        "codex_get_diff",
        "codex_review",
        "codex_resume",
        "codex_interactive",
        "codex_interactive_reply",
        "codex_get_config",
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


def test_mutating_tools_are_not_readonly():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert by_name["codex_apply_job"]["readOnlyHint"] is False


def test_readonly_tools_are_marked_readonly():
    readonly = PUBLIC_TOOL_NAMES - {"codex_apply_job"}
    by_name = {tool["name"]: tool for tool in TOOLS}
    for name in readonly:
        assert by_name[name]["readOnlyHint"] is True
