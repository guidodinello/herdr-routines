---
id: "024"
title: "Spawn a session on the fly from Telegram"
status: open
priority: low
area: infra
---

## Description

Start a *brand new* session against a named repo/job from a cold Telegram
message — no existing notification to reply to. The `herdr-telegram-bridge`
plugin (issue 023) already covers reply-to-steer an existing pane; what's
missing is a mapping from "which repo/job" to `herdr workspace create` +
`agent start`, roughly the same mechanics the pipeline launcher already uses.

## Acceptance

- A recognized command message (e.g. `/run <job>` or `/start <repo>`) creates
  a workspace and starts an agent against the right checkout, and replies
  with the pane so it can then be steered.
- Unknown / unmapped targets are rejected with a helpful message, not a
  silent no-op.
- Only a configured chat_id can trigger a spawn.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Parking Lot §. Scope shrank once
  the transport question was settled by issue 023 — this is now just the
  cold-start mapping.
