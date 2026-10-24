---
id: "080"
title: "issue-refinement can reuse an issue number taken by an open PR or a recent merge"
status: open
priority: low
area: pipeline
---

## Description

On 2026-10-24 the nightly `issue-refinement` job opened #225 as issue `071` while #224
(also `071`) was open and about to merge. The job computes "highest existing number + 1"
from its base snapshot of `docs/process/issues/` and checks nothing else. Main ended up
with two `071` files (fixed by hand in #227). Its ROADMAP pointer was also attached to the
wrong paragraph.

## Design

- Compute the next number in code (a `refine-issue --next-id` step, like 029's other
  checks), from the union of: main's issue files after a fresh fetch, and every
  `docs/process/issues/NNN-*` path added by an open PR (`gh pr list` + files).
- `--check-issue` (issue 029) also fails when the file's id is already used on
  `origin/main` or by an open PR.
- Make the ROADMAP pointer a code step too: append to the bullet whose slug the issue
  refines.

## Acceptance criteria

1. [blocking] The next id skips numbers used by open PRs. Test:
   `test_next_issue_id_skips_open_pr_numbers`
2. [blocking] `--check-issue` rejects a duplicate id. Test:
   `test_check_issue_rejects_duplicate_id`
3. [blocking] The ROADMAP pointer lands on the refined bullet. Test:
   `test_roadmap_pointer_appended_to_its_bullet`

## Notes

- Related: 029, 071/072 (the collision).
