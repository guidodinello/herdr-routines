---
id: "083"
title: "Type telegram spawn aliases as a record instead of re-stringifying WorkspaceMode"
status: open
priority: low
area: infra
---

## Description

`telegram_spawn.alias_table` (~:175) returns
`dict[str, dict[str, str | None]]`. As a result, the consumer at ~:242 rebuilds the
workspace mode with `mode = str(spec.get("workspace") or "worktree")`. Config already
validated that value as a closed `WorkspaceMode`, so this throws the type away and
duplicates the default.

A small frozen dataclass or TypedDict per alias (`repo`, `workspace: WorkspaceMode`,
`model`, ...) would carry the validated values through. The change spans config,
telegram_spawn and cli.

## Acceptance

- The alias table is typed by a named record. `workspace` is a `WorkspaceMode`.
- The `"worktree"` default lives in exactly one place (config).
- No behavior change in the spawn flow. `scripts/ci-check.sh` and
  `uv run mypy --strict src` are green.

## Log

- **2026-10-26**: filed from the type-health audit's INVESTIGATE list.
