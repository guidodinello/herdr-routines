"""Tier-1/2 tests for `herdr-routines scheduled` (spec 20260825T070012Z): the table must
show every jobs.yaml job with its next-fire time (reusing schedule.py's croniter + ZoneInfo
enumeration) and must surface disabled jobs marked DISABLED rather than omitting them — the
concrete `herdr-pr-review` disable case from the spec."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_routines.config import load_config
from herdr_routines.history import HistoryRecord, append
from herdr_routines.scheduled import build_scheduled_rows, next_fire_at


def make_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point default_history_path() (HERDR_PLUGIN_STATE_DIR) at tmp."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))
    return state_dir


def write_config(tmp_path: Path, *, include_disabled: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    config_path = tmp_path / "jobs.yaml"
    lines = [
        "version: 1",
        "jobs:",
        "  - name: nightly",
        "    cron: '0 3 * * *'",
        f"    repo: {repo}",
    ]
    if include_disabled:
        lines += [
            "  - name: herdr-pr-review",
            "    cron: '30 14 * * *'",
            f"    repo: {repo}",
            "    enabled: false",
        ]
    config_path.write_text("\n".join(lines) + "\n")
    return config_path


def _job_line(out: str, job_name: str) -> str:
    matches = [line for line in out.splitlines() if job_name in line]
    assert len(matches) == 1, f"expected exactly one row for {job_name}: {matches}"
    return matches[0]


# --- criterion 3 ---------------------------------------------------------------------------


def test_status_scheduled_table_shows_next_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The table lists jobs.yaml jobs via load_config() with a next-fire column whose time
    of day comes from the reused schedule.py cron logic (03:00 UTC for cron '0 3 * * *')."""
    from herdr_routines import cli

    make_state_dir(tmp_path, monkeypatch)
    config_path = write_config(tmp_path, include_disabled=False)

    assert cli.main(["--config", str(config_path), "scheduled"]) == 0

    out = capsys.readouterr().out
    assert "NEXT FIRE" in out
    assert "ENABLED" in out  # header column present
    line = _job_line(out, "nightly")
    # Deterministic regardless of when the test runs: the next occurrence of an hourly-
    # anchored daily cron is always 03:00 in the job's timezone.
    assert "03:00 UTC" in line
    assert "DISABLED" not in out


def test_next_fire_at_matches_schedule_enumeration() -> None:
    """next_fire_at reuses schedule._occurrences_since: same timezone handling as tick's
    decide() — '0 3 * * *' in America/Montevideo (UTC-3) is 06:00 UTC, never 03:00."""
    now = datetime(2026, 8, 25, 7, 0, 0, tzinfo=UTC)
    nxt = next_fire_at("0 3 * * *", "America/Montevideo", now=now)
    assert nxt is not None
    assert nxt.utcoffset().total_seconds() == -3 * 3600
    assert (nxt.hour, nxt.minute) == (3, 0)


# --- criterion 4 ---------------------------------------------------------------------------


def test_status_scheduled_table_shows_disabled_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A job with enabled: false (e.g. the disabled herdr-pr-review) must appear in the
    table marked DISABLED on its own row — with its next fire still computed — never
    silently omitted."""
    from herdr_routines import cli

    make_state_dir(tmp_path, monkeypatch)
    config_path = write_config(tmp_path, include_disabled=True)

    assert cli.main(["--config", str(config_path), "scheduled"]) == 0

    out = capsys.readouterr().out
    enabled_line = _job_line(out, "nightly")
    disabled_line = _job_line(out, "herdr-pr-review")
    assert "DISABLED" in disabled_line
    assert "DISABLED" not in enabled_line
    # Disabled jobs keep their would-be schedule visible but marked inactive.
    assert "14:30 UTC" in disabled_line


def test_disabled_job_still_gets_next_fire_in_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit-level: build_scheduled_rows computes next_fire for disabled jobs too."""
    state_dir = make_state_dir(tmp_path, monkeypatch)
    config_path = write_config(tmp_path, include_disabled=True)
    config = load_config(config_path)
    now = datetime(2026, 8, 25, 15, 0, 0, tzinfo=UTC)
    rows = {
        r.name: r
        for r in build_scheduled_rows(config, state_dir / "history.jsonl", now=now)
    }
    assert set(rows) == {"nightly", "herdr-pr-review"}
    assert rows["nightly"].enabled is True
    assert rows["herdr-pr-review"].enabled is False
    assert rows["herdr-pr-review"].next_fire is not None
    assert rows["herdr-pr-review"].last_state is None


def test_last_run_column_uses_history_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAST RUN reuses history.last_terminal_run; non-terminal records don't count."""
    state_dir = make_state_dir(tmp_path, monkeypatch)
    config_path = write_config(tmp_path, include_disabled=False)
    history_file = state_dir / "history.jsonl"
    append(
        history_file,
        HistoryRecord(
            ts=datetime(2026, 8, 24, 3, 0, tzinfo=UTC), job="nightly", state="running"
        ),
    )
    append(
        history_file,
        HistoryRecord(
            ts=datetime(2026, 8, 24, 3, 5, tzinfo=UTC), job="nightly", state="done"
        ),
    )

    config = load_config(config_path)
    rows = build_scheduled_rows(config, history_file, now=datetime.now(UTC))
    assert rows[0].last_state == "done"
