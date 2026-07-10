# Codex Model Routing Review — 2026-07-10

Status: evidence-backed routing decision for PatchBay's seven documented Codex worker models. Recheck the live Codex catalog and usage dashboard before treating availability or credit rates as permanent.

## Decision

PatchBay should optimize expected subscription use to a verified correct result, not raw price per token or benchmark score alone.

| Role | Default | Why |
| --- | --- | --- |
| Compact standard worker | GPT-5.6 Luna | Much stronger than the older small models and close to GPT-5.5 on several coding tasks, with materially higher included capacity than frontier models. |
| Main serious worker | GPT-5.6 Terra | Broadly matches or exceeds GPT-5.5 while retaining an everyday subscription-usage profile. |
| Highest-authority worker | GPT-5.6 Sol | Strongest broad model; use for hard synthesis, architecture, unresolved failures, sensitive review, and final judgment. |
| Tiny latency-sensitive worker | GPT-5.3-Codex-Spark | Separate Pro preview quota and extreme speed; keep tasks bounded because intelligence and context are lower. |
| Maximum-capacity simple worker | GPT-5.4 Mini | Highest standard included message capacity; use only when the task is simple enough that Luna's higher success probability is not worth the difference. |
| Legacy serious fallback | GPT-5.4 | Use for temporary 5.6 unavailability, compatibility, or a proven regression. |
| Legacy frontier fallback | GPT-5.5 | Use for temporary 5.6 unavailability or task families where its long-context, multimodal, or tool behavior is measurably better. |

Normal worker teams should therefore use Luna for compact lanes, Terra for primary investigator/implementer/reviewer lanes, and Sol for authority or difficult synthesis. Escalate after evidence of failure or uncertainty; do not assign Sol to every lane.

## Intelligence evidence

The ranking is multidimensional. The best-supported broad order is Sol > Terra ≳ GPT-5.5 > Luna > GPT-5.4 > GPT-5.4 Mini > Spark, with task-specific overlaps.

Current-generation comparisons from OpenAI's July 9 general-availability table:

| Evaluation | Sol | Terra | Luna | GPT-5.5 |
| --- | ---: | ---: | ---: | ---: |
| Artificial Analysis Intelligence Index v4.1 | 58.9 | 55.0 | 51.2 | 54.8 |
| Artificial Analysis Coding Agent Index v1.1 | 80.0 | 77.4 | 74.6 | 76.4 |
| SWE-Bench Pro | 64.6% | 63.4% | 62.7% | 59.4% |
| DeepSWE v1.1 | 72.7% | 69.6% | 67.2% | 67.0% |
| Terminal-Bench 2.1 | 88.8% | 87.4% | 84.7% | 85.6% |
| Agents' Last Exam | 52.7% | 50.4% | 50.3% | 46.9% |
| BrowseComp | 90.4% | 87.5% | 83.3% | 84.4% |
| RSI Index | 57.9% | 56.3% | 41.9% | 41.7% |
| MRCR 8-needle 512K-1M | 73.8% | 72.5% | 41.3% | 74.0% |

Important exceptions prevent a simplistic total ordering:

- GPT-5.5 remains slightly ahead of Luna on the broad intelligence and coding-agent indices, Terminal-Bench, multimodal scores, and very-long-context retrieval, even though Luna wins narrowly on SWE-Bench Pro, DeepSWE, and the aggregate RSI index.
- Terra beats GPT-5.5 on most professional, coding, computer-use, scientific, cyber, and self-improvement evaluations, but GPT-5.5 still wins selected multimodal, math, tool-use, and long-context rows.
- Sol is the clear broad leader, but it is not best on every row: the release table shows small regressions on selected long-context and tool evaluations.
- Ultra reaches 91.9% on Terminal-Bench 2.1, but it is a four-agent execution mode, not a separate eighth model or a reasoning-effort string.

Older-model bridge evidence uses the last apples-to-apples published comparisons:

| Evaluation | GPT-5.5 | GPT-5.4 | GPT-5.4 Mini | Spark |
| --- | ---: | ---: | ---: | ---: |
| SWE-Bench Pro | 58.6% | 57.7% | 54.4% | 51.5% at xhigh |
| Terminal-Bench 2.0 | 82.7% | 75.1% | 60.0% | 58.4% |
| GPQA Diamond | 93.6% | 92.8% | 88.0% | not published |
| HLE without tools | 41.4% | 39.8% | 28.2% | not published |
| OSWorld-Verified | 78.7% | 75.0% | 72.1% | not published |

These older and newer tables use different harness versions. They support role ordering but must not be merged into a fake single precise intelligence score.

## Subscription economics

OpenAI now describes Codex subscription consumption in credits per million input, cached-input, and output tokens. Real task consumption also depends on reasoning effort, context, tool calls, cache reuse, and how many tokens the model needs to finish successfully.

The most decision-useful included-capacity figures are the published Plus local-message ranges per shared five-hour window:

| Model | Approximate local messages / 5h | Relative operational reading |
| --- | ---: | --- |
| GPT-5.6 Sol | 15-90 | Authority model; token efficiency keeps effective capacity close to GPT-5.5. |
| GPT-5.6 Terra | 20-110 | Main default; slightly higher published capacity than GPT-5.4. |
| GPT-5.6 Luna | 50-280 | High-volume current model; somewhat less capacity than Mini but much more intelligence. |
| GPT-5.5 | 15-80 | Usually dominated by Sol unless a regression favors 5.5. |
| GPT-5.4 | 20-100 | Usually dominated by Terra in current effective capacity and quality. |
| GPT-5.4 Mini | 60-350 | Highest standard capacity; retain for truly simple volume work. |
| Spark | Separate preview limit | Pro-only preview quota; not comparable to standard credits and may vary with demand. |

Pro 5x and Pro 20x publish exactly 5x and 20x these message bands. Plus, Pro, and other agentic features can share usage pools; additional weekly limits may apply.

### Credit-rate contradiction

Two official pages disagreed during this investigation:

- The Help Center Codex rate card showed preview-era 5.6 rates equal to same-priced predecessors: Sol 125/12.5/750 credits, Terra 62.5/6.25/375, and Luna 25/2.5/150 per million input/cached/output tokens.
- The new Codex pricing page rendered Sol 250/25/1500 and Luna 50/5/300, exactly double those values. Its Terra row rendered 125/12.5/**125**, which is internally implausible and inconsistent with both API pricing and the surrounding family ratios; it likely contains a launch-day table error, but PatchBay must not silently rewrite an official number.

Therefore PatchBay guidance must not hard-code exact credit rates. Use the live usage dashboard and current official pricing page for accounting, and use the published per-five-hour message bands for coarse routing until OpenAI reconciles the rate card.

API prices are a separate axis: Sol $5/$30, Terra $2.50/$15, Luna $1/$6 per million uncached input/output tokens. They do not directly state how much included subscription allowance a PatchBay worker consumes.

## Reasoning and orchestration

- PatchBay should accept `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`, then let the selected model's live catalog determine which subset is valid.
- Use low or medium for routine Luna work, medium or high for normal Terra work, and high or xhigh for Sol authority work.
- Use `max` only when the live catalog supports it and the expected quality gain justifies the extra use. Published 5.6 results show large gains on some hard tasks but diminishing or even poor efficiency on others.
- Codex CLI `0.144.1` exposes `ultra` as a reasoning effort for GPT-5.6 Terra and Sol. It may automatically delegate subtasks inside one Codex worker. PatchBay accepts it, but explicit PatchBay worker teams preserve named ownership, independent reports, separate worktrees, diffs, and integration state and remain the preferred orchestration surface when those controls matter.
- Continue the same worker before escalating when the issue is missing evidence or an incomplete first report. Escalate the model when the failure reflects judgment, context, or reasoning limits.

## Availability boundary

General availability began July 9 with a gradual rollout. On July 10, `codex-cli 0.144.1` on the maintainer machine returned all seven documented worker models: GPT-5.6 Sol, Terra, and Luna; GPT-5.5; GPT-5.4; GPT-5.4 Mini; and Spark. It reported 372K context for Sol/Terra/Luna, 272K for GPT-5.5/5.4/5.4 Mini, and 128K for Spark. Terra and Sol advertised `ultra`; Luna advertised through `max`. Installations can still differ during rollout, so `codex_worker_options` remains the runtime authority.

## Sources

- [GPT-5.6 general-availability announcement and evaluation tables](https://openai.com/index/gpt-5-6/)
- [Codex pricing, plan message bands, and token-credit table](https://learn.chatgpt.com/docs/pricing)
- [Codex speed and Spark quota behavior](https://learn.chatgpt.com/docs/agent-configuration/speed)
- [Codex Help Center rate card](https://help.openai.com/en/articles/20001106-codex-rate-card-2)
- [GPT-5.5 evaluation tables](https://openai.com/index/introducing-gpt-5-5/)
- [GPT-5.4 Mini evaluation tables](https://openai.com/index/introducing-gpt-5-4-mini-and-nano/)
- [GPT-5.3-Codex-Spark launch and evaluation charts](https://openai.com/index/introducing-gpt-5-3-codex-spark/)
