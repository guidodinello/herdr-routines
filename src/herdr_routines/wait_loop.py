"""The one shared "prompt under a failure-marker watchdog, with bounded retries" wait
loop (issue 056, spec §4).

`runner.py` and `pipeline_run.py` both need exactly this primitive, and it already
existed twice: once in `runner._prompt_with_watchdog`, once as the bash
marker-poll loop inside `scripts/pipeline-launch.sh`. This module is the surviving
copy, lifted out of `runner.py` unchanged at issue 056 so the routine-job path's
behaviour stayed byte-identical; the one addition then was `on_poll_hook`, which is what
`pipeline_watchdog.is_stalled` reads: a heartbeat line written from inside the very
closure that scans the screen for failure markers, so it advances on every poll by
construction rather than by a model remembering to. Issue 094 has since added
`drain_opencode_background`: for an opencode agent a settle is only final once its
background subagents are done.

The `on_poll_hook` name (rather than another `on_poll`) is deliberate: `on_poll` is
already `HerdrClient.agent_prompt_wait_with_watchdog`'s screen-text-in/marker-out
contract and a zero-arg callback cannot be composed with it. `scan` below wraps both:
it fires the hook, then does the marker scan, on every poll.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from herdr_routines.config import OPENCODE_KIND
from herdr_routines.herdr import (
    AGENT_STATUS_WORKING,
    SETTLED_AGENT_STATUSES,
    AgentStatus,
    HerdrClient,
    HerdrCliError,
    OpencodeJobsUnsupported,
    PromptWatchdogKilled,
)

log = logging.getLogger(__name__)

# How often the mid-run watchdog polls the visible screen while the prompt child
# waits. Mirrors herdr.py's PROMPT_WATCHDOG_POLL_S default; module-level so tests can
# adjust it, same style as runner.READY_POLL_INTERVAL_S.
WATCHDOG_POLL_INTERVAL_S = 30.0

# Issue 094: how long to wait between polls while an opencode session still has background
# subagents, and between the two quiet polls that confirm it has none (the parent wakes
# ~10 ms after its last child ends, so one quiet poll can fall in that gap).
OPENCODE_DRAIN_POLL_S = 15.0
OPENCODE_DRAIN_CONFIRM_S = 2.0
_DRAIN_QUIET_POLLS = 2
# Consecutive failed reads of the background state after the first poll before the drain
# gives up and trusts the settle (a vanished agent or a dead service would otherwise spin
# until the deadline).
_DRAIN_MAX_READ_ERRORS = 5

# Retries for the prompt send itself (see prompt_with_watchdog's docstring).
PROMPT_RETRY_DELAYS_S = (5.0, 15.0)


def error_body_code(e: HerdrCliError) -> str | None:
    """The parsed error body's error.code when it is a string, else None. Never raises:
    both callers run inside except blocks, where crashing on a malformed body (e.g. a
    flat {"error": "timeout"}) would mask the original failure."""
    body = e.error_body
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def is_settle_timeout(e: HerdrCliError) -> bool:
    """True when the prompt was delivered but the agent didn't settle within timeout_ms
    (herdr exits 1 with a JSON body, code "timeout"). Resending in that case would
    double-prompt. Any malformed body conservatively classifies as not-a-settle-timeout
    rather than raising — see error_body_code."""
    return error_body_code(e) == "timeout"


def is_retryable_prompt_error(e: HerdrCliError) -> bool:
    """True only for provably-early server rejections: herdr exited 1 with a parsed JSON
    error body whose error.code is present and is not "timeout" (the session-not-ready
    EmptyResponse). Everything else raises immediately:
      - exit 124 (_subprocess_runner wrapper timeout): herdr ran past timeout_ms + grace,
        so the prompt was almost certainly delivered;
      - exit 0 shape errors (_extract_status): delivery AND settle already succeeded —
        only the response JSON was unexpected;
      - exit 1 without a parseable body or without an error.code: delivery state unknown.
    Resending in any terminal case risks duplicating the run's side effects."""
    if e.exit_code != 1 or not isinstance(e.error_body, dict):
        return False
    code = error_body_code(e)
    return code is not None and code != "timeout"


def matched_failure_marker(
    screen_text: str, markers: Sequence[str], prompt_text: str
) -> str | None:
    """The first marker visible on screen and not verbatim in the job's own prompt (the
    visible screen contains the prompt echo — docs/failure-reaping.md §3.2's
    false-positive guard). Empty screens match nothing."""
    if not screen_text:
        return None
    for marker in markers:
        if marker and marker in screen_text and marker not in prompt_text:
            return marker
    return None


def drain_opencode_background(
    client: HerdrClient,
    *,
    job_name: str,
    target: str,
    agent_kind: str,
    status: AgentStatus | None,
    deadline: float,
    scan: Callable[[str], str | None] | None = None,
) -> AgentStatus | None:
    """Issue 094: for an opencode agent that herdr reports settled, wait until its session
    has no background subagent left, and return the status to act on.

    opencode 2 runs subagents in the background; its parent session goes `idle` while they
    run, which herdr reports as `idle`/`done`. Finished means herdr settled AND
    `opencode_background_busy` is False on two consecutive polls. Any other kind, or an
    unsettled `status`, comes back untouched, so claude and codex never see the opencode API.

    `deadline` is a `time.monotonic()` instant. Reaching it returns `working` (callers file
    that as unsettled). If the first poll cannot read the background state (service
    stopped, opencode without `experimental.job.list`, no session id) it logs and returns
    `status`: unverifiable never blocks, which is the behaviour before this existed. A
    later read error counts as busy. `scan` is the watchdog's per-poll closure, so the
    heartbeat and failure-marker gate keep running; a confirmed marker raises
    PromptWatchdogKilled exactly as during the main wait."""
    if agent_kind != OPENCODE_KIND or status not in SETTLED_AGENT_STATUSES:
        return status
    session_id = client.agent_session_id(target)
    if session_id is None:
        log.warning("%s: no opencode session id; not waiting for subagents", job_name)
        return status
    started = time.monotonic()
    quiet = 0
    first_poll = True
    read_errors = 0
    announced = False
    while True:
        try:
            current = client.agent_current_status(target)
            if current not in SETTLED_AGENT_STATUSES and current not in (
                None,
                AGENT_STATUS_WORKING,
            ):
                return current  # blocked/unknown: the caller's existing handling
            busy = client.opencode_background_busy(session_id) or (
                current not in SETTLED_AGENT_STATUSES
            )
        except OpencodeJobsUnsupported as e:
            log.warning("%s: %s; not waiting for subagents", job_name, e)
            return status
        except (HerdrCliError, OSError) as e:
            if first_poll:
                log.warning(
                    "%s: cannot read opencode background state (%s); not waiting",
                    job_name,
                    e,
                )
                return status
            read_errors += 1
            log.warning(
                "%s: cannot read opencode background state (%d/%d): %s",
                job_name,
                read_errors,
                _DRAIN_MAX_READ_ERRORS,
                e,
            )
            if read_errors >= _DRAIN_MAX_READ_ERRORS:
                return status
            busy = True
        else:
            read_errors = 0
        first_poll = False
        if not busy:
            quiet += 1
            if quiet >= _DRAIN_QUIET_POLLS:
                if announced:
                    log.info(
                        "%s: opencode background work finished after %.0fs",
                        job_name,
                        time.monotonic() - started,
                    )
                return current
        else:
            quiet = 0
            if not announced:
                announced = True
                log.info(
                    "%s: opencode session %s has background work; waiting up to %.0fs",
                    job_name,
                    session_id,
                    max(deadline - time.monotonic(), 0.0),
                )
        wait = OPENCODE_DRAIN_CONFIRM_S if quiet else OPENCODE_DRAIN_POLL_S
        if time.monotonic() + wait >= deadline:
            if not busy:
                return current  # one quiet poll in, no work seen: the settle stands
            log.warning(
                "%s: opencode background work still running at the deadline (%.0fs)",
                job_name,
                time.monotonic() - started,
            )
            return AGENT_STATUS_WORKING
        time.sleep(wait)
        # Only while work is pending: a settled agent's screen is not the wedge the marker
        # gate looks for, and the closure's `previous_hit` carries over from the main wait.
        if scan is not None and busy:
            try:
                screen = client.agent_read_visible(target)
            except (HerdrCliError, OSError):
                screen = ""
            marker = scan(screen)
            if marker is not None:
                raise PromptWatchdogKilled(
                    f"failure marker matched during subagent wait: {marker!r}",
                    marker=marker,
                    screen_text=screen,
                )


def prompt_with_watchdog(
    client: HerdrClient,
    *,
    job_name: str,
    target: str,
    text: str,
    timeout_ms: int,
    markers: Sequence[str],
    prompt_text: str,
    agent_kind: str,
    on_poll_hook: Callable[[], None] | None = None,
    retry_delays_s: Sequence[float] = PROMPT_RETRY_DELAYS_S,
) -> AgentStatus | None:
    """agent_prompt_wait_with_watchdog with bounded retries over provably-early
    session-not-ready failures — the same whitelist phase 1's retry enforced (see
    is_retryable_prompt_error): settle timeouts, wrapper subprocess timeouts and shape
    errors raise immediately, because delivery is proven or likely and a resend would
    double-prompt the agent. While each attempt waits, the visible screen is polled every
    WATCHDOG_POLL_INTERVAL_S and scanned via matched_failure_marker; only the SAME marker
    on two consecutive polls (stability gate against transient screen tear / partial
    renders) confirms the wedge and kills the delivered child. A watchdog kill is terminal
    and never retried — one delivery, one terminal record — so it propagates immediately as
    PromptWatchdogKilled for the caller's fast-fail classification. Poll reads that fail are
    inert (the callback sees "", which matches nothing). Raises the last error if every
    attempt fails. Returns the settled agent_status, or None when herdr reports a value
    outside its schema. For an opencode agent a settle is only final once its background
    subagents are done (`drain_opencode_background`, issue 094), within `timeout_ms` of the
    call. `target` is the agent name; `job_name` only labels log lines.

    `on_poll_hook` (a no-arg callback) fires inside the same `scan` closure that scans
    for markers, once per poll. `pipeline_run` uses it to write its liveness heartbeat —
    the watchdog reads that file, so it must advance on every poll of every stage.
    """
    previous_hit: str | None = None
    deadline = time.monotonic() + timeout_ms / 1000

    def scan(screen_text: str) -> str | None:
        nonlocal previous_hit
        if on_poll_hook is not None:
            on_poll_hook()
        marker = matched_failure_marker(screen_text, markers, prompt_text)
        if marker is None:
            previous_hit = None
            return None
        if previous_hit == marker:
            # second consecutive sighting of the same marker — stable, kill
            return marker
        previous_hit = marker
        return None

    delays = (None, *retry_delays_s)
    for i, delay in enumerate(delays):
        if delay is not None:
            time.sleep(delay)
            log.info(
                "%s: retrying prompt (attempt %d/%d)", job_name, i + 1, len(delays)
            )
        try:
            status = client.agent_prompt_wait_with_watchdog(
                target=target,
                text=text,
                timeout_ms=timeout_ms,
                poll_interval_s=WATCHDOG_POLL_INTERVAL_S,
                on_poll=scan,
            )
        except PromptWatchdogKilled:
            # Terminal by construction (no error_body → never retryable anyway); re-raised
            # explicitly so the double-prompt audit stays a one-line proof.
            raise
        except HerdrCliError as e:
            if not is_retryable_prompt_error(e) or i == len(delays) - 1:
                raise
        else:
            # Outside the try: nothing the drain raises may reach the resend whitelist.
            return drain_opencode_background(
                client,
                job_name=job_name,
                target=target,
                agent_kind=agent_kind,
                status=status,
                deadline=deadline,
                scan=scan,
            )
    raise AssertionError(
        "unreachable"
    )  # for the type checker; loop always returns/raises
