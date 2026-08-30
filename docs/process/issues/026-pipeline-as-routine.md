---
id: "026"
title: "Unify routines + pipeline into one gated-workflow model"
status: open
priority: medium
area: pipeline
gate: PR #58 (issue close-on-merge + roadmaps) merged; issue 006 (jobs.d) landed
---

## Description

Make the overnight **pipeline** and a **routine** two instances of the same
"gated workflow" engine, not two separate subsystems. Investigation
(2026-08-30, parked in `ROADMAP.md`) found the pipeline is *not* a distinct
engine today: it is a single `herdr` agent whose
`docs/pipeline/orchestrator-prompt.md` instructs it to spawn `pl-1..pl-6`
workers via its own `herdr` tool calls. There is **no Python stage/spawn loop**
in `src/herdr_routines/` — `pick_feature.py` is the stage-0 selector, `ps.py`
only *reads* `pl-<N>-<run_id>` agent names to display "stage N/6", and
`tick.py` runs exactly one agent per job break out nothing that drives stages.
So:

- **Routine** = 1-step workflow: one agent, one worktree, one report, bounded by
  `timeout_ms`. Runs when `jobs.yaml` cron fires, driven by the tick loop
  (systemd `herdr-routines.timer` → `.service` → `tick`).
- **Pipeline** = N-step workflow: one agent whose *prompt* spawns sub-agents +
  gates between stages, with its own `deadline_epoch`, `state.json`, reports.
  Launched today via transient `systemd-run --on-calendar` (not a `jobs.yaml`
  entry); design.md defers owning it as a scheduled job ("Option C", not-v1).

Both already schedule on **systemd user timers**; the only real scheduler
difference is a persistent cron-evaluating tick loop (routines) vs. a one-shot
`systemd-run` unit (pipeline). The unifying primitive is the **gated workflow**:

| | Routine | Pipeline |
|-|---------|----------|
| steps | 1 agent | N agents (prompt-driven) |
| gate | optional `checks` (issue 025) | per-stage gate (Gate 4 etc.) |
| schedules on | recurring tick | transient one-shot |
| container | `jobs.yaml`/`jobs.d` entry | works in its own agent presence |

## Goal / first implementable increment

**"Pipeline as a routine job."** Provide code-level support to run the
orchestrator through the existing routine scheduler — the deferred design.md
"`herdr-routines run orchestrator` job wrapper" — so a `jobs.yaml` entry with a
long `timeout_ms` and a `cron:` launches the orchestrator (with
`orchestrator-prompt.md`) via the normal tick path instead of a separate
`systemd-run`. This unifies *scheduling* with zero change to the orchestrator
agent's prompt-based stage model.

Concretely that means `tick.py`/`config.py` supporting the named library entry:
a job whose `prompt` is `@docs/pipeline/orchestrator-prompt.md` (or an explicit
`pipeline: true` flag) is allowed a long `timeout_ms`, skips the `no_report`
guard (it writes `$PIPELINE_REPORT`, not `$ROUTINE_REPORT`), and is exempt from
the short systemd `TimeoutStartSec` assumption.

**Forward path (NOT in this first increment, design context only):** promote
stage gates out of the prompt into declarative job-definition `checks:`
("Code-level pipeline gates" parking-lot idea) once the pipeline moves out of
prompt-hardcoded stages (issue 013's `workflows/pipeline.yaml` or per-stage gate
fields). Routines-with-`checks` and pipeline-stages-with-gates then literally
share one gate primitive.

## Config / design shape (draft — for stage-1 review)

```yaml
# jobs.d/orchestrator.yaml      (dir mode, issue 006) — or a jobs.yaml block
- name: nightly-pipeline
  enabled: true
  cron: "0 2 * * *"
  prompt: "@docs/pipeline/orchestrator-prompt.md"   # file indirection, or inline
  timeout_ms: 25200000                               # 7h — pipeline deadline
  start_timeout_ms: 120000
  on_missed: log
  # optional: report target/prefs default to pipeline semantics
```

`default_config_path()`/consumer changes stay minimal — everything routes through
`cli.py:_load_config_or_exit`.

## Acceptance criteria

Each ends `Test: <name>`:

1. A routine job with `prompt: "@<path>.md"` (or `pipeline: true`) is accepted by
   `validate` and does **not** trip the `$ROUTINE_REPORT` `no_report` guard — it
   is recognized as a pipeline-style job that writes its own report.
   `Test: test_pipeline_job_skips_no_report_guard`
2. `tick` runs such a job to completion with a long `timeout_ms` (e.g. a stubbed
   7h budget), spawning the orchestrator agent once and settling, without the
   systemd-short-timeout misfire or the `agent_prompt_failed` path.
   `Test: test_tick_runs_pipeline_job_to_completion`
3. `validate`'s systemd-timeout calculation accepts the long pipeline budget and
   does not warn it's over the unit's `TimeoutStartSec` when the unit is also
   raised (runbook/`deploy` covers the unit bump).
   `Test: test_validate_systemd_timeout_for_pipeline_job`
4. The orchestrator's write path (`$PIPELINE_REPORT` + `state.json` +
   `docs/pipeline/runs/<id>/spec.md`) is honored when launched via `tick` — i.e.
   the job runs in a worktree whose repo/base is the target, and the orchestrator
   prompt functions identically to a `systemd-run` launch.
   `Test: test_tick_pipeline_job_report_and_state_written`
5. `status` / `scheduled` / `ps` show the pipeline as a normal job (it appears in
   the schedule and `ps` "stage N/6" display still derives from `pl-` agents).
   `Test: test_ps_shows_pipeline_job_stages`
6. Legacy behavior intact: regular one-agent routines are unaffected; a default
   `prompt` (non-pipeline) still writes `$ROUTINE_REPORT` and the `no_report`
   guard still applies.
   `Test: test_regular_routine_guard_unchanged`

## Why these tests

- 1–2 pin the scheduling unification (pipeline runs as a routine via `tick`).
- 3 pins the long-budget/unit interplay (the one real config friction).
- 4 pins that nothing is lost vs. the `systemd-run` launch (reports/spec/state).
- 5–6 pin no-regression: pipeline is visible in normal tools and ordinary
  routines keep their behavior.

## Non-goals (this increment)

- Not promoting stage gates to code yet (that's the "Code-level pipeline gates"
  roadmap item).
- Not changing the orchestrator's prompt-based 6-stage model.
- Not touching `src/herdr_routines/auto_fix.py` / gate internals from issue 025.

## Log

- **2026-08-30**: investigation established the pipeline has no Python
  stage/spawn loop (verified: `tick.py` runs one agent/job; `ps.py` only reads
  `pl-<N>-<run_id>` names; no module drives stages) and both schedule on systemd
  user timers. Parked as a ROADMAP idea ("Unify routines + pipeline as one
  gated-workflow engine"). Written up here as a spec with the first increment
  scoped to "pipeline as a routine job."
