---
id: "097"
title: "Capture one real Zen quota tail and compare it with the two Zen screen fixtures"
status: open
priority: medium
area: pipeline
---

## Description

Issue 096 made a Zen 429 detectable whether herdr reports `idle` or `blocked`, and completed the
two-shape corpus with `quota_zen_free_limit_settled_error.txt` and
`quota_zen_free_limit_retry.txt`. Both fixtures carry Zen's real 429 wording in the layout of
the local stub screens: no real opencode 2 capture exists, so the claim they prove is about the
message text, not the rendered lines.

Matching is a verbatim substring search over the visible screen and ignores chrome and
indentation. The one layout that can break it is a line wrap inside the marker. The fixtures
assert that the marker ends by column 36 (settled) / 60 (retry), so a pane at least that wide
cannot split it. That bound comes from fabricated fixtures, and a real modal could hide the
message or give it a longer prefix.

## Acceptance

- On the first real Zen quota failure of a routine/audit/gated job, keep the generated
  `<run_id>.tail.txt` (the blocked path now writes it on a quota match too) and record the
  run id, agent state, pane width and message text here.
- Compare that text with both Zen fixtures: first the message text, then the marker's line and
  end column. A pure layout difference needs no fixture change — the fixtures claim the text
  matches, not that the whole screen looks identical. Replace a fixture only if the message
  text or the wrap bound differs, and update the numeric bound assertion with it.
- If the real screen hides the message in a blocking modal, file a new detection issue: that is
  a different problem from a fixture swap.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-27**: filed from issue 096's residual. Needs a real quota event; deliberately not a
  code task. Until a capture exists, the wrap bound is an explicit assumption rather than
  evidence.
