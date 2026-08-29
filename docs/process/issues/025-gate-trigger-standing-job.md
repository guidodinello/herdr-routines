---
id: "025"
title: "Gate-triggered standing job (auto_fix.trigger: gate)"
status: open
priority: high
area: pipeline
gate: PR #53 (feature spec) merged to main
---

## Description

Second trigger mode for the auto-fix PR standing job (issue 015, PR #50):
run an arbitrary gate command list first and only spawn the expensive fix
agent when the gate fails. The Raspberry Pi hygiene routine: `uv run ruff
check .` + `uv run mypy` daily, agent only when they turn red. Clean ticks
cost zero — no agent, no research, no PR.

Full design: `docs/pipeline/features/gate-trigger-standing-job.md` (PR #53).
Config shape:

```yaml
auto_fix:
  trigger: gate                  # "pr" (default) | "gate"
  gate_commands:                 # run sequentially, cwd = worktree of base
    - uv run ruff check .
    - uv run mypy
  gate_command_timeout_ms: 120000
  timeout_ms: 1800000            # fix worker budget (reused)
  max_attempts_per_pr: 3         # budget per gate branch (reused)
```

## Acceptance

- All `gate_commands` exit 0 → `done`, no agent, no branch, worktree removed,
  `extra.gate == "passed"` (test_auto_fix_gate_pass_no_dispatch).
- Any command non-zero → exactly one worker on `auto/<job>-<ts>`, prompt
  carries the failing command + captured output (test_auto_fix_gate_fail_dispatches_worker).
- Command timeout counts as gate-failed, does not crash the tick
  (test_auto_fix_gate_command_timeout).
- `max_attempts_per_pr` budget respected per gate branch, tap out as
  `skipped`/`max_attempts_exceeded` + `_notify`
  (test_auto_fix_gate_respects_max_attempts_per_pr).
- `trigger: gate` requires non-empty `gate_commands`; invalid `trigger` and
  non-positive `gate_command_timeout_ms` rejected
  (test_auto_fix_gate_config_validation).
- `_check_systemd_timeout` budgets gate jobs as
  `start_timeout_ms + n_cmds × gate_command_timeout_ms + timeout_ms`, not
  `max_prs_per_tick × timeout_ms` (test_auto_fix_gate_systemd_timeout_budget).
- All 12 existing pr-mode acceptance tests still pass with `trigger: pr`
  default (test_auto_fix_pr_trigger_unchanged).
- Passing gate produces zero `gh`/agent/push activity
  (test_auto_fix_gate_clean_run_no_gh_activity).

## Log

- **2026-08-29**: idea proposed by the user after PR #50 review work — the
  pipeline's hard-coded PR-eligibility gate (`auto_fix.py` `is_eligible`) is
  blind to repo-hygiene debt on `main` (30 ruff + 18 mypy on the PR branch
  2026-08-29). Written up as
  `docs/pipeline/features/gate-trigger-standing-job.md` and parked as PR #53
  (spec + commented `repo-hygiene` example in `jobs.example.yaml`) so the
  pipeline can pick it up via `pick-feature` once #53 merges. `gate` and `pr`
  triggers compose: `gate` opens the debt-exposing PR, the same job in `pr`
  mode babysits it on later ticks.
