# Cross-Solution Conflict Register

| ID | Severity | Conflict | Resolution |
| --- | --- | --- | --- |
| CR-01 | S5 | Ownership was presented as authorization | Single persisted operator principal; private is coordination visibility |
| CR-02 | S4 | Status vocabulary conflicted | Canonical envelope and blocked normalization |
| CR-03 | S4 | Generated retry keys could be lost | Mutations require caller-stable idempotency keys |
| CR-04 | S4 | Unknown outcome looked terminal | Nonterminal reconciliation state |
| CR-05 | S4 | Generic receipts could not prove side effects | Action-specific durable correlation |
| CR-06 | S4 | Identity followed storage/broker in sequence | Identity and generation interfaces move first |
| CR-07 | S4 | Steering was promised but unavailable | V2 is explicitly next-turn-only |
| CR-08 | S4 | Worker state axes were flattened | Separate worker, turn, liveness, integration, review axes |
| CR-09 | S4 | Reassign changed the same group pin | Immutable predecessor plus successor group |
| CR-10 | S4 | Closed group conflicted with running workers | Freeze group decision fields; allow attached reconciliation |
| CR-11 | S4 | Integration crash window was unresolved | Preview token, fingerprints, apply/reverse reconciliation |
| CR-12 | S3 | Edge loop serialized heartbeat and commands | Independent scheduler tasks and target locks |
| CR-13 | S3 | JSON store could lose concurrent updates | Multi-process SQLite/WAL/CAS migration |
| CR-14 | S3 | Synthetic eval overstated readiness | Real EdgeRunner/ToolHandler/WorkerRuntime acceptance |

Normative details live in `../resolved-contract-addendum.md`.
