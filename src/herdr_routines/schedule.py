"""Due/missed/skip decision for one job at one tick.

Pure: takes `now` and history records as arguments rather than reading the clock or the
history file itself, so every case in docs/plan-v1.md §4 is exercisable without mocking time
or touching disk. See test_schedule.py for the cases that actually matter (never-run-before,
grace window, latest-occurrence-only, DST fall-back).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from croniter import croniter

from herdr_routines.history import HistoryRecord


class Decision(Enum):
    NOT_DUE = "not_due"
    RUN = "run"
    MISSED = "missed"  # too late to catch up; nothing to run


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    decision: Decision
    occurrence: datetime | None = (
        None  # the occurrence to run, or the one reported as missed
    )
    late_seconds: float | None = None
    skipped_occurrences: int = 0  # earlier occurrences in the interval, collapsed
    skipped_first: datetime | None = None
    skipped_last: datetime | None = None


def _occurrences_since(
    cron: str, tz_name: str, since: datetime, now: datetime
) -> list[datetime]:
    """All cron occurrences in the half-open interval (since, now], in tz_name, returned as
    tz-aware datetimes. `since` and `now` must both be tz-aware.

    croniter's `get_next` always returns a time strictly after the iterator's current position,
    so seeding it with `since` and calling `get_next` repeatedly enumerates exactly (since, now]
    with no risk of re-emitting `since` itself.

    DST fall-back note: on the day local clocks repeat an hour, croniter (correctly) emits two
    distinct occurrences for a cron time that falls in the repeated hour — they really are two
    different UTC instants that both match the same local wall-clock time. Left alone, that
    would fire the job twice. We dedupe by naive local wall-clock time, keeping only the first
    (chronologically earlier, i.e. pre-fallback) occurrence for each repeated local time, so a
    cron like "30 2 * * *" fires once on fall-back day rather than at both 02:30 instants.
    """
    tz = ZoneInfo(tz_name)
    it = croniter(cron, since.astimezone(tz))
    occurrences: list[datetime] = []
    seen_local_times: set[datetime] = set()
    while True:
        nxt = it.get_next(datetime)
        if nxt > now:
            break
        local_wall_clock = nxt.replace(tzinfo=None)
        if local_wall_clock in seen_local_times:
            continue
        seen_local_times.add(local_wall_clock)
        occurrences.append(nxt)
    return occurrences


def decide(
    *,
    cron: str,
    timezone: str,
    catch_up_minutes: int,
    now: datetime,
    last_terminal: HistoryRecord | None,
    job_registered_at: datetime,
) -> ScheduleResult:
    """Decide what a tick should do for one job right now.

    `last_terminal` is the most recent terminal-state history record for this job (see
    history.last_terminal_run), or None if the job has never finished. `job_registered_at` is
    when the job was first seen by any tick — used as the clock's starting point for a job
    that has never run, so a brand-new job does not immediately backfill (docs/plan-v1.md §4
    step 1).
    """
    since = last_terminal.ts if last_terminal is not None else job_registered_at
    occurrences = _occurrences_since(cron, timezone, since, now)

    if not occurrences:
        return ScheduleResult(decision=Decision.NOT_DUE)

    latest = occurrences[-1]
    earlier = occurrences[:-1]
    late = now - latest
    grace = timedelta(minutes=catch_up_minutes)

    skipped_kwargs = {}
    if earlier:
        skipped_kwargs = {
            "skipped_occurrences": len(earlier),
            "skipped_first": earlier[0],
            "skipped_last": earlier[-1],
        }

    if late <= grace:
        return ScheduleResult(
            decision=Decision.RUN,
            occurrence=latest,
            late_seconds=late.total_seconds(),
            **skipped_kwargs,
        )

    return ScheduleResult(
        decision=Decision.MISSED,
        occurrence=latest,
        late_seconds=late.total_seconds(),
        **skipped_kwargs,
    )
