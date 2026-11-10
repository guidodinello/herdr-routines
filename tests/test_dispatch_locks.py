"""Acceptance tests for issue 079: the tick lock stops spanning a whole dispatch.

A long synchronous run must hold only a per-job dispatch lock, never the global
`tick.lock`, so an unrelated job is still dispatched by a tick and by a manual
`run` while job A's run is in flight. Two dispatchers of the same job are
serialized by that job lock: a tick that finds the job's run in flight skips
quietly, as before issue 079, and a dispatcher that loses the lock itself records
`skipped` (reason `dispatch_in_flight`). Neither ever consumes a cron occurrence
that no run served (issue 074). Two jobs sharing a `workspace: root` checkout are
serialized by that checkout's lock.

Every test drives the real entry points (`cli.main(["tick"])` /
`cli.main(["run", ...])`); only the agent run itself (`tick.execute_run`) is
faked, with job A's run parked on a threading.Event so its dispatch is genuinely
open when the contender fires.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli, repos, tick
from herdr_routines.auto_fix import GithubEvent
from herdr_routines.config import GateCheck, RoutinesConfig
from herdr_routines.history import (
    HistoryRecord,
    append,
    last_scheduled_terminal_run,
    read_job,
)
from herdr_routines.repos import CheckoutBusy, _ensure_checkout, checkout_lock
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
    outcomes: list[Any], argv: tuple[str, ...] = ("tick",)
) -> tuple[threading.Thread, list[Any]]:
    """Run one real `cli.main(argv)` (a tick by default) in a daemon thread; the caller
    waits for the faked dispatch to signal, drives the contender, then releases and
    inspects *outcomes* — a call that died saves its exception there for the assertion
    to surface, since a bare traceback inside a thread would otherwise be invisible."""

    def first_call() -> None:
        try:
            outcomes.append(cli.main(list(argv)))
        except Exception as e:  # noqa: BLE001 — surfaced by the outcomes assertion
            outcomes.append(e)

    thread = threading.Thread(target=first_call, daemon=True)
    thread.start()
    return thread, outcomes


def _parked_execute_run(
    started: threading.Event, release: threading.Event, calls: list[str], park: str
) -> Any:
    """A fake `execute_run` recording each job it runs and parking job *park*'s run
    until *release* is set (pass `park=""` to park nothing)."""

    def execute_run(run_job, client, *, run_id, **_):
        calls.append(run_job.name)
        if run_job.name == park:
            started.set()
            release.wait(timeout=BLOCK_TIMEOUT_S)
        return RunOutcome(state="done", run_id=run_id)

    return execute_run


def _unparked(calls: list[str]) -> Any:
    return _parked_execute_run(threading.Event(), threading.Event(), calls, park="")


def _states(history: Path, name: str = "a") -> list[str]:
    return [r.state for r in read_job(history, name)]


def _extras(history: Path, state: str, name: str = "a") -> list[dict[str, Any]]:
    return [dict(r.extra or {}) for r in read_job(history, name) if r.state == state]


def test_long_run_does_not_block_other_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While job a's dispatch is in flight for a long run, job b still dispatches
    normally: once by a scheduled tick, once by a manual run (the 2026-10-23
    `babysit-prs` silent skip cannot recur)."""
    long_job = _job(tmp_path)
    # An *unrelated* job: its own checkout, since two `workspace: root` jobs sharing one
    # are serialized by the checkout lock (AC 7, tested below).
    other_repo = tmp_path / "b-repo"
    other_repo.mkdir()
    other = _job(tmp_path, name="b", repo=other_repo)
    started = threading.Event()
    release = threading.Event()
    dispatch_calls: list[str] = []

    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(long_job, other)),
        _parked_execute_run(started, release, dispatch_calls, park="a"),
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
    """Two dispatches of the same job never overlap (issue 004's one-live-agent
    guarantee). A tick that runs while a's dispatch is in flight sees the open run and
    skips without a record, as before issue 079: a record per tick would fill history
    for the whole run. A dispatcher that decided before the run's `running` record
    existed loses the job lock instead and records `skipped` (`dispatch_in_flight`);
    see `test_dispatch_lock_loser_records_skip_without_consuming_the_occurrence`."""
    job = _job(tmp_path)
    started = threading.Event()
    release = threading.Event()
    dispatch_calls: list[str] = []

    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job,)),
        _parked_execute_run(started, release, dispatch_calls, park="a"),
    )
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    try:
        first_thread, first = _run_first_tick_in_thread([])
        # a's first dispatch is fully in flight (running record written) before the
        # contenders run.
        assert started.wait(timeout=BLOCK_TIMEOUT_S)
        assert cli.main(["tick"]) == 0
        assert cli.main(["tick"]) == 0
    finally:
        release.set()
    first_thread.join(timeout=BLOCK_TIMEOUT_S)
    assert not first_thread.is_alive()
    assert first == [0]

    assert dispatch_calls == ["a"]
    assert _states(history) == ["registered", "running", "done"]


def test_dispatch_lock_loser_records_skip_without_consuming_the_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the job lock exists for: another dispatcher of a won the lock but has
    not written its `running` record yet, so this tick decides the cron occurrence is
    due and then loses the lock. It records `skipped` (`dispatch_in_flight`, trigger
    cron) and does not dispatch. That record never counts as the occurrence's terminal,
    so once the lock is free the occurrence still fires."""
    job = _job(tmp_path)
    dispatch_calls: list[str] = []
    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job,)),
        _unparked(dispatch_calls),
    )
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    with tick.job_lock(history, "a") as held:
        assert held
        assert cli.main(["tick"]) == 0
    assert dispatch_calls == []
    assert _states(history) == ["registered", "skipped"]
    assert _extras(history, "skipped") == [
        {"reason": "dispatch_in_flight", "trigger": "cron"}
    ]

    assert cli.main(["tick"]) == 0
    assert dispatch_calls == ["a"]
    assert _states(history) == ["registered", "skipped", "running", "done"]


def test_tick_during_a_manual_run_does_not_consume_the_cron_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue 074 AC 3 under issue 079. While a manual run of a is in flight (and its
    agent is live in herdr), ticks keep running and see a's cron occurrence due. They
    must write nothing: neither a trigger-less `agent_name_live` skip nor any other
    record that `schedule.decide` would count as the occurrence's terminal. Once the
    manual run ends, the next tick still dispatches the cron occurrence."""
    job = _job(tmp_path)
    started = threading.Event()
    release = threading.Event()
    dispatch_calls: list[str] = []
    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job,)),
        _parked_execute_run(started, release, dispatch_calls, park="a"),
    )
    agent_live = threading.Event()
    monkeypatch.setattr(tick, "_live_agent_exists", lambda c, j: agent_live.is_set())
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    try:
        manual_thread, manual = _run_first_tick_in_thread([], ("run", "a"))
        assert started.wait(timeout=BLOCK_TIMEOUT_S)
        agent_live.set()
        assert cli.main(["tick"]) == 0
        assert cli.main(["tick"]) == 0
    finally:
        agent_live.clear()
        release.set()
    manual_thread.join(timeout=BLOCK_TIMEOUT_S)
    assert manual == [0]
    assert _states(history) == ["registered", "running", "done"]

    assert cli.main(["tick"]) == 0
    assert dispatch_calls == ["a", "a"]
    assert _states(history) == ["registered", "running", "done", "running", "done"]
    assert [e["trigger"] for e in _extras(history, "running")] == ["manual", "cron"]


def test_refused_manual_run_does_not_consume_the_cron_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual `run` refused because a's dispatch lock is held exits 1 and records a
    `dispatch_in_flight` skip stamped `trigger: manual`, which is off-schedule: the
    cron occurrence still fires on the next tick."""
    job = _job(tmp_path)
    dispatch_calls: list[str] = []
    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job,)),
        _unparked(dispatch_calls),
    )
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    with tick.job_lock(history, "a") as held:
        assert held
        assert cli.main(["run", "a"]) == 1
    assert _extras(history, "skipped") == [
        {"reason": "dispatch_in_flight", "trigger": "manual"}
    ]

    assert cli.main(["tick"]) == 0
    assert dispatch_calls == ["a"]
    assert _extras(history, "running")[0]["trigger"] == "cron"


def test_stale_record_is_not_reaped_while_the_job_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `running` record can look stale while its run is alive (a retried run keeps
    the tick-start `ts`). The held job lock proves the run is alive, so the tick must
    not write `interrupted_unknown` over it; once the lock is free, the same record is
    reaped as before."""
    job = _job(tmp_path)
    client = _Client()
    config = RoutinesConfig(jobs=(job,))
    history = _wire(monkeypatch, tmp_path, client, config, _unparked([]))
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))
    append(
        history,
        HistoryRecord(
            ts=FROZEN_NOW - timedelta(hours=1),
            job="a",
            state="running",
            run_id="a-old",
            extra={"trigger": "cron"},
        ),
    )

    with tick.job_lock(history, "a") as held:
        assert held
        outcome = tick.run_tick(config, history, client=client, now=FROZEN_NOW)  # type: ignore[arg-type]
    assert outcome.summaries == ("a: skipped (already running)",)
    assert _states(history) == ["registered", "running"]

    tick.run_tick(config, history, client=client, now=FROZEN_NOW)  # type: ignore[arg-type]
    reaped = read_job(history, "a")[2]
    assert (reaped.state, reaped.run_id) == ("interrupted_unknown", "a-old")


def test_skipped_event_dispatch_leaves_the_event_unseen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The firing poll's watermark lands only when the dispatch really starts. A poll
    whose dispatch loses the job lock must not mark the event seen, or it would never
    fire; the next tick fires it."""
    job = _job(
        tmp_path,
        cron=None,
        trigger_event=GateCheck(
            kind="github_event", query="prs:labels", repo="o/r", match={"label": "go"}
        ),
    )
    dispatch_calls: list[str] = []
    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job,)),
        _unparked(dispatch_calls),
    )
    event = GithubEvent(
        event_id="evt-1", number=1, title="t", head_ref=None, label="go", payload={}
    )
    monkeypatch.setattr(tick, "poll_github_event", lambda gh, **kw: [event])
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    with tick.job_lock(history, "a") as held:
        assert held
        assert cli.main(["tick"]) == 0
    assert dispatch_calls == []
    assert _extras(history, "skipped") == [
        {"reason": "dispatch_in_flight", "trigger": "event"}
    ]
    assert not any("last_seen_event_ids" in e for e in _extras(history, "done"))

    assert cli.main(["tick"]) == 0
    assert dispatch_calls == ["a"]
    assert _extras(history, "running")[0]["event_id"] == "evt-1"


def test_manual_run_refused_by_a_live_agent_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual or request run that the live-agent guard stops did not run, so it is a
    refusal: rc 1, and a notification for the detached `/job` request."""
    job = _job(tmp_path, cron=None, request=True)
    client = _Client()
    dispatch_calls: list[str] = []
    history = _wire(
        monkeypatch,
        tmp_path,
        client,
        RoutinesConfig(jobs=(job,)),
        _unparked(dispatch_calls),
    )
    monkeypatch.setattr(tick, "_live_agent_exists", lambda c, j: True)

    assert cli.main(["run", "a", "--trigger", "request"]) == 1
    assert cli.main(["run", "a"]) == 1

    assert dispatch_calls == []
    assert _extras(history, "skipped") == [
        {"reason": "agent_name_live", "trigger": "request"},
        {"reason": "agent_name_live", "trigger": "manual"},
    ]
    assert [title for title, _ in client.notifications] == [
        "herdr-routines: a request refused"
    ]
    assert "agent" in (client.notifications[0][1] or "")


def test_manual_run_trusts_the_job_lock_over_an_open_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh `running` record with the job lock free belongs to a dead process (flock
    dies with it). A manual retry runs instead of being refused until the record goes
    stale, as `run` did before issue 079."""
    job = _job(tmp_path)
    dispatch_calls: list[str] = []
    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job,)),
        _unparked(dispatch_calls),
    )
    append(
        history,
        HistoryRecord(
            ts=FROZEN_NOW - timedelta(seconds=30),
            job="a",
            state="running",
            run_id="a-crashed",
            extra={"trigger": "manual"},
        ),
    )

    assert cli.main(["run", "a"]) == 0
    assert dispatch_calls == ["a"]


def test_root_jobs_sharing_a_checkout_never_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 7: two `workspace: root` jobs on one checkout. While a's run works in it, b's
    tick dispatch and b's manual run are both refused with `checkout_busy`, and neither
    refusal consumes b's cron occurrence: b runs once a's run releases the checkout."""
    job_a = _job(tmp_path)
    job_b = _job(tmp_path, name="b")
    started = threading.Event()
    release = threading.Event()
    dispatch_calls: list[str] = []
    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job_a, job_b)),
        _parked_execute_run(started, release, dispatch_calls, park="a"),
    )
    for job in (job_a, job_b):
        _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    try:
        first_thread, first = _run_first_tick_in_thread([])
        assert started.wait(timeout=BLOCK_TIMEOUT_S)
        assert cli.main(["tick"]) == 0
        # A second contended tick leaves the occurrence owed, but does not write a second
        # identical `checkout_busy` record: one skip already reports the contention.
        assert cli.main(["tick"]) == 0
        assert cli.main(["run", "b"]) == 1
    finally:
        release.set()
    first_thread.join(timeout=BLOCK_TIMEOUT_S)
    assert first == [0]

    # The first tick reached b after a's run released the checkout, and ran it.
    assert dispatch_calls == ["a", "b"]
    assert _states(history, "b") == [
        "registered",
        "skipped",
        "skipped",
        "running",
        "done",
    ]
    assert _extras(history, "skipped", "b") == [
        {"reason": "checkout_busy", "trigger": "cron"},
        {"reason": "checkout_busy", "trigger": "manual"},
    ]


def test_checkout_sync_waits_for_another_jobs_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 7, worktree mode: `ensure_repo`'s fetch and fast-forward run under the
    checkout lock. Another thread's dispatch holding it makes the sync fail as
    `CheckoutBusy` (a RuntimeError, so callers treat it as a sync failure) once the
    bounded wait is spent; the holder's own nested sync re-enters the lock."""
    synced: list[Path] = []
    monkeypatch.setattr(repos, "CHECKOUT_LOCK_WAIT_S", 0.1)
    monkeypatch.setattr(repos, "CHECKOUT_LOCK_POLL_S", 0.01)
    monkeypatch.setattr(
        repos, "_fetch_and_fast_forward", lambda dest, *, base: synced.append(dest)
    )
    checkout = tmp_path / "repo"
    holding = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def other_dispatch() -> None:
        try:
            with checkout_lock(checkout) as acquired:
                assert acquired
                # Re-entrant in the holding thread: a root run's own sync.
                _ensure_checkout(None, checkout, base="main")
                holding.set()
                release.wait(timeout=BLOCK_TIMEOUT_S)
        except BaseException as e:  # noqa: BLE001 — surfaced by the errors assertion
            errors.append(e)
            holding.set()

    thread = threading.Thread(target=other_dispatch, daemon=True)
    thread.start()
    try:
        assert holding.wait(timeout=BLOCK_TIMEOUT_S)
        with pytest.raises(CheckoutBusy, match="locked by another dispatch"):
            _ensure_checkout(None, checkout, base="main")
    finally:
        release.set()
    thread.join(timeout=BLOCK_TIMEOUT_S)
    assert errors == []

    _ensure_checkout(None, checkout, base="main")
    assert synced == [checkout, checkout]


def test_checkout_busy_during_a_worktree_sync_is_a_skip_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree-mode sync that meets a `workspace: root` holder raises `CheckoutBusy`
    after its bounded wait. That is contention, not a failed run: the dispatch records
    `skipped`/`checkout_busy` under the occurrence's trigger, closes the `running` record
    it wrote, leaves the cron occurrence owed, and sends no failure notification."""
    job = _job(tmp_path, workspace="worktree")
    client = _Client()

    def busy_execute_run(job: Any, client: Any, *, run_id: str, **_: Any) -> Any:
        raise CheckoutBusy(f"checkout {job.repo} is locked by another dispatch")

    history = _wire(
        monkeypatch,
        tmp_path,
        client,
        RoutinesConfig(jobs=(job,)),
        busy_execute_run,
    )
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    assert cli.main(["tick"]) == 0

    records = read_job(history, "a")
    assert [r.state for r in records] == ["registered", "running", "skipped"]
    running, skipped = records[-2:]
    assert skipped.run_id == running.run_id
    assert skipped.extra == {"reason": "checkout_busy", "trigger": "cron"}
    # Contention is not a run: the occurrence stays owed and nothing pages anyone.
    assert last_scheduled_terminal_run(history, "a") is None
    assert client.notifications == []


def test_dispatch_records_use_the_clock_at_dispatch_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue 079: a tick may decide a job is due and reach its dispatch much later, so the
    `running`/terminal timestamps come from the clock at dispatch time, not tick start."""
    job = _job(tmp_path)
    dispatch_now = FROZEN_NOW + timedelta(hours=2)

    class _DispatchClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return dispatch_now

    history = _wire(
        monkeypatch,
        tmp_path,
        _Client(),
        RoutinesConfig(jobs=(job,)),
        _unparked([]),
    )
    monkeypatch.setattr(tick, "datetime", _DispatchClock)
    _register(history, job, at=FROZEN_NOW - timedelta(days=2))

    assert cli.main(["tick"]) == 0

    records = read_job(history, "a")
    assert [r.state for r in records] == ["registered", "running", "done"]
    assert [r.ts for r in records[1:]] == [dispatch_now, dispatch_now]
