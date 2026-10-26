"""Tests for the pipeline stall watchdog (issue 031): a run must only be touched when
both the deadline (plus grace) has passed AND the heartbeat has gone stale — deadline
overrun alone is sanctioned behavior for a live orchestrator draining an in-flight
worker (see `pipeline_watchdog.py`'s module docstring). Mirrors the fake-client patterns
of test_tick.py / test_ps.py."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import pipeline_watchdog
from herdr_routines.herdr import HerdrCliError
from herdr_routines.history import HistoryRecord, append, read_all
from herdr_routines.pipeline_watchdog import (
    DEADLINE_GRACE_SECONDS,
    HEARTBEAT_STALE_SECONDS,
    InflightRun,
    _write_report,
    find_inflight_runs,
    is_stalled,
    pipeline_unit_active,
    recorded_pipeline_deadlines,
    run_watchdog,
    system_boot_epoch,
    validate_stage_sessions,
)

RUN_ID = "20260903T050016Z"
NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def unit_not_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every sweep to "orchestrator unit is gone", so tests never shell out to the
    host's real systemctl (absent on macOS, no user bus on CI, which would read as
    "can't tell" and block every reap). Tests of the liveness gate override it."""
    monkeypatch.setattr(
        "herdr_routines.pipeline_watchdog.pipeline_unit_active",
        lambda run_id: False,
        raising=False,
    )


@dataclass
class FakeWatchdogClient:
    """Fake HerdrClient surface run_watchdog actually uses: live_pipeline_agent_panes,
    pipeline_agent_panes, pane_close, notification_show. `panes_by_run` are `working`
    agents; `blocked_panes_by_run` are registered but not working (e.g. restored by herdr
    at boot), so only `pipeline_agent_panes` returns them."""

    panes_by_run: dict[str, dict[str, str]] = field(default_factory=dict)
    blocked_panes_by_run: dict[str, dict[str, str]] = field(default_factory=dict)
    raise_on_list: bool = False
    raise_os_error_for: set[str] = field(default_factory=set)
    closed_panes: list[str] = field(default_factory=list)
    notifications: list[tuple[str, str | None, str]] = field(default_factory=list)

    def live_pipeline_agent_panes(self, run_id: str) -> dict[str, str]:
        if run_id in self.raise_os_error_for:
            raise OSError("herdr socket vanished")
        if self.raise_on_list:
            raise HerdrCliError("server unreachable", exit_code=1)
        return self.panes_by_run.get(run_id, {})

    def pipeline_agent_panes(self, run_id: str) -> dict[str, str]:
        if self.raise_on_list:
            raise HerdrCliError("server unreachable", exit_code=1)
        return {
            **self.panes_by_run.get(run_id, {}),
            **self.blocked_panes_by_run.get(run_id, {}),
        }

    def pane_close(self, pane_id: str) -> None:
        self.closed_panes.append(pane_id)

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append((title, body, sound))


def write_state_json(
    worktrees_root: Path,
    *,
    run_id: str = RUN_ID,
    deadline_epoch: int,
    current_stage: int = 6,
    extra: dict | None = None,
) -> Path:
    run_dir = worktrees_root / f"auto-pipeline-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    payload = {
        "run_id": run_id,
        "current_stage": current_stage,
        "deadline_epoch": deadline_epoch,
    }
    if extra:
        payload.update(extra)
    state_path.write_text(json.dumps(payload))
    return state_path


def write_heartbeat(
    heartbeat_dir: Path, run_id: str, *, mtime: float, text: str
) -> Path:
    log_path = heartbeat_dir / f"pipeline_resume_{run_id}.log"
    log_path.write_text(text)
    os.utime(log_path, (mtime, mtime))
    return log_path


# -- find_inflight_runs ------------------------------------------------------------------


def test_find_inflight_runs_skips_run_with_terminal_report(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    write_state_json(worktrees_root, deadline_epoch=1000)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"pipeline-{RUN_ID}.md").write_text("done\n")

    assert find_inflight_runs(worktrees_root, reports_dir) == []


def test_find_inflight_runs_skips_run_with_report_at_artifact_paths(
    tmp_path: Path,
) -> None:
    """The orchestrator's own recorded report path (state.json artifact_paths.report) is
    authoritative and checked even when it doesn't match either naming convention."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    custom_report = tmp_path / "somewhere-else.md"
    custom_report.write_text("done\n")
    write_state_json(
        worktrees_root,
        deadline_epoch=1000,
        extra={"artifact_paths": {"report": str(custom_report)}},
    )

    assert find_inflight_runs(worktrees_root, reports_dir) == []


def test_find_inflight_runs_includes_run_with_no_report(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    write_state_json(worktrees_root, deadline_epoch=1000, current_stage=5)

    runs = find_inflight_runs(worktrees_root, reports_dir)
    assert len(runs) == 1
    assert runs[0].run_id == RUN_ID
    assert runs[0].deadline_epoch == 1000
    assert runs[0].current_stage == 5


def test_find_inflight_runs_skips_malformed_state_json(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    run_dir = worktrees_root / f"auto-pipeline-{RUN_ID}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text("not json")

    assert find_inflight_runs(worktrees_root, reports_dir) == []


def test_find_inflight_runs_skips_missing_deadline_epoch(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    run_dir = worktrees_root / f"auto-pipeline-{RUN_ID}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps({"run_id": RUN_ID}))

    assert find_inflight_runs(worktrees_root, reports_dir) == []


def test_find_inflight_runs_empty_when_root_missing(tmp_path: Path) -> None:
    assert find_inflight_runs(tmp_path / "nope", tmp_path / "reports") == []


# -- is_stalled ---------------------------------------------------------------------------


def _run(deadline_epoch: int):
    from herdr_routines.pipeline_watchdog import InflightRun

    return InflightRun(
        run_id=RUN_ID,
        state_path=Path("/dev/null"),
        deadline_epoch=deadline_epoch,
        current_stage=6,
    )


def test_is_stalled_false_when_deadline_not_passed(tmp_path: Path) -> None:
    run = _run(int(NOW.timestamp()) + 3600)
    assert is_stalled(run, now=NOW, heartbeat_dir=tmp_path) is False


def test_is_stalled_false_when_within_grace(tmp_path: Path) -> None:
    run = _run(int(NOW.timestamp()) - (DEADLINE_GRACE_SECONDS - 60))
    assert is_stalled(run, now=NOW, heartbeat_dir=tmp_path) is False


def test_is_stalled_false_when_heartbeat_still_fresh(tmp_path: Path) -> None:
    """A live orchestrator legitimately overruns its deadline while draining an
    in-flight --wait (orchestrator-prompt.md); a fresh heartbeat must never be killed."""
    run = _run(int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600)
    write_heartbeat(
        tmp_path,
        RUN_ID,
        mtime=NOW.timestamp() - 60,
        text="stage 6 poll 11:59:00Z\n",
    )
    assert is_stalled(run, now=NOW, heartbeat_dir=tmp_path) is False


def test_is_stalled_true_when_deadline_and_heartbeat_both_stale(tmp_path: Path) -> None:
    run = _run(int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600)
    write_heartbeat(
        tmp_path,
        RUN_ID,
        mtime=NOW.timestamp() - HEARTBEAT_STALE_SECONDS - 60,
        text="stage 5 poll 05:44:10Z\n",
    )
    assert is_stalled(run, now=NOW, heartbeat_dir=tmp_path) is True


def test_is_stalled_true_when_heartbeat_file_missing() -> None:
    """A missing heartbeat file counts as stale, not healthy — /tmp clears across a
    reboot, which is exactly the silent-death case this exists to catch."""
    run = _run(int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600)
    assert (
        is_stalled(run, now=NOW, heartbeat_dir=Path("/nonexistent-heartbeat-dir"))
        is True
    )


# -- run_watchdog end to end ----------------------------------------------------------------


def test_run_watchdog_ignores_healthy_run(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    write_state_json(worktrees_root, deadline_epoch=int(NOW.timestamp()) + 3600)
    client = FakeWatchdogClient()

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert actions == []
    assert client.closed_panes == []
    assert client.notifications == []
    assert not (reports_dir / f"pipeline-{RUN_ID}.md").exists()


def test_run_watchdog_kills_stalled_worker_and_writes_report(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    write_state_json(
        worktrees_root,
        deadline_epoch=int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600,
        current_stage=6,
    )
    write_heartbeat(
        heartbeat_dir,
        RUN_ID,
        mtime=NOW.timestamp() - HEARTBEAT_STALE_SECONDS - 60,
        text="stage 5 poll 05:44:10Z\n",
    )
    agent_name = f"pl-6-{RUN_ID}".lower()
    client = FakeWatchdogClient(panes_by_run={RUN_ID: {agent_name: "w1:p1"}})

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.run_id == RUN_ID
    assert action.killed_agents == (agent_name,)
    assert action.stage_killed == 6
    assert client.closed_panes == ["w1:p1"]

    report_path = reports_dir / f"pipeline-{RUN_ID}.md"
    assert report_path.exists()
    text = report_path.read_text()
    assert "## Outcome: failed (watchdog killed)" in text
    assert "watchdog_killed: true" in text
    assert "stage_killed: 6" in text
    assert "stage 5 poll 05:44:10Z" in text

    assert len(client.notifications) == 1
    title, _body, sound = client.notifications[0]
    assert RUN_ID in title
    assert sound == "request"


def test_run_watchdog_writes_report_even_with_no_live_worker(tmp_path: Path) -> None:
    """Deadline+heartbeat both stale but nothing left to kill (already exited on its
    own) — the report must still be written, or the next sweep re-detects it forever."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    write_state_json(
        worktrees_root,
        deadline_epoch=int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600,
    )
    client = FakeWatchdogClient(panes_by_run={})

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert len(actions) == 1
    assert actions[0].killed_agents == ()
    assert client.closed_panes == []
    report_path = reports_dir / f"pipeline-{RUN_ID}.md"
    assert report_path.exists()
    text = report_path.read_text()
    assert "## Outcome: failed (watchdog: no live worker found)" in text
    assert "watchdog_killed: false" in text
    assert len(client.notifications) == 1


def test_run_watchdog_skips_run_when_agent_list_fails(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    write_state_json(
        worktrees_root,
        deadline_epoch=int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600,
    )
    client = FakeWatchdogClient(raise_on_list=True)

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert actions == []
    assert client.notifications == []
    assert not (reports_dir / f"pipeline-{RUN_ID}.md").exists()


def test_run_watchdog_never_touches_run_with_existing_report(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    write_state_json(
        worktrees_root,
        deadline_epoch=int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600,
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"pipeline-{RUN_ID}.md").write_text("## Outcome: ok\n")
    client = FakeWatchdogClient(panes_by_run={RUN_ID: {"pl-6-x": "w1:p1"}})

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert actions == []
    assert client.closed_panes == []


# -- system_boot_epoch ------------------------------------------------------------------


def test_system_boot_epoch_parses_proc_stat(tmp_path: Path) -> None:
    stat = tmp_path / "stat"
    stat.write_text("cpu  1 2 3\nbtime 1788779818\nprocesses 42\n")
    assert system_boot_epoch(stat) == 1788779818.0


def test_system_boot_epoch_none_when_unreadable(tmp_path: Path) -> None:
    assert system_boot_epoch(tmp_path / "missing") is None
    no_btime = tmp_path / "stat2"
    no_btime.write_text("cpu 1 2 3\nprocesses 42\n")
    assert system_boot_epoch(no_btime) is None


# -- validate_stage_sessions (design.md G-17 gate) -------------------------------------


def test_validate_stage_sessions_accepts_distinct_real_ids() -> None:
    ok = {
        "current_stage": 7,
        "stage_sessions": {
            "1": "ses_f9f8186e3ffeY07GblyzaVJP01",
            "2": "ses_f9f7ee0e7ffeEXZ9pkSIMNPhmC",
            "3": "ses_f9a578e4effegxE5jPbimAei1R",
            "4": "ses_f9a446f22ffe4EQX3KWOAwetPz",
            "6": "ses_b2c3d4e5f6a1b2c3d4e5f6a1b2",
            # Stage 7 reuses stage 4's session by declaration.
            "7": "ses_f9a446f22ffe4EQX3KWOAwetPz",
        },
    }
    assert validate_stage_sessions(ok) is None


def test_validate_stage_sessions_flags_placeholders() -> None:
    # Real 2026-09-06 / 2026-09-07 fabrications.
    faked = {
        "current_stage": 3,
        "stage_sessions": {
            "1": "ses_f87fc2c5_fake1",
            "2": "ses_f87fc2c5_fake2",
            "3": "ses_f87fc2c5_fake3",
        },
    }
    issue = validate_stage_sessions(faked)
    assert issue is not None and "placeholder" in issue


def test_validate_stage_sessions_flags_reused_session() -> None:
    reused = {
        "current_stage": 2,
        "stage_sessions": {
            "1": "ses_f9f8186e3ffeY07GblyzaVJP01",
            "2": "ses_f9f8186e3ffeY07GblyzaVJP01",
        },
    }
    issue = validate_stage_sessions(reused)
    assert issue is not None and "reuses" in issue


def test_validate_stage_sessions_flags_missing_entries() -> None:
    incomplete = {
        "current_stage": 6,
        "stage_sessions": {
            "1": "ses_f9f8186e3ffeY07GblyzaVJP01",
            "2": "ses_f9f7ee0e7ffeEXZ9pkSIMNPhmC",
        },
    }
    issue = validate_stage_sessions(incomplete)
    assert issue is not None and "unverified" in issue


def test_validate_stage_sessions_flags_a_bogus_key_padding_a_missing_stage() -> None:
    """Right count, wrong stages: stage 2 recorded nothing, key "9" maps to no stage."""
    padded = {
        "current_stage": 2,
        "stage_sessions": {
            "1": "ses_f9f8186e3ffeY07GblyzaVJP01",
            "9": "ses_f9f7ee0e7ffeEXZ9pkSIMNPhmC",
        },
    }
    issue = validate_stage_sessions(padded)
    assert issue is not None and "stage(s) 2" in issue


def test_validate_stage_sessions_flags_missing_field() -> None:
    assert validate_stage_sessions({"current_stage": 3}) is not None


# -- is_stalled / run_watchdog: host reboot fast-fail ----------------------------------


def test_is_stalled_true_when_host_rebooted_after_state_write(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    # deadline is far in the future — the old path would say "not stalled".
    state_path = write_state_json(
        worktrees_root, deadline_epoch=int(NOW.timestamp()) + 6 * 3600
    )
    os.utime(state_path, (NOW.timestamp() - 600, NOW.timestamp() - 600))
    (run,) = find_inflight_runs(worktrees_root, reports_dir)

    # Booted 5 min ago, i.e. well after the state write 10 min ago.
    assert is_stalled(
        run,
        now=NOW,
        heartbeat_dir=heartbeat_dir,
        boot_epoch=NOW.timestamp() - 300,
    )
    # No boot_epoch (can't read /proc/stat) => falls back to the deadline path => not stalled.
    assert not is_stalled(run, now=NOW, heartbeat_dir=heartbeat_dir)


def test_run_watchdog_fails_fast_on_host_reboot(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    state_path = write_state_json(
        worktrees_root, deadline_epoch=int(NOW.timestamp()) + 6 * 3600
    )
    os.utime(state_path, (NOW.timestamp() - 1800, NOW.timestamp() - 1800))
    client = FakeWatchdogClient()

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
        boot_epoch=NOW.timestamp() - 300,
    )

    assert len(actions) == 1
    report = (reports_dir / f"pipeline-{RUN_ID}.md").read_text()
    assert "## Outcome: failed (watchdog: host rebooted mid-run)" in report
    assert "host_rebooted_mid_run: true" in report
    title, _body, _sound = client.notifications[0]
    assert "host rebooted mid-run" in title


def test_run_watchdog_closes_agents_herdr_restored_after_a_reboot(
    tmp_path: Path,
) -> None:
    """2026-10-01: after the reboot herdr restored pl-1 and pl-5 as `blocked`, and they
    held ~1.1 GB until closed by hand. A dead run's agents go, whatever their status."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    state_path = write_state_json(
        worktrees_root, deadline_epoch=int(NOW.timestamp()) + 6 * 3600
    )
    os.utime(state_path, (NOW.timestamp() - 1800, NOW.timestamp() - 1800))
    restored = {f"pl-1-{RUN_ID.lower()}": "w6E:p1", f"pl-5-{RUN_ID.lower()}": "w6E:p4"}
    client = FakeWatchdogClient(blocked_panes_by_run={RUN_ID: restored})

    run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
        boot_epoch=NOW.timestamp() - 300,
    )

    assert sorted(client.closed_panes) == ["w6E:p1", "w6E:p4"]
    report = (reports_dir / f"pipeline-{RUN_ID}.md").read_text()
    assert f"pl-1-{RUN_ID.lower()}" in report


def test_run_watchdog_leaves_a_blocked_agent_open_without_a_reboot(
    tmp_path: Path,
) -> None:
    """Past the deadline but no reboot: a `blocked` worker is a human's to inspect, not
    the watchdog's to close — only `working` ones are killed."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    write_state_json(worktrees_root, deadline_epoch=int(NOW.timestamp()) - 3 * 3600)
    client = FakeWatchdogClient(
        blocked_panes_by_run={RUN_ID: {f"pl-3-{RUN_ID.lower()}": "w1:p3"}}
    )

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
        boot_epoch=NOW.timestamp() - 30 * 24 * 3600,
    )

    assert len(actions) == 1
    assert client.closed_panes == []


# -- tick's recorded deadline wins over state.json ------------------------------------------


def _record_launch(history_path: Path, *, deadline_epoch: int) -> None:
    append(
        history_path,
        HistoryRecord(
            ts=NOW,
            job="feature-pipeline",
            state="running",
            run_id=f"feature-pipeline-{RUN_ID}",
            extra={"pipeline_run_id": RUN_ID, "deadline_epoch": deadline_epoch},
        ),
    )


def test_recorded_deadline_overrides_a_bogus_state_json_deadline(
    tmp_path: Path,
) -> None:
    """Replays 2026-09-28: the fallback model wrote a state.json deadline a year in the
    past and no heartbeat. With tick's recorded deadline still hours away, the live run
    must be left alone."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "hb"
    heartbeat_dir.mkdir()
    history_path = tmp_path / "history.jsonl"
    a_year_ago = int(NOW.timestamp()) - 365 * 24 * 3600
    write_state_json(worktrees_root, deadline_epoch=a_year_ago, current_stage=1)
    _record_launch(history_path, deadline_epoch=int(NOW.timestamp()) + 6 * 3600)
    client = FakeWatchdogClient(panes_by_run={RUN_ID: {f"pl-1-{RUN_ID}": "w1:p1"}})

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        history_path=history_path,
        now=NOW,
        boot_epoch=0,
    )

    assert actions == []
    assert client.closed_panes == []


def test_recorded_deadline_still_reaps_a_genuinely_stalled_run(tmp_path: Path) -> None:
    """The recorded deadline is used in both directions: past it (plus grace) with a
    stale heartbeat, the run is reaped even though state.json claims more time."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "hb"
    heartbeat_dir.mkdir()
    history_path = tmp_path / "history.jsonl"
    write_state_json(worktrees_root, deadline_epoch=int(NOW.timestamp()) + 3600)
    _record_launch(
        history_path,
        deadline_epoch=int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 60,
    )
    client = FakeWatchdogClient()

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        history_path=history_path,
        now=NOW,
        boot_epoch=0,
    )

    assert [a.run_id for a in actions] == [RUN_ID]


def test_recorded_deadline_covers_a_state_json_without_one(tmp_path: Path) -> None:
    """A state.json missing deadline_epoch used to make the run invisible to the
    watchdog forever; tick's recorded deadline now covers it."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    run_dir = worktrees_root / f"auto-pipeline-{RUN_ID}"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({"run_id": RUN_ID}))
    history_path = tmp_path / "history.jsonl"
    _record_launch(history_path, deadline_epoch=1234)

    runs = find_inflight_runs(
        worktrees_root, reports_dir, recorded_pipeline_deadlines(history_path)
    )

    assert [(r.run_id, r.deadline_epoch) for r in runs] == [(RUN_ID, 1234)]


# -- systemd unit liveness ---------------------------------------------------------------


def _stalled_run_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    write_state_json(
        worktrees_root,
        deadline_epoch=int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600,
    )
    write_heartbeat(
        heartbeat_dir,
        RUN_ID,
        mtime=NOW.timestamp() - HEARTBEAT_STALE_SECONDS - 60,
        text="stage 5 poll\n",
    )
    return worktrees_root, reports_dir, heartbeat_dir


@pytest.mark.parametrize("unit_state", [True, None])
def test_run_watchdog_does_not_reap_when_unit_active_or_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit_state: bool | None
) -> None:
    worktrees_root, reports_dir, heartbeat_dir = _stalled_run_fixture(tmp_path)
    monkeypatch.setattr(
        "herdr_routines.pipeline_watchdog.pipeline_unit_active",
        lambda run_id: unit_state,
        raising=False,
    )
    agent_name = f"pl-6-{RUN_ID}".lower()
    client = FakeWatchdogClient(panes_by_run={RUN_ID: {agent_name: "w1:p1"}})

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert actions == []
    assert client.closed_panes == []
    assert client.notifications == []
    assert not (reports_dir / f"pipeline-{RUN_ID}.md").exists()


def test_run_watchdog_reaps_when_unit_inactive(tmp_path: Path) -> None:
    worktrees_root, reports_dir, heartbeat_dir = _stalled_run_fixture(tmp_path)
    client = FakeWatchdogClient()

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert [a.run_id for a in actions] == [RUN_ID]
    assert (reports_dir / f"pipeline-{RUN_ID}.md").exists()


def _fake_systemctl(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = ""
) -> list[tuple[list[str], float]]:
    calls: list[tuple[list[str], float]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, (int, float))
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, returncode, stdout, "boom")

    monkeypatch.setattr("herdr_routines.pipeline_watchdog.subprocess.run", fake_run)
    return calls


def test_pipeline_unit_active_true_when_a_unit_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # drop the autouse stub; exercise the real function
    calls = _fake_systemctl(
        monkeypatch,
        stdout=f"herdr-pipeline-{RUN_ID}.service loaded active running x\n",
    )

    assert pipeline_unit_active(RUN_ID) is True
    argv, timeout = calls[0]
    assert argv[:3] == ["systemctl", "--user", "list-units"]
    assert argv[-1] == f"herdr-pipeline-{RUN_ID}*"
    assert timeout > 0


def test_pipeline_unit_active_false_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    _fake_systemctl(monkeypatch, stdout="")

    assert pipeline_unit_active(RUN_ID) is False


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="systemctl", timeout=10),
        FileNotFoundError("systemctl"),
    ],
)
def test_pipeline_unit_active_none_when_systemctl_cannot_run(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    monkeypatch.undo()

    def raising_run(*_a: object, **_k: object) -> None:
        raise failure

    monkeypatch.setattr("herdr_routines.pipeline_watchdog.subprocess.run", raising_run)

    assert pipeline_unit_active(RUN_ID) is None


def test_pipeline_unit_active_none_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    _fake_systemctl(monkeypatch, returncode=1)

    assert pipeline_unit_active(RUN_ID) is None


# -- atomic report write -----------------------------------------------------------------


def test_write_report_failure_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A torn report would count as terminal (`_has_terminal_report`) so the run is never
    rechecked, while tick would fail to parse it. The write must be all-or-nothing."""
    reports_dir = tmp_path / "reports"
    run = InflightRun(
        run_id=RUN_ID,
        state_path=tmp_path / "state.json",
        deadline_epoch=0,
        current_stage=6,
    )

    def failing_replace(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", failing_replace)

    with pytest.raises(OSError, match="disk full"):
        _write_report(
            reports_dir,
            run,
            killed_agents={},
            heartbeat_last_line=None,
            now=NOW,
        )

    assert list(reports_dir.iterdir()) == []


# -- per-run error isolation -------------------------------------------------------------


def test_run_watchdog_continues_after_one_run_raises(tmp_path: Path) -> None:
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir()
    bad_run, good_run = "20260901T050016Z", "20260902T050016Z"
    for run_id in (bad_run, good_run):
        write_state_json(
            worktrees_root,
            run_id=run_id,
            deadline_epoch=int(NOW.timestamp()) - DEADLINE_GRACE_SECONDS - 3600,
        )
        write_heartbeat(
            heartbeat_dir,
            run_id,
            mtime=NOW.timestamp() - HEARTBEAT_STALE_SECONDS - 60,
            text="stage 5 poll\n",
        )
    client = FakeWatchdogClient(
        raise_os_error_for={bad_run},
        panes_by_run={good_run: {f"pl-6-{good_run}".lower(): "w1:p1"}},
    )

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        now=NOW,
    )

    assert [a.run_id for a in actions] == [good_run]
    assert client.closed_panes == ["w1:p1"]
    assert (reports_dir / f"pipeline-{good_run}.md").exists()
    assert not (reports_dir / f"pipeline-{bad_run}.md").exists()


# -- issue 068: one automatic resume attempt in the sweep --------------------------------


NOW_TS = int(NOW.timestamp())
ORIGINAL_TS = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)  # the original launch
JOB = "feature-pipeline"
AGENT_NAME = f"pl-6-{RUN_ID}".lower()


def _launch_record(*, deadline_epoch: int) -> HistoryRecord:
    """Tick's `running` record at the original launch: it is what makes the run
    resume-eligible (job name + original budget) and what the recorded deadline comes
    from. `ts` two hours before NOW, so the original wall budget is positive."""
    return HistoryRecord(
        ts=ORIGINAL_TS,
        job=JOB,
        state="running",
        run_id=f"{JOB}-{RUN_ID}",
        extra={"pipeline_run_id": RUN_ID, "deadline_epoch": deadline_epoch},
    )


@dataclass
class ResumeFixture:
    worktrees_root: Path
    reports_dir: Path
    heartbeat_dir: Path
    history_path: Path
    state_path: Path
    repo_parent: Path
    report_path: Path
    recorded_deadline: int
    resume_count: int
    with_history: bool
    with_repo_parent: bool

    @property
    def budget_ms(self) -> int:
        return (self.recorded_deadline - int(ORIGINAL_TS.timestamp())) * 1000

    @property
    def new_deadline(self) -> int:
        return NOW_TS + self.budget_ms // 1000


_DEFAULT_REPO_PARENT = object()


def _resume_candidate(
    tmp_path: Path,
    *,
    budget_seconds: int = 3600,
    repo_parent: str | None | object = _DEFAULT_REPO_PARENT,
    resume_count: int = 0,
    with_history: bool = True,
) -> ResumeFixture:
    """A stalled run that is otherwise resume-eligible: unit inactive (autouse), stale
    heartbeat, tick's recorded deadline + `running` record on file, `repo_parent` and
    `artifact_paths.report` in state.json. Everything eligibility needs except good
    fortune. `repo_parent=None` (or "") and `with_history=False` are the knobs the
    ineligible-cases test turns."""
    worktrees_root = tmp_path / "worktrees"
    reports_dir = tmp_path / "reports"
    heartbeat_dir = tmp_path / "tmp"
    heartbeat_dir.mkdir(parents=True, exist_ok=True)
    repo_parent_path = tmp_path / "parent"
    if repo_parent is _DEFAULT_REPO_PARENT:
        repo_parent = str(repo_parent_path)
    assert repo_parent is None or isinstance(repo_parent, str)
    report_path = reports_dir / f"pipeline-{RUN_ID}.md"
    recorded_deadline = int(ORIGINAL_TS.timestamp()) + budget_seconds
    history_path = tmp_path / "history.jsonl"
    extra: dict = {
        "artifact_paths": {"report": str(report_path)},
        "resume_count": resume_count,
    }
    if repo_parent is not None:
        extra["repo_parent"] = repo_parent
    write_state_json(
        worktrees_root,
        deadline_epoch=recorded_deadline if with_history else NOW_TS - 3600,
        extra=extra,
    )
    write_heartbeat(
        heartbeat_dir,
        RUN_ID,
        mtime=NOW.timestamp() - HEARTBEAT_STALE_SECONDS - 60,
        text="stage 6 poll 11:58:50Z\n",
    )
    if with_history:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        append(history_path, _launch_record(deadline_epoch=recorded_deadline))
    state_path = pipeline_watchdog.pipeline_state_json_path(worktrees_root, RUN_ID)
    assert state_path is not None
    return ResumeFixture(
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        heartbeat_dir=heartbeat_dir,
        history_path=history_path,
        state_path=state_path,
        repo_parent=repo_parent_path,
        report_path=report_path,
        recorded_deadline=recorded_deadline,
        resume_count=resume_count,
        with_history=with_history,
        with_repo_parent=repo_parent is not None,
    )


class OrderRecordingClient(FakeWatchdogClient):
    """Records the sweep's actions as they happen, and — at the moment a pane is
    closed — how many `running` history records for this run are already on disk. That
    is exactly where issue 068's record-before-kill order is observable: tick must never
    see an in-flight gap between the dead record and the fresh one, so the fresh
    `running` record exists in the file *before* the kill."""

    def __init__(
        self,
        *args: Any,
        history_path: Path | None = None,
        events: list[tuple[object, ...]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.history_path = history_path
        self.events = events if events is not None else []

    def pane_close(self, pane_id: str) -> None:
        running_records = 0
        if self.history_path is not None and self.history_path.exists():
            running_records = sum(
                1
                for r in read_all(self.history_path)
                if r.state == "running"
                and (r.extra or {}).get("pipeline_run_id") == RUN_ID
            )
        self.events.append(("close", pane_id, running_records))
        super().pane_close(pane_id)


def _argv_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _patch_launch(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[object, ...]],
    *,
    rc: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> list[list[str]]:
    """The `launch_resume` seam, monkeypatched like `tick.launch_pipeline`."""
    launched: list[list[str]] = []

    def fake_launch(argv: list[str], **_kwargs: Any) -> tuple[int, str, str]:
        argv = list(argv)
        launched.append(argv)
        events.append(("launch", argv))
        return rc, stdout, stderr

    monkeypatch.setattr(pipeline_watchdog, "launch_resume", fake_launch, raising=False)
    return launched


def test_run_watchdog_resumes_a_stalled_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue 068: a stalled, unit-inactive run with a usable `repo_parent`, a zero
    `resume_count`, tick's `running` history record and a positive original wall budget
    is resumed instead of killed-and-reported: fresh `running` history record first (so
    tick never sees an in-flight gap), panes killed second, state.json rewritten
    atomically third, the resume unit registered last — and no terminal report."""
    fx = _resume_candidate(tmp_path)
    events: list[tuple[object, ...]] = []
    client = OrderRecordingClient(
        panes_by_run={RUN_ID: {AGENT_NAME: "w1:p1"}},
        history_path=fx.history_path,
        events=events,
    )
    launched = _patch_launch(monkeypatch, events)

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=fx.worktrees_root,
        reports_dir=fx.reports_dir,
        heartbeat_dir=fx.heartbeat_dir,
        history_path=fx.history_path,
        now=NOW,
        boot_epoch=0,
    )

    # One resume attempt, and the action says so — no report, `resumed is True`.
    assert len(actions) == 1
    action = actions[0]
    assert action.run_id == RUN_ID
    assert action.resumed is True
    assert action.report_path is None
    assert action.killed_agents == (AGENT_NAME,)
    assert not fx.report_path.exists()
    assert len(client.notifications) == 1
    title, body, _sound = client.notifications[0]
    assert "resum" in f"{title} {body}".lower()

    # Order: the fresh `running` record is already on disk when the first pane is
    # closed (original record + fresh record = 2), and the launch comes after the kill.
    assert [e[0] for e in events] == ["close", "launch"]
    assert events[0] == ("close", "w1:p1", 2)

    # state.json, rewritten atomically (tmpfile + rename): later deadline, the cap
    # counter moved once, a `resumed_at` marker, and nothing else disturbed.
    state = json.loads(fx.state_path.read_text())
    assert state["deadline_epoch"] == fx.new_deadline
    assert state["resume_count"] == 1
    assert state["resumed_at"] == NOW.isoformat()
    assert state["repo_parent"] == str(fx.repo_parent)
    assert state["artifact_paths"] == {"report": str(fx.report_path)}
    assert [p.name for p in fx.state_path.parent.iterdir()] == ["state.json"]

    # The fresh history record carries the new deadline/unit/counter before the kill.
    records = read_all(fx.history_path)
    fresh = records[-1]
    assert fresh.state == "running"
    assert fresh.job == JOB
    assert fresh.ts == NOW
    extra = fresh.extra or {}
    assert extra.get("pipeline_run_id") == RUN_ID
    assert extra.get("deadline_epoch") == fx.new_deadline
    assert extra.get("resume_count") == 1
    assert "resumed_from" in extra
    assert str(fx.report_path) in str(extra)

    # The resume unit, exactly the launcher in resume mode: same run/state/report,
    # prompts dir derivable from `repo_parent`, the budget reset (not the negative
    # remainder), RuntimeMaxSec with the margin on top.
    assert len(launched) == 1
    argv = launched[0]
    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert "--collect" in argv
    unit = next(a for a in argv if a.startswith("--unit="))
    assert re.fullmatch(rf"--unit=herdr-pipeline-{RUN_ID}-r\d{{6}}", unit)
    runtime = argv.index("-p")
    budget_s = fx.budget_ms // 1000
    assert argv[runtime + 1] == (
        f"RuntimeMaxSec={budget_s + pipeline_watchdog.RESUME_UNIT_MARGIN_S}"
    )
    bash = argv.index("/bin/bash")
    assert argv[bash + 1] == str(fx.repo_parent / "scripts" / "pipeline-launch.sh")
    assert _argv_value(argv, "--run-id") == RUN_ID
    assert _argv_value(argv, "--repo-parent") == str(fx.repo_parent)
    assert _argv_value(argv, "--report") == str(fx.report_path)
    assert _argv_value(argv, "--state-json") == str(fx.state_path)
    assert "--resume" in argv
    assert _argv_value(argv, "--deadline-epoch") == str(fx.new_deadline)
    assert _argv_value(argv, "--agent-name")
    assert _argv_value(argv, "--wait-timeout-ms") == str(fx.budget_ms)


@pytest.mark.parametrize(
    ("kw", "reason_pattern"),
    [
        pytest.param({"repo_parent": None}, "repo_parent", id="repo_parent_missing"),
        pytest.param({"repo_parent": ""}, "repo_parent", id="repo_parent_empty"),
        pytest.param(
            {"with_history": False},
            "running record|no .*history|history .*missing",
            id="no_running_history_record",
        ),
        pytest.param(
            {"resume_exhausted": True},
            "resume_count",
            id="resume_exhausted",
        ),
        pytest.param(
            {"budget_seconds": 0},
            "budget",
            id="budget_non_positive",
        ),
    ],
)
def test_run_watchdog_falls_back_to_kill_and_report_for_each_ineligible_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kw: dict[str, Any],
    reason_pattern: str,
) -> None:
    """Every run the watchdog cannot safely resume stays kill-and-report, naming why in
    the report — and the launcher is never called. The exhausted-resume variant must
    carry an explicit `resume_count`/`resume_attempted` line so a second dead night is
    not mistaken for a first-time stall that somehow resumed nothing."""
    resolved = dict(kw)
    if resolved.pop("resume_exhausted", None):
        resolved["resume_count"] = pipeline_watchdog.MAX_PIPELINE_RESUMES
    fx = _resume_candidate(tmp_path, **resolved)
    before = json.loads(fx.state_path.read_text())
    events: list[tuple[object, ...]] = []
    client = OrderRecordingClient(
        panes_by_run={RUN_ID: {AGENT_NAME: "w1:p1"}}, events=events
    )
    launched = _patch_launch(monkeypatch, events)

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=fx.worktrees_root,
        reports_dir=fx.reports_dir,
        heartbeat_dir=fx.heartbeat_dir,
        history_path=fx.history_path if fx.with_history else None,
        now=NOW,
        boot_epoch=0,
    )

    assert launched == []
    assert len(actions) == 1
    action = actions[0]
    assert action.resumed is False
    assert action.report_path is not None
    report_text = action.report_path.read_text()
    assert "## Outcome: failed (watchdog" in report_text
    # The ineligibility reason is visible in the report, not only in a log line.
    assert re.search(reason_pattern, report_text, re.IGNORECASE)
    # The state file is untouched: no half-applied resume.
    assert json.loads(fx.state_path.read_text()) == before


def test_run_watchdog_resume_launch_failure_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume unit that failed to register ends the sweep the old way: a terminal
    report carrying the launch rc and stderr, `resumed=False`, and — because the report
    now exists — the run no longer looks in-flight to tick (the fresh `running` record
    does not make it a ghost)."""
    fx = _resume_candidate(tmp_path)
    events: list[tuple[object, ...]] = []
    client = OrderRecordingClient(
        panes_by_run={RUN_ID: {AGENT_NAME: "w1:p1"}},
        history_path=fx.history_path,
        events=events,
    )
    launched = _patch_launch(
        monkeypatch, events, rc=1, stdout="", stderr="boom: cannot start unit"
    )

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=fx.worktrees_root,
        reports_dir=fx.reports_dir,
        heartbeat_dir=fx.heartbeat_dir,
        history_path=fx.history_path,
        now=NOW,
        boot_epoch=0,
    )

    assert len(launched) == 1
    assert len(actions) == 1
    action = actions[0]
    assert action.resumed is False
    assert action.report_path is not None
    report_text = action.report_path.read_text()
    assert "## Outcome: failed (resume_failed)" in report_text
    assert re.search(r"rc[\"': =]+1", report_text)
    assert "boom: cannot start unit" in report_text
    # Not in-flight: with the terminal report on disk, the next sweep (and tick) skips it.
    assert (
        find_inflight_runs(
            fx.worktrees_root,
            fx.reports_dir,
            recorded_pipeline_deadlines(fx.history_path),
        )
        == []
    )


def test_run_watchdog_resumes_after_host_reboot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reboot is an unambiguous death, but the rest of the resume contract is the
    same: the rebooted run's restored panes are closed first (the reboot check's own
    cleanup, working *and* blocked panes), and the sweep then attempts the resume — no
    reboot special-case out of resuming."""
    fx = _resume_candidate(tmp_path)
    # The state write is what a reboot postdates: mtime 30 min before now, booted 5 min ago.
    os.utime(fx.state_path, (NOW_TS - 1800, NOW_TS - 1800))
    restored = {
        f"pl-1-{RUN_ID.lower()}": "w6E:p1",
        f"pl-5-{RUN_ID.lower()}": "w6E:p4",
    }
    events: list[tuple[object, ...]] = []
    client = OrderRecordingClient(
        blocked_panes_by_run={RUN_ID: restored},
        history_path=fx.history_path,
        events=events,
    )
    launched = _patch_launch(monkeypatch, events)

    actions = run_watchdog(
        client=client,  # type: ignore[arg-type]
        worktrees_root=fx.worktrees_root,
        reports_dir=fx.reports_dir,
        heartbeat_dir=fx.heartbeat_dir,
        history_path=fx.history_path,
        now=NOW,
        boot_epoch=float(NOW_TS) - 300,
    )

    assert len(actions) == 1
    assert actions[0].resumed is True
    assert actions[0].report_path is None
    # Reboot cleanup ran (close all restored panes), and the resume was still attempted.
    assert sorted(client.closed_panes) == sorted(restored.values())
    assert len(launched) == 1
    assert "--resume" in launched[0]
    assert not fx.report_path.exists()
    # Closes first, launch last — the record-before-kill order still holds.
    assert [e[0] for e in events][: len(restored)] == ["close"] * len(restored)
    assert events[-1][0] == "launch"
