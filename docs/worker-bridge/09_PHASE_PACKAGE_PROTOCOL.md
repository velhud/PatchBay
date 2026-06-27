# Phase Package Protocol

Status: Phase 0 plan for future archive integration.

## Purpose

Future implementation archives should be integrated phase by phase into this repository, then verified locally. This protocol prevents each archive from becoming an ambiguous pile of files.

## Required Archive Shape

Future implementation archives should use:

```text
phase-N-name/
  README_PHASE_N.md
  PATCH_MANIFEST.md
  VERIFICATION.md
  KNOWN_GAPS.md
  repo/
    <files at real repository-relative paths>
```

Optional:

```text
MIGRATION_NOTES.md
REVIEW_NOTES.md
fixtures/
```

Phase 0 is an exception: it is architecture documentation and has no `repo/` implementation payload.

## Required Files

`README_PHASE_N.md` must state:

- phase goal;
- user-visible capability added;
- architecture decisions implemented;
- intentionally deferred behavior;
- required integration order;
- expected tests.

`PATCH_MANIFEST.md` must list every file in `repo/`:

| Path | Action | Reason | Public contract affected | Tests |
| --- | --- | --- | --- | --- |

`VERIFICATION.md` must include:

- commands to run;
- expected result;
- real-Codex or ChatGPT manual scenario when relevant;
- rollback instruction.

`KNOWN_GAPS.md` must include:

- deferred behavior;
- assumptions requiring local verification;
- compatibility uncertainty;
- unverified runtime claims;
- exact next-phase dependency.

## Local Integration Algorithm

For each future archive:

1. Read `AGENTS.md` and the phase README/manifest/verification/gaps.
2. Confirm current branch, commit, and dirty state.
3. Do not overwrite unrelated local work.
4. Compare every supplied replacement file with the current file before applying.
5. Adapt only where the repository has legitimately moved since the package baseline.
6. Preserve phase behavior and architecture; do not redesign silently.
7. Add or update supplied files at repository-relative paths.
8. Run compileall, targeted tests, full tests, and live MCP eval.
9. Run real Codex/ChatGPT checks when the local environment permits.
10. Fix integration defects caused by repository drift without broadening scope.
11. Report changed files, exact commands/results, unresolved gaps, and concise diff summary.

## Baseline Mismatch

If the repository has changed since a phase package baseline:

- inspect the differences;
- adapt the patch semantically;
- preserve current security/auth/tool metadata improvements;
- do not blindly replace newer files;
- report every material adaptation.

If the mismatch changes worker architecture itself, stop and report the conflict.

## Acceptance Report Format

```markdown
# Phase N Integration Result

## Status
COMPLETE_VERIFIED | COMPLETE_UNVERIFIED | PARTIAL | BLOCKED

## Integrated
- ...

## Adaptations From Supplied Package
- ...

## Verification
- command: result

## Real Runtime Checks
- ...

## Not Verified
- ...

## Remaining Risks
- ...

## Suggested Next Phase
- ...
```
