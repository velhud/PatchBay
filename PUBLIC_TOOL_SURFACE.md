# Public Tool Surface

## Design Principle

The wrapper should expose tools as product capabilities, not implementation conveniences. ChatGPT should see narrow, intentional tools that explain when to use them and what control boundary they cross.

CodexPro's generic `read`, `write`, `edit`, and `bash` tools are powerful. The wrapper keeps canonical `codex_*` names as the durable API, while `app.tool_mode` can advertise CodexPro-compatible aliases for ChatGPT live use. Aliases resolve to canonical handlers and do not create separate execution paths.

## Current Stable Tools

| Tool | Current role | Target status | Notes |
| --- | --- | --- | --- |
| `codex_plan_job` | Start read-only Codex analysis | keep | Must enforce read-only sandbox and command builder policy. |
| `codex_apply_job` | Start isolated worktree apply job | keep | Mutating. Should return worktree, branch, and review artifacts. |
| `codex_get_status` | Poll job state | keep | Read-only. Should work for durable jobs after restart. |
| `codex_get_result` | Fetch completed output | keep | Return summary by default, raw logs only opt-in. |
| `codex_get_diff` | Inspect file diff | keep | Requires completed apply job and changed file membership. |
| `codex_review` | Run Codex review | keep | Clarify whether it is read-only or can trigger writes through options. |
| `codex_list_sessions` | List metadata-only session ids | keep | Read-only, no transcript bodies, no repo paths by default. |
| `codex_resume` | Start async Codex resume job | keep, strengthen | Marked mutating/open-world because resumed sessions may write locally; returns a durable `job_id`. |
| `codex_interactive` | Start async interactive Codex exec job | keep, strengthen | Marked mutating/open-world; completed result includes `session_ref` when Codex reports one. |
| `codex_interactive_reply` | Start async Codex continuation job | keep, strengthen | Marked mutating/open-world; uses session repo metadata when available. |
| `codex_get_config` | Return redacted config/capabilities | keep | Does not expose raw local config, private paths, or hidden feature details. |

## New Context Tools

| Tool | Mutability | Purpose |
| --- | --- | --- |
| `codex_open_workspace` | read-only | Open the active workspace and return bounded orientation: repo name, branch, git status summary, AGENTS summary, available skills, and next suggested tools. |
| `codex_repo_tree` | read-only | Return bounded tree for the active workspace or a subpath. |
| `codex_search_repo` | read-only | Search allowed source files with ripgrep-first behavior and redacted snippets. |
| `codex_read_file` | read-only | Read a bounded file slice inside the workspace. |
| `codex_load_context` | read-only | Return AGENTS, selected files, git status, and `.ai-bridge` context for a task. |
| `codex_export_context` | mutating, scoped | Write a selected context pack under `.ai-bridge`, never arbitrary source files. |
| `codex_list_workspaces` | read-only | List configured workspaces known to the connector. |
| `codex_workspace_snapshot` | read-only | Return git status, recent commits, `.ai-bridge`, and a compact tree. |
| `codex_inventory` | read-only | Return tool modes, skill inventory, git state, and power-mode settings. |
| `codex_git_status` | read-only | Show branch and changed files without bash. |
| `codex_git_diff` | read-only | Show bounded unstaged or staged git diff without bash. |
| `codex_show_changes` | read-only | Return review-oriented status, diff stats, and optional diff. |
| `codex_list_skills` | read-only | List skill names/descriptions without exposing local install paths. |
| `codex_load_skill` | read-only | Load a bounded `SKILL.md` by known skill name. |

## Handoff Tools

| Tool | Mutability | Purpose |
| --- | --- | --- |
| `codex_write_handoff` | mutating, scoped | Write a plan into `.ai-bridge/current-plan.md` for explicit local execution. |
| `codex_get_handoff_status` | read-only | Read `.ai-bridge/agent-status.md`, execution summary, and current handoff state. |
| `codex_get_handoff_diff` | read-only | Return bounded diff artifacts written by local handoff execution. |

Handoff tools are not a replacement for Codex jobs. They are useful when the user wants ChatGPT to prepare work and then explicitly run a local agent from the terminal.

Local terminal commands provide the CodexPro-style non-MCP side of the flow:

- `python scripts/handoff.py execute ...`
- `python scripts/handoff.py watch ...`
- `python scripts/pro_context.py bundle ...`
- `python scripts/pro_context.py apply ...`

## Optional Power Tools

These tools are part of the public surface in full tool mode, but remain denied by default unless the user explicitly enables the matching power mode in config. They must stay clearly marked in descriptors.

| Tool | Mutability | Required control |
| --- | --- | --- |
| `codex_edit_file` | mutating | Explicit power mode, path guard, secret write block, diff return. |
| `codex_write_file` | mutating | Same as edit, preferably restricted to selected paths. |
| `codex_run_command` | open-world/mutating risk | Off by default; safe/full command mode, timeout, session gate, and output caps. |
| `codex_read_session` | read-only but highly sensitive | Implemented, off by default, bounded transcript, explicit config. |

## Tool Descriptor Requirements

Every public descriptor must include:

- `name`;
- `title` where supported;
- description with direct usage guidance;
- JSON input schema;
- output schema when returning structured content;
- `annotations.readOnlyHint`;
- `annotations.destructiveHint`;
- `annotations.openWorldHint`;
- `securitySchemes`;
- `_meta.securitySchemes`;
- invocation status labels;
- `_meta.ui.resourceUri` and `openai/outputTemplate` pointing to the shared ChatGPT card resource.

The canonical names remain `codex_*`. CodexPro-compatible aliases such as `read`, `write`, `edit`, `bash`, `show_changes`, `git_status`, `git_diff`, `workspace_snapshot`, `export_pro_context`, and `handoff_to_agent` may be advertised depending on `app.tool_mode`, but they must resolve to canonical handlers rather than duplicate execution paths.

Current implementation returns these descriptor fields from `tools/list`, including conservative object output schemas for structured results. It also exposes `ui://widget/codex-mcp-wrapper-tool-card-v1.html` through `resources/list` and `resources/read` as a `text/html;profile=mcp-app` resource. The first card is intentionally passive: it renders tool results and does not initiate tool calls. The test suite should snapshot public descriptors and fail if:

- a mutating tool is marked read-only;
- a read-only tool lacks `readOnlyHint`;
- an internal tool appears in `tools/list`;
- a schema advertises fields that handlers do not accept;
- aliases are advertised in the wrong tool mode or point to duplicate execution paths instead of canonical handlers.
- descriptor resource URIs drift from the registered resource.

## Schema Compatibility

Current wrapper schemas advertise `spec` and `repo_path`, while internal handlers consume `prompt` and `repo` through translation. That bridge should be made explicit:

- public schemas keep stable names for existing users;
- handlers receive one normalized internal request object;
- translation is tested for every public tool;
- new tools should avoid public/internal name drift.

## Compatibility Aliases

Tool modes:

- `minimal`: connector and workspace essentials.
- `standard`: core workspace, handoff, and Codex job tools.
- `full`: standard tools plus optional power tools and CodexPro-compatible aliases.

Alias policy:

- aliases are a ChatGPT selection aid, not the stable API;
- durable docs and client integrations should prefer canonical `codex_*` names;
- aliases must share the same schemas, validation, mutability, and power controls as the canonical tools they resolve to;
- disabling a canonical power tool also disables its alias behavior at execution time.

## Hidden And Deprecated Tools

The default internal dispatch table exposes only public tools. Legacy experimental cloud/apply-diff/string/sandbox method implementations have been deleted rather than hidden behind a flag. The supported power replacements are:

- `codex_apply_job` for isolated implementation work;
- `codex_get_diff` for proven apply-job diffs;
- `codex_write_file` and `codex_edit_file` for explicit direct workspace writes;
- `codex_run_command` for configured safe/full command execution.

Before importing more CodexPro features:

- keep the public registry separate from internal experiments;
- avoid adding hidden callable methods without public contract tests;
- keep aliases controlled by `app.tool_mode`;
- document deprecation timing for neutral aliases.

## ChatGPT Product Metadata

OpenAI Developer Mode and Apps-compatible clients use metadata to decide how tools are presented and confirmed. The wrapper should therefore treat descriptor metadata as product behavior, not decoration.

Required product behavior:

- write tools prompt for confirmation;
- read-only context tools should not require confirmation;
- destructive or open-world actions must be labeled;
- tool cards should show concise job/workspace/diff state;
- JSON payloads should remain understandable when a user expands them in ChatGPT.

The current resource card is a CodexPro-style transplant of the product contract rather than a wholesale HTML copy. CodexPro's large widget renderer is still useful source material for a richer future card, but the wrapper now owns a smaller Python-served resource that matches the wrapper's `codex_*` structured outputs.
