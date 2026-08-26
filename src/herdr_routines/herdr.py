"""Thin typed wrapper over the `herdr` CLI. The only module in this package that shells out.

Every call goes through the injected `CommandRunner`, which is the seam `test_herdr.py` fakes
(see docs/plan-v1.md §7 tier 2); `runner.py` in turn tests against a faked `HerdrClient`. Nothing
here parses stdout beyond JSON — IDs are always read from the response, never predicted, per
Herdr's own SKILL.md guidance.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from herdr_routines.config import AGENT_MODEL_FLAGS

HERDR_BIN = "herdr"

log = logging.getLogger(__name__)

# How long agent_prompt_wait_with_watchdog waits between visible-screen polls while the
# `herdr agent prompt --wait` child runs. Sparse on purpose (see the phase-2 spec's
# poll-amplification risk): ~120 reads over a 60-min run, capped to 1–2 in the wedge case
# by the early exit on marker match.
PROMPT_WATCHDOG_POLL_S = 30.0

# Wrapper grace on top of timeout_ms before the watchdog declares the child hung — mirrors
# the blocking agent_prompt_wait's `timeout_ms / 1000 + 30` subprocess timeout exactly, so
# the no-marker worst case is bit-for-bit today's behavior.
PROMPT_WAIT_GRACE_S = 30.0

# TERM → wait this long → KILL, when terminating a wedged prompt child.
WATCHDOG_KILL_GRACE_S = 5.0

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

# Statuses safe to reap when reusing a recurring job's agent name. Only idle/done are
# provably settled and will never need human follow-up; blocked (waiting on approval,
# answerable from bed via herdr-push per plan-v1 §2) and unknown must never be reaped.
SETTLED_AGENT_STATUSES = frozenset({AGENT_STATUS_IDLE, AGENT_STATUS_DONE})


class HerdrCliError(Exception):
    """A `herdr` invocation exited non-zero. Carries the parsed JSON error body when there was
    one (exit 1, server error) vs. plain stderr text (exit 2, syntax error)."""

    def __init__(
        self, message: str, *, exit_code: int, error_body: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.error_body = error_body


class PromptWatchdogKilled(HerdrCliError):
    """Raised by agent_prompt_wait_with_watchdog when the failure-marker watchdog confirmed a
    match and the delivered-but-wedged prompt child was terminated early. Carries the matched
    marker and the final visible-screen text so callers can persist the diagnostic tail
    without re-reading a pane they are about to close. Constructed with no error_body on
    purpose: `_is_retryable_prompt_error` requires a dict body, so a killed delivery is
    structurally barred from the resend whitelist — one delivery, one terminal record."""

    def __init__(self, message: str, *, marker: str, screen_text: str) -> None:
        super().__init__(message, exit_code=1)
        self.marker = marker
        self.screen_text = screen_text


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


class WatchdogProcess(Protocol):
    """The Popen lifecycle surface the watchdog wait loop needs (poll → collect on exit /
    terminate when wedged). Kept as a protocol so tier-2 tests can script exact
    exit/terminate sequences deterministically instead of spawning real children
    (docs/plan-v1.md §7)."""

    def poll(self) -> int | None:
        """The child's exit code once finished, None while still running."""
        ...

    def collect(self) -> tuple[int, str, str]:
        """(exit_code, stdout, stderr) — only called after poll() reported an exit."""
        ...

    def terminate(self) -> None:
        """Best-effort TERM → short grace → KILL. Never raises: a failed kill must not break
        the caller's classification path, which still records the run and reaps the pane."""


class _RealWatchdogProcess:
    """subprocess.Popen adapter satisfying WatchdogProcess. Output is captured via pipes;
    herdr's prompt --wait emits its single small JSON blob only at settle, so not draining
    the pipes while the poll loop runs cannot deadlock the child."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc

    def poll(self) -> int | None:
        return self._proc.poll()

    def collect(self) -> tuple[int, str, str]:
        stdout, stderr = self._proc.communicate()
        return (
            self._proc.returncode,
            stdout or "",
            stderr or "",
        )

    def terminate(self) -> None:
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=WATCHDOG_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                # Even SIGKILL gets a bounded reap: a child stuck in uninterruptible kernel
                # sleep would otherwise block the watchdog loop forever. Nothing more can be
                # done at that point — log and leave the reaping to init.
                try:
                    self._proc.wait(timeout=WATCHDOG_KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    log.warning(
                        "herdr prompt child %s survived SIGKILL for %.0fs; giving up on "
                        "the reap",
                        self._proc.pid,
                        WATCHDOG_KILL_GRACE_S,
                    )
        except Exception as e:  # noqa: BLE001 — best-effort kill must never raise into the wait loop
            log.warning("could not terminate herdr prompt child: %s", e)


class PopenFactory(Protocol):
    """The seam tier-2 tests fake for the watchdog's child process; the default spawns a real
    Popen. Sibling of CommandRunner: reads/polls still go through `runner`, only the long-
    lived prompt child is created here."""

    def __call__(self, argv: list[str]) -> WatchdogProcess: ...


def _popen_process(argv: list[str]) -> WatchdogProcess:
    return _RealWatchdogProcess(
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    )


@dataclass(frozen=True, slots=True)
class HerdrClient:
    """Wraps `herdr` subcommands used by this project. Construct with a fake `runner` in tests;
    the default is a real subprocess call."""

    runner: CommandRunner = _subprocess_runner
    bin_path: str = HERDR_BIN
    popen_factory: PopenFactory = _popen_process

    def _call(
        self, args: list[str], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        argv = [self.bin_path, *args]
        exit_code, stdout, stderr = self.runner(argv, timeout_s=timeout_s)
        return self._parse_result(
            args, exit_code=exit_code, stdout=stdout, stderr=stderr
        )

    def _parse_result(
        self,
        args: list[str],
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> dict[str, Any]:
        """Maps one completed herdr invocation's (exit_code, stdout, stderr) onto _call's
        return-or-raise contract. Shared by `_call` and the watchdog's Popen path so child
        exits parse identically no matter how the process was waited on."""
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

    def agent_prompt_wait_with_watchdog(
        self,
        *,
        target: str,
        text: str,
        timeout_ms: int,
        poll_interval_s: float = PROMPT_WATCHDOG_POLL_S,
        on_poll: Callable[[str], str | None] | None = None,
    ) -> str:
        """agent_prompt_wait with a mid-run fast-fail watchdog (phase 2,
        docs/pipeline/runs/<run_id>/spec.md). Runs `herdr agent prompt --wait` under Popen
        and, every poll_interval_s while the child is still running, feeds
        agent_read_visible's screen text to `on_poll`. A non-None return means the caller
        confirmed a failure marker (stability gating across polls is the caller's job): the
        delivered-but-wedged child is terminated (TERM → grace → KILL, never raising) and
        PromptWatchdogKilled is raised carrying the matched marker plus the screen text that
        triggered it. Poll reads are inert: any CLI/server failure yields "" into on_poll,
        never an exception — an unreachable server degrades to today's timeout path. With no
        marker ever confirmed this is behavior-identical to agent_prompt_wait: child exits
        parse through _parse_result (including the exit-124 wrapper timeout at
        timeout_ms + PROMPT_WAIT_GRACE_S) and the settled status string comes back."""
        args = ["agent", "prompt", target, text, "--wait", "--timeout", str(timeout_ms)]
        proc = self.popen_factory([self.bin_path, *args])
        deadline = time.monotonic() + timeout_ms / 1000 + PROMPT_WAIT_GRACE_S
        while True:
            if proc.poll() is not None:
                exit_code, stdout, stderr = proc.collect()
                body = self._parse_result(
                    args, exit_code=exit_code, stdout=stdout, stderr=stderr
                )
                return _extract_status(body)
            if time.monotonic() >= deadline:
                proc.terminate()
                raise HerdrCliError(
                    f"herdr call timed out: {' '.join(args)}", exit_code=124
                )
            if on_poll is not None:
                try:
                    screen = self.agent_read_visible(target)
                except (HerdrCliError, OSError):
                    # agent_read_visible already returns "" on CLI failures; this also covers
                    # a vanished herdr binary / dead server surfacing as OSError.
                    screen = ""
                marker = on_poll(screen)
                if marker is not None:
                    proc.terminate()
                    raise PromptWatchdogKilled(
                        f"failure marker matched; prompt child terminated: {marker!r}",
                        marker=marker,
                        screen_text=screen,
                    )
            remaining = deadline - time.monotonic()
            time.sleep(min(max(poll_interval_s, 0.0), max(remaining, 0.0)))

    def agent_get(self, target: str) -> str:
        body = self._call(["agent", "get", target])
        return _extract_status(body)

    def agent_session_id(self, target: str) -> str | None:
        """Best-effort: the agent's underlying CLI session id (e.g. an opencode `ses_...`
        id) from `agent get`'s `agent_session.value`, or None on any CLI/shape failure —
        same best-effort contract as agent_read/agent_read_visible. Callers capture this
        before closing a settled run's pane so a human can later resume and inspect the
        conversation (`herdr agent start ... -s <session_id>`) without the pane/process
        needing to stay resident just for that purpose (pane-lifecycle v2 for routine
        jobs)."""
        exit_code, stdout, _stderr = self.runner(
            [self.bin_path, "agent", "get", target], timeout_s=10
        )
        if exit_code != 0:
            return None
        body = _try_parse_json(stdout)
        if not isinstance(body, dict):
            return None
        try:
            value = body["result"]["agent"]["agent_session"]["value"]
        except (KeyError, TypeError):
            return None
        return value if isinstance(value, str) and value else None

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

    def agent_read_visible(self, target: str, *, lines: int = 200) -> str:
        """agent_read against the currently-rendered viewport (`--source visible`). While an
        agent is unsettled herdr rejects recent-unwrapped reads with agent_not_idle — observed
        live 2026-08-23 ("alternate-screen history can only be captured by scrolling while
        idle") — and failure-path agents are definitionally unsettled, so post-mortem screen
        capture needs this variant. Same best-effort contract as agent_read: "" on any
        failure."""
        args = [
            "agent",
            "read",
            target,
            "--source",
            "visible",
            "--lines",
            str(lines),
        ]
        exit_code, stdout, _stderr = self.runner([self.bin_path, *args], timeout_s=30)
        return stdout if exit_code == 0 else ""

    def agent_interactive_ready(self, target: str) -> bool:
        """True when the target agent's TUI reports itself ready to accept typed input.
        Prompting before readiness fails server-side (~5s in, EmptyResponse) — observed live
        against OpenCode and Claude TUIs seconds after start (Pi deploy 2026-08-22). A response
        without the flag is treated as ready (fail-open) so older herdr builds keep working; a
        malformed shape raises HerdrCliError like the sibling parsers do, which the readiness
        poll loop in runner.py swallows and retries."""
        body = self._call(["agent", "get", target])
        result = body.get("result")
        agent = result.get("agent") if isinstance(result, dict) else None
        if not isinstance(agent, dict):
            raise HerdrCliError(
                f"unexpected herdr agent response shape: {body!r}", exit_code=0
            )
        ready = agent.get("interactive_ready")
        return ready if isinstance(ready, bool) else True

    def agent_statuses(self) -> dict[str, str]:
        """Maps every currently-registered agent name to its `agent_status`. A name stays
        registered (and shows up here) long after its run has settled to idle/done, until the
        tab is closed — callers that want "still actively running" must filter by status
        against LIVE_AGENT_STATUSES, not just check for name presence."""
        body = self._call(["agent", "list"], timeout_s=10)
        result = body.get("result")
        if not isinstance(result, dict):
            raise HerdrCliError(
                f"unexpected herdr agent response shape: {body!r}", exit_code=0
            )
        agents = result.get("agents")
        if not isinstance(agents, list):
            raise HerdrCliError(
                f"unexpected herdr agent response shape: {body!r}", exit_code=0
            )
        return {
            a["name"]: a["agent_status"]
            for a in agents
            if isinstance(a, dict) and a.get("name") and a.get("agent_status")
        }

    def settled_agent_workspace(self, name: str) -> str | None:
        """The workspace_id hosting registered agent `name`, if its status is settled
        (idle/done). None when absent, still working, blocked, unknown, or status
        unreadable — callers must never close a busy or human-blocked pane. Only
        idle/done are considered settled (see SETTLED_AGENT_STATUSES); blocked/unknown
        are sticky under cron and must not be reaped."""
        body = self._call(["agent", "list"], timeout_s=10)
        result = body.get("result")
        if not isinstance(result, dict):
            raise HerdrCliError(
                f"unexpected herdr agent response shape: {body!r}", exit_code=0
            )
        agents = result.get("agents")
        if not isinstance(agents, list):
            raise HerdrCliError(
                f"unexpected herdr agent response shape: {body!r}", exit_code=0
            )
        # Names are unique only among live agents (plan-v1 empirical notes), so scan
        # all entries: if any matching agent is live, blocked, unknown, or has an
        # unreadable status, treat the whole name as not-settled and return None.
        matched: list[dict[str, object]] = []
        for agent in agents:
            if not isinstance(agent, dict) or agent.get("name") != name:
                continue
            matched.append(agent)
        if not matched:
            return None
        for agent in matched:
            status = agent.get("agent_status")
            if not isinstance(status, str) or status not in SETTLED_AGENT_STATUSES:
                return None
        # All matched entries are settled — return the first one's workspace.
        workspace_id = matched[0].get("workspace_id")
        return workspace_id if isinstance(workspace_id, str) and workspace_id else None

    def settled_agent_pane(self, name: str) -> str | None:
        """The pane_id hosting registered agent `name`, if its status is settled
        (idle/done). None when absent, still working, blocked, unknown, or status
        unreadable. Narrower than settled_agent_workspace — reaping a single pane
        preserves sibling tabs/panes the user may have opened to inspect the last run."""
        body = self._call(["agent", "list"], timeout_s=10)
        result = body.get("result")
        if not isinstance(result, dict):
            raise HerdrCliError(
                f"unexpected herdr agent response shape: {body!r}", exit_code=0
            )
        agents = result.get("agents")
        if not isinstance(agents, list):
            raise HerdrCliError(
                f"unexpected herdr agent response shape: {body!r}", exit_code=0
            )
        matched: list[dict[str, object]] = []
        for agent in agents:
            if not isinstance(agent, dict) or agent.get("name") != name:
                continue
            matched.append(agent)
        if not matched:
            return None
        for agent in matched:
            status = agent.get("agent_status")
            if not isinstance(status, str) or status not in SETTLED_AGENT_STATUSES:
                return None
        pane_id = matched[0].get("pane_id")
        return pane_id if isinstance(pane_id, str) and pane_id else None

    def workspace_close(self, workspace_id: str) -> None:
        self._call(["workspace", "close", workspace_id], timeout_s=10)

    def pane_close(self, pane_id: str) -> None:
        self._call(["pane", "close", pane_id], timeout_s=10)

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
