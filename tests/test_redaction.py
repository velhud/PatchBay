from security import redact_config_value, redact_sensitive_output


def test_redacts_openai_key_pattern():
    key_name = "OPENAI_API_KEY"
    data = {"output": f"{key_name}=fixture-value-for-tests"}
    assert redact_sensitive_output(data)["output"] == "[REDACTED_POSSIBLE_SECRET]"


def test_redacts_credential_config_keys():
    assert redact_config_value("api_key", "secret-value") == "[REDACTED]"
    assert redact_config_value("model", "gpt-5") == "gpt-5"


def test_redacts_token_assignment_without_destroying_key_name():
    token_name = "codex_mcp_" + "token"
    data = {"output": f"https://bridge.example/mcp?{token_name}=real-value"}

    assert redact_sensitive_output(data)["output"] == (
        f"https://bridge.example/mcp?{token_name}=[REDACTED_POSSIBLE_SECRET]"
    )


def test_does_not_redact_safe_token_metadata_words():
    data = {"output": "Bearer token when custom headers are supported; token_returned is false"}

    assert redact_sensitive_output(data)["output"] == data["output"]
