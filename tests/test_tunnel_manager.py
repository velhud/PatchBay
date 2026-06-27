import pytest

from patchbay.connector.tunnels import (
    cloudflared_release_asset,
    ProcessLogTail,
    resolve_cloudflared,
    resolve_ngrok,
    TunnelConfigurationError,
    build_tunnel_spec,
    mcp_url_from_public_base,
    public_base_from_hostname,
    url_with_query_token,
    wait_for_cloudflare_url,
)


class PollingProcess:
    def poll(self):
        return None


def test_public_base_from_hostname_normalizes_mcp_urls():
    assert public_base_from_hostname("bridge.example") == "https://bridge.example"
    assert public_base_from_hostname("https://bridge.example/mcp") == "https://bridge.example"
    assert public_base_from_hostname("https://bridge.example/path/mcp") == "https://bridge.example/path"

    with pytest.raises(TunnelConfigurationError):
        public_base_from_hostname("http://bridge.example")


def test_mcp_url_and_query_token_helpers():
    token_query = "codex_" + "mcp_token"
    assert mcp_url_from_public_base("https://bridge.example/") == "https://bridge.example/mcp"
    assert (
        url_with_query_token("https://bridge.example/mcp", token_query, "secret", redact=True)
        == f"https://bridge.example/mcp?{token_query}=%3Credacted%3E"
    )
    assert (
        url_with_query_token("https://bridge.example/mcp?x=1", token_query, "secret", redact=False)
        == f"https://bridge.example/mcp?x=1&{token_query}=secret"
    )


def test_build_cloudflare_quick_spec():
    spec = build_tunnel_spec(mode="cloudflare", local_base_url="http://127.0.0.1:8000", cloudflared="cf")

    assert spec.command == "cf"
    assert spec.args == ["tunnel", "--url", "http://127.0.0.1:8000"]
    assert spec.discover_cloudflare_url is True


def test_resolve_tunnel_binaries_run_version_checks(tmp_path):
    fake_cloudflared = tmp_path / "cloudflared"
    fake_cloudflared.write_text("#!/usr/bin/env python3\nimport sys\nprint('cloudflared fixture')\n", encoding="utf-8")
    fake_cloudflared.chmod(0o700)
    fake_ngrok = tmp_path / "ngrok"
    fake_ngrok.write_text("#!/usr/bin/env python3\nprint('ngrok fixture')\n", encoding="utf-8")
    fake_ngrok.chmod(0o700)

    assert resolve_cloudflared(str(fake_cloudflared)) == str(fake_cloudflared)
    assert resolve_ngrok(str(fake_ngrok)) == str(fake_ngrok)


def test_cloudflared_release_asset_mapping():
    assert cloudflared_release_asset("Darwin", "arm64").file_name == "cloudflared-darwin-arm64.tgz"
    assert cloudflared_release_asset("Linux", "x86_64").file_name == "cloudflared-linux-amd64"
    assert cloudflared_release_asset("Windows", "AMD64").file_name == "cloudflared-windows-amd64.exe"


def test_build_ngrok_spec_requires_https_hostname():
    spec = build_tunnel_spec(
        mode="ngrok",
        local_base_url="http://127.0.0.1:8000",
        hostname="codex.ngrok-free.app",
        ngrok="ngrok-bin",
        ngrok_config="/tmp/ngrok.yml",
    )

    assert spec.command == "ngrok-bin"
    assert spec.public_base_url == "https://codex.ngrok-free.app"
    assert spec.args == ["http", "http://127.0.0.1:8000", "--url", "https://codex.ngrok-free.app", "--config", "/tmp/ngrok.yml"]

    with pytest.raises(TunnelConfigurationError):
        build_tunnel_spec(mode="ngrok", local_base_url="http://127.0.0.1:8000")


def test_build_cloudflare_named_spec_without_secret_in_args(monkeypatch):
    token_value = "fixture-" + "secret"
    monkeypatch.setenv("CF_TUNNEL_TOKEN", token_value)

    spec = build_tunnel_spec(
        mode="cloudflare-named",
        local_base_url="http://127.0.0.1:8000",
        hostname="codex.example.com",
        cloudflared="cf",
        cloudflare_token_env="CF_TUNNEL_TOKEN",
    )

    assert spec.command == "cf"
    assert spec.public_base_url == "https://codex.example.com"
    assert spec.args == ["tunnel", "run", "--url", "http://127.0.0.1:8000"]
    assert spec.env_overrides == {"TUNNEL_TOKEN": token_value}
    assert token_value not in " ".join(spec.args)


def test_build_cloudflare_named_requires_tunnel_source(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_TUNNEL_TOKEN", raising=False)

    with pytest.raises(TunnelConfigurationError):
        build_tunnel_spec(
            mode="cloudflare-named",
            local_base_url="http://127.0.0.1:8000",
            hostname="codex.example.com",
        )


def test_wait_for_cloudflare_url_reads_bounded_tail():
    tail = ProcessLogTail("cloudflared")
    tail.record("stderr", "Visit https://alpha-beta.trycloudflare.com for your tunnel")

    assert wait_for_cloudflare_url(PollingProcess(), tail, timeout_seconds=0.5) == "https://alpha-beta.trycloudflare.com"
