# Spec v1 — 20261123T050000Z — Retries for every job kind, backed off across ticks

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

Blocking:

- [ ] Default `retry_attempts` is 1 with the five pre-agent reasons; 0 opts out; post-agent
      reasons opt in; multi-target explicit > 0 is a `ConfigError`. (`tests/test_config.py`)
- [ ] Routine, audit, gated base, gated PR and pipeline each retry a pre-agent sync
      failure on a later tick and end `done`, with the failed and done terminal records
      plus the `scheduled` marker. (`tests/test_kind_*.py`, `tests/test_pipeline_*.py`,
      `tests/test_tick.py`)
- [ ] The failed attempt record carries `attempt`; the pending retry record carries
      `attempt` and `retry_due_at`; no `time.sleep`; backoff is 1/2/4 ticks; the pending
      tick writes no `missed` record. (`tests/test_retry.py`)
- [ ] A pending retry is dropped and logged when the next cron occurrence comes first.
      (`tests/test_retry.py`)
- [ ] A post-agent reason does not retry under the defaults. (`tests/test_tick.py`)
- [ ] `fallback_model` still runs within one tick. (`tests/test_tick.py`)
- [ ] `validate`'s `TimeoutStartSec` check no longer multiplies by `retry_attempts`.
      (`tests/test_cli.py`)
- [ ] `scripts/ci-check.sh` is green.

Non-blocking:

- [ ] `history.py` docstring and `_guard_open_run` comment no longer describe the old
      synchronous retry.

Confidence: high on the policy and tests; medium on the exact `scheduled`-marker
interpretation of "two history records" (see Risks).
