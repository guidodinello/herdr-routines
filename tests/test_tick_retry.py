"""Tick-based retries of a failed scheduled run (issue 088).

Each job kind's first repo sync fails (`Could not resolve host`, the real `audit-type-health`
failure) and a later tick runs the retry. The clock is frozen (`tests/retry_clock.py`), so
"due 1 tick later" is an exact instant. The audit kind, which needs a real git repo, is
covered in tests/test_tick_dispatch.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.config import Job, RoutinesConfig
from herdr_routines.history import HistoryRecord, read_job
from herdr_routines.retry import find_pending_retry, retry_due
from tests.dispatch_modules import patch_dispatch
from tests.retry_clock import FrozenClock
from tests.test_tick import (
    FakeClient,
    FakeFullClient,
    FakePipelineClient,
    MockGhClient,
    MockSubprocess,
    _base_gate_job,
    make_auto_fix_job,
    make_job,
    make_pipeline_job,
)

DAILY = "0 3 * * *"
REGISTER = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
# 30 s after the 03:00 occurrence, inside a tick that started on the boundary.
FIRE = datetime(2026, 11, 20, 3, 0, 30, tzinfo=UTC)
# The first tick boundary after FIRE: "1 tick later".
RETRY = datetime(2026, 11, 20, 3, 5, tzinfo=UTC)
TICK = timedelta(minutes=5)


class FlakySync:
    """`ensure_repo` that loses its first `failures` DNS lookups, then works."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, job: Job) -> Path:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("fatal: Could not resolve host: github.com")
        return job.repo


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    patch_dispatch(monkeypatch, "pipeline_unit_active", lambda run_id: False)
    patch_dispatch(
        monkeypatch,
        "default_worktrees_root",
        lambda *a, **k: tmp_path / "home" / ".herdr" / "worktrees" / "herdr-routines",
    )
    return tmp_path / "state" / "history.jsonl"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    return FrozenClock(monkeypatch, REGISTER)


def _flaky(monkeypatch: pytest.MonkeyPatch, failures: int) -> FlakySync:
    sync = FlakySync(failures)
    patch_dispatch(monkeypatch, "ensure_repo", sync)
    monkeypatch.setattr("herdr_routines.runner.ensure_repo", sync)
    return sync


def _states(history: Path, job: Job) -> list[tuple[str, object]]:
    return [
        (r.state, (r.extra or {}).get("reason")) for r in read_job(history, job.name)
    ]


@dataclass(frozen=True)
class Kind:
    name: str
    build: Callable[[Path, pytest.MonkeyPatch], tuple[Job, Any]]
    # What the retry tick's summary says when the retry went through.
    retry_summary: str


def _routine(tmp_path: Path, mp: pytest.MonkeyPatch) -> tuple[Job, Any]:
    job = make_job(tmp_path, cron=DAILY)
    (tmp_path / ".git").mkdir(exist_ok=True)
    return job, FakeClient(settle_status="idle")


def _gated_pr(tmp_path: Path, mp: pytest.MonkeyPatch) -> tuple[Job, Any]:
    patch_dispatch(mp, "RealGhClient", MockGhClient)
    patch_dispatch(mp, "subprocess", MockSubprocess())
    (tmp_path / ".git").mkdir(exist_ok=True)
    return make_auto_fix_job(tmp_path, cron=DAILY), FakeFullClient()


def _gated_base(tmp_path: Path, mp: pytest.MonkeyPatch) -> tuple[Job, Any]:
    from herdr_routines.auto_fix import GateOutcome

    mp.setattr(
        "subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    patch_dispatch(
        mp,
        "run_checks",
        lambda checks, *, cwd, **kw: GateOutcome(
            passed=True, results=(), combined_output=""
        ),
    )
    (tmp_path / ".git").mkdir(exist_ok=True)
    return replace(_base_gate_job(tmp_path), kind="gated", cron=DAILY), FakeFullClient()


def _pipeline(tmp_path: Path, mp: pytest.MonkeyPatch) -> tuple[Job, Any]:
    patch_dispatch(mp, "launch_pipeline", lambda argv, *, timeout_s=30.0: (0, "", ""))
    (tmp_path / ".git").mkdir(exist_ok=True)
    return make_pipeline_job(tmp_path, cron=DAILY), FakePipelineClient()


KINDS = [
    Kind("routine", _routine, "a: done"),
    Kind("gated-pr", _gated_pr, "enumerated=0"),
    Kind("gated-base", _gated_base, "a: done (gate passed)"),
    Kind("pipeline", _pipeline, "dispatched"),
]


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_first_sync_fails_and_the_retry_on_a_later_tick_passes(
    kind: Kind,
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, client = kind.build(tmp_path, monkeypatch)
    sync = _flaky(monkeypatch, failures=1)
    config = RoutinesConfig(jobs=(job,))

    clock.tick(config, state, client, REGISTER)
    failed = clock.tick(config, state, client, FIRE)
    assert failed.any_job_failed
    assert "repo_sync_failed" in failed.summaries[0]

    # The failure is its own record, and the retry is queued for the next tick.
    failure = next(r for r in read_job(state, job.name) if r.state == "failed")
    assert (failure.extra or {})["reason"] == "repo_sync_failed"
    pending = find_pending_retry(read_job(state, job.name))
    assert pending is not None
    assert pending.due_at == RETRY
    assert pending.record.extra is not None
    assert pending.record.extra["retry_after_ticks"] == 1
    assert pending.record.extra["retry_eligible_reason"] == "repo_sync_failed"

    # Not due yet: nothing runs, and the tick did not sleep waiting for it.
    waiting = clock.tick(config, state, client, FIRE + timedelta(minutes=1))
    assert not waiting.any_job_failed
    assert sync.calls == 1

    retried = clock.tick(config, state, client, RETRY)
    assert not retried.any_job_failed
    assert kind.retry_summary in retried.summaries[0]
    assert sync.calls == 2

    records = read_job(state, job.name)
    assert [r.state for r in records if r.state == "failed"] == ["failed"]
    retry_run = [r for r in records if r.state == "running"][-1]
    assert retry_run.run_id != failure.run_id
    assert retry_run.extra is not None
    assert retry_run.extra["attempt"] == 1
    assert retry_run.extra["retried_from_run_id"] == failure.run_id
    assert retry_run.extra["scheduled_for"] == "2026-11-20T03:00:00+00:00"
    assert retry_run.extra["trigger"] == "cron"
    assert find_pending_retry(records) is None


def test_retries_back_off_one_two_then_four_ticks(
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, client = _routine(tmp_path, monkeypatch)
    job = replace(job, retry_attempts=3)
    sync = _flaky(monkeypatch, failures=10)
    config = RoutinesConfig(jobs=(job,))

    clock.tick(config, state, client, REGISTER)
    due_ats: list[datetime] = []
    at = FIRE
    for _ in range(3):
        clock.tick(config, state, client, at)
        pending = find_pending_retry(read_job(state, job.name))
        assert pending is not None
        due_ats.append(pending.due_at)
        at = pending.due_at
    clock.tick(config, state, client, at)  # the third retry fails: none left

    assert due_ats == [
        RETRY,  # +1 tick after the failure at 03:00:30
        RETRY + 2 * TICK,  # failed at 03:05, +2 ticks
        RETRY + 6 * TICK,  # failed at 03:15 (= RETRY + 2 ticks), +4 ticks
    ]
    assert due_ats[2] == datetime(2026, 11, 20, 3, 35, tzinfo=UTC)
    assert sync.calls == 4  # first try + 3 retries, no fifth
    assert find_pending_retry(read_job(state, job.name)) is None
    clock.tick(config, state, client, at + 10 * TICK)
    assert sync.calls == 4


def test_retry_due_counts_whole_ticks_from_the_next_boundary() -> None:
    failed_at = datetime(2026, 11, 20, 3, 4, 59, tzinfo=UTC)
    assert retry_due(failed_at, 0) == (datetime(2026, 11, 20, 3, 5, tzinfo=UTC), 1)
    on_boundary = datetime(2026, 11, 20, 3, 5, tzinfo=UTC)
    assert retry_due(on_boundary, 1) == (datetime(2026, 11, 20, 3, 15, tzinfo=UTC), 2)
    assert retry_due(on_boundary, 2) == (datetime(2026, 11, 20, 3, 25, tzinfo=UTC), 4)


def test_a_post_agent_reason_does_not_retry_under_the_defaults(
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flaky(monkeypatch, failures=0)
    job = make_job(tmp_path, cron=DAILY)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(fail_at="agent_prompt_wait")

    clock.tick(config, state, client, REGISTER)
    outcome = clock.tick(config, state, client, FIRE)

    assert outcome.summaries == ("a: failed (agent_prompt_failed)",)
    assert "scheduled" not in [s for s, _ in _states(state, job)]
    clock.tick(config, state, client, RETRY)
    assert [s for s, _ in _states(state, job)].count("running") == 1


def test_a_post_agent_reason_retries_when_retry_on_opts_in(
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flaky(monkeypatch, failures=0)
    job = make_job(tmp_path, cron=DAILY, retry_on=("agent_prompt_failed",))
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(fail_at="agent_prompt_wait")

    clock.tick(config, state, client, REGISTER)
    clock.tick(config, state, client, FIRE)
    clock.tick(config, state, client, RETRY)

    failures = [r for r in read_job(state, job.name) if r.state == "failed"]
    assert len(failures) == 2
    assert (failures[1].extra or {})["attempt"] == 1


def test_retry_attempts_zero_opts_out(
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, client = _routine(tmp_path, monkeypatch)
    job = replace(job, retry_attempts=0)
    sync = _flaky(monkeypatch, failures=1)
    config = RoutinesConfig(jobs=(job,))

    clock.tick(config, state, client, REGISTER)
    clock.tick(config, state, client, FIRE)
    clock.tick(config, state, client, RETRY)

    assert sync.calls == 1
    assert "scheduled" not in [s for s, _ in _states(state, job)]


def test_a_pending_retry_is_dropped_when_the_next_occurrence_comes_first(
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every 5 minutes: the retry is due at 03:05, and so is the next occurrence. The
    occurrence wins, the retry is dropped (and recorded as such), and the fresh run is a
    first attempt, not the retry."""
    job, client = _routine(tmp_path, monkeypatch)
    job = replace(job, cron="*/5 * * * *")
    sync = _flaky(monkeypatch, failures=1)
    config = RoutinesConfig(jobs=(job,))

    clock.tick(config, state, client, REGISTER)
    clock.tick(config, state, client, FIRE)
    assert find_pending_retry(read_job(state, job.name)) is not None

    outcome = clock.tick(config, state, client, RETRY)

    assert not outcome.any_job_failed
    assert sync.calls == 2
    records = read_job(state, job.name)
    dropped = [r for r in records if (r.extra or {}).get("reason") == "retry_dropped"]
    assert len(dropped) == 1
    assert dropped[0].state == "skipped"
    fresh = [r for r in records if r.state == "running"][-1]
    assert fresh.extra is not None
    assert fresh.extra["scheduled_for"] == "2026-11-20T03:05:00+00:00"
    assert "retried_from_run_id" not in fresh.extra
    assert find_pending_retry(records) is None


def test_a_pending_retry_is_not_a_missed_occurrence(
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed attempt consumed its occurrence, so waiting for the retry never reports it
    `missed`, and the retry never writes one either."""
    job, client = _routine(tmp_path, monkeypatch)
    _flaky(monkeypatch, failures=1)
    config = RoutinesConfig(jobs=(job,))

    clock.tick(config, state, client, REGISTER)
    clock.tick(config, state, client, FIRE)
    clock.tick(config, state, client, FIRE + timedelta(minutes=2))
    clock.tick(config, state, client, RETRY)

    assert "missed" not in [s for s, _ in _states(state, job)]


def test_the_retry_marker_is_a_nonterminal_record(
    state: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, client = _routine(tmp_path, monkeypatch)
    _flaky(monkeypatch, failures=1)
    config = RoutinesConfig(jobs=(job,))
    clock.tick(config, state, client, REGISTER)
    clock.tick(config, state, client, FIRE)

    marker = read_job(state, job.name)[-1]
    assert isinstance(marker, HistoryRecord)
    assert marker.state == "scheduled"
    assert (marker.extra or {})["reason"] == "retry_pending"


def test_validate_timeout_budget_does_not_scale_with_retry_attempts(
    tmp_path: Path,
) -> None:
    """A retry runs on a later tick, so it adds nothing to one tick's worst case (issue 088)."""
    from herdr_routines.cli import _check_systemd_timeout

    unit = tmp_path / "x.service"
    # One routine: start 10 s + run 100 s = 110 s, plus the 300 s margin.
    unit.write_text("[Service]\nTimeoutStartSec=410\n")
    for attempts in (0, 3):
        job = replace(
            make_job(tmp_path),
            start_timeout_ms=10_000,
            timeout_ms=100_000,
            retry_attempts=attempts,
        )
        assert _check_systemd_timeout(RoutinesConfig(jobs=(job,)), unit) == []
    unit.write_text("[Service]\nTimeoutStartSec=409\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=(job,)), unit) != []
