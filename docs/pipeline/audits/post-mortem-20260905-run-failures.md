# Autopipeline post-mortem — 2026-09-05

> **Status (2026-09-06):** all four findings were filed as issues 034-037 (PR #82).
> Two have shipped: **036** (PR #85) and **037** (PR #83). **034** and **035** remain
> open pending a decision on whether the pipeline's gates stay as prompt prose in
> `docs/pipeline/orchestrator-prompt.md` or are extracted into testable code.
> The PR this document diagnoses, #81, was merged after the review thread was
> answered and `ruff format` was fixed by hand.
>
> This is kept as the historical record of how the four defects were found. The
> "Recommended fixes" section below is preserved as written at the time; the issues
> themselves carry the designs that were actually implemented.

Scope: why PR #81 sits with failing CI and an unanswered review comment, and why
the 2026-09-05 run produced nothing. Evidence gathered on the Pi + GitHub.

## TL;DR

Four independent defects. Nothing "hung" — every stage reported success against a
gate that doesn't measure the thing the user cares about, and the one safety net
that would have caught it is structurally incapable of running on a pipeline PR.

---

## 1. CI is never a gate anywhere in the pipeline (why PR #81 is red)

`Lint Python` fails on PR #81: `ruff format --check .` → *4 files would be
reformatted, 37 already formatted*.

- `origin/main` is clean (`39 files already formatted`) — the pipeline introduced this.
- Stage 3's gate runs `uv run pytest -q` only. No `ruff format`, no `ruff check`.
- Stage 4's gate checks "PR exists on the right branch" + "issue flip committed".
- **No stage in `docs/pipeline/orchestrator-prompt.md` ever calls `gh pr checks`.**

The report's proud "428 tests green" is true and irrelevant: tests were never the
failing check. The pipeline is blind to CI by construction.

Note: MEMORY.md's `ci_lint_vs_precommit_gap` note is stale — `.github/workflows`
runs BOTH `ruff format --check .` and `ruff check .`.

## 2. Stage 6 ran, gated itself out, and reported success (why comments are unaddressed)

Stage 6 did spawn — heartbeat `/tmp/pipeline_resume_20260904T050000Z.log` has
`stage 6 poll 05:27:28Z`, and `state.json.stage_sessions["6"]` resumed pl-3's
session via `-s`. There is no `/tmp/pl-6-*.result.json`, so whether it ever
submitted a prompt is unknown. What is observable:

- PR head is still `234f79f` — the exact commit stage 5 reviewed. Zero commits after review.
- The one open review thread (a correct `[non-blocking]` note: `du` glob args in
  `runner.py:89-94` never expand without `shell=True`) has **no reply**.
- The report nevertheless says *"address-pr-comments handled"* and `## Outcome: ok`.

Root cause is the gate, not the worker. Gate 6 measures *"no unresolved thread whose
body matches blocking"*:

- The prompt's literal jq is `test("blocking")`. In jq that matches `"non-blocking"`
  too (substring), so the literal gate would have **failed** here.
- The orchestrator substituted a stricter `\[blocking\]` regex, which excludes
  `[non-blocking]` → 0 → PASS.

The stricter regex is arguably the sane intent. **The real defect is that neither
variant measures what the `address-pr-comments` skill contract actually promises:
"replies to every thread with the outcome."** A PR can pass gate 6 with every
thread untouched, as long as none is tagged `[blocking]`.

Either way — worker did nothing, or worker ran and correctly found no `[blocking]`
item — the gate lets the PR through with an unanswered thread. That is the defect.

## 3. babysit-prs (the safety net) cannot ever fix a pipeline PR — CONFIRMED

`babysit-prs` runs `*/10` and correctly flagged PR #81 as
`eligible_reason: unresolved_threads`. It dispatched 3 times (05:30 / 05:40 / 05:50
on 09-04), all `state: failed`, all with `pane_id: null` — i.e. it died *before*
creating a pane. Then `max_attempts_per_target: 3` tripped and it has reported
`Skipped (attempts): 1` on every tick since. It is still doing so right now.

Cause, reproduced live on the Pi today:

    $ git worktree add /tmp/probe-autofix-pr81 auto/pipeline-20260904T050000Z
    fatal: 'auto/pipeline-20260904T050000Z' is already used by worktree at
           '/home/guido/.herdr/worktrees/herdr-routines/auto-pipeline-20260904t050000z'

Caveat on attribution: because the history record drops `reason` (§3b below),
the three 09-04 attempts cannot be *confirmed* as this specific failure —
`repo_sync_failed` was also firing intermittently that night and produces the same
`pane_id: null`. The collision below is permanent regardless, so the conclusion
holds either way.

`tick._dispatch_fix_worker` (tick.py:984-1006) does
`git worktree add .worktrees/autofix-pr<n> <pr.head_ref>`. The orchestrator prompt
deliberately **keeps** its `auto/pipeline-<run_id>` worktree at end of run
(G-10/G-14: "do not `herdr worktree remove`"). Git refuses to check out a branch
already checked out elsewhere. So: **every pipeline PR is permanently un-babysittable
by design.** The two features contradict each other.

There are 20+ retained `auto/pipeline-*` worktrees on the Pi, so this is not a
one-off collision.

### 3b. Observability bug that hid this

`_dispatch_fix_worker` returns `{"state","reason","error",...}` but the history
record built at tick.py:411-422 copies only `pane_id`/`report_path`/
`report_written`/`final_agent_status`. **`reason` and `error` are dropped.** The
tick-level log line prints `done (dispatched=…)` regardless. That is why
`history.jsonl` shows three bare `state: failed` with no cause, and why this took
a live repro to find.

### 3c. Agent-name bugs (adjacent, cosmetic-to-real)

`auto_fix.build_worker_agent_name` truncates to 32 chars:

    "rt-babysit-prs-pr81-babysit-prs-20260904T053000Z"[:32]
      == "rt-babysit-prs-pr81-babysit-prs-"

The run_id — the only part that makes the name unique per attempt — is truncated
away entirely, and the job name appears twice (run_id is itself `<job>-<ts>`).
Separately, the liveness guard at tick.py:382 checks `build_pr_agent_name` →
`rt-babysit-prs-pr81`, a name that is **never created**. So the double-dispatch
guard (review finding E) never fires.

## 4. The 2026-09-05 run died at second zero and burned 8h of "in flight"

- `/tmp/pipeline_launch_20260905T050000Z.log`: `agent_prompted` returned with
  `"agent_status":"blocked"`.
- `pipeline-launch.sh` **does not inspect the settle status** — it logged
  `wait exited status=0` and fell through.
- The `cleanup` EXIT trap then closed the orchestrator pane, **destroying the
  visible-screen evidence of why it was blocked.** No `herdr-server.log` on the box.
- Consequence: no worktree, no `state.json`, no heartbeat, no report.
- `tick` kept logging `nightly-pipeline: in flight` every 5 min from 05:00 to
  13:05Z, then reconciled `failed (reason: no_report)`. Eight hours believing a
  process that was dead in seconds.

Issue 033 ("capture diagnostic tail on blocked settle", merged as 11cfb9c) added
exactly this diagnostic — **to `runner.py`, for routine jobs.** The pipeline
launcher is a separate path and never got it.

## Recommended fixes, in dependency order

1. **`pipeline-launch.sh`**: check the settle status of `herdr agent prompt --wait`;
   on `blocked`/`unknown`, `herdr agent read --source visible --lines 200` into the
   log *before* the cleanup trap closes the pane, and write a
   `## Outcome: failed` stub report so `tick` reconciles in minutes, not 8 hours.
   (Port issue 033's behaviour to this path.)
2. **Add a CI gate.** New gate after stage 4: poll `gh pr checks <n> --watch` (or
   `gh pr view --json statusCheckRollup`) until non-pending; any FAILURE routes to
   stage 6 as a must-fix. Cheap partial: add `uv run ruff format --check . && uv
   run ruff check .` to stage 3's gate alongside pytest.
3. **Redefine gate 6 as reply-coverage**, not blocking-count: every thread with
   `isResolved == false` must have >1 comment (i.e. got a reply). Keep the
   `[blocking]`-must-be-fixed check as an additional condition, not the only one.
4. **Unblock babysit-prs on pipeline PRs**: in `_dispatch_fix_worker`, detect that
   `pr.head_ref` is already checked out (`git worktree list --porcelain`) and reuse
   that worktree instead of `worktree add`. Also propagate `reason`/`error` into the
   history record, fix the two agent-name builders, and reset PR #81's attempt
   counter once fixed.

## Immediate manual unblock for PR #81

CI lint + the one review reply are both ~5 minutes of work by hand. Nothing on the
Pi will do it: babysit-prs is capped out and structurally blocked. Note the attempt
counter stays tripped even after CI goes green — reset it or just merge manually.

## Other live state on the Pi worth knowing

- Two stale agents still resident: `pipeline-orchestrator-nightly` (w2F, idle) and
  `pl-5-20260831t050020z` (w2Y, idle) — leftovers from earlier runs.
- 20+ retained `auto/pipeline-*` / `auto/herdr-pr-*` worktrees under
  `~/.herdr/worktrees/herdr-routines/`.
- Parent clone `~/.local/state/herdr-routines/repos/herdr-routines` is dirty:
  `027-tmp-hygiene.md` and `028-pick-feature-skip-open-prs.md` carry uncommitted
  `status: in-progress` flips from `pick-feature --mark-in-progress` on runs whose
  PRs never merged. `sync-repo` currently tolerates this (verified exit 0).
