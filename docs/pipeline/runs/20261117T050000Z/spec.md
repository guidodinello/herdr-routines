# Spec v2 — 20261117T050000Z — Enforce the HistoryExtra schema when writing history records

Implements [issue 064](../../process/issues/064-enforce-history-extra-on-write.md) only.
Background: `docs/plan-v1.md` (project context, not this feature).

## Problem

PR #195 added `HistoryExtra(TypedDict, total=False)` (`src/herdr_routines/history.py:116`)
as the write-side schema for a history record's `extra`. Every writer already builds its
payload as `HistoryExtra(...)`, but the enforcement is opt-in: `HistoryRecord.extra` is
typed `Mapping[str, object] | None` (`history.py:222`) so read-back stays honest about old
files with keys no writer emits any more. Consequence: a future writer that passes a plain
`dict` is not flagged by mypy — the schema can silently drift.

060's dispatch-flow split is done, so the record-construction sites are now spread over
`tick.py`, `dispatch_common.py` and the `*_flow.py` modules; the issue's original blocker
("about 60 sites all in tick.py") is gone.

## Approach

Add a write-side constructor and route every production construction through it:

1. **`HistoryRecord.new(...)` classmethod** in `history.py`: same parameters as the
   dataclass, except `extra: HistoryExtra | None = None`. It forwards to the dataclass
   constructor (the `extra` field itself keeps its open `Mapping[str, object] | None`
   annotation). A plain `dict` passed to `new` is a mypy error — that is the enforcement
   point the issue asks for.
2. **Migrate all production `HistoryRecord(...)` constructions to `HistoryRecord.new(...)`.**
   Grep count today: 68 `HistoryRecord(` sites in `src`, of which one is `from_dict` —
   67 writer sites to migrate, across 10 modules (see Files touched). Mechanical change:
   `HistoryRecord(` → `HistoryRecord.new(`, plus typing the three sites that currently
   pass plain dicts (below).
3. **Type the stragglers.** Three construction sites pass plain dicts today, and their
   keys are missing from `HistoryExtra` — the migration must widen the schema to match
   what writers actually emit:
   - `pipeline_watchdog.py:735` — keys `resume_count`, `resumed_from`
   - `gated_event_flow.py:392` and `:411` — key `head_ref` (note: distinct from the
     existing `headRefName` key)
   Add `resume_count: int`, `resumed_from: str`, `head_ref: str` to `HistoryExtra`.
4. **Keep the reader path untouched.** `HistoryRecord.from_dict` (`history.py:237`)
   continues to construct `HistoryRecord` directly with the open on-disk shape. After
   migration it is the only direct construction left in `src`; mark it with a comment as
   the sole sanctioned reader construction so a future writer visibly bypasses the
   convention if it constructs directly.
5. **Guard against regression** (cheap, closes the bypass hole that `new` alone leaves):
   a test asserting no `HistoryRecord(` direct construction exists in `src` outside
   `history.py` (regex over the source tree).

Non-goals: no change to `to_json_line`, `append`, lock ordering, or any reader's
key-narrowing logic; tests are not migrated (they build fixture records directly and
acceptance only covers production sites).

## Files touched

- `src/herdr_routines/history.py` — add `HistoryRecord.new`; widen `HistoryExtra` with
  `resume_count`, `resumed_from`, `head_ref`; reader-construction comment on `from_dict`.
- Migration of `HistoryRecord(` → `HistoryRecord.new(` (67 sites):
  - `tick.py` (17) · `gated_event_flow.py` (11, incl. the two plain-dict sites) ·
    `gated_base_flow.py` (10) · `gated_pr_flow.py` (8) · `routine_flow.py` (8) ·
    `audit_flow.py` (4) · `routine_targets_flow.py` (4) · `pipeline_flow.py` (3) ·
    `dispatch_common.py` (1) · `pipeline_watchdog.py` (1, plain-dict site)
- `tests/test_history.py` — two new tests, names pinned so the acceptance criteria
  resolve to canonical names:
  - `test_no_direct_historyrecord_construction_outside_history_module` — the no-bypass
    guard (regex over `src` for `HistoryRecord(`).
  - `test_widened_extra_keys_round_trip` — a record carrying `resume_count`,
    `resumed_from`, and `head_ref` serializes and round-trips byte-identically.

## Risks

- **Byte-identical JSONL.** `new` must be a pure forwarder; `to_json_line` is untouched.
  Verified by the #195 round-trip test staying green plus a spot-check diffing a before/after
  run's history output. Risk: low.
- **TypedDict splat** `HistoryExtra(**occ.extra)` in `dispatch_common.py:306`: unchanged by
  this work, but if mypy flags it during migration the fix is to return `occ.extra`
  directly (it already is a `HistoryExtra`). Do not widen beyond that.
- **Schema widening is additive**: `HistoryExtra` is `total=False`, so adding three keys
  cannot break any existing construction site.
- **Merge surface**: touches 11 source modules but every edit is a one-token rename or an
  isolated extra-dict conversion; no logic moves. Per-run spec path (`docs/pipeline/runs/<id>/`)
  avoids the shared-root conflict that caused the #28/#29 clash (G-15).
- **Read-back honesty preserved**: `extra` field annotation stays `Mapping[str, object] | None`;
  no casts introduced on the reader path.

## Acceptance criteria

Classification: each item is **blocking** (run must not ship without it) or
**non-blocking** (valuable, ship with a follow-up note if it slips). Confidence:
how sure the spec is that the item is both correct and testable as written
(high / medium / low). Each item ends with the exact test that proves it.

1. **blocking / confidence: high** — every production construction goes through
   `HistoryRecord.new(...)`: a regex scan of `src/` finds no direct
   `HistoryRecord(` construction outside `history.py`, and inside `history.py`
   exactly one sanctioned site remains (`from_dict`, marked with the reader
   comment). Baseline verified today: 68 `HistoryRecord(` sites in `src`, of
   which 67 are writer sites across 10 modules. Test:
   `test_no_direct_historyrecord_construction_outside_history_module`
2. **blocking / confidence: high** — `new` is a pure forwarder with
   `extra: HistoryExtra | None = None`: records built through `new` serialize to
   the same JSONL bytes as the same payload built via the dataclass constructor,
   and `to_json_line` is untouched (no diffs in the serialization path). Test:
   `test_round_trip_is_byte_identical_and_keeps_key_order`
3. **blocking / confidence: high** — the schema is widened to what writers
   actually emit: `HistoryExtra` gains `resume_count: int`, `resumed_from: str`,
   and `head_ref: str`, and the three plain-dict sites
   (`pipeline_watchdog.py:735`, `gated_event_flow.py:392`, `:411`) are migrated
   to `new` with typed payloads. A record carrying all three new keys
   round-trips through append/read unchanged. Test:
   `test_widened_extra_keys_round_trip`
4. **blocking / confidence: high** — the reader path stays open: `from_dict`
   keeps constructing `HistoryRecord` directly and the `extra` field annotation
   stays `Mapping[str, object] | None`, so keys no current writer emits survive
   read-back untouched. Test: `test_read_back_keeps_unknown_extra_keys`
5. **non-blocking / confidence: medium** — meta: the whole suite plus strict
   mypy stays green (`scripts/ci-check.sh` with lint, `uv run mypy`, tests, and
   `uv run mypy --strict src`), proving the migration introduced no type-level
   bypass and no test regression. Test:
   `test_ci_green_with_blocking_tests_run_first`

## Changelog v1→v2

- Added the `## Acceptance criteria` section: 5 numbered items, each classified
  **blocking**/non-blocking with a `confidence:` tier and ending
  `Test: <name>` (exact test names).
- Replaced the v1 "## Acceptance → verification" table: its four rows are
  subsumed by criteria 1–5, and the "CI green" row is now criterion 5 anchored
  to a pinned test name instead of a shell command.
- Pinned the two new test names in "Files touched"
  (`test_no_direct_historyrecord_construction_outside_history_module`,
  `test_widened_extra_keys_round_trip`) so every `Test:` reference resolves to a
  single canonical name; the other three anchor to pre-existing tests in
  `tests/test_history.py` and the CI-meta suite.
- No changes to the Approach, Problem, or Risks: every v1 claim was re-verified
  against the tree before v2 — 68 `HistoryRecord(` sites in `src` (67 writer
  sites + `from_dict`), the per-module counts (tick 17, gated_event_flow 11,
  gated_base_flow 10, gated_pr_flow 8, routine_flow 8, audit_flow 4,
  routine_targets_flow 4, pipeline_flow 3, dispatch_common 1,
  pipeline_watchdog 1), the three plain-dict `extra={` sites
  (`pipeline_watchdog.py` within the `HistoryRecord` at :735,
  `gated_event_flow.py:392` and `:411` with the `head_ref` key at :415, and
  `pipeline_prepare.py:290` confirmed to be a state payload, not a history
  record), `HistoryExtra` at `history.py:116`, the `extra` annotation at :222,
  and `from_dict` at :237. All hold; no corrections needed.
