---
id: "012"
title: "Worktree GC, delete half"
status: done
priority: medium
area: cli
---

## Description

Opt-in deletion of what `herdr-routines gc --dry-run` (issue 002, PR #28)
lists — merged or orphaned `auto/<name>-<ts>` branches and their worktrees.
Nothing is ever removed automatically: a scheduled tool that deletes branches
unattended is explicitly not wanted. Deletion is a human-invoked
`herdr-routines gc --delete` (or `--prune`) acting on the dry-run's own
output.

## Acceptance

- `herdr-routines gc --delete` removes exactly the branches/worktrees the
  paired `--dry-run` lists, and nothing else.
- Refuses to run from an unattended/scheduled context (interactive-only, or
  requires an explicit `--yes`).
- A branch that is not merged is never deleted without an explicit
  `--force`-style opt-in.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. Original gate ("several
  weeks of trusting the dry-run output") waived by decision 2026-08-27; the
  dry-run has been correct across the pipeline dogfood runs so far. Phase 1
  (dry-run) is issue 002.
