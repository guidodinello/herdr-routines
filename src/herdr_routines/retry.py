"""Pure policy for retries scheduled across ticks (issue 088).

No clock reads, no history writes, no sleeping: the tick asks these functions what a
pending retry means and appends the records they build. Backoff is measured in timer
intervals (`TICK_INTERVAL_SECONDS`), so a retry costs a later tick rather than blocking
the current one on a sleep while the global lock is held.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from herdr_routines.config import Job
from herdr_routines.dispatch_common import Occurrence
from herdr_routines.history import (
    NOT_A_RUN_REASONS,
    TERMINAL_STATES,
    TRIGGER_CRON,
    HistoryExtra,
    HistoryRecord,
)
from herdr_routines.runner import make_run_id
from herdr_routines.schedule import next_occurrence_after

TICK_INTERVAL_SECONDS = 300
RETRY_PENDING_REASON = "retry_pending"
RETRY_DROPPED_REASON = "retry_dropped"
# Contention says "the occurrence is still owed", so it must not consume a pending retry.
# `agent_name_live` is deliberately not in NOT_A_RUN_REASONS: it is a skip written before
# the schedule decision, not a record that feeds `schedule.decide`.
CONTENTION_REASONS = NOT_A_RUN_REASONS | {"agent_name_live"}

RetryDecision = Literal["drop", "due", "wait"]


@dataclass(frozen=True, slots=True)
class PendingRetry:
    """An unresolved `scheduled` marker, decoded from its history record."""

    run_id: str | None
    attempt: int
    max_retries: int
    trigger: str
    reason: str
    failed_at: datetime
    due_at: datetime


def retry_due_at(failed_at: datetime, attempt_index: int) -> datetime:
    """`failed_at` plus 1, 2 or 4 timer intervals for attempt index 0, 1 or 2."""
    return failed_at + timedelta(seconds=TICK_INTERVAL_SECONDS * 2**attempt_index)


def pending_retry(records: list[HistoryRecord]) -> PendingRetry | None:
    """The latest `retry_pending` marker that no later terminal run has resolved.

    A `running` record, or a contention skip such as `checkout_busy`, leaves the marker
    pending: the retry is still owed. A real terminal record, or a `retry_dropped` skip,
    resolves it.
    """
    pending: PendingRetry | None = None
    for record in records:
        extra = record.extra or {}
        reason = extra.get("reason")
        if record.state == "scheduled" and reason == RETRY_PENDING_REASON:
            due = extra.get("retry_due_at")
            attempt = extra.get("attempt")
            if not isinstance(due, str) or not isinstance(attempt, int):
                continue
            retry_of = extra.get("retry_of")
            max_retries = extra.get("max_retries")
            trigger = extra.get("trigger")
            eligible = extra.get("retry_eligible_reason")
            pending = PendingRetry(
                run_id=retry_of if isinstance(retry_of, str) else None,
                attempt=attempt,
                max_retries=max_retries if isinstance(max_retries, int) else 0,
                trigger=trigger if isinstance(trigger, str) else TRIGGER_CRON,
                reason=eligible if isinstance(eligible, str) else "",
                failed_at=record.ts,
                due_at=datetime.fromisoformat(due),
            )
            continue
        if pending is None:
            continue
        if (
            record.state == "skipped"
            and reason == RETRY_DROPPED_REASON
            or record.state in TERMINAL_STATES
            and (not isinstance(reason, str) or reason not in CONTENTION_REASONS)
        ):
            pending = None
    return pending


def retry_decision(
    pending: PendingRetry, *, cron: str | None, timezone: str, now: datetime
) -> RetryDecision:
    """Whether a pending retry is due now, superseded by the next cron fire, or waiting."""
    if cron is not None:
        next_fire = next_occurrence_after(cron, timezone, pending.failed_at)
        if next_fire <= pending.due_at and next_fire <= now:
            return "drop"
    if now >= pending.due_at:
        return "due"
    return "wait"


def schedule_record(
    job: Job, failed: HistoryRecord, attempt: int, *, trigger: str
) -> HistoryRecord:
    """Build the non-terminal `scheduled` marker for the next attempt."""
    extra = HistoryExtra(
        reason=RETRY_PENDING_REASON,
        trigger=trigger,
        attempt=attempt,
        max_retries=job.retry_attempts,
        retry_of=failed.run_id,
        retry_eligible_reason=str((failed.extra or {}).get("reason")),
        retry_due_at=retry_due_at(failed.ts, attempt - 1).isoformat(),
    )
    return HistoryRecord.new(ts=failed.ts, job=job.name, state="scheduled", extra=extra)


def retry_occurrence(job: Job, pending: PendingRetry, *, now: datetime) -> Occurrence:
    """Build the ordinary occurrence a due retry dispatches through."""
    return Occurrence(
        make_run_id(job.name, now),
        HistoryExtra(
            trigger=pending.trigger,
            attempt=pending.attempt,
            max_retries=pending.max_retries,
            retried_from_run_id=pending.run_id or "",
        ),
    )
