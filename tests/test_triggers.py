"""Acceptance tests for issue 075: triggers as job config (cron / event / request).

Every test drives a real entry point (`run_tick` / `cli.main`); only the agent run itself
(`tick.execute_run`), the `gh` event poll and the herdr client are faked.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli, tick
from herdr_routines.auto_fix import GithubEvent
from herdr_routines.config import (
    AuditSpec,
    GateCheck,
    Job,
    RoutinesConfig,
    TelegramSpawn,
    load_config,
)
from herdr_routines.herdr import HerdrCliError
from herdr_routines.history import (
    HistoryRecord,
    append,
    last_scheduled_terminal_run,
    read_job,
)
from herdr_routines.runner import RunOutcome
from herdr_routines.tick import tick_lock

# 10:20 on a day when the hourly cron last fired at 10:00.
FROZEN_NOW = datetime(2026, 10, 20, 10, 20, tzinfo=UTC)
OCCURRENCE = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

EVENT_CHECK = GateCheck(
    kind="github_event", query="prs:labels", repo="o/r", match={"label": "go"}
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return FROZEN_NOW


class _Client:
    """The HerdrClient surface a dispatch touches before any agent exists."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str | None]] = []

    def agent_statuses(self) -> dict[str, str]:
        return {}

    def notification_show(self, title: str, *, body: str | None = None, sound="none"):
        self.notifications.append((title, body))

    def tab_create(self, *, cwd: str, label: str | None = None) -> str:
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


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the agent run: every call lands in the returned list of run ids."""
    run_ids: list[str] = []

    def fake_execute_run(job: Job, client: Any, *, run_id: str, **_: Any) -> RunOutcome:
        run_ids.append(run_id)
        return RunOutcome(state="done", run_id=run_id)

    monkeypatch.setattr(tick, "execute_run", fake_execute_run)
    return run_ids


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, config: RoutinesConfig, *argv: str
) -> int:
    monkeypatch.setattr(cli, "_load_config_or_exit", lambda args: config)
    return cli.main(list(argv))


def _register(history: Path, job: Job, *, at: datetime) -> None:
    append(history, HistoryRecord(ts=at, job=job.name, state="registered"))


def _tick(job: Job, history: Path, client: _Client, now: datetime) -> tuple[str, ...]:
    return tick.run_tick(
        RoutinesConfig(jobs=(job,)),
        history,
        client=client,  # type: ignore[arg-type]
        now=now,
    ).summaries


def _triggers(history: Path, state: str, name: str = "a") -> list[object]:
    return [
        (r.extra or {}).get("trigger")
        for r in read_job(history, name)
        if r.state == state
    ]


def _poll_returns(monkeypatch: pytest.MonkeyPatch, events: list[GithubEvent]) -> None:
    monkeypatch.setattr(tick, "poll_github_event", lambda gh, **kw: list(events))


def _event(event_id: str, number: int = 1) -> GithubEvent:
    return GithubEvent(
        event_id=event_id,
        number=number,
        title="t",
        head_ref=None,
        label="go",
        payload={},
    )


def _write_job_yaml(root: Path, body: str) -> Path:
    jobs_d = root / "jobs.d"
    jobs_d.mkdir(parents=True, exist_ok=True)
    # A review job has no single checkout, so it takes no `repo`.
    repo = "" if "kind: review" in body else f"repo: {root}\n"
    (jobs_d / "a.yaml").write_text(
        f"name: a\n{repo}prompt: report to $ROUTINE_REPORT\n{body}"
    )
    return jobs_d


def _load_errors(root: Path, body: str) -> str:
    """A bad file in a jobs.d/ layout does not raise: it lands in `config.errors`."""
    config = load_config(_write_job_yaml(root, body))
    assert config.jobs == ()
    return "\n".join(config.errors)


# -- AC 1 ---------------------------------------------------------------------------


def test_trigger_cron_shorthand_equivalent(
    tmp_path: Path, history: Path, client: _Client, runs: list[str]
) -> None:
    short = load_config(_write_job_yaml(tmp_path / "s", 'cron: "0 * * * *"\n')).jobs[0]
    block = load_config(
        _write_job_yaml(tmp_path / "b", 'trigger:\n  cron: "0 * * * *"\n')
    ).jobs[0]
    assert short.cron == "0 * * * *" and short.trigger_event is None
    assert short == replace(block, repo=short.repo)

    # Both spellings at once, or neither, is a load error.
    both = _load_errors(tmp_path / "x", 'cron: "0 * * * *"\ntrigger: {request: true}\n')
    assert "not both" in both
    assert "'cron' or 'trigger'" in _load_errors(tmp_path / "y", "")

    # The loaded job runs exactly as before, now stamped as a cron firing.
    _register(history, short, at=datetime(2026, 10, 19, tzinfo=UTC))
    assert _tick(short, history, client, FROZEN_NOW) == ("a: done",)
    assert _triggers(history, "running") == ["cron"]
    assert runs == ["a-20261020T100000Z"]


# -- AC 2 ---------------------------------------------------------------------------


def test_request_only_job_never_fires_on_tick(
    tmp_path: Path,
    history: Path,
    client: _Client,
    runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(tmp_path, cron=None, request=True)
    _register(history, job, at=datetime(2026, 10, 1, tzinfo=UTC))

    for days in range(0, 20, 3):
        summary = _tick(job, history, client, FROZEN_NOW + timedelta(days=days))
        assert summary == ("a: not due (request only)",)
    assert runs == []
    assert [r.state for r in read_job(history, "a")] == ["registered"]

    # `run` still executes it, and the ticks after it report nothing either.
    assert _run_cli(monkeypatch, RoutinesConfig(jobs=(job,)), "run", "a") == 0
    assert len(runs) == 1
    assert _triggers(history, "running") == ["manual"]
    _tick(job, history, client, FROZEN_NOW + timedelta(days=30))
    assert not [r for r in read_job(history, "a") if r.state == "missed"]
    assert len(runs) == 1


# -- AC 3 ---------------------------------------------------------------------------


def test_event_trigger_fires_once_per_event(
    tmp_path: Path,
    history: Path,
    client: _Client,
    runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(tmp_path, cron=None, trigger_event=EVENT_CHECK)
    _register(history, job, at=datetime(2026, 10, 19, tzinfo=UTC))

    _poll_returns(monkeypatch, [_event("evt-pr-1")])
    assert _tick(job, history, client, FROZEN_NOW) == ("a: done",)
    assert len(runs) == 1

    # The same event again: nothing runs, however many ticks see it.
    for minutes in (5, 10):
        summary = _tick(job, history, client, FROZEN_NOW + timedelta(minutes=minutes))
        assert summary == ("a: not due (no new events)",)
    assert len(runs) == 1

    # A second event fires once, and only it.
    _poll_returns(monkeypatch, [_event("evt-pr-1"), _event("evt-pr-2", number=2)])
    assert _tick(job, history, client, FROZEN_NOW + timedelta(minutes=15)) == (
        "a: done",
    )
    assert len(runs) == 2
    fired = [
        (r.extra or {}).get("event_id")
        for r in read_job(history, "a")
        if r.state == "running"
    ]
    assert fired == ["evt-pr-1", "evt-pr-2"]
    assert _triggers(history, "running") == ["event", "event"]

    # An event that clears and comes back fires again (issue 014's watermark).
    _poll_returns(monkeypatch, [])
    _tick(job, history, client, FROZEN_NOW + timedelta(minutes=20))
    _poll_returns(monkeypatch, [_event("evt-pr-2", number=2)])
    _tick(job, history, client, FROZEN_NOW + timedelta(minutes=25))
    assert len(runs) == 3


def test_event_runs_do_not_consume_the_cron_occurrence(
    tmp_path: Path,
    history: Path,
    client: _Client,
    runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron first, events wait a tick, and an event run is never what `decide` sees."""
    job = _job(tmp_path, trigger_event=EVENT_CHECK)
    _register(history, job, at=datetime(2026, 10, 20, 9, 30, tzinfo=UTC))
    _poll_returns(monkeypatch, [_event("evt-pr-1")])

    # 10:20: the 10:00 occurrence is due. It runs; the event waits.
    assert _tick(job, history, client, FROZEN_NOW) == ("a: done",)
    assert runs == ["a-20261020T100000Z"]
    # 10:25: nothing else is due, so the event fires.
    assert _tick(job, history, client, FROZEN_NOW + timedelta(minutes=5)) == (
        "a: not due; a: done",
    )
    assert len(runs) == 2
    assert _triggers(history, "running") == ["cron", "event"]

    last = last_scheduled_terminal_run(history, "a")
    assert last is not None and last.run_id == "a-20261020T100000Z"
    # 11:05: the 11:00 occurrence is still on time, not collapsed with the event run.
    summary = _tick(job, history, client, FROZEN_NOW + timedelta(minutes=45))
    assert summary == ("a: done",)
    assert runs[-1] == "a-20261020T110000Z"


def test_event_poll_failure_fails_closed_and_pages_once(
    tmp_path: Path,
    history: Path,
    client: _Client,
    runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(tmp_path, cron=None, trigger_event=EVENT_CHECK)
    _register(history, job, at=datetime(2026, 10, 19, tzinfo=UTC))

    def broken(gh: Any, **kw: Any) -> list[GithubEvent]:
        raise RuntimeError("rate limited")

    monkeypatch.setattr(tick, "poll_github_event", broken)
    for minutes in (0, 5):
        summary = _tick(job, history, client, FROZEN_NOW + timedelta(minutes=minutes))
        assert summary == ("a: failed (github_event_poll_failed)",)
    assert runs == []
    assert [t[0] for t in client.notifications] == ["herdr-routines: a failed"]

    # The first good poll ends the outage, so the next one pages again.
    _poll_returns(monkeypatch, [])
    _tick(job, history, client, FROZEN_NOW + timedelta(minutes=10))
    monkeypatch.setattr(tick, "poll_github_event", broken)
    _tick(job, history, client, FROZEN_NOW + timedelta(minutes=15))
    assert len(client.notifications) == 2


# -- AC 4 ---------------------------------------------------------------------------


def test_telegram_job_verb_runs_request_jobs_only(
    tmp_path: Path,
    history: Path,
    client: _Client,
    runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = RoutinesConfig(
        jobs=(
            _job(tmp_path, name="a", cron=None, request=True),
            _job(tmp_path, name="plain"),
            _job(tmp_path, name="off", cron=None, request=True, enabled=False),
        ),
        telegram_spawn=TelegramSpawn(allowed_chat_id="123"),
    )
    launched: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kw: launched.append(list(argv)),
    )

    def message(text: str, chat_id: str = "123") -> str:
        capsys.readouterr()
        rc = _run_cli(
            monkeypatch,
            config,
            "spawn-from-telegram",
            "--text",
            text,
            "--chat-id",
            chat_id,
        )
        assert rc == 0
        return capsys.readouterr().out

    # Allowed chat, requestable job: it is launched through `run --trigger request`.
    assert "Started a" in message("/job a")
    assert len(launched) == 1
    assert launched[0][1:] == ["run", "a", "--trigger", "request"]

    # That argv is the shared run path: it dispatches and records the trigger.
    assert _run_cli(monkeypatch, config, *launched[0][1:]) == 0
    assert len(runs) == 1
    assert _triggers(history, "running") == ["request"]
    assert _triggers(history, "done") == ["request"]

    # Refused: a job without `request`, a disabled one, an unknown one, a disallowed chat.
    for text in ("/job plain", "/job off", "/job nope"):
        assert "not requestable" in message(text)
    assert "not authorized" in message("/job a", chat_id="999")
    assert "not authorized" in message("/job a", chat_id="-100123")
    assert len(launched) == 1
    assert len(runs) == 1
    # `run` itself still works on any job.
    assert _run_cli(monkeypatch, config, "run", "plain") == 0
    assert len(runs) == 2


def test_telegram_job_request_refused_while_tick_holds_lock_notifies(
    tmp_path: Path,
    history: Path,
    client: _Client,
    runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RoutinesConfig(jobs=(_job(tmp_path, cron=None, request=True),))
    with tick_lock(tmp_path / "tick.lock") as acquired:
        assert acquired
        rc = _run_cli(monkeypatch, config, "run", "a", "--trigger", "request")
        manual_rc = _run_cli(monkeypatch, config, "run", "a")

    assert rc == manual_rc == 1
    assert runs == []
    assert not history.exists()
    # Only the request run, which nobody is watching, says so; a manual run just exits 1.
    assert [t[0] for t in client.notifications] == ["herdr-routines: a request refused"]


# -- AC 5 ---------------------------------------------------------------------------


def _kind_job(tmp_path: Path, kind: str) -> Job:
    if kind == "gated":
        return _job(
            tmp_path,
            kind="gated",
            target="base",
            checks=(GateCheck(kind="command", command="true"),),
        )
    if kind == "audit":
        return _job(tmp_path, kind="audit", audit=AuditSpec(command="true"))
    if kind == "review":
        return _job(tmp_path, kind="review", target="pr", select=("author",))
    return _job(tmp_path)


@pytest.mark.parametrize("kind", ["routine", "gated", "audit", "review"])
def test_run_records_its_trigger(
    tmp_path: Path,
    history: Path,
    client: _Client,
    runs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Every run's opening `running` record names its trigger, for every kind. A routine's
    terminal record carries it too; the gated / audit / review cycles write their terminals
    without the occurrence, and `last_scheduled_terminal_run` links them by run_id."""

    # The cycles past the `running` record are other issues' business: stub them, closing
    # the run the way the real ones do, with a terminal record that has no trigger of its own.
    def closed_cycle(
        job: Job, history_path: Path, *a: Any, run_id: str, now: datetime, **kw: Any
    ) -> tuple[str, bool]:
        append(
            history_path,
            HistoryRecord(ts=now, job=job.name, state="done", run_id=run_id),
        )
        return "a: stubbed", False

    for cycle in ("_process_base_target", "_audit_cycle", "_review_cycle"):
        monkeypatch.setattr(tick, cycle, closed_cycle)
    job = replace(_kind_job(tmp_path, kind), request=True)
    event_job = (
        replace(job, trigger_event=EVENT_CHECK)
        if kind in ("routine", "audit")
        else None
    )
    _register(history, job, at=datetime(2026, 10, 19, tzinfo=UTC))

    _tick(job, history, client, FROZEN_NOW)
    assert _run_cli(monkeypatch, RoutinesConfig(jobs=(job,)), "run", "a") == 0
    assert (
        _run_cli(
            monkeypatch, RoutinesConfig(jobs=(job,)), "run", "a", "--trigger", "request"
        )
        == 0
    )
    expected = ["cron", "manual", "request"]
    if event_job is not None:
        _poll_returns(monkeypatch, [_event("evt-pr-1")])
        summaries = _tick(event_job, history, client, FROZEN_NOW + timedelta(minutes=5))
        assert summaries[0].startswith("a: not due; ")
        expected.append("event")
    assert _triggers(history, "running") == expected
    if kind == "routine":
        terminals = [
            (r.extra or {}).get("trigger")
            for r in read_job(history, "a")
            if r.state == "done" and r.run_id
        ]
        assert terminals == expected

    # And none of the off-schedule runs counts toward the schedule.
    last = last_scheduled_terminal_run(history, "a")
    assert last is not None and last.run_id == "a-20261020T100000Z"


# -- AC 6 ---------------------------------------------------------------------------


def test_validate_warns_on_untriggered_or_unrunnable_request_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs_d = tmp_path / "jobs.d"
    jobs_d.mkdir()
    (tmp_path / ".git").mkdir()
    common = f"repo: {tmp_path}\nprompt: report to $ROUTINE_REPORT\n"
    (jobs_d / "idle.yaml").write_text(f"name: idle\n{common}trigger: {{}}\n")
    (jobs_d / "dead.yaml").write_text(
        f"name: dead\n{common}enabled: false\ntrigger: {{request: true}}\n"
    )
    (jobs_d / "fine.yaml").write_text(
        f"name: fine\n{common}trigger: {{request: true}}\n"
    )
    (jobs_d / "daily.yaml").write_text(f'name: daily\n{common}cron: "0 9 * * *"\n')

    rc = cli.main(
        [
            "--config",
            str(jobs_d),
            "validate",
            "--systemd-unit",
            str(tmp_path / "missing.service"),
        ]
    )

    err = capsys.readouterr().err
    assert rc == 0, err
    assert "idle: no trigger" in err
    assert "dead: request: true but enabled: false" in err
    assert "fine:" not in err and "daily:" not in err


def test_trigger_event_rejected_where_it_cannot_run(tmp_path: Path) -> None:
    event = "trigger:\n  event:\n    github_event: {query: 'prs:labels'}\n"
    for kind, extra in (
        ("pipeline", "prompt_file: p.md\ndeadline_ms: 1000000\n"),
        ("review", "select: [author]\n"),
    ):
        errors = _load_errors(tmp_path / kind, f"kind: {kind}\n{extra}{event}")
        assert f"'trigger.event' is not supported on kind: {kind}" in errors
