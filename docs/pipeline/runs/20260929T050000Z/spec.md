# spec: `self-update` — keep the runner checkout current (run 20260929T050000Z)

Implements `docs/process/issues/053-runner-checkout-self-update.md`. No code here;
this is the plan the implementer stage codes against.

## Problem

The Pi runs herdr-routines from **two** checkouts, and only one of them maintains itself:

| Checkout | Used by | Updated by |
| --- | --- | --- |
| `~/projects/herdr-routines` | `herdr-routines.service`, `-watchdog.service`, `-digest.service` (`WorkingDirectory=`, `uv run herdr-routines …`) | **a human, by hand** |
| `~/.local/state/herdr-routines/repos/herdr-routines` | the `feature-pipeline` job's `repo:` → `scripts/pipeline-launch.sh` + the orchestrator's `uv run herdr-routines gate/pick-feature` | `ensure_repo` (`repos.py:42`) → `_fetch_and_fast_forward` (`repos.py:93`), every run |

The second row is automatic; the first is the one that actually executes `tick`. So a
fix merged to `main` reaches the pipeline launcher the next night while the tick,
watchdog and digest keep running whatever someone last pulled. The two halves drift
apart and disagree — `docs/process/pi-update-runbook.md` line 8 already calls the
runner "**Manual**".

**Incident 2026-09-27.** The runner was stuck at `01e6fd9` (PR #116, 2026-09-10) for 17
days — missing #117–#127, including #121 (routine-job retry), #122 (retention), #123
(model validation) and #125 (pipeline quota fast-fail + `fallback_model` retry). On
2026-09-20 the pipeline hit the OpenCode quota; the **new** `pipeline-launch.sh` wrote
`## Outcome: failed (quota_exhausted)`, but the **old** tick had no `quota_exhausted`
branch in `_classify_pipeline_outcome` (`tick.py:1614`) so it recorded
`reason: orchestrator_failed` and never launched the `fallback_model` retry #125 had
added 8 days earlier. #125 shipped, was green, was merged — and silently did not run on
the only host it was written for.

Two structural causes, both fixable by one command:

1. The manual fast-forward. `docs/process/pi-update-runbook.md` exists precisely to
   document it, including its two manual guards: **green CI** (step 0, "never ship a red
   runner") and **config migrations** (a reshaped schema can make an old job entry
   *silently ignored* rather than rejected, dropping the job with exit 0 — the PR #56
   and issue 049 examples). Both guards are worth keeping; neither is worth a human
   remembering them 17 times in a row.
2. Nothing in the running system notices it. The digest reports job outcomes, not the
   version of the code that produced them. The staleness is invisible until it bites.

The failure mode is worse than a missing feature: it is a *plausible* one. The host
looks healthy, the unit is green, jobs run — they just run last month's code.

## Approach

Add a `herdr-routines self-update` subcommand, driven by its own systemd timer
(`herdr-routines-update.{service,timer}`, once a day at 21:30, `Persistent=true`,
before the nightly jobs). New module `src/herdr_routines/self_update.py` holds the
logic; `cli.py` gets a thin handler, following the `sync-repo` / `pipeline-watchdog`
precedent (a small module + a `_cmd_*` in `cli.py`).

The process runs from the **currently installed (old) code** and exercises the **new**
code **out of process**, so a commit that doesn't even import is caught and rolled back
rather than wedging the host. Each step below is one small, separately-testable function
in `self_update.py`; the tests named in the issue map onto them one-for-one.

### Sequence

```
run_self_update(checkout, lock_path, history_path, config, gh, notify, runner=...)
  1. tick_lock(lock_path)                 -> not acquired: log "deferred", rc 0
  2. pipeline_run_open(config, history)   -> open run: log "deferred", rc 0
  3. repo_state(checkout)                 -> dirty / not main: notify, rc 1
  4. git fetch; new = rev-parse origin/main
     new == old                            -> log, rc 0
     commit_ci_state(gh, owner, repo, new) -> not green: log "deferred", rc 0
  5. _fetch_and_fast_forward(checkout, base="main")  -> not ff: notify, rc 1
  6. validate_subprocess(checkout)        -> rc!=0 or job count/name-set dropped:
                                              git reset --keep old, notify, rc 1
  7. deploy_changes(checkout, old, new)   -> list in the notification, never applied
  8. log "self-update: <old>..<new> (n commits)"; notify only if the SHA moved
```

**1. `tick_lock`.** Reuse `tick.tick_lock` (`tick.py:85`) on `tick.default_lock_path()`
(`tick.py:74`) — the *same* exclusive `flock` on `~/.local/state/herdr-routines/tick.lock`
that `_cmd_tick` holds (`cli.py:416`). It is already non-blocking (`LOCK_NB`), so "did not
acquire" is immediate and needs no timeout loop: log `self-update: deferred (tick lock
held)` and return 0. The lock is held for the **whole** fetch → CI → fast-forward →
validate → (rollback) sequence, not just the git step, so `validate` can never run
against a checkout a concurrent tick is mid-way through using.

**2. Defer while a pipeline run is open.** `pipeline_run_open(config, history_path)`
iterates `config.jobs` where `job.kind == "pipeline"` and returns the first
`tick._open_pipeline_run(history_path, job.name)` (`tick.py:1637`) hit. Deliberately that
function and **not** `is_currently_running`: `_open_pipeline_run` asks "is there a run in
flight" without folding in a staleness clock, which is exactly the question — and it
already documents *why* the two are separate predicates. Log `deferred`, rc 0.

This guard is not redundant with the lock. A `kind: pipeline` job's real work runs in a
**detached** `systemd-run` unit (`_build_pipeline_launch_argv`, `tick.py:1674`) that holds
no tick lock for the whole (hours-long) orchestrator run; the lock is released as soon as
`tick` returns. So the lock protects the launch/reconcile boundary and this guard
protects the in-flight run. Swapping the reconcile code mid-run is the one moment worth
avoiding.

**3. Refuse a dirty / non-`main` / diverged checkout.** `repo_state(checkout)` runs
`git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD` and `git status --porcelain`
and returns `(old_sha, branch, dirty)`. Non-`main` or dirty → **notify and exit 1,
touching nothing**. A hand-hotfixed host is a legitimate state (the 2026-09-27 incident
was fixed by hand) and clobbering it is worse than staying stale. No `reset --hard`, no
force, anywhere in this command.

**4. Green-CI gate on the target SHA.** `git fetch origin` (via
`_fetch_and_fast_forward`'s sibling — see step 5), then `new = git rev-parse
origin/main`. If `new == old`, log and exit 0 quietly, no notification (an unchanged SHA
is not news). Otherwise query the *commit's* check runs:
`gh api repos/{owner}/{repo}/commits/<new>/check-runs`, with `owner`/`repo` from
`gates.remote_owner_and_repo(checkout)` (`gates.py:82`, already reads `git remote
get-url origin`). A new `commit_check_runs(*, owner, repo, sha) -> list[dict]` goes on
the `GhClient` protocol and `RealGhClient` (`auto_fix.py:32` / `auto_fix.py:72`); the
existing test fakes are duck-typed and need no change.

Evaluation is `gates.evaluate_commit_checks` — a **new, deliberately stricter** function
sitting next to `gates.evaluate_ci_checks` (`gates.py:117`) so the difference is
reviewable side by side:

- any check still pending (`status != "COMPLETED"`, reusing `gates._is_pending_check`,
  `gates.py:101`) → defer;
- an **empty** check list → defer (`run_ci_gate` treats "no checks" as pass, which is
  right for a PR whose CI hasn't been *created* yet and wrong for "we could not confirm
  this commit is green");
- a conclusion outside `{success, skipped, neutral}` → defer. This is the meaningful
  divergence: `evaluate_ci_checks` fails only on `FAILURE` and tolerates everything
  else, but for a commit we are about to *deploy* a `CANCELLED` / `TIMED_OUT` /
  `ACTION_REQUIRED` / `STARTUP_FAILURE` runner is not green, and an unknown conclusion
  string is not evidence of anything;
- `gh` raising (`RuntimeError` from `RealGhClient._run`, expired auth, no network) →
  defer.

Every deferral logs a `self-update: deferred (<reason>)` line at `warning` and exits **0**.
Fail-closed: an unverifiable commit is not updated to. The human merge to `main` is
already the real gate; this only makes sure the host never runs something red.

**5. Fast-forward only.** Call `repos._fetch_and_fast_forward(checkout, base="main")`
(`repos.py:93`) — the exact primitive `ensure_repo` uses and `sync-repo` wraps
(`cli.py:911`). It is `git fetch --prune origin` + `git merge --ff-only origin/main`, and
raises `RuntimeError` on a non-ff merge. That `RuntimeError` maps to **notify and exit 1**:
a diverged history is a human decision, and the command must not resolve it by force.
(That helper also short-circuits on detached HEAD — for a detached runner checkout it
would fetch and silently do nothing, so `self_update` treats a detached `HEAD` as the
non-`main` refusal in step 3 rather than reporting a false "updated".)

**6. Out-of-process `validate`, and rollback.** `validate_subprocess(checkout)` runs
`[uv_bin, "run", "herdr-routines", "validate"]` with `cwd=checkout` as a
`subprocess.run`, so the *new* code, the *new* import graph, the *new* config schema and
the *new* `uv.lock` are all exercised against the host's live `jobs.d/`. This is the whole
point of validating out of process: the parent already has the old code imported, so an
import error in the new code is data, not a crash.

Job count is parsed from the existing pass line `ok: N job(s) valid` (`cli.py:660`).
A **baseline** count/name-set is captured by the same subprocess call *before* the
fast-forward. If that pre-update call fails, log a warning and continue with **no**
baseline — the count-drop check is then skipped and only the exit-code check applies
(exit 0 here, not a permanent block: the host was already broken and staying frozen
would help nobody).

On failure — non-zero exit, unparseable output, or the new code reporting **fewer** jobs
than the baseline, or having **dropped a job name** it previously reported — roll back:

```
git -C <checkout> reset --keep <old>
```

`--keep` (never `--hard`) rewinds HEAD, index and worktree, and *refuses* rather than
discarding if anything would be overwritten. Then notify with the validate stdout/stderr
and exit 1. Comparing **name sets**, not just counts, is what makes the migration hazard
from the runbook ("a reshaped schema can make an old job entry silently ignored") visible:
a single dropped job is a rollback even if a concurrently added one masks the count.

**7. `deploy/` changes: report, never apply.** `deploy_changes(checkout, old, new)` runs
`git diff --name-only <old>..<new> -- deploy/`. Non-empty → include the file list in the
success notification. Systemd units, `opencode.pipeline.json` and the example `jobs.d/`
are **host config** with their own install steps (`deploy/README.md`); applying them from
a timer is out of scope and is the one thing here that could take the scheduler down.

**8. Success logging.** `log.info("self-update: %s..%s (%d commits)", old, new, n)`
with `n` from `git rev-list --count old..new`. Notify only when the SHA actually moved
(step 4 already returned for `new == old`).

### Why a separate timer, not a step inside `tick`

- The code changes at one predictable time, before the night's runs, rather than between
  a pipeline launch and its reconcile.
- A failing update cannot block a tick: `tick_lock` is non-blocking both ways, so ticks
  keep running the old code and the update simply retries tomorrow.
- `digest` and `pipeline-watchdog` share the same checkout; updating from inside any one
  of them would be arbitrary.

### Why no restart

Each tick is a fresh `uv run herdr-routines tick` process, so the next tick picks up the
new code with no `daemon-reload`; `uv run` re-syncs `.venv` when `uv.lock` changed
(`docs/process/pi-update-runbook.md` lines 16–18 already documents this for the manual
path).

### CLI surface

`cli.py::_build_parser`, next to `sync-repo` (a pure-git, Herdr-free command):

- `self-update [--path PATH] [--base main] [--dry-run]`

`--path` defaults to `Path.cwd()` — same convention as `refine-issue`'s `--repo`
(`cli.py:334`) — and the unit supplies `WorkingDirectory=%h/projects/herdr-routines`,
exactly as the other three units do. `--dry-run` is optional and cheap: it runs steps
1–4, prints the intended `old..new` and deploy diff, and stops. It exists so the manual
runbook path can preview what the timer would have done.

`_cmd_self_update` builds `HerdrClient()` for notifications (as `_cmd_digest` does) and
`RealGhClient()` for the CI query, wraps everything in `tick_lock`, and maps
`SelfUpdateResult.status` to an exit code. Config comes from `load_config(default_config_path())`
— via plain `load_config`, **not** `_load_config_or_exit` (`cli.py:398`): a `ConfigError`
here means the host's own config no longer loads, which is a notify-and-exit-1 condition,
not a `SystemExit`. Notifications reuse `tick._notify` (`tick.py:2030`), which is
best-effort by contract.

Exit codes: `up_to_date` / `updated` / `deferred` → **0**; `refused` (dirty, non-main,
diverged) and `rolled_back` → **1**.

### systemd

`deploy/systemd/herdr-routines-update.service`, modeled line-for-line on
`herdr-routines-digest.service`: `Type=oneshot`, `WorkingDirectory=%h/projects/herdr-routines`,
`After=/Wants=herdr-server.service`, the same explicit
`Environment=PATH=%h/.local/bin:...` (a user unit does not inherit the login PATH — the
comment in the digest unit explains why), `ExecStart=%h/.local/bin/uv run herdr-routines
self-update`, and a **finite** `TimeoutStartSec` (~600s: a fetch, one `gh` call and two
`uv run validate` subprocesses, generously bounded; never `infinity`, per
`docs/plan-v1.md` §3).

`deploy/systemd/herdr-routines-update.timer`: `OnCalendar=*-*-* 21:30:00`,
`Persistent=true`, `AccuracySec=5min` — before the nightly jobs, and the same
`AccuracySec` the digest timer uses.

### Docs

- `deploy/README.md` — add the unit pair to the `cp` block and the `systemctl --user
  enable --now` list, with a paragraph in the style of the watchdog/digest ones
  explaining why it is a standalone unit (it dispatches no job and spawns no agent).
- `docs/process/pi-update-runbook.md` — "When to run" points at the timer once it
  ships. **The runbook's closing "Notes / gotchas" bullet must change too**: it currently
  states the manual step "is the deliberate design: the release/update strategy on
  `ROADMAP.md` Parking lot carves out the runner fast-forward as a human/`update` action,
  keeping self-update off the always-on Pi." Issue 053 reverses exactly that decision, so
  leaving the bullet would leave the repo contradicting itself. The runbook stays as the
  manual fallback and as the home for config-migration notes.

## Files touched

- `src/herdr_routines/self_update.py` — **new**. `SelfUpdateResult` dataclass
  (`status: up_to_date|updated|deferred|refused|rolled_back`, `old`, `new`, `reason`,
  `deploy_changes`, `validate_output`) plus `run_self_update` and the small helpers
  `repo_state`, `pipeline_run_open`, `commit_ci_state`, `validate_subprocess`,
  `deploy_changes`. Every impure call goes through an injectable `runner=`
  (defaulting to `subprocess.run`, matching `gates`' `runner=` convention) and an
  injectable `gh`/`notify`, so the whole flow is testable without a real host — but see
  the note on git below.
- `src/herdr_routines/auto_fix.py` — add `commit_check_runs(*, owner, repo, sha) ->
  list[dict[str, object]]` to the `GhClient` protocol and to `RealGhClient`
  (`gh api repos/{owner}/{repo}/commits/<sha>/check-runs`). No behavior change to
  existing methods; the four duck-typed test fakes need no edit.
- `src/herdr_routines/gates.py` — add `evaluate_commit_checks(checks) -> GateVerdict`
  beside `evaluate_ci_checks` (`gates.py:117`): pending / empty / non-`{success,skipped,
  neutral}` all defer. `evaluate_ci_checks` stays untouched and keeps its
  FAILURE-only semantics for PRs; the divergence is deliberate and documented in both
  docstrings.
- `src/herdr_routines/cli.py` — register the `self-update` subparser in
  `_build_parser` and add `_cmd_self_update`; update the module docstring's
  subcommand list on line 1.
- `deploy/systemd/herdr-routines-update.service` — **new**.
- `deploy/systemd/herdr-routines-update.timer` — **new**.
- `deploy/README.md` — install + enable instructions, rationale paragraph.
- `docs/process/pi-update-runbook.md` — "When to run" → the timer; amend the
  "manual step is the deliberate design" note.
- `tests/test_self_update.py` — **new**. The eight tests named in issue 053:
  `test_self_update_fast_forwards_and_validates`,
  `test_self_update_rolls_back_on_validate_failure`,
  `test_self_update_defers_while_pipeline_run_open`,
  `test_self_update_refuses_dirty_or_diverged`,
  `test_self_update_respects_tick_lock`,
  `test_self_update_reports_deploy_changes`,
  `test_self_update_defers_on_non_green_ci`,
  `test_self_update_rolls_back_on_job_count_drop`.
  Git state is exercised for real: a temp bare repo plus a clone, following
  `tests/test_sync_repo.py::_init_bare_git_repo` (already proven in this suite)
  rather than mocking `subprocess.run`. `validate` and the notification are faked
  behind their seams; the *lock*, the *branch state*, the *fast-forward*, the
  *rollback* and the *deploy diff* are real.
- `tests/test_gates.py` — add unit tests for `evaluate_commit_checks` (empty, pending,
  `CANCELLED`, `TIMED_OUT`, mixed).
- `docs/pipeline/runs/20260929T050000Z/spec.md` — this file.

Not touched: `tick.py` (its `tick_lock` / `_open_pipeline_run` are imported, not
modified), `config.py`, `history.py`, `runner.py`. The issue file's `status:` flips in
the implementing PR, per `docs/process/pi-update-runbook.md`.

## Risks

1. **`git reset --keep` can leave residue.** The one thing that can dirty the tree
   between step 3 and the rollback is the `validate` subprocess itself: `uv run` re-locks
   and re-syncs when `pyproject.toml`/`uv.lock` changed, and `uv` may rewrite `uv.lock`
   in the checkout. `--keep` then either refuses (exit 1, checkout still at `new`, **not**
   rolled back — this is the dangerous case) or rolls back leaving a modified `uv.lock`.
   Mitigations: after a failed rollback, re-check `git status --porcelain` and, if the
   `reset` itself failed, notify loudly with the HEAD SHA it is stuck on rather than
   trying anything stronger; never `--hard`, never `--quiet`-and-hope. The stale
   `uv.lock` self-heals — the next `uv run` re-resolves from whatever the checkout
   contains. A stuck-on-`new` checkout is loud and manual, which is the correct outcome
   for a host we could not prove healthy.
2. **The venv mutates under a running process.** The parent holds old code in memory
   while the child re-syncs `.venv` in place. Safe by construction (Python does not
   re-read imported modules), but it constrains the implementation: after the
   fast-forward, `run_self_update` must **not** import or lazily load any module whose
   on-disk version has changed. Everything needed afterwards is computed before the
   ff, or comes from the subprocess output.
3. **A permanently-deferring host looks green.** `gh` unauthenticated, offline, or a
   commit with no checks all exit **0** with a `warning`-level log line, so systemd shows
   the unit as succeeded and nothing notifies. A host that never self-updates for a
   month looks exactly like a host that self-updates every day. Mitigations: the
   `deferred (<reason>)` line is written daily to `herdr-routines.log` so it is greppable,
   and the manual runbook remains the fallback. **Residual risk, named rather than
   hidden** — a natural follow-up is surfacing "runner not updated in N days" in the
   digest, which is out of scope here.
4. **Job-count drop is a heuristic with false positives.** A legitimate future change
   could make an old job entry invalid-by-design, and we would roll back nightly. This
   is the intended trade (the runbook's failure mode — a silently dropped job, reported
   `ok` — is exactly the class of plausible-but-wrong unattended failure this project
   keeps designing against), and rolling back is the safe direction. Name-set comparison
   (not count alone) keeps the signal sharp: a genuine addition is never a rollback.
5. **Lock contention is one-sided but complete.** `tick_lock` is non-blocking, so
   `self-update` never delays a tick and a tick never delays `self-update` — it just
   skips the day. A tick that runs 8 hours (long gated job) means the daily window is
   often missed; that is a correct trade (updating under a live tick is the thing to
   avoid) and costs at most one day's staleness.
6. **The two-checkouts confusion is now in code.** `--path` defaults to `Path.cwd()`, so
   running `self-update` from the `repos/` checkout would update the wrong tree. Mitigation:
   every notification and log line carries the absolute path acted on, and the unit pins
   `WorkingDirectory=`. Worth a line in `deploy/README.md` too.
7. **`deploy/` drift is reported, never applied.** A `deploy/` change to a unit that has
   since been hand-edited on the host will keep being listed after it is first applied,
   since the diff is always `old..new` and never a "host already has this" comparison.
   Acceptable — the list is advisory and re-listing a known-applied file is a mild
   annoyance, not a defect. Auto-detecting already-applied host config is a non-goal.
8. **Non-goals, restated so they do not creep in:** no automatic `deploy/` application, no
   pinned deploy branch/tag (the host follows `main`; CI + the human merge is the gate),
   no logic-regression detection (`validate` proves "loads and accepts the live config",
   not "behaves correctly"), and no updating the `repos/` job checkout (`ensure_repo`
   already does that).
