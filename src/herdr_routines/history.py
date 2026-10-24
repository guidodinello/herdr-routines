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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypedDict, TypeIs, get_args

from logger import get_logger

log = get_logger(__name__)

TerminalState = Literal["done", "failed", "skipped", "missed", "interrupted_unknown"]
NonTerminalState = Literal["registered", "scheduled", "running"]
HistoryState = Literal[TerminalState, NonTerminalState]
TERMINAL_STATES: frozenset[TerminalState] = frozenset(get_args(TerminalState))
NON_TERMINAL_STATES: frozenset[NonTerminalState] = frozenset(get_args(NonTerminalState))
ALL_STATES: frozenset[HistoryState] = TERMINAL_STATES | NON_TERMINAL_STATES

# `extra.trigger` on a record written by `herdr-routines run` (issue 074). A scheduled
# record carries no `trigger` key at all, so a missing key means "the cron fired this".
TRIGGER_MANUAL = "manual"


def is_history_state(value: object) -> TypeIs[HistoryState]:
    return value in ALL_STATES


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


class VerifyRecord(TypedDict):
    """One post-run verify gate's result as recorded under `extra.verify` (issue 071)."""

    gate: str
    passed: bool
    reason: str | None


class HistoryExtra(TypedDict, total=False):
    """Every key a writer puts in a record's `extra`, merged into the JSONL line after
    the core fields. Write-side schema only: a record read back from disk carries
    whatever its file holds (older files have keys no writer emits any more), so readers
    narrow what they read by key instead of trusting this shape.

    Statuses are plain `str` here so this module stays free of herdr/config imports.
    """

    # why/where
    reason: str
    error: str | None
    gate: str
    target: str
    check: str
    phase: str
    trigger: str
    # schedule guard
    scheduled_for: str
    late_seconds: float | None
    occurrence: str
    skipped_occurrences: int
    skipped_first: str | None
    skipped_last: str | None
    reaped_agents: list[str]
    # agent run
    agent: str | None
    agent_name: str
    fix_worker_agent: str
    pane_id: str | None
    branch: str | None
    gate_branch: str
    final_agent_status: str | None
    session_id: str | None
    duration_seconds: float | None
    nudged: bool
    resigned_commits: int
    reaped_stale_agent: bool
    verify: list[VerifyRecord]
    # reports and artifacts
    report: str | None
    report_path: str | None
    report_written: bool
    report_bytes: int
    fix_report_path: str | None
    fix_report_written: bool
    manifest_path: str
    ledger_path: str
    audit_output_path: str
    gate_output_path: str | None
    failed_checks: str
    exit_code: int
    rc: int
    # retries
    attempt: int
    max_retries: int
    final_attempt: bool
    primary_run_id: str
    retried_from_run_id: str
    retry_eligible_reason: str | None
    # pipeline
    pipeline_run_id: str
    unit: str
    deadline_epoch: int
    # audit findings
    findings_total: int
    new: int
    regressed: int
    unchanged: int
    resolved: int
    adopted: int
    suppressed: int
    duplicate_ids: list[str]
    dispatched_ids: list[str]
    suppressed_ids: list[str]
    # github event / pr / review gates
    query: str | None
    polled: int
    event_id: str
    event_number: int
    last_seen_event_ids: list[str]
    pr_number: int
    headRefName: str
    eligible_reason: str
    enumerated: int
    eligible: int
    candidates: int
    dispatched: int
    skipped: int
    skipped_over_cap: int
    failed: int
    repos_failed: int
    target_key: str
    repo: str
    select_mode: str
    verified: bool | None


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    ts: datetime
    job: str
    state: HistoryState
    run_id: str | None = None
    extra: Mapping[str, object] | None = None

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
    def from_dict(d: Mapping[str, object]) -> HistoryRecord:
        """Raises KeyError for a missing core field and ValueError for a mistyped one;
        `_read_file` skips either as a bad line. Keys beyond the core fields are kept
        as-is in `extra`."""
        ts = d["ts"]
        if not isinstance(ts, str):
            raise ValueError(f"history ts must be a string, got {ts!r}")  # noqa: TRY004 — _read_file skips ValueError lines
        job = d["job"]
        if not isinstance(job, str):
            raise ValueError(f"history job must be a string, got {job!r}")  # noqa: TRY004 — same
        state = d["state"]
        if not is_history_state(state):
            raise ValueError(f"unknown history state {state!r}")
        run_id = d.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            raise ValueError(f"history run_id must be a string, got {run_id!r}")
        known = {"ts", "job", "state", "run_id"}
        extra = {k: v for k, v in d.items() if k not in known} or None
        return HistoryRecord(
            ts=datetime.fromisoformat(ts),
            job=job,
            state=state,
            run_id=run_id,
            extra=extra,
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


def _is_manual(record: HistoryRecord) -> bool:
    return (record.extra or {}).get("trigger") == TRIGGER_MANUAL


def last_scheduled_terminal_run(path: Path, job_name: str) -> HistoryRecord | None:
    """Like `last_terminal_run`, but skipping terminal records of manual runs (issue 074):
    `schedule.decide` feeds on this, so `herdr-routines run` never consumes the next cron
    occurrence or shifts the missed-run window.

    A terminal record is manual when it carries `trigger: manual` itself, or when the latest
    `running` record before it with the same run_id does. The second clause covers the
    terminals the gated / review / audit cycles write without knowing the trigger, and the
    `interrupted_unknown` / pipeline-reconcile records, which reuse the run's run_id. "Latest
    before it" keeps `run --run-id <scheduled run_id>` from un-counting that run's original
    scheduled terminal record."""
    manual_run_ids: set[str] = set()
    scheduled: HistoryRecord | None = None
    for record in read_job(path, job_name):
        if record.state == "running" and record.run_id:
            if _is_manual(record):
                manual_run_ids.add(record.run_id)
            else:
                manual_run_ids.discard(record.run_id)
        elif record.state in TERMINAL_STATES:
            manual = _is_manual(record) or (
                record.run_id is not None and record.run_id in manual_run_ids
            )
            if not manual:
                scheduled = record
    return scheduled


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
