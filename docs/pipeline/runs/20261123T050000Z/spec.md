# Spec v2 — 20261123T050000Z — Retries for every job kind, backed off across ticks

Implements [issue 088](../../../process/issues/088-retries-ignored-outside-routines.md) only.
Background: `docs/plan-v1.md` (project context, not this feature).

## Problem

`retry_attempts` / `retry_on` have existed since issue 008, but only
`routine_flow._run_routine` reads them (`routine_flow.py:256`). `audit_flow`,
`gated_base_flow`, `gated_pr_flow` and `pipeline_flow` write terminal `failed` records
with `repo_sync_failed` / `clone_failed` — both in `VALID_RETRY_REASONS` — and never
retry. `config._build_retry` accepts the keys on any kind, so `retry_attempts: 2` on an
audit is silently inert. Real case: `audit-type-health` lost its 2026-11-16 weekly run to
one DNS failure.

The one retry that does exist sleeps inside the tick (`routine_flow.py:282-284`): 5s, then
15s. An outage longer than one attempt fails every attempt, and a sleep in the tick holds
the global lock, delaying the next job and making the next tick skip. The fix must not
sleep.

## Approach

### 1. Default: one retry, pre-agent reasons only

- Add `PRE_AGENT_RETRY_REASONS = frozenset({...})` beside `VALID_RETRY_REASONS`
  (`config.py:141`): `clone_failed`, `repo_sync_failed`, `pane_creation_failed`,
  `agent_start_failed`, `report_dir_creation_failed`.
- `_build_retry` (`config.py:1827`) takes the raw job mapping so "key absent" is distinct
  from "explicit value". Absent `retry_attempts` resolves to `DEFAULT_RETRY_ATTEMPTS = 1`;
  absent `retry_on` resolves to `tuple(sorted(PRE_AGENT_RETRY_REASONS))` when attempts > 0,
  else `None`. Explicit `retry_attempts: 0` opts out; explicit `retry_on` (including
  post-agent reasons) opts in. An explicit empty `retry_on` with attempts > 0 stays a
  `ConfigError`. `Job` field defaults stay `0`/`None`: only `load_config` derives the new
  default, so directly constructed test jobs keep their behaviour.
- Keep the 0..3 bound and the "retry_on inert with attempts 0" warning. Remove the
  `workspace: root` warning: the risk is "agent started", not the workspace.
- Multi-target selectors are the one unsafe case (a retry could post a second review).
  Resolve `pick` before `_build_retry` (`config.py:2171`); for
  `pick in MULTI_TARGET_SELECTORS` (including `kind: review`) the default is `0`, and an
  explicit `retry_attempts > 0` is a `ConfigError` naming the reason. This replaces the
  "accepted but ignored" warning at `config.py:2185`.

### 2. Back off across ticks, never sleep

- After a dispatch, if the last history record is `failed`, its `reason` is in
  `job.retry_on`, and the dispatched run's attempt `< job.retry_attempts`, append a
  `scheduled` record (the state exists in `history.NonTerminalState` but is unused) with
  `reason: retry_pending`, `trigger`, `retry_of` (failed run_id), `attempt` (next, 1-based),
  `max_retries`, `retry_eligible_reason`, `retry_due_at`.
- `retry_due_at = failed.ts + TICK_INTERVAL_SECONDS * 2**attempt_index`, with
  `TICK_INTERVAL_SECONDS = 300` (the documented `OnCalendar=*:0/5`). Attempt 1 is due
  after 1 tick, attempt 2 after 2, attempt 3 after 4 (5/10/20 min).
- New `retry.py` holds the pure policy: `pending_retry(records)`, `retry_due_at`,
  `schedule_record`, `retry_decision`, `retry_occurrence`. `history.py` adds
  `retry_due_at` / `retry_of` to `HistoryExtra` and `retry_dropped` to
  `NOT_A_RUN_REASONS`; its docstring state list gains `scheduled`.
- The scheduler is told the attempt of the run that just failed — from the occurrence for
  synchronous kinds, from the open run for pipelines — and schedules `attempt + 1`. It
  must not rely on the flow's terminal record for the counter, or a retry whose terminal
  record omits `attempt` would schedule attempt 1 forever. The failed terminal record also
  carries `attempt` for history (thread the occurrence's attempt into the audit, gated and
  pipeline terminal writes).
- Pending detection: a `scheduled` record with `reason: retry_pending` is pending until a
  later terminal record whose reason is not contention (`NOT_A_RUN_REASONS` plus
  `agent_name_live`) or a `retry_dropped` record. `running` does not clear it, so a retry
  that is running, or skipped on `checkout_busy`, is not lost.

### 3. Due / dropped decision

- Add `schedule.next_occurrence_after(cron, tz, after)` (one `croniter.get_next`).
- In `_fire_triggers` (`tick.py:547`, after the shared guards, before the cron/event
  decision) and `_decide_pipeline_job` (`tick.py:1319`, after the open-run reconcile),
  look up the pending retry:
  - the next cron occurrence after the failure is `<= retry_due_at` and has arrived
    (`<= now`): append `skipped` (`reason: retry_dropped`, `trigger`, `retry_of`,
    `attempt`), log it, and fall through to the normal decision;
  - `now >= retry_due_at`: return a `_PendingDispatch` for a retry occurrence;
  - otherwise report "retry pending" and write nothing.
- The retry occurrence uses a fresh `make_run_id(job.name, now)` and `extra` with
  `trigger`, `attempt`, `max_retries`, `retried_from_run_id`, and goes through
  `dispatch_occurrence`, so routine, audit, gated base, gated PR and pipeline all re-run
  their normal path. A pending retry is never written as `missed`.
- Exclude the gated job whose `github_event` check is its trigger (issue 077): it keeps
  its per-event attempt budget. Manual/request runs do not create retries.

### 4. Where the retry is scheduled

- Synchronous kinds (routine, audit, gated): in `_dispatch_pending` (`tick.py:995`),
  under the job lock, after `pending.dispatch(now)`. It reads the last record, so a
  pipeline launch that wrote `running` schedules nothing. `_PendingDispatch` gains
  `schedule_retry: bool = True` and the occurrence's `attempt` / `max_retries` (defaults
  `0` / `0`); `_pending_occurrence` fills them from `occ.extra`. The event-gate path sets
  `schedule_retry=False`.
- Pipelines fail asynchronously: call the same scheduler after each `failed` record in
  `_decide_pipeline_job` (report failure, host reboot, no report), passing
  `open_run.extra.get("attempt", 0)`. The failed record carries the open run's `trigger`;
  the scheduler still filters on `job.retry_on`, so `host_rebooted` / `no_report` /
  `stage_independence_unverified` do not retry by default.
- Only `cron` / `event` triggers schedule. The scheduler checks the failed record's
  reason and the explicit attempt, then writes the next `scheduled` record.

### 5. Routines lose the synchronous loop

- `routine_flow._run_routine`: delete `_retry_eligible` (`:92`), the `while` / `attempt`
  loop and its `time.sleep` (`:202-312`). Keep the `fallback_model` block exactly where
  it is (same tick). Write one `running`, run, optionally fall back, write one terminal.
  Copy `attempt`, `max_retries`, `retried_from_run_id` from `occ.extra` onto the fallback
  running record and the terminal record.
- `routine_targets_flow`'s "one attempt, no retry" comment stays true: multi-target jobs
  now default to 0.

### 6. Validate

- `cli._check_systemd_timeout` (`cli.py:1186`) drops `* (1 + job.retry_attempts)` and its
  comment: retries run on later ticks, so a tick runs at most one attempt per job.
- Update `_guard_open_run`'s comment (`tick.py:365`) that still mentions
  `(1 + retry_attempts)`.

## Files touched

- `src/herdr_routines/config.py` — reasons, default, `_build_retry` signature and
  validation, move the call after `pick`, remove two warnings.
- `src/herdr_routines/retry.py` (new) — tick backoff and pending-retry policy.
- `src/herdr_routines/history.py` — `HistoryExtra` keys, `NOT_A_RUN_REASONS`, docstring.
- `src/herdr_routines/schedule.py` — `next_occurrence_after`.
- `src/herdr_routines/tick.py` — pending checks, retry `_PendingDispatch`, scheduler calls.
- `src/herdr_routines/routine_flow.py` — remove the sync retry loop; keep fallback.
- `src/herdr_routines/cli.py` — timeout check.
- `tests/test_retry.py` (new), `tests/test_config.py`, `tests/test_tick.py`,
  `tests/test_cli.py`, `tests/test_kind_audit.py`, `tests/test_kind_gated.py`,
  `tests/test_pipeline_*.py` — retry coverage per kind.

## Risks

- **Double-post on review jobs.** Mitigated by defaulting multi-target selectors to 0 and
  rejecting explicit retries; post-agent reasons stay opt-in.
- **A `scheduled` marker is an extra record.** The two attempt outcomes are still the
  failed and done/failed terminal records; the marker is non-terminal bookkeeping. Tests
  should count terminal records, not raw lines.
- **Wall-clock backoff assumes the 5-min timer.** `TICK_INTERVAL_SECONDS = 300` is the
  documented cadence; a different cadence changes minutes, not the tick count.
- **The first failure still notifies.** The flow notifies before the scheduler knows a
  retry is pending, so a transient failure can page and then succeed. Acceptable for the
  lost-audit case; suppressing the first page is a follow-up.
- **Stale pending retry for a disabled job.** Re-enabling can run an old retry; the
  next-occurrence drop covers the common case. Operator `prune` remains the escape hatch.
- **Tick exit status.** A first failure that schedules a retry still marks the tick
  failed; the retry is visible in history but systemd still reports the transient failure.

## Acceptance criteria

Classification: each item is **blocking** (run must not ship without it) or
**non-blocking** (valuable, ship with a follow-up note if it slips). Confidence:
how sure the spec is that the item is both correct and testable as written
(high / medium / low). Each item ends with the exact test that proves it.

1. **blocking / confidence: high** — With both retry keys absent, `load_config`
   resolves `retry_attempts` to `1` and `retry_on` to the five pre-agent reasons in
   sorted order; an explicit `retry_attempts: 0` opts out; an explicit post-agent
   `retry_on` opts in; an explicit empty `retry_on` with attempts > 0 is a
   `ConfigError`; directly constructed `Job`s keep `0`/`None`. Test:
   `test_retry_defaults_and_explicit_overrides`
2. **blocking / confidence: high** — For `pick in MULTI_TARGET_SELECTORS` (including
   `kind: review`) the derived default is `0`, and an explicit `retry_attempts > 0`
   is a `ConfigError` naming the double-post risk; the old "accepted but ignored"
   warning is gone. Test: `test_retry_rejected_for_multi_target_selector`
3. **blocking / confidence: medium** — Routine, audit, gated base, gated PR and
   pipeline each retry one pre-agent failure on a later tick and end `done`: the
   first attempt has a `failed` terminal record, a `scheduled` marker follows, and
   the retry attempt's terminal record is `done`. A pipeline launch that leaves
   `running` as the last record schedules nothing. Test:
   `test_every_kind_retries_pre_agent_failure_on_a_later_tick`
4. **blocking / confidence: high** — A gated job whose `github_event` check is its
   trigger (issue 077) keeps its existing per-event attempt budget and never gets a
   tick-scheduled retry. Test:
   `test_event_gate_job_keeps_per_event_attempt_budget`
5. **blocking / confidence: high** — Backoff is computed in ticks, not slept: the
   pending retry record's `retry_due_at` is 1, 2 then 4 timer intervals after the
   failure, and neither `retry.py` nor the tick path calls `time.sleep`. Test:
   `test_retry_backoff_is_one_two_four_ticks_without_sleeping`
6. **blocking / confidence: high** — The failed attempt record carries `attempt`;
   the pending retry record carries `attempt`, `max_retries`, `retry_of`,
   `retry_eligible_reason` and `retry_due_at`; a tick that is not yet due writes no
   `missed` record. Test:
   `test_retry_records_carry_attempt_metadata_and_no_missed`
7. **blocking / confidence: high** — A pending retry is dropped, logged as a
   `skipped` record with `reason: retry_dropped`, and falls through to the normal
   decision when the next cron occurrence arrives before `retry_due_at`. Test:
   `test_pending_retry_is_dropped_when_the_next_cron_occurrence_comes_first`
8. **blocking / confidence: high** — A post-agent reason (e.g. `agent_prompt_failed`,
   `no_report`) does not retry under the defaults; only the explicit pre-agent set
   does. Test: `test_post_agent_reason_does_not_retry_under_defaults`
9. **blocking / confidence: medium** — Manual and request runs never create a
   `scheduled` retry record, and a retry is scheduled only for `cron`/`event`
   triggers. Test: `test_manual_and_request_runs_do_not_create_retries`
10. **blocking / confidence: high** — Removing the synchronous loop leaves the
    `fallback_model` retry intact: a quota failure still falls back within the same
    tick, and the fallback running/terminal records copy `attempt`,
    `max_retries` and `retried_from_run_id` from `occ.extra`. Test:
    `test_fallback_model_still_runs_within_one_tick`
11. **blocking / confidence: high** — `validate`'s `TimeoutStartSec` check no longer
    multiplies by `(1 + job.retry_attempts)`, since a tick runs at most one attempt
    per job. Test: `test_validate_timeout_no_longer_multiplies_by_retry_attempts`
12. **non-blocking / confidence: medium** — The `history.py` docstring state list
    mentions `scheduled`, and `_guard_open_run`'s comment no longer describes the
    old synchronous retry multiplier. Test:
    `test_history_docstring_and_guard_comment_drop_sync_retry`
13. **non-blocking / confidence: medium** — Meta: lint, strict mypy and the full test
    suite are green through `scripts/ci-check.sh`, proving the migration introduced no
    type-level bypass and no unrelated regression. Test:
    `test_ci_green_with_retry_backoff_blocking_tests_run_first`

## Changelog v1→v2

- Retitled the document **v2** and replaced v1's file-anchored acceptance checklist
  with 13 numbered items. Each item is tagged **blocking** or **non-blocking**, carries
  a `confidence:` tier, and ends with one canonical `Test: <name>` anchor that stage 3
  must use verbatim.
- Merged v1's five per-kind bullets (routine, audit, gated base, gated PR, pipeline)
  into a single parametrized criterion anchored to
  `test_every_kind_retries_pre_agent_failure_on_a_later_tick`; this keeps one exact
  test name per criterion while still covering every kind. The same item now also
  pins the "pipeline launch leaves `running`, so no retry" case from Approach §4.
- Promoted two Approach exclusions that were prose-only in v1 into blocking
  criteria: the issue-077 event-gate budget (criterion 4) and manual/request runs
  never scheduling retries (criterion 9).
- Split v1's single combined retry-mechanics bullet into criterion 5 (tick backoff,
  1/2/4 intervals, no `time.sleep`) and criterion 6 (record metadata plus "a pending
  tick writes no `missed`"), so a failing test points at one behaviour.
- Replaced v1's lone non-blocking bullet with two: the docstring/comment cleanup
  (criterion 12) and the CI meta check (criterion 13). The meta test gets a unique
  name because `test_ci_green_with_blocking_tests_run_first` already exists in
  `tests/test_config.py`, `tests/test_issue_refinement.py` and
  `tests/test_telegram_spawn.py`.
- Review verification: the Problem and Approach line references still hold
  (`VALID_RETRY_REASONS` `config.py:141`, `_build_retry` `config.py:1827`,
  `_build_retry` call `config.py:2171` with `_build_pick` immediately after at
  `config.py:2178`, the ignored-retry warning `config.py:2185`,
  `_retry_eligible` `routine_flow.py:92`, retry loop `routine_flow.py:202-312`,
  `time.sleep` `routine_flow.py:282-284`, `_fire_triggers` `tick.py:547`,
  `_PendingDispatch` `tick.py:900`, `_dispatch_pending` `tick.py:995`,
  `_decide_pipeline_job` `tick.py:1319`, `_guard_open_run` comment
  `tick.py:365`, timeout check `cli.py:1112`/`:1186`). `scheduled` is already a
  `NonTerminalState`, and all five proposed pre-agent reasons are in
  `VALID_RETRY_REASONS`.
- No changes to Problem, Approach or Risks. Flagged for implementation (stage 4,
  not stage 3): `tests/test_tick.py`'s issue-008 synchronous retry tests
  (`:2440-2610`) and `tests/test_config.py`'s retry tests (`:1365+`) encode the old
  explicit-`retry_on` and `workspace: root` warning behaviour and must be updated
  alongside the new named acceptance tests.
