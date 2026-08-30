---
id: "025"
title: "Auto-fix standing job: checks + target (unified gate model)"
status: done
priority: high
area: pipeline
gate: PR #53 (feature spec) merged to main
---

## Description

Unify the auto-fix PR standing job (issue 015, PR #50) around one generic
model: **a job runs an agent; `checks` optionally gate it — all pass → skip,
free; any failure → spawn the fix agent.** Replaces both the "second trigger
mode" sketch (`auto_fix.trigger: pr|gate`) and the later three-axis draft
(`auto_fix:{scope,prechecks,...}`): there is no `auto_fix` container and no
`scope` axis. Babysit's CI/threads eligibility and a repo-hygiene
`ruff`/`mypy` check are the *same* signal ("a check failed"); the only
structural difference is *what gets fixed*, surfaced as one optional
`target: pr | base` (inferred from the check kinds: `pr_health` ⇒ `pr`,
`command` ⇒ `base`). The Raspberry Pi use case:
`uv run ruff check .` + `uv run mypy` daily, agent only when they turn red.
Clean ticks cost zero — no agent, no research, no PR.

**Routing rule (the discriminator is the `checks` field):** no `checks`
(absent or `[]`) ⇒ plain job, never enters the gated path; `checks` with
`pr_health` ⇒ `target: pr` (the PR #50 baseline); `checks` all `command` ⇒
`target: base`. `pr_health` + `command` in one job is rejected. Two-phase
workflows (failing base → debt-exposing PR, then babysit that PR) are **two
jobs**, each with its own `checks`.

Full design: `docs/pipeline/features/gate-trigger-standing-job.md`.
Config shape (flat — no `auto_fix` wrapper; fix agent = the job's own agent
fields):

```yaml
- name: repo-hygiene            # plain job + checks = conditional job
  cron: "0 13 * * *"
  max_workers_per_tick: 1       # dispatch cap (base target: at most 1/tick)
  max_attempts_per_target: 3    # retry budget: intra-occurrence; cron is the daily rate limit
  checks:                       # ordered, run-all-no-short-circuit; ANY failure dispatches
    - command: uv run ruff check .   # cwd = target worktree; inherits the tick's env
      timeout_ms: 120000
    - command: uv run mypy
      timeout_ms: 120000
  agent_kind: opencode
  model: null                   # null → agent-kind default model
  prompt: ""                    # empty = engine-injected fix prompt
  timeout_ms: 1800000

- name: babysit-prs
  cron: "*/10 * * * *"
  max_workers_per_tick: 3
  max_attempts_per_target: 3
  checks:
    - pr_health                # builtin; infers target: pr; polls gh, never spawns
  agent_kind: opencode
  model: null
  prompt: ""
  timeout_ms: 1800000
```

Notes for the implementer (2026-08-29):
- `pr_health` **polls, never spawns** — one `gh pr list`/tick, cheap per-PR CI+threads queries;
  detection is expressible as a `command` check too, but the *fixer mechanics* (worktree pinned
  to the PR head, push to its branch, `resolveReviewThread`) are what `target: pr` keeps
  engine-side.
- `target` is optional; inference (`pr_health` ⇒ `pr`, commands ⇒ `base`) covers the real cases.
  A job has exactly one target. An explicit override of the inferred target is **reserved for
  future check kinds** (how a `command` check would select a PR is not yet specified); for now it
  must equal the inferred value, mismatch rejected at config.
- Base-target `gate_slop` in the systemd budget covers worktree add/remove + `pr_health` polling
  and doubles as criterion 7's "+ margin" — one term, can't drift.
- Base-target worktree path must be unique per `run_id` (catch-up collapse can overlap two
  occurrences). Checks inherit the tick's env; only the worker gets the fuller agent env.
- `repo-hygiene` example in `deploy/jobs.example.yaml` uses `cron: "0 13 * * *"` — 13:00, not
  03:00: the overnight pipeline fires at 02:00 on this repo and builds its own PRs; 13:00 catches
  the pipeline's leftover lint/type debt before the next night's build.
- The Pi's shipped `babysit-prs` migrates to the flat shape **gaining `checks: [pr_health]`**
  (that line is what routes it into the pr-target path).

## Acceptance

- All `checks` pass → `done`, no agent, no branch, base-target worktree removed,
  `extra.gate == "passed"` (test_auto_fix_gate_pass_no_dispatch).
- Any check non-zero → exactly one worker dispatched (per failing target), prompt
  carries all failing checks + captured output, no short-circuit
  (test_auto_fix_gate_fail_dispatches_worker).
- Check timeout counts as gate-failed, does not crash the tick
  (test_auto_fix_gate_command_timeout).
- `max_attempts_per_target` budget respected per target — base: within one
  occurrence (fresh gate branch per cron fire); pr: across the PR's lifetime
  (stable `pr_number`); tap out as `skipped`/`max_attempts_exceeded` +
  `_notify` (test_auto_fix_gate_respects_max_attempts_per_target).
- Config validation: check kinds parse (`command` or `pr_health`, not both in
  one job); invalid `target` rejected; explicit `target` must equal the
  inferred value (mismatch = "not yet supported"); `base` validated as a
  non-empty string (pure schema — ref existence is a runtime tick check,
  fail-closed as a gate error); non-positive per-check `timeout_ms`
  rejected; no `checks` (absent or `[]`) = unconditional plain job
  (test_auto_fix_gate_config_validation).
- `_check_systemd_timeout` applies the corrected general form — base-target as
  `start_timeout_ms + gate_slop + n_checks × timeout_ms + timeout_ms` (single
  `Σ check.timeout_ms`; worker re-runs checks inside its own `timeout_ms`) and
  pr-target-with-commands as `start_timeout_ms + gate_slop + n_checks ×
  timeout_ms + max_workers × timeout_ms + max_workers × n_checks × timeout_ms`;
  no double-count of `n_checks × timeout_ms` for base
  (test_auto_fix_gate_systemd_timeout_budget).
- All 12 existing pr-scope acceptance tests still pass with baseline
  `checks: [pr_health]` (test_auto_fix_pr_trigger_unchanged).
- Clean base-target gate produces zero `gh`/agent/push activity; pr-target
  allows reads-only `gh` polling (test_auto_fix_gate_clean_run_no_gh_activity).

## Log

- **2026-08-29**: idea proposed by the user after PR #50 review work — the
  pipeline's hard-coded PR-eligibility gate (`auto_fix.py` `is_eligible`) is
  blind to repo-hygiene debt on `main` (30 ruff + 18 mypy on the PR branch
  2026-08-29). Written up as
  `docs/pipeline/features/gate-trigger-standing-job.md` and parked as PR #53
  (spec + commented `repo-hygiene` example in `jobs.example.yaml`) so the
  pipeline can pick it up via `pick-feature` once #53 merges. Gate and pr
  triggers compose: the gate opens the debt-exposing PR, the same job babysits
  it on later ticks.
- **2026-08-29**: unified (attempt 1) — replaced the "second trigger" framing
  (`trigger: pr|gate`, `gate_commands`) with a three-axis model: one
  `auto_fix` job = `scope` (target: `pr` | `branch`) + `prechecks` (ordered,
  `pr_health` builtin and/or `command`s) + shared fixer budget (commit
  `6b22a4c`, PR #55). Rationale: babysit's CI/threads signal is the same
  "cheap predicate" as a hygiene command. Docs-only.
- **2026-08-29**: redesign review — user and herdr claude-sonnet pane compared
  Design A (`auto_fix:{scope,prechecks}`) vs Design B (checks + agent). Verdict:
  B, amended. `auto_fix` is redundant nesting; the gate's presence is the mode.
  B's hidden "check kind ⇒ fixer target" coupling is a default, not a contract —
  add explicit optional `target`. Budgets (`max_workers_per_tick`,
  `max_attempts_per_target`) are dispatch concerns → hoist to job level, not
  under `agent`. Rewrote spec + issue + example to the flat shape (commit
  `a925978`); amended PR #55. Docs-only; no config landed so the rename carries
  no migration burden.
- **2026-08-29**: spec review round 2 (same herdr claude-sonnet pane) —
  verdict: "needs revision, 2 blocking contradictions, ~9 nits". Blocking:
  (a) the `auto_fix:` deletion left jobs unroutable — fixed with the explicit
  routing rule (no `checks` = plain; `pr_health` = pr baseline; commands =
  base) and criterion 8 rebased to `checks: [pr_health]`; (b) the "one job
  composes pr/base" claim contradicts single-shot target inference — dropped;
  two phases are two jobs. Also: budget formula now covers pr-target-with-
  commands + a `gate_slop` term; "clean run is free" split per target
  (pr allows reads-only `gh` polls); bare `pr_health` syntax; run-all-no-
  short-circuit; `failed_checks` list naming; `<base>` defined; test renamed
  `..._respects_max_attempts_per_target`. Docs-only.
- **2026-08-29**: spec review round 3 (same pane) — verdict: "nearly there —
  1 real contradiction and 1 formula mismatch; ~4 nits". Fixed: target
  override reframed (single target per job; override is whole-job, never
  per-check); systemd general form corrected so `Σ check.timeout_ms` is
  added per-worker only for pr target (base adds it once — the worker
  re-runs checks inside its own `timeout_ms`), criterion 7 re-aligned;
  attempt-budget scope split per target (base: per-occurrence; pr: across
  the PR's lifetime); `base` listed in Files-touched + criterion 5 matrix;
  criterion 5 "command and/or pr_health" → "not both"; criterion 1 worktree
  removal scoped to base target. Docs-only.
- **2026-08-29**: spec review round 4 (final pass, same pane) — verdict:
  "one trim from ready — 2 residual text contradictions". Fixed:
  (a) runaway-risk bullet rewired to the budget split (base: intra-occurrence
  gate branch; pr: across the PR's lifetime); (b) explicit `target` override
  deferred — reserved for future check kinds, must equal the inferred value
  for now (mismatch rejected "not yet supported"), criterion 5 + config
  validation updated; (c) `max_prs_per_tick` → `max_workers_per_tick` rename
  called out in the migration bullet; (d) `base` added to criterion 5 matrix. Docs-only.
- **2026-08-29**: spec review round 5 (sign-off check, same pane) —
  verdict: **READY WITH NITS**. Round-4 items all landed; full re-read
  internally consistent. Nits accepted: criterion 5 `base` reworded to
  "non-empty string" at config (pure schema) with ref existence as a
  runtime tick check (fail-closed gate error); `gate_slop` value left to
  implementation but must be named in the implementing PR so the test
  asserts a real number. Docs-only.
