# Solution-To-Conflict-Review Handoff

## Proposed Solution

Add exact-session terminal observation, atomic terminal transitions, bounded
post-completion wrapper cleanup, and restart reconciliation. Keep Hub as a thin
consumer of Edge truth.

## Required Adversarial Questions

1. Can Codex write `task_complete` before all repository mutations are durable?
2. Can a resumed turn append another terminal event to the same session, and
   how is the current job/turn boundary distinguished?
3. Can session IDs collide, be reused, or resolve to more than one file?
4. Can JSONL rotation, delayed flush, partial writes, or filesystem latency
   break incremental observation?
5. Can wrapper termination leave child processes mutating the worktree?
6. Can result capture deadlock because stdout/stderr pipes remain open in child
   processes?
7. Can cancellation and terminal observation both persist conflicting states?
8. Can restart reconciliation expose or attach another worker's final message?
9. Can new diagnostic fields leak local paths, prompts, or session content?
10. Can Codex CLI format changes silently disable the observer?
11. Does completing before process exit alter repo lock or integration safety?
12. Does any change accidentally reintroduce a total worker timeout?

## Blocking Gates Before Implementation Completion

- Resolve current-turn versus prior-turn terminal event identity for resumed
  sessions.
- Prove process-tree cleanup for write workers.
- Prove terminal state compare-and-set under cancellation races.
- Prove safe fallback when the session observer is unavailable.
- Pass an outside-in Hub run where the wrapper deliberately lingers after a
  terminal session event.

## Handoff State

The architecture has been implemented and passed local conflict-sensitive
tests, full regression, and outside-in MCP/Hub evaluation. It is not evidence
of deployment: the working tree remains uncommitted and no machine was updated.
