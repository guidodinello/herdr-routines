# Spec v1 — 20261126T050000Z — Detect opencode 2's terminal quota screen

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
gated on `outcome.reason == "quota_exhausted"` (`routine_flow.py:210-212`), so
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

- Reads the visible screen `reads` times, `interval_s` apart, defaulting to
  `WATCHDOG_POLL_INTERVAL_S` **read at call time** (the same seam as
  `PROMPT_RETRY_DELAYS_S`, so tests collapse it to 0).
- Returns the marker only when the same marker is seen on two consecutive reads, reusing
  `matched_failure_marker` so the marker-in-prompt guard applies unchanged. Returns the
  last screen text as well, so the caller persists the tail without a third read.
- A read error is inert; never raises. Mirrors the existing poll contract.

The two-read gate is kept on purpose: a foreign session's "Session failed / Free usage
exceeded" toast can render over this pane (`foreign_session_failed_toast.txt`), and the
existing stability rule is what keeps one sighting from reaping a healthy run.

### 2. Use it on the no-report path in `runner.execute_run`

Only when the agent settled to a success status **and** the report is missing or empty —
i.e. the run is already failing:

- Scan before `_attempt_report_nudge` (`runner.py:922`). On a match, skip the nudge (an
  exhausted model cannot answer it; this saves up to 120 s) and return through the
  existing machinery — tail written, session id captured, pane closed, commits re-signed —
  with `state="failed"`, `reason="quota_exhausted"`,
  `error=f"failure marker matched: {marker!r} (settled screen)"`.
- No match: today's path exactly, nudge included, ending in `no_report`.
- A written, non-empty report is never rescanned, so a `done` run cannot be downgraded.

No marker strings are added or changed. The corpus shows verbatim passthrough; guessing
new phrasings would reap the 401/5xx/unavailable screens as quota, which 091's log
explicitly rules out.

### 3. Record the comparison, and the residual

- `docs/failure-reaping.md` §3.2 gains the two opencode 2 shapes and which detection path
  covers each, pointing at the fixtures.
- Issue 091's log gets the finding, and stage 5 flips it to `status: done`.
- The one thing still unverified — Zen's actual quota wording, and whether a quota 429
  lands as `APIError` in `opencode export` — moves to a new follow-up issue (next free
  number, 095 unless taken) with the same "compare the first real `<run_id>.tail.txt`"
  instruction. Closing 091 without parking that would lose the note.

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
  match; choose `quota_exhausted` over `no_report`.
- `tests/test_runner.py` — corpus table test plus the settle-path tests below.
- `tests/test_tick.py` — one end-to-end fallback test through `routine_flow`.
- `docs/failure-reaping.md` — §3.2 detection table.
- `docs/process/issues/091-opencode2-failure-markers.md` — log entry, flip to done.
- `docs/process/issues/095-*.md` (new) — the residual above.

Not touched: `pipeline_run.py`, `pipeline_stages.py`, `config.py`,
`DEFAULT_FAILURE_MARKERS`, `scripts/pipeline-launch.sh`.

## Risks

- **+30 s on the no-report path.** Two reads, one poll interval apart, only for a run that
  is already failing. Any test that forgets to patch
  `wait_loop.WATCHDOG_POLL_INTERVAL_S` sleeps 30 s — the tests below must patch it.
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

Each item is **blocking** (must ship) or **non-blocking** (ship with a follow-up note if
it slips), with a confidence tier and the exact test that proves it.

1. **blocking / high** — The captured corpus is a contract: `quota_shaped_retry`,
   `quota_shaped_settled_error` and `foreign_session_failed_toast` contain the default
   marker; `normal_reply`, `model_unavailable`, `http_401_settled_error`,
   `http_500_retry` do not. Test: `test_opencode2_screen_corpus_classification`
2. **blocking / high** — A run that settles `idle` with `quota_shaped_settled_error.txt`
   on screen and no report ends `failed / quota_exhausted`, never sends the report nudge,
   writes `<run_id>.tail.txt` and closes its pane. Test:
   `test_settled_quota_screen_is_quota_exhausted_without_the_nudge`
3. **blocking / high** — The same screen with a written, non-empty report still ends
   `done` and reads the screen no extra time. Test:
   `test_settled_quota_screen_never_downgrades_a_written_report`
4. **blocking / high** — Each non-quota settled fixture (`http_401_settled_error`,
   `http_500_retry`, `model_unavailable`, `normal_reply`) still ends
   `failed / no_report` after the nudge, so a bad key, a 5xx or a missing model is not
   reaped as an exhausted quota. Test: `test_settled_non_quota_screens_stay_no_report`
5. **blocking / high** — A single sighting of the marker (the foreign-session toast,
   followed by a clean screen) does not classify the run as quota. Test:
   `test_settled_marker_needs_two_consecutive_reads`
6. **blocking / high** — A marker appearing verbatim in the job's own prompt stays inert
   on the settle path too. Test: `test_settled_marker_in_prompt_is_inert`
7. **non-blocking / medium** — `routine_flow`'s same-tick recovery now fires for the
   terminal shape: a job with `fallback_model` whose primary run settles into
   `quota_shaped_settled_error` records `quota_exhausted` and re-runs on the fallback
   model. Test: `test_fallback_model_retried_after_settled_quota_screen`
8. **non-blocking / medium** — `docs/failure-reaping.md` §3.2 names both opencode 2 shapes
   and their detection paths, and the follow-up issue records the residual. Test:
   `test_issue_091_log_records_both_opencode2_screen_shapes`
9. **non-blocking / medium** — Meta: lint, strict mypy and the full suite are green
   through `scripts/ci-check.sh`, and `DEFAULT_FAILURE_MARKERS` is unchanged. Test:
   `test_ci_green_with_settled_quota_detection`
