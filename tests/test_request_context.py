from patchbay.protocol.context import RequestContext, make_client_ref


def test_client_ref_is_stable_and_does_not_echo_session_id():
    ref = make_client_ref("private-session-id", salt="test-salt")

    assert ref.startswith("client_")
    assert ref == make_client_ref("private-session-id", salt="test-salt")
    assert ref != make_client_ref("private-session-id", salt="other-salt")
    assert "private-session-id" not in ref


def test_request_context_public_metadata_excludes_private_session_id():
    context = RequestContext.from_session(
        "private-session-id",
        {"client_label": "Planning chat", "tool_mode": "worker"},
        salt="test-salt",
        active_mcp_sessions=2,
    )

    public = context.public_metadata()
    assert public["client_ref"].startswith("client_")
    assert public["client_label"] == "Planning chat"
    assert public["tool_mode"] == "worker"
    assert public["active_mcp_sessions"] == 2
    assert public["has_mcp_session"] is True
    assert "private-session-id" not in str(public)


def test_request_context_can_use_stable_owner_ref_separate_from_transport_session():
    context = RequestContext.from_session(
        "short-lived-transport-session",
        {"owner_ref": "client_stableowner", "owner_scope": "token"},
        salt="test-salt",
    )

    public = context.public_metadata()
    assert context.client_ref != context.owner_ref
    assert public["owner_ref"] == "client_stableowner"
    assert public["owner_scope"] == "token"
    assert "short-lived-transport-session" not in str(public)
