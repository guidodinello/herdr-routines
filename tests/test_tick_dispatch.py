"""Behavior tests for tick's review and audit dispatch flows (issue 062).

`_process_review_job` and `_process_audit_job` share one schedule-guard shape (registered,
stale reap, already running, live agent, not due, missed, collapsed, due) that differs only
in the running-record staleness window, so the guards are one table over both kinds. The
cycles behind them (`_review_cycle` / `_dispatch_review_target`, `_audit_cycle` /
`_run_audit` / `_dispatch_audit_fix`) run against real local git and a fake herdr / gh,
reusing the fakes of `test_review_scan` and `test_audit_dispatch`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import tick
from herdr_routines.auto_fix import build_audit_agent_name
from herdr_routines.config import Job
from herdr_routines.history import STALE_MARGIN, HistoryRecord, append, read_job
from tests.test_audit_dispatch import _job as _audit_job
from tests.test_review_scan import _review_job

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
# One cron fire (08:30 UTC) lies between REGISTERED and NOW, 30 minutes late.
REGISTERED = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
STALE_MARGIN_MS = int(STALE_MARGIN.total_seconds() * 1000)


class GuardClient:
    """The slice of HerdrClient the schedule guards touch."""

    def __init__(
        self,
        *,
        statuses: dict[str, str] | None = None,
        panes: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.statuses = statuses or {}
        self.panes = panes or {}
        self.closed: list[str] = []
        self.notifications: list[tuple[str, str | None]] = []

    def agent_statuses(self) -> dict[str, str]:
        return dict(self.statuses)

    def agent_panes_by_status(self) -> dict[str, tuple[str, str]]:
        return dict(self.panes)

    def pane_close(self, pane_id: str) -> None:
        self.closed.append(pane_id)

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append((title, body))


@dataclass(frozen=True, slots=True)
class Kind:
    """What differs between the two flows under the shared guard table."""

    name: str
    make_job: Callable[..., Job]
    process: str
    cycle: str
    # Running-record ages (ms since it started): one that is still inside this kind's
    # staleness window but past a bare `timeout_ms` (so a wrong formula reads it stale),
    # and one past the window.
    inside_age_ms: int
    stale_age_ms: int
    live_agent: Callable[[Job], str]


def _review_kind(tmp_path: Path) -> Kind:
    # window = timeout_ms (60s) * max_workers_per_tick (3) + margin
    return Kind(
        name="review",
        make_job=lambda **over: _review_job(tmp_path, max_workers_per_tick=3, **over),
        process="_process_review_job",
        cycle="_review_cycle",
        inside_age_ms=STALE_MARGIN_MS + 120_000,
        stale_age_ms=STALE_MARGIN_MS + 200_000,
        live_agent=lambda job: job.agent_name,
    )


def _audit_kind(tmp_path: Path) -> Kind:
    # window = audit.timeout_ms (1.8M) + timeout_ms (2.7M) + margin
    return Kind(
        name="audit",
        # The audit helper's weekly cron is swapped for the review job's daily one so
        # both kinds share the REGISTERED / NOW dates.
        make_job=lambda **over: _audit_job(
            tmp_path / "repo", **{"cron": "30 8 * * *", **over}
        ),
        process="_process_audit_job",
        cycle="_audit_cycle",
        inside_age_ms=STALE_MARGIN_MS + 3_000_000,
        stale_age_ms=STALE_MARGIN_MS + 4_600_000,
        live_agent=lambda job: build_audit_agent_name(job.name, "any-run"),
    )


@pytest.fixture(params=["review", "audit"])
def kind(request: pytest.FixtureRequest, tmp_path: Path) -> Kind:
    return {"review": _review_kind, "audit": _audit_kind}[request.param](tmp_path)


@pytest.fixture
def history(tmp_path: Path) -> Path:
    return tmp_path / "history.jsonl"


@pytest.fixture
def cycle_calls(kind: Kind, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the kind's cycle with a recorder of the run_ids it is handed."""
    calls: list[str] = []

    def _cycle(job: Job, history_path: Path, *, run_id: str, **_kw: Any) -> Any:
        calls.append(run_id)
        return f"{job.name}: cycle ran", False

    monkeypatch.setattr(tick, kind.cycle, _cycle)
    return calls


def _process(
    kind: Kind, job: Job, history: Path, client: GuardClient
) -> tuple[str, bool]:
    return getattr(tick, kind.process)(
        job,
        history,
        client=client,
        now=NOW,
    )


def _seed(
    history: Path,
    job: Job,
    *,
    running_age_ms: int | None = None,
    run_id: str = "run-old",
) -> None:
    append(history, HistoryRecord(ts=REGISTERED, job=job.name, state="registered"))
    if running_age_ms is not None:
        append(
            history,
            HistoryRecord(
                ts=NOW - timedelta(milliseconds=running_age_ms),
                job=job.name,
                state="running",
                run_id=run_id,
            ),
        )


def _states(history: Path, job: Job) -> list[str]:
    return [r.state for r in read_job(history, job.name)]


def test_first_sight_registers_without_dispatch(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: registered", False)
    assert _states(history, job) == ["registered"]
    assert cycle_calls == []


def test_running_record_inside_the_kinds_window_blocks_the_tick(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    _seed(history, job, running_age_ms=kind.inside_age_ms)

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: skipped (already running)", False)
    assert _states(history, job) == ["registered", "running"]
    assert cycle_calls == []


def test_stale_running_record_is_interrupted_and_nothing_is_reaped_on_a_live_boot(
    kind: Kind, history: Path, cycle_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = kind.make_job()
    _seed(history, job, running_age_ms=kind.stale_age_ms)
    # The host booted before the stale run started, so its agents are not restored ones.
    monkeypatch.setattr(
        tick, "system_boot_epoch", lambda: (REGISTERED - timedelta(days=1)).timestamp()
    )
    client = GuardClient(panes={job.agent_name: ("blocked", "w1:p1")})

    _process(kind, job, history, client)

    stale = read_job(history, job.name)[-1]
    assert stale.state == "interrupted_unknown"
    assert stale.run_id == "run-old"
    assert stale.extra is not None
    assert stale.extra["reason"] == "stale_running_record"
    assert "reaped_agents" not in stale.extra
    assert client.closed == []


def test_stale_running_record_reaps_the_agents_a_reboot_restored(
    kind: Kind, history: Path, cycle_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `workspace: root` jobs never reap (their pane is the ambient workspace).
    job = kind.make_job(workspace="worktree")
    _seed(history, job, running_age_ms=kind.stale_age_ms)
    monkeypatch.setattr(tick, "system_boot_epoch", lambda: NOW.timestamp())
    client = GuardClient(
        panes={
            job.agent_name: ("blocked", "w1:p7"),
            "rt-unrelated-job": ("blocked", "w1:p8"),
        }
    )

    _process(kind, job, history, client)

    stale = read_job(history, job.name)[-1]
    assert stale.state == "interrupted_unknown"
    assert stale.extra is not None
    assert stale.extra["reaped_agents"] == [job.agent_name]
    assert client.closed == ["w1:p7"]


def test_live_agent_skips_the_tick_and_records_why(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    _seed(history, job)
    client = GuardClient(statuses={kind.live_agent(job): "working"})

    summary, failed = _process(kind, job, history, client)

    assert (summary, failed) == (f"{job.name}: skipped (agent already live)", False)
    skipped = read_job(history, job.name)[-1]
    assert skipped.state == "skipped"
    assert skipped.extra is not None
    assert skipped.extra["reason"] == "agent_name_live"
    assert cycle_calls == []


def test_not_due_writes_nothing(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    # Registered after this morning's 08:30 fire: nothing is due yet.
    append(
        history,
        HistoryRecord(ts=NOW - timedelta(minutes=15), job=job.name, state="registered"),
    )

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: not due", False)
    assert _states(history, job) == ["registered"]
    assert cycle_calls == []


@pytest.mark.parametrize("on_missed", ["log", "notify"])
def test_missed_occurrence_is_recorded_and_notifies_only_when_asked(
    kind: Kind, history: Path, cycle_calls: list[str], on_missed: str
) -> None:
    # The 08:30 fire is 30 minutes late; a 10 minute catch-up window misses it.
    job = kind.make_job(catch_up_minutes=10, on_missed=on_missed)
    _seed(history, job)
    client = GuardClient()

    summary, failed = _process(kind, job, history, client)

    assert (summary, failed) == (f"{job.name}: missed", False)
    missed = read_job(history, job.name)[-1]
    assert missed.state == "missed"
    assert missed.extra is not None
    assert missed.extra["reason"] == "outside_catch_up_window"
    assert missed.extra["occurrence"] == "2026-10-12T08:30:00+00:00"
    assert client.notifications == (
        [(f"herdr-routines: {job.name} missed", "outside catch-up window")]
        if on_missed == "notify"
        else []
    )
    assert cycle_calls == []


def test_due_tick_records_running_and_hands_the_run_id_to_the_cycle(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    _seed(history, job)

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: cycle ran", False)
    running = read_job(history, job.name)[-1]
    assert running.state == "running"
    assert running.run_id is not None
    assert running.extra is not None
    assert running.extra["scheduled_for"] == "2026-10-12T08:30:00+00:00"
    assert running.extra["late_seconds"] == 1800.0
    assert cycle_calls == [running.run_id]


def test_earlier_occurrences_collapse_into_one_missed_record_before_the_run(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    # Three fires (Oct 10, 11, 12) since registration: the first two collapse.
    append(
        history,
        HistoryRecord(
            ts=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
            job=job.name,
            state="registered",
        ),
    )

    _process(kind, job, history, GuardClient())

    records = read_job(history, job.name)
    assert [r.state for r in records] == ["registered", "missed", "running"]
    collapsed = records[1].extra
    assert collapsed is not None
    assert collapsed["reason"] == "collapsed_earlier_occurrences"
    assert collapsed["skipped_occurrences"] == 2
    assert collapsed["skipped_first"] == "2026-10-10T08:30:00+00:00"
    assert collapsed["skipped_last"] == "2026-10-11T08:30:00+00:00"
    assert cycle_calls == [records[2].run_id]


def test_process_job_routes_kind_review_to_the_review_flow(
    tmp_path: Path, history: Path
) -> None:
    job = _review_job(tmp_path)

    summary, failed = tick._process_job(
        job,
        history,
        client=GuardClient(),  # type: ignore[arg-type]
        now=NOW,
    )

    # First sight is `_process_review_job`'s own answer; no other kind registers a review job.
    assert (summary, failed) == (f"{job.name}: registered", False)
    assert _states(history, job) == ["registered"]
