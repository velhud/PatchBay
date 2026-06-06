from security import redact_config_value, redact_sensitive_output


def test_redacts_openai_key_pattern():
    data = {"output": "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456"}
    assert redact_sensitive_output(data)["output"] == "[REDACTED_POSSIBLE_SECRET]"


def test_redacts_credential_config_keys():
    assert redact_config_value("api_key", "secret-value") == "[REDACTED]"
    assert redact_config_value("model", "gpt-5") == "gpt-5"
