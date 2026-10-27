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
from herdr_routines.history import HistoryRecord, HistoryState, append


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


# --- issue 072: missed-run alerts in the digest ---

# The feature is deliberately not implemented yet: `missed_runs` / `MissedRow` are
# imported *inside* each test below so that these tests fail for the right reason
# (AttributeError on the missing function) while the pre-existing tests in this
# file stay green and untouched.

SECTION_HEADER = "missed runs (should have run, didn't):"


def _missed_runs(config, history_path, *, now):
    from herdr_routines.digest import missed_runs

    return missed_runs(config, history_path, now=now)


def _cron_row(
    history_path: Path, job: str, ts: datetime, state: HistoryState, trigger: str | None
) -> None:
    extra = {"trigger": trigger} if trigger is not None else None
    append(
        history_path,
        HistoryRecord(
            ts=ts,
            job=job,
            state=state,
            run_id=f"{job}-{ts:%Y%m%dT%H%M}",
            extra=extra,
        ),
    )


def test_missed_run_renders_in_its_own_section(tmp_path: Path) -> None:
    """Acceptance 1: an enabled job whose last scheduled terminal run is stale beyond
    grace appears in a separate 'missed runs' section carrying the last terminal
    state, when, the expected occurrence and the overdue amount."""
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"

    # Last scheduled terminal run: done at the 2026-09-01 03:00 occurrence.
    _cron_row(
        history_path, "nightly", datetime(2026, 9, 1, 3, 5, tzinfo=UTC), "done", "cron"
    )

    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)  # five 03:00 occurrences missed
    missed = _missed_runs(config, history_path, now=now)

    assert len(missed) == 1
    row = missed[0]
    assert row.name == "nightly"
    assert row.expected == datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    assert row.last_state == "done"
    assert row.last_ts == datetime(2026, 9, 1, 3, 5, tzinfo=UTC)
    assert row.late == now - datetime(2026, 9, 6, 3, 0, tzinfo=UTC)

    text = digest_now(config, history_path, reports_dir, timezone="UTC", now=now)
    assert SECTION_HEADER in text
    alert = text.split(SECTION_HEADER)[1]
    assert "nightly: last terminal done at 2026-09-01 03:05" in alert
    assert "expected by 2026-09-06 03:00" in alert
    assert "overdue" in alert


def test_disabled_jobs_are_never_missed(tmp_path: Path) -> None:
    """Acceptance 2: a disabled job with a stale terminal state produces no missed-run
    alert regardless of --include-disabled (which only affects the per-job rows)."""
    config = load_config(write_config(tmp_path, ["stale"], enabled={"stale": False}))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    _cron_row(
        history_path,
        "stale",
        datetime(2026, 9, 1, 3, 5, tzinfo=UTC),
        "done",
        "cron",
    )

    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    assert _missed_runs(config, history_path, now=now) == []

    text = digest_now(
        config,
        history_path,
        reports_dir,
        timezone="UTC",
        now=now,
        include_disabled=True,
    )
    assert SECTION_HEADER not in text


def test_missed_job_line_is_unchecked_and_additive(tmp_path: Path) -> None:
    """Acceptance 3: the per-job line for a missed job is unchanged (still its terminal
    state, not rewritten as failed, not moved into any failure grouping); the missed
    alert is additive and visually separate."""
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    _cron_row(
        history_path, "nightly", datetime(2026, 9, 1, 3, 5, tzinfo=UTC), "done", "cron"
    )

    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    text = digest_now(config, history_path, reports_dir, timezone="UTC", now=now)

    per_job = "- nightly: done at 2026-09-01 03:05 UTC"
    assert per_job in text  # unchanged line
    assert SECTION_HEADER in text
    job_lines = [ln for ln in text.splitlines() if ln.startswith("- nightly")]
    assert job_lines.count(per_job) == 1  # exactly one unchanged per-job line
    # nothing before the missed section rewrites or regroups the missed job as failed
    pre_section = text.split(SECTION_HEADER)[0]
    assert "nightly: failed" not in pre_section
    assert "expected by" not in pre_section


def test_recent_terminal_run_within_grace_is_never_missed(tmp_path: Path) -> None:
    """Acceptance 4: detection is read-only; an enabled job with a recent terminal
    state within grace digests exactly as before and writes nothing anywhere."""
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    _cron_row(
        history_path, "nightly", datetime(2026, 9, 6, 7, 30, tzinfo=UTC), "done", "cron"
    )
    (reports_dir / "nightly-20260906T0730.md").write_text("# nightly\n")

    history_before = history_path.read_bytes()
    listing_before = sorted(p.name for p in tmp_path.rglob("*"))

    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    assert _missed_runs(config, history_path, now=now) == []

    rows = build_digest(config, history_path, reports_dir)
    unchanged = render_digest(rows, timezone="UTC", now=now)
    assert "- nightly: done at 2026-09-06 07:30 UTC" in unchanged
    assert SECTION_HEADER not in unchanged

    # no new state store, no records written by detection
    listing_after = sorted(p.name for p in tmp_path.rglob("*"))
    assert listing_after == listing_before
    assert history_path.read_bytes() == history_before


def test_never_run_jobs_are_conservative(tmp_path: Path) -> None:
    """Acceptance 5: never-seen jobs never alert; freshly-seen (registered inside one
    cycle) jobs don't alert; a seen job with one full cycle beyond grace since
    first-seen alerts with the explicit 'never run (expected by ...)' phrasing."""
    history_path = tmp_path / "history.jsonl"

    # (a) unseen job: no history at all -> benign, no alert
    config_unseen = load_config(write_config(tmp_path, ["neverheardof"]))
    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    assert _missed_runs(config_unseen, history_path, now=now) == []

    # (b) freshly seen: registered 07:30, after the day's 03:00 occurrence — no
    #     occurrence exists after first-seen yet, so a mere half hour of age cannot
    #     fire a false alert
    config_fresh = load_config(write_config(tmp_path, ["fresh"]))
    append(
        history_path,
        HistoryRecord(
            ts=datetime(2026, 9, 6, 7, 30, tzinfo=UTC), job="fresh", state="registered"
        ),
    )
    assert _missed_runs(config_fresh, history_path, now=now) == []

    # (c) seen and overdue: registered 2026-09-01 12:00; daily 03:00 occurrences
    #     since; the most recent (2026-09-06 03:00) is 5h stale — beyond grace
    config_overdue = load_config(write_config(tmp_path, ["ghost"]))
    append(
        history_path,
        HistoryRecord(
            ts=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), job="ghost", state="registered"
        ),
    )
    missed = _missed_runs(config_overdue, history_path, now=now)
    assert len(missed) == 1
    assert missed[0].name == "ghost"
    assert missed[0].last_state is None
    assert missed[0].last_ts is None
    assert missed[0].expected == datetime(2026, 9, 6, 3, 0, tzinfo=UTC)

    text = digest_now(
        config_overdue,
        history_path,
        tmp_path / "reports",
        timezone="UTC",
        now=now,
    )
    assert SECTION_HEADER in text
    assert "ghost: never run (expected by 2026-09-06 03:00" in text


def test_off_schedule_runs_do_not_hide_a_missed_cron(tmp_path: Path) -> None:
    """Acceptance 6: manual/event/request runs do not satisfy (or shift) the expected
    window — 'I ran it by hand' must not suppress the missed-cron alert."""
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    _cron_row(
        history_path, "nightly", datetime(2026, 9, 1, 3, 5, tzinfo=UTC), "done", "cron"
    )
    # off-schedule manual run, completed recently — tempting but not a cron run
    _cron_row(
        history_path,
        "nightly",
        datetime(2026, 9, 6, 4, 0, tzinfo=UTC),
        "done",
        "manual",
    )

    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    missed = _missed_runs(config, history_path, now=now)

    assert len(missed) == 1
    assert missed[0].name == "nightly"
    # anchored on the scheduled terminal run, not the manual one
    assert missed[0].last_ts == datetime(2026, 9, 1, 3, 5, tzinfo=UTC)

    text = digest_now(config, history_path, reports_dir, timezone="UTC", now=now)
    assert SECTION_HEADER in text


def test_running_job_is_not_missed(tmp_path: Path) -> None:
    """Acceptance 7: a job with a live run in flight is not flagged missed."""
    config = load_config(write_config(tmp_path, ["nightly"]))
    history_path = tmp_path / "history.jsonl"
    reports_dir = tmp_path / "reports"
    _cron_row(
        history_path, "nightly", datetime(2026, 9, 1, 3, 5, tzinfo=UTC), "done", "cron"
    )
    # a run started minutes ago, no terminal record yet
    _cron_row(
        history_path,
        "nightly",
        datetime(2026, 9, 6, 7, 55, tzinfo=UTC),
        "running",
        "cron",
    )

    now = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)
    assert _missed_runs(config, history_path, now=now) == []
    text = digest_now(config, history_path, reports_dir, timezone="UTC", now=now)
    assert SECTION_HEADER not in text
