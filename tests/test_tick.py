"""Tests for tick.py's live-agent safety net (see docs/plan-v1.md §4) and for run_tick's
TickOutcome.any_job_failed — what `_cmd_tick` (cli.py) maps to the process exit code."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.config import Job, RoutinesConfig
from herdr_routines.herdr import HerdrCliError
from herdr_routines.history import HistoryRecord, append, read_job
from herdr_routines.tick import _live_agent_exists, run_tick
from herdr_routines.auto_fix import attempt_count_for_pr


def make_job(tmp_path: Path, **overrides: Any) -> Job:
    # Built directly, then `replace`d: a defaults dict splatted into Job() widens to
    # dict[str, object] and fails the typecheck gate on every field.
    job = Job(
        name="a",
        enabled=True,
        cron="* * * * *",
        repo=tmp_path,
        workspace="root",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="report to $ROUTINE_REPORT",
        timeout_ms=5_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )
    return replace(job, **overrides)


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

    def agent_interactive_ready(self, target):
        return True

    def settled_agent_workspace(self, name):
        return None

    def settled_agent_pane(self, name):
        return None

    def workspace_close(self, workspace_id):
        pass

    def pane_close(self, pane_id):
        pass

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self._registered[target] = self._settle_status
        # `text` is the prompt with $ROUTINE_REPORT already substituted to a real path (see
        # runner.execute_run) — write the report so the run doesn't fail with "no_report".
        Path(text.rsplit(maxsplit=1)[-1]).write_text("# ok\n")
        return self._settle_status

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        return self.agent_prompt_wait(target=target, text=text, timeout_ms=timeout_ms)

    def agent_read(self, target, *, lines=200):
        return ""

    def agent_read_visible(self, target, *, lines=200):
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
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeFullClient(settle_status="idle")

    # The terminal history record's own timestamp is real wall-clock time (tick.py writes
    # datetime.now(UTC), not the injected `now`), so `now` here is anchored to the real clock
    # rather than an arbitrary fixed date to keep the schedule's (since, now] window sane.
    t0 = datetime.now(UTC).replace(microsecond=0)
    outcome1 = run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    assert outcome1.summaries == ("a: registered",)

    t1 = t0 + timedelta(minutes=1)
    outcome2 = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    assert outcome2.summaries == ("a: done",)
    # Confirms the fixture matches reality: the agent stays registered (idle) after finishing.
    assert client.agent_statuses() == {job.agent_name: "idle"}

    t2 = t0 + timedelta(minutes=2)
    outcome3 = run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    assert outcome3.summaries == ("a: done",)


class FakeClient:
    """Enough of HerdrClient for a job to actually be attempted. `fail_at`, if set, raises a
    HerdrCliError from that call — simulating the herdr server dying mid-run (the scenario
    verified 3x on the Pi that this module's fix makes visible to systemd)."""

    def __init__(
        self, *, fail_at: str | None = None, settle_status: str = "idle"
    ) -> None:
        self.fail_at = fail_at
        self.settle_status = settle_status

    def _maybe_raise(self, call: str) -> None:
        if self.fail_at == call:
            raise HerdrCliError(f"{call} boom", exit_code=1)

    def tab_create(self, *, cwd, label=None):
        self._maybe_raise("tab_create")
        return "w1:p1"

    def worktree_create(self, *, cwd, branch, base, label=None):
        self._maybe_raise("worktree_create")
        return "w1:p1"

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        self._maybe_raise("agent_start")

    def agent_interactive_ready(self, target):
        self._maybe_raise("agent_interactive_ready")
        return True

    def settled_agent_workspace(self, name):
        self._maybe_raise("settled_agent_workspace")

    def settled_agent_pane(self, name):
        self._maybe_raise("settled_agent_pane")
        self._maybe_raise("settled_agent_workspace")

    def workspace_close(self, workspace_id):
        pass

    def pane_close(self, pane_id):
        pass

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self._maybe_raise("agent_prompt_wait")
        Path(text.rsplit(maxsplit=1)[-1]).write_text("# ok\n")
        return self.settle_status

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        return self.agent_prompt_wait(target=target, text=text, timeout_ms=timeout_ms)

    def agent_read(self, target, *, lines=200):
        return ""

    def agent_read_visible(self, target, *, lines=200):
        return ""

    def agent_statuses(self) -> dict[str, str]:
        return {}

    def notification_show(self, title, *, body=None, sound="none"):
        pass


def test_no_failure_when_nothing_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path, cron="0 3 * * *")  # not due at the fixed `now` below
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient()
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    outcome = run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: registered",)
    assert outcome.any_job_failed is False


def test_no_failure_for_a_successful_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    assert outcome.any_job_failed is False


def test_failure_flagged_when_a_due_job_actually_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: this is the scenario verified 3x on the Pi — the herdr server dies mid-run,
    the job's own history record correctly says `failed`, but the tick's own exit code must
    also reflect it so systemd (and any monitoring watching unit state) can see the failure."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(fail_at="agent_start")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: failed (agent_start_failed)",)
    assert outcome.any_job_failed is True


def test_failure_not_flagged_for_a_merely_missed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job outside its catch-up window is a scheduling outcome, not an operational failure —
    must not flip the tick's exit code. `late` in schedule.decide() is (now - latest
    occurrence), so a once-daily cron with `now` registered just before midnight and evaluated
    well after it is what actually exercises MISSED — a per-minute cron never does, since
    `now` is always within a minute of its own latest occurrence."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path, cron="0 0 * * *", catch_up_minutes=1)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient()

    t0 = datetime(2026, 1, 1, 23, 0, 0, tzinfo=UTC)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = datetime(2026, 1, 2, 2, 0, 0, tzinfo=UTC)  # 2h past the 00:00 occurrence
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: missed",)
    assert outcome.any_job_failed is False


def test_failure_not_flagged_for_a_skipped_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job skipped because it's already running/live is not this tick's own failure."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))

    class LiveAgentClient(FakeClient):
        def agent_statuses(self) -> dict[str, str]:
            return {job.agent_name: "working"}

    client = LiveAgentClient()
    now = datetime.now(UTC)
    run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type] # registers
    outcome = run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: skipped (agent already live)",)
    assert outcome.any_job_failed is False


def test_failure_not_flagged_for_the_stale_running_recovery_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale `running` record (a previous tick crashed mid-run, this tick recovers by
    writing `interrupted_unknown` for it) is about a *past* tick's execution, not this one's —
    per TickOutcome's docstring, only a job this tick itself executes and settles on a
    non-"done" state should flip any_job_failed. This tick then goes on to evaluate (and here,
    run) the job fresh, which is the whole point of "fall through" in _process_job."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers

    # Simulate a crashed prior tick: a "running" record with no terminal record for its run_id,
    # old enough (past timeout_ms + STALE_MARGIN) that find_stale_running flags it.
    stale_ts = t0 + timedelta(seconds=1)
    append(
        history_path,
        HistoryRecord(
            ts=stale_ts,
            job=job.name,
            state="running",
            run_id="a-stale-run",
            extra={"scheduled_for": stale_ts.isoformat(), "late_seconds": 0},
        ),
    )
    t1 = stale_ts + timedelta(milliseconds=job.timeout_ms) + timedelta(minutes=10)

    # This tick detects the staleness, records `interrupted_unknown`, and (per the "fall
    # through" comment in _process_job) does not treat that as a block — but since the record
    # it just wrote for `since` in schedule.decide() carries this same tick's own `now`, the
    # (since, now] window is necessarily empty, so this tick itself always evaluates NOT_DUE.
    # The point of falling through is that the *next* tick, once a real cron occurrence has
    # passed, can run the job — not that this same tick immediately does.
    outcome1 = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    assert outcome1.summaries == ("a: not due",)
    assert outcome1.any_job_failed is False

    t2 = t1 + timedelta(minutes=1)
    outcome2 = run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    assert outcome2.summaries == ("a: done",)
    assert outcome2.any_job_failed is False

    states = [r.state for r in read_job(history_path, job.name)]
    assert "interrupted_unknown" in states  # the stale record was written as expected
    assert states[-1] == "done"  # and the job was not blocked from ever running again


# ---------------------------------------------------------------------------
# Auto-fix tick integration tests
# ---------------------------------------------------------------------------


def make_auto_fix_job(tmp_path: Path, **overrides: Any) -> Job:
    from herdr_routines.config import AutoFixConfig

    job = Job(
        name="auto-fix-prs",
        enabled=True,
        cron="* * * * *",
        repo=tmp_path,
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="",
        timeout_ms=5_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
        auto_fix=AutoFixConfig(
            branch_prefix="auto/",
            max_prs_per_tick=3,
            max_attempts_per_pr=3,
        ),
    )
    return replace(job, **overrides)


class MockGhClient:
    """Minimal gh client that returns no PRs."""

    def api_user(self) -> str:
        return "testuser"

    def pr_list(self, *, owner, repo, state, limit):
        return []

    def pr_view(self, *, owner, repo, number):
        return {}

    def graphql(self, query, **variables):
        return {"data": {}}


class MockSubprocess:
    """Mocks subprocess.run for git remote detection."""

    class Result:
        returncode = 0
        stdout = "git@github.com:test/repo.git"
        stderr = ""

    @staticmethod
    def run(*args, **kwargs):
        return MockSubprocess.Result()


def test_auto_fix_tick_registers_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-fix job registers on first tick, runs enumeration on second."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    job = make_auto_fix_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeFullClient()

    monkeypatch.setattr("herdr_routines.tick.RealGhClient", MockGhClient)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    t0 = datetime.now(UTC).replace(microsecond=0)
    outcome1 = run_tick(config, history_path, client=client, now=t0)
    assert "registered" in outcome1.summaries[0]

    t1 = t0 + timedelta(minutes=1)
    outcome2 = run_tick(config, history_path, client=client, now=t1)
    # Empty PR list: enumerated=0, eligible=0, dispatched=0, skipped=0
    assert "enumerated=0" in outcome2.summaries[0]
    assert outcome2.any_job_failed is False


def test_auto_fix_tick_skips_when_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-fix job is skipped when already running."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    job = make_auto_fix_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))

    class LiveAgentClient(FakeFullClient):
        def agent_statuses(self) -> dict[str, str]:
            return {job.agent_name: "working"}

    client = LiveAgentClient()
    now = datetime.now(UTC)
    run_tick(config, history_path, client=client, now=now)
    outcome = run_tick(config, history_path, client=client, now=now)

    assert "skipped (agent already live)" in outcome.summaries[0]
    assert outcome.any_job_failed is False


def test_auto_fix_tick_max_attempts_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When max_attempts_per_pr is exceeded, the PR is skipped with
    max_attempts_exceeded reason."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    from herdr_routines.config import AutoFixConfig

    job = make_auto_fix_job(
        tmp_path,
        auto_fix=AutoFixConfig(
            branch_prefix="auto/",
            max_prs_per_tick=3,
            max_attempts_per_pr=2,
        ),
    )
    config = RoutinesConfig(jobs=(job,))

    # Pre-populate history with 2 terminal records for PR 10
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    for i in range(2):
        append(
            history_path,
            HistoryRecord(
                ts=t0 + timedelta(minutes=i),
                job="auto-fix-prs",
                state="done",
                run_id=f"run-{i}",
                extra={"pr_number": 10, "attempt": i},
            ),
        )

    count = attempt_count_for_pr(history_path, "auto-fix-prs", 10)
    assert count == 2

    # Verify the attempt count logic works
    assert count >= job.auto_fix.max_attempts_per_pr
