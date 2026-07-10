# Affected Surface And Ripple Map

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

## Direct Surfaces

| Surface | Expected change | Risk |
| --- | --- | --- |
| `src/patchbay/hub/protocol.py` | Replace handwritten catalog/dispatch with canonical manifest and semantic result handling | High |
| `src/patchbay/hub/runtime.py` | Replace command-only model with operation, worker, group, lane, ownership, and reconciliation services | High |
| `src/patchbay/hub/store.py` | Introduce transactional versioned durable state and migration | High |
| `src/patchbay/hub/edge.py` | Add version negotiation, concurrent/nonblocking delivery, operation receipts, projection events, and strict preflight | High |
| `src/patchbay/hub/server.py` | Carry stable owner/session identity, result waiting, visibility, and recovery endpoints | High |
| `src/patchbay/workers/tool_surface.py` | Become canonical source for routed worker schemas and output contracts | Medium |
| `src/patchbay/tools/handler.py` | Preserve canonical execution; add only narrow adapter hooks if required | Medium |
| Pro Request tool/runtime modules | Add machine-qualified routed references without collapsing respond/dispatch | Medium |
| Workspace context/tool descriptors | Add logical workspace and routed manager-inspection envelopes | Medium |

## Adjacent Surfaces

| Surface | Relationship | Risk |
| --- | --- | --- |
| Hub CLI enrollment/start/status commands | State-store and capability-version consumers | Medium |
| Configuration schema and examples | New state path, retention, operation timeout, protocol version settings | Medium |
| Existing Hub state files | Require migration or explicit legacy import | High |
| Existing enrolled Edges | Require capability handshake and staged compatibility | High |
| Deployed Hub/Edge services | Must not be upgraded partially without version checks | High |
| `tests/test_hub_protocol.py` | Public manifest, validation, annotations, semantic results | High |
| `tests/test_hub_runtime.py` | Lifecycle, operation, ownership, routing, close/reassign semantics | High |
| Worker coordination/integration tests | Parity and regression authority | High |
| `scripts/live_hub_edge_eval.py` | Retain as cheap smoke, no longer release evidence | Medium |
| New real Hub lifecycle evaluator | Required release evidence | High |
| README/Quickstart/ChatGPT instructions | Must describe target only after implementation | Medium |
| Runtime evidence/logging docs | New operation and projection records | Medium |

## Indirect Or Unknown Surfaces

| Surface | Why affected | Required follow-up |
| --- | --- | --- |
| ChatGPT connector manifest caching | Stable catalog must remain visible after deployment | Real connector refresh evaluation |
| Multi-conversation ownership | Stable owner plus participant sessions changes visibility | Multi-client MCP tests and real ChatGPT trials |
| Long worker briefs and report payloads | Transient delivery/TTL must not truncate needed context | Payload-size and retention tests |
| Concurrent manager calls | Transactional state and operation waits must avoid lock starvation | Load and race tests |
| Edge restart during Codex work | Projection reconciliation must distinguish alive, lost, and unknown | Process/heartbeat recovery tests |
| Repository changes during worker work | Integration preview tokens and base revision checks | Existing integration tests plus new Hub route |
| Artifact inbox after successor reassignment | Artifacts are machine-affine | Explicit restaging test and docs |

## Regression Classes

- Behavioral: wrong tool chosen or misleading outcome.
- Data: state migration loses machine/group/worker history.
- Lifecycle: duplicate operations, false completion, stranded workers.
- Contract: stale ChatGPT manifests or schema incompatibility.
- Security/visibility: one owner reads another group's private result.
- Performance: status waits block Edge delivery or Hub locks.
- Observability: operation uncertainty cannot be diagnosed.
- Test: synthetic success continues to pass while product workflow fails.
- Documentation: V1 and V2 claims become mixed.

## Cross-Solution Conflict Flags

The following changes must be designed together, not patched independently:

- canonical tool registry and Edge capability negotiation;
- transactional store and operation reconciliation;
- stable owner identity and result visibility;
- worker projection and group close semantics;
- logical workspace identity and successor reassignment;
- status/wait behavior and Edge concurrency;
- payload retention and report/file/inbox delivery;
- tool schemas/annotations and ChatGPT instructions;
- live evaluator and release claims.

Parallel implementation is safe only after interfaces are frozen and write
ownership is split by module. State schema, operation protocol, and public tool
manifest are sequencing dependencies for most later work.
