"""Daily digest: one morning summary of every job's last terminal state and report link.

Issue 010. Reads existing run history (`history.jsonl`) and the reports directory only —
no new state store, no new per-job config field. A job with no history at all (never
seen, or seen but never terminal) renders as "never run" rather than being omitted, so a
newly added job that hasn't fired yet is still visible in the digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from herdr_routines.config import RoutinesConfig
from herdr_routines.history import HistoryRecord, last_terminal_run


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


def render_digest(
    rows: list[DigestRow],
    *,
    timezone: str,
    now: datetime,
    all_disabled: bool = False,
) -> str:
    """Plain-text digest body, one line per job. `timezone` renders each job's last-run
    timestamp in local time (there's no single "the" timezone across jobs, so this is a
    display choice, not a per-job one — good enough for a morning summary).

    When *all_disabled* is True the empty-row message reads ``(all jobs disabled)``
    instead of ``(no jobs configured)`` — callers use this to distinguish "nothing to
    run" from "nothing configured"."""
    tz = ZoneInfo(timezone)
    header = (
        f"herdr-routines digest — {now.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')}"
    )
    if not rows:
        marker = "(all jobs disabled)" if all_disabled else "(no jobs configured)"
        return f"{header}\n{marker}"

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
    return "\n".join(lines)


def digest_now(
    config: RoutinesConfig,
    history_path: Path,
    reports_dir: Path,
    *,
    timezone: str,
    now: datetime | None = None,
    include_disabled: bool = False,
) -> str:
    """Convenience wrapper: build + render in one call, for the CLI handler."""
    now = now or datetime.now(UTC)
    rows = build_digest(
        config, history_path, reports_dir, include_disabled=include_disabled
    )
    all_disabled = not include_disabled and bool(config.jobs) and not rows
    return render_digest(rows, timezone=timezone, now=now, all_disabled=all_disabled)
