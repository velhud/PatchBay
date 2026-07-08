# Multi-ChatGPT Hub Coordination Idea

Status: documented future design, not implemented.

Current bridge: Hub work groups are implemented as the durable task object. A
future campaign/channel layer should build on work groups rather than returning
to a flat historical worker list. Several ChatGPT conversations may eventually
share or take over explicit groups, but private groups remain the default.

This document preserves the product idea that PatchBay Hub should eventually let
several ChatGPT conversations work through the same hub at the same time, see
shared machine/work state, exchange information, and coordinate higher-level
work across machines and worker teams.

## Core Idea

The first hub goal is one ChatGPT connector that can route work to multiple
PatchBay Edge machines.

The larger idea is stronger:

```text
ChatGPT conversation A
ChatGPT conversation B
ChatGPT conversation C
        |
one PatchBay Hub
        |
multiple machines, workers, reports, campaigns, and shared channels
```

If several ChatGPT tabs or conversations are connected to the same PatchBay Hub,
they should eventually be able to coordinate through server-side shared state.
That means each conversation can act as a high-level manager with its own focus,
while the hub provides a common coordination surface.

Examples:

- one ChatGPT conversation manages RetailMind backend work on a VM;
- another manages UI or documentation work on a local machine;
- another acts as a reviewer/synthesis conversation;
- all of them can see relevant campaign state, machine status, worker reports,
  and messages through PatchBay Hub.

The important point is not simply "many clients can connect." The important
point is that multiple high-level ChatGPT managers could exchange context,
assign work, leave messages, ask for reviews, and coordinate without the human
copying reports between tabs.

## Why This Could Be Powerful

PatchBay already treats Codex workers as local employees. Multi-ChatGPT Hub
coordination extends that idea upward:

```text
ChatGPT conversations = high-level managers / architects / reviewers
Codex workers = machine-local engineering employees
PatchBay Hub = command center and shared operating board
PatchBay Edge = machine-local execution authority
```

This could unlock:

- several ChatGPT conversations working on different parts of one large mission;
- one conversation delegating work to another conversation indirectly through a
  shared campaign or mailbox;
- cross-machine work where one conversation owns the VM lane and another owns a
  local-machine lane;
- dedicated reviewer conversations that inspect reports from implementer
  conversations;
- long-running asynchronous work where one conversation leaves a message or
  task for another to pick up later;
- central state that survives browser refreshes, ChatGPT session churn, and
  PatchBay restarts.

## What Must Not Happen

This feature must not become a confusing historical dump.

Bad default:

```text
Every conversation sees every old worker, every old report, every old machine
event, and every old status line forever.
```

Good default:

```text
Each conversation sees the current campaign/work run, live/problem workers,
assigned messages, and intentionally selected shared channels. Historical state
is available only through explicit scopes and filters.
```

The hub must avoid overwhelming ChatGPT with stale state. It must distinguish:

- current work run;
- current work group;
- same conversation;
- same campaign;
- same user/server owner;
- active/live/problem workers;
- recent history;
- full archive.

## Required Concepts

### Hub Identity

The hub needs stable identity and safe client references:

- `hub_id`: stable hub instance;
- `chatgpt_session_ref`: hashed ChatGPT conversation/session hint when
  available;
- `chatgpt_subject_ref`: hashed user/account hint when available;
- `work_run_ref`: current task/run grouping by activity and idle gap;
- `campaign_id`: explicit shared mission grouping;
- `channel_id`: explicit communication channel;
- `message_id`: one mailbox message or event;
- `machine_id`: edge machine;
- `worker_id`: machine-local worker.

Raw ChatGPT session metadata must not be logged or returned. Use hashed public
references only.

### Work Groups

Work groups are the implemented V1 task object. They contain:

- title and goal;
- visibility (`private` by default, `shared` explicitly);
- pinned machine;
- repo/workspace hint;
- lanes;
- worker refs;
- queued/running/completed command refs;
- preflight result;
- close/reassign history.

Multiple ChatGPT conversations should not discover old workers by reading a
giant fleet status dump. They should list/resume/close explicit groups. Sharing
or takeover should happen at the group/lane level before future mailbox or
campaign objects coordinate across conversations.

### Campaigns

A campaign is the central shared work object.

It should contain:

- title and goal;
- owner conversation;
- participating conversations;
- participating machines;
- participating workers;
- task lanes;
- reports;
- open questions;
- current decisions;
- messages/events;
- status;
- next actions.

Campaigns are how multiple ChatGPT conversations coordinate without each one
having to rediscover the whole world.

### Channels And Mailbox

The hub needs a small mailbox/channel layer.

Possible channels:

- campaign channel;
- machine channel;
- worker channel;
- conversation-to-conversation direct message;
- review request channel;
- handoff channel.

Messages should be structured but natural-language first:

```json
{
  "message_id": "msg_...",
  "campaign_id": "camp_...",
  "from": "chatgpt_session_...",
  "to": ["chatgpt_session_...", "machine:ucl-vm", "channel:review"],
  "kind": "question|report|handoff|review_request|decision|note",
  "text": "...",
  "references": []
}
```

The mailbox is not a replacement for worker reports. It is a coordination layer
for managers and machines.

### Shared Reports

Worker reports should be referenceable from the hub so another ChatGPT
conversation can ask:

```text
Show me reports from campaign X relevant to the UI lane.
```

The hub should return compact report references first, then allow bounded
inspection. It should avoid dumping full raw transcripts by default.

## Manager Behavior In Multi-ChatGPT Mode

ChatGPT conversations should be instructed to behave like managers:

1. Join or create a campaign.
2. Read campaign status and assigned messages.
3. Choose machines and workers based on capability/workspace.
4. Delegate work to Codex workers on the right machines.
5. Post useful reports/decisions back to the campaign.
6. Ask other conversations or workers for clarification when needed.
7. Avoid doing broad manual file reading unless it is a focused verification or
   escalation.

Different conversations can have different roles, but roles should remain
natural language, not rigid deterministic routing:

- implementation manager;
- architecture reviewer;
- release manager;
- evidence auditor;
- synthesis manager;
- machine-specific operator.

## Conflict And Authority Rules

Multi-ChatGPT coordination creates risks that must be explicit.

Risks:

- two conversations start conflicting implementations;
- one conversation integrates changes another conversation has not reviewed;
- historical workers are mistaken for current workers;
- one conversation stops another conversation's active worker;
- shared status becomes too large or confusing;
- messages are interpreted as higher-priority instructions than user/system
  authority.

Required safeguards:

- every mutating command must include explicit `machine_id`;
- campaign membership and current work run must be visible;
- cross-conversation mutation should require explicit takeover or campaign
  authority;
- integration should remain machine-local and explicit;
- hub messages and worker reports are data/evidence, not instruction authority;
- final commits/pushes/deploys still require ordinary release gates;
- default status views must hide old/stopped/historical clutter.

## Relationship To Current V1 Hub

Current V1 Hub/Edge has:

- hub server;
- edge enrollment;
- machine list/status;
- workspace projections;
- routed worker commands through polling;
- command status;
- live local smoke test.

Current V1 Hub/Edge does not yet have:

- campaigns;
- mailbox/channels;
- multi-ChatGPT conversation messaging;
- shared report registry;
- WebSocket progress streaming;
- cross-machine report injection as a first-class hub feature;
- formal real ChatGPT multi-tab evaluation.

The existing V1 is the right base because it already creates the central hub and
machine routing layer. Multi-ChatGPT coordination should be built on top of it,
not mixed into the single-machine server.

## Suggested Future Implementation Phases

### Phase A: Conversation And Campaign Registry

- Add `campaign_id`.
- Let ChatGPT create/list/join campaigns.
- Track safe `chatgpt_session_ref` and `work_run_ref`.
- Default all hub views to current campaign when one is active.

### Phase B: Shared Message Channels

- Add `patchbay_campaign_message_send`.
- Add `patchbay_campaign_message_list`.
- Add `patchbay_campaign_message_read`.
- Support message kinds: question, report, handoff, review request, decision,
  note.

### Phase C: Report References

- Let worker results be attached to a campaign.
- Return compact report indexes.
- Support bounded report inspection by reference.

### Phase D: Cross-Conversation Coordination Workflows

- Let one conversation leave a review request for another.
- Let a reviewer conversation attach a decision to a campaign.
- Let ChatGPT summarize campaign state without reading all history.

### Phase E: Multi-ChatGPT Live Eval

Test with real ChatGPT Developer Mode or equivalent:

1. Open two or three ChatGPT conversations connected to the same hub.
2. Create one campaign.
3. Have each conversation take a different lane.
4. Start workers on different machines.
5. Exchange messages through the hub.
6. Verify status stays focused and not historically noisy.
7. Verify one conversation does not accidentally mutate another's workers
   without explicit authority.

## Design Principle

The goal is not to make ChatGPT weaker or more constrained. The goal is to make
multiple ChatGPT managers able to cooperate through shared state while keeping
the interface legible.

The hub should provide:

- shared memory;
- shared task board;
- shared message channels;
- shared report references;
- machine routing;
- worker routing;
- status projection.

It should not turn into:

- a deterministic role engine;
- a hidden workflow bureaucracy;
- a raw transcript dump;
- a global lock system pretending to solve Git conflicts;
- a replacement for natural-language management.

The product direction remains:

```text
high-level ChatGPT managers
-> natural-language coordination through PatchBay Hub
-> machine-local Codex workers
-> explicit reports, integration, commits, and deployment gates
```
