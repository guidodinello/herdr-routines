"""Orchestrates one job run: creates the pane, starts the agent, sends the prompt, verifies the
result, and writes the terminal history record. See docs/plan-v1.md §6 for the report-file
contract and the post-run verification rationale.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from herdr_routines.config import Job
from herdr_routines.herdr import HerdrClient, HerdrCliError, build_agent_start_args

log = logging.getLogger(__name__)

# Settle states that count as success for a scheduled (never-focused) run. Both are included
# because "idle" is what was empirically observed on herdr 0.8.2 for a never-focused pane, and
# "done" is kept in case SKILL.md's seen/unseen distinction applies under some other condition
# not exercised by the step-5 probe. See docs/plan-v1.md.
SUCCESS_AGENT_STATUSES = frozenset({"idle", "done"})

# How often _wait_for_agent_ready re-checks `agent get` while waiting for the agent's TUI to
# accept typed input. Module-level so tests can zero it out.
READY_POLL_INTERVAL_S = 1.0

# Retries for the prompt send itself. `interactive_ready` only means the TUI is drawn — the
# agent's session backend can still reject the first prompt seconds later (server-side
# EmptyResponse, observed ~3s and ~10s after start on herdr 0.8.2/0.8.x). Only such provably-
# early server rejections are retried (see _is_retryable_prompt_error); everything else is
# terminal, because unknown-or-proven delivery plus a resend would double-prompt the agent and
# duplicate the run's side effects (branch/report written twice). Module-level so tests can
# adjust or zero it out.
PROMPT_RETRY_DELAYS_S = (5.0, 15.0)


def _error_body_code(e: HerdrCliError) -> str | None:
    """The parsed error body's error.code when it is a string, else None. Never raises: both
    callers run inside except blocks, where crashing on a malformed body (e.g. a flat
    {"error": "timeout"}) would mask the original failure."""
    body = e.error_body
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _is_settle_timeout(e: HerdrCliError) -> bool:
    """True when the prompt was delivered but the agent didn't settle within timeout_ms
    (herdr exits 1 with a JSON body, code "timeout"). Resending in that case would double-prompt.
    Any malformed body conservatively classifies as not-a-settle-timeout rather than raising —
    see _error_body_code."""
    return _error_body_code(e) == "timeout"


def _is_retryable_prompt_error(e: HerdrCliError) -> bool:
    """True only for provably-early server rejections: herdr exited 1 with a parsed JSON error
    body whose error.code is present and is not "timeout" (the session-not-ready EmptyResponse).
    Everything else raises immediately:
      - exit 124 (_subprocess_runner wrapper timeout): herdr ran past timeout_ms + grace, so
        the prompt was almost certainly delivered;
      - exit 0 shape errors (_extract_status): delivery AND settle already succeeded — only
        the response JSON was unexpected;
      - exit 1 without a parseable body or without an error.code: delivery state unknown.
    Resending in any terminal case risks duplicating the run's side effects."""
    if e.exit_code != 1 or not isinstance(e.error_body, dict):
        return False
    code = _error_body_code(e)
    return code is not None and code != "timeout"


def _prompt_with_retry(
    client: HerdrClient,
    *,
    job_name: str,
    target: str,
    text: str,
    timeout_ms: int,
) -> str:
    """agent_prompt_wait with bounded retries over provably-early session-not-ready failures
    (see _is_retryable_prompt_error). Any other error — settle timeouts, wrapper subprocess
    timeouts, shape errors — raises immediately, because delivery is proven or likely and a
    resend would double-prompt the agent. Raises the last error if every attempt fails.
    `target` is the agent name; `job_name` only labels log lines."""
    delays = (None, *PROMPT_RETRY_DELAYS_S)
    for i, delay in enumerate(delays):
        if delay is not None:
            time.sleep(delay)
            log.info(
                "%s: retrying prompt (attempt %d/%d)", job_name, i + 1, len(delays)
            )
        try:
            return client.agent_prompt_wait(
                target=target, text=text, timeout_ms=timeout_ms
            )
        except HerdrCliError as e:
            if not _is_retryable_prompt_error(e) or i == len(delays) - 1:
                raise
    raise AssertionError(
        "unreachable"
    )  # for the type checker; loop always returns/raises


def _wait_for_agent_ready(
    client: HerdrClient, target: str, *, timeout_s: float
) -> tuple[bool, str | None]:
    """Blocks until the agent reports interactive_ready, returning (True, None); once
    timeout_s elapses returns (False, last_error_text). `agent start` returns as soon as the
    process is detected, but the TUI needs another few seconds before typed input is delivered;
    prompting earlier makes the server-side agent.prompt fail (EmptyResponse), which
    _prompt_with_retry then retries via _is_retryable_prompt_error (terminal agent_prompt_failed
    only on exhaustion). Polling errors are swallowed and never escape this function — a
    transiently unreachable server (or a vanished `herdr` binary raising OSError) must not
    abort the wait nor break execute_run's never-raises contract; the last error's text
    accompanies the verdict so an eventual agent_not_interactive failure can be attributed to
    infrastructure rather than a slow agent."""
    deadline = time.monotonic() + timeout_s
    last_error: str | None = None
    while True:
        try:
            if client.agent_interactive_ready(target):
                return True, None
        except (HerdrCliError, OSError) as e:
            last_error = f"{type(e).__name__}: {e}"
        if time.monotonic() >= deadline:
            return False, last_error
        time.sleep(READY_POLL_INTERVAL_S)


def default_reports_dir() -> Path:
    import os

    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "reports"


def make_run_id(job_name: str, scheduled_for: datetime) -> str:
    return f"{job_name}-{scheduled_for.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def build_branch_name(job_name: str, run_id: str) -> str:
    # run_id already encodes the timestamp, so re-use it rather than duplicating a clock read.
    return f"auto/{job_name}-{run_id.rsplit('-', 1)[-1]}"


def substitute_prompt(
    prompt_template: str, *, report_path: Path, job_name: str, run_id: str
) -> str:
    return (
        prompt_template.replace("$ROUTINE_REPORT", str(report_path))
        .replace("$ROUTINE_JOB", job_name)
        .replace("$ROUTINE_RUN_ID", run_id)
    )


@dataclass(frozen=True, slots=True)
class RunOutcome:
    state: str  # "done" | "failed" | "interrupted_unknown" (see docs/plan-v1.md §4)
    run_id: str
    reason: str | None = None
    error: str | None = None
    agent_name: str | None = None
    pane_id: str | None = None
    branch: str | None = None
    final_agent_status: str | None = None
    report_written: bool = False
    report_bytes: int = 0
    report_path: str | None = None
    duration_seconds: float | None = None


def build_dry_run_argv(job: Job, *, run_id: str) -> list[list[str]]:
    """The `herdr` command sequence `run --dry-run` prints for pane creation and agent
    start/prompt, without executing anything. Kept in lockstep with `execute_run` below
    by sharing branch/prompt construction helpers. The pre-start reap probe (`agent list`
    + `pane close`) is intentionally omitted from dry-run output — it is a best-effort
    cleanup of our own previous settled pane and not part of the run's core argv."""
    report_path = default_reports_dir() / f"{run_id}.md"
    prompt = substitute_prompt(
        job.prompt, report_path=report_path, job_name=job.name, run_id=run_id
    )

    argv: list[list[str]] = []
    if job.workspace == "worktree":
        branch = build_branch_name(job.name, run_id)
        argv.append(
            [
                "herdr",
                "worktree",
                "create",
                "--cwd",
                str(job.repo),
                "--branch",
                branch,
                "--base",
                job.base,
                "--no-focus",
                "--label",
                job.name,
            ]
        )
    else:
        argv.append(
            [
                "herdr",
                "tab",
                "create",
                "--cwd",
                str(job.repo),
                "--no-focus",
                "--label",
                job.name,
            ]
        )

    argv.append(
        [
            "herdr",
            *build_agent_start_args(
                name=job.agent_name,
                kind=job.agent_kind,
                pane_id="<pane_id>",
                start_timeout_ms=job.start_timeout_ms,
                model=job.model,
            ),
        ]
    )
    argv.append(
        [
            "herdr",
            "agent",
            "prompt",
            job.agent_name,
            prompt,
            "--wait",
            "--timeout",
            str(job.timeout_ms),
        ]
    )
    return argv


def execute_run(job: Job, client: HerdrClient, *, run_id: str) -> RunOutcome:
    """Runs one job to completion (or failure) against a real or faked HerdrClient. Never
    raises — every failure mode is captured in the returned RunOutcome so `tick.py` can always
    write a terminal history record. (A `job.model` unsupported for `job.agent_kind` would raise
    `ValueError` from `agent_start`, but `config.py` already rejects that combination at load
    time, so it can't reach here.)"""
    started_at = datetime.now(UTC)
    report_path = default_reports_dir() / f"{run_id}.md"
    branch = (
        build_branch_name(job.name, run_id) if job.workspace == "worktree" else None
    )

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="report_dir_creation_failed",
            error=str(e),
            branch=branch,
        )

    prompt = substitute_prompt(
        job.prompt, report_path=report_path, job_name=job.name, run_id=run_id
    )

    # A recurring job reuses one agent name, and runs otherwise leave their workspace
    # behind for inspection (plan-v1 §6 and ROADMAP pane-retention notes defer automatic
    # branch/workspace GC). By tomorrow the settled pane from today would still make
    # agent start fail on the duplicate name, so we reap our own previous pane first:
    # only when its agent is settled to idle/done (never working/blocked/unknown), never
    # for workspace:root jobs (their tab lives in the shared ambient workspace), and
    # closing only the single pane (not the whole workspace) to preserve sibling tabs.
    if job.workspace != "root":
        try:
            stale_pane = client.settled_agent_pane(job.agent_name)
            if stale_pane is not None:
                client.pane_close(stale_pane)
                log.info(
                    "%s: closed stale pane %s from previous run",
                    job.name,
                    stale_pane,
                )
        except Exception as e:  # noqa: BLE001 — best-effort reap must never break execute_run's never-raises contract
            log.warning("%s: could not reap previous pane: %s", job.name, e)

    try:
        if job.workspace == "worktree":
            pane_id = client.worktree_create(
                cwd=str(job.repo), branch=branch or "", base=job.base, label=job.name
            )
        else:
            pane_id = client.tab_create(cwd=str(job.repo), label=job.name)
    except (HerdrCliError, OSError) as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="pane_creation_failed",
            error=str(e),
            branch=branch,
        )

    try:
        client.agent_start(
            name=job.agent_name,
            kind=job.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=job.model,
        )
    except (HerdrCliError, OSError, ValueError) as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="agent_start_failed",
            error=str(e),
            pane_id=pane_id,
            branch=branch,
        )

    # The prompt must not race the TUI's own startup (see _wait_for_agent_ready): reuse
    # start_timeout_ms as the readiness bound since both describe "how long agent startup
    # may take". All poll errors are handled inside the wait, so nothing escapes this call.
    ready, last_poll_error = _wait_for_agent_ready(
        client, job.agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        error = (
            f"agent {job.agent_name} did not report interactive_ready within "
            f"{job.start_timeout_ms}ms of start"
        )
        if last_poll_error:
            error = f"{error}; last poll error: {last_poll_error}"
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="agent_not_interactive",
            error=error,
            agent_name=job.agent_name,
            pane_id=pane_id,
            branch=branch,
        )

    try:
        settled_status = _prompt_with_retry(
            client,
            job_name=job.name,
            target=job.agent_name,
            text=prompt,
            timeout_ms=job.timeout_ms,
        )
    except (HerdrCliError, OSError) as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="agent_prompt_failed",
            error=str(e),
            agent_name=job.agent_name,
            pane_id=pane_id,
            branch=branch,
        )

    # Best-effort diagnostic tail — never allowed to fail the run (docs/plan-v1.md §6 layer 2).
    try:
        tail = client.agent_read(job.agent_name, lines=200)
        if tail:
            (report_path.parent / f"{run_id}.tail.txt").write_text(tail)
    except OSError:
        pass

    report_written = report_path.exists()
    report_bytes = report_path.stat().st_size if report_written else 0

    common = {
        "run_id": run_id,
        "agent_name": job.agent_name,
        "pane_id": pane_id,
        "branch": branch,
        "final_agent_status": settled_status,
        "report_written": report_written,
        "report_bytes": report_bytes,
        "report_path": str(report_path) if report_written else None,
        "duration_seconds": (datetime.now(UTC) - started_at).total_seconds(),
    }

    if settled_status == "blocked":
        return RunOutcome(state="failed", reason="blocked", **common)

    if settled_status == "unknown":
        # An unresolvable settle status maps to the same terminal state as a crashed/killed
        # tick (docs/plan-v1.md §4's state machine), not a plain "failed" — stale-run recovery
        # already treats interrupted_unknown as the "we don't know what happened" bucket.
        return RunOutcome(
            state="interrupted_unknown", reason="unsettled_status_unknown", **common
        )

    if settled_status not in SUCCESS_AGENT_STATUSES:
        # "working" here means agent_prompt_wait's --wait settled on something that isn't a
        # completion signal — treat as interrupted/unclear rather than success.
        return RunOutcome(
            state="failed", reason=f"unsettled_status_{settled_status}", **common
        )

    if not report_written or report_bytes == 0:
        # Direct response to the research repo's standing pattern that unattended scheduled
        # runs fail silently and plausibly (docs/plan-v1.md §6): a clean settle with no report,
        # or an empty one, is not "done".
        return RunOutcome(state="failed", reason="no_report", **common)

    return RunOutcome(state="done", **common)
