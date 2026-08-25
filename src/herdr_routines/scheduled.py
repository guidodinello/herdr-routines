"""`herdr-routines scheduled`: what's scheduled, per jobs.yaml.

Read-only view over `config.load_config()` output: for every job (disabled ones
included and marked), the next cron fire reuses `schedule.occurrences_since`
(the exact croniter + ZoneInfo enumeration `tick.decide()` uses, including its DST
fall-back dedup) so the table can never disagree with what tick would do. Last-run
state comes from `history.last_terminal_run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from herdr_routines.config import RoutinesConfig
from herdr_routines.history import last_terminal_run
from herdr_routines.schedule import occurrences_since
from herdr_routines.table import render_table

# Upper bound for next-fire search; a valid crontab fires at least once a year except
# for leap-day-only expressions ("0 0 29 2 *"), which legitimately render as "never".
NEXT_FIRE_HORIZON = timedelta(days=366)


def next_fire_at(cron: str, timezone: str, *, now: datetime) -> datetime | None:
    """First occurrence strictly after `now`, in the job's timezone, using the same
    occurrence enumeration (and DST dedup) as schedule.decide()."""
    horizon = now.astimezone(UTC) + NEXT_FIRE_HORIZON
    occurrences = occurrences_since(cron, timezone, now, horizon)
    return occurrences[0] if occurrences else None


@dataclass(frozen=True, slots=True)
class ScheduledRow:
    name: str
    enabled: bool
    cron: str
    timezone: str
    next_fire: datetime | None
    last_state: str | None


def build_scheduled_rows(
    config: RoutinesConfig, history_path: Path, *, now: datetime
) -> list[ScheduledRow]:
    rows = []
    for job in config.jobs:
        last = last_terminal_run(history_path, job.name)
        rows.append(
            ScheduledRow(
                name=job.name,
                enabled=job.enabled,
                cron=job.cron,
                timezone=job.timezone,
                # Disabled jobs still show their would-be next fire, marked inactive —
                # a disabled job's schedule must stay inspectable (spec criterion 4).
                next_fire=next_fire_at(job.cron, job.timezone, now=now),
                last_state=last.state if last else None,
            )
        )
    return rows


def _format_next_fire(row: ScheduledRow) -> str:
    if row.next_fire is None:
        return "never (within 1y)"
    local = row.next_fire.astimezone(ZoneInfo(row.timezone))
    return local.strftime("%Y-%m-%d %H:%M %Z")


def render_scheduled(rows: list[ScheduledRow]) -> str:
    return render_table(
        ["JOB", "ENABLED", "CRON", "TZ", "NEXT FIRE", "LAST RUN"],
        [
            [
                row.name,
                "yes" if row.enabled else "DISABLED",
                row.cron,
                row.timezone,
                _format_next_fire(row),
                row.last_state or "never run",
            ]
            for row in rows
        ],
    )
