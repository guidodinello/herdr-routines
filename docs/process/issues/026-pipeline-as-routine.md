---
id: "026"
title: "Unify routines + pipeline into one gated-workflow model"
status: open
priority: medium
area: pipeline
gate: PR #58 (issue close-on-merge + roadmaps) merged; issue 006 (jobs.d) landed
---

## Description

Make the overnight **pipeline** and a **routine** schedule from the same engine,
not from two unrelated launchers. Investigation (2026-08-30) confirmed the
pipeline is *not* a distinct code engine today: it is a single `herdr` agent
whose `docs/pipeline/orchestrator-prompt.md` instructs it to spawn `pl-1..pl-6`
workers via its own `herdr` tool calls. There is **no Python stage/spawn loop**
in `src/herdr_routines/` — `pick_feature.py` is the stage-0 selector, `ps.py`
only *reads* `pl-<N>-<run_id>` names to display "stage N/6", and `tick.py` runs
exactly one agent per job. The only real structural difference between a routine
and the pipeline today is **who schedules them**:

- **Routine** (1 step, 1 agent) is driven by the persistent 5-minute tick loop
  (systemd `herdr-routines.timer` → `.service` → `tick`, `Type=oneshot`).
- **Pipeline** (N prompt-driven steps + gates) is launched by a **one-shot,
  detached, transient `systemd-run` unit** (design.md:283-295) — *precisely so
  it does not block the tick cadence*. It has its own `deadline_epoch`,
  `state.json`, and mirror-report.

The unification being targeted here is real but **narrow and scheduling-only**:
**move pipeline scheduling into `tick`.** It is NOT an immediate engine
unification — stages stay hardcoded in `orchestrator-prompt.md` either way. Real
engine unification (a Python stage driver, declarative `workflows/<name>.yaml`,
code-level stage gates) is tracked separately as issue 013 / the
"code-level pipeline gates" roadmap item, not by this issue.

> Scope honesty: this increment advances the *scheduler*, not the engine.
> Selling it as "one gated-workflow engine" invites scope creep. The unification
> claim is a long-term direction; this issue is its first, narrow step.

## First increment: pipeline is a dispatched (detached) job, not a blocking routine

The core design decision: **`tick` dispatches the pipeline detached and returns.
It does not block on it.** A 5-minute `Type=oneshot` tick holding `tick.lock`
(flock, tick.py:64-83) must not run an orchestrator synchronously — with a
multi-hour budget that would freeze every other job (`babysit-prs`,
`repo-hygiene`, …) for the whole night (~84 no-op timer fires, no other job
runs). This is exactly why the pipeline uses a separate transient unit today.

Mechanism, on cron fire (`_process_pipeline_job`, a sibling of
`_process_gated_job` branched in `_process_job`, tick.py:1032):

1. **Launch first, then record.** `tick` generates the *same* detached
   `systemd-run --user --unit=herdr-pipeline-<RUN_ID>` invocation a human runs
   today (design.md:283-295), and only after `systemd-run` exits 0 writes the
   `running` record. (Writing the record before launch wedges the job for the
   whole deadline if `systemd-run` fails or tick dies between — order matters.)
2. **The generated command** runs `herdr workspace create --env HERDR_ENV=1 …`
   (that flag already exists, design.md:133) so the orchestrator's
   pane/workspace gets `HERDR_ENV=1`. `tick` returns immediately; subsequent
   ticks see the live agent (existing `_live_agent_exists` guard, tick.py:1194)
   → "skipped (already running)".
3. When the agent is gone, a later tick **reconciles completion from the report
   file's content** (existence = terminal; parse the orchestrator's outcome
   marker for done vs failed vs partial) — not from `state.json`, which lives in
   the orchestrator's own worktree, is unpinned, and may be gc'd by a human
   (design.md G-14). Reuse `find_stale_running` semantics bounded by
   `deadline_ms` to detect silent death.

### The RUN_ID contract (must be stated; the naive choice breaks stage 1)

`tick` mints its own history key with `make_run_id(job.name, occ)` =
`nightly-pipeline-20260830T020000Z` (runner.py:258-259). It must **not** hand
that string to the orchestrator as `RUN_ID`: the orchestrator builds worker
agent names `pl-<N>-<RUN_ID>` (orchestrator-prompt.md Worker spawn template;
design.md:215), and herdr caps agent names at 32 chars
(`[a-z][a-z0-9_-]{0,31}`, config.py:58-60). `pl-1-` + the full run id (33 chars
for `nightly-pipeline-20260830T020000Z`) = 38 chars — overflow by 6, every
worker spawn fails at stage 1.

Fix, stated explicitly:
- `tick` passes the **bare UTC timestamp** (`20260830T020000Z`, 16 chars →
  `pl-1-…` = 21 chars, fits) as the orchestrator's `RUN_ID`. The full
  `make_run_id(job.name, occ)` value is used only for `tick`'s own history key.
- The pinned report path is passed as an **absolute path literal** in the
  generated invocation (not reconstructed from either run_id on the orchestrator
  side), so tick and orchestrator need never agree on a run_id namespace.

### Report semantics: redirect, don't skip

`$ROUTINE_REPORT` / `$PIPELINE_REPORT` are placeholder strings today;
`substitute_prompt` (runner.py:267-274) only knows `$ROUTINE_*`, so the
orchestrator's `$PIPELINE_REPORT` passes through untouched and it computes the
path itself — a run_id namespace mismatch with the routine path.

Fix by **redirecting** to one name, then **keeping** the `no_report` guard.
The substitution contract must cover the token the orchestrator actually
receives, end to end:

1. Teach `substitute_prompt` a single `$REPORT`, substituted for every job kind
   to `default_reports_dir()/f"{run_id}.md"` (keep `$ROUTINE_REPORT` as a
   back-compat alias). **Also rewrite `$PIPELINE_REPORT` to the same pinned path**
   — the orchestrator prompt uses `$PIPELINE_REPORT` throughout
   (orchestrator-prompt.md:52/:155/:163), not `$REPORT`, so without this the pinned
   path never reaches the orchestrator ("green test, dead contract"). `AC #5`
   asserts the token the orchestrator receives, i.e. `$PIPELINE_REPORT` too.
   This also unifies the run_id namespace for the report.
2. **Do not** skip the "report exists and non-empty" check for pipeline jobs,
   and reconcile completion **from the report's content**: the orchestrator
   already writes a report "at end regardless of outcome" (design.md:168), so an
   empty/missing report after an agent-gone transition is the
   silent-orchestrator-death detector (the documented incident, design.md:347-356).
3. The one legitimate content difference — **tolerate a partial report only when
   the deadline was exceeded.** The signal `tick` reads must be named and
   **emitted**: this issue adds a small, scoped task to `orchestrator-prompt.md`
   to write an explicit outcome line on its terminal branches — a
   `## Outcome: ok`, `## Outcome: failed`, or `## Outcome: partial (deadline
   exceeded)` — so `tick` can reconcile done vs failed vs partial from the report
   content (it already writes a partial report + `notification show` on deadline,
   design.md:170-173; the marker makes that machine-readable). The natural single
   insertion point is the "Always write `$PIPELINE_REPORT` …" block at
   `orchestrator-prompt.md:163`. This prompt edit
   is carved out of the "no prompt edits" Non-goal below (it changes no stage
   model, only adds a terminal status line). Reconcile from the marker, not the
   clock.

> Note: the orchestrator agent is named `rt-<job>` (i.e. `rt-nightly-pipeline`)
> in the generated invocation, matching the `_live_agent_exists` guard — this
> diverges from design.md:289's `pipeline-orchestrator`; implementer should not
> copy the doc verbatim.

### `HERDR_ENV=1` is a string literal, not a `herdr.py` change

Under dispatch-detached, `tick` **emits a shell command** that itself runs
`herdr workspace create --env HERDR_ENV=1 …`. The env never flows through
`HerdrClient`/`herdr.py` at all (that client has zero env support and does not
need any here). `HERDR_ENV=1` is **mandatory** for `kind: pipeline` — it is a
constant folded into the generated string, not a `env:` config block. Implementer
builds the invocation string with `HERDR_ENV=1` (and the pinned absolute report
path) baked in. A separate `env:` map is out of scope (YAGNI; opening env
config invites "what about routines" and shell-escaping surface).

## Mode discriminator: `kind: pipeline | routine` (scoped, not full SSOT)

Introduce a minimal explicit enum — **only two values for this increment**:

- `kind: routine` (default) — the existing plain 1-agent job.
- `kind: pipeline` — detached, deadline-bounded orchestrator dispatch (this issue).

Gate mode is **left exactly as today**: `_process_job` still dispatches to
`_process_gated_job` on `job.checks is not None` (tick.py:1032) inside the
`routine` branch, untouched — no migration for the Pi's live `babysit-prs` /
`repo-hygiene` configs. Do **not** add `kind: gate` or a "routine has `checks`
optionally" clause here — both would make `kind` a non-SSOT (tick would still
need `checks is not None`), which is worse than today. Full `kind:` SSOT that
retires `checks is not None` is a separate migration issue.

`prompt` *source* (inline string vs file) is a **separate axis** (`prompt_file:`):
a plain routine may legitimately want a file-backed prompt without becoming a
pipeline. Do not infer mode from prompt source.

## Config / design shape (draft — for review)

```yaml
# jobs.d/orchestrator.yaml   (dir mode, issue 006) — or a jobs.yaml block
- name: nightly-pipeline
  kind: pipeline                       # explicit mode
  enabled: true
  cron: "0 2 * * *"
  catch_up_minutes: 0                  # ENFORCED: validate rejects >0 for pipeline
  repo: ~/.local/state/herdr-routines/repos/herdr-routines   # the PARENT clone, used as-is
  prompt_file: docs/pipeline/orchestrator-prompt.md          # I/O convenience axis
  deadline_ms: 25200000                # orchestrator wall budget → deadline_epoch
  start_timeout_ms: 120000
  on_missed: log
```

Design notes:
- **No blocking `timeout_ms`, no `env:` block, no `workspace:`.**
  `deadline_ms` is the orchestrator's wall-clock budget; the generated
  `systemd-run` unit cap is set via
  `-p RuntimeMaxSec=$(( (deadline_ms + PIPELINE_UNIT_MARGIN_MS) / 1000 ))`
  (systemd property, **seconds**, not the non-existent `--timeout` flag and not
  ms — `RuntimeMaxSec` is 1000× coarser than `deadline_ms`), where
  `PIPELINE_UNIT_MARGIN_MS = 600_000` is a module constant in `tick.py`, so the
  unit outlives the orchestrator's deadline and lets it write the partial report
  + notify before any kill, with deterministic arithmetic. `workspace:
  worktree|root` **does not apply** to `kind: pipeline` — the orchestrator runs
  in the parent clone and creates its own `auto/pipeline-<run_id>` worktree
  (orchestrator-prompt.md Prerequisite 1; design.md:337-338); the schema
  **rejects** an explicit `workspace:` on a `kind: pipeline` job rather than
  leaving it settable-but-ignored. `HERDR_ENV=1`
  and the pinned report path are baked into the generated command, not config.
- `prompt_file` is read by the runner/validate from disk — **not** in
  `load_config`, which is documented as pure (no filesystem access beyond the
  one YAML file, config.py:3-4). Preserve that invariant.
- `_check_systemd_timeout` (cli.py:430-499) must **skip `kind: pipeline` jobs
  entirely** — they never run in the tick's process, so they must not inflate the
  required unit `TimeoutStartSec` (a plain default `Job.timeout_ms` would quietly
  add ~30 min otherwise).
- `timeout_ms` on this path is unused; the reconcile-staleness bound comes from
  `job.deadline_ms`. Make that explicit in the code.
- `catch_up_minutes` defaults to 120 (config.py:105); because `kind: pipeline`
  must never fire a missed run late into the workday, `validate` **rejects** a
  pipeline job whose effective `catch_up_minutes != 0` — whether set or inherited
  from the default. The schema defaults it to 0 for `kind: pipeline`.
- `default_config_path()`/consumer wiring stays minimal, but the behavioural
  work lives in `tick` (`_process_pipeline_job`) — not in `cli.py` config
  loading.

## Acceptance criteria

Each ends `Test: <name>`:

1. `validate` accepts a `kind: pipeline` job and **suppresses** the
   missing-`$REPORT` warning for it (cli.py:398-414 warns when no
   `$ROUTINE_REPORT` and no `checks`; must not fire for `kind: pipeline`).
   `Test: test_validate_pipeline_job_suppresses_report_warning`
2. On a `kind: pipeline` cron fire, `tick` **launches first** (an injectable
   `systemd-run` launcher seam — a `CommandRunner`-style function that tests
   monkeypatch, same pattern as `HerdrClient`) returning immediately, then writes
   the `running` record. A stubbed launcher short-circuits so the test does not
   wait hours.
   `Test: test_tick_dispatches_pipeline_launch_before_record`
3. **Concurrency (make-or-break):** other due jobs are still evaluated and run
   during a pipeline night (the oneshot tick does not wedge while the pipeline
   runs detached). Two jobs in one `run_tick`, pipeline first; assert the second
   still reaches `execute_run`.
   `Test: test_other_jobs_still_run_during_pipeline`
4. The generated invocation is correct: it runs the orchestrator as `rt-<job>`,
   bakes in `HERDR_ENV=1`, sets the unit cap
   `-p RuntimeMaxSec=$(( (deadline_ms + PIPELINE_UNIT_MARGIN_MS) / 1000 ))`
   (seconds, not `--timeout`, not ms), passes the **bare UTC timestamp** as
   `RUN_ID`, and pins the **absolute** report path. Assert against the generated
   string.
   `Test: test_pipeline_invocation_string_contract`
5. The `## Outcome:` marker is a scoped task: `orchestrator-prompt.md` emits an
   explicit `## Outcome: ok | failed | partial (deadline exceeded)` line on its
   terminal branches, and `substitute_prompt` rewrites `$PIPELINE_REPORT` (the
   token the orchestrator actually uses) **and** `$REPORT` / back-compat
   `$ROUTINE_REPORT` to `default_reports_dir()/f"{run_id}.md"` for every kind.
   The report guard is **redirected, not skipped** — for a pipeline job it still
   marks `failed` on missing/empty report, and tolerates a **partial** report
   only when the report carries the `## Outcome: partial (deadline exceeded)`
   marker. Assessed as three focused sub-tests so a failure attributes cleanly:
   `Test: test_orchestrator_prompt_emits_outcome_marker`
   `Test: test_substitute_prompt_rewrites_pipeline_report`
   `Test: test_pipeline_report_guard_partial_tolerant`
6. `_check_systemd_timeout` **skips** `kind: pipeline` jobs entirely (they do not
   inflate the unit's required `TimeoutStartSec`).
   `Test: test_systemd_timeout_skips_pipeline_job`
7. `validate` treats `workspace` as N/A for `kind: pipeline` and instead repo-checks
   the value as a plain git clone (the parent), not the `workspace == "worktree"`
   `.git` branch (cli.py:389 / config.py:99 default).
   `Test: test_validate_pipeline_workspace_na_repo_is_clone`
8. `validate` **rejects** a `kind: pipeline` job whose effective
   `catch_up_minutes != 0` — whether explicitly set or inherited from the 120
   default — so a missed 02:00 never fires the 7h run mid-morning. Enforced (a
   validation rule + a pipeline-specific default of 0), not conventional.
   `Test: test_validate_rejects_pipeline_catchup`
9. Config schema: the new keys (`kind`, `prompt_file`, `deadline_ms`) parse, get
   defaults, and reject unknown/malformed values (config.py `_JOB_DEFAULTS`,
   `_JOB_ALLOWED_KEYS`), matching issue 025's config-validation test pattern.
   `Test: test_pipeline_config_schema_roundtrip`
10. `scheduled` lists the pipeline's cron, and `status` renders the
    `rt-nightly-pipeline` orchestrator row sanely while a run is in flight (and
    `skipped (already running)` on overlap).
    `Test: test_status_renders_inflight_orchestrator`
11. Resume: `herdr-routines run <job> --run-id <id>` (or an equivalent
    `pipeline-resume <run_id>`) regenerates the same detached invocation with the
    given RUN_ID, preserving the documented same-RUN_ID relaunch path
    (design.md:349-351). `_cmd_run` gains the `--run-id` flag, and **branches on
    `kind`** so that `herdr-routines run nightly-pipeline` (with *or* without
    `--run-id`) dispatches via the same detached launcher rather than calling
    `execute_run` synchronously (cli.py:519). The test drives both the resume and
    the plain-detached paths.
    `Test: test_pipeline_resume_same_run_id`
12. `gc` keeps excluding `auto/pipeline-*` now that the branch originates from a
    scheduled job (design.md G-14; gc.py:17 `PIPELINE_PREFIX`).
    `Test: test_gc_excludes_pipeline_worktrees`
13. Legacy behavior intact: `kind: routine` (and default) jobs still write
    `$REPORT`/`$ROUTINE_REPORT`, the `no_report` guard and the gate-mode
    `checks is not None` dispatch are unchanged.
    `Test: test_regular_routine_guard_unchanged`

## Why these tests

- 1–3 pin the core fix: pipeline becomes a **detached dispatched** job that does
  not freeze the tick — concurrency (3) is the make-or-break criterion the
  original synchronous design could not meet.
- 4–5 pin the two cross-cutting contracts that most easily break: the generated
  invocation string (RUN_ID, HERDR_ENV, deadline margin, report pin) and the
  redirected report guard.
- 6–9 pin the enforcement/schema rules (systemd-timeout skip, workspace N/A,
  catch-up rejection, config round-trip).
- 10–12 pin the operational invariants (status, resume, gc exclusion).
- 13 pins no-regression for ordinary routines and the untouched gate mode.

## Non-goals (this increment)

- Not promoting stage gates to code — that's the "Code-level pipeline gates"
  roadmap item / issue 013 (`workflows/<name>.yaml` + a Python stage driver).
- Not changing the orchestrator's prompt-based 6-stage model or stage internals,
  **except** the scoped `## Outcome:` terminal-status line required to reconcile
  completion (it adds a status line; it does not alter stages or gates).
- Not introducing `kind: gate` or retiring the implicit `checks is not None`
  gate-mode switch — that full-SSOT migration is a separate issue.
- Not touching `src/herdr_routines/auto_fix.py` / gate internals from issue 025.
- Not building `workspace: root` semantics for the pipeline (it owns its own
  worktree; see design notes).
- No generic `env:` config block — `HERDR_ENV=1` is a constant in the generated
  invocation.

## Known limitation (documented, accepted)

If the orchestrator dies before writing *any* report, the job shows in-flight
until `find_stale_running` trips at `job.deadline_ms` (~7h) — matching today's
behaviour (design.md:347-356 incident). Accepted; not fixed by this issue.

## Log

- **2026-08-30**: investigation established the pipeline has no Python
  stage/spawn loop (verified: `tick.py` runs one agent/job; `ps.py` only reads
  `pl-<N>-<run_id>` names; no module drives stages) and both schedule on systemd
  user timers. Parked as a ROADMAP idea.
- **2026-08-30**: drafted v1 scoped as "pipeline as a blocking routine".
  Review pass 1 (sonnet-5, `audits/audit-sonnet5-v1.md`) returned SHAKY: the
  synchronous 7h `tick` blocks the oneshot cadence; it fights `execute_run`;
  skipping the guard hides the silent-death detector; HERDR_ENV plumbing and
  report-path pinning missing; AC #4 contradicted design.md; AC #5 vacuous.
- **2026-08-30**: revised to dispatch-and-detach with a `kind:` enum, redirected
  guard, HERDR_ENV + report pinning, catch-up, rewritten ACs (commit 11ab2f5).
  Review pass 2 (`audits/audit-sonnet5-v2.md`) improved verdict to MOSTLY SOUND;
  residual items folded here: RUN_ID overflow on worker agent names, reconcile
  from report content (not state.json), drop `kind: gate`/env-map, systemd-timeout
  skip, enforced catch-up, named resume mechanism, launch-then-record ordering,
  injectable launcher seam, 3 added ACs. Re-review pending.
- **2026-08-30**: review pass 3 (`audits/audit-sonnet5-v3.md`) returned MOSTLY
  SOUND (close to SOUND), 6/8 residuals closed; residual contracts folded here
  (v4): put the `## Outcome:` marker and `$PIPELINE_REPORT` substitution in
  scope, `_cmd_run` `kind` branch, catch-up default-120 rule, `RuntimeMaxSec`
  seconds + workspace-schema reject, RUN_ID length corrected. Confirmation pass
  pending.
- **2026-08-30**: review pass 4 (`audits/audit-sonnet5-v4.md`) returned
  **SOUND (ready to implement)** — both one-sided contracts closed in scope;
  remaining items were non-blocking polish only. Per-revision design-review
  records live in `docs/process/audits/audit-<reviewer>-v<N>.md`, mirroring
  `docs/pipeline/audits/` — not `issues/`, which `pick-feature` parses via
  frontmatter glob.
- **2026-08-30**: closed the pass-4 polish before the issue reaches the pipeline:
  named `PIPELINE_UNIT_MARGIN_MS = 600_000` (deterministic `RuntimeMaxSec`
  arithmetic), changed explicit `workspace:` on a pipeline job to **reject**
  (dropped the "or ignores with a warning" ambiguity), named
  `orchestrator-prompt.md:163` as the `## Outcome:` marker insertion point, and
  split AC #5 into three focused sub-tests.
- **2026-09-05**: implementation surfaced two corrections, both audited-but-never-run
  gaps (same class of error as each other — a plausible-sounding contract nobody
  executed against the real mechanism):
  1. AC #5's premise was wrong once the launcher became a repo-tracked script
     (per-issue decision, not this doc originally): `substitute_prompt` is only
     called from `execute_run`, which `kind: pipeline` never reaches — so
     "`substitute_prompt` rewrites `$PIPELINE_REPORT`" targeted a function nothing
     calls. `runner.py` stays untouched; the report-path contract moved to the
     `systemd-run` argv/script boundary instead (asserted directly, plus the
     appended `RUN_ID:`/`REPO_PARENT:`/`PIPELINE_REPORT:` trailer the script
     already writes).
  2. **`catch_up_minutes: 0` does not work once folded into the shared 5-minute
     tick.** `schedule.decide()`'s grace is a strict `late <= grace`; with grace 0,
     any nonzero delay between the cron instant and when tick actually evaluates
     the job reports `MISSED` — confirmed by the repo's own
     `test_catch_up_zero_means_no_backfill_at_all` (30s late, grace 0 → MISSED).
     Since `run_tick` evaluates jobs sequentially under one flock and a slower
     gated job (e.g. `babysit-prs`) can easily run first, a pipeline job could
     legitimately be evaluated 30–60+ minutes after 02:00:00 on an ordinary
     night — `catch_up_minutes: 0` would then report MISSED on essentially every
     run, not just genuine multi-hour outages. Fixed: `kind: pipeline` now gets a
     **fixed, non-settable** `PIPELINE_CATCH_UP_MINUTES = 60` (config.py) instead
     of a literal 0 — any explicit `catch_up_minutes` key on a pipeline job is
     rejected outright (not just a nonzero one), since the key doesn't belong on
     this kind at all. 60 minutes comfortably exceeds realistic same-night tick
     delay while staying far short of "into the workday." The MISSED branch in
     `_process_pipeline_job` also gained the same `on_missed: notify` handling
     the routine/gated paths already have, since a starved night is now a real
     (if uncommon) outcome worth surfacing, not a purely theoretical one.
