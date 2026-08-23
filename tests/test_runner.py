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
        interactive_ready: bool = True,
        stale_workspace: str | None = None,
        write_report_at: Path | None = None,
        report_content: str = "# Report\n\nFindings.\n",
        raise_on: str | None = None,
    ) -> None:
        self.pane_id = pane_id
        self.agent_status = agent_status
        self.interactive_ready = interactive_ready
        self.stale_workspace = stale_workspace
        self.write_report_at = write_report_at
        self.report_content = report_content
        self.raise_on = raise_on
        self.calls: list[str] = []
        self.closed_workspaces: list[str] = []
        self.started_with_model: str | None = None

    def worktree_create(self, *, cwd, branch, base, label=None):
        self.calls.append("worktree_create")
        if self.raise_on == "worktree_create":
            raise HerdrCliError("boom", exit_code=1)
        return self.pane_id

    def settled_agent_workspace(self, name):
        self.calls.append("settled_agent_workspace")
        if self.raise_on == "settled_agent_workspace":
            raise HerdrCliError("boom", exit_code=1)
        return self.stale_workspace

    def workspace_close(self, workspace_id):
        self.calls.append("workspace_close")
        self.closed_workspaces.append(workspace_id)

    def tab_create(self, *, cwd, label=None):
        self.calls.append("tab_create")
        return self.pane_id

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        self.calls.append("agent_start")
        self.started_with_model = model
        if self.raise_on == "agent_start":
            raise HerdrCliError("boom", exit_code=1)

    def agent_interactive_ready(self, target):
        self.calls.append("agent_interactive_ready")
        if self.raise_on == "agent_interactive_ready":
            raise HerdrCliError("boom", exit_code=1)
        return self.interactive_ready

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


def test_build_dry_run_argv_passes_claude_model_flag(tmp_path: Path) -> None:
    job = make_job(tmp_path, agent_kind="claude", model="opus")
    commands = build_dry_run_argv(job, run_id="a-1")
    agent_start_argv = commands[1]
    assert agent_start_argv[-3:] == ["--", "--model", "opus"]


def test_build_dry_run_argv_passes_opencode_model_flag(tmp_path: Path) -> None:
    job = make_job(tmp_path, agent_kind="opencode", model="opencode/big-pickle")
    commands = build_dry_run_argv(job, run_id="a-1")
    agent_start_argv = commands[1]
    assert agent_start_argv[-3:] == ["--", "-m", "opencode/big-pickle"]


def test_execute_run_passes_model_through_to_agent_start(tmp_path: Path) -> None:
    job = make_job(tmp_path, agent_kind="opencode", model="opencode/big-pickle")
    client = ScriptedClient()
    execute_run(job, client, run_id="a-1")  # type: ignore[arg-type]
    assert client.started_with_model == "opencode/big-pickle"


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
        "settled_agent_workspace",
        "worktree_create",
        "agent_start",
        "agent_interactive_ready",
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
    assert client.calls == ["settled_agent_workspace", "worktree_create"]


def test_execute_run_agent_start_failure_short_circuits(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_start")
    outcome = execute_run(job, client, run_id="a-run7")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_start_failed"
    assert client.calls == [
        "settled_agent_workspace",
        "worktree_create",
        "agent_start",
    ]


def test_execute_run_prompt_failure_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ())
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_prompt_wait")
    outcome = execute_run(job, client, run_id="a-run8")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert client.calls == [
        "settled_agent_workspace",
        "worktree_create",
        "agent_start",
        "agent_interactive_ready",
        "agent_prompt_wait",
    ]


def test_execute_run_prompts_only_after_readiness(tmp_path: Path) -> None:
    """The prompt must not race the agent TUI's startup: live on the Pi (2026-08-22), prompting
    ~3s after `agent start` failed server-side while a prompt after settle succeeded."""
    job = make_job(tmp_path)
    client = ScriptedClient()
    execute_run(job, client, run_id="a-run9")  # type: ignore[arg-type]
    assert client.calls.index("agent_interactive_ready") < client.calls.index(
        "agent_prompt_wait"
    )


def test_execute_run_unready_agent_fails_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("herdr_routines.runner.READY_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path, start_timeout_ms=50)
    client = ScriptedClient(interactive_ready=False)
    outcome = execute_run(job, client, run_id="a-run10")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_not_interactive"
    assert "agent_prompt_wait" not in client.calls
    assert outcome.agent_name == "rt-a"


def test_execute_run_ready_polling_survives_cli_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transiently unreachable server during the readiness wait must not crash the run —
    it keeps polling until the deadline, then maps to agent_not_interactive with the last
    poll error preserved for attribution."""
    monkeypatch.setattr("herdr_routines.runner.READY_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path, start_timeout_ms=50)
    client = ScriptedClient(raise_on="agent_interactive_ready")
    outcome = execute_run(job, client, run_id="a-run11")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_not_interactive"
    assert "HerdrCliError: boom" in (outcome.error or "")


class MissingBinaryPollClient(ScriptedClient):
    """Raises FileNotFoundError from the readiness probe — what spawning `herdr` does when
    the binary vanishes mid-run, the exact escape flagged by PR#14's blocking finding."""

    def agent_interactive_ready(self, target):
        self.calls.append("agent_interactive_ready")
        raise FileNotFoundError(2, "No such file or directory: 'herdr'")


def test_execute_run_readiness_polling_survives_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError during readiness polling used to escape execute_run — its only unwrapped
    client call site — leaving tick.py without a terminal history record and skipping the
    remaining jobs in the tick. It must degrade to a failed RunOutcome instead."""
    monkeypatch.setattr("herdr_routines.runner.READY_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path, start_timeout_ms=50)
    client = MissingBinaryPollClient()
    outcome = execute_run(job, client, run_id="a-run18")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_not_interactive"
    assert "FileNotFoundError" in (outcome.error or "")
    assert "agent_prompt_wait" not in client.calls


class FlakyPromptClient(ScriptedClient):
    """Fails the first N prompt attempts with a non-timeout server error (the early
    session-not-ready EmptyResponse signature), then succeeds."""

    def __init__(self, *, fail_times: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        self._fail_times = fail_times

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        if self._fail_times > 0:
            self._fail_times -= 1
            self.calls.append("agent_prompt_wait[failed]")
            raise HerdrCliError("EmptyResponse", exit_code=1)
        return super().agent_prompt_wait(
            target=target, text=text, timeout_ms=timeout_ms
        )


def test_execute_run_retries_early_prompt_failures(
    tmp_path: Path,
    _isolated_reports_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", (0.0,))
    job = make_job(tmp_path)
    run_id = "a-run12"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = FlakyPromptClient(fail_times=1, write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert client.calls.count("agent_prompt_wait[failed]") == 1


def test_execute_run_prompt_retries_exhausted_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", (0.0, 0.0))
    job = make_job(tmp_path)
    client = FlakyPromptClient(fail_times=99)
    outcome = execute_run(job, client, run_id="a-run13")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert "EmptyResponse" in (outcome.error or "")


def test_execute_run_never_resends_after_settle_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON code:"timeout" error proves the prompt was delivered — retrying would make the
    agent do the work twice."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", (0.0,))

    class SettleTimeoutClient(ScriptedClient):
        def agent_prompt_wait(self, *, target, text, timeout_ms):
            self.calls.append("agent_prompt_wait")
            raise HerdrCliError(
                "timed out waiting for agent status",
                exit_code=1,
                error_body={"error": {"code": "timeout", "message": "timed out"}},
            )

    job = make_job(tmp_path)
    client = SettleTimeoutClient()
    outcome = execute_run(job, client, run_id="a-run14")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert client.calls.count("agent_prompt_wait") == 1


def test_real_herdr_client_satisfies_the_shape_used_by_execute_run() -> None:
    """Ensures ScriptedClient's protocol above doesn't silently drift from HerdrClient's real
    method signatures."""
    for name in (
        "settled_agent_workspace",
        "workspace_close",
        "worktree_create",
        "tab_create",
        "agent_start",
        "agent_interactive_ready",
        "agent_prompt_wait",
        "agent_read",
    ):
        assert hasattr(HerdrClient, name)


def test_execute_run_reaps_previous_run_workspace_before_starting(
    tmp_path: Path,
    _isolated_reports_dir: Path,
) -> None:
    """Recurring jobs reuse one agent name while runs deliberately leave their workspace
    behind (plan-v1 §6) — the settled pane from yesterday must be closed before today's
    agent start, or herdr rejects the duplicate name."""
    job = make_job(tmp_path)
    run_id = "a-run15"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(stale_workspace="w9", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert client.closed_workspaces == ["w9"]
    assert client.calls.index("workspace_close") < client.calls.index("worktree_create")


def test_execute_run_leaves_panes_alone_when_no_stale_workspace(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    job = make_job(tmp_path)
    run_id = "a-run16"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(stale_workspace=None, write_report_at=report_path)
    execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert "workspace_close" not in client.calls


def test_execute_run_survives_stale_workspace_reap_errors(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Failing to reap (e.g. server blip) must not abort the run — the worst case is the old
    duplicate-name failure at agent start, which is captured like any other failure."""
    job = make_job(tmp_path)
    run_id = "a-run17"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(
        raise_on="settled_agent_workspace", write_report_at=report_path
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
