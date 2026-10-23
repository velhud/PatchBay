# Local source release verification — 23 October 2026

This source update was reconciled onto public baseline `1409fd97245b9133f2f86e7e4ff2abad9b187bfa` before
validation. The source retains the newer public repairs and the local context,
process-identity, cleanup and detached-allocation changes. It is a source
publication, not a deployment or blanket platform/client qualification.

| Check | Actual result |
| --- | --- |
| Complete combined suite | 1660 passed, 63 skipped, zero failures |
| Separate Author subset | 347 passed, 59 skipped, zero failures |
| Critical audit regressions | 10 passed |
| Compilation and dependency consistency | Passed; dependency manifests unchanged |
| Real HTTP MCP evaluator | 29 checks passed |
| Hub V2 evaluator | 32 checks passed |
| Hub/Edge evaluator | Passed |
| Author interface evaluator | Completed; profiles are synthetic, actual A/B outcomes remain NOT_RUN |
| Real native worker on combined source | One Luna/high worker, 9 MCP calls, saved bytes and actual command output verified |
| Native lifecycle | Automatic supervisor cleanup proof; zero cleanup blockers; detached allocation left base refs unchanged |
| Native restart/reconnect | Same retained worker and exact saved bytes recovered through MCP after restarting only the task-owned probe service |
| Wheel | Built with declared isolated build dependencies; extracted new modules match source |

The actual native probe selected `codex-cli 0.163.0-alpha.11.2` with explicit
`gpt-6-luna` / `high`. Provider-observed model metadata is null; request/CLI
selection is not independent provider provenance. No formal client/native
qualification flag was set. The test used a separate authenticated loopback
service and disposable fixture; existing browser trial services were unchanged.

All 357 Python files remained byte-identical throughout final
validation. Their sorted relative-path/SHA256 manifest digest is
`a6508585d970a8a867d78db7cc7d21c82beba5fcc2a8cea4cbdc6bc282eaae72`. This establishes tested-source identity, not functional
correctness by itself. See the [machine-readable result](local-source-release-2026-10-23.json).

The first combined Mac run had five failures from newer Author tests missing
Linux/WSL2 execution-admission markers. They received the correct
BLOCKED_ENVIRONMENT error. Explicit platform markers fixed the test contract;
no product guard was weakened. The clean full run above followed that change.
An initial no-build-isolation wheel attempt lacked setuptools in the runtime
environment; the normal declared isolated build succeeded without a runtime
dependency or lockfile change. Other earlier trial failures and the exact
unresolved ChatGPT renderer boundary are in the
[repair record](../repairs/2026-10-23-local-author-and-manager.md).

Observed warnings are retained rather than hidden: pinned FastAPI/Starlette and
websockets interfaces emit deprecation notices, Python warns about a test's
fork from a multithreaded process, and the deliberate duplicate-ZIP-member
fixture emits its duplicate-name warning. These do not establish an observed
deadlock or successful hostile-process containment. Dependency upgrades and a
framework lifecycle migration are separate changes; no manifest was changed
solely to silence warnings.

Skipped or synthetic checks do not qualify Linux Author execution on this Mac,
hostile Darwin containment, a new browser route, client file import, production
ingress/deployment or permanent upstream rendering reliability. Raw host,
worker, transport and privacy-audit evidence remains outside public source.
