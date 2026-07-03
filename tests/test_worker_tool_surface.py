from patchbay.protocol.mcp import PUBLIC_TOOL_DESCRIPTORS, tool_descriptors_for_mode


WORKER_TOOLS = {
    "codex_worker_options",
    "codex_worker_inbox",
    "codex_worker_start",
    "codex_worker_message",
    "codex_worker_list",
    "codex_worker_inspect",
    "codex_worker_integrate",
    "codex_worker_stop",
}


def test_worker_tools_are_public_with_semantic_schemas():
    by_name = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}
    assert WORKER_TOOLS <= set(by_name)
    assert by_name["codex_worker_options"]["readOnlyHint"] is True
    assert "models" in by_name["codex_worker_options"]["outputSchema"]["properties"]
    assert "reasoning_efforts" in by_name["codex_worker_options"]["outputSchema"]["properties"]
    assert by_name["codex_worker_inbox"]["readOnlyHint"] is False
    assert by_name["codex_worker_inbox"]["_meta"]["openai/fileParams"] == ["artifact_file"]
    assert "artifact_file" in by_name["codex_worker_inbox"]["inputSchema"]["properties"]
    assert "takeover" in by_name["codex_worker_inbox"]["inputSchema"]["properties"]
    assert "artifact_id" in by_name["codex_worker_inbox"]["outputSchema"]["properties"]
    assert "owned_by_current_client" in by_name["codex_worker_inbox"]["outputSchema"]["properties"]
    assert "takeover_required" in by_name["codex_worker_inbox"]["outputSchema"]["properties"]
    assert by_name["codex_worker_start"]["inputSchema"]["required"] == ["name", "brief"]
    assert "workspace_mode" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "context_from_workers" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "context_from_artifacts" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "model" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "reasoning_effort" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "takeover" not in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "durable named Codex colleague" in by_name["codex_worker_start"]["description"]
    assert "lead instead of micromanaging code" in by_name["codex_worker_start"]["description"]
    assert "do not spell out every file or line" in by_name["codex_worker_start"]["description"]
    assert "durable report file" in by_name["codex_worker_start"]["description"]
    assert "start multiple workers with separate responsibilities" in by_name["codex_worker_start"]["description"]
    assert "workspace_mode=read_only" in by_name["codex_worker_start"]["description"]
    assert by_name["codex_worker_message"]["inputSchema"]["required"] == ["worker", "message"]
    assert "context_detail" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "context_from_artifacts" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "model" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "reasoning_effort" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "repo_path" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "takeover" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "not for a new independent colleague" in by_name["codex_worker_message"]["description"]
    assert "natural language" in by_name["codex_worker_message"]["description"]
    assert "manual file-by-file implementation loop" in by_name["codex_worker_message"]["description"]
    assert "thin, contradictory, missing evidence" in by_name["codex_worker_message"]["description"]
    assert "lacks a durable report file" in by_name["codex_worker_message"]["description"]
    assert "takeover=true" in by_name["codex_worker_message"]["description"]
    assert "workers" in by_name["codex_worker_list"]["outputSchema"]["properties"]
    assert "team_report" in by_name["codex_worker_list"]["outputSchema"]["properties"]
    assert "active_only" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "include_stopped" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "owned_only" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "created_after" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "reduce historical worker clutter" in by_name["codex_worker_list"]["description"]
    assert "report" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "text" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "next_start_line" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "max_bytes_applied" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "worker_report_files" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "workspace_location" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "view" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]
    assert "file" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]["view"]["enum"]
    assert "start_line" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]
    assert "repo_path" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]
    assert "view=file" in by_name["codex_worker_inspect"]["description"]
    assert "view=integration_preview" in by_name["codex_worker_inspect"]["description"]
    assert "normal management signal" in by_name["codex_worker_inspect"]["description"]
    assert "question the worker again with codex_worker_message" in by_name["codex_worker_inspect"]["description"]
    assert "pagination" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]["max_bytes"]["description"]
    assert "codex_worker_integrate" in by_name
    assert "allow_dirty_base" in by_name["codex_worker_integrate"]["inputSchema"]["properties"]
    assert "repo_path" in by_name["codex_worker_integrate"]["inputSchema"]["properties"]
    assert "takeover" in by_name["codex_worker_integrate"]["inputSchema"]["properties"]
    assert "can_apply" in by_name["codex_worker_integrate"]["outputSchema"]["properties"]
    assert "takeover_required" in by_name["codex_worker_integrate"]["outputSchema"]["properties"]
    assert "explicitly accepted" in by_name["codex_worker_integrate"]["description"]
    assert "does not commit" in by_name["codex_worker_integrate"]["description"]
    assert "cleanup_workspace" in by_name["codex_worker_stop"]["inputSchema"]["properties"]
    assert "repo_path" in by_name["codex_worker_stop"]["inputSchema"]["properties"]
    assert "takeover" in by_name["codex_worker_stop"]["inputSchema"]["properties"]
    assert "discard" in by_name["codex_worker_stop"]["description"]


def test_worker_tool_annotations_match_real_effects():
    by_name = {tool["name"]: tool for tool in PUBLIC_TOOL_DESCRIPTORS}
    assert by_name["codex_worker_start"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name["codex_worker_message"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name["codex_worker_list"]["annotations"]["readOnlyHint"] is True
    assert by_name["codex_worker_options"]["annotations"]["readOnlyHint"] is True
    assert by_name["codex_worker_inbox"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
        "idempotentHint": False,
    }
    assert by_name["codex_worker_inspect"]["annotations"]["readOnlyHint"] is True
    assert by_name["codex_worker_integrate"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": False,
        "idempotentHint": False,
    }
    assert by_name["codex_worker_stop"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": False,
        "idempotentHint": False,
    }


def test_worker_mode_hides_low_level_job_controls():
    names = {tool["name"] for tool in tool_descriptors_for_mode({"app": {"tool_mode": "worker"}})}
    assert WORKER_TOOLS <= names
    assert "codex_open_workspace" in names
    assert "codex_read_file" in names
    assert "codex_plan_job" not in names
    assert "codex_get_status" not in names
    assert "codex_read_session" not in names
    assert "bash" not in names


def test_worker_peer_context_arguments_validate():
    from patchbay.protocol.mcp import validate_public_tool_arguments

    validate_public_tool_arguments(
        "codex_worker_message",
        {
            "worker": "Implementer",
            "message": "Review the peer context.",
            "context_from_workers": ["Reviewer"],
            "context_from_artifacts": ["art_abc123"],
            "context_detail": "diff",
            "model": "gpt-5.5",
            "reasoning_effort": "high",
            "takeover": True,
            "takeover_reason": "User asked this chat to continue the worker.",
        },
    )

    validate_public_tool_arguments(
        "codex_worker_integrate",
        {"worker": "Implementer", "takeover": True, "takeover_reason": "User accepted this worker."},
    )

    import pytest

    with pytest.raises(ValueError, match="Invalid value for argument 'context_detail'"):
        validate_public_tool_arguments(
            "codex_worker_start",
            {
                "name": "Bad Context",
                "brief": "Start.",
                "context_from_workers": ["A"],
                "context_detail": "everything",
            },
        )

    with pytest.raises(ValueError, match="Invalid value for argument 'reasoning_effort'"):
        validate_public_tool_arguments(
            "codex_worker_start",
            {
                "name": "Bad Reasoning",
                "brief": "Start.",
                "reasoning_effort": "maximum",
            },
        )
