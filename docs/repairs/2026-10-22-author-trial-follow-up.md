# Author trial follow-up

The read-only trial assessment found two actionable contract defects. Its
successful reads and integrity checks remain valid observations of that trial;
they do not qualify native ChatGPT writes or strict-container execution.

## Changes

Recovery now exposes `candidate.source_context_id` and `source_context` for the
context against which the candidate was authored. The old `candidate.context_id`
is retained as a **deprecated provenance alias**, with that meaning in the MCP
output schema and instructions. It is not a current-source selector.

Use `candidate.candidate_context_id` for reading the current implementation.
`candidate.candidate_context` identifies its revision and `commit_oid`; both must
match the candidate. These fields are null until a context for that exact
revision and commit has been exported. Recovery never creates an export or
changes the old context. This works with existing stored revisions without a
database migration. Historical contexts remain immutable.

When a current canonical receipt passes and publication is disabled, recovery
returns `awaiting_review`. Feedback, Markdown and continuation exports request
review of the implementation and receipt. Review can be recorded externally;
there is no new persisted acceptance action or implicit publication approval.
Publication-enabled tasks retain `awaiting_approval`. Pending inbox approvals,
revoked grants, failed checks and uncertain execution retain their existing
authority/fencing behavior. Passing checks never authorize merging.

Successful feedback exports no longer instruct the author to implement another
correction. The capabilities schema clarifies that `strict_backend` describes a
supported backend; the receipt environment identifies what actually ran.

## Deployment handoff

The running connector, its state, authorization, qualification record, routes
and browser are unchanged by this repair. The owner can deploy this revision
using the existing service procedure:

1. Preserve the existing independent Author home and immutable receipts. Deploy
   the updated source/package and restart only the intended Author service.
   No schema migration, task recreation, token rotation or fixture reset is
   needed for these changes.
2. Refresh the client tool catalog. Runtime/catalog changes deliberately
   invalidate old native-write qualifications. Keep the read-only connector
   read-only; do not turn on writes merely to remove a qualification warning.
3. Recover the existing task. Verify the source context still reads the old
   source and the candidate context reads the repaired commit. If the latter is
   null, an authorized writer must export the exact current revision first.
4. For a passing nonpublishing task, verify `awaiting_review`, policy-aware
   feedback and continued `POLICY_DENIED` for publication preview. A reader
   must not acquire a lease, create exports or start checks.
5. For native writes, use a separate disposable task and the actual intended
   ChatGPT client. Qualify that client/runtime/catalog/output-profile binding
   through the existing operator qualification process. Exercise draft edits,
   stale versions, seal, lost-response replay, fresh failed checks, correction,
   fresh passing checks and recovery in a new conversation. Retain real
   request/response and executor evidence; synthetic test qualification records
   cannot enable a production client.

The fresh repository regression
`tests/authoring/test_trial_report_contracts.py` covers source-context separation,
read-only recovery, both publication policies, stale-policy invalidation,
successful feedback exports and a Linux MCP repair/check/recovery loop. Run it
alongside the existing Author concurrency, crash, cancellation, restart,
backup and process-ownership tests. Set `PATCHBAY_AUTHOR_LONG_TESTS=1` to include
the real 185-second execution test. This remains a bounded development fixture,
not an hour-long, many-project capacity result or ordinary-Chat qualification.

## Remaining deployment qualification

- Strict Docker: use the pinned-image/offline procedure in
  [qualification.md](../authoring/qualification.md). Record daemon/image
  availability, image digest, actual receipt backend, network and mount
  inspection, cancellation and cleanup evidence. A trusted-local pass or a
  simulated Docker lifecycle test cannot substitute for this.
- Native ChatGPT: perform the client round trip above after deployment. The
  existing read-only trial intentionally exposes no write/check actions.
- Long/concurrent capacity: choose the real target duration, repository sizes
  and project count, then retain restart/interruption and admission evidence.
  The bounded concurrent-writer and long-check regressions do not prove an
  arbitrary production workload.
- Account quota: Author performs no inference delegation and reports account
  usage visibility as unknown. Measure account usage externally for a bounded
  trial if a savings claim is needed. No model reasoning recovery or automatic
  continuation is promised; only persisted work is recoverable.

These are explicit qualification boundaries, not features to enable by
weakening policy, isolation, client qualification or execution fencing.

## Verification of this repair

- macOS full suite: 1,272 passed, 40 skipped, one failure in the unchanged
  manager cleanup timing test (0.86 seconds versus its 0.5-second assertion).
  That exact test and its adjacent recovery test passed on an unchanged,
  focused rerun. No timing bound or production cleanup code was changed.
  All 239 Author cases were successful or explicitly skipped (203/36).
- Linux Author suite with long checks enabled: 233 passed, four skipped, two
  preparation failures because the disposable source copy initially lacked
  Git metadata. After supplying baseline Git history, all three preparation
  tests passed. The new four-case trial regression file also passed separately.
  The real long-check test passed and took over 185 seconds.
- Required audit regressions: ten passed. MCP evaluation: 27 checks passed;
  Hub/Edge evaluation passed; Hub V2 evaluation: 32 checks passed. Compileall,
  dependency consistency and diff whitespace checks passed.
- An offline, dependency-free wheel build succeeded. Installing it into a
  disposable target produced the same Author catalog/runtime identity as the
  source, without importing the manager worker runtime.

These records include the initial failures and their focused reruns; they do
not describe either initial full-suite command as a clean pass. The failures
above required no product edits beyond the trial contract fixes.
