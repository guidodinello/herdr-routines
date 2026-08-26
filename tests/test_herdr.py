"""Tier-2 tests: exercise HerdrClient against a fake CommandRunner. No `herdr` binary involved —
this tests command *construction* and response *handling* against the real API shapes recorded
in tests/fixtures/api-schema.json and observed live output (see docs/plan-v1.md §7)."""

from __future__ import annotations

import json
import logging
import subprocess

import pytest

from herdr_routines.herdr import (
    HerdrClient,
    HerdrCliError,
    PromptWatchdogKilled,
    _RealWatchdogProcess,
)


class FakeRunner:
    """Records every argv it was called with and returns canned (exit_code, stdout, stderr)
    responses in order."""

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], *, timeout_s: float | None
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        return self._responses.pop(0)


def ok(body: dict) -> tuple[int, str, str]:
    return 0, json.dumps(body), ""


def agent_response(status: str) -> dict:
    return {"result": {"agent": {"agent_status": status, "pane_id": "w1:p1"}}}


def test_worktree_create_builds_expected_argv_and_parses_pane_id() -> None:
    runner = FakeRunner([ok({"result": {"root_pane": {"pane_id": "w7:p1"}}})])
    client = HerdrClient(runner=runner)
    pane_id = client.worktree_create(cwd="/repo", branch="auto/job-1", base="main")
    assert pane_id == "w7:p1"
    argv = runner.calls[0]
    assert argv[0] == "herdr"
    assert argv[1:4] == ["worktree", "create", "--cwd"]
    assert "--branch" in argv and "auto/job-1" in argv
    assert "--base" in argv and "main" in argv
    assert "--no-focus" in argv


def test_tab_create_builds_root_mode_argv() -> None:
    runner = FakeRunner([ok({"result": {"root_pane": {"pane_id": "w2:p3"}}})])
    client = HerdrClient(runner=runner)
    pane_id = client.tab_create(cwd="/repo")
    assert pane_id == "w2:p3"
    argv = runner.calls[0]
    assert argv[1:3] == ["tab", "create"]
    assert "--no-focus" in argv


def test_agent_start_passes_timeout_and_raises_seam() -> None:
    runner = FakeRunner([ok({"result": {"agent": {"agent_status": "idle"}}})])
    client = HerdrClient(runner=runner)
    client.agent_start(
        name="rt-a", kind="claude", pane_id="w1:p1", start_timeout_ms=120_000
    )
    argv = runner.calls[0]
    assert argv[1:3] == ["agent", "start"]
    assert "rt-a" in argv
    assert "--kind" in argv and "claude" in argv
    assert "--pane" in argv and "w1:p1" in argv
    assert "--timeout" in argv and "120000" in argv


def test_agent_start_passes_claude_model_via_native_flag() -> None:
    runner = FakeRunner([ok({"result": {"agent": {"agent_status": "idle"}}})])
    client = HerdrClient(runner=runner)
    client.agent_start(
        name="rt-a",
        kind="claude",
        pane_id="w1:p1",
        start_timeout_ms=120_000,
        model="opus",
    )
    argv = runner.calls[0]
    assert argv[-3:] == ["--", "--model", "opus"]


def test_agent_start_passes_opencode_model_via_native_flag() -> None:
    runner = FakeRunner([ok({"result": {"agent": {"agent_status": "idle"}}})])
    client = HerdrClient(runner=runner)
    client.agent_start(
        name="rt-a",
        kind="opencode",
        pane_id="w1:p1",
        start_timeout_ms=120_000,
        model="opencode/big-pickle",
    )
    argv = runner.calls[0]
    assert argv[-3:] == ["--", "-m", "opencode/big-pickle"]


def test_agent_start_rejects_model_for_unsupported_kind() -> None:
    runner = FakeRunner([])
    client = HerdrClient(runner=runner)
    with pytest.raises(ValueError, match="codex"):
        client.agent_start(
            name="rt-a",
            kind="codex",
            pane_id="w1:p1",
            start_timeout_ms=120_000,
            model="something",
        )
    assert runner.calls == []


@pytest.mark.parametrize("status", ["idle", "done"])
def test_agent_prompt_wait_returns_settled_status_for_success_states(
    status: str,
) -> None:
    """Both idle and done map to success in runner.py — idle because that's what a
    never-focused pane actually settles to (verified empirically, docs/plan-v1.md step 5),
    done in case SKILL.md's documented distinction does apply under some other condition."""
    runner = FakeRunner([ok(agent_response(status))])
    client = HerdrClient(runner=runner)
    result = client.agent_prompt_wait(target="rt-a", text="hello", timeout_ms=60_000)
    assert result == status


def test_agent_prompt_wait_returns_blocked() -> None:
    runner = FakeRunner([ok(agent_response("blocked"))])
    client = HerdrClient(runner=runner)
    assert (
        client.agent_prompt_wait(target="rt-a", text="hi", timeout_ms=1000) == "blocked"
    )


def test_agent_prompt_wait_returns_unknown() -> None:
    runner = FakeRunner([ok(agent_response("unknown"))])
    client = HerdrClient(runner=runner)
    assert (
        client.agent_prompt_wait(target="rt-a", text="hi", timeout_ms=1000) == "unknown"
    )


def test_exit_1_with_json_stderr_raises_with_error_body() -> None:
    error_body = {
        "error": {"code": "agent_blocked", "message": "agent is waiting on approval"}
    }
    runner = FakeRunner([(1, "", json.dumps(error_body))])
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError) as exc_info:
        client.agent_prompt_wait(target="rt-a", text="hi", timeout_ms=1000)
    assert exc_info.value.exit_code == 1
    assert exc_info.value.error_body == error_body


def test_exit_2_is_syntax_error() -> None:
    runner = FakeRunner([(2, "", "error: unrecognized argument")])
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError) as exc_info:
        client.agent_get("rt-a")
    assert exc_info.value.exit_code == 2


def test_timeout_raises_with_exit_code_124() -> None:
    runner = FakeRunner([(124, "", "")])
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError) as exc_info:
        client.agent_prompt_wait(target="rt-a", text="hi", timeout_ms=1000)
    assert exc_info.value.exit_code == 124


def test_non_json_stdout_raises() -> None:
    runner = FakeRunner([(0, "not json at all", "")])
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError):
        client.agent_get("rt-a")


def test_missing_expected_field_raises() -> None:
    runner = FakeRunner([ok({"result": {"agent": {}}})])  # no agent_status key
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError):
        client.agent_get("rt-a")


def test_agent_statuses_maps_names_to_status() -> None:
    body = {
        "result": {
            "agents": [
                {"name": "rt-a", "agent_status": "working"},
                {"name": "rt-b", "agent_status": "idle"},
                {"pane_id": "no-name", "agent_status": "idle"},
            ]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.agent_statuses() == {"rt-a": "working", "rt-b": "idle"}


def test_agent_interactive_ready_parses_flag_and_argv() -> None:
    body = {"result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.agent_interactive_ready("rt-a") is True
    assert runner.calls[0][1:4] == ["agent", "get", "rt-a"]


def test_agent_interactive_ready_false_is_respected() -> None:
    body = {"result": {"agent": {"agent_status": "idle", "interactive_ready": False}}}
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.agent_interactive_ready("rt-a") is False


def test_agent_interactive_ready_fails_open_when_flag_absent() -> None:
    """Older herdr builds may not report the flag; absence must not wedge every run into
    agent_not_interactive — treat unknown as ready."""
    body = {"result": {"agent": {"agent_status": "idle"}}}
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.agent_interactive_ready("rt-a") is True


@pytest.mark.parametrize(
    "body",
    [
        {"result": None},
        {"result": ["unexpected"]},
        {"result": {}},
        {"result": {"agent": None}},
        {"result": {"agent": "w1:p1"}},
    ],
)
def test_agent_interactive_ready_converts_malformed_shapes_to_cli_error(
    body: dict,
) -> None:
    """Chained .get() used to raise AttributeError on null/non-dict nodes (the default only
    covers absent keys) — an exception type that bypassed both this method's contract and the
    readiness poll loop's HerdrCliError-only catch. Shape surprises must surface as
    HerdrCliError like _extract_status/_extract_pane_id; only the flag itself fails open."""
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError):
        client.agent_interactive_ready("rt-a")


def test_notification_show_includes_sound_and_optional_body() -> None:
    runner = FakeRunner([ok({"result": {}})])
    client = HerdrClient(runner=runner)
    client.notification_show("Job failed", body="see report", sound="request")
    argv = runner.calls[0]
    assert argv[1:3] == ["notification", "show"]
    assert "Job failed" in argv
    assert "--sound" in argv and "request" in argv
    assert "--body" in argv and "see report" in argv


def test_agent_read_returns_empty_string_on_failure_rather_than_raising() -> None:
    """agent_read is diagnostic/best-effort (docs/plan-v1.md §6 layer 2) — a failure here must
    not blow up the run, just yield nothing to attach to the report."""
    runner = FakeRunner([(1, "", "some error")])
    client = HerdrClient(runner=runner)
    assert client.agent_read("rt-a") == ""


def test_agent_read_visible_builds_expected_argv() -> None:
    """Failure-path screen capture reads the live viewport (--source visible): herdr rejects
    recent-unwrapped while an agent is unsettled (agent_not_idle, observed 2026-08-23)."""
    runner = FakeRunner([(0, "Free usage exceeded", "")])
    client = HerdrClient(runner=runner)
    assert client.agent_read_visible("rt-a") == "Free usage exceeded"
    argv = runner.calls[0]
    assert argv[1:3] == ["agent", "read"]
    assert "--source" in argv and "visible" in argv
    assert "--lines" in argv and "200" in argv


def test_agent_read_visible_returns_empty_string_on_failure() -> None:
    """Same best-effort contract as agent_read: "" on any failure, never a raise."""
    runner = FakeRunner([(1, "", "some error")])
    client = HerdrClient(runner=runner)
    assert client.agent_read_visible("rt-a") == ""


# -- settled_agent_* / pane_close tier-2 tests -----------------------------------------------


def test_settled_agent_pane_returns_pane_when_settled() -> None:
    body = {
        "result": {
            "agents": [
                {
                    "name": "rt-a",
                    "agent_status": "idle",
                    "pane_id": "w9:p1",
                    "workspace_id": "w9",
                }
            ]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") == "w9:p1"
    assert runner.calls[0][1:3] == ["agent", "list"]


def test_settled_agent_workspace_returns_workspace_when_settled() -> None:
    body = {
        "result": {
            "agents": [
                {
                    "name": "rt-a",
                    "agent_status": "done",
                    "pane_id": "w9:p1",
                    "workspace_id": "w9",
                }
            ]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_workspace("rt-a") == "w9"
    assert runner.calls[0][1:3] == ["agent", "list"]


def test_settled_agent_pane_does_not_return_for_working() -> None:
    body = {
        "result": {
            "agents": [
                {
                    "name": "rt-a",
                    "agent_status": "working",
                    "pane_id": "w9:p1",
                    "workspace_id": "w9",
                }
            ]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") is None


def test_settled_agent_pane_does_not_return_for_blocked() -> None:
    body = {
        "result": {
            "agents": [
                {
                    "name": "rt-a",
                    "agent_status": "blocked",
                    "pane_id": "w9:p1",
                    "workspace_id": "w9",
                }
            ]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") is None


def test_settled_agent_pane_does_not_return_for_unknown() -> None:
    body = {
        "result": {
            "agents": [
                {
                    "name": "rt-a",
                    "agent_status": "unknown",
                    "pane_id": "w9:p1",
                    "workspace_id": "w9",
                }
            ]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") is None


def test_settled_agent_pane_does_not_return_when_status_missing() -> None:
    body = {
        "result": {
            "agents": [{"name": "rt-a", "pane_id": "w9:p1", "workspace_id": "w9"}]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") is None


def test_settled_agent_pane_first_match_fragility() -> None:
    """If any matching entry is still live, the name must not be considered settled —
    names are unique only among live agents, so a settled duplicate listed before a
    working one must not be reaped."""
    body = {
        "result": {
            "agents": [
                {
                    "name": "rt-a",
                    "agent_status": "idle",
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                },
                {
                    "name": "rt-a",
                    "agent_status": "working",
                    "pane_id": "w2:p1",
                    "workspace_id": "w2",
                },
            ]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") is None


def test_settled_agent_pane_missing_pane_id_returns_none() -> None:
    body = {"result": {"agents": [{"name": "rt-a", "agent_status": "idle"}]}}
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") is None


def test_settled_agent_pane_absent_name_returns_none() -> None:
    body = {
        "result": {
            "agents": [{"name": "rt-other", "agent_status": "idle", "pane_id": "w1:p1"}]
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.settled_agent_pane("rt-a") is None


@pytest.mark.parametrize(
    "body",
    [
        {"result": None},
        {"result": {"agents": None}},
        {"result": {"agents": "bad"}},
        {"result": []},
    ],
)
def test_settled_agent_pane_converts_malformed_shapes_to_cli_error(body: dict) -> None:
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError):
        client.settled_agent_pane("rt-a")


def test_pane_close_builds_expected_argv() -> None:
    runner = FakeRunner([ok({"result": {}})])
    client = HerdrClient(runner=runner)
    client.pane_close("w9:p1")
    assert runner.calls[0] == ["herdr", "pane", "close", "w9:p1"]


def test_workspace_close_builds_expected_argv() -> None:
    runner = FakeRunner([ok({"result": {}})])
    client = HerdrClient(runner=runner)
    client.workspace_close("w9")
    assert runner.calls[0] == ["herdr", "workspace", "close", "w9"]


@pytest.mark.parametrize(
    "body",
    [
        {"result": None},
        {"result": {"agents": None}},
    ],
)
def test_agent_statuses_converts_malformed_shapes_to_cli_error(body: dict) -> None:
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    with pytest.raises(HerdrCliError):
        client.agent_statuses()


# -- agent_session_id (pane-lifecycle v2 for routine jobs) ----------------------------------


def test_agent_session_id_parses_value_and_builds_expected_argv() -> None:
    body = {
        "result": {
            "agent": {
                "agent_status": "done",
                "agent_session": {
                    "agent": "opencode",
                    "kind": "id",
                    "source": "herdr:opencode",
                    "value": "ses_fc9c01dabffeMGspFeeB9flH7y",
                },
            }
        }
    }
    runner = FakeRunner([ok(body)])
    client = HerdrClient(runner=runner)
    assert client.agent_session_id("rt-a") == "ses_fc9c01dabffeMGspFeeB9flH7y"
    assert runner.calls[0] == ["herdr", "agent", "get", "rt-a"]


def test_agent_session_id_returns_none_on_failure_rather_than_raising() -> None:
    """Best-effort, same contract as agent_read/agent_read_visible — a failure here must not
    block the run or the pane close that follows it."""
    runner = FakeRunner([(1, "", "some error")])
    client = HerdrClient(runner=runner)
    assert client.agent_session_id("rt-a") is None


def test_agent_session_id_returns_none_on_non_json_stdout() -> None:
    runner = FakeRunner([(0, "not json at all", "")])
    client = HerdrClient(runner=runner)
    assert client.agent_session_id("rt-a") is None


def test_agent_session_id_returns_none_when_session_absent() -> None:
    """Not every agent kind necessarily reports a session id — absence is not an error."""
    runner = FakeRunner([ok({"result": {"agent": {"agent_status": "done"}}})])
    client = HerdrClient(runner=runner)
    assert client.agent_session_id("rt-a") is None


# -- agent_prompt_wait_with_watchdog (phase 2: mid-run fast-fail watchdog) -------------------


class FakeWatchdogProcess:
    """Scripted WatchdogProcess: `polls` yields successive poll() results (None = child still
    running); once exhausted, poll() reports `returncode`. Records termination calls."""

    def __init__(
        self,
        *,
        polls: list[int | None],
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._polls = list(polls)
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.terminate_calls = 0

    def poll(self) -> int | None:
        if self._polls:
            return self._polls.pop(0)
        return self.returncode

    def collect(self) -> tuple[int, str, str]:
        return self.returncode, self._stdout, self._stderr

    def terminate(self) -> None:
        # Honors the never-raises contract the real _RealWatchdogProcess.terminate has.
        self.terminate_calls += 1


class ScriptedPopenFactory:
    """PopenFactory fake handing back one scripted process and recording every argv."""

    def __init__(self, process: FakeWatchdogProcess) -> None:
        self.process = process
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str]) -> FakeWatchdogProcess:
        self.argvs.append(argv)
        return self.process


class ExplodingReadRunner:
    """CommandRunner fake whose every invocation raises OSError — what spawning a vanished
    `herdr` binary does mid-run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], *, timeout_s: float | None
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        raise FileNotFoundError(2, "No such file or directory: 'herdr'")


class FakeTime:
    """Controllable clock for the watchdog wait loop: sleep() advances monotonic time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def idle_json() -> str:
    return json.dumps({"result": {"agent": {"agent_status": "idle"}}})


def test_agent_prompt_wait_with_watchdog_argv_and_status() -> None:
    proc = FakeWatchdogProcess(polls=[None], returncode=0, stdout=idle_json())
    factory = ScriptedPopenFactory(proc)
    client = HerdrClient(runner=FakeRunner([]), popen_factory=factory)
    status = client.agent_prompt_wait_with_watchdog(
        target="rt-a",
        text="hello",
        timeout_ms=60_000,
        poll_interval_s=0.0,
        on_poll=None,
    )
    assert status == "idle"
    assert factory.argvs == [
        ["herdr", "agent", "prompt", "rt-a", "hello", "--wait", "--timeout", "60000"]
    ]


def test_agent_prompt_wait_with_watchdog_polls_visible_argv() -> None:
    """Each poll is an `agent read --source visible --lines 200` through the CommandRunner
    seam (the argv pin failure-reaping §8 requires, so a herdr bump renaming flags is caught),
    and on_poll sees exactly the screens those reads returned."""
    read_argv = [
        "herdr",
        "agent",
        "read",
        "rt-a",
        "--source",
        "visible",
        "--lines",
        "200",
    ]
    runner = FakeRunner([(0, "screen one", ""), (0, "screen two", "")])
    proc = FakeWatchdogProcess(polls=[None, None], returncode=0, stdout=idle_json())
    client = HerdrClient(runner=runner, popen_factory=ScriptedPopenFactory(proc))
    seen: list[str] = []
    status = client.agent_prompt_wait_with_watchdog(
        target="rt-a",
        text="hi",
        timeout_ms=60_000,
        poll_interval_s=0.0,
        on_poll=seen.append,
    )
    assert status == "idle"
    assert seen == ["screen one", "screen two"]
    assert runner.calls == [read_argv, read_argv]
    assert proc.terminate_calls == 0


def test_agent_prompt_wait_with_watchdog_kills_child_on_confirmed_marker() -> None:
    screen = "Free usage exceeded, subscribe to Go [retrying in 3h 35m attempt #1]"
    proc = FakeWatchdogProcess(polls=[None], returncode=0, stdout=idle_json())
    runner = FakeRunner([(0, screen, "")])
    client = HerdrClient(runner=runner, popen_factory=ScriptedPopenFactory(proc))

    def confirm(screen_text: str) -> str | None:
        return "Free usage exceeded" if screen_text else None

    with pytest.raises(PromptWatchdogKilled) as excinfo:
        client.agent_prompt_wait_with_watchdog(
            target="rt-a",
            text="hi",
            timeout_ms=60_000,
            poll_interval_s=0.0,
            on_poll=confirm,
        )
    assert excinfo.value.marker == "Free usage exceeded"
    assert excinfo.value.screen_text == screen
    assert excinfo.value.exit_code == 1
    assert excinfo.value.error_body is None
    assert proc.terminate_calls == 1


def test_agent_prompt_wait_with_watchdog_poll_cli_failure_is_inert() -> None:
    """A failed read exits non-zero → agent_read_visible returns "" → on_poll sees "" and the
    loop keeps waiting; the child's own settle still decides the outcome."""
    runner = FakeRunner([(1, "", "server down"), (1, "", "server down")])
    proc = FakeWatchdogProcess(polls=[None, None], returncode=0, stdout=idle_json())
    client = HerdrClient(runner=runner, popen_factory=ScriptedPopenFactory(proc))
    seen: list[str] = []
    status = client.agent_prompt_wait_with_watchdog(
        target="rt-a",
        text="hi",
        timeout_ms=60_000,
        poll_interval_s=0.0,
        on_poll=seen.append,
    )
    assert status == "idle"
    assert seen == ["", ""]
    assert proc.terminate_calls == 0


def test_agent_prompt_wait_with_watchdog_poll_oserror_is_inert() -> None:
    """A vanished `herdr` binary raising OSError out of the read must not escape the wait
    loop either — it degrades to "" this poll, exactly like a CLI-level failure."""
    runner = ExplodingReadRunner()
    proc = FakeWatchdogProcess(polls=[None], returncode=0, stdout=idle_json())
    client = HerdrClient(runner=runner, popen_factory=ScriptedPopenFactory(proc))
    seen: list[str] = []
    status = client.agent_prompt_wait_with_watchdog(
        target="rt-a",
        text="hi",
        timeout_ms=60_000,
        poll_interval_s=0.0,
        on_poll=seen.append,
    )
    assert status == "idle"
    assert seen == [""]
    assert proc.terminate_calls == 0


def test_agent_prompt_wait_with_watchdog_child_exit_parity() -> None:
    """Exit-1-with-JSON-body from the child must raise the same HerdrCliError shape the
    blocking sibling raises — classification code downstream can't tell the difference."""
    error_body = {"error": {"code": "timeout", "message": "timed out"}}
    proc = FakeWatchdogProcess(polls=[], returncode=1, stderr=json.dumps(error_body))
    client = HerdrClient(
        runner=FakeRunner([]), popen_factory=ScriptedPopenFactory(proc)
    )
    with pytest.raises(HerdrCliError) as excinfo:
        client.agent_prompt_wait_with_watchdog(
            target="rt-a", text="hi", timeout_ms=1000, poll_interval_s=0.0, on_poll=None
        )
    assert excinfo.value.exit_code == 1
    assert excinfo.value.error_body == error_body


def test_agent_prompt_wait_with_watchdog_wrapper_deadline_terminates_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that never exits trips the wrapper deadline at timeout_ms + PROMPT_WAIT_GRACE_S
    — same exit-124 shape as the blocking path — after terminating it."""
    fake_time = FakeTime()
    monkeypatch.setattr("herdr_routines.herdr.time", fake_time)
    proc = FakeWatchdogProcess(polls=[None] * 50)  # wedged: never exits by itself
    client = HerdrClient(
        runner=FakeRunner([(0, "", "")]), popen_factory=ScriptedPopenFactory(proc)
    )
    with pytest.raises(HerdrCliError) as excinfo:
        client.agent_prompt_wait_with_watchdog(
            target="rt-a",
            text="hi",
            timeout_ms=60_000,
            poll_interval_s=30.0,
            on_poll=None,
        )
    assert excinfo.value.exit_code == 124
    assert proc.terminate_calls == 1
    # Slept through the full effective window (timeout + grace), then tripped.
    assert fake_time.now >= 1000.0 + 90.0


def test_real_process_wrapper_swallows_terminate_failures() -> None:
    class ExplodingInner:
        pid = 4242

        def terminate(self) -> None:
            raise OSError("no such process")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    # The wrapper's terminate must never raise, whatever the inner Popen does.
    _RealWatchdogProcess(ExplodingInner()).terminate()  # type: ignore[arg-type]


def test_real_process_wrapper_escalates_to_kill_after_grace() -> None:
    class StubbornInner:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.pid = 4242

        def terminate(self) -> None:
            self.events.append("term")

        def kill(self) -> None:
            self.events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            if "wait" not in self.events:
                self.events.append("wait")
                raise subprocess.TimeoutExpired(
                    cmd="herdr", timeout=float(timeout or 0.0)
                )
            self.events.append("reaped")
            return -9

    inner = StubbornInner()
    _RealWatchdogProcess(inner).terminate()  # type: ignore[arg-type]
    assert inner.events == ["term", "wait", "kill", "reaped"]


def test_real_process_wrapper_gives_up_after_sigkill_survivor() -> None:
    """A child stuck in uninterruptible kernel sleep can ignore even SIGKILL — the bounded
    post-kill reap logs a warning and returns instead of blocking the watchdog loop forever."""

    class ImmortalInner:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.pid = 4242

        def terminate(self) -> None:
            self.events.append("term")

        def kill(self) -> None:
            self.events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            self.events.append(f"wait:{timeout}")
            raise subprocess.TimeoutExpired(cmd="herdr", timeout=float(timeout or 0.0))

    inner = ImmortalInner()
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("herdr_routines.herdr")
    logger.addHandler(handler)
    try:
        _RealWatchdogProcess(inner).terminate()  # type: ignore[arg-type]  # must not raise
    finally:
        logger.removeHandler(handler)

    assert inner.events == ["term", "wait:5.0", "kill", "wait:5.0"]
    assert any("survived SIGKILL" in r.getMessage() for r in records)
