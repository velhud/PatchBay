from patchbay.protocol.resources import (
    DEFAULT_WIDGET_DOMAIN,
    TOOL_CARD_HTML,
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
            "description": "Compact ChatGPT Apps card for PatchBay tool results.",
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
