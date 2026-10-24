---
id: "072"
title: "Missed-run alert in the digest"
status: open
priority: medium
area: infra
---

## Description

The daily digest (`herdr-routines digest`, issue 010) currently shows a job's last terminal state or "never run". It has no concept of a job that *should have run by now but didn't* — an enabled job whose schedule says it ought to have fired and produced a terminal/history entry by the digest time, but the most recent relevant activity is missing or is a stale terminal state from long ago.

Issue 046 fixed the case where a *disabled* job's old failure was misread as current. The remaining gap is the opposite: an enabled job that silently stopped running (cron missed, service down, config issue, runner wedged) looks like "last ran successfully at X" forever, or just has no recent terminal run and we never call attention to it.

This idea was suggested and deferred in issue 046. The fix must read only existing history, reports and job config (no new per-job state store). It must be shown separately from failures so it doesn't train the reader to ignore the "failures" section the way disabled stale failures did.

## Design

### What counts as "missed"
- Consider only jobs with `enabled: true` (disabled jobs are already handled by issue 046's behavior; do not lump them into missed-run alerts).
- A job is "missed" if, relative to the digest's `now` (and using the job's schedule), it *should have run at least once by now* and there is no terminal run in the expected window / the last terminal run is too old relative to the schedule, or there is evidence it should have fired but produced no terminal record (and it's not merely "never run" in a benign newly-added sense).
- Need to be precise about "should have run": use the job's schedule/crontab/interval as configured (jobs.d config). The digest already has access to `RoutinesConfig` and history; no new background tracking.
- Distinguish from failures: render in its own section/group (e.g. "Missed runs" or "Should have run") separate from the failure list. Do not count a missed run as `failed` in the existing terminal-state line.

### Data sources (read-only)
- `jobs.d/*` job config (enabled flag, schedule) — already loaded by `build_digest` via `RoutinesConfig`.
- `history.jsonl` via `herdr_routines.history` (existing helpers) — to know last run time/state.
- Reports dir — only if needed to correlate; issue says "reads only the existing history, reports and job config".

### Rendering
- Add a separate section in the digest output, e.g.:
  - `Missed runs (should have run, didn't):`
  - `- <job>: last terminal <state> at <when> (expected by <schedule>; > <grace> overdue)` or similar. If never run but enabled and overdue per schedule, say "never run; expected by ..."
- Keep existing per-job lines intact. Missed-run alerts are *additional* callouts shown separately from failures.
- Respect `--include-disabled` semantics: disabled jobs never produce missed-run alerts.

### Implementation notes

### Schedule parsing and "should have run"
- **Source of truth**: read schedule from job config as parsed by the codebase (look up how `Job` stores schedule/cron/interval in `config.py`). Do not invent a new config schema.
- **Determination**: compute the most recent expected run time(s) up to `now` based on the schedule. A job is overdue if the last terminal run in history is before the most recent expected run time (or there is no terminal run at all and the first expected run time is <= `now - grace`).
- **Supported forms**: only rely on schedule forms the codebase already understands; if parsing is ambiguous, err on the side of *not* alerting (conservative) rather than false positives.
- **Grace window**: define a reasonable grace window (e.g. max(job interval, 10-15 minutes) or a fixed default like 30 minutes) to account for tick timing/slop. Make it explicit in code/comments; default conservatively.
- **Newly added jobs**: a newly added enabled job with no history is "never run" (benign) unless it has been enabled long enough that it's clearly overdue per its schedule + grace. Do not alert on first-ever expected run that hasn't happened yet if it just became enabled recently? Or simply require that we've had at least one full cycle pass or that `now - enabled_time` > grace + interval. But we don't have enabled_time in history - be conservative: if no terminal run exists, only alert if schedule is very frequent and clearly overdue by > grace, else keep "never run". Better to follow existing behavior: "never run" stays as-is unless there's strong evidence of being missed; prefer missing false positives over false negatives but avoid noise.

### API changes
- In `digest.py`, extend to compute missed runs. Options: add a `missed` list to digest result or have `build_digest` return both rows and missed rows. Keep backward compatibility - `render_digest` takes rows today; add optional missed parameter or return a new structure. Look at how it's used (CLI calls `digest_now`).
- `digest_now` should return/render missed section when present.
- All changes must be read-only w.r.t. history/reports; no writes.

- Extend `digest.py`: compute missed runs in `build_digest` or a helper, return them (e.g. add `missed: list[MissedRow]` or include in rows with a new kind). Keep `render_digest` pure and testable.
- Be conservative to avoid false positives (grace window). Use existing patterns from the codebase for schedule parsing if available; if schedule parsing is non-trivial, prefer a simple, documented heuristic that only fires when clearly overdue.
- No changes to job execution, no new history records written by the digest.

## Acceptance criteria

1. [blocking] For an enabled job whose schedule indicates it should have run by digest time but has no recent terminal run (or last terminal run is stale beyond a grace window), the digest shows it in a separate "Missed runs" section, not as a generic failure.
2. [blocking] Disabled jobs are never reported as missed runs.
3. [blocking] Missed-run alerts are shown separately from failures (different section/header) so they are visually distinct.
4. [blocking] The digest still reads only history, reports and job config (no new per-job state store). Existing digest behavior for enabled jobs with recent terminal states remains unchanged.
5. [non-blocking] The output is clear about why it's missed (e.g. "overdue by ...", schedule context) without being noisy.

## Notes

- Suggested and deferred in issue 046.
- Related: issue 009 (notification policy) — missed-run alerts should follow the same principle of "one actionable summary" rather than per-step noise.
- Related: issue 031 (pipeline stall watchdog) — different mechanism (active run watchdog) vs passive digest alerting.
