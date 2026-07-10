# Public Hub Acceptance

This is the release gate for claims that PatchBay Hub works from ChatGPT's
actual connector boundary. Local unit tests and synthetic Hub/Edge evaluators
are necessary, but they are not substitutes for this test.

## Required Boundary

Use the same authenticated public MCP URL and stable path that the real ChatGPT
app uses. Initialize a fresh MCP client, read only the instructions returned by
that server, and operate only through the tools returned by `tools/list`.

The expected Hub V2 catalog is exactly 31 manager-facing tools. A partial
catalog is a release failure even if internal handlers or local tests pass.

## Consequential Scenario

Run against a disposable git repository on a real enrolled Edge:

1. initialize MCP and verify the 31-tool catalog;
2. inspect fleet and workspace projections;
3. create one durable work group and wait for Edge preflight;
4. batch-start two real Codex workers in separate lanes, including one isolated
   writer;
5. wait through Hub worker projections without rapid polling;
6. inspect both semantic reports;
7. continue the same named worker with a natural-language follow-up and verify
   that its Codex session is reused;
8. inspect the writer's changes and signed integration preview;
9. integrate through the public tool, verify the disposable base checkout
   changed, and verify no commit was created automatically;
10. stop or clean remaining workers deliberately;
11. close the group and verify its terminal status;
12. reconnect with a new MCP transport session and prove the durable group and
   worker history remain coherent.

Use unique worker names or `auto_suffix: true` for repeated acceptance runs.
Duplicate-name refusal must be terminal and actionable, never reported as an
unknown mutation outcome.

## Waiting Semantics

`patchbay_worker_wait` without `since_revision` must snapshot the current worker
projection and wait for a later worker-state revision or timeout. Machine
heartbeats and resource telemetry must not wake it. A timeout while a worker is
still active or quiet is normal and must not trigger cancellation.

## Failure Classification

Classify failures at their actual boundary:

- catalog, routing, projection, operation, or result errors are PatchBay;
- repository/test failures belong to the disposable task;
- Codex authentication or subscription quota is an external execution block;
- client-side safety or connector rejection is a client/platform block.

Do not convert an external quota block into a PatchBay pass. Preserve the
durable group, resume after quota recovery, and finish the same scenario.

## Evidence

Keep raw request/response logs and deployment-specific identifiers in ignored
private evidence. Public documentation may record the scenario, date, commit,
tool count, and pass/fail result, but must not include tokens, private hostnames,
machine identifiers, private paths, or user prompts.
