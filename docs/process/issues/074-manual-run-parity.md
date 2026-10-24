---
id: "074"
title: "Manual `herdr-routines run` takes the scheduled path: fallback, retry, history, and per-kind dispatch"
status: done
priority: high
area: scheduler
---

## Description

`herdr-routines run <job>` (`cli._cmd_run`) is a separate, thinner dispatch path from the
one the tick uses (`tick._process_job`). It differs in ways that have already cost us:

1. **No `fallback_model`, no retry.** `_cmd_run` calls `runner.execute_run` once. On
   2026-10-24 a manual rerun of `fitted-pr-review` died on `quota_exhausted`. The
   scheduled path would have retried on `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
   (issue 022).
2. **No history.** That run wrote nothing to `history.jsonl`. `status`, `history`,
   the digest and any later reasoning about the job never saw it. Its only trace
   was a tmux log.
3. **Wrong dispatch for `gated` and `review` jobs.** `_cmd_run` special-cases only
   `pipeline` and `audit`. A `gated` job (`babysit-prs`) or a `review` job falls
   through to plain `execute_run`: its prompt is sent with none of that kind's gate
   checks, target selection or post-condition.
4. **No notification** on a manual failure, unlike the same failure from the tick.

The selector (issue 073) already runs on both paths, but it does so through two copies
of the same code, which is how drift like the above happens.

## Design

One dispatch function for a single occurrence of a job, called by both the tick and
`run`. The tick decides *whether* a job is due (`schedule.decide`). `run` skips that
decision. Everything after it is shared: selector, `running` record, `execute_run`
with fallback and retry, verify gates, terminal record, notification, and the
per-kind branch (`gated` / `review` / `audit` / `pipeline`).

- A manual run is recorded like a scheduled one, plus `extra.trigger: "manual"`.
  The field already exists in `HistoryExtra`. Check how scheduled runs set it today
  and reuse the vocabulary. A manual run must not count as the scheduled occurrence:
  the next cron firing still runs, and catch-up and missed-run logic ignore manual
  records. State how in the plan.
- `--dry-run` stays side-effect free.
- `--run-id` (resume) keeps its current meaning.
- Lock behaviour is unchanged: `run` still takes the tick lock and refuses while a tick
  holds it. Making a long manual run not block scheduled ticks is out of scope; note it.

## Acceptance criteria

1. [blocking] A manual run whose primary model hits `quota_exhausted` retries once on
   the job's `fallback_model`, exactly as the tick would. Test:
   `test_manual_run_uses_fallback_model`
2. [blocking] A manual run appends `running` and terminal history records with
   `extra.trigger == "manual"`, and a failure notifies per `notify_policy`. Test:
   `test_manual_run_records_history_and_notifies`
3. [blocking] A manual run does not consume the next scheduled occurrence. The next
   due cron firing still runs, and it isn't reported missed. Test:
   `test_manual_run_does_not_consume_scheduled_occurrence`
4. [blocking] `run` on a `gated` job goes through the gated path (checks first, fix
   worker only when they fire), and on a `review` job through the review path. Test:
   `test_manual_run_dispatches_by_kind`
5. [blocking] The tick and `run` call the same dispatch function for a due
   occurrence. A test fails if either re-implements it. Test:
   `test_tick_and_manual_run_share_dispatch`
6. [non-blocking] `--dry-run` output is unchanged.

## Notes

- Related: 022 (fallback_model), 073 (selector, currently duplicated in cli and tick),
  071 (verify gates), 049 (kind dispatch).
