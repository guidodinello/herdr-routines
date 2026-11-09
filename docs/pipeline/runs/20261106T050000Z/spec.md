# Issue 079 — A long run holds the tick lock and blocks every scheduled job

## Problem

`tick.lock` is a single global flock held for an entire synchronous dispatch. `herdr-routines
run` (issue 074) and Telegram `/job` (issue 075) take it for the whole run, exactly like a
scheduled tick that runs a routine job in-process. A 30–60 minute agent run therefore:

- makes every scheduled tick exit quietly (skip-not-queue) for the whole run — on 2026-10-23 a
  manual reviewer run held the lock while `babysit-prs` was silently skipped;
- refuses a request run outright while a tick holds the lock;
- makes `self-update` burn its 5-minute `TICK_LOCK_WAIT_S` (`self_update.py:119`) before
  deferring, every night it lands on a long run.

The lock exists for issue 004's guarantees, and those must survive:
1. history JSONL appends are not corrupted by concurrent writers;
2. one live `rt-<job>` agent per job — no two dispatches of the same job overlap (shared agent
   name, so the second would fail agent-name uniqueness and wedge on `agent_name_live`);
3. a job's checkout (managed clone under `default_repos_dir()` or a shared pre-existing
   `repo:` path) is not mutated by two runs at once (`ensure_repo` + worktree add, and for
   `workspace: root` the run works *in* the checkout).

## Approach — option 1: per-job dispatch lock + short global lock

Not option 2 (detached dispatch like `kind: pipeline`): routine/gated/audit dispatch is a
long synchronous flow (settle loop, post-run verify, tails, notifications) with no reconcile
machinery behind it; that re-architecture is issue 019's shape and much larger. Option 1 keeps
dispatch code synchronous and untouched, and only changes lock granularity around it.

Two locks, replacing the one-everything lock:

- **Global tick lock** (`tick.lock`, unchanged path): held only for *brief* critical sections
  — the per-job schedule decision (`schedule.decide` + registration + stale-running rewrite,
  and appends that belong to it), the tmp-hygiene preamble, and history rotation. Acquired
  non-blocking by the tick as today; a tick that cannot get it for a decision phase exits
  quietly (rc 0, same systemd contract as now).
- **Per-job dispatch lock**, new: flock on `locks/job-<name>.lock` in the state dir, held for
  the *entire* synchronous dispatch (`dispatch_occurrence` and everything downstream). Taken
  after the global decision phase is released; non-blocking, so a second dispatcher of the
  same job records `skipped` (reason `dispatch_in_flight`) instead of running.

Flow per job (all `dispatch_occurrence` callers — `run_tick`, `_cmd_run`, `/job` request
launch — converge on this, so tick and manual paths cannot drift):

1. Global lock (brief): decide; no dispatch here. Release.
2. Job lock (non-blocking): lost → `skipped` record (skip-not-queue, unchanged semantics).
3. Won → capture `now` at dispatch time (see risks), re-verify not-currently-running /
   agent liveness, append `running`, dispatch synchronously, terminal record.

`herdr agent list`-liveness checks (`agent_name_live`) move inside the job-locked section;
they stay as a second guard behind the flock.

Consequence: a long run of job A holds only `job-A.lock`; tick and `run` of job B proceed.
`self-update` must still never swap code under a running dispatch (issue 053), and the
global lock no longer covers dispatches, so it takes the global lock *and* every configured
job's dispatch lock, polling within `TICK_LOCK_WAIT_S` and releasing all of them between
polls (amended after review, see below).

Files touched (test-fixture seams consulted; nothing else exists):

- `src/herdr_routines/tick.py`: new `job_lock()` context manager and per-job state; restructure
  the job loop in `run_tick` / `_process_*_job` into decide-under-global + dispatch-under-job;
  `dispatch_occurrence` unchanged in signature, its take-the-locks contract made explicit.
- `src/herdr_routines/history.py`: `append()` becomes internally lock-guarded (flock on a
  `history.lock` sibling, short bounded wait then error) — this is what makes AC 3 true for
  the concurrent writers of step 1-3 across jobs and manual runs, without threading a lock
  through ~40 call sites; `maybe_rotate()` takes the same lock (its docstring already assumes
  it runs under the tick lock).
- `src/herdr_routines/cli.py`: `_cmd_run` takes the per-job lock instead of the global tick
  lock; refusal now means "this job's dispatch is already running" (which is also no longer a
  30-minute sentence). `/job` request path needs no separate change — it calls `run` detached.
- `src/herdr_routines/self_update.py`: hold the global lock plus every job's dispatch lock
  across the swap (amended after review: "no behavioural change" would have let it
  fast-forward under a live run); fix the doc comments whose premise ("a tick holds it for
  the length of a run") is now false.
- `src/herdr_routines/repos.py` / dispatch call sites: a per-checkout lock covering
  `ensure_repo` (brief) for worktree-mode, and the whole dispatch for `workspace: root` —
  two jobs of the same repo must not share git state (issue 036 already showed this class
  of collision). The lock file lives in the state dir (`locks/checkout-<name>-<hash>.lock`,
  keyed by the resolved path), not at `<clone>/.routines-checkout.lock` (amended after
  review, see below).
- Tests: AC tests named exactly as the issue lists them —
  `test_long_run_does_not_block_other_jobs`, `test_same_job_never_dispatched_concurrently`,
  `test_concurrent_history_appends_are_atomic` — plus updates where existing tests assume the
  global lock is held for dispatch (`test_tick.py`, `test_manual_run.py`, `test_history.py`,
  `test_self_update.py` doc-comment assertions).

## Risks

- **Race between decide and dispatch:** two processes may both decide job A is due before
  either wins the job lock. Loser records `skipped` (`dispatch_in_flight`); the occurrence
  is consumed once, by the winner's terminal record, never by the loser's skip (which
  `last_scheduled_terminal_run` ignores). Acceptable (matches skip-not-queue), but the
  `running` append must move *into* the job-locked section so `is_currently_running` is
  visible to the loser.
- **`ts=now` timestamp discipline:** much of tick.py writes records with the tick-start `now`
  (e.g. `_record_unexpected_failure` closes open running records by `ts == now`). Under
  concurrent dispatch, timestamps must be captured per dispatch, not per tick-process.
- **Lock ordering:** per-checkout lock inside job lock, history lock innermost (flock inside
  `append`). No code may acquire a job lock first and then need the global lock for a long
  section; global sections that fire while job locks are held (rotation at tick end) must
  skip/warn, not wait, since the holder may be an hour away.
- **Readers of the JSONL** (`status`, `digest`, `prune`, reconcile) can now read while a
  writer appends. Pre-existing readers tolerate a malformed trailing line; verify, and keep
  each record a single under-lock write.
- **root-mode serialization** is per-checkout, not global: two *different* repos' root jobs
  now run concurrently — that is the point — but anything global assuming "one dispatch at a
  time" (e.g. `validate`'s `TimeoutStartSec` worst-case budget comment, tick.py:3311) must be
  re-read, not blindly kept.
- **`pipeline` kind** stays detached and untouched by the job lock (it already holds no lock
  for its run); only its launch boundary flows through the per-job lock like everything else.

## Acceptance criteria

Each criterion has a tier (`blocking` / `non-blocking`) and a `confidence:` rating. Test
names are exact — the three new tests are named in the issue; the others are existing tests
this change must keep green (with the updates noted in Files touched).

**Blocking**

1. **blocking, confidence: high** — AC 1: while job A's dispatch holds its per-job lock for a
   long run, job B dispatches normally via tick and manual `run` (the 2026-10-23
   `babysit-prs` silent skip cannot recur). Test: `test_long_run_does_not_block_other_jobs`
2. **blocking, confidence: high** — AC 2: two dispatches of the same job never overlap; a
   dispatcher that loses the job lock records `skipped` with reason `dispatch_in_flight`,
   and issue 004's one-live-agent guarantee holds. A tick that already sees the job's run in
   flight skips without a record, as before this change (amended after review). Neither
   consumes a cron occurrence. Tests: `test_same_job_never_dispatched_concurrently`,
   `test_dispatch_lock_loser_records_skip_without_consuming_the_occurrence`
3. **blocking, confidence: high** — AC 3: history appends are atomic under concurrent
   writers across jobs and manual runs — multi-process writers produce a JSONL where every
   line is valid JSON with no interleaving. Test: `test_concurrent_history_appends_are_atomic`

**Non-blocking**

4. **non-blocking, confidence: medium** — `_cmd_run` refusal semantics change from "global
   tick lock held" to "this job's dispatch already in flight", and the refusal no longer
   waits a long run out. Test: `test_manual_pipeline_run_refuses_while_a_run_is_open`
5. **non-blocking, confidence: medium** — `self-update` never swaps code under a running
   dispatch: it holds the global lock and every job's dispatch lock across the swap, and a
   lock still held after `TICK_LOCK_WAIT_S` defers with a notification naming it (amended
   after review). Tests: `test_self_update_respects_tick_lock`,
   `test_self_update_defers_while_a_job_dispatch_lock_is_held`,
   `test_self_update_holds_every_job_lock_across_the_swap`
6. **non-blocking, confidence: low** — history rotation while job locks are held skips/warns
   instead of waiting on a holder that may be an hour away; no deadlock from the documented
   lock ordering (per-checkout inside job lock, history innermost).
   Test: `test_run_tick_rotates_and_survives_rotation_error`
7. **non-blocking, confidence: low** — two jobs sharing a repo's checkout cannot mutate git
   state concurrently (`ensure_repo` brief lock for worktree mode; whole dispatch for
   `workspace: root`). Tests: `test_dispatch_reuses_existing_worktree`,
   `test_root_jobs_sharing_a_checkout_never_overlap`,
   `test_checkout_sync_waits_for_another_jobs_lock`
8. **non-blocking, confidence: medium** — `/job` request launches need no separate change
   (they converge on `run`, which takes the per-job lock). Test: `test_tick_and_manual_run_share_dispatch`

## Verification

`uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`; manual tier-3
spot-check of one long `run` coinciding with a tick that dispatches an unrelated job.
Issue doc 079 gets a Log entry when this lands.

## Changelog v1→v2

- Renamed `## Acceptance` to `## Acceptance criteria` and expanded the three-issue-mapped
  ACs into tiered items (`blocking` / `non-blocking`) each ending in `Test: <exact name>`,
  with `confidence:` per item. Criteria 1–3 (blocking) are unchanged in substance from v1;
  criteria 4–8 (non-blocking) pin down the guarantee-preserving side effects v1 only
  described in prose (run refusal semantics, self-update, rotation/deadlock, per-checkout
  lock, `/job` convergence).
- Added `## Changelog v1→v2` (this section).
- No changes to Problem, Approach, or Risks: the design, file list, and risk register are
  carried over from v1 verbatim; v2 only strengthens the acceptance mapping.

## Amendments after review (PR #255)

The review found places where v2, implemented as written, broke a guarantee it meant to
keep. Each amendment below is reflected in the sections above.

- **A skip must not consume a cron occurrence (issue 074 AC 3).** Every skip record the
  dispatch writes is stamped with the occurrence's `trigger`, so a refused manual or request
  run is off-schedule. `dispatch_in_flight` and `checkout_busy` skips (and
  `collapsed_earlier_occurrences`) are listed in `history.NOT_A_RUN_REASONS`, which
  `last_scheduled_terminal_run` ignores: they report contention, not a run.
- **The decision phase keeps main's guard order.** Open-run check, then `agent_name_live`,
  then triggers. With the global lock no longer spanning a run, ticks now run *during* a
  job's own long or manual run; checking the live agent first wrote a trigger-less
  `agent_name_live` record every tick, which consumed the occurrence. A tick that sees the
  run in flight therefore skips without a record, and AC 2's `dispatch_in_flight` record is
  written only by a dispatcher that actually loses the job lock (the decide/dispatch race).
- **Stale reaping takes the job lock.** A stale-looking `running` record is reconciled to
  `interrupted_unknown` only while the tick holds the job lock (non-blocking); a held lock
  proves the run alive, because flock dies with its process.
- **An event's watermark lands only when its dispatch starts.** The firing poll's
  `last_seen_event_ids` record is written after the job lock and guards pass, just before
  the run, so a skipped dispatch leaves the event to fire on a later tick.
- **Manual refusals are reported.** Every way a decided manual or request run can fail to
  start is a refusal: rc 1, plus a notification for `/job`. A manual run trusts the job
  lock alone for "a run is in flight" (as `run` did under the old global lock), so a fresh
  `running` record left by a dead process does not block a retry.
- **Self-update holds the job locks** (AC 5 above).
- **Per-checkout lock location (AC 7).** v2 named `<clone>/.routines-checkout.lock`. A lock
  file inside the checkout is an untracked file there: `git status` turns dirty (self-update
  and the dirty-worktree checks read it) and a `workspace: root` agent's `git add -A` could
  commit it. It lives in the state dir instead, keyed by the resolved checkout path. A
  `workspace: root` dispatch takes it non-blocking for the whole run (waiting would stall
  every later job in a sequential tick) and records `checkout_busy` when it loses; a sync
  waits for it up to `CHECKOUT_LOCK_WAIT_S`, then fails as `CheckoutBusy` (a
  `RuntimeError`, so callers keep their sync-failure handling); the event poll, which runs
  under the global lock, only tries it once and skips quietly. The lock is re-entrant per
  thread, so a root dispatch's own `ensure_repo` does not wait on itself. Worktree creation
  (`herdr worktree create` / `git worktree add`) is not wrapped: git serializes it with its
  own lock files, and AC 7 only asks for the sync.
