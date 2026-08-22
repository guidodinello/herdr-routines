"""Tests for runner.py's orchestration, against a fake HerdrClient (tier 2 — no `herdr` binary
involved). See docs/plan-v1.md §7."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_routines.config import Job
from herdr_routines.herdr import HerdrClient, HerdrCliError
from herdr_routines.runner import (
    build_branch_name,
    build_dry_run_argv,
    execute_run,
    make_run_id,
    substitute_prompt,
)


def make_job(tmp_path: Path, **overrides) -> Job:
    defaults = {
        "name": "a",
        "enabled": True,
        "cron": "0 3 * * *",
        "repo": tmp_path,
        "workspace": "worktree",
        "base": "main",
        "agent_kind": "claude",
        "model": None,
        "prompt": "Write a report to $ROUTINE_REPORT for job $ROUTINE_JOB run $ROUTINE_RUN_ID.",
        "timeout_ms": 60_000,
        "start_timeout_ms": 30_000,
        "catch_up_minutes": 120,
        "timezone": "UTC",
        "on_missed": "log",
    }
    defaults.update(overrides)
    return Job(**defaults)


class ScriptedClient:
    """A HerdrClient-shaped fake for runner-level tests, scripted per method rather than at the
    subprocess seam (that's what test_herdr.py covers)."""

    def __init__(
        self,
        *,
        pane_id: str = "w1:p1",
        agent_status: str = "idle",
        write_report_at: Path | None = None,
        report_content: str = "# Report\n\nFindings.\n",
        raise_on: str | None = None,
    ) -> None:
        self.pane_id = pane_id
        self.agent_status = agent_status
        self.write_report_at = write_report_at
        self.report_content = report_content
        self.raise_on = raise_on
        self.calls: list[str] = []

    def worktree_create(self, *, cwd, branch, base, label=None):
        self.calls.append("worktree_create")
        if self.raise_on == "worktree_create":
            raise HerdrCliError("boom", exit_code=1)
        return self.pane_id

    def tab_create(self, *, cwd, label=None):
        self.calls.append("tab_create")
        return self.pane_id

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms):
        self.calls.append("agent_start")
        if self.raise_on == "agent_start":
            raise HerdrCliError("boom", exit_code=1)

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self.calls.append("agent_prompt_wait")
        if self.raise_on == "agent_prompt_wait":
            raise HerdrCliError("boom", exit_code=1)
        if self.write_report_at is not None:
            self.write_report_at.parent.mkdir(parents=True, exist_ok=True)
            self.write_report_at.write_text(self.report_content)
        return self.agent_status

    def agent_read(self, target, *, lines=200):
        self.calls.append("agent_read")
        return "some tail output"


@pytest.fixture(autouse=True)
def _isolated_reports_dir(tmp_path, monkeypatch):
    reports_dir = tmp_path / "state" / "reports"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    return reports_dir


def test_make_run_id_is_deterministic_from_occurrence() -> None:
    occ = datetime(2026, 8, 22, 3, 0, 0, tzinfo=UTC)
    assert make_run_id("nightly-audit", occ) == "nightly-audit-20260822T030000Z"


def test_build_branch_name_reuses_run_id_timestamp() -> None:
    run_id = "nightly-audit-20260822T030000Z"
    assert (
        build_branch_name("nightly-audit", run_id)
        == "auto/nightly-audit-20260822T030000Z"
    )


def test_substitute_prompt_fills_all_placeholders(tmp_path: Path) -> None:
    report_path = tmp_path / "state" / "reports" / "a-1.md"
    text = substitute_prompt(
        "report=$ROUTINE_REPORT job=$ROUTINE_JOB run=$ROUTINE_RUN_ID",
        report_path=report_path,
        job_name="a",
        run_id="a-1",
    )
    assert str(report_path) in text
    assert "job=a" in text
    assert "run=a-1" in text


def test_build_dry_run_argv_worktree_mode(tmp_path: Path) -> None:
    job = make_job(tmp_path, workspace="worktree")
    commands = build_dry_run_argv(job, run_id="a-1")
    assert commands[0][:3] == ["herdr", "worktree", "create"]
    assert "--branch" in commands[0]
    assert "--label" in commands[0] and job.name in commands[0]
    assert commands[1][:3] == ["herdr", "agent", "start"]
    assert commands[2][:3] == ["herdr", "agent", "prompt"]


def test_build_dry_run_argv_root_mode(tmp_path: Path) -> None:
    job = make_job(tmp_path, workspace="root")
    commands = build_dry_run_argv(job, run_id="a-1")
    assert commands[0][:3] == ["herdr", "tab", "create"]
    assert "--label" in commands[0] and job.name in commands[0]


def test_execute_run_success_writes_report(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    job = make_job(tmp_path)
    run_id = "a-run1"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(agent_status="idle", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert outcome.report_written is True
    assert outcome.report_bytes > 0
    assert outcome.final_agent_status == "idle"
    assert client.calls == [
        "worktree_create",
        "agent_start",
        "agent_prompt_wait",
        "agent_read",
    ]


def test_execute_run_done_status_also_succeeds(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    job = make_job(tmp_path)
    run_id = "a-run2"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(agent_status="done", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"


def test_execute_run_missing_report_is_failed_not_done(tmp_path: Path) -> None:
    """Direct test of the post-run verification in docs/plan-v1.md §6: a clean settle with no
    report file must not be recorded as done."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status="idle", write_report_at=None)
    outcome = execute_run(job, client, run_id="a-run3")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "no_report"


def test_execute_run_blocked_status_is_failed_with_reason(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    job = make_job(tmp_path)
    run_id = "a-run4"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(agent_status="blocked", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "blocked"


def test_execute_run_unknown_status_is_interrupted_unknown(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """An unresolvable settle status maps to the same terminal state stale-run recovery uses
    for a crashed/killed run (docs/plan-v1.md §4), not a plain "failed"."""
    job = make_job(tmp_path)
    run_id = "a-run5"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(agent_status="unknown", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "interrupted_unknown"
    assert outcome.reason == "unsettled_status_unknown"


def test_execute_run_empty_report_is_failed_not_done(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """A report file that exists but is empty must not be recorded as done."""
    job = make_job(tmp_path)
    run_id = "a-run5b"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(
        agent_status="idle", write_report_at=report_path, report_content=""
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "no_report"
    assert outcome.report_written is True
    assert outcome.report_bytes == 0


def test_execute_run_worktree_creation_failure_short_circuits(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="worktree_create")
    outcome = execute_run(job, client, run_id="a-run6")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "pane_creation_failed"
    assert client.calls == ["worktree_create"]


def test_execute_run_agent_start_failure_short_circuits(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_start")
    outcome = execute_run(job, client, run_id="a-run7")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_start_failed"
    assert client.calls == ["worktree_create", "agent_start"]


def test_execute_run_prompt_failure_short_circuits(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_prompt_wait")
    outcome = execute_run(job, client, run_id="a-run8")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert client.calls == ["worktree_create", "agent_start", "agent_prompt_wait"]


def test_real_herdr_client_satisfies_the_shape_used_by_execute_run() -> None:
    """Ensures ScriptedClient's protocol above doesn't silently drift from HerdrClient's real
    method signatures."""
    for name in (
        "worktree_create",
        "tab_create",
        "agent_start",
        "agent_prompt_wait",
        "agent_read",
    ):
        assert hasattr(HerdrClient, name)
