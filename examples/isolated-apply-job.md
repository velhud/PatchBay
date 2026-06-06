# Isolated Apply Job

Goal: ask Codex to stage a change without editing the original checkout directly.

Workflow:

1. Start with a read-only plan.
2. Review the plan.
3. Call `codex_apply_job`.
4. The server creates an isolated git worktree.
5. Fetch result with `codex_get_result`.
6. Inspect diff with `codex_get_diff`.
7. Run tests manually.
8. Merge manually only after review.

Safe principle:

> Codex proposes and stages changes; the maintainer reviews, tests, and merges.
