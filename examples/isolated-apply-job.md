# Isolated Apply Job

Goal: ask ChatGPT to delegate a change to local Codex without editing the original checkout directly.

Workflow:

1. Start with a read-only plan.
2. Review the plan.
3. Call `codex_apply_job`.
4. The server creates an isolated git worktree.
5. Fetch result with `codex_get_result`.
6. Inspect diff with `codex_get_diff`.
7. Run tests manually.
8. Merge manually only after review.

Power-control principle:

> ChatGPT delegates, Codex stages changes, and the maintainer reviews, tests, and merges.
