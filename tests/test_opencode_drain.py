"""Issue 094: an opencode run is finished only when herdr says settled AND the session has
no background subagent left. Real response shapes below were recorded on the Pi
(opencode 2.4.1) on 2026-11-25; see docs/process/issues/094-*.md."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import runner, wait_loop
from herdr_routines.herdr import (
    HerdrClient,
    HerdrCliError,
    OpencodeJobsUnsupported,
    PromptWatchdogKilled,
)
from herdr_routines.runner import execute_run
from herdr_routines.wait_loop import drain_opencode_background, prompt_with_watchdog
from tests.test_runner import ScriptedClient, make_job

pytestmark = pytest.mark.real_opencode_drain

SESSION = "ses_deafc7e63ffeIY5AYTgHg32VDz"
CHILD = "ses_deafc64d9ffeKl04zAqFAD0N0F"
SERVICE_URL = "http://127.0.0.1:4096"

# Recorded: job.list while the child ran, and after it ended.
JOBS_RUNNING = {
    "data": [
        {
            "kind": "subagent",
            "id": "job_1",
            "sessionID": SESSION,
            "status": "running",
            "childSessionID": CHILD,
            "agent": "general",
            "description": "essay",
            "time": {"started": 1795648887603},
        }
    ]
}
JOBS_NONE: dict[str, Any] = {"data": []}
ACTIVE_CHILD_ONLY = {"data": {CHILD: {"type": "running"}}}
ACTIVE_PARENT = {"data": {SESSION: {"type": "running"}}}
ACTIVE_NONE: dict[str, Any] = {"data": {}}
NOT_FOUND = (
    1,
    "",
    "UnknownError: An error occurred in Effect.tryPromise\n    at cli.api\n"
    + "  [cause]: Error: Operation not found: experimental.job.list\n",
)


@pytest.fixture
def _isolated_reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state" / "reports"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(wait_loop.time, "sleep", slept.append)
    return slept


class FakeRunner:
    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], *, timeout_s: float | None
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self._responses.pop(0)


def ok(body: dict[str, Any]) -> tuple[int, str, str]:
    return 0, json.dumps(body), ""


SERVICE_UP = (0, f"{SERVICE_URL}\n", "")


@pytest.fixture(autouse=True)
def _opencode_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("herdr_routines.herdr.opencode_bin", lambda: "/x/opencode")


# -- HerdrClient.opencode_background_busy --------------------------------------------------


def test_busy_when_a_job_is_running_and_reads_nothing_else() -> None:
    runner_ = FakeRunner([SERVICE_UP, ok(JOBS_RUNNING)])
    assert HerdrClient(runner=runner_).opencode_background_busy(SESSION) is True
    assert runner_.calls[1] == [
        "/x/opencode",
        "api",
        "experimental.job.list",
        "--param",
        f"sessionID={SESSION}",
        "--param",
        "descendants=true",
    ]
    assert len(runner_.calls) == 2


def test_busy_when_the_session_is_still_active_without_a_job() -> None:
    """The parent re-enters `session.active` as the job disappears."""
    runner_ = FakeRunner([SERVICE_UP, ok(JOBS_NONE), ok(ACTIVE_PARENT)])
    assert HerdrClient(runner=runner_).opencode_background_busy(SESSION) is True
    assert runner_.calls[2][:5] == [
        "/x/opencode",
        "api",
        "session.active",
    ]


def test_not_busy_when_no_job_runs_and_the_session_is_inactive() -> None:
    """A child still active under the parent id does not count: its job shows in job.list."""
    runner_ = FakeRunner([SERVICE_UP, ok(JOBS_NONE), ok(ACTIVE_CHILD_ONLY)])
    assert HerdrClient(runner=runner_).opencode_background_busy(SESSION) is False


def test_a_stopped_service_makes_no_api_call() -> None:
    """A bare `opencode api` would start the service in this process; `--server` is no
    alternative (the service demands a password for it)."""
    runner_ = FakeRunner([(0, "stopped\n", "")])
    with pytest.raises(HerdrCliError, match="service is not running"):
        HerdrClient(runner=runner_).opencode_background_busy(SESSION)
    assert runner_.calls == [["/x/opencode", "service", "status"]]


def test_an_opencode_without_job_list_raises_unsupported() -> None:
    """opencode 2.1.4 prints the operation error after a stack trace."""
    runner_ = FakeRunner([SERVICE_UP, NOT_FOUND])
    with pytest.raises(OpencodeJobsUnsupported):
        HerdrClient(runner=runner_).opencode_background_busy(SESSION)


def test_a_malformed_job_list_raises() -> None:
    runner_ = FakeRunner([SERVICE_UP, ok({"data": "nope"})])
    with pytest.raises(HerdrCliError, match="shape"):
        HerdrClient(runner=runner_).opencode_background_busy(SESSION)


# -- drain_opencode_background --------------------------------------------------------------


class DrainClient:
    """Scripted per poll: each entry is a busy bool, an exception to raise, or a herdr status."""

    def __init__(
        self,
        polls: list[Any],
        *,
        session_id: str | None = SESSION,
        screen: str = "",
    ) -> None:
        self._polls = list(polls)
        self._session_id = session_id
        self._screen = screen
        self.calls: list[str] = []
        self.status = "idle"

    def agent_session_id(self, target: str) -> str | None:
        self.calls.append("agent_session_id")
        return self._session_id

    def agent_current_status(self, target: str) -> str:
        self.calls.append("agent_current_status")
        return self.status

    def opencode_background_busy(self, session_id: str) -> bool:
        self.calls.append("busy")
        poll = self._polls.pop(0)
        if isinstance(poll, Exception):
            raise poll
        if isinstance(poll, str):
            self.status = poll
            return False
        return bool(poll)

    def agent_read_visible(self, target: str, *, lines: int = 200) -> str:
        self.calls.append("read_visible")
        return self._screen


def drain(
    client: DrainClient,
    *,
    kind: str = "opencode",
    status: str | None = "idle",
    seconds: float = 3600,
    scan: Callable[[str], str | None] | None = None,
) -> str | None:
    return drain_opencode_background(
        client,  # type: ignore[arg-type]
        job_name="j",
        target="agent",
        agent_kind=kind,
        status=status,  # type: ignore[arg-type]
        deadline=wait_loop.time.monotonic() + seconds,
        scan=scan,
    )


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_other_agent_kinds_never_touch_opencode(kind: str) -> None:
    client = DrainClient([])
    assert drain(client, kind=kind) == "idle"
    assert client.calls == []


@pytest.mark.parametrize("status", ["blocked", "working", "unknown", None])
def test_an_unsettled_status_is_returned_untouched(status: str | None) -> None:
    client = DrainClient([])
    assert drain(client, status=status) == status
    assert client.calls == []


def test_finished_only_after_two_consecutive_quiet_polls(
    _no_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    """busy, quiet, busy, quiet, quiet: the lone quiet poll between busy ones (the ~10 ms
    gap between a child ending and the parent waking) must not end the wait."""
    client = DrainClient([True, False, True, False, False])
    with caplog.at_level(logging.INFO, logger="herdr_routines.wait_loop"):
        assert drain(client) == "idle"
    assert client.calls.count("busy") == 5
    assert _no_sleep == [
        wait_loop.OPENCODE_DRAIN_POLL_S,
        wait_loop.OPENCODE_DRAIN_CONFIRM_S,
        wait_loop.OPENCODE_DRAIN_POLL_S,
        wait_loop.OPENCODE_DRAIN_CONFIRM_S,
    ]
    assert "has background work; waiting up to" in caplog.text
    assert "background work finished" in caplog.text


def test_no_background_work_costs_one_confirm_poll_and_logs_nothing(
    _no_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    client = DrainClient([False, False])
    with caplog.at_level(logging.INFO, logger="herdr_routines.wait_loop"):
        assert drain(client) == "idle"
    assert _no_sleep == [wait_loop.OPENCODE_DRAIN_CONFIRM_S]
    assert caplog.text == ""


def test_the_deadline_bounds_the_wait_and_returns_working(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(wait_loop.time, "monotonic", lambda: float(next(clock)))
    client = DrainClient([True] * 50)
    with caplog.at_level(logging.WARNING, logger="herdr_routines.wait_loop"):
        assert drain(client, seconds=250) == "working"
    assert client.calls.count("busy") < 5
    assert "still running at the deadline" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        OpencodeJobsUnsupported("no job.list", exit_code=1),
        HerdrCliError("x", exit_code=1),
    ],
)
def test_an_unreadable_first_poll_keeps_todays_behaviour(error: Exception) -> None:
    client = DrainClient([error])
    assert drain(client) == "idle"
    assert client.calls.count("busy") == 1


def test_a_later_read_error_counts_as_busy() -> None:
    client = DrainClient([True, HerdrCliError("blip", exit_code=1), False, False])
    assert drain(client) == "idle"
    assert client.calls.count("busy") == 4


def test_no_session_id_keeps_todays_behaviour() -> None:
    client = DrainClient([], session_id=None)
    assert drain(client) == "idle"
    assert "busy" not in client.calls


def test_a_status_that_turns_blocked_mid_wait_is_returned() -> None:
    client = DrainClient([True, "blocked"])
    assert drain(client) == "blocked"


def test_the_marker_scan_runs_during_the_wait_and_kills_on_a_stable_hit() -> None:
    seen: list[str] = []

    def scan(screen: str) -> str | None:
        seen.append(screen)
        return "Free usage exceeded" if len(seen) >= 2 else None

    client = DrainClient([True] * 10, screen="Free usage exceeded")
    with pytest.raises(PromptWatchdogKilled) as caught:
        drain(client, scan=scan)
    assert caught.value.marker == "Free usage exceeded"
    assert len(seen) == 2


# -- the wired entry points ------------------------------------------------------------------


class SettlesClient(DrainClient):
    def agent_prompt_wait_with_watchdog(self, **kwargs: Any) -> str:
        self.calls.append("prompt")
        kwargs["on_poll"]("")
        return "idle"


def test_prompt_with_watchdog_waits_out_subagents_and_keeps_the_heartbeat() -> None:
    client = SettlesClient([True, True, False, False])
    beats: list[None] = []
    status = prompt_with_watchdog(
        client,  # type: ignore[arg-type]
        job_name="j",
        target="agent",
        text="t",
        timeout_ms=3_600_000,
        markers=(),
        prompt_text="t",
        agent_kind="opencode",
        on_poll_hook=lambda: beats.append(None),
    )
    assert status == "idle"
    assert client.calls.count("busy") == 4
    assert len(beats) == 1 + 3  # the prompt's own poll, then one per sleep in the drain


def test_prompt_with_watchdog_does_not_drain_for_claude() -> None:
    client = SettlesClient([])
    assert (
        prompt_with_watchdog(
            client,  # type: ignore[arg-type]
            job_name="j",
            target="agent",
            text="t",
            timeout_ms=1000,
            markers=(),
            prompt_text="t",
            agent_kind="claude",
        )
        == "idle"
    )
    assert client.calls == ["prompt"]


class SubagentsFinishAfterFirstSettle(ScriptedClient):
    """The issue 094 incident: herdr settles idle with no report while a subagent runs; the
    report appears only once it finishes."""

    def __init__(self, report_path: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._report_path = report_path
        self._busy_polls = [True, True, False, False]

    def agent_current_status(self, target: str) -> str:
        return "idle"

    def opencode_background_busy(self, session_id: str) -> bool:
        self.calls.append("busy")
        busy = self._busy_polls.pop(0)
        if not busy:
            self._report_path.write_text("# Report\n\nReview posted.\n")
        return busy


def test_execute_run_waits_for_subagents_before_nudging_or_verifying(
    tmp_path: Path,
    _isolated_reports_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("herdr_routines.runner.ensure_repo", lambda job: job.repo)
    job = make_job(tmp_path, agent_kind="opencode", model="opencode/big-pickle")
    run_id = "a-run-094"
    _isolated_reports_dir.mkdir(parents=True, exist_ok=True)
    client = SubagentsFinishAfterFirstSettle(
        _isolated_reports_dir / f"{run_id}.md", agent_status="idle"
    )
    outcome = execute_run(job, client, run_id=run_id)  # type: ignore[arg-type]
    assert (outcome.state, outcome.reason, outcome.error) == ("done", None, None)
    assert outcome.nudged is False
    assert outcome.report_written is True
    assert client.calls.count("agent_prompt_wait") == 1


def test_the_nudge_drains_subagents_too(monkeypatch: pytest.MonkeyPatch) -> None:
    drained: list[dict[str, Any]] = []

    def record(client: object, **kwargs: Any) -> None:
        drained.append(kwargs)

    monkeypatch.setattr(runner, "drain_opencode_background", record)

    class Client:
        def agent_prompt_wait(self, **_: Any) -> str:
            return "idle"

    runner._attempt_report_nudge(
        Client(),  # type: ignore[arg-type]
        target="agent",
        report_path=Path("/r.md"),
        agent_kind="opencode",
    )
    assert drained[0]["agent_kind"] == "opencode"
    assert drained[0]["status"] == "idle"
    assert "scan" not in drained[0]  # a one-shot nudge has no failure-marker watchdog
