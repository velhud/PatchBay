# Option Comparison Matrix

Scores: 1 is poor, 5 is strong.

| Option | Correctness | Preserves long work | Recovery | Compatibility | Total |
|---|---:|---:|---:|---:|---:|
| A: hard timeout | 1 | 1 | 2 | 3 | 7 |
| B: message heuristic | 1 | 3 | 1 | 3 | 8 |
| C: instructions only | 2 | 5 | 1 | 5 | 13 |
| D: session observer | 5 | 5 | 4 | 4 | 18 |
| E: reconciler only | 3 | 5 | 5 | 4 | 17 |
| F: CLI version dependency | 2 | 4 | 1 | 2 | 9 |

## Decision

Use Option D for live execution and Option E for restart/legacy recovery.
Retain manager patience guidance as operational defense, not as a substitute for
correct lifecycle state.

