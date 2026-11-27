---
id: "097"
title: "Capture one real Zen quota tail and compare it with the two Zen screen fixtures"
status: done
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
- **2026-11-27**: first real capture. Run `fitted-pr-review-5-20261127T130000Z` on the Pi
  (`opencode/deepseek-v4.1-flash-free`), herdr status `blocked` after 43 s, pane 117 columns.
  The tail is kept verbatim as `tests/fixtures/opencode2_screens/quota_zen_free_limit_modal_blocked.txt`.
  Compare: the message text differs from both Zen fixtures. The TUI never prints the API's
  `Free usage limit reached`; it draws a modal over the prompt titled `Free limit reached`,
  with `You've used today's free messages. Resets at 9:45 PM.` under it. The title is on one
  line and ends at column 48. So this is the "modal hides the message" case. Instead of
  filing a new issue, it is fixed here: `Free limit reached` joins `DEFAULT_FAILURE_MARKERS`,
  and the blocked-settle scan from 096 classifies the run `quota_exhausted`. Without it the
  run ended `failed / blocked` and never reached `fallback_model`. The two stub-layout Zen
  fixtures stay, because the retry and `Error:` lines may still appear in other states.
