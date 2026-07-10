# Dependency And Sequencing Plan

1. Freeze resolved contracts and ordered tool registry.
2. Implement principal, conversation, machine-generation, and logical workspace identities.
3. Implement SQLite schema, migration, and legacy classification.
4. Implement V2 Edge capability handshake and claim fence.
5. Implement operation/attempt state machine, payload store, and event waits.
6. Implement Edge journal/outbox and independent scheduler.
7. Implement full worker projection snapshots and immutable worker refs.
8. Restore read-only worker and workspace surfaces.
9. Restore mutating worker/inbox/batch operations with action-specific reconciliation.
10. Implement preview-token integration and safe stop/cleanup.
11. Implement authoritative group close and successor reassignment.
12. Route Pro Requests.
13. Run migration, failure, multi-session, two-Edge, and real ChatGPT acceptance.
14. Update public docs and perform atomic catalog/deployment cutover.

No step may publish a partial V2 catalog. Safe internal parallelism begins only
after the interfaces owned by prior steps are frozen.
