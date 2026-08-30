# spec: Auto-fix standing job — checks + target (unified gate model) (20260830T050021Z)

Per-run spec at `docs/pipeline/runs/20260830T050021Z/spec.md` (G-15: per-run path avoids PR #28/#29 shared-path conflict). Implements `docs/process/issues/025-gate-trigger-standing-job.md` as designed in `docs/pipeline/features/gate-trigger-standing-job.md`.

## Problem

The auto-fix standing job gate is hard-coded to PR signals: a routine-owned `auto/*` PR is eligible only when CI is red or review threads are unresolved (`src/herdr_routines/auto_fix.py:361` `is_eligible`). Correct for PR babysitting, blind to repo-hygiene debt on `base` — e.g. 30 `ruff` + 18 `mypy` errors on `main` on 2026-08-29 while the scheduler had no command-exit-code gate. The generic need is **"run commands, if any fail spawn the fix agent, if all pass do nothing — clean ticks cost zero (no agent, no branch, no PR)"**. Existing `auto_fix` container (`src/herdr_routines/config.py:82` `_AUTO_FIX_ALLOWED_KEYS` / `src/herdr_routines/tick.py:118` `_process_auto_fix_job`) couples the fixer's target to that hard-coded PR enumeration and cannot express a `ruff`/`mypy` gate on `base`.

## Approach

Unify around one model: **a job runs an agent; `checks` optionally gate it — all pass → skip/free; any failure → spawn the fix agent.** Replaces `auto_fix` trigger modes with a flat `checks` field, `target` `pr|base` inference, and `max_workers_per_tick` dispatch caps.

### Unified model

- `checks` = the gate. Ordered, run-all-no-short-circuit; any non-zero or timeout dispatches. No `checks` (absent or `[]`) → plain job, never enters the gated path.
- Agent behavior = the job's existing fields (`agent_kind`, `model`, `prompt`, `timeout_ms`). Presence of `checks` is the mode — no wrapper encodes "act only when broken".
- Budgets hoisted to job level: `max_workers_per_tick` (dispatch cap) and `max_attempts_per_target` (retry budget keyed per target). Replaces per-agent budgets.
- `target: pr | base` — what the fixer repairs; **inferred** from check kinds, optional explicit override reserved for future kinds (must equal inferred value, mismatch rejected "not yet supported"):

| check kind | inferred target | fixer behavior |
|---|---|---|
| `pr_health` builtin (polls owned `auto/*` PRs: CI red OR threads unresolved; `is_eligible` `auto_fix.py:361`) | `pr` | worktree at PR head → push fix to its branch → reply + `resolveReviewThread` |
| `command` (`uv run ruff check .`, `uv run mypy`, … exit code decides) | `base` | worktree at `base` → fix → new `auto/<job>-<ts>` branch → open PR |

Mixed `pr_health` + `command` in one job rejected at config. Two-phase workflows (failing base → debt-exposing PR, then babysit that PR) are two jobs.

```yaml
- name: repo-hygiene
  cron: "0 13 * * *"
  max_workers_per_tick: 1
  max_attempts_per_target: 3
  checks:
    - command: uv run ruff check .
      timeout_ms: 120000
    - command: uv run mypy
      timeout_ms: 120000
  agent_kind: opencode
  prompt: ""          # empty → engine-injected fix prompt
  timeout_ms: 1800000

- name: babysit-prs   # PR #50 baseline migrates to flat shape gaining checks: [pr_health]
  cron: "*/10 * * * *"
  max_workers_per_tick: 3
  max_attempts_per_target: 3
  checks: [{pr_health:}]  # bare pr_health; infers target: pr
  agent_kind: opencode
  prompt: ""
  timeout_ms: 1800000
```

`pr_health` polls, never spawns — one `gh pr list`/tick plus per-PR CI/threads queries; detection is expressible as a `command` but fixer mechanics (worktree pinned to PR head, `resolveReviewThread`, per-PR cap keyed by `pr_number`) stay engine-side via `target: pr`.

### Semantics inside the tick

`_process_auto_fix_job` (`src/herdr_routines/tick.py:118`) after standard guards (`has_ever_been_seen` / `find_stale_running` / `is_currently_running` / `_live_agent_exists`, `tick.py:106`) routes on `checks` (plain path if absent/empty):

- `target: pr` (via `pr_health`): `list_open_prs` → `pr_health` (`is_eligible`) → cap `max_workers_per_tick` → per-PR `attempt_count_for_pr` (`auto_fix.py:380` keyed by `pr_number`) → dispatch per flagged PR. Byte-for-byte PR #50 behavior with `checks: [pr_health]`.
- `target: base` (via commands):
  1. `git worktree add <wt-aside> <base>` (same seam `tick.py:519`; `base` reuses existing job field, non-empty string at config, ref existence fail-closed at runtime; path unique per `run_id` for catch-up overlap).
  2. Run all `command` checks sequentially, each bounded by `timeout_ms`, stdout+stderr to `gate_output_path`; no short-circuit.
  3. All pass → `done` with `extra.gate == "passed"`, remove worktree, no agent, `any_failed` false.
  4. Any non-zero/timeout → gate failed; retry budget counts prior terminal records for job+gate-branch (mirrors `attempt_count_for_pr` but keyed by gate branch); `>= max_attempts_per_target` → `skipped`/`max_attempts_exceeded` + `_notify`.
  5. Else dispatch one worker: branch `auto/<job>-<ts>` (`runner.py:262` `build_branch_name`), agent `rt-<job>-gate-<run_id>` truncated to 32 (`NAME_RE` cap, `auto_fix.py:436`), prompt `build_base_fix_prompt` seeded with failing output.

Budget scope split: base → fresh gate branch per cron occurrence, so `max_attempts_per_target` bounds **within one occurrence** and cron is the cross-occurrence rate limiter (perpetual-red base = 1 worker/day, intended); pr → stable `pr_number` key, budget spans **across the PR's lifetime** (existing PR #50 behavior).

Prompt selection: empty `prompt:` → engine injects per target (`build_fix_prompt` `auto_fix.py:394` for pr, new `build_base_fix_prompt` for base seeded with combined gate output + `uv run pytest -q` + `gh pr create --base <base>`); custom `prompt:` replaces wholesale (`tick.py:507`).

History: reuse `history.jsonl` terminal states; new `extra` keys `gate: passed|failed`, `failed_checks`, `gate_output_path`, `target`, `branch`. Attempt derivation stays append-only (`history.py:112`).

Systemd timeout (`src/herdr_routines/cli.py:444` `_check_systemd_timeout`): fixed `gate_slop` (e.g. 60s) covers worktree add/remove + `pr_health` polling and doubles as the "+ margin":

```
start_timeout_ms + gate_slop + Σ check.timeout_ms + (pr ? max_workers : 1) × timeout_ms + (pr ? max_workers × Σ check.timeout_ms : 0)
```

Base has single `Σ check.timeout_ms` (worker re-runs checks inside its `timeout_ms`); pr-with-commands adds `max_workers × Σ check.timeout_ms`. No double-count for base.

Environment: `command` checks inherit the tick's env (scheduler's `uv`/`gh`); fix worker gets the fuller agent env — documented in `deploy/jobs.example.yaml`.

## Files touched

- `docs/pipeline/runs/20260830T050021Z/spec.md` — this file (per-run spec, G-15).
- `src/herdr_routines/config.py` — drop `auto_fix` container; add job-level `checks` (`pr_health` | `{command, timeout_ms}`), `target` (`pr`/`base` inferred), `max_workers_per_tick`, `max_attempts_per_target`; rename `max_prs_per_tick` → `max_workers_per_tick` (`_AUTO_FIX_ALLOWED_KEYS:82` / `_AUTO_FIX_DEFAULTS:94` migrate into `_JOB_ALLOWED_KEYS`); validate: check kinds parse, `target` in `pr|base`, explicit `target` must equal inferred (mismatch "not yet supported"), `pr_health`+`command` rejected, per-check `timeout_ms` positive, `base` non-empty string (pure schema, ref existence runtime in tick), `checks: []` = plain job.
- `src/herdr_routines/auto_fix.py` — `build_base_fix_prompt`, `build_gate_worker_agent_name`, pure `run_checks(checks, cwd, env) -> GateOutcome` (injectable runner, no direct subprocess, preserves pure-ish posture).
- `src/herdr_routines/tick.py` — routing on `checks` in `_process_auto_fix_job` (`tick.py:118`); base-ref existence runtime check fail-closed; `pr_health` reuses `is_eligible`; base worktree create/remove per-`run_id`; dispatch on non-zero/timeout.
- `src/herdr_routines/cli.py` — checks-aware `_check_systemd_timeout` (`cli.py:444`) with `gate_slop` general form.
- `src/herdr_routines/runner.py` — reuse `build_branch_name`, `substitute_prompt`, `_prompt_with_watchdog`, `default_reports_dir`; no new primitive.
- `tests/test_auto_fix.py` — check runner (pass/fail/timeout/order, no short-circuit), dispatch-on-fail, pass-no-dispatch, prompt, attempt budget keyed by target.
- `tests/test_tick.py` — base tick integration: clean → no agent/worktree removed, red → one dispatch, `any_job_failed` semantics.
- `tests/test_config.py` — `checks`/`target` validation matrix (mixed rejection, `checks: []` plain, `target` mismatch).
- `tests/test_cli.py` — systemd budget variants (base single Σ, pr-with-commands multi).
- `deploy/jobs.example.yaml` — sample `repo-hygiene` (`ruff` + `mypy`); Pi `babysit-prs` migrates to flat `checks: [pr_health]`.

## Risks

- **Runaway / perpetual-red base.** Mitigation: `max_attempts_per_target` keyed by gate branch (intra-occurrence) for base and by `pr_number` (across lifetime) for pr; taps out as `skipped`/`max_attempts_exceeded` + `_notify` identical across targets; base additionally rate-limited by cron (1/day for `repo-hygiene`), not a loop — `_notify` on worker failure is the stay-red alert.
- **Non-hermetic / non-existent `base` ref.** `base` validated as non-empty string at config (pure); ref existence checked at runtime in tick fail-closed as gate error; `command` availability host-specific (`uv` on Pi) — check that cannot run fails-closed as gate-failed dispatch with "command not found" output, then attempt budget taps out.
- **Time-bounded gate / wedged tick.** Slow check must not wedge tick. Mitigation: per-check `timeout_ms` via `timeout=`, timeout counts as gate-failed not crash.
- **Branch collision.** `auto/<job>-<ts>` reuse per occurrence plus catch-up overlap. Mitigation: unique per `run_id`; existing `_dispatch_fix_worker` "remove stale worktree `--force` then re-add" (`tick.py:521`) and `gh pr list` branch check handle retry.
- **Regression against pr mode (12 tests).** Mitigation: pr path unchanged — `checks: [pr_health]` is current babysit baseline; base is a sibling branch, not a modification of enumeration.
- **Migration.** Pi shipped `babysit-prs` uses old `auto_fix:` keys. Mitigation: land schema with hard error naming new keys (or compat shim) in same change; flip Pi `jobs.yaml` to flat shape (`checks: [pr_health]`) at deploy; `max_prs_per_tick` → `max_workers_per_tick` rename called out.
- **Systemd budget drift.** Mitigation: single `gate_slop` term covers worktree + `pr_health` polling and the "+ margin" in one constant; `validate --systemd-unit` asserts worst-case against `TimeoutStartSec`.
- **Clean-run cost.** Base clean → zero `gh`/agent/push (spy asserts only `git worktree` + check subprocesses); pr clean → reads-only `gh` polling allowed, assert "no agent, no push".

## Acceptance criteria

1. All `checks` pass → `done`, no agent, no branch, base worktree removed, `extra.gate == "passed"` (`test_auto_fix_gate_pass_no_dispatch`).
2. Any check non-zero → exactly one worker per failing target, prompt carries all failing checks + captured output, no short-circuit (`test_auto_fix_gate_fail_dispatches_worker`).
3. Check timeout → gate-failed dispatch, tick does not crash (`test_auto_fix_gate_command_timeout`).
4. `max_attempts_per_target` per target — base intra-occurrence (fresh gate branch per cron fire), pr across PR lifetime (`pr_number` stable) — then `skipped`/`max_attempts_exceeded` + `_notify` (`test_auto_fix_gate_respects_max_attempts_per_target`).
5. Config validation: `command` or `pr_health` not both; `target` outside `pr|base` rejected; explicit `target` mismatch "not yet supported"; per-check `timeout_ms` positive; `base` non-empty string at config, ref existence runtime; `checks: []` = plain job (`test_auto_fix_gate_config_validation`).
6. `_check_systemd_timeout` general form as in Approach; no double-count for base (`test_auto_fix_gate_systemd_timeout_budget`).
7. 12 pr-scope tests still pass with `checks: [pr_health]` baseline (`test_auto_fix_pr_trigger_unchanged`).
8. Clean base → zero `gh`/agent/push; clean pr → reads-only `gh` polling allowed (`test_auto_fix_gate_clean_run_no_gh_activity`).
