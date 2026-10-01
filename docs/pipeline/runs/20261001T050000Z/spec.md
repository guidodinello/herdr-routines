# spec v2 — orchestrator stage loop and PR stage in code (issue 056, phases B and C)

Source issue: [`docs/process/issues/056-orchestrator-stage-loop-in-code.md`](../../process/issues/056-orchestrator-stage-loop-in-code.md)
(issue 054 phases B and C). Built on phase A, already shipped in `src/herdr_routines/pipeline_prepare.py`.

v1 → v2 corrections, all verified against the code on this branch, are listed in
[`## Changelog v1→v2`](#changelog-v1v2).

## problem

Phase A moved the pre-flight into code; everything after it is still an LLM. The
orchestrator `opencode/big-pickle` session that `scripts/pipeline-launch.sh:162` starts is
instructed by `docs/pipeline/orchestrator-prompt.md` to do five purely mechanical things that
code can do exactly and a model can do *plausibly wrong*:

1. **It writes its own evidence.** `orchestrator-prompt.md:59` tells the orchestrator to record
   `agent_session.value` per stage into `state.json:stage_sessions`. That map is the G-17
   stage-independence proof, and it is written by the model it is supposed to be evidence about.
   It has been fabricated twice — `ses_..._fake1`, `ses_fake_1_<run_id>` on 2026-09-06/07 (see
   `pipeline_watchdog.py:84-88`), caught only afterwards by `validate_stage_sessions`
   downgrading a `done` report to `failed` in `tick.py:1788-1790`.
2. **It judges its own gates.** Gates 1–5 are `test`/`rg`/`git`/`gh`/`jq` one-liners in prose
   (`orchestrator-prompt.md:125,132,139,144-146,153`). Gates `ci` and `6` already became code
   (`gates.py`) *because* the model silently substituted a stricter regex on PR #81 — the prose
   gates have the same exposure and no defence.
3. **It emits its own liveness signal.** The heartbeat at
   `/tmp/pipeline_resume_<run_id>.log` is a model-written line, and `pipeline_watchdog.is_stalled`
   (`pipeline_watchdog.py:361`) decides whether to kill a run based on it.
4. **It is the fragile part of the run.** `design.md`'s own finding: it died silently between
   stages 4 and 5 on the first real run, wrote no report, and only `state.json` + the resume
   recipe saved the night. The processes that must not die are the workers.
5. **Stage 4 needs no model at all.** It is `git push -u` + `gh pr create` from a spec's title
   and body. It currently costs a worker pane and a model that can mis-execute it.

The judgement calls the orchestrator was kept for (rewrite a prompt after a weak review,
addressable vs needs-human per finding) are exactly what stages 2 and 6 are for; the loop
between them is not judgement.

## approach

A new subcommand `herdr-routines pipeline-run` owns the stage loop. It starts no orchestrator
agent; it is the orchestrator's *mechanical* half, and `pipeline-launch.sh` calls it in place of
`herdr agent start pipeline-orchestrator`.

### 1. The workflow becomes data (`src/herdr_routines/pipeline_stages.py`)

`STAGES` does **not** live in `pipeline_run.py` (v1 had it there — that is a circular import:
`pipeline_run` needs `pipeline_watchdog.heartbeat_log_path`, and `pipeline_watchdog`'s
validator needs the layout table). One leaf module, `pipeline_stages.py`, stdlib-only, imported
by both:

```python
@dataclass(frozen=True, slots=True)
class StageSpec:
    stage: int
    model: str | None          # None => no agent at all (stage 4)
    prompt_file: str | None    # None for stage 4
    timeout_ms: int
    isolation: Literal["independent", "none", "reused"]  # G-17, read by the gate below
    reuses_stage: int | None   # 6 -> 3
```

One entry per stage, built from `orchestrator-prompt.md:121-160` with the models and timeouts it
already pins (line 115 for timeouts, 122/129/136/150/156 for models): stage 1/2
`opencode/muse-spark-1.2-contributor-free` 60m, stage 3 `opencode/x-preview-f-free` 90m, stage 4
no agent, stage 5 `opencode/big-pickle` 60m, stage 6 reuses stage 3's session, all
`start_timeout_ms=120000`.

`isolation` is the concrete instantiation `design.md:261-267` asked for: a declarative
per-stage field with one generic gate function reading it, instead of bespoke Gate 1i/2i pairs
re-derived per hardcoded stage.

### 2. `validate_stage_sessions` gains a **defaulted** layout, so no caller changes

`pipeline_watchdog.validate_stage_sessions` (`pipeline_watchdog.py:115`) requires every entry to
be distinct and `len(values) >= min(current_stage, 6)` (lines 136-147). Phase B+C deliberately
breaks both: stage 6 legitimately records stage 3's id, and stage 4 records none while
`current_stage` reaches 4. Left alone, every run downgrades itself to
`failed (stage_independence_unverified)` at `tick.py:1790`.

New signature `validate_stage_sessions(raw, *, expected: Sequence[StageSpec] = STAGES)`. The
default matters more than the check: both existing call sites — `tick._pipeline_stage_independence_issue`
(`tick.py:1621`) and `pipeline_watchdog._parse_state_json` (`pipeline_watchdog.py:293`) — keep
working untouched, so **`tick.py` needs no change for this** (v1 said it did).

The relaxed rules, all derived from `isolation`:
- every stage ≤ `current_stage` with `isolation="independent"` has exactly one entry, a real
  `ses_…` id, distinct from every other independent stage's;
- a `model=None` stage (4) contributes no entry and no count;
- a `reused` stage's entry must equal the id of the stage it reuses (6 → 3) and must not count
  toward the distinct-session total.

### 3. Stage prompts move to files, verbatim

`docs/pipeline/stages/stage-{1,2,3,5,6}.md`, created by cutting the `**Prompt:**` lines out of
`orchestrator-prompt.md` (124, 131, 138, 152, 158) unchanged. `pipeline-run` substitutes
`$RUN_ID`, `$WT`, `$BRANCH`, `$ISSUE_ID`, `$FEATURE_SOURCE`, `$PIPELINE_REPORT`, `$PR_NUMBER`
from `state.json` — the same values `pipeline_prepare.render_prepared_values` already emits as
`KEY=VALUE`, so substitution is a read of the file phase A wrote, not a second resolution
path. No prompt content is rewritten, so stage 3's "flip the issue `status: done`" instruction
survives into the code-run PR.

### 4. One shared wait loop, with the heartbeat inside it

The loop already exists twice and needs no new hook. `HerdrClient.agent_prompt_wait_with_watchdog`
(`herdr.py:382`) already runs the prompt under `Popen`, polls `agent_read_visible` every
`poll_interval_s`, and raises `PromptWatchdogKilled` (`herdr.py:94`, a `HerdrCliError`
subclass) — and its `on_poll` callback already has the shape that matters:
`Callable[[str], str | None]`, screen text in, confirmed marker out.
`runner._prompt_with_watchdog` (`runner.py:164-224`) is a thin wrapper adding only the
start-race retry and the stability gate; `runner._matched_failure_marker` (`runner.py:253-264`)
supplies the prompt-echo false-positive guard.

So: move `_prompt_with_watchdog`, `_matched_failure_marker`, `_is_retryable_prompt_error`,
`_error_body_code`, `_is_settle_timeout` into `src/herdr_routines/wait_loop.py` as public
`prompt_with_watchdog(...)`; `runner.py` imports it back unchanged. **The heartbeat is written
inside the existing `on_poll` closure** — the same closure that scans for markers — one line per
poll. There is no new `on_poll` parameter: v1 proposed a zero-arg `on_poll: Callable[[], None]`
hook, which cannot receive the screen text the marker scan needs and duplicates a hook that
already exists with a different signature.

`pipeline-launch.sh:189-223` re-implemented this loop in bash; that copy is deleted.

### 5. `stage_sessions` and `current_stage` written by code, atomically

After `agent_start`, `pipeline-run` reads the real id via `client.agent_session_id(name)`
(`herdr.py:440`, reading `result.agent.agent_session.value`) and writes `state.json` with
`pipeline_prepare._write_state_json` (atomic tmp+rename, `pipeline_prepare.py:328`), promoted to
a shared public `write_state_json`. The write happens after the agent exists and before the
prompt is sent. Stage 6 records stage 3's id under both `3` and `6` — honest, and what the
declared reuse allowance in §2 consumes. Stage 4 records nothing, because it starts no agent.
`current_stage` advances after each stage's gate passes.

### 6. Gates in-process — but not gate 3, and not gate 5 verbatim

Add `run_stage_gate(stage, *, cwd, run_id, state, gh, owner, repo)` to `gates.py`, dispatching to
one function per gate, and extend `cli.py`'s `gate --stage` choices from `["ci","6"]` to
`["1","2","4","5","6","ci"]` so the same verdicts stay reachable by exit code from a shell
(`orchestrator-prompt.md:147,159` already relies on that). Extending it needs two edits v1 missed:
`--pr` is `required=True` (`cli.py:320`) and must become conditionally required, and `_cmd_gate`
resolves owner/repo unconditionally before dispatch (`cli.py:968-970`) — that resolution has to
move inside the PR-dependent branches, since gates 1/2/4 need no GitHub repo at all.

Three porting rules, each from the code:

- **Gate 3 stays prose.** `orchestrator-prompt.md:139` is explicit ("prose by design (issue
  034) … the authoritative check is Gate CI below") and `gates.py:12-17` says
  `GATE3_LINT_TEST_CHECKS` is "not exposed as a CLI stage". So do **not** add `3` to `--stage`
  and do not have `pipeline-run` compute the lint/pytest verdict. Only the mechanical half —
  every acceptance-criterion test name present under `tests/` via fixed-string `rg -F`, scoped to
  `tests/` (G-2), "existence first, green second" — becomes `gate3_test_names(...)`, run in-process.
- **Gate 5 must not be ported verbatim.** `orchestrator-prompt.md:153`'s filter is
  `test("blocking")`, which matches `non-blocking` by substring — the exact issue-035 bug that
  `gates.BLOCKING_TAG = "[blocking]"` (`gates.py:58-60`) exists to kill. Port it anchored: at
  least one `[blocking]` or `[non-blocking]` tier label must be present, matched on the literal
  bracketed form. (The `confidence:` token the skill never emits stays out of it; gate 5 was
  deliberately relaxed for that reason, `design.md:241`.)
- **`gh pr create` has no client method.** The `GhClient` protocol (`auto_fix.py:32`) has only
  `api_user`, `pr_view`, `graphql`. Gate 4 needs a `git`/`gh` seam (reuse `run_checks`'s
  injectable `runner`, `auto_fix.py:549`) *and* stage 4 needs a real `pr create`; add
  `RealGhClient.pr_create` rather than shelling out from `pipeline_run`.

`pipeline-run` calls `run_stage_gate` directly. A failing gate aborts before stage N+1 and
writes the partial report naming the gate and its reason — criterion 2. Gates 1i/2i stop being
live subprocess checks (`herdr agent list | jq …`, `orchestrator-prompt.md:126,133`) and become
the §2 in-code session-layout check, which is strictly stronger: it compares against ids this
same process recorded, so a fabricated id is unrepresentable rather than merely detectable.

### 7. Stage 4 in code (phase C)

`_open_pr(cwd, *, branch, run_id, state)`: `git push -u origin <branch>`, then
`gh pr create` with the title from the spec's first `# ` heading and the body from its
`## problem` section plus the acceptance-criteria test-name list and `Closes <issue>`. Title/body
come from `state.json:artifact_paths.spec` — the per-run path phase A already records
(`pipeline_prepare.py:221-229`), never a root-level `spec.md` (G-15). The issue id comes from
`pick_feature._issue_id_from_feature_source` (`pick_feature.py:320`) over
`state.json:feature_source`, so no new flag is needed for it. The PR number is written to
`state.json:pr_number` before gate 4 runs. Gate 4 checks both prompt conditions:
`gh pr view <n> --json state,url,headRefName` with `headRefName == branch`, and the issue file
committed as `status: done` on the branch (`git status --porcelain` empty for it *and*
`git show HEAD:<path>` matching `^status: done$`). No `pl-4-*` agent exists at any point —
criterion 6 is "the run reaches gate 4 green and no agent was ever started for stage 4".

### 8. Deadline, quota, cleanup, report

- **Deadline.** Checked *between* stages, after the in-flight stage settles — never mid-stage,
  matching `design.md:180` and `orchestrator-prompt.md:164`. Read from
  `state.json:deadline_epoch` (never recomputed: a fallback model once wrote one a year in the
  past and got a live run reaped 15 min in, 2026-09-28 — `tick.pipeline_deadline_epoch:1707`,
  `pipeline_watchdog.py:228-234`). On overrun: skip the rest, write
  `## Outcome: partial (deadline exceeded)`, `herdr notification show --sound request`.
  `tick._classify_pipeline_outcome` (`tick.py:1635`, mapping at 1659-1660) already maps
  `partial` → `failed` / `partial_deadline`.
- **Quota.** `PromptWatchdogKilled` from the shared wait loop → close the pane, write
  `## Outcome: failed (quota_exhausted)`. `tick._process_pipeline_job` (`tick.py:1826-1846`)
  already retries once with `fallback_model` on exactly that reason — criterion 5 only has to
  produce it.
- **Outcome vocabulary is `ok`, not `done`.** `PrepareResult.outcome` is
  `{"ok","no_feature","sync_failed","prepare_failed"}` and `runner.RunOutcome.state` is
  `{"done","failed","interrupted_unknown"}`; they are *not* the same shape (v1 claimed they
  were). The only thing tick reads is the report text, and
  `_classify_pipeline_outcome` maps `## Outcome: ok…` → `done` and anything it does not
  recognise → `interrupted_unknown`. A `RunOutcome` modelled on `runner.RunOutcome`'s `state`
  vocabulary that writes `## Outcome: done` files **every successful night as
  `interrupted_unknown`**. Pin `outcome: Literal["ok", "partial", "failed"]` and emit `ok`.
- **Exit codes.** `pipeline_prepare.EXIT_*` (`pipeline_prepare.py:52-54`) is the convention:
  `0` ok, `1` partial-or-failed, `2` argparse usage. v1's "0 done/partial, 1 failed" hides a
  bad night from `systemctl status`; the launcher instead branches on "did `$REPORT` end up
  non-empty", and must treat `pipeline-run`'s non-zero exit as *already reported, do not stub*.
- **Report writer.** `pipeline_prepare._write_terminal_report` (`pipeline_prepare.py:347`)
  promoted to shared `write_terminal_report(...)`, but two fixes are required to share it: its
  title is hardcoded `# Pipeline run <id> — pipeline-prepare report` (line 354), which would
  mislabel every pipeline-run report, and it uses non-atomic `write_text` (line 362) — the
  report is the file tick reconciles this run from, so a torn write reads as
  `outcome_marker_missing` / `interrupted_unknown`. Parameterise the title, write via the same
  tmp+rename as `write_state_json`.
- **Settle mapping** reuses `runner.SUCCESS_AGENT_STATUSES` (`runner.py:32`, `{idle, done}`);
  `blocked` → abort + report, `unknown` → `interrupted_unknown`-style abort.
- **Pane lifecycle.** Each worker's pane closes as soon as its gate passes
  (`orchestrator-prompt.md:108-111`, G-16), after the session id is captured and the state file
  written. Stage 6 reopens against a fresh pane with `-s <stage-3 session id>`. That needs
  `build_agent_start_args` (`herdr.py:687`) to take an optional `session_id`, **and** the same
  parameter threaded through `HerdrClient.agent_start` (`herdr.py:358`) and its
  `partial(...)` call in `_start_agent_reaping_stale_collision` (`runner.py:565`) — v1 named only
  the first. The flag must be appended *inside* the existing `model is not None` branch
  (`herdr.py:713-721`), because the documented resume form is `-m <model> -s <session_id>`
  (`orchestrator-prompt.md:156`) and that branch is the only one that emits the `--` separator.

### 9. Launcher and systemd

`scripts/pipeline-launch.sh` keeps its pre-flight block verbatim (`pipeline-prepare`, exit
0/3/1 handling) and replaces **lines 153-236 *and* 238-259** with one call. Line range matters:

| lines | what | fate |
| --- | --- | --- |
| 153-160 | orchestrator `workspace create`, sets `$WS_PANE` | **go** — nothing starts an agent |
| 162-177 | `agent start`, prompt assembly | **go** |
| 189-223 | the bash marker-poll loop (a third copy of §4) | **go** |
| 236 | `=== prompted …` echo | **go** |
| 238-259 | `herdr agent get "$AGENT_NAME"` settle check + visible-tail capture | **go** — reads an agent that no longer exists |
| 260-298 | "report still empty → `failed` stub + notify" backstop | **stays, and becomes unconditional** |

v1 said "replace 153-236" *and* "`$WS_PANE` and the `cleanup` trap stay" — self-contradictory,
since 153-160 is what sets `$WS_PANE`; with it gone the trap has nothing to close and is dead
code. And the backstop survives precisely because there is no longer an `agent get` settle
status to key it on, so it must stop being conditional on a bad settle.

The call, with the flags that are actually needed:

```
uv run herdr-routines pipeline-run --run-id "$RUN_ID" --state-json "$STATE_JSON" \
  --report "$REPORT" --prompts-dir "$REPO_PARENT/docs/pipeline/stages" \
  $(printf -- '--failure-marker %s ' "${FAILURE_MARKERS[@]}")
```

`--worktree`, `--branch`, `--shared-workspace`, `--feature-source`, `--issue-id` and
`--deadline-epoch` are dropped: `$SHARED_WS` and friends are not shell variables — the launcher
only `echo`s the `KEY=VALUE` blob (lines 151, 176) — and every one of them is already in
`state.json` (`shared_worktree`, `branch`, `shared_workspace`, `feature_source`,
`artifact_paths`, `deadline_epoch`). Reading the file phase A just wrote is also the safer
contract for `deadline_epoch` (see §8).

`--agent-name`/`--agent-kind`/`--model`/`--prompt-file`/`--wait-timeout-ms` stay on the
launcher's own flag surface (`tick._build_pipeline_launch_argv`, `tick.py:1716-1756`, still
passes them) but stop mattering to the script; `--agent-name` in particular must keep being
accepted, since tick's overlap guard is built on it — see risk 1.

**`tick.py` is touched**, for one reason only: see risk 1. Everything else in tick — the launch
argv, reconcile, outcome classification, the `fallback_model` retry — keeps working unchanged.

`deploy/systemd/*` needs **no** change: the launcher still runs in the same
`systemd-run --user --collect -p RuntimeMaxSec=…` unit, and
`herdr-routines-watchdog.{timer,service}` fires on its own `pipeline-watchdog` subcommand. One
consequence worth stating: because `pipeline-run` is now the process the unit supervises,
`RuntimeMaxSec` (`PIPELINE_UNIT_MARGIN_MS`-padded, `tick.py:1578`) becomes the *only* outer
bound, behind the in-process `deadline_epoch` check.

Also a benefit rather than a risk: `git`, `gh`, `jq`, `rg` calls that move into code stop passing
through opencode's per-command allowlist, shrinking the `deploy/opencode.pipeline.json` surface
that `design.md:140-152` documents as a per-host trap.

## Acceptance criteria

Items 1–6 are the issue 056 contract, verbatim test names. Items 7–11 are the additional pins
this review added after checking the code.

1. **`stage_sessions` is written by `pipeline-run` from the started agent's real
   `agent_session.value`; the map in `state.json` is never taken from model output.** The stage
   prompt no longer mentions `stage_sessions`, and the write happens after `agent_start`
   returns and before the prompt is sent. blocking — the whole point of phase B: a fabricated id
   is unrepresentable once the writer is not the model. confidence: high
   Test: `test_pipeline_run_records_real_stage_sessions`

2. **A failing `gate --stage N` aborts before stage N+1 and writes a partial report naming the
   failed gate and its reason.** blocking — a green history line on a run that stopped at gate 2
   is the failure mode `plan-v1.md` §6 warns about. confidence: high
   Test: `test_pipeline_run_aborts_on_gate_failure`

3. **The heartbeat file advances on every poll, written by code.** The write happens inside the
   `on_poll` closure of the shared wait loop, at `pipeline_watchdog.heartbeat_log_path(...)`, so
   it advances by construction on every poll of every stage. blocking — `is_stalled` kills live
   workers on this signal. confidence: high
   Test: `test_pipeline_run_writes_heartbeat`

4. **Past `deadline_epoch` the in-flight stage is allowed to finish, the remaining stages are
   skipped, and `## Outcome: partial (deadline exceeded)` is written.** blocking — an overrunning
   night with no report yields no record at all (`design.md:172-175`). confidence: high
   Test: `test_pipeline_run_partial_on_deadline`

5. **A quota marker on a worker's screen ends the run as `failed (quota_exhausted)`, so tick's
   existing `fallback_model` retry still applies.** blocking — without it the retry never fires
   and the night burns its whole deadline. confidence: high
   Test: `test_pipeline_run_quota_marker_fast_fails`

6. **Stage 4 opens the PR from the branch and passes gate 4 with no agent started.** blocking —
   the one purely mechanical stage needs no model at all. confidence: high
   Test: `test_pipeline_stage4_opens_pr_without_agent`

7. **A successful run writes `## Outcome: ok`, which
   `tick._classify_pipeline_outcome` maps to `done`.** blocking — a report the classifier does
   not recognise becomes `interrupted_unknown`, i.e. every green night files as a broken one.
   confidence: high (verified against `tick.py:1635-1667`)
   Test: `test_pipeline_run_writes_outcome_ok_that_tick_reads_as_done`

8. **A finished run's `state.json` passes `validate_stage_sessions` with the declared layout:**
   six stage entries, stage 4 absent, stage 6 repeating stage 3, five distinct real session ids.
   blocking — unchanged, this downgrades the run to `failed (stage_independence_unverified)`
   (`tick.py:1790`) while every other test is green. confidence: high
   Test: `test_pipeline_run_stage_sessions_satisfy_declared_layout`

9. **Gate 5 matches its tier labels on the literal bracketed form, so a body containing only
   `[non-blocking]` does not satisfy it.** blocking — verbatim-porting
   `orchestrator-prompt.md:153`'s `test("blocking")` re-imports issue 035's substring
   false-positive. confidence: high (verified against `gates.py:58-60`)
   Test: `test_gate5_rejects_nonblocking_only_review`

10. **Tick's pipeline overlap guard still fires when a run is in flight, without an orchestrator
    agent.** blocking — `_live_agent_exists` looks up `job.agent_name`, the orchestrator's
    `rt-<name>` agent, which no longer exists, so a second night can launch on top of a live
    one (see risk 1). confidence: high
    Test: `test_tick_pipeline_overlap_guard_survives_without_orchestrator_agent`

11. **The launcher runs `pipeline-run` and starts no agent; `docs/pipeline/stages/` supplies
    every prompt file `STAGES` names.** blocking — a missing prompt file aborts at stage 1 after
    the whole night has been prepared. confidence: medium (test seam is the existing fake-`uv`
    harness in `tests/test_pipeline_launch_sh.py`)
    Test: `test_launcher_runs_pipeline_run_and_stages_dir_is_complete`

## files touched

**Added**

- `src/herdr_routines/pipeline_stages.py` — `StageSpec`, `STAGES`, `expected_session_layout()`.
  Stdlib-only leaf, imported by `pipeline_run` *and* `pipeline_watchdog` (see §1 for why it
  cannot live in either).
- `src/herdr_routines/pipeline_run.py` — `run_pipeline(...)`, `RunOutcome`
  (`outcome: Literal["ok","partial","failed"]`, **not** `runner.RunOutcome`'s shape), `_run_stage`,
  `_open_pr` (stage 4), `_write_run_report`.
- `src/herdr_routines/wait_loop.py` — `prompt_with_watchdog(...)` + `matched_failure_marker` +
  the error classifiers, moved out of `runner.py` (behaviour-preserving for `runner.py`; the
  heartbeat is written inside its existing `on_poll` closure).
- `docs/pipeline/stages/stage-1.md`, `stage-2.md`, `stage-3.md`, `stage-5.md`, `stage-6.md` — the
  stage prompts, cut verbatim from `orchestrator-prompt.md`.
- `tests/test_pipeline_run.py`.

**Modified**

- `src/herdr_routines/cli.py` — new `pipeline-run` subparser + `_cmd_pipeline_run` (mirroring
  `pipeline_prepare`'s exit-code contract: `0` ok, `1` partial/failed, `2` argparse usage);
  `gate --stage` choices extended to `["1","2","4","5","6","ci"]`; `--pr` conditionally required;
  `_cmd_gate` resolves owner/repo only for the PR-dependent stages and dispatches to
  `run_stage_gate`.
- `src/herdr_routines/gates.py` — `run_stage_gate` dispatcher plus `gate1`/`gate2`/
  `gate3_test_names`/`gate4`/`gate5`; `gate5` anchored on `BLOCKING_TAG`; gate 4 gets a
  `git`/`gh` seam via `run_checks`' `runner` injection. Module docstring updated to say gate 3's
  *lint* half is still not a CLI stage.
- `src/herdr_routines/auto_fix.py` — `GhClient.pr_create` + `RealGhClient.pr_create`.
- `src/herdr_routines/runner.py` — import `wait_loop` instead of defining the helpers locally.
- `src/herdr_routines/herdr.py` — `build_agent_start_args(session_id=…)` and
  `agent_start(session_id=…)` for stage 6's `-s` resume, appended inside the
  `model is not None` branch.
- `src/herdr_routines/pipeline_watchdog.py` — `validate_stage_sessions` gains the defaulted
  `expected` layout; its own call site is unchanged.
- `src/herdr_routines/pipeline_prepare.py` — `_write_state_json` → public `write_state_json`;
  `_write_terminal_report` → public `write_terminal_report(path, *, title, run_id, outcome, lines)`
  with an atomic write; the `"stage_sessions": {}` comment updated to name phase B's writer.
- `src/herdr_routines/tick.py` — **only** the overlap guard in `_process_pipeline_job` (risk 1);
  the launch argv, reconcile, outcome classification and `fallback_model` retry are untouched,
  and `validate_stage_sessions`' new parameter defaults away any change here.
- `scripts/pipeline-launch.sh` — lines 153-236 and 238-259 replaced by the `pipeline-run` call;
  the unconditional empty-report backstop stays.
- `tests/test_pipeline_launch_sh.py`, `tests/test_cli.py`, `tests/test_gates.py`,
  `tests/test_runner.py`, `tests/test_tick.py` — updated for the above.
- `docs/pipeline/orchestrator-prompt.md` — trimmed to the loop description, with a pointer to
  `docs/pipeline/stages/`; the `stage_sessions` recording instruction and the prose gates deleted.

**Not touched:** `deploy/systemd/*.service|timer` (no unit change needed),
`deploy/opencode.pipeline.json` (the allowlist only shrinks), `state.json`'s key set (same keys,
better writer).

## risks

1. **Removing the orchestrator agent silently disables tick's pipeline overlap guard. blocking.
   confidence: high.** `_process_pipeline_job` calls `_live_agent_exists(client, job)` before
   launching (`tick.py:1768`), which resolves `client.agent_statuses().get(job.agent_name)`
   (`tick.py:2138-2151`) — the orchestrator's `rt-<name>` agent, the very thing
   `pipeline-launch.sh:31-34` says the guard depends on ("or tick's `_live_agent_exists` overlap
   guard silently never fires and a second run can launch on top of this one"). No orchestrator,
   no guard: two nights can stack. Mitigation is a one-line predicate swap, not new machinery —
   gate on `pipeline_watchdog.find_inflight_runs(default_worktrees_root(), reports_dir)` being
   non-empty (`pipeline_watchdog.py:297`), which is literally "a `state.json` with no terminal
   report yet" and is the condition the guard is approximating today. Criterion 10.
2. **The existing G-17 gate will false-fail this design if §2's default parameter is dropped.**
   blocking. confidence: high. Mitigation is in the design (`isolation` → one generic layout
   check), but it must land *with* the stage loop, not after it — a run that is green by every
   test and still records `failed` in history is exactly the "silently and plausibly" failure
   mode `plan-v1.md:498` warns about.
3. **Gate code is newly authoritative and newly untested against reality.** blocking.
   confidence: medium. Gates 1–5 have only ever run as prose an LLM interpreted and relaxed
   (gate 5 was explicitly relaxed after `pipeline-20260823T234906Z` because the skill never
   emits a literal `confidence:` token — `design.md:241`). Ported as code, that relaxation must
   be *made* explicitly, or every run aborts at gate 5. Each ported gate needs a fixture that
   mirrors one real run's output, and the port must be reviewed against the real dogfood
   artifacts rather than against the prompt text. Gate 5's substring trap (§6) is the specific
   case that already bit once.
4. **Code now pushes to a remote unattended.** blocking. confidence: high. Stage 4 issues
   `git push -u` and `gh pr create` with no model in the loop. Blast radius is unchanged in kind
   — stage 4's agent already did exactly this — but it is no longer gated on a model noticing
   the branch name, and an auth failure surfaces as a gate-4 failure hours later rather than a
   permission prompt at 03:00. `setup.md`'s signing-key and `GH_TOKEN` steps become hard
   requirements, not agent-visible advice; `docs/pipeline/setup.md` should gain a "phase C"
   note. Keep the dogfood feature trivial (`design.md:327`, build order step 3 at
   `design.md:425`).
5. **Resume semantics change.** non-blocking. confidence: high. `orchestrator-prompt.md:166`'s
   resume recipe reconciles live `pl-*` agents against `state.json` to adopt or reap orphans.
   With code, a relaunch re-reads `current_stage` and restarts that stage; a leftover live
   `pl-N-<run_id>` then collides on the name. Mitigation is concrete — reuse
   `runner._start_agent_reaping_stale_collision` (`runner.py:548`) to force-close the stale pane
   and retry the start once. Genuine mid-stage adoption is **not** preserved and is out of
   scope; the watchdog's reap path is the fallback, and that behaviour regression belongs in the
   PR body.
6. **The prompt is the only record of what stages used to do.** non-blocking. confidence:
   medium. Moving "Stage Details" into files splits the workflow across
   `orchestrator-prompt.md` and `docs/pipeline/stages/`, and `design.md`/`spec.md` describe
   gates that now live in `gates.py`. Nothing enforces that triple stays in sync — the same
   class of drift that produced gate 5's regex mismatch. Mitigation is a `test_pipeline_run.py`
   assertion that `STAGES` covers stages 1–6 with the layout `validate_stage_sessions` expects
   and that every `prompt_file` it names exists (criterion 11), so the two cannot disagree
   structurally.
7. **Prompt-echo false positives move from the model to a fixed string.** non-blocking.
   confidence: medium. `_matched_failure_marker` (`runner.py:253-264`) skips markers that appear
verbatim in the prompt it sent. Stage prompts are now files, and stage 1's prompt text
    contains the literal words `status: done` and the test-name line prefix; a
    `--failure-marker` string that appears in a prompt can never fire. The guard is the same
    code the runner already uses, so
   the behaviour is unchanged — but the *markers* now have to be checked against five fixed
   prompt files rather than one orchestrator prompt, and `Free usage exceeded` is the only one
   that is safe by inspection.

## Changelog v1→v2

v1 was a competent design with several claims that do not survive contact with the code on this
branch. This section records what changed and why. Every line reference below was checked
against `src/herdr_routines/`, `scripts/pipeline-launch.sh` and `docs/pipeline/` at
`ced6e40`.

**Additions**

- `## Acceptance criteria` — the issue 056 contract as numbered items 1–6 with their verbatim
  test names, plus five pins this review added (items 7–11) covering the `## Outcome: ok`
  vocabulary, the `validate_stage_sessions` layout, gate 5's substring trap, tick's overlap
  guard, and the launcher/prompt-file completeness check. Each item is labelled
  blocking/non-blocking with a `confidence:` rating.
- `src/herdr_routines/pipeline_stages.py` as a third added module, and risk 1 (the overlap
  guard) as a new blocking risk.

**Corrections to v1's claims about the code**

1. **`wait_loop.py`'s `on_poll` hook was invented.** v1 proposed
   `wait_for_stage(..., on_poll: Callable[[], None])`. `HerdrClient.agent_prompt_wait_with_watchdog`
   (`herdr.py:382`) already exists and already takes `on_poll: Callable[[str], str | None]` —
   screen text in, confirmed marker out — and raises `PromptWatchdogKilled` (`herdr.py:94`).
   The heartbeat is now written *inside* that existing closure; no new parameter, and the
   extraction is now `prompt_with_watchdog(...)` rather than a second loop primitive.
2. **`STAGES` in `pipeline_run.py` was a circular import.** `pipeline_run` needs
   `pipeline_watchdog.heartbeat_log_path`; `pipeline_watchdog.validate_stage_sessions` needs the
   layout table. Hence the stdlib-only `pipeline_stages.py`.
3. **v1 said `tick.py` must be modified to pass the stage layout in. It must not.** A defaulted
   `expected: Sequence[StageSpec] = STAGES` leaves both existing call sites
   (`tick.py:1621`, `pipeline_watchdog.py:293`) untouched. `tick.py` is back in files-touched,
   but for risk 1 alone.
4. **Gate 3 must not become a CLI stage.** v1 extended `--stage` to
   `["1","2","3","4","5","6","ci"]` and gave `gates.py` a `gate3` implementation. Both
   `orchestrator-prompt.md:139` and `gates.py:12-17` document gate 3 as prose-by-design, with
   `GATE3_LINT_TEST_CHECKS` explicitly "not exposed as a CLI stage" and Gate CI authoritative.
   Corrected to `["1","2","4","5","6","ci"]` plus `gate3_test_names` (existence only).
5. **Extending `--stage` needs two edits v1 missed:** `--pr` is `required=True` (`cli.py:320`)
   and `_cmd_gate` resolves owner/repo unconditionally before dispatch (`cli.py:968-970`).
   Gates 1/2/4 need neither a PR number nor a GitHub repo.
6. **Gate 5 must not be "ported verbatim from the prompt".** `orchestrator-prompt.md:153`'s
   `test("blocking")` matches `non-blocking` by substring — issue 035, the bug
   `gates.BLOCKING_TAG = "[blocking]"` (`gates.py:58-60`) exists to kill. Ported anchored.
7. **`RunOutcome` is not "the same shape as `PrepareResult` and `runner.RunOutcome`."**
   `PrepareResult.outcome` is `{"ok",…}`, `runner.RunOutcome.state` is `{"done",…}`, and the
   field names differ. Consequence with teeth: `_classify_pipeline_outcome` (`tick.py:1635`)
   maps `## Outcome: ok…` → `done` and anything unrecognised → `interrupted_unknown`, so a
   `done`-flavoured vocabulary files every successful night as broken. Pinned to `ok`.
8. **Sharing `_write_terminal_report` needs two fixes v1 skipped:** its title is hardcoded
   `# Pipeline run <id> — pipeline-prepare report` (`pipeline_prepare.py:354`) and it is a
   non-atomic `write_text` (line 362). Parameterised title + tmp+rename.
9. **The launcher's replaced range was wrong and v1's `$WS_PANE` claim self-contradictory.**
   v1: "replaces lines 153-236" plus "`$WS_PANE` and the `cleanup` trap stay" — but 153-160 is
   what creates the workspace and sets `$WS_PANE`. Correct scope is 153-236 **and 238-259** (the
   `agent get` settle check and tail capture read an agent that will no longer exist); 260-298
   stays and becomes unconditional, since it can no longer be keyed on a settle status.
10. **`$SHARED_WS`, `$BRANCH` etc. are not shell variables**, and most of v1's proposed flags are
    redundant with `state.json`. The `pipeline-run` CLI is now `--run-id`, `--state-json`,
    `--report`, `--prompts-dir`, `--failure-marker`; the issue id comes from
    `pick_feature._issue_id_from_feature_source` (`pick_feature.py:320`), and `deadline_epoch` is
    read from the file phase A wrote (re-deriving it from a flag is the 2026-09-28 bug class,
    `tick.py:1707`).
11. **Exit codes corrected** from v1's "0 done/partial, 1 failed" to `0` ok / `1`
    partial-or-failed / `2` usage, matching `pipeline_prepare.EXIT_*`.
12. **`build_agent_start_args` alone is not enough** — `HerdrClient.agent_start` (`herdr.py:358`)
    and the `partial(...)` in `_start_agent_reaping_stale_collision` (`runner.py:565`) need the
    parameter too, and `-s` must be appended inside the `model is not None` branch
    (`herdr.py:713-721`) since that branch alone emits the `--` separator.
13. **`gh pr create` has no client method** — the `GhClient` protocol (`auto_fix.py:32`) has
    only `api_user`/`pr_view`/`graphql`. v1 only mentioned gate 4's seam.
14. **New blocking risk:** removing the orchestrator agent silently disables tick's
    `_live_agent_exists` overlap guard (`tick.py:1768` → `2138-2151`), which is keyed on the
    orchestrator's `rt-<name>` agent. This is precisely the dependency
    `pipeline-launch.sh:31-34` documents. Mitigation: `pipeline_watchdog.find_inflight_runs(...)`.
15. **New non-blocking risk 7:** prompt-echo false positives are now evaluated against five
    fixed prompt files.

**Citation corrections**

- `runner._prompt_with_watchdog` is `runner.py:164-224`, not 164-196 (v1 undercounted by the
  retry loop).
- "Keep the dogfood feature trivial" is `design.md:327` (blast radius) and `design.md:425`
  (build order step 3) — not `design.md:257`, which is mid-G-17 prose.
- `tick._classify_pipeline_outcome` is `tick.py:1635`; v1's `tick.py:1659` cited the `partial`
  mapping line specifically and is fine as-is.
- `validate_stage_sessions` was cited at `pipeline_watchdog.py:115` and `is_stalled` at `361`;
  both correct, as were the `orchestrator-prompt.md` line refs (59, 125, 132, 139, 144-146, 153,
  159, 164, 108-111), the stage models/timeouts (`:115`, `:122,129,136,150,156`) and
  `pipeline_prepare.py:221-229,328`.