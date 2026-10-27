"""Daily digest: one morning summary of every job's last terminal state and report link.

Issue 010. Reads existing run history (`history.jsonl`) and the reports directory only —
no new state store, no new per-job config field. A job with no history at all (never
seen, or seen but never terminal) renders as "never run" rather than being omitted, so a
newly added job that hasn't fired yet is still visible in the digest.

Issue 072 adds a "missed runs" section: an enabled job can keep rendering an old terminal
state forever if its schedule silently stops firing (missed cron, service down, config
issue, wedged runner). Detection also reads only existing history and job config — no new
state, no writes — and reuses `catch_up_minutes` as the grace window, the same knob the
tick uses to decide an occurrence is too late to run. It is deliberately a separate
section so it cannot train the reader to ignore the per-job failure lines (the mirror of
issue 046's disabled-stale-failure fix).

A host drift section (`host_drift.check_host`) is appended when there is any: a
missing or stale systemd unit is otherwise invisible, and the digest is the one
message read every morning. It is not the only place drift is reported — the digest
timer was itself the unit missing on the Pi — so self-update reports it too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from herdr_routines.config import RoutinesConfig
from herdr_routines.history import (
    HistoryRecord,
    first_seen_at,
    is_currently_running,
    last_scheduled_terminal_run,
    last_terminal_run,
)
from herdr_routines.host_drift import DriftFinding, format_findings
from herdr_routines.schedule import occurrences_since


@dataclass(frozen=True, slots=True)
class DigestRow:
    name: str
    state: str | None  # None = never had a terminal run
    ts: datetime | None
    report_path: Path | None  # None if never run, or the run's report file is missing
    enabled: bool = True


def _report_path_for(record: HistoryRecord, reports_dir: Path) -> Path | None:
    """The report file `runner.execute_run` writes for this run, if it still exists.

    Report files live at `reports_dir / f"{run_id}.md"` (runner.default_reports_dir).
    A record with no run_id (shouldn't happen for a terminal state, but history is an
    append-only log we don't fully control) has no report to link.
    """
    if record.run_id is None:
        return None
    path = reports_dir / f"{record.run_id}.md"
    return path if path.exists() else None


def build_digest(
    config: RoutinesConfig,
    history_path: Path,
    reports_dir: Path,
    *,
    include_disabled: bool = False,
) -> list[DigestRow]:
    """One row per configured job, in config order, from existing history alone.

    When *include_disabled* is False (the default), jobs with ``enabled=False`` are
    omitted entirely.  When True, disabled rows are included with ``enabled=False`` so
    the renderer can mark them as historical rather than as active failures.
    """
    rows = []
    for job in config.jobs:
        record = last_terminal_run(history_path, job.name)
        row = DigestRow(
            name=job.name,
            state=record.state if record else None,
            ts=record.ts if record else None,
            report_path=_report_path_for(record, reports_dir) if record else None,
            enabled=job.enabled,
        )
        if not job.enabled and not include_disabled:
            continue
        rows.append(row)
    return rows


@dataclass(frozen=True, slots=True)
class MissedRow:
    """One enabled job whose schedule expects an occurrence it has not produced."""

    name: str
    expected: datetime  # most recent occurrence the job should have fired for
    last_state: str | None  # None = has never had a terminal run
    last_ts: datetime | None
    late: timedelta  # now - expected, beyond grace


def missed_runs(
    config: RoutinesConfig, history_path: Path, *, now: datetime
) -> list[MissedRow]:
    """Enabled jobs that should have run by *now* but have no terminal run for it.

    One alert per job, in config order, naming the single most recent expected occurrence
    (not one per occurrence). Disabled jobs are skipped: they are issue 046's territory and
    never alert here, regardless of `--include-disabled` (which only affects the per-job
    rows). Jobs without a cron trigger (event/request-only) have no schedule to miss, and a
    job with a live run in flight is the opposite of missed.

    The window starts at the last *scheduled* terminal run, so a manual/event/request run
    can neither satisfy nor shift it (issues 074/075); for a seen-but-never-run job it
    starts at `first_seen_at`. A job no tick has ever seen is benign "never run" and is
    skipped, and a never-run job only alerts once a full occurrence beyond grace has passed
    since it was first seen.

    Grace is the job's existing `catch_up_minutes` — the same knob the tick uses to call an
    occurrence too late to run. No new config field. `catch_up_minutes: 0` therefore alerts
    almost immediately after a miss, matching that job's explicit "don't run late" intent.
    """
    missed: list[MissedRow] = []
    for job in config.jobs:
        if not job.enabled or job.cron is None:
            continue
        if is_currently_running(
            history_path, job.name, timeout_ms=job.timeout_ms, now=now
        ):
            continue

        record = last_scheduled_terminal_run(history_path, job.name)
        since = (
            record.ts if record is not None else first_seen_at(history_path, job.name)
        )
        if since is None:
            continue

        occurrences = occurrences_since(job.cron, job.timezone, since, now)
        if not occurrences:
            continue
        expected = occurrences[-1]
        late = now - expected
        if late <= timedelta(minutes=job.catch_up_minutes):
            continue

        missed.append(
            MissedRow(
                name=job.name,
                expected=expected,
                last_state=record.state if record is not None else None,
                last_ts=record.ts if record is not None else None,
                late=late,
            )
        )
    return missed


def _human_delta(delta: timedelta) -> str:
    """Coarse, human-readable duration for the overdue callout (days/hours/minutes)."""
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def render_digest(
    rows: list[DigestRow],
    *,
    timezone: str,
    now: datetime,
    all_disabled: bool = False,
    drift: Sequence[DriftFinding] = (),
    missed: Sequence[MissedRow] = (),
) -> str:
    """Plain-text digest body, one line per job. `timezone` renders each job's last-run
    timestamp in local time (there's no single "the" timezone across jobs, so this is a
    display choice, not a per-job one — good enough for a morning summary).

    When *all_disabled* is True the empty-row message reads ``(all jobs disabled)``
    instead of ``(no jobs configured)`` — callers use this to distinguish "nothing to
    run" from "nothing configured".

    *missed* (issue 072) renders as an additional section after the per-job rows and
    before host drift. It never rewrites or regroups those rows — a missed job keeps its
    terminal-state line and merely gains a callout, so this cannot become background
    noise in the failure list."""
    tz = ZoneInfo(timezone)
    header = (
        f"herdr-routines digest — {now.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}"
    )
    drift_lines = (
        ["", "host drift (run `herdr-routines host-check`):", *format_findings(drift)]
        if drift
        else []
    )
    missed_lines = ["", "missed runs (should have run, didn't):"]
    for missed_row in missed:
        expected = missed_row.expected.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        if missed_row.last_state is None or missed_row.last_ts is None:
            missed_lines.append(
                f"- {missed_row.name}: never run (expected by {expected})"
            )
        else:
            last_when = missed_row.last_ts.astimezone(tz).strftime("%Y-%m-%d %H:%M")
            missed_lines.append(
                f"- {missed_row.name}: last terminal {missed_row.last_state} at "
                f"{last_when} (expected by {expected}; overdue "
                f"{_human_delta(missed_row.late)})"
            )
    if not missed:
        missed_lines = []
    if not rows:
        marker = "(all jobs disabled)" if all_disabled else "(no jobs configured)"
        return "\n".join([header, marker, *missed_lines, *drift_lines])

    lines = [header]
    for row in rows:
        when = row.ts.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if row.ts else None
        report = f" — {row.report_path}" if row.report_path else ""

        if not row.enabled:
            if row.state is None:
                lines.append(f"- {row.name}: disabled (never run)")
            else:
                lines.append(
                    f"- {row.name}: disabled (last: {row.state} at {when}){report}"
                )
            continue

        if row.state is None:
            lines.append(f"- {row.name}: never run")
            continue
        lines.append(f"- {row.name}: {row.state} at {when}{report}")
    return "\n".join([*lines, *missed_lines, *drift_lines])


def digest_now(
    config: RoutinesConfig,
    history_path: Path,
    reports_dir: Path,
    *,
    timezone: str,
    now: datetime | None = None,
    include_disabled: bool = False,
    drift: Sequence[DriftFinding] = (),
) -> str:
    """Convenience wrapper: build + render in one call, for the CLI handler."""
    now = now or datetime.now(UTC)
    rows = build_digest(
        config, history_path, reports_dir, include_disabled=include_disabled
    )
    all_disabled = not include_disabled and bool(config.jobs) and not rows
    missed = missed_runs(config, history_path, now=now)
    return render_digest(
        rows,
        timezone=timezone,
        now=now,
        all_disabled=all_disabled,
        drift=drift,
        missed=missed,
    )
