---
id: "066"
title: "Inspect-only CLI for review candidates (`herdr-routines review-scan`)"
status: done
priority: medium
area: infra
---

## Description

When a `kind: review` job does not review a PR, the only way to find out why is to
open the tick's `reports/review-<run_id>.md` (or its history record) and read the row
for that target. There is no way to ask the question directly, and no way to inspect a
target without waiting for the next tick. The ROADMAP Parking Lot entry asks for a
read-only subcommand that runs the review candidate scan and prints every PR it found
with the reason it would or would not be dispatched, answering "why didn't it review PR
X?". Issue 055 deliberately deferred it (non-goal, AC section) after building the scan
itself in `review_scan.py`.

The selector is already separable from dispatch: `scan_review_targets`,
`apply_attempt_budget`, `apply_marker_guard`, and `cap_candidates` are pure functions
over an injected `GhClient`, a `Job`, and a history path. The only non-reusable part is
`tick.py:_review_cycle`, which builds the disposition rows inline (around
`tick.py:4215-4239`) and then immediately starts spawning agents.

## Design

### 1. Extract the classification rows into `review_scan.py`

Add one function so the tick and the CLI cannot drift:

```python
@dataclass(frozen=True, slots=True)
class Classification:
    rows: tuple[tuple[Candidate, str], ...]   # skipped/over-cap, in tick order
    dispatchable: tuple[Candidate, ...]       # passed every guard and the cap
    truncated_modes: tuple[str, ...]
    enumerated: int                           # ScanOutcome.enumerated
    candidate_count: int                      # len(ScanOutcome.candidates), pre-guard

def classify_review_candidates(
    gh: GhClient,
    job: Job,
    *,
    login: str,
    history_path: Path,
    trusted_authors: frozenset[str],
    limit: int = 100,
) -> Classification | None:
```

`candidate_count` is the post-scan, pre-budget count `_review_cycle` already writes as
`candidates=` in the report and history (today `len(outcome.candidates)`), so the
refactor keeps those numbers byte-identical. `limit` is forwarded to
`scan_review_targets`; the CLI pins it to 1–100 (GitHub's search ceiling).

It performs, in this exact order (the order is load-bearing and documented in
`_review_cycle`):

1. `scan_review_targets` — `None` propagates as `None` (fail closed).
2. `apply_attempt_budget` (local history read, cheapest).
3. `apply_marker_guard` (one `gh pr view` per surviving candidate).
4. `cap_candidates` to `job.max_workers_per_tick`.

`Classification.rows` reproduces the current row order exactly: scan skips, then
budget skips, then marker skips, then `over_cap`. Refactor
`_review_cycle` to call this function and keep its behaviour byte-identical; this is
the only change to `tick.py` and it must not alter dispositions, ordering, history
writes, or the aggregate report.

### 2. Add the `review-scan` subcommand

Wire it in `cli.py` the same way as `refine-issue` / `ps` / `scheduled`: a
`sub.add_parser("review-scan", ...)`, arguments, `set_defaults(handler=_cmd_review_scan)`,
and a read-only handler (no history append, no report file, no notifications, no
worktree, no agent). Load config through the existing `_load_config_or_exit(args)`; do
**not** add a per-command `--config` — `--config` is already a global parser flag
(`cli.py:141`) and every command reads it via `args.config`.

Arguments:

- positional `job` (required), matching `run` / `history`: the `kind: review` job to
  inspect. A missing job exits `EXIT_FAILURE` (1) like `_cmd_run`; a job whose `kind`
  is not `review` is a usage error, `EXIT_USAGE` (2).
- `--limit N` (default 100, validated 1–100) forwarded to the classification.
- `--json`: emit the rows as JSON for scripting instead of the human table.

Behaviour:

- Resolve the job, build a `RealGhClient`, call `gh.api_user()`. Auth failure exits
  `EXIT_FAILURE` (1) with `gh_auth_missing` on stderr rather than pretending there is
  nothing to review.
- Call `classify_review_candidates` with the config-level trusted authors:
  `effective_trusted_authors(_configured_trusted_review_authors(args), viewer_login=login)`.
  There is no per-job trusted-author field; the list is `RoutinesConfig.trusted_review_authors`
  (`config.py:557`), and `_configured_trusted_review_authors` (`cli.py:1419`) already
  wraps the read. `None` from the scan exits `EXIT_FAILURE` (1) with `search_failed`.
- Print a short header (job name, select modes, gh login, enumerated count, and any
  `truncated_modes`) and one line per PR, sorted by `target_key`, giving
  `target_key`, `select_mode`, and the disposition. `Classification.dispatchable`
  entries print as `would_dispatch`; everything else prints the exact `skip_reason`
  the tick would record (`author_mismatch`, `routine_owned_branch`, `fork_pr`,
  `max_attempts_exceeded`, `review_already_posted`, `over_cap`). No generic
  `"skipped"` disposition: `classify_review_candidates` assigns every row an explicit
  reason.
- Print one note that the command reports scan eligibility only: it does not evaluate
  the job's cron schedule or the live-agent guard (both need tick/Herdr state), so
  "would dispatch" means "the scan would offer this target", not "the next tick will
  run it".
- Exit `EXIT_OK` (0) on a successful scan, regardless of whether any target would
  dispatch.
- `--json` emits a list of objects with `target_key`, `repo`, `number`, `title`,
  `url`, `select_mode`, and `disposition` (`would_dispatch` or the skip reason).
  Under `--json`, stdout is only the JSON document: the header and the
  schedule/live-agent note go to stderr, and every error goes to stderr.

### 3. Non-goals

- No schedule evaluation, no live-agent check, no attempt to predict `repo_info` /
  `clone` / worktree failures (those happen after the scan).
- No writes of any kind: no history record, no `reports/` file, no notification.
- No change to what `tick` dispatches.

## Acceptance

- `herdr-routines review-scan <review-job>` runs read-only: a test asserts the
  `history.jsonl` bytes and mtime are unchanged, no report file is created, and no
  worktree/agent/notification call is made.
- Every PR returned by the search appears exactly once, sorted by `target_key`, with
  exactly one disposition from
  `{would_dispatch, author_mismatch, routine_owned_branch, fork_pr,
  max_attempts_exceeded, review_already_posted, over_cap}`.
- A drift test in `tests/test_review_scan.py` calls `classify_review_candidates`
  directly for a fake `GhClient` + history, and separately runs `_review_cycle` with
  that same fake and `default_reports_dir` pointed at `tmp_path`, then asserts the
  `review-<run_id>.md` lines for the scanned targets equal the classification rows in
  the same order. This proves the tick path actually goes through the shared helper.
- A `gh.api_user()` failure and a `search_prs` failure both exit `EXIT_FAILURE` (1)
  with the reason on stderr, and neither prints a clean empty list.
- A missing job exits `EXIT_FAILURE` (1); a non-`review` job exits `EXIT_USAGE` (2);
  both name the problem on stderr.
- `--json` output parses as a JSON array and carries the fields listed above; a test
  pins the shape, and asserts non-JSON text does not leak onto stdout.
- `--limit` outside 1–100 is rejected as a usage error; `--limit` is forwarded to the
  search.
- The truncated-search note appears when `truncated_modes` is non-empty.
- `scripts/ci-check.sh` is green, including `uv run mypy --strict src`.
- The existing review tests (`tests/test_review_scan.py`,
  `tests/test_tick_dispatch.py`) stay green with the `_review_cycle` refactor, and the
  report/history numbers (`enumerated`, `candidates`, dispositions) are unchanged.
- New CLI tests live in `tests/test_cli.py` (command wiring, exit codes, JSON,
  read-only); selector tests live in `tests/test_review_scan.py`.

## Log

- **2026-10-21**: filed from the ROADMAP Parking Lot; deferred by issue 055 now that
  the scan is a separable module.
