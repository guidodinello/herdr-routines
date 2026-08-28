---
id: "014"
title: "API / webhook trigger"
status: blocked
priority: low
area: infra
gate: issue 015 (auto-fix PRs) shipping first — it establishes the gh-api-polling pattern this would generalize; revisit after it lands
---

## Description

Claude Routines supports "Call via API" (POST to trigger a run) and a
GitHub-event trigger. Both assume inbound reachability, which the Pi doesn't
have without a tunnel (declined earlier for the Telegram relay — see
`../agent-orchestrator-research/herdr.md`).

The transport question is largely settled by constraints: the Pi has no
inbound reachability and tunnels were declined, so a **poll-based** trigger
(a timer checks `gh api` on an interval and diffs) is the only realistic
GitHub-event path. A same-LAN "call via API" (small local HTTP endpoint, no
tunnel) is a separate, smaller thing — split it out if push-button "run job
X now" is actually wanted.

Why this stays `blocked`: issue 015 (auto-fix PRs) will already build a
"poll `gh api` on each tick" mechanism. Unblocking this now means designing a
second polling path before the first exists. Once 015 lands, this becomes a
small "generalize 015's polling into an event-triggered job type"
follow-up — revisit then.

## Acceptance

To be written once issue 015 has shipped and its polling pattern is known.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §.
- **2026-08-28**: transport question narrowed to "poll-based" by the Pi's
  no-inbound constraint. Gate changed from "pick a transport" to "wait for
  issue 015" — 015 establishes the gh-api-polling pattern this generalizes,
  so building both independently is wasteful. Trigger unchanged: a recurring
  need to start runs from outside the cron model.
