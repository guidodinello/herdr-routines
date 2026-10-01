# Pipeline Orchestrator — Prompt (retired)

**This prompt is no longer executed.** Issue 056 moved the overnight pipeline's stage
loop out of prose and into code. `scripts/pipeline-launch.sh` no longer starts an
orchestrator agent; it runs the pre-flight (`pipeline-prepare`) and then hands the run to
`herdr-routines pipeline-run` (`src/herdr_routines/pipeline_run.py`).

Nothing below is a live instruction. It is kept because the design rationale in
`design.md`, the audits under `docs/process/audits/` and the issue files all cite this
document by line number, and because it is the clearest statement of *what the loop is
for* — which is the thing that has to survive the port.

## What moved where

| was here | is now |
| --- | --- |
| the worker-spawn template (`herdr agent start` … `--wait`) | `pipeline_run._run_agent_stage` + `wait_loop.prompt_with_watchdog` |
| "Stage Details", stages 1–6 | `docs/pipeline/stages/stage-{1,2,3,5,6}.md`, invoked from `pipeline_stages.STAGES` |
| the stage table (model, prompt file, layout) | `pipeline_stages.STAGES` |
| the prose gates 1–5 | `gates.run_stage_gate`, invoked per stage by `pipeline_run` |
| "record `agent_session.value` in `state.json`" | `pipeline_run` writes `state_sessions` on every poll |
| deadline, quota-marker and resume handling | `pipeline_run.run_pipeline` (deadline between stages, `wait_loop` quota markers, `herdr.agent_start(session_id=…)` resume for stage 6) |
| pane close per stage | `pipeline_run`, immediately after the stage's gate passes |
| "always write `$PIPELINE_REPORT`" | `pipeline_run` writes the terminal report on every path; the launcher stubs one only if the run was killed first |
| overlap guard on the `rt-<name>` agent | `tick._pipeline_run_is_live`, on an in-flight `state.json` |

The gates were the reason to do this. Prose gates were interpreted and relaxed — gate 5 was
explicitly relaxed once already (`design.md:241`) because the skill never emitted a
literal `confidence:` token — and a ported gate that is not itself under test just moves
the relaxation somewhere harder to see.

## Why the stage prompts are separate files

Each stage's *instruction to the model* is still prose, because that is what a model reads.
Only the machinery around it — when to start, what to name the session, when to consider
the stage finished, which gate decides whether to continue — is code. The stage prompt
files are substituted with values read from `state.json` at run time; they never compute
anything about the run themselves.

## First manual run checklist (still applies)

1. `~/.config/opencode/opencode.json` allowlist (source of truth:
   `deploy/opencode.pipeline.json`, see [`setup.md`](setup.md) §4) plus a valid `GH_TOKEN`
   on the host. Both are now hard requirements: stage 4 pushes and opens the PR with no
   model in the loop (issue 056 risk 4).
2. Keep the dogfood feature trivial to bound blast radius (`design.md:257`).

---

Historical model table (from the v1 prompt, for reference):
`1 muse-spark plan/spec / 2 muse-spark fresh spec review / 3 ox-alpha-free implement /
5 big-pickle primary / 6 ox fixes + muse GH ops`
(`opencode-e2e-workflow-recommendations.md:13`). These remain in `pipeline_stages.STAGES`.