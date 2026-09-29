from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_routines.auto_fix import attempt_count_for_pr
from herdr_routines.history import (
    HistoryRecord,
    append,
    find_stale_running,
    first_seen_at,
    has_ever_been_seen,
    is_currently_running,
    last_terminal_run,
    maybe_rotate,
    read_all,
    read_job,
    rolled_history_paths,
    rolled_path,
)

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
        HistoryRecord(
            ts=T0, job="auto-fix", state="running", run_id="r1", extra={"pr_number": 10}
        ),
    )
    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0, job="auto-fix", state="registered", extra={"pr_number": 10}
        ),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 0


def test_attempt_count_for_pr_ignores_other_pr(tmp_history_path: Path) -> None:
    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0, job="auto-fix", state="done", run_id="r1", extra={"pr_number": 10}
        ),
    )
    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0, job="auto-fix", state="done", run_id="r2", extra={"pr_number": 20}
        ),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 1
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 20) == 1


def test_attempt_count_for_pr_ignores_other_job(tmp_history_path: Path) -> None:
    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0, job="other-job", state="done", run_id="r1", extra={"pr_number": 10}
        ),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 0


def test_attempt_count_for_pr_ignores_records_without_extra(
    tmp_history_path: Path,
) -> None:
    append(
        tmp_history_path,
        HistoryRecord(ts=T0, job="auto-fix", state="done", run_id="r1"),
    )
    assert attempt_count_for_pr(tmp_history_path, "auto-fix", 10) == 0


# -- rotation (issue 021) ------------------------------------------------------


def test_read_all_spans_rolled_files_in_chronological_order(
    tmp_history_path: Path,
) -> None:
    """Acceptance 1: the live file's records come last, after every roll, oldest first."""
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="registered"))
    first_roll = rolled_path(tmp_history_path, T0)
    os.replace(tmp_history_path, first_roll)

    append(
        tmp_history_path,
        HistoryRecord(ts=T0 + timedelta(hours=1), job="a", state="done", run_id="a-1"),
    )
    second_roll = rolled_path(tmp_history_path, T0 + timedelta(hours=1))
    os.replace(tmp_history_path, second_roll)

    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0 + timedelta(hours=2), job="a", state="running", run_id="a-2"
        ),
    )

    assert [p.name for p in rolled_history_paths(tmp_history_path)] == sorted(
        [first_roll.name, second_roll.name]
    )
    records = read_all(tmp_history_path)
    assert [r.state for r in records] == ["registered", "done", "running"]
    assert [r.ts for r in records] == sorted(r.ts for r in records)
    # The live file's record is the last one, not merely one of them.
    assert records[-1].run_id == "a-2"


def test_three_rolls_in_one_month_read_in_write_order(tmp_history_path: Path) -> None:
    """Acceptance 2: the naming trap `history-YYYYMM.jsonl` sets — two size-triggered rolls
    in one month collide, and the obvious disambiguator sorts *before* the plain name
    ('-' 0x2D < '.' 0x2E), so a lexicographic read returns that month backwards and
    corrupts `first_seen_at`. Three rolls on three days of one month is the case that
    proves the timestamped name sorts in write order instead."""
    stamps = [
        datetime(2026, 9, 3, 2, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 9, 2, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 27, 2, 0, 0, tzinfo=UTC),
    ]
    rolls = []
    for i, ts in enumerate(stamps):
        append(
            tmp_history_path,
            HistoryRecord(ts=ts, job="a", state="done", run_id=f"a-{i}"),
        )
        roll = rolled_path(tmp_history_path, ts)
        os.replace(tmp_history_path, roll)
        rolls.append(roll)

    assert not tmp_history_path.exists()
    assert all("-202609" in p.name for p in rolls)
    assert [p.name for p in rolled_history_paths(tmp_history_path)] == [
        p.name for p in sorted(rolls)
    ]
    assert [r.run_id for r in read_all(tmp_history_path)] == ["a-0", "a-1", "a-2"]


def test_history_queries_span_rolled_files(tmp_path: Path) -> None:
    """Acceptance 3: every read-back query answers identically whether a record sits in
    the live file or in a roll. `read_all` is the only seam that knows rolls exist, so all
    of `read_job`/`last_terminal_run`/`has_ever_been_seen`/`first_seen_at`/
    `find_stale_running`/`is_currently_running` inherit that for free — this is the
    regression that would catch a future caller reading the live file directly."""

    def build(path: Path, *, roll_first_three: bool) -> Path:
        records = [
            HistoryRecord(ts=T0, job="a", state="registered"),
            HistoryRecord(
                ts=T0 + timedelta(minutes=10), job="a", state="running", run_id="a-1"
            ),
            HistoryRecord(
                ts=T0 + timedelta(minutes=20), job="a", state="done", run_id="a-1"
            ),
            HistoryRecord(
                ts=T0 + timedelta(minutes=30), job="a", state="running", run_id="a-2"
            ),
        ]
        for i, record in enumerate(records):
            append(path, record)
            if roll_first_three and i == 2:
                assert maybe_rotate(path, max_bytes=1, now=record.ts) is not None
        return path

    all_live = build(tmp_path / "all-live" / "history.jsonl", roll_first_three=False)
    across_roll = build(tmp_path / "rolled" / "history.jsonl", roll_first_three=True)

    assert len(rolled_history_paths(across_roll)) == 1
    now = T0 + timedelta(hours=1)
    for path in (all_live, across_roll):
        last = last_terminal_run(path, "a")
        assert last is not None
        assert last.run_id == "a-1"
        assert first_seen_at(path, "a") == T0
        assert has_ever_been_seen(path, "a") is True
        assert has_ever_been_seen(path, "never-seen") is False
        stale = find_stale_running(path, "a", timeout_ms=1_000, now=now)
        assert stale is not None
        assert stale.run_id == "a-2"
        assert is_currently_running(path, "a", timeout_ms=1_000, now=now) is False


def test_maybe_rotate_preserves_all_records(tmp_history_path: Path) -> None:
    """Acceptance 4: a roll loses nothing and duplicates nothing, and the live file is
    present, empty, and immediately appendable afterwards."""
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="registered"))
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="done", run_id="a-1"))
    before = read_all(tmp_history_path)

    rolled = maybe_rotate(tmp_history_path, max_bytes=1, now=T0 + timedelta(hours=1))
    assert rolled is not None
    assert rolled.exists()
    assert tmp_history_path.exists()
    assert tmp_history_path.read_text() == ""
    assert read_all(tmp_history_path) == before

    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0 + timedelta(hours=2), job="a", state="running", run_id="a-2"
        ),
    )
    after = read_all(tmp_history_path)
    assert after == [*before, after[-1]]
    assert len(after) == 3
    assert [r.run_id for r in after] == [None, "a-1", "a-2"]


def test_maybe_rotate_triggers_on_size_and_age(tmp_history_path: Path) -> None:
    """Acceptance 5: either threshold alone triggers a roll; neither one fires when the
    file is under both."""
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="registered"))
    size = tmp_history_path.stat().st_size
    now = T0 + timedelta(hours=1)

    # Under the size threshold, and one hour old against a 30-day age window: no roll.
    assert (
        maybe_rotate(tmp_history_path, max_bytes=size + 1_000, max_age_days=30, now=now)
        is None
    )
    assert not rolled_history_paths(tmp_history_path)

    # Over the size threshold alone.
    assert maybe_rotate(tmp_history_path, max_bytes=size - 1, now=now) is not None
    assert len(rolled_history_paths(tmp_history_path)) == 1

    # Over the age threshold alone, at a size nobody would roll for.
    append(
        tmp_history_path,
        HistoryRecord(ts=now, job="a", state="done", run_id="a-1"),
    )
    assert maybe_rotate(tmp_history_path, max_age_days=1, now=now) is None
    later = now + timedelta(days=2)
    assert maybe_rotate(tmp_history_path, max_age_days=1, now=later) is not None
    assert len(rolled_history_paths(tmp_history_path)) == 2

    # No parseable records: age falls back to mtime, not to a crash.
    tmp_history_path.write_text("not json at all\n")
    mtime = now + timedelta(days=400)
    os.utime(tmp_history_path, (mtime.timestamp(), mtime.timestamp()))
    assert (
        maybe_rotate(tmp_history_path, max_age_days=1, now=mtime + timedelta(days=2))
        is not None
    )


def test_maybe_rotate_is_off_by_default(tmp_history_path: Path) -> None:
    """Acceptance 6: with both thresholds unset — the default — nothing rolls, however big
    or however old the file gets."""
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="registered"))
    tmp_history_path.write_text(tmp_history_path.read_text() * 500)

    ancient = T0 + timedelta(days=3650)
    os.utime(tmp_history_path, (ancient.timestamp(), ancient.timestamp()))

    assert maybe_rotate(tmp_history_path, now=ancient) is None
    assert not rolled_history_paths(tmp_history_path)
    assert len(read_all(tmp_history_path)) == 500

    # A live file that doesn't exist yet is not an error either.
    assert (
        maybe_rotate(tmp_history_path / "absent.jsonl", max_bytes=1, now=ancient)
        is None
    )


def test_maybe_rotate_skips_on_collision(
    tmp_history_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Acceptance 8: a same-second collision skips the rotation and warns. Inventing a
    suffix instead would reintroduce exactly the out-of-order-read bug the timestamped
    name exists to prevent."""
    append(tmp_history_path, HistoryRecord(ts=T0, job="a", state="registered"))
    now = T0 + timedelta(hours=1)
    squatter = rolled_path(tmp_history_path, now)
    squatter.write_text('{"ts": "2026-01-01T00:00:00Z", "job": "z", "state": "done"}\n')

    with caplog.at_level("WARNING"):
        assert maybe_rotate(tmp_history_path, max_bytes=1, now=now) is None

    assert "already exists" in caplog.text
    # Live file untouched: its record is still there, last, because the squatter roll
    # sorts ahead of it. The pre-existing roll is left exactly as it was.
    assert [r.job for r in read_all(tmp_history_path)] == ["z", "a"]
    assert squatter.read_text().endswith('"state": "done"}\n')
