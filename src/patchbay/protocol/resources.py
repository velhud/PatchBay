"""MCP resource templates for ChatGPT Apps-compatible tool cards."""
from __future__ import annotations

from typing import Any, Dict, List


TOOL_CARD_URI = "ui://widget/patchbay-tool-card-v2.html"
TOOL_CARD_LEGACY_URIS = ["ui://widget/patchbay-tool-card-v1.html"]
TOOL_CARD_MIME_TYPE = "text/html;profile=mcp-app"
DEFAULT_WIDGET_DOMAIN = "https://web-sandbox.oaiusercontent.com"


TOOL_CARD_HTML = r"""
<div id="root" class="wrap">
  <article class="receipt pending">
    <span class="rail"></span>
    <div class="main">
      <div class="line"><span class="tool">PatchBay</span><span class="dot">·</span><span class="status">waiting</span></div>
      <div class="detail">Waiting for tool result</div>
    </div>
    <span class="pill info">ready</span>
  </article>
</div>

<style>
  :root {
    color-scheme: dark light;
    --panel: rgba(17, 21, 28, 0.74);
    --line: rgba(210, 218, 230, 0.16);
    --line-strong: rgba(210, 218, 230, 0.26);
    --text: #f2f4f7;
    --soft: #c9d0da;
    --muted: #8f99a8;
    --accent: #d7b56d;
    --blue: #9dc3ff;
    --green: #8edc99;
    --red: #f29a9a;
    --amber: #e8c978;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: transparent;
    color: var(--text);
    font: 12px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    letter-spacing: 0;
  }

  .wrap { width: 100%; }

  .receipt {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    min-height: 42px;
    max-height: 58px;
    overflow: hidden;
    padding: 7px 10px 7px 13px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel);
  }

  .rail {
    position: absolute;
    inset: 7px auto 7px 0;
    width: 3px;
    border-radius: 999px;
    background: var(--accent);
    opacity: 0.75;
  }

  .main { min-width: 0; }

  .line {
    display: flex;
    align-items: baseline;
    min-width: 0;
    gap: 5px;
    color: var(--text);
    font-size: 12px;
    font-weight: 760;
    white-space: nowrap;
  }

  .tool, .status, .detail {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tool {
    max-width: 24ch;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
  }

  .dot { color: var(--muted); }

  .status {
    min-width: 0;
    color: var(--soft);
  }

  .detail {
    margin-top: 2px;
    color: var(--muted);
    font-size: 11px;
    font-weight: 560;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    max-width: 16ch;
    min-height: 20px;
    overflow: hidden;
    padding: 2px 7px;
    border: 1px solid var(--line-strong);
    border-radius: 999px;
    color: var(--muted);
    font-size: 10px;
    font-weight: 760;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pill.good { color: var(--green); border-color: rgba(142, 220, 153, 0.32); }
  .pill.bad { color: var(--red); border-color: rgba(242, 154, 154, 0.32); }
  .pill.warn { color: var(--amber); border-color: rgba(232, 201, 120, 0.32); }
  .pill.info { color: var(--blue); border-color: rgba(157, 195, 255, 0.28); }

  .receipt.bad .rail { background: var(--red); }
  .receipt.warn .rail { background: var(--amber); }
  .receipt.good .rail { background: var(--green); }
  .receipt.info .rail { background: var(--blue); }

  @media (max-width: 540px) {
    .receipt { grid-template-columns: minmax(0, 1fr); gap: 4px; }
    .pill { display: none; }
    .tool { max-width: 18ch; }
  }
</style>

<script>
  const root = document.getElementById("root");

  function esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function compact(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.slice(0, 3).map(compact).filter(Boolean).join(", ");
    if (typeof value === "object") return Object.entries(value).slice(0, 3).map(([key, val]) => key + "=" + compact(val)).join(", ");
    return String(value);
  }

  function oneLine(value, max = 120) {
    const text = compact(value).replace(/\s+/g, " ").trim();
    if (!text) return "";
    return text.length > max ? text.slice(0, Math.max(0, max - 1)).trimEnd() + "…" : text;
  }

  function basename(value) {
    const text = String(value || "");
    return text.split("/").filter(Boolean).pop() || text || ".";
  }

  function countLines(value) {
    const text = String(value || "");
    if (!text) return 0;
    return text.replace(/\n$/, "").split("\n").length;
  }

  function listCount(value) {
    return Array.isArray(value) ? value.length : 0;
  }

  function boolText(value) {
    if (value === true) return "yes";
    if (value === false) return "no";
    return value ?? "-";
  }

  const TOOL_LABELS = {
    codex_open_workspace: "Workspace opening",
    open_current_workspace: "Workspace opening",
    open_workspace: "Workspace opening",
    codex_repo_tree: "Repository tree",
    tree: "Repository tree",
    codex_read_file: "File reading",
    read: "File reading",
    codex_search_repo: "Repository search",
    search: "Repository search",
    codex_load_context: "Context loading",
    codex_context: "Context loading",
    codex_export_context: "Context export",
    export_pro_context: "Context export",
    codex_list_workspaces: "Workspace list",
    list_workspaces: "Workspace list",
    codex_workspace_snapshot: "Workspace snapshot",
    workspace_snapshot: "Workspace snapshot",
    codex_inventory: "Inventory check",
    codex_list_skills: "Skill list",
    codex_load_skill: "Skill loading",
    load_skill: "Skill loading",
    codex_write_handoff: "Handoff writing",
    handoff_to_agent: "Handoff writing",
    handoff_to_codex: "Handoff writing",
    codex_get_handoff_status: "Handoff reading",
    read_handoff: "Handoff reading",
    codex_get_handoff_diff: "Handoff diff",
    codex_git_status: "Git status",
    git_status: "Git status",
    codex_git_diff: "Git diff",
    git_diff: "Git diff",
    codex_show_changes: "Change review",
    show_changes: "Change review",
    codex_write_file: "File writing",
    write: "File writing",
    codex_edit_file: "File editing",
    edit: "File editing",
    codex_run_command: "Command run",
    bash: "Command run",
    codex_plan_job: "Planning job",
    codex_apply_job: "Implementation job",
    codex_get_status: "Job status",
    codex_get_result: "Job result",
    codex_get_diff: "Job diff",
    codex_cancel_job: "Job cancellation",
    codex_review: "Code review",
    codex_resume: "Session resume",
    codex_list_sessions: "Session list",
    codex_sessions: "Session list",
    codex_read_session: "Session reading",
    read_codex_session: "Session reading",
    codex_interactive: "Interactive Codex",
    codex_interactive_reply: "Codex continuation",
    codex_worker_options: "Worker options",
    worker_options: "Worker options",
    codex_worker_inbox: "Worker inbox",
    worker_inbox: "Worker inbox",
    codex_worker_start: "Worker creation",
    worker_start: "Worker creation",
    codex_worker_message: "Worker message",
    worker_message: "Worker message",
    codex_worker_list: "Worker list",
    worker_list: "Worker list",
    codex_worker_status: "Worker status",
    worker_status: "Worker status",
    codex_worker_wait: "Worker wait",
    worker_wait: "Worker wait",
    codex_worker_inspect: "Worker inspection",
    worker_inspect: "Worker inspection",
    codex_worker_integrate: "Worker integration",
    worker_integrate: "Worker integration",
    codex_worker_stop: "Worker stop",
    worker_stop: "Worker stop",
    codex_pro_request_list: "Pro request list",
    pro_request_list: "Pro request list",
    codex_pro_request_read: "Pro request reading",
    pro_request_read: "Pro request reading",
    codex_pro_request_claim: "Pro request claim",
    pro_request_claim: "Pro request claim",
    codex_pro_request_respond: "Pro response storage",
    pro_request_respond: "Pro response storage",
    codex_pro_request_dispatch: "Pro response dispatch",
    pro_request_dispatch: "Pro response dispatch",
    codex_pro_request_close: "Pro request close",
    pro_request_close: "Pro request close",
    codex_self_test: "Connector check",
    self_test: "Connector check",
    codex_get_config: "Configuration reading",
    server_config: "Configuration reading",
    codex_tool_mode_info: "Tool mode check",
    tool_mode_info: "Tool mode check",
    codex_tool_mode_switch: "Tool mode switch",
    tool_mode_switch: "Tool mode switch",
    widget: "Widget"
  };

  const KIND_LABELS = {
    repo_busy: "Repository busy",
    worker_list: "Worker list",
    artifact: "Worker inbox",
    worker: "Worker status",
    job: "Job status",
    command: "Command run",
    diff: "Change review",
    workspace: "Workspace opening",
    self_test: "Connector check",
    worker_options: "Worker options",
    tool_mode: "Tool mode check",
    sessions: "Session list",
    text: "File reading",
    generic: "PatchBay"
  };

  function displayText(value, max = 120) {
    return oneLine(value, max)
      .replace(/\bcodex_/gi, "")
      .replaceAll("_", " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function inferKind(data) {
    if (data?.repo_busy) return "repo_busy";
    if (Array.isArray(data?.workers) || data?.team_report) return "worker_list";
    if (data?.artifact_id || Array.isArray(data?.artifacts) || Array.isArray(data?.top_level_entries)) return "artifact";
    if (data?.worker_id || data?.integration_state || data?.can_apply !== undefined || data?.applied !== undefined || data?.workspace_mode) return "worker";
    if (data?.reference_id || data?.job_id || data?.session_ref || data?.delta_content) return "job";
    if (data?.command || data?.exit_code !== undefined || data?.stdout !== undefined || data?.stderr !== undefined) return "command";
    if (data?.diff || data?.status_short || data?.files_changed || data?.changed_files) return "diff";
    if (Array.isArray(data?.models) || Array.isArray(data?.reasoning_efforts) || data?.model_selection_guidance || data?.default_model) return "worker_options";
    if (Array.isArray(data?.available_modes) || Array.isArray(data?.modes) || data?.current_mode) return "tool_mode";
    if (Array.isArray(data?.checks) || data?.coordination || data?.connection) return "self_test";
    if (Array.isArray(data?.sessions)) return "sessions";
    if (data?.workspace_id || data?.tree || data?.git || data?.skill_counts || data?.root) return "workspace";
    if (data?.text || data?.path || data?.file_path) return "text";
    return "generic";
  }

  function rawToolName(data, meta) {
    return String(
      meta?.["patchbay/tool_name"] ||
      meta?.tool_name ||
      meta?.toolName ||
      data?._meta?.["patchbay/tool_name"] ||
      data?._meta?.tool_name ||
      data?.tool_name ||
      data?.tool_id ||
      data?.codexpro_tool ||
      ""
    );
  }

  function normalizedToolKey(data, meta) {
    const raw = rawToolName(data, meta).trim();
    if (!raw) return "";
    if (TOOL_LABELS[raw]) return raw;
    const noPrefix = raw.replace(/^codex_/, "");
    if (TOOL_LABELS[noPrefix]) return noPrefix;
    const withPrefix = "codex_" + noPrefix;
    if (TOOL_LABELS[withPrefix]) return withPrefix;
    return raw;
  }

  function humanToolLabel(data, kind, meta) {
    const key = normalizedToolKey(data, meta);
    return TOOL_LABELS[key] || KIND_LABELS[kind] || "PatchBay";
  }

  function rawStatus(data, kind) {
    return String(data?.status || data?.state || data?.apply_check || data?.integration_state || data?.note || data?.message || "");
  }

  function humanStatusLabel(data, kind) {
    if (data?.error) return "failed";
    if (kind === "repo_busy") return "busy";
    if (kind === "command") {
      const code = Number(data.exit_code ?? data.exitCode ?? 0);
      return data.timed_out ? "timed out" : code === 0 ? "finished" : "failed";
    }
    if (kind === "diff") return data.changed === false ? "clean" : "changes found";
    if (kind === "worker_list") return (data.active ?? 0) + " active";
    if (kind === "worker_options") return "ready";
    if (kind === "tool_mode") return "ready";
    if (kind === "self_test") return data.ready === false || data.status === "fail" ? "not ready" : "ready";
    const raw = rawStatus(data, kind).toLowerCase();
    if (["working", "running", "starting", "pending", "queued"].includes(raw)) return "in progress";
    if (["idle", "ready", "completed", "complete", "done", "success", "succeeded", "applied"].includes(raw)) return raw === "idle" ? "ready" : "finished";
    if (["failed", "error", "blocked", "lost", "cancelled", "canceled"].includes(raw)) return raw === "cancelled" || raw === "canceled" ? "cancelled" : "failed";
    if (raw === "repo_busy" || raw === "busy") return "busy";
    if (raw) return displayText(raw, 48);
    return "ready";
  }

  function toneFor(status, data, kind) {
    const text = String(status || "").toLowerCase();
    if (data?.error || text.includes("fail") || text.includes("error") || text.includes("blocked") || text.includes("timed out")) return "bad";
    if (kind === "repo_busy" || text.includes("busy") || text.includes("warn") || text.includes("dirty") || text.includes("conflict") || text.includes("stale")) return "warn";
    if (text.includes("done") || text.includes("ready") || text.includes("passed") || text.includes("clean") || text.includes("idle") || text.includes("complete") || text.includes("finished")) return "good";
    return "info";
  }

  function pillFor(tone) {
    if (tone === "bad") return "Issue";
    if (tone === "warn") return "Attention";
    if (tone === "good") return "Done";
    return "Info";
  }

  function humanDetailLine(data, kind, meta) {
    const toolKey = normalizedToolKey(data, meta);
    if (data?.error) return displayText(data.error, 150);
    if (kind === "repo_busy") return displayText(data.note || data.operation || "Repository mutation lock is held.", 150);
    if (kind === "worker") {
      const name = data.name || data.worker || data.worker_id || "worker";
      const live = data.liveness?.status || data.latest_turn?.progress || data.report || data.note;
      if (toolKey.includes("worker_start")) return displayText(name + " started", 150);
      if (toolKey.includes("worker_message")) return displayText("Message sent to " + name, 150);
      if (toolKey.includes("worker_stop")) return displayText(name + " stopped", 150);
      if (toolKey.includes("worker_integrate")) return displayText(data.applied ? "Worker result applied" : (live || name), 150);
      return displayText(name + (live ? " - " + live : ""), 150);
    }
    if (kind === "worker_list") {
      const count = data.count ?? listCount(data.workers);
      const active = data.active ?? 0;
      return displayText(count + " workers, " + active + " active" + (data.team_report ? " - " + data.team_report : ""), 150);
    }
    if (kind === "workspace") return displayText(data.workspace_name || basename(data.root || data.path) || data.workspace_id || "workspace opened", 150);
    if (kind === "command") return displayText("Exit " + (data.exit_code ?? data.exitCode ?? "-") + ", " + countLines(data.stdout) + " output lines, " + countLines(data.stderr) + " error lines", 150);
    if (kind === "diff") {
      const files = data.files_changed || data.changed_files || data.status_short || [];
      return displayText((data.changed === false ? "clean" : (listCount(files) || data.change_count || 0) + " files") + (data.path || data.record_path ? " - " + basename(data.path || data.record_path) : ""), 150);
    }
    if (kind === "text") return displayText(basename(data.path || data.file_path || "text") + (data.truncated ? " - truncated" : ""), 150);
    if (kind === "artifact") return displayText((data.count ?? listCount(data.artifacts) ?? 0) + " artifacts" + (data.label ? " - " + data.label : ""), 150);
    if (kind === "job") return displayText(data.reference_id || data.job_id || data.session_ref || data.message || data.note || "job accepted", 150);
    if (kind === "worker_options") return displayText((data.model_count ?? listCount(data.models)) + " models" + (data.default_model ? ", default " + data.default_model : ""), 150);
    if (kind === "tool_mode") return displayText("Current " + (data.current_mode || data.default_mode || "-") + (data.recommended_default ? ", recommended " + data.recommended_default : ""), 150);
    if (kind === "self_test") return displayText((data.tool_count !== undefined ? data.tool_count + " tools" : "checks complete") + (data.public_tunnel_used ? ", public tunnel" : ""), 150);
    if (kind === "sessions") return displayText((data.count ?? listCount(data.sessions)) + " sessions", 150);
    return displayText(data.summary || data.message || data.note || data.path || data.file_path || "Tool result ready", 150);
  }

  function renderReceipt(data, meta = {}) {
    const kind = inferKind(data);
    const status = displayText(humanStatusLabel(data, kind), 48);
    const tone = toneFor(status, data, kind);
    const detail = displayText(humanDetailLine(data, kind, meta), 150);
    const label = displayText(humanToolLabel(data, kind, meta), 42);
    const pill = pillFor(tone);
    root.innerHTML = [
      '<article class="receipt ' + esc(tone) + '">',
      '<span class="rail"></span>',
      '<div class="main">',
      '<div class="line"><span class="tool">' + esc(label) + '</span><span class="dot">·</span><span class="status">' + esc(status || "ready") + '</span></div>',
      '<div class="detail">' + esc(detail || "Tool result ready") + '</div>',
      '</div>',
      '<span class="pill ' + esc(tone) + '">' + esc(pill) + '</span>',
      '</article>'
    ].join("");
  }

  function isPlaceholderPayload(data) {
    if (!data || typeof data !== "object") return true;
    return Object.keys(data).length === 0;
  }

  function renderPending() {
    root.innerHTML = '<article class="receipt pending"><span class="rail"></span><div class="main"><div class="line"><span class="tool">PatchBay</span><span class="dot">·</span><span class="status">waiting</span></div><div class="detail">Waiting for tool result</div></div><span class="pill info">Info</span></article>';
  }

  function renderWidgetError(error, data) {
    const message = oneLine(error?.message || String(error || "Unknown widget error"), 140);
    renderReceipt({ error: message, message, payload_present: !!data }, { "patchbay/tool_name": "widget" });
  }

  function render(data, meta = {}) {
    try {
      if (isPlaceholderPayload(data)) {
        renderPending();
        return;
      }
      renderReceipt(data, meta);
    } catch (error) {
      renderWidgetError(error, data);
    }
  }

  function extractMeta(value) {
    if (!value || typeof value !== "object") return {};
    const candidates = [
      value._meta,
      value.toolOutput?._meta,
      value.toolResponseMetadata?._meta,
      value.mcp_tool_result?._meta,
      value.call_tool_result?._meta,
      value.result?._meta,
      value.params?._meta
    ];
    for (const candidate of candidates) {
      if (candidate && typeof candidate === "object") return candidate;
    }
    return {};
  }

  function extractStructuredContent(value) {
    if (!value || typeof value !== "object") return {};
    const candidates = [
      value.structuredContent,
      value.toolOutput?.structuredContent,
      value.toolOutput,
      value.toolResponseMetadata?.structuredContent,
      value.mcp_tool_result?.structuredContent,
      value.call_tool_result?.structuredContent,
      value.result?.structuredContent,
      value.params?.structuredContent
    ];
    for (const candidate of candidates) {
      if (candidate && typeof candidate === "object") return candidate;
    }
    if (Object.keys(value).length > 0) return value;
    return {};
  }

  function firstToolPayload(...values) {
    for (const value of values) {
      const structured = extractStructuredContent(value);
      if (!isPlaceholderPayload(structured)) return { data: structured, meta: extractMeta(value) };
    }
    return { data: {}, meta: {} };
  }

  const initialPayload = firstToolPayload(window.openai?.toolOutput, window.openai?.toolResponseMetadata);
  render(initialPayload.data, initialPayload.meta);

  window.addEventListener("openai:set_globals", (event) => {
    const payload = firstToolPayload(
      event.detail?.globals?.toolOutput ||
      event.detail?.globals?.structuredContent,
      event.detail?.globals?.toolResponseMetadata,
      event.detail,
      window.openai?.toolOutput,
      window.openai?.toolResponseMetadata
    );
    render(payload.data, payload.meta);
  }, { passive: true });

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.method === "ui/notifications/tool-result") {
      render(extractStructuredContent(message.params || {}), extractMeta(message.params || {}));
    }
  }, { passive: true });
</script>
""".strip()


def widget_domain(config: Dict[str, Any] | None = None) -> str:
    app_config = (config or {}).get("app", {})
    value = app_config.get("widget_domain") if isinstance(app_config, dict) else None
    if isinstance(value, str) and value.startswith("https://") and value.rstrip("/") == value:
        return value
    return DEFAULT_WIDGET_DOMAIN


def tool_cards_enabled(config: Dict[str, Any] | None = None) -> bool:
    """Return whether ChatGPT Apps widget cards should be advertised."""
    app_config = (config or {}).get("app", {})
    value = app_config.get("tool_cards") if isinstance(app_config, dict) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


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
            "Renders PatchBay tool results as compact status receipts while leaving full structured output "
            "available to ChatGPT."
        ),
        "openai/widgetPrefersBorder": True,
        "openai/widgetDomain": domain,
        "openai/widgetCSP": legacy_csp,
    }


def tool_card_descriptor() -> Dict[str, Any]:
    return {
        "uri": TOOL_CARD_URI,
        "name": "patchbay-tool-card",
        "title": "PatchBay Tool Card",
        "description": "Compact ChatGPT Apps receipt for PatchBay tool results.",
        "mimeType": TOOL_CARD_MIME_TYPE,
    }


def list_resource_templates() -> List[Dict[str, Any]]:
    return [tool_card_descriptor()]


def list_resources(config: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    if not tool_cards_enabled(config):
        return []
    return [tool_card_descriptor()]


def read_resource(uri: str, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not tool_cards_enabled(config):
        raise ValueError("PatchBay tool cards are disabled by server config")
    if uri != TOOL_CARD_URI and uri not in TOOL_CARD_LEGACY_URIS:
        raise ValueError(f"Unknown resource URI: {uri}")
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": TOOL_CARD_MIME_TYPE,
                "text": TOOL_CARD_HTML,
                "_meta": tool_resource_meta(config),
            }
        ]
    }
