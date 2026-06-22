# Context And Handoff Specification

## Goal

ChatGPT should be able to understand a local repository and move work into local Codex without manual export/import. The current context layer provides bounded workspace orientation, file/search/git context, AGENTS and skill loading, `.ai-bridge` artifacts, and Pro-style context packs while preserving scope, redaction, and user control.

## Active Workspace

An active workspace is an allowed git repository or subdirectory selected through config/profile/startup options. All context tools operate inside this workspace.

The workspace object should include:

- display name;
- active branch or detached state;
- repo root identifier that does not reveal private machine paths in public docs/logs;
- configured mode: core, context, handoff, or power;
- git status summary;
- AGENTS files found;
- skill inventory summary;
- available tool tiers;
- warnings about disabled bash, disabled write, missing Codex CLI, or untrusted repo state.

## AGENTS Loading

The wrapper should follow the same intent as Codex and CodexPro:

- load root-level `AGENTS.md` when present;
- load nested `AGENTS.md` files relevant to selected paths;
- preserve order from broadest to most specific;
- cap bytes per file and total bytes;
- report omitted files because of caps;
- never load outside the workspace.

AGENTS contents may be included in:

- `codex_open_workspace` summary;
- `codex_load_context`;
- Codex job prompt assembly;
- `.ai-bridge/pro-context.md`.

## Skill Discovery And Loading

Skill support should be progressive:

1. List skill names, descriptions, and source category.
2. Load a bounded `SKILL.md` by known skill name.
3. Avoid exposing local absolute paths by default.
4. Never load arbitrary file paths supplied by the model.

Skill categories:

- workspace skills;
- user Codex skills;
- plugin/cache skills;
- system-provided skills, if discoverable.

The first release may expose only names and descriptions. Full loading can be opt-in if path disclosure or private notes become a concern.

## Repository Snapshot

`codex_open_workspace` and `codex_load_context` should build a compact snapshot:

- branch and short commit;
- dirty/clean state;
- staged/unstaged/untracked counts;
- recent commits;
- bounded tree;
- key root files such as README, package manifests, pyproject, Cargo, AGENTS, config files;
- selected files requested by the user;
- current `.ai-bridge` status if present.

Snapshots should not include:

- `.env` files;
- private keys;
- `.git` internals;
- dependency folders;
- build outputs;
- local databases;
- raw Codex auth/session files;
- arbitrary home-directory content.

## Selected-File Context Packs

`codex_export_context` writes context packs under `.ai-bridge` for use by ChatGPT, Codex, or another local agent.

Default output:

- `.ai-bridge/pro-context.md`
- `.ai-bridge/current-plan.md` when a plan is supplied
- `.ai-bridge/agent-status.md`
- `.ai-bridge/open-questions.md`
- `.ai-bridge/decisions.md`
- `.ai-bridge/implementation-diff.patch`
- `.ai-bridge/execution-log.md`
- `.ai-bridge/session-log.md`

Context pack sections:

- task summary;
- workspace summary;
- relevant instructions;
- selected files;
- git status and diff summary;
- open questions;
- next-step instructions for local Codex;
- redaction/omission notes.

Context pack generation should prefer selected paths. Whole-repo context should require explicit request and stay bounded.

## Handoff Lifecycle

The handoff model is:

1. ChatGPT writes or updates `.ai-bridge/current-plan.md`.
2. A local CLI command previews or executes the handoff.
3. The local process invokes Codex or another configured agent.
4. Execution writes status, logs, and diff artifacts into `.ai-bridge`.
5. ChatGPT reads status and diffs through MCP.

The MCP server should not silently execute a handoff just because a plan file changed. Execution is a local user action or an explicitly enabled job tool.

## Handoff CLI Requirements

The local CLI should support:

- `doctor`: verify Codex CLI, git, config, auth mode, and tunnel risk.
- `handoff dry-run`: show command that would run.
- `handoff execute`: run one handoff once.
- `handoff watch`: poll for plan changes and execute after explicit opt-in.

Execution output must be:

- capped;
- redacted;
- split into summary and raw artifacts;
- written under `.ai-bridge`;
- linked to a correlation/job id.

## Relationship To Codex Jobs

Codex jobs remain the primary high-power workflow:

- `codex_plan_job`: ask local Codex to analyze without writes.
- `codex_apply_job`: ask local Codex to change an isolated worktree.
- `codex_get_diff`: inspect the result.

Handoff is an additional path for cases where the user wants a local terminal workflow or wants ChatGPT to prepare a plan without starting a job directly.

## Current Verification Status

Verified:

- `.ai-bridge` context and handoff file behavior in unit tests;
- local `scripts/handoff.py` dry-run behavior;
- `scripts/pro_context.py bundle` and `apply` behavior;
- MCP readback of handoff status/diff artifacts.

Pending before release:

- real ChatGPT-originated handoff write and local execute/watch cycle on a disposable repo.

## Failure Modes To Document And Test

- no active workspace;
- invalid workspace root;
- blocked path requested;
- symlink escape;
- binary or too-large file;
- missing AGENTS file;
- skill name not found;
- context pack would exceed max bytes;
- `.ai-bridge` missing or malformed;
- handoff plan exists but watcher is not running;
- handoff command exits non-zero;
- git diff unavailable;
- local agent output includes secret-like content.
