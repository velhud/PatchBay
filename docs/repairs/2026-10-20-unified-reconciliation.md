# Unified reconciliation — 20 October 2026

## Candidate

- Branch: `chatgpt/patchbay-unified-reconciliation-20261020`
- Pull request: `#55`, targeting `main`
- Main pin used for consolidation: `bf31bd4edd4f4ea4307e1be0153e4c1ed4a5463d`
- Main source-equivalent audited base: `678aa3ff93dda5cbfd2c517234504170d4cd3db7`
- Critical repair pin: `58bb843baa3fd810c11071a1eed02f6b44cfd82f`
- Author Mode pin: `ff6a8d3752775d91fa24f049355b3ab315f5af73`
- Earlier overlapping audit branch inspected: `6d4a594f25fe08b6a710f6998e0e91a9fc52c27c`

The candidate was created from current `main` history. Main was not reset, force-pushed, or merged.

## Incorporated

### Critical repairs

The critical branch is the worker-runtime foundation. The unified candidate preserves:

- complete Git candidates and machine-safe paths;
- delivered-snapshot integration baselines;
- stale-acceptance invalidation;
- cleanup preservation for undelivered content;
- read-only enforcement;
- lossless file pagination and exact cursors;
- signal delivery to proven process ownership;
- complete Pro response dispatch;
- cross-process request serialization;
- Hub/Edge projection corrections;
- compatibility `*_legacy.py` layers.

The known pagination response-contract defect was corrected without weakening the existing test: when a requested limit exceeds policy, the response now states that it was capped to the applied byte count.

### Author Mode

The Author branch remains an independent subsystem with its own CLI, service, packages, tasks, leases, revisions, checks, feedback, approval/publication, maintenance, HTTP, and MCP boundaries.

The remote Author implementation remained authoritative because it contains stronger package-path validation, maintenance locking, lease checks, unknown-execution fencing, supervisor-launch handling, JSON-RPC validation, task creation, publication listing, safe exports, and terminal failure codes.

Selected useful behavior from the loose variants was adapted rather than overlaid:

- bounded CLI waits;
- lease-cache symlink rejection;
- explicit approval actor and token role;
- disabled HTTP access logs;
- public qualification output filtering.

The loose `identity.py` executable behavior was equivalent and was not substituted. Loose tests were consolidated into focused regression coverage rather than duplicated.

### Shared policy and documentation

- Manager/worker and Author startup paths are explicit.
- Author Mode does not import or initialize the worker runtime.
- Ordinary worker policy is explicit Luna/High when supported by the installed catalog.
- Terra/Sol are escalations; smaller/legacy models are visible fallbacks, not silent routing.
- Root manager instructions are scoped away from the nested Author contract.
- Installed `patchbay author` and `patchbay-author` entrypoints are documented and tested.

## Deliberately not activated or imported

- The older `ReliableWorkerRuntime` monkey-patch and its older candidate/read mixins were not activated. They would override stronger critical behavior.
- Export-only branches (`chatgpt-source-export-20261018`, `chatgpt/patchbay-fixes-20261019`, and `chatgpt/source-export-20261019`) were rechecked but their temporary export links, triggers, and delivery configuration were not treated as production fixes.
- GitPython PR `#54` is deferred as separate dependency maintenance. It is not required to reconcile the two implementation conversations and needs its own qualification.
- External Desktop task bridge PR `#47` remains independent and outstanding.
- Older dependency PRs remain separate compatibility work.

## Checks executed during reconciliation

The reconciliation environment could not clone the public repository or execute the full installed suite because outbound GitHub DNS was unavailable. No GitHub Actions run was created for the branch commits, so CI is not claimed.

Executed against the authored changed files:

- Python compilation of the reconciled Author CLI and wrapper modules;
- `compileall` over the staged reconciliation source files;
- `compileall` over the staged reconciliation test files;
- source-level comparison of the critical, Author, older audit, loose implementation, and loose test variants;
- branch ancestry, tree, entrypoint, runtime composition, prompt-surface, and evaluator-command inspection through the public repository API.

These checks establish syntactic and reconciliation intent only. They do not qualify the combined repository.

## Checks still required before merge

Run the complete matrix in `TESTING.md`, including:

- full compilation and test suite;
- critical regressions and the 120-byte pagination test;
- all Author tests on Linux/WSL2;
- installed entrypoints and independent Author state;
- deterministic MCP and Hub/Edge evaluators;
- production restart and old-schema rehearsal;
- optional real-Codex worker trial when account-dependent worker execution is authorized.

## Remaining gaps and untouched scope

- Final Author Mode qualification on Linux/WSL2 is incomplete.
- The earlier macOS five-failure result remains attributable to the explicit platform guard unless Linux/WSL2 evidence proves separate defects.
- Complete historical audit backlog closure is outside this consolidation; the critical fixes and selected compatibility work do not claim every audit candidate was resolved.
- Account-dependent natural ChatGPT connector selection, public tunnel, and real worker execution remain environment-specific qualification.
- Separate dependency, Desktop bridge, and older compatibility PRs remain outstanding.
- Any failures found by local qualification must be corrected on this continuation branch before merge.
