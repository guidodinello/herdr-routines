# Spec v2 — 20261126T050000Z — Detect opencode 2's terminal quota screen

Status: reviewed at stage 2 (2026-11-26). Supersedes v1 (`d61ba02`).
Implements [issue 091](../../../process/issues/091-opencode2-failure-markers.md) only.
Background: `docs/plan-v1.md` (project context, not this feature).

## Problem

Issue 091 asked whether opencode 2 still puts a quota message on screen where
`failure_markers` can see it. PR #291 answered the first half: it does, verbatim, in two
shapes captured under `tests/fixtures/opencode2_screens/`:

- **retry shape** — `⚠ Retrying in 5s · attempt 5 · Free usage exceeded, subscribe to Go`,
  herdr status `working` (`quota_shaped_retry.txt`);
- **terminal shape** — `Error: Free usage exceeded, subscribe to Go`, status `idle`
  (`quota_shaped_settled_error.txt`).

So `DEFAULT_FAILURE_MARKERS = ("Free usage exceeded",)` (`runner.py:91`) still matches and
#291 left the markers unchanged, correctly. What it did not check is **when** the marker is
looked for:

- `wait_loop.prompt_with_watchdog`'s `scan` closure (`wait_loop.py:267-279`) only runs
  *while the prompt is in flight*, and needs the same marker on two consecutive polls
  `WATCHDOG_POLL_INTERVAL_S = 30.0` apart (`wait_loop.py:43`). A quota failure that
  surfaces in under one poll interval is invisible.
- `drain_opencode_background` re-scans only while background subagents are pending
  (`wait_loop.py:217`), so it does not cover a plain settle either.
- `runner.execute_run` captures the visible tail after the settle (`runner.py:946`) and
  never scans it.

The terminal shape is exactly the state that exists *after* the wait has ended. When it is
the only sighting, `runner.execute_run` files `failed / no_report` (`runner.py:972-983`)
after first spending up to `NUDGE_TIMEOUT_MS = 120_000` (`runner.py:97`) asking an
exhausted model to write a report it cannot write. `routine_flow`'s same-tick recovery is
gated on `outcome.reason == "quota_exhausted"` (`routine_flow.py:208-212`), so
`fallback_model` never fires. That is the failure 091 exists to prevent, still reachable
for routine/audit/gated jobs (e.g. `issue-refinement`, the only deployed job with
`fallback_model`).

The test #291 added for the terminal fixture feeds it through the *during-wait* poll loop
(`tests/test_runner.py:1247-1263`), a state real opencode never reaches — that screen
appears only after the wait returns. It therefore proves the marker string matches, not
that the settled screen is detected.

Pipeline stages are a different path and are deliberately left alone here (see
Non-goals): `pipeline_run._fallback_cause` already has a screen-independent signal,
`_session_api_error` reading `info.error.name == "APIError"` from `opencode export`
(`pipeline_run.py:1697-1745`).

## Approach

### 1. One shared settle-time scan

Add to `wait_loop.py` (the module both `runner` and `pipeline_run` already import):

```python
def settled_screen_marker(
    client, *, target: str, markers: Sequence[str], prompt_text: str,
    reads: int = 2, interval_s: float | None = None,
) -> tuple[str | None, str]
```

- Reads `client.agent_read_visible(target, lines=200)` — the same call shape as
  `_capture_visible_tail`, so the returned text is tail-equivalent — `reads` times,
  `interval_s` apart.
- `interval_s=None` resolves `WATCHDOG_POLL_INTERVAL_S` **inside the function, at call
  time**. Do not write `interval_s: float = WATCHDOG_POLL_INTERVAL_S` as a default
  parameter: that binds at import and silently defeats the test seam, adding a real 30 s
  to every test that forgets to pass `interval_s=0`.
- Returns the marker only when the same marker is seen on two consecutive reads, reusing
  `matched_failure_marker` so the marker-in-prompt guard applies unchanged. Returns the
  last screen text as well, so the caller persists the tail without a third read.
- A read error is inert; never raises. Mirrors the existing poll contract.

The two-read gate is kept on purpose: a foreign session's "Session failed / Free usage
exceeded" toast can render over this pane (`foreign_session_failed_toast.txt`), and the
existing stability rule is what keeps one sighting from reaping a healthy run.

Tests patch `herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S` (the global the helper
reads), not `runner`'s re-export of the value.

### 2. Use it on the no-report path in `runner.execute_run`

Only when the agent settled to a success status **and** the report is missing or empty —
i.e. the run is already failing:

- Scan once before `_attempt_report_nudge` (`runner.py:922-925`), using
  `effective_markers` and the same substituted `prompt` the wait used.
- On a match: skip the nudge (an exhausted model cannot answer it; this saves up to
  120 s). Write the scan's last screen text to `<run_id>.tail.txt` when non-empty and set
  a `tail_written` flag so the later `_capture_visible_tail` at `runner.py:946` does not
  read a third time. Then continue through the existing session-id capture, pane close,
  and commit re-sign block — do **not** return early, or those fields are lost. At the
  final no-report check (`runner.py:972-983`), return `state="failed"`,
  `reason="quota_exhausted"`,
  `error=f"failure marker matched: {marker!r} (settled screen)"`.
- **Re-stat the report after the scan.** If a non-empty report appeared during the two
  reads, the report wins: clear the quota flag, skip the nudge because the report exists,
  and fall through to the normal success path. The no-downgrade invariant must hold across
  the 30 s scan window, not only at the instant of settle.
- No match: today's path exactly, nudge included, ending in `no_report`.
- A written, non-empty report is never scanned, so a `done` run cannot be downgraded.

No marker strings are added or changed. The corpus shows verbatim passthrough; guessing
new phrasings would reap the 401/5xx/unavailable screens as quota, which 091's log
explicitly rules out.

### 3. Record the comparison, and the residual

- `docs/failure-reaping.md` §3.2 gains the two opencode 2 shapes and which detection path
  covers each, pointing at the fixtures. It also reconciles its current "Classification
  applies to the `agent_prompt_failed` path only … does not inspect screens on healthy
  runs" wording: the settle scan runs only for a settled success with a missing/empty
  report (already a failing run), never on a healthy run.
- Issue 091's log gets the finding. The entry records that the settle-time detection
  closes 091's detection gap, that the real-Zen comparison moves to 095, and that this
  supersedes the log's earlier "set done once it matches" instruction; stage 5 then flips
  091 to `status: done`.
- The one thing still unverified — Zen's actual quota wording, and whether a quota 429
  lands as `APIError` in `opencode export` — moves to a new follow-up issue,
  095 (`095-zen-quota-wording-and-export-api-error.md`, next free number; 091-094 are
  taken), with the same "compare the first real `<run_id>.tail.txt`" instruction. Closing
  091 without parking that would lose the note.

## Non-goals

- **No settle scan in `pipeline_run`.** Every agent stage has a `fallback_model`
  (`pipeline_stages.py`), including stages 6 and 7, which post a review and reply to
  comments. A false positive on a successful stage would re-run it and double-post — the
  duplicate-side-effect risk the repo already designs around. `_session_api_error` is the
  right signal there, and it is screen-independent.
- **No new markers, no heuristic on `Retrying in`/`Error:`.** Duration and generic error
  prefixes are not quota.
- **No change to `agent_prompt_wait`'s poll interval.** 30 s stays; the scan is added where
  the screen is stable, not made more frequent.

## Files touched

- `src/herdr_routines/wait_loop.py` — new `settled_screen_marker`.
- `src/herdr_routines/runner.py` — call it on the no-report path; skip the nudge on a
  match; write the tail from the scan; choose `quota_exhausted` over `no_report`.
- `tests/test_runner.py` — corpus table test plus the settle-path, helper-contract,
  race, and docs tests below.
- `tests/test_tick.py` — one end-to-end fallback test through `routine_flow`.
- `docs/failure-reaping.md` — §3.2 detection table and the healthy-run wording fix.
- `docs/process/issues/091-opencode2-failure-markers.md` — log entry, flip to done.
- `docs/process/issues/095-zen-quota-wording-and-export-api-error.md` (new) — the residual
  above.

Not touched: `pipeline_run.py`, `pipeline_stages.py`, `config.py`,
`DEFAULT_FAILURE_MARKERS`, `scripts/pipeline-launch.sh`.

## Risks

- **+30 s on the no-report path.** Two reads, one poll interval apart, only for a run that
  is already failing. Any test that forgets to patch
  `wait_loop.WATCHDOG_POLL_INTERVAL_S` sleeps 30 s — the tests below must patch it.
- **Default-argument trap.** If `interval_s` is written as a default parameter rather than
  resolved at call time, the seam binds at import and every test silently waits 30 s. The
  dedicated call-time test is what catches this.
- **Report-during-scan race.** A report that lands during the 30 s scan must not be
  downgraded by a marker seen in the same window; the post-scan re-stat is what enforces
  it. Without it, a foreign toast could turn a just-completed `done` run into
  `quota_exhausted`.
- **Toast false positive.** A foreign quota toast outliving 30 s would file an already
  failed run as `quota_exhausted` instead of `no_report`; the only consequence is one
  fallback retry on a different model. It can never downgrade a `done` run, because a
  written report skips the scan. Toast duration is still unmeasured (091's log).
- **History reason shift.** Runs that used to end `no_report` may now end
  `quota_exhausted`. Both are in `VALID_RETRY_REASONS`, but a job whose explicit
  `retry_on` lists `no_report` and not `quota_exhausted` loses its tick-level retry.
  No deployed job does this; note it in the log entry.
- **The pipeline's residual stays open.** A stage that settles into the terminal shape
  with no `APIError` in its export still fails its gate without falling back. That is
  recorded in the follow-up issue rather than silently closed.
- **`_session_api_error` does not distinguish quota from 5xx** — pre-existing behaviour,
  out of scope.

## Acceptance criteria

Every item is **blocking** (must ship) or **non-blocking** (ship with a follow-up note if
it slips), carries a `confidence:` tier, and ends with the exact test that proves it.
Items 1-9, 12 and 13 live in `tests/test_runner.py`; item 10 is the existing pipeline
regression test in `tests/test_pipeline_run.py`; item 11 lives in `tests/test_tick.py`.

1. **blocking** — confidence: high — The captured corpus is a contract:
   `quota_shaped_retry`, `quota_shaped_settled_error` and
   `foreign_session_failed_toast` contain the default marker; `normal_reply`,
   `model_unavailable`, `http_401_settled_error`, `http_500_retry` do not.
   Test: `test_opencode2_screen_corpus_classification`
2. **blocking** — confidence: high — A run that settles `idle` with
   `quota_shaped_settled_error.txt` on screen and no report ends `failed /
   quota_exhausted`, never sends the report nudge, writes `<run_id>.tail.txt` from the
   matched screen, and closes its pane.
   Test: `test_settled_quota_screen_is_quota_exhausted_without_the_nudge`
3. **blocking** — confidence: high — The same screen with a written, non-empty report
   still ends `done` and reads the screen zero extra times.
   Test: `test_settled_quota_screen_never_downgrades_a_written_report`
4. **blocking** — confidence: high — A report that appears during the settle scan also
   wins: the run ends `done`, not `quota_exhausted`.
   Test: `test_settled_quota_screen_does_not_downgrade_a_report_written_during_the_scan`
5. **blocking** — confidence: high — Each non-quota settled fixture
   (`http_401_settled_error`, `http_500_retry`, `model_unavailable`, `normal_reply`)
   still ends `failed / no_report` after the nudge, so a bad key, a 5xx or a missing model
   is not reaped as an exhausted quota.
   Test: `test_settled_non_quota_screens_stay_no_report`
6. **blocking** — confidence: high — A single sighting of the marker (the foreign-session
   toast, followed by a clean screen) does not classify the run as quota.
   Test: `test_settled_marker_needs_two_consecutive_reads`
7. **blocking** — confidence: high — A marker appearing verbatim in the job's own prompt
   stays inert on the settle path too.
   Test: `test_settled_marker_in_prompt_is_inert`
8. **blocking** — confidence: high — `settled_screen_marker` itself reads exactly `reads`
   times, returns the marker only after two consecutive sightings, applies the
   marker-in-prompt guard, returns the last screen text, and swallows read errors.
   Test: `test_settled_screen_marker_contract`
9. **blocking** — confidence: high — The interval default resolves
   `wait_loop.WATCHDOG_POLL_INTERVAL_S` at call time: patching it to `0` after import
   collapses the wait to zero.
   Test: `test_settled_screen_marker_reads_interval_at_call_time`
10. **non-blocking** — confidence: medium — `pipeline_run.py` and `pipeline_stages.py`
    are untouched, and the pipeline's fallback still keys off `_session_api_error` rather
    than a settle screen scan.
    Test: `test_pipeline_run_resumes_a_stage_on_its_fallback_model_after_a_provider_error`
11. **non-blocking** — confidence: medium — `routine_flow`'s same-tick recovery now fires
    for the terminal shape: a job with `fallback_model` whose primary run settles into
    `quota_shaped_settled_error` records `quota_exhausted` and re-runs on the fallback
    model.
    Test: `test_fallback_model_retried_after_settled_quota_screen`
12. **non-blocking** — confidence: medium — `docs/failure-reaping.md` §3.2 names both
    opencode 2 shapes and their detection paths, states that the settle scan is limited to
    the already-failing no-report path (healthy runs stay unscanned), the 091 log records
    the finding and the move of the real-Zen comparison to 095, and the 095 file records
    that residual.
    Test: `test_failure_reaping_and_issue_091_document_both_opencode2_screen_shapes`
13. **blocking** — confidence: high — `DEFAULT_FAILURE_MARKERS` is unchanged and no new
    marker strings were added.
    Test: `test_default_failure_markers_unchanged`

## Changelog v1→v2

- Retitled to v2 and added this review record; v1's committed acceptance list was a first
  pass and is now expanded, renumbered, and given an explicit `confidence:` tier per item.
- Added the helper's own contract criterion (two consecutive sightings, prompt guard, last
  screen text, inert read errors) and the call-time `WATCHDOG_POLL_INTERVAL_S` criterion.
  v1 only exercised the helper through `execute_run`, so a default parameter that binds at
  import would have passed the suite while adding a real 30 s to every test.
- Added the report-during-scan criterion and the post-scan re-stat requirement. v1's
  "a written report is never rescanned" held only for a report present at the instant of
  settle; a report landing during the two 30 s-apart reads could be downgraded.
- Clarified the implementation on a match: write the scan's last screen as the tail
  exactly once, set `tail_written` so `runner.py:946` does not read a third time, and
  continue through the existing session/close/re-sign block instead of returning early
  (v1's "return through the existing machinery" was ambiguous about both).
- Corrected the nudge anchor from `runner.py:922` to `922-925`, specified
  `agent_read_visible(..., lines=200)` parity with `_capture_visible_tail`, and named the
  test seam explicitly (`herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S`).
- Replaced v1's meta "CI green" acceptance item with a behavioral
  `DEFAULT_FAILURE_MARKERS`-unchanged criterion; `scripts/ci-check.sh` remains the
  stage-5 gate, not a pytest test.
- Split the docs criterion into one named test covering `docs/failure-reaping.md` §3.2,
  the 091 log, and the new 095 file, and required §3.2's "does not inspect screens on
  healthy runs" wording to be reconciled with the new no-report-only settle scan.
- Added a non-goal guard criterion for the untouched pipeline path, referencing the
  existing provider-error fallback regression test.
- Pinned the follow-up issue to
  `docs/process/issues/095-zen-quota-wording-and-export-api-error.md` and required the 091
  log entry to state that the real-Zen comparison moves there, superseding the log's
  earlier "set done once it matches" instruction before stage 5 flips 091 to `done`.
