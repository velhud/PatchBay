---
name: patchbay-pro-response-implementer
description: Consume and implement a ChatGPT Pro response for a PatchBay Pro Escalation Request. Use when the user says "implement the Pro response", "continue from proreq", "consume this Pro escalation answer", "resume the worker from ChatGPT Pro's solution", or when a PatchBay worker receives a stored Pro Request response. Do not use before a Pro response exists.
---

# PatchBay Pro Response Implementer Skill

## Purpose

Use this skill when ChatGPT Pro has already answered a Pro Escalation Request and local Codex must continue implementation safely.

The response is an expert plan and diagnostic answer. It is not an automatic command, patch to blindly apply, or authority over user instructions, `AGENTS.md`, repository rules, or tests.

## First Steps

1. Identify the request id.
2. Load the original report.
3. Load the ChatGPT Pro response.
4. Check current repository state:

```bash
git branch --show-current
git rev-parse --short HEAD
git status --short
git diff --stat
```

If available, prefer:

```bash
patchbay pro-request show <request-id>
patchbay pro-request response <request-id>
```

Otherwise read:

```text
.ai-bridge/pro-requests/<request-id>/report.md
.ai-bridge/pro-requests/<request-id>/response.md
.ai-bridge/pro-requests/<request-id>/status.json
```

## Critical Rule

Do not blindly execute the Pro response. Convert it into a local checklist:

- files to inspect;
- files to modify;
- tests to run;
- risks;
- what not to do;
- definition of done.

If the response is incomplete, contradictory, stale, or incompatible with current repo state, stop and create a follow-up note or escalation.

## Implementation Behavior

If running inside a PatchBay isolated worker worktree:

- modify only the worker worktree;
- do not apply to base checkout;
- do not commit;
- do not delete the worktree;
- report changed files, diff summary, and test results.

If running directly in a base checkout:

- be conservative;
- preserve unrelated local changes;
- do not commit;
- keep diffs focused.

## Final Report

```md
# Pro Response Implementation Report

## Request

- Request id:
- Source response:
- Repo:
- Branch:
- Head:

## Implementation summary

## Changed files

## Tests run

```bash
```

## Test results

```text
```

## Diff summary

```text
```

## Remaining risks

## Follow-up needed
```

If implementation is complete and the CLI supports it:

```bash
patchbay pro-request close <request-id> --reason "Implemented locally and validated. See worker report."
```
