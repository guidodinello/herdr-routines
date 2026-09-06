---
id: "009"
title: "Notification policy per job"
status: done
priority: low
area: config
---

## Description

Decide what "worth telling you" means per job — only on failure, only on a
non-trivial finding, or every run — instead of a ping on every run. Claude
Routines frames its notification toggle the same way.

An unattended overnight run should push exactly one notification (the final
report / PR link, or a failure), not a stream of progress pings. This is the
*policy* half (what to send); the *transport* half (how it reaches the phone)
is settled — the `herdr-telegram-bridge` plugin (see issue 023).

## Acceptance

- A job can declare a notification policy: `always` | `on-failure` |
  `on-finding` (or similar), defaulting to a single terminal-state
  notification.
- An unattended run under the default policy produces one notification, not
  per-step noise.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. The 2026-08-25 note there
  observed the gate is close to clearing — the pipeline POC's own
  manual-monitoring loop (repeated 2–5 min check-ins) is exactly the
  noisy-ping experience this fixes. Bundle with issue 010 (daily digest).
- **2026-09-06**: implemented. `Job.notify_policy: always | terminal |
  on-finding | on-failure` (default `terminal`), validated in `config.py`
  alongside the pre-existing `on_missed`. The fourth value beyond the issue's
  three examples earns its place: the acceptance criteria's own wording —
  "defaulting to a single terminal-state notification" and "the final report
  / PR link, or a failure" — requires the default to still notify on a clean
  success, not go silent; "on-failure" needed to keep its narrower, stricter
  meaning (silent unless the run actually failed) for jobs that want less than
  the default, e.g. a frequently-run job where even a clean-success ping is
  too much (see `babysit-prs.yaml`, set to `on-finding` below). `tick.py`
  classifies every `_notify()` call site into one of four kinds and gates it
  through `_notify_gate(job, kind)`:
  - `"failure"` — a terminal failure this tick (setup errors, an unresolved
    gate, a max-attempts cap). Notified under every policy value.
  - `"finding"` — no failure, but something worth knowing: a gated job's
    checks found something and the fix agent resolved it
    (`_process_base_target`'s post-fix `done`, `_process_pr_target`'s
    aggregate `done` when `dispatched_count > 0`), or a plain routine needed
    its `fallback_model` retry. Notified under `on-finding`, `terminal`, and
    `always`.
  - `"success"` — a clean terminal outcome with nothing notable (gate passed
    first try, routine finished on its primary model, pr-target enumerated
    nothing eligible). Notified under `terminal` and `always`; suppressed by
    `on-finding` and `on-failure`.
  - `"progress"` — a sub-step inside a still-in-progress job, never the job's
    own once-per-tick terminal notify: the one example is
    `_process_pr_target`'s per-PR `max_attempts_exceeded` skip mid-loop
    (the aggregate done/failed notify at the end of that same tick still
    fires per its own kind). Notified only under `always` — this is the one
    thing the default policy newly suppresses relative to pre-issue-009
    behavior.

  `on_missed`-triggered notifications (3 call sites, one per dispatch family)
  are deliberately **not** gated by `notify_policy` — that's its own
  pre-existing per-job opt-in, kept orthogonal so an existing `jobs.d/` config
  with `on_missed: notify` keeps exactly its current behavior.

  `pipeline_watchdog.py` needed no change: both of its notifications are
  failure-kind by construction (a stalled run, worker killed or not) and it
  has no per-job binding today — `run_watchdog` discovers in-flight runs from
  `state.json` files, not from `RoutinesConfig`. Wiring it to a job's policy
  would mean threading `config` through `cli.py`'s `_cmd_pipeline_watchdog`,
  which is out of this PR's scope (`cli.py` is off-limits — issue 010 is
  editing it in parallel).

  Tests assert on notifications a fake `HerdrClient` actually recorded across
  realistic `run_tick` calls (plain routine, pr-target gate with an eligible
  PR, pr-target gate with a PR over its attempt cap, fallback-model retry),
  not on `_notify_gate` in isolation — see `tests/test_tick.py`'s
  "notify_policy (issue 009)" section.

  `deploy/jobs.d/babysit-prs.yaml` sets `notify_policy: on-finding` live —
  its `*/10` cron is the noisy-ping case from this issue's own log entry
  above, and the default `terminal` policy alone still pings once per clean
  10-minute pass all night; `on-finding` keeps the useful signal (a PR was
  actually found and fixed) while staying silent on a clean pass.
