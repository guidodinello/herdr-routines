---
id: "031"
title: "Pipeline stall watchdog: detect deadline overrun and kill orphaned workers"
status: done
priority: high
area: pipeline
---

## Description

`docs/pipeline/orchestrator-prompt.md` already documents orchestrator
self-death as a known, accepted gap: "Orchestrator self-death remains silent
(no report) — morning checklist: no report file ⇒ `systemctl --user status`
+ `herdr agent list`" (design:150, G-4). That checklist is manual and
human-triggered — nothing automatically notices, and nothing reaps the
worker the dead orchestrator was waiting on.

**Hit on 2026-09-03**: pipeline run `20260903T050016Z` (PR #69) reached
stage 5 (code review posted, `state.json:current_stage: 5`), then its
heartbeat log (`/tmp/pipeline_resume_20260903T050016Z.log`) stopped dead
after `stage 5 poll 05:44:10Z` — no further polls. Stage 6 (address-pr-
comments) *had* started: an `opencode -m opencode/x-preview-f-free -s
ses_f9a578e4...` process (the resumed stage-3 session, per G-16) was found
still running **19.5 hours later**, well past the run's 7-hour
`deadline_epoch`, spinning at ~105% CPU with no progress. Nothing had
noticed or killed it; it was found and killed manually in-session with
Claude Code. The PR sat with an unaddressed review the entire time.

This generalizes beyond this one run: any stage's worker can outlive its
orchestrator (which owns polling `--wait` and enforcing `deadline_epoch`)
and nothing external ever reaps it — a silent resource leak (a stuck
opencode process holds real CPU/RAM on the Pi indefinitely) in addition to
the stalled PR.

## Design (proposal)

A watchdog, run as its own `jobs.d/` routine (e.g. `cron: "*/15 * * * *"` —
frequent, since the cost of a check is cheap and the cost of an unnoticed
19-hour leak is not):

- Enumerate in-flight pipeline runs: `state.json` files under each
  `~/.herdr/worktrees/herdr-routines/auto-pipeline-*/state.json` (or a
  dedicated index, if one is added) that have no matching terminal report at
  `~/.local/state/herdr-routines/reports/pipeline-<run_id>.md`.
- For each, read `deadline_epoch` and the heartbeat log
  (`/tmp/pipeline_resume_<run_id>.log`)'s last line timestamp.
- If `now > deadline_epoch` (give a grace margin, e.g. +30 min, to avoid
  racing a run that's about to finish its own deadline check and write a
  partial report per orchestrator-prompt.md:152):
  - `herdr agent list | jq` to find any `pl-<N>-<run_id>` agent still
    reporting `working`/`busy`.
  - Kill it: `herdr agent stop`/`herdr pane close` (whichever primitive
    actually terminates the underlying process — verify empirically, since
    `pane close` alone may not kill a hung subprocess).
  - Write a terminal report noting `watchdog_killed: true` + which stage,
    so a human reviewing in the morning sees *why* the run has no normal
    stage-6 report, distinct from a run that's still legitimately in
    progress.
  - `herdr notification show --sound request` — same channel the
    orchestrator itself would have used on a normal terminal state
    (orchestrator-prompt.md:163), so this doesn't need a new notification
    path.
- Does **not** attempt to resume or retry the run — killing + reporting only.
  Resuming a run whose orchestrator is dead is out of scope here (the
  existing `-s <session_id>` resume mechanism assumes a *live* orchestrator
  driving it).

Files: new `jobs.d/pipeline-watchdog.yaml` (or equivalent routine
definition), a small script/module it runs (`src/herdr_routines/` if it
needs `Job`/state helpers, otherwise a standalone script is enough since
this doesn't dispatch through `tick.py`'s job model).

## Acceptance

- Given a pipeline run whose `deadline_epoch` has passed and whose heartbeat
  log has not advanced, the watchdog kills any still-`working` `pl-<N>-*`
  agent for that run within one watchdog cycle.
- The watchdog writes a report distinguishing "killed by watchdog" from a
  normal terminal report, so the existing "no report file" morning check
  (G-4) is superseded rather than silently duplicated.
- A run that is still healthy (heartbeat advancing, deadline not yet passed)
  is never touched.
- Given the PR #69 scenario replayed, the stage-6 worker is killed within
  15-30 minutes of deadline overrun, not found 19.5 hours later by a human.

## Log

- **2026-09-03**: filed after killing a 19.5-hour-hung stage-6 `opencode`
  process for run `20260903T050016Z` (PR #69) by hand. Discussed with the
  human: automate the kill, not just the detection — a stuck worker is a
  resource leak on top of being a stalled PR, and the existing G-4 "morning
  checklist" only ever covered noticing a missing report, never reaping the
  orphaned process.
