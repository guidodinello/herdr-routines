# Spec v1 — 20261021T050000Z

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

## Acceptance (from the issue, all must hold)

1. Read-only: `history.jsonl` bytes and mtime unchanged; no report file; no
   worktree/agent/notification call.
2. Every PR from the search appears exactly once, sorted by `target_key`, with
   exactly one disposition from the closed set above.
3. Drift test proves the tick goes through the shared helper.
4. `gh.api_user()` failure and `search_prs` failure both exit 1, reason on stderr,
   never a clean empty list.
5. Missing job → 1; non-review job → 2; both name the problem on stderr.
6. `--json` parses as an array with the pinned fields; no non-JSON stdout leakage.
7. `--limit` outside 1–100 rejected as usage error; forwarded to the search.
8. Truncated-search note appears when `truncated_modes` non-empty.
9. `scripts/ci-check.sh` green, including `uv run mypy --strict src`.
10. Existing review tests stay green with the refactor; numbers unchanged.

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
