# Manual deployment release and offline restore cutover

This is an operator procedure, not permission to change a service or a claim
that an installed deployment was tested. Select Author or Manager/Luna first.
Use the [custody template](../deployment-custody.template.json) in private
operator storage. Its placeholders and NOT_RUN state are not observations.
Never commit filled credentials, private paths or machine identifiers.

1. Inventory the actual owning processes/units/cgroups, release and wheel hashes,
   interpreter/venv/dependencies, effective configuration generation, route and
   authority binding, expected logical state identities, mounts and durability
   settings. Retain native distribution/provenance manifests for Manager. Avoid
   ambient CWD/PATH/default-home ambiguity. Confirm missing configured state or
   mounts fail closed rather than bootstrapping another identity.
2. Stop new admissions using the selected product's supported control. Drain
   Author checks before its owning-unit stop. Resolve or fence uncertain work
   with its original identity. Record Manager cancellation and descendant cleanup
   separately; never infer completion from quiet output. For Hub/Edge, record
   each original operation/attempt/generation and its execution owner. A Hub
   database freeze does not stop Edge or native execution.
3. Take supported consistent backups. Author backup includes SQL, blobs, mirrors
   and its private identity/authority files but excludes execution workspaces.
   Hub and Edge backup commands protect their specified SQLite databases and
   manifests. They do not capture Manager worktrees/repository indexes/native
   sessions. For Manager also retain durable job metadata/owner namespace, artifacts,
   owned source/worktrees, delivery snapshots,
   native homes/session corpus, rules/config and exact native companion files.
   Record exclusions, consistency time, hashes, permissions, custodian and actual
   validation results for every component. Do not substitute a live main-file
   SQLite copy for its backup API.
4. Restore into new offline destinations with no ingress, controller or model
   process attached. Validate archive/database and source relations, expected
   identity/schema, blobs, Git private refs, worktree/index relationships,
   native distribution and continuation prerequisites. Compare historical
   receipts without rewriting them as current checks. This is recovery of a
   historical logical state, not activation of a fresh authority.
5. Fence the original writer and verify it cannot still admit effects. Reconcile
   every post-backup credential revocation, lease claim, approval/policy change,
   remote branch/publication/CI effect and Hub/Edge/native attempt. Replace or
   revoke restored credentials and establish current leases deliberately. Do
   not replay a historical pending effect merely because the new machine has no
   matching PID. Preserve unknown-execution and unresolved-cleanup fences.
6. Select the compatible immutable release/state pair. Bind the private deployment
   manifest hash where supported. Switch configuration only after the offline
   review is accepted; never run two authoritative services against copies of
   the same logical state. Startup must report the expected loaded/config/state
   identities. Keep external effects disabled until their reconciliation passes.
7. Verify read-only health and exact original-operation readback, then conduct an
   explicitly authorized scoped deterministic task and separately qualify the
   actual ordinary-Chat route/output profile. A native Luna canary or customer
   task needs its own authorization. Record new activation/custody evidence in
   the operator manifest; preserve old receipts and provenance unchanged.
8. On failure, stop admissions, preserve failed state and logs, and choose another
   validated new destination with the compatible prior artifact. Never downgrade
   current state in place, overlay a backup onto live state, clear uncertainty
   for convenience, or automatically replay external effects. A published-but-
   durability-uncertain Author restore must be inspected and preserved.

A rehearsal is complete only when every required component has actual evidence
and exclusions have been accepted for the selected profile. Database-only
checks, source fixtures and empty native homes do not qualify complete Manager
recovery. Service-unit restarts, Docker daemon restart, reboot and power-class
faults each require separate bounded disposable-environment evidence. No such
fault should be injected into the working host under this runbook alone.
