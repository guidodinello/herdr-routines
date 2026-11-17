# Spec v1 — 20261117T050000Z — Enforce the HistoryExtra schema when writing history records

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
- `tests/test_history.py` (or a new small test) — the no-bypass guard.

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

## Acceptance → verification

| Criterion | Check |
| --- | --- |
| Every production construction goes through `new` (`HistoryExtra \| None`) | Migration complete; guard test green |
| `from_dict` / `extra` field keep the open on-disk shape | Annotations unchanged in `history.py` |
| JSONL byte-identical for the same run | Round-trip test from #195 green; no `to_json_line` change |
| CI green | `scripts/ci-check.sh` (lint + `uv run mypy` + tests) and `uv run mypy --strict src` |
