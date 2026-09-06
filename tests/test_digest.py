"""Tests for the daily digest (issue 010): must read existing history.jsonl + reports dir
only, must show a never-run job rather than omitting it, and must show "no jobs" rather
than crashing on an empty config."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_routines.cli import main
from herdr_routines.config import load_config
from herdr_routines.digest import build_digest, digest_now, render_digest
from herdr_routines.history import HistoryRecord, append


def write_config(tmp_path: Path, job_names: list[str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    config_path = tmp_path / "jobs.yaml"
    lines = ["version: 1", "jobs:"]
    for name in job_names:
        lines += [
            f"  - name: {name}",
            "    cron: '0 3 * * *'",
            f"    repo: {repo}",
        ]
    config_path.write_text("\n".join(lines) + "\n")
    return config_path


def test_never_run_job_shows_as_never_run(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"  # never created
    reports_dir = tmp_path / "reports"

    rows = build_digest(config, history_path, reports_dir)

    assert len(rows) == 1
    row = rows[0]
    assert row.name == "nightly"
    assert row.state is None
    assert row.ts is None
    assert row.report_path is None

    text = render_digest(rows, timezone="UTC", now=datetime(2026, 9, 6, tzinfo=UTC))
    assert "nightly: never run" in text


def test_empty_config_renders_no_jobs(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, []))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"

    rows = build_digest(config, history_path, reports_dir)

    assert rows == []
    text = render_digest(rows, timezone="UTC", now=datetime(2026, 9, 6, tzinfo=UTC))
    assert "no jobs configured" in text


def test_terminal_run_shows_state_and_report_link(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    run_id = "nightly-20260905T030000Z"
    ts = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    append(history_path, HistoryRecord(ts=ts, job="nightly", state="registered"))
    append(
        history_path,
        HistoryRecord(ts=ts, job="nightly", state="running", run_id=run_id),
    )
    report_path = reports_dir / f"{run_id}.md"
    report_path.write_text("# Nightly report\n\nAll good.\n")
    done_ts = datetime(2026, 9, 5, 3, 5, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=done_ts, job="nightly", state="done", run_id=run_id),
    )

    rows = build_digest(config, history_path, reports_dir)

    assert len(rows) == 1
    row = rows[0]
    assert row.name == "nightly"
    assert row.state == "done"
    assert row.ts == done_ts
    assert row.report_path == report_path

    text = render_digest(rows, timezone="UTC", now=datetime(2026, 9, 6, tzinfo=UTC))
    assert "nightly: done at 2026-09-05 03:05 UTC" in text
    assert str(report_path) in text


def test_terminal_run_with_missing_report_omits_link(tmp_path: Path) -> None:
    """A history record can outlive its report file (e.g. reports dir cleaned up
    separately) — the digest must not claim a link that 404s."""
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"  # deliberately never created

    run_id = "nightly-20260905T030000Z"
    ts = datetime(2026, 9, 5, 3, 5, tzinfo=UTC)
    append(
        history_path, HistoryRecord(ts=ts, job="nightly", state="done", run_id=run_id)
    )

    rows = build_digest(config, history_path, reports_dir)

    assert rows[0].state == "done"
    assert rows[0].report_path is None


def test_digest_now_wraps_build_and_render(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, ["nightly", "weekly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"

    text = digest_now(
        config,
        history_path,
        reports_dir,
        timezone="UTC",
        now=datetime(2026, 9, 6, 8, 0, tzinfo=UTC),
    )

    assert "herdr-routines digest — 2026-09-06 08:00 UTC" in text
    assert "nightly: never run" in text
    assert "weekly: never run" in text


def test_cli_digest_prints_without_notifying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercises the actual `herdr-routines digest` entry point (main -> _cmd_digest),
    not just the module functions — and confirms omitting --notify never constructs a
    HerdrClient (would fail if it tried: no Herdr server in this test)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))

    config_path = write_config(tmp_path, ["nightly"])

    rc = main(["--config", str(config_path), "digest", "--timezone", "UTC"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "nightly: never run" in out
