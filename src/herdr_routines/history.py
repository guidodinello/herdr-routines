"""Run history: append-only JSONL, plus the read-back queries `schedule.py` needs.

The file is a log of state transitions, not a mutable record set — see docs/plan-v1.md §5.
States actually written: registered, running, done, failed, skipped, missed, interrupted_unknown.

Rotation (issue 021) is non-destructive: `maybe_rotate` renames the live file to a
timestamped sibling (`history-<YYYYMMDDTHHMMSSZ>.jsonl`) and leaves a fresh empty live
file behind, so nothing is ever lost by rotating. Every read goes through `read_all`,
which spans the rolls, so no caller has to know they exist.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from logger import get_logger

log = get_logger(__name__)

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
    """Every record in file order (oldest first), spanning rolled files. Empty list when
    neither the live file nor any roll exists.

    Rolls are the `history-<timestamp>.jsonl` siblings left behind by `maybe_rotate`. The
    read order is `sorted(rolls) + [live]` — explicit, rather than leaning on the fact that
    `history-*.jsonl` happens to sort before `history.jsonl`. That coincidence is an ASCII
    artifact of `-` (0x2D) vs `.` (0x2E) and would silently reverse the order of two
    differently-named file families; the roll timestamp suffix itself is what makes
    `sorted()` chronological (issue 021).

    A line that fails to parse (e.g. truncated by a power cut or SIGKILL mid-append, since
    `append` is not atomic) is skipped rather than raised — one bad line must not take down
    every future tick. A file that can't be read at all (unreadable permissions, or a roll
    pruned by a concurrent `prune history`) degrades to a warning for the same reason."""
    records: list[HistoryRecord] = []
    for source in (*rolled_history_paths(path), path):
        records.extend(_read_file(source))
    return records


def rolled_history_paths(path: Path) -> list[Path]:
    """The rolled history siblings of *path*, oldest first, by name. Empty when nothing
    has been rolled yet. The suffix shape is derived from the live file's own name, so a
    non-default live path (`custom.jsonl` -> `custom-*.jsonl`) rolls consistently too."""
    return sorted(path.parent.glob(f"{path.stem}-*{path.suffix}"))


def rolled_path(path: Path, now: datetime) -> Path:
    """Where the roll for *path* taken at *now* goes. The same `<YYYYMMDDTHHMMSSZ>` suffix
    `runner.make_run_id` uses, so roll names sort strictly chronologically and match repo
    convention. Deliberately NOT `<YYYYMM>`: two size-triggered rolls in one month would
    need a disambiguator, and the obvious one breaks lexicographic ordering (`-` < `.`),
    which would feed a wrong `first_seen_at` — and therefore a wrong `job_registered_at` —
    into `schedule.decide` (issue 021)."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return path.parent / f"{path.stem}-{stamp}{path.suffix}"


def maybe_rotate(
    path: Path,
    *,
    max_bytes: int | None = None,
    max_age_days: int | None = None,
    now: datetime,
) -> Path | None:
    """Roll *path* aside if it is over either threshold; returns the roll's path, or None if
    it did not rotate. No-op (returning None) when both thresholds are unset, which is the
    default: the mechanism exists, but nothing rolls until an operator asks for it.

    The trigger is size OR age, either one sufficing:
      - size: the live file's `st_size` exceeds *max_bytes*.
      - age:  the live file's oldest record is older than *max_age_days*. The record's own
        `ts` is preferred over mtime — a file restored from backup or copied in carries a
        misleading mtime, and the records' timestamps are what the age policy is about.
        mtime is the fallback for a live file with no parseable records.

    Rotation is `os.replace` (atomic rename, so a concurrent reader sees either the whole
    old file or the whole new one, never a truncated one) followed by re-creating an empty
    live file. The window where the live file doesn't exist self-heals on the next `append`,
    which creates parents and opens in "a" mode.

    A same-second collision (two rotations in the same second) skips the rotation and warns
    rather than inventing a suffix, which would reintroduce the ordering bug `rolled_path`'s
    docstring rules out. Callers hold the exclusive `tick.lock` (see tick.run_tick), which
    makes this unreachable in practice; the guard is there so the failure mode is a
    warning, not corrupted history. `Retention`-independent on purpose — the thresholds are
    passed in, so this module stays free of any config import.
    """
    if max_bytes is None and max_age_days is None:
        return None
    if not path.exists():
        return None
    if not _over_rotation_threshold(
        path, max_bytes=max_bytes, max_age_days=max_age_days, now=now
    ):
        return None

    target = rolled_path(path, now)
    if target.exists():
        log.warning(
            "history rotation skipped: %s already exists (leaving %s in place)",
            target,
            path,
        )
        return None

    os.replace(path, target)
    path.touch()
    log.info("history rotated: %s -> %s", path, target)
    return target


def _over_rotation_threshold(
    path: Path, *, max_bytes: int | None, max_age_days: int | None, now: datetime
) -> bool:
    if max_bytes is not None and path.stat().st_size > max_bytes:
        return True
    if max_age_days is None:
        return False
    records = _read_file(path)
    baseline = (
        records[0].ts if records else datetime.fromtimestamp(path.stat().st_mtime, UTC)
    )
    return now - baseline > timedelta(days=max_age_days)


def _read_file(path: Path) -> list[HistoryRecord]:
    """Records from one history file, in file order. Missing file -> empty list; an
    unreadable file -> a warning and an empty list (fail open, as with a bad line)."""
    if not path.exists():
        return []
    records = []
    try:
        with path.open() as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(HistoryRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError):
                    log.warning("skipping unparseable history line %s:%d", path, lineno)
    except OSError as e:
        log.warning("skipping unreadable history file %s: %s", path, e)
        return []
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
