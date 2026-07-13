# Implementation And Verification Plan Seed

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Status: implemented and live verified. `resolved-contract-addendum.md` remains
the normative contract and the WorkPacket ledger records implementation order.

## Implementation Strategy

Do not rebuild all 31 tools as independent patches. Build the shared control
plane in dependency order, then expose tool families through the canonical
registry.

## Phase 0: Freeze Contracts And Baseline

Deliverables:

- approve this design pack after conflict review;
- snapshot current V1 manifests, Hub state schema, Edge capabilities, and tests;
- add an explicit V1 compatibility fixture;
- classify existing Hub docs as current V1 versus target V2;
- create a public tool parity manifest with all 31 target tools;
- define current deployment rollback/fallback requirements.

Gate:

- no runtime behavior change;
- current single-machine and Hub tests still pass;
- implementation WorkPackets have disjoint ownership.

## Phase 1: Identity, Vocabulary, And Canonical Capability Registry

Deliverables:

- shared descriptor/handler registry;
- generated Hub worker/workspace/Pro Request schemas;
- common routing and semantic output envelopes;
- strict argument validation;
- truthful annotations and output schemas;
- Hub/Edge contract version and schema hashes;
- parity and descriptor regression tests.

Do not yet claim routed tools work end to end.

The stable principal/conversation/transport/work-run vocabulary, Edge
generation, logical workspace identity, public result vocabulary, operation
transition tables, and idempotency rules in `resolved-contract-addendum.md`
must be represented in the registry and schema interfaces before storage or
broker implementation begins.

## Phase 2: Transactional State And Migration

Deliverables:

- versioned SQLite schema and migrations;
- atomic machine/workspace/owner/group/lane/worker/operation/event records;
- import of current V1 machine/group history as legacy state;
- corruption/recovery behavior;
- pruning/retention policies;
- state backup and rollback procedure;
- concurrency tests across heartbeat, MCP calls, Edge result delivery, and CLI
  inspection.

Forbidden:

- silent reset on migration/corruption;
- treating filesystem read failure as empty state;
- storing temporary download URLs in durable tables.

## Phase 3: Operation Broker And Edge Receipt Protocol

Deliverables:

- operation IDs and idempotency keys;
- attempt/lease tokens and compare-and-set terminal transitions;
- bounded synchronous result waiting;
- pending/unknown outcome response path;
- Edge durable deduplication receipts;
- lease renewal/expiry/reconciliation;
- safe cancellation of unclaimed operations;
- no stale terminal overwrite;
- transient payload delivery/acknowledgement/TTL;
- `patchbay_operation_status`.

Failure tests:

- crash before claim;
- crash after claim before execution;
- execution succeeds but result POST is lost;
- duplicate result POST;
- duplicate MCP retry;
- Hub restart during each state;
- Edge restart during each state;
- integration retry cannot apply twice.

## Phase 4: Stable Identity And Logical Workspaces

Deliverables:

- stable owner versus conversation/transport/work-run identities;
- current-group mapping per conversation;
- participant/takeover records;
- logical workspace refs and machine-local projections;
- strict preflight using Edge path guards and repository identity;
- capability/version compatibility gate;
- workspace discovery/open tools.

Tests:

- new MCP session in same conversation;
- new conversation under same owner;
- concurrent conversations on different groups;
- explicit same-group resume/takeover;
- private result visibility;
- same repo under different machine paths;
- path outside allowed roots fails preflight;
- stale/incompatible Edge blocks group readiness.

## Phase 5: Authoritative Worker Projection

Deliverables:

- immutable fleet worker refs;
- Edge worker projection events/revisions;
- Hub worker/list/status/wait state;
- nonblocking Hub event waits;
- group/lane derivation from actual workers;
- report/checkpoint/change/integration/cleanup references;
- heartbeat and control delivery independence.

Tests:

- active worker remains active after start dispatch finishes;
- idle/completed/failed/stopped/lost transitions;
- quiet worker is not treated as failed;
- waiting does not delay message/stop/heartbeat;
- old worker remains routable after successor group creation;
- group close rejects active/uncertain/unintegrated work.

## Phase 6: Full Worker Surface Parity

Implement through the shared registry:

- options;
- inbox;
- single start;
- batch start;
- message;
- list;
- status;
- wait;
- inspect;
- integrate;
- stop.

Parity requirements:

- all existing mature fields and semantics preserved;
- grouped target preferred and machine inferred;
- ungrouped exceptional route explicit;
- context from workers/artifacts works on same Edge;
- follow-up is next-turn continuation; an active turn returns
  `active_turn_in_progress` rather than accepting steering;
- inspect pagination preserved;
- integration preview token required in Hub mode;
- stop/cleanup cannot discard unintegrated changes silently.

## Phase 7: Group Lifecycle And Successors

Deliverables:

- create/list/status/resume/close;
- readiness/activity/outcome axes;
- availability-only placement explanations;
- successor-based reassign;
- group close dispositions;
- collision warnings for same logical workspace;
- current/history scopes without global clutter.

Tests:

- same group workers stay pinned after load changes;
- full machine blocks/queues according to policy without scatter;
- offline machine produces wait/reassign choice;
- reassignment uses logical workspace on successor;
- old worker controls remain on old machine;
- closed groups are immutable and hidden by default;
- close cannot claim success over active work.

## Phase 8: Manager Inspection And Pro Requests

Deliverables:

- routed workspace open/tree/search/read/changes;
- machine/group-aware Pro Request list/read/claim/respond/dispatch/close;
- machine-qualified artifact/request references;
- claim leases and revision checks;
- result visibility parity.

Tests:

- direct inspection is correctly read-only;
- file paging and search timeout recovery;
- worker-created files remain under worker inspect before integration;
- respond never dispatches implicitly;
- dispatch routes to the correct owning Edge/worker;
- another owner cannot read operation/report/file results.

## Phase 9: Instructions And Documentation

Only after behavior exists:

- update Hub initialize instructions;
- update public tool surface and Hub/Edge reference;
- update ChatGPT manager instructions and copy-paste brief;
- update README/Quickstart/configuration/security/testing docs;
- document current versus legacy state and migration;
- remove V1 target claims that are no longer true;
- preserve exact failure/continuation guidance.

## Phase 10: Standard Verification

Required commands from repo authority:

```bash
python -m compileall src scripts tests
python -m pytest tests -q
python scripts/live_mcp_eval.py --json
```

Additional required suites:

- target 31-tool manifest/parity tests;
- schema/annotation/validation tests;
- Hub state migration tests;
- operation failure-injection tests;
- multi-session ownership tests;
- two-Edge group/routing/reassignment tests;
- worker parity and integration tests through Hub.

## Phase 11: Consequential Local Live Evaluation

Run a real temporary Hub and real EdgeRunner against a disposable Git repo.
Use a small/cheap Codex model when available.

Required sequence:

1. initialize MCP and list the exact target tools;
2. list logical workspaces;
3. create a group and complete real Edge preflight;
4. batch-start at least two workers in separate lanes;
5. wait through Hub projections;
6. inspect both reports;
7. message the same workers with corrections;
8. pass one worker's report/review context to another;
9. have a writing worker change a disposable file;
10. inspect report, changed files, file page, diff, and integration preview;
11. integrate with preview token;
12. verify base changed and no commit was created;
13. stop/clean remaining workers deliberately;
14. prove group status is idle and close it;
15. restart Hub/Edge and prove history/ownership remains coherent.

## Phase 12: Failure-Injection Live Evaluation

Repeat with controlled interruption:

- Edge offline before claim;
- Edge killed after claim;
- Edge killed after worker start but before result acknowledgement;
- duplicate MCP start call;
- Hub restart while workers continue;
- lost result response;
- status wait concurrent with message and stop;
- group machine offline then successor reassign;
- old worker inspected/messaged after successor creation;
- integration response lost after apply;
- two ChatGPT/MCP sessions contend for one group/request.

Expected evidence:

- no duplicate workers/turns/integrations;
- explicit pending or unknown outcome where truth is unavailable;
- eventual reconciliation from Edge receipts/projections;
- no stale operation overwrites terminal result;
- no group closes active work as complete.

## Phase 13: Two-Edge Live Evaluation

With two real Edges:

- verify fleet telemetry and contract compatibility;
- create one group without machine choice and verify availability placement;
- start several workers and prove all stay on the pin;
- change load and prove existing group does not move;
- create another group and verify router may choose the other Edge;
- create a successor group and verify predecessor workers remain controllable;
- run a bounded write/integration flow on each Edge.

## Phase 14: Real ChatGPT Acceptance

Connect through the real public/tunnel MCP endpoint and use a fresh ChatGPT
conversation with no private implementation knowledge.

Golden prompts must test:

- first orientation and group creation;
- parallel team appointment;
- patient monitoring;
- repeated follow-up to the same worker;
- worker-to-worker context;
- focused manual inspection exception;
- integration preview and apply;
- tool-call interruption/continuation note;
- serious PatchBay failure stop behavior;
- minor friction continuation behavior;
- new conversation resumes an owned group;
- no command-ID management during normal success.

## Release Gate

Do not deploy or claim Hub V2 ready until:

- all standard/failure/live/two-Edge/ChatGPT scenarios pass;
- no private machine data or credentials appear in public source/docs/tests;
- migration and rollback are tested;
- docs describe implemented reality;
- independent architecture and final mission reviews accept the evidence.

Production-boundary acceptance is explicit: a live evaluator may provide test
executors for Codex itself, but it must not bypass the real Hub adapter, Edge
transport, `ToolHandler`, worker runtime, result receipt, or Pro Request
projection paths. Include long-history cases above 100 retained records so
pagination and queue limits are tested after realistic accumulated use.

## Review Lanes Required

- architecture/state-machine review;
- concurrency/idempotency review;
- MCP schema and ChatGPT tool-selection review;
- worker-runtime parity review;
- visibility/privacy boundary review;
- migration/deployment review;
- independent final live-evidence review.

## Evidence Required Before Completion

- test commands and full results;
- tool manifest dump and schema hashes;
- migration report;
- real local lifecycle artifact;
- failure-injection operation ledger;
- two-Edge routing and successor report;
- real ChatGPT session transcript summary with tool sequence;
- final docs audit;
- explicit residual-risk register.
