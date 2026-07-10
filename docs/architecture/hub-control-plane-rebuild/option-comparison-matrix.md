# Option Comparison Matrix

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

| Criterion | O1: add fields | O2: generated schemas | O3: control-plane rebuild | O4: hide groups | O5: prompt only |
| --- | --- | --- | --- | --- | --- |
| Root-cause coverage | Low | Medium-low | High | Low | None |
| Worker capability parity | Medium | High at schema level | High end to end | Medium | None |
| Manager experience | Misleading | Still command-oriented | Natural worker lifecycle | Simple but opaque | Fragile |
| Source-of-truth alignment | Low | Medium | High | Low | None |
| Lifecycle consistency | Low | Low | High | Low | None |
| Retry/idempotency safety | Low | Low | High | Low | None |
| Cross-session continuity | Low | Low | High | Medium-low | None |
| Reassignment correctness | Low | Low | High | Hidden, unresolved | None |
| Backward compatibility | Superficially high | Medium-high | Requires migration/versioning | Medium | High but broken |
| Implementation size | Small | Medium | Large but bounded | Medium | Small |
| Testability | Low | Medium | High | Medium | Low |
| Long-term maintainability | Low | Medium | High | Medium-low | Low |
| Risk of another illusion | Very high | High | Low after acceptance gates | High | Certain |

## Selection Reasoning

Selected: O3 with one UX principle from O4.

O3 is the only option that repairs the public contract, operation transport,
state ownership, group lifecycle, retry behavior, and verification model
together. O4 contributes the principle that routing and preflight should not
create unnecessary manager steps, but groups remain explicit durable objects.

The larger implementation surface is accepted because a smaller change would
leave the exact root causes that made repeated prior fixes ineffective.

Decision status: `SELECTED_WITH_STAGING`
