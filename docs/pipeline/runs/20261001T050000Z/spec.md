# spec v1 — orchestrator stage loop and PR stage in code (issue 056, phases B and C)

Source issue: [`docs/process/issues/056-orchestrator-stage-loop-in-code.md`](../../process/issues/056-orchestrator-stage-loop-in-code.md)
(issue 054 phases B and C). Built on phase A, already shipped in `src/herdr_routines/pipeline_prepare.py`.

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
   downgrading a `done` report to `failed` in `tick.py:1788`.
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

### 1. The workflow becomes data (`src/herdr_routines/pipeline_run.py`)

A module-level `STAGES: tuple[StageSpec, ...]`, one entry per stage, built from the tables in
`orchestrator-prompt.md:121-160` with the models and timeouts it already pins (stage 1/2
`muse-spark-1.2-contributor-free` 60m, stage 3 `x-preview-f-free` 90m, stage 5 `big-pickle` 60m,
stage 6 reuses stage 3's session, all `start_timeout_ms=120000`):

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

`isolation` is the concrete instantiation `design.md:262-267` asked for: a declarative
per-stage field with one generic gate function reading it, instead of bespoke Gate 1i/2i pairs
re-derived per hardcoded stage. `pipeline_watchdog.validate_stage_sessions` and
`tick._pipeline_stage_independence_issue` both already exist as the generic function; they gain
one parameter (the expected session layout) instead of a new check per stage. This is required,
not cosmetic — see risks.

### 2. Stage prompts move to files, verbatim

`docs/pipeline/stages/stage-{1,2,3,5,6}.md`, created by cutting the `**Prompt:**` lines out of
`orchestrator-prompt.md` unchanged. `pipeline-run` substitutes `$RUN_ID`, `$WT`, `$BRANCH`,
`$FEATURE_IDEA`, `$ISSUE_ID`, `$FEATURE_SOURCE`, `$SPEC_PATH`, `$PR_NUMBER` from `state.json` +
the launcher's resolved values — the same values `pipeline_prepare.render_prepared_values`
already emits as `KEY=VALUE`. No prompt content is rewritten, so stage 3's "commit the
`docs/process/issues/<file>` `status: done` flip" instruction survives into the code-run PR.

### 3. One shared wait loop, with the heartbeat in it

`runner._prompt_with_watchdog` + `runner._matched_failure_marker`
(`runner.py:164-196,253-264`) are the semantics issue 056 names: start-race retry on
provably-early `EmptyResponse` only, and the *same marker on two consecutive polls* stability
gate for quota wedges. `pipeline-launch.sh:189-223` re-implements the same loop in bash, a third
copy in the making.

Extract both into `src/herdr_routines/wait_loop.py` as `wait_for_stage(...)` with an extra
`on_poll: Callable[[], None]` hook; `runner.py` imports them back with no behaviour change.
`pipeline-run` passes `on_poll` = write one heartbeat line
(`/tmp/pipeline_resume_<run_id>.log`, the exact path
`pipeline_watchdog.heartbeat_log_path` reads) — so the heartbeat advances on every poll of every
stage **by construction**, not by an instruction an LLM may skip. Criterion 3 is a direct
assertion on that callback firing.

### 4. `stage_sessions` and `current_stage` written by code, atomically

After `HerdrClient.agent_start`, `pipeline-run` reads the real id itself via
`client.agent_session_id(name)` (already implemented, `herdr.py:440`, reading
`result.agent.agent_session.value`) and writes `state.json` with
`pipeline_prepare._write_state_json` (atomic tmp+rename, `pipeline_prepare.py:328`), promoting
that function to a shared public `write_state_json`. Every model-supplied path to that map is
gone: the prompt no longer mentions `stage_sessions`, and `pipeline-run` writes it after the
agent exists, before the prompt is sent. Stage 6's resumed agent records stage 3's id under
both `3` and `6` — honest, and what the declared reuse allowance consumes. Stage 4 records
nothing, because it starts no agent. `current_stage` advances after each stage's gate passes.

### 5. Gates in-process

Add `run_stage_gate(stage, *, cwd, run_id, state, gh, owner, repo)` to `gates.py`, dispatching to
one function per gate, and extend `cli.py`'s `gate --stage` choices from `["ci","6"]` to
`["1","2","3","4","5","6","ci"]` so the same verdicts are reachable by exit code from a shell as
today (`orchestrator-prompt.md:147,159` already relies on that). Ported verbatim from the prompt:
gate 1 (`test -s` spec, `>2` lines, branch `^auto/pipeline-`, spec committed),
gate 2 (`## Acceptance criteria` + `## Changelog` + `-w blocking`/`non-blocking` + `confidence:`),
gate 3 (each `Test: <name>` present under `tests/` via fixed-string `rg`, then
`GATE3_LINT_TEST_CHECKS` via the existing `run_checks`), gate 4 (below), gate 5 (tier structure
on `gh pr view --json comments,reviews`).

`pipeline-run` calls `run_stage_gate` directly. A failing gate aborts before stage N+1 and
writes the partial report naming the gate and its reason — criterion 2. Gate 1i/2i stop being
live subprocess checks (`herdr agent list | jq …`) and become the in-code session-layout check,
which is strictly stronger: it compares against ids this same process recorded, so a fabricated
id is unrepresentable rather than merely detectable.

### 6. Stage 4 in code (phase C)

`_open_pr(cwd, *, branch, run_id, state)`: `git push -u origin <branch>`, then `gh pr create`
with the title from the spec's first `# ` heading and the body from its `## problem` section plus
the acceptance-criteria `Test:` list and `Closes <issue>`. Title/body come from
`state.json:artifact_paths.spec` — the per-run path phase A already records
(`pipeline_prepare.py:221-229`), never a root-level `spec.md` (G-15). The PR number is written
to `state.json:pr_number` before gate 4 runs. Gate 4 (`gates.py`) checks both prompt conditions:
`gh pr view <n> --json state,url,headRefName` with `headRefName == branch`, and the issue file
committed as `status: done` on the branch (`git status --porcelain` empty for it *and*
`git show HEAD:<path>` matching `^status: done$`). No `pl-4-*` agent exists at any point —
criterion 6 is "the run reaches gate 4 green and `herdr agent list` was never asked for a stage 4".

### 7. Deadline, quota, cleanup

- **Deadline.** Checked *between* stages, after the in-flight stage settles — never mid-stage,
  matching `design.md:180` and `orchestrator-prompt.md:164`. On overrun: skip the rest, write
  `## Outcome: partial (deadline exceeded)`, `herdr notification show --sound request`, exit 0.
  `tick._classify_pipeline_outcome` (`tick.py:1659`) already maps `partial` → `failed` /
  `partial_deadline`, so no tick change is needed.
- **Quota.** `PromptWatchdogKilled` from the shared wait loop → close the pane, write
  `## Outcome: failed (quota_exhausted)`. `tick._process_pipeline_job:1830` already retries once
  with `fallback_model` on exactly that reason — criterion 5 only has to produce it.
- **Settle mapping** reuses `runner.SUCCESS_AGENT_STATUSES` (`{idle, done}`); `blocked` → abort +
  report, `unknown` → `interrupted_unknown`-style abort.
- **Pane lifecycle.** Each worker's pane is closed as soon as its gate passes
  (`orchestrator-prompt.md:108-111`, G-16), after the session id is captured and the state file
  written. Stage 6 reopens against a fresh pane with `-s <stage-3 session id>`; that requires
  extending `build_agent_start_args` (`herdr.py:687`) with an optional `session_id` appended after
  the model flag — one added parameter, shared with `runner.build_dry_run_argv`.
- **Report.** One writer, reusing `pipeline_prepare._write_terminal_report`'s shape: title, then
  `## Outcome:` on line 3, then stage-by-stage status/gate reasons/artifacts/PR number.

### 8. Launcher and systemd

`scripts/pipeline-launch.sh` keeps its pre-flight block verbatim (`pipeline-prepare`, exit 0/3/1
handling) and replaces lines 153-236 — workspace-for-orchestrator, `agent start`, prompt assembly,
marker-poll loop, `agent get` settle check — with one call:

```
uv run herdr-routines pipeline-run --run-id "$RUN_ID" --state-json "$STATE_JSON" \
  --report "$REPORT" --deadline-epoch "$DEADLINE_EPOCH" --worktree "$WT" \
  --branch "$BRANCH" --shared-workspace "$SHARED_WS" --feature-source "$FEATURE_SOURCE" \
  --issue-id "$ISSUE_ID" --base main $(printf -- '--failure-marker %s ' "${FAILURE_MARKERS[@]}")
```

`$WS_PANE` and the `cleanup` trap stay: the trap now also fires when `RuntimeMaxSec`
(`tick.PIPELINE_UNIT_MARGIN_MS`-padded) SIGTERMs the unit, and the launcher's post-call guard
("if `$REPORT` is still empty, write the `failed` stub + notify") remains the backstop for a kill
no code path could report from. `deploy/systemd/*` needs **no** change: the launcher still runs in
the same `systemd-run --user --collect -p RuntimeMaxSec=…` unit built by
`tick._build_pipeline_launch_argv`, and `herdr-routines-watchdog.{timer,service}` fires on its own
`pipeline-watchdog` subcommand. One consequence is worth stating: because `pipeline-run` is now
the process the unit supervises, `RuntimeMaxSec` becomes the *only* thing bounding it, which is
already `deadline_ms + 10 min` and stays the outer bound behind the in-process `deadline_epoch`.

Also worth noting as a benefit rather than a risk: `git`, `gh`, `jq`, and `rg` calls that move into
code stop passing through opencode's per-command allowlist at all, shrinking the
`deploy/opencode.pipeline.json` surface that `design.md:140-152` documents as a per-host trap.

### 9. Tests (the issue's six, by name)

`tests/test_pipeline_run.py`, faked `HerdrClient` + real tmp/git fixtures, the shape
`tests/test_pipeline_prepare.py` already uses:

- `test_pipeline_run_records_real_stage_sessions` — a fake client whose
  `agent_session_id` returns distinct `ses_…` values per agent; after the run, every entry in
  `state.json:stage_sessions` equals one `pipeline-run` observed and the file passes
  `validate_stage_sessions`. A fake that instead reports one shared id fails the run's own
  isolation gate, proving the map is not self-asserted.
- `test_pipeline_run_aborts_on_gate_failure` — gate 2 forced to fail: stage 3's agent is never
  started, and the report reads `## Outcome: failed` and names gate 2's reason.
- `test_pipeline_run_writes_heartbeat` — one `pl-1-<run_id>` heartbeat line per poll, asserted
  against `pipeline_watchdog.heartbeat_log_path`, with `_heartbeat_is_stale` false at poll time.
- `test_pipeline_run_partial_on_deadline` — clock past `deadline_epoch` at the stage 1→2 boundary:
  stage 1 runs to completion, stage 2 is never started, report says
  `partial (deadline exceeded)`.
- `test_pipeline_run_quota_marker_fast_fails` — two consecutive polls showing
  `Free usage exceeded`; assert the run ends `failed (quota_exhausted)` without sleeping out
  `timeout_ms`.
- `test_pipeline_stage4_opens_pr_without_agent` — assert `agent_start` was never called for stage 4,
  `gh pr create` ran once, and gate 4 passes on a fixture branch whose issue file is committed
  `status: done`.

Plus a `tests/test_pipeline_launch_sh.py` update (fake `uv` stub asserting the launcher runs
`pipeline-run` and no longer starts an orchestrator agent) and a `test_cli.py` case for the
extended `gate --stage` choices.

## files touched

**Added**

- `src/herdr_routines/pipeline_run.py` — `StageSpec`/`STAGES`, `run_pipeline(...)`,
  `RunOutcome` (same shape as `pipeline_prepare.PrepareResult` and `runner.RunOutcome`),
  `_run_stage`, `_open_pr` (stage 4), the state writer and report writer.
- `src/herdr_routines/wait_loop.py` — `wait_for_stage(...)` + `matched_failure_marker`, moved out
  of `runner.py` (behaviour-preserving for `runner.py`; adds the `on_poll` heartbeat hook).
- `docs/pipeline/stages/stage-1.md`, `stage-2.md`, `stage-3.md`, `stage-5.md`, `stage-6.md` — the
  stage prompts, cut verbatim from `orchestrator-prompt.md`.
- `tests/test_pipeline_run.py`.

**Modified**

- `src/herdr_routines/cli.py` — new `pipeline-run` subparser + `_cmd_pipeline_run` (mirroring
  `pipeline-prepare`'s exit-code contract: `0` done/partial, `1` failed, `2` argparse usage);
  `gate --stage` choices extended to `1..6` + `ci`; `_cmd_gate` dispatches to `run_stage_gate`.
- `src/herdr_routines/gates.py` — `run_stage_gate` dispatcher plus `gate1`/`gate2`/`gate3`/`gate4`/
  `gate5` implementations; `gate4` needs a `git`/`gh` seam (reuse `run_checks`' runner
  injection pattern).
- `src/herdr_routines/runner.py` — import `wait_loop` instead of defining the helpers locally.
- `src/herdr_routines/herdr.py` — `build_agent_start_args(session_id=…)` for stage 6's `-s` resume.
- `src/herdr_routines/pipeline_watchdog.py` — `validate_stage_sessions` gains the expected-layout
  parameter (allow stage 6 to repeat stage 3; require no entry for a `model=None` stage).
- `src/herdr_routines/pipeline_prepare.py` — `_write_state_json` → public `write_state_json`,
  `_write_terminal_report` → public `write_terminal_report` (shared with `pipeline_run`); the
  `"stage_sessions": {}` comment updated to note phase B's writer.
- `src/herdr_routines/tick.py` — pass the expected stage layout into
  `_pipeline_stage_independence_issue`; otherwise untouched (launch argv, reconcile, and the
  `fallback_model` retry all keep working unchanged).
- `scripts/pipeline-launch.sh` — replace the orchestrator-agent block with the `pipeline-run` call.
- `tests/test_pipeline_launch_sh.py`, `tests/test_cli.py`, `tests/test_runner.py` — updated for the
  above.
- `docs/pipeline/orchestrator-prompt.md` — trimmed to the loop description, with a pointer to
  `docs/pipeline/stages/`; the `stage_sessions` recording instruction and the prose gates deleted.

**Not touched:** `deploy/systemd/*.service|timer` (no unit change needed),
`deploy/opencode.pipeline.json` (the allowlist only shrinks), `src/herdr_routines/tick.py`'s launch
and reconcile contracts, `state.json`'s key set (same keys, better writer).

## risks

1. **The existing G-17 gate will false-fail this design as written.**
   `pipeline_watchdog.validate_stage_sessions` (`pipeline_watchdog.py:115`) requires every entry to
   be distinct and `len(values) >= min(current_stage, 6)`. Phase B+C deliberately break both:
   stage 6 legitimately records stage 3's id (G-16 close-then-resume, verified 2026-08-25), and
   stage 4 records none while `current_stage` reaches 4. Left as-is, every phase-C run downgrades
   itself to `failed (stage_independence_unverified)` in `tick.py:1790`. Mitigation is in the
   design (the `isolation` field feeding one generic layout check), but it must land *with* the
   stage loop, not after it — a run that is green by every new test and still records
   `failed` in history is exactly the "silently and plausibly" failure mode `plan-v1.md` §6 warns
   about.
2. **Gate code is newly authoritative and newly untested against reality.** Gates 1–5 have only
   ever run as prose an LLM interpreted and relaxed (gate 5 was explicitly relaxed after
   `pipeline-20260823T234906Z` because the skill never emits a literal `confidence:` token). Ported
   as code, the same relaxation must be *made*, or every run aborts at gate 5. Each ported gate
   needs a fixture that mirrors one real run's output, and the port must be reviewed against the
   real dogfood artifacts rather than against the prompt text.
3. **Code now pushes to a remote unattended.** Stage 4 issues `git push -u` and `gh pr create`
   with no model in the loop. Blast radius is unchanged in kind — stage 4's agent already did
   exactly this — but it is no longer gated on an LLM noticing the branch name, and an auth
   failure surfaces as a gate-4 failure hours later rather than a permission prompt at 03:00.
   `setup.md`'s signing-key and `GH_TOKEN` steps become hard requirements, not agent-visible
   advice; `docs/pipeline/setup.md` should gain a "phase C" note. Keep the dogfood feature
   trivial (`design.md:257`).
4. **Resume semantics change.** `orchestrator-prompt.md`'s resume recipe reconciles live `pl-*`
   agents against `state.json` to adopt or reap orphans. With code, a relaunch re-reads
   `current_stage` and restarts that stage; a leftover live `pl-N-<run_id>` then collides on the
   name. Mitigation is concrete — reuse `runner._start_agent_reaping_stale_collision` to
   force-close the stale pane and retry the start once. Genuine mid-stage adoption (reusing a
   half-finished worker) is **not** preserved and is out of scope; the watchdog's reap path is the
   fallback, and that is a behaviour regression a human should be told about in the PR body.
5. **The prompt is the only record of what stages used to do.** Moving "Stage Details" into files
   splits the workflow across `orchestrator-prompt.md` and `docs/pipeline/stages/`, and
   `design.md`/`spec.md` describe gates that now live in `gates.py`. Nothing enforces that
   triple stays in sync — the same class of drift that produced gate 5's regex mismatch. Mitigation
   is a `test_pipeline_run.py` assertion that `STAGES` covers stages 1–6 with the isolation layout
   `validate_stage_sessions` expects, so the two can't disagree structurally.
6. **Waiting for the spec review** before implementing. Stage 2's acceptance criteria (including
   any test names it assigns to items 1–6 above) will be merged into this file as
   `## Acceptance criteria` + `## Changelog v1→v2`, per the pipeline's own stage-2 contract. If
   those tests disagree with the names pinned here, theirs win and this section becomes the
   record of what changed.
