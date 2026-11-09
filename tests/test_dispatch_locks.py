"""Acceptance tests for issue 079: the tick lock stops spanning a whole dispatch.

A long synchronous run must hold only a per-job dispatch lock, never the global
`tick.lock`, so an unrelated job is still dispatched by a tick and by a manual
`run` while job A's run is in flight — and two dispatchers of the same job are
serialized by that job lock, with the loser recording `skipped` (reason
`dispatch_in_flight`) instead of either racing or being skipped-not-queued by a
global lock miss.

Every test drives the real entry points (`cli.main(["tick"])` /
`cli.main(["run", ...])`); only the agent run itself (`tick.execute_run`) is
faked, with job A's run parked on a threading.Event so its dispatch is genuinely
open when the contender fires.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli, tick
from herdr_routines.config import RoutinesConfig
from herdr_routines.history import read_job
from herdr_routines.runner import RunOutcome
from tests.test_manual_run import FROZEN_NOW, _Client, _FrozenDatetime, _job, _register

BLOCK_TIMEOUT_S = 30.0


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _Client,
    config: RoutinesConfig,
    execute_run: Any,
) -> Path:
    """Point every real entry point at the test's tmp state dir and fakes."""
    history = tmp_path / "state" / "history.jsonl"
    monkeypatch.setattr(
        cli, "default_lock_path", lambda: tmp_path / "state" / "tick.lock"
    )
    monkeypatch.setattr(cli, "default_history_path", lambda: history)
    monkeypatch.setattr(cli, "HerdrClient", lambda: client)
    monkeypatch.setattr(cli, "datetime", _FrozenDatetime)
    monkeypatch.setattr(cli, "_load_config_or_exit", lambda args: config)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr(tick, "_live_agent_exists", lambda client, job: False)
    monkeypatch.setattr(tick, "_live_audit_agent_exists", lambda client, job: False)
    monkeypatch.setattr(tick, "execute_run", execute_run)
    return history


def _run_first_tick_in_thread(
    outcomes: list[Any],
) -> tuple[threading.Thread, list[Any]]:
    """Run one real `cli.main(["tick"])` in a daemon thread; the caller waits for the
    faked dispatch to signal, drives the contender, then releases and inspects
    *outcomes* — a tick that died saves its exception there for the assertion to
    surface, since a bare traceback inside a thread would otherwise be invisible."""

    def first_tick() -> None:
        try:
            outcomes.append(cli.main(["tick"]))
        except Exception as e:  # noqa: BLE001 — surfaced by the outcomes assertion
            outcomes.append(e)

    thread = threading.Thread(target=first_tick, daemon=True)
    thread.start()
    return thread, outcomes


def test_long_run_does_not_block_other_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While job a's dispatch is in flight for a long run, job b still dispatches
    normally: once by a scheduled tick, once by a manual run (the 2026-10-23
    `babysit-prs` silent skip cannot recur)."""
    long_job = _job(tmp_path)
    other = _job(tmp_path, name="b")
    started = threading.Event()
    release = threading.Event()
    dispatch_calls: list[str] = []

    def execute_run(run_job, client, *, run_id, **_):
        dispatch_calls.append(run_job.name)
        if run_job.name == "a":
            started.set()
            release.wait(timeout=BLOCK_TIMEOUT_S)
        return RunOutcome(state="done", run_id=run_id)

    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(long_job, other)),
        execute_run,
    )
    for job in (long_job, other):
        _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    try:
        first_thread, first = _run_first_tick_in_thread([])
        # a's dispatch is open (its `running` record is already in history) while
        # the same process proceeds to the other job, via both entry points.
        assert started.wait(timeout=BLOCK_TIMEOUT_S)
        assert cli.main(["tick"]) == 0
        assert cli.main(["run", "b"]) == 0
    finally:
        release.set()
    first_thread.join(timeout=BLOCK_TIMEOUT_S)
    assert not first_thread.is_alive()
    assert first == [0]

    # One dispatch each: the parked scheduled one for a, one cron + one manual for b.
    assert dispatch_calls == ["a", "b", "b"]
    records = read_job(history, "b")
    assert [r.state for r in records] == [
        "registered",
        "running",
        "done",
        "running",
        "done",
    ]
    # The first run must have come from the tick, not only the manual entry point.
    cron = records[1]
    assert cron.extra is not None and cron.extra["trigger"] == "cron"
    manual = records[3]
    assert manual.extra is not None and manual.extra["trigger"] == "manual"


def test_same_job_never_dispatched_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two dispatches of the same job never overlap: while one dispatch of a is in
    flight, a second tick records `skipped` (reason `dispatch_in_flight`) instead
    of dispatching a second run of it (issue 004's one-live-agent guarantee)."""
    job = _job(tmp_path)
    started = threading.Event()
    release = threading.Event()
    dispatch_calls: list[str] = []

    def execute_run(run_job, client, *, run_id, **_):
        dispatch_calls.append(run_job.name)
        started.set()
        release.wait(timeout=BLOCK_TIMEOUT_S)
        return RunOutcome(state="done", run_id=run_id)

    history = _wire(
        monkeypatch, tmp_path, _Client(), RoutinesConfig(jobs=(job,)), execute_run
    )
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    try:
        first_thread, first = _run_first_tick_in_thread([])
        # a's first dispatch is fully in flight (running record written) before the
        # contender runs: the loser must lose on the dispatch lock, not race ahead.
        assert started.wait(timeout=BLOCK_TIMEOUT_S)
        assert cli.main(["tick"]) == 0
    finally:
        release.set()
    first_thread.join(timeout=BLOCK_TIMEOUT_S)
    assert not first_thread.is_alive()
    assert first == [0]

    assert dispatch_calls == ["a"]
    records = read_job(history, job.name)
    assert [r.state for r in records] == ["registered", "running", "skipped", "done"]
    skipped = records[2]
    assert skipped.extra is not None
    assert skipped.extra["reason"] == "dispatch_in_flight"
