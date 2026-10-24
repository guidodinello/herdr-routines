---
id: "075"
title: "Triggers as job config: cron, polled events and on-request runs (phase C of 071)"
status: done
priority: medium
area: scheduler
---

## Description

Phase C of issue 071's split of `kind` into trigger / target / payload / proof. Today
every job has exactly one trigger, a required `cron:`. Everything else that starts work
lives outside the job model:

- **Event-ish work is cron-polled inside one kind.** `gated` jobs carry `github_event`
  checks (issue 014) that only fire when the job's cron fires. They're part of that
  kind's gate list, not something any job can be triggered by.
- **On-request work is a separate system.** Telegram `/run <alias>` (issue 024,
  `telegram_spawn.py`) cold-starts an interactive session from an alias map. It cannot
  run a configured job. `herdr-routines run <job>` is CLI-only.
- **Request-only jobs don't exist.** A job that should only ever run on request still
  needs a `cron:`, which is why the repo keeps disabled `manual-pr6xx` job files on the
  Pi as one-shots.

## Design

A `trigger:` block on a job. `cron:` stays accepted as shorthand for one cron trigger,
so no existing job file changes.

```yaml
trigger:
  cron: "0 9 * * *"          # optional
  event:                     # optional: polled each tick, fires the job when it matches
    github_event: { ... }    # same closed query set as issue 014's check
  request: true              # optional: runnable via `run` and Telegram `/job <name>`
```

- At least one trigger is required. A job with only `request: true` never fires on its
  own and is never reported missed.
- **`event` triggers are polled, not pushed.** The Pi has no public endpoint, so no
  webhooks. Each tick evaluates the job's event query with the existing
  `github_event` machinery and runs the job once per new matching event. Dedupe uses the
  event ids issue 014 already tracks (`extra.last_seen_event_ids`). The target selector
  (073) still runs after the trigger, so a trigger answers "should we look?" and the
  selector answers "at what?".
- **`request`**: Telegram gets a `/job <name>` verb next to `/run`, allowed only for
  jobs with `request: true`, through the same allowlist/chat-id rule as `/run`. It
  dispatches through the shared run path from issue 074, recorded with
  `extra.trigger: "request"`. `herdr-routines run` keeps working on any job.
- Every run records which trigger fired it (`extra.trigger`: `cron` / `event` /
  `request` / `manual`).
- `gated`'s `github_event` *checks* stay as they are in this issue. Migrating gated jobs
  to an `event` trigger is a follow-up once this lands.

## Acceptance criteria

1. [blocking] `cron:` alone loads exactly as today, and `trigger: {cron: ...}` is
   equivalent. Test: `test_trigger_cron_shorthand_equivalent`
2. [blocking] A request-only job never runs from the tick and is never reported
   missed, but `run` executes it. Test: `test_request_only_job_never_fires_on_tick`
3. [blocking] An `event` trigger runs the job once per new matching event and not again
   for an already-seen event. Test: `test_event_trigger_fires_once_per_event`
4. [blocking] Telegram `/job <name>` runs a `request: true` job through the shared run
   path, and is refused for a job without it or from a disallowed chat. Test:
   `test_telegram_job_verb_runs_request_jobs_only`
5. [blocking] Every run record carries `extra.trigger`. Test:
   `test_run_records_its_trigger`
6. [non-blocking] `validate` warns on a job with no trigger at all, or with
   `request: true` and `enabled: false` (it can never run).

## Notes

- Depends on 074 (one shared run path). Implement after it merges.
- Related: 014 (github_event), 024 (Telegram spawn), 071/073 (phases A/B).

## Log

- **2026-10-24**: implemented. Where the code settled the spec's open points:
  - A job with neither `cron:` nor `trigger:` is a load error; an explicit `trigger:` that
    fires nothing loads and `validate` warns (AC 6).
  - Cron goes first each tick; a due occurrence dispatches and the event poll waits for the
    next tick, so a job starts at most one run per tick. For the same reason at most one new
    event fires per tick (the rest fire on later ticks), which keeps `validate`'s systemd
    timeout budget true.
  - Event dedupe is issue 014's `last_seen_event_ids` watermark, but written before the
    dispatch (at-most-once per event); a failed run is retried by its own `retry_on` /
    `fallback_model`, not by the trigger, unlike 014's per-event attempt budget.
  - `trigger.event` is rejected on `kind: pipeline`, `kind: review` and a gated job with its
    own `github_event` check.
  - Only `cron` runs feed `schedule.decide`: a run whose `trigger` is not `cron` (event,
    request, manual) is skipped by `history.last_scheduled_terminal_run`, as are the
    run_id-less event-poll records.
  - AC 5 is met by the `running` record every run opens with, plus the routine and pipeline
    terminals; the gated / audit / review cycles write their terminals without the
    occurrence (linked by run_id, as 074 does).
  - `/job` launches `herdr-routines run <name> --trigger request` detached, so it reaches
    `dispatch_occurrence` through the same path as `run`. If a tick holds the lock, the run
    posts a "request refused" notification instead of vanishing.
  - **Deploy follow-up**: the `herdr-remote` fork's `HERDR_TG_SPAWN_CMD` hook only forwards
    `/run` and `/start` to `scripts/herdr-spawn-from-telegram.sh`; it needs `/job` added
    before the verb works on the Pi.
