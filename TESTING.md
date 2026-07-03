# Testing

The test strategy separates four things:

- static/unit checks that do not require Codex login;
- live local MCP probing without ChatGPT or a public tunnel;
- real Codex CLI execution through PatchBay;
- release evals that still need real ChatGPT Developer Mode coverage. Direct tokenized public-tunnel MCP simulation is tracked separately from ChatGPT UI/tool-selection proof.

For the detailed release matrix, see [docs/testing/evals.md](docs/testing/evals.md).

## Baseline

Install runtime and test dependencies before running the suite:

```bash
pip install -r requirements.txt -e ".[test]"
```

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase1_eval.py --timeout 600
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase2_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase3_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --include-safety-cases
PYTHONDONTWRITEBYTECODE=1 python scripts/external_chatgpt_style_validation.py --json
```

Current verified Codex CLI baseline:

```text
codex-cli 0.142.2
```

The unit suite verifies:

- advertised public tool names and compatibility aliases;
- rejection of hidden/internal tools;
- read/write/open-world metadata;
- public schema validation and argument translation;
- connector doctor and auth policy behavior;
- MCP request body size limits;
- durable redacted job metadata persistence;
- current Codex JSONL `agent_message` result parsing;
- redacted/capped job stdout/stderr artifacts;
- strict completed-apply-job diff retrieval;
- `codex review` prompt stdin transport and config override allowlisting;
- metadata-only session listing;
- metadata-only Codex session discovery, PatchBay/Codex-home session dedupe, and gated bounded redacted transcript reads;
- process cancellation for running jobs;
- durable named worker start/message/list/inspect/stop behavior;
- worker artifact inbox import/list/inspect, repeated imports, structural archive rejection, sensitive-looking artifact filenames, and worker attachment;
- worker model/reasoning option discovery, sanitized model catalog output, and inherited worker execution settings;
- isolated worker worktree creation, same-worktree resume, change/file/diff views, workspace-scoped worker names, and explicit cleanup;
- multi-worker context relay through `context_from_workers` and `context_detail`;
- worker integration preview, dirty-base refusal, blocked-path refusal, artifact-context exclusion, conflict reporting, and explicit accepted-result application;
- worker tool descriptors, worker-only mode, initialize instructions, ChatGPT-facing manager-loop guidance, direct-tool exception wording, multi-worker encouragement, worker status/list filtering, compact team status, activity deltas since last status check, paged base and worker file inspection, durable evidence location labels, ownership-scope wording, live Codex session/heartbeat diagnostics, liveness/checkpoint views, configurable liveness display thresholds, broader Codex agent-message event parsing, and partial report preservation after cancellation;
- Pro Request store behavior, sanitized mirrors, ownership/takeover, CLI create/list/show/response/dispatch/close, MCP list/read/claim/respond/dispatch/close descriptors, and blocked/busy/new-worker dispatch behavior;
- durable real MCP worker trial evidence writer, sanitizer, and negative cases;
- optional direct workspace write/edit and command power tools;
- runtime descriptor truthfulness for disabled direct write, bash, and transcript-read profiles;
- launcher profile storage and runtime config generation;
- installable CLI dispatch, noninteractive setup behavior, settings profile management, stdio transport, and tunnel binary resolution;
- fake public tunnel process supervision;
- ChatGPT Apps tool-card resource discovery and widget hydration from both direct `window.openai.toolOutput` and standard `ui/notifications/tool-result` payloads;
- operator boundary and runtime-profile checks;
- path validation and symlink escape rejection;
- redaction helpers;
- MCP initialize instructions, including manager-first worker delegation guidance, direct read/search exception rules, and multi-worker team guidance.

## Connector Doctor

```bash
patchbay doctor
patchbay doctor --json
patchbay start --root /absolute/path/to/allowed/repo --tool-mode worker --print-only
patchbay start --root /absolute/path/to/allowed/repo --tool-mode worker --print-only --json
patchbay stdio --config config.yaml
```

Expected output includes readiness checks, the local MCP URL, a redacted ChatGPT Server URL preview when token auth is enabled, a ChatGPT setup guide, and no raw token value. JSON output should include `setup_guide` with `chatgpt_steps`, `operator_commands`, `controls`, `warnings`, and profile metadata.

For public ChatGPT tunnel previews, set `PATCHBAY_HTTP_TOKEN` before using `--public-base-url`; the launcher should fail closed without that token.

## Live Local MCP Eval

Run a real launcher/server/probe cycle without ChatGPT and without a public tunnel:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

The eval creates a temporary git repo with `AGENTS.md`, source files, `.env`, a symlink escape, and a repo-local `SKILL.md`; starts the compatibility launcher path `scripts/start.py`; then probes:

- MCP health and initialize;
- `tools/list`;
- precise compatibility alias descriptors;
- Apps resources;
- workspace open;
- skill list/load;
- file read and alias read;
- git status;
- workspace snapshot;
- show changes alias, including a path-scoped tracked-file diff;
- blocked `.env` read;
- blocked symlink read;
- enabled direct write and command execution in the full-power profile;
- `codex_self_test`.
- Pro Request CLI create plus MCP list/read/claim/respond and blocked dispatch when no origin worker exists.

This test proves the local MCP surface behaves like a compact ChatGPT-style client, but it does not prove ChatGPT Developer Mode itself.

## Direct Tokenized Public Tunnel Probe

For connector and tunnel changes, run a disposable public-tunnel probe before attempting real ChatGPT UI validation. Earlier validation has verified this through ngrok with a generated disposable token: missing token startup failed closed, Bearer-auth health passed, query-token MCP `initialize` passed, worker-mode `tools/list` exposed worker tools while hiding low-level job status tools, and an Apps-style file parameter drove `codex_worker_inbox` import/list/inspect, repeated import, `file://` rejection, isolated worker artifact attachment/read, artifact-context exclusion from integration, clean base checkout preservation, and worker cleanup. Re-run the tunnel probe with a configured hostname before treating it as current release evidence.

This proves public network reachability and token enforcement at the MCP level. It does not prove ChatGPT Developer Mode setup, tool selection, or ChatGPT-originated worker flows.

The consolidated external validation harness also covers this gate:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/external_chatgpt_style_validation.py --skip-heavy-codex --json
```

If `ngrok config check` passes but no `PATCHBAY_VALIDATION_NGROK_HOSTNAME` or `--ngrok-hostname` is provided, the public tunnel scenario is recorded as an external setup blocker rather than a PatchBay failure.

## External ChatGPT-Style Validation

Run the consolidated direct-MCP simulation when changing ChatGPT-facing connector behavior, worker lifecycle behavior, artifact import, session discovery, public schemas, or low-level Codex job/resume flows:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/external_chatgpt_style_validation.py --json
```

For a faster local surface pass without real Codex workers:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/external_chatgpt_style_validation.py --skip-heavy-codex --json
```

The harness writes `calls.jsonl`, `results.json`, and `summary.md` under `.local/validation/external_chatgpt_style/<timestamp>/`. It starts PatchBay through the compatibility launcher path `scripts/start.py`, uses disposable repositories, creates separate MCP clients to simulate separate ChatGPT conversations, records `codex --version`, and redacts temporary paths and token-like values in evidence. Real ChatGPT Developer Mode UI validation remains a separate manual gate.

For worker lifecycle regressions, add focused tests proving a durable `running` job is not marked stale while an executor-owned asyncio task is still alive, proving worker start/message code schedules jobs through `JobExecutor.schedule_job` instead of orphaned background tasks, proving running workers expose compact `codex_worker_status` lines, activity deltas, liveness/checkpoints, and latest partial notes to ChatGPT, proving liveness thresholds are configurable display policy rather than task limits, proving Codex `agent_message` content variants parse into reports/checkpoints, and proving cancellation preserves captured partial reports/checkpoints.

## Real Codex CLI Through MCP

For execution changes, run a disposable real-Codex plan job through MCP. The expected path is:

1. start `patchbay start` or `scripts/start.py` against a disposable git repo;
2. initialize MCP;
3. call `codex_plan_job`;
4. poll `codex_get_status`;
5. call `codex_get_result`;
6. confirm a clean structured summary and `session_ref` when Codex returns one.

Current final validation recorded Codex CLI `0.142.2` and confirmed PatchBay parses the current JSONL `item.completed` / `agent_message` result shape. Worker verification should always record the current local `codex --version`.

## Real Codex Worker Continuity

For read-only worker continuity, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase1_eval.py --timeout 600
```

Expected result:

1. start one named read-only worker;
2. complete its first Codex turn;
3. capture a Codex session internally;
4. reconstruct runtime objects to simulate PatchBay restart;
5. list the worker by name;
6. continue the same Codex session by worker name;
7. avoid exposing backend job/session ids or private paths in public worker output.

## Real Codex Isolated Writing Worker

For Phase 2 worker changes, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase2_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase3_eval.py --timeout 900
```

Expected result:

1. start one named worker in default `isolated_write` mode;
2. create one external worker worktree;
3. write only inside that worktree;
4. keep the base checkout clean;
5. reconstruct runtime objects to simulate PatchBay restart;
6. continue the same Codex session by worker name;
7. reuse the same worker worktree;
8. expose changed files and one-file diff only when requested;
9. explicitly discard the worker workspace on cleanup.



For Phase 3 worker coordination, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase3_eval.py --timeout 900
```

The Phase 3 eval should:

1. start one isolated writing implementer;
2. inspect its changed files;
3. start one read-only reviewer with `context_from_workers` and `context_detail="diff"`;
4. verify the reviewer receives bounded diff context without private paths;
5. send the reviewer report back to the implementer with `context_detail="report"`;
6. verify the implementer keeps the same session and worktree;
7. verify `codex_worker_status` and `codex_worker_list` return useful compact team status / `team_report`;
8. keep the base checkout clean.

## Manual Curl Smoke

## Direct MCP Worker Trial

After applying worker integration changes, run a direct MCP worker trial before claiming the worker bridge is ready for normal use.

Run the durable direct-MCP worker trial when validating real MCP worker lifecycle evidence:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py
```

This writes progressive `calls.jsonl`, `results.json`, and `summary.md` artifacts under `.local/validation/real_mcp_trial/<timestamp>/`. It uses a disposable repo, a trial-specific runtime config, `worker` tool mode by default, and proves worker integration does not create a commit by comparing commit counts before and after `codex_worker_integrate`. The trial config runs worker Codex subprocesses with `--ignore-user-config`; Codex authentication still uses `CODEX_HOME`, but unrelated user-level MCP connector config is not loaded into validation workers.

Run the negative-case variant to cover refusal and leak checks over the same real MCP path:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --include-safety-cases
```

The `--include-safety-cases` flag name is historical. It adds active-worker integration refusal, read-only worker integration refusal, dirty-base refusal, blocked `.env` refusal, untracked binary refusal, conflict preview refusal, cleanup isolation, connector/OAuth stderr noise scanning, and artifact leak scanning.

Run the multi-client variant to cover one shared MCP server URL with two logical MCP sessions:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json
```

The multi-client variant verifies session-local tool modes, safe shared inspection, cross-owner mutation refusal, explicit takeover, ownership transfer, preview-before-integrate, no automatic commit, connector/OAuth stderr noise scanning, and sanitized private evidence under `.local/validation/real_mcp_trial/<timestamp>/`. Unit coverage also verifies that new worker/job/artifact owner metadata stores owner scope and schema, and that old unscoped records report `legacy_connection` until explicit takeover migrates them to the current scoped owner model.

Start the server:

```bash
patchbay start --root /absolute/path/to/allowed/repo
```

Health:

```bash
curl http://127.0.0.1:8000/
```

Initialize:

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

Save the returned `Mcp-Session-Id` header and use it for `tools/list`, `resources/list`, and `tools/call`.

## Release Evals Still Required

Before public release, run all of these against disposable repos:

- real ChatGPT Developer Mode connection;
- ChatGPT-originated worker flow through a token-gated public tunnel if tunnel use is advertised in the real ChatGPT UI;
- direct workspace orientation from ChatGPT;
- real `codex_plan_job` from ChatGPT;
- real `codex_apply_job` from ChatGPT with diff inspection;
- real resume or interactive continuation from ChatGPT using `session_ref`;
- real named worker start/list/inspect/restart/message flow from ChatGPT;
- `.ai-bridge` handoff write, local dry-run, local execute, and status/diff readback;
- blocked path, blocked symlink, disabled power-tool, unsafe bash, and missing-token failures.

## Checklist

- `codex --version` is recorded.
- Compile and pytest pass.
- `scripts/live_mcp_eval.py --json` passes.
- `scripts/worker_phase1_eval.py --timeout 600` passes for read-only worker continuity, or the Codex-auth/environment blocker is reported.
- `scripts/worker_phase2_eval.py --timeout 900` passes for isolated writing worker continuity, or the Codex-auth/environment blocker is reported.
- `scripts/worker_phase3_eval.py --timeout 900` passes for multi-worker peer context relay, or the Codex-auth/environment blocker is reported.
- `scripts/worker_phase4_eval.py --timeout 900` passes for worker integration preview and accepted-result application, or the Codex-auth/environment blocker is reported.
- `scripts/real_mcp_worker_trial.py` passes for direct MCP worker lifecycle evidence, or the blocker is reported with partial artifacts.
- `scripts/real_mcp_worker_trial.py --include-safety-cases` passes for direct MCP worker negative cases, or the blocker is reported with partial artifacts.
- `scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json` passes for shared-server multi-client coordination, or the blocker is reported with partial artifacts.
- `tools/list` returns the expected public catalog and metadata.
- `resources/list` and `resources/read` return `ui://widget/patchbay-tool-card-v2.html`; the legacy v1 URI remains readable for compatibility.
- Async starter tools return `job_id`.
- Real Codex plan jobs complete through MCP.
- Structured Codex result parsing is clean.
- The checked-in full-power profile exposes direct write, bash, and transcript reads; disabled-profile tests prove those tools and aliases disappear from `tools/list` and calls are rejected.
- Token-gated tunnel startup fails closed without `PATCHBAY_HTTP_TOKEN`.
- Direct tokenized public-tunnel MCP probes pass before treating real ChatGPT UI failures as tool-selection or descriptor failures.
- Logs and runtime files do not contain real tokens, prompt bodies, or private paths in committed docs.


## Worker Integration Eval

Run after worker integration changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
```

This proves that a real isolated writing worker result can be previewed, explicitly applied to the base checkout, and preserved in the worker worktree without exposing private paths.
