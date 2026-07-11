# Codex Terminal State Reconciliation

Status: `LOCALLY_IMPLEMENTED_AND_VERIFIED_NOT_COMMITTED_OR_DEPLOYED`

Design ID: `CODEX-TERMINAL-STATE-RECONCILIATION-V1`

This pack defines the planned repair for a lifecycle mismatch observed in a
real multi-worker Hub run: Codex had produced final answers and recorded
`task_complete` in its own session JSONL, but the CLI wrapper processes remained
alive. PatchBay continued to report those workers as running and eventually
stale because it currently waits for subprocess exit before finalizing a job.

The selected design makes semantic Codex completion authoritative while keeping
process exit as transport cleanup evidence. It does not add a worker-duration
timeout, reduce concurrency, or change the Hub manager tool surface.

The design is now implemented in the local working tree and verified through
unit, integration, direct-MCP, Hub compatibility, Hub V2, and a dedicated
public-MCP lingering-wrapper scenario. The cross-project production follow-up
is locally implemented and verified; release and deployment status is recorded
in the production reliability report.

## Reading Order

1. [Intake checklist](solution-design-intake-checklist.md)
2. [Root-cause evidence](root-cause-evidence-brief.md)
3. [Purpose and invariants](app-purpose-and-invariant-map.md)
4. [Affected surfaces](affected-surface-and-ripple-map.md)
5. [Solution options](solution-options-register.md)
6. [Option comparison](option-comparison-matrix.md)
7. [Selected design](selected-solution-design.md)
8. [Implementation and verification](implementation-verification-plan.md)
9. [Decision record](design-decision-record.md)
10. [Conflict-review handoff](solution-to-conflict-review-handoff.md)
11. [Additional runtime findings](additional-runtime-findings.md)
12. [RetailMind continuation fixes](retailmind-continuation-fixes.md)
13. [Cross-project production reliability release](cross-project-production-reliability-release.md)
