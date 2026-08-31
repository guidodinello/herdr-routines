---
id: "029"
title: "Issue refinement job: Parking Lot → refined issue PR"
status: open
priority: medium
area: pipeline
---

## Description

ROADMAP.md's Parking Lot collects ideas ("idea, not designed") that no plan
document should own invisibly. Today, promoting one to a buildable issue
(`docs/process/issues/NNN-*.md` + ROADMAP bullet) is a manual, human-crafted
job. This item automates that promotion with a short product-refinement loop:

- **input**: a Parking Lot bullet,
- **loop**: an author agent drafts a refined issue → an independent reviewer
  audits it → the author updates → repeat until both agree nothing more is
  improvable (or the cap is hit),
- **output**: `docs/process/issues/NNN-*.md` (frontmatter + description) +
  ROADMAP entry, opened as a **PR for the human to review** — never written
  straight to `main`.

Distinction that justifies its own job: the overnight pipeline (issues
004/013) is **implementation refinement** — given a formed issue, it polishes
*how* to build it (spec → review → address → green). This job is **product
refinement** — it polishes *what/why/should-we* from a raw idea. It improves
the pipeline's input quality: run `20260831T012350Z` aborted because issue 006
entered too coarse for a single worker; product refinement would have split it
earlier.

## Design

- Runs as a **`jobs.d/` routine** (`cron: "0 22 * * *"` — clear of the morning
  cluster 05:00-08:00/30-06 and the pipeline's 02:00 launch), not the overnight
  pipeline.
- **Harness: `opencode` only** (no Claude Code): author = `opencode/big-pickle`,
  reviewer = `opencode/muse-spark-1.2-contributor-free` **fresh session**
  (independence via sessions, same convention as pipeline stage 2).
- **Iteration cap: 3.** Each pass: author updates the issue draft; reviewer
  audits; consensus = reviewer marks `confidence: high` + "no more improvements".
  At the cap, stop and surface for the human with the remaining notes.
- **Selection:** pick a Parking Lot bullet in _order_ consistent with the rest
  of the repo (oldest-gated / earliest date first); mark it picked in ROADMAP
  so a later pass never re-refines an idea already promoted.
- **Output path:** opening the PR (issue file + ROADMAP, head like
  `docs/issue-029-issue-refinement`) keeps the human merge gate. The refined
  issue is not visible to `pick-feature` until it lands on `main` (issue files
  are only read from the parent clone's `main`) — so no overlap with the
  pipeline backlog. Its head does **not** start with `auto/pipeline-`, so it is
  correctly invisible to issue 028's open-PR guard (which only skips pipeline
  PRs; manual-review PRs should never be auto-skipped).
- Skip ideas already covered by an existing issue file; skip `blocked` ideas
  (gated on an unmade design decision) unless the job derives the decision.

## Acceptance

- Given a Parking Lot bullet, the loop produces a `docs/process/issues/NNN-*.md`
  with valid frontmatter (id, title, status open, priority, area) and a ROADMAP
  bullet — as a PR the human can review, request changes on, or merge.
- The reviewer is an independent session (not the author re-reading its own
  draft); at least one reviewer pass happens before output.
- The loop terminates: consensus or iteration cap 3, whichever first — it never
  runs unbounded.
- The selected idea is marked picked so a later run does not refine it twice.
- `opencode` only: no `agent_kind: claude` anywhere in the job.
- Interaction with the overnight pipeline: zero — the job's PR head is not
  `auto/pipeline-` (issue 028 guard doesn't touch it) and the refined issue
  only enters the pool after human merge.

## Log

- **2026-08-31**: filed from Parking-Lot brainstorm. Decisions locked with the
  human: iteration cap 3, `opencode`-only, nightly **22:00** (least overlap:
  morning cluster 05-08 h + pipeline 02:00 launch), output = open PR for human
  merge. Framing: product vs implementation refinement; motivation = issue 006
  abort (oversized issue).
