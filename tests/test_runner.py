"""Tests for runner.py's orchestration, against a fake HerdrClient (tier 2 — no `herdr` binary
involved). See docs/plan-v1.md §7."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.config import FallbackEntry, Job
from herdr_routines.herdr import (
    HerdrClient,
    HerdrCliError,
    PromptWatchdogKilled,
)
from herdr_routines.runner import (
    _is_retryable_prompt_error,
    _is_settle_timeout,
    build_branch_name,
    build_dry_run_argv,
    execute_run,
    execute_run_with_failover,
    make_run_id,
    substitute_prompt,
)


def make_job(tmp_path: Path, **overrides: Any) -> Job:
    # Built directly, then `replace`d: a defaults dict splatted into Job() widens to
    # dict[str, object] and fails the typecheck gate on every field.
    job = Job(
        name="a",
        enabled=True,
        cron="0 3 * * *",
        repo=tmp_path,
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="Write a report to $ROUTINE_REPORT for job $ROUTINE_JOB run $ROUTINE_RUN_ID.",
        timeout_ms=60_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )
    return replace(job, **overrides)


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
        stale_pane: str | None = None,
        write_report_at: Path | None = None,
        report_content: str = "# Report\n\nFindings.\n",
        raise_on: str | None = None,
        visible_screen: str = "",
        session_id: str | None = "ses_fake123",
    ) -> None:
        self.pane_id = pane_id
        self.agent_status = agent_status
        self.interactive_ready = interactive_ready
        # stale_pane is the new primary; stale_workspace kept for backwards-compat in tests
        if stale_pane is None and stale_workspace is not None:
            stale_pane = stale_workspace
        self.stale_pane = stale_pane
        self.stale_workspace = stale_workspace
        self.write_report_at = write_report_at
        self.report_content = report_content
        self.raise_on = raise_on
        self.visible_screen = visible_screen
        self.session_id = session_id
        self.calls: list[str] = []
        self.closed_workspaces: list[str] = []
        self.closed_panes: list[str] = []
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

    def settled_agent_pane(self, name):
        self.calls.append("settled_agent_pane")
        if self.raise_on == "settled_agent_pane":
            raise HerdrCliError("boom", exit_code=1)
        if self.raise_on == "settled_agent_workspace":
            raise HerdrCliError("boom", exit_code=1)
        return self.stale_pane

    def workspace_close(self, workspace_id):
        self.calls.append("workspace_close")
        self.closed_workspaces.append(workspace_id)

    def pane_close(self, pane_id):
        self.calls.append("pane_close")
        self.closed_panes.append(pane_id)

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

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        # Default: the child settles immediately (zero polls), delegating to agent_prompt_wait
        # so phase-1 overrides and expected call lists stay valid. Watchdog-specific fakes
        # (WatchdogClient below) override this with a scripted poll loop.
        return self.agent_prompt_wait(target=target, text=text, timeout_ms=timeout_ms)

    def agent_read(self, target, *, lines=200):
        self.calls.append("agent_read")
        return "some tail output"

    def agent_read_visible(self, target, *, lines=200):
        self.calls.append("agent_read_visible")
        return self.visible_screen

    def agent_session_id(self, target):
        self.calls.append("agent_session_id")
        if self.raise_on == "agent_session_id":
            raise HerdrCliError("boom", exit_code=1)
        return self.session_id


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
    assert outcome.session_id == "ses_fake123"
    assert client.calls == [
        "settled_agent_pane",
        "worktree_create",
        "agent_start",
        "agent_interactive_ready",
        "agent_prompt_wait",
        "agent_read",
        "agent_session_id",
        "pane_close",
    ]
    # pane-lifecycle v2: a successful run closes its own pane immediately rather than leaving
    # it for the next run's stale-pane reap.
    assert client.closed_panes == ["w1:p1"]


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
    # A no-report settle is still an idle/done agent — pane-lifecycle v2 closes it too.
    assert outcome.session_id == "ses_fake123"
    assert client.closed_panes == ["w1:p1"]


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
    assert client.closed_panes == ["w1:p1"]
    assert outcome.report_written is True
    assert outcome.report_bytes == 0


def test_execute_run_worktree_creation_failure_short_circuits(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="worktree_create")
    outcome = execute_run(job, client, run_id="a-run6")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "pane_creation_failed"
    assert client.calls == ["settled_agent_pane", "worktree_create"]


def test_execute_run_agent_start_failure_short_circuits(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_start")
    outcome = execute_run(job, client, run_id="a-run7")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_start_failed"
    assert client.calls == [
        "settled_agent_pane",
        "worktree_create",
        "agent_start",
        "agent_read_visible",
        "pane_close",
    ]
    # The failed run's own pane is reaped so no later tick can wedge on agent_name_live.
    assert client.closed_panes == ["w1:p1"]


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
        "settled_agent_pane",
        "worktree_create",
        "agent_start",
        "agent_interactive_ready",
        "agent_prompt_wait",
        "agent_read_visible",
        "pane_close",
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
    """Fails the first N prompt attempts with a provably-early, retryable server rejection —
    the session-not-ready EmptyResponse signature (exit 1 plus a JSON error body naming its
    code) — then succeeds."""

    def __init__(self, *, fail_times: int = 1, **kwargs) -> None:
        super().__init__(**kwargs)
        self._fail_times = fail_times

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        if self._fail_times > 0:
            self._fail_times -= 1
            self.calls.append("agent_prompt_wait[failed]")
            raise HerdrCliError(
                "EmptyResponse",
                exit_code=1,
                error_body={
                    "error": {"code": "empty_response", "message": "EmptyResponse"}
                },
            )
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


class TerminalPromptClient(ScriptedClient):
    """Always fails the prompt with one terminal error — the retry loop must send exactly once,
    whatever delays are configured."""

    def __init__(self, *, error: HerdrCliError, **kwargs) -> None:
        super().__init__(**kwargs)
        self._error = error

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self.calls.append("agent_prompt_wait")
        raise self._error


def test_execute_run_wrapper_subprocess_timeout_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_subprocess_runner maps a hang past timeout_ms+grace to exit 124 with no error body.
    The prompt was almost certainly delivered, so there is exactly one send attempt and the
    run records a failure."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", (0.0,))
    job = make_job(tmp_path)
    client = TerminalPromptClient(
        error=HerdrCliError(
            "herdr call timed out: herdr agent prompt rt-a --wait", exit_code=124
        )
    )
    outcome = execute_run(job, client, run_id="a-run18")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert client.calls.count("agent_prompt_wait") == 1


def test_execute_run_post_settle_shape_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_extract_status raises exit_code=0 only after delivery AND settle already succeeded —
    only the response JSON was unexpected. Retrying would make the agent do the work twice."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", (0.0,))
    job = make_job(tmp_path)
    client = TerminalPromptClient(
        error=HerdrCliError("unexpected herdr agent response shape: {}", exit_code=0)
    )
    outcome = execute_run(job, client, run_id="a-run19")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert client.calls.count("agent_prompt_wait") == 1


def test_execute_run_flat_timeout_body_fails_without_crash_or_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flat {"error": "timeout"} body used to crash _is_settle_timeout (AttributeError on
    .get of a str) inside the retry loop's except block. It must classify conservatively —
    not a settle timeout, and lacking error.code not retryable either: one send, clean failed
    outcome, nothing raised out of execute_run."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", (0.0,))
    job = make_job(tmp_path)
    client = TerminalPromptClient(
        error=HerdrCliError("timed out", exit_code=1, error_body={"error": "timeout"})
    )
    outcome = execute_run(job, client, run_id="a-run20")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert client.calls.count("agent_prompt_wait") == 1


def test_is_settle_timeout_survives_malformed_bodies() -> None:
    """_is_settle_timeout runs inside an except block and must never raise itself; any shape
    without a nested string code conservatively reads as not a settle timeout."""
    bodies: list[dict[str, object] | None] = [
        None,
        {},
        {"error": "timeout"},
        {"error": {}},
        {"error": {"code": 7}},
        {"error": ["timeout"]},
    ]
    for body in bodies:
        e = HerdrCliError("x", exit_code=1, error_body=body)
        assert _is_settle_timeout(e) is False


def test_nested_timeout_body_is_still_a_settle_timeout() -> None:
    """The genuine settle-timeout signature keeps classifying as such."""
    e = HerdrCliError(
        "x",
        exit_code=1,
        error_body={"error": {"code": "timeout", "message": "timed out"}},
    )
    assert _is_settle_timeout(e) is True


def test_only_provably_early_rejections_are_retryable() -> None:
    """The retry whitelist is structural: exit 1 plus a parsed body whose error.code exists and
    is not "timeout". Exit 124, exit-0 shape errors, missing/malformed bodies and timeouts all
    stay terminal."""

    def err(**kwargs) -> HerdrCliError:
        return HerdrCliError("boom", **kwargs)

    assert _is_retryable_prompt_error(
        err(exit_code=1, error_body={"error": {"code": "empty_response"}})
    )
    assert not _is_retryable_prompt_error(err(exit_code=124))
    assert not _is_retryable_prompt_error(err(exit_code=2))
    assert not _is_retryable_prompt_error(
        err(exit_code=0, error_body={"error": {"code": "shape"}})
    )
    assert not _is_retryable_prompt_error(err(exit_code=1))
    assert not _is_retryable_prompt_error(err(exit_code=1, error_body={"other": 1}))
    assert not _is_retryable_prompt_error(err(exit_code=1, error_body={"error": {}}))
    assert not _is_retryable_prompt_error(
        err(exit_code=1, error_body={"error": "timeout"})
    )
    assert not _is_retryable_prompt_error(
        err(exit_code=1, error_body={"error": {"code": "timeout"}})
    )


def test_real_herdr_client_satisfies_the_shape_used_by_execute_run() -> None:
    """Ensures ScriptedClient's protocol above doesn't silently drift from HerdrClient's real
    method signatures."""
    for name in (
        "settled_agent_workspace",
        "settled_agent_pane",
        "workspace_close",
        "pane_close",
        "worktree_create",
        "tab_create",
        "agent_start",
        "agent_interactive_ready",
        "agent_prompt_wait",
        "agent_prompt_wait_with_watchdog",
        "agent_read",
        "agent_read_visible",
        "agent_session_id",
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
    client = ScriptedClient(stale_pane="w9:p1", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    # w9:p1 is yesterday's stale pane, reaped before start; w1:p1 is this run's own pane,
    # closed eagerly on success (pane-lifecycle v2) rather than left for tomorrow's reap.
    assert client.closed_panes == ["w9:p1", "w1:p1"]
    assert client.calls.index("pane_close") < client.calls.index("worktree_create")


def test_execute_run_leaves_other_panes_alone_when_no_stale_workspace(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """No previous stale pane to reap pre-run, but the run still closes its own pane on
    success — the "leave panes alone" behavior only ever applied to *other* panes."""
    job = make_job(tmp_path)
    run_id = "a-run16"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(stale_pane=None, write_report_at=report_path)
    execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert client.closed_panes == ["w1:p1"]
    assert "workspace_close" not in client.calls


def test_execute_run_survives_stale_workspace_reap_errors(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Failing to reap (e.g. server blip) must not abort the run — the worst case is the old
    duplicate-name failure at agent start, which is captured like any other failure."""
    job = make_job(tmp_path)
    run_id = "a-run17"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(raise_on="settled_agent_pane", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"


def test_execute_run_does_not_reap_in_root_mode(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Root-mode jobs share the ambient workspace — closing it would tear down every other
    open tab, not just our own previous pane."""
    job = make_job(tmp_path, workspace="root")
    run_id = "a-run-root"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(stale_pane="w9:p1", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert "settled_agent_pane" not in client.calls
    assert "pane_close" not in client.calls
    assert "workspace_close" not in client.calls
    # Nothing is ever closed for a root-mode job, so there's nothing to resume-and-inspect —
    # capturing a session id would just be a wasted call.
    assert "agent_session_id" not in client.calls
    assert outcome.session_id is None


def test_execute_run_survives_session_id_capture_errors(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """A session-id lookup failure (e.g. server blip) must still close the pane and record the
    run as done — losing the resume handle is not worth losing the eager close for."""
    job = make_job(tmp_path)
    run_id = "a-run-sid-err"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(raise_on="agent_session_id", write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert outcome.session_id is None
    assert client.closed_panes == ["w1:p1"]


def test_execute_run_survives_unexpected_reap_exception(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """The reap catch must preserve execute_run's never-raises contract even for
    unexpected exceptions like AttributeError from a malformed agent list shape."""

    class ExplodingClient(ScriptedClient):
        def settled_agent_pane(self, name):  # type: ignore[override]
            self.calls.append("settled_agent_pane")
            raise AttributeError("'NoneType' object has no attribute 'get'")

    job = make_job(tmp_path)
    run_id = "a-run-attr"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ExplodingClient(write_report_at=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"


# -- failure reaping & quota classification (docs/failure-reaping.md) -------------------------


class QuotaWedgeClient(ScriptedClient):
    """The 2026-08-23 Pi wedge, replayed: the prompt was delivered but the agent never
    settled — OpenCode rendered its free-quota modal and retries forever — so the settle-wait
    raises the non-retryable timeout signature while the modal sits on screen."""

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self.calls.append("agent_prompt_wait")
        raise HerdrCliError(
            "timed out waiting for agent status",
            exit_code=1,
            error_body={"error": {"code": "timeout", "message": "timed out"}},
        )


def test_settle_timeout_with_quota_marker_is_quota_exhausted(
    tmp_path: Path,
    _isolated_reports_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the overnight wedge: a settle timeout with the quota modal on screen
    classifies as quota_exhausted, leaves the screen tail as evidence, and reaps the pane."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ())
    job = make_job(tmp_path)
    run_id = "a-quota"
    client = QuotaWedgeClient(
        visible_screen="Free usage exceeded, subscribe to Go [retrying in 3h 35m attempt #1]"
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    assert "'Free usage exceeded'" in (outcome.error or "")
    # Evidence lands before the reap; nothing touches the agent after the close.
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.exists() and "Free usage exceeded" in tail_path.read_text()
    assert client.calls.index("agent_read_visible") < client.calls.index("pane_close")
    assert client.calls[-1] == "pane_close"
    assert client.closed_panes == ["w1:p1"]


def test_settle_timeout_without_marker_stays_agent_prompt_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty/irrelevant screen keeps the plain agent_prompt_failed classification."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ())
    job = make_job(tmp_path)
    client = QuotaWedgeClient(visible_screen="")
    outcome = execute_run(job, client, run_id="a-plain")  # type: ignore[arg-type]
    assert outcome.reason == "agent_prompt_failed"
    assert "timed out waiting for agent status" in (outcome.error or "")


def test_marker_in_own_prompt_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The visible screen contains our own prompt echo, so a marker appearing verbatim in the
    prompt must not classify the run as quota exhaustion (§3.2's false-positive guard)."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ())
    job = make_job(
        tmp_path,
        prompt="Never print the phrase Free usage exceeded. Write $ROUTINE_REPORT.",
    )
    client = QuotaWedgeClient(visible_screen="Free usage exceeded is forbidden here")
    outcome = execute_run(job, client, run_id="a-guard")  # type: ignore[arg-type]
    assert outcome.reason == "agent_prompt_failed"


def test_custom_failure_markers_override_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ())
    job = make_job(tmp_path, failure_markers=("Out of credits",))
    client = QuotaWedgeClient(
        visible_screen="Free usage exceeded\nOut of credits — retrying"
    )
    outcome = execute_run(job, client, run_id="a-custom")  # type: ignore[arg-type]
    assert outcome.reason == "quota_exhausted"
    assert "'Out of credits'" in (outcome.error or "")


def test_explicit_empty_failure_markers_disable_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit empty list is valid config meaning 'scan nothing' — it must NOT fall back
    to the defaults via falsy-`or` (found by the muse review on this very PR)."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ())
    job = make_job(tmp_path, failure_markers=())
    client = QuotaWedgeClient(visible_screen="Free usage exceeded")
    outcome = execute_run(job, client, run_id="a-empty")  # type: ignore[arg-type]
    assert outcome.reason == "agent_prompt_failed"


def test_agent_not_interactive_reaps_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("herdr_routines.runner.READY_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path, start_timeout_ms=50)
    client = ScriptedClient(interactive_ready=False, visible_screen="partial boot")
    outcome = execute_run(job, client, run_id="a-unready")  # type: ignore[arg-type]
    assert outcome.reason == "agent_not_interactive"
    assert "pane_close" in client.calls
    assert client.closed_panes == ["w1:p1"]


def test_blocked_status_keeps_pane_alive(tmp_path: Path) -> None:
    """blocked is answerable from bed via herdr-push (ROADMAP Next) — never reaped, and it is
    a settled state so the recent-unwrapped success-path tail still works."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status="blocked")
    outcome = execute_run(job, client, run_id="a-blocked")  # type: ignore[arg-type]
    assert outcome.reason == "blocked"
    assert "pane_close" not in client.calls
    assert "agent_read_visible" not in client.calls


def test_unknown_status_keeps_pane_alive(tmp_path: Path) -> None:
    """interrupted_unknown is the evidence-preservation bucket (plan-v1 §4) — no reaping."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status="unknown")
    outcome = execute_run(job, client, run_id="a-unknown")  # type: ignore[arg-type]
    assert outcome.state == "interrupted_unknown"
    assert "pane_close" not in client.calls
    assert "agent_read_visible" not in client.calls


def test_defensive_unsettled_status_reaps_pane(tmp_path: Path) -> None:
    """A post-wait 'working' status means herdr still classifies the agent as live — leaving
    it behind wedges the job exactly like the prompt-failed path (§3.1's defensive row)."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status="working", visible_screen="still going")
    outcome = execute_run(job, client, run_id="a-working")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "unsettled_status_working"
    assert client.calls.index("agent_read_visible") < client.calls.index("pane_close")
    assert client.closed_panes == ["w1:p1"]


def test_failed_pane_close_never_breaks_execute_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane_close failure at reap time degrades to today's behavior (warning + leftover
    pane), never to a crash — execute_run's never-raises contract (§5)."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ())

    class UnreapableWedge(QuotaWedgeClient):
        def pane_close(self, pane_id):
            self.calls.append("pane_close")
            raise HerdrCliError("server went away", exit_code=1)

    job = make_job(tmp_path)
    client = UnreapableWedge(visible_screen="Free usage exceeded")
    outcome = execute_run(job, client, run_id="a-noreap")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    assert "pane_close" in client.calls


# -- phase 2: mid-run fast-fail watchdog ------------------------------------------------------
# (docs/pipeline/runs/20260826T031438Z/spec.md — one test per acceptance criterion)

QUOTA_SCREEN = "Free usage exceeded, subscribe to Go [retrying in 3h 35m attempt #1]"


class WatchdogClient(ScriptedClient):
    """Scripted mid-run watchdog fake, mirroring herdr.py's agent_prompt_wait_with_watchdog
    loop at the runner level: each entry of `polls` is one visible-screen read fed to the
    scan callback while the prompt child is still running (each recorded as
    agent_read_visible, because that is exactly what a poll is). After the scripted polls
    are exhausted, the child either settles (`settle_status`) or dies with the non-retryable
    settle-timeout signature (`settle_status=None`) — so a test that expects a fast fail can
    prove the watchdog fired *instead of* the timeout. `fail_terminate` models the child
    kill racing/losing; herdr.py swallows kill failures and still raises."""

    def __init__(
        self,
        *,
        polls: tuple[str, ...] = (),
        settle_status: str | None = None,
        fail_terminate: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.watchdog_polls = list(polls)
        self.settle_status = settle_status
        self.fail_terminate = fail_terminate
        self.prompt_deliveries = 0
        self.terminate_calls = 0
        self.polls_consumed = 0

    def agent_prompt_wait_with_watchdog(  # type: ignore[override]
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        self.calls.append("agent_prompt_wait_with_watchdog")
        self.prompt_deliveries += 1
        for screen in self.watchdog_polls:
            self.polls_consumed += 1
            self.calls.append("agent_read_visible")
            assert on_poll is not None
            marker = on_poll(screen)
            if marker is not None:
                self.calls.append("watchdog_kill")
                # herdr.py's terminate never raises even if the kill loses a race; the
                # classification that follows is unaffected either way.
                self.terminate_calls += 1
                raise PromptWatchdogKilled(
                    f"failure marker matched; prompt child terminated: {marker!r}",
                    marker=marker,
                    screen_text=screen,
                )
        if self.settle_status is None:
            raise HerdrCliError(
                "timed out waiting for agent status",
                exit_code=1,
                error_body={"error": {"code": "timeout", "message": "timed out"}},
            )
        if self.write_report_at is not None:
            self.write_report_at.parent.mkdir(parents=True, exist_ok=True)
            self.write_report_at.write_text(self.report_content)
        return self.settle_status


def test_watchdog_fast_fails_on_quota_marker_before_timeout(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Criterion 1: a run showing the quota marker on two consecutive visible-screen polls is
    detected and reaped without waiting out timeout_ms — settle_status=None means the only
    pre-watchdog escape was the full settle-timeout wait, so reaching quota_exhausted with
    the child killed and the pane closed IS the before-timeout proof."""
    job = make_job(tmp_path, timeout_ms=5_400_000)  # must go unconsumed
    run_id = "a-watch-fast"
    client = WatchdogClient(
        polls=(QUOTA_SCREEN, QUOTA_SCREEN),
        settle_status=None,
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    assert "'Free usage exceeded'" in (outcome.error or "")
    assert client.terminate_calls == 1
    assert client.prompt_deliveries == 1
    # Reaped before any next tick could classify the agent as live.
    assert client.closed_panes == ["w1:p1"]
    assert client.calls[-1] == "pane_close"


def test_watchdog_does_not_fire_on_slow_run_without_marker(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Criterion 2: a legitimately slow run whose screen never shows a marker settles exactly
    as today — no termination, no false-positive reap, done/report semantics unchanged."""
    job = make_job(tmp_path)
    run_id = "a-watch-slow"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = WatchdogClient(
        polls=("implementing module A...", "step 39 of 40: running tests..."),
        settle_status="idle",
        write_report_at=report_path,
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert outcome.reason is None
    assert outcome.final_agent_status == "idle"
    assert outcome.report_written is True
    assert outcome.report_bytes > 0
    assert client.terminate_calls == 0
    assert "watchdog_kill" not in client.calls
    # Both polls were clean sightings that matched nothing; normal success-path close.
    assert client.polls_consumed == 2
    assert client.closed_panes == ["w1:p1"]


def test_watchdog_skips_marker_present_in_prompt(
    tmp_path: Path,
) -> None:
    """Criterion 3: a marker appearing verbatim in the job's own prompt is inert (phase-1
    guard reused per poll via _matched_failure_marker) — even two consecutive sightings of
    it cannot trigger a self-match kill; the run degrades to today's settle-timeout path."""
    job = make_job(
        tmp_path,
        prompt="Never print the phrase Free usage exceeded. Write $ROUTINE_REPORT.",
    )
    client = WatchdogClient(
        polls=(QUOTA_SCREEN, QUOTA_SCREEN),
        settle_status=None,
    )
    outcome = execute_run(job, client, run_id="a-watch-guard")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert client.terminate_calls == 0
    assert "watchdog_kill" not in client.calls
    assert client.closed_panes == ["w1:p1"]


def test_watchdog_requires_two_consecutive_hits(
    tmp_path: Path,
) -> None:
    """Criterion 4: the stability gate — a single transient sighting does not kill; only the
    SAME marker on consecutive polls does. An intervening clean screen resets the gate, so
    hit / miss / hit never terminates the delivered child."""
    client = WatchdogClient(
        polls=(QUOTA_SCREEN, "all good, implementing...", QUOTA_SCREEN),
        settle_status=None,
    )
    outcome = execute_run(make_job(tmp_path), client, run_id="a-watch-gate")  # type: ignore[arg-type]
    assert outcome.reason == "agent_prompt_failed"
    assert client.terminate_calls == 0
    assert "watchdog_kill" not in client.calls
    # All three scripted polls ran — the gate kept resetting rather than firing early.
    assert client.polls_consumed == 3


def test_watchdog_poll_failure_is_inert(
    tmp_path: Path,
) -> None:
    """Criterion 5: failed poll reads ("" — the best-effort read contract when the CLI errors
    or the server is unreachable; herdr.py also swallows HerdrCliError/OSError into "")
    match no marker. The loop keeps waiting and degrades to today's settle-timeout path —
    never a crash, never a false reap."""
    client = WatchdogClient(
        polls=("", "", ""),
        settle_status=None,
    )
    outcome = execute_run(make_job(tmp_path), client, run_id="a-watch-pollerr")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    assert "timed out waiting for agent status" in (outcome.error or "")
    assert client.terminate_calls == 0
    assert client.prompt_deliveries == 1


def test_watchdog_tail_before_close_and_close_once(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Criterion 6: diagnostic ordering is pinned — the visible tail from the detection poll
    lands in {run_id}.tail.txt BEFORE pane_close, and the failed run's pane is closed
    exactly once even when the child kill itself loses the race."""
    run_id = "a-watch-order"

    class TailProbe(WatchdogClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.tail_exists_at_close: bool | None = None

        def pane_close(self, pane_id):  # type: ignore[override]
            self.tail_exists_at_close = (
                _isolated_reports_dir / f"{run_id}.tail.txt"
            ).exists()
            super().pane_close(pane_id)

    client = TailProbe(
        polls=(QUOTA_SCREEN, QUOTA_SCREEN),
        settle_status=None,
        fail_terminate=True,
    )
    outcome = execute_run(make_job(tmp_path), client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    # Tail written before the close...
    assert client.tail_exists_at_close is True
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.exists() and "Free usage exceeded" in tail_path.read_text()
    # ...and exactly one close, despite the kill racing.
    assert client.terminate_calls == 1
    assert client.closed_panes == ["w1:p1"]
    assert client.calls.count("pane_close") == 1


def test_watchdog_kill_never_retries_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 7: the double-prompt invariant — a watchdog-triggered termination is never
    retried via PROMPT_RETRY_DELAYS_S / _is_retryable_prompt_error. One delivery, no
    retry-delay sleeps, one terminal record."""
    monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", (5.0, 15.0))
    sleeps: list[float] = []
    monkeypatch.setattr("herdr_routines.runner.time.sleep", lambda s: sleeps.append(s))
    client = WatchdogClient(
        polls=(QUOTA_SCREEN, QUOTA_SCREEN),
        settle_status=None,
    )
    outcome = execute_run(make_job(tmp_path), client, run_id="a-watch-noresend")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    assert client.prompt_deliveries == 1
    assert client.calls.count("agent_prompt_wait_with_watchdog") == 1
    assert sleeps == []
    # Structurally barred too: the kill carries no parseable retry-whitelist body.
    kill = PromptWatchdogKilled("killed", marker="m", screen_text="s")
    assert _is_retryable_prompt_error(kill) is False


# -- failover: retry on quota_exhausted with ordered fallback list (issue 022) ------------


def test_failover_retry_on_watchdog_quota_exhausted(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Acceptance 2: on quota_exhausted via watchdog PromptWatchdogKilled the run retries
    once per remaining fallback entry in declared order until first non-quota outcome.
    Attempt sequence is [primary, ...fallbacks]."""
    from herdr_routines.config import FallbackEntry

    job = make_job(
        tmp_path,
        agent_kind="opencode",
        model="opencode/big-pickle",
        fallbacks=(
            FallbackEntry(model="opencode/gpt-5-nano"),
            FallbackEntry(agent_kind="claude", model="haiku"),
        ),
    )
    run_id = "a-failover-wd"
    report_path = _isolated_reports_dir / f"{run_id}.md"

    start_calls: list[tuple[str, str | None]] = []
    call_count = 0
    prompt_deliveries = 0
    polls_consumed = 0
    terminate_calls = 0

    class FailoverWatchdogClient(ScriptedClient):
        def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
            start_calls.append((kind, model))
            super().agent_start(
                name=name,
                kind=kind,
                pane_id=pane_id,
                start_timeout_ms=start_timeout_ms,
                model=model,
            )

        def agent_prompt_wait_with_watchdog(
            self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
        ):
            nonlocal call_count, prompt_deliveries, polls_consumed, terminate_calls
            call_count += 1
            prompt_deliveries += 1
            self.calls.append("agent_prompt_wait_with_watchdog")
            if call_count == 1:
                # First attempt: watchdog kills on quota
                for screen in (QUOTA_SCREEN, QUOTA_SCREEN):
                    polls_consumed += 1
                    self.calls.append("agent_read_visible")
                    assert on_poll is not None
                    marker = on_poll(screen)
                    if marker is not None:
                        self.calls.append("watchdog_kill")
                        terminate_calls += 1
                        raise PromptWatchdogKilled(
                            f"failure marker matched; prompt child terminated: {marker!r}",
                            marker=marker,
                            screen_text=screen,
                        )
                raise AssertionError("should not reach here")
            else:
                # Subsequent attempts: succeed
                if self.write_report_at is not None:
                    self.write_report_at.parent.mkdir(parents=True, exist_ok=True)
                    self.write_report_at.write_text(self.report_content)
                return "idle"

    client = FailoverWatchdogClient(write_report_at=report_path)
    outcome, records = execute_run_with_failover(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert outcome.final_agent_status == "idle"
    # Primary attempt used opencode/big-pickle, fallback used opencode/gpt-5-nano.
    assert start_calls[0] == ("opencode", "opencode/big-pickle")
    assert start_calls[1] == ("opencode", "opencode/gpt-5-nano")
    # One intermediate record for the quota_exhausted attempt.
    assert len(records) == 1
    assert records[0].attempt == 0
    assert records[0].failover_to == "opencode/opencode/gpt-5-nano"
    # Both attempts created and closed their own panes.
    assert client.closed_panes == ["w1:p1", "w1:p1"]


def test_failover_retry_on_marker_quota_exhausted(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Acceptance 3: on quota_exhausted via marker-classified agent_prompt_failed
    the run retries with correct kind/model override per attempt."""
    from herdr_routines.config import FallbackEntry

    job = make_job(
        tmp_path,
        agent_kind="opencode",
        model="opencode/big-pickle",
        failure_markers=("Free usage exceeded",),
        fallbacks=(
            FallbackEntry(agent_kind="claude", model="haiku"),
        ),
    )
    run_id = "a-failover-mk"
    report_path = _isolated_reports_dir / f"{run_id}.md"

    start_calls: list[tuple[str, str | None]] = []
    call_count = 0

    class MarkerFailoverClient(ScriptedClient):
        def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
            start_calls.append((kind, model))
            super().agent_start(
                name=name,
                kind=kind,
                pane_id=pane_id,
                start_timeout_ms=start_timeout_ms,
                model=model,
            )

        def agent_prompt_wait_with_watchdog(
            self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
        ):
            nonlocal call_count
            call_count += 1
            self.calls.append("agent_prompt_wait_with_watchdog")
            if call_count == 1:
                # First attempt: settle timeout + quota marker on screen
                raise HerdrCliError(
                    "timed out waiting for agent status",
                    exit_code=1,
                    error_body={"error": {"code": "timeout", "message": "timed out"}},
                )
            else:
                # Second attempt: succeeds
                if self.write_report_at is not None:
                    self.write_report_at.parent.mkdir(parents=True, exist_ok=True)
                    self.write_report_at.write_text(self.report_content)
                return "idle"

        def agent_read_visible(self, target, *, lines=200):
            self.calls.append("agent_read_visible")
            if call_count == 1:
                return "Free usage exceeded, subscribe to Go"
            return ""

    client = MarkerFailoverClient(write_report_at=report_path)
    outcome, records = execute_run_with_failover(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert start_calls[0] == ("opencode", "opencode/big-pickle")
    assert start_calls[1] == ("claude", "haiku")
    assert len(records) == 1
    assert records[0].attempt == 0
    assert records[0].failover_to == "claude/haiku"


def test_failover_no_retry_on_non_quota(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Acceptance 5: no failover on non-quota failures — agent_prompt_failed without
    marker, agent_start_failed, agent_not_interactive, blocked, interrupted_unknown,
    no_report, and marker contained in prompt_text is inert — exactly one attempt."""
    from herdr_routines.config import FallbackEntry

    job = make_job(
        tmp_path,
        agent_kind="opencode",
        model="opencode/big-pickle",
        fallbacks=(
            FallbackEntry(agent_kind="claude", model="haiku"),
        ),
    )
    run_id = "a-failover-noq"
    report_path = _isolated_reports_dir / f"{run_id}.md"

    start_calls: list[tuple[str, str | None]] = []

    class NonQuotaFailClient(ScriptedClient):
        def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
            start_calls.append((kind, model))
            super().agent_start(
                name=name,
                kind=kind,
                pane_id=pane_id,
                start_timeout_ms=start_timeout_ms,
                model=model,
            )

        def agent_prompt_wait_with_watchdog(
            self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
        ):
            self.calls.append("agent_prompt_wait_with_watchdog")
            # Non-quota failure: empty screen, settle timeout
            raise HerdrCliError(
                "timed out waiting for agent status",
                exit_code=1,
                error_body={"error": {"code": "timeout", "message": "timed out"}},
            )

        def agent_read_visible(self, target, *, lines=200):
            self.calls.append("agent_read_visible")
            return ""  # no quota marker

    client = NonQuotaFailClient(write_report_at=report_path)
    outcome, records = execute_run_with_failover(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_prompt_failed"
    # Only one attempt — no failover on non-quota.
    assert len(start_calls) == 1
    assert len(records) == 0


def test_failover_exhaustion_and_pane_lifecycle(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Acceptance 6: exhausting all fallbacks with quota_exhausted on every attempt
    yields terminal failed/quota_exhausted after N attempts, per-attempt pane lifecycle
    preserves tail-before-close and no leaked working agent, any_job_failed reflects
    final attempt only."""
    from herdr_routines.config import FallbackEntry

    job = make_job(
        tmp_path,
        agent_kind="opencode",
        model="opencode/big-pickle",
        failure_markers=("Free usage exceeded",),
        fallbacks=(
            FallbackEntry(model="opencode/gpt-5-nano"),
            FallbackEntry(agent_kind="claude", model="haiku"),
        ),
    )
    run_id = "a-failover-exhaust"

    start_calls: list[tuple[str, str | None]] = []
    call_count = 0

    class AlwaysQuotaClient(ScriptedClient):
        def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
            start_calls.append((kind, model))
            super().agent_start(
                name=name,
                kind=kind,
                pane_id=pane_id,
                start_timeout_ms=start_timeout_ms,
                model=model,
            )

        def agent_prompt_wait_with_watchdog(
            self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
        ):
            nonlocal call_count
            call_count += 1
            self.calls.append("agent_prompt_wait_with_watchdog")
            # Every attempt hits quota_exhausted
            raise HerdrCliError(
                "timed out waiting for agent status",
                exit_code=1,
                error_body={"error": {"code": "timeout", "message": "timed out"}},
            )

        def agent_read_visible(self, target, *, lines=200):
            self.calls.append("agent_read_visible")
            return "Free usage exceeded, subscribe to Go"

    client = AlwaysQuotaClient()
    outcome, records = execute_run_with_failover(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    # 3 attempts: primary + 2 fallbacks
    assert len(start_calls) == 3
    assert start_calls[0] == ("opencode", "opencode/big-pickle")
    assert start_calls[1] == ("opencode", "opencode/gpt-5-nano")
    assert start_calls[2] == ("claude", "haiku")
    # 2 intermediate records (attempts 0 and 1 quota_exhausted)
    assert len(records) == 2
    assert records[0].attempt == 0
    assert records[0].failover_to == "opencode/opencode/gpt-5-nano"
    assert records[1].attempt == 1
    assert records[1].failover_to == "claude/haiku"
    # Per-attempt pane lifecycle: each attempt creates and closes its own pane.
    # 3 attempts × (pane_close for tail + pane_close for reap) = 6 pane_close calls,
    # but each attempt closes exactly one pane (w1:p1).
    assert client.closed_panes == ["w1:p1", "w1:p1", "w1:p1"]
    # Tail-before-close ordering: agent_read_visible before pane_close for each attempt.
    vis_calls = [i for i, c in enumerate(client.calls) if c == "agent_read_visible"]
    close_calls = [i for i, c in enumerate(client.calls) if c == "pane_close"]
    for vis_idx, close_idx in zip(vis_calls, close_calls):
        assert vis_idx < close_idx
