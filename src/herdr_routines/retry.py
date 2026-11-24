"""Tick-based retries of a failed scheduled run (issue 088).

A failed run is never retried by sleeping inside the tick: that would hold the tick for every
job dispatched after it. The dispatch instead writes a `scheduled` record
(`reason: retry_pending`) carrying the tick the retry becomes due, and a later tick runs it.
Retry N (1-based) is due 2^(N-1) ticks after the failure: 1, 2, then 4 ticks.

Pure functions over history records and `datetime`s, like `schedule.py`. The wall-clock
side lives in tick.py.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from herdr_routines.config import Job
from herdr_routines.history import (
    TERMINAL_STATES,
    HistoryExtra,
    HistoryRecord,
)

# `deploy/systemd/herdr-routines.timer` fires `OnCalendar=*:0/5`. The backoff is counted in
# these ticks, so a change to the timer's period means a change here.
TICK_PERIOD = timedelta(minutes=5)

# `reason` of the `scheduled` record a failed attempt leaves behind.
RETRY_PENDING_REASON = "retry_pending"
# `reason` of the `skipped` record that discards a pending retry.
RETRY_DROPPED_REASON = "retry_dropped"


def retry_due(failed_at: datetime, failed_attempt: int) -> tuple[datetime, int]:
    """`(when, ticks)` for the retry after attempt `failed_attempt` (0 = the first try).

    Ticks fire on wall-clock multiples of `TICK_PERIOD`, so "1 tick later" is the first
    boundary after the failure (a failure late inside a tick must not miss it by seconds),
    and each further doubling adds whole ticks."""
    ticks = 2**failed_attempt
    period_s = TICK_PERIOD.total_seconds()
    first_boundary_s = (math.floor(failed_at.timestamp() / period_s) + 1) * period_s
    due_s = first_boundary_s + (ticks - 1) * period_s
    return datetime.fromtimestamp(due_s, tz=UTC), ticks


def eligible_failure(
    new_records: Sequence[HistoryRecord], job: Job
) -> HistoryRecord | None:
    """The dispatch's final outcome, when it is a failure `job.retry_on` lists.

    `new_records` are the records one dispatch wrote. Only the last terminal one counts: a
    later `done` means the dispatch ended well, whatever failed before it."""
    terminals = [r for r in new_records if r.state in TERMINAL_STATES]
    if not terminals:
        return None
    last = terminals[-1]
    reason = (last.extra or {}).get("reason")
    if last.state == "failed" and isinstance(reason, str) and reason in job.retry_on:
        return last
    return None


def pending_retry_record(
    job: Job,
    failure: HistoryRecord,
    *,
    failed_attempt: int,
    occurrence_extra: HistoryExtra,
) -> HistoryRecord | None:
    """The `scheduled` record that queues the next attempt, or None when the job has none
    left. Carries what the retry's occurrence needs: the original `scheduled_for`, the
    failed run's id, and the due time."""
    if failed_attempt >= job.retry_attempts:
        return None
    due, ticks = retry_due(failure.ts, failed_attempt)
    extra = HistoryExtra(
        reason=RETRY_PENDING_REASON,
        attempt=failed_attempt,
        max_retries=job.retry_attempts,
        retry_eligible_reason=str((failure.extra or {}).get("reason")),
        retry_due_at=due.isoformat(),
        retry_after_ticks=ticks,
    )
    for key in ("trigger", "scheduled_for"):
        value = occurrence_extra.get(key)
        if isinstance(value, str):
            extra[key] = value
    if failure.run_id is not None:
        extra["retried_from_run_id"] = failure.run_id
    return HistoryRecord.new(
        ts=failure.ts,
        job=job.name,
        state="scheduled",
        run_id=failure.run_id,
        extra=extra,
    )


@dataclass(frozen=True, slots=True)
class PendingRetry:
    record: HistoryRecord
    failed_attempt: int
    due_at: datetime
    scheduled_for: str | None
    failed_run_id: str | None


def find_pending_retry(records: Sequence[HistoryRecord]) -> PendingRetry | None:
    """The retry a job is still owed, from its records (oldest first).

    A `retry_pending` record is open until a later `running` record (the retry started, or
    something else did) or a `retry_dropped` record closes it."""
    pending: HistoryRecord | None = None
    for record in records:
        extra = record.extra or {}
        if record.state == "scheduled" and extra.get("reason") == RETRY_PENDING_REASON:
            pending = record
        elif record.state == "running" or (
            record.state == "skipped" and extra.get("reason") == RETRY_DROPPED_REASON
        ):
            pending = None
    if pending is None:
        return None
    extra = pending.extra or {}
    due_at = extra.get("retry_due_at")
    attempt = extra.get("attempt")
    if not isinstance(due_at, str) or not isinstance(attempt, int):
        return None  # fails closed: a malformed marker is never run
    scheduled_for = extra.get("scheduled_for")
    return PendingRetry(
        record=pending,
        failed_attempt=attempt,
        due_at=datetime.fromisoformat(due_at),
        scheduled_for=scheduled_for if isinstance(scheduled_for, str) else None,
        failed_run_id=pending.run_id,
    )
