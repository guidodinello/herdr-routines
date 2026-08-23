"""Tier-2 tests: exercise HerdrClient against a fake CommandRunner. No `herdr` binary involved —
this tests command *construction* and response *handling* against the real API shapes recorded
in tests/fixtures/api-schema.json and observed live output (see docs/plan-v1.md §7)."""

from __future__ import annotations

import json

import pytest

from herdr_routines.herdr import HerdrClient, HerdrCliError


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
