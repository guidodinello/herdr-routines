# Spec v2 — 20261021T050000Z

**Feature:** Issue 066 — inspect-only CLI for review candidates (`herdr-routines review-scan`)
**Source:** `docs/process/issues/066-inspect-only-review-candidate-cli.md`

## Problem

When a `kind: review` job skips a PR, the only way to learn why is to open the tick's
`reports/review-<run_id>.md` (or the history record) and read the row for that target —
there is no way to ask the question directly, and no way to inspect a target without
waiting for the next tick. The selector is already separable: `scan_review_targets`,
`apply_attempt_budget`, `apply_marker_guard`, and `cap_candidates` (all in
`review_scan.py`) are pure functions over an injected `GhClient`, a `Job`, and a
history path. The only non-reusable part is `tick.py:_review_cycle`, which builds
disposition rows inline (`tick.py:4215-4239`) and then immediately dispatches.

## Approach

Two changes, in dependency order:

### 1. Extract classification into `review_scan.py` (shared helper)

Add a `Classification` dataclass and `classify_review_candidates`:

```python
@dataclass(frozen=True, slots=True)
class Classification:
    rows: tuple[tuple[Candidate, str], ...]   # skipped/over-cap, tick order
    dispatchable: tuple[Candidate, ...]       # passed every guard and the cap
    truncated_modes: tuple[str, ...]
    enumerated: int                           # ScanOutcome.enumerated
    candidate_count: int                      # len(ScanOutcome.candidates), pre-guard

def classify_review_candidates(
    gh: GhClient, job: Job, *, login: str, history_path: Path,
    trusted_authors: frozenset[str], limit: int = 100,
) -> Classification | None
```

Pipeline in this exact load-bearing order (documented in `_review_cycle`):
`scan_review_targets` (`None` → `None`, fail closed) → `apply_attempt_budget`
(local history read, cheapest) → `apply_marker_guard` (one `gh pr view` per
survivor) → `cap_candidates` to `job.max_workers_per_tick`.

`rows` must reproduce today's row order exactly: scan skips, then budget skips
(`max_attempts_exceeded`), then marker skips (`review_already_posted`), then
`over_cap`, each row using the candidate's `skip_reason` with the same fallback
strings `_review_cycle` uses today (`tick.py:4215-4239`). `candidate_count` is the
post-scan pre-budget count already written as `candidates=` in the report and
history (`tick.py:4310`, `tick.py:4337`) — kept byte-identical.

Then refactor `_review_cycle` to call this function instead of building rows inline.
This is the *only* change to `tick.py`: dispositions, ordering, history writes, the
`repo_info`/`clone`/dispatch phases (which append `repo_info_failed`, `clone_failed`,
and per-target dispositions to `rows` *after* the helper returns), and the aggregate
report must be byte-identical.

### 2. Add the `review-scan` subcommand in `cli.py`

Wired like `refine-issue` / `ps` / `scheduled`: `sub.add_parser("review-scan", ...)`,
`set_defaults(handler=_cmd_review_scan)`, read-only handler
(`cli.py` global `--config` at line 141; `_load_config_or_exit` at `cli.py:623`;
`_configured_trusted_review_authors` at `cli.py:1419`). No per-command `--config`.

Arguments: positional `job` (required, like `run`/`history`); `--limit N`
(default 100, validated 1–100 = GitHub's search ceiling, forwarded to the
classification); `--json` (rows as JSON for scripting).

Behaviour:
- Resolve the job; missing job → `EXIT_FAILURE` (1) on stderr; `kind` is not
  `review` → `EXIT_USAGE` (2). Build a `RealGhClient`, call `gh.api_user()`;
  auth failure exits `EXIT_FAILURE` (1) with `gh_auth_missing` on stderr
  (never a clean empty list).
- `login = gh.api_user()`; scan `None` → `EXIT_FAILURE` (1), `search_failed` on stderr.
- Trusted authors: `effective_trusted_authors(_configured_trusted_review_authors(args),
  viewer_login=login)` → passed as `trusted_authors` frozenset. There is no per-job
  trusted-author field; the list lives in `RoutinesConfig.trusted_review_authors`
  (`config.py`).
- Human output: short header (job name, select modes, gh login, `enumerated` count,
  and `truncated_modes` when non-empty), then one line per PR **sorted by
  `target_key`** with `target_key`, `select_mode`, and the disposition —
  `would_dispatch` for `Classification.dispatchable`, else the exact `skip_reason`
  (`author_mismatch`, `routine_owned_branch`, `fork_pr`, `max_attempts_exceeded`,
  `review_already_posted`, `over_cap`). No generic `"skipped"`.
- A fixed note stating this reports scan eligibility only: it does not evaluate the
  cron schedule or the live-agent guard, so "would dispatch" ≠ "next tick will run it".
- Exit `EXIT_OK` (0) on a successful scan regardless of outcome count.
- `--json`: stdout carries *only* a JSON array of objects with
  `target_key`, `repo`, `number`, `title`, `url`, `select_mode`, `disposition`
  (`would_dispatch` or the skip reason); header + note go to stderr; all errors to
  stderr. Exit codes identical to the human path.

Non-goals (from the issue): no schedule/live-agent evaluation, no writes of any kind
(no history append, `reports/` file, notifications, worktree, or agent), no change to
what `tick` dispatches.

## Files touched

- `src/herdr_routines/review_scan.py` — add `Classification` +
  `classify_review_candidates` (only additive code; existing pure functions untouched).
- `src/herdr_routines/tick.py` — only `_review_cycle` refactored to call the helper;
  behaviour byte-identical.
- `src/herdr_routines/cli.py` — add `review-scan` parser + `_cmd_review_scan`.
- `tests/test_review_scan.py` — selector tests + the drift test (direct
  `classify_review_candidates` call vs `_review_cycle` run with the same fake
  `GhClient` and `default_reports_dir` → `tmp_path`; asserts the
  `review-<run_id>.md` target lines equal the classification rows, same order).
- `tests/test_cli.py` — new CLI tests: wiring, exit codes (missing job 1, wrong kind 2,
  auth/search failure 1, `--limit` outside 1–100 usage error), JSON shape (array,
  pinned fields, no non-JSON text on stdout), read-only (history bytes + mtime
  unchanged, no report file, no worktree/agent/notification call).
- `tests/test_tick_dispatch.py` — existing tests stay green; report/history numbers
  (`enumerated`, `candidates`, dispositions) unchanged.

## Acceptance criteria

Each item states its tier (`blocking` or `non-blocking`) and a `confidence:` tier
(`high` / `medium` / `low`), and ends with its exact verification test name (12
items, 12 `Test:` lines — items 1, 2, 3, 7, 11 and 12 fail against today's tree
and pass only after the changes land; items 4–6 and 8–10 are new CLI/contract
tests). All are additions to `tests/test_review_scan.py` and `tests/test_cli.py`
unless noted; exact names are the ones stage 3 must implement.

1. [blocking, confidence: high] `classify_review_candidates` is added to
   `review_scan.py` with the documented signature (`Classification` frozen dataclass:
   `rows`, `dispatchable`, `truncated_modes`, `enumerated`, `candidate_count`) and
   runs the pipeline in the load-bearing order scan → attempt budget → marker guard →
   cap against `job.max_workers_per_tick`; a `None` scan result propagates as `None`
   (fail closed). Test: test_classify_review_candidates_pipeline_order_and_none_fail_closed
2. [blocking, confidence: high] `Classification.rows` reproduces `_review_cycle`'s
   pre-dispatch row contract exactly: scan skips, then `max_attempts_exceeded` skips,
   then `review_already_posted` skips, then `over_cap`, each candidate's `skip_reason`
   with the same fallback strings as today (`skipped`, `max_attempts_exceeded`,
   `review_already_posted`, `over_cap`); `enumerated` and `candidate_count` equal
   `ScanOutcome`'s post-scan pre-budget numbers. Test: test_classify_review_candidates_rows_reproduce_tick_order_and_fallbacks
3. [blocking, confidence: high] Drift test: calling `classify_review_candidates`
   directly with the same fake `GhClient` fixtures a `_review_cycle` run
   (`default_reports_dir` → `tmp_path`) is exercised with, the
   `review-<run_id>.md` target lines equal the classification rows in the same
   order — proving the tick goes through the shared helper. Test: test_review_scan_drift_tick_report_matches_classification
4. [blocking, confidence: high] CLI wiring: `sub.add_parser("review-scan")` with
   `set_defaults(handler=_cmd_review_scan)`, required positional `job`, `--limit`
   (default 100), `--json` flag; wired like `refine-issue`/`ps`/`scheduled` (global
   `--config`, `_load_config_or_exit`, `_configured_trusted_review_authors`) with no
   per-command `--config`. Test: test_review_scan_cli_wiring
5. [blocking, confidence: high] Exit codes for job resolution: missing job →
   `EXIT_FAILURE` (1) with the problem named on stderr; resolved job whose `kind` is
   not `review` → `EXIT_USAGE` (2), also named on stderr. Test: test_review_scan_missing_job_and_non_review_kind_exit_codes
6. [blocking, confidence: high] No silent empty output: `gh.api_user()` failure exits
   1 with `gh_auth_missing` on stderr and `scan_review_targets` returning `None` exits
   1 with `search_failed` on stderr — never a clean empty list. Test: test_review_scan_auth_and_search_failures_exits_1_stderr
7. [blocking, confidence: medium] Human output: short header (job name, select
   modes, gh login, `enumerated`, `truncated_modes` when non-empty), a fixed
   eligibility-only note (no schedule/live-agent evaluation, so "would dispatch" ≠
   "next tick will run it"), then one line per PR sorted by `target_key` carrying
   `target_key`, `select_mode`, and exactly one disposition from the closed set —
   `would_dispatch` for `Classification.dispatchable`, else the exact skip reason
   (`author_mismatch`, `routine_owned_branch`, `fork_pr`, `max_attempts_exceeded`,
   `review_already_posted`, `over_cap`) with no generic `"skipped"`; exit 0 on a
   successful scan regardless of outcome count. Test: test_review_scan_human_output_sorted_dispositions_and_note
8. [blocking, confidence: high] `--json`: stdout carries only a JSON array of objects
   with exactly `target_key`, `repo`, `number` (int), `title`, `url`, `select_mode`,
   `disposition`; header and note go to stderr, all errors to stderr, exit codes
   identical to the human path. Test: test_review_scan_json_stdout_only_array_with_pinned_fields
9. [blocking, confidence: high] Read-only by observation, not absence of writes in a
   happy path: after a full `review-scan` run, `history.jsonl` bytes and mtime are
   unchanged, no `reports/review-<run_id>.md` appears, and no worktree, agent, or
   notification call is made. Test: test_review_scan_is_read_only_touches_nothing
10. [blocking, confidence: medium] `--limit` outside 1–100 is rejected as a usage
    error (2); an in-range `--limit` is forwarded verbatim to the search (observed
    via the fake client's recorded call). Test: test_review_scan_limit_validation_and_forwarding
11. [non-blocking, confidence: medium] Truncated search is surfaced: when
    `truncated_modes` is non-empty the human path shows the truncated-search note
    (header field / note) and the JSON path mirrors it without leaking non-JSON text
    onto stdout. Test: test_review_scan_truncated_modes_note
12. [blocking, confidence: high] The `_review_cycle` refactor is byte-identical in
    behaviour: the existing review suites stay green unmodified and the report and
    history aggregate numbers (`enumerated`, `candidates`, `skipped_count`,
    `failed_count`, `repos_failed`) are unchanged. Test: test_review_dispatch_creates_per_repo_checkout

Non-test acceptance obligations: `scripts/ci-check.sh` green including
`uv run mypy --strict src` (clause 9 of v1) stays in Verification below — it is a
gate, not a single named test, and is asserted by the CI stage rather than by one
of the tests named above.

## Changelog v1→v2

- Title `Spec v1` → `Spec v2`.
- Replaced the informal `## Acceptance (from the issue, all must hold)` list with
  `## Acceptance criteria` (12 numbered items): each item carries a
  `blocking`/`non-blocking` tier plus a `confidence:` tier (`high`/`medium`/`low`)
  and ends with its exact verification test name (12 items, 12 `Test:`-marked
  lines, machine-extractable via `rg -c "Test:"` like prior runs).
- Mapping from v1's 10 clauses: clause 1 → item 9 (read-only, by file-bytes/mtime
  assertion); clause 2 → items 2 and 7 (row contract + CLI disposition set); clause
  3 → item 3 (drift test); clause 4 → item 6; clause 5 → item 5; clause 6 → item 8;
  clause 7 → item 10; clause 8 → item 11; clause 9 stays in Verification (CI gate,
  no single test); clause 10 → item 12. Items 1 and 4 pin the helper contract and
  CLI wiring that v1 only described in the Approach.
- Review of v1 (stage 2): all line references verified against the tree
  (`tick.py:_review_cycle` row order and fallbacks `cand.skip_reason or ...` at the
  four stage nodes, `skipped_count`/report/history aggregate writes, `cli.py`
  `EXIT_OK=0`/`EXIT_USAGE=2`/`EXIT_FAILURE=1`, `_configured_trusted_review_authors`
  at `cli.py:1419`, `effective_trusted_authors` applied before the marker guard in
  `_review_cycle`); **non-blocking** (confidence: low) the v1 wording "each row
  using the candidate's `skip_reason`" hides that scan-stage skips may have
  `skip_reason=None` and fall back to the literal `"skipped"` — item 2 pins the
  fallback strings explicitly to remove that ambiguity. **non-blocking**
  (confidence: medium) the v1 acceptance list mixed a CI gate (clause 9) with test
  clauses; the gate is now called out under the criteria table instead of
  masquerading as a test.
- Test names follow the `test_review_scan_*` / existing `tests/test_review_scan.py`
  helper conventions (`ReviewGh`, `_run_cycle`, `_pr`), so stage 3 can implement
  them without further spec churn.
- No change to Problem, Approach, Files touched, Risks, or Verification scope.

## Risks

- **Tick drift / behaviour change (highest).** Moving the row-building out of
  `_review_cycle` could subtly change disposition order or fallback strings. Mitigation:
  the drift test (clause 3 + 10) compares the report lines against the classification
  rows for the same fixtures; fallback strings copied exactly from
  `tick.py:4215-4239`.
- **`Rows` contract subtlety.** `_review_cycle` continues to append
  `repo_info_failed` / `clone_failed` / post-dispatch dispositions after the helper
  returns; the helper must own only the pre-dispatch four-stage rows or counts
  (`skipped_count` in `_review_cycle` at `tick.py:4297`) shift.
- **Trusted-author wiring.** `trusted_authors` into the helper must be the
  post-`effective_trusted_authors` set (viewer login included), matching what
  `_review_cycle` passes to `apply_marker_guard`; wrong wiring would make marker
  decisions differ between tick and CLI.
- **Unicode/JSON pitfalls in `--json`.** `ensure_ascii` handling and field types
  (`number` as int) must be pinned by a shape test to avoid silent shape drift.

## Verification

`scripts/ci-check.sh` (includes `uv run mypy --strict src`), existing review tests
green, all acceptance clauses covered by new tests. Read-only claim verified by
asserting file bytes/mtime, not just by not writing anything in a happy path.
