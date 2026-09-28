"""Tests for state-dir retention: `prune reports` / `prune history` (issue 021).

The load-bearing claim of this feature is the asymmetry it encodes: rotation renames and
never deletes, so it can run inside the tick unattended, while deletion is reachable from
exactly one place — an explicit `prune` invocation. Most of what follows is about that
second half: what gets deleted, what must not, and what happens when one entry misbehaves.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from herdr_routines.config import Retention, RoutinesConfig
from herdr_routines.herdr import HerdrClient
from herdr_routines.history import (
    HistoryRecord,
    append,
    maybe_rotate,
    read_job,
    rolled_history_paths,
    rolled_path,
)
from herdr_routines.reports_prune import (
    PruneResult,
    protected_paths,
    prune_reports,
    prune_rolled_history,
)
from herdr_routines.tick import run_tick

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 28, 5, 15, 15, tzinfo=UTC)


def _write(path: Path, text: str = "x", *, age_days: float = 0) -> Path:
    """Write *path* and backdate its mtime by *age_days* — the selection signal `prune`
    actually reads, so tests set it explicitly rather than sleeping."""
    path.write_text(text)
    if age_days:
        stamp = time.time() - age_days * 86_400
        os.utime(path, (stamp, stamp))
    return path


def _plant_pipeline_state(
    reports_dir: Path,
    run_id: str,
    *,
    report_path: str | None = None,
    age_days: float = 0,
) -> Path:
    """A `state.json` of the shape `ps.scan_pipeline_runs` reads. With *report_path* the run
    records where its final report is going to land (and it has not landed yet, so the run
    reads as in progress); without one, ps falls back to the two naming conventions and
    still resolves to a not-yet-written report as long as neither exists."""
    raw: dict[str, Any] = {"run_id": run_id, "current_stage": 3}
    if report_path is not None:
        raw["artifact_paths"] = {"report": report_path}
    return _write(reports_dir / "state.json", json.dumps(raw), age_days=age_days)


# -- prune reports ------------------------------------------------------------


def test_prune_reports_deletes_only_expired(tmp_path: Path) -> None:
    """Acceptance 12: with a 90-day window, an old report/tail/state set goes and a recent
    one stays. Selection is mtime-based at the top level of the reports dir only — the flat
    set the state dir actually accumulates."""
    reports = tmp_path / "reports"
    reports.mkdir()

    old = _write(reports / "a-20260101T000000Z.md", age_days=200)
    old_tail = _write(reports / "a-20260101T000000Z.tail.txt", age_days=200)
    old_auto_fix = _write(reports / "auto-fix-a-20260101T000000Z-pr7.md", age_days=200)
    fresh = _write(reports / "a-20260927T000000Z.md", age_days=1)
    fresh_tail = _write(reports / "a-20260927T000000Z.tail.txt", age_days=1)

    result = prune_reports(reports, older_than_days=90)

    assert isinstance(result, PruneResult)
    assert set(result.removed) == {old, old_tail, old_auto_fix}
    assert set(result.kept_fresh) == {fresh, fresh_tail}
    assert result.errors == ()
    assert not old.exists() and not old_tail.exists() and not old_auto_fix.exists()
    assert fresh.exists() and fresh_tail.exists()

    # A narrower window collects the recent pair too, and a 0-day window is a valid
    # "everything not touched in the last instant" rather than a rejected value.
    result = prune_reports(reports, older_than_days=1)
    assert set(result.removed) == {fresh, fresh_tail}
    assert result.kept_fresh == ()
    assert not reports.joinpath("a-20260927T000000Z.md").exists()

    # A host that has never run anything has no reports dir; that is not an error.
    assert prune_reports(tmp_path / "absent", older_than_days=90) == PruneResult()

    # Dry-run reports the same plan and touches nothing.
    dry = _write(reports / "b-20200101T000000Z.md", age_days=1000)
    planned = prune_reports(reports, older_than_days=90, dry_run=True)
    assert planned.removed == (dry,)
    assert dry.exists()


def test_prune_reports_protects_inflight_pipeline_run(tmp_path: Path) -> None:
    """Acceptance 13: the reports and `state.json` of an in-flight pipeline run survive a
    prune however old they are. Deleting that `state.json` would make `ps` drop the row
    entirely and let `pick_feature` re-pick the same issue; deleting the report would make
    `pipeline_watchdog` read a finished run as stalled again.

    The in-flight predicate is reused from `ps` rather than reinvented: a run is in flight
    exactly while its final report has not been written yet. Run A exercises the awkward
    case where report files do exist but `state.json` points its final report somewhere
    else, so ps still calls the run in flight — its report files are protected too, not just
    its state. Run C is the same shape but *finished* (its report is where the convention
    says it will be), and its files are collectable: protecting those as well is what would
    make the reports dir grow without bound, which is what the command is for.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    reported_elsewhere = str(tmp_path / "not-here-yet" / "pipeline-runA.md")
    state_a = _plant_pipeline_state(
        reports, "runA", report_path=reported_elsewhere, age_days=300
    )
    a_pipeline = _write(reports / "pipeline-runA.md", age_days=300)
    a_plain = _write(reports / "runA.md", age_days=300)
    expired = _write(reports / "a-20200101T000000Z.md", age_days=300)

    result = prune_reports(reports, older_than_days=90)

    assert set(result.protected) == {state_a, a_pipeline, a_plain}
    assert state_a.exists(), "in-flight state.json must survive"
    assert a_pipeline.exists() and a_plain.exists(), "in-flight reports must survive"
    assert set(result.removed) == {expired}
    assert not expired.exists()

    # A run whose report has not been written at all protects its state.json alone.
    inflight = tmp_path / "inflight"
    inflight.mkdir()
    state_d = _plant_pipeline_state(inflight, "runD", age_days=300)
    d_result = prune_reports(inflight, older_than_days=90)
    assert d_result.protected == (state_d,)
    assert d_result.removed == ()
    assert state_d.exists()

    # A finished run's report is collectable, state.json included.
    done = tmp_path / "done"
    done.mkdir()
    state_c = _plant_pipeline_state(done, "runC", age_days=300)
    c_report = _write(done / "pipeline-runC.md", age_days=300)
    c_result = prune_reports(done, older_than_days=90)
    assert c_result.protected == ()
    assert set(c_result.removed) == {state_c, c_report}
    assert not c_report.exists()


def test_protected_paths_covers_ps_scan_dirs(tmp_path: Path) -> None:
    """The protected set covers the same candidates `ps._resolve_report_path` and
    `pipeline_watchdog._candidate_report_paths` consult — beside the state file and beside its
    parent — so a `state.json` dropped beside history.jsonl rather than under reports/ still
    protects its run (ps would still find that run, so prune must spare it too)."""
    state_dir = tmp_path / "state"
    reports = state_dir / "reports"
    reports.mkdir(parents=True)
    strayed = _plant_pipeline_state(state_dir, "runE", age_days=300)

    protected = protected_paths([reports, state_dir])
    assert strayed in protected
    assert state_dir / "pipeline-runE.md" in protected
    assert tmp_path / "pipeline-runE.md" in protected
    assert state_dir / "runE.md" in protected
    # Scanned out of reach, so nothing is known about the run and nothing is protected.
    assert protected_paths([reports]) == set()


def test_prune_reports_survives_per_entry_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Acceptance 15: one entry that cannot be inspected and one that cannot be unlinked are
    each counted and the sweep carries on, so a single locked file cannot turn
    `prune --yes` into a no-op that reports success. The exit code follows the error count
    (see cli._cmd_prune) precisely so this cannot pass silently."""
    reports = tmp_path / "reports"
    reports.mkdir()
    assert prune_reports(reports, older_than_days=0).errors == ()
    first = _write(reports / "a-old.md", age_days=300)
    unstattable = _write(reports / "b-old.md", age_days=300)
    unremovable = _write(reports / "c-old.md", age_days=300)
    last = _write(reports / "d-old.md", age_days=300)

    real_stat = Path.stat
    real_unlink = Path.unlink

    def flaky_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        # Only the sweep's own `entry.stat()` (follow_symlinks defaulting to True) is made to
        # fail; `is_symlink()`'s lstat still works, which is the real-world shape anyway — a
        # file we cannot stat but can still see the name of.
        if self == unstattable and kwargs.get("follow_symlinks", True):
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    def flaky_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == unremovable:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with caplog.at_level("WARNING"):
        result = prune_reports(reports, older_than_days=90)

    assert set(result.removed) == {first, last}
    assert result.error_count == 2, "both an unstattable and an unremovable entry count"
    assert any("b-old.md" in e for e in result.errors)
    assert any("c-old.md" in e for e in result.errors)
    # os.path, not Path: Path.exists() goes through the patched stat and would raise here.
    assert os.path.exists(unstattable) and os.path.exists(unremovable)
    assert "prune:" in caplog.text


# -- nothing deletes itself ---------------------------------------------------


# Every module in src/ that can remove a file or directory. gc deletes git branches and
# worktree dirs by explicit request, repos.py removes a half-cloned temp dir on a failed
# clone, tmp_hygiene reaps /tmp leaks from the tick — all pre-existing. reports_prune.py is
# the new one. Anything else that shows up here is a new delete path that has not been
# reviewed against the "only `prune` deletes" rule.
_DELETE_CALL_RE = re.compile(r"\.(?:unlink|rmdir)\(|\bos\.remove\(|\bshutil\.rmtree\(")
_EXPECTED_DELETERS = {"gc.py", "reports_prune.py", "repos.py", "tmp_hygiene.py"}


def test_no_automatic_deletion_outside_prune(tmp_path: Path) -> None:
    """Acceptance 14: nothing deletes a report or a roll without someone asking. Two halves
    — the code that does the deleting (a static scan, so a future `unlink` in tick.py or
    runner.py fails here rather than in production), and one tick that rotates, proving the
    automatic path is the rename it claims to be: same files on disk afterwards, plus one
    roll."""
    found = {
        p.name
        for p in (REPO_ROOT / "src" / "herdr_routines").glob("*.py")
        if _DELETE_CALL_RE.search(p.read_text())
    }
    assert found == _EXPECTED_DELETERS, (
        "a module gained a delete path; only the reviewed deleters may remove files"
    )

    state_dir = tmp_path / "state"
    reports = state_dir / "reports"
    reports.mkdir(parents=True)
    history_path = state_dir / "history.jsonl"
    report = _write(reports / "a-20260101T000000Z.md")
    tail = _write(reports / "a-20260101T000000Z.tail.txt")
    append(history_path, HistoryRecord(ts=NOW, job="a", state="registered"))

    before = sorted(p.name for p in state_dir.rglob("*") if p.is_file())

    # A tick with rotation turned all the way down: the threshold is met on every call. No
    # jobs, so the client is never touched — hence the cast rather than a fake.
    config = RoutinesConfig(jobs=(), retention=Retention(history_max_bytes=1))
    client = cast("HerdrClient", object())
    outcome = run_tick(config, history_path, client=client, now=NOW)
    assert outcome.summaries == ()
    # Rotating twice in the same second is a collision, so the second tick rolls nothing and
    # only warns — a skipped roll is still not a deletion.
    run_tick(config, history_path, client=client, now=NOW)

    assert report.exists() and tail.exists()
    rolls = rolled_history_paths(history_path)
    assert len(rolls) == 1
    assert rolls[0].exists()  # the roll is on disk, with the pre-roll records in it
    assert read_job(history_path, "a")[0].state == "registered"
    after = sorted(p.name for p in state_dir.rglob("*") if p.is_file())
    assert set(before) <= set(after), "the tick must not remove anything"
    assert history_path.exists()

    # Rotation's own primitive is a rename, not an unlink: the pre-roll inode survives.
    rolled = rolled_path(history_path, NOW)
    assert rolled.exists()
    assert (
        rolled.read_text()
        == HistoryRecord(ts=NOW, job="a", state="registered").to_json_line() + "\n"
    )
    assert (
        maybe_rotate(history_path, max_bytes=1, now=NOW) is None
    )  # collision, skipped


# -- prune history ------------------------------------------------------------


def test_prune_rolled_history_leaves_the_live_file(tmp_path: Path) -> None:
    """`prune history` collects rolls only, and only past the window. Off by default at the
    config level (no threshold, no schedule), so this is reachable only by typing the
    command — the earliest roll is exactly the record `first_seen_at` depends on."""
    history_path = tmp_path / "history.jsonl"
    append(history_path, HistoryRecord(ts=NOW, job="a", state="registered"))
    old_roll = rolled_path(history_path, NOW - timedelta(days=800))
    old_roll.write_text('{"ts": "2024-01-01T00:00:00Z", "job": "a", "state": "done"}\n')
    stamp = time.time() - 800 * 86_400
    os.utime(old_roll, (stamp, stamp))
    fresh_roll = rolled_path(history_path, NOW - timedelta(days=1))
    fresh_roll.write_text(
        '{"ts": "2026-01-01T00:00:00Z", "job": "a", "state": "done"}\n'
    )

    result = prune_rolled_history(history_path, older_than_days=365)

    assert result.removed == (old_roll,)
    assert result.kept_fresh == (fresh_roll,)
    assert not old_roll.exists()
    assert history_path.exists() and fresh_roll.exists()

    # The live file is never a candidate, however old it is: 0 days means "not touched in
    # the last instant", which does collect the fresh roll but must still leave history.jsonl.
    os.utime(history_path, (0, 0))
    zero = prune_rolled_history(history_path, older_than_days=0)
    assert history_path not in zero.removed
    assert fresh_roll in zero.removed
    assert history_path.exists()
