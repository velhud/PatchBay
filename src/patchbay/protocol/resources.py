"""MCP resource templates for ChatGPT Apps-compatible tool cards."""
from __future__ import annotations

from typing import Any, Dict, List


TOOL_CARD_URI = "ui://widget/patchbay-tool-card-v2.html"
TOOL_CARD_LEGACY_URIS = ["ui://widget/patchbay-tool-card-v1.html"]
TOOL_CARD_MIME_TYPE = "text/html;profile=mcp-app"
DEFAULT_WIDGET_DOMAIN = "https://web-sandbox.oaiusercontent.com"


TOOL_CARD_HTML = r"""
<div id="root" class="wrap">
  <article class="card pending">
    <div class="rail"></div>
    <header class="head">
      <span class="glyph">P</span>
      <div class="headline">
        <div class="title">PatchBay</div>
        <div class="subtitle">Waiting for tool result...</div>
      </div>
      <span class="pill info">waiting</span>
    </header>
    <div class="skeleton"><span></span><span></span><span></span></div>
  </article>
</div>

<style>
  :root {
    color-scheme: dark light;
    --panel: #11151c;
    --panel-2: #161b24;
    --panel-3: #0c1016;
    --line: rgba(212, 219, 229, 0.13);
    --line-strong: rgba(212, 219, 229, 0.24);
    --text: #f2f4f7;
    --soft: #c9d0da;
    --muted: #97a1af;
    --quiet: #6f7988;
    --accent: #d7b56d;
    --blue: #9dc3ff;
    --green: #8edc99;
    --red: #f29a9a;
    --amber: #e8c978;
    --shadow: rgba(0, 0, 0, 0.26);
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
    position: relative;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 8px;
    background:
      radial-gradient(circle at 18px 0, rgba(215, 181, 109, 0.12), transparent 42px),
      linear-gradient(180deg, rgba(255, 255, 255, 0.042), rgba(255, 255, 255, 0)),
      var(--panel);
    box-shadow: 0 14px 34px var(--shadow);
  }

  .rail {
    position: absolute;
    inset: 0 auto 0 0;
    width: 3px;
    background: linear-gradient(180deg, var(--accent), rgba(142, 220, 153, 0.75) 64%, transparent);
    opacity: 0.88;
  }

  .head {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    min-height: 56px;
    padding: 11px 12px 10px 14px;
    border-bottom: 1px solid var(--line);
  }

  .glyph {
    display: inline-grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border: 1px solid rgba(215, 181, 109, 0.28);
    border-radius: 8px;
    background: linear-gradient(180deg, rgba(215, 181, 109, 0.16), rgba(215, 181, 109, 0.04));
    color: var(--accent);
    font-size: 10px;
    font-weight: 900;
  }

  .headline { min-width: 0; }

  .title {
    overflow: hidden;
    color: var(--text);
    font-size: 12px;
    font-weight: 760;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .subtitle {
    overflow: hidden;
    margin-top: 2px;
    color: var(--muted);
    font-size: 11px;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
    min-width: 0;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    min-height: 20px;
    max-width: 22ch;
    overflow: hidden;
    padding: 2px 8px;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.035);
    color: var(--muted);
    font-size: 10px;
    font-weight: 720;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .pill.good { color: var(--green); border-color: rgba(134, 239, 172, 0.28); background: rgba(134, 239, 172, 0.08); }
  .pill.bad { color: var(--red); border-color: rgba(253, 164, 175, 0.28); background: rgba(253, 164, 175, 0.08); }
  .pill.info { color: var(--blue); border-color: rgba(157, 195, 255, 0.28); background: rgba(157, 195, 255, 0.08); }
  .pill.warn { color: var(--amber); border-color: rgba(253, 230, 138, 0.28); background: rgba(253, 230, 138, 0.08); }

  .body {
    max-height: 420px;
    overflow: auto;
    padding: 10px;
  }

  .summary, .metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    margin-bottom: 10px;
  }

  .summary-item, .metric {
    min-width: 0;
    padding: 8px 9px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.025);
  }

  .summary-label, .metric .label {
    display: block;
    margin-bottom: 4px;
    color: var(--quiet);
    font-size: 10px;
    font-weight: 850;
    text-transform: uppercase;
  }

  .summary-value {
    color: var(--text);
    font-size: 15px;
    font-variant-numeric: tabular-nums;
    font-weight: 760;
  }

  .metric .value {
    overflow: hidden;
    color: var(--soft);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .section-label {
    margin: 10px 1px 6px;
    color: var(--quiet);
    font-size: 10px;
    font-weight: 850;
    text-transform: uppercase;
  }

  .file-list {
    display: grid;
    gap: 4px;
    margin-bottom: 10px;
  }

  .file-row {
    display: grid;
    grid-template-columns: 48px minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    padding: 7px 8px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.022);
  }

  .file-code {
    overflow: hidden;
    color: var(--accent);
    font: 10px/1.2 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    font-weight: 800;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-name {
    overflow: hidden;
    color: var(--soft);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .empty {
    padding: 10px;
    border: 1px dashed var(--line-strong);
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.018);
    color: var(--muted);
  }

  .fold {
    margin-top: 8px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.018);
  }

  .fold > summary {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    min-height: 34px;
    padding: 8px 10px;
    cursor: pointer;
    color: var(--soft);
    font-weight: 760;
    list-style: none;
  }

  .fold > summary::-webkit-details-marker { display: none; }

  .fold-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .fold-count {
    color: var(--muted);
    font-size: 10px;
    font-weight: 800;
  }

  .fold-body { padding: 0 8px 8px; }

  .code {
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--panel-3);
  }

  .codebar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-height: 30px;
    padding: 6px 9px;
    border-bottom: 1px solid var(--line);
    background: var(--panel-2);
    color: var(--muted);
    font-size: 11px;
    font-weight: 720;
  }

  pre {
    margin: 0;
    padding: 10px;
    overflow: visible;
    color: var(--soft);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    font-size: 11px;
    line-height: 1.52;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .diff-line { display: block; min-height: 18px; padding: 0 4px; border-radius: 3px; }
  .diff-add { color: var(--green); background: rgba(142, 220, 153, 0.08); }
  .diff-del { color: var(--red); background: rgba(242, 154, 154, 0.08); }
  .diff-hunk { color: var(--blue); }
  .terminal pre { color: #dbe7f5; }
  .prompt { color: var(--accent); }

  .hit {
    display: grid;
    grid-template-columns: minmax(120px, 0.34fr) minmax(0, 1fr);
    gap: 8px;
    padding: 6px 8px;
    border-radius: 7px;
  }

  .hit:nth-child(odd) { background: rgba(255, 255, 255, 0.025); }

  .hit-file {
    overflow: hidden;
    color: var(--blue);
    font-weight: 850;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .hit-text {
    color: var(--soft);
    overflow-wrap: anywhere;
  }

  .skeleton {
    display: grid;
    gap: 7px;
    padding: 11px 13px 13px 17px;
  }

  .skeleton span {
    height: 8px;
    max-width: 78%;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(148, 163, 184, 0.12), rgba(148, 163, 184, 0.22), rgba(148, 163, 184, 0.12));
    animation: patchbay-sheen 1.55s ease-in-out infinite;
  }

  .skeleton span:nth-child(2) { max-width: 52%; animation-delay: 0.12s; }
  .skeleton span:nth-child(3) { max-width: 66%; animation-delay: 0.24s; }

  @keyframes patchbay-sheen {
    0%, 100% { opacity: 0.46; transform: translateX(0); }
    50% { opacity: 1; transform: translateX(2px); }
  }

  @media (max-width: 640px) {
    .head { grid-template-columns: 28px minmax(0, 1fr); }
    .meta { grid-column: 1 / -1; justify-content: flex-start; }
    .summary, .metrics, .hit { grid-template-columns: 1fr; }
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

  function truncate(value, max = 9000) {
    const text = String(value ?? "");
    return text.length > max ? text.slice(0, max) + "\n...[truncated in widget]" : text;
  }

  function countLines(value) {
    const text = String(value || "");
    if (!text) return 0;
    return text.replace(/\n$/, "").split("\n").length;
  }

  function previewLines(value, maxLines = 18) {
    const text = String(value || "").replace(/\n$/, "");
    if (!text) return "";
    const lines = text.split("\n");
    const shown = lines.slice(0, maxLines).join("\n");
    const remaining = lines.length - maxLines;
    return remaining > 0 ? shown + "\n...[" + remaining + " more lines]" : shown;
  }

  function basename(value) {
    const text = String(value || "");
    return text.split("/").filter(Boolean).pop() || text || ".";
  }

  function compact(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.slice(0, 5).map(compact).join(", ");
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function boolText(value) {
    if (value === true) return "yes";
    if (value === false) return "no";
    return value ?? "-";
  }

  function pill(text, cls) {
    if (text === undefined || text === null || text === "") return "";
    return '<span class="pill ' + esc(cls || "") + '">' + esc(text) + '</span>';
  }

  function summaryItem(label, value) {
    return '<div class="summary-item"><span class="summary-label">' + esc(label) + '</span><div class="summary-value">' + esc(value ?? "-") + '</div></div>';
  }

  function metric(label, value) {
    return '<div class="metric"><span class="label">' + esc(label) + '</span><div class="value">' + esc(compact(value) || "-") + '</div></div>';
  }

  function codebox(label, text, extraClass) {
    return '<div class="code ' + esc(extraClass || "") + '"><div class="codebar"><span>' + esc(label || "output") + '</span></div><pre>' + text + '</pre></div>';
  }

  function fold(title, count, body, open) {
    if (!body) return "";
    return '<details class="fold"' + (open ? " open" : "") + '><summary><span class="fold-title">' + esc(title) + '</span><span class="fold-count">' + esc(count || "") + '</span></summary><div class="fold-body">' + body + '</div></details>';
  }

  function fileRow(code, name) {
    return '<div class="file-row"><span class="file-code">' + esc(code || "-") + '</span><span class="file-name">' + esc(name || "") + '</span></div>';
  }

  function fileRows(items, code = "file", max = 16) {
    const values = Array.isArray(items) ? items : [];
    const rows = values.slice(0, max).map((item) => {
      if (typeof item === "string") return fileRow(code, item);
      return fileRow(item?.status || item?.kind || item?.source || code, item?.path || item?.file_path || item?.name || item?.label || JSON.stringify(item));
    }).join("");
    const more = values.length > max ? '<div class="empty">+' + esc(values.length - max) + ' more</div>' : "";
    return rows ? '<div class="file-list">' + rows + more + '</div>' : "";
  }

  function renderDiff(diff) {
    return truncate(diff, 14000).split("\n").map((line) => {
      let cls = "diff-line";
      if (line.startsWith("+") && !line.startsWith("+++")) cls += " diff-add";
      else if (line.startsWith("-") && !line.startsWith("---")) cls += " diff-del";
      else if (line.startsWith("@@")) cls += " diff-hunk";
      return '<span class="' + cls + '">' + esc(line) + '</span>';
    }).join("");
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
    if (data?.workspace_id || data?.tree || data?.git || data?.skill_counts) return "workspace";
    if (data?.text || data?.path || data?.file_path) return "text";
    return "generic";
  }

  function titleFor(data, kind) {
    if (data?.tool_name) return data.tool_name;
    if (kind === "repo_busy") return "Repository busy";
    if (kind === "worker_list") return "Workers";
    if (kind === "worker") return data.name || data.worker || "Worker";
    if (kind === "artifact") return data.label || data.original_file_name || data.artifact_id || "Artifact inbox";
    if (kind === "job") return data.mode ? "Codex " + data.mode : "Codex job";
    if (kind === "command") return "Command";
    if (kind === "self_test") return "Self-test";
    if (kind === "worker_options") return "Worker options";
    if (kind === "tool_mode") return "Tool modes";
    if (kind === "sessions") return "Codex sessions";
    if (kind === "workspace") return "Workspace";
    if (kind === "diff") return data.path || data.record_path || "Changes";
    if (kind === "text") return data.path || data.file_path || "File";
    return "PatchBay";
  }

  function iconFor(kind) {
    if (kind === "worker" || kind === "worker_list") return "W";
    if (kind === "artifact") return "A";
    if (kind === "job") return "J";
    if (kind === "command") return "$";
    if (kind === "diff") return "D";
    if (kind === "workspace") return "R";
    if (kind === "self_test") return "T";
    if (kind === "worker_options") return "M";
    if (kind === "tool_mode") return "T";
    if (kind === "repo_busy") return "!";
    if (kind === "sessions") return "S";
    return "P";
  }

  function subtitleFor(data, kind) {
    if (data?.report) return data.report;
    if (data?.latest_report) return data.latest_report;
    if (data?.note) return data.note;
    if (data?.summary) return data.summary;
    if (data?.error) return data.error;
    if (data?.message) return data.message;
    if (kind === "worker_list") return (data.count ?? data.workers?.length ?? 0) + " workers";
    if (kind === "artifact") return data.kind || data.view || ((data.count ?? data.artifacts?.length) + " artifacts");
    if (kind === "job") return data.state || data.status || data.reference_id || data.job_id || "Job status";
    if (kind === "workspace") return data.workspace_name || data.root || "Repository context";
    if (kind === "worker_options") return data.default_model || data.source || "Model and reasoning menu";
    if (kind === "tool_mode") return data.current_mode || data.default_mode || "Available tool modes";
    if (kind === "command") return data.command || data.cwd || "Terminal result";
    if (kind === "repo_busy") return data.operation || "Mutation lock is held";
    if (kind === "sessions") return (data.count ?? data.sessions?.length ?? 0) + " sessions";
    if (data?.text && typeof data.text === "string") return data.text.split(/\r?\n/)[0];
    return "Tool output";
  }

  function header(data, kind, pills) {
    return [
      '<div class="rail"></div>',
      '<header class="head">',
      '<span class="glyph">' + esc(iconFor(kind)) + '</span>',
      '<div class="headline"><div class="title">' + esc(titleFor(data, kind)) + '</div><div class="subtitle">' + esc(subtitleFor(data, kind)) + '</div></div>',
      '<div class="meta">' + (pills || '') + '</div>',
      '</header>'
    ].join("");
  }

  function statusPill(data) {
    const raw = String(data?.status || data?.state || data?.apply_check || (data?.error ? "error" : "ready"));
    const lower = raw.toLowerCase();
    const cls = lower.includes("fail") || lower.includes("error") || lower.includes("blocked") ? "bad" :
      lower.includes("busy") || lower.includes("dirty") || lower.includes("warn") || lower.includes("conflict") ? "warn" :
      lower.includes("complete") || lower.includes("ready") || lower.includes("ok") || lower.includes("clean") || lower.includes("idle") ? "good" : "info";
    return pill(raw, cls);
  }

  function renderRepoBusy(data) {
    return '<article class="card">' + header(data, "repo_busy", pill("repo_busy", "warn") + pill(data.operation || "locked", "info")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Busy", "yes") + summaryItem("Operation", data.operation || "-") + summaryItem("Retry", data.retry_after_seconds ?? "-") + '</div>' +
      '<div class="empty">' + esc(data.note || data.error || "Another mutation is already using this repository. Inspect or wait instead of bypassing the lock.") + '</div>' +
      '</div></article>';
  }

  function renderWorkerList(data) {
    const workers = Array.isArray(data.workers) ? data.workers : [];
    const rows = workers.slice(0, 12).map((worker) => {
      const left = worker.state || worker.workspace_mode || "worker";
      const right = (worker.name || worker.worker_id || "worker") + (worker.report ? " - " + worker.report : "");
      return fileRow(left, right);
    }).join("");
    return '<article class="card">' + header(data, "worker_list", pill((data.count ?? workers.length) + " workers", "info") + pill((data.active ?? 0) + " active")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Count", data.count ?? workers.length) + summaryItem("Active", data.active ?? 0) + summaryItem("Shared", "state") + '</div>' +
      (data.team_report ? '<div class="empty">' + esc(data.team_report) + '</div>' : "") +
      '<div class="file-list">' + (rows || '<div class="empty">No workers yet.</div>') + '</div>' +
      '</div></article>';
  }

  function renderWorker(data) {
    const changed = data.changed_files || data.files_changed || [];
    const pills = [
      statusPill(data),
      data.workspace_mode ? pill(data.workspace_mode, "info") : "",
      data.ownership_status ? pill(data.ownership_status, data.takeover_required ? "warn" : "info") : "",
      data.integration_state ? pill(data.integration_state, data.applied || data.can_apply ? "good" : "warn") : ""
    ].join("");
    const ownership = data.takeover_required || data.ownership_note
      ? '<div class="empty">' + esc(data.ownership_note || data.required_action || "Takeover required before mutating this worker.") + '</div>'
      : "";
    const lists = [
      fileRows(changed, "chg", 14),
      fileRows(data.base_changed_files, "base", 10),
      fileRows(data.blocked_files, "block", 10),
      fileRows(data.skipped_files, "skip", 10)
    ].filter(Boolean).join("");
    const preview = data.diff ? fold("Diff", countLines(data.diff) + " lines", codebox("diff", renderDiff(data.diff), ""), false) : "";
    const text = data.text ? fold(data.file_path || "Text", countLines(data.text) + " lines", codebox(data.file_path || "text", esc(previewLines(data.text, 40)), ""), false) : "";
    const raw = fold("Structured result", "", codebox("json", esc(truncate(JSON.stringify(data || {}, null, 2), 8000)), ""), false);
    return '<article class="card">' + header(data, "worker", pills) +
      '<div class="body">' +
      '<div class="summary">' +
      summaryItem("Files", data.change_count ?? changed.length ?? 0) +
      summaryItem("Can apply", boolText(data.can_apply)) +
      summaryItem("Applied", boolText(data.applied)) +
      '</div>' +
      ownership +
      (data.context_detail || data.context_sources ? '<div class="empty">' + esc(data.context_detail || compact(data.context_sources)) + '</div>' : "") +
      (lists || '<div class="empty">No file changes reported.</div>') +
      preview + text + raw +
      '</div></article>';
  }

  function renderArtifact(data) {
    const artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
    const files = data.top_level_entries || data.entries || data.files || [];
    const rows = artifacts.slice(0, 12).map((artifact) =>
      fileRow(artifact.kind || artifact.ownership_status || "art", (artifact.label || artifact.original_file_name || artifact.artifact_id || "artifact") + (artifact.file_count ? " - " + artifact.file_count + " files" : ""))
    ).join("");
    const fileList = fileRows(files, "file", 18);
    const text = data.text ? fold(data.file_path || "File preview", countLines(data.text) + " lines", codebox(data.file_path || "artifact", esc(previewLines(data.text, 48)), ""), true) : "";
    return '<article class="card">' + header(data, "artifact", [
      pill(data.kind || data.view || "inbox", "info"),
      data.count !== undefined ? pill(data.count + " artifacts") : "",
      data.ownership_status ? pill(data.ownership_status, data.takeover_required ? "warn" : "info") : ""
    ].join("")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Artifacts", data.count ?? artifacts.length ?? "-") + summaryItem("Files", data.file_count ?? files.length ?? "-") + summaryItem("Bytes", data.total_bytes ?? data.bytes ?? "-") + '</div>' +
      (data.note ? '<div class="empty">' + esc(data.note) + '</div>' : "") +
      '<div class="file-list">' + (rows || "") + '</div>' +
      (fileList || "") + text +
      fold("Structured result", "", codebox("json", esc(truncate(JSON.stringify(data || {}, null, 2), 8000)), ""), false) +
      '</div></article>';
  }

  function renderJob(data) {
    const ref = data.reference_id || data.job_id || data.session_ref || "-";
    const diff = data.delta_content || data.diff || "";
    const files = data.files_changed || data.changed_files || [];
    const bodyDiff = diff ? fold(data.record_path || "Diff", countLines(diff) + " lines", codebox(data.record_path || "diff", renderDiff(diff), ""), true) : "";
    return '<article class="card">' + header(data, "job", statusPill(data) + pill(data.mode, "info") + pill(ref)) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("State", data.state || data.status || "-") + summaryItem("Mode", data.mode || "-") + summaryItem("Files", files.length) + '</div>' +
      (data.message || data.error ? '<div class="empty">' + esc(data.message || data.error) + '</div>' : "") +
      (fileRows(files, "file", 14) || "") + bodyDiff +
      fold("Structured result", "", codebox("json", esc(truncate(JSON.stringify(data || {}, null, 2), 8000)), ""), false) +
      '</div></article>';
  }

  function renderCommand(data) {
    const stdoutLines = countLines(data.stdout);
    const stderrLines = countLines(data.stderr);
    const ok = Number(data.exit_code ?? data.exitCode ?? 0) === 0 && !data.timed_out;
    const pills = [
      pill(ok ? "passed" : "failed", ok ? "good" : "bad"),
      pill((stdoutLines + stderrLines) + " lines", "info"),
      data.bash_mode ? pill("bash " + data.bash_mode) : ""
    ].join("");
    const command = data.command ? codebox("command", '<span class="prompt">$</span> ' + esc(data.command), "terminal") : "";
    const out = data.stdout ? fold("stdout", stdoutLines + " lines", codebox("stdout", esc(previewLines(data.stdout, 40)), "terminal"), false) : "";
    const err = data.stderr ? fold("stderr", stderrLines + " lines", codebox("stderr", esc(previewLines(data.stderr, 40)), "terminal"), false) : "";
    return '<article class="card">' + header(data, "command", pills) +
      '<div class="body"><div class="summary">' + summaryItem("Exit", data.exit_code ?? data.exitCode ?? "-") + summaryItem("Stdout", stdoutLines) + summaryItem("Stderr", stderrLines) + '</div>' +
      command + (out || err ? out + err : '<div class="empty">Command produced no output.</div>') +
      '</div></article>';
  }

  function renderDiffCard(data) {
    const files = data.files_changed || data.changed_files || data.status_short || [];
    const diff = data.diff || data.delta_content || data.text || data.status || "";
    return '<article class="card">' + header(data, "diff", [
      data.changed === false ? pill("clean", "good") : pill(files.length ? files.length + " files" : "changes", "info"),
      data.additions !== undefined ? pill("+" + data.additions, "good") : "",
      data.deletions !== undefined ? pill("-" + data.deletions, "bad") : ""
    ].join("")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Files", files.length) + summaryItem("Added", data.additions ?? "-") + summaryItem("Deleted", data.deletions ?? "-") + '</div>' +
      (fileRows(files, "file", 14) || '<div class="empty">No changed files listed.</div>') +
      (diff ? fold("Preview", countLines(diff) + " lines", codebox("preview", data.diff || data.delta_content ? renderDiff(diff) : esc(previewLines(diff, 48)), ""), true) : "") +
      '</div></article>';
  }

  function renderWorkspace(data) {
    const skills = Array.isArray(data.skills) ? data.skills : Array.isArray(data.skill_inventory) ? data.skill_inventory : [];
    const tree = data.tree ? (typeof data.tree === "string" ? data.tree : JSON.stringify(data.tree, null, 2)) : "";
    const gitText = data.git_status || data.recent_commits || "";
    return '<article class="card">' + header(data, "workspace", pill(data.workspace_id || "workspace", "info") + (data.truncated ? pill("truncated", "warn") : "")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Skills", data.skill_counts?.total ?? skills.length ?? "-") + summaryItem("Mode", data.tool_mode || "-") + summaryItem("Power", data.power_tools ? "shown" : "-") + '</div>' +
      fileRows([data.root, data.path].filter(Boolean), "root", 2) +
      (skills.length ? fold("Skills", skills.length + " found", fileRows(skills, "skill", 18), false) : "") +
      (gitText ? fold("Git", countLines(gitText) + " lines", codebox("git", esc(previewLines(gitText, 36)), ""), false) : "") +
      (tree ? fold("Tree", countLines(tree) + " lines", codebox("tree", esc(previewLines(tree, 48)), ""), false) : "") +
      '</div></article>';
  }

  function renderSelfTest(data) {
    const checks = Array.isArray(data.checks) ? data.checks : [];
    const rows = checks.slice(0, 18).map((check) => fileRow(check?.ok === false || check?.status === "fail" ? "fail" : "ok", (check?.name || "check") + (check?.detail ? ": " + check.detail : ""))).join("");
    return '<article class="card">' + header(data, "self_test", [
      pill(data.ready === false || data.status === "fail" ? "not ready" : "ready", data.ready === false || data.status === "fail" ? "bad" : "good"),
      data.tool_count !== undefined ? pill(data.tool_count + " tools", "info") : "",
      data.public_tunnel_used ? pill("public tunnel", "warn") : ""
    ].join("")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Checks", checks.length) + summaryItem("Tools", data.tool_count ?? "-") + summaryItem("Tunnel", data.public_tunnel_used ? "yes" : "no") + '</div>' +
      '<div class="file-list">' + (rows || '<div class="empty">No checks returned.</div>') + '</div>' +
      (data.coordination ? fold("Shared server coordination", "", codebox("coordination", esc(truncate(JSON.stringify(data.coordination, null, 2), 4000)), ""), false) : "") +
      '</div></article>';
  }

  function renderWorkerOptions(data) {
    const models = Array.isArray(data.models) ? data.models : [];
    const efforts = Array.isArray(data.reasoning_efforts) ? data.reasoning_efforts : [];
    const rows = models.slice(0, 14).map((model) => {
      if (typeof model === "string") return fileRow("model", model);
      return fileRow(
        model?.recommended_for || model?.role || model?.tier || "model",
        model?.id || model?.model || model?.name || compact(model)
      );
    }).join("");
    const effortRows = efforts.slice(0, 8).map((effort) => {
      if (typeof effort === "string") return fileRow("effort", effort);
      return fileRow(effort?.value || effort?.name || "effort", effort?.description || compact(effort));
    }).join("");
    return '<article class="card">' + header(data, "worker_options", [
      pill(data.default_model || "model menu", "info"),
      data.model_count !== undefined ? pill(data.model_count + " models", "info") : "",
      data.models_truncated ? pill("truncated", "warn") : ""
    ].join("")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Models", data.model_count ?? models.length ?? "-") + summaryItem("Default", data.default_model || "-") + summaryItem("Reasoning", data.default_reasoning_effort || data.selected_reasoning_effort || "-") + '</div>' +
      (data.next_step || data.note ? '<div class="empty">' + esc(data.next_step || data.note) + '</div>' : "") +
      (rows ? fold("Models", models.length + " listed", '<div class="file-list">' + rows + '</div>', true) : "") +
      (effortRows ? fold("Reasoning efforts", efforts.length + " listed", '<div class="file-list">' + effortRows + '</div>', false) : "") +
      (data.model_selection_guidance ? fold("Model guidance", "", codebox("guidance", esc(truncate(JSON.stringify(data.model_selection_guidance, null, 2), 5000)), ""), false) : "") +
      '</div></article>';
  }

  function renderToolMode(data) {
    const modes = Array.isArray(data.modes) ? data.modes : [];
    const rows = modes.slice(0, 8).map((mode) =>
      fileRow(mode?.current ? "current" : "mode", (mode?.mode || "mode") + (mode?.tool_count !== undefined ? " - " + mode.tool_count + " tools" : "") + (mode?.purpose ? " - " + mode.purpose : ""))
    ).join("");
    return '<article class="card">' + header(data, "tool_mode", [
      pill(data.current_mode || data.default_mode || "mode", "info"),
      data.recommended_default ? pill("recommended " + data.recommended_default, "good") : "",
      data.persisted_to_config === false ? pill("session", "warn") : ""
    ].join("")) +
      '<div class="body">' +
      '<div class="summary">' + summaryItem("Current", data.current_mode || "-") + summaryItem("Default", data.default_mode || "-") + summaryItem("Modes", data.available_modes?.length ?? modes.length ?? "-") + '</div>' +
      (data.chatgpt_refresh_note ? '<div class="empty">' + esc(data.chatgpt_refresh_note) + '</div>' : "") +
      '<div class="file-list">' + (rows || '<div class="empty">No tool modes listed.</div>') + '</div>' +
      '</div></article>';
  }

  function renderSessions(data) {
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    const rows = sessions.slice(0, 16).map((session) =>
      fileRow(session.state || session.mode || "session", (session.summary || session.session_id || "session") + (session.files_changed ? " - " + session.files_changed.length + " files" : ""))
    ).join("");
    return '<article class="card">' + header(data, "sessions", pill((data.count ?? sessions.length) + " sessions", "info") + (data.transcripts_returned ? pill("transcripts", "warn") : "")) +
      '<div class="body"><div class="file-list">' + (rows || '<div class="empty">No sessions listed.</div>') + '</div>' +
      fold("Structured result", "", codebox("json", esc(truncate(JSON.stringify(data || {}, null, 2), 8000)), ""), false) +
      '</div></article>';
  }

  function renderText(data) {
    const text = data.text || data.content || "";
    return '<article class="card">' + header(data, "text", pill((data.bytes ?? countLines(text)) + (data.bytes !== undefined ? " bytes" : " lines"), "info") + (data.truncated ? pill("truncated", "warn") : "")) +
      '<div class="body">' + codebox(basename(data.path || data.file_path || "text"), esc(previewLines(text, 60)), "") +
      fold("Structured result", "", codebox("json", esc(truncate(JSON.stringify(data || {}, null, 2), 8000)), ""), false) +
      '</div></article>';
  }

  function renderGeneric(data) {
    const keys = Object.keys(data || {});
    const metrics = keys.slice(0, 6).map((key) => metric(key, data[key])).join("");
    return '<article class="card">' + header(data, "generic", statusPill(data)) +
      '<div class="body">' + (metrics ? '<div class="metrics">' + metrics + '</div>' : "") +
      codebox("structured output", esc(truncate(JSON.stringify(data || {}, null, 2))), "") +
      '</div></article>';
  }

  function isPlaceholderPayload(data) {
    if (!data || typeof data !== "object") return true;
    return Object.keys(data).length === 0;
  }

  function renderPending() {
    root.innerHTML = [
      '<article class="card pending">',
      '<div class="rail"></div>',
      '<header class="head">',
      '<span class="glyph">P</span>',
      '<div class="headline"><div class="title">PatchBay</div><div class="subtitle">Waiting for tool result...</div></div>',
      '<span class="pill info">waiting</span>',
      '</header>',
      '<div class="skeleton"><span></span><span></span><span></span></div>',
      '</article>'
    ].join("");
  }

  function renderWidgetError(error, data) {
    const message = error?.message || String(error || "Unknown widget error");
    root.innerHTML = '<article class="card">' + header({ error: message }, "generic", pill("widget error", "bad")) +
      '<div class="body">' +
      '<div class="empty">' + esc(message) + '</div>' +
      fold("Payload", "", codebox("json", esc(truncate(JSON.stringify(data || {}, null, 2), 4000)), ""), false) +
      '</div></article>';
  }

  function render(data) {
    try {
      if (isPlaceholderPayload(data)) {
        renderPending();
        return;
      }
      const kind = inferKind(data);
      if (kind === "repo_busy") root.innerHTML = renderRepoBusy(data);
      else if (kind === "worker_list") root.innerHTML = renderWorkerList(data);
      else if (kind === "worker") root.innerHTML = renderWorker(data);
      else if (kind === "artifact") root.innerHTML = renderArtifact(data);
      else if (kind === "job") root.innerHTML = renderJob(data);
      else if (kind === "command") root.innerHTML = renderCommand(data);
      else if (kind === "diff") root.innerHTML = renderDiffCard(data);
      else if (kind === "workspace") root.innerHTML = renderWorkspace(data);
      else if (kind === "self_test") root.innerHTML = renderSelfTest(data);
      else if (kind === "worker_options") root.innerHTML = renderWorkerOptions(data);
      else if (kind === "tool_mode") root.innerHTML = renderToolMode(data);
      else if (kind === "sessions") root.innerHTML = renderSessions(data);
      else if (kind === "text") root.innerHTML = renderText(data);
      else root.innerHTML = renderGeneric(data);
    } catch (error) {
      renderWidgetError(error, data);
    }
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
    // ChatGPT's Apps SDK compatibility layer exposes window.openai.toolOutput
    // as the structuredContent object itself. Treat any non-empty plain object
    // as renderable structured data after checking known envelope shapes.
    if (Object.keys(value).length > 0) return value;
    return {};
  }

  function firstStructuredContent(...values) {
    for (const value of values) {
      const structured = extractStructuredContent(value);
      if (!isPlaceholderPayload(structured)) return structured;
    }
    return {};
  }

  render(firstStructuredContent(window.openai?.toolOutput, window.openai?.toolResponseMetadata));

  window.addEventListener("openai:set_globals", (event) => {
    render(firstStructuredContent(
      event.detail?.globals?.toolOutput ||
      event.detail?.globals?.structuredContent,
      event.detail?.globals?.toolResponseMetadata,
      event.detail,
      window.openai?.toolOutput,
      window.openai?.toolResponseMetadata
    ));
  }, { passive: true });

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.method === "ui/notifications/tool-result") {
      render(extractStructuredContent(message.params || {}));
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
            "Renders named Codex worker reports, workspace orientation, low-level job status, diffs, "
            "handoffs, power-tool results, and connector diagnostics as compact developer cards."
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
        "description": "Rich ChatGPT Apps card for PatchBay worker, artifact, job, diff, and power-tool results.",
        "mimeType": TOOL_CARD_MIME_TYPE,
    }


def list_resource_templates() -> List[Dict[str, Any]]:
    return [tool_card_descriptor()]


def read_resource(uri: str, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
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
