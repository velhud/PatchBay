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
    assert "model_selection_guidance" in by_name["codex_worker_options"]["outputSchema"]["properties"]
    assert "GPT-5.6 Sol, Terra, and Luna" in by_name["codex_worker_options"]["description"]
    assert by_name["codex_worker_start"]["inputSchema"]["properties"]["reasoning_effort"]["enum"] == [
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert "repo_path is accepted as a harmless compatibility field" in by_name["codex_worker_options"]["description"]
    assert "repo_path" in by_name["codex_worker_options"]["inputSchema"]["properties"]
    assert "not a hard router" in by_name["codex_worker_options"]["description"]
    assert by_name["codex_worker_inbox"]["readOnlyHint"] is False
    assert by_name["codex_worker_inbox"]["_meta"]["openai/fileParams"] == ["artifact_file"]
    assert "artifact_file" in by_name["codex_worker_inbox"]["inputSchema"]["properties"]
    assert "takeover" in by_name["codex_worker_inbox"]["inputSchema"]["properties"]
    assert "artifact_id" in by_name["codex_worker_inbox"]["outputSchema"]["properties"]
    assert "owned_by_current_client" in by_name["codex_worker_inbox"]["outputSchema"]["properties"]
    assert "ownership_scope" in by_name["codex_worker_inbox"]["outputSchema"]["properties"]
    assert "takeover_required" in by_name["codex_worker_inbox"]["outputSchema"]["properties"]
    worker_result_schema = by_name["codex_worker_start"]["outputSchema"]["properties"]
    assert worker_result_schema["work_run_started_at"]["type"] == ["number", "null"]
    assert worker_result_schema["work_run_last_activity_at"]["type"] == ["number", "null"]
    assert by_name["codex_worker_start"]["inputSchema"]["required"] == ["name", "brief"]
    assert "workspace_mode" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "context_from_workers" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "Up to 10 workers" in by_name["codex_worker_start"]["inputSchema"]["properties"]["context_from_workers"]["description"]
    assert "context_from_artifacts" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "model" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "reasoning_effort" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "auto_suffix" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "include_untracked_from_base" in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "takeover" not in by_name["codex_worker_start"]["inputSchema"]["properties"]
    assert "durable named Codex colleague" in by_name["codex_worker_start"]["description"]
    assert "manager instead of primary file reader" in by_name["codex_worker_start"]["description"]
    assert "small team of workers" in by_name["codex_worker_start"]["description"]
    assert "long manual read/search loop" in by_name["codex_worker_start"]["description"]
    assert "let the worker find relevant files" in by_name["codex_worker_start"]["description"]
    assert "durable report file" in by_name["codex_worker_start"]["description"]
    assert "live checkpoints" in by_name["codex_worker_start"]["description"]
    assert "start multiple workers with separate responsibilities" in by_name["codex_worker_start"]["description"]
    assert "up to 10 concurrent worker slots" in by_name["codex_worker_start"]["description"]
    assert "workspace_mode=read_only" in by_name["codex_worker_start"]["description"]
    assert by_name["codex_worker_message"]["inputSchema"]["required"] == ["worker", "message"]
    assert "context_detail" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "Up to 10 workers" in by_name["codex_worker_message"]["inputSchema"]["properties"]["context_from_workers"]["description"]
    assert "context_from_artifacts" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "model" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "reasoning_effort" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "repo_path" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "takeover" in by_name["codex_worker_message"]["inputSchema"]["properties"]
    assert "not for a new independent colleague" in by_name["codex_worker_message"]["description"]
    assert "natural language" in by_name["codex_worker_message"]["description"]
    assert "manual file-by-file implementation loop" in by_name["codex_worker_message"]["description"]
    assert "evidence is usable" in by_name["codex_worker_message"]["description"]
    assert "thin, contradictory, missing evidence" in by_name["codex_worker_message"]["description"]
    assert "lacks a durable report file" in by_name["codex_worker_message"]["description"]
    assert "latest_checkpoints" in by_name["codex_worker_message"]["description"]
    assert "next turn after completion" in by_name["codex_worker_message"]["description"]
    assert "takeover=true" in by_name["codex_worker_message"]["description"]
    assert "workers" in by_name["codex_worker_list"]["outputSchema"]["properties"]
    assert "team_report" in by_name["codex_worker_list"]["outputSchema"]["properties"]
    assert "team_status" in by_name["codex_worker_list"]["outputSchema"]["properties"]
    assert "liveness" in by_name["codex_worker_list"]["description"]
    assert "activity deltas" in by_name["codex_worker_list"]["description"]
    assert "active_only" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "include_stopped" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "owned_only" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "created_after" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "scope" in by_name["codex_worker_list"]["inputSchema"]["properties"]
    assert "hidden_workers" in by_name["codex_worker_list"]["outputSchema"]["properties"]
    assert "reduce historical worker clutter" in by_name["codex_worker_list"]["description"]
    assert by_name["codex_worker_status"]["readOnlyHint"] is True
    assert "scope" in by_name["codex_worker_status"]["inputSchema"]["properties"]
    assert "scope" in by_name["codex_worker_status"]["outputSchema"]["properties"]
    assert "hidden_workers" in by_name["codex_worker_status"]["outputSchema"]["properties"]
    assert "worker_lines" in by_name["codex_worker_status"]["outputSchema"]["properties"]
    assert "since_last_check" in by_name["codex_worker_status"]["outputSchema"]["properties"]
    assert "recommended_next_poll_seconds" in by_name["codex_worker_status"]["outputSchema"]["properties"]
    assert "minimum_next_poll_seconds" in by_name["codex_worker_status"]["outputSchema"]["properties"]
    assert "poll_guidance" in by_name["codex_worker_status"]["outputSchema"]["properties"]
    assert "compact pull-based worker team status bar" in by_name["codex_worker_status"]["description"]
    assert "active/quiet/stale/lost" in by_name["codex_worker_status"]["description"]
    assert "20-30 seconds" in by_name["codex_worker_status"]["description"]
    assert "do not poll every few seconds" in by_name["codex_worker_status"]["description"]
    assert "scope" in by_name["codex_worker_wait"]["inputSchema"]["properties"]
    assert "report" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "liveness" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "status_line" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "latest_partial_note" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "activity_since_last_check" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "latest_checkpoints" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "checkpoint_count" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "report_artifacts" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "active_steering_supported" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "text" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "next_start_line" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "max_bytes_applied" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "worker_report_files" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "ownership_scope" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "workspace_location" in by_name["codex_worker_inspect"]["outputSchema"]["properties"]
    assert "view" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]
    assert "compact" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]["view"]["enum"]
    assert "diagnostics" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]["view"]["enum"]
    assert "file" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]["view"]["enum"]
    assert "start_line" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]
    assert "repo_path" in by_name["codex_worker_inspect"]["inputSchema"]["properties"]
    assert "view=file" in by_name["codex_worker_inspect"]["description"]
    assert "view=integration_preview" in by_name["codex_worker_inspect"]["description"]
    assert "latest_checkpoints" in by_name["codex_worker_inspect"]["description"]
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
    assert "force" in by_name["codex_worker_stop"]["inputSchema"]["properties"]
    assert "repo_path" in by_name["codex_worker_stop"]["inputSchema"]["properties"]
    assert "takeover" in by_name["codex_worker_stop"]["inputSchema"]["properties"]
    assert "partial checkpoints" in by_name["codex_worker_stop"]["description"]
    assert "stop_confirmation_required" in by_name["codex_worker_stop"]["description"]
    assert "force=true" in by_name["codex_worker_stop"]["description"]
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
        {
            "worker": "Implementer",
            "accepted_dirty_base": ["dev/big_update/00-*.md"],
            "takeover": True,
            "takeover_reason": "User accepted this worker.",
        },
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

    validate_public_tool_arguments("codex_worker_options", {"repo_path": "/repo"})
