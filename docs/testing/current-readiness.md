# Current Readiness

This page preserves the detailed readiness matrix that used to live near the top of the root README. The README now keeps only a compact product-facing status summary.

PatchBay Hub V2 is implemented. Release verification is build-specific: every
connector-facing repair must pass the current full suite and public Hub
acceptance before deployment, rather than inheriting an older build's evidence.
Deployment-specific raw evidence remains private; the public repository records
the generic verification contract and collaborator-safe results.

| Area | Status |
| --- | --- |
| Codex CLI baseline | Current local verification recorded `codex-cli 0.144.1` |
| Python checks | `compileall` passes |
| Test suite | Current integrated repair candidate: `967 passed, 4 skipped` on macOS and `969 passed, 2 skipped` in the isolated production Linux image. Both runs cover the same 971-test inventory with platform-specific skips. |
| Live local MCP probe | `scripts/live_mcp_eval.py --json` passes against a disposable repo |
| Pro Escalation request loop | Unit tests and the live MCP probe cover CLI create, MCP list/read/claim/respond, CLI response readback, and blocked origin-worker dispatch |
| Named worker continuity eval | `scripts/worker_phase1_eval.py --timeout 600` passes real Codex start/restart/continue |
| Isolated writing worker eval | `scripts/worker_phase2_eval.py --timeout 900` passes real Codex isolated write/restart/continue/diff/cleanup |
| Multi-worker coordination eval | `scripts/worker_phase3_eval.py --timeout 900` passes real Codex peer diff/report relay |
| Worker integration eval | `scripts/worker_phase4_eval.py --timeout 900` passes real Codex integration preview/apply |
| Real MCP worker negative-case trial | `scripts/real_mcp_worker_trial.py --include-safety-cases` passes direct MCP worker lifecycle and negative cases |
| Direct multi-client MCP trial | `scripts/real_mcp_worker_trial.py --multi-client --include-safety-cases --tool-mode worker --json` passes two-session tool-mode, ownership, takeover, safety refusals, preview, integration, and artifact sanitization checks |
| Fresh-worker stop protection | A focused live MCP probe confirms ordinary `codex_worker_stop` on a newly started worker returns `stop_confirmation_required: true`; `force: true` then stops it |
| Public Hub V2 acceptance | The last deployed build passed authenticated production-tunnel initialize, exact 31-tool discovery, fleet/workspace discovery, durable group preflight, real parallel Codex workers, patient wait, report inspection, signed integration without commit, and base verification against a disposable Edge repository. Every connector-facing release must repeat this gate after deployment; local outside-in evaluation is not a substitute for the deployed tunnel. |
| Production entrypoint restart | `scripts/production_entrypoint_restart_eval.py --json --rehearse-old-schema` passes real Hub/Edge CLI startup, enrollment, grouped worker dispatch, clean restart, same-worker follow-up, stable Hub/Edge identity, monotonic durable state, and real schema-2-to-3 Hub/Edge backup/migration/restore. Six additional consecutive Linux rehearsals passed, including two concurrent evaluator processes. |
| Real Codex through MCP | `codex_plan_job` completes through PatchBay |
| Current Codex JSONL parsing | `agent_message` results parse into structured output |
| Active ChatGPT Pro VM worker use | Operational through Hub V2 and enrolled Edges; the public acceptance contract must still be rerun for connector-facing releases |
| Multi-client state | Durable Hub/group state and fresh MCP transport reconnection are covered; independent ChatGPT browser conversations remain a deployment-specific operational exercise |
| Real apply-job diff eval from ChatGPT | Pending |
| Real resume/continuation eval from ChatGPT | Pending |

## Local baseline

Run:

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
PYTHONDONTWRITEBYTECODE=1 python scripts/live_hub_edge_eval.py --json
PYTHONDONTWRITEBYTECODE=1 python scripts/live_hub_v2_eval.py --json
PYTHONDONTWRITEBYTECODE=1 python scripts/production_entrypoint_restart_eval.py --json
```

The live eval does not use ChatGPT and does not open a public tunnel. It starts the real launcher/server against a temporary repo and behaves like a compact MCP client.

## Shared-server coordination check

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --multi-client --include-safety-cases --tool-mode worker --json
```

That direct MCP trial uses two logical MCP sessions against a disposable repo. It verifies session-local tool modes, shared inspection, cross-owner mutation refusal, explicit takeover, ownership transfer, safety refusals, preview-before-integrate, no automatic commit, connector-noise scanning, and sanitized private evidence under `.local/validation/`.
