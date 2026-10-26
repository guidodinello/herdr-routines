---
id: "068"
title: "Resume a pipeline run whose orchestrator died"
status: done
priority: medium
area: pipeline
---

## Description

Since issue 056, `pipeline-run` owns the overnight stage loop, and it has always
been *manually* resumable: `state.json:current_stage` is the resume point, and
relaunching `pipeline-run` against the same state file continues from it. What
has never happened is anybody relaunching it automatically. The stall watchdog
(issue 031) detects a run whose orchestrator died — the host rebooted under it,
or the deadline plus grace passed with a quiet heartbeat and no active systemd
unit — kills any worker it left behind, writes a terminal report, and stops.
Its module docstring says so plainly: "Never resumes or retries. Kill + report
only."

That is now the last manual step in a night that is otherwise fully automated.
The ROADMAP bullet proposed either resuming from the last completed stage or
"at least writing a partial report". The partial-report half has since shipped:
`pipeline-run` writes a report on every path it controls, `pipeline-launch.sh`
writes a failed stub if the process is killed before it can, and the watchdog
writes one for a run it reaps. So what is left is the resume.

This issue makes the watchdog take one automatic resume attempt before falling
back to today's kill-and-report behaviour. One attempt, not a retry loop: a run
that dies twice is evidence of a systemic problem, and a second automatic
restart would just burn another night on it.

Two smaller pieces are prerequisites, and both are bugs in their own right:

### 1. `current_stage` advances before the gate passes

`run_pipeline`'s docstring says `current_stage` "advances only after a gate
passes, so re-running a half-finished night replays a stage rather than skipping
one". The code does not do that. `_run_agent_stage` commits
`current_stage=spec.stage` as soon as the stage's agent settles and its session
id is read — *before* the gate runs (`pipeline_run.py`, the `state.commit` after
`agent_session_id`). The post-gate commit writes the same value again.

A crash between those two commits leaves `state.json` claiming a stage is done
when its gate never ran. Today that only means a manual resume could skip a
gate. With an automatic resume it becomes the normal path, so the resume point
must mean "last gate-passed stage" before anything relies on it.

### 2. The watchdog cannot locate the stage prompts

A resume has to launch `pipeline-run` with `--prompts-dir`, and the launcher
derives it from `$REPO_PARENT/docs/pipeline/stages`. `state.json` records the
worktree, branch, spec, report and deadline, but not `repo_parent`, so a
watchdog holding only the state file cannot construct the launch command. The
job's `repo:` path is in config, but the watchdog is deliberately not wired
through tick's job model (see its module docstring) and does not load config.

## Design

### Part 1 — make `current_stage` mean "last gate-passed stage"

`_run_agent_stage` already re-reads state after the stage and commits the real
session id. Keep that commit, but remove `current_stage` from it:

```python
state.commit(state_json, stage_sessions={...})   # after the agent settles
```

The only writer of `current_stage` becomes the existing post-gate commit:

```python
state = state.commit(state_json, current_stage=spec.stage)
```

Consequences, all intended:

- A crash after the agent settles but before the gate replays that stage on
  resume. The stage's session id is already in `stage_sessions`, but the loop's
  `if spec.stage <= state.current_stage: continue` will not skip it; the stage
  starts a fresh `pl-<N>-<run_id>` agent as it does today.
- `validate_stage_sessions` is unaffected. It only requires sessions for stages
  up to `current_stage`, so a recorded session for a not-yet-gate-passed stage
  is extra, not missing.
- The prompt values do not read `current_stage` (see `_prompt_values`), so no
  prompt changes.
- The docstring becomes true, and the comment above the session commit should
  say why the session and the stage marker are written at different times.

Add a regression test: run one agent stage whose gate fails, then assert
`state.json:current_stage` is still the previous stage while that stage's
session id is present. A subsequent `run_pipeline` against the same state must
prompt that stage again rather than skip it.

### Part 2 — persist the resume inputs in `state.json`

`pipeline_prepare.py` already receives `repo_parent` and writes the state
document. Add two keys at prepare time:

- `repo_parent`: the absolute path of the parent clone (the value
  `pipeline-prepare --repo-parent` was given).
- `resume_count`: `0`.

`repo_parent` is enough to derive `prompts_dir` exactly the way
`pipeline-launch.sh` does: `<repo_parent>/docs/pipeline/stages`. Do not add a
second copy of the prompts path.

Old state files written before this change have neither key; a run whose state
lacks `repo_parent` is simply not resumable, and falls back to today's
kill-and-report path. Do not guess it from the worktree or the feature source.

### Part 3 — one resume attempt in the watchdog

`_reap_stalled_run` already establishes that a run is stalled and that its
systemd unit is what the box says it is (`pipeline_unit_active`). After that,
before it writes a terminal report, it tries a resume.

**Eligibility.** All of these must hold; otherwise fall through to the existing
`_write_report` path unchanged:

- `state.json` parses and has a usable `repo_parent` (a non-empty string) and an
  `artifact_paths.report`;
- `resume_count` is an int below `MAX_PIPELINE_RESUMES` (a module constant, `1`);
- `history_path` is available and contains a `running` record whose
  `extra.pipeline_run_id` is this run id. That record supplies the job name (for
  the new history record) and the original deadline. A manual
  `herdr-routines pipeline-run` outside tick has no such record and is not
  resumed;
- the original budget is positive:
  `(recorded_deadline_epoch - running_record.ts.timestamp()) * 1000`. This is
  the run's original wall budget, reset for the resume — deliberately not the
  remaining budget, which is already negative by the time the watchdog fires.

**Order.** The steps matter because tick reconciles the same run from a
different timer process:

1. Append a fresh `running` history record for the same `run_id` and job, with
   `extra` carrying the pipeline run id, the report path, the new unit name, the
   new `deadline_epoch`, `resume_count`, and `resumed_from` (the old unit or
   `None`). Its `ts` is `now`. This resets `history.find_stale_running`'s clock,
   so tick cannot file the run as `no_report` during the window between the
   kill and the new unit becoming active.
2. Kill the stale worker panes with the existing logic (working panes normally;
   all panes after a host reboot).
3. Rewrite `state.json` atomically with `deadline_epoch = now + budget`,
   `resume_count + 1`, and `resumed_at = now.isoformat()`. Use the shared
   `write_state_json` (tmp + rename); never edit the JSON in place.
4. Register the resume unit and return. Do **not** write a terminal report on
   success.

**The unit.** Reuse the existing launcher in a new resume mode rather than a
second copy of the `uv run` line:

```
systemd-run --user --collect --unit=herdr-pipeline-<run_id>-r<HHMMSS> \
  -p RuntimeMaxSec=<budget_s + RESUME_UNIT_MARGIN_S> \
  /bin/bash <repo_parent>/scripts/pipeline-launch.sh \
    --run-id <run_id> --repo-parent <repo_parent> --report <report> \
    --state-json <state_json> --resume --deadline-epoch <new_deadline> \
    --agent-name <anything> --wait-timeout-ms <budget_ms>
```

The `-r<HHMMSS>` suffix is already covered by `PIPELINE_UNIT_GLOB`, and the
existing watchdog comment describes a resume unit in exactly this shape. The
`systemd-run` call goes behind a module-level `launch_resume(argv)` seam, the
same way `tick.launch_pipeline` is monkeypatched, so tests never shell out.

**Failure handling.** If `launch_resume` returns non-zero, write a terminal
report with `## Outcome: failed (resume_failed)`, including the rc and stderr,
and return an action with `resumed=False`. Add `resume_failed` to
`tick._classify_pipeline_outcome` so the history reason is `resume_failed`
rather than `orchestrator_failed`. If the resumed unit later dies without a
report, the next sweep sees `resume_count >= 1`, takes the existing report path,
and the report should carry a `resume_count` / `resume_attempted` line so the
morning reader can tell an exhausted resume from a first-time stall.

**Reporting.** `WatchdogAction` gains `resumed: bool`, and `report_path` becomes
`Path | None` (None when a resume was launched). `_cmd_pipeline_watchdog` logs a
resumed action as "stalled, resuming from stage N (unit …)" instead of "report
at …". Send one notification on a successful resume — the human should know a
night was rescued, not just see a green history line later.

**Host reboot.** This is the commonest cause of a dead run. The reboot check
runs before the liveness unit check, and the existing code closes *all* of the
run's panes after a reboot (herdr restores them as `blocked`). Resume after that
cleanup; do not special-case the reboot out of resuming.

Non-goal: resuming a run whose *stage* died but whose `pipeline-run` process is
still alive. The existing per-stage fallback/quota handling owns that; the
watchdog only acts when the whole unit is gone.

### Part 4 — `pipeline-launch.sh --resume`

Add `--resume` and `--state-json PATH` to the launcher:

- with `--resume`, skip the `pipeline-prepare` block entirely; the state file
  already exists and the feature is already claimed;
- require `--state-json` in this mode and use it as `STATE_JSON`;
- use the same `--repo-parent/docs/pipeline/stages` prompts dir, the same
  `--report`, the same failure-marker forwarding and the same empty-report stub
  backstop as the normal path;
- leave the default (no `--resume`) path byte-for-byte behaviourally unchanged.

The launcher is the right place for this because it already owns the
`RuntimeMaxSec`/report-stub contract that makes a killed resume visible to tick.
Putting the resume launch in the watchdog instead would duplicate that backstop.

### Part 5 — docs

- Update `pipeline_watchdog.py`'s module docstring: it now makes one resume
  attempt before reporting, not "never resumes".
- Update the checkpointing/resume paragraph in `docs/pipeline/design.md` so it
  describes the automatic attempt and the `resume_count` cap instead of listing
  resume as a future v2 item.
- Add a Log entry to issue 031 pointing here, since 031 deferred this explicitly
  and its "does not resume" statement is being narrowed.

## Acceptance

1. **`current_stage` is gate-gated.** A stage whose gate fails leaves
   `state.json:current_stage` at the previous stage, while its real session id
   is recorded in `stage_sessions`. A second `run_pipeline` against that state
   prompts the failed stage again. Test:
   `test_pipeline_run_does_not_advance_current_stage_before_its_gate`.
2. **Resume point is last gate-passed stage.** With `current_stage = 4` and the
   matching sessions, `run_pipeline` starts at stage 5 and never prompts stages
   1–4. Test: `test_pipeline_run_starts_after_the_last_gate_passed_stage`.
3. **State carries the resume inputs.** `pipeline-prepare` writes `repo_parent`
   as the absolute `--repo-parent` value and `resume_count: 0`. Test in
   `tests/test_pipeline_prepare.py`.
4. **A stalled run is resumed once.** Given a stalled run with an inactive unit,
   a matching `running` history record and a resumable state file, one sweep:
   kills the worker panes, appends a fresh `running` record with the new
   deadline/unit, rewrites `state.json` with a later `deadline_epoch`,
   `resume_count == 1` and `resumed_at`, calls `launch_resume` once with the
   argv above (including `--resume` and the same state/report/prompts), writes
   **no** terminal report, and returns an action with `resumed is True`.
5. **Fallback paths stay kill-and-report.** Each of these writes the existing
   terminal report and calls no launcher: missing `repo_parent`; no matching
   history record; `resume_count >= MAX_PIPELINE_RESUMES`; a non-positive
   original budget. The report names the reason. Test each path.
6. **A failed launch is terminal.** When `launch_resume` returns non-zero, the
   sweep writes `## Outcome: failed (resume_failed)` with the rc/stderr, returns
   `resumed=False`, and does not leave the run looking in-flight. Tick's
   classifier maps that marker to reason `resume_failed`.
7. **Reap-after-reboot resumes.** The host-reboot fast-fail path closes the
   restored panes and then launches a resume instead of only writing a report
   (when its state is otherwise resumable).
8. **No resume attempt while the unit is alive.** The existing
   `pipeline_unit_active` guard is unchanged: an active or unknown unit is never
   killed or resumed. The existing tests for that stay green.
9. **Launcher resume mode.** `scripts/pipeline-launch.sh --resume
   --state-json PATH …` does not invoke `pipeline-prepare`, invokes
   `pipeline-run` with that state file and the derived prompts dir, forwards the
   failure markers, and writes the failed stub if `pipeline-run` leaves the
   report empty. Tests in `tests/test_pipeline_launch_sh.py`; the existing
   non-resume tests must pass unchanged.
10. **Docs and CI.** The watchdog module docstring and `docs/pipeline/design.md`
    describe the single automatic resume, and issue 031's Log points here.
    `scripts/ci-check.sh` is green (ruff format, ruff check, `uv run mypy`,
    `uv run pytest -q`).

## Log

- **2026-10-22**: filed from the ROADMAP Parking Lot, selected by
  `herdr-routines refine-issue` under issue 029. The partial-report half of the
  bullet already shipped (pipeline-run, the launcher stub and the watchdog all
  write terminal reports), so this covers the remaining resume half. Two
  prerequisites are included because an automatic resume is only safe if
  `current_stage` really is the gate-passed stage and the watchdog can locate
  the stage prompts from `state.json` alone.
