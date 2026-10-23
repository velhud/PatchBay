# Public PatchBay: Linux qualification attempt and macOS continuation

Date: 23 October 2026 (UTC).

## Result and unfinished assignment

The assignment was to verify actual ChatGPT delivery in both Author and Manager
modes, using GPT-6 Extra High without Pro, including longer coding work and
parallel Luna delegation, review, correction, and acceptance.

That assignment is NOT complete. I did not operate the user's logged-in Chromium
ChatGPT session. I did not demonstrate ordinary ChatGPT calling this repaired
Linux installation, and I did not create or reconnect a working ChatGPT plugin.
Native Codex-client delivery is evidence for its own route, not a substitute for
the requested ChatGPT browser route.

I spent too much time on repository suites and ran a full suite before the useful
end-to-end work was complete. The user corrected this. Do not repeat that
sequence. No further suites were run after that correction. The handoff below
preserves actual successes, failed runs, limitations, and the work needed next.

## Source and publication custody

This is PUBLIC velhud/PatchBay, not private PatchBayHub.

- Repository: https://github.com/velhud/PatchBay.git
- Initial live-trial source: ecd0cbe539fb826e17d5e15815442041e30ae196.
- Product repair already published to main:
  1409fd97245b9133f2f86e7e4ff2abad9b187bfa.
- This handoff branch: codex/patchbay-linux-handoff-20261023.
- The handoff branch contains the product repair in its ancestry plus this
  explanation and the synthetic delivered fixture code. No main history was
  reset, rewritten, merged, or rebased.
- The main repair was verified using actual remote ls-remote, fetch, commit
  equality, and byte readback of all seven changed files.
- GitHub reported a branch-rule bypass for the authorized direct-main push:
  PR and required CI status checks were expected. Local results below are not
  a claim that GitHub CI passed.
- No PR or production deployment was created.

Earlier integrated programme anchors remain in ancestry: S06 60817e37c910ce578b43a7c623a166576787bb69;
S07 77bcc72312f5339bb1bcd033e06f653ad2f3a174;
S08 e1d23d9889edef5c35d9cfead0f501a89dc1b60d;
S09 4eb9ba96c41f66ad1a2a009b63e9fcda95bf3ba3;
S10 39b0b626446a07dcdbbcafd7b8449ef70598d349;
postimplementation ecd0cbe539fb826e17d5e15815442041e30ae196.
Their stage documentation and receipts are historical evidence, not new browser
qualification. Do not reapply old research archives wholesale.

## What I prepared and built

I used separate disposable source repositories and independent Author/Manager
homes. No customer repository or live application home was used.

1. A loopback-only Manager instance from the current public checkout, scoped to
   one synthetic SQLite ledger/report repository, with capacity for three jobs.
2. A primary native Codex MCP client selecting gpt-6-sol with xhigh reasoning.
   Every delegated start and message explicitly selected gpt-6-luna/high.
3. Two Author artifact tasks: a small addition repair and a larger exact-decimal
   CSV reconciliation task. The external primary client authored the packages
   itself; Author performed deterministic import/materialization/checks.
4. Independent protected fixture specifications/tests written before coding.
5. Durable client JSONL, server logs, worker results/state, canonical Author
   receipts, source hashes, publication readback, and installation records.
6. A fresh offline wheel installation from published commit 1409fd9. All 167
   Python payload files matched the source. All declared CLI entrypoints were
   checked and the Author import remained independent of Manager workers.
7. The shared-checkout peer-context and diff repair described below.
8. This portable handoff. The fixture deliverables are under fixtures/ beside
   this document; they contain synthetic source only, with no credentials,
   user data, actual worker Git metadata, or private endpoint URLs.

The native CLI was 0.160.0. A real selected-Luna shell canary ran a command and
returned its expected output. A supplied local installation manifest matched
five coding-related files. That manifest was locally computed, not independently
authenticated upstream provenance; voice assets and nested helpers were excluded.

## Actual Author delivery

### Small task

The primary client read the exact exported context, prepared a separate editable
tree with the shipped builder, changed subtraction to addition in app.py, and
built a package requesting the authorized unit check.

The shipped Author CLI accepted revision 1. Its canonical check completed with
READY_FOR_REVIEW and proven cleanup. The one protected unittest passed.
The original source checkout and protected test were unchanged.

### Larger task

The client implemented reconcile(csv_text) with:

- Exact integer cents rather than floating point.
- Strict decimal syntax and CSV shape validation.
- Quoted commas/quotes and embedded newlines.
- Unicode account names, CRLF and a leading BOM.
- Normalized exact replay, conflict rejection, and sorted account balances.
- A 10,000-row protected case and rejection of malformed records.

It built a package changing only app.py. Eighteen protected tests and compilation
passed; the client additionally reported five valid and nine invalid probes.
Author accepted revision 1 and its canonical check returned READY_FOR_REVIEW
with proven cleanup. Operator probes against the exact accepted Git candidate
passed 100 generated cases containing 25,000 unique rows plus duplicate replays.

The small package apply and larger check were replayed with their original keys:
the same operations/results were returned; no new revision or run was created.

Both accepted revisions were checked again by the fresh published installation.
The final receipt hashes were:

- Small: b6e087b5c3e5be28b2fe018d9fc839b76fac7f442bc524bcd50b371b9597da83.
- Larger: 9fe09cc0f299e947552fe2e6e608f3a0bc1550f0c879464d722701aba490b9c5.

These used trusted-local execution. They do not qualify adversarial Docker
isolation. The canonical checks were process-exit checks; unittest counts came
from the protected program's output, not an independent JUnit acceptance oracle.
No Author worker, provider implementation fallback, publication, or integration
into a real project was used.

## Actual Manager delivery

The primary native client appointed:

- Ledger Implementer: isolated_write, ledger.py only.
- Report Implementer: isolated_write, report.py only.
- Independent Reviewer: read_only source review.
- Independent Verification: initially read_only; later same-session advisory
  follow-ups.
- Verification Executor: serialized shared_write lane, verification only.

The two implementers had 35.019 seconds of overlapping actual execution.
The ledger task covered SQLite persistence, validation, exact replay/conflict
semantics, multiple connections and concurrent duplicate posting.
The report task covered deduplication, conflicts, deterministic totals, Unicode
and input preservation.

Each isolated worker correctly reported that the other component remained a stub.
The source reviewer found that decimal-string conversion failed for very large
valid Python integers. The named Ledger Implementer changed the storage codec
to signed BLOBs, exercised a 20,001-bit value, and the reviewer rechecked the
retained source. This was a defect in generated fixture code, not a claim that
PatchBay's own ledger had such a defect.

The manager inspected retained candidates, recorded target-bound decisions,
obtained fresh preview tokens and integrated ledger.py, then report.py.
The second preview explicitly accepted the first file as existing checkout dirt.
There was no commit, skipped-file shortcut, overwrite of protected source, or
worktree deletion.

The first read-only verifier could not create temporary databases or compiler
caches. It reported four passing tests and seven setup errors; that is not a pass.
The manager then appointed a writable verification-only executor without source
edits. On the integrated files it reported:

- All 11 protected integration tests passed.
- Compilation succeeded.
- A 1,000-row Unicode/persistence/replay/conflict/report probe passed.

Operator execution against the integrated bytes independently passed those
checks plus the large-integer probe. HEAD, SPEC, protected tests and both retained
worktrees remained intact. Only ledger.py and report.py were modified in the
disposable checkout.

Five logical workers and nine native turn records were retained, all terminal.
That does not mean every reported check in every turn succeeded: the read-only
verification attempt above remained an explicitly failed environment attempt.
Final worker views had no active or unknown-cleanup state.

## Product defect found, repaired and published

During final reconciliation, the manager sent detailed context from the
shared-checkout Verification Executor to an existing read-only colleague.
The actual MCP request failed before turn admission with:

    worker integration baseline is unavailable

Detailed peer context treated any writable workspace as an isolated candidate.
A shared-write worker has no isolated delivery baseline and its checkout can
include other actors' changes.

The repair in runtime_views_legacy.py:

- Keeps shared peer reports advisory at changes/diff/review context levels.
- Explicitly states that no isolated candidate or attributable patch exists.
- Does not capture unrelated checkout dirt, invent a baseline, create a review
  target, or imply source acceptance.
- Returns an incomplete shared_checkout_unattributed result for direct shared
  diff inspection instead of the same internal error.
- Preserves isolated-source snapshots, leases, review, integration and cleanup
  rules. No Author or authority boundary was relaxed.

Tool descriptions, README, testing instructions and reference/repair
documentation were updated.

Six start/follow-up regressions failed against the exact pre-repair defining
method, then passed. A seventh direct-diff regression reproduced the same root.
The focused coordination/source-review suite passed 48 tests.

The real native primary client retried the formerly failing request with a new
key. The same named read-only worker/session received the shared report,
completed and cleared cleanup. A direct MCP observation also verified the
explicit incomplete shared-diff result.

After normal restart into the fresh published wheel, the primary client repeated
the same-worker handoff and shared-diff observation. The outer admission and
completed turn are logged as successful, with protected file hashes unchanged.

However, the installed worker report claimed an internal MCP approval denial.
The primary correctly distinguished that claim from the observed successful
outer call. I did not fully reconcile the internal attempted call before handoff.
Do not turn that unresolved inner claim into a pass or conceal it.

## Repository checks actually run

These are regression evidence for the real source repair, not ChatGPT browser
qualification. I ran them too early in the overall workflow.

| Check | Actual final result |
| --- | --- |
| Compile source/scripts/tests | Exit 0 |
| Full repository suite | 1,692 passed, 7 skipped |
| Audit regressions | Exit 0 |
| Dedicated Author suite | 401 passed, 5 skipped |
| MCP evaluator | Exit 0 |
| MCP terminal reconciliation | Exit 0 |
| Hub/Edge evaluator | Exit 0 |
| Hub V2 evaluator | Exit 0 |
| Restart / old-schema rehearsal | Exit 0 |
| Author interface evaluator | Exit 0 |
| Operational requirements evaluator | Expected exit 1; all 52 cases NOT_RUN |
| Dependency check | Exit 0 |
| Wheel build | Exit 0 |
| Diff check | Exit 0 |

The long-duration opt-in and strict Docker switches were not enabled in this
repair matrix. Earlier programme results remain separate historical evidence.
The first root-filesystem matrix was interrupted and superseded after the
direct-diff extension and slow fixture behavior. Its interruption and teardown
error remain in the logs; it is not a passing run. The final matrix used fresh
owned tmpfs fixtures without weakening assertions.

## Failures, mistakes and unresolved limits

- Wrong browser: I opened the in-app browser, saw a signed-out state and asked
  for sign-in instead of using the user's existing Chromium. That was the wrong
  target. The user says Chromium is already logged in. Do not describe this as
  missing user authentication.
- The app browser inventory exposed only MCP Apps and the in-app browser.
  Selecting chrome returned Browser is not available: chrome.
- The VM Playwright browser-tabs tool returned Browser is already in use for
  its persistent profile. I did not use its suggested isolated browser, because
  that would not be the requested logged-in session.
- Read-only host inspection found the existing Chromium/MCP parent and a
  remote-debugging pipe, but I established no usable attachment. I did not kill
  the browser, copy cookies/profile data, or expose a new debugging endpoint.
- Existing Author and Linux-worker ChatGPT trial connectors each failed
  read-only discovery twice with UNAVAILABLE / mcp_network_error. No domain
  mutations occurred through them. An expired tunnel was an initial suspicion;
  expiry itself was not established.
- No new ChatGPT plugin, fresh public tunnel, or ordinary-Chat write canary was
  created. No ordinary-Chat qualification grant was fabricated.
- Primary clients selected GPT-6 Sol/xhigh; workers explicitly selected Luna/high.
  Native selection records support those requests. Provider-observed model
  fields remained null. One worker claimed gpt-6.1-sol in prose. Exact backend
  model/account routing is unverified; selection is not independent attestation.
- Initial native worker starts were denied by the client's approval policy.
  Zero workers/effects were verified, then approval was configured only for the
  specifically authorized disposable start/message/integrate tools.
- Unrelated inherited Upwork MCP startup authentication errors occurred. No
  Upwork tools were invoked for domain work. Workers also encountered unavailable
  or approval-gated ambient tools; do not assume a clean nested-tool route.
- The first read-only executable verification was wrongly assigned a mode that
  could not create temp files. Its failed result was preserved and a separate
  verification executor produced the passing executable result.
- An operator verifier generated two import bytecode files. I removed only those
  known generated files and disabled its bytecode. That explains the transient
  cache discrepancy observed by the manager; it was not an unexplained source
  mutation.
- A primary Author local patch was rejected for duplicate operations targeting
  one file; the author corrected it before packaging. The rejected event remains.
- Early helper mistakes included an output path error, an owned-process restart
  identity assertion, a resume missing the skip-Git flag, and Git-status trimming
  in publication preflight. They were corrected; failed/early artifacts were not
  counted as final evidence. The prepublication wheel is separate from the final
  published-commit wheel.
- I did not demonstrate direct ordinary-Chat Author MCP writes, the requested
  browser-driven longer task, or browser-driven parallel Luna orchestration.
- No strict Docker, native macOS execution, production ingress/deployment,
  physical durability, every operational research case, or universal model
  entitlement is qualified by these runs.
- No Pro mode was requested or used for these native trials. No Pro Request
  automatic retry journal was added; the existing manual/non-idempotent
  limitation remains.

## Continue on the other machine

Use the user's existing logged-in Chromium only. Never use the in-app browser.
Do not repeat these local suites as a substitute for useful browser work.

1. Fetch this handoff branch and read the repair plus this record. Do not redo
   S01-S10 or apply research archives wholesale.
2. Establish control of the existing Chromium session without copying its
   credentials/profile or silently starting a different isolated browser.
3. Observe the actual ChatGPT model/effort UI: GPT-6 Extra High, without Pro.
4. Reconnect a fresh narrowly scoped Linux endpoint/plugin, refreshing actual
   metadata. Existing trial routes are currently unavailable. Do not reuse
   credentials or pretend a server restart alone qualifies a client.
5. Perform real Author coding in that client: exact context, client-authored
   changes, deterministic candidate/check receipt, an actual correction cycle,
   expected final bytes, and no hidden worker fallback.
6. Perform real Manager work: parallel explicit Luna workers, retained candidate
   review, named-worker correction, fresh-preview acceptance and integration,
   expected useful result and complete logs.
7. Reconcile the unresolved inner approval claim and distinguish native selection
   from observed client/provider routing.
8. Run focused regressions only if real changes are made. Reserve any justified
   full suite until all useful work and other verification are complete.
9. Keep Author check execution on Linux/WSL2. Using a Mac browser to reach a Linux
   backend does not qualify native macOS Author execution. Do not remove the
   platform guard.

The disposable installed Linux Manager was left loopback-only, with no workers
running. The private handoff email supplies its VM paths, source and evidence
bundle. No credentials, live browser profile, Author authentication database,
or private capability URL is committed here or included in the portable bundle.

The included synthetic fixture tests are supplied as historical inputs for
inspection; no additional execution was performed merely to prepare this handoff.

