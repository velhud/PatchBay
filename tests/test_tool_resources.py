import re
import shutil
import subprocess

import pytest

from patchbay.protocol.resources import (
    DEFAULT_WIDGET_DOMAIN,
    TOOL_CARD_HTML,
    TOOL_CARD_LEGACY_URIS,
    TOOL_CARD_MIME_TYPE,
    TOOL_CARD_URI,
    list_resource_templates,
    read_resource,
    widget_domain,
)


def test_tool_card_resource_contract():
    resources = list_resource_templates()

    assert resources == [
        {
            "uri": TOOL_CARD_URI,
            "name": "patchbay-tool-card",
            "title": "PatchBay Tool Card",
            "description": "Rich ChatGPT Apps card for PatchBay worker, artifact, job, diff, and power-tool results.",
            "mimeType": TOOL_CARD_MIME_TYPE,
        }
    ]

    result = read_resource(TOOL_CARD_URI, {})
    content = result["contents"][0]
    assert content["uri"] == TOOL_CARD_URI
    assert content["mimeType"] == "text/html;profile=mcp-app"
    assert content["text"] == TOOL_CARD_HTML
    assert content["_meta"]["ui"]["domain"] == DEFAULT_WIDGET_DOMAIN
    assert content["_meta"]["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}


def test_legacy_tool_card_resource_uri_still_reads_current_widget():
    result = read_resource(TOOL_CARD_LEGACY_URIS[0], {})
    content = result["contents"][0]

    assert content["uri"] == TOOL_CARD_LEGACY_URIS[0]
    assert content["mimeType"] == TOOL_CARD_MIME_TYPE
    assert content["text"] == TOOL_CARD_HTML


def test_widget_domain_uses_safe_https_origin_only():
    assert widget_domain({}) == DEFAULT_WIDGET_DOMAIN
    assert widget_domain({"app": {"widget_domain": "https://widgets.example.com"}}) == "https://widgets.example.com"
    assert widget_domain({"app": {"widget_domain": "http://127.0.0.1:3000"}}) == DEFAULT_WIDGET_DOMAIN
    assert widget_domain({"app": {"widget_domain": "https://widgets.example.com/"}}) == DEFAULT_WIDGET_DOMAIN


def test_tool_card_html_has_no_machine_specific_paths_or_tokens():
    forbidden_fragments = [
        "/" + "Users/",
        "/" + "Volumes/",
        "sk-",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
    ]

    for fragment in forbidden_fragments:
        assert fragment not in TOOL_CARD_HTML


def test_tool_card_widget_has_patchbay_specific_rich_renderers():
    expected_fragments = [
        "renderWorkerList",
        "renderWorker",
        "renderArtifact",
        "renderJob",
        "renderRepoBusy",
        "renderCommand",
        "renderDiffCard",
        "openai:set_globals",
        "ui/notifications/tool-result",
        "repo_busy",
        "takeover_required",
        "integration_state",
        "artifact_id",
    ]

    for fragment in expected_fragments:
        assert fragment in TOOL_CARD_HTML


def test_tool_card_javascript_is_syntax_valid(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    match = re.search(r"<script>(.*?)</script>", TOOL_CARD_HTML, flags=re.DOTALL)
    assert match, "tool card script missing"

    script_path = tmp_path / "tool-card.js"
    script_path.write_text(match.group(1), encoding="utf-8")
    result = subprocess.run([node, "--check", str(script_path)], check=False, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
