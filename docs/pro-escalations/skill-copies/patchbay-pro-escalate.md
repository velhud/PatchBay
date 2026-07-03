---
name: patchbay-pro-escalate
description: Create a structured ChatGPT Pro escalation package when Codex, a PatchBay worker, or a local agent is blocked on a repository task. Use when the user says "escalate to ChatGPT Pro", "prepare Pro escalation", "ask Pro", "create reverse inbox request", "Codex is stuck", or repeated local attempts failed. Do not use for ordinary implementation, simple syntax errors, or tasks that can be resolved locally.
---

# PatchBay Pro Escalation Skill

## Purpose

Use this skill to convert a blocked local engineering problem into a high-quality PatchBay Pro Escalation Request.

The goal is to avoid manual copy-paste between local Codex and ChatGPT Pro. The output must be a clear diagnostic package that ChatGPT Pro can later read through PatchBay, reason about, query the repository/workers for additional context, and answer.

## Core Rule

A Pro Escalation is not a generic task, worker queue item, automatic background ChatGPT job, scheduler, commit, or apply operation. It is a durable blocked-problem report waiting for an active ChatGPT Pro session to read it.

After creating the escalation, stop speculative implementation unless the user explicitly tells you to continue.

## Collect

- Repository root/name, branch, short HEAD, dirty status, dirty file summary.
- Original task and definition of done.
- Exact blocker and why local Codex cannot confidently continue.
- Commands run, focused outputs, errors, logs, changed files, failed hypotheses, and relevant file paths.
- A precise question for ChatGPT Pro.
- Desired answer format:
  1. root cause / conceptual diagnosis;
  2. correct architecture;
  3. file-level implementation plan;
  4. tool/CLI/API details;
  5. tests;
  6. risks and what not to do;
  7. worker-ready instruction.

Keep logs and diffs bounded. Do not attach the whole repository. Redact obvious secrets.

## Draft Location

Write the draft report under:

```text
.ai-bridge/pro-requests/drafts/<timestamp-slug>/report.md
```

Optional supporting files go beside it, such as `test-output.txt`, `relevant-diff.patch`, or `context-notes.md`.

## Report Template

```md
# Pro Escalation Request

## One-sentence problem

## Original task

## Current repository state

- Repo:
- Root:
- Branch:
- Head:
- Dirty:
- Dirty files:

## Origin

- Origin kind: patchbay_worker | terminal_codex | local_agent | human
- Worker/session name, if known:
- Whether origin can be resumed by PatchBay:

## What was attempted

## What failed

## Exact evidence

### Commands run

```bash
```

### Output / errors

```text
```

## Relevant files

## Current diff summary

## Focused diff, if needed

## Hypotheses considered

## What I need from ChatGPT Pro

## Desired answer format

1. Root cause / conceptual diagnosis
2. Correct architecture
3. File-level implementation plan
4. Tool/CLI/API details
5. Tests
6. Risks and what not to do
7. Worker-ready instruction
```

## Submit To PatchBay

If available:

```bash
patchbay pro-request create \
  --repo "<repo-root>" \
  --title "<short blocker title>" \
  --kind "debugging" \
  --priority "normal" \
  --origin-kind "<patchbay_worker|terminal_codex|local_agent|human>" \
  --origin-worker "<worker name if known>" \
  --report "<path-to-report.md>" \
  --desired-output "Root cause, correct architecture, file-level implementation plan, tests, risks, worker-ready instruction"
```

If the CLI is unavailable, do not pretend submission occurred. Report the draft path.

## Final Response

End with either:

```text
Created Pro Escalation Request: proreq_...
Report: <path>
Origin: <origin>
Status: open
```

or:

```text
Prepared Pro Escalation draft only.
Report: <path>
Reason not submitted: patchbay pro-request CLI is unavailable.
```
