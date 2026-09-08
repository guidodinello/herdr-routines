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


def write_config(
    tmp_path: Path, job_names: list[str], *, enabled: dict[str, bool] | None = None
) -> Path:
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
        if enabled and name in enabled and not enabled[name]:
            lines.append("    enabled: false")
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


# --- issue 046: digest omits disabled jobs by default ---


def test_digest_omits_disabled_job(tmp_path: Path) -> None:
    """Default digest omits disabled jobs entirely; order of remaining enabled jobs preserved."""
    config = load_config(
        write_config(
            tmp_path,
            ["alpha", "beta", "gamma"],
            enabled={"beta": False},
        )
    )
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    # Give each job a terminal state
    ts = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    for name in ("alpha", "beta", "gamma"):
        append(
            history_path,
            HistoryRecord(ts=ts, job=name, state="done", run_id=f"{name}-run1"),
        )
        (reports_dir / f"{name}-run1.md").write_text(f"# {name}\n")

    rows = build_digest(config, history_path, reports_dir)
    names = [r.name for r in rows]
    assert names == ["alpha", "gamma"]
    assert all(r.enabled for r in rows)


def test_digest_include_disabled_marks_not_fails(tmp_path: Path) -> None:
    """With include_disabled=True, disabled job appears, contains 'disabled',
    does NOT contain ': failed at' for that job, historical state uses 'last:'."""
    config = load_config(
        write_config(
            tmp_path,
            ["active", "stale"],
            enabled={"stale": False},
        )
    )
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    ts = datetime(2026, 9, 2, 5, 0, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=ts, job="stale", state="failed", run_id="stale-run1"),
    )
    (reports_dir / "stale-run1.md").write_text("# stale report\n")

    active_ts = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=active_ts, job="active", state="done", run_id="active-run1"),
    )
    (reports_dir / "active-run1.md").write_text("# active report\n")

    rows = build_digest(config, history_path, reports_dir, include_disabled=True)
    assert len(rows) == 2
    stale_row = next(r for r in rows if r.name == "stale")
    assert stale_row.enabled is False
    assert stale_row.state == "failed"  # historical state preserved in data

    text = render_digest(rows, timezone="UTC", now=datetime(2026, 9, 6, tzinfo=UTC))
    # disabled line must contain 'disabled' (case-insensitive)
    assert "disabled" in text.lower()
    # must NOT render stale as ': failed at' — that's the failure format
    assert "- stale: failed at" not in text
    # should show historical state as 'last:' secondary
    assert "last: failed" in text


def test_digest_still_reports_enabled_failure(tmp_path: Path) -> None:
    """Enabled job's failure renders verbatim regardless of other disabled jobs."""
    config = load_config(
        write_config(
            tmp_path,
            ["broken", "spare"],
            enabled={"spare": False},
        )
    )
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    ts = datetime(2026, 9, 4, 7, 0, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=ts, job="broken", state="failed", run_id="broken-run1"),
    )
    (reports_dir / "broken-run1.md").write_text("# broken report\n")

    off_ts = datetime(2026, 9, 2, 5, 0, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=off_ts, job="spare", state="failed", run_id="spare-run1"),
    )

    rows = build_digest(config, history_path, reports_dir)
    assert len(rows) == 1
    assert rows[0].name == "broken"
    assert rows[0].enabled is True

    text = render_digest(rows, timezone="UTC", now=datetime(2026, 9, 6, tzinfo=UTC))
    assert "- broken: failed at 2026-09-04 07:00 UTC" in text
    assert "spare" not in text


def test_digest_all_disabled_is_explicit(tmp_path: Path) -> None:
    """All-disabled filtered digest is explicit, not empty or 'no jobs configured'.
    Empty-config case still says 'no jobs configured'."""
    # All jobs disabled, include_disabled=False -> filtered list empty
    config_all_disabled = load_config(
        write_config(
            tmp_path,
            ["a", "b"],
            enabled={"a": False, "b": False},
        )
    )
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"

    text = digest_now(
        config_all_disabled,
        history_path,
        reports_dir,
        timezone="UTC",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert "no jobs configured" not in text
    # Must contain an explicit marker for the all-disabled case
    assert "disabled" in text.lower()

    # Empty-config case still says "no jobs configured"
    text_empty = render_digest([], timezone="UTC", now=datetime(2026, 9, 6, tzinfo=UTC))
    assert "no jobs configured" in text_empty


def test_digest_disabled_never_run_marked_disabled(tmp_path: Path) -> None:
    """A disabled job with state=None (never run) rendered via --include-disabled
    contains 'disabled' and is not bare 'never run'."""
    config = load_config(
        write_config(
            tmp_path,
            ["newjob"],
            enabled={"newjob": False},
        )
    )
    history_path = tmp_path / "history.jsonl"  # never created -> state None
    reports_dir = tmp_path / "reports"

    rows = build_digest(config, history_path, reports_dir, include_disabled=True)
    assert len(rows) == 1
    assert rows[0].state is None
    assert rows[0].enabled is False

    text = render_digest(rows, timezone="UTC", now=datetime(2026, 9, 6, tzinfo=UTC))
    assert "disabled" in text.lower()
    # Must not be rendered as bare 'never run' without 'disabled'
    assert "never run" not in text or "disabled" in text.lower()


def test_digest_cli_include_disabled_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI --include-disabled is accepted and forwards include_disabled=True."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))

    config_path = write_config(
        tmp_path,
        ["active", "stale"],
        enabled={"stale": False},
    )
    history_path = tmp_path / "state" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    reports_dir = tmp_path / "state" / "reports"
    reports_dir.mkdir()

    ts = datetime(2026, 9, 2, 5, 0, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=ts, job="stale", state="failed", run_id="stale-run1"),
    )

    active_ts = datetime(2026, 9, 5, 3, 0, tzinfo=UTC)
    append(
        history_path,
        HistoryRecord(ts=active_ts, job="active", state="done", run_id="active-run1"),
    )

    rc = main(
        [
            "--config",
            str(config_path),
            "digest",
            "--include-disabled",
            "--timezone",
            "UTC",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # stale should appear (it's disabled but --include-disabled shows it)
    assert "stale" in out
    assert "active" in out
    # disabled marker present
    assert "disabled" in out.lower()
