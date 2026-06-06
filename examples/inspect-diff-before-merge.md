# Inspect Diff Before Merge

Never merge generated changes without inspecting the diff.

Checklist:

- Are files limited to the intended repository?
- Are secrets, local paths, or credentials absent?
- Are generated changes small enough to review?
- Do tests cover the changed behavior?
- Are mutating actions visible in the result?
- Did the job use a worktree rather than modifying the original branch directly?
