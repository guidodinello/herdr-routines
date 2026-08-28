---
id: "007"
title: "Approval path for blocked runs"
status: open
priority: low
area: pipeline
---

## Description

A job blocked on an agent permission prompt ends as `blocked` and waits.
Preferred direction (`docs/plan-v1.md` §2): surface an actionable
notification (herdr-push / the installed `herdr-telegram-bridge`) so the
prompt can be approved from the phone.

The alternative — a skip-permissions / unattended auto-approve escape hatch —
is deliberately **not** in scope: scheduled + unattended + auto-approve is
the one combination where a single bad prompt becomes an unreviewable repo
mutation, and worktree isolation does not contain it (worktrees share the
object store and can push). Reconsider auto-approve only if the
phone-approval path proves too slow in practice.

## Acceptance

- A run that settles `blocked` on a permission prompt emits exactly one
  actionable notification identifying the job and the prompt.
- Approving from the phone (reply-to-steer via the Telegram bridge, or the
  herdr-push path) unblocks the waiting run without a manual SSH session.
- No auto-approve / skip-permissions mode is added.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. Partly overtaken by the
  `herdr-telegram-bridge` install (2026-08-25) which already notifies on
  `blocked` pane transitions and supports reply-to-steer — remaining work is
  wiring that to a real approval of the pending prompt, not just visibility.
