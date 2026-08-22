"""Run history: append-only JSONL, plus the read-back queries `schedule.py` needs.

The file is a log of state transitions, not a mutable record set — see docs/plan-v1.md §5.
States: registered, scheduled, running, done, failed, skipped, missed, interrupted_unknown.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TERMINAL_STATES = frozenset(
    {"done", "failed", "skipped", "missed", "interrupted_unknown"}
)
NON_TERMINAL_STATES = frozenset({"registered", "scheduled", "running"})
ALL_STATES = TERMINAL_STATES | NON_TERMINAL_STATES

# How much slack beyond a job's own timeout before an orphaned "running" record is
# considered stale rather than genuinely still in progress. See docs/plan-v1.md §4.
STALE_MARGIN = timedelta(minutes=5)


def default_history_path() -> Path:
    """$HERDR_PLUGIN_STATE_DIR/history.jsonl if set, else ~/.local/state/herdr-routines/.

    Same forethought as config.default_config_path — keeps the door open for the optional
    plugin manifest in docs/plan-v1.md §8.4 without moving files later.
    """
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if plugin_dir:
        return Path(plugin_dir) / "history.jsonl"
    return Path.home() / ".local" / "state" / "herdr-routines" / "history.jsonl"


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    ts: datetime
    job: str
    state: str
    run_id: str | None = None
    extra: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        payload: dict[str, Any] = {
            "ts": self.ts.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "job": self.job,
            "state": self.state,
        }
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.extra:
            payload.update(self.extra)
        return json.dumps(payload, sort_keys=False)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> HistoryRecord:
        ts = datetime.fromisoformat(d["ts"])
        known = {"ts", "job", "state", "run_id"}
        extra = {k: v for k, v in d.items() if k not in known} or None
        return HistoryRecord(
            ts=ts, job=d["job"], state=d["state"], run_id=d.get("run_id"), extra=extra
        )


def append(path: Path, record: HistoryRecord) -> None:
    """Append one record. Callers needing overlap-safety should hold the tick lock first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(record.to_json_line())
        f.write("\n")


def read_all(path: Path) -> list[HistoryRecord]:
    """Read every record in file order (oldest first). Empty list if the file doesn't exist."""
    if not path.exists():
        return []
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(HistoryRecord.from_dict(json.loads(line)))
    return records


def read_job(
    path: Path, job_name: str, *, limit: int | None = None
) -> list[HistoryRecord]:
    """Records for one job, oldest first. `limit`, if given, keeps the most recent `limit`."""
    records = [r for r in read_all(path) if r.job == job_name]
    if limit is not None and len(records) > limit:
        records = records[-limit:]
    return records


def last_terminal_run(path: Path, job_name: str) -> HistoryRecord | None:
    """Most recent record in a terminal state for this job, or None if it has never finished
    (including if it has never run at all). Non-terminal records (registered/scheduled/running)
    are deliberately ignored here — see docs/plan-v1.md §4 step 1."""
    for record in reversed(read_job(path, job_name)):
        if record.state in TERMINAL_STATES:
            return record
    return None


def has_ever_been_seen(path: Path, job_name: str) -> bool:
    """Whether any record at all exists for this job (terminal or not)."""
    return any(read_job(path, job_name, limit=1))


def first_seen_at(path: Path, job_name: str) -> datetime | None:
    """Timestamp of the earliest record for this job (normally its 'registered' record), or
    None if the job has never been seen. This — not the current tick's `now` — is the correct
    `job_registered_at` to feed schedule.decide() for a job that has been seen but never had a
    terminal run yet; using `now` there would make the search window always empty."""
    records = read_job(
        path, job_name
    )  # oldest-first; take the earliest, not read_job's `limit`
    # (which keeps the most recent N) — we want the first record this job ever got.
    return records[0].ts if records else None


def find_stale_running(
    path: Path, job_name: str, *, timeout_ms: int, now: datetime
) -> HistoryRecord | None:
    """The most recent 'running' record for this job, if it has no later terminal record for the
    same run_id and has been running longer than timeout_ms + STALE_MARGIN. This is the recovery
    path for a tick that was killed mid-run — see docs/plan-v1.md §4."""
    records = read_job(path, job_name)
    terminal_run_ids = {
        r.run_id for r in records if r.state in TERMINAL_STATES and r.run_id
    }

    latest_running: HistoryRecord | None = None
    for record in records:
        if record.state == "running":
            latest_running = record

    if latest_running is None:
        return None
    if latest_running.run_id in terminal_run_ids:
        return None

    deadline = latest_running.ts + timedelta(milliseconds=timeout_ms) + STALE_MARGIN
    if now <= deadline:
        return None
    return latest_running


def is_currently_running(
    path: Path, job_name: str, *, timeout_ms: int, now: datetime
) -> bool:
    """True if the job has an active (non-stale) 'running' record with no terminal record yet."""
    records = read_job(path, job_name)
    terminal_run_ids = {
        r.run_id for r in records if r.state in TERMINAL_STATES and r.run_id
    }

    latest_running: HistoryRecord | None = None
    for record in records:
        if record.state == "running":
            latest_running = record

    if latest_running is None or latest_running.run_id in terminal_run_ids:
        return False

    deadline = latest_running.ts + timedelta(milliseconds=timeout_ms) + STALE_MARGIN
    return now <= deadline
