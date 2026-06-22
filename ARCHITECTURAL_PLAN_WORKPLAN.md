# Architectural Plan Workplan v2

## Failure Reset

The previous version of this workplan was not strict enough. It described phases, but it did not force the future architecture plan to earn its conclusions through repeated source inspection, alternative designs, counterarguments, consequence mapping, and adversarial review.

This v2 sets a higher standard:

- no architectural claim without source evidence;
- no subsystem decision without alternatives;
- no chosen direction without a counterargument;
- no "safe" default that quietly removes product power;
- no power feature without a named control model;
- no final architecture plan until skeptical review has either changed the plan or explicitly accepted the risk.

The purpose of this file is to define how to produce `FULL_INTEGRATION_ARCHITECTURE_PLAN.md`. This file is not the integration plan itself. It is the process that makes the integration plan hard to write shallowly.

The fixed final target:

- final release repository: `codex-mcp-wrapper`;
- CodexPro is MIT-licensed source material;
- large CodexPro copies are allowed;
- wrapper rewrites are allowed;
- runtime replacement is allowed;
- nothing in either codebase is untouchable;
- the final product must be a powerful ChatGPT-to-local-Codex bridge.

The future architecture plan must optimize for final user capability, not sentimental attachment to either current implementation.

## Output Contract

Following this workplan must produce:

- `FULL_INTEGRATION_ARCHITECTURE_PLAN.md`

The final plan may also create appendices if necessary, but the primary document must contain all major decisions and a runnable implementation sequence.

Required final plan sections:

1. Executive decision summary.
2. Product goal and non-goals.
3. End-to-end ChatGPT user workflows.
4. Full source inventory summary.
5. Runtime architecture decision.
6. CodexPro transplant ledger.
7. Wrapper keep/rewrite/delete ledger.
8. Public MCP contract.
9. Internal service architecture.
10. Workspace/context architecture.
11. Codex execution and worktree architecture.
12. Connector, auth, tunnel, and profile architecture.
13. Power-mode architecture.
14. State, artifacts, logging, and observability.
15. Licensing and attribution plan.
16. Implementation roadmap.
17. Evals, release gates, and rollback criteria.
18. Explicit unresolved risks, if any.

The final plan must be implementation-ready. Another engineer should be able to start the first PR from it without deciding the architecture again.

## Grounding Protocol

The future planning pass must begin by reading source and docs, not by writing architecture.

### Wrapper Material To Inspect

Inspect every relevant root file and grouped subtree:

- `server.py`
- `mcp_protocol.py`
- `tools.py`
- `job_manager.py`
- `job_executor.py`
- `security.py`
- `config.yaml`
- `requirements.txt`
- `scripts/`
- `tests/`
- `examples/`
- `maintainer-workflows/`
- `docs/`
- root docs and security docs

For each item, record:

- responsibility;
- public behavior;
- dependencies;
- state touched;
- side effects;
- tests covering it;
- gaps or contradictions;
- whether it is worth preserving.

### CodexPro Material To Inspect

Inspect every relevant CodexPro group:

- package metadata and lockfile;
- `src/server.ts`;
- `src/http.ts`;
- `src/config.ts`;
- `src/guard.ts`;
- `src/fsOps.ts`;
- `src/searchOps.ts`;
- `src/gitOps.ts`;
- `src/workspaceOps.ts`;
- `src/proContext.ts`;
- `src/profileStore.ts`;
- `src/capabilitiesOps.ts`;
- `src/codexSessions.ts`;
- `src/bashOps.ts`;
- `src/toolCardWidget.ts`;
- `src/redact.ts`;
- `src/stdio.ts`;
- `scripts/codexpro.mjs`;
- smoke and doctor scripts;
- upstream README and SECURITY material.

For each item, record:

- responsibility;
- product value;
- code quality;
- hidden assumptions;
- dependencies;
- side effects;
- security or privacy implications;
- whether it should be copied, ported, rewritten, deferred, or rejected.

### Evidence Ledger

Before architectural decisions, create an evidence ledger inside the final plan or an appendix.

Minimum columns:

| Area | Source file/group | Observed behavior | Strength | Weakness | Open question | Evidence reference |
| --- | --- | --- | --- | --- | --- | --- |

No subsystem decision may be accepted until its relevant evidence row exists.

## Decision Loop

Every major subsystem must go through this loop.

### Step 1: Evidence

Summarize what the source actually does. Do not infer architecture from names alone. Use code paths, tests, docs, and smoke behavior.

### Step 2: First Hypothesis

State the most obvious decision, even if it seems right:

- keep wrapper implementation;
- copy CodexPro implementation;
- port CodexPro behavior;
- rewrite both;
- delete/defer;
- expose as power mode.

### Step 3: Counterargument

Attack the first hypothesis. Ask:

- What breaks if this is wrong?
- What hidden cost does it create?
- What user workflow does it fail?
- What test would expose weakness?
- Does it preserve old architecture for weak reasons?
- Does it reject CodexPro copying for weak reasons?
- Does it reduce product power under the excuse of safety?

### Step 4: Alternatives

List at least three alternatives for important subsystems, unless the source evidence proves only one viable path.

At minimum compare:

- use wrapper implementation;
- use CodexPro implementation;
- build hybrid;
- rewrite from scratch.

### Step 5: Consequences

For each serious alternative, document consequences:

- implementation effort;
- packaging impact;
- public API impact;
- test impact;
- user workflow impact;
- performance or latency impact;
- auth/security impact;
- migration and rollback impact;
- maintenance impact.

### Step 6: Decision

Choose one direction and mark it:

- `confirmed`;
- `tentative, needs spike`;
- `defer`;
- `reject`;
- `power-mode only`.

### Step 7: Validation Gate

Define the exact tests, probes, or review steps that would prove the decision works.

Every decision must end with:

- accepted approach;
- rejected alternatives;
- known risks;
- validation method;
- implementation phase.

## Architecture Outcomes To Compare

The future plan must compare at least these outcomes before choosing the final shape.

### Outcome A: Python Wrapper Absorbs CodexPro

The wrapper remains Python/FastAPI. CodexPro behavior is ported or copied conceptually into Python.

Evaluate:

- can Python implement ChatGPT tool-card/resource behavior cleanly;
- how much CodexPro code becomes unusable because of language mismatch;
- whether wrapper job execution benefits outweigh porting cost;
- how quickly the ChatGPT connector becomes strong;
- whether this path underuses CodexPro.

### Outcome B: TypeScript CodexPro Core Absorbs Wrapper Engine

The final repo becomes TypeScript-first. Wrapper job engine concepts are ported into the CodexPro-style runtime.

Evaluate:

- speed of reusing CodexPro connector and UI systems;
- cost of porting async jobs, worktrees, Codex execution, tests, and redaction;
- risk of losing wrapper's useful job boundaries;
- packaging and release impact;
- whether Python code becomes obsolete.

### Outcome C: Dual-Runtime Hybrid

One runtime owns connector/context, another owns Codex execution.

Evaluate:

- process supervision;
- auth and policy duplication;
- failure recovery;
- logs and artifacts;
- IPC boundary;
- test complexity;
- whether it is acceptable as temporary or permanent architecture.

### Outcome D: Wholesale Transplant/Rewrite Inside Wrapper Repo

Large parts of CodexPro are copied into the wrapper repo and the wrapper is reorganized around a new architecture.

Evaluate:

- fastest route to product power;
- code deletion opportunities;
- compatibility breakage;
- licensing/NOTICE requirements;
- whether the result is coherent or just glued together;
- what must be rewritten after copy.

### Outcome E: Minimal Integration

Only small CodexPro concepts are ported.

Evaluate honestly, then likely reject if it fails the user's goal. This option exists to prevent unexamined overbuilding, but the final product goal probably requires more than minimal integration.

## Subsystem Workstreams

Each workstream must produce evidence, alternatives, decision, consequences, and validation gates.

### Product UX And ChatGPT Flow

Questions to answer:

- What is the ideal first-run experience?
- What does ChatGPT see before any tool call?
- How does the user select or switch workspace?
- What can ChatGPT do without manual export/import?
- Where does direct Codex job delegation fit?
- Where does `.ai-bridge` handoff fit?
- What is the difference between default mode and power mode?
- What does failure look like in ChatGPT?

Deliverables:

- workflow map;
- ChatGPT-visible tool journey;
- first-run setup story;
- failure recovery story.

### Runtime And Package Architecture

Questions to answer:

- Which runtime owns the MCP server?
- Which runtime owns Codex execution?
- Which runtime owns setup/start/doctor?
- Is a sidecar allowed, and if so for how long?
- What files move, what files stay, what files disappear?
- How is the package installed and started?

Deliverables:

- runtime ADR;
- package layout;
- migration map;
- rollback plan.

### MCP Transport And Public Tools

Questions to answer:

- What is the final public tool list?
- Which tools are default, optional, compatibility-only, or internal-only?
- What schemas and output formats do tools use?
- What ChatGPT `_meta` and annotations are required?
- Which tools get tool-card resources?
- How are tool errors structured?

Deliverables:

- public MCP contract;
- schema/mutability matrix;
- descriptor metadata plan;
- compatibility plan.

### Workspace Context And `.ai-bridge`

Questions to answer:

- How is active workspace chosen?
- How are allowed roots enforced?
- How are paths normalized and symlink escapes blocked?
- How are tree/search/read bounded?
- How are AGENTS and skills discovered?
- What is the context pack format?
- Which `.ai-bridge` files exist and who writes them?
- What is source context versus execution artifact?

Deliverables:

- context architecture;
- path policy;
- `.ai-bridge` spec;
- context pack spec.

### Codex Job Engine And Worktrees

Questions to answer:

- What is a job?
- What states can it enter?
- What owns process handles?
- How are prompts passed to Codex?
- How are `codex exec` options built?
- How are plan jobs forced read-only?
- How are apply jobs isolated?
- How are diffs stored and served?
- How does resume work?
- How does cancellation work?

Deliverables:

- execution service design;
- job lifecycle state machine;
- worktree lifecycle;
- artifact and diff contract.

### Auth, Tunnels, Profiles, Setup UX

Questions to answer:

- What is allowed on localhost without auth?
- What requires bearer token?
- Are query tokens allowed for copied ChatGPT URLs?
- When is OAuth required or deferred?
- How do tunnels fail closed?
- What does setup generate?
- Where are profiles stored?
- What does doctor verify?

Deliverables:

- auth/tunnel model;
- profile schema;
- setup/start/doctor design;
- self-test behavior.

### Power Tools

Questions to answer:

- Should direct edit/write exist?
- Should safe bash exist?
- Should full bash exist?
- Should Codex session metadata exist?
- Should transcript read exist?
- Should broad context export exist?
- Which are default, optional, or forbidden?
- What warnings and confirmations are needed?

Deliverables:

- power-control matrix;
- tool availability rules;
- denial behavior;
- tests per power mode.

### Observability, Logs, Artifacts, Evals

Questions to answer:

- What is logged by default?
- What is never logged?
- Where do job artifacts live?
- What is redacted?
- How are traces correlated?
- How is a run replayed or diagnosed?
- What evals prove the app works?

Deliverables:

- logging policy;
- artifact store design;
- trace/event model;
- eval suite.

### Licensing, Attribution, Release Packaging

Questions to answer:

- Which copied CodexPro code requires attribution?
- Does the wrapper need a `NOTICE` file?
- How is upstream provenance recorded?
- How does the package communicate that this is not an upstream CodexPro fork?
- What migration notes are needed for existing wrapper users?

Deliverables:

- attribution plan;
- release checklist;
- migration notes.

## Transplant Method

The final architecture plan must not use vague phrases like "borrow ideas" when a concrete transplant is possible. It must classify each item precisely.

### Copy Wholesale

Use when:

- CodexPro implementation is cohesive;
- the target runtime can use it directly;
- copying saves substantial time;
- policy boundaries remain clear;
- attribution is preserved.

Required record:

| CodexPro source | Wrapper destination | Changes needed | License note | Tests |
| --- | --- | --- | --- | --- |

### Port Concept

Use when:

- CodexPro behavior is valuable but runtime/language differs;
- wrapper service boundaries are better;
- source code depends on incompatible package structure;
- direct copy would create duplicated policy.

Required record:

| Behavior | Source evidence | New wrapper service | Compatibility requirements | Tests |
| --- | --- | --- | --- | --- |

### Rewrite Both

Use when:

- both implementations have important flaws;
- combining them would create confusing state;
- public product needs a cleaner abstraction.

Required record:

| Old wrapper behavior | CodexPro behavior | New design | Deleted code | Tests |
| --- | --- | --- | --- | --- |

### Delete

Use when:

- a feature is obsolete;
- a handler is hidden and dangerous;
- a compatibility alias should be retired;
- a direct tool conflicts with the final model;
- a subsystem becomes redundant after transplant.

Required record:

| Item | Why delete | Replacement | Compatibility risk | Removal phase |
| --- | --- | --- | --- | --- |

### Power-Mode Only

Use when:

- the capability is valuable but high-risk;
- default exposure would be too broad;
- explicit user enablement makes it useful.

Required record:

| Capability | Default | Enablement | Tool names | Denial behavior | Tests |
| --- | --- | --- | --- | --- | --- |

## Review Loop

Before finalizing `FULL_INTEGRATION_ARCHITECTURE_PLAN.md`, run adversarial reviews from these perspectives.

### Product Manager Review

Challenge:

- Does this actually solve the ChatGPT-to-local-Codex workflow?
- Does it remove manual conversation export/import?
- Are default workflows powerful enough?
- Are power modes discoverable?
- Is setup understandable?

Required output:

- product gaps;
- workflow friction;
- missing user outcomes;
- recommended changes.

### Systems Architect Review

Challenge:

- Are boundaries coherent?
- Is runtime choice justified?
- Is there duplicated policy?
- Is state ownership clear?
- Can failures be recovered?

Required output:

- architectural contradictions;
- state ownership issues;
- complexity risks;
- recommended changes.

### MCP/App Connector Engineer Review

Challenge:

- Are tool descriptors ChatGPT-ready?
- Are schemas strict?
- Are tool-card resources feasible?
- Are auth and sessions compatible with ChatGPT Developer Mode and Apps expectations?

Required output:

- descriptor gaps;
- connector incompatibilities;
- metadata/resource risks;
- recommended changes.

### Codex Execution Engineer Review

Challenge:

- Does Codex CLI invocation work?
- Are jobs resumable?
- Are worktrees isolated?
- Are diffs correct?
- Are logs and artifacts useful?
- Does cancellation work?

Required output:

- execution failures;
- worktree risks;
- command builder risks;
- recommended changes.

### Security And Power-Control Review

Challenge:

- Does any power leak accidentally?
- Are tokens/tunnels controlled?
- Are path guards real?
- Are logs safe?
- Are direct edit/bash/session reads correctly gated?

Required output:

- power-control gaps;
- exfiltration paths;
- misleading safety claims;
- recommended changes.

### Release Maintainer Review

Challenge:

- Can this be shipped coherently?
- Are tests enough?
- Is packaging understandable?
- Are docs honest?
- Are compatibility breaks planned?

Required output:

- release blockers;
- migration issues;
- doc gaps;
- recommended changes.

## Skeptical Iteration Requirement

The future planning agent must not stop after the first complete draft.

Minimum loop:

1. Draft architecture from evidence.
2. Run adversarial reviews.
3. Revise architecture.
4. Re-check decisions against source evidence.
5. Re-run reviews on changed sections.
6. Produce final plan with accepted risks.

Each review finding must be marked:

- `resolved by plan change`;
- `accepted risk`;
- `deferred with spike`;
- `rejected with reason`.

The final plan must include a review disposition table.

## Quality Gates

The final architecture plan fails if any gate is unmet.

### Evidence Gates

- Every major source module has been inspected or intentionally excluded with reason.
- Every subsystem decision cites evidence.
- Every CodexPro transplant has source and target.
- Every wrapper rewrite/delete has reason and replacement.

### Product Gates

- ChatGPT can open a workspace.
- ChatGPT can load useful context.
- ChatGPT can delegate Codex plan work.
- ChatGPT can delegate Codex apply work.
- ChatGPT can inspect status, results, and diffs.
- ChatGPT can resume or continue work.
- ChatGPT can use handoff flow.
- Optional direct power is specified, not ignored.

### MCP Gates

- Every public tool has schema, result shape, mutability, and mode.
- Mutating tools are not marked read-only.
- Internal tools are not advertised.
- Compatibility aliases are handled deliberately.
- Tool-card/resource strategy is decided.

### Execution Gates

- Job state machine is defined.
- Process ownership is defined.
- Cancellation is defined.
- Codex command builder is defined.
- Worktree lifecycle is defined.
- Diff contract is defined.
- Artifact retention is defined.

### Power-Control Gates

- Public tunnel requires auth model.
- Direct edit/write requires mode and tests.
- Bash requires mode and tests.
- Session transcript read requires mode and tests.
- Logs do not include raw prompts/secrets by default.
- Path guard blocks traversal and symlink escape.

### Release Gates

- Implementation sequence is PR-sized.
- Each PR has tests.
- Rollback or recovery exists for risky changes.
- Licensing/attribution is handled.
- Docs do not overstate safety.

## Final Plan Acceptance Checklist

`FULL_INTEGRATION_ARCHITECTURE_PLAN.md` is acceptable only when it answers these questions directly:

- What exact product are we building?
- What exact runtime architecture will it use?
- What exact parts of CodexPro are copied, ported, rewritten, deferred, or rejected?
- What exact parts of the wrapper are kept, rewritten, deleted, or replaced?
- What exact MCP tools will ChatGPT see?
- What exact state does the server persist?
- What exact artifacts are created?
- What exact auth/tunnel model is used?
- What exact power modes exist?
- What exact tests prove it works?
- What exact sequence builds it?
- What exact release gate says it is done?

If any answer is vague, the architecture plan is not done.

## Verification For This Workplan

After changing this file, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .
python -m pytest tests -q
```

Also scan this file for:

- local absolute paths;
- usernames;
- API-key-like values;
- connector tokens;
- private prompt content;
- private session content.

Expected repository result for this step:

- `AGENTS.md` remains untouched by this work;
- the previously created investigation docs remain as they were;
- only `ARCHITECTURAL_PLAN_WORKPLAN.md` is replaced for the restart workplan step.
