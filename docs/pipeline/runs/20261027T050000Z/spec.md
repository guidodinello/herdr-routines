# Spec v2 — 20261027T050000Z

Issue: `docs/process/issues/072-missed-run-alert-in-the-digest.md`
(medium, infra). Claimed for this run.

## Problem

The daily digest (issue 010) renders each enabled job's last terminal state, or
"never run" — forever. If an enabled job silently stops running (cron missed,
service down, config issue, runner wedged), the digest keeps saying
"last: done at <old date>" with no signal that the schedule says it should have
fired since. Issue 046 fixed the mirror case (disabled jobs' stale failures read
as current); this is the remaining half: enabled jobs that stopped producing
terminal runs. The fix must read only existing history/reports/job config, and
must render in its own section so it does not train the reader to ignore the
failure lines the way disabled stale failures did.

## Approach

All detection lives in `src/herdr_routines/digest.py`; no new state, no writes,
no changes to execution or tick. The building blocks already exist:

- `schedule.occurrences_since(cron, tz, since, now)` — enumerates expected run
  times in `(since, now]` with DST fall-back dedup (schedule.py:59).
- `history.last_scheduled_terminal_run` — last terminal record that is *not*
  from a manual/event/request run, so an off-schedule run (issues 074/075)
  neither satisfies nor shifts the missed window (history.py:380).
- `history.first_seen_at` — the anchor for a seen-but-never-run job
  (history.py:413).
- `history.is_currently_running` — a live run means not missed (history.py:452).

### Part 1 — `missed_runs()` + `MissedRow` in `digest.py`

```python
@dataclass(frozen=True, slots=True)
class MissedRow:
    name: str
    expected: datetime      # most recent occurrence the job should have fired for
    last_state: str | None  # None = has never had a terminal run
    last_ts: datetime | None
    late: timedelta         # now - expected, beyond grace
```

`missed_runs(config, history_path, *, now) -> list[MissedRow]`, in config
order, one alert per job (not per occurrence — the single most-expected one).
For each job in `config.jobs`:

- Skip if `not job.enabled` — disabled jobs are issue 046's territory and never
  produce missed-run alerts, regardless of `--include-disabled` (which only
  affects the per-job rows).
- Skip if `job.cron is None` (event/request-trigger jobs — no schedule to be
  "missed" against).
- Skip if `history.is_currently_running(history_path, job.name)` (or the
  equivalent live-run probe it exposes) — a run in flight is the opposite of
  missed.
- Anchor `since` = `last_scheduled_terminal_run(...).ts`, else
  `first_seen_at(...)`. If both are `None` (job never seen by any tick), skip:
  a brand-new unregistered job is benign "never run", not evidence of breakage.
- `occurrences = schedule.occurrences_since(job.cron, job.timezone, since, now)`.
  Empty → not missed.
- Grace window: the job's existing `catch_up_minutes` (config default 120),
  the same knob the tick already uses to say "late but run anyway". No new
  config field; documented in the docstring. Typically the occurrence is beyond
  catch_up — that is *why* the tick never ran or recorded it.
- Missed iff `now - latest_occurrence > grace`. Record `expected` and `late`.

Never-run conservatism (per the issue's noise-avoidance rules): a job alerts as
"never run; expected by …" only when an occurrence exists after its
`first_seen_at` and that occurrence is beyond grace — i.e. at least one full
cycle came and went after registration. A job registered 5 minutes ago with a
daily cron cannot fire a false alert.

### Part 2 — rendering: separate section, per-job rows untouched

`render_digest` gains an optional keyword parameter
`missed: Sequence[MissedRow] = ()` (backward compatible; existing callers and
tests pass nothing). When non-empty, after the per-job rows and before the
drift section it emits:

```

missed runs (should have run, didn't):
- <job>: last terminal <state> at <when> (expected by <expected-when>; overdue
  <human>)
- <job>: never run (expected by <expected-when>)

```

times rendered in the same `timezone` as the header. `digest_now` computes
`missed_runs(...)` and passes it through (the `now` it already takes for
rendering is the same `now` used for detection — one clock, no ambiguity). The
per-job lines are not changed in any way: a missed run is an *additional*
callout, never a rewrite of the terminal-state line, and never rendered inside
the failure sequence. `cli._cmd_digest` is unchanged — it already calls
`digest_now`.

## Files touched

| File | Change |
| ---- | ------ |
| `src/herdr_routines/digest.py` | Part 1: `MissedRow`, `missed_runs()`; Part 2: `render_digest(missed=...)` section, `digest_now` wiring, module docstring |
| `tests/test_digest.py` | New tests below; existing digest tests unchanged and green |

## Risks

- [blocking] **Off-schedule anchors poison the window.** Using plain
  `last_terminal_run` would let a manual/request run suppress a genuinely
  missed alert ("I ran it by hand at 15:00, the 03:00 cron failure hides").
  `last_scheduled_terminal_run` exists precisely for this (074/075); the test
  below pins it. — confidence: high
- [blocking] **False positives on never-run jobs.** A newly added job with no
  history must not alert before one cycle beyond grace has passed since
  first-seen; a never-seen job never alerts. Both conservative thresholds are
  explicit in the code and covered by tests. — confidence: high
- [blocking] **Double reporting an active run.** A job currently running (or a
  registered-but-running run) is not missed; `is_currently_running` guard plus
  test. — confidence: high
- [non-blocking] **Grace semantics reuse.** `catch_up_minutes` doubles as the
  digest grace; a job with `catch_up_minutes: 0` (explicitly "don't run late")
  would alert almost immediately after any miss. That matches its configured
  intent but is worth a comment; no new config field, per the issue. —
  confidence: medium
- [non-blocking] **Multiple history reads.** `last_scheduled_terminal_run` /
  `first_seen_at` / `is_currently_running` each re-read the history file per
  job — the same per-job pattern `build_digest` already uses; fine for small
  configs, noted rather than optimized. — confidence: high
- [non-blocking] **Timezone/DST.** Detection delegates to
  `occurrences_since`, which already dedupes DST fall-back; Montevideo has no
  DST, so regressions here surface only in other zones (same limitation as the
  tick, accepted there). — confidence: high

## Acceptance criteria

1. [blocking] An enabled job whose schedule says it should have run by digest
   time but whose last scheduled terminal run is stale beyond grace appears in
   a separate "missed runs (should have run, didn't)" section with the last
   terminal state, when, expected occurrence and overdue amount. — confidence:
   high — Test: test_missed_run_renders_in_its_own_section
2. [blocking] Disabled jobs never produce missed-run alerts, regardless of
   `--include-disabled`. — confidence: high — Test:
   test_disabled_jobs_are_never_missed
3. [blocking] The per-job line for a missed job is unchanged (not rewritten as
   failed, not moved into any failure grouping); the missed alert is additive
   and visually separate. — confidence: high — Test:
   test_missed_job_line_is_unchecked_and_additive
4. [blocking] Detection reads only history, reports and job config; no new
   state store, no records written; enabled jobs with recent terminal states
   digest exactly as before (existing tests green unchanged). — confidence:
   high — Test: test_recent_terminal_run_within_grace_is_never_missed
5. [non-blocking] For never-run enabled jobs the alert names the expectation
   explicitly ("never run (expected by …)") and only fires after one full
   cycle beyond grace since first-seen; never-seen jobs stay benign "never
   run" (one test covering: unseen job no alert, freshly-seen job no alert,
   seen and overdue job alerts with the "never run" phrasing). — confidence:
   high — Test: test_never_run_jobs_are_conservative
6. [non-blocking] Off-schedule runs (manual/event/request) do not satisfy the
   expected window. — confidence: medium — Test:
   test_off_schedule_runs_do_not_hide_a_missed_cron
7. [non-blocking] A job with a live run in flight is not flagged missed. —
   confidence: medium — Test: test_running_job_is_not_missed

## Changelog v1→v2

- Added `## Acceptance criteria`: seven numbered items (blocking/non-blocking
  with confidence tiers), each ending `Test: <name>`.
- Kept the v1 title numbering consistent with the new version (spec header now
  v2); no changes to Problem, Approach, Files touched, or Risks content.

## Review

- blocking: 1–4 gate the merge (detection correct, separate section, additive
  lines, read-only).
- non-blocking: 5–7 are conservatism/noise guards; each has a named test but
  losing one degrades UX, not correctness.
- CI gate: `scripts/ci-check.sh` green (ruff format, ruff check, mypy, pytest).
