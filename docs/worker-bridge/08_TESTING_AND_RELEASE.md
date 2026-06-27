# Testing And Release Plan

Status: Phase 4 worker gates, artifact inbox transfer, and shared-server multi-client coordination implemented; direct MCP and tokenized public-tunnel artifact-flow evidence exists; real ChatGPT Developer Mode UI validation remains a release gate.

## Testing Principle

Test exact mechanics deterministically and test the natural product workflow through real ChatGPT and real Codex.

Do not build a large semantic grading system for worker report quality before real use. Start with clear behavioral scenarios and inspect failures.

## Baseline Required For Every Worker Phase

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
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json
```

Record:

- repository commit before integration;
- Codex CLI version;
- test count and result;
- live probe result;
- skipped real-Codex or ChatGPT scenarios.

## Unit Test Matrix

Worker regression coverage should cover:

- worker persistence;
- name resolution;
- job linkage;
- reconciliation;
- busy-worker rejection without a queue;
- worker worktrees;
- concurrency limits;
- bounded reports;
- worker inspection views;
- peer-worker report/change/diff context;
- team-report projection;
- integration preview/apply;
- session-local tool modes;
- shared-server ownership flags and explicit takeover;
- per-repository mutation locks and `repo_busy` refusals;
- multi-client direct MCP trial evidence;
- public tool descriptors and mode filtering;
- compatibility with existing low-level tools.

## Deterministic Disposable-Repo Scenarios

### Worker State Without Real Codex

Use a recording/fake executor:

- start worker;
- complete fake job with session and report;
- restart managers;
- message worker;
- verify continuation request;
- stop worker.

### Worker Worktree

- initialize temporary git repo;
- create isolated worker;
- modify tracked and untracked files;
- inspect changes;
- verify main repo unchanged;
- restart runtime;
- verify same worktree;
- explicitly clean up.

### Parallel Workers

- two isolated workers from the same base;
- distinct branches and paths;
- concurrent fake jobs;
- independent reports and queues;
- no cross-resolution.

### Peer Context

- create a source worker report;
- start a reviewer with source report context;
- create source worker changes and pass bounded diff context;
- message a target worker with another worker's report;
- verify no job ids, session ids, branch names, or private paths appear in public output or peer-context prompts.

### Integration

- clean worker patch;
- target modified in unrelated file;
- target modified in same file;
- untracked file collision;
- binary file;
- patch already applied;
- preview leaves target untouched.

## Real Codex Scenarios

Run only on disposable repositories:

- read-only worker start and continuation;
- isolated writing worker with same worktree across turns;
- multi-worker investigation, implementation, review, and report relay;
- stop and later continuation;
- preview and apply accepted change.

## Real ChatGPT Developer Mode Scenarios

These are release-critical because the product is specifically a ChatGPT-to-Codex bridge:

- ChatGPT selects worker tools for a natural worker request.
- ChatGPT continues a worker by name without user-supplied session IDs.
- ChatGPT starts an independent review worker.
- ChatGPT asks for targeted evidence, not raw transcripts.
- ChatGPT explicitly integrates an accepted result and receives conflict explanation when needed.
- ChatGPT understands that one Server URL is shared local state and asks before takeover when another conversation owns a worker or artifact.

## Tunnel And Auth

Before public release, repeat representative worker flows through supported token-gated public tunnel configuration if tunnel use is advertised. Local direct public-tunnel validation has proved MCP health, `initialize`, worker-mode `tools/list`, artifact inbox transfer, isolated worker artifact read, integration exclusion, base checkout preservation, and cleanup through ngrok; it did not prove ChatGPT UI setup, natural tool selection, or ChatGPT-originated worker flows from the actual UI.

The direct multi-client MCP trial is the current regression gate for one shared local Server URL. It must pass before treating shared-server behavior as documented: two logical sessions, session-local tool modes, shared inspection, cross-owner mutation refusal, explicit takeover, ownership transfer, preview-before-integrate, no automatic commit, and sanitized evidence.

Verify:

- missing token fails closed;
- token is not logged or returned unless `--reveal-token` is explicitly requested for the ChatGPT Server URL copy/paste flow;
- worker state files remain local;
- private local paths do not appear in normal ChatGPT cards;
- cancellation and shutdown leave no child process.

## Release Gates

### W1: Single Worker Ready

- Phase 1 tests pass.
- Real Codex start/continue/restart passes.
- Existing suite passes.
- Real ChatGPT remains a separate release gate unless explicitly verified.

### W2: Writing Worker Ready

- Phase 2 tests pass.
- Same worktree across turns.
- Main repo remains unchanged.
- Cleanup behavior proven.
- `scripts/worker_phase2_eval.py --timeout 900` passes against real Codex.

### W3: Team Ready

- Phase 3 tests pass.
- Real peer diff/report relay passes through `scripts/worker_phase3_eval.py --timeout 900`.
- Team report is readable and omits backend ids/private paths.
- Worker-first ChatGPT selection passes.

### W4: Integration Ready

- Conflict and clean integration tests pass.
- Real disposable integration passes.
- No partial target mutation in failure cases.

### W5: Release Ready

- Real ChatGPT Developer Mode worker scenarios pass.
- Direct tunnel/auth MCP scenario passes, and ChatGPT-originated tunnel worker flow passes from the real UI if included.
- Documentation accurately states verified versus pending behavior.
- App-server is either verified and enabled or explicitly deferred.

## Completion Language

Each worker phase report must distinguish:

```text
implemented
unit-verified
live-local-MCP-verified
real-Codex-verified
real-ChatGPT-verified
public-tunnel-verified
not verified
```

A passing unit suite does not imply the worker UX works in ChatGPT.
