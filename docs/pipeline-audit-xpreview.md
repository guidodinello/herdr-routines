# Pipeline POC — Audit Report

Status: audit (2026-08-23). Reviewer: x-preview. Scope: `docs/feature-pipeline-orchestrator-spec.md`
("spec"), `docs/pipeline-poc-design.md` ("design"), against `docs/failure-reaping.md` ("reaping"),
`ROADMAP.md`, `docs/plan-v1.md` ("plan"). Read-only audit; no spec/design edits.

Abbreviations above are used for citations (`design:171` = design line 171).

## Summary

The design is mechanically well-grounded — it ports runner.py's hard-won spawn/settle handling
(EmptyResponse retry, quota-vs-settle disambiguation, per-worker timeouts, empirically verified
settle mapping) and its POC scoping (hardcoded stages, no cron, no gate code) is appropriately
lean. The load-bearing problems are elsewhere: (1) the handoff contract says artifacts live in
"the repo worktree" while the workspace model gives every worker its **own** worktree — untracked
files don't cross worktree boundaries, so the very first handoff (spec.md, stage 1 → 2) breaks as
specified; (2) four of six gates have no machine-checkable form, so an orchestrator-judged design
leans hardest on exactly the judgments LLMs fail silently at (structural completeness, absence
checks); and (3) the unattended-run plumbing — permission allowlist breadth, quota classification,
whole-night deadline, resume collision handling — is thinner than this project's own incident
history (reaping §1) warrants.

## Gaps / Risks

1. **HIGH — Handoff contract contradicts the per-worker worktree model.**
   `spec:69-73` / `design:68-75` put all artifacts in "the repo worktree", but `design:171-175`
   has every worker run `herdr worktree create --cwd <repo> --base main` — a *separate* worktree
   directory per stage (under `~/.herdr/worktrees/…`, cf. reaping:182). Git worktrees share the
   object store, **not** untracked files: `spec.md` written uncommitted in stage 1's worktree is
   invisible to stage 2's freshly-created worktree, and committing it to an `auto/pipeline-<ts>`
   branch doesn't help because stage 2 branches off `main`. Branch topology is undefined (one
   shared branch? one per stage?). The first manual run breaks at the stage-1→2 handoff.
   *(Note: this is the audit's only speculative-mechanism claim; verified behavior is standard
   git — worth a 2-minute empirical confirm at build time.)*

2. **HIGH — Gates 2, 3 (partially), 5, 6 are not machine-checkable; silent judge failure lives
   here.** `design:120-131` lists verification commands for stages 1, 3, 4 — but:
   - Stage 2 "reviewer posted updated spec + change notes" (`design:58`, `spec:40`): "posted"
     *where*? No location is defined for change notes, and nothing checks the defining property
     of spec v2 — numbered acceptance criteria, each mapped to ≥1 named test (`spec:40`,
     `design:58`). An LLM asked "is this spec reviewed?" says yes.
   - Stage 3 "tests pass locally" is **insufficient**: a green suite proves nothing about
     acceptance-criteria coverage. `design:64-65` states the right definition ("every acceptance
     test exists and passes") but no command verifies *existence* of the named tests. Without a
     criterion→test artifact, nobody — human or orchestrator — can distinguish "feature done"
     from "suite passes, feature missing".
   - Stage 5 "review posted (blocking allowed)" (`design:61`) accepts any text blob; the
     structured confidence/blocking tiers the stage exists to produce (`spec:80`) go unchecked,
     leaving stage 6 nothing structured to act on.
   - Stage 6 "no unresolved blocking findings" (`design:62`, `spec:44`) depends on GitHub
     thread-resolution state — but nobody is specified to *resolve* threads (worker? reviewer?
     human?). If unresolved-by-default, the gate can never pass; if self-resolved by the
     implementer, the gate is theater.

3. **HIGH — Unattended permission surface is under-specified.** `design:83-87` handles
   `HERDR_ENV=1` and `permission.external_directory` only. The orchestrator and stage-3/4/6
   workers will invoke `herdr`, `git`, `gh`, `uv run pytest` — none of which are described as
   allowlisted. The first un-allowlisted call becomes a **blocked agent at 03:00**, the exact
   silent-wedge class from reaping §1 and `roadmap:54-61`. Also unstated: *how* HERDR_ENV reaches
   the orchestrator agent (natural channel: `herdr workspace create --env K=V`, `plan:63`), and
   that the opencode.json allowlist must exist on the Pi, not just the laptop.

4. **HIGH (accepted-risk, but must be surfaced) — the orchestrator runs big-pickle**, the model
   that died twice of quota exhaustion (reaping:8-9), in the longest-running, most
   context-hungry role, while quota is explicitly out of scope (`design:3`, `spec:3-4`). Worker
   quota death is handled (timeout backstop, `design:109-112`). Orchestrator quota death is
   worse: the orchestrator is the *writer* of `$PIPELINE_REPORT`, so its own death produces no
   report, no notification, nothing but absence in the morning.

5. **MEDIUM — No whole-pipeline wall-clock budget.** Per-worker timeouts only (`design:109-112`);
   worst case ≈ 6 stages × 90 min + 2 addressal loops > 10 h, past any overnight window. Since
   `$PIPELINE_REPORT` is written "at end regardless" (`design:93-94`), an overrunning night
   yields *no report at all*.

6. **MEDIUM — Polling choice contradicts its own hygiene goal and ignores `--wait`.**
   `design:88-91` picks 30–60 s `herdr agent get` polling — over a 39-min review (its own tuning
   figure, `design:110`) that's ~50–80 tool calls *per stage*, each burning orchestrator context:
   the exact burn `spec:93-95` warns against. runner.py's actual primitive is blocking
   `agent prompt --wait --timeout` (`plan:65`), which is one call per stage. If the orchestrator's
   bash tool can't block 40–90 min, a detached (`nohup … &`) `--wait` writing a result file,
   polled sparsely, dominates `agent get` polling. The design neither chooses nor rejects this.

7. **MEDIUM — Resume is asserted, not specified.** `spec:64-67` ("POC may fake this with
   convention"), `design:92-95` defines state.json contents but not the resume *procedure*.
   Crash between worker-spawn and state-write ⇒ relaunch spawns a duplicate whose
   unique-among-live-agents name collides (`plan:72`) and fails confusingly. Resume needs:
   read state.json → `herdr agent list` reconcile → adopt-or-reap stragglers.

8. **MEDIUM — Stage 6 session semantics ambiguous.** "Implementer-context session" (`spec:44`,
   `design:62`): the *same* agent as stages 3–4 (context preserved — but possibly burned after a
   90-min implement), or a fresh session fed diff+findings? This changes the worker prompt, the
   gate inputs, and the agent-name plan (`design:117`).

9. **MEDIUM — `retry_stage` vs stop-on-failure is an unresolved contradiction.** The
   orchestrator's decision space is `proceed | retry_stage | abort` (`design:125`), but failure
   semantics say any gate miss stops the pipeline (`spec:57-59`, `design:132-134`). Which
   failures are retryable (flaky test run? transient spawn failure?) is left to orchestrator
   improvisation at 03:00.

10. **LOW/MEDIUM — Blast radius is real and self-referential.** Worktree isolation shares the
    object store and can push (`roadmap:58-61`); stage 4 *requires* push. Dogfood target is
    herdr-routines itself (`design:166-167`) — the repo containing the scheduler that runs
    everything else. The stage 1→2→3 chain is a prompt-to-code-execution path on the Pi. This is
    consistent with the existing single-user threat model, but the design should say so in one
    sentence and lean on the trivial-first-feature rule (build order step 3, `design:202-203`).

11. **LOW — Launcher sketches omit prerequisites.** `herdr agent start` requires an existing
    shell pane and never creates layout (`plan:70-71`); option A's `--pane <id>` and option B's
    `<pane_id>` hide workspace-create + `.result.root_pane.pane_id` parsing; option A's
    `-- prompt.md` mixes start-args with the prompt flow (prompts go via `agent prompt`).
    Mechanically proven by runner.py, but the sketch misleads whoever types it cold. Also
    `herdr workspace create --cwd <repo-parent>` (`design:79`) puts the orchestrator's cwd
    *outside* the repo — all its commands need explicit paths.

12. **LOW — No pane/workspace lifecycle or cleanup.** Nothing closes the 6+ worker panes or
    workspaces after the run; leaked live agents were the central incident of reaping §1, and
    pane retention is explicitly punted (`roadmap:73-76`). Cleanup must also be ordered:
    artifacts out (committed / copied to report paths) *before* any workspace close.

13. **LOW — Small undefineds:** wait-for-comments timeout has no value (`spec:51`); stage 5's
    full code-review skill is multi-agent (5 reviewers) and may strain a single worker session;
    "change notes" file location (see gap 2); state.json schema deferred to build (`design:201`)
    — fine, but it's the resume keystone, not a detail.

14. **LOW — Scheduler interaction: correctly excluded, residuals are benign.** Tick/cron stay
    out (`design:17-18`, `roadmap:36`). Remaining interactions: agent-name namespaces are cleanly
    disjoint (`pl-` vs `rt-`); Pi resource contention (orchestrator + worker concurrently with
    scheduled tick jobs) is plausible but acceptable at current scale (`plan:318-323`). Note-only.

## Improvements / Recommendations

### v1 — do before the first overnight run

1. **Fix branch topology (kills gap 1).** Orchestrator creates *one* worktree + branch
   (`auto/pipeline-<run_id>`) before stage 1; all stages attach to the *same* worktree path;
   stage 1 commits `spec.md` before settling; stage 3 continues on that branch; stage 4 PRs it.
   Independence comes from fresh sessions (contexts), not fresh worktrees — update
   `design:171-175` and the handoff contract accordingly. Cheapest correct shape for v1.
2. **Make gates 2/3/6 command-checkable (gap 2).**
   - Pin stage 2's output format: spec v2 must contain a `## Acceptance criteria` section of
     numbered items, each ending `Test: <name>`; change notes appended as a v1→v2 changelog
     section *inside spec.md*. Orchestrator verifies by grep/count, not judgment.
   - Stage 3 gate: for each named test — file/symbol exists (`rg`) — **and** `uv run pytest -q`
     passes. Existence first, green second.
   - Stage 6 gate: either query unresolved threads via `gh api` (and instruct the worker to
     resolve threads it has addressed), or redefine the gate as "a reply exists on every
     blocking finding" — self-contained and checkable via comments JSON.
   - Stage 5 gate: review body must contain the skill's blocking/non-blocking structure.
3. **Enumerate the unattended allowlist now (gap 3):** opencode.json permission entries for
   `herdr`, `git`, `gh`, `uv`/pytest + `external_directory` for socket/state dirs; inject
   `HERDR_ENV=1` via `workspace create --env`; replicate the config on the Pi.
4. **Add a hard pipeline deadline** (e.g. launch + 7 h): finish current stage, write a partial
   `$PIPELINE_REPORT`, fire `herdr notification show --sound request`. One paragraph in the
   orchestrator prompt (closes gap 5).
5. **Port the quota-marker scan** (reaping:88 `DEFAULT_FAILURE_MARKERS`) into the orchestrator's
   timeout handler: on settle-timeout, one visible-screen read; marker matched ⇒ report
   `quota_exhausted` instead of a bare timeout (closes the morning-postmortem repeat of 08-23).
   Accept orchestrator self-death explicitly: systemd unit failure + "no report file ⇒
   investigate" belongs in the morning checklist (closes gap 4's operational half).
6. **Replace hot polling with detached `--wait`:** `nohup herdr agent prompt <worker> --wait
   --timeout X > <stage>.result.json 2>&1 &`, then sleep in 3–5 min chunks checking for the
   result file; fall back to `agent get` only near the timeout. ~10× fewer orchestrator cycles
   than 30–60 s `agent get` (closes gap 6).
7. **Write the resume recipe + retry policy into the orchestrator prompt** (gaps 7, 9):
   resume = state.json + `herdr agent list` reconcile; `retry_stage` permitted *only* for
   start-race EmptyResponse (once — already specced, `design:102-105`) and infra-flavored test
   failures (max 1); every gate-content failure stops the pipeline, matching `spec:57-59`.
8. **Name stage 6's session:** recommended: same agent as stages 3–4, prompted with a findings
   digest + `gh pr view --comments` (keeps code context, honors "implementer-context"); update
   `design:62` (gap 8).
9. **Add a cleanup step:** after `$PIPELINE_REPORT` is written and artifacts are committed/
   copied out, close worker workspaces; keep the PR branch (aligns with the manual-GC stance,
   `roadmap:77-79`) (gap 12).

### v2 / deliberately deferred (agree with the design's deferrals)

- `verify-gate.sh` (Option B, `design:136-139`) — revisit after runs show what the orchestrator
  actually misses; recommendation 2 preempts most of its value at near-zero cost.
- Provider/model switching on quota (Parking Lot, `roadmap:126-129`); paid tier per `spec:4`.
- `workflows/pipeline.yaml` promotion after the pattern survives real runs (`design:46-50`).
- Phone-approval for blocked workers (`roadmap:54-57`) — becomes relevant the first time a
  pipeline agent blocks despite the allowlist.

## Open questions

1. Who resolves GitHub review threads — stage-6 worker via `gh api`, the stage-5 reviewer, or
   the human at merge? Determines stage 6's checkable gate form.
2. Is a shared worktree/branch acceptable for "independence" (sessions independent, tree
   shared)? Any reason stage 2 must *not* see stage 1's committed-but-unmerged spec?
3. Can opencode's bash tool block for tens of minutes (making plain `--wait` viable without
   detaching)? Empirical check at build time; decides recommendation 6's exact shape.
4. What is the actual latest-safe finish time overnight? Sets the deadline in recommendation 4.
5. Stage 5 reviewer harness: `opencode/code-review` vs `claude` (`design:170`) — decide after
   the first dogfood run, as planned?
6. Should the very first pipeline run target a throwaway repo rather than herdr-routines, to
   bound gap 10's blast radius while the orchestrator prompt is still green?

## Verdict

Not ready to build today — close, and no new code is needed to get there. Three blockers, all
design/prompt edits: **(1)** resolve the handoff-vs-worktree contradiction with an explicit
single-branch topology; **(2)** pin machine-checkable forms and file locations for the stage
2/3/5/6 gates (acceptance-criteria→test mapping; thread-resolution semantics); **(3)** enumerate
the unattended permission/command allowlist and HERDR_ENV injection so an overnight run cannot
wedge on a permission prompt. With those fixed, the remaining findings are additive v1
hardening (pipeline deadline, quota-marker scan, detached `--wait`, resume recipe, cleanup) and
the POC is worth running — its failure-handling port from runner.py is genuinely the strong part
of this plan.
