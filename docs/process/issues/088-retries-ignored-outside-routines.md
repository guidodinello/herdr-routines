---
id: "088"
title: "Retries: work for every job kind, back off across ticks, retry pre-agent failures by default"
status: done
priority: medium
area: runner
---

## Description

Issue 008 added `retry_attempts` / `retry_on`, but only `routine_flow.py` reads them
(the loop at `routine_flow.py:256`). `audit_flow`, `gated_base_flow`, `gated_pr_flow`
and `pipeline_flow` all emit `repo_sync_failed` or `clone_failed`, which are in
`VALID_RETRY_REASONS`, yet they never retry. `config._build_retry` accepts the keys on
any job kind without a warning, so a job of one of those kinds can set
`retry_attempts: 2` and get no retries.

Real case: `audit-type-health` (kind `audit`, weekly) failed on 2026-11-16 with
`repo_sync_failed` / `Could not resolve host: github.com`. The Pi still drops some DNS
lookups (see the 2026-10-16 note on the NetworkManager `timeout:1,attempts:2` setting).
One lost lookup cost a whole week of the audit.

A second gap: the routine retry runs straight after the failed attempt, with no delay.
A network outage that lasts longer than one attempt (seconds, for a git fetch) fails
every attempt, so even routines get little out of `retry_on: [repo_sync_failed]`.

## Design

Two decisions, made on 2026-11-22:

- **Back off across ticks instead of sleeping.** A failed attempt is recorded as a
  pending retry, and a later tick runs it again: 1 tick later, then 2, then 4
  (5, 10, 20 min at the 5-minute tick). Sleeping inside a tick would hold the tick
  lock, delay every job dispatched after this one, and make the next tick skip. With
  at most 3 attempts, sleeps short enough to fit in a tick (seconds to 2 min) cannot
  ride out an outage longer than that.
- **Retry pre-agent failures by default.** Issue 008 defaulted to no retries because
  rerunning a job that is not idempotent can post or push twice. That risk only exists
  once the agent has started. The reasons raised before it starts (`clone_failed`,
  `repo_sync_failed`, `pane_creation_failed`, `agent_start_failed`,
  `report_dir_creation_failed`) are safe to retry for every job, so they get one retry by
  default. Reasons raised after the agent started (`no_report`, `blocked`,
  `agent_prompt_failed`, `quota_exhausted`, `agent_not_interactive`, `tmp_full`) stay
  opt-in through `retry_on`.

## Acceptance

- The default is `retry_attempts: 1` with `retry_on` set to the five pre-agent reasons
  above. A job can still set `retry_attempts: 0` to opt out, or list post-agent reasons
  in `retry_on` to opt in. The pre-agent reasons are declared once, as data (a frozenset
  next to `VALID_RETRY_REASONS`), and the default derives from it.
- Every job kind that can emit a retry-eligible reason (routine, audit, gated base,
  gated PR, pipeline) retries it. If a kind is unsafe to retry, `validate` rejects
  `retry_attempts` on that kind with a `ConfigError` saying why. A key is never accepted
  and then ignored.
- A failed attempt with retries left is written to `history.jsonl` as its own record,
  with `attempt` and the tick it becomes due. The tick does not sleep. A later tick runs
  it: attempt N becomes due 2^(N-1) ticks after the failure. The schedule guard treats a
  pending retry as due, not as a missed occurrence.
- A pending retry is dropped, and logged as dropped, when the job's next scheduled
  occurrence comes first, so a retry never runs on top of a fresh run.
- Routines move from back-to-back retries in `routine_flow.py` to the same tick-based
  retries. `fallback_model` still runs within the same tick, as it does today.
- `validate`'s timeout check no longer multiplies the timeout by `(1 + retry_attempts)`
  (`cli.py:1186`), since retries no longer run within one tick.
- Tests: for each kind, the first sync fails and the retry on a later tick passes, with
  two history records. A post-agent reason does not retry under the defaults. A pending
  retry is dropped when the next scheduled occurrence comes first.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-22**: filed after `audit-type-health` lost its 2026-11-16 run to one DNS
  failure. No job on the Pi sets `retry_attempts` today.
- **2026-11-22**: design added: tick-based exponential backoff, and one retry by default for
  pre-agent failures only.
- **2026-11-24**: built. `src/herdr_routines/retry.py` holds the backoff and the pending-retry
  record; `tick._dispatch_pending` queues a retry after any cron-triggered dispatch that ends
  in a `retry_on` failure, and `_fire_triggers` / the pipeline decision run it when due.
  Decisions the issue left open:
  - The pending retry is a non-terminal `scheduled` record (`reason: retry_pending`), not a
    change to the failed record, so every kind's own failure writer stays as it was. Due time
    is stored as `retry_due_at`; it is the next 5-minute tick boundary after the failure, plus
    2^(N-1) - 1 further ticks.
  - A retry is a new occurrence with a fresh run id (`make_run_id(job, now)`), the original
    `scheduled_for`, `attempt: N` and `retried_from_run_id`. The drop is a `skipped` record
    with `reason: retry_dropped`, listed in `history.NOT_A_RUN_REASONS`, so it never consumes a
    cron occurrence. Any cron occurrence between the failure and now drops the retry.
  - Only cron-triggered runs retry. `validate` rejects a `retry_attempts` / `retry_on` the job
    sets itself where it could not be honored (no cron, multi-target `pick`, a `github_event`
    gated job, a pipeline `retry_on` with a post-agent reason); the default is switched off
    there without an error. A `retry_on` list replaces the default set rather than extending it.
  - A gated PR cycle retries as a whole when its last record is an eligible failure.
  - Not done: a failed attempt still sends its own failure notification, because the flows
    notify where they write the failure.
