---
id: "003"
title: "Status CLI, table view"
status: done
priority: medium
area: cli
---

## Description

(2026-08-25 idea.) `herdr-routines` already has `status`/`history` for its
own scheduled jobs, but there's no single place to see everything running or
scheduled across the whole Herdr+pipeline stack: workspaces/panes/agents
(`herdr pane list` / `herdr agent list`), pipeline runs in progress
(`~/.local/state/herdr-routines/reports/pipeline-*.md` + `state.json`), and
scheduled jobs (`jobs.yaml` + `history.jsonl`) each need a separate manual
query today — one whole session was hours of hand-written `jq`/`ssh`
one-liners stitching those together.

Two simple commands, no web server, no daemon:

- one prints a table of what's *currently running* (panes/agents/in-progress
  pipeline runs)
- another prints what's *scheduled* (cron jobs + their next-fire time, any
  pending `systemd-run` one-shots)

Purely additive read path over data that already exists in these three
places — no new state to own. Narrower and cheaper than the Later "Web/TUI
dashboard" item — a natural first step there, not a replacement for it if
that's still wanted once this exists.

## Acceptance

- One command shows currently-running work across panes/agents/pipeline runs
  in a plain table.
- One command shows scheduled work (cron jobs + next-fire, pending one-shots)
  in a plain table.
- No new persisted state; both commands only read what already exists.

## Log

- **2026-08-25**: shipped as `ps` and `scheduled` subcommands (PR #41,
  `20260825T070012Z` — the pipeline's own dogfood run implemented this item),
  with a same-day follow-up (PR #43) fixing the tables to "say what they
  mean." `ROADMAP.md`'s Now section still described this as open at curation
  time — stale entry, not caught until this pass cross-checked against
  `git log`.
