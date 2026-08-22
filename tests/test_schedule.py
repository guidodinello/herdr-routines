from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from herdr_routines.history import HistoryRecord
from herdr_routines.schedule import Decision, decide


def _decide(**kwargs):
    return decide(**kwargs)


def test_never_run_before_does_not_backfill() -> None:
    """A brand-new daily job, registered just before now, must not immediately fire just
    because a nearer occurrence exists somewhere in the unbounded past (docs/plan-v1.md §4)."""
    registered_at = datetime(2026, 8, 22, 2, 59, 0, tzinfo=UTC)
    now = datetime(2026, 8, 22, 3, 0, 30, tzinfo=UTC)  # 30s after 03:00
    result = _decide(
        cron="0 3 * * *",
        timezone="UTC",
        catch_up_minutes=120,
        now=now,
        last_terminal=None,
        job_registered_at=registered_at,
    )
    # There IS an occurrence at 03:00 today, which is fine (it's after registration) —
    # the key assertion is that registration, not epoch, bounds the search.
    assert result.decision == Decision.RUN
    assert result.occurrence == datetime(2026, 8, 22, 3, 0, 0, tzinfo=UTC)


def test_never_run_before_registered_after_only_occurrence_is_not_due() -> None:
    """If the job was only just registered, well after today's occurrence already happened,
    it must not treat that (pre-registration) occurrence as due."""
    registered_at = datetime(
        2026, 8, 22, 5, 0, 0, tzinfo=UTC
    )  # registered after 03:00 fired
    now = datetime(2026, 8, 22, 5, 30, 0, tzinfo=UTC)
    result = _decide(
        cron="0 3 * * *",
        timezone="UTC",
        catch_up_minutes=120,
        now=now,
        last_terminal=None,
        job_registered_at=registered_at,
    )
    assert result.decision == Decision.NOT_DUE


def test_not_due_when_no_occurrence_in_window() -> None:
    last = HistoryRecord(
        ts=datetime(2026, 8, 22, 3, 0, 5, tzinfo=UTC), job="a", state="done"
    )
    now = datetime(2026, 8, 22, 3, 30, 0, tzinfo=UTC)
    result = _decide(
        cron="0 3 * * *",
        timezone="UTC",
        catch_up_minutes=120,
        now=now,
        last_terminal=last,
        job_registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.decision == Decision.NOT_DUE


def test_run_inside_grace_window() -> None:
    last = HistoryRecord(
        ts=datetime(2026, 8, 21, 3, 0, 5, tzinfo=UTC), job="a", state="done"
    )
    # Today's 03:00 occurrence, tick fires 40 minutes late (reboot).
    now = datetime(2026, 8, 22, 3, 40, 0, tzinfo=UTC)
    result = _decide(
        cron="0 3 * * *",
        timezone="UTC",
        catch_up_minutes=120,
        now=now,
        last_terminal=last,
        job_registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.decision == Decision.RUN
    assert result.occurrence == datetime(2026, 8, 22, 3, 0, 0, tzinfo=UTC)
    assert result.late_seconds == 40 * 60


def test_missed_outside_grace_window() -> None:
    last = HistoryRecord(
        ts=datetime(2026, 8, 21, 3, 0, 5, tzinfo=UTC), job="a", state="done"
    )
    now = datetime(2026, 8, 22, 6, 0, 0, tzinfo=UTC)  # 3h late, grace is 120 min
    result = _decide(
        cron="0 3 * * *",
        timezone="UTC",
        catch_up_minutes=120,
        now=now,
        last_terminal=last,
        job_registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.decision == Decision.MISSED
    assert result.occurrence == datetime(2026, 8, 22, 3, 0, 0, tzinfo=UTC)


def test_catch_up_zero_means_no_backfill_at_all() -> None:
    last = HistoryRecord(
        ts=datetime(2026, 8, 21, 3, 0, 5, tzinfo=UTC), job="a", state="done"
    )
    now = datetime(2026, 8, 22, 3, 0, 30, tzinfo=UTC)  # 30s late
    result = _decide(
        cron="0 3 * * *",
        timezone="UTC",
        catch_up_minutes=0,
        now=now,
        last_terminal=last,
        job_registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.decision == Decision.MISSED


def test_multiple_missed_occurrences_collapse_to_one_record() -> None:
    """An hourly job down for 8 hours should report one missed record with a count, not eight
    (docs/plan-v1.md §4 step 5) — and only the latest occurrence should actually run."""
    last = HistoryRecord(
        ts=datetime(2026, 8, 22, 0, 0, 5, tzinfo=UTC), job="a", state="done"
    )
    now = datetime(
        2026, 8, 22, 8, 5, 0, tzinfo=UTC
    )  # 8 occurrences missed (01:00..08:00)
    result = _decide(
        cron="0 * * * *",
        timezone="UTC",
        catch_up_minutes=120,
        now=now,
        last_terminal=last,
        job_registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.decision == Decision.RUN  # 08:00 is only 5 min late, within grace
    assert result.occurrence == datetime(2026, 8, 22, 8, 0, 0, tzinfo=UTC)
    assert result.skipped_occurrences == 7
    assert result.skipped_first == datetime(2026, 8, 22, 1, 0, 0, tzinfo=UTC)
    assert result.skipped_last == datetime(2026, 8, 22, 7, 0, 0, tzinfo=UTC)


def test_dst_fallback_fires_exactly_once() -> None:
    """Europe/Madrid falls back from CEST to CET on the last Sunday of October, at 03:00 local
    time (clocks go back to 02:00), so the local hour 02:00-03:00 happens twice. A job scheduled
    for 02:30 must fire once, not twice, in a zone we don't deploy in but should still get right
    (docs/plan-v1.md context: Montevideo has had no DST since 2015, so this can't be exercised
    against the real deployment timezone)."""
    tz = ZoneInfo("Europe/Madrid")
    last = HistoryRecord(
        ts=datetime(2026, 10, 24, 2, 30, 0, tzinfo=tz).astimezone(UTC),
        job="a",
        state="done",
    )
    # 2026-10-25 is DST fall-back Sunday in Europe/Madrid: 03:00 CEST -> 02:00 CET.
    # Ask right after the second local 02:30 has passed.
    now = datetime(2026, 10, 25, 2, 30, 0, tzinfo=tz).replace(fold=1).astimezone(
        UTC
    ) + timedelta(minutes=5)
    result = _decide(
        cron="30 2 * * *",
        timezone="Europe/Madrid",
        catch_up_minutes=120,
        now=now,
        last_terminal=last,
        job_registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.decision == Decision.RUN
    assert result.skipped_occurrences == 0  # exactly one fire, not two


def test_local_timezone_offset_from_utc_is_respected() -> None:
    """A cron of '0 3 * * *' in America/Montevideo (UTC-3) is 06:00 UTC, not 03:00 UTC."""
    last = HistoryRecord(
        ts=datetime(2026, 8, 21, 3, 0, 5, tzinfo=UTC), job="a", state="done"
    )
    now = datetime(2026, 8, 22, 6, 5, 0, tzinfo=UTC)
    result = _decide(
        cron="0 3 * * *",
        timezone="America/Montevideo",
        catch_up_minutes=120,
        now=now,
        last_terminal=last,
        job_registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.decision == Decision.RUN
    assert result.occurrence == datetime(2026, 8, 22, 6, 0, 0, tzinfo=UTC)
