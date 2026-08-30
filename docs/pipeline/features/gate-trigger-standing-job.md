# Feature spec — auto-fix standing job: checks + target (unified gate model)

Unifies the auto-fix standing job (PR #50, run `20260829T050025Z`) around the model an independent
design review picked (claude sonnet, 2026-08-29): **a job runs an agent; `checks` optionally gate
it — all pass → skip, free; any failure → spawn the fix agent.** There is no `auto_fix` container,
no second trigger kind, no `scope` axis. A gated job is a plain job plus a `checks` list, plus two
budget keys. The Raspberry Pi driver: a `repo-hygiene` routine running `uv run ruff check .` +
`uv run mypy` daily, agent only when they turn red.

## Problem

The auto-fix standing job's gate is **hard-coded to PR signals**: a routine-owned `auto/*` PR is
eligible only when CI is red or review threads are unresolved (`src/herdr_routines/auto_fix.py:361`
`is_eligible`). That is the right gate for PR babysitting, but it is blind to repo-hygiene debt
that lives on `main` — e.g. the 30 `ruff` + 18 `mypy` errors present on the PR branch on
2026-08-29 (pre-existing from the auto-fix feature module, parent `115c254` had 25/19,
`005417d`-era head had 30/18). Nothing in the scheduler watches for that, because nothing runs a
command and reacts to its exit code.

The generic need: **"run my commands, if they fail spawn the agent, if they pass do nothing — and
charge me nothing when clean."** A clean tick must cost zero (no agent, no research, no PR).

## Unified model

Two concepts, nothing else:

- **`checks`** = the gate. Ordered; **all** must pass. Any non-zero (or timeout) dispatches the
  agent. A plain job simply omits `checks` and always runs.
- **`agent` behavior** = the job's existing agent fields (`prompt`, `agent_kind`, `model`,
  `timeout_ms`). Presence of `checks` is what makes it *conditional* — the gate's presence is the
  mode; no wrapper encodes "act only when broken".

Two budget keys, hoisted to job level (they are dispatch/gate concerns, not agent properties):
`max_workers_per_tick` (how many fix workers may run in one tick) and `max_attempts_per_target`
(retry budget, keyed per target so nothing loops forever).

One optional field: **`target: pr | base`** — *what the fixer repairs*. It is **inferred** from the
check kinds so the common cases need no explicit thought:

| check kind | inferred target | fixer behavior |
| --- | --- | --- |
| `pr_health` builtin (polls my owned `auto/*` PRs: CI red OR threads unresolved) | `pr` | worktree at that PR's head → push fix to its branch → reply + resolve threads |
| `command` (any executable, exit code decides) | `base` | worktree at `base` → fix → new `auto/<job>-<ts>` branch → open PR |

`target` is **optional** because inference covers the real cases. An explicit override of the
inferred target is **reserved for future check kinds** (e.g. a command check that should fix a PR
instead of `base` — how the PR would be selected is not yet specified). Until then, `target`
must equal the inferred value and a mismatch is rejected at config time. A job has exactly **one**
target; `target` applies to the whole job, never per-check. It is a seam, not an axis.

```
- name: repo-hygiene               # plain job + checks = conditional job
  cron: "0 13 * * *"
  max_workers_per_tick: 1          # dispatch cap (base target: at most 1 worker/tick)
  max_attempts_per_target: 3       # retry budget, keyed per target (here: the gate branch)
  checks:                          # ordered; ANY failure dispatches the fix agent
    - command: uv run ruff check . #   cwd = the target worktree
      timeout_ms: 120000           #   per-check budget (default: 120000)
    - command: uv run mypy
      timeout_ms: 120000
  agent_kind: opencode             # fix agent = the job's agent fields, no separate block
  model: null                      # null → agent-kind default model
  prompt: ""                       # empty → engine-injected fix prompt seeded with failing check output
  timeout_ms: 1800000              # fix worker budget

- name: babysit-prs
  cron: "*/10 * * * *"
  max_workers_per_tick: 3          # one worker per flagged PR, ≤3/tick
  max_attempts_per_target: 3       # retry budget per PR
  checks:
    - pr_health                    # builtin; infers target: pr; polls gh, never spawns
  agent_kind: opencode
  model: null
  prompt: ""                       # engine fix prompt seeded with the flagged PR's CI+threads output
  timeout_ms: 1800000
```

### Routing: which jobs enter the auto-fix path

The two job dialects are mutually exclusive, and the discriminator is the `checks` field itself —
the removal of the `auto_fix:` container does not leave jobs unroutable:

- **No `checks`** (field absent or `[]`) → a **plain job**: every scheduled occurrence runs the
  agent. It never enters `_process_auto_fix_job`'s gated path. `checks: []` is validated as the
  same thing as absent, so it cannot slip into the gated path.
- **`checks` containing `pr_health`** → `target: pr` (inference rule below) → the **PR #50 path**:
  the existing babysit behavior is the `checks: [pr_health]` baseline, not "no checks".
- **`checks` all `command`** → `target: base` → the **new base-target path** below.

Mixed `pr_health` + `command` in one job is **rejected at config time** — a target is inferred
once, and one job repairs one kind of unit of work. Two-phase workflows (open the debt-exposing PR
from a failing base, then babysit that PR) are therefore **two jobs**, each with its own `checks`:
a base-target `repo-hygiene` job and a `pr_health` `babysit-prs` job. This is exactly how the Pi
already runs them.

### Vocabulary — why `checks` alone replaces `auto_fix` / `prechecks` / `scope`

The earlier draft (PR #53, commit `6b22a4c`) named three layers. Review feedback: that is nesting,
not grouping. `auto_fix:` encoded nothing the gate doesn't already imply; `scope:` and `prechecks`
were not independent (target `pr` forced `pr_health`, target `branch` forced commands — nonsense
combinations were expressible). Collapsed to:

- `checks` — the gate policy (the only signal; new signals are just new entries in `checks`).
- `target` — where the fix lands. Implied by the check kind by default; an explicit override is
  reserved for future check kinds (see §Unified model). This is the one axis that *does* change
  the engine's fixer mechanics (worktree ref — PR head vs
  `base`, push-to-remote-branch + `resolveReviewThread` vs open-a-fresh-PR, budget key — PR# vs
  gate branch), so it must be visible.
- plain jobs — no `checks`, no `target`, no budgets (defaults apply). Unchanged schema.

Important on `pr_health`: it **polls, it never spawns.** One `gh pr list` per tick (cheap),
filtered to owned `auto/*` PRs, then per-PR `gh` queries for CI/threads — no agents, no worktrees.
The fixer is spawned only for PRs a check flags as red. Restating babysit's signal as a `command`
script (`gh pr list | jq` … exit non-zero with the broken PR#s in stdout) is faithful *detection* —
but the fix then needs correct targeting (worktree pinned to that PR's head), push-to-remote-branch
safety, `resolveReviewThread` handling, and per-PR boundedness (cap + retry budget keyed by PR#).
Those are fixer mechanics, not gate mechanics — which is why keeping the builtin + `target: pr`
means the babysit fixer stays engine-side instead of living in a prose prompt.

### Why not two `trigger` kinds (recap)

`trigger: pr | gate` was the first sketch. It conflates two independent axes:

- **The gate signal** — babysit's CI/threads predicate is exactly as much a "check" as `ruff check`
  is; both are cheap predicates whose non-zero result dispatches the fixer. One signal kind,
  parameterized (`pr_health` builtin vs `command`).
- **The target** — the structural difference is *what gets fixed* (a PR vs `base`), and it changes
  the worktree ref, the prompt context, and the budget key — surfaced via `target`, none of them
  needing a second control for the signal.

## Semantics inside the tick

`_process_auto_fix_job` (`src/herdr_routines/tick.py:118`) runs after the standard guards
(`has_ever_been_seen` / `find_stale_running` / `is_currently_running` / `_live_agent_exists`,
`tick.py:106`). Routing first (see above); a job without `checks` is dispatched by the plain path
and never reaches the gated branch.

Gated semantics branch on the inferred `target`:

- `target: pr` (default via `pr_health`):
  1. `list_open_prs` → `pr_health` (≈ `is_eligible`) → cap → attempt-check → dispatch per flagged
     PR. The existing PR #50 path — its behavior with `checks: [pr_health]` is byte-for-byte the
     current babysit run.
- `target: base` (default via commands):
  1. `git worktree add <wt-aside> <base>` (the same `herdr worktree create` seam
     `src/herdr_routines/tick.py:519` uses; checked out to `base` — the job's configured `base`
     branch, defaulting to the repo's default branch when unset).
  2. Run **all** checks sequentially via subprocess, each bounded by its `timeout_ms`, stdout+stderr
     captured to `gate_output_path`. **No short-circuit**: run every check and collect every
     failure — the fixer gets the full failure context, not the first one. (Per-check ordering is
     preserved in the captured output.)
  3. All pass → append `HistoryRecord(state="done", extra={..., "gate": "passed"})`, remove
     worktree, `return summary, False`. **No agent.**
  4. Any non-zero/timeout → gate failed. Retry budget: count prior terminal records for
     job+gate-branch (mirrors `attempt_count_for_pr` `src/herdr_routines/auto_fix.py:380`, keyed
     by the gate branch instead of `pr_number`). `>= max_attempts_per_target` → skip + `_notify`,
     `reason: max_attempts_exceeded` (same treatment as pr mode).
  5. Else dispatch one worker: branch `auto/<job>-<ts>` (reuse `build_branch_name`
     `src/herdr_routines/runner.py:262`), agent name `rt-<job>-gate-<run_id>` truncated to 32
     (`NAME_RE` cap, same rule as `build_worker_agent_name` `src/herdr_routines/auto_fix.py:436`),
     prompt from `build_base_fix_prompt` (below).
  6. After worker: per-attempt `HistoryRecord(extra={branch, attempt, reason, fix_worker_agent,
     pane_id, report_path, report_written, final_agent_status})` (shape identical to pr mode
     `tick.py:373`), aggregate report lists gate passed/failed + branch,
     `any_failed` flips on `failed`/`interrupted_unknown` only.

A note on budget scope — the target key's lifetime differs, and the budget follows it:
- **base target:** the gate branch includes a scheduled-occurrence timestamp, so every cron fire is
  a **fresh target** and `max_attempts_per_target` bounds retries **within one occurrence**. The
  perpetual-red-base case (agent every day, forever) is not a runaway: each occurrence gets at most
  one worker, and the **cron schedule** is the cross-occurrence rate limiter — 1/day for
  `repo-hygiene`, not a loop. Intended; `_notify` on worker failure tells you each day it stayed red.
- **pr target:** the budget key is the **PR number, stable for the PR's lifetime**, so
  `max_attempts_per_target` spans every 10-min tick that PR stays red — the existing PR #50
  behavior, unchanged.

### Prompt

Default prompt is selected by target. Empty `prompt:` → engine injects it; a custom `prompt:`
replaces wholesale (same rule as pr mode `tick.py:507` `af.prompt or build_fix_prompt(...)`).

- `target: pr` → `build_fix_prompt` (`src/herdr_routines/auto_fix.py:394`), seeded with the
  flagged PR's CI output + thread bodies.
- `target: base` → new `build_base_fix_prompt`, seeded with the failing checks' output:

> You are fixing a failing gate on branch auto/<job>-<ts>. The checks that failed:
>
> `$GATE_OUTPUT` (the combined output of the failed checks, plus the checks that passed)
>
> Fix the code, run the failing checks yourself to verify, run `uv run pytest -q`, commit, push,
> then open a PR via `gh pr create --base <base> --title ... --body "..."`.

(base mode drops the pr-mode steps that make no sense for a self-owned branch: no "reply to review
threads", no `resolveReviewThread`, no thread bodies — those arrive later, via a `pr_health` job
once the PR exists.)

### History states

No new state file — reuse `history.jsonl`, terminal states `running`/`done`/`failed`/`skipped`/
`missed`/`interrupted_unknown`. New `extra` keys on gate records: `gate: passed|failed`,
`failed_checks` (list of `{command, exit_code | timeout, output_snippet}`), `gate_output_path`,
`target`, `branch`. Attempt derivation stays append-only over `history.jsonl` per the existing
pattern (`src/herdr_routines/history.py:112`).

### Systemd timeout (`_check_systemd_timeout`)

`src/herdr_routines/cli.py:444` currently budgets the auto-fix path as
`start_timeout_ms + max_prs_per_tick * timeout_ms` (PR #50 finding K) — note `max_prs_per_tick` is
the **old** key, renamed `max_workers_per_tick` here; the Files-touched migration bullet maps it.
With checks present, worst case per tick must cover gate execution **and** the worker(s):

```
start_timeout_ms
  + gate_slop                                     # worktree add/remove + pr_health gh polling
  + Σ check.timeout_ms                            # one gate pass per tick (all targets)
  + (target pr ? max_workers_per_tick : 1) × timeout_ms
  + (target pr ? max_workers_per_tick × Σ check.timeout_ms : 0)
```

- `target: pr`, no extra command checks → `start_timeout_ms + gate_slop + max_workers_per_tick ×
  timeout_ms` (unchanged budget for babysit).
- `target: pr` with extra `command` checks → the gate runs once, but command checks run per-PR in
  each PR's worktree, so the check time also appears once per worker (the
  `max_workers_per_tick × Σ check.timeout_ms` term).
- `target: base` → `start_timeout_ms + gate_slop + Σ check.timeout_ms + timeout_ms` (at most one
  worker; the worker re-runs the checks itself inside its own `timeout_ms`, so check time is
  **not** added twice — the base form has a single `Σ check.timeout_ms`).

`gate_slop` is a fixed constant (e.g. 60 s) that also absorbs the "+ margin" referenced by
criterion 7 — a single term appears in both places so the two definitions cannot drift apart.

## Files touched

- `src/herdr_routines/config.py` — drop the `auto_fix` container; add job-level `checks`
  (`pr_health` | `{command, timeout_ms}`), `target` (`pr` when any `pr_health` present, else
  `base`), `max_workers_per_tick`, `max_attempts_per_target` to the job schema
  (`_AUTO_FIX_ALLOWED_KEYS` `config.py:82` / `_AUTO_FIX_DEFAULTS` `config.py:94` migrate into the
  job's `_JOB_ALLOWED_KEYS`; `base` reuses the existing job field, now also meaning the base-target
  worktree ref, defaulting to the repo default branch; `max_prs_per_tick` → `max_workers_per_tick`
  rename maps the Pi's old value). Validate (pure schema, no git): check kinds parse; `target ∈
  {pr, base}`; explicit `target` must equal the inferred value (mismatch rejected as "not yet
  supported"); `pr_health` cannot be mixed with `command` in one job; per-check `timeout_ms`
  positive; `base` a non-empty string; `checks: []` equivalent to absent (plain job). Base **ref
  existence is a runtime check** in tick.py (fail-closed as a gate error).
- `src/herdr_routines/auto_fix.py` — `build_base_fix_prompt`, `build_gate_worker_agent_name`,
  a pure `run_checks(checks, cwd, env) -> GateOutcome` (injectable subprocess/pr_health runner,
  keeps the module's "pure-ish, no subprocess except via injection" posture per
  `docs/pipeline/runs/20260829T050025Z/spec.md:73`).
- `src/herdr_routines/tick.py` — routing on `checks` in `_process_auto_fix_job` (`tick.py:118`);
  runtime base-ref existence check (fail-closed as a gate error, after `git worktree add`);
  `pr_health` check reuses `is_eligible`; worktree create/remove for the base-target worktree (path
  unique per `run_id` — catch-up collapse can run two occurrences close together, so never reuse a
  fixed `<wt-aside>` path); dispatch on non-zero check.
- `src/herdr_routines/cli.py` — checks-aware worst case in `_check_systemd_timeout` (`cli.py:444`).
- `src/herdr_routines/runner.py` — reuse `build_branch_name`, `substitute_prompt`,
  `_prompt_with_watchdog`, `default_reports_dir` unchanged; no new primitive.
- `tests/test_auto_fix.py` — check runner (pass/fail/timeout/order, no short-circuit),
  dispatch-on-fail, pass-no-dispatch, prompt content, attempt budget keyed by target.
- `tests/test_tick.py` — base-target tick integration: no agent when clean, one dispatch when red,
  `any_job_failed` semantics, worktree removed on pass.
- `tests/test_config.py` — `checks`/`target` validation matrix (incl. `pr_health`+`command`
  rejection, `checks: []` = plain job).
- `tests/test_cli.py` — plus the existing `test_auto_fix_job_counts_max_prs_per_tick_worst_case`
  stays green; add base-target budget variant.
- `deploy/jobs.example.yaml` — sample `repo-hygiene` job (ruff + mypy checks) so the Pi derates
  to: daily gate, agent only when red. Pi `jobs.yaml` `babysit-prs` migrates to the flat shape
  (gains `checks: [pr_health]`) when this lands.

### Environment (`command` checks and workers)

Checks run as child processes of the tick and **inherit the tick's environment** — whatever `uv`,
`gh`, and env vars the scheduler has. The fix worker, by contrast, gets the agent's fuller
environment (agent kinds may add vars, PATH entries, or config). This asymmetry is intended and
must be stated in `deploy/jobs.example.yaml`: a check must be reproducible from the scheduler's
own env; do not rely on worker-only env in a check.

## Risks

- **Runaway.** A gate that keeps failing with a worker that keeps failing must not loop over
  `history.jsonl`:
  `max_attempts_per_target` — keyed by the gate branch in base target (**intra-occurrence**, fresh
  branch per cron fire) and by `pr_number` in pr target (**across the PR's lifetime**, stable key
  for every 10-min tick it stays red) — taps out as `skipped`/`max_attempts_exceeded` + `_notify`,
  identical across targets. See the budget-scope note in Semantics for the split.
- **Perpetual-red base spawns daily.** Intended; each occurrence = one fresh attempt, rate-limited
  by cron (see the budget-scope note in Semantics). Not a runaway — but it does produce one agent
  per day until green, so `_notify` on worker failure is the alert that it stayed red.
- **Non-hermetic gate.** `command` checks run in a worktree of the target, but their *availability*
  is host-specific (`uv` on the Pi, env vars; see the Environment note). Mitigation: a check that
  cannot run on the host must fail-closed as gate-failed dispatch (it will spawn the worker with
  the "command not found" output; if the worker can't help, attempts tap out); document that
  commands must be reproducible from the machine's own env.
- **Time-bounded gate.** A slow/hanging check must not wedge the tick. Mitigation: per-check
  `timeout_ms`, subprocess `timeout=`; timeout counts as gate-failed, not crash.
- **Branch collision on retry.** Repeated gate failures on `auto/<job>-<ts>` (same scheduled
  occurrence can't recur, but catch-up collapse and retry ticks can). Mitigation: reuse the
  scheduled-occurrence timestamp name (unique per occurrence, same scheme the runner already uses);
  if the branch exists, `_dispatch_fix_worker`'s existing "remove stale worktree `--force` then
  re-add" (`tick.py:521`) handles the worktree side; the worker checks `gh pr list` for the branch.
- **Regression against pr mode.** The gated path must not disturb pr-mode behavior or its 12
  existing acceptance tests. Mitigation: pr mode is unchanged — `checks: [pr_health]` is the
  current babysit baseline; base-target is a sibling branch in `_process_auto_fix_job`, not a
  modification of the enumeration path.
- **Migration.** The Pi's shipped `babysit-prs` uses the old `auto_fix:` keys. Mitigation: land the
  schema + a config migration (or a hard error naming the new keys) in the same change, and flip
  the Pi `jobs.yaml` to the flat shape (adding `checks: [pr_health]`) at deploy time.

## Acceptance criteria

1. **gate passes:** all `checks` exit 0 (and, in pr target, `pr_health` is clean) → no agent spawned,
   no branch created, `HistoryRecord` `done` with `extra.gate == "passed"`, base-target worktree
   removed, `any_failed` false. Test: test_auto_fix_gate_pass_no_dispatch
2. **gate fails:** any check non-zero → exactly one worker dispatched (per failing target), prompt
   contains the failing checks and their captured output (all failures, no short-circuit),
   per-attempt record written with `reason: gate_failed`, `any_failed` false unless the worker
   fails. Test: test_auto_fix_gate_fail_dispatches_worker
3. **check timeout:** a check exceeding its `timeout_ms` counts as gate-failed and dispatches (does
   not crash the tick) with the timeout in the output. Test: test_auto_fix_gate_command_timeout
4. **attempt budget:** after `max_attempts_per_target` gate-failing attempts per target — base
   target: **within one occurrence** (fresh branch per cron fire); pr target: **across the PR's
   lifetime** (stable `pr_number` key, one budget for all ticks that PR stays red) — subsequent
   ticks skip with `skipped`/`reason: max_attempts_exceeded` + `_notify`, no worker spawned.
   Test: test_auto_fix_gate_respects_max_attempts_per_target
5. **config validation:** check kinds parse (`command` or `pr_health`, **not both** in one job);
   `target` outside `{pr, base}` rejected; explicit `target` equal to the inferred value accepted
   (mismatch = "not yet supported" until PR selection for command checks is specified); `pr_health`
   + `command` mixing rejected; per-check `timeout_ms` positive; `base` (optional, default repo
   default branch) validated as a **non-empty string** at config time, with **ref existence
   checked at runtime** in the tick (fail-closed as a gate error — config validation is pure
   schema, no git); **no `checks`** (absent or `[]`) =
   unconditional plain job (plain path). Test: test_auto_fix_gate_config_validation
6. **branch + agent name:** base-target worker branch is `auto/<job>-<ts>`; agent name
   `rt-<job>-gate-<run_id>` fits the 32-char `NAME_RE` cap. Test: test_auto_fix_gate_branch_and_agent_name
7. **systemd budget:** `_check_systemd_timeout` applies the corrected general form — base-target as
   `start_timeout_ms + gate_slop + n_checks × timeout_ms + timeout_ms` (single `Σ check.timeout_ms`,
   the worker re-runs checks inside its own `timeout_ms`) and pr-target-with-commands as
   `start_timeout_ms + gate_slop + n_checks × timeout_ms + max_workers × timeout_ms +
   max_workers × n_checks × timeout_ms`; does **not** add `n_checks × timeout_ms` twice for
   base-target. Test: test_auto_fix_gate_systemd_timeout_budget
8. **pr mode unchanged:** the 12 pr-scope acceptance tests (run `20260829T050025Z`) still pass with
   `checks: [pr_health]` (the babysit baseline — *not* "no checks"); `checks: [pr_health]` with
   `pr_health` polling is the existing behavior. Test: test_auto_fix_pr_trigger_unchanged
9. **clean run is free:** a passing gate in **base target** produces zero `gh`/`gh api`/agent/push
   activity (spy harness asserts only git worktree + check subprocess calls); for **pr target**,
   reads-only `gh` polling (`gh pr list` + per-PR queries) is allowed — the assertion is "no agent,
   no push, no PR-create". Test: test_auto_fix_gate_clean_run_no_gh_activity
