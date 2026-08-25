# spec: pane-lifecycle v2 — close-and-resume (20260825T021919Z)

## Problem

The current pipeline keeps every worker's opencode process resident for the entire run and only cleans up once at the very end (`design.md:159-168`, `orchestrator-prompt.md:118`). By stage 4 of `20260825T000735Z` there were 5 concurrent opencode processes (orchestrator + `pl-1`/`pl-2`/`pl-3` + stage-4 worker), each 400–870 MB RSS, and swap on the 4 GB Pi was exhausted to ~112 KB free (`docs/pipeline/pane-lifecycle-v2-proposal.md:17-25`). Only `pl-3` is ever reused (stage 4 and stage 6 reuse it for code context, `design.md:204` G-11); `pl-1`, `pl-2`, and `pl-5` are never touched again after their gate passes, so the "keep everything alive" policy buys nothing for most workers. Verified behavior across PR #27 (`20260823T234906Z`), PR #28 (`20260824T232136Z`), and PR #29 confirms this is structural, not a single-run fluke.

The proposal doc (`docs/pipeline/pane-lifecycle-v2-proposal.md`) already describes a close-and-resume fix but leaves open questions 1–3 unresolved. Manual verification on 2026-08-25 confirmed open question 1: `herdr agent start ... -- -m <model> -s <session_id>` against a fresh pane correctly resumes the original opencode session (`agent_session.value` matches), so `-s <session_id>` is a true resume, not a fork.

## Approach

Amend the two pipeline authority docs (not `src/` — Option C deferred) per `docs/pipeline/pane-lifecycle-v2-proposal.md`:

- `docs/pipeline/design.md` — in the Cleanup section and Worker-spawning / Handoff contract, document (a) per-stage pane close: after a stage's gate passes and the handoff (`git commit` + `state.json` atomic update) is confirmed on disk, close that worker's pane (`herdr pane close` / `herdr tab close`) immediately, for every worker except the one a later stage reuses; (b) close-then-resume-by-session-id for the reused worker (`pl-3`, currently reused by stage 6): close its pane too after stage 4, but when stage 6 needs it, reopen by `herdr agent start pl-6-<run_id> --kind opencode --pane <fresh_pane> -- -m <model> -s <session_id>` where `<session_id>` is that worker's `agent_session.value` captured in `state.json`/history — instead of keeping the pane open idle from stage 3 through stage 6. Add a new G-number (next available, G-16) for this change and keep existing G-1..G-15 intact. Note the verified 2026-08-25 `-s <session_id>` resume result as settled, and note open questions 2 (grace window) and 3 (cross-model `-s`) as still-open if not resolved, with the decision recorded in `design.md` (immediate close vs grace).

- `docs/pipeline/orchestrator-prompt.md` — in the Worker spawn template add a "close this worker's pane once its gate passes" step (unless it's the reused worker, which gets closed too but with resume metadata saved), and in Stage 6 replace `herdr agent prompt pl-3-…` (hold-open) with `herdr agent start pl-6-<run_id> --kind opencode --pane <fresh_pane> -- -m <model> -s <session_id>` against a fresh pane. Keep the orchestrator's existing `herdr agent start` / `pane` / `tab` semantics and `state.json` checkpointing; update gate-polling / resume notes to acknowledge that a closed-but-resumable worker ("not found live, but session ID in state.json") is now expected, not only a crash-recovery edge case.

- `docs/pipeline/pane-lifecycle-v2-proposal.md` — update Status to `implemented` with a pointer to this PR (`auto/pipeline-20260825T021919Z` / PR number once opened), so future readers don't mistake it for still-unbuilt. Keep the evidence section (PR #27/#28/#29 swap numbers) intact.

No `src/herdr_routines/pipeline.py` module, no launcher-script changes (`~/.local/bin/pipeline-launch-*.sh` stays out of scope per proposal Notes), no re-verification of open question 1 from first principles.

## Files touched

- `docs/pipeline/design.md` — amend Cleanup / Worker-spawning / Handoff-contract sections; add G-16; document per-stage close and session-resume.
- `docs/pipeline/orchestrator-prompt.md` — amend Worker spawn template and Stage 6 instructions to close panes per-stage and to use `-s <session_id>` resume; keep stage 1/2/4/5 prompts and gate commands otherwise intact.
- `docs/pipeline/pane-lifecycle-v2-proposal.md` — set Status to `implemented` with PR pointer; leave evidence/rationale sections.
- `tests/test_pipeline_pane_lifecycle.py` (new) — doc-contract tests grepping the two docs and the proposal for required content (existing pattern: `test_plugin_manifest.py` asserting on `herdr-plugin.toml`); see Acceptance criteria.
- `docs/pipeline/runs/20260825T021919Z/spec.md` — this file (per-run spec, `design.md:79` G-15).

## Risks

- **Session-resume fidelity across workspaces.** Manual verification used a bare workspace root, not the shared multi-pane workspace pattern the orchestrator actually uses. Risk low but non-zero that pane lifecycle or `herdr` workspace scoping differs; mitigation: stage 1 sanity-checks resume once against the shared-workspace flow in this spec's risks section, and documents any deviation in `design.md` rather than blocking the run.
- **Pane close vs scrollback reliance.** If any gate or later stage relied on `herdr agent read --source visible` after gate-pass, closing the pane would lose that. Design already treats files/commits as handoff and gates as `rg`/`gh`/`jq` on disk, not scrollback, so immediate close matches "files are the handoff" but open question 2 (grace window) is kept explicitly open until confirmed no reliance remains.
- **Cross-model `-s` (open question 3).** Stage 6 reuses `pl-3`'s `x-preview-f-free` session with the same model family, so no cross-model resume is needed for the current stage plan; `pl-5`'s `big-pickle` session is never resumed. If a future stage reuses a different-model session, `-s` semantics would need re-verification; leave noted as still-open if not resolved now.
- **G-number and spec-path hygiene.** Must add G-16 without renumbering G-1..G-15 and keep spec at `docs/pipeline/runs/<run_id>/spec.md` (G-15 fix); a slip reintroduces the PR #28/#29 merge conflict pattern or confuses future readers.

