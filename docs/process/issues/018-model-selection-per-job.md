---
id: "018"
title: "Model selection per job beyond claude/opencode"
status: open
priority: low
area: config
---

## Description

`model` is wired through to `agent_start` for `agent_kind: claude` /
`opencode` — the only two kinds with a pinned-down native flag
(`AGENT_MODEL_FLAGS` in `config.py`). Extend to other agent kinds, and/or add
a model-catalog / existence check so a typo'd model name fails validation
rather than at run time.

## Acceptance

- `model` is honored for at least one additional `agent_kind`, with its
  native flag documented alongside the existing entries in `AGENT_MODEL_FLAGS`.
- `validate` rejects a `model` that the corresponding agent kind cannot
  accept (unknown flag / unsupported kind), rather than deferring the failure
  to run time.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Trigger ("actually
  wanting either") — modest; kept low priority. Split the catalog/existence
  check out if the additional-agent-kind half has no concrete demand.
