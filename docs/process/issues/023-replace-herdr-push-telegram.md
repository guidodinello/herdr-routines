---
id: "023"
title: "Replace the herdr-push Telegram plugin"
status: done
priority: low
area: infra
---

## Description

Replace the `herdr.push` notification plugin (dead once the `herdr-remote`
relay was removed) with a real bidirectional phone channel.

Resolved: went with the community plugin `cokekitten/herdr-telegram-bridge`
rather than building a bot from scratch. It does exactly the shape wanted —
outbound-only `getUpdates` long-polling (so the Pi's lack of inbound
reachability is a non-issue), reply-to-steer any agent pane from Telegram,
and notifies on `done` / `blocked` pane transitions.

## Acceptance

- Bridge installed and configured on the Pi with a real bot token / chat_id. ✓
- Test notification received, reply poller confirmed running. ✓ (2026-08-25)
- Dead `herdr.push` removed from the Pi. ✓

## Log

- **2026-08-25**: plugin installed, source-reviewed
  (`agent-orchestrator-research/herdr/security-reviews.md`). Token/chat_id
  moved over from the old `herdr-remote` secrets file; test notification
  received; `herdr.push` uninstalled from the Pi. Laptop still has the dead
  `herdr.push` installed — same cleanup pending there (minor, tracked here).
- **Open verification item** (carried, not blocking): the bridge's
  notification hook is `pane.agent_status_changed` — raw Herdr pane state.
  `execute_run` closing a job's pane immediately on settle (G-16 / PR #42)
  could race that hook. Needs confirming across a few real overnight runs; if
  unreliable, the fallback is wiring notifications off `herdr notification
  show` (the routine's explicit completion signal) instead of pane status.
- **2026-08-27**: marked done — the decision was made and the transport is
  deployed. Residual items above are follow-ups, not open scope.
