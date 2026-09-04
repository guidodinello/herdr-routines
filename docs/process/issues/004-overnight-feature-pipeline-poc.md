---
id: "004"
title: Overnight feature-pipeline orchestrator (POC)
status: done
priority: high
area: pipeline
gate: promotion out of POC needs a few real overnight runs finishing end-to-end without human rescue
---

## Description

One orchestrator agent session drives an entire feature lifecycle by
spawning per-stage worker sessions via herdr: plan/spec → independent spec
review (adds acceptance criteria + test plan) → implement-until-spec-tests-
pass → PR → code review → capped comment-addressal loop. Files-as-handoff,
machine-checkable gates between stages, stop-on-failure semantics,
checkpoint/resume.

Full POC spec: [`docs/pipeline/spec.md`](../../pipeline/spec.md) (canonical).
Design draft: [`docs/pipeline/design.md`](../../pipeline/design.md) (v1:
workflow hardcoded in orchestrator prompt, gates judged by orchestrator).
Audits: [`docs/pipeline/audits/`](../../pipeline/audits/).

Under Now because it needs no real-run evidence to *attempt*: every piece it
composes (programmatic spawn/settle via `runner.py`'s patterns, worktree
jobs, gh-driven code review) already works in isolation — the missing thing
is the integration, learned only by running it. Generalizes the auto-fix-PR
idea (`ROADMAP.md` Later) into a full chain.

This issue is itself the dogfood target for
[item 005](005-failure-reaping-phase-2-watchdog.md)'s cousin work and for
`ROADMAP.md` Later § "Autonomous task selection for the pipeline" — the item
that motivated curating this directory in the first place.

## Acceptance

Promotion out of POC status (the actual gate, not a single acceptance
criterion): several real overnight runs completing end-to-end (spec → PR →
review → address-comments) with no human intervention beyond the initial
feature idea and the final merge decision.

## Log

- **2026-08-23**: proposed, spec + design draft landed (PR #26).
- **2026-08-24**: two real dogfood runs completed (PR #27, PR #28). Second
  run pushed the Pi's swap to functionally zero mid-run (design's "keep every
  worker's pane alive until the run ends" cleanup policy meant cumulative,
  not peak, memory cost). Proposed fix:
  [`docs/pipeline/pane-lifecycle-v2-proposal.md`](../../pipeline/pane-lifecycle-v2-proposal.md).
- **2026-08-24 (same night)**: third dogfood run (`20260825T000735Z`, PR
  #29) hit a second bug — `spec.md` at repo root is a single shared path
  every run rewrites in full, so PR #29 conflicted merging against PR #28's
  already-merged `spec.md`. Fixed by moving to a per-run path
  (`docs/pipeline/runs/<run_id>/spec.md`, design.md G-15) and backfilling PR
  #28's spec into that convention.
- **2026-08-25**: the `-s <session_id>` resume mechanism (pane-lifecycle-v2
  open question 1) manually verified — closed a throwaway session's pane,
  reopened via `-s`, confirmed true resume (matching `agent_session.value`,
  not a fork). Proposal then dogfooded as the pipeline's 4th real run
  (`20260825T021919Z`, PR #31, merged `61cc5af`) — the pipeline amended its
  own `design.md`/`orchestrator-prompt.md` (new G-16) and marked the
  proposal `Status: implemented`. This run finished in ~9 minutes (docs-only
  change, no infra hiccups — not evidence runs are now reliably fast in
  general). Open questions 2 (grace window before closing a pane) and 3
  (cross-model `-s` interaction) remain open. **Still outstanding:** the live
  Pi launcher scripts (`~/.local/bin/pipeline-launch-*.sh`, outside this
  repo) still need a manual update to match G-16's per-stage close-and-resume.
- **2026-08-25 (same run)**: pane-lifecycle-v2 run itself surfaced a deeper
  gap — the orchestrator skipped spawning stages 1/2 as separate workers and
  authored `spec.md` v1 and v2 itself in one session, and every gate passed
  anyway, because gates only checked *content shape*, never *which process
  produced it*. Fixed generally as **G-17**: any stage a workflow declares
  independent must be gate-verified by agent name + session id
  (`state.json:stage_sessions`) — meant to survive the later move to a
  declarative per-stage `isolation:` field rather than being re-derived per
  hardcoded stage pair. Landed in the same PR as the "implemented" update
  above.
- **2026-08-25**: G-16 only ever amended the *pipeline orchestrator's* own
  prompt/design (`pl-*` panes) — `runner.py` was untouched, so the separate
  `fitted-pr-review-2..5` routine jobs added the same day hit the exact same
  swap-exhaustion failure mode independently (swap at 95%, 3 of 4 newly added
  jobs' first runs failed). Fixed the same way G-16 reasoned about it,
  generalized to routine jobs: `execute_run` now closes its own pane
  immediately on every settled terminal path, capturing the agent's session
  id first (`RunOutcome.session_id`, now in `history.jsonl`) so a human can
  still resume-and-inspect (PR #42, shipped as commit `3516491`).
- **2026-08-29 to 2026-09-02**: five consecutive overnight runs completed all
  6 stages end-to-end with no human intervention beyond the merge decision —
  PR #49 (worktree GC delete-half, 276 tests), PR #50 (auto-fix PRs standing
  job, 300 tests), PR #56 (gate model, 324 tests), PR #67 (worktree GC, 336
  tests), PR #68 (repository job field, 357 tests). Each run's full report
  is under `~/.local/state/herdr-routines/reports/pipeline-<run_id>.md` on
  the Pi (not checked into this repo). This satisfies the acceptance bar
  above (initially missed when this log was last updated after the 031
  entry below — the log had gone stale while the runs kept succeeding).
- **2026-09-03**: run `20260903T050016Z` (PR #69) hit two separate bugs, both
  filed as their own issues since each is independently actionable: (1) the
  run branched from a checkout 2 days / 6 merged PRs behind `origin/main`
  (including PR #65, the same feature issue 022 asked for), producing a
  conflicting, duplicative PR — filed as
  [`030`](030-sync-repo-before-every-run.md); (2) the run's heartbeat died
  after stage 5 and its stage-6 worker was found still running 19.5 hours
  later, past `deadline_epoch`, with nothing having reaped it — filed as
  [`031`](031-pipeline-stall-watchdog.md), which turns G-4's manual "morning
  checklist" into an automated kill+report watchdog. PR #69 closed manually,
  stuck worker killed manually, no code fix shipped yet for either.
- **2026-09-04**: closing as done. 030 and 031 both shipped (PRs #72, #73),
  and five clean runs already preceded the 09-03 infra failures they fix —
  the orchestrator chain itself is proven; remaining risk is
  environment/infra hardening (tracked as it surfaces, not a reason to keep
  this POC open indefinitely).
