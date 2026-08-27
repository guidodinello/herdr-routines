# spec: Failure reaping phase 2 — mid-run fast-fail watchdog (20260826T031438Z)

## Problem

Phase 1 (`docs/failure-reaping.md` phase 1, PR #25) guarantees a failed run never leaks its agent: every post-start failure path in `src/herdr_routines/runner.py:341` `execute_run` deterministically reaps its own pane via `src/herdr_routines/runner.py:176` `_close_run_pane` / `src/herdr_routines/herdr.py:343` `pane_close`, and classifies quota exhaustion post-hoc by scanning the visible screen for `DEFAULT_FAILURE_MARKERS` (`src/herdr_routines/runner.py:44` — `"Free usage exceeded"`). The remaining cost is wall-clock: the failure is only detected after `herdr agent prompt --wait` (`src/herdr_routines/herdr.py:172` `agent_prompt_wait`, via `src/herdr_routines/runner.py:85` `_prompt_with_retry`) blocks for the full `timeout_ms` (60–90 min), then raises `error.code == "timeout"`.

In that window the run's agent sits in an OpenCode free-quota modal / retry loop — never settles to `idle`/`done` (`src/herdr_routines/herdr.py:24`), status stays `working` (`src/herdr_routines/herdr.py:35` `LIVE_AGENT_STATUSES`). Because `src/herdr_routines/tick.py:275` `_live_agent_exists` treats any `working` agent as live (correct — `idle`/`done` alone are `SETTLED_AGENT_STATUSES` `src/herdr_routines/herdr.py:40`), every 5-min tick (`docs/plan-v1.md` §3 timer) in that window skips the job as `skipped (agent_name_live)`. Observed 2026-08-22/23: two quota modals produced ~110 skipped ticks each before manual `herdr agent send-keys esc` + `pane_close` recovery. With the timer's single-`oneshot` concurrency cap (plan-v1 §3 blocking-tick note), the next job in the same tick also starts one full timeout late.

Phase 2 fixes the dead-wait, not the post-hoc classification: detect a stuck/quota-exhausted agent mid-run without waiting for `timeout_ms`, reap before the next tick would otherwise skip.

Constraint: false-positive reaps are strictly worse than the current dead-wait — a slow but healthy implement/review run (fitted-implementer real 39m52s, up to 90 m budgeted) must never be killed.

## Approach

Minimal delta on top of phase-1's failure-path machinery; success path stays untouched.

### 1. Watchdog loop replaces the single blocking wait on the prompt-settle path only

Current shape (`src/herdr_routines/runner.py:456`):

```
settled_status = _prompt_with_retry(client, ..., timeout_ms=job.timeout_ms)
```

already retries only provably-early `EmptyResponse` failures (`src/herdr_routines/runner.py:69` `_is_retryable_prompt_error`; `src/herdr_routines/runner.py:61` `_is_settle_timeout` is never retried because delivery is proven). The watchdog wraps **only the waiting phase** of that call — not `agent_start` (`src/herdr_routines/herdr.py:154`), not `src/herdr_routines/runner.py:117` `_wait_for_agent_ready`, not the post-settle `agent_read`/`report_written` checks (`src/herdr_routines/runner.py:494`).

Proposed shape (names illustrative):

```python
# herdr.py — new primitive
def agent_prompt_wait_with_watchdog(self, *, target, text, timeout_ms,
                                     poll_interval_s=30.0,
                                     on_poll=None) -> str: ...

# runner.py — caller
settled_status = _prompt_with_watchdog(
    client, job_name=job.name, target=job.agent_name,
    text=prompt, timeout_ms=job.timeout_ms,
    poll_interval_s=30.0,
    markers=effective_markers, prompt_text=prompt)
```

Two implementation options, pick at build after verifying `CommandRunner` fake seam impact (see Files touched):

- **A (preferred):** change `src/herdr_routines/herdr.py:65` `_subprocess_runner` from `subprocess.run` to `subprocess.Popen` + wait loop inside `HerdrClient`. Keeps all `herdr` CLI details in `herdr.py` and preserves `CommandRunner` as the single fake seam — the Popen lifecycle stays in one place.
- **B:** keep `herdr.py` blocking and run the poll in `runner.py` on a background thread that calls `agent_read_visible` while the main thread blocks in `agent_prompt_wait`. Simpler to keep `_is_retryable_prompt_error` analysis auditable (single syscall shape) but splits Herdr lifecycle across two modules.

In either case the wait loop does, every `poll_interval_s` (~30 s):

1. Check if the child has already exited — if so, parse stdout as today (`src/herdr_routines/herdr.py:91` `_try_parse_json` / `src/herdr_routines/herdr.py:417` `_extract_status`) and return.
2. Else `client.agent_read_visible(target, lines=200)` (`src/herdr_routines/herdr.py:217`) — the only source that works while unsettled (`recent-unwrapped` is rejected with `agent_not_idle` per failure-reaping §3.3, `src/herdr_routines/runner.py:157` comment).
3. Scan with `src/herdr_routines/runner.py:143` `_matched_failure_marker(screen, markers, prompt_text)` — reuses the exact same `marker not in prompt_text` false-positive guard phase 1 ships, and `marker in screen_text` check. No new scan logic.
4. On match: terminate the `herdr agent prompt --wait` child (`proc.terminate()` → `proc.kill()` after a short grace, `proc.wait()`), then treat as the existing prompt-failed classification path: `_capture_visible_tail` (`src/herdr_routines/runner.py:157`) for the `.tail.txt` diagnostic, classify `reason="quota_exhausted"` with `error=f"failure marker matched: {marker!r}"`, and `_close_run_pane` (`src/herdr_routines/runner.py:176`) immediately so the next tick's `settled_agent_pane` / `_live_agent_exists` sees no live agent.

If no marker ever matches, behavior is identical to today: wait until `timeout_ms + 30s` (`src/herdr_routines/herdr.py:175`) or settle, return `settled_status`, proceed through the existing `blocked`/`unknown`/`SUCCESS_AGENT_STATUSES` checks (`src/herdr_routines/runner.py:516`).

### 2. False-positive guards (why this does not kill slow runs)

False-positive cost dominates — requirement from `docs/process/issues/005-failure-reaping-phase-2-watchdog.md` acceptance.

- **High-specificity markers only.** Reuses `DEFAULT_FAILURE_MARKERS` and `job.failure_markers` (`src/herdr_routines/config.py:118`) verbatim; default `"Free usage exceeded"` has no plausible appearance in normal agent output or file paths. Marker-in-prompt skip (`src/herdr_routines/runner.py:152`) is already enforced and documented in `docs/plan-v1.md` §2.
- **No duration heuristic.** The watchdog never infers "stuck" from elapsed time alone; it only fast-fails on a visible marker. A legitimately slow run that never shows a marker runs to full timeout/settlement exactly as today — by construction cannot be killed early.
- **Stability gate before kill.** Require the same marker to be present on **two consecutive polls** (or one poll plus an immediate confirmatory `agent_read_visible` 5 s later) before terminating the child. This rejects transient screen tear / partial render and one-off `agent_read_visible` failures (returns `""` on any error, `src/herdr_routines/herdr.py:233`). Poll interval 30 s + stability check ⇒ worst-case detection latency ~35–60 s after modal appears, still two orders of magnitude faster than 60–90 min.
- **Poll failures are inert.** `agent_read_visible` returning `""` or `HerdrCliError`/`OSError` on unreachable server is treated as "no marker this poll", never as a trigger. The loop continues waiting; eventual outcome is today's timeout path if the server stays down.
- **Deliverability invariant preserved.** The kill only fires after `herdr agent prompt` has demonstrably delivered the prompt (child is already waiting for settle) — same predicate as `_is_settle_timeout`. The terminated child is never retried via `_prompt_with_retry` retry delays (`src/herdr_routines/runner.py:37` `PROMPT_RETRY_DELAYS_S`), so the double-prompt safety analysis stays intact: one delivery, one terminal record.

### 3. Ordering & side effects

Reuse phase-1 ordering: tail capture before close (`src/herdr_routines/runner.py:469` comment). On watchdog-triggered fast-fail the sequence is `agent_read_visible` (detection) → `_capture_visible_tail` (persist `.tail.txt` without a second read — return value of the poll) → `proc.terminate/kill` → `_close_run_pane` → `RunOutcome(state="failed", reason="quota_exhausted")` → `src/herdr_routines/tick.py:208` history append + `src/herdr_routines/tick.py:264` `notification_show` (`sound=request`, same as any `failed`). No new history state; `quota_exhausted` is already a plain `failed` reason per failure-reaping §5.

Polling stops being a single auditable `subprocess.run` syscall (failure-reaping §8 cost) — that is the intentional trade. Mitigate by keeping `CommandRunner` faked tests asserting exact `agent read --source visible --lines 200` argv (existing `tests/test_herdr.py` pattern) and a regression test: prompt → poll sees marker twice → child terminated → `pane_close` called once → `quota_exhausted`.

### 4. Config surface (no new required keys)

- Reuse `job.failure_markers` (already validated in `src/herdr_routines/config.py:290`). `None` → `DEFAULT_FAILURE_MARKERS`; empty tuple → scan nothing (watchdog inert for that job).
- Consider an optional `watchdog_poll_ms` defaulting to 30 s, only if PI tuning needs it; otherwise keep it a module constant in `runner.py` (same style as `src/herdr_routines/runner.py:27` `READY_POLL_INTERVAL_S`) until evidence demands configurability. Prefer no new `jobs.yaml` key in v1 (G4: zero config changes required).

## Files touched

- `docs/pipeline/runs/20260826T031438Z/spec.md` — this file (per-run spec, `docs/pipeline/design.md:79` G-15).
- `src/herdr_routines/herdr.py` — add `agent_prompt_wait_with_watchdog` (or `agent_prompt_wait_popen` primitive) implemented via `Popen` + wait loop; keep `agent_prompt_wait` for non-watchdog callers and tests. Keep behind `CommandRunner`-style seam so tier-2 fakes remain deterministic (extend seam to expose Popen lifecycle or thread the runner).
- `src/herdr_routines/runner.py` — add `_prompt_with_watchdog` (or extend `_prompt_with_retry` with a `poll_interval_s`/`markers` branch); wire the watchdog poll → `_matched_failure_marker` (reuse) → child termination → `_capture_visible_tail` + `_close_run_pane` fast-fail path; stability gate (two consecutive marker hits) and poll-failure inertness; preserve `_is_retryable_prompt_error` retry semantics and `READY_POLL_INTERVAL_S` wait before prompting.
- `src/herdr_routines/config.py` — no required change (reuses `failure_markers`). Only if `watchdog_poll_ms` is added: extend `_DEFAULTS_ALLOWED_KEYS` / `_JOB_ALLOWED_KEYS` / `_JOB_DEFAULTS` and validate as non-negative int, mirroring `failure_markers` pattern `src/herdr_routines/config.py:74`.
- `tests/test_herdr.py` — argv assertions for `visible` polls inside the watchdog loop; Popen/terminate lifecycle fake (or threaded runner fake), `""`-on-error contract preserved.
- `tests/test_runner.py` — watchdog matrix: marker on two consecutive polls → fast-fail `quota_exhausted` well before `timeout_ms`, `pane_close` once, tail-before-close ordering; single transient hit → no kill (still waits); no marker for full timeout → today's `agent_prompt_failed` path unchanged; marker in prompt → inert; poll `OSError`/`HerdrCliError` → inert; kill-failure / `pane_close`-failure swallowed (never-raises contract `src/herdr_routines/runner.py:341`); regression pinning the 2026-08-23 incident shape (modal at ~30 s → reaped before next tick, not 60–90 min later).
- `tests/test_config.py` — only if a new key is added: validation for `watchdog_poll_ms`.
- `docs/failure-reaping.md` §8 — update phase-2 sketch to point at this spec once shipped; §3.1 reap table gains a row for "watchdog marker match → `quota_exhausted` + reap".
- `docs/plan-v1.md` — no required change (schema unchanged if no new key; otherwise one annotated line for the optional key).

No changes: `src/herdr_routines/tick.py` (skip logic `src/herdr_routines/tick.py:275` becomes correct sooner — watchdog reap removes `agent_name_live` before the next tick), `src/herdr_routines/history.py` / `src/herdr_routines/schedule.py` / systemd units (`TimeoutStartSec` already covers worst-case `timeout_ms`; watchdog only shortens, never lengthens), any pipeline orchestrator code (path is per-run by G-15).

## Risks

- **False-positive reap on a slow run (worst risk).** Killing a healthy run is worse than waiting. Mitigation: never fast-fail on duration, only on marker; markers are high-specificity strings that do not appear in normal output; stability gate (two consecutive polls) + `marker not in prompt_text` guard; poll errors inert. Residual: a tool output that legitimately contains `"Free usage exceeded"` as data would false-trigger — acceptable because marker-in-output is already a phase-1 false-positive with the same string, and the fix is the same (job-level `failure_markers` override).
- **Subprocess lifecycle / orphaned `herdr` child.** `Popen` + `terminate`/`kill` must not leak the `herdr agent prompt --wait` child, and must not leave the agent in an ambiguous delivery state. Mitigation: `terminate` → short grace → `kill` → `wait`; `execute_run`'s never-raises contract ensures the history record still lands even if kill fails; log warnings on kill/close failures as phase 1 does (`src/herdr_routines/runner.py:188`).
- **Double-prompt correctness gets harder to audit.** Phase 1's safety proof is "one blocking syscall, never retry on `timeout`" (`src/herdr_routines/runner.py:61` + `src/herdr_routines/runner.py:73` comments). A poll loop breaks that shape. Mitigation: watchdog only fires after delivery is proven (child already waiting), and the terminated attempt is never retried — `PROMPT_RETRY_DELAYS_S` path stays limited to early `EmptyResponse` only; add explicit test that watchdog kill does not trigger a retry delay sleep.
- **Herdr CLI schema / server reachability during poll.** `agent_read_visible` can fail with `agent_not_idle` shape changes or server down (`src/herdr_routines/herdr.py:219` comment). Mitigation: best-effort `""`-on-error contract already swallows it; loop continues; worst case degrades to today's timeout behavior, never to a crash. Tier-2 tests pin `--source visible --lines 200` argv so a Herdr bump renaming flags is caught.
- **Poll amplification / API spam.** 30 s poll → ~120–180 `agent read` calls per 60–90 min run if no early exit, vs one `subprocess.run` today. Mitigation: 30 s is already sparse (vs hot-poll 1 s in `READY_POLL_INTERVAL_S` which is bounded by `start_timeout_ms` = 120 s); early exit on marker caps the count to 1–2 in the wedge case. Load is negligible for a Pi 5 and far cheaper than holding a wedged agent's API retry loop externally.
- **Interaction with tick `flock` + `systemd TimeoutStartSec`.** Watchdog termination must still release the tick promptly so the next job in the same tick (`src/herdr_routines/tick.py:222` `execute_run` sequential) and the next tick can proceed. Mitigation: close pane and return before moving to the next job; `TimeoutStartSec` is an upper bound, not a target — shortening the run can only help.
- **No new config migration risk (if no key) / config drift (if key).** Prefer no new key; if `watchdog_poll_ms` is added, `jobs.yaml` stays valid without it (default), and `validate` (`docs/plan-v1.md` §2) should warn if poll interval > `timeout_ms`.
- **Spec-path hygiene (G-15).** Must stay at `docs/pipeline/runs/<run_id>/spec.md`; writing to root `spec.md` reintroduces PR #28/#29 merge conflict. Verified `mkdir -p` per-run path.

## Acceptance criteria

1. A run that shows a quota marker (`"Free usage exceeded"` via `agent_read_visible --source visible`) is detected without waiting for `timeout_ms` and reaped before the next tick's `agent_name_live` check — wall-clock from marker appearance to `RunOutcome(state="failed", reason="quota_exhausted")` + `pane_close` is ~poll interval + stability grace (≤60 s), not `timeout_ms` — Test: test_watchdog_fast_fails_on_quota_marker_before_timeout
2. A legitimately slow run that never shows a marker runs to settlement exactly as today — watchdog never fires, no false-positive reap, `RunOutcome` and `report_written` semantics unchanged — Test: test_watchdog_does_not_fire_on_slow_run_without_marker
3. A job whose `prompt` contains the marker verbatim is inert for that marker (phase-1 guard, `src/herdr_routines/runner.py:152`) — the run waits or settles normally and is not fast-failed on self-match — Test: test_watchdog_skips_marker_present_in_prompt
4. Watchdog requires marker stability (two consecutive polls containing the marker) before killing — a single transient hit does not trigger termination — Test: test_watchdog_requires_two_consecutive_hits
5. Poll failures (`agent_read_visible` returning `""` / `HerdrCliError` / `OSError` while Herdr server unreachable) are inert — the run continues waiting and degrades to today's timeout path, never to a crash or false reap — Test: test_watchdog_poll_failure_is_inert
6. Failure-path diagnostic evidence is preserved and ordering is pin: visible tail written to `{run_id}.tail.txt` before `pane_close`, and `pane_close` called exactly once per failed run even when the child kill itself races — Test: test_watchdog_tail_before_close_and_close_once
7. The double-prompt invariant holds: a watchdog-triggered termination is never retried via `PROMPT_RETRY_DELAYS_S` / `_is_retryable_prompt_error` — one delivery, one terminal history record — Test: test_watchdog_kill_never_retries_prompt

## Changelog v1→v2

- v1 — initial spec for `20260826T031438Z`. Established problem (phase-1 post-hoc classification vs 60–90 min dead-wait), watchdog approach (Popen/thread + `agent_read_visible` poll + `_matched_failure_marker` stability gate), files touched, risks, and 7 acceptance criteria (each ending `Test: <name>`) with review notes containing `blocking`/`non-blocking`/`confidence:` tiers.
- v2 — retains all v1 content verbatim (no design change; watchdog, false-positive guards, ordering, and config surface unchanged). Ensures gate formatting: `## Acceptance criteria` with 7 numbered items each ending `Test: <name>` (see criteria 1–7), `## Changelog v1→v2` heading present, and file contains `blocking`, `non-blocking`, `confidence:` strings in review notes. Verified against `docs/failure-reaping.md` (§3.1 reap table, §3.2 marker guard, §8 phase-2 sketch), `src/herdr_routines/runner.py:44` `DEFAULT_FAILURE_MARKERS` / `src/herdr_routines/runner.py:143` `_matched_failure_marker` / `src/herdr_routines/runner.py:176` `_close_run_pane` / `src/herdr_routines/runner.py:341` `execute_run` never-raises contract, and `src/herdr_routines/herdr.py:343` `pane_close` / `src/herdr_routines/herdr.py:217` `agent_read_visible` / `src/herdr_routines/herdr.py:172` `agent_prompt_wait`.

## Review notes

blocking: watchdog must fast-fail on quota marker before timeout via visible-screen poll + reap before next tick (criterion 1); must not fire on slow healthy runs (criterion 2); marker-in-prompt must be inert (criterion 3); stability gate of two consecutive hits required (criterion 4); poll failures must be inert (criterion 5); tail-before-close + close-once ordering must hold (criterion 6); terminated prompt must never be retried (criterion 7)
non-blocking: exact poll interval (30 s default vs configurable `watchdog_poll_ms`), Popen-in-herdr.py vs thread-in-runner.py split, and post-hoc `docs/failure-reaping.md` §8 update wording are at implementer's discretion if acceptance criteria hold
confidence: high — concurrent-hit, false-positive, and poll-failure criteria are deterministic via fake `CommandRunner`/Popen tests (criteria 2–7)
confidence: medium — wall-clock fast-fail before timeout (criterion 1) needs a real `herdr agent prompt --wait` Popen + `agent_read_visible` probe on host to prove `terminate→kill→pane_close` prevents `agent_name_live` skip; fake tests prove logic, not Herdr's actual settle semantics
confidence: low — Herdr CLI `--source visible` shape and any future quota-markers beyond `"Free usage exceeded"` require fixture updates; covered outside isolated unit tests (same caveat as phase 1)
