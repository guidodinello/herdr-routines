# Spec v2 — 20261026T050000Z

Issue: `docs/process/issues/068-resume-a-pipeline-run-whose-orchestrator-died.md`
(medium, pipeline). Claimed for this run.

## Problem

Since issue 056, `pipeline-run` owns the overnight stage loop and is *manually*
resumable (`state.json:current_stage` is the resume point), but nobody relaunches
it automatically. The stall watchdog (issue 031) detects a run whose orchestrator
died — host reboot, or deadline + grace with a quiet heartbeat and no active
systemd unit — then kills any leftover worker, writes a terminal report, and
stops ("Never resumes or retries. Kill + report only."). The partial-report half
of the ROADMAP bullet already shipped (pipeline-run, launcher stub, watchdog all
write terminal reports); the automatic resume is the last manual step in an
otherwise fully automated night.

Two prerequisite bugs make an automatic resume unsafe until fixed:

1. **`current_stage` advances before the gate passes.** `_run_agent_stage`
   commits `current_stage=spec.stage` right after the agent settles and its
   session id is read — *before* the gate (`pipeline_run.py`, the `state.commit`
   after `agent_session_id`). A crash between that commit and the post-gate
   commit leaves `state.json` claiming a stage is done when its gate never ran.
   For a manual resume that only means a gate could be skipped; with an automatic
   resume it becomes the normal path.
2. **The watchdog cannot locate the stage prompts.** A resume must launch
   `pipeline-run` with `--prompts-dir`, derived from
   `<repo_parent>/docs/pipeline/stages`. `state.json` records worktree, branch,
   spec, report and deadline but not `repo_parent`, and the watchdog deliberately
   does not load tick's config.

## Approach

Five parts, in dependency order:

### Part 1 — `current_stage` means "last gate-passed stage"

In `_run_agent_stage` (src/herdr_routines/pipeline_run.py): keep the post-agent-settle commit that records the real session id, but drop
`current_stage` from it (`state.commit(state_json, stage_sessions={...})`). The
only writer of `current_stage` becomes the existing post-gate commit
(`state.commit(state_json, current_stage=spec.stage)`).

Intended consequences:

- A crash after the agent settles but before the gate replays that stage on
  resume (`if spec.stage <= state.current_stage: continue` won't skip it); the
  stage starts a fresh `pl-<N>-<run_id>` agent as today.
- `validate_stage_sessions` unaffected: it requires sessions for stages up to
  `current_stage`, so a recorded session for a not-yet-gate-passed stage is
  extra, not missing.
- No prompt changes: `_prompt_values` does not read `current_stage`.
- `run_pipeline`'s docstring becomes true; comment above the session commit
  explains why session and stage marker are committed at different times.

Regression test: `test_pipeline_run_does_not_advance_current_stage_before_its_gate`
— one agent stage whose gate fails; assert `current_stage` is still the previous
stage while that stage's session id is present, and a second `run_pipeline`
against the same state prompts the stage again rather than skipping.

Also add `test_pipeline_run_starts_after_the_last_gate_passed_stage` (with
`current_stage = 4` + matching sessions, `run_pipeline` starts at stage 5 and
never prompts stages 1–4).

### Part 2 — persist resume inputs in `state.json`

`pipeline_prepare.py` already receives `repo_parent` and writes the state doc.
Add two keys at prepare time:

- `repo_parent`: the absolute `--repo-parent` value given to `pipeline-prepare`.
- `resume_count`: `0`.

Prompts dir derives exactly as `pipeline-launch.sh` does:
`<repo_parent>/docs/pipeline/stages`. No second copy of the prompts path.
Old state files lacking `repo_parent` make a run simply not resumable (falls
back to kill-and-report); never guess it from the worktree or feature source.
Test: `test_pipeline_prepare_writes_repo_parent_and_resume_count` in
`tests/test_pipeline_prepare.py`.

### Part 3 — one resume attempt in the watchdog

In `pipeline_watchdog.py`, `_reap_stalled_run`, after stall + unit-inactive are
established (`pipeline_unit_active`) and before the terminal report:

**Eligibility** (all must hold, else fall through to `_write_report` unchanged):

- `state.json` parses with a usable `repo_parent` (non-empty string) and
  `artifact_paths.report`;
- `resume_count` is an int below `MAX_PIPELINE_RESUMES` (module constant, `1`);
- `history_path` available with a `running` record whose
  `extra.pipeline_run_id` is this run id — supplies job name and original
  deadline (a manual `herdr-routines pipeline-run` outside tick has no such
  record and is not resumed);
- positive original budget `(recorded_deadline_epoch - running_record.ts) *
  1000` — the run's wall budget, reset for the resume (not the remaining
  budget, already negative by watchdog time).

**Order** (matters: tick reconciles the same run from another timer process):

1. Append a fresh `running` history record for the same `run_id`/job, `extra`
   carrying pipeline run id, report path, new unit name, new `deadline_epoch`,
   `resume_count`, `resumed_from` (old unit or `None`); `ts = now`. Resets
   `history.find_stale_running`'s clock so tick can't file `no_report` between
   kill and new-unit-active.
2. Kill stale worker panes (existing logic: working panes normally, all panes
   after host reboot — the reboot check runs first and resume happens after that
   cleanup; no reboot special-case out of resuming).
3. Rewrite `state.json` atomically via the shared `write_state_json`
   (tmp + rename): `deadline_epoch = now + budget`, `resume_count + 1`,
   `resumed_at = now.isoformat()`.
4. Register the resume unit and return — **no terminal report on success.**

**The unit.** New module-level `launch_resume(argv)` seam (monkeypatched by
tests, like `tick.launch_pipeline`); it shells out to the existing launcher in
resume mode:

```
systemd-run --user --collect --unit=herdr-pipeline-<run_id>-r<HHMMSS> \
  -p RuntimeMaxSec=<budget_s + RESUME_UNIT_MARGIN_S> \
  /bin/bash <repo_parent>/scripts/pipeline-launch.sh \
    --run-id <run_id> --repo-parent <repo_parent> --report <report> \
    --state-json <state_json> --resume --deadline-epoch <new_deadline> \
    --agent-name <anything> --wait-timeout-ms <budget_ms>
```

`-r<HHMMSS>` is covered by `PIPELINE_UNIT_GLOB`; the existing watchdog comment
already sketches this unit shape.

**Failure handling.** Non-zero `launch_resume` → terminal report with
`## Outcome: failed (resume_failed)` incl. rc and stderr, action
`resumed=False`. Add `resume_failed` to `tick._classify_pipeline_outcome`. A
resumed unit that later dies report-less: next sweep sees `resume_count >= 1`,
takes the existing report path, and the report carries a `resume_count` /
`resume_attempted` line (exhausted resume vs first-time stall).

**Reporting.** `WatchdogAction` gains `resumed: bool`; `report_path` becomes
`Path | None` (None when a resume was launched). `_cmd_pipeline_watchdog` logs
"stalled, resuming from stage N (unit …)" instead of "report at …". One
notification on a successful resume so a rescued night is visible in the
morning.

Non-goal: resuming a run whose *stage* died but `pipeline-run` is alive — the
per-stage fallback/quota handling owns that; watchdog acts only when the whole
unit is gone. The `pipeline_unit_active` guard is unchanged (tests stay green).

Tests: `tests/test_pipeline_watchdog.py` —
`test_run_watchdog_resumes_a_stalled_run_once` (incl. the record-before-kill
order), `test_run_watchdog_falls_back_to_kill_and_report_for_each_ineligible_reason`,
`test_run_watchdog_resume_launch_failure_is_terminal`,
`test_run_watchdog_resumes_after_host_reboot`; the guard's existing
`test_run_watchdog_does_not_reap_when_unit_active_or_unknown` stays as-is.
Tick's classifier: `test_tick_classifies_resume_failed` in
`tests/test_tick.py`.

### Part 4 — `pipeline-launch.sh --resume`

Add `--resume` and `--state-json PATH` to `scripts/pipeline-launch.sh`:

- with `--resume`, skip the `pipeline-prepare` block (state exists, feature
  claimed);
- require `--state-json` in this mode, use as `STATE_JSON`;
- same prompts dir derivation, `--report`, failure-marker forwarding, and
  empty-report stub backstop as the normal path;
- default (no `--resume`) path byte-for-byte behaviourally unchanged.

The launcher is right because it already owns the `RuntimeMaxSec`/report-stub
contract that makes a killed resume visible to tick; putting resume launch in
the watchdog directly would duplicate that backstop. Tests in
`tests/test_pipeline_launch_sh.py`: `test_launcher_resume_skips_prepare_and_uses_state_json`
and `test_launcher_resume_forwards_failure_markers_and_stub`; existing
non-resume tests (e.g. `test_launcher_runs_pipeline_run_after_prepare`) pass
byte-for-byte unchanged.

### Part 5 — docs

- `pipeline_watchdog.py` module docstring: one resume attempt before reporting,
  not "never resumes".
- `docs/pipeline/design.md` checkpointing/resume paragraph: describe the
  automatic attempt and `resume_count` cap; drop "resume as future v2" framing.
- Issue 031: add a Log entry pointing here (031 deferred this explicitly; its
  "does not resume" statement is being narrowed).

## Files touched

| File | Change |
| ---- | ------ |
| `src/herdr_routines/pipeline_run.py` | Part 1: move `current_stage` write to post-gate commit only; docstring/comments |
| `src/herdr_routines/pipeline_prepare.py` | Part 2: write `repo_parent` (absolute) and `resume_count: 0` at prepare time |
| `src/herdr_routines/pipeline_watchdog.py` | Part 3: eligibility checks, resume ordering (history record, pane kill, `write_state_json`, unit), `launch_resume` seam, `resume_failed` report path, `WatchdogAction.resumed`, docstring |
| `src/herdr_routines/tick.py` | Part 3: add `resume_failed` to `_classify_pipeline_outcome`; adapt to `WatchdogAction` change |
| `scripts/pipeline-launch.sh` | Part 4: `--resume` + `--state-json` mode |
| `tests/test_pipeline_run.py` | Part 1: `test_pipeline_run_does_not_advance_current_stage_before_its_gate`, `test_pipeline_run_starts_after_the_last_gate_passed_stage` |
| `tests/test_pipeline_prepare.py` | Part 2: `test_pipeline_prepare_writes_repo_parent_and_resume_count` |
| `tests/test_pipeline_watchdog.py` | Part 3: `test_run_watchdog_resumes_a_stalled_run_once`, `test_run_watchdog_falls_back_to_kill_and_report_for_each_ineligible_reason`, `test_run_watchdog_resume_launch_failure_is_terminal`, `test_run_watchdog_resumes_after_host_reboot`; guard tests unchanged |
| `tests/test_tick.py` | Part 3: `test_tick_classifies_resume_failed` |
| `tests/test_pipeline_launch_sh.py` | Part 4: `test_launcher_resume_skips_prepare_and_uses_state_json`, `test_launcher_resume_forwards_failure_markers_and_stub`; existing non-resume tests unchanged |
| `docs/pipeline/design.md` | Part 5: checkpointing/resume paragraph |
| `docs/process/issues/031-*.md` | Part 5: Log entry pointing here |

## Risks

- [blocking] **Tick/watchdog interleaving.** Tick reconciles the same run on its
  own clock. The record-then-kill ordering (step 1 before step 2) is chosen so
  `history.find_stale_running` never sees a gap; getting the order wrong is the
  most likely way to file a fine run as `no_report`. The ordering is asserted by
  `test_run_watchdog_resumes_a_stalled_run_once` — confidence: high
- [blocking] **Resume storm.** Cap at one attempt (`MAX_PIPELINE_RESUMES = 1`) so a run
  that dies twice stops burning nights. If the second sweep fires while the
  resumed unit is not yet active (window between `systemd-run` returning and
  unit active), the `resume_count >= 1` check routes it to kill-and-report, not
  a second resume — that is the intended fallout, not a bug, but it means the
  sweep cadence bounds how quickly a resumed run dying again is reported. — confidence: high
- [non-blocking] **`state.json` schema drift.** Old state files missing `repo_parent` fall back
  to kill-and-report, silently; acceptable (one-time transition), but the
  fallback report should make the ineligibility reason visible so it isn't
  mistaken for a staleness bug. — confidence: high
- [blocking] **Launcher regression surface.** `pipeline-launch.sh` is shared by every
  normal run; the resume mode must be additive only. Existing non-resume tests
  must pass byte-for-byte unchanged. — confidence: high
- [blocking] **Unit-name collisions** between the dead original unit and the `-r<HHMMSS>`
  resume unit are handled by the existing `PIPELINE_UNIT_GLOB` matchers and the
  `--collect` flag; a mismatched glob would make the next sweep kill or double-
  resume the wrong unit — covered by naming tests. — confidence: medium
- [blocking] **Budget semantics.** The resume resets the *original* wall budget, not the
  remaining (negative) one. Wrong sign handling here would create a zero or
  negative `RuntimeMaxSec` unit — eligibility requires a positive budget, and a
  test covers the non-positive-budget fallback. — confidence: high

## Acceptance criteria

1. [blocking] `current_stage` is gate-gated: a stage whose gate fails leaves `state.json:current_stage` at the previous stage while its real session id is recorded in `stage_sessions`, and a second `run_pipeline` against that state prompts the failed stage again instead of skipping it — confidence: high — Test: test_pipeline_run_does_not_advance_current_stage_before_its_gate
2. [blocking] Resume point is the last gate-passed stage: with `current_stage = 4` and the matching sessions, `run_pipeline` starts at stage 5 and never prompts stages 1–4 — confidence: high — Test: test_pipeline_run_starts_after_the_last_gate_passed_stage
3. [blocking] State carries the resume inputs: `pipeline-prepare` writes `repo_parent` (the absolute `--repo-parent` value) and `resume_count: 0` into `state.json` at prepare time — confidence: high — Test: test_pipeline_prepare_writes_repo_parent_and_resume_count
4. [blocking] A stalled run is resumed once: one sweep kills the worker panes, appends a fresh `running` history record with the new deadline/unit/`resume_count` (before the kill: tick must not see an `in-flight` gap), rewrites `state.json` atomically with a later `deadline_epoch`, `resume_count == 1` and `resumed_at`, calls `launch_resume` once with the argv above (incl. `--resume`, same state/report/prompts), writes no terminal report and returns an action with `resumed is True` — confidence: high — Test: test_run_watchdog_resumes_a_stalled_run_once
5. [blocking] Every ineligible run stays kill-and-report: missing/empty `repo_parent`, no matching `running` history record, `resume_count >= MAX_PIPELINE_RESUMES`, and a non-positive original budget each write the existing terminal report naming the reason (the exhausted-resume variant carries a `resume_count`/`resume_attempted` line) and call the launcher zero times — confidence: high — Test: test_run_watchdog_falls_back_to_kill_and_report_for_each_ineligible_reason
6. [blocking] A failed launch is terminal: non-zero `launch_resume` writes `## Outcome: failed (resume_failed)` including rc and stderr and returns `resumed=False`, leaving the run not looking in-flight — confidence: high — Test: test_run_watchdog_resume_launch_failure_is_terminal
7. [blocking] Tick's classifier maps the `resume_failed` marker to history reason `resume_failed`, not `orchestrator_failed` — confidence: high — Test: test_tick_classifies_resume_failed
8. [blocking] A run reaped after a host reboot is resumed like any other: the reboot check closes the restored panes first and the sweep then attempts the resume (no reboot special-case out of resuming) when state/history are otherwise resumable — confidence: medium — Test: test_run_watchdog_resumes_after_host_reboot
9. [blocking] No resume attempt while the unit is alive or unknown: the `pipeline_unit_active` guard is unchanged — active/unknown units are never killed or resumed; the existing guard tests stay green unchanged — confidence: high — Test: test_run_watchdog_does_not_reap_when_unit_active_or_unknown
10. [blocking] Launcher resume mode skips prepare: `pipeline-launch.sh --resume --state-json PATH` does not invoke `pipeline-prepare` and invokes `pipeline-run` with that state file and the derived `<repo_parent>/docs/pipeline/stages` prompts dir — confidence: high — Test: test_launcher_resume_skips_prepare_and_uses_state_json
11. [non-blocking] Launcher resume contract parity: resume mode forwards the failure markers and keeps the empty-report failed-stub backstop exactly like the normal path — confidence: medium — Test: test_launcher_resume_forwards_failure_markers_and_stub
12. [non-blocking] Launcher default path is untouched: without `--resume` the script behaves byte-for-byte as today; existing non-resume launcher tests pass unchanged — confidence: high — Test: test_launcher_runs_pipeline_run_after_prepare
13. [non-blocking] Docs say one resume attempt: the watchdog module docstring, the `docs/pipeline/design.md` checkpointing paragraph, and issue 031's Log no longer say "never resumes" but describe the single attempt and the `resume_count` cap — confidence: medium — Test: test_resume_once_is_documented

## Changelog v1→v2

- Added `## Acceptance criteria` (13 numbered items, each ending
  `Test: <exact test name>`), replacing v1's pointer-only `## Acceptance`
  section; maps to the ten criteria in issue 068 with tick's classifier split
  out and the launcher covered by three items (resume mode, parity, default
  path unchanged).
- Added `blocking`/`non-blocking` tiers and `confidence: high|medium|low`
  annotations to both Risks and the new Acceptance criteria (v1 had neither).
- Named the tests v1 left anonymous: Part 2's
  `test_pipeline_prepare_writes_repo_parent_and_resume_count` (v1: "Test in
  `tests/test_pipeline_prepare.py`"), Part 3's watchdog fallback/launch/reboot
  tests and tick's `test_tick_classifies_resume_failed`, Part 4's launcher
  `test_launcher_resume_skips_prepare_and_uses_state_json` /
  `test_launcher_resume_forwards_failure_markers_and_stub`, and the docs-pin
  `test_resume_once_is_documented`.
- Files touched table: added the missing `tests/test_tick.py` row (v1 listed
  `src/herdr_routines/tick.py` but no test for its classifier change) and
  fixed the other test rows to the pinned names.
- Header retitled to Spec v2; Problem/Approach unchanged — v1's mechanism
  review found no further defects to amend.

## Review

- blocking: items 1–10 gate the merge — the resume must never skip a gate
  (1–2), be derivable from state alone (3), fire at most once in a safe order
  (4), and every path that cannot resume must stay terminal (5–9); launcher
  resume mode must not break the shared launch path (10).
- non-blocking: items 11–13 are contract-hardening (launcher stub/backstop
  parity, default-path pin) and the docs pin; losing them would not break the
  resume semantics.
- confidence: high for the direct state/argv/terminal assertions
  (items 1–7, 9–12 — they are seam-level tests like the existing
  `tick.launch_pipeline` monkeypatched tests); medium for the host-reboot
  interplay sweep (item 8) and the docs-as-test pin (item 13), where wording
  drift is the main failure mode.
- CI gate: `scripts/ci-check.sh` green (ruff format, ruff check, `uv run
  mypy`, `uv run pytest -q`) covers items across parts 1–4; no dedicated test
  name, it is the merge gate for all of the above.
