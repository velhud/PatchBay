"""MCP resource templates for ChatGPT Apps-compatible tool cards."""
from __future__ import annotations

from typing import Any, Dict, List


TOOL_CARD_URI = "ui://widget/codex-mcp-wrapper-tool-card-v1.html"
TOOL_CARD_MIME_TYPE = "text/html;profile=mcp-app"
DEFAULT_WIDGET_DOMAIN = "https://web-sandbox.oaiusercontent.com"


TOOL_CARD_HTML = r"""
<div id="root" class="wrap">
  <article class="card pending">
    <header>
      <span class="mark">C</span>
      <div>
        <h1>Codex MCP</h1>
        <p>Waiting for tool result</p>
      </div>
      <span class="pill">ready</span>
    </header>
    <pre>{}</pre>
  </article>
</div>

<style>
  :root {
    color-scheme: dark light;
    --panel: #11151c;
    --panel-2: #171d26;
    --line: rgba(222, 228, 238, 0.16);
    --text: #f2f5f9;
    --muted: #98a3b3;
    --accent: #d7b56d;
    --ok: #85d89b;
    --warn: #edc86f;
    --bad: #f19a9a;
    --info: #9fc6ff;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: transparent;
    color: var(--text);
    font: 12px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    letter-spacing: 0;
  }

  .wrap { width: 100%; }

  .card {
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: linear-gradient(180deg, rgba(255,255,255,0.04), transparent), var(--panel);
  }

  header {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    padding: 11px 12px;
    border-bottom: 1px solid var(--line);
  }

  .mark {
    display: inline-grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border: 1px solid rgba(215,181,109,0.32);
    border-radius: 8px;
    color: var(--accent);
    font-size: 10px;
    font-weight: 800;
  }

  h1, p { margin: 0; }
  h1 {
    overflow: hidden;
    font-size: 12px;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  p {
    overflow: hidden;
    margin-top: 2px;
    color: var(--muted);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pill {
    max-width: 20ch;
    overflow: hidden;
    padding: 2px 7px;
    border: 1px solid var(--line);
    border-radius: 999px;
    color: var(--info);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pill.ok { color: var(--ok); }
  .pill.warn { color: var(--warn); }
  .pill.bad { color: var(--bad); }

  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    padding: 10px 12px 12px;
  }

  .row {
    min-width: 0;
    padding: 8px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--panel-2);
  }

  .label {
    color: var(--muted);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
  }

  .value {
    overflow: hidden;
    margin-top: 3px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  pre {
    max-height: 260px;
    overflow: auto;
    margin: 0;
    padding: 10px 12px 12px;
    border-top: 1px solid var(--line);
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
  }

  @media (max-width: 520px) {
    .grid { grid-template-columns: 1fr; }
  }
</style>

<script type="module">
  const root = document.getElementById("root");
  const MAX_TEXT = 3600;
  const LABELS = {
    workspace_id: "Workspace",
    job_id: "Job",
    session_id: "Session",
    state: "State",
    status: "Status",
    operation_type: "Operation",
    path: "Path",
    files_changed: "Files",
    changed: "Changed",
    truncated: "Truncated"
  };

  function esc(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function compact(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.slice(0, 5).map(compact).join(", ");
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function titleFor(data) {
    if (data.operation_type) return data.operation_type;
    if (data.tool_name) return data.tool_name;
    if (data.name) return data.name;
    if (data.path) return data.path;
    return "Codex MCP";
  }

  function subtitleFor(data) {
    if (data.note) return data.note;
    if (data.summary) return data.summary;
    if (data.error) return data.error;
    if (data.text && typeof data.text === "string") return data.text.split(/\r?\n/)[0];
    return "Tool result";
  }

  function pillFor(data) {
    const raw = String(data.status || data.state || (data.error ? "error" : "ready"));
    const lower = raw.toLowerCase();
    const klass = lower.includes("fail") || lower.includes("error") ? "bad" :
      lower.includes("cancel") || lower.includes("warn") ? "warn" :
      lower.includes("complete") || lower.includes("ready") || lower.includes("ok") ? "ok" : "";
    return { text: raw, klass };
  }

  function renderRows(data) {
    const rows = Object.keys(LABELS)
      .filter((key) => data[key] !== undefined && data[key] !== null && compact(data[key]) !== "")
      .slice(0, 8)
      .map((key) => {
        return '<div class="row"><div class="label">' + esc(LABELS[key]) +
          '</div><div class="value">' + esc(compact(data[key])) + '</div></div>';
      });
    return rows.length ? '<section class="grid">' + rows.join("") + '</section>' : "";
  }

  function render(toolResult) {
    const data = toolResult?.structuredContent && typeof toolResult.structuredContent === "object"
      ? toolResult.structuredContent
      : {};
    const pill = pillFor(data);
    let preview = JSON.stringify(data, null, 2) || "{}";
    if (preview.length > MAX_TEXT) preview = preview.slice(0, MAX_TEXT) + "\n...[truncated in widget]";
    root.innerHTML =
      '<article class="card">' +
      '<header><span class="mark">C</span><div><h1>' + esc(titleFor(data)) +
      '</h1><p>' + esc(subtitleFor(data)) + '</p></div><span class="pill ' + pill.klass + '">' +
      esc(pill.text) + '</span></header>' +
      renderRows(data) +
      '<pre>' + esc(preview) + '</pre>' +
      '</article>';
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.method !== "ui/notifications/tool-result") return;
    render(message.params);
  }, { passive: true });
</script>
""".strip()


def widget_domain(config: Dict[str, Any] | None = None) -> str:
    app_config = (config or {}).get("app", {})
    value = app_config.get("widget_domain") if isinstance(app_config, dict) else None
    if isinstance(value, str) and value.startswith("https://") and value.rstrip("/") == value:
        return value
    return DEFAULT_WIDGET_DOMAIN


def tool_resource_meta(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    domain = widget_domain(config)
    csp = {"connectDomains": [], "resourceDomains": []}
    legacy_csp = {"connect_domains": [], "resource_domains": []}
    return {
        "ui": {
            "prefersBorder": True,
            "domain": domain,
            "csp": csp,
        },
        "openai/widgetDescription": (
            "Renders codex-mcp-wrapper workspace orientation, Codex job status, diffs, "
            "handoffs, power-tool results, and connector diagnostics as compact developer cards."
        ),
        "openai/widgetPrefersBorder": True,
        "openai/widgetDomain": domain,
        "openai/widgetCSP": legacy_csp,
    }


def tool_card_descriptor() -> Dict[str, Any]:
    return {
        "uri": TOOL_CARD_URI,
        "name": "codex-mcp-wrapper-tool-card",
        "title": "Codex MCP Wrapper Tool Card",
        "description": "Compact ChatGPT Apps card for Codex MCP Wrapper tool results.",
        "mimeType": TOOL_CARD_MIME_TYPE,
    }


def list_resource_templates() -> List[Dict[str, Any]]:
    return [tool_card_descriptor()]


def read_resource(uri: str, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if uri != TOOL_CARD_URI:
        raise ValueError(f"Unknown resource URI: {uri}")
    return {
        "contents": [
            {
                "uri": TOOL_CARD_URI,
                "mimeType": TOOL_CARD_MIME_TYPE,
                "text": TOOL_CARD_HTML,
                "_meta": tool_resource_meta(config),
            }
        ]
    }
