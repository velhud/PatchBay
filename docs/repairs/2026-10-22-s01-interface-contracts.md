# S01 interface-contract repairs

The S01 review examined public PatchBay at
`992e1da8306d1d8b4dace113f32bfb45cc2fa92f`. Its eight findings were a mixture of
confirmed interface defects, conditional projection risks, an annotation
experiment and missing live qualification. They were not eight reproduced
production failures. This change implements the supported repository corrections
while preserving Author and Manager/Luna authority and existing durable state.

| Finding | Repository disposition |
| --- | --- |
| F001 | Hub guidance distinguishes a complete canonical capture from scoped, role-filtered and host-loaded observations; the 31-tool catalog remains intact. |
| F002 | `ReadFile` publishes its two selector alternatives and bounded safe field/constraint errors; strict types, paths and exact-version domain checks remain. |
| F003 | Local, Hub V2/V1 and Author initialization select an implemented supported version; local/Hub POST headers respect session negotiation and Author validates stateless supported headers. |
| F004 | Compact Manager text independently retains bounded outcome, identity, candidate, coverage, cleanup and monitoring fields. Full structured evidence and pages remain intact; actual client channel access remains unqualified. |
| F005 | Read-only Author guidance uses existing source, feedback, logs, operations and artifacts. New exports remain authorized writes. |
| F006 | Invalid/oversized result errors distinguish a possible admitted effect from a read fault, preserve trusted recovery identity and caller keys, and return existing read-only actions. No second journal or success state is introduced. |
| F007 | A complete Author effect table and deterministic annotation/permission coverage were added. Existing hints are retained because no native-client benefit has been measured. |
| F008 | The complete immutable audited tree inventory and generated catalog profiles were captured privately. Inventory does not mean complete content review. Native Pro/deployment/profile qualification remains pending. |

The defining behavior and complete effect table are in the
[MCP interface contract](../reference/mcp-interface-contract.md). Input errors
never echo raw submitted values or arbitrary Pydantic input/context. Author
result faults ignore identities in the malformed result and consult existing
authorized state. Hub accepts a result's operation identity only after matching
durable principal, tool, original key and any requested group association.
Compatibility size-error identity fields remain, restricted to trusted readback.

The shared version helper has no runtime imports. Author qualification includes
its source bytes; its PR workflow path filter includes that dependency too.
Optional protocol capabilities are unchanged. The Author compatibility output
profile remains the default, and role/qualification gates are unchanged.

## Verification

The final complete suite passed **1,270 tests with 39 skips and zero failures**.
Compilation, critical repair regressions, MCP evaluation (27 checks), terminal
reconciliation evaluation (38 checks), Hub/Edge evaluation, Hub V2 evaluation
(32 checks), and production-entrypoint restart/old-schema rehearsal (20 checks)
passed. Dependency consistency passed. The focused contract set passed 34 tests.
The separate Author suite passed 200 with 35 skips. A
no-index wheel build and isolated installation matched the source Author
catalog/runtime identity and imported Author without the worker runtime.
Project dependencies and manifests were unchanged.

Independent JSON Schema validation agreed with the project Pydantic 2.9.2
runtime for 250 generated selector/type combinations; a supplementary
253-case environment also aligned. JSON Schema's mathematical integer type
cannot express a lexical difference between `1` and `1.0`; strict runtime
integers remain enforced. Path safety and whether a version exists remain
domain checks. These are finite contract checks, not measured model behavior.

Disposable fault tests admit an Author task or Hub operation, corrupt the
returned output, recover through the original state, drop a successful response,
and replay the original key without another effect. Read faults are separately
classified. Unicode, long lines, exact source paging, stale draft versions,
role denials and oversized envelopes are covered. The original 2,000-character
size-error regression remains unchanged.

The first complete run retained three response-budget failures. Their fixes
preserved compatibility fields and corrected a fixture that had not exceeded
the selected summary profile's budget. The final complete run and remaining
required evaluator results are recorded separately; intermediate failures are
not erased or presented as final evidence.

A subsequent complete run exposed an existing wrapper-exit fixture race:
completion remained correct, while post-terminal cleanup produced exit 143
before the fixture's intended natural exit 7. The fixture now establishes
natural wrapper exit before consuming its terminal events and retains the exact
exit-code assertion and deadline. Three focused terminal/cleanup/parser cases
pass. Production execution and cleanup code are unchanged; the complete suite
passed after this required-check repair. The requirements evaluator separately
reports `INCOMPLETE`: 52 `NOT_RUN`, zero failed cases and no evidence errors.
No absent native/deployment evidence is converted into a pass.

## Remaining qualification

No active trial route, deployment, client metadata or Pro conversation was
changed. No paid/model worker or helper agent was launched. Synthetic fixture
qualification and deterministic local evaluators do not establish native Pro
behavior, Linux/WSL2 execution, offline Docker, persistent ingress, reboot,
power-loss durability or macOS worker cleanup. Those gates remain explicit.

Upgrading Author source, schemas or instructions invalidates old qualification.
Capture the actual source/build, complete catalog, approved profile, role,
instructions, negotiated version, output profile and model/client before the
next authorized trial. Exercise discovery, a permitted action, a denied action,
complete source/log readback, selected-plugin follow-up, lost response and
fresh-chat recovery. Use the existing qualification mechanism only after an
observed write. Keep canonical and scoped trial results separate; retain the
original annotation hints unless a controlled client experiment demonstrates
benefit. See [TESTING.md](../../TESTING.md) and the
[qualification guide](../authoring/qualification.md).

S02 should reconcile the new response-fault contract with its durable-effect
analysis. S08 retains broader HTTP-verb, proxy, restart and deployment
qualification. S10 retains compound native-client release evidence. No later
audit package is implemented by this S01 change.
