# Spec — Gate 6 accepts a clean review (issue 069)

## Problem

`gates.run_gate6` (`src/herdr_routines/gates.py:585`) passes only if some trusted
review or comment body contains the literal `BLOCKING_TAG = "[blocking]"`
(`gates.py:61`). That strictness was deliberate — issue 035's fix — because the
orchestrator prompt's old `test("blocking")` substring matched `[non-blocking]`.
But the fix overshot: a review that finds nothing blocking writes
`**[non-blocking]**` on its inline comments and "Found 0 blocking issue(s) and N
non-blocking note(s)" in its summary review body. Neither contains the literal
`[blocking]`, so a genuinely clean review fails gate 6.

Seen on PR #211 (run `20261021T050000Z`): 0 blocking findings, 4 non-blocking
notes, run ended with "gate 6 failed: no [blocking] tier label in any of PR
#211's 6 review/comment body/bodies". Stage 7 never ran, the 4 threads stayed
unanswered, and `required_review_thread_resolution` left the PR blocked until a
human resolved them.

What the gate actually wants is proof the review skill's tier structure ran. A
clean review proves exactly that — it just has no `[blocking]` findings to tag.

## Approach

Widen the pass condition in `run_gate6` with a second, anchored piece of
evidence: the summary phrase the `code-review` skill emits. Concretely, gate 6
passes when (a) some trusted body contains `BLOCKING_TAG` (unchanged), or (b)
some trusted body matches a new anchored regex constant placed next to
`BLOCKING_TAG`:

```python
BLOCKING_SUMMARY_RE = re.compile(r"Found \d+ blocking issue\(s\)")
```

Design decisions, pinned:

- **The regex is count-agnostic.** A review with 1 blocking finding posts
  `**[blocking]**` inline anyway (evidence (a) covers it), so the count value
  itself never needs interpreting; there is one source of truth per evidence
  type. Counts `\d+` keeps the same constant valid for both clean and
  finding-bearing summaries (existing fixture `Found 1 blocking issue(s).
  ` at `tests/test_gates.py:500` keeps matching, no fudge factor).
- **It is anchored, not a substring test:** the literal skill-emitted prefix
  `Found N blocking issue(s)` (parentheses are emitted literally, verified in
  the live skill output quoted in issue 035's era and in the issue text). The
  bare word `blocking`, `[non-blocking]`, or prose like "this is
  blocking-ish" still never satisfy it. Issue 035's protection is kept, restated
  below as criterion 2's test.
- **No `[non-blocking]`-tag precondition.** The zero-findings review (summary
  only, no tags at all) must pass — criterion 3. Requiring a tier tag would
  re-break clean reviews from the other direction.
- **Failure reason string updates** to name both accepted proofs, e.g. `"no
  [blocking] tier label and no review skill summary (Found N blocking
  issue(s)) in any of PR #<pr>'s N review/comment body/bodies — the review
  skill's tier structure is unproven"`. The pipeline-level failure *category*
  `gate_6_failed` is untouched (only the verdict reason text changes).
- **Trusted-author scoping is inherited for free:** the summary phrase counts
  only from bodies already filtered by `trusted_authors` / `_pr_bodies`, the
  same scope gate 7 takes. A drive-by author quoting the skill's summary line is
  not evidence.
- Gate 7 needs no change: `evaluate_gate7` already enforces reply coverage, so
  letting a clean review through gate 6 still forces stage 7 to answer every
  non-blocking thread — and passes trivially when there are no threads.

`re` is already imported in `gates.py` (`gates.py:22`); no new dependency.

## Acceptance criteria

1. [blocking] Gate 6 passes when the only trusted tier labels are
   `**[non-blocking]**` inline comments, provided the review summary body states
   a blocking count ("Found 0 blocking issue(s) and 4 non-blocking note(s)");
   the fixture copies PR #211's real shape (summary review body + 4 `**[non-blocking]**` inline comments, from a trusted author) — confidence: high — Test: test_gate6_passes_clean_review_with_only_non_blocking_notes
2. [blocking] Gate 6 still fails when the bodies contain neither a `[blocking]`
   tag nor a blocking-count summary: a review with only bare `[non-blocking]`
   labels and no summary phrase fails, with the verdict reason still containing
   `[blocking]`. Issue 035's protection kept — confidence: high — Test: test_gate6_still_rejects_bare_non_blocking_without_summary
3. [blocking] A zero-findings review (summary "Found 0 blocking issue(s) and 0
   non-blocking note(s)", no tier tags anywhere) passes gate 6, and
   `evaluate_gate7` on an empty thread list passes in the same test, pinning the
   end-to-end path (gate 6 → stage 7 → resolved PR) — confidence: high — Test: test_gate6_passes_zero_findings_review
4. [blocking] The summary match is anchored on the exact `code-review` skill
   phrase as a module constant `BLOCKING_SUMMARY_RE` next to `BLOCKING_TAG`
   (`gates.py:61`); prose containing the word `blocking` alone, unbracketed or
   reworded ("all blocking concerns cleared" / "nothing found"), does not
   satisfy it — confidence: high — Test: test_gate6_summary_match_is_anchored_not_substring
5. [non-blocking] With `trusted_authors`, the summary phrase counts only from a
   trusted author's body, on both `gh pr view` surfaces and inline comments,
   mirroring `test_gate6_ignores_tier_labels_from_untrusted_authors
   `- confidence: high — Test: test_gate6_ignores_summary_from_untrusted_authors

## Files touched

- `src/herdr_routines/gates.py`
  - new constant `BLOCKING_SUMMARY_RE` at the `BLOCKING_TAG` block
    (`gates.py:58-61`)
  - `run_gate6` (`gates.py:585-639`): second pass branch + updated docstring
    (the existing docstring's "deliberately stricter" paragraph gets a
    correction note pointing at issue 069) + updated failure reason string
- `tests/test_gates.py` — the five tests above, using the existing
  `FakeGhClient` (`tests/test_gates.py:29`), no new fakes

No changes to gate 5, gate 7, `pipeline_run`, trusted-author resolution
(`auto_fix.is_trusted_author`), or any orchestrator prompt prose (the gate is
code-invoked; `docs/pipeline/design.md` stage-table semantics unchanged).

## Risks

- [blocking] **Regressing issue 035's false positive.** The whole trap was a
  loose `blocking` match standing in for evidence the review ran. Mitigation:
  the new evidence is a compiled, anchored regex on the skill's exact summary
  prefix, not a substring test; `[non-blocking]` alone (no summary) still
  fails (criterion 2); untrusted authors are excluded for both evidence types.
  The concrete false positive issue 035 killed — an unrelated trusted body
  containing a bare `blocking` word — matches neither `BLOCKING_TAG` nor
  `BLOCKING_SUMMARY_RE`. — confidence: high
- [blocking] **A pre-existing test silently flips.**
  `test_gate5_rejects_nonblocking_only_review` (`tests/test_gates.py:454`)
  feeds only `[non-blocking]` with no summary and must keep failing;
  `test_gate5_reads_blocking_labels_from_inline_review_comments
  ` (`:495`) has a summary + `**[blocking]**` inline and must keep passing;
  the pipeline-run trusted-author test (`tests/test_pipeline_run.py:790`)
  makes every review untrusted and must keep failing. Running the full suite
  verifies all three unambiguously. — confidence: high
- [non-blocking] **Reason-string drift.** The grown failure sentence could
  break an assertion that pins the old wording mid-string. Mitigation: the only
  gate-6 reason assertion today checks `BLOCKING_TAG in verdict.reason`
  (`tests/test_gates.py:468`), which the new string keeps satisfying. —
  confidence: high
- [non-blocking] **Fixture-vs-skill drift.** If the `code-review` skill ever
  rewords its summary, gate 6 re-fails clean reviews. That is a gate failure,
  visible within one run, and the anchor is deliberately exact per criterion 4;
  no forward-compatible fuzzing is added on purpose. — confidence: medium

## Out of scope

- Changing gate 5 (the spec-tier gate) or gate 7.
- Parsing/validating the counts (e.g. requiring the summary total to equal the
  number of inline comments) — the review's integrity is enforced downstream by
  stage 7's reply coverage, not by arithmetic here.
- Updates to `docs/pipeline/design.md` — the stage table's gate column already
  defers to the Gates doc, whose gate 6 entry is code-anchored, not prose.
