"""Tests for the pipeline stall watchdog (issue 031): a run must only be touched when
both the deadline (plus grace) has passed AND the heartbeat has gone stale — deadline
overrun alone is sanctioned behavior for a live orchestrator draining an in-flight
worker (see `pipeline_watchdog.py`'s module docstring). Mirrors the fake-client patterns
of test_tick.py / test_ps.py."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from herdr_routines.herdr import HerdrCliError
from herdr_routines.pipeline_watchdog import (
    DEADLINE_GRACE_SECONDS,
    HEARTBEAT_STALE_SECONDS,
    find_inflight_runs,
    is_stalled,
    run_watchdog,
)

RUN_ID = "20260903T050016Z"
NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)


@dataclass
class FakeWatchdogClient:
    """Fake HerdrClient surface run_watchdog actually uses: live_pipeline_agent_panes,
    pane_close, notification_show."""

    panes_by_run: dict[str, dict[str, str]] = field(default_factory=dict)
    raise_on_list: bool = False
    closed_panes: list[str] = field(default_factory=list)
    notifications: list[tuple[str, str | None, str]] = field(default_factory=list)

    def live_pipeline_agent_panes(self, run_id: str) -> dict[str, str]:
        if self.raise_on_list:
            raise HerdrCliError("server unreachable", exit_code=1)
        return self.panes_by_run.get(run_id, {})

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
    assert "watchdog_killed: false" in report_path.read_text()
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
