from patchbay.security import redact_config_value, redact_sensitive_output


def test_redacts_openai_key_pattern():
    key_name = "OPENAI_API_KEY"
    data = {"output": f"{key_name}=fixture-value-for-tests"}
    assert redact_sensitive_output(data)["output"] == "[REDACTED_POSSIBLE_SECRET]"


def test_redacts_credential_config_keys():
    assert redact_config_value("api_key", "secret-value") == "[REDACTED]"
    assert redact_config_value("model", "gpt-5") == "gpt-5"


def test_redacts_token_assignment_without_destroying_key_name():
    token_name = "patchbay_" + "token"
    data = {"output": f"https://bridge.example/mcp?{token_name}=real-value"}

    assert redact_sensitive_output(data)["output"] == (
        f"https://bridge.example/mcp?{token_name}=[REDACTED_POSSIBLE_SECRET]"
    )


def test_does_not_redact_safe_token_metadata_words():
    data = {"output": "Bearer token when custom headers are supported; token_returned is false"}

    assert redact_sensitive_output(data)["output"] == data["output"]


def test_preserves_fencing_expressions_and_common_type_annotations():
    source = (
        "lease_token=lease_token+1\n"
        "token: int\n"
        "lease_token: row['lease_token']\n"
        '"lease_token": row["lease_token"]}\n'
        "lease_token=lease_token+1 WHERE job_id=?\n"
        "lease_token=?\n"
        "lease_token=? AND lease_until>?\n"
        "password=literal-secret-value\n"
        "api_token=fixture-value\n"
    )

    result = redact_sensitive_output({"source": source})["source"]

    assert "lease_token=lease_token+1" in result
    assert "token: int" in result
    assert "lease_token: row['lease_token']" in result
    assert '"lease_token": row["lease_token"]}' in result
    assert "lease_token=lease_token+1 WHERE job_id=?" in result
    assert "lease_token=?" in result
    assert "lease_token=? AND lease_until>?" in result
    assert "password=[REDACTED_POSSIBLE_SECRET]" in result
    assert "api_token=[REDACTED_POSSIBLE_SECRET]" in result
    assert "literal-secret-value" not in result
    assert "fixture-value" not in result


def test_redacts_punctuated_unquoted_and_quoted_credential_literals():
    source = (
        "access_token=abc.defghijklmnop\n"
        "session_token=eyJheader.eyJpayload.signature\n"
        "api_token=abc/defghijklmnop\n"
        "session_token=(abc.defghijklmnop)\n"
        "# access_token=token.secret.value\n"
        "password='quoted-password-value'\n"
        'auth_token="quoted-token-value"\n'
        "token: Optional['literal-secret-value']\n"
        "token: Optional['sk-abcdefghijklmnopqrstuvwxyz']\n"
    )

    result = redact_sensitive_output({"source": source})["source"]

    assert result.count("[REDACTED_POSSIBLE_SECRET]") == 9
    for literal in (
        "abc.defghijklmnop",
        "eyJheader.eyJpayload.signature",
        "abc/defghijklmnop",
        "token.secret.value",
        "quoted-password-value",
        "quoted-token-value",
        "literal-secret-value",
        "sk-abcdefghijklmnopqrstuvwxyz",
    ):
        assert literal not in result


def test_redacts_entire_quoted_credential_with_spaces():
    source = "password='fixture phrase with spaces'\nauth_token=\"another fixture phrase\"\n"
    result = redact_sensitive_output({"source": source})["source"]
    assert result.count("[REDACTED_POSSIBLE_SECRET]") == 2
    assert "fixture" not in result
    assert "phrase" not in result
    assert "spaces" not in result
