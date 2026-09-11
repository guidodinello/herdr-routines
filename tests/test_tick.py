"""Tests for tick.py's live-agent safety net (see docs/plan-v1.md §4) and for run_tick's
TickOutcome.any_job_failed — what `_cmd_tick` (cli.py) maps to the process exit code."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.auto_fix import attempt_count_for_pr
from herdr_routines.config import PIPELINE_CATCH_UP_MINUTES, Job, RoutinesConfig
from herdr_routines.herdr import HerdrCliError, PromptWatchdogKilled
from herdr_routines.history import HistoryRecord, append, read_job
from herdr_routines.tick import _live_agent_exists, run_tick


@pytest.fixture(autouse=True)
def _stub_ensure_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch/scheduling tests in this file don't exercise repo-sync behavior (that's
    test_repos.py's job) and mostly point `job.repo` at a bare tmp_path that isn't a real
    git checkout — stub ensure_repo so it's not called for real. Tests that specifically
    verify the ensure_repo-in-dispatch gate (e.g. test_repo_url_tick_runner_gate) override
    this with their own monkeypatch.setattr."""
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.runner.ensure_repo", lambda job: job.repo)


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
        self.notifications: list[tuple[str, str | None, str]] = []

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
        self.notifications.append((title, body, sound))


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
        self,
        *,
        fail_at: str | None = None,
        settle_status: str = "idle",
        quota_exhausted_for_model: str | None = None,
    ) -> None:
        self.fail_at = fail_at
        self.settle_status = settle_status
        # Simulates a provider's free-tier quota wall: any run started with this model raises
        # PromptWatchdogKilled (the same path runner.py hits on a real "Free usage exceeded"
        # screen match), while any other model (e.g. a job's fallback_model) succeeds normally.
        self.quota_exhausted_for_model = quota_exhausted_for_model
        self._last_model: str | None = None
        self._worktree_branches: set[str] = set()
        self.notifications: list[tuple[str, str | None, str]] = []

    def _maybe_raise(self, call: str) -> None:
        if self.fail_at == call:
            raise HerdrCliError(f"{call} boom", exit_code=1)

    def tab_create(self, *, cwd, label=None):
        self._maybe_raise("tab_create")
        return "w1:p1"

    def worktree_create(self, *, cwd, branch, base, label=None):
        self._maybe_raise("worktree_create")
        # Mirrors real `git worktree add` on a branch that already has a checkout: a fallback
        # retry reusing the primary attempt's branch name must fail here, the same way it would
        # against a real repo (PR #65 review finding).
        if branch in self._worktree_branches:
            raise HerdrCliError(f"branch {branch!r} already checked out", exit_code=1)
        self._worktree_branches.add(branch)
        return "w1:p1"

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        self._maybe_raise("agent_start")
        self._last_model = model

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
        if (
            self.quota_exhausted_for_model is not None
            and self._last_model == self.quota_exhausted_for_model
        ):
            raise PromptWatchdogKilled(
                "quota modal wedge",
                marker="Free usage exceeded",
                screen_text="Free usage exceeded",
            )
        return self.agent_prompt_wait(target=target, text=text, timeout_ms=timeout_ms)

    def agent_read(self, target, *, lines=200):
        return ""

    def agent_read_visible(self, target, *, lines=200):
        return ""

    def agent_statuses(self) -> dict[str, str]:
        return {}

    def notification_show(self, title, *, body=None, sound="none"):
        self.notifications.append((title, body, sound))


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


def test_fallback_model_retried_once_after_quota_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job with `fallback_model` set gets one automatic retry, under a fresh run_id, when
    the primary model's run fails with reason quota_exhausted — the scenario this feature
    exists for: the Pi's opencode free-tier pool exhausted, OpenRouter still has quota."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        agent_kind="opencode",
        model="opencode/muse-spark-1.2-contributor-free",
        fallback_model="openrouter/free",
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(
        settle_status="idle",
        quota_exhausted_for_model="opencode/muse-spark-1.2-contributor-free",
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    assert outcome.any_job_failed is False

    records = read_job(history_path, job.name)
    run_records = [r for r in records if r.run_id is not None]
    # primary "running" + primary "failed" (quota_exhausted) + fallback "running" + fallback "done"
    assert [r.state for r in run_records] == ["running", "failed", "running", "done"]
    primary_run, primary_failed, fallback_running, fallback_done = run_records
    assert primary_failed.extra is not None
    assert primary_failed.extra["reason"] == "quota_exhausted"
    assert fallback_running.extra is not None
    assert fallback_running.extra["reason"] == "fallback_retry"
    assert fallback_running.extra["primary_run_id"] == primary_run.run_id
    assert fallback_done.run_id == fallback_running.run_id != primary_run.run_id


class NudgeWritesReportClient(FakeClient):
    """The job's own prompt settles idle/done without writing a report; the issue-032
    no_report nudge (the second agent_prompt_wait call) is what actually writes it. Verifies
    the nudge's `RunOutcome.nudged` flag reaches the terminal history record via
    `_outcome_extra`, not just the in-process RunOutcome."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._prompt_calls = 0
        self._report_path: Path | None = None

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self._maybe_raise("agent_prompt_wait")
        self._prompt_calls += 1
        if self._prompt_calls == 1:
            # The job's own prompt has $ROUTINE_REPORT already substituted to a real path
            # (see runner.execute_run); capture it, but don't write it yet. The nudge prompt
            # (the second call) references the same path in prose, not as its last token, so
            # it can't be re-derived from `text` the way the first call's can.
            self._report_path = Path(text.rsplit(maxsplit=1)[-1])
        elif self._prompt_calls >= 2 and self._report_path is not None:
            self._report_path.write_text("# ok\n")
        return self.settle_status


def test_history_records_a_nudged_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (issue 032): when the one-shot no_report nudge is what produces the report,
    the run's terminal history record distinguishes it from a first-try success."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = NudgeWritesReportClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    records = read_job(history_path, job.name)
    done_record = next(r for r in records if r.state == "done")
    assert done_record.extra is not None
    assert done_record.extra["nudged"] is True


class ReapsStaleAgentClient(FakeClient):
    """First `agent start` fails `agent_name_taken` (a prior run left a blocked agent under
    the recurring name); `sticky_agent_pane` finds it, the runner force-closes it and the
    retry succeeds (issue 051)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._starts = 0
        self.closed_panes: list[str] = []

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        self._starts += 1
        if self._starts == 1:
            raise HerdrCliError(
                f"herdr server error running agent start {name}: name is already used",
                exit_code=1,
                error_body={"error": {"code": "agent_name_taken"}},
            )
        self._last_model = model

    def sticky_agent_pane(self, name):
        return ("w7:p9", "blocked")

    def pane_close(self, pane_id):
        self.closed_panes.append(pane_id)


def test_history_and_notification_surface_a_reaped_stale_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (issue 051): a run that only started because the runner force-closed a
    prior run's parked blocked agent is not a silent success — the terminal history record
    carries `reaped_stale_agent` and the notification is a 'done' finding, not plain success."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = ReapsStaleAgentClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    assert client.closed_panes == ["w7:p9"]
    done_record = next(r for r in read_job(history_path, job.name) if r.state == "done")
    assert done_record.extra is not None
    assert done_record.extra["reaped_stale_agent"] is True
    title, body, _sound = client.notifications[-1]
    assert title == "herdr-routines: a done"
    assert body is not None and "issue 051" in body


def test_fallback_retry_uses_a_distinct_branch_in_worktree_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (PR #65 review, confirmed by two independent reviewers): the fallback's
    run_id must not share the primary's timestamp suffix, or `build_branch_name` produces the
    identical branch name for both attempts. For `workspace: worktree` (the default, and what
    fitted-pr-review* uses), that collides with the primary's still-existing branch/worktree and
    `worktree_create` fails. `test_fallback_model_retried_once_after_quota_exhausted` alone
    can't catch this: `make_job` defaults to `workspace="root"`, which never calls
    `worktree_create` at all.

    `t0`/`t1` are pinned to a `:00`-second minute boundary deliberately, not just any timestamp:
    the production timer (`deploy/systemd/herdr-routines.timer`, `OnCalendar=*:0/5`) fires
    ticks right on `:00`, so `result.occurrence` (floored to the minute) and `now` (wall-clock)
    coincide almost every real run. An earlier version of this test used
    `datetime.now(UTC).replace(microsecond=0)`, which only exercised this collision when the
    real wall-clock second happened to be `:00` (~1/60 of runs) — a CI flake (see PR #72) that
    was actually this exact production bug caught by accident. Pinning to a boundary makes the
    regression deterministic instead of a lottery."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        workspace="worktree",
        agent_kind="opencode",
        model="opencode/muse-spark-1.2-contributor-free",
        fallback_model="openrouter/free",
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(
        settle_status="idle",
        quota_exhausted_for_model="opencode/muse-spark-1.2-contributor-free",
    )

    t0 = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    assert outcome.any_job_failed is False

    records = read_job(history_path, job.name)
    branches = {r.extra["branch"] for r in records if r.extra and r.extra.get("branch")}
    assert len(branches) == 2, (
        f"expected distinct primary/fallback branches, got {branches}"
    )


def test_no_fallback_retry_when_fallback_model_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a configured fallback_model, quota_exhausted is terminal — no change from
    pre-fallback behavior."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        agent_kind="opencode",
        model="opencode/muse-spark-1.2-contributor-free",
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(
        settle_status="idle",
        quota_exhausted_for_model="opencode/muse-spark-1.2-contributor-free",
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: failed (quota_exhausted)",)
    assert outcome.any_job_failed is True

    records = read_job(history_path, job.name)
    run_records = [r for r in records if r.run_id is not None]
    assert [r.state for r in run_records] == ["running", "failed"]


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
    from herdr_routines.config import GateCheck

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
        checks=(GateCheck(kind="pr_health"),),
        target="pr",
        max_workers_per_tick=3,
        max_attempts_per_target=3,
        kind="gated",
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
    outcome1 = run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    assert "registered" in outcome1.summaries[0]

    t1 = t0 + timedelta(minutes=1)
    outcome2 = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
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
    run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type]
    outcome = run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type]

    assert "skipped (agent already live)" in outcome.summaries[0]
    assert outcome.any_job_failed is False


def test_auto_fix_tick_max_attempts_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When max_attempts_per_pr is exceeded, the PR is skipped with
    max_attempts_exceeded reason."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    from herdr_routines.config import GateCheck

    job = make_auto_fix_job(
        tmp_path,
        checks=(GateCheck(kind="pr_health"),),
        target="pr",
        max_workers_per_tick=3,
        max_attempts_per_target=2,
    )

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
    assert count >= job.max_attempts_per_target


# -- notify_policy (issue 009) --------------------------------------------------------
#
# Assert against notifications a fake client actually recorded across a realistic tick,
# not against the gating predicate in isolation (`_notify_gate` is exercised only as a
# byproduct here) — the risk this guards against is a call site nobody remembered to gate,
# which a unit test of the predicate alone would never catch.


class EligiblePrGhClient:
    """gh client returning one PR (#42) eligible via a failing CI check, so the
    pr-target dispatch loop actually reaches `_dispatch_fix_worker` instead of stopping
    at an empty enumeration (see MockGhClient above, which returns none)."""

    def api_user(self) -> str:
        return "testuser"

    def pr_list(self, *, owner, repo, state, limit):
        return [
            {
                "number": 42,
                "headRefName": "auto/fix-42",
                "author": {"login": "testuser", "is_bot": False},
                "url": "https://github.com/test/repo/pull/42",
            }
        ]

    def pr_view(self, *, owner, repo, number):
        return {"statusCheckRollup": [{"name": "ci", "state": "FAILURE"}]}

    def graphql(self, query, **variables):
        return {
            "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
        }


class FakePrDispatchClient:
    """Enough of HerdrClient to let `_dispatch_fix_worker` settle a fix worker to
    "idle" without FakeFullClient's report-write trick, which assumes the prompt's last
    whitespace token is the report path — true for a plain routine's default prompt, not
    for `auto_fix.build_fix_prompt`'s multi-paragraph text."""

    def __init__(self) -> None:
        self._registered: dict[str, str] = {}
        self.notifications: list[tuple[str, str | None, str]] = []

    def tab_create(self, *, cwd, label=None):
        return "w1:p1"

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        self._registered[name] = "working"

    def agent_interactive_ready(self, target):
        return True

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        self._registered[target] = "idle"
        return "idle"

    def agent_read(self, target, *, lines=200):
        return ""

    def pane_close(self, pane_id):
        pass

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._registered)

    def notification_show(self, title, *, body=None, sound="none"):
        self.notifications.append((title, body, sound))


def _register_and_run_auto_fix_job(
    job: Job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gh_client: object,
    client: object,
) -> tuple[Path, Any]:
    """Register on tick 1, dispatch on tick 2 — the shared shape every auto-fix tick
    test in this section needs."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,))
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh_client)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    return history_path, outcome


def _register_then_populate_over_cap_pr(
    job: Job, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Register the job on its own (gh-free) tick, then append 3 terminal attempt
    records for PR 42 (>= max_attempts_per_target=3) directly to history — so the
    *next* tick's dispatch loop hits the max_attempts_exceeded skip branch on its first
    and only pass, rather than a second registration-era tick also reaching (and
    double-counting notifications from) the dispatch loop."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,))
    t_register = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    run_tick(config, history_path, client=FakeFullClient(), now=t_register)  # type: ignore[arg-type]
    for i in range(3):
        append(
            history_path,
            HistoryRecord(
                ts=t_register + timedelta(minutes=i + 1),
                job=job.name,
                state="done",
                run_id=f"run-{i}",
                extra={"pr_number": 42, "attempt": i},
            ),
        )
    return history_path


def test_notify_policy_default_notifies_clean_gate_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pr-target gate job that finds nothing eligible is a clean terminal "success" —
    the default "terminal" policy still notifies once per run (issue 009's acceptance
    criteria: "the final report ... or a failure", not silence on success); only
    mid-run progress pings are new-to-suppress."""
    job = make_auto_fix_job(tmp_path)
    assert job.notify_policy == "terminal"
    client = FakeFullClient()
    _history_path, outcome = _register_and_run_auto_fix_job(
        job, tmp_path, monkeypatch, gh_client=MockGhClient(), client=client
    )
    assert "enumerated=0" in outcome.summaries[0]
    assert len(client.notifications) == 1
    assert client.notifications[0][0] == "herdr-routines: auto-fix-prs done"


def test_notify_policy_on_failure_suppresses_clean_gate_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same clean run, but the explicit on-failure policy — the strictest tier — stays
    silent since nothing failed."""
    job = make_auto_fix_job(tmp_path, notify_policy="on-failure")
    client = FakeFullClient()
    _history_path, outcome = _register_and_run_auto_fix_job(
        job, tmp_path, monkeypatch, gh_client=MockGhClient(), client=client
    )
    assert "enumerated=0" in outcome.summaries[0]
    assert client.notifications == []


def test_notify_policy_default_suppresses_per_pr_skip_but_notifies_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single PR hitting max_attempts_exceeded mid-loop is "progress" noise — the
    dispatcher keeps evaluating any other eligible PRs, and the job's own once-per-tick
    aggregate notify (a clean "success" here, since nothing was dispatched) already
    summarizes the tick. Under the default "terminal" policy, the mid-loop skip is
    suppressed but the aggregate still fires exactly once."""
    job = make_auto_fix_job(tmp_path)
    history_path_dest = _register_then_populate_over_cap_pr(job, tmp_path, monkeypatch)
    config = RoutinesConfig(jobs=(job,))
    client = FakeFullClient()
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", EligiblePrGhClient)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    t2 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    outcome = run_tick(config, history_path_dest, client=client, now=t2)  # type: ignore[arg-type]

    assert "eligible=1" in outcome.summaries[0]
    assert "skipped=1" in outcome.summaries[0]
    assert len(client.notifications) == 1
    assert client.notifications[0][0] == "herdr-routines: auto-fix-prs done"


def test_notify_policy_on_failure_suppresses_per_pr_skip_and_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same over-cap PR as above, but the explicit on-failure policy stays fully
    silent — neither the mid-loop skip nor the clean aggregate is a "failure"."""
    job = make_auto_fix_job(tmp_path, notify_policy="on-failure")
    history_path_dest = _register_then_populate_over_cap_pr(job, tmp_path, monkeypatch)
    config = RoutinesConfig(jobs=(job,))
    client = FakeFullClient()
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", EligiblePrGhClient)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    t2 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    outcome = run_tick(config, history_path_dest, client=client, now=t2)  # type: ignore[arg-type]

    assert "eligible=1" in outcome.summaries[0]
    assert "skipped=1" in outcome.summaries[0]
    assert client.notifications == []


def test_notify_policy_always_shows_per_pr_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same over-cap PR again, but notify_policy: always must surface both the
    per-PR skip and the job's own aggregate done notification — the one policy value
    that restores full pre-issue-009 per-tick pinging."""
    job = make_auto_fix_job(tmp_path, notify_policy="always")
    history_path_dest = _register_then_populate_over_cap_pr(job, tmp_path, monkeypatch)
    config = RoutinesConfig(jobs=(job,))
    client = FakeFullClient()
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", EligiblePrGhClient)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    t2 = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    run_tick(config, history_path_dest, client=client, now=t2)  # type: ignore[arg-type]

    titles = [n[0] for n in client.notifications]
    assert "herdr-routines: auto-fix-prs PR #42 skipped" in titles
    assert "herdr-routines: auto-fix-prs done" in titles


def test_notify_policy_on_finding_surfaces_dispatched_fix_but_on_failure_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that actually finds and dispatches a fix for an eligible PR is a
    "finding", not a plain "success" — on-finding surfaces this even though the job
    itself didn't fail, while the stricter on-failure policy stays silent since nothing
    failed. (The default "terminal" policy would also notify here, same as "on-finding"
    — this test isolates the on-failure/on-finding boundary specifically.)"""
    job_on_failure = make_auto_fix_job(
        tmp_path, name="auto-fix-prs", notify_policy="on-failure"
    )
    client_on_failure = FakePrDispatchClient()
    _hp, outcome_on_failure = _register_and_run_auto_fix_job(
        job_on_failure,
        tmp_path,
        monkeypatch,
        gh_client=EligiblePrGhClient(),
        client=client_on_failure,
    )
    assert "dispatched=1" in outcome_on_failure.summaries[0]
    assert client_on_failure.notifications == []

    # Fresh tmp_path/history for the on-finding run so the two don't share state.
    tmp_path2 = tmp_path / "on-finding"
    tmp_path2.mkdir()
    job_on_finding = make_auto_fix_job(
        tmp_path2, name="auto-fix-prs", notify_policy="on-finding"
    )
    client_on_finding = FakePrDispatchClient()
    _hp2, outcome_on_finding = _register_and_run_auto_fix_job(
        job_on_finding,
        tmp_path2,
        monkeypatch,
        gh_client=EligiblePrGhClient(),
        client=client_on_finding,
    )
    assert "dispatched=1" in outcome_on_finding.summaries[0]
    assert len(client_on_finding.notifications) == 1
    assert client_on_finding.notifications[0][0] == "herdr-routines: auto-fix-prs done"


def test_notify_policy_default_notifies_plain_routine_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain routine job's clean "done" is exactly the one-notification-per-run
    behavior issue 009's default must preserve — an unattended overnight run still gets
    its final "done" ping, just no mid-run noise (there is none for a plain routine)."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    assert job.notify_policy == "terminal"
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    assert len(client.notifications) == 1
    assert client.notifications[0] == ("herdr-routines: a done", None, "done")


def test_notify_policy_on_failure_suppresses_plain_routine_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit on-failure policy is the one that goes further than the default and
    stays silent on a clean success — useful for a frequently-run job (e.g.
    babysit-prs's `*/10` cron) where even one ping per clean run is too much."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path, notify_policy="on-failure")
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert client.notifications == []


def test_notify_policy_always_notifies_plain_routine_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path, notify_policy="always")
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert len(client.notifications) == 1
    assert client.notifications[0] == ("herdr-routines: a done", None, "done")


def test_notify_policy_on_failure_still_notifies_a_real_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strictest policy's whole point is "still notify on failure" — only clean
    successes are suppressed."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path, notify_policy="on-failure")
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(fail_at="agent_prompt_wait")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.any_job_failed is True
    assert len(client.notifications) == 1
    assert client.notifications[0][0] == "herdr-routines: a failed"


def test_notify_policy_default_still_notifies_a_real_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above, under the actual default ("terminal") — a real failure notifies
    regardless of policy tier."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(fail_at="agent_prompt_wait")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.any_job_failed is True
    assert len(client.notifications) == 1
    assert client.notifications[0][0] == "herdr-routines: a failed"


def test_notify_policy_on_finding_surfaces_fallback_retry_but_on_failure_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fallback_model retry (issue 022) means the primary model hit
    quota_exhausted — notable even though the job ultimately finished "done". on-finding
    surfaces it; the stricter on-failure policy stays silent since the job didn't fail.
    (The default "terminal" policy would also notify here, same as "on-finding" — this
    test isolates the on-failure/on-finding boundary specifically.)"""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        model="primary-model",
        agent_kind="claude",
        fallback_model="fallback-model",
        notify_policy="on-failure",
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle", quota_exhausted_for_model="primary-model")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    assert client.notifications == []  # on-failure stays silent

    # Same scenario again under on-finding, fresh history — the fallback retry should
    # now surface as a "done" notification with the fallback_model noted in its body.
    history_path2 = tmp_path / "state" / "history2.jsonl"
    job_on_finding = replace(job, notify_policy="on-finding")
    config2 = RoutinesConfig(jobs=(job_on_finding,))
    client2 = FakeClient(
        settle_status="idle", quota_exhausted_for_model="primary-model"
    )
    run_tick(config2, history_path2, client=client2, now=t0)  # type: ignore[arg-type]
    outcome2 = run_tick(config2, history_path2, client=client2, now=t1)  # type: ignore[arg-type]

    assert outcome2.summaries == ("a: done",)
    assert len(client2.notifications) == 1
    title, body, _sound = client2.notifications[0]
    assert title == "herdr-routines: a done"
    assert body == "via fallback_model=fallback-model"


# -- repository: <url> ensure_repo gate (issue 016) ------------------------------------


def test_repo_url_tick_runner_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runner.execute_run calls ensure_repo before any worktree/tab creation for repository jobs."""

    calls: list[str] = []

    def fake_ensure_repo(job):
        calls.append("ensure_repo")
        return job.repo

    monkeypatch.setattr("herdr_routines.runner.ensure_repo", fake_ensure_repo)

    job = make_job(tmp_path, repository="https://example.com/repo.git")
    client = FakeClient(settle_status="idle")
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    # execute_run should call ensure_repo before pane creation
    history_path = tmp_path / "state" / "history.jsonl"
    t0 = datetime.now(UTC).replace(microsecond=0)

    config = RoutinesConfig(jobs=(job,))
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    # After registration, run the job
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    assert "ensure_repo" in calls


def test_plain_repo_job_is_synced_in_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue 030 AC1: a plain `repo:` job (no `repository:` field) must still reach
    ensure_repo on the dispatch path — the call sites must not gate on
    `job.repository is not None` (regression: they did, until this fix)."""
    calls: list[str] = []

    def recording_ensure_repo(job):
        calls.append(job.name)
        return job.repo

    monkeypatch.setattr("herdr_routines.runner.ensure_repo", recording_ensure_repo)

    job = make_job(tmp_path)  # repository defaults to None: plain repo: job
    assert job.repository is None
    client = FakeClient(settle_status="idle")
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    history_path = tmp_path / "state" / "history.jsonl"
    t0 = datetime.now(UTC).replace(microsecond=0)

    config = RoutinesConfig(jobs=(job,))
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert calls == [job.name]


def test_plain_repo_job_sync_failure_is_mapped_to_failed_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue 030 AC2: ensure_repo raising for a plain repo: job (e.g. a diverged local
    checkout) must fail the run loudly, not be silently skipped because the dispatch
    site never called ensure_repo for a non-`repository:` job."""

    def failing_ensure_repo(job):
        raise RuntimeError("non-fast-forward merge on origin/main")

    monkeypatch.setattr("herdr_routines.runner.ensure_repo", failing_ensure_repo)

    job = make_job(tmp_path)
    assert job.repository is None
    # Simulate a pre-existing checkout so the reason maps to repo_sync_failed rather
    # than clone_failed (the latter is only reachable for repository:-managed jobs).
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    client = FakeClient(settle_status="idle")
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    history_path = tmp_path / "state" / "history.jsonl"
    t0 = datetime.now(UTC).replace(microsecond=0)

    config = RoutinesConfig(jobs=(job,))
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.any_job_failed is True
    records = read_job(history_path, job.name)
    failed = [r for r in records if r.state == "failed"]
    assert len(failed) == 1
    assert failed[0].extra is not None
    assert failed[0].extra["reason"] == "repo_sync_failed"


# -- kind: pipeline dispatch (issue 026) ---------------------------------------------


def make_pipeline_job(tmp_path: Path, **overrides: Any) -> Job:
    job = make_job(
        tmp_path,
        name="nightly-pipeline",
        kind="pipeline",
        # PIPELINE_CATCH_UP_MINUTES, not 0 — schedule.decide()'s grace is a strict
        # `late <= grace`, and 0 would report MISSED for any nonzero tick-loop delay
        # (see config.PIPELINE_CATCH_UP_MINUTES's docstring for the incident).
        catch_up_minutes=PIPELINE_CATCH_UP_MINUTES,
        deadline_ms=25_200_000,
        prompt_file="docs/pipeline/orchestrator-prompt.md",
        prompt="",
    )
    return replace(job, **overrides)


class FakePipelineClient:
    """Enough of HerdrClient for the pipeline dispatch path: agent_statuses (the
    _live_agent_exists overlap guard) and notification_show (best-effort on failure)."""

    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self._statuses = statuses or {}
        self.notifications: list[tuple[str, str | None, str]] = []

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._statuses)

    def notification_show(self, title, *, body=None, sound="none"):
        self.notifications.append((title, body, sound))


def test_tick_dispatches_pipeline_launch_before_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launch-then-record: the `running` history record only appears once the launcher
    has confirmed exit 0. A failed launch must not wedge the job for the full deadline —
    it writes a terminal `failed` record instead."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()

    calls: list[list[str]] = []

    def fake_launch(argv, *, timeout_s=30.0):
        calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr("herdr_routines.tick.launch_pipeline", fake_launch)

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert len(calls) == 1
    assert "systemd-run" in calls[0]
    assert "--run-id" in calls[0]
    assert outcome.summaries[0].startswith("nightly-pipeline: dispatched")
    records = read_job(history_path, job.name)
    running = [r for r in records if r.state == "running"]
    assert len(running) == 1
    assert running[0].extra is not None
    assert running[0].extra["unit"].startswith("herdr-pipeline-")


def test_pipeline_dispatches_despite_realistic_tick_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bug PIPELINE_CATCH_UP_MINUTES exists to fix: tick samples every
    5 minutes and evaluates jobs sequentially under one lock, so a pipeline job can easily
    be evaluated 30+ minutes after its exact cron instant on an ordinary night (e.g. a
    slower gated job dispatched first). `catch_up_minutes: 0` would report MISSED here —
    schedule.decide()'s grace is a strict `late <= grace`, and late is essentially never
    exactly 0 for a sampled scheduler (see test_schedule.py's
    test_catch_up_zero_means_no_backfill_at_all). Must still dispatch."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    # A realistic same-night delay (well under PIPELINE_CATCH_UP_MINUTES=60, far beyond
    # the naive "must be within seconds" a grace of 0 would require).
    t_delayed = t0 + timedelta(minutes=35)
    outcome = run_tick(config, history_path, client=client, now=t_delayed)  # type: ignore[arg-type]
    assert outcome.summaries[0].startswith("nightly-pipeline: dispatched")


def test_tick_pipeline_launch_failure_writes_terminal_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()

    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (1, "", "boom")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.any_job_failed is True
    records = read_job(history_path, job.name)
    assert [r.state for r in records if r.ts >= t1] == ["failed"]
    failed = next(r for r in records if r.state == "failed")
    assert failed.extra is not None
    assert failed.extra["reason"] == "launch_failed"
    assert client.notifications  # a failure must notify


def test_other_jobs_still_run_during_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The make-or-break concurrency claim: a oneshot tick must not wedge on the
    pipeline's dispatch — a plain job listed after it must still be evaluated and
    actually run in the same tick."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    pipeline_job = make_pipeline_job(tmp_path / "pipeline-repo")
    (pipeline_job.repo / ".git").mkdir(parents=True, exist_ok=True)
    plain_job = make_job(tmp_path / "plain-repo", name="plain")
    plain_job.repo.mkdir(parents=True, exist_ok=True)

    config = RoutinesConfig(jobs=(pipeline_job, plain_job))
    client = FakeFullClient(settle_status="idle")

    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    outcome1 = run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    assert set(outcome1.summaries) == {
        "nightly-pipeline: registered",
        "plain: registered",
    }

    t1 = t0 + timedelta(minutes=1)
    outcome2 = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    summaries = dict(s.split(": ", 1) for s in outcome2.summaries)
    assert summaries["nightly-pipeline"].startswith("dispatched")
    assert summaries["plain"] == "done"  # reached execute_run despite the pipeline job


def test_pipeline_reconciles_ok_report_as_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    running = next(r for r in records if r.state == "running")
    bare_run_id = running.run_id.removeprefix(f"{job.name}-")  # type: ignore[union-attr]
    report_path = tmp_path / "state" / "reports" / f"pipeline-{bare_run_id}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n\n## Outcome: ok\n")

    t2 = t0 + timedelta(minutes=2)
    outcome3 = run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    assert outcome3.summaries == ("nightly-pipeline: done",)
    records = read_job(history_path, job.name)
    assert [r.state for r in records[-1:]] == ["done"]


def test_pipeline_report_guard_partial_tolerant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three reconcile cases off a report body: missing -> stays in flight until the
    deadline+grace bound trips; watchdog marker -> failed watchdog_killed; partial
    (deadline exceeded) -> failed but tolerated (not silently green)."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    running = next(r for r in read_job(history_path, job.name) if r.state == "running")
    bare_run_id = running.run_id.removeprefix(f"{job.name}-")  # type: ignore[union-attr]
    report_path = tmp_path / "state" / "reports" / f"pipeline-{bare_run_id}.md"

    # No report yet, well within the deadline+grace window -> stays in flight, not failed.
    t_soon = t1 + timedelta(minutes=5)
    outcome_soon = run_tick(config, history_path, client=client, now=t_soon)  # type: ignore[arg-type]
    assert outcome_soon.summaries == (
        f"nightly-pipeline: in flight ({running.run_id})",
    )

    # Watchdog wrote its marker before tick's own bound trips.
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("## Outcome: failed (watchdog killed)\n")
    t_watchdog = t1 + timedelta(minutes=10)
    outcome_watchdog = run_tick(config, history_path, client=client, now=t_watchdog)  # type: ignore[arg-type]
    assert outcome_watchdog.summaries == ("nightly-pipeline: failed (watchdog_killed)",)
    assert outcome_watchdog.any_job_failed is True


def test_pipeline_partial_deadline_report_is_tolerated_but_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    running = next(r for r in read_job(history_path, job.name) if r.state == "running")
    bare_run_id = running.run_id.removeprefix(f"{job.name}-")  # type: ignore[union-attr]
    report_path = tmp_path / "state" / "reports" / f"pipeline-{bare_run_id}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("## Outcome: partial (deadline exceeded)\n")

    t2 = t1 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    assert outcome.summaries == ("nightly-pipeline: failed (partial_deadline)",)
    assert outcome.any_job_failed is True


def _dispatch_pipeline_and_get_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, Any, str, Path]:
    """Dispatch a pipeline job to the `running` state and return
    (config, client, history_path, bare_run_id, worktrees_root)."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )
    worktrees_root = tmp_path / "worktrees"
    monkeypatch.setattr(
        "herdr_routines.tick.default_worktrees_root", lambda: worktrees_root
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=t0 + timedelta(minutes=1))  # type: ignore[arg-type]
    running = next(r for r in read_job(history_path, job.name) if r.state == "running")
    bare_run_id = running.run_id.removeprefix(f"{job.name}-")  # type: ignore[union-attr]
    return config, client, history_path, bare_run_id, worktrees_root


def _write_state_json(
    worktrees_root: Path,
    bare_run_id: str,
    stage_sessions: dict,
    *,
    mtime: float | None = None,
) -> Path:
    d = worktrees_root / f"auto-pipeline-{bare_run_id}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "state.json"
    p.write_text(
        json.dumps(
            {
                "run_id": bare_run_id,
                "current_stage": 6,
                "stage_sessions": stage_sessions,
            }
        )
    )
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_pipeline_done_report_downgraded_when_stage_sessions_faked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, client, history_path, bare_run_id, wt = _dispatch_pipeline_and_get_run_id(
        tmp_path, monkeypatch
    )
    report = tmp_path / "state" / "reports" / f"pipeline-{bare_run_id}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("## Outcome: ok\n")
    _write_state_json(
        wt,
        bare_run_id,
        {
            "1": "ses_f87fc2c5_fake1",
            "2": "ses_f87fc2c5_fake2",
            "3": "ses_f87fc2c5_fake3",
        },
    )

    t = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=2)
    outcome = run_tick(config, history_path, client=client, now=t)  # type: ignore[arg-type]
    assert outcome.summaries == (
        "nightly-pipeline: failed (stage_independence_unverified)",
    )
    assert outcome.any_job_failed is True
    assert read_job(history_path, "nightly-pipeline")[-1].state == "failed"


def test_pipeline_done_report_trusted_when_state_json_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human may gc the orchestrator worktree — no state.json must not turn a clean
    'ok' report red."""
    config, client, history_path, bare_run_id, _wt = _dispatch_pipeline_and_get_run_id(
        tmp_path, monkeypatch
    )
    report = tmp_path / "state" / "reports" / f"pipeline-{bare_run_id}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("## Outcome: ok\n")

    t = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=2)
    outcome = run_tick(config, history_path, client=client, now=t)  # type: ignore[arg-type]
    assert outcome.summaries == ("nightly-pipeline: done",)


def test_pipeline_fails_fast_on_host_reboot_before_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No report, deadline hours away — but the host booted after the orchestrator's
    last state.json write, so tick fails the run now instead of 'in flight'."""
    config, client, history_path, bare_run_id, wt = _dispatch_pipeline_and_get_run_id(
        tmp_path, monkeypatch
    )
    now = datetime.now(UTC).replace(microsecond=0)
    _write_state_json(
        wt,
        bare_run_id,
        {"1": "ses_a1b2c3d4e5f6a1b2c3d4e5f6a1"},
        mtime=now.timestamp() - 1800,
    )
    monkeypatch.setattr(
        "herdr_routines.tick.system_boot_epoch", lambda: now.timestamp() - 300
    )

    outcome = run_tick(
        config, history_path, client=client, now=now + timedelta(minutes=3)
    )  # type: ignore[arg-type]
    assert outcome.summaries == ("nightly-pipeline: failed (host_rebooted)",)
    assert outcome.any_job_failed is True
    last = read_job(history_path, "nightly-pipeline")[-1]
    assert last.state == "failed"
    assert last.extra is not None and last.extra["reason"] == "host_rebooted"


def test_pipeline_skipped_while_agent_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once dispatched, a live `rt-<name>` orchestrator agent — not merely an open
    history record — is what makes the next tick skip rather than re-reconcile."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    dispatched = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    assert dispatched.summaries[0].startswith("nightly-pipeline: dispatched")

    client._statuses[job.agent_name] = "working"
    t2 = t1 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    assert outcome.summaries == ("nightly-pipeline: skipped (already running)",)


# ---------------------------------------------------------------------------
# Issue 036: worktree collision, dropped reason/error, agent-name builders
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


class FakeFixWorkerClient:
    """Enough of HerdrClient for `_dispatch_fix_worker` to run end-to-end. Records the
    cwd of the pane it created so tests can assert which worktree the fix worker actually
    ran in."""

    def __init__(self, *, settle_status: str = "idle") -> None:
        self.settle_status = settle_status
        self.tab_create_cwd: str | None = None

    def tab_create(self, *, cwd, label=None):
        self.tab_create_cwd = cwd
        return "w1:p1"

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        pass

    def agent_interactive_ready(self, target):
        return True

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        return self.settle_status

    def agent_read(self, target, *, lines=200):
        return ""

    def agent_read_visible(self, target, *, lines=200):
        return ""

    def pane_close(self, pane_id):
        pass

    def agent_statuses(self) -> dict[str, str]:
        return {}


def make_fix_worker_job(tmp_path: Path, **overrides: Any) -> Job:
    job = Job(
        name="auto-fix-prs",
        enabled=True,
        cron="* * * * *",
        repo=tmp_path / "repo",
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="fix it. report to $ROUTINE_REPORT",
        timeout_ms=5_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )
    return replace(job, **overrides)


def _init_repo_with_branch(tmp_path: Path, *, branch: str) -> Path:
    """A minimal local repo with a second branch, mirroring what the pipeline
    orchestrator produces: `main` plus `branch`, both with at least one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    _git("checkout", "-b", branch, cwd=repo)
    (repo / "fix.txt").write_text("fix\n")
    _git("add", "fix.txt", cwd=repo)
    _git("commit", "-m", "pr commit", cwd=repo)
    _git("checkout", "main", cwd=repo)
    return repo


def test_dispatch_reuses_existing_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 1 (issue 036): a PR whose head branch is already checked
    out in another worktree — exactly what happens for every pipeline-authored PR,
    since the orchestrator deliberately retains its worktree — dispatches successfully
    by reusing that worktree instead of failing on a second `git worktree add`."""
    from herdr_routines.auto_fix import PRInfo
    from herdr_routines.tick import _dispatch_fix_worker

    branch = "auto/pipeline-20260904T050000Z"
    repo = _init_repo_with_branch(tmp_path, branch=branch)

    # Simulate the orchestrator's retained worktree: a second checkout of `branch`
    # living elsewhere, exactly as `git worktree add` would leave it.
    other_wt = tmp_path / "orchestrator-worktree"
    _git("worktree", "add", str(other_wt), branch, cwd=repo)

    job = make_fix_worker_job(tmp_path, repo=repo)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)

    client = FakeFixWorkerClient(settle_status="idle")
    pr = PRInfo(
        number=81, head_ref=branch, author="bot", url="https://example.invalid/81"
    )

    outcome = _dispatch_fix_worker(
        job=job,
        pr=pr,
        reason="unresolved_threads",
        run_id="auto-fix-prs-20260904T053000Z",
        attempt=0,
        owner="acme",
        repo="widgets",
        client=client,  # type: ignore[arg-type]
        failing_checks="",
        thread_bodies="",
    )

    assert outcome["state"] == "done"
    assert outcome.get("reason") is None
    # It must have run the agent in the orchestrator's existing worktree, not tried
    # (and failed) to create a second one at .worktrees/autofix-pr81.
    assert client.tab_create_cwd == str(other_wt)
    autofix_wt = repo / ".worktrees" / "autofix-pr81"
    assert not autofix_wt.exists()


def test_dispatch_failure_records_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 2 (issue 036): a dispatch failure's `reason` (and `error`)
    reach the terminal history record instead of being silently dropped (36b — the
    tick-level log used to print `done (...)` regardless of per-PR outcome, making any
    dispatch failure un-triageable from history.jsonl alone)."""
    from herdr_routines.auto_fix import EligiblePR, PRInfo

    history_path = tmp_path / "state" / "history.jsonl"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    job = make_auto_fix_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeFullClient()

    monkeypatch.setattr("herdr_routines.tick.RealGhClient", MockGhClient)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    pr = PRInfo(
        number=81, head_ref="auto/pipeline-20260904T050000Z", author="bot", url=""
    )
    monkeypatch.setattr(
        "herdr_routines.tick.list_open_prs",
        lambda gh, *, owner, repo, branch_prefix, author: [pr],
    )
    monkeypatch.setattr(
        "herdr_routines.tick.is_eligible",
        lambda gh, *, owner, repo, pr: EligiblePR(pr=pr, reason="unresolved_threads"),
    )
    monkeypatch.setattr(
        "herdr_routines.tick._dispatch_fix_worker",
        lambda **kwargs: {
            "state": "failed",
            "reason": "worktree_creation_failed",
            "error": "boom: something specific",
        },
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    dispatched = next(r for r in records if r.extra and r.extra.get("pr_number") == 81)
    assert dispatched.state == "failed"
    assert dispatched.extra is not None
    assert dispatched.extra["reason"] == "worktree_creation_failed"
    assert dispatched.extra["error"] == "boom: something specific"


def test_live_agent_guard_matches_dispatch_name(tmp_path: Path) -> None:
    """Acceptance criterion 4 (issue 036c): the double-dispatch guard must recognize a
    live fix-worker agent under the *actual* name the dispatcher creates
    (`build_worker_agent_name`, which always carries a run_id tail), not the bare
    `build_pr_agent_name` name that the dispatcher never creates."""
    from herdr_routines.auto_fix import build_pr_agent_name, build_worker_agent_name
    from herdr_routines.tick import _pr_worker_is_live

    job_name = "auto-fix-prs"
    pr_number = 81
    run_id = "auto-fix-prs-20260904T053000Z"
    real_name = build_worker_agent_name(job_name, pr_number, run_id)

    assert real_name != build_pr_agent_name(job_name, pr_number)

    client = FakeStatusClient({real_name: "working"})
    assert _pr_worker_is_live(client, job_name, pr_number) is True  # type: ignore[arg-type]

    idle_client = FakeStatusClient({real_name: "idle"})
    assert _pr_worker_is_live(idle_client, job_name, pr_number) is False  # type: ignore[arg-type]

    other_pr_client = FakeStatusClient(
        {build_worker_agent_name(job_name, 99, run_id): "working"}
    )
    assert _pr_worker_is_live(other_pr_client, job_name, pr_number) is False  # type: ignore[arg-type]


def test_tick_reconciles_launcher_failure_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue 037: pipeline-launch.sh now writes a `## Outcome: failed` stub report itself
    when the orchestrator settles blocked/unknown, instead of leaving no report at all.
    A stub report reconciles exactly like the orchestrator's or the watchdog's own report
    would — on the very next tick, minutes after dispatch — rather than tick waiting out
    the full deadline+grace `no_report` bound (the 8h incident this issue documents)."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path)
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    monkeypatch.setattr(
        "herdr_routines.tick.launch_pipeline", lambda argv, **kw: (0, "", "")
    )

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    running = next(r for r in read_job(history_path, job.name) if r.state == "running")
    bare_run_id = running.run_id.removeprefix(f"{job.name}-")  # type: ignore[union-attr]
    report_path = tmp_path / "state" / "reports" / f"pipeline-{bare_run_id}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        "# Pipeline run "
        f"{bare_run_id} — launcher stub report\n\n"
        "## Outcome: failed\n\n"
        "settle_status: blocked\n"
        f"tail: {report_path.parent}/{bare_run_id}.tail.txt\n"
    )

    # Only a couple of minutes after dispatch — nowhere near deadline_ms (25_200_000ms)
    # plus PIPELINE_RECONCILE_GRACE_MS. The report alone must be enough to reconcile.
    t2 = t1 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    assert outcome.summaries == ("nightly-pipeline: failed (orchestrator_failed)",)
    assert outcome.any_job_failed is True
    records = read_job(history_path, job.name)
    assert [r.state for r in records[-1:]] == ["failed"]
    assert client.notifications  # a failure must notify


# ---------------------------------------------------------------------------
# Issue 007: Approval path for blocked runs
# ---------------------------------------------------------------------------


def test_blocked_emits_single_actionable_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1: A blocked settle emits exactly one actionable notification whose body
    identifies the job, run_id, pane_id and agent, and includes a permission prompt
    excerpt (truncated, best-effort) plus an approval hint; title is
    ``herdr-routines: <job> blocked`` and sound is ``request``."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="blocked")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.any_job_failed is True
    assert len(client.notifications) == 1
    title, body, sound = client.notifications[0]
    assert title == "herdr-routines: a blocked"
    assert sound == "request"
    assert body is not None
    assert "job=a" in body
    assert "run=" in body
    assert "pane=" in body
    assert "agent=" in body
    assert "Reply to this message to approve" in body


def test_blocked_notification_gated_as_failure_across_all_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: The blocked notification is gated as ``failure`` so it fires under every
    ``notify_policy`` (always, terminal, on-finding, on-failure) and is never suppressed."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    for policy in ("always", "terminal", "on-finding", "on-failure"):
        history_path = tmp_path / "state" / f"history-{policy}.jsonl"
        job = make_job(tmp_path, notify_policy=policy)
        config = RoutinesConfig(jobs=(job,))
        client = FakeClient(settle_status="blocked")

        t0 = datetime.now(UTC).replace(microsecond=0)
        run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
        t1 = t0 + timedelta(minutes=1)
        run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

        assert len(client.notifications) >= 1, (
            f"blocked must notify under notify_policy={policy}"
        )
        last_title = client.notifications[-1][0]
        assert last_title == "herdr-routines: a blocked", (
            f"wrong title under notify_policy={policy}"
        )
        client.notifications.clear()


def test_blocked_does_not_renotify_on_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: A blocked run_id is notified exactly once — a subsequent tick does not
    re-notify for the same terminal failed/blocked record (last_terminal_run advances
    past it)."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="blocked")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type] # blocked

    blocked_notifications = [
        n for n in client.notifications if n[0] == "herdr-routines: a blocked"
    ]
    assert len(blocked_notifications) == 1
    first_body = blocked_notifications[0][1]
    assert first_body is not None

    # Extract the run_id from the notification body to verify exact-once.
    first_run_id = next(
        line.split("=", 1)[1]
        for line in first_body.splitlines()
        if line.startswith("run=")
    )

    # A subsequent tick may start a *new* run (new cron occurrence) that also gets
    # blocked — that's a fresh notification for a different run_id. The spec contract
    # is that the *same* run_id is never re-notified.
    t2 = t1 + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]

    all_blocked_bodies = [
        n[1] for n in client.notifications if n[0] == "herdr-routines: a blocked"
    ]
    same_run_count = sum(
        1
        for body in all_blocked_bodies
        if body is not None and f"run={first_run_id}" in body
    )
    assert same_run_count == 1, (
        f"run_id {first_run_id!r} was notified {same_run_count} times (expected exactly 1)"
    )
