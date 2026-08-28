---
id: "014"
title: "API / webhook trigger"
status: blocked
priority: low
area: infra
gate: a design decision on transport (poll-based diff vs. same-LAN HTTP endpoint vs. tunnelled webhook) — not yet made
---

## Description

Claude Routines supports "Call via API" (POST to trigger a run) and a
GitHub-event trigger. Both assume inbound reachability, which the Pi doesn't
have without a tunnel (declined earlier for the Telegram relay — see
`../agent-orchestrator-research/herdr.md`).

Undecided design question, which is why this is `blocked` and not `open`:
- A GitHub-event-style trigger would likely start **poll-based** (a timer
  checks `gh api` on a short interval and diffs) rather than a true webhook.
- A same-LAN "call via API" (small local HTTP endpoint, no tunnel) is more
  plausible short-term.

Nobody has chosen between these, so there is no spec to implement.

## Acceptance

To be written once the transport decision is made.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Kept `blocked` — the
  waived gates were the "weeks of real runs" ones; this one is gated on an
  unmade design decision, not on elapsed time. Trigger: a recurring need to
  start runs from outside the cron model.
