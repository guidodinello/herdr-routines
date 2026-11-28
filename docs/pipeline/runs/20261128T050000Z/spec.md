# Spec v2 — 20261128T050000Z — Close the pipeline first-prompt start race, park the week-long watch

Status: v2, stage-2 review complete.
Implements [issue 092](../../../process/issues/092-opencode2-prompt-stalled-after-start.md) only.
Background: `docs/plan-v1.md` (project context, not this feature).
v1 is in this file's git history; `## Changelog v1→v2` at the bottom records what the
review changed and why.

## Problem

Issue 092 tracks a post-opencode-2-start race: on 2026-11-24, a prompt sent right after
`herdr agent start` reported idle could return `agent_prompt_stalled` while opencode printed
"Starting background server…". PR #286 moved opencode to a persistent
`opencode-service.service` and herdr-routines refuses to start its own service, so the startup
wait that produced the stall should be gone.

Evidence is still insufficient to close the issue. The 2026-11-25 log entry records no
`agent_prompt_stalled` in `herdr-routines.log*` and no failed/stalled `history.jsonl` record
since deploy, but only a few smoke/scheduled/Telegram runs. The prompt retry path can also hide
a stall it absorbs, so absence of terminal failure is weaker evidence than absence of the error.

## Review findings (v2)

1. **Only half the code has a readiness gate.** `runner.execute_run` polls
   `client.agent_interactive_ready(agent_name)` through `_wait_for_agent_ready`
   (`runner.py:216-239`, called at `runner.py:783`) and bounds it by `start_timeout_ms` before
   its first prompt. `pipeline_run._start_and_prompt` (`pipeline_run.py:1620-1683`) calls
   `agent_start` and goes straight to `prompt_with_watchdog`, relying entirely on
   `PROMPT_RETRY_DELAYS_S` to absorb an early `agent_prompt_stalled`. Every pipeline stage
   prompt *is* a just-started agent's first prompt — the exact race 092 describes, already
   observed live in run `20261008T003531Z` stage 1 (39 s, `stage_prompt_failed`; see the
   comment at `pipeline_run.py:1668-1675`). That is an offline-provable gap, not speculation.

2. **The pipeline cannot execute v1's acceptance.** The week of production `history.jsonl`
   and `herdr-routines.log*` evidence lives on the Pi; this worktree cannot honestly claim to
   have inspected it. Worse, the pipeline itself flips the picked issue's frontmatter to
   `status: done` at bookkeeping (`pipeline_run.py:1244-1351`, `_flip_issue`), before the PR
   opens. Leaving 092's acceptance as "close after a week" would make this run close it a week
   early. v2 therefore closes only the offline-provable race and parks the time-gated watch in
   a new follow-up issue, `098-opencode2-prompt-stall-week-watch.md`, linked from `ROADMAP.md`
   next to 092. 092 stays `status: open` in this file's edits; the pipeline owns the flip.

3. **The retry whitelist is a safety boundary, not a knob.** `is_retryable_prompt_error`
   (`wait_loop.py:82-95`) resends only on exit 1 with a parsed JSON body whose string
   `error.code` is present and is not `timeout`. Broadening it to settle timeouts, wrapper
   timeouts (exit 124), exit-0 shape errors, or unknown delivery would risk double-prompting
   (v1's third risk). v2 keeps it byte-for-byte; the readiness gate removes the *cause* of the
   early rejection instead of hiding more of its symptoms.

## Approach

### 1. Reuse the runner readiness gate for pipeline stage prompts (new in v2)

In `pipeline_run._start_and_prompt`, after `agent_start` succeeds and before
`prompt_with_watchdog`, call the existing `runner._wait_for_agent_ready` (import it next to
`DEFAULT_FAILURE_MARKERS` at `pipeline_run.py:92`) with
`timeout_s=spec.start_timeout_ms / 1000` — the same bound `agent_start` already uses and the
same one `runner.execute_run` uses. The poll already inherits
`HerdrClient.agent_interactive_ready`'s fail-open shape handling (`herdr.py:856-871`), swallows
`HerdrCliError`/`OSError` from individual polls, and honours the existing
`runner.READY_POLL_INTERVAL_S` test seam. Do not add a second poll loop or a new CLI shape.

When the bound expires: close the stage pane (the function's existing `_close_pane` contract
must hold on every abort), send **no** prompt, and raise `_Abort("stage_agent_not_interactive",
detail=...)` carrying the error text `agent <name> did not report interactive_ready within
<ms>ms of start` plus any last poll error, so `run_pipeline` records a clear reason instead of
a stall. A ready result changes nothing else: the existing whitelisted retry remains as
defence in depth, and a provably-early `agent_prompt_stalled` still resends at most twice
without ever double-delivering.

### 2. Record evidence over a week of scheduled runs (v1's plan, now issue 098)

- Inspect production evidence on the Pi (not in this worktree): `history.jsonl` and
  `herdr-routines.log*` for `agent_prompt_stalled`, focusing on runs whose first prompt follows
  an opencode start.
- Record each check in the follow-up issue's log with date, evidence source, and whether any
  `agent_prompt_stalled` occurred on an opencode start.
- After a week of scheduled runs with no such occurrence, flip the follow-up issue to
  `status: done` and note the closing evidence. Keep the ROADMAP entry pointing at 092 (now
  code-fixed) and add one for 098; do not silently drop either issue.
- Append one entry to 092's log stating that the offline race was fixed here and that the
  time-gated confirmation moved to 098. Do **not** claim a week of evidence was inspected and
  do **not** edit 092's frontmatter in this run.

### 3. If the stall still occurs, record and fix narrowly (unchanged from v1)

If `agent_prompt_stalled` is observed on an opencode start after #286:

- Record when it happened (timestamp, run/job, agent kind/model, log excerpt, whether prompt
  retry absorbed it).
- Confirm the error shape is provably early and delivery did not occur. `agent_prompt_stalled`
  is currently treated as a stage-prompt start-race case in pipeline runs
  (`pipeline_run.py` passes `retry_delays_s=PROMPT_RETRY_DELAYS_S` and relies on
  `wait_loop.is_retryable_prompt_error`); routine/audit/gated jobs already go through
  `wait_loop.prompt_with_watchdog` / `runner._prompt_with_retry`.
- Apply the smallest fix that matches the evidence:
  - Extend prompt retry classification only if `agent_prompt_stalled` is provably early and not
    already covered by the existing whitelist. Do not broaden retries to settle timeouts,
    wrapper timeouts, or unknown-delivery errors.
  - Or strengthen readiness before first prompt: wait/poll for `interactive_ready` (and/or a
    stronger opencode-ready signal) after `agent start` when the observed failure indicates the
    current readiness gate is insufficient. Keep the wait bounded and fail with a clear reason.

Any code fix must add a regression test that reproduces the observed error shape and proves no
double-prompt on ambiguous delivery.

## Files touched

Expected for this run:

- `src/herdr_routines/pipeline_run.py` — call the shared bounded readiness poll between
  `agent_start` and the first stage prompt; abort clearly and close the pane on timeout.
- `tests/test_pipeline_run.py` — add an `agent_interactive_ready` seam to the fake clients
  (default ready, so existing tests keep passing) and the acceptance tests below.
- `tests/test_runner.py` — pin the unchanged retry whitelist across both call sites.
- `docs/process/issues/092-opencode2-prompt-stalled-after-start.md` — append the offline-fix /
  follow-up log entry; leave `status: open` for the pipeline to flip.
- `docs/process/issues/098-opencode2-prompt-stall-week-watch.md` — new issue holding the
  week-long production watch, with the same `status: open` frontmatter shape as 092.
- `ROADMAP.md` — one new bullet linking 098 next to the existing 092 bullet.
- `docs/pipeline/runs/20261128T050000Z/spec.md` — this spec (v2).

Not touched: `src/herdr_routines/wait_loop.py`, `src/herdr_routines/herdr.py`,
`src/herdr_routines/runner.py`'s poll body, `PROMPT_RETRY_DELAYS_S`, or any retry classification.

## Risks

- **Prompt retry can hide real stalls.** A clean terminal history may mask retries that absorbed
  `agent_prompt_stalled`; evidence should look for the error in logs, not only failed outcomes.
  The readiness gate removes the common cause but does not make the log check unnecessary —
  hence issue 098.
- **Production evidence lives outside the worktree.** `history.jsonl` and system logs are on the
  Pi; this run can plan and document, but cannot honestly claim week-long production evidence it
  did not inspect. The 092-to-done flip is justified by the code fix only.
- **Over-broad retry is dangerous.** Treating non-provably-early errors as retryable risks
  double-prompting and duplicate side effects. Any change must preserve the existing delivery
  safety rules; the whitelist is deliberately unchanged here.
- **Readiness waits can hide a different failure.** Waiting longer for `interactive_ready` may
  convert a fast failure into a timeout without fixing the underlying opencode startup problem;
  the wait is bounded by the existing `start_timeout_ms` and fails with a named reason, and the
  root cause stays visible for issue 098's operator checks.

## Acceptance criteria

Every item is **blocking** (must ship) or **non-blocking** (ship with a follow-up note if it
slips), carries a `confidence:` tier, and ends with exactly one exact test name. Items 1-8 are
new tests; 9-10 are new doc/meta tests. Stage 3 writes each named test under `tests/` with that
exact function name; stage 4 implements the behaviour. Existing tests
`test_pipeline_run_retries_a_stage_prompt_rejected_while_the_agent_starts` and
`test_pipeline_run_records_real_stage_sessions` must stay green unchanged.

1. **blocking** — confidence: high — `pipeline_run._start_and_prompt` calls
   `client.agent_interactive_ready(agent_name)` after a successful `agent_start` and before
   `prompt_with_watchdog`; no prompt is sent while readiness is false.
   Test: `test_pipeline_stage_waits_for_interactive_ready_before_first_prompt`
2. **blocking** — confidence: high — When readiness never becomes true within
   `spec.start_timeout_ms`, the stage sends no prompt, closes its pane, and aborts with reason
   `stage_agent_not_interactive` and an error naming the agent and the timeout bound.
   Test: `test_pipeline_stage_readiness_timeout_aborts_without_prompt`
3. **blocking** — confidence: high — `HerdrCliError` and `OSError` from an individual readiness
   poll are swallowed and polled again, both on the pipeline path and in the shared helper;
   one transient poll error never aborts the stage.
   Test: `test_pipeline_stage_readiness_poll_errors_are_inert`
4. **blocking** — confidence: high — The pipeline reuses `runner._wait_for_agent_ready` and
   `HerdrClient.agent_interactive_ready` (no second poll loop, no new CLI shape), so a response
   without `interactive_ready` still fails open and the malformed-shape error is retried like
   any other poll error.
   Test: `test_pipeline_stage_readiness_uses_shared_client_probe`
5. **blocking** — confidence: medium — Every `agent_start` — fresh stages, the stage-7 resume,
   and a fallback-model restart — is followed by a readiness poll before its prompt; no stage
   bypasses the gate.
   Test: `test_every_pipeline_stage_including_fallback_gates_on_readiness`
6. **blocking** — confidence: high — `is_retryable_prompt_error` keeps its exact contract: exit 1
   with a dict body and a string `error.code` other than `timeout` (including
   `agent_prompt_stalled`) is retryable, while exit 124, exit 0 shape errors, `timeout`, a
   missing code, and a non-dict body are not.
   Test: `test_prompt_retry_whitelist_unchanged_for_stalled_and_terminal_shapes`
7. **blocking** — confidence: high — An ambiguous delivery is still never retried: a stage
   prompt failing with `error.code == "timeout"` or wrapper exit 124 aborts `stage_prompt_failed`
   after exactly one delivery, on the pipeline path and via `runner._prompt_with_watchdog`.
   Test: `test_pipeline_stage_never_retries_unknown_delivery`
8. **blocking** — confidence: high — With readiness already true, a provably-early
   `agent_prompt_stalled` is still absorbed by the existing retry and a full pipeline run ends
   `ok` with exactly one delivered prompt per stage.
   Test: `test_pipeline_run_stage_retry_still_delivers_once_after_readiness`
9. **non-blocking** — confidence: medium — Issue 092's log records the offline fix and the
   hand-off without claiming a week of evidence, a follow-up issue
   `docs/process/issues/098-opencode2-prompt-stall-week-watch.md` exists with `status: open`, and
   `ROADMAP.md` links it; 092's own frontmatter is still `status: open` in the committed diff.
   Test: `test_issue_092_log_and_followup_watch_are_recorded`
10. **non-blocking** — confidence: medium — Meta: lint, strict mypy and the full test suite are
    green through `scripts/ci-check.sh`, with the blocking criteria run first.
    Test: `test_ci_green_with_opencode_readiness_blocking_tests_run_first`

`scripts/ci-check.sh` is the stage-5 gate, not a pytest test.

## Review

- blocking: 1-8 — the bounded readiness gate on every pipeline first prompt (1-5), an unchanged
  retry whitelist and no double-prompt on ambiguous delivery (6-8). These are the offline-provable
  part of 092 and the reason its flip to done is honest.
- non-blocking: 9-10 — the evidence hand-off to 098/ROADMAP and the CI meta check. The week-long
  watch itself is real work but belongs to issue 098, not to a code run.
- confidence: high — items 1-4, 6-8 are deterministic through fake `HerdrClient` probes and
  error bodies; the call order, timeout verdict, and retry classifications are all inspectable
  without the Pi.
- confidence: medium — item 5 needs the fake to cover fresh, resumed and fallback starts; item 9
  is a docs assertion, and item 10 depends on the host's CI toolchain. None of them claim that
  production evidence was collected here.
- confidence: low — none. The remaining uncertainty (whether opencode 2 still stalls after a
  week of scheduled runs) is explicitly outside this spec and tracked in issue 098.

## Changelog v1→v2

- Retitled the document **v2**, marked the stage-2 review complete, and kept v1's Problem and
  Approach substance in the same file (v1 itself remains in git history).
- Added a `## Review findings (v2)` section with three corrections to v1's framing: (a) the
  pipeline stage path has no `interactive_ready` gate while `runner.execute_run` does, and run
  `20261008T003531Z` is a recorded instance of that gap; (b) `pipeline_run._flip_issue` will mark
  092 done before a week of evidence exists, so v1's "close after a week" cannot be this run's
  acceptance; (c) the retry whitelist must stay unchanged, since broadening it is the double-
  prompt risk v1 itself flagged.
- Reconciled "observational first" with the pipeline's mechanical issue flip: v2 implements the
  offline-provable readiness fix, appends a log entry to 092 without touching its frontmatter,
  and moves the time-gated watch into a new issue 098 linked from `ROADMAP.md`. The ROADMAP entry
  for 092 is kept as v1 asked; 098 is an addition, not a replacement.
- Added `## Acceptance criteria` with 10 numbered items, each ending in exactly one exact
  named-test anchor for stages 3-4 and gate 3. Every item carries a **blocking**/**non-
  blocking** tier and a `confidence:` tier; the `## Review` summary repeats the split.
- Split v1's single "strengthen readiness" option into distinct criteria for ordering (1), the
  bounded fail-closed abort (2), inert poll errors (3), reuse of the shared probe (4), and
  coverage of resumed/fallback starts (5), so a failing test points at one behaviour.
- Pinned the negative side of v1's "do not broaden retries" rule as tests (6-7) rather than
  prose, and added (8) to prove the existing early-stall retry and one-delivery-per-stage
  invariant survive the new gate.
- Updated `## Files touched` to name the two docs changes and the new issue file, and added an
  explicit not-touched list (`wait_loop.py`, `herdr.py`, `runner.py`'s poll body,
  `PROMPT_RETRY_DELAYS_S`, all retry classification).
- Expanded `## Risks` with the v2 mitigations: the readiness wait is bounded and names its
  failure, and the log check remains necessary (issue 098) even after the race is fixed offline.
