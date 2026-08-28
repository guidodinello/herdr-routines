---
id: "010"
title: "Daily digest"
status: open
priority: low
area: infra
---

## Description

Aggregate terminal states + report links into one morning summary instead of
(or alongside) per-run notifications. Natural home is the herdr-push /
Telegram relay.

## Acceptance

- A single scheduled digest job (or a built-in) posts one message per morning
  listing each job's last terminal state and a link to its report.
- The digest reads existing run history (`history.jsonl` / reports dir) — no
  new state store.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. Original gate ("enough
  nightly jobs that per-run pings are noise") — we now run ~6 nightly jobs;
  gate effectively cleared. Pairs with issue 009 (notification policy).
