# S07 evidence, acceptance and authority

S07 follows published S06 `60817e37c910ce578b43a7c623a166576787bb69`.
The nine conditional findings and 66 proposed oracles were researched against
older S05 source. They are not nine installed incidents or 66 passing tests.
Current S06 custody, review, delivery, group and mode boundaries are retained.

| Finding / request | Current disposition |
| --- | --- |
| F01 / CR01 | JUnit parsing validates optional suite subtree totals against leaf cases, without counting ancestors twice. Unsupported outcome shapes, contradictory counts and multiple outcomes fail closed. The XML parser rejects DTDs through its declaration callback for supported encodings, including UTF-16; bounded raw reports retain their digest even on parse failure. |
| F02 / CR02 | `Check.min_executed` is an additive opt-in minimum, default zero. Collected and reported executed counts remain distinct; executed means testcase count minus skipped, not independently witnessed assertions. Allowed all-skipped reports explicitly say `explicitly_allowed_no_execution`. Exit checks retain command-success semantics. |
| F03 / CR03 | New version-2 receipts track source observation as not_started/incomplete/complete, with initial and command-boundary identities. Missing/failed observation cannot set source_unchanged true. Partial negative evidence and the first mutation remain retained; later commands cannot erase it. |
| F04 / CR04 | Executor facts, candidate-program assertions and worker-authored reports have separate provenance. No independent acceptance is inferred. Existing blocked paths and pinned image harnesses remain the opt-in means to protect reference tests; no universal harness or new judge is introduced. |
| F05 / CR05 | CI keeps status/check namespaces and producer identities. Legacy required names require all matching producers and every returned check attempt. Status history is reduced only within one producer/context; missing, pending or malformed observations cannot become green. Explicit app-selector policy is deferred: all-source evaluation is conservative and sufficient for the current name-only API. |
| F06 / CR06 | Preview and fresh apply check downstream-effect consent independently of observer metadata. Default `remote_effects=downstream_possible` requires `allow_external_ci=true`. Explicit `controlled_no_automation` is an operator assertion about its destination, never inferred from a local path or URL. Existing uncertain pushes are read back at their original address without new push or CI. Revocation and current candidate/policy are rechecked at durable push intent. |
| F07 / CR07 | Program failures, testcase names and log bodies use dynamically sized literal fences. Structured feedback labels origin and keeps canonical next actions separate. This improves framing; it does not qualify a model's resistance to hostile instructions. |
| F08 / CR08 | Mutable workspace and retained candidate file views label transformations, representation and returned-text SHA separately from raw file/blob identity. Existing redaction remains; exact protected source is explicitly unavailable. Mutable file hashing is a separate read, not a coherent snapshot claim. |
| F09 / CR09 | Canonical admission rederives state/verdict and required checks from the admitted profile, rejects duplicate/unexpected IDs and command/exit/count contradictions, and checks complete source and environment binding for passes. Fresh and recovered pending evidence share this predicate. No rerun or model call occurs. |

## Evidence and compatibility

The accepted JUnit dialect is unnamespaced testsuite/testsuites containing
nested suites and testcase elements. Optional tests/failures/errors/skipped
attributes must exactly equal descendant outcomes. Each testcase has at most
one failure, error or skipped outcome. Properties and system-out/system-err are
informational and cannot hide outcome elements. Suite-level errors and unknown
outcome elements are incomplete evidence. Nesting is bounded to 100 levels.
Nonzero process exit remains failed even when report parsing fails. Missing,
malformed, stale, symlinked and oversized reports retain their existing guards.

New fields participate in newly admitted policy/profile digests. Existing
stored policy and run identities are not rewritten. Old terminal receipts remain
readable with their historical meaning. Pending version-1 evidence cannot gain
a version-2 passing source-observation guarantee: preserve it for operator
inspection rather than relaunching it. Already committed terminal results win
before new semantic admission; cancellation still linearizes at its existing
intent transaction. A cancellation committed before canonical persistence
prevents a ready outcome. Runtime/source changes invalidate prior client write
qualification through the existing runtime binding.

`READY_FOR_REVIEW` means the configured checks met their recorded rules. It
never supplies semantic acceptance, independent test adequacy, human approval,
publication, merge or deployment. A report can be fresh and consistent while
its candidate-edited generator tests nothing useful. For independent acceptance,
protect the reference harness and discovery/configuration dependencies using
operator blocked paths or a prepared immutable image; supply candidate source
explicitly and review verification-code changes. Pin the baseline only when
claiming reproduction or regression attribution. A hash is identity, not proof
of adequacy. Manager `tests_run` and `evidence` remain worker assertions; native
completion proves lifecycle progress. S06 controller/combined-tree attestations
remain separately labelled and are not canonical execution receipts.

Source equality means only complete comparisons at recorded boundaries. A
command that changes then restores a file can have equal endpoints. No continuous
immutability, adversarial trusted-local sandbox or arbitrary dependency identity
is claimed. Strict Docker containment requires its own available immutable image
and actual execution; a parser or mocked lifecycle fixture does not qualify it.

## Remote effects and recovery

Publication stays disabled by default. Enabling it does not silently authorize
remote hooks. An operator migrating an enabled policy must either explicitly
allow downstream effects or attest a controlled no-automation destination after
checking its server-side configuration. The flag cannot suppress arbitrary hooks.
GitHub observer presence makes no difference to this admission rule. Refusal
occurs before push intent. A push already admitted at that durable boundary is
a historical effect; later revocation cannot recall it. Recovery reads the
retained remote/ref and requests no new CI. Confirmed push, CI observations,
merge and deployment remain distinct. This path creates no PR.

## Verification and remaining gates

Run the full matrix in TESTING.md, the S07 parser/CI/source/semantic-admission/
feedback/source-view regressions, and all S01–S06 retained regressions. Tests use
actual disposable Git/SQLite/Author supervisors and no-model Manager executors,
plus explicitly controlled parser/adapter/race fixtures. Synthetic canonical
receipt inputs are labelled persistence fixtures and contain consistent command,
source-boundary and environment fields; they are not backend qualification.
The external implementation receipt records actual results and acceptance-ID
mapping, including failed baseline probes.

Linux revalidation also corrected three verification fixtures. The endpoint
continuity client honors bounded `429 Retry-After` using the original keyed
request, preserving S03's urgent burst limits. The MCP evaluator spools server
output rather than leaving a startup-only pipe undrained; a captured stack
showed that pipe filling and blocking the event loop in logging. Its diagnostic
read stays bounded. The cleanup-timeout test injects its timeout boundary
directly instead of relying on a five-second sleep racing host process scans;
its lock-retention, async-reap assertions and outer deadline remain intact.
Production process cleanup budgets and ownership gates are unchanged.

No ordinary Chat/Pro, provider, customer, account, installed service, deployment,
remote CI, native macOS cleanup, production capacity, reboot or power-loss gate
is qualified by these repository checks. The separate 52-case operational
register remains NOT_RUN without per-case observed evidence. Linux execution
cannot qualify Darwin behavior. Service owners must install the published build
through their authorized procedure, preserve state, refresh metadata and qualify
the exact runtime/catalog/role/route/output profile before relying on live writes.
