# Overnight Feature Pipeline — Orchestrator Spec (POC)

Status: proposed (2026-08-23). Not implemented. POC scope — free models only,
quota explicitly out of scope (upgrade to paid tier if the idea proves out).

## Vision

Define an agentic feature end-to-end while asleep: one feature goes from rough
idea to reviewed PR overnight, through a chain of independent agent sessions —
each stage a fresh context, each handoff a file artifact, every transition
gated on machine-checkable results instead of agent self-report.

## Core architectural decision

A **single orchestrator agent session** is instructed with this workflow and
spawns the required worker sessions itself using Herdr (panes/workspaces via
the herdr CLI — herdr-routines' `runner.py` already proves Herdr can be driven
programmatically with settle-detection).

Why an agentic orchestrator instead of a hardcoded driver script: the
orchestrator can *reason* between stages — rewrite a prompt after a weak spec
review, decide a stage needs a retry vs. abort, summarize where things stand.
That judgment is exactly where we want the intelligence spent, and it is the
part a bash script cannot do.

Tradeoffs accepted for the POC:

- The orchestrator itself burns context/quota across the whole night.
- If the orchestrator dies mid-pipeline, the run halts — mitigated by
  checkpointing (below), not by pretending it can't happen.
- Worker stages still use plain herdr sessions; herdr-routines cron stays out
  of the pipeline entirely (this is not a scheduled routine, it's a launched
  one-shot run).

## Stage graph

| # | Stage | Harness/model (POC default) | Input | Output | Gate to proceed |
|---|-------|------------------------------|-------|--------|-----------------|
| 1 | Plan + draft spec | claude session | feature idea (one paragraph from the human) | `spec.md` v1 (problem, approach, files touched, risks) | file exists, settles ok |
| 2 | Spec review + update | opencode session (different model than #1) | spec v1 | spec v2 **including "Acceptance criteria & test plan"**: numbered criteria, each mapped to ≥1 named test | reviewer posted updated spec + change notes |
| 3 | Implement | opencode session | spec v2 | branch commits implementing the feature **and authoring all tests from the acceptance section** | all spec-derived tests pass locally (run them, don't trust the report) |
| 4 | Open PR | same session as #3 | branch | PR against base | PR exists |
| 5 | Code review | separate session (code-review skill pattern) | PR number | posted review | review posted (blocking findings allowed) |
| 6 | Address comments | implementer-context session | review findings | fixes pushed, replies posted | no unresolved blocking findings |

Stage rules:

- **Tests before code**: stage 3's definition-of-done is "every test listed in
  spec v2's acceptance section exists and passes", not "feels finished".
- **Comment-addressal loop is capped**: max 2 iterations, plus a
  wait-for-comments timeout. After the cap the orchestrator writes a summary
  of what remains and stops. This is where the orchestrator earns its keep —
  deciding "addressable finding vs. needs-human" per comment.
- **Human gate stays at merge.** Nothing merges unattended.

## Failure semantics

- Any stage failing its gate → pipeline stops. Never open a PR from a broken
  upstream stage (no PR off a failed spec, no "address comments" off a failed
  review).
- Orchestrator writes a `$PIPELINE_REPORT` (same pattern as
  `$ROUTINE_REPORT`) at the end regardless of outcome: stage-by-stage status,
  artifacts produced, where it stopped and why.
- Checkpointing: orchestrator maintains a small state file (`state.json`:
  current stage, PR number, artifact paths) updated after every stage
  transition, so a relaunched run can resume instead of restarting. POC may
  fake this with convention over code — acceptable.

## Handoff contract

All inter-stage artifacts live in the repo worktree (committed or not):
`spec.md`, the state file, the report. Every worker prompt names its input
files explicitly — workers never rely on prior-session context, only on disk.

## Relationship to existing pieces

- Generalizes roadmap §3's pending "fitted-implementer-with-review as a
  scripted pane/agent pair" item — that becomes one instance of this shape.
- Stage 5 reuses the code-review skill pattern (independent reviewers,
  confidence scoring, blocking/non-blocking tiers).
- Stages mirror fitted's `.claude/skills/feature-workflow` phases, but split
  across sessions/models instead of subagents inside one session.

## Open questions (resolve at build time)

1. Which repo hosts the POC feature? (herdr-routines itself is the natural
   dogfood target; fitted is the stress test.)
2. Orchestrator→worker driving: mechanically solved — the same herdr CLI calls
   runner.py uses are available to an agent. What must be *ported* (not
   rediscovered) is runner.py's hard-won failure handling: start-race prompt
   retry (#15), settle-vs-stuck-retrying disambiguation (quota-retry loops
   never settle — memory #1042), and a timeout backstop per worker. Also:
   orchestrator wait hygiene (coarse `sleep`-based polling, not per-minute
   tool calls — a 40-min review would otherwise eat the orchestrator's own
   context), plus HERDR_ENV/config so the orchestrator is allowed to drive
   herdr at all.
3. Does the orchestrator run as a herdr session itself (so it's watchable in
   the TUI) or bare opencode? Lean herdr session.
4. Model assignments per stage for the POC (only constraint: stage 1 ≠
   stage 2 harness, for genuine independence).
5. Where does the pipeline launcher live — manual SSH one-liner (transient
   systemd timer, see roadmap §3 one-shot options) or a `herdr-routines run`
   job with the orchestrator prompt?

## Non-goals for POC

- Generic DAG/pipeline config format in herdr-routines. Hardcode the stages;
  promote to config only after the pattern survives a few real runs.
- Parallel stages (sequenced back-to-back instead — same wall-clock cost
  overnight, strictly simpler).
- Unattended merging, auto-retry loops without caps, multi-feature pipelines.
