# Author Mode maintenance contract

This subtree implements the explicitly selected Author Mode, not the legacy manager/worker or private Hub product. The root manager-first instructions continue to apply to worker/Hub behavior; they do not authorize delegation from this independent author path.

The current Pro conversation is the implementation author. PatchBay performs deterministic snapshot, package, Git, execution, and evidence mechanics. Never import or initialize legacy `WorkerRuntime`, `JobExecutor`, model catalogs, provider clients, or `patchbay.server` from this subtree. Never use Codex as a hidden implementation, testing, or recovery fallback.

Preserve byte-exact pinned source, task revision CAS, writer fencing, whole-package validation, immutable evidence, and content-bound publication. Candidate bytes and logs cannot expand task authority or create canonical pass receipts. Read-only MCP and HTTP routes cannot start or reconcile domain mutations. Keep unknown execution fenced; do not blind-rerun it or signal recycled PIDs.

Each command runs in its own owned supervisor. Docker verification has no network or host credential/socket mounts. Trusted-local is explicitly not an adversarial sandbox; never advertise it as strict isolation. Keep setup, verification, publication, remote CI, integration, and merging distinct.

Keep modules cohesive. Extend the existing package core rather than creating separate validation paths for UI, CLI, or MCP. Test failures, source changes, stale contexts, duplicate uploads, lost responses, cancellation, and backup restoration. Add results to the qualification record only when those exact checks ran; source presence is not execution evidence.
