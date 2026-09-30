# Spec — move the orchestrator's mechanical steps into code (issue 054, phase A) — 20260930T050000Z — v1

Per-run spec at `docs/pipeline/runs/20260930T050000Z/spec.md` (per-run path on purpose, not
`$WT/spec.md` — G-15; see `docs/pipeline/orchestrator-prompt.md:144`).

Implements `docs/process/issues/054-orchestrator-mechanical-steps-to-code.md` **phase A only**.

## Scope

**This PR implements phase A (pre-flight in code) and nothing else. Phases B and C are filed as
a follow-on issue and are out of scope here.** Read that as a recommendation, not a fait accompli —
here is the reasoning, and the orchestrator/human can overrule it.

The issue is 3 phases and 9 acceptance criteria, and the phases are not slices of one change; they
are three changes to three different owners:

| Phase | What changes | ACs | Size | Shares a PR with A? |
|---|---|---|---|---|
| **A — pre-flight in code** | new `pipeline-prepare` subcommand; launcher calls it before any agent starts; prompt's Prerequisite section shrinks | 1–3 | 1 new module + 4 source edits + 5 test files + 4 doc edits | — |
| **B — stage loop in code** | `pipeline-run` replaces the orchestrator LLM session entirely; 6 stage prompts move to files; session map, gate calls, heartbeat, deadline, pane lifecycle all move into code | 4–8 | new module + prompt-file extraction + the launcher rewrite | no |
| **C — stage 4 in code** | `git push` + `gh pr create` from the spec; no agent for stage 4 | 9 | small | **yes — with B** |

**Recommendation: A alone in this PR, then B+C in one follow-on PR (new issue 055).**

1. **A is independently shippable and independently valuable.** It is the only phase that changes
   nothing about how the pipeline *runs* — the orchestrator still orchestrates, the same prompt
   still drives the same 6 stages. It just stops the model from doing setup. It also subsumes issue
   052, which is the one with a real cost today (every empty backlog night burns a full
   orchestrator session). A can ship and be observed for a week with zero behavioural change to
   the stage loop.
2. **B is the risky change and deserves a PR of its own.** It deletes the orchestrator session —
   the component currently running *this* pipeline. Its blast radius is every stage, the watchdog's
   two signals, the reconcile contract, and the resume path. Bundling "setup in code" with "delete
   the orchestrator" means that when a run misbehaves, the diff cannot tell you which of the two
   did it. B's own risk register is also the reason to run it second: it is much easier to review
   and bisect against a Phase-A baseline that has already proven itself overnight.
3. **C folds into B, not into A.** C only makes sense once the stage loop is in code — its whole
   content is "this stage's worker is no worker". Splitting C out would mean shipping B with a
   known-pointless agent spawn in it, and a second PR whose diff is a deletion. So: 3 phases, 2
   PRs.
4. **One thing A must not do:** flip 054 to `done` without filing the follow-on. The pipeline's
   stage-3 contract is "flip the picked issue to `done`" (prompt:158); a Phase-A PR that obeys that
   literally would retire the issue and lose phases B and C from the backlog permanently. A's PR
   therefore files **055** and sets 054's `status: done` with a `gate:` note pointing at it —
   exactly the pattern 052 already uses (`docs/process/issues/052-…md:6`).

Phase A's own scope note, for completeness: counting issue 054's 11-row table, **4 rows are phase
A** (pick-feature, sync-repo, worktree create + `jq`, atomic `state.json`), **6 rows are phase B**
(the heartbeat line, spawn/prompt/poll/settle/quota/retry, `stage_sessions` + `current_stage`, gate
dispatch, pane close + save `pl-3`'s session, deadline check + partial report) and **1 row is phase
C** (stage 4's `git push` + `gh pr create`). **A therefore leaves 7 of the 11 steps on the model.**
That is deliberate and acceptable: A is a strict improvement that changes no run semantics, and it
is the only phase whose absence costs something every single night.

## Problem

The overnight pipeline's orchestrator is an LLM session driven by
`docs/pipeline/orchestrator-prompt.md`. Much of what that prompt asks has one correct answer and
no judgment in it, yet a model is trusted to do it by following instructions. The rest of the
system then trusts the output of those steps without checking them.

The structural statement, which the three incidents are instances of: **every signal the pipeline
consumes about itself is written by the thing being measured.** `state.json` (`deadline_epoch`,
`feature_source`, `current_stage`, `stage_sessions`), the heartbeat log
(`/tmp/pipeline_resume_<run_id>.log`), and the terminal report's `## Outcome:` line are all
model-authored, and the watchdog, `tick`'s reconcile, `ps`, and the `digest` all read them as
facts. When the model is weaker than the prompt assumes — the quota `fallback_model` — or cuts a
corner, the failure is silent and downstream:

- **2026-09-28:** the fallback model wrote `state.json:deadline_epoch` a year in the past and
  wrote no heartbeat. The watchdog trusted both and reaped a live run 15 min in. The deadline half
  was then fixed properly, on its own, in PR #131 (`tick.pipeline_deadline_epoch`,
  `tick.py:1692`; the watchdog now prefers the value tick recorded in history —
  `pipeline_watchdog.recorded_pipeline_deadlines`, `pipeline_watchdog.py:228`). The heartbeat half
  is still model-written, and phase B is what fixes it.
- **2026-09-07:** the orchestrator faked `stage_sessions` and ran all 6 stages in its own session.
  Code now *checks* for this (`pipeline_watchdog.validate_stage_sessions`, G-17), but the model
  still *writes* the map it is checked against — so G-17 is a detector, not a prevention.
- **Issue 050:** the orchestrator ran a bare `herdr-routines`, wandered off probing `~/.local/bin`,
  and wedged on a permission prompt. Pinned by `tests/test_orchestrator_prompt_invocation.py`,
  which is a doc-contract test on a markdown file — i.e. a lint, not an enforcement.

**Why phase A is the right first cut.** All three incidents are about *setup* being done by a
model, and setup is the part that needs no judgment whatsoever. But setup is also the part that
runs on **every** run, including the runs where there is nothing to do at all. Today an empty
backlog costs: a launched orchestrator agent, a synced clone, a worktree, a shared workspace, a
`state.json`, six doomed worker spawns (or one prompt failure), and a report — all to discover at
step 1 that `pick-feature` had nothing to return. That is issue 052, and 052 is the *only* one of
the three incidents with a per-night cost, so it is the one phase A closes outright.

## Approach

Phase A moves the pre-flight into a `herdr-routines` subcommand that runs **before any agent is
started**, and hands its resolved values to the prompt instead of asking the prompt to compute
them. The orchestrator session keeps its entire current responsibility (stages 1–6, gates, the
report); it just no longer performs setup. — confidence: high

### 1. New `pipeline-prepare` subcommand

`src/herdr_routines/pipeline_prepare.py` (new; module name matches the existing
`pipeline_watchdog.py` / `pick_feature.py` convention), wired into `cli.py`'s `_build_parser`
next to `sync-repo`/`pick-feature` (`cli.py:370`) and dispatched by a `_cmd_pipeline_prepare`
modelled on `_cmd_sync_repo` (`cli.py:1129`) — same posture: log, `return 1`, no config load
(`pipeline-prepare` has nothing to do with `jobs.yaml`; the launcher already holds every value it
needs as flags).

```
herdr-routines pipeline-prepare
    --run-id ID              # bare UTC timestamp, e.g. 20260930T050000Z
    --repo-parent PATH       # the parent clone to branch from
    --report PATH            # pinned terminal-report path (the launcher's --report)
    --deadline-epoch EPOCH   # REQUIRED. tick computes it, the launcher passes it verbatim
    [--base main]            # sync + worktree base branch
    [--issues-dir docs/process/issues]   # relative to --repo-parent
```

**`--deadline-epoch` is required with no default, and that is the whole point.** The launcher's
current default is `now + WAIT_TIMEOUT_MS/1000` computed in bash (`pipeline-launch.sh:85-87`) —
i.e. the shell, not the model, already owns the fallback. Making the flag required at the Python
boundary means a `pipeline-prepare` invocation can never invent a deadline, while the launcher's
own default keeps the pre-tick-flag path working. `[non-blocking]` Proposed extra guard: reject a
`deadline_epoch` that is not in `(now, now + 48h]` with
`## Outcome: failed (invalid_deadline)` rather than writing a value the watchdog will later
believe. Cheap, and it turns a caller bug into a loud failure at the cheapest possible moment. —
confidence: medium

`prepare_run(...)` returns a `PrepareResult` frozen dataclass (shape mirrors
`runner.RunOutcome`, the existing convention for "a run's terminal disposition"):

```python
@dataclass(frozen=True, slots=True)
class PrepareResult:
    outcome: Literal["ok", "no_feature", "sync_failed", "prepare_failed"]
    feature_idea: str | None
    feature_source: str | None      # "docs/process/issues/054-….md"
    issue_id: str | None            # for the report's "Closes issue NN" line
    worktree: str | None
    branch: str | None
    workspace_id: str | None
    state_path: Path | None
    reclaimed: tuple[str, ...]      # issue 040's reclaim notices, verbatim, for the report
```

### 2. Order: sync → pick → worktree/workspace → `state.json`, with fail-loud reports at each step

Matches the prompt's Prerequisite order exactly (prompt:49, :61, :70), so the failure semantics
are the ones the prompt already promises — just executed in Python instead of by a model.

1. **sync** — `_fetch_and_fast_forward(repo_parent, base=base)` (`repos.py:93`), the same primitive
   `sync-repo` (`cli.py:1129`) and `ensure_repo` use. Non-zero/exception →
   `## Outcome: failed (repo_sync_failed)` (AC 3) and **no agent**. Note this makes `$REPO_PARENT`
   synced twice on a normal run: once by `tick._launch_pipeline_run`'s `ensure_repo(job)` at
   dispatch (`tick.py:1991`) and once here. That is deliberate, not an oversight — the dispatch
   sync happens minutes before `systemd-run` registers the unit, the branch-off happens after, and
   the second is the one that guards the branch point. Behaviour on a non-fast-forward is
   unchanged (the prompt's `sync-repo` step already said stop and report).
2. **pick** — `pick-feature --mark-in-progress`. "no open issues" →
   `## Outcome: skipped (no_feature)` (AC 2), **no agent, no worktree, no workspace, no
   `state.json`**. The worktree is created *after* the pick for exactly this reason: an empty
   backlog must cost zero worktrees, not merely zero stages. This is issue 052's whole ask.
3. **worktree + shared workspace** — `herdr worktree create --cwd <repo_parent> --base <base>
   --branch auto/pipeline-<run_id>`, parse `.result.worktree.path` and `.result.branch` exactly as
   the prompt does (prompt:64-65); then locate-or-create the shared workspace with
   `--env HERDR_ENV=1` (prompt:66-67 — without it every `herdr` call settles `blocked`,
   design:98) and `--label pipeline-<run_id>`. This is the first step that needs a Herdr server,
   which is why it sits after the pick: **a no-feature night needs no Herdr server at all**, the
   same posture `pick_feature` already documents (`cli.py:1071-1075`).
4. **`state.json`** — written atomically to `$WT/state.json` via tmpfile + `os.replace` in the
   same directory (G-9), with the exact key set from the prompt's example (prompt:72-84) so that
   `pipeline_watchdog._parse_state_json`, `tick._pipeline_stage_independence_issue`, `ps.py`, and
   `validate_stage_sessions` all keep working with no changes:

   ```json
   {"run_id": …, "current_stage": 0, "pr_number": null, "shared_worktree": …,
    "branch": "auto/pipeline-<run_id>", "shared_workspace": …, "deadline_epoch": …,
    "feature_source": "docs/process/issues/<file>",
    "artifact_paths": {"spec": "$WT/docs/pipeline/runs/<run_id>/spec.md", "report": "<report>"},
    "stage_sessions": {}}
   ```

   `feature_source` is required by the prompt (prompt:20) but **missing from the prompt's own
   JSON example** (prompt:72-84) — a small latent bug in the prompt, fixed here by including it.
   `stage_sessions` stays empty: the orchestrator still fills it in phase A (phase B moves the
   writer). `current_stage: 0` with an empty session map is safe for the watchdog — its
   stage-independence downgrade is applied by `tick` only when the reconciled state is `done`
   (`tick.py:1771-1783`), and `is_stalled` requires *both* the deadline+grace to have passed
   *and* a stale heartbeat (`pipeline_watchdog.is_stalled`, `pipeline_watchdog.py:361-385`), so a
   state.json written seconds before the first heartbeat cannot trip the reaper.

### 3. `feature_source` needs a real return value, not parsed stdout

`run_pick_feature` (`pick_feature.py:430`) returns `int` and prints the feature idea to stdout.
`feature_source` and issue 040's reclaim notices are not in that return value. They *are*
recoverable from stdout — `render_feature_idea` embeds `issue.path.as_posix()` in the text
(`pick_feature.py:148-150`) — but scraping stdout to recover a path is precisely the practice this
issue exists to delete, and the reclaim lines go to **stderr**.

So: add `PickResult` (frozen dataclass: `issue: Issue`, `feature_idea: str`, `reclaimed: tuple[str, …]`)
and `select_and_claim_feature(...) -> PickResult | None` as the new core; make `run_pick_feature` a
thin printer over it (`if result is None: print("no open issues", file=sys.stderr); return 1`).
`run_pick_feature`'s signature, stdout, stderr and exit codes are **unchanged**, so
`tests/test_pick_feature.py` (603 lines) and `_cmd_pick_feature` (`cli.py:1071`) need no edits.

### 4. The terminal report: one format, written by whoever fails

The two non-`ok` paths write the report themselves, in the shape the launcher already uses for its
own stubs (`pipeline-launch.sh:233-247`): title, `## Outcome: …` immediately after it (the
"near the top, trivial to `grep -m1`" rule, prompt:203), then reason lines, including any
`reclaimed stale claim: …` lines verbatim (issue 040's "a reclaim must be visible, never silent").
`_write_terminal_report` should be a single helper used by both paths so the marker never drifts.

**Exit codes** (the launcher branches on these and nothing else): `0` = prepared, `3` = no feature
(AC 2's skip), `1` = sync or prepare failure (AC 3), `2` = argparse usage error. `3` is distinct
from `1` on purpose — the launcher must be able to tell "healthy night, nothing to build" from
"something broke", because the two want opposite exit statuses from the unit. `sync-repo` today
returns plain `1` on failure (`cli.py:1136`) and `pick-feature` returns `1` for "no open issues"
(`pick_feature.py:509`); inside `prepare_run` those two conditions are *distinguished* and only the
collapsed exit code is 3-or-1, so nothing changes for the existing commands' own contracts.

`## Outcome: skipped (no_feature)` is a **new value on the reconcile contract** and
`_classify_pipeline_outcome` (`tick.py:1632-1652`) has no branch for it today — it would fall
through to `return "interrupted_unknown", "outcome_marker_unrecognized"`. See §5.

### 5. The load-bearing tick change: `skipped` must classify as `skipped`

AC 2 says "tick records `skipped`, not `failed`". Getting there needs **two** edits in
`tick.py`, and the second is easy to miss:

1. `_classify_pipeline_outcome` (`tick.py:1632-1652`): add
   `if status.startswith("skipped"): return "skipped", None` **before** the `partial`/`failed`
   checks. `skipped` is already in `history.TERMINAL_STATES` (`history.py:25-27`), so
   `_open_pipeline_run` (`tick.py:1655`) closes the run correctly and the next scheduled
   occurrence makes a fresh decision — an empty backlog never wedges the job.
2. The reconcile path (`tick.py:1764-1846`): after the `done` branch returns, **everything else
   falls through to the failure notify** (`if _notify_gate(job, "failure"): _notify(…, sound="request")`,
   `tick.py:1835-1841`). A `skipped` run must return before that, exactly like `done` does, or
   every empty-backlog night sends a failure notification to Telegram at 05:00 — the single worst
   possible outcome for a feature whose whole point is that "nothing to do" is normal and cheap.

This is a change to the *reconcile contract*, so it gets its own test beyond the three named in
the issue (see "Acceptance criteria → tests" below). It is also why the follow-on work is not
optional: nothing today has ever written a `skipped` report, so this branch is untested in
production and must be.

### 6. Launcher wiring

In `scripts/pipeline-launch.sh`, call prepare **before** `herdr workspace create`
(`pipeline-launch.sh:126`) and before the `cleanup` trap has anything to clean:

```sh
PREPARE_OUT=$(uv run herdr-routines pipeline-prepare \
  --run-id "$RUN_ID" --repo-parent "$REPO_PARENT" --report "$REPORT" \
  --deadline-epoch "$DEADLINE_EPOCH" 2>&1) ; PREPARE_RC=$?
```

`uv run` is required — `herdr-routines` is not on `PATH` on the pipeline hosts (issue 050, pinned
by `tests/test_orchestrator_prompt_invocation.py:27-41`).

- `rc == 0` → `$PREPARE_OUT` is `KEY=VALUE` lines; append them to the prompt header next to
  `RUN_ID`/`REPO_PARENT`/`PIPELINE_REPORT`/`DEADLINE_EPOCH` (`pipeline-launch.sh:143-147`) and fall
  through to the existing agent-start path unchanged. Appending to the *header* rather than editing
  the prompt file keeps `--prompt-file` (a flag, default
  `docs/pipeline/orchestrator-prompt.md`) working for any custom prompt.
- `rc == 3` (no feature) or any other non-zero (sync/prepare failure) → **exit without
  `herdr agent start`**. The report is already written and is the terminal artifact tick reconciles
  on its next pass; the launcher adds nothing and stubs nothing. Exit 0 for the `no_feature` case
  (a skip is a successful skip, and a non-zero unit would page a human for a healthy night) and
  non-zero for real failures.

`--deadline-epoch` is already computed and defaulted at `pipeline-launch.sh:85-87`; it is simply
now also forwarded. `test_pipeline_launcher_hands_orchestrator_tick_computed_deadline`
(`tests/test_pipeline_launch_sh.py:318`) should be extended to assert the *same* value reaches
prepare, so the two writers of the deadline can never drift.

### 7. Prompt trim

`orchestrator-prompt.md`'s "Prerequisite (do once, before stage 1)" section (prompt:40-90) shrinks
to a short "this is already done, here are your values" note; the `sync-repo` block (prompt:55-59),
the worktree/workspace block (prompt:63-68) and the `state.json` template (prompt:72-84) are
replaced by a pointer to the header values. The `uv run` note (prompt:42-47) **stays** — issue
050's lesson is about *how* to invoke the CLI, it is not specific to the subcommands being removed,
and the orchestrator still invokes `gate` by hand. "Inputs you will receive" (prompt:11-34)
loses the whole `pick-feature` paragraph and gains `FEATURE_IDEA` + `FEATURE_SOURCE` as resolved
header values. The stage-1 spawn template, the stage details, the deadline/quota/resume/cleanup
section, the failure semantics and the final-report contract are **unchanged** in phase A.

`tests/test_orchestrator_prompt_invocation.py` needs a matching edit, and this one is worth
stating explicitly because it looks like a test-weakening: `HERDR_ROUTINES_SUBCOMMANDS`
(`tests/test_orchestrator_prompt_invocation.py:21`) is
`("sync-repo", "pick-feature", "gate")`, and each is asserted to appear as `uv run …`. After the
trim, `sync-repo` and `pick-feature` no longer appear in the prompt at all, so those two assertions
go vacuous and the tuple should be narrowed to `("gate",)`. The *intent* of the test — issue 050's
"never a bare `herdr-routines`" — is fully preserved, because `gate` is the one subcommand the
orchestrator still invokes by hand and is therefore the one that can still regress that way.
`test_prompt_notes_herdr_routines_not_a_binary` still passes (the note is kept).

### 8. Issue bookkeeping (do this or lose phases B and C)

- `docs/process/issues/052-…md`: `status: blocked` → `status: done`, `gate:` updated to record that
  phase A shipped. Its own frontmatter already predicts this (052 line 6).
- `docs/process/issues/055-orchestrator-stage-loop-in-code.md` (new): phases B and C, with ACs 4–9
  copied from 054 and the same design text, so they are picked up as their own run.
- `docs/process/issues/054-…md`: `status: open` → `status: done`, `gate: phased — phase A in this
  PR; phases B+C tracked by issue 055`, plus a log line.

## Acceptance criteria → tests (proposal; stage 2 turns this into the formal section)

From issue 054, phase A, unchanged in intent:

1. `test_pipeline_prepare_sets_up_run` — eligible issue → synced repo, claimed issue, worktree on
   `auto/pipeline-<RUN_ID>`, `state.json` with `deadline_epoch == <passed>` and `feature_source`
   naming the claimed file.
2. `test_pipeline_prepare_no_feature_skips_without_agent` — no eligible issue →
   `## Outcome: skipped (no_feature)`, no agent started, tick records `skipped` not `failed`.
3. `test_pipeline_prepare_fails_loud_on_sync_error` — sync failure →
   `## Outcome: failed (repo_sync_failed)`, no agent started.

**Three gaps in the named set, proposed as additions (each is a real behaviour this PR adds, and
none of the three named tests would catch it):**

4. `test_classify_pipeline_outcome_skipped` (in `tests/test_tick.py`) — the `## Outcome: skipped
   (no_feature)` marker maps to `("skipped", None)`, and the reconcile path emits **no** failure
   notification for it. This is the tick half of AC 2 and the one thing in this PR that can page a
   human at 05:00.
5. `test_pipeline_prepare_writes_state_json_atomically` — no partially-written `state.json` is ever
   observable; a write that raises leaves no file at the target path (G-9 is a documented invariant
   of the pipeline, currently enforced only by prompt prose).
6. `test_launcher_forwards_the_same_deadline_to_prepare` (extend
   `tests/test_pipeline_launch_sh.py:318`) — the value tick computed reaches prepare unmodified, so
   the deadline has exactly one owner end to end.

Test-side notes for the implementer: `pipeline_prepare` must be importable and testable with **no
Herdr server** for the sync/pick/state paths (monkeypatch the two `HerdrClient` call sites the way
`tests/test_tick.py`'s autouse fixtures already do for `ensure_repo`/`launch_pipeline`), and the
`--report` path should be a `tmp_path` in tests, never the real `default_reports_dir()`.

## Files touched

**New**

- `src/herdr_routines/pipeline_prepare.py` — `PrepareResult`, `prepare_run`, `_write_state_json`,
  `_write_terminal_report`, the `KEY=VALUE` printer (~200 lines).
- `tests/test_pipeline_prepare.py` — the three named tests + the atomicity test.
- `docs/process/issues/055-orchestrator-stage-loop-in-code.md` — phases B + C (§8).

**Modified**

- `src/herdr_routines/pick_feature.py` — add `PickResult` + `select_and_claim_feature`;
  `run_pick_feature` becomes a printer over it. **Public signature, stdout, stderr and exit codes
  unchanged** (`pick_feature.py:430-516`).
- `src/herdr_routines/cli.py` — register `pipeline-prepare` (`_build_parser`, next to
  `sync-repo` at `cli.py:370`) + `_cmd_pipeline_prepare` (next to `_cmd_sync_repo` at
  `cli.py:1129`).
- `src/herdr_routines/tick.py` — `_classify_pipeline_outcome`: `skipped` branch (`tick.py:1632`);
  reconcile: quiet return for `skipped` before the failure notify (`tick.py:1835`).
- `scripts/pipeline-launch.sh` — prepare call before `herdr workspace create`
  (`pipeline-launch.sh:126`); resolved values appended to the prompt header
  (`pipeline-launch.sh:143-147`); exit codes; no agent on the skip/failure paths.
- `docs/pipeline/orchestrator-prompt.md` — trim Prerequisite (prompt:40-90) and the `pick-feature`
  paragraph of Inputs (prompt:11-34); keep the `uv run` note and every stage section.
- `tests/test_orchestrator_prompt_invocation.py` — narrow `HERDR_ROUTINES_SUBCOMMANDS` to `("gate",)`
  (line 21).
- `tests/test_tick.py` — the `skipped` classification + no-failure-notification test.
- `tests/test_pick_feature.py` — coverage for `select_and_claim_feature`'s return value.
- `tests/test_pipeline_launch_sh.py` — extend the deadline test; new prepare-wiring tests.
- `docs/process/issues/052-…md` (`status: done` + `gate:`), `docs/process/issues/054-…md`
  (`status: done` + `gate:` + log line) — §8.
- `docs/pipeline/runs/20260930T050000Z/spec.md` — this file.

**Deliberately NOT touched** (so the reviewer can check the blast radius is what I claim):
`runner.py`, `gates.py`, `herdr.py`, `pipeline_watchdog.py` (including
`validate_stage_sessions` — phase A does not change who writes `stage_sessions`, only who writes
everything *before* it), `ps.py`, `digest.py`, `config.py`'s schema, `deploy/` units, `docs/pipeline/design.md`.

## Risks

1. **Phase A leaves 7 of 11 mechanical steps on the model, and the biggest one is
   `stage_sessions`.**
   The 2026-09-07 fabrication is *not* fixed by this PR — only detected earlier, by the same
   G-17 check, one stage earlier. A reader could reasonably come away thinking G-17 is now
   structurally impossible. It is not. Mitigation: the spec text, the 054 log line, and 055's
   description all say phase B is what removes the writer. — confidence: high
2. **`## Outcome: skipped` is a new value on a contract with exactly one parser.**
   `tick._classify_pipeline_outcome` is the only reader of the marker's value (verified: no other
   module greps `Outcome:`; `pipeline_watchdog._has_terminal_report` only tests file *existence*,
   so it treats a skip report as terminal correctly and `reports_prune`/`ps` are unaffected). The
   blast radius is small, but the risk is a *silent* one: if the `skipped` branch is missed, the
   run reconciles as `interrupted_unknown (outcome_marker_unrecognized)` — a red history line for
   a completely healthy night. Test 4 is the mitigation. — confidence: high
3. **Reconcile latency on the skip path.** tick never blocks on a pipeline run; it reconciles on a
   *later* tick. With no agent started there is no live-agent short-circuit, and the run stays
   `in flight` until the next tick reads the report — bounded by the timer interval (5 min), and
   the pre-existing "no report yet is not a failure" branch (`tick.py:1844-1847`) means the window
   cannot produce a false failure. Only downside: `skipped` appears in history up to one interval
   late. Accepted. — confidence: high
4. **Two writers of `deadline_epoch` (tick's `running` record and `state.json`) must agree.**
   They are fed by one value (`--deadline-epoch`, computed by `tick.pipeline_deadline_epoch` and
   forwarded verbatim by the launcher), so they can only disagree if the launcher synthesises its
   own — which it does only when tick predates the flag (`pipeline-launch.sh:85-87`). Test 6
   pins the forwarding. The watchdog already prefers the history value
   (`pipeline_watchdog.recorded_pipeline_deadlines`, `pipeline_watchdog.py:228-247`), so even a
   disagreement is not fatal. — confidence: high
5. **The shared workspace is now created by prepare, not the orchestrator.** If prepare creates it
   and the orchestrator then also looks for it (prompt:66-67 still in flight during rollout), the
   orchestrator's `herdr workspace list` lookup finds it and skips creation — the prompt is written
   to tolerate that ("if `$SHARED_WS` not found, create"). Order is create-then-find, so the
   duplicate-create branch cannot fire. The trim in §7 removes the ambiguity anyway. — confidence: high
6. **Editing `orchestrator-prompt.md` weakens a doc-contract test.** Narrowing
   `HERDR_ROUTINES_SUBCOMMANDS` (§7) is correct but it *is* a test change that removes assertions.
   It belongs in the PR description with that reasoning, and it should not be bundled with unrelated
   prompt edits. — confidence: high
7. **The reconcile contract is now written by two writers (prepare and the orchestrator), so the
   "one report, one format" invariant needs enforcement, not just intent.** A single shared
   `_write_terminal_report` helper, used by prepare's two failure paths and asserted against the
   launcher's existing stub shape, is the mitigation; the alternative — three hand-written marker
   strings — is how this class of bug starts. — confidence: medium
8. **Issue bookkeeping is the one irreversible part of this PR.** Flipping 054 to `done` without
   filing 055 retires phases B and C from the backlog with no record that they were ever proposed
   beyond a `gate:` line nobody reads. §8 exists for this; if the implementer skips it, the feature
   is 1/3 shipped and looks complete. — confidence: high

## Verification beyond the suite

- `uv run ruff format --check . && uv run ruff check . && uv run pytest -q` (all three — CI runs
  all three; issue 034).
- `herdr-routines validate` on the pipeline job, plus one manual dry run of
  `pipeline-prepare` against a throwaway `REPO_PARENT` with a fake report path, confirming it
  writes `state.json` at `$WT/state.json` with the expected keys and exits 0.
- Night after merge: confirm one *empty-backlog* run records `skipped` in `history.jsonl` and sends
  no Telegram notification. That is the behaviour 052 wanted, observed rather than asserted.
