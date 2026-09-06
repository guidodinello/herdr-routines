---
id: "035"
title: "Gate 6 measures blocking-count, not reply coverage — unanswered threads pass"
status: open
priority: high
area: pipeline
---

## Description

Stage 6's contract is the `address-pr-comments` skill's contract: *"fetches all
unresolved inline review threads, assesses their validity, fixes valid ones,
commits and pushes, then replies to every thread with the outcome."*

Gate 6 does not measure that. It measures **"are there unresolved threads whose
body matches `blocking`?"** — a completely different question. A PR passes with
every thread untouched, unanswered, and unfixed, so long as none is tagged
`[blocking]`.

Observed on PR #81 (run 20260904T050000Z):

- Stage 6 spawned — `state.json.stage_sessions["6"]` shows pl-3's session resumed
  via `-s`, and `/tmp/pipeline_resume_20260904T050000Z.log` has `stage 6 poll
  05:27:28Z`. (No `/tmp/pl-6-*.result.json` exists, so whether it ever submitted a
  prompt is unknown — the G-8 blocking fallback writes no result file.)
- PR head is still `234f79f`, the exact commit stage 5 reviewed. **Zero commits
  after the review.**
- The single open thread — a correct `[non-blocking]` note that the glob args
  passed to `du` in `runner.py:89-94` never expand without a shell — got **no
  reply**.
- The run reported `## Outcome: ok` and the report text claims
  "address-pr-comments handled".

Whether the worker did nothing or ran and correctly concluded there was no
`[blocking]` item, the gate let an unanswered thread through. That is the defect.

### Secondary: the literal jq in the prompt is also wrong

The prompt's gate 6 filters with `test("blocking")`. In jq, `"non-blocking" |
test("blocking")` is **true** — substring match — so the literal gate would have
flagged this thread. The orchestrator substituted a stricter `\[blocking\]`
regex, which excludes `[non-blocking]` and passes at 0.

The stricter regex is arguably the sane reading of intent. Both are wrong for the
same underlying reason: neither counts replies. Fix the metric, and pick one
regex deliberately rather than leaving the orchestrator to improvise.

## Design

**Decided 2026-09-06: extract the gate into code rather than reword the prompt.**

The prompt's literal `test("blocking")` matches `"non-blocking"` by substring, so
it *would* have flagged PR #81's thread. The orchestrator silently substituted a
stricter `\[blocking\]` regex that did not, and nothing detected the swap. That is
the real lesson: **a gate written as prose is advisory.** An agent told "run this
jq" can run a different jq and still report the gate passed. Rewording it produces
a better sentence with the same enforcement properties — none.

So gate 6 becomes `herdr-routines gate --stage 6 --pr <n>`, backed by
`src/herdr_routines/gates.py` and pinned by `tests/test_gates.py`. The orchestrator
can run it and read the exit code; it cannot rewrite it.

**Scope is deliberately two gates, not six.** Only this gate and 034's CI gate move
to code now. The rest stay as prose. That keeps the change to one reviewable PR and
proves the pattern before committing to a 192-line prompt rewrite. The cost,
accepted knowingly: gate logic lives in two places during the transition.

**This issue and 034 are one PR** — both create `gates.py`.

Redefine gate 6 as **reply coverage**, with the blocking-fix check as an
additional condition rather than the only one:

```sh
gh api graphql -f query='
  query($owner:String!,$repo:String!,$pr:Int!){
    repository(owner:$owner,name:$repo){ pullRequest(number:$pr){
      reviewThreads(first:50){ nodes{
        isResolved
        comments(first:100){ totalCount nodes{ body author{login} } }
      }}}}}' \
  -f owner=<o> -f repo=<r> -F pr=<n> \
| jq -e '
    [ .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.isResolved == false)
      | select(.comments.totalCount < 2) ] | length == 0'
```

i.e. **every unresolved thread must carry at least one reply.** Keep a second
assertion that no thread whose first comment matches `\[blocking\]` remains
unresolved. Anchor the tag match to the literal bracketed form so
`[non-blocking]` can never satisfy a `[blocking]` search by substring.

Worth considering alongside: if stage 6 makes zero commits *and* posts zero
replies, that is itself suspicious enough to warrant a distinct report line, even
when the gate legitimately passes because there was genuinely nothing to fix.

## Acceptance criteria

1. An unresolved thread with no reply fails gate 6. Test: `test_gate6_fails_on_unreplied_thread`
2. An unresolved thread with a reply and no `[blocking]` tag passes. Test: `test_gate6_passes_on_replied_nonblocking`
3. `[non-blocking]` never matches the blocking-tag assertion. Test: `test_blocking_tag_match_excludes_non_blocking`
