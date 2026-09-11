---
id: "052"
title: "tick launches the pipeline orchestrator even when pick-feature has nothing to build"
status: open
priority: low
area: pipeline
---

## Description

`tick._process_pipeline_job` (`src/herdr_routines/tick.py:1616`) decides whether
to launch the overnight orchestrator from **three signals only**: no run is
already open, `ensure_repo(job)` succeeds, and `schedule.decide()` returns
`Decision.RUN`. On the `Decision.RUN` path (`tick.py:1791`) it goes straight
from `ensure_repo` (`tick.py:1799`) to `_build_pipeline_launch_argv` and
`launch_pipeline(argv)` (`tick.py:1826`) — a detached `systemd-run --user` unit.

Whether there is actually a feature to build is **not** one of those signals.
`pick-feature` is **stage 0 inside the orchestrator prompt**
(`docs/pipeline/orchestrator-prompt.md:14` — `uv run herdr-routines pick-feature
--issues-dir docs/process/issues --mark-in-progress`). So "the backlog is empty"
is discovered only *after*:

1. the `systemd-run` unit is registered (`herdr-pipeline-<run_id>`),
2. `scripts/pipeline-launch.sh` has created a `herdr` workspace + pane,
3. the `pl-1` orchestrator agent (opencode, a paid/quota'd model) has started
   and run `sync-repo` + `pick-feature`.

`run_pick_feature` then returns `1` with `no open issues` on stderr
(`src/herdr_routines/pick_feature.py:511-512`), and the prompt's contract is
"**stop and write a report saying so** — do not fabricate a feature idea"
(`orchestrator-prompt.md:24`).

### Cost of a no-op launch

- A `herdr` workspace + worktree that G-14 `gc` now has to reclaim
  (`docs/process/issues/045`, `048`).
- A wasted orchestrator agent session (model tokens, quota).
- Notification noise (`on_missed: notify` is set on the deploy example; a
  launched-then-bailed run also notifies on its terminal state).
- A history entry whose state is **ambiguous**: the prompt says "write a report
  saying so" but does not pin the `## Outcome:` marker for this branch.
  `_classify_pipeline_outcome` (`tick.py:1527`) maps `ok*` → `done`, `failed*`
  → `failed`, and anything else → `interrupted_unknown / outcome_marker_*`. A
  "nothing to do" night can therefore land as `done`, `failed`, or
  `interrupted_unknown` depending on how the model phrases the stub — and the
  digest (`docs/process/issues/046`) surfaces whichever it is.

### Why it hasn't bitten hard yet

The `docs/process/issues/` backlog almost always has an eligible item, so
`pick-feature` nearly always succeeds — tonight's `20260910T050000Z` run picked
issue 049 itself and produced PR #116. This is a latent gap, not an active
outage. It becomes real as the backlog is drained, or on a night when every
open issue is already claimed / has an open `auto/pipeline-*` PR
(`docs/process/issues/028`).

## Design (proposal)

Add a **read-only feasibility precheck** to the `Decision.RUN` path in
`_process_pipeline_job`, after `ensure_repo(job)` (so the issues dir and the
`gh pr list` exclusion reflect fresh `origin/<base>`) and before
`_build_pipeline_launch_argv`:

- Run the same selection `pick-feature` would — **without** `--mark-in-progress`
  — against `job.repo / "docs/process/issues"` and `job.repo`. Preferred: call
  `run_pick_feature(...)` (or a thin shared `select_next`-level helper) in-
  process rather than shelling out, capturing its return code and discarding
  stdout.
- If it reports nothing to build (`rc == 1` / `select_next` returns `None`),
  **do not launch**. Append a terminal-ish `HistoryRecord(state="skipped",
  extra={"reason": "no_feature"})` — mirroring the existing
  `skipped (agent_name_live)` record at `tick.py:1737-1746` — and return
  `f"{job.name}: skipped (no_feature)", False`. No notification (a skipped
  night with an empty backlog is not a failure; `on_missed` semantics are for
  the catch-up window, not this).
- If it finds a candidate, launch exactly as today. The orchestrator's stage 0
  still runs `pick-feature --mark-in-progress` for real — the precheck must not
  take the claim, must not run `reclaim_stale_claims` (that is gated on
  `mark=True` and is stage 0's job), and must not write anything to
  `claims_path` or the repo.

### Known tradeoff — stale-claim false negative

`reclaim_stale_claims` only runs under `--mark-in-progress`
(`pick_feature.py:466`). A read-only precheck therefore treats a stale claim as
still-claimed and could skip a night where stage 0's reclaim *would* have freed
an issue. This is acceptable: stale claims are rare (a prior run that died
before opening a PR), the lease expiry is short relative to the nightly cadence,
and the next night's run reclaims it. Document it; do not replicate the reclaim
logic in tick. If it proves annoying, the follow-up is a dedicated
`pick-feature --check` mode that runs the reclaim read-only, not widening the
tick-side precheck.

### Non-goals

- **Not** moving `pick-feature` / the claim out of the orchestrator. Stage 0
  keeps owning `--mark-in-progress`, the `state.json` `feature_source` write,
  and the stale-claim reclaim. This issue only adds a *guard* that avoids
  spinning up the orchestrator to learn something tick can cheaply check first.
- **Not** the "Code-level pipeline gates" Roadmap bullet (declarative per-stage
  `checks:` for the pipeline). This is a single pre-launch feasibility check,
  not a stage driver.
- **Not** changing `schedule.decide()` or the `Decision.MISSED` /
  `outside_catch_up_window` path.
- **Not** pinning the orchestrator's "nothing to do" `## Outcome:` marker —
  that prompt-contract tightening is worth doing but is separate (and mostly
  moot once tick stops launching these).

## Acceptance criteria

Each ends `Test: <name>`.

1. On the `Decision.RUN` path, when a read-only feature selection against the
   job's issues dir yields nothing, `_process_pipeline_job` does **not** call
   `launch_pipeline`, appends a `HistoryRecord(state="skipped",
   extra={"reason": "no_feature"})`, and returns `"<job>: skipped
   (no_feature)"`. Test: `test_pipeline_skips_launch_when_no_feature`
2. When the selection yields a candidate, launch happens exactly as today
   (argv unchanged, `running` record written after a successful launch). The
   precheck writes nothing to `claims_path` and makes no `--mark-in-progress`
   call. Test: `test_pipeline_launches_when_feature_available`
3. The precheck runs **after** `ensure_repo(job)` — a test where `origin/main`
   has an issue freshly marked `status: done` (so the local clone is stale
   until `ensure_repo` fast-forwards) must skip, proving the check reads
   post-sync state. Test: `test_no_feature_check_runs_after_ensure_repo`
4. A `skipped (no_feature)` record is **not** treated as an open run by
   `_open_pipeline_run` and does **not** trigger a `_notify` call. The next
   tick after a candidate appears launches normally. Test:
   `test_no_feature_skip_is_not_an_open_run`
5. Regression: `Decision.NOT_DUE`, `Decision.MISSED` (`outside_catch_up_window`
   + `on_missed: notify`), the already-open-run reconcile path, and the
   `repo_sync_failed` / `launch_failed` paths are all unchanged. Test:
   `test_pipeline_dispatch_regression_unchanged`

## Why these tests

- 1–2 pin the new guard and, crucially, that it stays read-only (the claim and
  reclaim remain stage 0's — replicating them in tick is the tempting wrong
  turn).
- 3 pins ordering: a precheck before `ensure_repo` would read stale state and
  either skip a real feature or launch for a done one.
- 4 pins that a skip is inert — not an open run, not a notification, not a
  digest failure.
- 5 pins that only the empty-backlog launch decision moved.

## Log

- **2026-09-10**: filed after PR #116 (issue 049) merged and the Pi was
  migrated to `kind: gated`. Noticed while deciding whether to trigger a manual
  `feature-pipeline` run: `feature-pipeline` launches on cron alone, and
  `pick-feature` is stage 0 *inside* the orchestrator, so an empty backlog costs
  a full orchestrator spin-up before the run bails. Scoped narrowly per the
  human: a read-only tick-side precheck, not moving the claim out of stage 0,
  and explicitly not the declarative-pipeline-gates Roadmap item.
