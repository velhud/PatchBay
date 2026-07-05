# OpenAI Services And Usage Boundaries

PatchBay should be described as a local workflow bridge for user-authorized ChatGPT and Codex usage.

It should not be described as an OpenAI product, a quota bypass, an account pooling system, a scraper, or a way to obtain free Codex execution.

## Correct project framing

PatchBay is an independent local MCP bridge that lets ChatGPT coordinate user-authorized Codex CLI work in local repositories.

The legitimate workflow is:

- ChatGPT provides high-context reasoning, planning, decomposition, and worker coordination.
- PatchBay exposes a local MCP control plane with explicit tool boundaries.
- Codex CLI executes local investigation, editing, testing, review, and reporting through the user's configured Codex environment.
- The user remains responsible for their accounts, subscriptions, billing, permissions, repositories, and connected data.

## Required boundary language

Use this language in public-facing docs when the OpenAI/ChatGPT/Codex relationship is relevant:

```md
PatchBay is an independent open-source project. It is not affiliated with,
endorsed by, sponsored by, or maintained by OpenAI.

PatchBay is a local workflow bridge, not a quota bypass layer. It does not
bypass OpenAI rate limits, usage limits, billing, safety systems, account
controls, or Codex usage accounting.

ChatGPT interactions remain under the user's ChatGPT account and connector
permissions. Codex execution remains under the user's local Codex CLI
configuration, subscription, API key, or billing arrangement.
```

## Things PatchBay does not do

PatchBay does not:

- bypass OpenAI rate limits, usage limits, billing, safety systems, account controls, or Codex usage accounting;
- scrape ChatGPT;
- automate hidden ChatGPT UI extraction;
- reverse engineer OpenAI services;
- modify OpenAI clients;
- pool, share, or resell OpenAI accounts;
- impersonate OpenAI;
- imply OpenAI endorsement, sponsorship, or official status;
- operate on repositories, systems, or data the user is not authorized to use.

## Recommended wording

Prefer:

- "Coordinate local Codex CLI workers from ChatGPT."
- "Use ChatGPT as a high-context manager for local Codex work."
- "Route user-approved context through a local MCP bridge."
- "Codex still runs through the user's local Codex CLI configuration."
- "Review reports, changed files, diffs, and integration previews before applying work."
- "Independent project; not affiliated with OpenAI."

Avoid:

- "Use ChatGPT Pro to run many Codex workers for free."
- "Multiply ChatGPT Pro capacity."
- "Bypass Codex limitations."
- "Unlimited workers through ChatGPT Pro."
- "Official ChatGPT/Codex bridge."
- "OpenAI-powered control plane" if it could imply endorsement.
- "Exploit ChatGPT Pro reasoning."
- "Automate ChatGPT UI."
- "Extract ChatGPT output automatically."

## How to describe multiple workers

Good:

```md
PatchBay supports multi-worker local development patterns: one worker can
investigate architecture, another can implement in an isolated worktree, and
a third can review or verify the result. ChatGPT acts as the coordinator;
Codex does the local tool work through the user's configured Codex CLI.
```

Bad:

```md
Run many Codex workers through one ChatGPT Pro subscription.
```

The first description is workflow orchestration. The second sounds like usage arbitrage and must be avoided.

## Brand relationship

PatchBay should always be presented as independent.

Recommended relationship statement:

```md
PatchBay is an independent open-source project. It is not affiliated with,
endorsed by, sponsored by, or maintained by OpenAI. ChatGPT, Codex, OpenAI,
and related marks are trademarks of OpenAI. PatchBay uses those names only
to describe compatibility with user-configured OpenAI services.
```
