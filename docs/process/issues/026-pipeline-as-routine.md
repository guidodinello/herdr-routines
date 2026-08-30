---
id: "026"
title: "Unify routines + pipeline into one gated-workflow model"
status: open
priority: medium
area: pipeline
gate: PR #58 (issue close-on-merge + roadmaps) merged; issue 006 (jobs.d) landed
---

## Description

Make the overnight **pipeline** and a **routine** schedule from the same engine,
not from two unrelated launchers. Investigation (2026-08-30) confirmed the
pipeline is *not* a distinct code engine today: it is a single `herdr` agent
whose `docs/pipeline/orchestrator-prompt.md` instructs it to spawn `pl-1..pl-6`
workers via its own `herdr` tool calls. There is **no Python stage/spawn loop**
in `src/herdr_routines/` — `pick_feature.py` is the stage-0 selector, `ps.py`
only *reads* `pl-<N>-<run_id>` names to display "stage N/6", and `tick.py` runs
exactly one agent per job. The only real structural difference between a routine
and the pipeline today is **who schedules them**:

- **Routine** (1 step, 1 agent) is driven by the persistent 5-minute tick loop
  (systemd `herdr-routines.timer` → `.service` → `tick`, `Type=oneshot`).
- **Pipeline** (N prompt-driven steps + gates) is launched by a **one-shot,
  detached, transient `systemd-run` unit** (design.md:283-295) — *precisely so
  it does not block the tick cadence*. It has its own `deadline_epoch`,
  `state.json`, and mirror-report.

So the unification being targeted here is real but *narrow and scheduling-only*:
**move pipeline scheduling into `tick`.** It is NOT an immediate engine
unification — stages stay hardcoded in `orchestrator-prompt.md` either way. Real
engine unification (a Python stage driver, declarative `workflows/<name>.yaml`,
code-level stage gates) is tracked separately as issue 013 / the
"code-level pipeline gates" roadmap item, not by this issue.

> Scope honesty: this increment advances the *scheduler*, not the engine.
> Selling it as "one gated-workflow engine" invites scope creep. The unification
> claim is a long-term direction; this issue is its first, narrow step.

## First increment: pipeline is a dispatched (detached) job, not a blocking routine

The core design decision: **`tick` dispatches the pipeline detached and returns.
It does not block on it.** A 5-minute `Type=oneshot` tick holding `tick.lock`
(flock, tick.py:64-83) must not run an orchestrator synchronously — with a
multi-hour budget that would freeze every other job (`babysit-prs`,
`repo-hygiene`, …) for the whole night (~84 no-op timer fires, no other job runs).
This is exactly why the pipeline uses a separate transient unit today.

Mechanism, on cron fire:

1. `tick` recognizes the job by `kind: pipeline`, writes a `running` record
   (with the fresh `run_id`).
2. It launches a **detached** transient unit — `tick` *generates* the same
   `systemd-run --user --unit=herdr-pipeline-<run_id>` invocation a human runs
   today (design.md:283-295) — injecting `HERDR_ENV=1` into the orchestrator's
   pane/workspace and pinning `$PIPELINE_REPORT` to
   `default_reports_dir()/f"{run_id}.md"` so `tick` and the orchestrator agree on
   the report path.
3. `tick` returns immediately. Subsequent ticks see the live `rt-nightly-pipeline`
   agent (reusing the existing `_live_agent_exists` guard, tick.py:1194) →
   "skipped (already running)". When the agent is gone, a later tick reconciles
   completion from `state.json` (`current_stage`, terminal) + the report file,
   writing `done` / `failed` / `interrupted_unknown` (extending
   `find_stale_running` semantics against the deadline).

The existing codebase already has the right shape for "a job that isn't a plain
single-agent run": `_process_job` branches to `_process_gated_job` when
`job.checks is not None` (tick.py:1032). A `kind: pipeline` job gets a **sibling
`_process_pipeline_job`** — not five special-cases threaded through
`execute_run` / `_process_job`.

### Mode discriminator: an explicit `kind:` enum, not overloaded flags

Today `checks is not None` is an *implicit* mode switch (tick.py:1032). Replace
that intuition with one explicit, single-source-of-truth field:

- `kind: routine` (default) — plain 1-agent job, has `checks` optionally.
- `kind: gate` — 1-agent job with `checks` (the old implicit gate mode).
- `kind: pipeline` — detached, deadline-bounded orchestrator dispatch (this issue).

`prompt` *source* (inline string vs file) is a **separate axis** (`prompt_file:`):
a plain routine may legitimately want a file-backed prompt without becoming a
pipeline. Do not infer mode from prompt source.

## Config / design shape (draft — for review)

```yaml
# jobs.d/orchestrator.yaml   (dir mode, issue 006) — or a jobs.yaml block
- name: nightly-pipeline
  kind: pipeline                       # explicit mode; SSOT discriminator
  enabled: true
  cron: "0 2 * * *"
  catch_up_minutes: 0                  # never fire a 7h run late into the workday
  repo: ~/.local/state/herdr-routines/repos/herdr-routines   # the PARENT clone, used as-is
  prompt_file: docs/pipeline/orchestrator-prompt.md          # I/O convenience axis
  deadline_ms: 25200000                # orchestrator wall budget → deadline_epoch
  start_timeout_ms: 120000
  env:                                 # injected into the orchestrator's pane/workspace
    HERDR_ENV: "1"
  on_missed: log
```

Design notes:
- **No blocking `timeout_ms`.** `deadline_ms` is the orchestrator's own wall-clock
  budget; `tick` does not wait it out. This removes the whole systemd
  `TimeoutStartSec` conflict that a blocking model would create.
- **`workspace: worktree|root` does not apply** to `kind: pipeline` — the
  orchestrator runs in the parent clone and creates its own
  `auto/pipeline-<run_id>` worktree (`orchestrator-prompt.md` Prerequisite 1;
  design.md:337-338). Document that; do not force-fit it.
- `default_config_path()`/consumer wiring stays minimal, but the behavioural
  work lives in `tick` (`_process_pipeline_job`) — not in `cli.py` config
  loading. `_load_config_or_exit` only loads config; it is not a shortcut for
  the runner changes here.

## Report semantics: redirect, don't skip

`$ROUTINE_REPORT` / `$PIPELINE_REPORT` are placeholder strings today;
`substitute_prompt` (runner.py:267-274) only knows `$ROUTINE_*`, so the
orchestrator's `$PIPELINE_REPORT` passes through untouched and it computes the
path itself — a run_id namespace mismatch with the routine path.

Fix by **redirecting** to one name, then **keeping** the `no_report` guard:

1. Teach `substitute_prompt` a single `$REPORT`, substituted for every job kind
   to `default_reports_dir()/f"{run_id}.md"` (keep `$ROUTINE_REPORT` as a
   back-compat alias). This also unifies the run_id namespace between tick and
   the orchestrator.
2. **Do not** skip the "report exists and non-empty" check for pipeline jobs. The
   guard is the *only* mechanism that turns "agent settled `idle` having done
   nothing" into `failed` (runner.py:617-623) — and the pipeline has a documented
   silent-clean-exit failure mode (a killed orchestrator writes nothing,
   design.md:347-356). The pipeline needs that detector *more*, not less.
3. The one legitimate content difference: tolerate a **partial** report
   (deadline exceeded) as non-failure. That is a report-content rule, not a reason
   to disable the guard.

## Hard runtime prerequisite: `HERDR_ENV=1`

`herdr.py` today has **zero env-injection support**; the orchestrator is dead
without `HERDR_ENV=1` (it cannot drive `herdr` at all, design.md:131-134) —
without it, every `herdr` call wedges `blocked` at 02:00. Plumb an env-injection
path through the launch (pane/workspace creation) and pin `$PIPELINE_REPORT`.
This is a hard prerequisite, not a detail — it gets its own acceptance criterion.

## Acceptance criteria

Each ends `Test: <name>`:

1. `validate` accepts a `kind: pipeline` job and **suppresses** the missing-`$REPORT`
   warning for it (`cli.py:398-414` warns when no `$ROUTINE_REPORT` and no
   `checks`; must not fire for `kind: pipeline` with `prompt_file`).
   `Test: test_validate_pipeline_job_suppresses_report_warning`
2. `tick`, on a `kind: pipeline` cron fire, **dispatches a detached transient unit**
   (matching the human `systemd-run` invocation, with `env.HERDR_ENV=1` and the
   report path pinned) and **returns immediately** — it does not block for
   `deadline_ms`. A stubbed unit short-circuits so the test does not wait hours.
   `Test: test_tick_dispatches_pipeline_detached`
3. **Concurrency (make-or-break):** other due jobs are still evaluated and run
   during a pipeline night (the oneshot tick does not wedge while the pipeline
   runs detached).
   `Test: test_other_jobs_still_run_during_pipeline`
4. `substitute_prompt` rewrites `$REPORT` (and back-compat `$ROUTINE_REPORT`) to
   `default_reports_dir()/f"{run_id}.md"` for every kind, and the report guard is
   **redirected, not skipped** — for a pipeline job it still marks `failed` when
   no/empty report after a non-deadline exit, and tolerates a **partial** report
   only when the deadline was exceeded.
   `Test: test_pipeline_report_guard_redirected_and_partial_tolerant`
5. `HERDR_ENV=1` reaches the orchestrator's pane/workspace (assert via the
   launch env plumbing); without it the launch is blocked, not silently degraded.
   `Test: test_pipeline_launch_injects_herdr_env`
6. A pipeline job sets `catch_up_minutes: 0` so a missed 02:00 occurrence does
   **not** launch the 7h run mid-morning.
   `Test: test_pipeline_job_skips_catchup`
7. Deadline vs. kill: the orchestrator gets enough wall-clock past `deadline_epoch`
   to write the partial report + `notification show` before any watchdog/timeout
   kill — i.e. `deadline_ms` + margin, not `timeout_ms == deadline_ms`.
   `Test: test_pipeline_deadline_leaves_partial_report_margin`
8. `scheduled` lists the pipeline's cron, and `status` renders the
   `rt-nightly-pipeline` orchestrator row sanely while a run is in flight (and
   `skipped (already running)` on overlap).
   `Test: test_status_renders_inflight_orchestrator`
9. Resume: the same-RUN_ID relaunch path (design.md:349-351) still works when
   `tick` owns launching (or is explicitly replaced/reworked in this issue).
   `Test: test_pipeline_resume_same_run_id`
10. `gc` keeps excluding `auto/pipeline-*` now that the branch originates from a
    scheduled job (design.md G-14).
    `Test: test_gc_excludes_pipeline_worktrees`
11. Legacy behavior intact: `kind: routine` (and default) jobs still write
    `$REPORT`/`$ROUTINE_REPORT` and the `no_report` guard still applies.
    `Test: test_regular_routine_guard_unchanged`

## Why these tests

- 1–3 pin the core fix: pipeline becomes a **detached dispatched** job that does
  not freeze the tick — concurrency (3) is the make-or-break criterion that the
  original synchronous design could not meet.
- 4 pins the report guard is **redirected, not skipped**, so the
  silent-orchestrator-death detector survives.
- 5–7 pin the hard prerequisites the original spec ignored (`HERDR_ENV`, catch-up,
  deadline-vs-kill margin).
- 8–10 pin the operational invariants (status rendering, resume, gc exclusion).
- 11 pins no-regression for ordinary routines.

## Non-goals (this increment)

- Not promoting stage gates to code — that's the "Code-level pipeline gates"
  roadmap item / issue 013 (`workflows/<name>.yaml` + a Python stage driver).
- Not changing the orchestrator's prompt-based 6-stage model or stage internals.
- Not touching `src/herdr_routines/auto_fix.py` / gate internals from issue 025.
- Not building `workspace: root` semantics for the pipeline (it owns its own
  worktree; see design notes).

## Log

- **2026-08-30**: investigation established the pipeline has no Python
  stage/spawn loop (verified: `tick.py` runs one agent/job; `ps.py` only reads
  `pl-<N>-<run_id>` names; no module drives stages) and both schedule on systemd
  user timers. Parked as a ROADMAP idea.
- **2026-08-30**: drafted v1 scoped as "pipeline as a blocking routine";
  independent review (sonnet-5, `docs/process/issues/026-review-sonnet5.md`)
  returned SHAKY: the synchronous 7h `tick` blocks the oneshot cadence; it
  fights `execute_run` in ~5 places; skipped guard hides the silent-death
  detector; `HERDR_ENV` plumbing and report-path pinning are missing; AC #4
  contradicted design.md and AC #5 was vacuous. Revised here to
  dispatch-and-detach with a `kind:` enum, a redirected (not skipped) report
  guard, `HERDR_ENV` plumbing, `catch_up_minutes: 0`, and rewritten/missing
  criteria. Re-review pending.
