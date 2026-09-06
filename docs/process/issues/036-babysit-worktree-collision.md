---
id: "036"
title: "babysit-prs can never fix a pipeline PR: worktree collision on the retained branch"
status: open
priority: high
area: infra
---

## Description

`babysit-prs` is the safety net that catches PRs the pipeline left unfinished. It
is structurally incapable of running on the PRs it most needs to fix.

`tick._dispatch_fix_worker` (`src/herdr_routines/tick.py`, ~line 984) checks out
the PR head branch into its own worktree:

```python
wt_path = Path(job.repo) / ".worktrees" / f"autofix-pr{pr.number}"
subprocess.run(["git", "-C", str(job.repo), "worktree", "remove", "--force", str(wt_path)], ...)
proc = subprocess.run(["git", "-C", str(job.repo), "worktree", "add", str(wt_path), pr.head_ref], ...)
```

It removes a stale worktree at *its own* path first, but the branch is held by a
*different* worktree: the orchestrator's, which `docs/pipeline/orchestrator-prompt.md`
**deliberately retains** at end of run (G-10: "do **not** `herdr worktree remove`
the shared worktree (would destroy branch to keep)"; G-14: "Future `gc` must
exclude `auto/pipeline-*`").

Git refuses the second checkout. Reproduced live on the Pi, 2026-09-05:

```
$ git worktree add /tmp/probe-autofix-pr81 auto/pipeline-20260904T050000Z
fatal: 'auto/pipeline-20260904T050000Z' is already used by worktree at
       '/home/guido/.herdr/worktrees/herdr-routines/auto-pipeline-20260904t050000z'
```

Two features that were each correct in isolation cancel each other out. There are
20+ retained `auto/pipeline-*` worktrees on the Pi, so this is the normal case,
not an edge case.

Observed consequence on PR #81: eligible (`unresolved_threads`) at 05:30, 05:40
and 05:50 on 09-04, three dispatches, all `state: failed` with `pane_id: null`
(died before pane creation), then `max_attempts_per_target: 3` tripped. It has
reported `Skipped (attempts): 1` on every tick since and will never retry.

**Attribution caveat:** because the history record drops `reason` (see below), the
three 09-04 failures cannot be *confirmed* as this specific error —
`repo_sync_failed` was also firing intermittently that night and produces the same
`pane_id: null` signature. The collision above is permanent and reproducible
regardless, so it must be fixed either way.

### 36b — the dispatch failure reason is discarded

`_dispatch_fix_worker` returns `{"state", "reason", "error", ...}` on every failure
path (`clone_failed`, `repo_sync_failed`, `report_dir_creation_failed`,
`worktree_creation_failed`, `agent_start_failed`, `agent_not_interactive`). The
history record built at `tick.py:411-422` copies only `pane_id`, `report_path`,
`report_written` and `final_agent_status` — **`reason` and `error` are dropped.**
The tick-level log prints `done (enumerated=…, dispatched=…)` regardless of
per-PR outcome.

Result: `history.jsonl` shows bare `"state": "failed"` with no cause, and
diagnosing this required a live repro on the host. Any dispatch failure is
currently un-triageable from the logs alone.

### 36c — both agent-name builders are wrong

`auto_fix.build_worker_agent_name` truncates to 32 chars:

```python
raw = f"rt-{job_name}-pr{pr_number}-{run_id}"   # run_id is itself "<job>-<ts>"
return raw[:32]
```

For `babysit-prs` / PR 81 that yields `rt-babysit-prs-pr81-babysit-prs-` — the job
name appears twice and the timestamp, the only part making the name unique per
attempt, is truncated away entirely. Every attempt collides on one name.

Separately, the liveness guard at `tick.py:382` looks up
`build_pr_agent_name(...)` → `rt-babysit-prs-pr81`, a name that is **never
created** by the dispatcher. The double-dispatch guard (review finding E) can
therefore never fire.

## Design (proposal)

1. **Reuse an existing checkout instead of forcing a second one.** Before
   `worktree add`, look for the branch in `git worktree list --porcelain`; if a
   worktree already has it checked out, run the fix worker there (after verifying
   it is clean and at the PR head). Only `worktree add` when nothing holds it.
2. **Propagate `reason` and `error`** into `extra_record` so `history.jsonl`
   records why a dispatch failed, and log per-PR failures at WARNING.
3. **Fix the name builders**: drop the redundant job-name prefix from the run_id
   segment and truncate from the middle (or hash the tail) so the timestamp
   survives; make the liveness guard check the name the dispatcher actually
   creates.
4. **Reset PR #81's attempt counter** once the above lands, or it stays skipped.

## Acceptance criteria

1. A PR whose head branch is already checked out in another worktree dispatches successfully. Test: `test_dispatch_reuses_existing_worktree`
2. A dispatch failure records `reason` in the history record. Test: `test_dispatch_failure_records_reason`
3. `build_worker_agent_name` keeps the run timestamp distinct across attempts. Test: `test_worker_agent_name_unique_per_attempt`
4. The liveness guard checks the same name the dispatcher creates. Test: `test_live_agent_guard_matches_dispatch_name`
