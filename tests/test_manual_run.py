"""Acceptance tests for issue 074: `herdr-routines run` takes the scheduled path.

Every test drives a real entry point (`cli.main` / `run_tick`); only the agent run itself
(`tick.execute_run`), `gh` and the herdr client are faked.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli, tick
from herdr_routines.auto_fix import GateOutcome
from herdr_routines.config import AuditSpec, GateCheck, Job, RoutinesConfig
from herdr_routines.herdr import HerdrCliError
from herdr_routines.history import (
    HistoryRecord,
    append,
    last_scheduled_terminal_run,
    read_job,
)
from herdr_routines.runner import RunOutcome

# 10:20 on a day when the hourly cron last fired at 10:00.
FROZEN_NOW = datetime(2026, 10, 20, 10, 20, tzinfo=UTC)
OCCURRENCE = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return FROZEN_NOW


class _Client:
    """The HerdrClient surface a dispatch touches before any agent exists."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str | None]] = []
        self.tab_creates = 0

    def agent_statuses(self) -> dict[str, str]:
        return {}

    def notification_show(self, title: str, *, body: str | None = None, sound="none"):
        self.notifications.append((title, body))

    def tab_create(self, *, cwd: str, label: str | None = None) -> str:
        self.tab_creates += 1
        raise HerdrCliError("no panes in this test", exit_code=1)


def _job(tmp_path: Path, **overrides: Any) -> Job:
    job = Job(
        name="a",
        enabled=True,
        cron="0 * * * *",
        repo=tmp_path,
        workspace="root",
        base="main",
        agent_kind="claude",
        model="primary-model",
        prompt="report to $ROUTINE_REPORT",
        timeout_ms=5_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )
    return replace(job, **overrides)


@pytest.fixture
def history(tmp_path: Path) -> Path:
    """The state dir is pinned to tmp_path/state by conftest, so this is the file
    `herdr-routines run` appends to."""
    return tmp_path / "state" / "history.jsonl"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    fake = _Client()
    monkeypatch.setattr(cli, "HerdrClient", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "default_lock_path", lambda: tmp_path / "tick.lock")
    monkeypatch.setattr(cli, "datetime", _FrozenDatetime)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr(tick, "_live_agent_exists", lambda client, job: False)
    monkeypatch.setattr(tick, "_live_audit_agent_exists", lambda client, job: False)


def _run_cli(monkeypatch: pytest.MonkeyPatch, job: Job, *extra: str) -> int:
    config = RoutinesConfig(jobs=(job,))
    monkeypatch.setattr(cli, "_load_config_or_exit", lambda args: config)
    return cli.main(["run", job.name, *extra])


def _register(history: Path, job: Job, *, at: datetime) -> None:
    append(history, HistoryRecord(ts=at, job=job.name, state="registered"))


def test_manual_run_uses_fallback_model(
    tmp_path: Path,
    history: Path,
    client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(tmp_path, fallback_model="fallback-model")
    models: list[str | None] = []

    def fake_execute_run(job: Job, client: Any, *, run_id: str, **_: Any) -> RunOutcome:
        models.append(job.model)
        if job.model == "primary-model":
            return RunOutcome(state="failed", run_id=run_id, reason="quota_exhausted")
        return RunOutcome(state="done", run_id=run_id)

    monkeypatch.setattr(tick, "execute_run", fake_execute_run)

    assert _run_cli(monkeypatch, job) == 0

    assert models == ["primary-model", "fallback-model"]
    records = read_job(history, "a")
    retry = next(
        r for r in records if r.extra and r.extra.get("reason") == "fallback_retry"
    )
    assert retry.extra is not None and retry.extra["trigger"] == "manual"
    assert records[-1].state == "done"
    assert records[-1].extra is not None and records[-1].extra["trigger"] == "manual"
    # A run that needed the fallback is a "finding", which the default policy announces.
    assert [n[0] for n in client.notifications] == ["herdr-routines: a done"]
    assert client.notifications[0][1] == "via fallback_model=fallback-model"


def test_manual_run_records_history_and_notifies(
    tmp_path: Path,
    history: Path,
    client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(tmp_path, notify_policy="on-failure")
    monkeypatch.setattr(
        tick,
        "execute_run",
        lambda job, client, *, run_id, **_: RunOutcome(
            state="failed", run_id=run_id, reason="agent_prompt_failed"
        ),
    )

    assert _run_cli(monkeypatch, job, "--run-id", "a-manual") == 1

    running, terminal = read_job(history, "a")
    assert (running.state, running.run_id) == ("running", "a-manual")
    assert (terminal.state, terminal.run_id) == ("failed", "a-manual")
    assert running.extra is not None and running.extra["trigger"] == "manual"
    assert terminal.extra is not None and terminal.extra["trigger"] == "manual"
    assert terminal.extra["reason"] == "agent_prompt_failed"
    assert client.notifications == [("herdr-routines: a failed", "agent_prompt_failed")]

    # A failure that the policy excludes stays quiet, but is still recorded.
    quiet = _job(tmp_path, notify_policy="on-failure")
    monkeypatch.setattr(
        tick,
        "execute_run",
        lambda job, client, *, run_id, **_: RunOutcome(state="done", run_id=run_id),
    )
    client.notifications.clear()
    assert _run_cli(monkeypatch, quiet, "--run-id", "a-manual-2") == 0
    assert client.notifications == []
    assert read_job(history, "a")[-1].state == "done"


def test_manual_run_does_not_consume_scheduled_occurrence(
    tmp_path: Path,
    history: Path,
    client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manual terminal record lands after the 10:00 occurrence. If `decide` were fed it,
    the tick at 10:25 would see nothing since 10:20 and report `not due`."""
    job = _job(tmp_path)
    _register(history, job, at=datetime(2026, 10, 19, tzinfo=UTC))
    append(
        history,
        HistoryRecord(
            ts=datetime(2026, 10, 20, 9, 5, tzinfo=UTC),
            job="a",
            state="done",
            run_id="a-20261020T090000Z",
            extra={"scheduled_for": "2026-10-20T09:00:00+00:00"},
        ),
    )
    monkeypatch.setattr(
        tick,
        "execute_run",
        lambda job, client, *, run_id, **_: RunOutcome(state="done", run_id=run_id),
    )

    assert _run_cli(monkeypatch, job) == 0
    manual = read_job(history, "a")[-1]
    assert manual.ts == FROZEN_NOW and manual.state == "done"

    outcome = tick.run_tick(
        RoutinesConfig(jobs=(job,)),
        history,
        client=client,  # type: ignore[arg-type]
        now=FROZEN_NOW + timedelta(minutes=5),
    )

    assert outcome.summaries == ("a: done",)
    records = read_job(history, "a")
    assert not [r for r in records if r.state == "missed"]
    scheduled_run = next(
        r
        for r in records
        if r.state == "running" and (r.extra or {}).get("trigger") is None
    )
    assert scheduled_run.run_id == "a-20261020T100000Z"
    assert scheduled_run.extra is not None
    assert scheduled_run.extra["scheduled_for"] == OCCURRENCE.isoformat()


def test_last_scheduled_terminal_run_ignores_manual_runs(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    base = datetime(2026, 10, 20, 9, tzinfo=UTC)

    def rec(minute: int, state: str, run_id: str | None, **extra: Any) -> None:
        append(
            path,
            HistoryRecord(
                ts=base + timedelta(minutes=minute),
                job="a",
                state=state,  # type: ignore[arg-type]
                run_id=run_id,
                extra=extra or None,
            ),
        )

    rec(0, "running", "a-sched", scheduled_for="x")
    rec(1, "done", "a-sched")
    # A gated manual run: its terminal record never carries the trigger, only its
    # `running` record does. The stale reaper's record reuses the run_id too.
    rec(10, "running", "a-man", trigger="manual")
    rec(11, "done", "a-man", gate="passed")
    rec(12, "interrupted_unknown", "a-man", reason="stale_running_record")
    rec(13, "failed", "a-tagged", trigger="manual")
    last = last_scheduled_terminal_run(path, "a")
    assert last is not None and last.run_id == "a-sched"

    # Resuming a *scheduled* run id manually must not un-count its original terminal.
    rec(20, "running", "a-sched", trigger="manual")
    last = last_scheduled_terminal_run(path, "a")
    assert last is not None and last.run_id == "a-sched"
    assert last.ts == base + timedelta(minutes=1)


@pytest.mark.parametrize("checks_pass", [True, False])
def test_manual_run_dispatches_by_kind(
    tmp_path: Path,
    history: Path,
    client: _Client,
    monkeypatch: pytest.MonkeyPatch,
    checks_pass: bool,
) -> None:
    def fail_execute_run(*a: Any, **kw: Any) -> RunOutcome:
        raise AssertionError("gated and review jobs never go through execute_run")

    monkeypatch.setattr(tick, "execute_run", fail_execute_run)

    # gated: the checks run first; the fix worker only starts when they fire.
    monkeypatch.setattr(
        tick.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    monkeypatch.setattr(
        tick,
        "run_checks",
        lambda checks, *, cwd, **kw: GateOutcome(
            passed=checks_pass,
            results=(),
            combined_output="" if checks_pass else "boom",
        ),
    )
    gated = _job(
        tmp_path,
        kind="gated",
        target="base",
        checks=(GateCheck(kind="command", command="true"),),
    )
    _run_cli(monkeypatch, gated, "--run-id", "a-manual")
    records = read_job(history, "a")
    assert records[0].state == "running"
    assert records[0].extra is not None and records[0].extra["trigger"] == "manual"
    terminal = records[-1]
    assert terminal.run_id == "a-manual"
    assert terminal.extra is not None
    if checks_pass:
        assert terminal.state == "done"
        assert terminal.extra["gate"] == "passed"
        assert client.tab_creates == 0
    else:
        assert client.tab_creates == 1  # the fix worker was started
        assert terminal.extra["gate"] == "failed"

    # review: the review cycle runs (here it stops at gh auth).
    class _NoAuthGh:
        def api_user(self) -> str:
            raise RuntimeError("gh auth missing")

    monkeypatch.setattr(tick, "RealGhClient", _NoAuthGh)
    review = _job(tmp_path, name="r", kind="review", target="pr", select=("author",))
    assert _run_cli(monkeypatch, review, "--run-id", "r-manual") == 1
    review_terminal = read_job(history, "r")[-1]
    assert review_terminal.run_id == "r-manual"
    assert review_terminal.extra is not None
    assert review_terminal.extra["reason"] == "gh_auth_missing"


def _kinds(tmp_path: Path) -> dict[str, Job]:
    return {
        "routine": _job(tmp_path),
        "gated": _job(
            tmp_path,
            kind="gated",
            target="base",
            checks=(GateCheck(kind="command", command="true"),),
        ),
        "review": _job(tmp_path, kind="review", target="pr", select=("author",)),
        "audit": _job(tmp_path, kind="audit", audit=AuditSpec(command="true")),
        "pipeline": _job(
            tmp_path,
            kind="pipeline",
            prompt_file="p.md",
            deadline_ms=1_000_000,
        ),
    }


@pytest.mark.parametrize("kind", ["routine", "gated", "review", "audit", "pipeline"])
def test_tick_and_manual_run_share_dispatch(
    tmp_path: Path,
    history: Path,
    client: _Client,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Both entry points reach the one dispatch function, and nothing else dispatches: a
    stubbed `dispatch_occurrence` is the only thing that runs in either."""
    job = _kinds(tmp_path)[kind]
    calls: list[tuple[str, tick.Occurrence]] = []

    def spy(job: Job, history_path: Path, occ: tick.Occurrence, **kw: Any):
        calls.append((job.name, occ))
        return f"{job.name}: spied", False

    monkeypatch.setattr(tick, "dispatch_occurrence", spy)
    _register(history, job, at=datetime(2026, 10, 19, tzinfo=UTC))

    outcome = tick.run_tick(
        RoutinesConfig(jobs=(job,)),
        history,
        client=client,  # type: ignore[arg-type]
        now=FROZEN_NOW,
    )
    assert outcome.summaries == ("a: spied",)
    assert len(calls) == 1
    name, scheduled = calls[0]
    assert name == "a" and "trigger" not in scheduled.extra
    assert scheduled.extra.get("scheduled_for") == OCCURRENCE.isoformat()

    assert _run_cli(monkeypatch, job) == 0
    assert len(calls) == 2
    name, manual = calls[1]
    assert name == "a" and manual.extra == {"trigger": "manual"}
    assert manual.run_id.startswith("a-")


def test_manual_pipeline_run_refuses_while_a_run_is_open(
    tmp_path: Path,
    history: Path,
    client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _kinds(tmp_path)["pipeline"]
    append(
        history,
        HistoryRecord(
            ts=FROZEN_NOW - timedelta(hours=1),
            job="a",
            state="running",
            run_id="a-20261020T090000Z",
        ),
    )
    launched: list[list[str]] = []

    def fake_launch(argv: list[str], **kw: Any) -> tuple[int, str, str]:
        launched.append(argv)
        return 0, "", ""

    monkeypatch.setattr(tick, "launch_pipeline", fake_launch)

    assert _run_cli(monkeypatch, job) == 1
    assert launched == []
    assert len(read_job(history, "a")) == 1
