---
id: "054"
title: "move the orchestrator's mechanical steps into code; agents keep only the judgment work"
status: done
priority: high
area: pipeline
gate: phases B and C (the stage loop and stage 4 in code) are tracked in issue 056
---

## Description

The overnight pipeline's orchestrator is an LLM session driven by
`docs/pipeline/orchestrator-prompt.md`. Much of what that prompt asks for has one
correct answer and no judgment in it, yet a model is trusted to do it by following
instructions. When the model is weaker than the prompt assumes (the quota
`fallback_model`) or cuts a corner, these steps fail silently, and the rest of the
system trusts the result:

- **2026-09-28:** the fallback model wrote `state.json:deadline_epoch` a year in the
  past and wrote no heartbeat. The watchdog trusted both and reaped a live run 15 min
  in. (Deadline fixed in PR #131. Tick now owns it.)
- **2026-09-07:** the orchestrator faked `stage_sessions` and ran all 6 stages in its
  own session. Code now *checks* for this (`validate_stage_sessions`, G-17), but the
  model still *writes* the map it's checked against.
- **Issue 050:** the orchestrator ran a bare `herdr-routines`, wandered off to probe
  `~/.local/bin`, and wedged on a permission prompt.

**Principle:** anything deterministic runs in code (the launcher, `tick`, or a
`herdr-routines` subcommand). Agents only do work that needs judgment: writing the
spec, reviewing it, implementing, reviewing the PR, and addressing review comments.

### What the prompt still has the model do mechanically

| Step | Where in `orchestrator-prompt.md` | Kind |
|---|---|---|
| `pick-feature --mark-in-progress`, parse stdout, record `feature_source` | "Inputs", `FEATURE_IDEA` | pure CLI |
| `sync-repo --path $REPO_PARENT` | Prerequisite 1 | pure CLI |
| `herdr worktree create`, parse `$WT`/`$BRANCH` with `jq` | Prerequisite 2 | pure CLI |
| Write the initial `state.json` atomically (`tmpfile && mv`) | Prerequisite 3 | file I/O |
| Heartbeat line per poll cycle | Prerequisite 3 prose | file I/O |
| Spawn `pl-<N>-<RUN_ID>`, prompt with `--wait`, poll loop, settle mapping, quota-marker read, start-race retry | "Worker spawn template" | control loop |
| Record `stage_sessions[N]`, update `current_stage` | Stage loop | file I/O |
| Run `herdr-routines gate --stage N` and act on its exit code | Stage details | control flow |
| Close each worker's pane, save `pl-3`'s session for stage 6's resume | Spawn template step 5 | control flow |
| Stage 4: `git push -u` + `gh pr create` | Stage 4 | pure CLI |
| Deadline check between stages, partial report on overrun | "Pipeline deadline…" | control flow |

## Design (proposal)

Do it in phases, so each one ships and runs on its own.

### Phase A: pre-flight in code

A `herdr-routines pipeline-prepare --run-id … --repo-parent …` subcommand, called by
`scripts/pipeline-launch.sh` **before** it starts the orchestrator agent:

1. `sync-repo` (`_fetch_and_fast_forward`, `repos.py:93`).
2. `pick-feature --mark-in-progress`. On "no open issues", write the terminal report
   `## Outcome: skipped (no_feature)` and exit without starting any agent. **This
   subsumes issue 052**: the phase A PR also flips 052 (`blocked` pending this) to
   `status: done`.
3. Create the shared worktree and branch, and the shared workspace.
4. Write `state.json` atomically, with `deadline_epoch` (from `--deadline-epoch`),
   `feature_source`, `shared_worktree`, `branch`, `shared_workspace`,
   `current_stage: 0` and empty `stage_sessions`.
5. Print the resolved values. The launcher appends them to the prompt header,
   next to `RUN_ID`/`DEADLINE_EPOCH`, and the prompt's Prerequisite section shrinks
   to "these are done; here are your values".

### Phase B: the stage loop in code

A `herdr-routines pipeline-run` subcommand that replaces the orchestrator LLM
session entirely. For each stage it:

1. Starts `pl-<N>-<RUN_ID>` with the stage's model (and for stage 6, resumes
   `pl-3`'s session with `-s`), and records `agent_session.value` into
   `stage_sessions[N]` **itself**. The G-17 fabrication risk disappears because no
   model writes the map.
2. Sends the stage prompt (the "Stage Details" sections, moved into per-stage prompt
   files) and waits with the same marker-polling loop `pipeline-launch.sh` and
   `runner.py` already use (quota fast-fail, start-race retry, settle mapping).
3. Runs `herdr-routines gate --stage N` in-process. On failure it aborts with a
   partial report.
4. Updates `current_stage` and the heartbeat, and closes the worker's pane.
5. Checks the deadline between stages and writes the partial report on overrun.

The launcher then runs `pipeline-run` instead of starting an orchestrator agent.
The heartbeat becomes a code-written liveness signal, so the watchdog stops
depending on a model's diligence for both of its signals.

### Phase C: stage 4 in code

`git push -u` + `gh pr create` from the spec's title and summary, with the Gate 4
checks (PR exists on the right head, and the issue-close commit is present) run in
the same function. No agent is needed for stage 4.

### Non-goals

- Changing *what* the judgment stages do (their prompts move to files unchanged).
- Making the pipeline decide *when* to run. That's still `tick` and cron.
- Replacing `herdr` as the agent runtime.

## Acceptance criteria

**Phase A**
1. With an eligible issue, `pipeline-prepare` leaves a synced `$REPO_PARENT`, a
   claimed issue, a worktree on `auto/pipeline-<RUN_ID>`, and a `state.json` whose
   `deadline_epoch` equals the passed value and whose `feature_source` names the
   claimed file. Test: `test_pipeline_prepare_sets_up_run`
2. With no eligible issue, it writes `## Outcome: skipped (no_feature)`, starts no
   agent, and tick records `skipped`, not `failed`. Test:
   `test_pipeline_prepare_no_feature_skips_without_agent`
3. A `sync-repo` failure writes a `failed (repo_sync_failed)` report and starts no
   agent. Test: `test_pipeline_prepare_fails_loud_on_sync_error`

**Phase B**
4. `stage_sessions[N]` is written by `pipeline-run` from the started agent's real
   `agent_session.value`. The map in `state.json` is never taken from model output.
   Test: `test_pipeline_run_records_real_stage_sessions`
5. A failing `gate --stage N` aborts before stage N+1 with a partial report naming
   the failed gate. Test: `test_pipeline_run_aborts_on_gate_failure`
6. The heartbeat file advances on every poll, written by code. Test:
   `test_pipeline_run_writes_heartbeat`
7. Past `deadline_epoch`, the in-flight stage is allowed to finish, the remaining
   stages are skipped, and `## Outcome: partial (deadline exceeded)` is written.
   Test: `test_pipeline_run_partial_on_deadline`
8. A quota marker on a worker's screen ends the run as `failed (quota_exhausted)`,
   so tick's existing `fallback_model` retry still applies. Test:
   `test_pipeline_run_quota_marker_fast_fails`

**Phase C**
9. Stage 4 opens the PR from the branch and passes Gate 4 with no agent started.
   Test: `test_pipeline_stage4_opens_pr_without_agent`

## Why these tests

- 1–3 pin that setup happens before any model is involved, and that an empty
  backlog costs nothing (issue 052's goal).
- 4–6 pin the three signals the rest of the system trusts (stage independence,
  liveness, gate verdicts): code produces them, not model output.
- 7–8 pin the deadline and quota behavior tick and the watchdog already depend on.
- 9 pins that the one purely mechanical stage needs no model at all.

## Log

- **2026-09-30:** phase A shipped as `herdr-routines pipeline-prepare` (see
  `src/herdr_routines/pipeline_prepare.py` and `docs/pipeline/runs/20260930T050000Z`),
  which also closes 052. Phases B and C were filed as issue 056, which now carries
  acceptance criteria 4–9; this issue is `done` for phase A.
- **2026-09-29:** filed after the 09-28 run, where the fallback model computed
  `deadline_epoch` a year off and the watchdog reaped a live run. The deadline was
  fixed first, on its own, in PR #131 (tick computes it and records it in history,
  and the watchdog prefers that). Principle from the human: "move everything that
  can be automated into code and let the agents do the work they are needed for".
