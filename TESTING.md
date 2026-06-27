# Testing

The test strategy separates four things:

- static/unit checks that do not require Codex login;
- live local MCP probing without ChatGPT or a public tunnel;
- real Codex CLI execution through the wrapper;
- release evals that still need real ChatGPT Developer Mode coverage. Direct tokenized public-tunnel MCP probing is tracked separately from ChatGPT UI/tool-selection proof.

## Baseline

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase1_eval.py --timeout 600
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase2_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase3_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --output-dir validation-reports/real_mcp_trial
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --include-safety-cases --output-dir validation-reports/real_mcp_trial
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
- gated, bounded, redacted Codex session transcript reads;
- process cancellation for running jobs;
- durable named worker start/message/list/inspect/stop behavior;
- worker model/reasoning option discovery, sanitized model catalog output, and inherited worker execution settings;
- isolated worker worktree creation, same-worktree resume, change/file/diff views, workspace-scoped worker names, and explicit cleanup;
- multi-worker context relay through `context_from_workers` and `context_detail`;
- worker integration preview, dirty-base refusal, blocked-path refusal, conflict reporting, and explicit accepted-result application;
- worker tool descriptors and worker-only mode;
- durable real MCP worker trial evidence writer, sanitizer, and safety negative cases;
- optional direct workspace write/edit and command power tools;
- launcher profile storage and runtime config generation;
- fake public tunnel process supervision;
- ChatGPT Apps tool-card resource discovery;
- conservative security defaults;
- path validation and symlink escape rejection;
- redaction helpers;
- MCP initialize instructions.

## Connector Doctor

```bash
python scripts/doctor.py
python scripts/doctor.py --json
python scripts/start.py --root /absolute/path/to/allowed/repo --print-only
python scripts/start.py --root /absolute/path/to/allowed/repo --print-only --json
```

Expected output includes readiness checks, the local MCP URL, a redacted ChatGPT Server URL preview when token auth is enabled, and no raw token value.

For public ChatGPT tunnel previews, set `CODEX_MCP_HTTP_TOKEN` before using `--public-base-url`; the launcher should fail closed without that token.

## Live Local MCP Eval

Run a real launcher/server/probe cycle without ChatGPT and without a public tunnel:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

The eval creates a temporary git repo with `AGENTS.md`, source files, `.env`, a symlink escape, and a repo-local `SKILL.md`; starts `scripts/start.py`; then probes:

- MCP health and initialize;
- `tools/list`;
- Apps resources;
- workspace open;
- skill list/load;
- file read and alias read;
- git status;
- workspace snapshot;
- show changes alias;
- blocked `.env` read;
- blocked symlink read;
- disabled direct write;
- `codex_self_test`.

This test proves the local MCP surface behaves like a compact ChatGPT-style client, but it does not prove ChatGPT Developer Mode itself.

## Direct Tokenized Public Tunnel Probe

For connector and tunnel changes, run a disposable public-tunnel probe before attempting real ChatGPT UI validation. Current local validation has verified this through ngrok with a generated disposable token: missing token startup failed closed, Bearer-auth health passed, query-token MCP `initialize` passed, and worker-mode `tools/list` exposed worker tools while hiding low-level job status tools.

This proves public network reachability and token enforcement at the MCP level. It does not prove ChatGPT Developer Mode setup, tool selection, or ChatGPT-originated worker flows.

## Real Codex CLI Through MCP

For execution changes, run a disposable real-Codex plan job through MCP. The expected path is:

1. start `scripts/start.py` against a disposable git repo;
2. initialize MCP;
3. call `codex_plan_job`;
4. poll `codex_get_status`;
5. call `codex_get_result`;
6. confirm a clean structured summary and `session_ref` when Codex returns one.

Current manual verification used Codex CLI `0.142.2` and confirmed the wrapper parses the current JSONL `item.completed` / `agent_message` result shape. Worker verification should record the current local `codex --version`.

## Real Codex Worker Continuity

For read-only worker continuity, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase1_eval.py --timeout 600
```

Expected result:

1. start one named read-only worker;
2. complete its first Codex turn;
3. capture a Codex session internally;
4. reconstruct runtime objects to simulate wrapper restart;
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
5. reconstruct runtime objects to simulate wrapper restart;
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
7. verify `codex_worker_list` returns a useful `team_report`;
8. keep the base checkout clean.

## Manual Curl Smoke

## Direct MCP Worker Trial

After applying worker integration changes, run a direct MCP worker trial before claiming the worker bridge is ready for normal use.

Run the durable direct-MCP worker trial when validating real MCP worker lifecycle evidence:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --output-dir validation-reports/real_mcp_trial
```

This writes progressive `calls.jsonl`, `results.json`, and `summary.md` artifacts under `validation-reports/real_mcp_trial/<timestamp>/`. It uses a disposable repo, a trial-specific runtime config, `worker` tool mode by default, and proves worker integration does not create a commit by comparing commit counts before and after `codex_worker_integrate`. The trial config runs worker Codex subprocesses with `--ignore-user-config`; Codex authentication still uses `CODEX_HOME`, but unrelated user-level MCP connector config is not loaded into validation workers.

Run the safety variant to cover negative cases over the same real MCP path:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --include-safety-cases --output-dir validation-reports/real_mcp_trial
```

The safety variant adds active-worker integration refusal, read-only worker integration refusal, dirty-base refusal, blocked `.env` refusal, untracked binary refusal, conflict preview refusal, cleanup isolation, connector/OAuth stderr noise scanning, and artifact leak scanning.

Start the server:

```bash
python scripts/start.py --root /absolute/path/to/allowed/repo
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
- ChatGPT-originated worker flow through a token-gated public tunnel if tunnel use is advertised;
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
- `scripts/real_mcp_worker_trial.py --output-dir validation-reports/real_mcp_trial` passes for direct MCP worker lifecycle evidence, or the blocker is reported with partial artifacts.
- `scripts/real_mcp_worker_trial.py --include-safety-cases --output-dir validation-reports/real_mcp_trial` passes for direct MCP worker safety negative cases, or the blocker is reported with partial artifacts.
- `tools/list` returns the expected public catalog and metadata.
- `resources/list` and `resources/read` return `ui://widget/codex-mcp-wrapper-tool-card-v1.html`.
- Async starter tools return `job_id`.
- Real Codex plan jobs complete through MCP.
- Structured Codex result parsing is clean.
- Direct write, bash, and transcript reads deny by default.
- Token-gated tunnel startup fails closed without `CODEX_MCP_HTTP_TOKEN`.
- Direct tokenized public-tunnel MCP probes pass before treating real ChatGPT UI failures as tool-selection or descriptor failures.
- Logs and runtime files do not contain real tokens, prompt bodies, or private paths in committed docs.


## Phase 4 Worker Integration Eval

Run after Phase 4 changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/worker_phase4_eval.py --timeout 900
```

This proves that a real isolated writing worker result can be previewed, explicitly applied to the base checkout, and preserved in the worker worktree without exposing private paths.
