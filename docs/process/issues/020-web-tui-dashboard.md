---
id: "020"
title: "Web / TUI dashboard"
status: blocked
priority: low
area: cli
gate: reaching for `status` feeling like friction rather than ritual, even after the plain-table view (issue 003) exists
---

## Description

A web or TUI dashboard for run inspection. The `status` / `history` /
`scheduled` / `ps` CLI covers inspection at current scale; issue 003 (PR #41,
#43) already added plain tables as the cheaper first step.

Kept `blocked`: no framework choice made, and the trigger is a subjective
friction threshold that hasn't been hit. Building a web/TUI layer now is
structure for a need that doesn't exist.

## Acceptance

To be written if/when the CLI inspection surface is demonstrably not enough.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Gate is a friction
  trigger, not a time gate.
