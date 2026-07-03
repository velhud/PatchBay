import re
import json
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


def _render_tool_card_with_node(tmp_path, *, tool_output=None, tool_response_metadata=None, message_params=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    match = re.search(r"<script>(.*?)</script>", TOOL_CARD_HTML, flags=re.DOTALL)
    assert match, "tool card script missing"

    script_path = tmp_path / "render-widget.js"
    script_path.write_text(
        "\n".join(
            [
                "const listeners = {};",
                "const rootNode = { innerHTML: '' };",
                "global.document = { getElementById(id) { return rootNode; } };",
                "global.window = {",
                "  parent: {},",
                f"  openai: {{ toolOutput: {json.dumps(tool_output)}, toolResponseMetadata: {json.dumps(tool_response_metadata)} }},",
                "  addEventListener(name, callback) { listeners[name] = callback; },",
                "};",
                match.group(1),
                f"const messageParams = {json.dumps(message_params)};",
                "if (messageParams) {",
                "  listeners.message({ source: window.parent, data: { jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: messageParams } });",
                "}",
                "console.log(rootNode.innerHTML);",
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script_path)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_tool_card_renders_direct_window_openai_tool_output(tmp_path):
    html = _render_tool_card_with_node(
        tmp_path,
        tool_output={
            "workspace_id": "ws_test",
            "workspace_name": "PatchBay",
            "root": "/repo",
            "tree": "README.md\nsrc/patchbay/protocol/resources.py",
        },
    )

    assert "Waiting for tool result" not in html
    assert "Workspace" in html
    assert "ws_test" in html
    assert "README.md" in html


def test_tool_card_renders_standard_tool_result_notification(tmp_path):
    html = _render_tool_card_with_node(
        tmp_path,
        tool_output={},
        message_params={
            "structuredContent": {
                "worker_id": "worker_test",
                "name": "Card Hydration Worker",
                "state": "idle",
                "report": "Rendered from ui notification.",
            },
            "content": [],
        },
    )

    assert "Waiting for tool result" not in html
    assert "Card Hydration Worker" in html
    assert "Rendered from ui notification." in html


def test_tool_card_falls_back_from_empty_tool_output_to_response_metadata(tmp_path):
    html = _render_tool_card_with_node(
        tmp_path,
        tool_output={},
        tool_response_metadata={
            "mcp_tool_result": {
                "structuredContent": {
                    "workspace_id": "ws_metadata",
                    "workspace_name": "Metadata Workspace",
                    "tree": "metadata.md",
                },
                "content": [],
            }
        },
    )

    assert "Waiting for tool result" not in html
    assert "Metadata Workspace" in html
    assert "metadata.md" in html


def test_tool_card_renders_worker_options_direct_tool_output(tmp_path):
    html = _render_tool_card_with_node(
        tmp_path,
        tool_output={
            "source": "runtime_catalog",
            "default_model": "gpt-5.4",
            "default_reasoning_effort": "medium",
            "model_count": 2,
            "models": [
                {"id": "gpt-5.4", "recommended_for": "major worker"},
                {"id": "spark", "recommended_for": "fast reader"},
            ],
            "reasoning_efforts": [{"value": "medium", "description": "balanced"}],
            "next_step": "Pass model to codex_worker_start when needed.",
        },
    )

    assert "Waiting for tool result" not in html
    assert "Worker options" in html
    assert "gpt-5.4" in html
    assert "spark" in html


def test_tool_card_renders_tool_mode_direct_tool_output(tmp_path):
    html = _render_tool_card_with_node(
        tmp_path,
        tool_output={
            "current_mode": "worker",
            "default_mode": "worker",
            "recommended_default": "worker",
            "available_modes": ["worker", "full"],
            "modes": [
                {"mode": "worker", "current": True, "tool_count": 8, "purpose": "Worker-first"},
                {"mode": "full", "current": False, "tool_count": 32, "purpose": "All tools"},
            ],
            "chatgpt_refresh_note": "Refresh connector after mode changes.",
        },
    )

    assert "Waiting for tool result" not in html
    assert "Tool modes" in html
    assert "worker - 8 tools" in html
    assert "Refresh connector" in html
