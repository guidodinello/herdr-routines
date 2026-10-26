---
id: "082"
title: "Carry the review select mode as a Literal from config to history"
status: open
priority: low
area: infra
---

## Description

Config validates the review select mode against `VALID_REVIEW_SELECT`
(`{"author", "review_requested"}`, config.py ~:133), then stores it as a plain `str`.
Every later consumer receives an unchecked `str`:

- `auto_fix.ReviewPR.mode` (~:158)
- `auto_fix.search_prs(modes: Sequence[str])`
- `review_scan.Candidate.select_mode` (~:70)
- `HistoryExtra.select_mode` (history.py ~:166)

Typos in a comparison such as `pr.mode == "author"` (review_scan ~:112) go
unchecked. A `ReviewSelectMode` Literal derived with `get_args` would give one source
of truth for both validation and typing.

The history part overlaps
[064](064-enforce-history-extra-on-write.md). Records read back from disk stay
open-ended.

## Acceptance

- One `ReviewSelectMode` Literal defined in config. `VALID_REVIEW_SELECT` is derived
  from it.
- The config → auto_fix → review_scan → history-writer chain carries the Literal.
  No `cast`.
- No behavior change. `scripts/ci-check.sh` and `uv run mypy --strict src` are green.

## Log

- **2026-10-26**: filed from the type-health audit's INVESTIGATE list.
