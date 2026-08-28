---
id: "009"
title: "Notification policy per job"
status: open
priority: low
area: config
---

## Description

Decide what "worth telling you" means per job — only on failure, only on a
non-trivial finding, or every run — instead of a ping on every run. Claude
Routines frames its notification toggle the same way.

An unattended overnight run should push exactly one notification (the final
report / PR link, or a failure), not a stream of progress pings. This is the
*policy* half (what to send); the *transport* half (how it reaches the phone)
is settled — the `herdr-telegram-bridge` plugin (see issue 023).

## Acceptance

- A job can declare a notification policy: `always` | `on-failure` |
  `on-finding` (or similar), defaulting to a single terminal-state
  notification.
- An unattended run under the default policy produces one notification, not
  per-step noise.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. The 2026-08-25 note there
  observed the gate is close to clearing — the pipeline POC's own
  manual-monitoring loop (repeated 2–5 min check-ins) is exactly the
  noisy-ping experience this fixes. Bundle with issue 010 (daily digest).
