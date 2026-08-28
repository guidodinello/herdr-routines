---
id: "021"
title: "Log rotation"
status: open
priority: low
area: infra
---

## Description

`history.jsonl` and the reports directory grow unbounded. A handful of jobs
writing a few JSONL lines a day won't matter for years
(`docs/plan-v1.md` §5), but a simple, mechanical rotation/retention is cheap
to add now and removes a "someday" chore.

Scope: size- or age-based rotation of `history.jsonl` (roll to
`history-YYYYMM.jsonl` or similar) and an age-based prune of the reports
directory, both configurable, both off or generous by default.

## Acceptance

- `history.jsonl` rotates on a configurable size/age threshold; `history` /
  `ps` / `scheduled` still read across the rolled files transparently.
- Reports older than a configurable window can be pruned by an explicit
  command; nothing is deleted automatically without opt-in.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Original gate
  ("history.jsonl size becoming noticeable") waived 2026-08-27 — small,
  mechanical, and worth having in place before it's needed rather than after.
