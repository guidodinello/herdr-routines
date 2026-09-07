# Spec — Issue Refinement Job (029) — 20260907T050000Z

## Problem
ROADMAP Parking Lot collects raw ideas ("idea, not designed") with no owner. Promoting one to a buildable `docs/process/issues/NNN-*.md` + ROADMAP entry is manual. Issue 006 was too coarse for a single worker, showing input quality matters. Need automated product-refinement distinct from pipeline's implementation refinement.

## Approach
Create a `jobs.d/` routine `issue-refinement` running nightly 22:00 (`cron: "0 22 * * *"`), `opencode` only (no Claude), using a bounded author→reviewer loop:
- Selection: pick oldest Parking Lot bullet not already covered by an issue file, skipping `blocked` unless job derives decision; marks picked to avoid re-refinement.
- Loop (cap 3): author (`opencode/big-pickle`) drafts/updates issue file + ROADMAP bullet → reviewer (`opencode/muse-spark-1.2-contributor-free` fresh session) audits → consensus when reviewer says `confidence: high` + no more improvements, else continue.
- Output: opens PR with `docs/process/issues/NNN-*.md` + ROADMAP change, head `auto/issue-refinement-<timestamp>` (not `auto/pipeline-`), for human merge. Not visible to `pick-feature` until merged.

## Files touched
- `deploy/jobs.d/issue-refinement.yaml` (new job definition)
- `src/herdr_routines/issue_refinement.py` (selection + loop helpers, if needed) or inline prompt logic via job's agent
- `tests/test_issue_refinement.py` (selection, cap, opencode-only, head prefix)
- `docs/pipeline/runs/20260907T050000Z/spec.md` (this spec)
- `ROADMAP.md` patch via generated PR only, not in this branch

## Risks
- Prompt drift causing author and reviewer to diverge on schema; mitigate via strict frontmatter validation and iteration cap.
- Parking Lot parsing fragility if ROADMAP format changes; keep parser simple and test against real ROADMAP snapshot.
- Duplicate issue creation if selection misses coverage check; ensure grep against existing issue titles/bullets.
- Overlap with pipeline backlog: head prefix avoids `auto/pipeline-*` guard; verified.
- Model quota exhaustion during loop; fallback to next night via cap.

