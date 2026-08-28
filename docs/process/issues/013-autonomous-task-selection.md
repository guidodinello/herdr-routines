---
id: "013"
title: "Autonomous task selection for the pipeline"
status: done
priority: low
area: pipeline
---

## Description

For the pipeline to run nightly unattended, stage 0 needs to pick its own
next feature rather than waiting on a human to hand-author a `FEATURE_IDEA`
file.

Resolved via option (a) from `ROADMAP.md` Later §: build the structured
selection layer. `docs/process/issues/` now curates roadmap items into
frontmatter'd files (`id`/`title`/`status`/`priority`/`area`/`gate`), and
`herdr-routines pick-feature` selects the highest-priority, lowest-id
`status: open` issue → `FEATURE_IDEA` text (`--mark-in-progress` to claim
it). The orchestrator prompt falls back to it when no `FEATURE_IDEA` is
supplied (`docs/pipeline/orchestrator-prompt.md` § Inputs).

Scope explicitly **not** covered by this issue: making the pipeline
self-*scheduling*. A human (or a `systemd-run --on-calendar` they configure)
still decides *when* a run happens — only *which* item it builds is
automated.

## Acceptance

- `pick-feature` returns a `FEATURE_IDEA` for a non-empty open backlog and
  exits 1 with `no open issues` for an empty one. ✓
- Orchestrator prompt self-selects via `pick-feature` when launched without
  `FEATURE_IDEA`. ✓

## Log

- **2026-08-25**: `docs/process/` structured layer + `pick-feature` shipped
  (PR #46). Curation also caught 3 stale `Now` entries already shipped.
- **2026-08-27**: marked done. The empty-backlog abort path was exercised
  twice (`20260826T050016Z`, `20260827T050015Z`) and behaved correctly —
  the missing piece was backlog content, not selection logic. This curation
  pass refills it.
