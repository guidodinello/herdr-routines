---
id: "053"
title: "runner checkout is never updated — merged fixes silently don't reach the host"
status: done
priority: high
area: deploy
---

## Description

A host runs herdr-routines from **two different checkouts**, and only one of
them updates itself:

| Checkout | Used by | Updated how |
|---|---|---|
| `~/projects/herdr-routines` | `herdr-routines.service`, `-watchdog.service`, `-digest.service` (`WorkingDirectory=`, `uv run herdr-routines …`) | **by hand only** |
| `~/.local/state/herdr-routines/repos/herdr-routines` | the `feature-pipeline` job's `repo:`, so `scripts/pipeline-launch.sh` and the orchestrator's `uv run herdr-routines gate/pick-feature` | `ensure_repo` (`repos.py:42`) → `_fetch_and_fast_forward` (`repos.py:93`) on every run |

So a fix merged to `main` reaches the pipeline launcher the next night, but the
tick/watchdog/digest code keeps running whatever someone last pulled by hand.
A fix that spans both halves gets split: the new launcher and the old tick
disagree.

### Incident (2026-09-27)

The Pi's runner checkout had been stuck at `01e6fd9` (PR #116, 2026-09-10) for
17 days. It was missing #117–#127, including #121 (transient-failure retry for
routine jobs), #122 (retention), #123 (model validation) and #125 (pipeline
quota fast-fail + `fallback_model` retry).

On 2026-09-20 the `feature-pipeline` run hit the OpenCode free-tier quota. The
**new** `pipeline-launch.sh` (from the auto-synced job repo) detected it and
wrote `## Outcome: failed (quota_exhausted)`. The **old** tick didn't have the
`quota_exhausted` branch in `_classify_pipeline_outcome`, so it recorded
`reason: orchestrator_failed` and never launched the `fallback_model` retry
that #125 added. #125 had been merged for 8 days and never took effect on the
host it was written for. Fixed by hand: `flock tick.lock git merge --ff-only
origin/main` in the runner checkout.

## Design (proposal)

A new `herdr-routines self-update` subcommand, run by its own systemd timer
(`herdr-routines-update.{service,timer}`), once a day **before** the nightly
jobs (e.g. `OnCalendar=*-*-* 21:30:00`, `Persistent=true`).

It runs from the **currently installed (old) code** and checks the new code
**out of process**, so a commit that doesn't even import still gets rolled back:

1. Take `tick_lock` (the same lock `_cmd_tick` holds, `cli.py:416`) so code is
   never swapped under a running tick. If the lock isn't acquired in time,
   exit 0 and try tomorrow.
2. If any `kind: pipeline` job has an open run (`_open_pipeline_run`,
   `tick.py:1637`), **defer**: exit 0 without updating. Swapping the code that
   reconciles an in-flight overnight run is the one moment worth avoiding.
3. Record `old = git rev-parse HEAD`. If the checkout is dirty or not on
   `main`, don't touch it: notify and exit non-zero.
4. `git fetch`, then resolve `new = origin/main`. If `new == old`, exit 0
   quietly. **Require green CI on `new`**, the same gate as step 0 of
   `docs/process/pi-update-runbook.md` ("never ship a red runner"): query the
   commit's check runs (`gh api repos/{owner}/{repo}/commits/<new>/check-runs`).
   If any check is failing, pending, or can't be queried, defer: exit 0
   without updating, and log the reason.
5. `_fetch_and_fast_forward(checkout, base="main")` (`repos.py:93`), or an
   ff-only merge of exactly `new`. If ff-only fails because the checkout has
   diverged, notify and exit non-zero, never force.
6. Run `uv run herdr-routines validate` **as a subprocess** from the checkout.
   This exercises the new import graph, the new config schema and the new
   `uv.lock` against the host's live `jobs.d/`. On a non-zero exit, move the
   checkout back to `old` (`git reset --keep <old>`) and notify with the
   validate output. Also compare the **job count** `validate` reports before
   and after. The runbook's "Config migrations" section warns that a reshaped
   schema can make an old job entry silently ignored rather than rejected, so
   a drop in job count also triggers a rollback.
7. If `git diff --name-only old..new -- deploy/` is non-empty, **notify and
   list the files; don't apply them**. Systemd units, `opencode.pipeline.json`
   and example `jobs.d/` are host config with their own deploy steps
   (`deploy/README.md`).
8. On success, log `self-update: <old>..<new> (<n> commits)`. Notify only when
   the SHA actually changed.

`docs/process/pi-update-runbook.md` stays as the manual fallback (and the
place to document config migrations). Its "When to run" section should point
at this timer once it ships.

No restart is needed. Each tick is a fresh `uv run herdr-routines tick`
process, so the next tick picks up the new code, and `uv run` re-syncs the
environment when `uv.lock` changed.

### Why a separate timer, not a step inside `tick`

- The code changes at one predictable time, before the night's runs, not
  between a pipeline launch and its reconcile.
- A failing update can't block ticks. The scheduler keeps running the old code.
- The digest and watchdog units share the checkout. Updating from inside one of
  them would be arbitrary.

### Non-goals

- Applying `deploy/` changes (units, opencode config) automatically. Notify only.
- A pinned `pi-deploy` branch or tag. The host follows `main`; CI plus the
  human merge is the gate. (The herdr-remote fork uses a pinned deploy branch.
  That's deliberate for an upstream fork and doesn't apply here.)
- Catching logic regressions. `validate` proves "loads and accepts the live
  config", not "behaves correctly". Rollback covers the first case only.
- Updating the job repo checkout. `ensure_repo` already does that.

## Acceptance criteria

1. `self-update` on a checkout behind `origin/main` fast-forwards it, runs
   `validate` as a subprocess, and exits 0. History/log shows `old..new`.
   Test: `test_self_update_fast_forwards_and_validates`
2. When the post-update `validate` subprocess exits non-zero (including an
   import error in the new code), the checkout is back at `old` and a
   notification carrying the validate output is sent. Test:
   `test_self_update_rolls_back_on_validate_failure`
3. With an open `kind: pipeline` run, `self-update` makes no git changes and
   exits 0 with a `deferred` log line. Test:
   `test_self_update_defers_while_pipeline_run_open`
4. A dirty working tree, a non-`main` branch, or a diverged history leaves the
   checkout untouched, notifies, and exits non-zero. Never `reset --hard`,
   never force. Test: `test_self_update_refuses_dirty_or_diverged`
5. It holds `tick_lock` for the whole fetch→validate→(rollback) sequence. If
   the lock can't be acquired, it's a no-op exit 0. Test:
   `test_self_update_respects_tick_lock`
6. Changed `deploy/` paths are reported in the notification and not applied.
   Test: `test_self_update_reports_deploy_changes`
7. When `origin/main`'s check runs are failing, pending or unqueryable, no
   git change is made and the exit code is 0 (deferred). Test:
   `test_self_update_defers_on_non_green_ci`
8. A post-update `validate` that passes but reports **fewer** jobs than before
   is treated as a failure and rolled back. Test:
   `test_self_update_rolls_back_on_job_count_drop`
9. `deploy/systemd/herdr-routines-update.{service,timer}` exist, modeled on
   the digest unit (`Type=oneshot`, same `PATH`, finite `TimeoutStartSec`).
   `deploy/README.md` documents enabling them, and
   `docs/process/pi-update-runbook.md` points at the timer.

## Why these tests

- 1–2 are the feature: stay current, and never leave the host on code that
  won't load. The rollback has to work when the new code can't be imported,
  which is why validation runs out of process.
- 3 and 5 pin the timing: never swap code under a tick or an open overnight run.
- 4 pins that a hand-edited or hotfixed host is never clobbered.
- 6 pins that host config is still applied deliberately.
- 7–8 carry over the runbook's two manual guards: green CI, and the
  silently-dropped-job migration hazard.

## Log

- **2026-09-27**: filed after diagnosing a week with no pipeline PRs. Two
  causes: the Pi was off Wi-Fi from 09-20 to 09-27 (5 GHz link degraded, then
  NetworkManager exhausted its 4 autoconnect retries; moved to 2.4 GHz), and
  the runner checkout was 11 commits stale, which silently disabled #125's
  fallback retry on the 09-20 run. The runner was fast-forwarded to `c82b27e`
  by hand. This issue removes the manual step.
