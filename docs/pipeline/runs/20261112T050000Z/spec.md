# Spec v2 — issue 084: close the `_WorkerFailed.reason` value set in tick.py

## Problem

`tick._WorkerFailed.reason` (tick.py:656) is a plain `str`. Its value lands in history
`extra.reason` (`outcome_reason = worker_outcome.reason` at tick.py:1806/2396 →
`extra["reason"]` at tick.py:3339-3340), where `auto_fix._UNCOUNTED_FAILURE_REASONS`
(auto_fix.py:49) compares it against string constants to decide whether a `failed` record
consumes a PR's retry budget (auto_fix.py:52-59). A typo on either side silently changes how
retry attempts are counted. Issue 084 asks to trace the producers, decide whether the value
set is closed, and if so make it a `Literal` typed end to end.

## Trace verdict: the set is closed

The two "pass-through" producers named in the issue (its ~:1645 and ~:2766, now shifted) do
**not** pass values in from elsewhere: each computes a local ternary between two literals —

```python
reason = ("clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed")
return _WorkerFailed(reason=reason, error=str(e))
```

(tick.py:1932-1935 for the event worker, tick.py:3067-3070 for the PR fix worker — mirrored
code). No herdr error code flows into `reason`; herdr error codes (`agent_not_ready`,
`agent_blocked`, `agent_prompt_stalled`) land in `error`, not `reason`. The set is therefore
closed, and no split of internal vs external reasons is needed:

| value | producers (one per mirrored function) |
|---|---|
| `clone_failed` | tick.py:1933, tick.py:3068 (ternary branch) |
| `repo_sync_failed` | tick.py:1933, tick.py:3068 (ternary branch) |
| `report_dir_creation_failed` | tick.py:1940, tick.py:3076 |
| `worktree_reuse_not_clean` | tick.py:1964, tick.py:3099 |
| `worktree_creation_failed` | tick.py:2030, tick.py:3142 |
| `agent_start_failed` | tick.py:2048, tick.py:3161 |
| `agent_not_interactive` | tick.py:2059, tick.py:3173 |
| `agent_prompt_failed` | tick.py:2078, tick.py:3193 |

Consumers that benefit: the `== "worktree_reuse_not_clean"` comparisons at tick.py:1721 and
tick.py:2320, and `auto_fix._UNCOUNTED_FAILURE_REASONS`.

## Approach

1. **Define the type** in `history.py`, next to `HistoryExtra` (which this change touches per
   the issue's pointer to 064):
   `WorkerFailedReason = Literal["clone_failed", "repo_sync_failed", "report_dir_creation_failed", "worktree_reuse_not_clean", "worktree_creation_failed", "agent_start_failed", "agent_not_interactive", "agent_prompt_failed"]`
   with a docstring/comment naming the two consumers. Rationale for the home: tick.py imports
   from auto_fix.py (tick.py:24), so auto_fix cannot import from tick (cycle); history.py is
   already imported by both, is the write-side schema module for `extra` keys, and 064's
   future work anchors here. `HistoryExtra.reason: str` itself stays `str` — the key also
   carries reasons from other producers (skip reasons like `checkout_busy`, selector reasons,
   `verify.REASON_LEFT_UNCOMMITTED`), and 064 is blocked, separate work.
2. **Type the field**: `_WorkerFailed.reason: WorkerFailedReason` (tick.py:656).
3. **Fix the two ternary sites**: inline the ternary into the constructor call
   (`_WorkerFailed(reason="clone_failed" if ... else "repo_sync_failed", error=str(e))`) so
   mypy checks each branch against the Literal; a `reason: str` local would fail `--strict`.
4. **Type the consumer set**: `_UNCOUNTED_FAILURE_REASONS: frozenset[WorkerFailedReason]` in
   auto_fix.py. Runtime behavior is unchanged (same frozenset of strings; `_is_countable_attempt`
   still reads `extra.get("reason")` as `object` from disk, so legacy records with old strings
   keep behaving identically).
5. **Test**: one static closure test (below). The existing issue-070 tests already cover the
   uncounted behavior.

Explicitly out of scope (different reason families, not this issue): `_AgentRun.reason`
(tick.py:644), `_DIRTY_WORKTREE_REASONS` (tick.py:3238 — mixes a gate reason in, stays
`frozenset[str]`), `_REVIEW_COUNTABLE_FAILURES` (auto_fix.py:~1031, review dispatch),
`NOT_A_RUN_REASONS` (history.py:58), and config.py's `RunOutcome` retry set.

## Files touched

- `src/herdr_routines/history.py` — add `WorkerFailedReason` Literal (+ import of `Literal`).
- `src/herdr_routines/tick.py` — `_WorkerFailed.reason` annotation; inline two ternaries
  (tick.py:1932-1935, tick.py:3067-3070); import the type.
- `src/herdr_routines/auto_fix.py` — annotate `_UNCOUNTED_FAILURE_REASONS`; import the type.
- `tests/test_auto_fix.py` — new test `test_worker_failed_reason_set_is_closed`.

## Acceptance criteria

Blocking:

1. AC1 — `_WorkerFailed.reason` is annotated `WorkerFailedReason` covering exactly the eight
   traced values, and the two former pass-through sites are Literal-clean (inlined ternaries
   type-check under `--strict`). `uv run mypy --strict src` green. (confidence: high)
   Test: `test_worker_failed_reason_set_is_closed`
2. AC2 — `_UNCOUNTED_FAILURE_REASONS` is typed `frozenset[WorkerFailedReason]`, so a typo in
   the set or in a producer string is now a mypy error; the test asserts the set is a subset
   of the Literal's args. (confidence: high)
   Test: `test_worker_failed_reason_set_is_closed`
3. AC3 — No behavior change: same reason strings, same frozenset contents, JSONL output
   byte-identical for the same run; legacy on-disk records with old reason strings keep
   counting semantics unchanged. `scripts/ci-check.sh` green. (confidence: high)
   Test: `test_attempt_counters_skip_legacy_worktree_reuse_refusals`

Non-blocking:

4. NB1 — A docstring comment on `WorkerFailedReason` records the closed-set verdict and names
   the two consumers (tick producer site, `auto_fix._UNCOUNTED_FAILURE_REASONS`) so a future
   producer adding a reason must widen the Literal deliberately; the closure test asserts the
   comment exists and names both consumers. (confidence: high)
   Test: `test_worker_failed_reason_set_is_closed`

## Risks

- **Import cycle** if the Literal is placed in tick.py (auto_fix is imported by tick, plus
  verify/review_scan/gates/cli/runner/target_selectors/pipeline_run/self_update import
  auto_fix). Avoided by the history.py home; do not move it to tick.py or auto_fix.py.
- **Legacy on-disk records**: old files may carry reason strings outside the Literal. Readers
  compare `object` against the frozenset, so membership semantics are unchanged — no migration,
  no write-side change. Risk is only that someone later narrows the *read* side; the NB1
  comment should not invite that.
- **Ternary inlining** must preserve the exact condition and strings (`(job.repo / ".git").exists()`
  decides `clone_failed` vs `repo_sync_failed`); both sites are mirrored, so a partial edit
  leaves one site failing mypy — which `--strict` catches.
- **Typo-style drift** in the test (copying a misspelled literal into both sides) would make
  AC1 vacuous; the test's expected set is spelled out independently of the Literal definition
  for exactly this reason.
- Scope creep into 064 (`HistoryExtra` enforcement) or the other reason families listed above —
  deliberately not attempted here; 064 is blocked on the tick split and is separate work.

## Changelog v1→v2

- 2026-11-12: v2 — Restructured acceptance criteria: items are numbered, split into explicit
  `blocking` / `non-blocking` tiers, and every item now ends with an exact `Test:` name plus a
  `confidence:` tier. AC3 gained an executable pin: the existing issue-070 regression test
  `test_attempt_counters_skip_legacy_worktree_reuse_refusals` covers legacy-record counting
  semantics, while the byte-identical-output and CI-green gates stay in AC3's body as
  verification steps (`scripts/ci-check.sh`, `uv run mypy --strict src`). NB1 is now
  machine-checked: the closure test additionally asserts the `WorkerFailedReason` docstring
  names both consumers. Approach, trace, scope, and files touched are unchanged from v1.

## Changelog v1

- 2026-11-12: v1 — initial spec. Trace performed: two pass-through producers resolved to local
  ternaries between literals (`clone_failed`/`repo_sync_failed`), set is closed at eight values,
  no internal/external split needed. Type to live in history.py (cycle-free schema home);
  `HistoryExtra.reason` untouched (064 separate).
