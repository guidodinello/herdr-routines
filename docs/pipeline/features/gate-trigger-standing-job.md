# Feature spec — gate-triggered standing job (`auto_fix.trigger: gate`)

Adds a second trigger mode to the auto-fix PR standing job (PR #50, run `20260829T050025Z`):
run an arbitrary "gate" command list first, and only spawn the (expensive) fix agent when the
gate fails. The pattern you described — a Raspberry Pi routine that runs `uv run ruff check .` +
`uv run mypy` daily and triggers the fixer agent only when they turn red.

## Problem

The auto-fix standing job's eligibility gate is **hard-coded to PR signals**: a routine-owned
`auto/*` PR is eligible only when CI is red or review threads are unresolved
(`src/herdr_routines/auto_fix.py:361` `is_eligible`). That is exactly the right gate for
PR babysitting, but it is blind to repo-hygiene debt that lives on `main` — e.g. the
30 `ruff` + 18 `mypy` errors present on the PR branch on 2026-08-29 (pre-existing from the
auto-fix feature module, parent `115c254` had 25/19, `005417d`-era head had 30/18). Nothing in
the scheduler watches for that, because nothing runs a command and reacts to its exit code.

The generic need: **"run my commands, if they fail spawn the agent, if they pass do nothing —
and charge me nothing when clean."** A clean tick must cost zero (no agent, no research, no PR).

## Approach

Generalize the auto-fix standing job's trigger. Today's behavior stays `pr` (unchanged, byte-for-byte
the PR #50 implementation). New `gate` mode:

```
auto_fix:
  trigger: gate                     # "pr" (default) | "gate"
  gate_commands:                    # required iff trigger=gate; run sequentially, cwd=worktree
    - uv run ruff check .
    - uv run mypy
  gate_command_timeout_ms: 120000   # per-command budget
  timeout_ms: 1800000               # fix worker budget (reused as-is)
  max_attempts_per_pr: 3            # budget per gate branch (reused as-is)
  agent_kind: opencode
  model: null
```

All commands passing → `done`, no agent, worktree removed. Any command non-zero → dispatch one fix
worker on a fresh `auto/<job>-<scheduled_occurrence>` branch, prompt seeded with the failing
command + captured output; worker fixes, pushes, and opens a PR. The PR it opens carries the
`auto/` prefix, so the **same job in `pr` mode babysits it on the next tick** — the two triggers
compose: `gate` creates the debt-exposing PR, `pr` keeps it green.

### Gate semantics inside the tick

In `_process_auto_fix_job` (`src/herdr_routines/tick.py:118`), after the standard guards
(`has_ever_been_seen` / `find_stale_running` / `is_currently_running` / `_live_agent_exists`,
`tick.py:106`), branch on `af.trigger`:

- `trigger: pr` → existing path (`list_open_prs` → `is_eligible` → cap → attempt-check → dispatch).
  Unchanged.
- `trigger: gate`:
  1. `git worktree add <wt-aside> <base>` (or reuse the `herdr worktree create` seam
     `src/herdr_routines/tick.py:519` uses for PR checkout; gate uses `base`, not a head branch).
  2. Run `gate_commands[i]` sequentially via subprocess, each bounded by
     `gate_command_timeout_ms`, stdout+stderr captured to `gate_output_path`.
  3. All exit `0` → append `HistoryRecord(state="done", extra={..., "gate": "passed"})`,
     remove worktree, `return summary, False`. **No agent.**
  4. Any non-zero or timeout → gate failed. Retry budget: count prior terminal records for
     job+gate branch (mirrors `attempt_count_for_pr` `src/herdr_routines/auto_fix.py:380`, keyed
     by branch instead of `pr_number`). `>= max_attempts_per_pr` → skip + `_notify`, same
     `reason: max_attempts_exceeded` treatment as pr mode.
  5. Else dispatch one worker: branch `auto/<job>-<ts>` (reuse `build_branch_name`
     `src/herdr_routines/runner.py:262`), agent name `build_gate_worker_agent_name(job, run_id)`
     → `rt-<job>-gate-<run_id>` truncated to 32 (`NAME_RE` cap, same rule as
     `build_worker_agent_name` `src/herdr_routines/auto_fix.py:436`), prompt from
     `build_gate_fix_prompt` (below).
  6. After worker: per-attempt `HistoryRecord(extra={branch, attempt, reason, fix_worker_agent,
     pane_id, report_path, report_written, final_agent_status})` (shape identical to pr mode
     `tick.py:373`), aggregate report lists gate passed/failed + branch,
     `any_failed` flips on `failed`/`interrupted_unknown` only.

### Prompt: `build_gate_fix_prompt`

Mirrors `build_fix_prompt` (`src/herdr_routines/auto_fix.py:394`) but sourced from the gate:

> You are fixing a failing gate on branch auto/<job>-<ts>. The commands that failed:

> `$GATE_OUTPUT`

> Fix the code, run the gate commands yourself to verify, run `uv run pytest -q`, commit, push,
> then open a PR via `gh pr create --base <base> --title ... > --body "..."`.

Drops the pr-mode steps that make no sense for a self-owned branch: no "reply to review threads",
no `resolveReviewThread`, no thread bodies (those arrive later, via pr-mode-once-the-PR-exists).

### History states

No new state file — reuse `history.jsonl`, terminal states `running`/`done`/`failed`/`skipped`/
`missed`/`interrupted_unknown`. New `extra` keys on gate records: `gate: passed|failed`,
`gate_command`, `gate_output_path`, `branch`. Attempt derivation stays append-only over
`history.jsonl` per the existing pattern (`src/herdr_routines/history.py:112`).

### Systemd timeout (`_check_systemd_timeout`)

`src/herdr_routines/cli.py:444` already budgets `auto_fix` jobs as
`start_timeout_ms + max_prs_per_tick * timeout_ms` (PR #50 finding K). That formula is wrong for
`trigger: gate`: a gate tick spawns **at most one** worker, plus the gate command budget. Worst case
per tick must become trigger-aware:

- `trigger: pr` → `start_timeout_ms + max_prs_per_tick * timeout_ms` (unchanged).
- `trigger: gate` → `start_timeout_ms + sum(gate_command_timeout_ms × n_cmds) + timeout_ms`.

Without the branch, a gate job under-budgets by `(max_prs_per_tick − 1) × timeout_ms` in the old
formula (or over-budgets after my PR #50 fix). Either way `TimeoutStartSec` is wrong.

## Files touched

- `src/herdr_routines/config.py` — extend `AutoFixConfig` (`config.py:125`) + `_AUTO_FIX_ALLOWED_KEYS`
  (`config.py:82`) + `_AUTO_FIX_DEFAULTS` (`config.py:94`) with `trigger`, `gate_commands`,
  `gate_command_timeout_ms`; validate `gate` ⇒ non-empty `gate_commands`, `pr` ⇒ no `gate_commands`
  (or ignored), `trigger ∈ {pr, gate}`.
- `src/herdr_routines/auto_fix.py` — `build_gate_fix_prompt`, `build_gate_worker_agent_name`,
  a pure `run_gate_commands(commands, timeout_ms, cwd) -> GateOutcome` (injectable subprocess
  runner, keeps the module's "pure-ish, no subprocess except via injection" posture per
  `docs/pipeline/runs/20260829T050025Z/spec.md:73`).
- `src/herdr_routines/tick.py` — `trigger` branch in `_process_auto_fix_job` (`tick.py:118`);
  worktree create/remove for the gate worktree; dispatch on non-zero gate.
- `src/herdr_routines/cli.py` — trigger-aware worst case in `_check_systemd_timeout` (`cli.py:444`).
- `src/herdr_routines/runner.py` — reuse `build_branch_name`, `substitute_prompt`,
  `_prompt_with_watchdog`, `default_reports_dir` unchanged; no new primitive.
- `tests/test_auto_fix.py` — gate command runner (pass/fail/timeout/order), dispatch-on-fail,
  pass-no-dispatch, prompt content, attempt budget.
- `tests/test_tick.py` — gate tick integration: no agent when clean, one dispatch when red,
  `any_job_failed` semantics, worktree removed on pass.
- `tests/test_config.py` — `trigger`/`gate_commands`/`gate_command_timeout_ms` validation matrix.
- `tests/test_cli.py` — plus the existing `test_auto_fix_job_counts_max_prs_per_tick_worst_case`
  stays green; add gate-budget variant.
- `deploy/jobs.example.yaml` — sample `repo-hygiene` job (ruff + mypy gate) so the Pi derates to:
  daily gate, agent only when red.

## Risks

- **Runaway gate → infinite agent loop.** Mitigation: reuse `max_attempts_per_pr` budget (counted
  per gate branch over `history.jsonl`), tap out as `skipped`/`max_attempts_exceeded` + `_notify`,
  identical to pr mode. Failures do not silently retry forever.
- **Non-hermetic gate.** `gate_commands` run in a worktree of `base`, but their *availability*
  (e.g. `uv` on the Pi, env vars) is host-specific. Mitigation: gate run on a machine where a
  command is missing must fail-closed as gate-failed dispatch (it will spawn the worker with the
  "command not found" output; if the worker can't help, attempts tap out); document in
  `deploy/jobs.example.yaml` that gates must be reproducible from the machine's own env.
- **Time-bounded gate.** A slow/hanging gate must not wedge the tick. Mitigation: per-command
  `gate_command_timeout_ms`, subprocess `timeout=`; timeout counts as gate-failed, not crash.
- **Branch collision on retry.** Repeated gate failures on `auto/<job>-<ts>` (same scheduled
  occurrence can't recur, but catch-up collapse and retry ticks can). Mitigation: reuse the
  scheduled-occurrence timestamp name (unique per occurrence, same scheme the runner already uses);
  if the branch exists, `_dispatch_fix_worker`'s existing "remove stale worktree `--force` then
  re-add" (`tick.py:521`) handles the worktree side; the worker checks `gh pr list` for the branch.
- **Scope creep against pr mode.** Gate mode must not disturb pr-mode behavior or its 12 existing
  acceptance tests. Mitigation: `trigger` defaults to `pr`; gate path is a sibling branch in
  `_process_auto_fix_job`, not a modification of the enumeration path.

## Acceptance criteria

1. **gate passes:** all `gate_commands` exit 0 → no agent spawned, no branch created, `HistoryRecord`
   `done` with `extra.gate == "passed"`, worktree removed, `any_failed` false. Test: test_auto_fix_gate_pass_no_dispatch
2. **gate fails:** any command non-zero → exactly one worker dispatched, prompt contains the failing
   command and its captured output, per-attempt record written with `reason: gate_failed`,
   `any_failed` false unless the worker fails. Test: test_auto_fix_gate_fail_dispatches_worker
3. **command timeout:** a command exceeding `gate_command_timeout_ms` counts as gate-failed and
   dispatches (does not crash the tick) with the timeout in the output. Test: test_auto_fix_gate_command_timeout
4. **attempt budget:** after `max_attempts_per_pr` gate-failing attempts for a branch, subsequent
   ticks skip with `skipped`/`reason: max_attempts_exceeded` + `_notify`, no worker spawned.
   Test: test_auto_fix_gate_respects_max_attempts_per_pr
5. **config validation:** `trigger: gate` requires non-empty `gate_commands`; `trigger` values
   outside `{pr, gate}` rejected; `gate_command_timeout_ms` must be positive; pr-mode default
   `trigger: pr` unchanged. Test: test_auto_fix_gate_config_validation
6. **branch + agent name:** gate worker branch is `auto/<job>-<ts>`; agent name
   `rt-<job>-gate-<run_id>` fits the 32-char `NAME_RE` cap. Test: test_auto_fix_gate_branch_and_agent_name
7. **systemd budget:** `_check_systemd_timeout` budgets gate jobs as
   `start_timeout_ms + n_cmds × gate_command_timeout_ms + timeout_ms` (+ margin) and does **not**
   apply `max_prs_per_tick × timeout_ms` to them. Test: test_auto_fix_gate_systemd_timeout_budget
8. **pr mode untouched:** all 12 existing pr-mode acceptance tests (run `20260829T050025Z`) still
   pass with `trigger: pr` default. Test: test_auto_fix_pr_trigger_unchanged
9. **clean run is free:** a passing gate produces zero `gh`/`gh api`/agent/push activity (spy
   harness from `test_auto_fix...` asserts only git worktree + gate subprocess calls).
   Test: test_auto_fix_gate_clean_run_no_gh_activity
