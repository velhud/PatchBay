# Current Readiness

This page preserves the detailed readiness matrix that used to live near the top of the root README. The README now keeps only a compact product-facing status summary.

PatchBay is **pre-release verified**, not public-release complete.

| Area | Status |
| --- | --- |
| Codex CLI baseline | Current local verification recorded `codex-cli 0.142.2` |
| Python checks | `compileall` passes |
| Test suite | `356` tests pass |
| Live local MCP probe | `scripts/live_mcp_eval.py --json` passes against a disposable repo |
| Pro Escalation request loop | Unit tests and the live MCP probe cover CLI create, MCP list/read/claim/respond, CLI response readback, and blocked origin-worker dispatch |
| Named worker continuity eval | `scripts/worker_phase1_eval.py --timeout 600` passes real Codex start/restart/continue |
| Isolated writing worker eval | `scripts/worker_phase2_eval.py --timeout 900` passes real Codex isolated write/restart/continue/diff/cleanup |
| Multi-worker coordination eval | `scripts/worker_phase3_eval.py --timeout 900` passes real Codex peer diff/report relay |
| Worker integration eval | `scripts/worker_phase4_eval.py --timeout 900` passes real Codex integration preview/apply |
| Real MCP worker negative-case trial | `scripts/real_mcp_worker_trial.py --include-safety-cases` passes direct MCP worker lifecycle and negative cases |
| Direct multi-client MCP trial | `scripts/real_mcp_worker_trial.py --multi-client --include-safety-cases --tool-mode worker --json` passes two-session tool-mode, ownership, takeover, safety refusals, preview, integration, and artifact sanitization checks |
| Fresh-worker stop protection | A focused live MCP probe confirms ordinary `codex_worker_stop` on a newly started worker returns `stop_confirmation_required: true`; `force: true` then stops it |
| Public tunnel MCP probe | Earlier tokenized ngrok MCP simulator passed health, `initialize`, worker-mode `tools/list`, artifact inbox import/list/inspect, isolated worker artifact attachment/read, integration exclusion, and cleanup; current run blocked only because no validation ngrok hostname was provided |
| Real Codex through MCP | `codex_plan_job` completes through PatchBay |
| Current Codex JSONL parsing | `agent_message` results parse into structured output |
| Active ChatGPT Pro VM worker use | Working reliably in current internal use for ChatGPT Pro to a private PatchBay VM managing local Codex workers; occasional small bugs are still expected |
| Parallel ChatGPT browser conversations | Pending; multiple independent ChatGPT browser conversations sharing one Server URL have not yet been tried |
| Real apply-job diff eval from ChatGPT | Pending |
| Real resume/continuation eval from ChatGPT | Pending |

## Local baseline

Run:

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

The live eval does not use ChatGPT and does not open a public tunnel. It starts the real launcher/server against a temporary repo and behaves like a compact MCP client.

## Shared-server coordination check

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --multi-client --include-safety-cases --tool-mode worker --json
```

That direct MCP trial uses two logical MCP sessions against a disposable repo. It verifies session-local tool modes, shared inspection, cross-owner mutation refusal, explicit takeover, ownership transfer, safety refusals, preview-before-integrate, no automatic commit, connector-noise scanning, and sanitized private evidence under `.local/validation/`.
