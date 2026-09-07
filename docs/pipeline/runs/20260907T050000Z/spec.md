# Spec — Issue Refinement Job (029) — 20260907T050000Z

## Problem
ROADMAP Parking Lot collects raw ideas ("idea, not designed") with no owner. Promoting one to a buildable `docs/process/issues/NNN-*.md` + ROADMAP entry is manual. Issue 006 was too coarse for a single worker, showing input quality matters. Need automated product-refinement distinct from pipeline's implementation refinement.

## Approach
Create a `jobs.d/` routine `issue-refinement` running nightly 22:00 (`cron: "0 22 * * *"`), `opencode` only (no Claude), using a bounded author→reviewer loop:
- Selection (stage 0): `herdr-routines refine-issue` parses ROADMAP Parking Lot bullets, skips promoted (links an issue file) and gated (`Gate:`/`blocked`) bullets, and skips any bullet already covered by an existing issue file or an open `auto/issue-refinement-*` PR. Emits the bullet + a `Refines-Parking-Lot: <slug>` marker. Fails safe (exit 1, no pick) on any `gh` error.
- Loop (cap 3): author (`opencode/big-pickle`, the job's own session) drafts/updates issue file + ROADMAP bullet → reviewer (`opencode/muse-spark-1.2-contributor-free`, a fresh session spawned from the prompt) audits → consensus when reviewer says `confidence: high`, else continue. Prompt-driven, not a Python loop (same as the pipeline orchestrator).
- Output: opens PR with `docs/process/issues/NNN-*.md` + ROADMAP change, head `auto/issue-refinement-<run_id>` (built from `name` by `runner.build_branch_name`, not `auto/pipeline-`), for human merge. Not visible to `pick-feature` until merged.

## Files touched
- `deploy/jobs.d/issue-refinement.yaml` (new job definition, prompt = the loop checklist)
- `src/herdr_routines/issue_refinement.py` (Parking Lot parser, selection, coverage/dedup guard, frontmatter validation)
- `src/herdr_routines/cli.py` (`refine-issue` subcommand)
- `tests/test_issue_refinement.py` (selection + guard, cap, frontmatter, job file via the real config validator, pipeline independence)
- `docs/process/issues/029-issue-refinement-job.md` (`status: done` + Design/Acceptance updated for the open-PR guard substitution)
- `docs/pipeline/runs/20260907T050000Z/spec.md` (this spec)
- `ROADMAP.md` patch via the generated PR only, not in this branch

## Risks
- Prompt drift causing author and reviewer to diverge on schema; mitigate via strict frontmatter validation and iteration cap.
- Parking Lot parsing fragility if ROADMAP format changes; keep parser simple and test against real ROADMAP snapshot.
- Duplicate issue creation if selection misses coverage check; ensure grep against existing issue titles/bullets.
- Overlap with pipeline backlog: head prefix avoids `auto/pipeline-*` guard; verified.
- Model quota exhaustion during loop; fallback to next night via cap.


## Acceptance criteria

1. [blocking] Selection picks the oldest Parking Lot bullet that is not already promoted (links no issue file) and not covered by an existing issue file or an open `auto/issue-refinement-*` PR, skipping gated (`Gate:`/`blocked`) bullets unless `--allow-blocked`. The open-PR check is the "never re-refine the same idea" guard and fails safe on a `gh` error. Test: test_issue_refinement_selection_picks_oldest_uncovered + test_issue_refinement_no_pipeline_overlap confidence: high
2. [blocking] Author→reviewer loop terminates at consensus or cap 3; reviewer is fresh session (not author re-reading own draft) with at least one reviewer pass. Test: test_issue_refinement_loop_caps_at_three confidence: high
3. [blocking] Output PR contains valid `docs/process/issues/NNN-*.md` with frontmatter (id, title, status open, priority, area) and ROADMAP bullet, head `auto/issue-refinement-*` not `auto/pipeline-*`. Test: test_issue_refinement_pr_contains_valid_issue_file confidence: high
4. [non-blocking] Job definition is `opencode` only, no `agent_kind: claude`, cron `0 22 * * *`. Test: test_issue_refinement_job_is_opencode_only confidence: medium
5. [non-blocking] Refined issue not visible to `pick-feature` until PR merged; head avoids open-PR guard. Test: test_issue_refinement_no_pipeline_overlap confidence: medium

## Changelog v1→v2

- Added Acceptance criteria with 5 numbered items each ending `Test: <name>` and `confidence:` tier.
- Added `blocking`/`non-blocking` labels per criteria (tiers present for gate).
- Clarified head prefix and opencode-only constraint per acceptance.
