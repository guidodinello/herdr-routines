---
id: "084"
title: "Close the _WorkerFailed.reason value set in tick.py"
status: open
priority: low
area: infra
---

## Description

`tick._WorkerFailed.reason` (~:489) is a plain `str`. Six producers pass literal
reasons. Two (~:1645 and ~:2766) pass a `reason` variable through from elsewhere. The
value lands in history `extra.reason`. `auto_fix._UNCOUNTED_FAILURE_REASONS` then
compares it against string constants. A typo on either side silently changes how
retry attempts are counted.

The first step is to trace the two pass-through producers and decide whether the set
is closed. If it is, make it a Literal and type `_UNCOUNTED_FAILURE_REASONS` against
it. If it is open (for example, it includes herdr error codes), split the closed
internal reasons from the open external ones. Touches
[064](064-enforce-history-extra-on-write.md)'s `HistoryExtra.reason`.

## Acceptance

- Either `_WorkerFailed.reason` is a Literal and the reason sets in auto_fix are typed
  against it, or the issue log records why the set is open.
- No behavior change. `scripts/ci-check.sh` and `uv run mypy --strict src` are green.

## Log

- **2026-10-26**: filed from the type-health audit's INVESTIGATE list.
