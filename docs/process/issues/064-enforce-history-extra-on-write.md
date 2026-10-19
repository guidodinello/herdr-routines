---
id: "064"
title: "Enforce the HistoryExtra schema when writing history records"
status: blocked
priority: low
area: infra
gate: 060 (tick.py split) is merged
---

## Description

PR #195 added `HistoryExtra(TypedDict, total=False)` in `history.py`. It lists every key a
writer puts in a history record's `extra`. All current writers in `tick.py` build their
payloads as `HistoryExtra(...)`, so mypy checks the keys and value types at each call
site. But the schema is opt-in: `HistoryRecord.extra` is typed
`Mapping[str, object] | None`, so that read-back stays honest about old files with keys
no writer emits any more. As a result, a future writer that passes a plain dict is not
flagged.

The way to enforce it is a write-side constructor, for example
`HistoryRecord.new(..., extra: HistoryExtra | None)`, used by every writer. Records read
from disk keep going through `from_dict`. That touches about 60 record-construction
sites in `tick.py`, so it waits for 060 (the dispatch-flow split). Doing it first would
conflict with that split twice.

## Acceptance

- Every production `HistoryRecord(...)` construction goes through a constructor whose
  `extra` parameter is `HistoryExtra | None`. A plain `dict` there is a mypy error.
- `HistoryRecord.from_dict` and the `extra` field keep accepting the open-ended
  on-disk shape (`Mapping[str, object] | None`).
- JSONL output is byte-identical for the same run (the round-trip test from #195 stays
  green).
- `scripts/ci-check.sh` and `uv run mypy --strict src` are green.

## Log

- **2026-10-19**: filed from the type-gaps run (`extra` task, PR #195). The opt-in design
  was chosen there because enforcing it would touch about 60 tick.py sites while a parallel
  run was also editing tick.py.
