"""Thin typed wrapper over the `herdr` CLI. The only module in this package that shells out.

Every call goes through the injected `CommandRunner`, which is the seam `test_herdr.py` fakes
(see docs/plan-v1.md §7 tier 2); `runner.py` in turn tests against a faked `HerdrClient`. Nothing
here parses stdout beyond JSON — IDs are always read from the response, never predicted, per
Herdr's own SKILL.md guidance.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from herdr_routines.config import AGENT_MODEL_FLAGS

HERDR_BIN = "herdr"

# Settle states `agent get`/`agent prompt --wait` can report. Confirmed empirically against
# herdr 0.8.2 (see docs/plan-v1.md step 5): a never-focused pane settles to "idle", not the
# "done" that SKILL.md's seen/unseen distinction predicted — so both map to success below.
AGENT_STATUS_IDLE = "idle"
AGENT_STATUS_DONE = "done"
AGENT_STATUS_WORKING = "working"
AGENT_STATUS_BLOCKED = "blocked"
AGENT_STATUS_UNKNOWN = "unknown"

# The only status that self-clears without outside intervention: an actively-working agent
# will transition on its own once it settles. idle/done are already settled. "blocked" and
# "unknown" are sticky under an unattended cron job — nothing in herdr-routines answers a
# blocked agent's prompt or resolves an unknown state — so treating them as "live" would just
# move the skip-forever bug (permanently registered, never re-evaluated) rather than fix it.
# See tick.py's _live_agent_exists.
LIVE_AGENT_STATUSES = frozenset({AGENT_STATUS_WORKING})


class HerdrCliError(Exception):
    """A `herdr` invocation exited non-zero. Carries the parsed JSON error body when there was
    one (exit 1, server error) vs. plain stderr text (exit 2, syntax error)."""

    def __init__(
        self, message: str, *, exit_code: int, error_body: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.error_body = error_body


class CommandRunner(Protocol):
    """The seam tier-2 tests fake: anything that can run an argv and return (exit_code, stdout,
    stderr). `HerdrClient` depends only on this, never on `subprocess` directly."""

    def __call__(
        self, argv: list[str], *, timeout_s: float | None
    ) -> tuple[int, str, str]: ...


def _subprocess_runner(
    argv: list[str], *, timeout_s: float | None
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr = e.stderr if isinstance(e.stderr, str) else ""
        return (
            124,
            stdout,
            stderr,
        )  # conventional timeout exit code; not one `herdr` itself uses
    return proc.returncode, proc.stdout, proc.stderr


@dataclass(frozen=True, slots=True)
class HerdrClient:
    """Wraps `herdr` subcommands used by this project. Construct with a fake `runner` in tests;
    the default is a real subprocess call."""

    runner: CommandRunner = _subprocess_runner
    bin_path: str = HERDR_BIN

    def _call(
        self, args: list[str], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        argv = [self.bin_path, *args]
        exit_code, stdout, stderr = self.runner(argv, timeout_s=timeout_s)
        if exit_code == 124:
            raise HerdrCliError(
                f"herdr call timed out: {' '.join(args)}", exit_code=exit_code
            )
        if exit_code == 2:
            raise HerdrCliError(
                f"herdr CLI syntax error: {stderr.strip()}", exit_code=exit_code
            )
        if exit_code == 1:
            body = _try_parse_json(stderr) or _try_parse_json(stdout)
            raise HerdrCliError(
                f"herdr server error running {' '.join(args)}: {stderr.strip() or stdout.strip()}",
                exit_code=exit_code,
                error_body=body,
            )
        if exit_code != 0:
            raise HerdrCliError(
                f"herdr exited {exit_code} running {' '.join(args)}: {stderr.strip()}",
                exit_code=exit_code,
            )
        body = _try_parse_json(stdout)
        if body is None:
            raise HerdrCliError(
                f"herdr returned non-JSON stdout for: {' '.join(args)}", exit_code=0
            )
        return body

    # -- workspace / worktree / pane creation ------------------------------------------------

    def worktree_create(
        self, *, cwd: str, branch: str, base: str, label: str | None = None
    ) -> str:
        """Returns the new workspace's root pane id."""
        args = [
            "worktree",
            "create",
            "--cwd",
            cwd,
            "--branch",
            branch,
            "--base",
            base,
            "--no-focus",
        ]
        if label:
            args += ["--label", label]
        body = self._call(args)
        return _extract_pane_id(body, path=("result", "root_pane", "pane_id"))

    def tab_create(self, *, cwd: str, label: str | None = None) -> str:
        """Returns the new tab's root pane id. Used for `workspace: root` jobs."""
        args = ["tab", "create", "--cwd", cwd, "--no-focus"]
        if label:
            args += ["--label", label]
        body = self._call(args)
        return _extract_pane_id(body, path=("result", "root_pane", "pane_id"))

    # -- agent lifecycle -----------------------------------------------------------------------

    def agent_start(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        start_timeout_ms: int,
        model: str | None = None,
    ) -> None:
        args = build_agent_start_args(
            name=name,
            kind=kind,
            pane_id=pane_id,
            start_timeout_ms=start_timeout_ms,
            model=model,
        )
        self._call(args, timeout_s=start_timeout_ms / 1000 + 10)

    def agent_prompt_wait(self, *, target: str, text: str, timeout_ms: int) -> str:
        """Sends the prompt, waits for settle, and returns the settled agent_status."""
        args = ["agent", "prompt", target, text, "--wait", "--timeout", str(timeout_ms)]
        body = self._call(args, timeout_s=timeout_ms / 1000 + 30)
        return _extract_status(body)

    def agent_get(self, target: str) -> str:
        body = self._call(["agent", "get", target])
        return _extract_status(body)

    def agent_read(self, target: str, *, lines: int = 200) -> str:
        args = [
            "agent",
            "read",
            target,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(lines),
        ]
        exit_code, stdout, _stderr = self.runner([self.bin_path, *args], timeout_s=30)
        return stdout if exit_code == 0 else ""

    def agent_statuses(self) -> dict[str, str]:
        """Maps every currently-registered agent name to its `agent_status`. A name stays
        registered (and shows up here) long after its run has settled to idle/done, until the
        tab is closed — callers that want "still actively running" must filter by status
        against LIVE_AGENT_STATUSES, not just check for name presence."""
        body = self._call(["agent", "list"])
        agents = body.get("result", {}).get("agents", [])
        return {
            a["name"]: a["agent_status"]
            for a in agents
            if a.get("name") and a.get("agent_status")
        }

    # -- misc -----------------------------------------------------------------------------------

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        args = ["notification", "show", title, "--sound", sound]
        if body:
            args += ["--body", body]
        self._call(args)


def build_agent_start_args(
    *,
    name: str,
    kind: str,
    pane_id: str,
    start_timeout_ms: int,
    model: str | None = None,
) -> list[str]:
    """Builds the `agent start` argv (without the leading `herdr` binary). Shared by
    `HerdrClient.agent_start` and `runner.build_dry_run_argv` so the two can't drift."""
    args = [
        "agent",
        "start",
        name,
        "--kind",
        kind,
        "--pane",
        pane_id,
        "--timeout",
        str(start_timeout_ms),
    ]
    if model is not None:
        flag = AGENT_MODEL_FLAGS.get(kind)
        if flag is None:
            raise ValueError(
                f"agent kind {kind!r} has no known native model flag "
                f"(supported: {sorted(AGENT_MODEL_FLAGS)})"
            )
        args += ["--", flag, model]
    return args


def _try_parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_pane_id(body: dict[str, Any], *, path: tuple[str, ...]) -> str:
    node: Any = body
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise HerdrCliError(
                f"unexpected herdr response shape, missing {path}: {body!r}",
                exit_code=0,
            )
        node = node[key]
    if not isinstance(node, str):
        raise HerdrCliError(
            f"unexpected herdr response shape at {path}: {body!r}", exit_code=0
        )
    return node


def _extract_status(body: dict[str, Any]) -> str:
    try:
        status = body["result"]["agent"]["agent_status"]
    except (KeyError, TypeError) as e:
        raise HerdrCliError(
            f"unexpected herdr agent response shape: {body!r}", exit_code=0
        ) from e
    if not isinstance(status, str):
        raise HerdrCliError(f"unexpected agent_status type in: {body!r}", exit_code=0)
    return status
