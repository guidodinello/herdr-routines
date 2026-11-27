# Spec v1 — 20261127T050000Z — Make the real Zen 429 reach detection whatever the screen looks like

Status: draft for stage 2 review.
Implements [issue 096](../../../process/issues/096-zen-quota-screen-wording.md) only.
Background: `docs/plan-v1.md` (project context, not this feature).

## Problem

096 asks one question: the Zen wording is now a default marker, but nobody has seen the real
opencode 2 screen, so we do not know whether the TUI's layout defeats the match. Reading the
code, the layout is not the only — or even the largest — gap between "the marker is right" and
"a real Zen 429 is detected":

1. **The new marker does not reach four dispatch paths.** `DEFAULT_FAILURE_MARKERS`
   (`runner.py:93-96`) holds both phrasings, but only `runner.execute_run` and the pipeline
   launcher read it. Four call sites still fall back to a hardcoded
   `("Free usage exceeded",)` — the stub/opencode 1 wording:
   `dispatch_common.py:169-172` (audit and gated workers), `gated_base_flow.py:378-380`,
   `gated_pr_flow.py:651`, `gated_event_flow.py:800`. A real Zen 429 on an audit or gated
   job is therefore not detected at all, whatever its layout. Two of them also use `or`
   instead of `is not None`, so `failure_markers: []` — the documented opt-out — silently
   becomes the defaults there (`runner.py:801-806` already calls this out as a PR #25 bug).

2. **The settle scan may never run on the real failure.** #301's settle-time scan fires only
   for a settled `idle`/`done` agent with a missing/empty report (`runner.py:882` returns
   before it on any other status). The only real Zen 429 on record (091's log, 2026-11-26)
   made herdr report the stage **`blocked`**, not `idle`. A routine job in that state ends
   `failed / blocked`, leaves its pane open, and `routine_flow`'s same-tick recovery — gated
   on `outcome.reason == "quota_exhausted"` (`routine_flow.py:207-212`) — never fires. That
   is exactly the failure 091 exists to prevent, still reachable on a real screen.

3. **The fixture's layout is the stub's, not a captured one.** That is fine for the claim it
   makes (`matched_failure_marker` is a plain substring test over the whole visible screen,
   `wait_loop.py:98-109`, so chrome and indentation are irrelevant), but it is not fine as
   evidence: the corpus cannot show that the marker survives the real rendering. The one
   layout that *can* break a substring match is a line wrap inside the marker itself. In the
   settled fixture the marker starts at column 12 and ends at 36; in the retry shape it ends
   at 60. Any pane ≥60 columns keeps it unbroken — but nothing in the repo says so, and no
   test would notice if a future fixture edit moved it.

So 096's acceptance ("compare the first real `<run_id>.tail.txt`; replace the fixture if the
layout differs") cannot be executed today: there is no real capture, and the run that would
produce one has no reliable path to detection. This spec closes the detection gap that is
provable offline, and parks the capture itself as a narrow follow-up.

## Approach

### 1. One source of truth for the effective markers

Add to `runner.py`, next to `DEFAULT_FAILURE_MARKERS`:

```python
def effective_failure_markers(configured: tuple[str, ...] | None) -> tuple[str, ...]:
    return DEFAULT_FAILURE_MARKERS if configured is None else configured
```

Use it at the five marker call sites: `runner.execute_run` (replacing the local
`effective_markers` expression at `runner.py:803-806`), `dispatch_common.py:169-172`,
`gated_base_flow.py:378-380`, `gated_pr_flow.py:651`, `gated_event_flow.py:800`. The two `or`
sites become the helper too, which fixes the `failure_markers: []` opt-out structurally —
no call site can reintroduce the truthiness bug.

`DEFAULT_FAILURE_MARKERS` stays in `runner.py`: `pipeline_run`, `pipeline_flow`, `cli` and the
existing tests import it from there, and `dispatch_common` already imports `runner` at module
level (no new cycle).

### 2. Classify a quota screen on the blocked settle

In `runner.execute_run`'s blocked branch (`runner.py:882-891`), the tail is already read and
returned as `visible_tail`. Scan that text — no second read — with
`matched_failure_marker(tail, effective_markers, prompt)`, only when
`job.agent_kind == OPENCODE_KIND` (the corpus is opencode's TUI; the settle scan has the same
guard).

- **Match:** persist the tail as `<run_id>.tail.txt` (already done by
  `_capture_visible_tail`), close the pane, and return `failed / quota_exhausted` with
  `error=f"failure marker matched: {marker!r} (blocked screen)"`. `common` keeps
  `final_agent_status="blocked"`, so history still shows what herdr reported.
  Closing the pane matters: the fallback run reuses the job's agent name, and a live
  `rt-<name>` makes `agent_start` fail with `agent_name_taken`.
- **No match:** today's behaviour exactly — pane left open, `failed / blocked`.

Rationale for overriding "blocked is answerable from bed": a quota wall is not answerable by
an approval. The screen says the model cannot continue, so the one useful action is the
fallback, and the tail is on disk either way.

### 3. Complete the corpus and pin the layout assumption

- New fixture `tests/fixtures/opencode2_screens/quota_zen_free_limit_retry.txt`: Zen's real
  text in the retry shape (`⚠ Retrying in 5s · attempt 5 · <message>`), mirroring
  `quota_shaped_retry.txt`. Its docstring/test notes that, like the settled Zen fixture, it
  is the real wording in the stub's layout, not a captured screen.
- Extend the corpus tests so both Zen shapes match `ZEN_FREE_LIMIT_MARKER` and neither
  matches `QUOTA_MARKER`, and so `http_401_settled_error`, `http_500_retry`,
  `model_unavailable` and `normal_reply` still match nothing in either shape.
- Add a wrap-invariance test: derive the marker's end column from each Zen fixture's own
  message line, then re-wrap the message with `textwrap` at that width and at 80/120 and
  assert the marker is still intact. This turns "we do not know the layout" into a stated,
  tested bound (≥60 columns for the retry shape), and fails if a fixture edit moves the
  marker behind the bound.
- Two end-to-end tests, not just matcher tests (091's lesson: the string matching is not the
  same claim as the path firing): a settled-Zen run and a blocked-Zen run both reach
  `quota_exhausted` through `execute_run`.

### 4. Record what is proven, and the residual

- `docs/failure-reaping.md` §3.2: add the Zen rows to the shape table, add the blocked-settle
  path to the detection table, and state the wrap bound and why matching is layout-blind
  above it.
- 096's log: what this closes, what it does not, and the comparison protocol for the first
  real tail (compare text first; a pure layout difference needs no fixture change, because
  the test's claim is about the message text).
- New issue 097 (`097-capture-real-zen-quota-screen.md`, next free number): capture one real
  `<run_id>.tail.txt` from a routine/audit/gated job, compare it with the two Zen fixtures,
  and replace them only if the message text or the wrap bound differs. This is the piece
  that needs a real event; it is deliberately not a code task.

Stage 5 flips 096 to `done` once the PR lands: its detection question is answered, and 097
carries the evidence task.

## Non-goals

- **No new marker strings, no heuristic on `Error:`/`Retrying`.** 091's rule stands: only
  verbatim provider text is a marker, or 401/5xx/unavailable screens get reaped as quota.
- **No whitespace normalisation of the screen.** It would let a marker bridge the main
  column and the right-hand sidebar, adding a false-positive class to buy a wrap case that
  cannot occur at ≥60 columns.
- **No settle scan in audit/gated flows.** They have no `fallback_model`, so a quota reason
  there changes only the label; a retry on the same exhausted model just burns a run.
- **No change to `pipeline_run`/`pipeline_flow`.** 095 (#304) reads the session export's
  provider error and already reaches `fallback_model` without the screen.
- **No change to the in-flight watchdog's two-poll gate or the settle scan's single read.**

## Files touched

- `src/herdr_routines/runner.py` — `effective_failure_markers`; use it locally; blocked-tail
  scan and the `quota_exhausted` return with the pane closed.
- `src/herdr_routines/dispatch_common.py`, `gated_base_flow.py`, `gated_pr_flow.py`,
  `gated_event_flow.py` — use the helper; drop the hardcoded tuple (and the `or`).
- `tests/fixtures/opencode2_screens/quota_zen_free_limit_retry.txt` (new).
- `tests/test_runner.py` — helper contract, blocked-path tests (match, no-match, prompt
  guard, opencode-only), corpus/negative-screen tests, wrap-invariance, one-source-of-truth
  guard, docs test.
- `tests/test_tick.py` — `routine_flow` fallback after a blocked Zen screen.
- `docs/failure-reaping.md` — §3.2 shape/detection tables, wrap bound.
- `docs/process/issues/096-zen-quota-screen-wording.md` — log entry, `status: done` at
  stage 5.
- `docs/process/issues/097-capture-real-zen-quota-screen.md` (new).

Not touched: `wait_loop.py`, `pipeline_run.py`, `pipeline_stages.py`, `config.py`,
`DEFAULT_FAILURE_MARKERS`' contents, `scripts/`.

## Risks

- **A blocked run that is not quota can be misread.** A resumed session showing an old quota
  line above a genuine permission prompt would be filed `quota_exhausted` and its pane
  closed, losing the evidence a human would have inspected. The prompt-guard and the
  opencode-only scope narrow it; the cost is one fallback retry. The alternative — missing
  every real Zen 429 — is worse.
- **The blocked pane is no longer preserved on a quota match.** Deliberate (a quota wall is
  not approvable), but it is a behaviour change: `test_blocked_status_keeps_pane_alive`
  stays true for non-quota screens only.
- **`failure_markers: []` now actually disables scanning in gated paths.** That is the
  documented intent, but a deployed job that set it while relying on the old fallback would
  stop scanning. No committed config sets it; the live `jobs.yaml` is host-local, so the
  stage-3 note should say to grep it on the Pi.
- **A new `quota_exhausted` reason can come from a blocked settle**, so a job with
  `retry_on: [quota_exhausted]` retries where it previously recorded `blocked`. Both are
  valid reasons; the retry is one extra run, not a loop.
- **The wrap bound is derived from fabricated fixtures.** A longer real prefix (a different
  icon, a wider indent) could move the marker past column 60. 097 is the only real answer;
  this spec makes the assumption explicit instead of silent.

## What this does and does not prove

Proves: every dispatch path uses the Zen wording; a quota screen is classified whether herdr
calls it `idle` or `blocked`; no 401/5xx/unavailable screen is classified; the marker cannot
be split by a wrap at any realistic pane width.

Does not prove: that the real screen shows the message text at all (a modal that hides it, or
a screen that scrolls it away, would still be missed), or that the real line's prefix matches
the fixtures'. Those need the capture in 097 — and if 097 finds a hidden/blocking modal with
no message text, that is a new detection problem, not a fixture swap.

## Acceptance criteria

Every item is **blocking** (must ship) or **non-blocking**, carries a `confidence:` tier, and
ends with the exact test that proves it.

1. **blocking** — confidence: high — `effective_failure_markers(None)` is
   `DEFAULT_FAILURE_MARKERS`, `()` stays empty, and a custom tuple passes through.
   Test: `test_effective_failure_markers_contract`
2. **blocking** — confidence: high — No module outside `runner.py` hardcodes either marker
   literal, and each of the four dispatch modules passes the helper's result.
   Test: `test_failure_markers_have_one_source_of_truth`
3. **blocking** — confidence: high — A settled `blocked` opencode agent whose screen is
   `quota_zen_free_limit_settled_error.txt` ends `failed / quota_exhausted`, writes
   `<run_id>.tail.txt`, closes its pane, and keeps `final_agent_status="blocked"`.
   Test: `test_blocked_settle_with_zen_screen_is_quota_exhausted`
4. **blocking** — confidence: high — The same blocked settle with no marker keeps today's
   behaviour: `failed / blocked`, pane open, tail written.
   Test: `test_blocked_settle_without_a_marker_keeps_the_pane`
5. **blocking** — confidence: high — A marker appearing verbatim in the job's prompt is inert
   on the blocked path.
   Test: `test_blocked_settle_marker_in_prompt_is_inert`
6. **blocking** — confidence: high — The blocked scan is opencode-only: a claude agent with
   the same text stays `blocked`.
   Test: `test_blocked_settle_scan_is_opencode_only`
7. **blocking** — confidence: high — `routine_flow` runs the fallback model after a blocked
   Zen screen, with the primary recorded as `quota_exhausted`.
   Test: `test_fallback_model_retried_after_blocked_quota_screen`
8. **blocking** — confidence: high — Both Zen fixtures match `ZEN_FREE_LIMIT_MARKER` and not
   `QUOTA_MARKER`; `http_401_settled_error`, `http_500_retry`, `model_unavailable` and
   `normal_reply` match neither in either shape.
   Test: `test_zen_free_limit_matches_in_both_shapes` and
   `test_non_quota_screens_match_nothing_in_either_shape`
9. **blocking** — confidence: medium — Re-wrapping each Zen fixture's message at its own
   marker-end column and at 80/120 columns leaves the marker intact.
   Test: `test_zen_marker_survives_a_wrap_at_realistic_widths`
10. **blocking** — confidence: high — A settled-Zen run (idle, no report) reaches
    `quota_exhausted` through `execute_run`, not just through the matcher.
    Test: `test_settled_zen_screen_is_quota_exhausted_without_the_nudge`
11. **non-blocking** — confidence: medium — `docs/failure-reaping.md` §3.2 names both Zen
    shapes, the blocked-settle path, and the wrap bound; 096's log and the new 097 file
    record what remains.
    Test: `test_failure_reaping_documents_the_zen_shapes_and_blocked_path`
12. **blocking** — confidence: high — `DEFAULT_FAILURE_MARKERS` is unchanged (both
    phrasings, in order).
    Test: `test_default_failure_markers_unchanged` (existing)

`scripts/ci-check.sh` is the stage-5 gate, not a pytest test.
