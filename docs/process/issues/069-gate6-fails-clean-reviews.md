---
id: "069"
title: "Gate 6 fails a clean review, so stage 7 never addresses its non-blocking notes"
status: done
priority: high
area: pipeline
---

## Description

`gates.run_gate6` passes only if some trusted review or comment body contains the
literal `[blocking]`. Issue 035 made it that strict on purpose, because the old substring test
`test("blocking")` also matched `[non-blocking]`. The fix overshot. A review that finds nothing blocking writes
`**[non-blocking]**` on its inline comments and "Found 0 blocking issue(s) and N
non-blocking note(s)" in its summary. Neither contains `[blocking]`, so a clean
review fails the gate.

Seen on PR #211 (run `20261021T050000Z`). The review posted 0 blocking findings and 4
non-blocking notes. The run ended with:

> gate 6 failed: no [blocking] tier label in any of PR #211's 6 review/comment
> body/bodies — the review skill's tier structure is unproven

Stage 7 never ran, the 4 threads stayed unanswered, and the repo's
`required_review_thread_resolution` rule left the PR blocked until a human resolved them.

The gate wants proof that the review skill's tier structure ran. A clean review
does prove that. It just has no `[blocking]` findings to tag.

## Acceptance criteria

1. [blocking] Gate 6 passes when the only trusted tier labels are `[non-blocking]`,
   provided the review summary states a blocking count (e.g. "Found 0 blocking
   issue(s)"). Test: `test_gate6_passes_clean_review_with_only_non_blocking_notes`
2. [blocking] Gate 6 still fails when the bodies contain neither a `[blocking]` tag nor a
   blocking-count summary. A bare `[non-blocking]` tag without the summary still
   fails, so issue 035's protection is kept. Test:
   `test_gate6_still_rejects_bare_non_blocking_without_summary`
3. [blocking] A clean review whose summary says "Found 0 blocking issue(s) and 0
   non-blocking note(s)" passes gate 6, and gate 7 then passes because there are no
   threads. Test: `test_gate6_passes_zero_findings_review`
4. [non-blocking] The summary match is anchored on the exact phrase the `code-review`
   skill emits, as a module constant next to `BLOCKING_TAG`. It is not a loose
   substring test.

## Notes

- Fixture shape: copy PR #211's real review and comment bodies, so the test runs
  on the shape the live review actually produces.
- Gate 7 (`evaluate_gate7`) already enforces reply coverage, so letting a clean review
  through gate 6 still forces stage 7 to answer every non-blocking thread.
