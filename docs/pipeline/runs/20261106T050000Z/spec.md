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
`self-update` contends for the *global* lock only, which is held seconds not hours — the
`TICK_LOCK_WAIT_S` polling loop stays as written (it just never realistically fires).

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
- `src/herdr_routines/self_update.py`: no behavioural change expected; fix the doc comments
  whose premise ("a tick holds it for the length of a run") is now false.
- `src/herdr_routines/repos.py` / dispatch call sites: a per-checkout lock
  (`<clone>/.routines-checkout.lock`) covering `ensure_repo` (brief) for worktree-mode, and
  the whole dispatch for `workspace: root` — two jobs of the same repo must not share git
  state (issue 036 already showed this class of collision).
- Tests: AC tests named exactly as the issue lists them —
  `test_long_run_does_not_block_other_jobs`, `test_same_job_never_dispatched_concurrently`,
  `test_concurrent_history_appends_are_atomic` — plus updates where existing tests assume the
  global lock is held for dispatch (`test_tick.py`, `test_manual_run.py`, `test_history.py`,
  `test_self_update.py` doc-comment assertions).

## Risks

- **Race between decide and dispatch:** two processes may both decide job A is due before
  either wins the job lock. Loser records `skipped`; occurrence consumed once. Acceptable
  (matches skip-not-queue), but the `running` append must move *into* the job-locked section
  so `is_currently_running` is visible to the loser.
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

## Acceptance

Mapping to the issue's blocking ACs:

1. AC 1 — job A's per-job lock held does not stop job B dispatching: `test_long_run_does_not_block_other_jobs`.
2. AC 2 — two dispatches of one job never overlap: `test_same_job_never_dispatched_concurrently`.
3. AC 3 — history appends atomic under concurrent writers:
   `test_concurrent_history_appends_are_atomic` (multi-process writers → every line valid
   JSON, no interleaving).

Verification: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`; manual
tier-3 spot-check of one long `run` coinciding with a tick that dispatches an unrelated job.
Issue doc 079 gets a Log entry when this lands.
