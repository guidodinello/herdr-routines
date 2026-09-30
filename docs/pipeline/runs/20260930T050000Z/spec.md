# Spec — move the orchestrator's mechanical steps into code (issue 054, phase A) — 20260930T050000Z — v2

Per-run spec at `docs/pipeline/runs/20260930T050000Z/spec.md` (per-run path on purpose, not
`$WT/spec.md` — G-15; see `docs/pipeline/orchestrator-prompt.md:144`).

Implements `docs/process/issues/054-orchestrator-mechanical-steps-to-code.md` **phase A only**.
v2 is stage 2's review: v1 was kept, its claims about the existing code were re-checked line by
line, four were wrong and are corrected below, and the formal `## Acceptance criteria` /
`## Review tiers` / `## Changelog v1→v2` sections are added.

## Scope

**This one PR implements issue 054 phase A — pre-flight in code — and nothing else. Phases B
(stage loop in code) and C (stage 4 in code) are out of scope and are filed as follow-on issue
055.** Reviewer's verdict on v1's recommendation: **agreed, scope stays narrowed.** v1 argued
for this correctly; the reasoning is kept below in condensed form.

**In this PR, concretely — the complete list of what ships:**

1. A new `herdr-routines pipeline-prepare` subcommand (`pipeline_prepare.py` + `cli.py`).
2. Two new `HerdrClient` methods it needs (`herdr.py`) — see §3b, v1 missed these.
3. `pick_feature.select_and_claim_feature` as a structured core, with `run_pick_feature` as a
   thin printer over it.
4. `tick._classify_pipeline_outcome` learns `skipped`, and the reconcile path returns quietly
   for it.
5. `scripts/pipeline-launch.sh` calls prepare before any agent exists and exits on its verdict.
6. `orchestrator-prompt.md`'s Prerequisite section trimmed; `tests/` extended; 052 flipped to
   done; 055 filed; 054 flipped to done with a phased `gate:`.

**Explicitly not in this PR:** `pipeline-run` (any stage-loop code), stage-4 `git push` /
`gh pr create`, prompt extraction of the six stage prompts to files, code-written heartbeats,
`stage_sessions` ownership, and any change to `runner.py` / `gates.py` / `config.py`'s schema /
`deploy/` units / `docs/pipeline/design.md`.

Why one phase per PR, in v1's terms, condensed:

| Phase | What changes | ACs | Ships with A? |
|---|---|---|---|
| **A — pre-flight in code** | new `pipeline-prepare`; launcher calls it before any agent; prompt's Prerequisite section shrinks | 1–3 | — (this PR) |
| **B — stage loop in code** | `pipeline-run` replaces the orchestrator session; 6 stage prompts move to files; session map, gate dispatch, heartbeat, deadline, pane lifecycle all move into code | 4–8 | no (055) |
| **C — stage 4 in code** | `git push` + `gh pr create` from the spec; no agent for stage 4 | 9 | no (055, with B) |

1. **A is independently shippable and independently valuable.** It is the only phase that changes
   nothing about how the pipeline *runs*. It also subsumes issue 052, the one with a real cost
   today (every empty-backlog night burns a full orchestrator session). A can ship and be observed
   for a week with zero behavioural change to the stage loop.
2. **B is the risky change and deserves a PR of its own.** It deletes the orchestrator session —
   the component running *this* pipeline. Its blast radius is every stage, the watchdog's two
   signals, the reconcile contract, and the resume path. Bisecting against a proven Phase-A
   baseline is far easier than bisecting A+B together.
3. **C folds into B, not into A.** C's whole content is "this stage's worker is no worker"; it
   only makes sense once the stage loop is in code. 3 phases, 2 PRs.
4. **One thing A must not do:** flip 054 to `done` without filing the follow-on. Stage 3's
   contract is "flip the picked issue to `done`" (`orchestrator-prompt.md:158`), so a Phase-A PR
   that obeys that literally would retire the issue and lose phases B and C from the backlog. A's
   PR therefore files **055** and sets 054's `status: done` with a `gate:` note pointing at it —
   exactly the pattern 052 already uses (`docs/process/issues/052-pipeline-launches-with-no-feature-to-build.md:6`).

Phase A's own scope note: counting issue 054's 11-row table, **4 rows are phase A** (pick-feature,
sync-repo, worktree create + `jq`, atomic `state.json`), **6 rows are phase B** (the heartbeat
line, spawn/prompt/poll/settle/quota/retry, `stage_sessions` + `current_stage`, gate dispatch,
pane close + save `pl-3`'s session, deadline check + partial report), **1 row is phase C** (stage
4's `git push` + `gh pr create`). **A therefore leaves 7 of the 11 steps on the model.** That is
deliberate: A changes no run semantics, and it is the only phase whose absence costs something
every single night.

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
  Code now *checks* for this (`pipeline_watchdog.validate_stage_sessions`,
  `pipeline_watchdog.py:115`; G-17), but the model still *writes* the map it is checked against —
  so G-17 is a detector, not a prevention.
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
`pipeline_watchdog.py` / `pick_feature.py` convention), registered in `cli.py`'s `_build_parser`
next to `sync-repo` (`cli.py:370-381`) and dispatched by a `_cmd_pipeline_prepare` modelled on
`_cmd_sync_repo` (`cli.py:1129-1139`) — same posture: log, `return 1`, no config load
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

`prepare_run(...)` returns a `PrepareResult` frozen dataclass (shape mirrors the existing
`runner.RunOutcome`, `runner.py:441-461`, the convention for "a run's terminal disposition"):

```python
@dataclass(frozen=True, slots=True)
class PrepareResult:
    outcome: Literal["ok", "no_feature", "sync_failed", "prepare_failed"]
    feature_idea: str | None
    feature_source: str | None      # "docs/process/issues/054-….md"
    issue_id: str | None            # the claimed issue's id, for the report's picked-issue line
    worktree: str | None
    branch: str | None
    workspace_id: str | None
    state_path: Path | None
    reclaimed: tuple[ReclaimedPick, ...]   # issue 040's reclaims, for the report
```

### 2. Order: sync → pick → worktree/workspace → `state.json`, with fail-loud reports at each step

Matches the prompt's Prerequisite order exactly (`orchestrator-prompt.md:49`, `:61`, `:70`), so
the failure semantics are the ones the prompt already promises — just executed in Python instead
of by a model.

1. **sync** — `_fetch_and_fast_forward(repo_parent, base=base)` (`repos.py:93-129`), the same
   primitive `sync-repo` (`cli.py:1129`) and `ensure_repo` (`repos.py:42-66`) use. It raises
   `RuntimeError`; non-zero/exception → `## Outcome: failed (repo_sync_failed)` (AC 3) and **no
   agent**. Note this makes `$REPO_PARENT` synced twice on a normal run: once by
   `tick._launch_pipeline_run`'s `ensure_repo(job)` at dispatch (`tick.py:1991`) and once here.
   That is deliberate, not an oversight. `ensure_repo` runs inside tick's own process on tick's
   checkout and happens in the same tick that registers the unit; the branch-off that actually
   consumes this clone happens later, inside a different process on a possibly different
   checkout (the launcher comes from the auto-synced job repo — `pipeline-launch.sh:82-84` says
   so explicitly). The second sync is the one that guards the branch point. Behaviour on a
   non-fast-forward is unchanged: `_fetch_and_fast_forward` raises rather than merging
   (`repos.py:126-129`), which is the fail-loud behaviour the prompt's step 1 already promised
   (`orchestrator-prompt.md:57-58`).
   **[non-blocking]** If the double sync shows up as latency on the Pi, dropping prepare's sync
   and relying on dispatch's is a one-line change — but not before, because the branch point is
   the thing that must be current. — confidence: medium
2. **pick** — `select_and_claim_feature(..., mark=True)` (§3). "no open issues" →
   `## Outcome: skipped (no_feature)` (AC 2), **no agent, no worktree, no workspace, no
   `state.json`**. The worktree is created *after* the pick for exactly this reason: an empty
   backlog must cost zero worktrees, not merely zero stages. This is issue 052's whole ask.
3. **worktree + shared workspace** — `herdr worktree create --cwd <repo_parent> --base <base>
   --branch auto/pipeline-<run_id>`, then locate-or-create the shared workspace with
   `--env HERDR_ENV=1` and `--label pipeline-<run_id>` (`orchestrator-prompt.md:63-68`). The
   `--env` is not optional: without `HERDR_ENV=1` the orchestrator cannot drive `herdr` at all
   (`docs/pipeline/design.md:137-139`, `orchestrator-prompt.md:90`). This is the first step that
   needs a Herdr server, which is why it sits after the pick: **a no-feature night needs no Herdr
   server at all**, the same posture `pick_feature` already documents (`cli.py:1071-1075`).
4. **`state.json`** — written atomically to `$WT/state.json` via tmpfile + `os.replace` in the
   same directory (G-9), with the exact key set from the prompt's example
   (`orchestrator-prompt.md:72-84`) so that `pipeline_watchdog._parse_state_json`
   (`pipeline_watchdog.py:249`), `tick._pipeline_stage_independence_issue` (`tick.py:1606`),
   `ps._parse_state_json` (`ps.py:77`) and `validate_stage_sessions` all keep working unchanged:

   ```json
   {"run_id": …, "current_stage": 0, "pr_number": null, "shared_worktree": …,
    "branch": "auto/pipeline-<run_id>", "shared_workspace": …, "deadline_epoch": …,
    "feature_source": "docs/process/issues/<file>",
    "artifact_paths": {"spec": "$WT/docs/pipeline/runs/<run_id>/spec.md", "report": "<report>"},
    "stage_sessions": {}}
   ```

   `feature_source` is required by the prompt (`orchestrator-prompt.md:20`) but **missing from the
   prompt's own JSON example** (`orchestrator-prompt.md:72-84`) — a small latent bug in the
   prompt, fixed here by including it. `stage_sessions` stays empty: the orchestrator still fills
   it in phase A (phase B moves the writer). `current_stage: 0` with an empty session map is safe
   for the watchdog — its stage-independence downgrade is applied by `tick` only when the
   reconciled state is `done` (`tick.py:1772-1781`), and `is_stalled` requires *both* the
   deadline+grace to have passed *and* a stale heartbeat
   (`pipeline_watchdog.is_stalled`, `pipeline_watchdog.py:361-385`; `DEADLINE_GRACE_SECONDS` at
   `:67`, `HEARTBEAT_STALE_SECONDS` at `:72`), so a `state.json` written seconds before the first
   heartbeat cannot trip the reaper.

### 3. `feature_source` needs a real return value

`run_pick_feature` (`pick_feature.py:430-516`) returns `int` and writes the feature idea to
`out`/`sys.stdout`; the picked `Issue` — and therefore `feature_source` — is not in that return
value. It *is* recoverable from stdout (`render_feature_idea` embeds `issue.path.as_posix()` at
`pick_feature.py:148-150`), but scraping stdout to recover a path is precisely the practice this
issue exists to delete.

**Correction to v1:** the reclaim notices are *not* stderr-only. `run_pick_feature` already takes
`notify: Callable[[ReclaimedPick], None] | None` (`pick_feature.py:439`, documented at `:453-455`)
and calls it for every reclaim, and `tests/test_pick_feature.py:370-405` already pins that
injection point. v1's justification ("the reclaim lines go to stderr") was wrong. The real gap is
narrower and is only about `feature_source`.

So: add `PickResult` (frozen dataclass: `issue: Issue`, `feature_idea: str`,
`reclaimed: tuple[ReclaimedPick, …]`) and `select_and_claim_feature(...) -> PickResult | None` as
the new core, with the reclaim loop moved up from `run_pick_feature` (it currently lives inline at
`pick_feature.py:466-481`). `run_pick_feature` becomes a thin printer over it: it passes
`notify=lambda r: (notified.append(r), print(f"reclaimed stale claim: issue {r.issue_id} …",
file=sys.stderr))`-equivalent so both the injected notifier and the stderr line keep working, and
`print("no open issues", file=sys.stderr); return 1` for the empty case
(`pick_feature.py:510-512`). `run_pick_feature`'s signature, stdout, stderr and exit codes are
**unchanged**, so all 603 lines of `tests/test_pick_feature.py` and `_cmd_pick_feature`
(`cli.py:1071-1101`) need no edits.

### 3b. `HerdrClient` cannot do what prepare needs today (v1 missed this — blocking)

v1 claimed prepare would "parse `.result.worktree.path` and `.result.branch` exactly as the
prompt does" and listed `herdr.py` under **deliberately NOT touched**. Both are wrong:

- `HerdrClient.worktree_create` (`herdr.py:261-279`) returns **only the root pane id** — it
  resolves `("result","root_pane","pane_id")` and discards everything else. The worktree path and
  branch the prompt parses with `jq` (`orchestrator-prompt.md:64-65`) are unreachable through it.
- There is **no `workspace_create` and no `workspace_list` anywhere in `src/`** — and no
  `--env` support at all (`rg -- '--env' src/ scripts/ deploy/` matches exactly one line:
  `pipeline-launch.sh:127`). So prepare currently *cannot* create the `HERDR_ENV=1` shared
  workspace the prompt requires, and cannot do the "find it, else create it" lookup.

Phase A therefore adds two `HerdrClient` methods (the only change to `herdr.py`):

- `worktree_create_full(*, cwd, branch, base, label) -> WorktreeInfo` returning
  `WorktreeInfo(path, branch, root_pane_id)`; `worktree_create` keeps its current signature and
  becomes a thin wrapper returning `.root_pane_id`, so no existing caller changes.
- `workspace_create(*, cwd, label, env) -> str` returning the workspace id, and
  `workspace_list() -> list[dict]` for the locate-or-create lookup.

Both get `FakeRunner` tests in `tests/test_herdr.py`, matching
`test_worktree_create_builds_expected_argv_and_parses_pane_id` (`tests/test_herdr.py:42-53`).

### 4. The terminal report: one format, written by whoever fails

The two non-`ok` paths write the report themselves, in the shape the launcher already uses for its
own stubs (`pipeline-launch.sh:233-247`): title, `## Outcome: …` immediately after it (the "near
the top, trivial to `grep -m1`" rule, `orchestrator-prompt.md:203`), then reason lines, including
any `reclaimed stale claim: issue NN …` lines verbatim (issue 040's "a reclaim must be visible,
never silent"). `_write_terminal_report` should be a single helper used by both paths so the
marker never drifts.

**Exit codes** (the launcher branches on these and nothing else): `0` = prepared, `3` = no feature
(AC 2's skip), `1` = sync or prepare failure (AC 3), `2` = argparse usage error. `3` is distinct
from `1` on purpose — the launcher must be able to tell "healthy night, nothing to build" from
"something broke", because the two want opposite exit statuses from the unit. `sync-repo` today
returns plain `1` on failure (`cli.py:1133-1139`) and `run_pick_feature` returns `1` for "no open
issues" (`pick_feature.py:510-512`); inside `prepare_run` those two conditions are
*distinguished* and only the collapsed exit code is 3-or-1, so nothing changes for the existing
commands' own contracts.

`## Outcome: skipped (no_feature)` is a **new value on the reconcile contract** and
`_classify_pipeline_outcome` (`tick.py:1632-1652`) has no branch for it today — it would fall
through to `return "interrupted_unknown", "outcome_marker_unrecognized"` (`tick.py:1652`). See §5.

### 5. The load-bearing tick change: `skipped` must classify as `skipped`

AC 2 says "tick records `skipped`, not `failed`". Getting there needs **two** edits in `tick.py`,
and the second is easy to miss:

1. `_classify_pipeline_outcome` (`tick.py:1632-1652`): add
   `if status.startswith("skipped"): return "skipped", None` **before** the `partial`/`failed`
   checks. `skipped` is already in `history.TERMINAL_STATES` (`history.py:25-27`) and is already
   used as a history state elsewhere in tick for "nothing to do" (`tick.py:205` for an agent
   already live, `tick.py:405` for a PR-target over cap), so `_open_pipeline_run`
   (`tick.py:1655-1672`) closes the run correctly and the next scheduled occurrence makes a fresh
   decision — an empty backlog never wedges the job.
2. The reconcile path (`tick.py:1766-1842`): after the `done` branch returns
   (`tick.py:1798-1801`), **everything else falls through to the failure notify**
   (`if _notify_gate(job, "failure"): _notify(…, sound="request")`, `tick.py:1835-1841`). A
   `skipped` run must return before that, exactly like `done` does, or every empty-backlog night
   sends a failure notification to Telegram at 05:00 — the single worst possible outcome for a
   feature whose whole point is that "nothing to do" is normal and cheap.

This is a change to the *reconcile contract*, so it gets its own tests beyond the three named in
the issue (see `## Acceptance criteria` 7). It is also why the follow-on work is not optional:
nothing today has ever written a `skipped` report, so this branch is untested in production.

### 6. Launcher wiring

In `scripts/pipeline-launch.sh`, call prepare **after** `cd "$REPO_PARENT"` (`pipeline-launch.sh:124`)
and **before** `herdr workspace create` (`pipeline-launch.sh:126`) and before the `cleanup` trap
has anything to clean:

```sh
PREPARE_OUT=$(uv run herdr-routines pipeline-prepare \
  --run-id "$RUN_ID" --repo-parent "$REPO_PARENT" --report "$REPORT" \
  --deadline-epoch "$DEADLINE_EPOCH") ; PREPARE_RC=$?
```

**Correction to v1:** do *not* append `2>&1` to that capture (v1 did). The script already
redirects stdout+stderr into `$LOG` (`pipeline-launch.sh:106-107`), so prepare's `log.*` lines
stay in the run log for free; merging them into `$PREPARE_OUT` would splice log text into a
`KEY=VALUE` stream that is about to be appended to the orchestrator prompt. Capture stdout only.

`uv run` is required — `herdr-routines` is not on `PATH` on the pipeline hosts (issue 050, pinned
by `tests/test_orchestrator_prompt_invocation.py:28-40`).

- `rc == 0` → `$PREPARE_OUT` is `KEY=VALUE` lines; append them to the prompt header next to
  `RUN_ID`/`REPO_PARENT`/`PIPELINE_REPORT`/`DEADLINE_EPOCH` (`pipeline-launch.sh:143-147`) and fall
  through to the existing agent-start path unchanged. Appending to the *header* rather than editing
  the prompt file keeps `--prompt-file` (a flag, default `docs/pipeline/orchestrator-prompt.md`)
  working for any custom prompt — tick passes it as `job.repo / job.prompt_file`
  (`tick.py:1727-1728`).
- `rc == 3` (no feature) or any other non-zero (sync/prepare failure) → **exit without
  `herdr agent start`**. The report is already written at the `--report` path, which *is* tick's
  reconciled path (`tick._build_pipeline_launch_argv` passes `pipeline_report_path(bare_run_id)`,
  i.e. `default_reports_dir()/pipeline-<run_id>.md`, `tick.py:1590-1594` and `:1721-1722`), so the
  launcher adds nothing, mirrors nothing and stubs nothing. Exit 0 for the `no_feature` case (a
  skip is a successful skip, and a non-zero unit would page a human for a healthy night) and
  non-zero for real failures.

`--deadline-epoch` is already computed and defaulted at `pipeline-launch.sh:85-87`; it is simply
now also forwarded. `test_launcher_hands_orchestrator_tick_computed_deadline`
(`tests/test_pipeline_launch_sh.py:318-325`) should be extended to assert the *same* value reaches
prepare, so the two writers of the deadline can never drift.

**Correction to v1 — the launcher test harness will break unless it is extended (blocking).**
`tests/test_pipeline_launch_sh.py` runs the real script against a fake `herdr` written to
`$HOME/.local/bin/herdr` (`tests/test_pipeline_launch_sh.py:27-70`, installed at `:88-95`) precisely
because the script hardcodes its own `PATH` (`pipeline-launch.sh:105`). There is no fake `uv`.
Adding a `uv run herdr-routines pipeline-prepare` call means every existing test in that file
would shell out to the **real** `uv` and the **real** subcommand — which syncs a git repo, reads
`docs/process/issues/`, and may create a worktree. Fix in the same PR: add a fake `uv` (or a fake
`herdr-routines`) stub beside `FAKE_HERDR`, recording its argv to a second call log and emitting
canned `KEY=VALUE` stdout plus a settable exit code. Without this, the launcher change is untestable
and CI-hostile.

### 7. Prompt trim

`orchestrator-prompt.md`'s "Prerequisite (do once, before stage 1)" section
(`orchestrator-prompt.md:40-90`) shrinks to a short "this is already done, here are your values"
note; the `sync-repo` block (`:55-59`), the worktree/workspace block (`:63-68`) and the
`state.json` template (`:72-84`) are replaced by a pointer to the header values. The `uv run` note
(`:42-47`) **stays** — issue 050's lesson is about *how* to invoke the CLI, it is not specific to
the subcommands being removed, and the orchestrator still invokes `gate` by hand. The
`stage_sessions`/G-17 paragraphs (`:86-88`) and the heartbeat instruction (`:90`) **stay**: phase A
does not move those writers. "Inputs you will receive" (`:11-34`) loses the whole `pick-feature`
paragraph and gains `FEATURE_IDEA` + `FEATURE_SOURCE` as resolved header values. The stage-1 spawn
template, the stage details, the deadline/quota/resume/cleanup section, the failure semantics and
the final-report contract are **unchanged** in phase A.

`tests/test_orchestrator_prompt_invocation.py` needs a matching edit, and this one is worth stating
explicitly because it looks like a test-weakening: `HERDR_ROUTINES_SUBCOMMANDS`
(`tests/test_orchestrator_prompt_invocation.py:21`) is `("sync-repo", "pick-feature", "gate")`, and
each is asserted to appear as `uv run …` (`:28-40`). After the trim, `sync-repo` and `pick-feature`
no longer appear in the prompt at all, so those two assertions go vacuous and the tuple should be
narrowed to `("gate",)`. The *intent* of the test — issue 050's "never a bare `herdr-routines`" — is
fully preserved, because `gate` is the one subcommand the orchestrator still invokes by hand and
is therefore the one that can still regress that way. `test_prompt_notes_herdr_routines_not_a_binary`
(`:43-48`) still passes unchanged because the note is kept.

### 8. Issue bookkeeping (do this or lose phases B and C)

- `docs/process/issues/052-pipeline-launches-with-no-feature-to-build.md`: `status: blocked` →
  `status: done`, `gate:` updated to record that phase A shipped. Its own frontmatter already
  predicts this (`052-…md:6`).
- `docs/process/issues/055-orchestrator-stage-loop-in-code.md` (new): phases B and C, with ACs 4–9
  copied from 054 and the same design text, so they are picked up as their own run.
- `docs/process/issues/054-orchestrator-mechanical-steps-to-code.md`: `status: open` → `status: done`,
  `gate: phased — phase A in this PR; phases B+C tracked by issue 055`, plus a log line.

## Acceptance criteria

Each item is test-anchored; `Test:` lines name the exact test function stage 3 must author. Test
home is named inline so the implementer does not have to guess where each one lives.

1. With an eligible issue, `pipeline-prepare` leaves a synced `$REPO_PARENT`, a claimed issue (in
   the out-of-tree claims store — the issue file is untouched, `pick_feature.py:441-454`), a
   worktree on `auto/pipeline-<RUN_ID>`, and a `$WT/state.json` whose `deadline_epoch` equals the
   passed value and whose `feature_source` names the claimed file. Exit 0, and the resolved values
   are printed as `KEY=VALUE` on **stdout** with logs on stderr. `tests/test_pipeline_prepare.py`
   (real bare-origin git fixture, copied from `tests/test_sync_repo.py:38-67`; no `herdr` binary).
   Test: test_pipeline_prepare_sets_up_run
   Test: test_pipeline_prepare_prints_resolved_values

2. With no eligible issue, `pipeline-prepare` writes `## Outcome: skipped (no_feature)` to
   `--report` and exits 3, having created **no** worktree, **no** workspace, **no** `state.json`
   and **no** claim — the whole point of issue 052. `tests/test_pipeline_prepare.py`.
   Test: test_pipeline_prepare_no_feature_skips_without_agent
   Test: test_pipeline_prepare_no_feature_creates_no_worktree

3. A `sync-repo` failure writes `## Outcome: failed (repo_sync_failed)` to `--report` and exits 1
   with no agent, no worktree and no claim. `tests/test_pipeline_prepare.py` (diverged origin, per
   `tests/test_sync_repo.py:103-139`).
   Test: test_pipeline_prepare_fails_loud_on_sync_error

4. `--deadline-epoch` is required (argparse `SystemExit`), and the exit codes are exactly
   {`0` prepared, `3` no feature, `1` sync/prepare failure, `2` usage} — `3` and `1` are distinct
   so the launcher can tell a healthy night from a broken one.
   `tests/test_pipeline_prepare.py`, matching `test_sync_repo_requires_path`'s shape
   (`tests/test_sync_repo.py:142-144`).
   Test: test_pipeline_prepare_requires_deadline_epoch
   Test: test_pipeline_prepare_exit_codes_are_distinct

5. The two `HerdrClient` capabilities prepare needs exist and are exercised against a `FakeRunner`
   — `worktree create`'s response yields the worktree path and branch, and `workspace create`
   carries `--env HERDR_ENV=1`. `tests/test_herdr.py`, next to
   `test_worktree_create_builds_expected_argv_and_parses_pane_id` (`:42-53`).
   Test: test_worktree_create_full_returns_path_and_branch
   Test: test_workspace_create_passes_env_argv

6. `select_and_claim_feature` returns the picked `Issue` (so `feature_source` never comes from
   scraped stdout) and the reclaims it performed; `run_pick_feature` remains a thin printer whose
   signature, stdout, stderr and exit codes are byte-for-byte unchanged, so the existing
   `tests/test_pick_feature.py` needs no edits.
   `tests/test_pick_feature.py`, extending `test_reclaimed_pick_is_surfaced` (`:370-405`) which
   already pins the `notify` injection point.
   Test: test_select_and_claim_feature_returns_issue_and_reclaims
   Test: test_run_pick_feature_contract_unchanged

7. `state.json` is written atomically (tmpfile + `os.replace` in the same directory, G-9): no
   partially-written file is ever observable, and a write that raises leaves **no** `state.json` at
   the target path. `tests/test_pipeline_prepare.py`.
   Test: test_pipeline_prepare_writes_state_json_atomically

8. The sync/pick/state paths need no Herdr server at all: `prepare_run` is importable and testable
   with the two `HerdrClient` call sites injected and no `herdr` binary on `PATH` — the same posture
   `pick_feature`'s module docstring and `cli.py:1071-1075` already claim.
   `tests/test_pipeline_prepare.py`.
   Test: test_pipeline_prepare_needs_no_herdr_server

9. `## Outcome: skipped (…)` classifies as `("skipped", None)` in `_classify_pipeline_outcome`, and
   the reconcile path records `state="skipped"` and emits **no** notification — not even when
   `notify_gate: failure` is configured. This is the tick half of AC 2 and the only thing in this
   PR that can page a human at 05:00. `tests/test_tick.py`, modelled on
   `test_pipeline_quota_exhausted_report_is_classified` (`:1646-1676`) with `FakePipelineClient`'s
   `.notifications` list (`tests/test_tick.py:1368-1380`).
   Test: test_pipeline_skipped_report_classifies_as_skipped
   Test: test_pipeline_skipped_report_sends_no_failure_notification

10. The launcher calls `pipeline-prepare` **before** `herdr agent start` (ordering asserted against
    the fake `uv` and fake `herdr` call logs), and on rc 3 or any other non-zero it starts no
    agent and adds nothing to the report. `tests/test_pipeline_launch_sh.py`, which needs the new
    fake `uv` stub from §6.
    Test: test_launcher_prepares_before_starting_the_orchestrator
    Test: test_launcher_skips_agent_when_no_feature

11. On rc 0, the prepared `KEY=VALUE` lines are appended to the prompt header alongside the
    existing `RUN_ID`/`REPO_PARENT`/`PIPELINE_REPORT`/`DEADLINE_EPOCH` block, so `--prompt-file`
    keeps working for a custom prompt. Asserted against the fake-herdr `agent prompt` argv, the way
    the existing deadline tests do. `tests/test_pipeline_launch_sh.py`.
    Test: test_launcher_appends_prepared_values_to_the_prompt_header
    Test: test_launcher_hands_orchestrator_tick_computed_deadline

12. The tick-computed `--deadline-epoch` reaches prepare **unmodified**, so the deadline has
    exactly one owner end to end (the two writers — tick's `running` record at
    `tick.py:2047-2062` and `state.json` — cannot drift). Extends
    `test_launcher_hands_orchestrator_tick_computed_deadline` (`tests/test_pipeline_launch_sh.py:318`).
    Test: test_launcher_forwards_same_deadline_to_prepare
    Test: test_launcher_computes_deadline_when_tick_passes_none

13. The prompt trim keeps issue 050's guard intact: the Prerequisite section no longer instructs
    the orchestrator to sync, branch or write `state.json`; the `uv run` note survives; the prompt
    still invokes `herdr-routines gate` as `uv run …` and contains no bare subcommand.
    `tests/test_orchestrator_prompt_invocation.py` (with `HERDR_ROUTINES_SUBCOMMANDS` narrowed to
    `("gate",)` at `:21`), plus one new doc-contract test.
    Test: test_prompt_no_longer_asks_the_orchestrator_to_do_setup
    Test: test_prompt_invokes_herdr_routines_via_uv_run
    Test: test_prompt_notes_herdr_routines_not_a_binary

14. Issue bookkeeping is pinned by a test, not by good intentions: 055 exists with 054's ACs 4–9,
    054 is `status: done` with a `gate:` naming 055, and 052 is `status: done`. Without this, the
    feature is 1/3 shipped and looks complete. Doc-contract test, same pattern as
    `tests/test_sync_repo.py:147-190` and `tests/test_pipeline_pane_lifecycle.py`.
    Test: test_phase_b_and_c_filed_as_follow_on_issue_055

## Review tiers

Severity is per acceptance criterion, the same `blocking` / `non-blocking` split the code-review
skill uses (`docs/pipeline/spec.md:89`).

| # | Criterion | Severity | confidence: |
|---|---|---|---|
| 1 | `pipeline-prepare` sets up a real run (worktree, state.json, forwarded values) | blocking | confidence: high |
| 2 | No-feature night writes `skipped` and creates nothing | blocking | confidence: high |
| 3 | Sync failure is loud (`failed (repo_sync_failed)`, exit 1) | blocking | confidence: high |
| 4 | `--deadline-epoch` required; exit codes {0,3,1,2} distinct | blocking | confidence: high |
| 5 | `worktree_create_full` + `workspace_create` (incl. `--env`) exist and are tested | blocking | confidence: high |
| 6 | `select_and_claim_feature` returns the `Issue`; `run_pick_feature` contract unchanged | blocking | confidence: high |
| 7 | `state.json` written atomically, no partial file observable | blocking | confidence: medium |
| 8 | Sync/pick/state paths need no Herdr server | blocking | confidence: high |
| 9 | `skipped` classifies as `skipped` **and** sends no failure notification | blocking | confidence: high |
| 10 | Launcher prepares before any agent; no agent on skip/failure | blocking | confidence: high |
| 11 | Prepared values reach the prompt header; `--prompt-file` keeps working | blocking | confidence: medium |
| 12 | One deadline owner end to end | blocking | confidence: high |
| 13 | Prompt trim keeps issue 050's `uv run` guard | blocking | confidence: medium |
| 14 | Phases B and C filed as 055 before 054 is closed | blocking | confidence: high |

- **blocking** = a failure here means the feature does not do what the issue asked, or does it in a
  way the next stage cannot observe. Every criterion above is blocking, and deliberately so: this
  is a phase whose entire value is "code owns the signal instead of the model", and a partially
  moved signal is worse than an unmoved one because it is no longer obviously unmoved.
- **non-blocking** = correctness of the follow-on details. Carried from v1 as proposals, not as
  gates: the `(now, now + 48h]` deadline sanity guard (approach §1), and dropping prepare's sync
  if the double sync measurably costs Pi latency (approach §2.1). Both are cheap to add later and
  neither blocks the phase's goal.
- **Reviewer's own confidence, per finding:** high on everything except criterion 7 (atomic-write
  failure injection is fiddly to test without over-mocking; a `tmp_path` + monkeypatched
  `os.replace` is the honest shape, medium), criterion 11 (the header text is a launcher/CLI
  contract with no precedent in this repo — `KEY=VALUE` is my proposal, not an established
  convention, medium), and criterion 13 (trimming a prompt that gates the current pipeline is the
  kind of edit that passes its own test and still confuses a model, medium).

## Files touched

**New**

- `src/herdr_routines/pipeline_prepare.py` — `PrepareResult`, `prepare_run`, `_write_state_json`,
  `_write_terminal_report`, the `KEY=VALUE` printer (~220 lines).
- `tests/test_pipeline_prepare.py` — criteria 1, 2, 3, 4, 7, 8.
- `docs/process/issues/055-orchestrator-stage-loop-in-code.md` — phases B + C (§8).

**Modified**

- `src/herdr_routines/herdr.py` — `worktree_create_full` + `WorktreeInfo`; `workspace_create`;
  `workspace_list`; `worktree_create` becomes a wrapper (§3b). **v1 listed this file as
  deliberately untouched; that was wrong** and the correction is load-bearing.
- `src/herdr_routines/pick_feature.py` — add `PickResult` + `select_and_claim_feature`;
  `run_pick_feature` becomes a printer over it. **Public signature, stdout, stderr and exit codes
  unchanged** (`pick_feature.py:430-516`).
- `src/herdr_routines/cli.py` — register `pipeline-prepare` (next to `sync-repo`,
  `cli.py:370-381`) + `_cmd_pipeline_prepare` (next to `_cmd_sync_repo`, `cli.py:1129-1139`).
- `src/herdr_routines/tick.py` — `_classify_pipeline_outcome`: `skipped` branch
  (`tick.py:1632-1652`); reconcile: quiet return for `skipped` before the failure notify
  (`tick.py:1835`).
- `scripts/pipeline-launch.sh` — prepare call after `cd` (`:124`) and before `herdr workspace
  create` (`:126`); resolved values appended to the prompt header (`:143-147`); exit codes; no
  agent on the skip/failure paths; **stdout-only capture** (no `2>&1`).
- `docs/pipeline/orchestrator-prompt.md` — trim Prerequisite (`orchestrator-prompt.md:40-90`) and
  the `pick-feature` paragraph of Inputs (`:11-34`); keep the `uv run` note, the `stage_sessions`
  /G-17 paragraphs, the heartbeat instruction, and every stage section.
- `tests/test_pipeline_launch_sh.py` — **new fake `uv` stub** (§6); extend the deadline tests;
  new prepare-wiring tests (criteria 10–12).
- `tests/test_orchestrator_prompt_invocation.py` — narrow `HERDR_ROUTINES_SUBCOMMANDS` to
  `("gate",)` (`:21`); new Prerequisite-trim test (criterion 13).
- `tests/test_herdr.py` — `worktree_create_full` / `workspace_create` argv + response tests
  (criterion 5).
- `tests/test_pick_feature.py` — coverage for `select_and_claim_feature` (criterion 6).
- `tests/test_tick.py` — the `skipped` classification + no-failure-notification tests
  (criterion 9).
- `docs/process/issues/052-…md` (`status: done` + `gate:`), `docs/process/issues/054-…md`
  (`status: done` + `gate:` + log line) — §8.
- `docs/pipeline/runs/20260930T050000Z/spec.md` — this file.

**Deliberately NOT touched** (so the reviewer can check the blast radius is what I claim):
`runner.py`, `gates.py`, `config.py`'s schema, `pipeline_watchdog.py` (including
`validate_stage_sessions` — phase A does not change who writes `stage_sessions`, only who writes
everything *before* it), `ps.py`, `digest.py`, `reports_prune.py`, `deploy/` units,
`docs/pipeline/design.md`.

## Risks

1. **Phase A leaves 7 of 11 mechanical steps on the model, and the biggest one is
   `stage_sessions`.**
   The 2026-09-07 fabrication is *not* fixed by this PR — only detected earlier, by the same
   G-17 check, one stage earlier. A reader could reasonably come away thinking G-17 is now
   structurally impossible. It is not. Mitigation: the spec text, the 054 log line, and 055's
   description all say phase B is what removes the writer. — [blocking] confidence: high
2. **`## Outcome: skipped` is a new value on a contract with exactly one parser.**
   `tick._classify_pipeline_outcome` is the only reader of the marker's *value* (verified: `_OUTCOME_RE`
   at `tick.py:1587` is used only at `:1638`; `pipeline_watchdog._write_report` *writes* the marker
   but only `failed (watchdog: …)` values at `pipeline_watchdog.py:415-424`; `pipeline_watchdog._has_terminal_report`
   at `:222` only tests file *existence*, so it treats a skip report as terminal correctly, and
   `reports_prune` / `ps` are unaffected). The blast radius is small, but the risk is a *silent*
   one: if the `skipped` branch is missed, the run reconciles as
   `interrupted_unknown (outcome_marker_unrecognized)` — a red history line for a completely
   healthy night. Criteria 9 is the mitigation. — [blocking] confidence: high
3. **The launcher's own test harness has no `uv` stub, so the wiring is untestable until it gets
   one** (approach §6). Miss this and CI either performs a real sync/pick/worktree-create or fails
   in a way that reads like a flake. — [blocking] confidence: high
4. **Reconcile latency on the skip path.** tick never blocks on a pipeline run; it reconciles on a
   *later* tick. With no agent started there is no live-agent short-circuit, and the run stays
   `in flight` until the next tick reads the report — bounded by the timer interval (5 min), and
   the pre-existing "no report yet is not a failure" branch (`tick.py:1844-1849`) means the window
   cannot produce a false failure. Only downside: `skipped` appears in history up to one interval
   late. Accepted. — [non-blocking] confidence: high
5. **Two writers of `deadline_epoch` (tick's `running` record at `tick.py:2060` and `state.json`)
   must agree.** They are fed by one value (`--deadline-epoch`, computed by
   `tick.pipeline_deadline_epoch` and forwarded verbatim by the launcher), so they can only
   disagree if the launcher synthesises its own — which it does only when tick predates the flag
   (`pipeline-launch.sh:85-87`). Criterion 12 pins the forwarding. The watchdog already prefers the
   history value (`pipeline_watchdog.recorded_pipeline_deadlines`, `pipeline_watchdog.py:228-247`),
   so even a disagreement is not fatal. — [blocking] confidence: high
6. **The shared workspace is now created by prepare, not the orchestrator.** If prepare creates it
   and the orchestrator then also looks for it (`orchestrator-prompt.md:66-67` still in flight
   during rollout), the orchestrator's `herdr workspace list` lookup finds it and skips creation —
   the prompt is written to tolerate that ("if `$SHARED_WS` not found, create"). The trim in §7
   removes the ambiguity anyway. — [non-blocking] confidence: medium
7. **Editing `orchestrator-prompt.md` weakens a doc-contract test.** Narrowing
   `HERDR_ROUTINES_SUBCOMMANDS` (§7) is correct but it *is* a test change that removes assertions.
   It belongs in the PR description with that reasoning, and it should not be bundled with unrelated
   prompt edits. — [blocking] confidence: high
8. **The reconcile contract now has two writers (prepare and the orchestrator), so the "one report,
   one format" invariant needs enforcement, not just intent.** A single shared
   `_write_terminal_report` helper, used by prepare's two failure paths and asserted against the
   launcher's existing stub shape, is the mitigation; the alternative — three hand-written marker
   strings — is how this class of bug starts. — [blocking] confidence: medium
9. **Issue bookkeeping is the one irreversible part of this PR.** Flipping 054 to `done` without
   filing 055 retires phases B and C from the backlog with no record that they were ever proposed
   beyond a `gate:` line nobody reads. §8 exists for this, and criterion 14 makes it a test.
   — [blocking] confidence: high
10. **A `KEY=VALUE` stdout contract is a new convention in this repo.** Nothing else in `src/`
   emits machine-parsed stdout; `ps`/`digest`/`scheduled` render human tables. It is the right
   shape here (the values have to cross a shell boundary), but it is un-precedented and therefore
   the thing most likely to surprise a future reader. — [non-blocking] confidence: medium

## Verification beyond the suite

- `uv run ruff format --check . && uv run ruff check . && uv run pytest -q` (all three — CI runs
  all three; issue 034). Plus `uv run mypy` if the repo's CI runs it — `pyproject.toml` configures
  `mypy` over `src` and `tests`, and `pipeline_prepare.py` is a new typed module.
- `herdr-routines validate` on the pipeline job, plus one manual dry run of `pipeline-prepare`
  against a throwaway `REPO_PARENT` with a fake report path, confirming it writes `state.json` at
  `$WT/state.json` with the expected keys and exits 0.
- Run the modified `scripts/pipeline-launch.sh` once by hand against a real parent clone and read
  `/tmp/pipeline_launch_<run_id>.log` to confirm prepare's log lines landed in the log (not in the
  prompt) and the header carries the prepared values.
- Night after merge: confirm one *empty-backlog* run records `skipped` in `history.jsonl` and sends
  no Telegram notification. That is the behaviour 052 wanted, observed rather than asserted.

## Changelog v1→v2

Stage 2 (independent spec review) over v1. No change to the scope decision, the architecture, or the
phase split — the reviewer's verdict on v1's recommendation is **agreed: phase A only in this PR**,
and the `## Scope` section was rewritten to be unambiguous about that (v1 argued the case well but
left "phases B and C are out of scope" as a recommendation a reader could still overrule; v2 states
the in-PR list of six items and the explicit not-in-PR list).

**Factual corrections to v1 (all verified against the code; each was wrong):**

1. **The reclaim notices are not stderr-only.** `run_pick_feature` already accepts
   `notify: Callable[[ReclaimedPick], None]` (`pick_feature.py:439`) and `tests/test_pick_feature.py:370-405`
   already pins it. v1 used "they go to stderr" as a reason for the `PickResult` refactor; the real
   reason is narrower and is only about `feature_source`. Approach §3 rewritten.
2. **`HerdrClient` cannot do what prepare needs.** v1 said prepare would parse
   `.result.worktree.path` / `.result.branch` "exactly as the prompt does" and listed `herdr.py`
   under *deliberately NOT touched*. `worktree_create` (`herdr.py:261-279`) returns only the root
   pane id, there is no `workspace_create` or `workspace_list` in `src/`, and `--env` appears
   nowhere in `src/` (only `pipeline-launch.sh:127`). New section §3b specifies the two methods and
   a `FakeRunner` test each; `herdr.py` moved from NOT-touched to Modified. This is the single most
   consequential correction — v1's approach was not implementable as written.
3. **The launcher tests have no `uv` stub.** v1 proposed a `uv run … pipeline-prepare` call in
   `pipeline-launch.sh` and only said "new prepare-wiring tests". `tests/test_pipeline_launch_sh.py`
   runs the real script with a hardcoded `PATH` (`pipeline-launch.sh:105`) and a fake `herdr` at
   `$HOME/.local/bin/herdr`; a real `uv run` would sync a repo, read `docs/process/issues/`, and
   possibly create a worktree during unit tests. Added as a blocking prerequisite (approach §6,
   risk 3).
4. **`PREPARE_OUT=$(… 2>&1)` was wrong.** The launcher already redirects to `$LOG`
   (`pipeline-launch.sh:106-107`); merging stderr in would splice log text into the `KEY=VALUE`
   stream appended to the orchestrator prompt. Capture stdout only.
5. **Two line citations were off**: the stage-independence downgrade is `tick.py:1772-1781` (not
   `1771-1783`), the "no report yet is not a failure" branch is `tick.py:1844-1849` (not
   `1844-1847`), and the `HERDR_ENV` design reference is `design.md:137-139` (not `design:98`).
6. **The justification for the double sync was wrong.** v1 said the dispatch sync "happens minutes
   before `systemd-run` registers the unit" — it happens in the same tick, immediately before
   (`tick.py:1991` then `:2022`). The real reason to sync twice is that the dispatch sync runs in
   tick's process on tick's checkout while the branch-off runs later in a different process on the
   auto-synced job repo's checkout (`pipeline-launch.sh:82-84`).

**Additions:** `## Acceptance criteria` (14 numbered items, each ending `Test: <exact function
name>`, drawn from the existing `tests/` layout and naming conventions — `test_pipeline_*` in
`tests/test_tick.py`, `test_herdr.py`'s `FakeRunner` argv tests, `test_sync_repo.py`'s real
bare-origin git fixtures); `## Review tiers` (per-criterion `blocking`/`non-blocking` severity plus
`confidence: high|medium` per item, and the reviewer's own per-finding confidence); this changelog.
Every acceptance test name maps to a file that exists today or to a seam the approach explicitly
introduces — none is aspirational.

**Kept from v1 unchanged:** the problem statement and its three incident citations, the phase
table and the 4-row/6-row/1-row split of issue 054's step table, the `PrepareResult` shape, the
atomic-write and `state.json` key-set argument, the two-part tick change, the prompt-trim plan
including the `HERDR_ROUTINES_SUBCOMMANDS` narrowing, and the issue-bookkeeping requirement.
