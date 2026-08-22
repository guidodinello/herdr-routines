"""Tests for tick.py's live-agent safety net. See docs/plan-v1.md §4."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_routines.config import Job, RoutinesConfig
from herdr_routines.herdr import HerdrCliError
from herdr_routines.tick import _live_agent_exists, run_tick


def make_job(tmp_path: Path, **overrides) -> Job:
    defaults = {
        "name": "a",
        "enabled": True,
        "cron": "0 3 * * *",
        "repo": tmp_path,
        "workspace": "root",
        "base": "main",
        "agent_kind": "claude",
        "model": None,
        "prompt": "",
        "timeout_ms": 60_000,
        "start_timeout_ms": 30_000,
        "catch_up_minutes": 120,
        "timezone": "UTC",
        "on_missed": "log",
    }
    defaults.update(overrides)
    return Job(**defaults)


class FakeStatusClient:
    def __init__(
        self, statuses: dict[str, str] | None = None, *, raise_error: bool = False
    ):
        self._statuses = statuses or {}
        self._raise_error = raise_error

    def agent_statuses(self) -> dict[str, str]:
        if self._raise_error:
            raise HerdrCliError("server unreachable", exit_code=1)
        return self._statuses


def test_finished_agent_is_not_live(tmp_path: Path) -> None:
    """Regression: a settled (idle/done) agent stays registered under `agent list` forever
    until its tab is closed. Treating mere presence as "live" would permanently skip every
    later tick for a recurring root-mode job. See docs/plan-v1.md §4."""
    job = make_job(tmp_path)
    client = FakeStatusClient({job.agent_name: "idle"})
    assert _live_agent_exists(client, job) is False  # type: ignore[arg-type]


def test_done_agent_is_not_live(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = FakeStatusClient({job.agent_name: "done"})
    assert _live_agent_exists(client, job) is False  # type: ignore[arg-type]


def test_working_agent_is_live(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = FakeStatusClient({job.agent_name: "working"})
    assert _live_agent_exists(client, job) is True  # type: ignore[arg-type]


def test_blocked_agent_is_not_live(tmp_path: Path) -> None:
    """Regression: `blocked` (waiting on a human) is just as sticky as idle/done under an
    unattended cron job — nothing in herdr-routines answers the prompt, so treating it as
    "live" would reproduce the same skip-forever bug via a different agent_status."""
    job = make_job(tmp_path)
    client = FakeStatusClient({job.agent_name: "blocked"})
    assert _live_agent_exists(client, job) is False  # type: ignore[arg-type]


def test_unregistered_agent_is_not_live(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = FakeStatusClient({})
    assert _live_agent_exists(client, job) is False  # type: ignore[arg-type]


def test_other_jobs_agents_do_not_affect_this_job(tmp_path: Path) -> None:
    job = make_job(tmp_path, name="a")
    client = FakeStatusClient({"rt-other-job": "working"})
    assert _live_agent_exists(client, job) is False  # type: ignore[arg-type]


def test_herdr_cli_error_fails_open(tmp_path: Path) -> None:
    """The history/flock check is the primary overlap guard; a server-unreachable error here
    must not block the job from being evaluated."""
    job = make_job(tmp_path)
    client = FakeStatusClient(raise_error=True)
    assert _live_agent_exists(client, job) is False  # type: ignore[arg-type]


class FakeFullClient:
    """Enough of HerdrClient for run_tick to complete a job end-to-end. Tracks each agent's
    status the way herdr actually does: `agent_start` registers it as "working", and it stays
    registered at its settled status (default "idle") after the prompt completes — it is never
    removed just because the run finished, only when a tab is closed (which nothing here does).
    """

    def __init__(self, *, settle_status: str = "idle") -> None:
        self._settle_status = settle_status
        self._registered: dict[str, str] = {}

    def tab_create(self, *, cwd, label=None):
        return "w1:p1"

    def worktree_create(self, *, cwd, branch, base, label=None):
        return "w1:p1"

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        self._registered[name] = "working"

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self._registered[target] = self._settle_status
        # `text` is the prompt with $ROUTINE_REPORT already substituted to a real path (see
        # runner.execute_run) — write the report so the run doesn't fail with "no_report".
        Path(text.rsplit(maxsplit=1)[-1]).write_text("# ok\n")
        return self._settle_status

    def agent_read(self, target, *, lines=200):
        return ""

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._registered)

    def notification_show(self, title, *, body=None, sound="none"):
        pass


def test_recurring_root_job_is_not_skipped_after_its_agent_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression for the bug this module fixes: a workspace:root job whose agent
    settled to idle/done on a prior run must be schedulable again on its next due occurrence,
    not skipped forever as "agent already live". Runs three ticks a minute apart against
    run_tick itself (not just the _live_agent_exists unit), matching the reported symptom."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path, cron="* * * * *", timeout_ms=5_000, prompt="report to $ROUTINE_REPORT"
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeFullClient(settle_status="idle")

    # The terminal history record's own timestamp is real wall-clock time (tick.py writes
    # datetime.now(UTC), not the injected `now`), so `now` here is anchored to the real clock
    # rather than an arbitrary fixed date to keep the schedule's (since, now] window sane.
    t0 = datetime.now(UTC).replace(microsecond=0)
    summary1 = run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    assert summary1 == ["a: registered"]

    t1 = t0 + timedelta(minutes=1)
    summary2 = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    assert summary2 == ["a: done"]
    # Confirms the fixture matches reality: the agent stays registered (idle) after finishing.
    assert client.agent_statuses() == {job.agent_name: "idle"}

    t2 = t0 + timedelta(minutes=2)
    summary3 = run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    assert summary3 == ["a: done"]
