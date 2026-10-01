---
id: "056"
title: "orchestrator stage loop and PR stage in code (issue 054 phases B and C)"
status: done
priority: high
area: pipeline
gate: phase A (issue 054) shipped and ran a full overnight; phase B and C can then be split into their own PRs
---

## Description

Phase A of [issue 054](054-orchestrator-mechanical-steps-to-code.md) moved the
orchestrator's *pre-flight* into code: `herdr-routines pipeline-prepare` now syncs the
parent clone, picks and claims the feature, creates the shared worktree and workspace,
and writes `state.json` before any agent exists. The orchestrator LLM session still
drives everything after that — and the two remaining mechanical phases are where the
same fabrication risk lives.

Split out of 054 so the pre-flight could ship and run on its own. 054 is closed by the
phase A PR; the rest of its scope is tracked here.

### Phase B: the stage loop in code

A `herdr-routines pipeline-run` subcommand that replaces the orchestrator LLM session
entirely. For each stage it:

1. Starts `pl-<N>-<RUN_ID>` with the stage's model (and for stage 6, resumes `pl-3`'s
   session with `-s`), and records `agent_session.value` into `stage_sessions[N]`
   **itself**. The G-17 fabrication risk disappears because no model writes the map.
2. Sends the stage prompt (the "Stage Details" sections, moved into per-stage prompt
   files) and waits with the same marker-polling loop `pipeline-launch.sh` and
   `runner.py` already use (quota fast-fail, start-race retry, settle mapping).
3. Runs `herdr-routines gate --stage N` in-process. On failure it aborts with a partial
   report.
4. Updates `current_stage` and the heartbeat, and closes the worker's pane.
5. Checks the deadline between stages and writes the partial report on overrun.

`scripts/pipeline-launch.sh` then runs `pipeline-run` instead of starting an
orchestrator agent. The heartbeat becomes a code-written liveness signal, so the
watchdog stops depending on a model's diligence for both of its signals.

### Phase C: stage 4 in code

`git push -u` + `gh pr create` from the spec's title and summary, with the Gate 4 checks
(PR exists on the right head, and the issue-close commit is present) run in the same
function. No agent is needed for stage 4.

## Acceptance criteria

**Phase B**

1. `stage_sessions[N]` is written by `pipeline-run` from the started agent's real
   `agent_session.value`. The map in `state.json` is never taken from model output.
   Test: `test_pipeline_run_records_real_stage_sessions`
2. A failing `gate --stage N` aborts before stage N+1 with a partial report naming the
   failed gate. Test: `test_pipeline_run_aborts_on_gate_failure`
3. The heartbeat file advances on every poll, written by code. Test:
   `test_pipeline_run_writes_heartbeat`
4. Past `deadline_epoch`, the in-flight stage is allowed to finish, the remaining stages
   are skipped, and `## Outcome: partial (deadline exceeded)` is written. Test:
   `test_pipeline_run_partial_on_deadline`
5. A quota marker on a worker's screen ends the run as `failed (quota_exhausted)`, so
   tick's existing `fallback_model` retry still applies. Test:
   `test_pipeline_run_quota_marker_fast_fails`

**Phase C**

6. Stage 4 opens the PR from the branch and passes Gate 4 with no agent started. Test:
   `test_pipeline_stage4_opens_pr_without_agent`

## Why these tests

- 1–3 pin the three signals the rest of the system trusts (stage independence,
  liveness, gate verdicts): code produces them, not model output.
- 4–5 pin the deadline and quota behavior tick and the watchdog already depend on.
- 6 pins that the one purely mechanical stage needs no model at all.

## Non-goals

- Changing *what* the judgment stages do (their prompts move to files unchanged).
- Making the pipeline decide *when* to run. That's still `tick` and cron.
- Replacing `herdr` as the agent runtime.

## Log

- **2026-09-30:** filed from issue 054 when its phase A PR landed, carrying 054's phase
  B and C design and acceptance criteria 4–9 verbatim. 054 is marked `done` with a gate
  pointing here.
