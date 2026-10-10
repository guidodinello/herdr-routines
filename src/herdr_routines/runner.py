"""Orchestrates one job run: creates the pane, starts the agent, sends the prompt, verifies the
result, and writes the terminal history record. See docs/plan-v1.md §6 for the report-file
contract and the post-run verification rationale.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TypedDict

from herdr_routines.config import Job
from herdr_routines.herdr import (
    HerdrClient,
    HerdrCliError,
    PromptWatchdogKilled,
    build_agent_start_args,
)
from herdr_routines.repos import ensure_repo
from herdr_routines.signing import ResignError, resign_unsigned_branch
from herdr_routines.wait_loop import (
    PROMPT_RETRY_DELAYS_S,
    WATCHDOG_POLL_INTERVAL_S,
    prompt_with_watchdog,
)
from herdr_routines.wait_loop import (
    error_body_code as _error_body_code,
)
from herdr_routines.wait_loop import (
    is_retryable_prompt_error as _is_retryable_prompt_error,
)
from herdr_routines.wait_loop import (
    is_settle_timeout as _is_settle_timeout,
)
from herdr_routines.wait_loop import (
    matched_failure_marker as _matched_failure_marker,
)

__all__ = [
    "PROMPT_RETRY_DELAYS_S",
    "WATCHDOG_POLL_INTERVAL_S",
    "_error_body_code",
    "_is_retryable_prompt_error",
    "_is_settle_timeout",
]

log = logging.getLogger(__name__)

# Settle states that count as success for a scheduled (never-focused) run. Both are included
# because "idle" is what was empirically observed on herdr 0.8.2 for a never-focused pane, and
# "done" is kept in case SKILL.md's seen/unseen distinction applies under some other condition
# not exercised by the step-5 probe. See docs/plan-v1.md.
SUCCESS_AGENT_STATUSES = frozenset({"idle", "done"})

# How often _wait_for_agent_ready re-checks `agent get` while waiting for the agent's TUI to
# accept typed input. Module-level so tests can zero it out.
READY_POLL_INTERVAL_S = 1.0

# Retries for the prompt send itself, and how often the mid-run watchdog polls the visible
# screen, now live in `wait_loop` (issue 056 §4 — one copy, shared with `pipeline_run`).
# They are re-exported here under their original names because they are module-level test
# seams for the routine-job path, which is what every existing test monkeypatches.

# Screen markers scanned once after a failed prompt wait (docs/failure-reaping.md §3.2). The
# first observed wedge cause: OpenCode's free-tier limit renders a "Free usage exceeded" modal
# and retries forever instead of settling. A job's `failure_markers` config overrides this
# tuple wholesale (config.py); markers appearing verbatim in the job's own prompt are skipped —
# the visible screen contains the prompt echo, so scanning would self-match.
DEFAULT_FAILURE_MARKERS: tuple[str, ...] = ("Free usage exceeded",)

# Bound for the one-shot no_report nudge (issue 032): a settled idle/done agent that wrote no
# (or an empty) $ROUTINE_REPORT is nudged once, on the same still-open agent, to write it. This
# is a small top-up prompt, not another full run — job.timeout_ms is already spent by the time
# this fires — so it gets its own short, fixed budget. Module-level so tests can adjust it.
NUDGE_TIMEOUT_MS = 120_000


def diagnose_tmp() -> dict[str, str | bool]:
    """Best-effort /tmp disk diagnosis for agent start failures (issue 027).
    Returns dict with df_tmp, du_tmp, tmp_full keys. Never raises — the df call is
    bounded by a 5s timeout and the size tally is a bounded filesystem walk."""
    diagnosis: dict[str, str | bool] = {"tmp_full": False}
    try:
        proc = subprocess.run(
            ["df", "-h", "/tmp"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        diagnosis["df_tmp"] = proc.stdout.strip()
        # Parse Use% from df output (second line, 4th column is Use%)
        for line in proc.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 5:
                pct_str = parts[4].rstrip("%")
                try:
                    if int(pct_str) >= 95:
                        diagnosis["tmp_full"] = True
                except ValueError:
                    pass
    except (OSError, subprocess.TimeoutExpired):
        diagnosis["df_tmp"] = "(unavailable)"
    # Size breakdown of the known /tmp offenders. Done with an iterdir tally rather
    # than `du` in a subprocess: the glob patterns would never expand without a
    # shell, so `du` would just receive literal paths and return nothing.
    try:
        tmp = Path("/tmp")
        lines: list[str] = []
        for pattern in (".3cdc*", "pytest-of-*", "opencode"):
            for entry in sorted(tmp.glob(pattern)):
                lines.append(f"{_dir_size_h(entry)}\t{entry}")
        diagnosis["du_tmp"] = "\n".join(lines)
    except OSError:
        diagnosis["du_tmp"] = "(unavailable)"
    return diagnosis


def _dir_size_h(path: Path) -> str:
    """Total size of path (recursively, if a directory), formatted like `du -h`."""
    total = 0
    entries = (
        path.rglob("*") if path.is_dir() and not path.is_symlink() else iter((path,))
    )
    for p in entries:
        try:
            total += p.lstat().st_size
        except OSError:
            pass
    size = float(total)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def _prompt_with_watchdog(
    client: HerdrClient,
    *,
    job_name: str,
    target: str,
    text: str,
    timeout_ms: int,
    markers: tuple[str, ...],
    prompt_text: str,
) -> str:
    """Thin wrapper over `wait_loop.prompt_with_watchdog` (issue 056 §4: the loop moved
    out of this module so `pipeline_run` shares exactly one copy, and `runner`'s
    behaviour is unchanged). Retained as a named seam for the routine-job tests.

    `retry_delays_s=PROMPT_RETRY_DELAYS_S` reads this module's global at call time, so
    the long-standing `monkeypatch.setattr("herdr_routines.runner.PROMPT_RETRY_DELAYS_S", ...)`
    test seam keeps working unchanged — every one of those tests exists to collapse the
    backoff to zero, and silently ignoring the patch would make them assert nothing."""
    return prompt_with_watchdog(
        client,
        job_name=job_name,
        target=target,
        text=text,
        timeout_ms=timeout_ms,
        markers=markers,
        prompt_text=prompt_text,
        retry_delays_s=PROMPT_RETRY_DELAYS_S,
    )


def _wait_for_agent_ready(
    client: HerdrClient, target: str, *, timeout_s: float
) -> tuple[bool, str | None]:
    """Blocks until the agent reports interactive_ready, returning (True, None); once
    timeout_s elapses returns (False, last_error_text). `agent start` returns as soon as the
    process is detected, but the TUI needs another few seconds before typed input is delivered;
    prompting earlier makes the server-side agent.prompt fail (EmptyResponse), which
    _prompt_with_watchdog then retries via _is_retryable_prompt_error (terminal
    agent_prompt_failed only on exhaustion). Polling errors are swallowed and never escape
    this function — a transiently unreachable server (or a vanished `herdr` binary raising
    OSError) must not abort the wait nor break execute_run's never-raises contract; the last
    error's text accompanies the verdict so an eventual agent_not_interactive failure can be
    attributed to infrastructure rather than a slow agent."""
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


def _capture_visible_tail(
    client: HerdrClient, target: str, *, reports_dir: Path, run_id: str
) -> str:
    """Best-effort failure-path diagnostic: read the working agent's visible screen via
    agent_read_visible (--source visible; recent-unwrapped is rejected while unsettled), write
    it to {run_id}.tail.txt when non-empty, and return whatever was read so callers can scan
    it for failure markers without a second read. Never raises."""
    try:
        tail = client.agent_read_visible(target, lines=200)
    except OSError:
        return ""
    if tail:
        try:
            (reports_dir / f"{run_id}.tail.txt").write_text(tail)
        except OSError:
            pass
    return tail


def extract_prompt_excerpt(tail: str, *, max_chars: int = 300) -> str:
    """Best-effort extraction of a permission-prompt excerpt from an agent's visible
    screen tail for the blocked-notification body (issue 007). Agent-agnostic: scans
    for common permission-prompt keywords rather than agent-specific markers. Returns
    at most *max_chars* characters; returns "" when *tail* is empty or unparseable.
    Never fails — callers always get a valid string."""
    if not tail:
        return ""
    keywords = ("approve", "allow", "permission", "y/n", "[y/n]", "yes/no", "confirm")
    for line in reversed(tail.splitlines()):
        lower = line.strip().lower()
        if lower and any(kw in lower for kw in keywords):
            excerpt = line.strip()
            if len(excerpt) > max_chars:
                excerpt = excerpt[: max_chars - 3] + "..."
            return excerpt
    # No keyword match: return the last non-empty line (best-effort context).
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if stripped:
            if len(stripped) > max_chars:
                return stripped[: max_chars - 3] + "..."
            return stripped
    return ""


def _close_run_pane(client: HerdrClient, *, job_name: str, pane_id: str) -> None:
    """Best-effort close of THIS run's pane. The pane was created by this very execute_run
    call, so closing it is ours to do — on a post-start failure, leaving it behind would wedge
    every future tick on agent_name_live, because a never-settled agent stays "working" and the
    stale-run reap only touches settled agents (docs/failure-reaping.md §1/§3.1); on a
    successful or no-report settle, closing it eagerly (rather than deferring to the next run's
    stale-pane reap) is what pane-lifecycle v2 for routine jobs relies on — see
    _capture_session_id for how inspection still works without the pane staying open. Never
    raises — mirrors execute_run's contract."""
    try:
        client.pane_close(pane_id)
    except Exception as e:  # noqa: BLE001 — best-effort close must never break execute_run's never-raises contract
        log.warning("%s: could not close run pane %s: %s", job_name, pane_id, e)


def _capture_session_id(client: HerdrClient, target: str) -> str | None:
    """Best-effort: the agent's underlying session id, captured before _close_run_pane closes
    its pane. A human can later resume and inspect the conversation via
    `herdr agent start <name> --kind <kind> --pane <fresh_pane> -- <model_flag> <model> -s
    <session_id>` (same mechanism the pipeline orchestrator uses for its own pl-3->pl-6 reuse,
    docs/pipeline/design.md) — this is what makes "keep the pane open for inspection" no longer
    necessary. Never raises."""
    try:
        return client.agent_session_id(target)
    except Exception as e:  # noqa: BLE001 — best-effort capture must never break execute_run's never-raises contract
        log.warning("could not capture session id for %s: %s", target, e)
        return None


def _attempt_report_nudge(
    client: HerdrClient, *, target: str, report_path: Path
) -> None:
    """One bounded, non-looping follow-up prompt to the same still-open agent for a settled
    idle/done run that produced no (or an empty) $ROUTINE_REPORT — issue 032. Real Pi history
    showed this is usually a free-tier model that did the real work but dropped the prompt's
    final "write the summary" step, not a job misconfiguration; retrying the whole job (issue
    008) isn't safe here because it risks duplicate side effects (e.g. a second review
    comment), so this asks only for the missing report and never repeats the job's original
    (possibly side-effecting) instructions. Raises HerdrCliError/OSError on failure or timeout
    exactly like the initial prompt send — the caller never retries this call; any exception
    here falls straight through to the existing no_report failure path."""
    prompt = (
        f"You appear to have finished without writing a summary to {report_path}. "
        "Write one now describing what you did."
    )
    client.agent_prompt_wait(target=target, text=prompt, timeout_ms=NUDGE_TIMEOUT_MS)


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
    # Keep everything past "<job_name>-" (not just the trailing "-"-segment via rsplit):
    # a fallback retry's run_id is "<job_name>-fallback-<timestamp>", and truncating to the
    # last segment collapsed it to the same timestamp suffix as the primary attempt whenever
    # the primary and fallback shared the same second (near-certain in production, since the
    # timer fires on :00 boundaries — see test_fallback_retry_uses_a_distinct_branch_in_worktree_mode).
    # Keeping the full remainder makes the branch name injective in run_id instead of relying
    # on the clock to differ.
    suffix = run_id.removeprefix(f"{job_name}-")
    return f"auto/{job_name}-{suffix}"


def substitute_prompt(
    prompt_template: str,
    *,
    report_path: Path,
    job_name: str,
    run_id: str,
    findings_path: Path | None = None,
) -> str:
    resolved = (
        prompt_template.replace("$ROUTINE_REPORT", str(report_path))
        .replace("$ROUTINE_JOB", job_name)
        .replace("$ROUTINE_RUN_ID", run_id)
    )
    # $ROUTINE_FINDINGS only resolves for audit jobs (issue 057); the placeholder is left
    # verbatim elsewhere rather than replaced with an empty/None string, so a non-audit
    # prompt that happens to contain it fails loudly instead of silently pointing nowhere.
    if findings_path is not None:
        resolved = resolved.replace("$ROUTINE_FINDINGS", str(findings_path))
    return resolved


def _best_effort_tmp_diagnosis(
    reports_dir: Path, run_id: str
) -> dict[str, str | bool] | None:
    """Best-effort /tmp diagnosis appended to the tail file. Returns None on total failure
    so RunOutcome.diagnosis stays clean."""
    try:
        diagnosis = diagnose_tmp()
        # Append diagnosis to the tail file for post-mortem visibility.
        tail_path = reports_dir / f"{run_id}.tail.txt"
        try:
            reports_dir.mkdir(parents=True, exist_ok=True)
            with tail_path.open("a") as f:
                f.write("\n--- /tmp diagnosis ---\n")
                f.write(f"tmp_full: {diagnosis.get('tmp_full', False)}\n")
                if diagnosis.get("df_tmp"):
                    f.write(f"df -h /tmp:\n{diagnosis['df_tmp']}\n")
                if diagnosis.get("du_tmp"):
                    f.write(f"du summary:\n{diagnosis['du_tmp']}\n")
        except OSError:
            pass
        return diagnosis
    except Exception:  # noqa: BLE001
        return None


class _CommonOutcomeFields(TypedDict):
    """The fields `execute_run` fills in identically for every terminal outcome, so they can
    be splatted into RunOutcome without restating nine keyword arguments at six call sites.
    A plain dict widens to its join type (`float | int | str | None`) and mypy then rejects
    every `**common`; a TypedDict keeps each key's own type. Erased at runtime."""

    run_id: str
    agent_name: str | None
    pane_id: str | None
    branch: str | None
    final_agent_status: str | None
    report_written: bool
    report_bytes: int
    report_path: str | None
    duration_seconds: float | None
    reaped_stale_agent: bool


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
    session_id: str | None = None
    nudged: bool = False  # issue 032: a no_report settle got one follow-up prompt
    resigned_commits: int = (
        0  # unsigned commits on the pushed branch, re-signed after run
    )
    diagnosis: dict[str, str | bool] | None = None  # issue 027: /tmp disk diagnosis
    reaped_stale_agent: bool = (
        False  # issue 051: force-closed a prior run's blocked agent
    )
    visible_tail: str | None = (
        None  # issue 007: captured visible screen for blocked notification
    )


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


def _is_agent_name_taken(error: Exception) -> bool:
    """The `herdr agent start` failure that means a prior run's agent still holds this
    recurring name (issue 051). Matched on the server error body's `code` first, with a
    message-substring fallback for a body-less shape."""
    if not isinstance(error, HerdrCliError):
        return False
    if _error_body_code(error) == "agent_name_taken":
        return True
    return "is already used" in str(error)


def _start_agent_reaping_stale_collision(
    client: HerdrClient,
    job: Job,
    *,
    pane_id: str,
    session_id: str | None = None,
) -> bool:
    """Start `job.agent_name` on `pane_id`. If the start fails because a prior run's
    blocked/unknown agent still holds the name (issue 051 — nothing answers a cron job's
    blocked prompt, so it wedges every subsequent run), force-close that agent's pane and
    retry the start exactly once. Returns True iff a stale agent was reaped. Propagates
    the start error unchanged when the collision is not reapable (agent still working, or
    the retry also failed) — the caller's existing failure handling takes it from there.

    `session_id` is threaded through the same `partial` (issue 056 §8) so the reaped-retry
    resumes a conversation rather than starting cold; routine jobs never pass one, but the
    parameter has to live here or a caller that did would silently start a fresh session."""
    start = partial(
        client.agent_start,
        name=job.agent_name,
        kind=job.agent_kind,
        pane_id=pane_id,
        start_timeout_ms=job.start_timeout_ms,
        model=job.model,
        session_id=session_id,
    )
    try:
        start()
        return False
    except (HerdrCliError, OSError, ValueError) as first_error:
        if not _is_agent_name_taken(first_error):
            raise
        try:
            stale = client.sticky_agent_pane(job.agent_name)
        except (HerdrCliError, OSError):
            stale = None
        if stale is None:
            raise
        stale_pane, stale_status = stale
        log.warning(
            "%s: agent name %s is held by a %s agent (pane %s) from a prior run — "
            "force-closing it and retrying start once (issue 051)",
            job.name,
            job.agent_name,
            stale_status,
            stale_pane,
        )
        client.pane_close(stale_pane)
        start()  # a second collision here is a real failure — let it propagate
        return True


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

    # Ensure the repo checkout exists (clone-if-missing / fetch) before any worktree
    # or tab creation.
    try:
        ensure_repo(job)
    except (RuntimeError, OSError) as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="clone_failed"
            if not (job.repo / ".git").exists()
            else "repo_sync_failed",
            error=str(e),
            branch=branch,
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

    # A recurring job reuses one agent name, and every settled terminal path below now closes
    # its own pane eagerly (pane-lifecycle v2 for routine jobs — a human can resume-and-inspect
    # via the captured session_id instead of the pane needing to stay open, see
    # _capture_session_id). This pre-run check is now just a defensive fallback for panes that
    # outlived that eager close (a close that itself failed, or a pane from before this fix):
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
        reaped_stale_agent = _start_agent_reaping_stale_collision(
            client, job, pane_id=pane_id
        )
    except (HerdrCliError, OSError, ValueError) as e:
        # Our pane, dead run: leave nothing behind to wedge later ticks on agent_name_live
        # (docs/failure-reaping.md §3.1).
        _capture_visible_tail(
            client, job.agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        _close_run_pane(client, job_name=job.name, pane_id=pane_id)
        diagnosis = _best_effort_tmp_diagnosis(report_path.parent, run_id)
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="tmp_full"
            if diagnosis and diagnosis.get("tmp_full")
            else "agent_start_failed",
            error=str(e),
            pane_id=pane_id,
            branch=branch,
            diagnosis=diagnosis,
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
        _capture_visible_tail(
            client, job.agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        _close_run_pane(client, job_name=job.name, pane_id=pane_id)
        diagnosis = _best_effort_tmp_diagnosis(report_path.parent, run_id)
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="tmp_full"
            if diagnosis and diagnosis.get("tmp_full")
            else "agent_not_interactive",
            error=error,
            agent_name=job.agent_name,
            pane_id=pane_id,
            branch=branch,
            diagnosis=diagnosis,
            reaped_stale_agent=reaped_stale_agent,
        )

    # `is not None`, not truthiness: an explicit empty failure_markers list is valid config
    # meaning "scan nothing" — `or` would silently fall back to the defaults (PR #25 review).
    effective_markers = (
        job.failure_markers
        if job.failure_markers is not None
        else DEFAULT_FAILURE_MARKERS
    )

    try:
        settled_status = _prompt_with_watchdog(
            client,
            job_name=job.name,
            target=job.agent_name,
            text=prompt,
            timeout_ms=job.timeout_ms,
            markers=effective_markers,
            prompt_text=prompt,
        )
    except PromptWatchdogKilled as e:
        # Phase-2 fast-fail (failure-reaping §8 / the run's spec): the quota modal sat
        # through two consecutive visible-screen polls while the delivered prompt was
        # wedged. Persist the detection poll's own screen text as the tail — no second read
        # of a pane we're about to close — then reap immediately so the next tick's
        # settled_agent_pane / _live_agent_exists check sees nothing live, instead of this
        # job blocking its full timeout_ms and every later tick skipping it.
        try:
            if e.screen_text:
                (report_path.parent / f"{run_id}.tail.txt").write_text(e.screen_text)
        except OSError:
            pass
        _close_run_pane(client, job_name=job.name, pane_id=pane_id)
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="quota_exhausted",
            error=f"failure marker matched: {e.marker!r}",
            agent_name=job.agent_name,
            pane_id=pane_id,
            branch=branch,
            reaped_stale_agent=reaped_stale_agent,
        )
    except (HerdrCliError, OSError) as e:
        # The wedge case: the prompt was delivered but the agent never settled (e.g. an
        # OpenCode quota modal retry-looping forever). Capture what's on screen, classify
        # quota exhaustion from it, then close our pane — otherwise every future tick skips
        # this job forever (docs/failure-reaping.md §1).
        screen_tail = _capture_visible_tail(
            client, job.agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        marker = _matched_failure_marker(screen_tail, effective_markers, prompt)
        reason = "quota_exhausted" if marker else "agent_prompt_failed"
        error = f"failure marker matched: {marker!r}" if marker else str(e)
        _close_run_pane(client, job_name=job.name, pane_id=pane_id)
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason=reason,
            error=error,
            agent_name=job.agent_name,
            pane_id=pane_id,
            branch=branch,
            reaped_stale_agent=reaped_stale_agent,
        )

    report_written = report_path.exists()
    report_bytes = report_path.stat().st_size if report_written else 0

    common: _CommonOutcomeFields = {
        "run_id": run_id,
        "agent_name": job.agent_name,
        "pane_id": pane_id,
        "branch": branch,
        "final_agent_status": settled_status,
        "report_written": report_written,
        "report_bytes": report_bytes,
        "report_path": str(report_path) if report_written else None,
        "duration_seconds": (datetime.now(UTC) - started_at).total_seconds(),
        "reaped_stale_agent": reaped_stale_agent,
    }

    if settled_status == "blocked":
        # Leave the pane open (no _close_run_pane/_capture_session_id) so a human can
        # resume and see what it's stuck on — but still capture a diagnostic tail, since
        # the pane may not survive until then (manual close, host reboot). Uses
        # agent_read_visible (visible source) which succeeds while unsettled/blocked.
        tail = _capture_visible_tail(
            client, job.agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        return RunOutcome(state="failed", reason="blocked", visible_tail=tail, **common)

    if settled_status == "unknown":
        # An unresolvable settle status maps to the same terminal state as a crashed/killed
        # tick (docs/plan-v1.md §4's state machine), not a plain "failed" — stale-run recovery
        # already treats interrupted_unknown as the "we don't know what happened" bucket.
        return RunOutcome(
            state="interrupted_unknown", reason="unsettled_status_unknown", **common
        )

    if settled_status not in SUCCESS_AGENT_STATUSES:
        # "working" here means agent_prompt_wait's --wait settled on something that isn't a
        # completion signal — treat as interrupted/unclear rather than success, and close our
        # pane: herdr still classifies the agent as live, so leaving it behind wedges the job
        # exactly like the prompt-failed path (docs/failure-reaping.md §3.1). Capture a
        # diagnostic tail via visible source before close (issue 033).
        _capture_visible_tail(
            client, job.agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        _close_run_pane(client, job_name=job.name, pane_id=pane_id)
        return RunOutcome(
            state="failed", reason=f"unsettled_status_{settled_status}", **common
        )

    # Both remaining outcomes are a settled idle/done agent. Before closing our pane (issue
    # 032): a settle with no report, or an empty one, gets exactly one bounded follow-up
    # prompt to the same still-open agent asking it to write the summary it skipped — real Pi
    # history showed this is usually a free-tier model dropping the prompt's last step, not a
    # job failure. This must happen before _close_run_pane below (worktree mode) or the agent
    # would no longer be reachable; root-mode jobs never close their pane either way, so the
    # nudge applies identically there.
    nudged = False
    if not report_written or report_bytes == 0:
        nudged = True
        try:
            _attempt_report_nudge(
                client, target=job.agent_name, report_path=report_path
            )
        except (HerdrCliError, OSError) as e:
            # Never loop: any nudge failure/timeout falls straight through to the existing
            # no_report path below, exactly as if the nudge had never been attempted.
            log.warning("%s: no_report nudge failed: %s", job.name, e)
        report_written = report_path.exists()
        report_bytes = report_path.stat().st_size if report_written else 0
        common["report_written"] = report_written
        common["report_bytes"] = report_bytes
        common["report_path"] = str(report_path) if report_written else None
        # The nudge can itself take real wall-clock time (up to NUDGE_TIMEOUT_MS); reflect it
        # in the recorded duration rather than the pre-nudge snapshot taken above.
        common["duration_seconds"] = (datetime.now(UTC) - started_at).total_seconds()

    # Bounded visible-tail capture (issue 011): placed after the nudge so the tail
    # reflects the nudge's final screen if the nudge ran, matching spec v2 L33.
    _capture_visible_tail(
        client, job.agent_name, reports_dir=report_path.parent, run_id=run_id
    )

    # Pane-lifecycle v2: close our own pane now instead of leaving it for the next run's
    # stale-pane reap (root-mode jobs share the ambient workspace and are never auto-closed,
    # same guard as the pre-run reap above; skip capturing a session id too, since there's
    # nothing to resume-and-inspect for a pane that's never closed). Session id is captured
    # before closing since a closed pane's agent record won't answer `agent get` afterwards.
    session_id: str | None = None
    if job.workspace != "root":
        session_id = _capture_session_id(client, job.agent_name)
        _close_run_pane(client, job_name=job.name, pane_id=pane_id)

    # An agent can push unsigned commits however the clone is configured (`git -c
    # commit.gpgsign=false` overrides it — PR #140), and the rulesets then block the merge.
    # Re-sign in code after the agent is done; best-effort, never fails the run.
    resigned_commits = 0
    if branch is not None:
        try:
            resigned_commits = resign_unsigned_branch(
                job.repo, branch=branch, base=job.base
            )
        except (ResignError, OSError, subprocess.TimeoutExpired) as e:
            log.warning("%s: could not re-sign %s: %s", job.name, branch, e)

    if not report_written or report_bytes == 0:
        # Direct response to the research repo's standing pattern that unattended scheduled
        # runs fail silently and plausibly (docs/plan-v1.md §6): a clean settle with no report,
        # or an empty one, is not "done" — even after the one-shot nudge above.
        return RunOutcome(
            state="failed",
            reason="no_report",
            session_id=session_id,
            nudged=nudged,
            resigned_commits=resigned_commits,
            **common,
        )

    return RunOutcome(
        state="done",
        session_id=session_id,
        nudged=nudged,
        resigned_commits=resigned_commits,
        **common,
    )
