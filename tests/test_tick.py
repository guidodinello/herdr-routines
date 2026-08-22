"""Tests for run_tick's TickOutcome.any_job_failed — what `_cmd_tick` (cli.py) maps to the
process exit code. See docs/plan-v1.md §4."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from herdr_routines.config import Job, RoutinesConfig
from herdr_routines.herdr import HerdrCliError
from herdr_routines.tick import run_tick


def make_job(tmp_path: Path, **overrides) -> Job:
    defaults = {
        "name": "a",
        "enabled": True,
        "cron": "* * * * *",
        "repo": tmp_path,
        "workspace": "root",
        "base": "main",
        "agent_kind": "claude",
        "model": None,
        "prompt": "report to $ROUTINE_REPORT",
        "timeout_ms": 5_000,
        "start_timeout_ms": 30_000,
        "catch_up_minutes": 120,
        "timezone": "UTC",
        "on_missed": "log",
    }
    defaults.update(overrides)
    return Job(**defaults)


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

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self._maybe_raise("agent_prompt_wait")
        Path(text.rsplit(maxsplit=1)[-1]).write_text("# ok\n")
        return self.settle_status

    def agent_read(self, target, *, lines=200):
        return ""

    def agent_list_names(self) -> frozenset[str]:
        return frozenset()

    def notification_show(self, title, *, body=None, sound="none"):
        pass


def test_no_failure_when_nothing_is_due(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path, cron="0 3 * * *")  # not due at the fixed `now` below
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient()
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    outcome = run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: registered",)
    assert outcome.any_job_failed is False


def test_no_failure_for_a_successful_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")

    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type] # registers
    t1 = t0.replace(minute=(t0.minute + 1) % 60)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: done",)
    assert outcome.any_job_failed is False


def test_failure_flagged_when_a_due_job_actually_fails(
    tmp_path: Path, monkeypatch
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
    t1 = t0.replace(minute=(t0.minute + 1) % 60)
    outcome = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: failed (agent_start_failed)",)
    assert outcome.any_job_failed is True


def test_failure_not_flagged_for_a_merely_missed_job(
    tmp_path: Path, monkeypatch
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


def test_failure_not_flagged_for_a_skipped_job(tmp_path: Path, monkeypatch) -> None:
    """A job skipped because it's already running/live is not this tick's own failure."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(tmp_path)
    config = RoutinesConfig(jobs=(job,))

    class LiveAgentClient(FakeClient):
        def agent_list_names(self) -> frozenset[str]:
            return frozenset({job.agent_name})

    client = LiveAgentClient()
    now = datetime.now(UTC)
    run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type] # registers
    outcome = run_tick(config, history_path, client=client, now=now)  # type: ignore[arg-type]

    assert outcome.summaries == ("a: skipped (agent already live)",)
    assert outcome.any_job_failed is False
