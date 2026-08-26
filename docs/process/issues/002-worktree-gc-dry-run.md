---
id: "002"
title: "Worktree GC, dry-run half"
status: done
priority: low
area: cli
---

## Description

`herdr-routines gc --dry-run`: list `auto/<name>-<ts>` branches that are
merged or whose worktree is gone. Read-only and mechanical; useful as soon as
the first real worktree jobs run.

The deletion half is a separate, gated item (`ROADMAP.md` Next § "Worktree
GC, delete half" — gate: several weeks of trusting this dry-run output).
Nothing is deleted by this issue's scope.

## Acceptance

- `herdr-routines gc --dry-run` lists merged/orphaned `auto/*` branches
  without modifying anything.
- Output is legible enough to trust before the delete half is ever built.

## Log

- **2026-08-25**: shipped (PR #28, `20260824T232136Z` — pipeline dogfood
  run) as `herdr-routines gc --dry-run` (`src/herdr_routines/gc.py`).
  `ROADMAP.md` still listed this as open at curation time — stale entry. The
  delete half remains open, tracked in `ROADMAP.md` Next §, not curated here
  per this pass's scope (Now-only).
