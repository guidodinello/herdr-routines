# Independent design review — Issue 026 "Unify routines + pipeline into one gated-workflow model"

Reviewer stance: skeptical architect. The author has a strong prior that unification is
correct. This review challenges that prior against the actual code.

**Verdict up front: SHAKY.** The scheduling-layer observation is real and useful, but the
proposed first increment ("run the orchestrator synchronously through `tick` with a 7 h
`timeout_ms` and skip the `no_report` guard") is the wrong cut: it freezes every other
routine for the whole pipeline night, threads special-cases through `execute_run` that the
spec doesn't acknowledge, ignores two hard runtime prerequisites (`HERDR_ENV=1`, report-path
namespacing), and contradicts `design.md` on execution context. The acceptance criteria
encode the wrong design in several places and one of them (AC #2) would fail as written.

---

## 1. Is the "one gated-workflow engine" framing sound?

**Partly. It is sound at the scheduling layer and fuzzy — bordering on false-analogy — at
the engine layer, and the spec conflates the two.**

### What is genuinely the same

Both paths ultimately do: create a pane → `agent_start` → wait for interactive-ready →
send one prompt → block on `--wait` → inspect a file on disk. `runner.execute_run`
(`runner.py:382`) and the pipeline launcher (`design.md:283-295`) share that skeleton, and
`ps.py` already treats pipeline runs as first-class rows (`ps.py:5-11`, `pl-<N>-<run_id>`
enrichment). The claim that there is **no Python stage/spawn loop** is correct — I verified:
`tick.run_tick` → `_process_job` runs exactly one agent per job (`tick.py:1141`
`execute_run`), `_process_gated_job` dispatches fix workers but still one prompt each,
and nothing in `src/herdr_routines/` drives pipeline stages. Stages live only in
`orchestrator-prompt.md`.

### What is genuinely different (and the spec doesn't reckon with)

| Concern | Routine (`execute_run`) | Pipeline orchestrator |
|---|---|---|
| cwd / worktree | **forces** a detached worktree on `auto/<name>-<ts>` (`runner.py:431-434`), or a plain tab in `job.repo` (`workspace: root`) | runs **in the parent clone**, then creates its *own* `auto/pipeline-<run_id>` worktree (`orchestrator-prompt.md` Prerequisite 1); `design.md:337-338` explicitly says the `workspace` worktree/root distinction "does not apply to the pipeline POC" |
| herdr access | worker never spawns sub-agents, never needs `HERDR_ENV` | **`HERDR_ENV=1` is mandatory** or "the orchestrator cannot drive `herdr` at all" (`design.md:131-134`); injected via `herdr workspace create --env HERDR_ENV=1` |
| report path | exact: `default_reports_dir()/f"{run_id}.md"`, run_id = `make_run_id(job.name, occ)` = `nightly-pipeline-20260830T020000Z` (`runner.py:258-259`) | `$PIPELINE_REPORT` on the shared worktree, mirrored to `reports/pipeline-<RUN_ID>.md`, RUN_ID = bare `20260830T020000Z` derived by the orchestrator itself (`orchestrator-prompt.md` Inputs) |
| time budget | one number: `timeout_ms` = the `agent prompt --wait` timeout | **two** numbers: per-worker `--wait` timeout *and* `deadline_epoch` (launch + 7 h) that the orchestrator polls itself to still write a partial report (`design.md:170-173`) |
| failure detection | `no_report` guard turns "settled clean but did nothing" into `failed` (`runner.py:617-623`, the whole point per plan-v1 §6) | silent-orchestrator-death is a **known real incident** (`design.md:347-356`); mitigated by heartbeat file + morning checklist, not by the engine |
| env injection | none — `herdr.py` has no `--env` support anywhere (grep confirms) | required (above) |

None of these are cosmetic. Four of the six are load-bearing for the pipeline to run at
all. So "the pipeline could **trivially** become a `jobs.yaml` entry" (ROADMAP.md:165) and
"**zero change** to the orchestrator agent's prompt-based stage model" (spec, Goal) are
both overstatements. The scheduling unification (persistent tick vs one-shot `systemd-run`)
is small and real. The engine unification is where the work — and the risk — actually is,
and the spec waves at it with "`default_config_path()`/consumer changes stay minimal —
everything routes through `cli.py:_load_config_or_exit`." That is misleading:
`_load_config_or_exit` only *loads config*; every behavioural change the increment needs
lives in `execute_run` / `_process_job`, which the spec barely names.

---

## 2. Is the proposed first increment the right cut? Would it work against the code?

**No on both counts, for one dominant reason and several supporting ones.**

### 2a. The dominant problem: a synchronous 7 h tick freezes every other routine for the night

`deploy/systemd/herdr-routines.timer` fires `OnCalendar=*:0/5` — **every 5 minutes**. The
service is `Type=oneshot` (`herdr-routines.service`), and `tick` also holds an exclusive
`tick.lock` flock (`tick.py:64-83`). systemd will not start a second instance of a oneshot
service while one is running. Therefore:

> If `tick` runs the orchestrator synchronously via `execute_run` with a 7 h `timeout_ms`,
> then for ~7 hours the timer fires ~84 times and **every one is a no-op**. `babysit-prs`
> (`*/10 * * * *`), `repo-hygiene` (`0 13 * * *`), and every other job do not run for the
> entire pipeline night.

The current architecture uses a **separate** transient `systemd-run` unit
(`design.md:275-295`) precisely so the pipeline does not block the tick cadence. The
proposed increment throws that away. The spec does not mention this regression anywhere —
it is the single most important thing wrong with "pipeline as a routine job," and AC #2
("`tick` runs such a job to completion … and settling") tacitly bakes the synchronous model
in without acknowledging the cost. Note the tell: an honest AC would read "other due jobs in
the same tick still run" — and that criterion would **fail** with the design as written.

### 2b. It fights `execute_run` in ~5 places the spec doesn't budget for

To reuse `execute_run` for the orchestrator you must special-case:
1. **Worktree creation** — skip the forced `worktree_create` / `auto/<name>-<ts>` branch
   (`runner.py:431-434`); the orchestrator needs cwd = parent clone. `workspace: root`
   half-covers this but then `job.repo` must literally be the parent clone path and the
   validate check at `cli.py:389` (`.git` exists) still applies.
2. **`HERDR_ENV=1`** — `herdr.py` has no env-injection path at all. New plumbing through
   `agent_start` / `tab_create` / `worktree_create`. Without it: every `herdr` call the
   orchestrator makes wedges as `blocked` at 02:00 (`design.md:131-134`).
3. **The `no_report` guard** — see §4.
4. **`timeout_ms` semantics** — `_prompt_with_watchdog` blocks for the full value
   (`runner.py:505-514`); it also scans the *orchestrator's* visible screen every 30 s for
   `"Free usage exceeded"` (`runner.py:49`, `DEFAULT_FAILURE_MARKERS`) — meaningless for the
   orchestrator (quota modals appear in *worker* panes), and a false-positive risk if a
   worker's screen text bleeds into a shared view.
5. **Deadline** — the orchestrator derives `deadline_epoch` itself, so that part survives,
   but `timeout_ms` must exceed `deadline` + time-to-write-partial-report + notify, or the
   watchdog kills the orchestrator before it can produce the partial report the deadline
   logic exists to guarantee. `25200000` (exactly 7 h) in the spec's example is too tight.

The codebase already shows the clean pattern for "a job that isn't a plain single-agent
run": `_process_job` branches to `_process_gated_job` when `job.checks is not None`
(`tick.py:1032`). A pipeline job should get a **sibling** `_process_pipeline_job` branch,
not five special-cases sprinkled through `execute_run`. The spec never raises "shared
`execute_run` vs. dedicated function" as the real design fork — it frames the choice as
"prompt indirection vs. a `pipeline:` flag," which is the wrong axis.

### 2c. Overloading `prompt` source with execution mode

AC #1 makes "`prompt: "@<path>.md"` (or `pipeline: true`)" the discriminator for
pipeline-ness. Prompt *source* (inline string vs file) and job *kind* (routine vs pipeline)
are orthogonal — a plain routine may legitimately want `prompt_file:` for a long prompt
without becoming a pipeline. Keep them separate.

### 2d. Minor but real

- `_live_agent_exists` (`tick.py:1194`) checks `rt-nightly-pipeline`; with a 7 h+
  `timeout_ms`, `is_currently_running` (`tick.py:1054`) blocks re-dispatch for 7 h — for a
  nightly cron that's the *desired* mutex, fine, but note it.
- First tick after registration is a no-op (`has_ever_been_seen` → just "registered",
  `tick.py:1035`); the "stubbed 7h budget" test must account for that.
- Catch-up: `catch_up_minutes` default 120 (`config.py:107`). A box asleep at 02:00 could
  fire a 7 h pipeline at ~03:59 into the workday. Pipeline jobs want `catch_up_minutes: 0`.
- Resume: the documented silent-death recovery is "relaunch with the same RUN_ID"
  (`design.md:349-351`). When `tick` owns launching there is no ergonomic way for a human to
  re-run *just that job* with the *same* run_id — `herdr-routines run <job>` mints a fresh
  one (`cli.py:510`, `make_run_id(job.name, now)`).

---

## 3. What the correct unified API actually looks like

The right primitive is **not** "a job with a long blocking timeout." It is: **`tick`
recognises a pipeline job and *dispatches* it (detached), then returns** — `tick` becomes
the pipeline's *scheduler*, not its *runner*. Completion is reconciled on a later tick from
`state.json` / the mirrored report, exactly the way `_process_gated_job` already reconciles
via history + live-agent checks.

### Recommended schema (increment scope)

```yaml
- name: nightly-pipeline
  kind: pipeline              # NEW enum: routine (default) | gate | pipeline.
                             # Becomes the single SSOT mode discriminator — also
                             # subsumes today's implicit "checks is not None" mode flag.
  cron: "0 2 * * *"
  catch_up_minutes: 0        # never fire a 7h run late into the workday
  repo: ~/.local/state/herdr-routines/repos/herdr-routines   # the PARENT clone, used as-is
  prompt_file: docs/pipeline/orchestrator-prompt.md          # I/O convenience, separate axis
  deadline_ms: 25200000      # orchestrator wall budget → deadline_epoch
  start_timeout_ms: 120000
  env:                       # NEW: injected into the orchestrator's pane/workspace
    HERDR_ENV: "1"
  on_missed: log
```

Key points:
- **`kind:`** is an explicit enum, not an inferred flag. Today `checks is not None` *is* an
  implicit mode switch (`tick.py:1032`); `kind` makes both modes legible and gives the
  pipeline a home without overloading `prompt`/`checks`.
- **No blocking `timeout_ms`.** `deadline_ms` is the orchestrator's budget; `tick` does not
  wait it out.
- **Dispatch mechanism:** on cron fire, `tick` writes the `running` record then launches a
  detached transient unit (`systemd-run --user --unit=herdr-pipeline-<run_id> …`) — i.e.
  `tick` *generates* the exact `systemd-run` invocation that a human runs today
  (`design.md:283-295`), with `HERDR_ENV=1` and `$PIPELINE_REPORT` pinned to
  `default_reports_dir()/f"{run_id}.md"` so tick and orchestrator agree on the path. Then
  `tick` returns immediately; the 5-min cadence and every other job are unaffected.
- **Completion:** subsequent ticks see the live `rt-nightly-pipeline` agent (existing
  `_live_agent_exists` guard) → "skipped (already running)". When it's gone, tick reads
  `state.json` (`current_stage`, terminal) + the report file and writes `done` / `failed` /
  `interrupted_unknown`, reusing `find_stale_running` semantics against `deadline_ms`.
- **`workspace: worktree|root` does not apply** to `kind: pipeline` — the orchestrator owns
  its own worktree; document that, don't try to force-fit it.

### The endgame (explicitly not this increment — issue 013 / "code-level pipeline gates")

A top-level `workflows/<name>.yaml` with `stages: [{name, model, prompt_file, gate,
isolation: independent|reused:<stage>}]` and a real Python driver. That is where
"routine = 1-step gated workflow, pipeline = N-step" becomes *true* rather than analogical,
and where `design.md`'s Gate 1i/2i process-fidelity checks (`design.md:230, 236-256`) get a
generic implementation instead of a bespoke check per hardcoded stage pair. The 026
increment should be framed as "move pipeline scheduling into tick," full stop — not as a
down payment on engine unification it doesn't actually advance (stages stay
prompt-hardcoded either way).

---

## 4. Is the `no_report` guard skip a hack?

**As worded, yes.** `$ROUTINE_REPORT` and `$PIPELINE_REPORT` are just placeholder strings.
`substitute_prompt` (`runner.py:267-274`) only rewrites `$ROUTINE_REPORT` / `$ROUTINE_JOB`
/ `$ROUTINE_RUN_ID`; it does **not** know `$PIPELINE_REPORT`, so the orchestrator prompt's
literal `$PIPELINE_REPORT` passes through untouched and the orchestrator computes the path
itself. "Skip the guard for pipeline jobs" then throws away the *only* mechanism that turns
"agent settled `idle` having done nothing" into `failed` (`runner.py:617-623`) — and the
pipeline has a **documented** silent-clean-exit failure mode (`design.md:347-356`: "a
killed orchestrator writes nothing"). Skipping the guard makes that failure invisible;
tick would record `done` off `settled_status in {idle, done}` (`runner.py:29`).

**Cleaner fix — redirect, don't skip:**
1. Collapse the placeholder to one name. Teach `substitute_prompt` a single `$REPORT` (keep
   `$ROUTINE_REPORT` as an alias for back-compat), substituted for every job kind to
   `default_reports_dir()/f"{run_id}.md"`.
2. Keep the exact "file exists and non-empty" check for pipeline jobs too — the orchestrator
   already writes a report "at end regardless of outcome" (`design.md:168`), so the guard is
   *more* useful here, not less: it's precisely the silent-death detector the morning
   checklist is a manual stand-in for.
3. The one legitimate content difference: tolerate a **partial** report (deadline exceeded)
   as non-failure. That is a report-content rule, not a reason to disable the guard.

This also kills the run_id-namespace mismatch (§1): tick and orchestrator both use
`f"{run_id}.md"` under `default_reports_dir()`.

---

## 5. Acceptance criteria — testability and correctness

| # | Problem |
|---|---|
| 1 `test_pipeline_job_skips_no_report_guard` | Encodes the hack as the contract (see §4). "Accepted by `validate`" is ambiguous: `validate` currently *warns* (not errors) when `$ROUTINE_REPORT` is absent and `checks is None` (`cli.py:398-414`) — a pipeline prompt containing `$PIPELINE_REPORT` would trip that warning. The test should assert the warning is **suppressed for `kind: pipeline`**, and (if §4 is adopted) that the guard is *redirected*, not skipped. Rename accordingly. |
| 2 `test_tick_runs_pipeline_job_to_completion` | Bakes in the synchronous model whose cost (§2a) the spec never states. As written this AC is **incompatible** with an AC that other due jobs still run — and there should be such an AC. "without the systemd-short-timeout misfire" conflates runtime with validate-time: `tick` never reads the unit file; only `_check_systemd_timeout` does. Split. |
| 3 `test_validate_systemd_timeout_for_pipeline_job` | Circular as phrased ("does not warn … when the unit is also raised" — trivially true if you raise it enough). Needs the concrete formula term, the way issue 025's AC pins the gate formula (`025 …` Acceptance, `_check_systemd_timeout`): for a pipeline job the added term should be `start_timeout_ms + deadline_ms + margin` — and the test asserts exact numbers. Also: with the **dispatch-and-detach** model (§3) this check largely goes away, because `tick` no longer blocks for `deadline_ms` — which is itself an argument for that model. |
| 4 `test_tick_pipeline_job_report_and_state_written` | **Contradicts `design.md:337-338`.** "the job runs in a worktree whose repo/base is the target" is exactly what the pipeline does *not* do — the orchestrator runs in the parent clone and creates its own `auto/pipeline-<run_id>` worktree (`orchestrator-prompt.md` Prerequisite 1). This AC asserts the wrong execution context. It should assert: cwd == parent clone; `HERDR_ENV=1` present in the orchestrator's pane env; `$PIPELINE_REPORT` resolved to `default_reports_dir()/f"{run_id}.md"`; artifacts land on the orchestrator's self-created worktree. |
| 5 `test_ps_shows_pipeline_job_stages` | Near-vacuous. `ps.py` derives "stage N/6" from scanning `state.json` for `pl-<N>-<run_id>` names (`ps.py:55-96`) — entirely independent of how the orchestrator was launched, so this passes without the increment doing anything. The worthwhile version: `scheduled` lists the pipeline's cron, and `status` renders the `rt-nightly-pipeline` orchestrator row sanely while a run is in flight. |
| 6 `test_regular_routine_guard_unchanged` | The one solid criterion. Keep. |

### Missing criteria (the ones that would actually de-risk this)

- **Concurrency:** other due jobs are still evaluated and run during a pipeline night. (The
  make-or-break test. If the answer is "no," the increment needs redesign — see §2a/§3.)
- **`HERDR_ENV=1`** reaches the orchestrator's pane/workspace — grep confirms `herdr.py`
  has no env plumbing today; without a test this silently regresses to a 02:00 `blocked`
  wedge (`design.md:131-134`).
- **Deadline vs. kill:** the orchestrator gets enough wall-clock past `deadline_epoch` to
  write the partial report + `notification show` before any watchdog/timeout kill.
- **Catch-up:** a missed 02:00 occurrence does **not** launch a 7 h run mid-morning
  (`catch_up_minutes: 0` for pipeline jobs).
- **Resume:** the documented same-RUN_ID relaunch path (`design.md:349-351`) still works
  when `tick` owns launching — or is explicitly replaced.
- **`gc` exclusion:** `auto/pipeline-*` stays excluded from `herdr-routines gc`
  (`design.md` G-14) now that the branch originates from a scheduled job.

---

## Blunt verdict: **SHAKY**

The observation ("pipeline has no Python driver; both schedule on systemd timers") is
correct and worth acting on. The *chosen increment* is the wrong one of the available cuts.

### The 5 changes to make before this is implementable

1. **Kill the synchronous model.** `tick` must **dispatch the pipeline detached** (generate
   the `systemd-run` transient unit it runs today, with `HERDR_ENV=1` and a pinned report
   path) and return immediately — not block a oneshot 5-minute tick for 7 hours. Reconcile
   completion on a later tick from `state.json` + the report file. Add an AC that other due
   jobs still run during a pipeline night.

2. **Make it a sibling code path, not `execute_run` surgery.** Add
   `_process_pipeline_job` alongside `_process_gated_job` (`tick.py:1032`). Introduce an
   explicit **`kind: routine | gate | pipeline`** enum as the single mode discriminator
   (retire the implicit `checks is not None` switch), and a separate `prompt_file:` axis for
   prompt source.

3. **Redirect the report guard, don't skip it.** Collapse `$ROUTINE_REPORT` /
   `$PIPELINE_REPORT` to one substituted `$REPORT` = `default_reports_dir()/f"{run_id}.md"`,
   keep the "exists and non-empty" check for pipeline jobs (it's the silent-orchestrator-
   death detector `design.md:347-356` calls for), and add explicit tolerance for a *partial*
   report on deadline-exceeded.

4. **Plumb `HERDR_ENV=1` and pin the report path.** `herdr.py` currently has zero env-
   injection support; the orchestrator is dead without `HERDR_ENV=1` (`design.md:131-134`).
   This is a hard prerequisite, not a detail — give it its own AC.

5. **Fix the acceptance criteria to match reality.** Rewrite AC #4 to assert cwd = parent
   clone + orchestrator-owned worktree (not "a worktree whose repo/base is the target" —
   that contradicts `design.md:337-338`); make AC #3 pin the exact timeout formula term;
   replace the vacuous AC #5 with a `scheduled`/`status` assertion; add the six missing
   criteria (concurrency, `HERDR_ENV`, deadline-vs-kill, catch-up, resume, `gc` exclusion).

### Also worth saying

Frame the increment honestly as **"move pipeline *scheduling* into `tick`."** It does not
advance engine unification — stages stay hardcoded in `orchestrator-prompt.md` either way —
and selling it as a step toward "one gated-workflow engine" invites scope creep and
disappointment. The real engine unification is issue 013's `workflows/<name>.yaml` + a
Python stage driver + `design.md`'s Gate 1i/2i process-fidelity checks; that is a separate,
larger piece of work and should be named as such.
