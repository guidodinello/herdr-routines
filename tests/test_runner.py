"""Tests for runner.py's orchestration, against a fake HerdrClient (tier 2 — no `herdr` binary
involved). See docs/plan-v1.md §7."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.config import Job
from herdr_routines.herdr import (
    HerdrClient,
    HerdrCliError,
    PromptWatchdogKilled,
)
from herdr_routines.runner import (
    DEFAULT_FAILURE_MARKERS,
    _is_retryable_prompt_error,
    _is_settle_timeout,
    build_branch_name,
    build_dry_run_argv,
    execute_run,
    make_run_id,
    substitute_prompt,
)
from herdr_routines.wait_loop import matched_failure_marker


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

    notifications: list[tuple[str, str | None]]

    def notification_show(self, title, *, body=None, sound="none"):
        self.notifications = [*getattr(self, "notifications", []), (title, body)]

    def agent_statuses(self) -> dict[str, str]:
        return {}

    def __init__(
        self,
        *,
        pane_id: str = "w1:p1",
        agent_status: str | None = "idle",
        interactive_ready: bool = True,
        stale_pane: str | None = None,
        write_report_at: Path | None = None,
        report_content: str = "# Report\n\nFindings.\n",
        raise_on: str | None = None,
        visible_screen: str = "",
        session_id: str | None = "ses_fake123",
        sticky_pane: tuple[str, str] | None = None,
        agent_start_collides_once: bool = False,
    ) -> None:
        self.pane_id = pane_id
        self.agent_status = agent_status
        self.interactive_ready = interactive_ready
        self.stale_pane = stale_pane
        self.write_report_at = write_report_at
        self.report_content = report_content
        self.raise_on = raise_on
        self.visible_screen = visible_screen
        self.session_id = session_id
        self.sticky_pane = sticky_pane
        self.agent_start_collides_once = agent_start_collides_once
        self.calls: list[str] = []
        self.closed_panes: list[str] = []
        self.started_with_model: str | None = None

    def worktree_create(self, *, cwd, branch, base, label=None):
        self.calls.append("worktree_create")
        if self.raise_on == "worktree_create":
            raise HerdrCliError("boom", exit_code=1)
        return self.pane_id

    def settled_agent_pane(self, name):
        self.calls.append("settled_agent_pane")
        if self.raise_on == "settled_agent_pane":
            raise HerdrCliError("boom", exit_code=1)
        return self.stale_pane

    def pane_close(self, pane_id):
        self.calls.append("pane_close")
        self.closed_panes.append(pane_id)

    def tab_create(self, *, cwd, label=None):
        self.calls.append("tab_create")
        return self.pane_id

    def agent_start(
        self, *, name, kind, pane_id, start_timeout_ms, model=None, session_id=None
    ):
        self.calls.append("agent_start")
        self.started_with_model = model
        if self.raise_on == "agent_start":
            raise HerdrCliError("boom", exit_code=1)
        if self.agent_start_collides_once and self.calls.count("agent_start") == 1:
            raise HerdrCliError(
                f"herdr server error running agent start {name}: "
                f'{{"error":{{"code":"agent_name_taken","message":"agent name {name} '
                'is already used"}}}',
                exit_code=1,
                error_body={"error": {"code": "agent_name_taken"}},
            )

    def sticky_agent_pane(self, name):
        self.calls.append("sticky_agent_pane")
        if self.raise_on == "sticky_agent_pane":
            raise HerdrCliError("boom", exit_code=1)
        return self.sticky_pane

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


@pytest.fixture(autouse=True)
def _stub_ensure_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_run's orchestration tests exercise agent dispatch, not repo-sync behavior
    (that's test_repos.py's job), and `make_job` points `job.repo` at a bare tmp_path
    that isn't a real git checkout — stub ensure_repo so it's not called for real."""
    monkeypatch.setattr("herdr_routines.runner.ensure_repo", lambda job: job.repo)


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


def test_build_dry_run_argv_creates_opencode_session_on_model(tmp_path: Path) -> None:
    """opencode 2 has no model flag, so the dry run shows the session-create step and an
    `agent start` that opens the session it returns. The request body comes from the same
    builder the real start uses, so `location` is in it."""
    from herdr_routines.herdr import opencode_session_create_args

    job = make_job(tmp_path, agent_kind="opencode", model="opencode/big-pickle")
    commands = build_dry_run_argv(job, run_id="a-1")
    create_argv, agent_start_argv = commands[1], commands[2]
    assert create_argv == [
        "opencode",
        "api",
        *opencode_session_create_args("opencode/big-pickle", "<pane_cwd>"),
    ]
    assert json.loads(create_argv[4]) == {
        "model": {"providerID": "opencode", "id": "big-pickle"},
        "location": {"directory": "<pane_cwd>"},
    }
    assert agent_start_argv[-3:] == ["--", "-s", "<session_id>"]
    assert "-m" not in agent_start_argv


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
        "agent_read_visible",
        "agent_session_id",
        "pane_close",
    ]
    # pane-lifecycle v2: a successful run closes its own pane immediately rather than leaving
    # it for the next run's stale-pane reap.
    assert client.closed_panes == ["w1:p1"]
    # A first-try report means the no_report nudge (issue 032) is never sent: exactly one
    # agent_prompt_wait call above, and outcome.nudged stays False.
    assert client.calls.count("agent_prompt_wait") == 1
    assert outcome.nudged is False


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
    report file must not be recorded as done, even after the issue-032 nudge also produces
    nothing."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status="idle", write_report_at=None)
    outcome = execute_run(job, client, run_id="a-run3")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "no_report"
    # A no-report settle is still an idle/done agent — pane-lifecycle v2 closes it too.
    assert outcome.session_id == "ses_fake123"
    assert client.closed_panes == ["w1:p1"]
    # The nudge was attempted (one extra agent_prompt_wait beyond the original prompt) but
    # produced no report, so the run still fails exactly as before this nudge existed.
    assert outcome.nudged is True
    assert client.calls.count("agent_prompt_wait") == 2


class NudgeWritesReportClient(ScriptedClient):
    """Settles idle/done without writing a report on the job's own prompt (the first
    agent_prompt_wait call); the issue-032 no_report nudge (the second call) is what actually
    writes it — modeling the free-tier model that did the real work but dropped the final
    "write the summary" step until asked again."""

    def __init__(self, *, report_path: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._report_path = report_path
        self._prompt_calls = 0

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self.calls.append("agent_prompt_wait")
        self._prompt_calls += 1
        if self._prompt_calls >= 2:
            self._report_path.parent.mkdir(parents=True, exist_ok=True)
            self._report_path.write_text("# Nudged report\n\nDid the thing.\n")
        return self.agent_status


def test_execute_run_nudge_success_ends_done_and_records_nudged(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Acceptance: if the one-shot nudge produces a non-empty report, the run ends `done` (not
    `no_report`), and the outcome records that a nudge was needed."""
    job = make_job(tmp_path)
    run_id = "a-run-nudge-ok"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = NudgeWritesReportClient(agent_status="idle", report_path=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert outcome.reason is None
    assert outcome.report_written is True
    assert outcome.report_bytes > 0
    assert outcome.nudged is True
    # Pins the nudge strictly before the visible-tail capture, which is itself before
    # pane_close (issue 011 spec v2 L33: nudge stays before capture so the tail
    # reflects the nudge's final screen).
    assert client.calls == [
        "settled_agent_pane",
        "worktree_create",
        "agent_start",
        "agent_interactive_ready",
        "agent_prompt_wait",
        "agent_prompt_wait",
        "agent_read_visible",
        "agent_session_id",
        "pane_close",
    ]


class NudgeErrorsClient(ScriptedClient):
    """The job's own prompt settles idle/done with no report; the issue-032 nudge (the second
    agent_prompt_wait call) itself errors — the nudge must never be retried, and the run must
    fall through to today's exact no_report failure."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._prompt_calls = 0

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self.calls.append("agent_prompt_wait")
        self._prompt_calls += 1
        if self._prompt_calls >= 2:
            raise HerdrCliError("nudge boom", exit_code=1)
        return self.agent_status


def test_execute_run_nudge_error_falls_through_to_no_report(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    client = NudgeErrorsClient(agent_status="idle", write_report_at=None)
    outcome = execute_run(job, client, run_id="a-run-nudge-err")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "no_report"
    assert outcome.nudged is True
    assert client.calls.count("agent_prompt_wait") == 2
    # Never loops: exactly one nudge attempt even though it errored.
    assert client.closed_panes == ["w1:p1"]


def test_execute_run_nudge_applies_in_root_mode(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Root-mode jobs never close a pane, but the nudge must still fire and still be able to
    turn a no_report settle into `done`."""
    job = make_job(tmp_path, workspace="root")
    run_id = "a-run-nudge-root"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = NudgeWritesReportClient(agent_status="idle", report_path=report_path)
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert outcome.nudged is True
    assert client.calls.count("agent_prompt_wait") == 2
    assert "pane_close" not in client.calls
    assert outcome.session_id is None


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


def test_execute_run_reaps_blocked_agent_on_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue 051: a prior run that ended `blocked` leaves its agent registered under the
    recurring name. `agent start` then fails `agent_name_taken` every run until a human
    intervenes. The runner now force-closes that parked pane and retries the start once."""
    monkeypatch.setattr("herdr_routines.runner.READY_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path)
    report_path = tmp_path / "state" / "reports" / "a-run-c1.md"
    client = ScriptedClient(
        agent_start_collides_once=True,
        sticky_pane=("w9:p1", "blocked"),
        write_report_at=report_path,
    )
    outcome = execute_run(job, client, run_id="a-run-c1")  # type: ignore[arg-type]

    assert outcome.state == "done"
    assert outcome.reaped_stale_agent is True
    assert client.closed_panes.count("w9:p1") == 1
    # start attempted, failed, sticky pane looked up + closed, start retried
    assert client.calls.count("agent_start") == 2
    assert client.calls.index("sticky_agent_pane") < client.calls.index("pane_close")


def test_execute_run_does_not_reap_a_working_agent_on_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name collision with a genuinely `working` agent (sticky_agent_pane returns None)
    must NOT be force-closed — fall straight through to agent_start_failed."""
    monkeypatch.setattr("herdr_routines.runner.READY_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path)
    client = ScriptedClient(agent_start_collides_once=True, sticky_pane=None)
    outcome = execute_run(job, client, run_id="a-run-c2")  # type: ignore[arg-type]

    assert outcome.state == "failed"
    assert outcome.reason == "agent_start_failed"
    assert outcome.reaped_stale_agent is False
    assert client.calls.count("agent_start") == 1  # no retry
    # only the run's own dead pane is cleaned up — nothing was force-reaped
    assert client.closed_panes == ["w1:p1"]


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
        "settled_agent_pane",
        "pane_close",
        "worktree_create",
        "tab_create",
        "agent_start",
        "agent_interactive_ready",
        "agent_prompt_wait",
        "agent_prompt_wait_with_watchdog",
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


def test_execute_run_leaves_other_panes_alone_when_no_stale_pane(
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


def test_execute_run_survives_stale_pane_reap_errors(
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
        def settled_agent_pane(self, name):
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
    """blocked is answerable from bed via herdr-push (ROADMAP Next) — never reaped. It still
    captures a diagnostic tail (issue 033) via agent_read_visible, since the pane may not
    survive until a human resumes it (manual close, host reboot)."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status="blocked")
    outcome = execute_run(job, client, run_id="a-blocked")  # type: ignore[arg-type]
    assert outcome.reason == "blocked"
    assert "pane_close" not in client.calls
    assert "agent_read_visible" in client.calls


def test_blocked_status_writes_diagnostic_tail(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """Issue 033: a blocked settle is the failure mode most likely to need a human to see what
    it was blocked on, so it must leave a {run_id}.tail.txt behind exactly like every other
    failure path, even though the pane itself stays open."""
    job = make_job(tmp_path)
    run_id = "a-blocked-tail"
    client = ScriptedClient(
        agent_status="blocked", visible_screen="stuck on a y/n prompt"
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.reason == "blocked"
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.read_text() == "stuck on a y/n prompt"
    assert "pane_close" not in client.calls


def test_blocked_status_survives_tail_read_failure(tmp_path: Path) -> None:
    """_capture_visible_tail already swallows OSError from agent_read_visible internally — a
    blocked settle whose visible-tail read fails must still terminate cleanly as
    state="failed", reason="blocked" with no new exception escaping."""

    class UnreadableBlockedClient(ScriptedClient):
        def agent_read_visible(self, target, *, lines=200):
            self.calls.append("agent_read_visible")
            raise OSError("pane gone")

    job = make_job(tmp_path)
    client = UnreadableBlockedClient(agent_status="blocked")
    outcome = execute_run(job, client, run_id="a-blocked-unreadable")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "blocked"
    assert "pane_close" not in client.calls


def test_unknown_status_keeps_pane_alive(tmp_path: Path) -> None:
    """interrupted_unknown is the evidence-preservation bucket (plan-v1 §4) — no reaping."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status="unknown")
    outcome = execute_run(job, client, run_id="a-unknown")  # type: ignore[arg-type]
    assert outcome.state == "interrupted_unknown"
    assert "pane_close" not in client.calls


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


def test_unrecognized_status_reaps_pane_with_distinct_reason(tmp_path: Path) -> None:
    """None is a status outside herdr's schema: same reap path as a defensive 'working',
    but the reason must not read 'unsettled_status_None'."""
    job = make_job(tmp_path)
    client = ScriptedClient(agent_status=None, visible_screen="who knows")
    outcome = execute_run(job, client, run_id="a-unrecognized")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "unsettled_status_unrecognized"
    assert outcome.final_agent_status is None
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

    def agent_prompt_wait_with_watchdog(
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


# Real opencode 2.1.4 screens, captured 2026-11-24 through herdr 0.9.5 (`herdr agent read
# --source visible`) in an isolated XDG dir (issue 091). All were produced by the same prompt
# ("say hi in one word"); only the footer's scratch path was redacted. How each was made:
#   quota_shaped_*         a local stub upstream answered HTTP 429 with the body message
#                          "Free usage exceeded, subscribe to Go". SYNTHETIC upstream text: no
#                          real opencode Zen quota screen could be provoked on the laptop, so
#                          these prove how opencode 2 *renders* a provider message, not what
#                          Zen's own quota message says today.
#   http_401_settled_error stub upstream HTTP 401 ("Invalid API key").
#   http_500_retry         stub upstream HTTP 500 ("upstream exploded"), mid-retry.
#   model_unavailable      session created on `opencode/does-not-exist-9x`, a model missing from
#                          the catalog.
#   normal_reply           `opencode/big-pickle` answering "Hi".
#   foreign_session_failed_toast  the pane's own session is retrying a 500; the "Session failed"
#                          toast on top belongs to ANOTHER session on the shared service.
OPENCODE2_SCREENS = Path(__file__).parent / "fixtures" / "opencode2_screens"


def opencode2_screen(name: str) -> str:
    return (OPENCODE2_SCREENS / f"{name}.txt").read_text()


@pytest.mark.parametrize(
    "fixture", ["quota_shaped_retry", "quota_shaped_settled_error"]
)
def test_watchdog_fast_fails_on_opencode2_quota_screens(
    tmp_path: Path, _isolated_reports_dir: Path, fixture: str
) -> None:
    """opencode 2 prints the provider's message verbatim, both in the `Retrying in ... ·
    attempt N · <message>` line while it retries and in `Error: <message>` once it gives
    up, so the default marker still matches (issue 091)."""
    screen = opencode2_screen(fixture)
    client = WatchdogClient(polls=(screen, screen), settle_status=None)
    job = make_job(tmp_path, timeout_ms=5_400_000)
    outcome = execute_run(job, client, run_id=f"a-oc2-{fixture}")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    assert "'Free usage exceeded'" in (outcome.error or "")
    assert client.terminate_calls == 1


@pytest.mark.parametrize(
    "fixture",
    ["normal_reply", "model_unavailable", "http_401_settled_error", "http_500_retry"],
)
def test_watchdog_does_not_call_other_opencode2_screens_quota(
    tmp_path: Path, _isolated_reports_dir: Path, fixture: str
) -> None:
    """A bad key, a missing model or a 5xx is not an exhausted quota: reaping one as
    `quota_exhausted` would send the job to its fallback model for the wrong reason.
    (A 5xx retry loop and an unavailable model therefore still wait out the timeout; the
    unavailable model is caught earlier, at start, by issue 093.)"""
    screen = opencode2_screen(fixture)
    run_id = f"a-oc2-{fixture}"
    client = WatchdogClient(
        polls=(screen, screen),
        settle_status="idle",
        write_report_at=_isolated_reports_dir / f"{run_id}.md",
    )
    outcome = execute_run(make_job(tmp_path), client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert client.terminate_calls == 0


def test_watchdog_ignores_a_single_sighting_from_another_sessions_toast(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """opencode's background service is shared: a "Session failed" toast for a quota-failed
    session of another job rendered over this pane (observed live). The marker is on screen,
    but only for as long as the toast lives, so the two-consecutive-polls gate must not reap
    this healthy run."""
    toast = opencode2_screen("foreign_session_failed_toast")
    assert "Free usage exceeded" in toast
    run_id = "a-oc2-toast"
    client = WatchdogClient(
        polls=(toast, opencode2_screen("http_500_retry")),
        settle_status="idle",
        write_report_at=_isolated_reports_dir / f"{run_id}.md",
    )
    outcome = execute_run(make_job(tmp_path), client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert client.terminate_calls == 0


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

        def pane_close(self, pane_id):
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


# ---------------------------------------------------------------------------
# Issue 038: execute_run's failure reason must come from the injected diagnosis,
# never from the disk of whatever machine happens to be running pytest.
# ---------------------------------------------------------------------------


def test_execute_run_failure_reason_is_not_host_disk_dependent(tmp_path: Path) -> None:
    """The autouse conftest stub — not the host — supplies the /tmp diagnosis.

    Six tests used to pass on CI (runners have free disk) and fail on a laptop at
    >=95%, because `execute_run` consults a real `df`. Asserting the diagnosis came
    from the stub is what makes that regression visible instead of environmental.
    """
    from tests.conftest import NOT_FULL_DIAGNOSIS

    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_start")
    outcome = execute_run(job, client, run_id="a-run-038a")  # type: ignore[arg-type]
    assert outcome.reason == "agent_start_failed"
    assert outcome.diagnosis == NOT_FULL_DIAGNOSIS


def test_execute_run_maps_full_disk_to_tmp_full_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk overrides the underlying reason (issue 027's actual contract).

    Only `diagnose_tmp()` in isolation was covered before; this asserts the mapping
    that ships — and, by injecting the diagnosis, does so without touching the host.
    """
    monkeypatch.setattr(
        "herdr_routines.runner._best_effort_tmp_diagnosis",
        lambda *_a, **_k: {"tmp_full": True, "df_tmp": "98%", "du_tmp": ""},
    )
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_start")
    outcome = execute_run(job, client, run_id="a-run-038b")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "tmp_full"


def test_execute_run_keeps_underlying_reason_when_disk_not_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy disk must not mask the real failure — the other half of the mapping."""
    monkeypatch.setattr(
        "herdr_routines.runner._best_effort_tmp_diagnosis",
        lambda *_a, **_k: {"tmp_full": False, "df_tmp": "10%", "du_tmp": ""},
    )
    job = make_job(tmp_path)
    client = ScriptedClient(raise_on="agent_interactive_ready")
    outcome = execute_run(job, client, run_id="a-run-038c")  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "agent_not_interactive"


# ---------------------------------------------------------------------------
# Issue 007: Approval path for blocked runs
# ---------------------------------------------------------------------------


def test_blocked_leaves_pane_open_and_writes_tail(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """AC 4: A blocked settle leaves the pane open (no pane_close) and writes the
    visible tail to reports/<run_id>.tail.txt via agent_read_visible, preserving the
    steer target for phone approval."""
    job = make_job(tmp_path)
    run_id = "a-blocked-pane"
    client = ScriptedClient(
        agent_status="blocked", visible_screen="Do you want to allow? [y/n]"
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "blocked"
    assert outcome.pane_id == "w1:p1"
    assert "pane_close" not in client.calls
    assert "agent_read_visible" in client.calls
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.exists()
    assert tail_path.read_text() == "Do you want to allow? [y/n]"


def test_blocked_prompt_excerpt_is_truncated_and_best_effort() -> None:
    """AC 5: Prompt excerpt extraction is truncated (<=300 chars), agent-agnostic,
    and never fails the run — an empty or unparseable tail still produces a valid
    excerpt."""
    from herdr_routines.runner import extract_prompt_excerpt

    # Empty tail
    assert extract_prompt_excerpt("") == ""

    # Keyword match — permission prompt
    tail = "some noise\nDo you want to approve? [y/n]\nmore noise"
    excerpt = extract_prompt_excerpt(tail)
    assert "approve" in excerpt.lower()
    assert excerpt == "Do you want to approve? [y/n]"

    # Oversized line gets truncated to 300 chars
    long_line = "approve " * 50  # 400 chars
    excerpt = extract_prompt_excerpt(long_line)
    assert len(excerpt) <= 300
    assert excerpt.endswith("...")

    # No keyword: returns last non-empty line
    tail = "no keywords here\nlast line"
    assert extract_prompt_excerpt(tail) == "last line"

    # Pure whitespace — no keyword match, no non-empty line
    assert extract_prompt_excerpt("   \n  \n  ") == ""


# ---------------------------------------------------------------------------
# Issue 011: pane/session retention — success-path visible tail capture
# ---------------------------------------------------------------------------


def test_success_captures_bounded_visible_tail_before_close(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """AC 1: Routine success (idle with non-empty report) persists a bounded visible tail
    to reports/{run_id}.tail.txt before pane close."""
    job = make_job(tmp_path)
    run_id = "a-success-tail"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(
        agent_status="idle",
        write_report_at=report_path,
        visible_screen="last screenful of agent output\nline 2\nline 3",
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.exists()
    assert "last screenful" in tail_path.read_text()
    # Pane close must come after the tail is on disk.
    assert client.calls.index("agent_read_visible") < client.calls.index("pane_close")


def test_success_tail_uses_visible_source_and_single_write(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """AC 2: Success tail is a single write via agent_read_visible --lines 200 (visible
    source, 200-line bound); no leftover recent-unwrapped write and no dual-write."""
    job = make_job(tmp_path)
    run_id = "a-visibility"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(
        agent_status="idle",
        write_report_at=report_path,
        visible_screen="visible output only",
    )
    execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    # agent_read_visible must have been called (visible source).
    assert "agent_read_visible" in client.calls
    # Exactly one tail file produced.
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.exists()
    assert tail_path.read_text() == "visible output only"


def test_capture_before_session_id_before_close_invariant(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """AC 3: Ordering invariant on success: agent_read_visible (tail) → agent_session_id →
    pane_close; pane is never closed before tail is on disk."""
    job = make_job(tmp_path)
    run_id = "a-order"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(
        agent_status="idle",
        write_report_at=report_path,
        visible_screen="diagnostic tail",
    )
    execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    idx_tail = client.calls.index("agent_read_visible")
    idx_sid = client.calls.index("agent_session_id")
    idx_close = client.calls.index("pane_close")
    assert idx_tail < idx_sid < idx_close


def test_blocked_captures_tail_but_leaves_pane_open(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """AC 4: blocked settle captures visible tail via agent_read_visible but skips
    pane_close and session_id (pane stays open for human)."""
    job = make_job(tmp_path)
    run_id = "a-blocked-retention"
    client = ScriptedClient(
        agent_status="blocked",
        visible_screen="waiting on approval [y/n]",
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "failed"
    assert outcome.reason == "blocked"
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.exists()
    assert "waiting on approval" in tail_path.read_text()
    assert "pane_close" not in client.calls
    assert "agent_session_id" not in client.calls


def test_root_mode_never_closes_pane(
    tmp_path: Path, _isolated_reports_dir: Path
) -> None:
    """AC 5: root workspace jobs never call pane_close and never capture session_id
    on success; tail capture remains best-effort."""
    job = make_job(tmp_path, workspace="root")
    run_id = "a-root-retention"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    client = ScriptedClient(
        agent_status="idle",
        write_report_at=report_path,
        visible_screen="root tail",
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert outcome.state == "done"
    assert "pane_close" not in client.calls
    assert "agent_session_id" not in client.calls
    # Tail capture is best-effort — it still fires in root mode.
    assert "agent_read_visible" in client.calls


# ---------------------------------------------------------------------------
# Issue 091 (spec 20261126T050000Z): opencode 2's terminal quota screen exists
# only AFTER the prompt wait returns, so it is classified by a settle-time scan
# on the already-failing (settled success + missing/empty report) path.
# ---------------------------------------------------------------------------


QUOTA_MARKER = "Free usage exceeded"


def _opencode2_screen(name: str) -> str:
    return (OPENCODE2_SCREENS / f"{name}.txt").read_text()


class SettledScreenClient(ScriptedClient):
    """Settles idle with no watchdog polls and scripts the post-settle visible reads.

    `screens` is consumed one entry per `agent_read_visible`, the last entry repeating once
    exhausted (a healthy success path still performs its own tail capture). `on_read(n)`
    fires after read n, so a test can make a report appear *during* the settle scan.
    """

    def __init__(
        self,
        *,
        screens: Sequence[str] = (),
        on_read: Callable[[int], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.screens = list(screens)
        self.on_read = on_read
        self.read_count = 0

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        # No in-flight polls: the terminal quota shape is only on screen after the settle.
        # Deliberately does NOT delegate to agent_prompt_wait, so a nudge is the only call
        # that records "agent_prompt_wait" (see the no-nudge assertions below).
        self.calls.append("agent_prompt_wait_with_watchdog")
        if self.write_report_at is not None:
            self.write_report_at.parent.mkdir(parents=True, exist_ok=True)
            self.write_report_at.write_text(self.report_content)
        return self.agent_status

    def agent_read_visible(self, target, *, lines=200):
        self.calls.append("agent_read_visible")
        # Tail-equivalent read: the same shape _capture_visible_tail uses.
        assert lines == 200
        index = min(self.read_count, len(self.screens) - 1) if self.screens else -1
        screen = self.screens[index] if index >= 0 else ""
        self.read_count += 1
        if self.on_read is not None:
            self.on_read(self.read_count)
        return screen


class ReadsClient:
    """A wait_loop-shaped client whose `agent_read_visible` replays scripted values.

    Entries may be strings or exception instances (raised on that read), which is how the
    helper's inert-read-error contract is exercised without a real herdr binary.
    """

    def __init__(self, screens: Sequence[str | Exception]) -> None:
        self._screens = list(screens)
        self.calls: list[tuple[str, int]] = []

    def agent_read_visible(self, target, *, lines=200):
        self.calls.append((target, lines))
        item = self._screens.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.parametrize(
    ("fixture", "matches"),
    [
        ("quota_shaped_retry", True),
        ("quota_shaped_settled_error", True),
        ("foreign_session_failed_toast", True),
        ("normal_reply", False),
        ("model_unavailable", False),
        ("http_401_settled_error", False),
        ("http_500_retry", False),
    ],
)
def test_opencode2_screen_corpus_classification(fixture: str, matches: bool) -> None:
    """The captured corpus is a contract: opencode 2 passes the provider message through
    verbatim, in the retrying line and in the settled `Error:` line, while a normal reply, a
    missing model, a 401 and a 5xx are not quota (issue 091)."""
    screen = _opencode2_screen(fixture)
    marker = matched_failure_marker(screen, DEFAULT_FAILURE_MARKERS, "")
    assert (marker == QUOTA_MARKER) is matches


def test_settled_quota_screen_is_quota_exhausted_without_the_nudge(
    tmp_path: Path, _isolated_reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that settles idle with the terminal quota screen and no report is
    quota_exhausted, not no_report: an exhausted model cannot answer the report nudge, so
    the scan replaces it, persists the matched screen as the tail, and the run still closes
    its pane."""
    monkeypatch.setattr("herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path)
    run_id = "a-settled-quota"
    screen = _opencode2_screen("quota_shaped_settled_error")
    client = SettledScreenClient(agent_status="idle", screens=(screen, screen))

    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]

    assert outcome.state == "failed"
    assert outcome.reason == "quota_exhausted"
    assert "settled screen" in (outcome.error or "")
    assert outcome.final_agent_status == "idle"
    # The nudge is the only path that records agent_prompt_wait on this fake; it never ran.
    assert "agent_prompt_wait" not in client.calls
    # Exactly the scan's two reads: the tail comes from the matched screen, no third read.
    assert client.read_count == 2
    tail_path = _isolated_reports_dir / f"{run_id}.tail.txt"
    assert tail_path.exists()
    assert tail_path.read_text() == screen
    # The run still goes through the normal post-settle machinery and closes its pane.
    assert client.closed_panes == ["w1:p1"]


def test_settled_quota_screen_never_downgrades_a_written_report(
    tmp_path: Path, _isolated_reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A written, non-empty report wins over the same quota screen: the run stays done and
    the settle scan never reads the screen (zero extra reads beyond the success tail)."""
    monkeypatch.setattr("herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path)
    run_id = "a-settled-quota-done"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    screen = _opencode2_screen("quota_shaped_settled_error")
    client = SettledScreenClient(
        agent_status="idle",
        screens=(screen,),
        write_report_at=report_path,
    )

    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]

    assert outcome.state == "done"
    assert outcome.reason is None
    assert outcome.report_written is True
    # Only the existing success-path tail capture; the settle scan is skipped entirely.
    assert client.read_count == 1


def test_settled_quota_screen_does_not_downgrade_a_report_written_during_the_scan(
    tmp_path: Path, _isolated_reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-downgrade invariant spans the scan window: a report that lands between the two
    reads wins over the marker seen in that same window, so the run ends done. Without the
    post-scan re-stat a foreign toast could turn a just-completed run into quota_exhausted."""
    monkeypatch.setattr("herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path)
    run_id = "a-settled-quota-race"
    report_path = _isolated_reports_dir / f"{run_id}.md"
    screen = _opencode2_screen("quota_shaped_settled_error")

    def write_report_during_scan(read_number: int) -> None:
        if read_number == 2:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("# Report\n\nWritten during the settle scan.\n")

    client = SettledScreenClient(
        agent_status="idle",
        screens=(screen, screen, screen),
        on_read=write_report_during_scan,
    )

    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]

    assert outcome.state == "done"
    assert outcome.reason is None
    assert outcome.report_written is True
    assert outcome.report_bytes > 0
    # The report exists after the scan, so the nudge is skipped as well.
    assert "agent_prompt_wait" not in client.calls


@pytest.mark.parametrize(
    "fixture",
    ["http_401_settled_error", "http_500_retry", "model_unavailable", "normal_reply"],
)
def test_settled_non_quota_screens_stay_no_report(
    tmp_path: Path,
    _isolated_reports_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
) -> None:
    """A bad key, a 5xx, a missing model or a normal reply must not be reaped as an
    exhausted quota: no settle marker matches, so today's nudge-then-no_report path is
    unchanged."""
    monkeypatch.setattr("herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S", 0.0)
    job = make_job(tmp_path)
    run_id = f"a-settled-{fixture}"
    screen = _opencode2_screen(fixture)
    client = SettledScreenClient(agent_status="idle", screens=(screen, screen, screen))

    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]

    assert outcome.state == "failed"
    assert outcome.reason == "no_report"
    assert outcome.nudged is True
    # Exactly one nudge (the initial prompt uses the watchdog entry point on this fake).
    assert client.calls.count("agent_prompt_wait") == 1
    assert client.closed_panes == ["w1:p1"]


def test_settled_marker_needs_two_consecutive_reads(
    tmp_path: Path, _isolated_reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The foreign-session toast can put the marker over this pane for one read. The
    stability gate keeps a single sighting from classifying the run as quota."""
    monkeypatch.setattr("herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S", 0.0)
    toast = _opencode2_screen("foreign_session_failed_toast")
    clean = _opencode2_screen("http_500_retry")
    assert QUOTA_MARKER in toast
    assert QUOTA_MARKER not in clean
    job = make_job(tmp_path)
    client = SettledScreenClient(agent_status="idle", screens=(toast, clean, clean))

    outcome = execute_run(job, client, run_id="a-settled-toast")  # type: ignore[arg-type]

    assert outcome.state == "failed"
    assert outcome.reason == "no_report"
    assert outcome.nudged is True


def test_settled_marker_in_prompt_is_inert(
    tmp_path: Path, _isolated_reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker-in-prompt guard applies on the settle path too: a job whose own prompt
    contains the phrase cannot self-match, even on two consecutive reads."""
    monkeypatch.setattr("herdr_routines.wait_loop.WATCHDOG_POLL_INTERVAL_S", 0.0)
    job = make_job(
        tmp_path,
        prompt=(
            "Never print the phrase Free usage exceeded. "
            "Write the report to $ROUTINE_REPORT."
        ),
    )
    screen = _opencode2_screen("quota_shaped_settled_error")
    client = SettledScreenClient(agent_status="idle", screens=(screen, screen, screen))

    outcome = execute_run(job, client, run_id="a-settled-guard")  # type: ignore[arg-type]

    assert outcome.state == "failed"
    assert outcome.reason == "no_report"
    assert outcome.nudged is True


def test_settled_screen_marker_contract() -> None:
    """settled_screen_marker's own contract: exactly `reads` reads at
    agent_read_visible(lines=200), the same marker on two consecutive reads, the
    marker-in-prompt guard, the last screen text returned, and read errors inert."""
    from herdr_routines.wait_loop import settled_screen_marker

    quota = _opencode2_screen("quota_shaped_settled_error")

    client = ReadsClient(["nothing here", quota, quota])
    marker, last = settled_screen_marker(
        client,  # type: ignore[arg-type]
        target="agent",
        markers=DEFAULT_FAILURE_MARKERS,
        prompt_text="",
        reads=3,
        interval_s=0,
    )
    assert (marker, last) == (QUOTA_MARKER, quota)
    assert len(client.calls) == 3
    assert client.calls == [("agent", 200)] * 3

    # A single sighting followed by a clean read is not stable.
    single = ReadsClient([quota, "all clear"])
    assert settled_screen_marker(
        single,  # type: ignore[arg-type]
        target="agent",
        markers=DEFAULT_FAILURE_MARKERS,
        prompt_text="",
        reads=2,
        interval_s=0,
    ) == (None, "all clear")

    # Marker-in-prompt guard: unchanged from matched_failure_marker.
    in_prompt = ReadsClient([quota, quota])
    assert settled_screen_marker(
        in_prompt,  # type: ignore[arg-type]
        target="agent",
        markers=DEFAULT_FAILURE_MARKERS,
        prompt_text=f"never say {QUOTA_MARKER}",
        reads=2,
        interval_s=0,
    ) == (None, quota)

    # Read errors are inert and never raised; the last screen text is still returned.
    flaky = ReadsClient([OSError("boom"), quota, quota])
    assert settled_screen_marker(
        flaky,  # type: ignore[arg-type]
        target="agent",
        markers=DEFAULT_FAILURE_MARKERS,
        prompt_text="",
        reads=3,
        interval_s=0,
    ) == (QUOTA_MARKER, quota)

    dead = ReadsClient([OSError("boom"), HerdrCliError("gone", exit_code=1)])
    assert settled_screen_marker(
        dead,  # type: ignore[arg-type]
        target="agent",
        markers=DEFAULT_FAILURE_MARKERS,
        prompt_text="",
        reads=2,
        interval_s=0,
    ) == (None, "")


def test_settled_screen_marker_reads_interval_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interval_s=None must resolve wait_loop.WATCHDOG_POLL_INTERVAL_S at call time. A
    default parameter would bind 30.0 at import and silently add a real 30 s to every test
    that forgets interval_s=0; patching the module global to 0 must collapse the wait."""
    from herdr_routines import wait_loop
    from herdr_routines.wait_loop import settled_screen_marker

    monkeypatch.setattr(wait_loop, "WATCHDOG_POLL_INTERVAL_S", 0.0)
    sleeps: list[float] = []
    monkeypatch.setattr(wait_loop.time, "sleep", lambda s: sleeps.append(s))

    quota = _opencode2_screen("quota_shaped_settled_error")
    client = ReadsClient([quota, quota, quota])
    marker, _ = settled_screen_marker(
        client,  # type: ignore[arg-type]
        target="agent",
        markers=DEFAULT_FAILURE_MARKERS,
        prompt_text="",
        reads=3,
    )

    assert marker == QUOTA_MARKER
    # Every interval actually slept was zero: a default parameter bound at import would
    # have recorded a real 30.0 here instead.
    assert all(s == 0.0 for s in sleeps), sleeps


def test_failure_reaping_and_issue_091_document_both_opencode2_screen_shapes() -> None:
    """The docs record what was learned: §3.2 names both opencode 2 shapes and their
    detection paths and limits the settle scan to the already-failing no-report path, the
    091 log moves the real-Zen comparison to 095, and 095 records that residual."""
    root = Path(__file__).resolve().parent.parent
    failure_reaping = (root / "docs" / "failure-reaping.md").read_text()
    section = failure_reaping.split("### 3.2", 1)[1].split("### 3.3", 1)[0]
    lower = section.lower()

    assert "quota_shaped_retry" in section
    assert "quota_shaped_settled_error" in section
    assert "watchdog" in lower  # the retry shape is caught during the wait
    assert "settle" in lower  # the terminal shape is caught after the settle
    assert any(
        term in lower
        for term in ("no_report", "no-report", "no report", "missing/empty report")
    )
    assert "healthy" in lower

    issue_091 = (
        root / "docs" / "process" / "issues" / "091-opencode2-failure-markers.md"
    ).read_text()
    assert "095" in issue_091
    assert "zen" in issue_091.lower()
    assert "settle" in issue_091.lower()

    issue_095 = (
        root
        / "docs"
        / "process"
        / "issues"
        / "095-zen-quota-wording-and-export-api-error.md"
    )
    assert issue_095.exists()
    text_095 = issue_095.read_text()
    assert "zen" in text_095.lower()
    assert "APIError" in text_095
    assert "export" in text_095
    assert "tail.txt" in text_095


def test_default_failure_markers_unchanged() -> None:
    """The corpus shows verbatim passthrough, so no marker strings were added or changed:
    guessing new phrasings would reap 401/5xx/unavailable screens as quota."""
    assert DEFAULT_FAILURE_MARKERS == (QUOTA_MARKER,)
