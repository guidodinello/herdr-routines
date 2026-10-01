"""The one shared "prompt under a failure-marker watchdog, with bounded retries" wait
loop (issue 056, spec §4).

`runner.py` and `pipeline_run.py` both need exactly this primitive, and it already
existed twice: once in `runner._prompt_with_watchdog`, once as the bash
marker-poll loop inside `scripts/pipeline-launch.sh`. This module is the surviving
copy, lifted out of `runner.py` unchanged so the routine-job path's behaviour is
byte-identical — the only addition is `on_poll_hook`, which is what
`pipeline_watchdog.is_stalled` reads: a heartbeat line written from inside the very
closure that scans the screen for failure markers, so it advances on every poll by
construction rather than by a model remembering to.

The `on_poll_hook` name (rather than another `on_poll`) is deliberate: `on_poll` is
already `HerdrClient.agent_prompt_wait_with_watchdog`'s screen-text-in/marker-out
contract and a zero-arg callback cannot be composed with it. `scan` below wraps both:
it fires the hook, then does the marker scan, on every poll.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from herdr_routines.herdr import HerdrClient, HerdrCliError, PromptWatchdogKilled

log = logging.getLogger(__name__)

# How often the mid-run watchdog polls the visible screen while the prompt child
# waits. Mirrors herdr.py's PROMPT_WATCHDOG_POLL_S default; module-level so tests can
# adjust it, same style as runner.READY_POLL_INTERVAL_S.
WATCHDOG_POLL_INTERVAL_S = 30.0

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


def prompt_with_watchdog(
    client: HerdrClient,
    *,
    job_name: str,
    target: str,
    text: str,
    timeout_ms: int,
    markers: Sequence[str],
    prompt_text: str,
    on_poll_hook: Callable[[], None] | None = None,
    retry_delays_s: Sequence[float] = PROMPT_RETRY_DELAYS_S,
) -> str:
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
    attempt fails. `target` is the agent name; `job_name` only labels log lines.

    `on_poll_hook` (a no-arg callback) fires inside the same `scan` closure that scans
    for markers, once per poll. `pipeline_run` uses it to write its liveness heartbeat —
    the watchdog reads that file, so it must advance on every poll of every stage.
    """
    previous_hit: str | None = None

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
            return client.agent_prompt_wait_with_watchdog(
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
    raise AssertionError(
        "unreachable"
    )  # for the type checker; loop always returns/raises
