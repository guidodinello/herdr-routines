from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from herdr_routines.history import (
    HistoryRecord,
    append,
    find_stale_running,
    has_ever_been_seen,
    is_currently_running,
    last_terminal_run,
    read_all,
    read_job,
)
from herdr_routines.auto_fix import attempt_count_for_pr

T0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=UTC)


def test_append_and_read_round_trip(tmp_history_path: Path) -> None:
    rec = HistoryRecord(
        ts=T0, job="a", state="running", run_id="a-1", extra={"pane_id": "w1:p1"}
    )
    append(tmp_history_path, rec)
    records = read_all(tmp_history_path)
    assert len(records) == 1
    got = records[0]
    assert got.job == "a"
    assert got.state == "running"
    assert got.run_id == "a-1"
    assert got.extra == {"pane_id": "w1:p1"}
    assert got.ts == T0


def test_read_all_on_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_all(tmp_path / "nope.jsonl") == []


def test_read_job_filters_by_job_name(tmp_history_path: Path) -> None:
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="done", run_id="a-1"))
    append(tmp_history_path, HistoryRecord(ts=T0, job="b", state="done", run_id="b-1"))
    append(
        tmp_history_path, HistoryRecord(ts=T0, job="a", state="failed", run_id="a-2")
    )
    a_records = read_job(tmp_history_path, "a")
    assert [r.run_id for r in a_records] == ["a-1", "a-2"]


def test_read_job_limit_keeps_most_recent(tmp_history_path: Path) -> None:
    for i in range(5):
        append(
            tmp_history_path,
            HistoryRecord(ts=T0, job="a", state="done", run_id=f"a-{i}"),
        )
    limited = read_job(tmp_history_path, "a", limit=2)
    assert [r.run_id for r in limited] == ["a-3", "a-4"]


def test_last_terminal_run_ignores_non_terminal_states(tmp_history_path: Path) -> None:
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="done", run_id="a-1"))
    later = T0 + timedelta(hours=1)
    append(
        tmp_history_path,
        HistoryRecord(ts=later, job="a", state="running", run_id="a-2"),
    )
    result = last_terminal_run(tmp_history_path, "a")
    assert result is not None
    assert result.run_id == "a-1"


def test_last_terminal_run_none_for_never_run_job(tmp_history_path: Path) -> None:
    assert last_terminal_run(tmp_history_path, "never-run") is None


def test_last_terminal_run_none_when_only_registered(tmp_history_path: Path) -> None:
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="registered"))
    assert last_terminal_run(tmp_history_path, "a") is None


def test_has_ever_been_seen(tmp_history_path: Path) -> None:
    assert has_ever_been_seen(tmp_history_path, "a") is False
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="registered"))
    assert has_ever_been_seen(tmp_history_path, "a") is True


def test_find_stale_running_none_when_within_timeout(tmp_history_path: Path) -> None:
    append(
        tmp_history_path, HistoryRecord(ts=T0, job="a", state="running", run_id="a-1")
    )
    now = T0 + timedelta(minutes=10)
    assert (
        find_stale_running(tmp_history_path, "a", timeout_ms=1_800_000, now=now) is None
    )


def test_find_stale_running_detects_orphaned_run(tmp_history_path: Path) -> None:
    append(
        tmp_history_path, HistoryRecord(ts=T0, job="a", state="running", run_id="a-1")
    )
    # timeout_ms=60_000 (1 min) + 5 min margin = 6 min deadline; 10 min later is past it.
    now = T0 + timedelta(minutes=10)
    stale = find_stale_running(tmp_history_path, "a", timeout_ms=60_000, now=now)
    assert stale is not None
    assert stale.run_id == "a-1"


def test_find_stale_running_none_once_terminal_record_exists(
    tmp_history_path: Path,
) -> None:
    append(
        tmp_history_path, HistoryRecord(ts=T0, job="a", state="running", run_id="a-1")
    )
    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0 + timedelta(minutes=1), job="a", state="done", run_id="a-1"
        ),
    )
    now = T0 + timedelta(hours=5)
    assert find_stale_running(tmp_history_path, "a", timeout_ms=1000, now=now) is None


def test_is_currently_running_true_for_active_run(tmp_history_path: Path) -> None:
    append(
        tmp_history_path, HistoryRecord(ts=T0, job="a", state="running", run_id="a-1")
    )
    now = T0 + timedelta(minutes=5)
    assert (
        is_currently_running(tmp_history_path, "a", timeout_ms=1_800_000, now=now)
        is True
    )


def test_is_currently_running_false_once_stale(tmp_history_path: Path) -> None:
    append(
        tmp_history_path, HistoryRecord(ts=T0, job="a", state="running", run_id="a-1")
    )
    now = T0 + timedelta(minutes=10)
    assert (
        is_currently_running(tmp_history_path, "a", timeout_ms=60_000, now=now) is False
    )


def test_is_currently_running_false_after_terminal_record(
    tmp_history_path: Path,
) -> None:
    append(
        tmp_history_path, HistoryRecord(ts=T0, job="a", state="running", run_id="a-1")
    )
    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0 + timedelta(seconds=30), job="a", state="done", run_id="a-1"
        ),
    )
    now = T0 + timedelta(minutes=1)
    assert (
        is_currently_running(tmp_history_path, "a", timeout_ms=1_800_000, now=now)
        is False
    )


def test_to_json_line_uses_utc_z_suffix() -> None:
    rec = HistoryRecord(ts=T0, job="a", state="done", run_id="a-1")
    line = rec.to_json_line()
    assert '"ts": "2026-08-22T06:00:00Z"' in line


# -- attempt_count_for_pr ---------------------------------------------------


def test_attempt_count_for_pr_zero_for_no_records(tmp_history_path: Path) -> None:
    assert attempt_count_for_pr(tmp_history_path, "job", 10) == 0


def test_attempt_count_for_pr_counts_terminal_records(tmp_history_path: Path) -> None:
    for i in range(3):
        append(
            tmp_history_path,
            HistoryRecord(
                ts=T0 + timedelta(minutes=i),
                job="auto-fix",
                state="done",
                run_id=f"run-{i}",
                extra={"pr_number": 10},
            ),
        )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 3


def test_attempt_count_for_pr_ignores_non_terminal(tmp_history_path: Path) -> None:
    append(
        tmp_history_path,
        HistoryRecord(ts=T0, job="auto-fix", state="running", run_id="r1", extra={"pr_number": 10}),
    )
    append(
        tmp_history_path,
        HistoryRecord(ts=T0, job="auto-fix", state="registered", extra={"pr_number": 10}),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 0


def test_attempt_count_for_pr_ignores_other_pr(tmp_history_path: Path) -> None:
    append(
        tmp_history_path,
        HistoryRecord(ts=T0, job="auto-fix", state="done", run_id="r1", extra={"pr_number": 10}),
    )
    append(
        tmp_history_path,
        HistoryRecord(ts=T0, job="auto-fix", state="done", run_id="r2", extra={"pr_number": 20}),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 1
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 20) == 1


def test_attempt_count_for_pr_ignores_other_job(tmp_history_path: Path) -> None:
    append(
        tmp_history_path,
        HistoryRecord(ts=T0, job="other-job", state="done", run_id="r1", extra={"pr_number": 10}),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 0


def test_attempt_count_for_pr_ignores_records_without_extra(tmp_history_path: Path) -> None:
    append(
        tmp_history_path,
        HistoryRecord(ts=T0, job="auto-fix", state="done", run_id="r1"),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 0
