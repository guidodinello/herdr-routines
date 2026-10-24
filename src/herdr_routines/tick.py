"""The systemd entrypoint: acquire the tick lock, load config, decide which jobs are due, run
them, write history. This is what `herdr-routines tick` calls. See docs/plan-v1.md §3/§4.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never

from logger import get_logger

from herdr_routines import verify
from herdr_routines.auto_fix import (
    EligiblePR,
    GhClient,
    GithubEvent,
    PRInfo,
    RealGhClient,
    attempt_count_for_event,
    attempt_count_for_pr,
    attempt_count_for_target,
    build_audit_agent_name,
    build_audit_fix_prompt,
    build_audit_prompt,
    build_base_fix_prompt,
    build_event_fix_prompt,
    build_fix_prompt,
    build_gate_worker_agent_name,
    build_pr_agent_name,
    build_review_agent_name,
    build_review_agent_prefix,
    build_review_prompt,
    build_worker_agent_name,
    consecutive_base_gate_failures,
    effective_trusted_authors,
    enrich_event_detail,
    event_worktree_slug,
    fetch_failing_checks,
    fetch_fix_worker_threads,
    is_eligible,
    list_open_prs,
    poll_github_event,
    repo_owner_and_name,
    review_marker,
    review_target_hash,
    run_checks,
)
from herdr_routines.config import (
    DEFAULT_TRUSTED_REVIEW_AUTHORS,
    GateCheck,
    Job,
    JobInvariantError,
    RoutinesConfig,
    VerifyGate,
)
from herdr_routines.findings import (
    Finding,
    FindingDiff,
    FindingsManifest,
    Ledger,
    apply_diff,
    diff_findings,
    dispatch_pool,
    ledger_path,
    load_findings_manifest_full,
    load_ledger,
    mark_handed_off,
    prune_ledger,
    save_ledger,
)
from herdr_routines.herdr import (
    LIVE_AGENT_STATUSES,
    SETTLED_AGENT_STATUSES,
    AgentStatus,
    HerdrClient,
    HerdrCliError,
)
from herdr_routines.history import (
    TERMINAL_STATES,
    TRIGGER_MANUAL,
    HistoryExtra,
    HistoryRecord,
    HistoryState,
    VerifyRecord,
    append,
    find_stale_running,
    first_seen_at,
    has_ever_been_seen,
    is_currently_running,
    last_scheduled_terminal_run,
    maybe_rotate,
    read_job,
)
from herdr_routines.pipeline_watchdog import (
    PIPELINE_UNIT_PREFIX,
    default_heartbeat_dir,
    default_worktrees_root,
    find_inflight_runs,
    host_rebooted_after_state_write,
    is_stalled,
    pipeline_state_json_path,
    pipeline_unit_active,
    recorded_pipeline_deadlines,
    system_boot_epoch,
    validate_stage_sessions,
)
from herdr_routines.repos import _ensure_checkout, ensure_repo
from herdr_routines.review_scan import (
    REVIEW_FAILURE_DISPOSITIONS,
    Candidate,
    classify_review_candidates,
    render_review_report,
)
from herdr_routines.runner import (
    RunOutcome,
    build_branch_name,
    default_reports_dir,
    execute_run,
    extract_prompt_excerpt,
    make_run_id,
    substitute_prompt,
)
from herdr_routines.schedule import Decision, decide
from herdr_routines.target_selectors import (
    SelectorContext,
    SelectorFailed,
    select_target,
)
from herdr_routines.tmp_hygiene import reap_tmp
from herdr_routines.verify import VerifyContext, VerifyTarget

log = get_logger(__name__)


def default_lock_path() -> Path:
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "tick.lock"


@contextmanager
def tick_lock(path: Path) -> Generator[bool]:
    """Exclusive, non-blocking flock. Yields True if acquired, False if another tick already
    holds it — callers should exit quietly (rc 0) rather than treat that as an error, since it
    just means the previous tick is still running (docs/plan-v1.md §4)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """What one `run_tick` call did. `any_job_failed` is what `cli._cmd_tick` maps to a
    non-zero process exit — it is True only when a job this tick *itself* called `execute_run`
    for and got back a state other than "done" (`failed`, or `interrupted_unknown` from an
    unsettled agent status). It is never set for a routine scheduling outcome
    (`missed`/`skipped`/`not due`), so systemd only marks the unit "failed" for a genuine
    operational problem, not a job that simply wasn't due.

    Note the one case that also writes `interrupted_unknown` but does *not* set this flag: the
    stale-running recovery path in `_process_job` (a *previous* tick's crashed run, discovered
    by `find_stale_running`). That record describes a past tick's failure, not this one's own
    execution — this tick didn't run anything for it, so it has nothing of its own to report as
    failed. That past tick, whenever it ran, already exited non-zero on its own account (or was
    killed outright, which systemd/monitoring sees independently)."""

    summaries: tuple[str, ...]
    any_job_failed: bool


def _record_unexpected_failure(
    job: Job, history_path: Path, *, now: datetime, error: Exception
) -> str:
    """Give a job whose processing raised unexpectedly a terminal `failed` record.

    Closes the `running` records this tick wrote for the job (every `running` append in
    this module uses `ts=now`, which is what scopes this to the current tick), or writes a
    `run_id`-less `failed` when none was open so the occurrence is consumed rather than
    re-raising every tick. A pipeline job's open records are left alone: its detached unit
    may still be live and the pipeline watchdog owns finalising them. Best-effort: a
    history write failing here must not abort the tick either."""
    extra = {"reason": "unexpected_error", "error": repr(error)}
    try:
        records = read_job(history_path, job.name)
        terminal_run_ids = {
            r.run_id for r in records if r.state in TERMINAL_STATES and r.run_id
        }
        open_run_ids = [
            r.run_id
            for r in records
            if r.state == "running"
            and r.ts == now
            and r.run_id
            and r.run_id not in terminal_run_ids
        ]
        if job.kind == "pipeline" and open_run_ids:
            log.warning(
                "%s: leaving running record(s) %s open for the pipeline watchdog",
                job.name,
                open_run_ids,
            )
        else:
            to_close: list[str | None] = [*open_run_ids] or [None]
            for run_id in to_close:
                append(
                    history_path,
                    HistoryRecord(
                        ts=now,
                        job=job.name,
                        state="failed",
                        run_id=run_id,
                        extra=extra,
                    ),
                )
    except Exception as e:  # noqa: BLE001 — see docstring
        log.warning("%s: could not record unexpected failure: %s", job.name, e)
    return f"{job.name}: failed (unexpected_error)"


def run_tick(
    config: RoutinesConfig, history_path: Path, *, client: HerdrClient, now: datetime
) -> TickOutcome:
    """Process every enabled job once. Does not raise for individual job failures; each is
    captured in its own history record so one bad job cannot prevent the rest from being
    evaluated."""
    # /tmp hygiene preamble (issue 027): best-effort reap under tick.lock, never fails
    # the tick. Runs before job dispatch so leaked agent .so files and pytest artifacts
    # are cleaned before any new agent spawn. Configured from `defaults:` only.
    tmp_hygiene = config.tmp_hygiene
    if tmp_hygiene.enabled:
        try:
            reaped = reap_tmp(
                tmp_dir=Path(tmp_hygiene.tmp_dir), max_age_s=tmp_hygiene.max_age_s
            )
            log.info(
                "tmp hygiene: removed %d, skipped %d fresh, %d errors",
                reaped.removed,
                reaped.skipped_fresh,
                reaped.errors,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("tmp hygiene reap failed (continuing): %s", e)

    summaries: list[str] = []
    any_job_failed = False
    for job in config.jobs:
        if not job.enabled:
            continue
        try:
            summary, failed = _process_job(
                job,
                history_path,
                client=client,
                now=now,
                trusted_review_authors=config.trusted_review_authors,
            )
        except Exception as e:
            # One job's unexpected error must not skip the rest of the tick.
            log.exception("%s: unexpected error (continuing)", job.name)
            summary, failed = (
                _record_unexpected_failure(job, history_path, now=now, error=e),
                True,
            )
        summaries.append(summary)
        any_job_failed = any_job_failed or failed

    # History rotation (issue 021), after dispatch rather than before it: the records that
    # pushed the file past the threshold are the ones this tick just wrote, and rotating
    # first would always be a tick behind. `maybe_rotate` is a rename and never a delete, so
    # it is safe to run unattended here; it carries the same best-effort contract as the reap
    # above, because a state-dir housekeeping failure must not be the reason systemd marks
    # the unit failed and a job never dispatches. Deleting anything is `prune`'s job.
    try:
        maybe_rotate(
            history_path,
            max_bytes=config.retention.history_max_bytes,
            max_age_days=config.retention.history_max_age_days,
            now=now,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("history rotation failed (continuing): %s", e)

    return TickOutcome(summaries=tuple(summaries), any_job_failed=any_job_failed)


def _process_audit_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    """Process a kind: audit job (issues 057, 058).

    The schedule guards are a gated job's; when the cron fires, `_audit_cycle` runs the
    audit, diffs its findings against the ledger, and dispatches at most one fix worker.
    One run spans the audit and the fix worker, so staleness is judged against both."""
    audit = job.require_audit()
    run_timeout_ms = audit.timeout_ms + job.timeout_ms

    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=run_timeout_ms, now=now
    )
    if stale is not None:
        stale_extra = HistoryExtra(reason="stale_running_record")
        reaped = _reap_agents_of_rebooted_run(
            client, job, stale, boot_epoch=system_boot_epoch()
        )
        if reaped:
            stale_extra["reaped_agents"] = reaped
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="interrupted_unknown",
                run_id=stale.run_id,
                extra=stale_extra,
            ),
        )

    if is_currently_running(history_path, job.name, timeout_ms=run_timeout_ms, now=now):
        return f"{job.name}: skipped (already running)", False

    if _live_audit_agent_exists(client, job):
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="skipped",
                extra=HistoryExtra(reason="agent_name_live"),
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_scheduled_terminal_run(history_path, job.name)
    registered_at = first_seen_at(history_path, job.name) or now
    result = decide(
        cron=job.cron,
        timezone=job.timezone,
        catch_up_minutes=job.catch_up_minutes,
        now=now,
        last_terminal=last,
        job_registered_at=registered_at,
    )

    if result.decision == Decision.NOT_DUE:
        return f"{job.name}: not due", False

    if result.decision == Decision.MISSED:
        extra = HistoryExtra(
            reason="outside_catch_up_window", occurrence=result.occurrence.isoformat()
        )
        if result.skipped_occurrences:
            extra["skipped_occurrences"] = result.skipped_occurrences
        append(
            history_path,
            HistoryRecord(ts=now, job=job.name, state="missed", extra=extra),
        )
        if job.on_missed == "notify":
            _notify(
                client,
                f"herdr-routines: {job.name} missed",
                body="outside catch-up window",
                sound="request",
            )
        return f"{job.name}: missed", False

    run_id = make_run_id(job.name, result.occurrence)

    if result.skipped_occurrences:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra=HistoryExtra(
                    reason="collapsed_earlier_occurrences",
                    skipped_occurrences=result.skipped_occurrences,
                    skipped_first=result.skipped_first.isoformat()
                    if result.skipped_first
                    else None,
                    skipped_last=result.skipped_last.isoformat()
                    if result.skipped_last
                    else None,
                ),
            ),
        )

    return dispatch_occurrence(
        job,
        history_path,
        Occurrence(
            run_id,
            HistoryExtra(
                scheduled_for=result.occurrence.isoformat(),
                late_seconds=result.late_seconds,
            ),
        ),
        client=client,
        now=now,
    )


# Per-run cap on how many suppressed IDs the history record carries. Suppression is
# terminal (never re-dispatched) so the count matters more than the list, but a bounded
# sample keeps one bad night from bloating every history line.
_AUDIT_SUPPRESSED_ID_CAP = 20

# gate -> notification kind (see _notify_gate's four-tier policy).
_AUDIT_GATE_NOTIFY_KIND = {
    "baseline": "finding",
    "fix_dispatched": "finding",
    "suppressed": "finding",
    "passed": "success",
    "failed": "failure",
}


def _render_audit_report(
    *,
    job_name: str,
    run_id: str,
    check: str,
    gate: str,
    manifest: FindingsManifest,
    diff: FindingDiff,
    dispatched_ids: list[str],
    suppressed_ids: list[str],
) -> str:
    dispatched = set(dispatched_ids)
    suppressed = set(suppressed_ids)
    new_ids = {f.id for f in diff.new}
    regressed_ids = {f.id for f in diff.regressed}
    unchanged_ids = {f.id for f in diff.unchanged}

    lines = [
        f"# Audit report: {job_name}",
        "",
        f"- run: {run_id}",
        f"- check: {check}",
        f"- gate: {gate}",
        (
            f"- findings: {len(manifest.findings)} total — "
            f"{len(diff.new)} new, {len(diff.regressed)} regressed, "
            f"{len(diff.unchanged)} unchanged, {len(diff.resolved)} resolved"
        ),
        f"- dispatched: {len(dispatched_ids)}",
        f"- suppressed: {len(suppressed_ids)}",
    ]
    if manifest.duplicate_ids:
        lines.append(f"- duplicate_ids: {', '.join(manifest.duplicate_ids)}")
    lines += ["", "## Findings", ""]
    if not manifest.findings:
        lines.append("_No findings._")
    for finding in manifest.findings:
        if finding.id in new_ids:
            disposition = "new"
        elif finding.id in regressed_ids:
            disposition = "regressed"
        elif finding.id in unchanged_ids:
            disposition = "unchanged"
        else:
            disposition = "unknown"
        flags = []
        if finding.id in dispatched:
            flags.append("dispatched")
        if finding.id in suppressed:
            flags.append("suppressed")
        flag_text = f" ({', '.join(flags)})" if flags else ""
        lines.append(
            f"- [{disposition}{flag_text}] id=`{finding.id}` "
            f"severity={finding.severity} kind={finding.kind} "
            f"location={finding.location} — {finding.summary}"
        )
    if diff.resolved:
        lines += ["", "## Resolved", ""]
        for fid in diff.resolved:
            lines.append(f"- id=`{fid}`")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _AuditPaths:
    """Where one audit cycle's files live. The audit and the fix worker each get their own
    Markdown report; `report` is the engine's aggregate (issue 058, "Three report paths")."""

    manifest: Path
    report: Path
    audit_report: Path
    audit_instructions: Path
    fix_report: Path
    ledger: Path

    @classmethod
    def for_run(cls, job_name: str, run_id: str) -> _AuditPaths:
        reports = default_reports_dir()
        return cls(
            manifest=reports / f"{run_id}-findings.json",
            report=reports / f"{run_id}.md",
            audit_report=reports / f"{run_id}-audit.md",
            audit_instructions=reports / f"{run_id}-audit-instructions.md",
            fix_report=reports / f"{run_id}-fix.md",
            ledger=ledger_path(job_name),
        )


@dataclass(frozen=True, slots=True)
class _AgentRun:
    """How one agent dispatched into a worktree ended. `reason` is set iff it failed."""

    reason: str | None = None
    error: str | None = None
    pane_id: str | None = None
    final_agent_status: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkerFailed:
    """A fix worker that never got an agent to settle: why, and the pane it had already
    opened, if any."""

    reason: str
    error: str | None
    pane_id: str | None = None
    # Set for `worktree_reuse_not_clean` (issue 070): the dirty checkout the refusal names.
    worktree_path: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkerSettled:
    """A fix worker whose agent settled. `state` is `failed` when the settled status is not
    one of SETTLED_AGENT_STATUSES; `report_path` is set iff the agent wrote its report."""

    state: Literal["done", "failed"]
    pane_id: str
    report_path: str | None
    final_agent_status: AgentStatus | None
    session_id: str | None
    report_bytes: int = 0
    # The worktree the worker ran in (issue 070), so the settle site can check it.
    worktree_path: str | None = None


_WorkerResult = _WorkerFailed | _WorkerSettled


def _record_audit_failure(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    paths: _AuditPaths,
    reason: str,
    extra: HistoryExtra | None = None,
) -> tuple[str, bool]:
    if _notify_gate(job, _AUDIT_GATE_NOTIFY_KIND["failed"]):
        _notify(client, f"herdr-routines: {job.name} failed", body=reason)
    failure_extra = HistoryExtra(
        gate="failed",
        target=job.target or "base",
        reason=reason,
        manifest_path=str(paths.manifest),
        ledger_path=str(paths.ledger),
        report_path=str(paths.report),
        report_written=False,
    )
    if extra:
        failure_extra.update(extra)
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="failed",
            run_id=run_id,
            extra=failure_extra,
        ),
    )
    return f"{job.name}: failed ({reason})", True


def _add_worktree(repo: Path, wt_path: Path, base: str) -> None:
    """Force-remove then add a detached worktree at `base` — the sequence
    `_process_base_target` runs. Raises RuntimeError when `git worktree add` fails."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt_path), base],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")


def _run_agent_in_worktree(
    job: Job,
    client: HerdrClient,
    *,
    agent_name: str,
    wt_path: Path,
    prompt_text: str,
    timeout_ms: int,
    tail_id: str,
) -> _AgentRun:
    """Start `agent_name` in a pane at `wt_path`, prompt it and wait for it to settle,
    then close the pane. The same start / ready / watchdog / tail / close path
    `_process_base_target` uses. Never raises; the caller owns the worktree."""
    from herdr_routines.runner import (
        _capture_visible_tail,
        _close_run_pane,
        _prompt_with_watchdog,
        _wait_for_agent_ready,
    )

    reports_dir = default_reports_dir()
    pane_id: str | None = None
    try:
        pane_id = client.tab_create(cwd=str(wt_path), label=agent_name)
        client.agent_start(
            name=agent_name,
            kind=job.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=job.model,
        )
    except (HerdrCliError, OSError) as e:
        if pane_id is not None:
            _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _AgentRun(reason="agent_start_failed", error=str(e), pane_id=pane_id)

    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(
            client, agent_name, reports_dir=reports_dir, run_id=tail_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _AgentRun(
            reason="agent_not_interactive", error=last_error, pane_id=pane_id
        )

    try:
        settled_status = _prompt_with_watchdog(
            client,
            job_name=agent_name,
            target=agent_name,
            text=prompt_text,
            timeout_ms=timeout_ms,
            markers=job.failure_markers
            if job.failure_markers is not None
            else ("Free usage exceeded",),
            prompt_text=prompt_text,
        )
    except Exception as e:  # noqa: BLE001
        _capture_visible_tail(
            client, agent_name, reports_dir=reports_dir, run_id=tail_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _AgentRun(reason="agent_prompt_failed", error=str(e), pane_id=pane_id)

    _capture_visible_tail(client, agent_name, reports_dir=reports_dir, run_id=tail_id)
    session_id: str | None = None
    try:
        session_id = client.agent_session_id(agent_name)
    except Exception as e:  # noqa: BLE001 — session id is best-effort reporting data
        log.debug("could not read session id for %s: %s", agent_name, e)
    _close_run_pane(client, job_name=agent_name, pane_id=pane_id)

    return _AgentRun(
        reason=None
        if settled_status in SETTLED_AGENT_STATUSES
        else "agent_prompt_failed",
        pane_id=pane_id,
        final_agent_status=settled_status,
        session_id=session_id,
    )


def _audit_cycle(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime, run_id: str
) -> tuple[str, bool]:
    """One cron fire of a kind: audit job: run the audit (phase 1), diff its manifest
    against the ledger (phase 2), then dispatch at most one fix worker (phase 3)."""
    paths = _AuditPaths.for_run(job.name, run_id)
    failure = _run_audit(
        job, history_path, client=client, now=now, run_id=run_id, paths=paths
    )
    if failure is not None:
        return failure
    return _diff_and_fix(
        job, history_path, client=client, now=now, run_id=run_id, paths=paths
    )


def _run_audit(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    paths: _AuditPaths,
) -> tuple[str, bool] | None:
    """Phase 1: run the audit in its own `audit-<run_id>` worktree at `base`, which is
    removed before this returns. Returns a failure result, or None when the audit ran —
    whether it produced a valid manifest is phase 2's question.

    `audit.command`'s exit code is not the gate: non-zero is the healthy case for an
    audit, so only 127 (the binary is missing) is a failure."""
    audit = job.require_audit()

    def _fail(reason: str, extra: HistoryExtra) -> tuple[str, bool]:
        return _record_audit_failure(
            job,
            history_path,
            client=client,
            now=now,
            run_id=run_id,
            paths=paths,
            reason=reason,
            extra=extra,
        )

    try:
        ensure_repo(job)
    except (RuntimeError, OSError) as e:
        reason = (
            "clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed"
        )
        return _fail(reason, HistoryExtra(error=str(e)))

    try:
        paths.report.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail("report_dir_creation_failed", HistoryExtra(error=str(e)))

    wt_path = Path(job.repo) / ".worktrees" / f"audit-{run_id}"
    try:
        _add_worktree(job.repo, wt_path, job.base)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return _fail("worktree_creation_failed", HistoryExtra(error=str(e)))

    try:
        if audit.command is not None:
            command = substitute_prompt(
                audit.command,
                report_path=paths.audit_report,
                job_name=job.name,
                run_id=run_id,
                findings_path=paths.manifest,
            )
            env = {
                **os.environ,
                "ROUTINE_FINDINGS": str(paths.manifest),
                "ROUTINE_REPORT": str(paths.audit_report),
            }
            outcome = run_checks(
                (
                    GateCheck(
                        kind="command", command=command, timeout_ms=audit.timeout_ms
                    ),
                ),
                cwd=str(wt_path),
                env=env,
            )
            output_path = paths.report.parent / f"{run_id}-audit-output.txt"
            try:
                output_path.write_text(outcome.combined_output)
            except OSError as e:
                log.warning("%s: could not write audit output: %s", job.name, e)
            exit_code = outcome.results[0].exit_code
            if exit_code == 127:
                return _fail(
                    "audit_command_failed",
                    HistoryExtra(
                        exit_code=exit_code, audit_output_path=str(output_path)
                    ),
                )
            return None

        if audit.skill is None:
            raise JobInvariantError(
                f"job {job.name!r}: audit requires exactly one of 'skill'/'command'"
            )
        agent_name = build_audit_agent_name(job.name, run_id)
        # A non-empty `prompt` is the audit itself, inline (for a repo without the skill);
        # the output contract around it is always the engine's.
        prompt_text = build_audit_prompt(
            skill=audit.skill,
            base=job.base,
            report_path=str(paths.audit_report),
            findings_path=str(paths.manifest),
            instructions=job.prompt,
        )
        prompt_text = substitute_prompt(
            prompt_text,
            report_path=paths.audit_report,
            job_name=job.name,
            run_id=run_id,
            findings_path=paths.manifest,
        )
        # Inline instructions are kept for the fix worker, which re-scans from them. Only
        # the instructions: the engine's wrapper tells the audit agent not to commit,
        # which a fix worker must not read as addressed to it.
        if job.prompt is not None and job.prompt.strip():
            try:
                paths.audit_instructions.write_text(
                    substitute_prompt(
                        job.prompt,
                        report_path=paths.audit_report,
                        job_name=job.name,
                        run_id=run_id,
                        findings_path=paths.manifest,
                    )
                )
            except OSError as e:
                log.warning("%s: could not save audit instructions: %s", job.name, e)
        run = _run_agent_in_worktree(
            job,
            client,
            agent_name=agent_name,
            wt_path=wt_path,
            prompt_text=prompt_text,
            timeout_ms=audit.timeout_ms,
            tail_id=f"{run_id}-audit",
        )
        if run.reason is not None:
            return _fail(
                run.reason,
                HistoryExtra(
                    phase="audit",
                    agent_name=agent_name,
                    error=run.error,
                    pane_id=run.pane_id,
                    final_agent_status=run.final_agent_status,
                ),
            )
        return None
    finally:
        _cleanup_worktree(job.repo, wt_path)


def _dispatch_audit_fix(
    job: Job,
    *,
    client: HerdrClient,
    run_id: str,
    check: str,
    findings: list[Finding],
    paths: _AuditPaths,
) -> _AgentRun:
    """Phase 3: exactly one fix worker, in its own `audit-fix-<run_id>` worktree at
    `base`, told to push `auto/<job>-<run_id>` and open a PR. The worktree is removed
    after the worker settles; the branch it pushed survives."""
    audit = job.require_audit()
    wt_path = Path(job.repo) / ".worktrees" / f"audit-fix-{run_id}"
    try:
        _add_worktree(job.repo, wt_path, job.base)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return _AgentRun(reason="worktree_creation_failed", error=str(e))

    try:
        if audit.command is not None:
            recheck = f"`{audit.command}` (it writes the findings manifest)"
        elif job.prompt is not None and job.prompt.strip():
            recheck = (
                f"follow the `{audit.skill}` audit instructions in "
                f"{paths.audit_instructions} to re-scan. Do not overwrite the "
                f"audit's report or manifest"
            )
        else:
            recheck = f"run the `{audit.skill}` skill and read its findings"
        prompt_text = job.fix_prompt or build_audit_fix_prompt(
            job_name=job.name,
            check=check,
            base=job.base,
            branch=build_branch_name(job.name, run_id),
            report_path=str(paths.fix_report),
            findings=findings,
            recheck=recheck,
        )
        prompt_text = substitute_prompt(
            prompt_text,
            report_path=paths.fix_report,
            job_name=job.name,
            run_id=run_id,
            findings_path=paths.manifest,
        )
        return _run_agent_in_worktree(
            job,
            client,
            agent_name=build_gate_worker_agent_name(job.name, run_id),
            wt_path=wt_path,
            prompt_text=prompt_text,
            timeout_ms=job.timeout_ms,
            tail_id=f"{run_id}-fix",
        )
    finally:
        _cleanup_worktree(job.repo, wt_path)


def _diff_and_fix(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    paths: _AuditPaths,
) -> tuple[str, bool]:
    """Phases 2 and 3: parse the manifest, diff it against the ledger, write the ledger
    and the aggregate report, then dispatch one fix worker for the capped set.

    Fail-closed on an unverifiable manifest or a corrupt ledger: record `failed`,
    notify, act on nothing. The ledger is written *before* the dispatch (write-ahead),
    so a dispatch that fails has still consumed its attempt."""
    job.require_audit()  # fail before the ledger is written, not in the dispatch

    def _fail(reason: str) -> tuple[str, bool]:
        return _record_audit_failure(
            job,
            history_path,
            client=client,
            now=now,
            run_id=run_id,
            paths=paths,
            reason=reason,
        )

    manifest = load_findings_manifest_full(paths.manifest)
    if manifest is None:
        return _fail("findings_manifest_invalid")

    ledger: Ledger | None
    if paths.ledger.exists():
        ledger = load_ledger(paths.ledger)
        if ledger is None:
            # Never silently re-baseline: preserve the corrupt file for inspection so the
            # regression signal is not thrown away, then fail closed.
            backup = paths.ledger.parent / (
                f"{paths.ledger.name}.corrupt-{now.strftime('%Y%m%dT%H%M%SZ')}"
            )
            try:
                shutil.copyfile(paths.ledger, backup)
            except OSError as e:
                log.warning("could not back up corrupt ledger %s: %s", paths.ledger, e)
            return _fail("ledger_corrupt")
    else:
        ledger = None

    check = manifest.check
    diff = diff_findings(manifest.findings, ledger, check=check)
    old_entries = ledger.entries if ledger is not None else {}

    cold = ledger is None
    dispatched_ids: list[str] = []
    suppressed_ids: list[str] = []
    adopted = 0

    if cold and job.adopt_baseline:
        # Adopt the current report as the baseline and dispatch nothing. An empty report
        # is a clean pass, not a baseline (nothing to adopt).
        adopted = len(manifest.findings)
        gate = "baseline" if manifest.findings else "passed"
    else:
        # Suppression is checked against *every* current finding, not just the actionable
        # ones: an unchanged finding already at its budget stays suppressed (and reported)
        # rather than quietly re-entering the dispatch pool.
        suppressed_ids = [
            f.id
            for f in manifest.findings
            if (entry := old_entries.get(f.id)) is not None
            and entry.state == "open"
            and entry.attempts >= job.max_attempts_per_target
        ]
        suppressed_set = set(suppressed_ids)
        candidates = [
            f.id for f in dispatch_pool(diff, ledger) if f.id not in suppressed_set
        ]
        dispatched_ids = candidates[: job.max_findings_per_dispatch]
        if dispatched_ids:
            gate = "fix_dispatched"
        elif suppressed_ids:
            gate = "suppressed"
        else:
            gate = "passed"

    base_ledger = ledger if ledger is not None else Ledger(job=job.name, entries={})
    # A baseline adoption records debt without queueing it.
    queued_ids = (
        [] if gate == "baseline" else [f.id for f in (*diff.new, *diff.regressed)]
    )
    updated = apply_diff(
        base_ledger,
        diff,
        now=now,
        run_id=run_id,
        queued_ids=queued_ids,
        dispatched_ids=dispatched_ids,
    )
    updated = prune_ledger(updated, now=now, retention_days=job.ledger_retention_days)
    save_ledger(paths.ledger, updated)

    paths.report.parent.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(
        _render_audit_report(
            job_name=job.name,
            run_id=run_id,
            check=check,
            gate=gate,
            manifest=manifest,
            diff=diff,
            dispatched_ids=dispatched_ids,
            suppressed_ids=suppressed_ids,
        )
    )

    extra = HistoryExtra(
        gate=gate,
        target=job.target or "base",
        check=check,
        manifest_path=str(paths.manifest),
        ledger_path=str(paths.ledger),
        report_path=str(paths.report),
        report_written=True,
        duplicate_ids=list(manifest.duplicate_ids),
        findings_total=len(manifest.findings),
        new=len(diff.new),
        regressed=len(diff.regressed),
        unchanged=len(diff.unchanged),
        resolved=len(diff.resolved),
        dispatched=len(dispatched_ids),
        suppressed=len(suppressed_ids),
        adopted=adopted,
        dispatched_ids=list(dispatched_ids),
        suppressed_ids=list(suppressed_ids[:_AUDIT_SUPPRESSED_ID_CAP]),
    )

    if not dispatched_ids:
        if gate == "baseline":
            body = f"baseline adopted ({adopted} findings)"
        elif gate == "suppressed":
            body = f"{len(suppressed_ids)} suppressed at budget"
        else:
            body = "clean"
        if _notify_gate(job, _AUDIT_GATE_NOTIFY_KIND[gate]):
            _notify(client, f"herdr-routines: {job.name} {gate}", body=body)
        append(
            history_path,
            HistoryRecord(
                ts=now, job=job.name, state="done", run_id=run_id, extra=extra
            ),
        )
        return f"{job.name}: done ({gate})", False

    by_id = {f.id: f for f in manifest.findings}
    run = _dispatch_audit_fix(
        job,
        client=client,
        run_id=run_id,
        check=check,
        findings=[by_id[fid] for fid in dispatched_ids],
        paths=paths,
    )
    fix_report_written = paths.fix_report.exists()
    gate_branch = build_branch_name(job.name, run_id)
    extra.update(
        {
            "gate_branch": gate_branch,
            "branch": gate_branch,
            "agent_name": build_gate_worker_agent_name(job.name, run_id),
            "pane_id": run.pane_id,
            "final_agent_status": run.final_agent_status,
            "session_id": run.session_id,
            "fix_report_path": str(paths.fix_report) if fix_report_written else None,
            "fix_report_written": fix_report_written,
        }
    )

    if run.reason is not None:
        extra.update({"reason": run.reason, "error": run.error})
        if _notify_gate(job, _AUDIT_GATE_NOTIFY_KIND["failed"]):
            _notify(client, f"herdr-routines: {job.name} failed", body=run.reason)
        append(
            history_path,
            HistoryRecord(
                ts=now, job=job.name, state="failed", run_id=run_id, extra=extra
            ),
        )
        return f"{job.name}: failed ({run.reason})", True

    # The worker settled: its findings are handed off, so an unmerged fix PR is not
    # dispatched again next cycle. A failed dispatch (above) keeps them queued for a retry.
    save_ledger(paths.ledger, mark_handed_off(updated, dispatched_ids))
    if _notify_gate(job, _AUDIT_GATE_NOTIFY_KIND[gate]):
        _notify(
            client,
            f"herdr-routines: {job.name} {gate}",
            body=f"fix worker ran for {len(dispatched_ids)} findings",
        )
    append(
        history_path,
        HistoryRecord(ts=now, job=job.name, state="done", run_id=run_id, extra=extra),
    )
    return f"{job.name}: done ({gate})", False


def _live_audit_agent_exists(client: HerdrClient, job: Job) -> bool:
    """An audit job's cross-tick double-dispatch guard: a live audit agent
    (`rt-<job>-au<h>`) or fix worker (`rt-<job>-gate-<h>`). Matched by prefix, because the
    32-char cap truncates a 24-char job's fix-worker name to `rt-<job>-gate` with no hash
    at all. A prefix can also match another job named `<job>-au…`; for a guard that only
    costs a skipped tick, which is why this is not used for reaping. Fails open on a
    HerdrCliError, like `_live_agent_exists`."""
    prefixes = (
        f"rt-{job.name}-au"[:32].lower(),
        f"rt-{job.name}-gate"[:32].lower(),
    )
    try:
        statuses = client.agent_statuses()
    except HerdrCliError as e:
        log.warning("%s: could not query live agents, proceeding: %s", job.name, e)
        return False
    return any(
        name.lower().startswith(prefixes) and status in LIVE_AGENT_STATUSES
        for name, status in statuses.items()
    )


def _process_gated_job(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    """Process a gated job: run checks, dispatch fix agent on failure.
    Handles both pr and base targets."""
    # Standard guards (same as _process_job for regular jobs).
    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=job.timeout_ms, now=now
    )
    if stale is not None:
        stale_extra = HistoryExtra(reason="stale_running_record")
        reaped = _reap_agents_of_rebooted_run(
            client, job, stale, boot_epoch=system_boot_epoch()
        )
        if reaped:
            stale_extra["reaped_agents"] = reaped
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="interrupted_unknown",
                run_id=stale.run_id,
                extra=stale_extra,
            ),
        )

    if is_currently_running(history_path, job.name, timeout_ms=job.timeout_ms, now=now):
        return f"{job.name}: skipped (already running)", False

    if _live_agent_exists(client, job):
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="skipped",
                extra=HistoryExtra(reason="agent_name_live"),
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_scheduled_terminal_run(history_path, job.name)
    registered_at = first_seen_at(history_path, job.name) or now
    result = decide(
        cron=job.cron,
        timezone=job.timezone,
        catch_up_minutes=job.catch_up_minutes,
        now=now,
        last_terminal=last,
        job_registered_at=registered_at,
    )

    if result.decision == Decision.NOT_DUE:
        return f"{job.name}: not due", False

    if result.decision == Decision.MISSED:
        extra = HistoryExtra(
            reason="outside_catch_up_window", occurrence=result.occurrence.isoformat()
        )
        if result.skipped_occurrences:
            extra["skipped_occurrences"] = result.skipped_occurrences
        append(
            history_path,
            HistoryRecord(ts=now, job=job.name, state="missed", extra=extra),
        )
        if job.on_missed == "notify":
            _notify(
                client,
                f"herdr-routines: {job.name} missed",
                body="outside catch-up window",
                sound="request",
            )
        return f"{job.name}: missed", False

    # Decision.RUN
    run_id = make_run_id(job.name, result.occurrence)

    if result.skipped_occurrences:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra=HistoryExtra(
                    reason="collapsed_earlier_occurrences",
                    skipped_occurrences=result.skipped_occurrences,
                    skipped_first=result.skipped_first.isoformat()
                    if result.skipped_first
                    else None,
                    skipped_last=result.skipped_last.isoformat()
                    if result.skipped_last
                    else None,
                ),
            ),
        )

    return dispatch_occurrence(
        job,
        history_path,
        Occurrence(
            run_id,
            HistoryExtra(
                scheduled_for=result.occurrence.isoformat(),
                late_seconds=result.late_seconds,
            ),
        ),
        client=client,
        now=now,
        trusted_review_authors=trusted_review_authors,
    )


def _last_seen_event_ids(history_path: Path, job_name: str) -> list[str] | None:
    """The event IDs the most recent poll already handled: the `last_seen_event_ids`
    of the latest terminal record that carries one (a failed poll carries none and
    leaves the watermark where it was). None when no poll has ever succeeded, which
    is not the same as an empty watermark.

    The latest watermark, not a union over all history: an event that clears and
    comes back (CI red again on the same PR, a label removed and re-added) is absent
    from the poll in between, so it drops out of the watermark and fires again."""
    for r in reversed(read_job(history_path, job_name)):
        ids = r.extra.get("last_seen_event_ids") if r.extra else None
        if isinstance(ids, list):
            return [e for e in ids if isinstance(e, str)]
    return None


def _event_target(check: GateCheck) -> str:
    """The event's own kind for the history `target` field: an `issues:opened` poll
    fires on issues, every other query fires on PRs. Kept distinct from `job.target`
    (where a fix lands) so an issue event is never recorded as a PR."""
    return "issue" if check.query == "issues:opened" else "pr"


def _resolve_event_owner_repo(job: Job, check: GateCheck) -> tuple[str, str]:
    """owner/repo for the event poll: the check's explicit `repo:` when set, else the
    job's managed-clone `repository:` URL, else the job repo's `origin` remote. Raises
    on a missing/unparseable source so the caller can fail closed rather than silently
    poll the wrong repo."""
    if check.repo:
        owner, repo_name = check.repo.split("/", 1)
        return owner, repo_name
    if job.repository:
        return repo_owner_and_name(job.repository)
    proc = subprocess.run(
        ["git", "-C", str(job.repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "cannot resolve owner/repo for github_event check: set the check's "
            "'repo:', the job's 'repository:', or an 'origin' git remote "
            f"(git remote failed: {proc.stderr.strip()})"
        )
    return repo_owner_and_name(proc.stdout.strip())


def _process_event_target(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    check: GateCheck,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    """Event-triggered gate (issue 014): poll `gh` for matching events, diff the IDs
    against `last_seen_event_ids`, and dispatch one worker per new event (capped by
    `max_workers_per_tick`). No new event → clean pass, no agent, no worktree; a
    poll failure fails closed as a gate error (never a silent green).

    ``trusted_review_authors`` scopes an `issues:opened` poll without `match.author`
    to issues from trusted authors (`poll_github_event`)."""
    gh = RealGhClient()
    event_target = _event_target(check)

    try:
        ensure_repo(job)
    except (RuntimeError, OSError) as e:
        reason = (
            "clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed"
        )
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed", reason=reason, error=str(e), target=event_target
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=reason,
                sound="request",
            )
        return f"{job.name}: failed ({reason})", True

    try:
        owner, repo_name = _resolve_event_owner_repo(job, check)
    except Exception as e:  # noqa: BLE001
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="repo_detection_failed",
                    error=str(e),
                    target=event_target,
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="repo_detection_failed",
                sound="request",
            )
        return f"{job.name}: failed (repo_detection_failed)", True

    # Fail closed: an auth/rate-limit/transient `gh` failure is a gate ERROR, never
    # "no events" — the same rule commit_check_runs follows (unverifiable != green).
    try:
        events = poll_github_event(
            gh,
            owner=owner,
            repo=repo_name,
            check=check,
            trusted_review_authors=trusted_review_authors,
        )
    except Exception as e:  # noqa: BLE001
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="github_event_poll_failed",
                    error=str(e),
                    target=event_target,
                    query=check.query,
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="github_event_poll_failed",
                sound="request",
            )
        return f"{job.name}: failed (github_event_poll_failed)", True

    last_seen = _last_seen_event_ids(history_path, job.name)
    if last_seen is None and check.query == "issues:opened":
        # First successful poll: every open issue would look new and get a fix worker.
        # Adopt them as the baseline instead, so only issues opened from now on fire.
        # prs:labels and ci:failed keep firing on the first poll; a labelled PR or a
        # red CI that already exists is usually exactly what the job is for.
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="done",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="baseline",
                    target=event_target,
                    query=check.query,
                    polled=len(events),
                    last_seen_event_ids=[e.event_id for e in events],
                ),
            ),
        )
        if _notify_gate(job, "finding" if events else "success"):
            _notify(
                client,
                f"herdr-routines: {job.name} baseline",
                body=f"baseline adopted ({len(events)} open issues)",
            )
        return f"{job.name}: done (baseline, {len(events)} open issues adopted)", False

    seen = set(last_seen or [])
    new_events = [e for e in events if e.event_id not in seen]
    # The watermark is the polled events that need nothing more: already seen, or
    # handled this tick (worker done, or budget exhausted). An over-cap event or one
    # whose worker failed stays out of it, so the next tick retries it until the
    # per-event budget taps out.
    handled: set[str] = set()

    def _watermark() -> list[str]:
        return [e.event_id for e in events if e.event_id in seen | handled]

    if not new_events:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="done",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="passed",
                    target=event_target,
                    query=check.query,
                    polled=len(events),
                    last_seen_event_ids=_watermark(),
                ),
            ),
        )
        if _notify_gate(job, "success"):
            _notify(client, f"herdr-routines: {job.name} done", sound="done")
        return f"{job.name}: done (no new events)", False

    dispatchable = new_events[: job.max_workers_per_tick]
    skipped_over_cap = len(new_events) - len(dispatchable)

    dispatched_count = 0
    skipped_count = 0
    any_failed = False
    for event in dispatchable:
        attempt = attempt_count_for_event(history_path, job.name, event.event_id)
        if attempt >= job.max_attempts_per_target:
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    run_id=run_id,
                    extra=HistoryExtra(
                        reason="max_attempts_exceeded",
                        event_id=event.event_id,
                        event_number=event.number,
                        attempt=attempt,
                        target=event_target,
                    ),
                ),
            )
            if _notify_gate(job, "failure"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} skipped",
                    body=f"max_attempts_exceeded for {event.event_id}",
                    sound="request",
                )
            skipped_count += 1
            handled.add(event.event_id)
            continue

        detailed = enrich_event_detail(gh, event, owner=owner, repo=repo_name)
        # The head ref is GitHub data that becomes a `git worktree add` argument and a
        # worktree lookup key, so it must be a well-formed branch name first. Checked
        # after enrichment, which may replace it. A malformed name stays malformed, so
        # the event is skipped and marked seen rather than retried every tick; a check
        # that could not run is a failed attempt, retried within the event's budget.
        ref_ok = _is_valid_branch_name(detailed.head_ref) if detailed.head_ref else True
        if ref_ok is None:
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="failed",
                    run_id=run_id,
                    extra={
                        "reason": "head_ref_check_failed",
                        "event_id": event.event_id,
                        "event_number": event.number,
                        "attempt": attempt,
                        "target": event_target,
                    },
                ),
            )
            any_failed = True
            continue
        if not ref_ok:
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    run_id=run_id,
                    extra={
                        "reason": "invalid_head_ref",
                        "event_id": event.event_id,
                        "event_number": event.number,
                        "head_ref": detailed.head_ref,
                        "target": event_target,
                    },
                ),
            )
            if _notify_gate(job, "failure"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} skipped",
                    body=f"invalid_head_ref for {event.event_id}",
                    sound="request",
                )
            skipped_count += 1
            handled.add(event.event_id)
            continue

        agent_name = build_worker_agent_name(job.name, event.number, run_id)
        worker_outcome = _dispatch_event_worker(
            job=job,
            event=detailed,
            run_id=run_id,
            owner=owner,
            repo=repo_name,
            client=client,
        )

        # Same refusal rule as the pr path (issue 070): no agent ran, so it is a `skipped`
        # that spends no attempt and leaves the event unseen to be re-checked next poll.
        if (
            isinstance(worker_outcome, _WorkerFailed)
            and worker_outcome.reason == "worktree_reuse_not_clean"
            and worker_outcome.worktree_path is not None
        ):
            wt = worker_outcome.worktree_path
            first_detection = not _dirty_worktree_already_reported(
                history_path, job.name, wt
            )
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    run_id=run_id,
                    extra=HistoryExtra(
                        reason=worker_outcome.reason,
                        error=worker_outcome.error,
                        event_id=event.event_id,
                        event_number=event.number,
                        attempt=attempt,
                        target=event_target,
                        worktree_path=wt,
                    ),
                ),
            )
            if first_detection and _notify_gate(job, "failure"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} event {event.event_id} blocked",
                    body=f"uncommitted changes in {wt}; rescue or discard by hand",
                    sound="request",
                )
            skipped_count += 1
            continue

        outcome_state: Literal["done", "failed"] = "failed"
        outcome_error: str | None = None
        outcome_report_path: str | None = None
        outcome_agent_status: AgentStatus | None = None
        if isinstance(worker_outcome, _WorkerSettled):
            outcome_state = worker_outcome.state
            outcome_report_path = worker_outcome.report_path
            outcome_agent_status = worker_outcome.final_agent_status
        else:
            outcome_error = worker_outcome.error

        extra_record = HistoryExtra(
            event_id=event.event_id,
            event_number=event.number,
            attempt=attempt,
            reason="gate_failed",
            fix_worker_agent=agent_name,
            pane_id=worker_outcome.pane_id,
            report_path=outcome_report_path,
            report_written=outcome_report_path is not None,
            final_agent_status=outcome_agent_status,
            target=event_target,
        )
        if outcome_error is not None:
            extra_record["error"] = outcome_error

        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state=outcome_state,
                run_id=run_id,
                extra=extra_record,
            ),
        )

        if outcome_state == "failed":
            any_failed = True
        else:
            handled.add(event.event_id)
        dispatched_count += 1

    state: HistoryState = "failed" if any_failed else "done"
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state=state,
            run_id=run_id,
            extra=HistoryExtra(
                gate="failed" if any_failed else "passed",
                target=event_target,
                query=check.query,
                polled=len(events),
                new=len(new_events),
                dispatched=dispatched_count,
                skipped=skipped_count,
                skipped_over_cap=skipped_over_cap,
                last_seen_event_ids=_watermark(),
            ),
        ),
    )

    summary = (
        f"{job.name}: {state} "
        f"(polled={len(events)}, new={len(new_events)}, "
        f"dispatched={dispatched_count}, skipped={skipped_count})"
    )
    if any_failed:
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=f"{dispatched_count} dispatched, {skipped_count} skipped",
                sound="request",
            )
        return summary, True

    if _notify_gate(job, "finding" if dispatched_count > 0 else "success"):
        _notify(client, f"herdr-routines: {job.name} done", sound="done")
    return summary, False


def _dispatch_event_worker(
    *,
    job: Job,
    event: GithubEvent,
    run_id: str,
    owner: str,
    repo: str,
    client: HerdrClient,
) -> _WorkerResult:
    """Dispatch one fix worker for a new GitHub event. Mirrors `_dispatch_fix_worker`
    but seeds `build_event_fix_prompt` from the event payload instead of PR CI/threads.
    The worker checks out the event's head branch when the event carries one (a PR
    event), else a detached base checkout."""
    from herdr_routines.runner import (
        _capture_visible_tail,
        _close_run_pane,
        _prompt_with_watchdog,
        _wait_for_agent_ready,
        default_reports_dir,
        substitute_prompt,
    )

    agent_name = build_worker_agent_name(job.name, event.number, run_id)
    event_run_id = f"{run_id}-evt{event.number}"
    report_path = default_reports_dir() / f"auto-event-{run_id}-{event.number}.md"

    try:
        ensure_repo(job)
    except (RuntimeError, OSError) as e:
        reason = (
            "clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed"
        )
        return _WorkerFailed(reason=reason, error=str(e))

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _WorkerFailed(reason="report_dir_creation_failed", error=str(e))

    prompt_text = job.prompt or build_event_fix_prompt(
        job_name=job.name,
        event=event,
        owner_repo=f"{owner}/{repo}",
        report_path=str(report_path),
        base=job.base,
    )
    prompt_text = substitute_prompt(
        prompt_text, report_path=report_path, job_name=job.name, run_id=event_run_id
    )

    # A PR event's head branch may already be checked out elsewhere (the
    # orchestrator retains its worktree, or babysit's fix worker holds it); git
    # refuses a second `worktree add` on it, so reuse that checkout instead, exactly
    # like _dispatch_fix_worker (issue 036).
    existing_wt = (
        _find_existing_worktree(job.repo, event.head_ref) if event.head_ref else None
    )
    if existing_wt is not None:
        ok, detail = _worktree_reuse_check(existing_wt)
        if not ok:
            return _WorkerFailed(
                reason="worktree_reuse_not_clean",
                error=f"existing worktree {existing_wt} for {event.head_ref}: {detail}",
                worktree_path=str(existing_wt),
            )
        wt_path = existing_wt
    else:
        wt_path = (
            Path(job.repo)
            / ".worktrees"
            / f"event-{event_worktree_slug(event.event_id)}"
        )
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(job.repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(wt_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            target_ref = event.head_ref or job.base
            if event.head_ref:
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(job.repo),
                        "worktree",
                        "add",
                        "--",
                        str(wt_path),
                        target_ref,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            else:
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(job.repo),
                        "worktree",
                        "add",
                        "--detach",
                        "--",
                        str(wt_path),
                        target_ref,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            if proc.returncode != 0:
                raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
        except Exception as e:  # noqa: BLE001
            return _WorkerFailed(reason="worktree_creation_failed", error=str(e))

    pane_id: str | None = None
    try:
        pane_id = client.tab_create(cwd=str(wt_path), label=agent_name)
        client.agent_start(
            name=agent_name,
            kind=job.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=job.model,
        )
    except (HerdrCliError, OSError) as e:
        if pane_id is not None:
            try:
                client.pane_close(pane_id)
            except Exception as e2:  # noqa: BLE001
                log.debug("error closing pane %s after start failure: %s", pane_id, e2)
        return _WorkerFailed(reason="agent_start_failed", error=str(e), pane_id=pane_id)

    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=event_run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _WorkerFailed(
            reason="agent_not_interactive", error=last_error, pane_id=pane_id
        )

    try:
        settled_status = _prompt_with_watchdog(
            client,
            job_name=agent_name,
            target=agent_name,
            text=prompt_text,
            timeout_ms=job.timeout_ms,
            markers=job.failure_markers or ("Free usage exceeded",),
            prompt_text=prompt_text,
        )
    except Exception as e:  # noqa: BLE001
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=event_run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _WorkerFailed(
            reason="agent_prompt_failed", error=str(e), pane_id=pane_id
        )

    _capture_visible_tail(
        client, agent_name, reports_dir=report_path.parent, run_id=event_run_id
    )

    report_written = report_path.exists()
    session_id: str | None = None
    try:
        session_id = client.agent_session_id(agent_name)
    except Exception as e:  # noqa: BLE001
        log.debug("could not read session id for %s: %s", agent_name, e)

    _close_run_pane(client, job_name=agent_name, pane_id=pane_id)

    return _WorkerSettled(
        state="done" if settled_status in SETTLED_AGENT_STATUSES else "failed",
        pane_id=pane_id,
        report_path=str(report_path) if report_written else None,
        final_agent_status=settled_status,
        session_id=session_id,
    )


def _process_pr_target(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    """PR-target gate: enumerate eligible PRs, dispatch fix workers per flagged PR.

    Only PRs and review threads from trusted authors (``trusted_review_authors`` plus
    the authenticated gh user, `effective_trusted_authors`) are considered, so no other
    account's text can make a PR eligible or reach a fix worker's prompt."""
    gh = RealGhClient()

    # Ensure repo checkout before any git remote / worktree operations
    try:
        ensure_repo(job)
    except (RuntimeError, OSError) as e:
        reason = (
            "clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed"
        )
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(reason=reason, error=str(e)),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=reason,
                sound="request",
            )
        return f"{job.name}: failed ({reason})", True

    try:
        proc = subprocess.run(
            ["git", "-C", str(job.repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git remote failed: {proc.stderr.strip()}")
        owner, repo_name = repo_owner_and_name(proc.stdout.strip())
    except Exception as e:  # noqa: BLE001
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(reason="repo_detection_failed", error=str(e)),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="repo_detection_failed",
                sound="request",
            )
        return f"{job.name}: failed (repo_detection_failed)", True

    try:
        author = gh.api_user()
    except Exception as e:  # noqa: BLE001
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(reason="gh_auth_missing", error=str(e)),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="gh_auth_missing",
                sound="request",
            )
        return f"{job.name}: failed (gh_auth_missing)", True

    trusted = effective_trusted_authors(trusted_review_authors, viewer_login=author)
    open_prs = list_open_prs(
        gh,
        owner=owner,
        repo=repo_name,
        branch_prefix="auto/",
        author=author,
        trusted_authors=trusted,
    )

    eligible: list[EligiblePR] = []
    for pr_info in open_prs:
        elig = is_eligible(
            gh, owner=owner, repo=repo_name, pr=pr_info, trusted_authors=trusted
        )
        if elig is not None:
            eligible.append(elig)

    eligible.sort(key=lambda e: e.pr.number)
    dispatched = eligible[: job.max_workers_per_tick]
    skipped_over_cap = len(eligible) - len(dispatched)

    any_failed = False
    dispatched_count = 0
    skipped_count = skipped_over_cap
    # Skips that are neither the cap nor the attempt budget (issue 070): a PR whose head
    # could not be read, or whose worktree a human has to clean.
    skipped_other = 0
    # (PR number, worktree path, reason) of every dirty worktree left for a human.
    dirty_worktrees: list[tuple[int, str, str]] = []
    for elig_pr in dispatched:
        attempt = attempt_count_for_pr(history_path, job.name, elig_pr.pr.number)
        if attempt >= job.max_attempts_per_target:
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    extra=HistoryExtra(
                        reason="max_attempts_exceeded",
                        pr_number=elig_pr.pr.number,
                        attempt=attempt,
                    ),
                ),
            )
            # Per-PR mid-loop skip, not the job's own once-per-tick terminal notify (that
            # follows below, at the aggregate done/failed notify) — "progress" kind, only
            # surfaced under notify_policy: always.
            if _notify_gate(job, "progress"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} PR #{elig_pr.pr.number} skipped",
                    body="max_attempts_exceeded",
                    sound="request",
                )
            skipped_count += 1
            continue

        if _pr_worker_is_live(client, job.name, elig_pr.pr.number):
            continue

        # No head to measure the worker's push against: an agent run we could not verify
        # would burn an attempt on an infrastructure gap, so do not start one (issue 070).
        if elig_pr.pr.head_sha is None:
            log.warning(
                "%s: PR #%d skipped, head commit unknown",
                job.name,
                elig_pr.pr.number,
            )
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    extra=HistoryExtra(
                        reason="pr_head_unknown",
                        pr_number=elig_pr.pr.number,
                        attempt=attempt,
                    ),
                ),
            )
            skipped_other += 1
            skipped_count += 1
            continue

        failing_checks = fetch_failing_checks(
            gh, owner=owner, repo=repo_name, number=elig_pr.pr.number
        )
        thread_bodies, given_thread_ids = fetch_fix_worker_threads(
            gh,
            owner=owner,
            repo=repo_name,
            number=elig_pr.pr.number,
            trusted_authors=trusted,
        )

        worker_outcome = _dispatch_fix_worker(
            job=job,
            pr=elig_pr.pr,
            reason=elig_pr.reason,
            run_id=run_id,
            attempt=attempt,
            owner=owner,
            repo=repo_name,
            client=client,
            failing_checks=failing_checks,
            thread_bodies=thread_bodies,
        )

        # A refusal that never launched an agent (issue 070) spent no attempt and is not
        # a failure of this tick: record it `skipped`, tell a human once, and leave the
        # tree exactly as it is.
        if (
            isinstance(worker_outcome, _WorkerFailed)
            and worker_outcome.reason == "worktree_reuse_not_clean"
            and worker_outcome.worktree_path is not None
        ):
            wt = worker_outcome.worktree_path
            first_detection = not _dirty_worktree_already_reported(
                history_path, job.name, wt
            )
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    run_id=run_id,
                    extra=HistoryExtra(
                        reason=worker_outcome.reason,
                        error=worker_outcome.error,
                        pr_number=elig_pr.pr.number,
                        headRefName=elig_pr.pr.head_ref,
                        attempt=attempt,
                        target="pr",
                        worktree_path=wt,
                    ),
                ),
            )
            dirty_worktrees.append((elig_pr.pr.number, wt, worker_outcome.reason))
            if first_detection and _notify_gate(job, "failure"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} PR #{elig_pr.pr.number} blocked",
                    body=f"uncommitted changes in {wt}; rescue or discard by hand",
                    sound="request",
                )
            skipped_other += 1
            skipped_count += 1
            continue

        agent_name = build_worker_agent_name(job.name, elig_pr.pr.number, run_id)
        outcome_state: Literal["done", "failed"] = "failed"
        outcome_reason: str | None = None
        outcome_error: str | None = None
        outcome_report_path: str | None = None
        outcome_agent_status: AgentStatus | None = None
        verify_results: tuple[verify.VerifyResult, ...] = ()
        if isinstance(worker_outcome, _WorkerSettled):
            outcome_state = worker_outcome.state
            outcome_report_path = worker_outcome.report_path
            outcome_agent_status = worker_outcome.final_agent_status
            if outcome_state == "done":
                # `done` is the agent's word (issue 070): prove the worktree is clean and
                # the PR changed before keeping it.
                verify_results = _verify_fix_worker(
                    job,
                    gh=gh,
                    run_id=run_id,
                    now=now,
                    owner=owner,
                    repo=repo_name,
                    pr=elig_pr.pr,
                    worker=worker_outcome,
                    given_thread_ids=given_thread_ids,
                    trusted=trusted,
                )
                failed_gate = next((r for r in verify_results if not r.passed), None)
                if failed_gate is not None:
                    outcome_state = "failed"
                    outcome_reason = failed_gate.reason
                    if outcome_reason == verify.REASON_LEFT_UNCOMMITTED:
                        dirty_worktrees.append(
                            (
                                elig_pr.pr.number,
                                worker_outcome.worktree_path or "",
                                outcome_reason,
                            )
                        )
        else:
            outcome_reason = worker_outcome.reason
            outcome_error = worker_outcome.error

        extra_record = HistoryExtra(
            pr_number=elig_pr.pr.number,
            headRefName=elig_pr.pr.head_ref,
            attempt=attempt,
            eligible_reason=elig_pr.reason,
            fix_worker_agent=agent_name,
            pane_id=worker_outcome.pane_id,
            report_path=outcome_report_path,
            report_written=outcome_report_path is not None,
            final_agent_status=outcome_agent_status,
            target="pr",
        )
        if outcome_reason is not None:
            extra_record["reason"] = outcome_reason
        if outcome_error is not None:
            extra_record["error"] = outcome_error
        if verify_results:
            extra_record["verify"] = [
                {"gate": r.gate, "passed": r.passed, "reason": r.reason}
                for r in verify_results
            ]
        if isinstance(worker_outcome, _WorkerSettled) and worker_outcome.worktree_path:
            extra_record["worktree_path"] = worker_outcome.worktree_path
        if outcome_state == "failed":
            log.warning(
                "%s: PR #%d dispatch failed: reason=%s error=%s",
                job.name,
                elig_pr.pr.number,
                outcome_reason,
                outcome_error,
            )

        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state=outcome_state,
                run_id=run_id,
                extra=extra_record,
            ),
        )

        if outcome_state == "failed":
            any_failed = True
        dispatched_count += 1

    try:
        from herdr_routines.runner import default_reports_dir

        reports_dir = default_reports_dir()
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{run_id}.md"
        report_lines = [
            f"# Gate tick report: {run_id}",
            "",
            f"- **Job**: {job.name}",
            f"- **Time**: {now.isoformat()}",
            "- **Target**: pr",
            f"- **Enumerated**: {len(open_prs)} open PRs",
            f"- **Eligible**: {len(eligible)}",
            f"- **Dispatched**: {dispatched_count}",
            f"- **Skipped (cap)**: {skipped_over_cap}",
            f"- **Skipped (attempts)**: {skipped_count - skipped_over_cap - skipped_other}",
            "",
        ]
        for elig_pr in dispatched:
            report_lines.append(f"- PR #{elig_pr.pr.number}: {elig_pr.reason}")
        if dirty_worktrees:
            report_lines += ["", "## Uncommitted changes kept for a human"]
            report_lines += [
                f"- PR #{number}: {wt_path} ({why}) — not discarded; rescue or "
                f"discard by hand"
                for number, wt_path, why in dirty_worktrees
            ]
        report_path.write_text("\n".join(report_lines) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("%s: could not write aggregate report: %s", job.name, e)

    summary = (
        f"{job.name}: done "
        f"(enumerated={len(open_prs)}, eligible={len(eligible)}, "
        f"dispatched={dispatched_count}, skipped={skipped_count})"
    )

    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="done" if not any_failed else "failed",
            run_id=run_id,
            extra=HistoryExtra(
                gate="passed" if not any_failed else "failed",
                target="pr",
                enumerated=len(open_prs),
                eligible=len(eligible),
                dispatched=dispatched_count,
                skipped=skipped_count,
            ),
        ),
    )

    if any_failed:
        if _notify_gate(job, "failure"):
            body = f"{dispatched_count} dispatched, {skipped_count} skipped"
            left = [
                wt_path
                for _, wt_path, why in dirty_worktrees
                if why == verify.REASON_LEFT_UNCOMMITTED
            ]
            if left:
                body += f"; uncommitted changes kept at {', '.join(left)}"
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=body,
                sound="request",
            )
        return summary, True

    # dispatched_count > 0 means the gate found eligible PRs and dispatched fix workers
    # for them — worth a "finding"-tier notification even though the job itself didn't
    # fail; a clean run with nothing eligible is "success"-tier (on-failure's default
    # silence is exactly the "nothing to report" case issue 009 wants suppressed).
    if _notify_gate(job, "finding" if dispatched_count > 0 else "success"):
        _notify(client, f"herdr-routines: {job.name} done", sound="done")
    return summary, False


def _process_base_target(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime, run_id: str
) -> tuple[str, bool]:
    """Base-target gate: create worktree at base, run command checks, dispatch fix agent on failure."""
    from herdr_routines.runner import (
        _capture_visible_tail,
        _close_run_pane,
        _prompt_with_watchdog,
        _wait_for_agent_ready,
        build_branch_name,
        default_reports_dir,
        substitute_prompt,
    )

    checks = job.require_checks()
    gate_branch = build_branch_name(job.name, run_id)

    attempt = consecutive_base_gate_failures(history_path, job.name)

    # Ensure repo checkout before any worktree creation
    try:
        ensure_repo(job)
    except (RuntimeError, OSError) as e:
        reason = (
            "clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed"
        )
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed", reason=reason, error=str(e), target="base"
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=reason,
                sound="request",
            )
        return f"{job.name}: failed ({reason})", True

    wt_path = Path(job.repo) / ".worktrees" / f"gate-{run_id}"
    try:
        subprocess.run(
            ["git", "-C", str(job.repo), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(job.repo),
                "worktree",
                "add",
                "--detach",
                str(wt_path),
                job.base,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    except Exception as e:  # noqa: BLE001
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="worktree_creation_failed",
                    error=str(e),
                    target="base",
                    gate_branch=gate_branch,
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="worktree_creation_failed",
                sound="request",
            )
        return f"{job.name}: failed (worktree_creation_failed)", True

    gate_outcome = run_checks(checks, cwd=str(wt_path))

    try:
        subprocess.run(
            ["git", "-C", str(job.repo), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:  # noqa: BLE001, S110
        pass

    if gate_outcome.passed:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="done",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="passed",
                    target="base",
                    gate_branch=gate_branch,
                    gate_output_path=None,
                ),
            ),
        )
        if _notify_gate(job, "success"):
            _notify(client, f"herdr-routines: {job.name} done", sound="done")
        return f"{job.name}: done (gate passed)", False

    # The budget limits fix-agent dispatches, not gate runs: a passing gate above
    # still records `passed` and resets it, so only a failing gate is skipped here.
    if attempt >= job.max_attempts_per_target:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="skipped",
                run_id=run_id,
                extra=HistoryExtra(
                    reason="max_attempts_exceeded",
                    gate_branch=gate_branch,
                    attempt=attempt,
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} skipped",
                body="max_attempts_exceeded",
                sound="request",
            )
        return f"{job.name}: skipped (max_attempts_exceeded)", False

    gate_output_path = default_reports_dir() / f"{run_id}-gate-output.txt"
    try:
        gate_output_path.parent.mkdir(parents=True, exist_ok=True)
        gate_output_path.write_text(gate_outcome.combined_output)
    except OSError:
        pass

    agent_name = build_gate_worker_agent_name(job.name, run_id)
    report_path = default_reports_dir() / f"{run_id}.md"

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="report_dir_creation_failed",
                    error=str(e),
                    target="base",
                    gate_branch=gate_branch,
                ),
            ),
        )
        return f"{job.name}: failed (report_dir_creation_failed)", True

    prompt_text = job.prompt or build_base_fix_prompt(
        job_name=job.name,
        gate_output=gate_outcome.combined_output,
        base=job.base,
        report_path=str(report_path),
        checks=checks,
    )
    prompt_text = substitute_prompt(
        prompt_text, report_path=report_path, job_name=job.name, run_id=run_id
    )

    fix_wt_path = Path(job.repo) / ".worktrees" / f"fix-{run_id}"
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(job.repo),
                "worktree",
                "remove",
                "--force",
                str(fix_wt_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(job.repo),
                "worktree",
                "add",
                "--detach",
                str(fix_wt_path),
                job.base,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    except Exception as e:  # noqa: BLE001
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="worktree_creation_failed",
                    error=str(e),
                    target="base",
                    gate_branch=gate_branch,
                ),
            ),
        )
        return f"{job.name}: failed (worktree_creation_failed)", True

    pane_id: str | None = None
    try:
        pane_id = client.tab_create(cwd=str(fix_wt_path), label=agent_name)
        client.agent_start(
            name=agent_name,
            kind=job.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=job.model,
        )
    except (HerdrCliError, OSError) as e:
        if pane_id is not None:
            try:
                client.pane_close(pane_id)
            except Exception:  # noqa: BLE001, S110
                pass
        _cleanup_worktree(job.repo, fix_wt_path)
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="agent_start_failed",
                    error=str(e),
                    pane_id=pane_id,
                    target="base",
                    gate_branch=gate_branch,
                ),
            ),
        )
        return f"{job.name}: failed (agent_start_failed)", True

    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        _cleanup_worktree(job.repo, fix_wt_path)
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="agent_not_interactive",
                    error=last_error,
                    pane_id=pane_id,
                    target="base",
                    gate_branch=gate_branch,
                ),
            ),
        )
        return f"{job.name}: failed (agent_not_interactive)", True

    try:
        settled_status = _prompt_with_watchdog(
            client,
            job_name=agent_name,
            target=agent_name,
            text=prompt_text,
            timeout_ms=job.timeout_ms,
            markers=job.failure_markers
            if job.failure_markers is not None
            else ("Free usage exceeded",),
            prompt_text=prompt_text,
        )
    except Exception as e:  # noqa: BLE001
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        _cleanup_worktree(job.repo, fix_wt_path)
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=HistoryExtra(
                    gate="failed",
                    reason="agent_prompt_failed",
                    error=str(e),
                    pane_id=pane_id,
                    target="base",
                    gate_branch=gate_branch,
                ),
            ),
        )
        return f"{job.name}: failed (agent_prompt_failed)", True

    _capture_visible_tail(
        client, agent_name, reports_dir=report_path.parent, run_id=run_id
    )

    report_written = report_path.exists()
    _report_bytes = report_path.stat().st_size if report_written else 0

    session_id: str | None = None
    try:
        session_id = client.agent_session_id(agent_name)
    except Exception as e:  # noqa: BLE001 — session id is best-effort reporting data
        log.debug("could not read session id for %s: %s", agent_name, e)

    _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
    _cleanup_worktree(job.repo, fix_wt_path)

    state: HistoryState = (
        "done" if settled_status in SETTLED_AGENT_STATUSES else "failed"
    )
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state=state,
            run_id=run_id,
            extra=HistoryExtra(
                gate="failed",
                target="base",
                gate_branch=gate_branch,
                reason="gate_failed",
                failed_checks=gate_outcome.combined_output,
                gate_output_path=str(gate_output_path)
                if gate_output_path.exists()
                else None,
                branch=gate_branch,
                pane_id=pane_id,
                report_path=str(report_path) if report_written else None,
                report_written=report_written,
                final_agent_status=settled_status,
                session_id=session_id,
            ),
        ),
    )

    if state == "failed":
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="agent_prompt_failed",
                sound="request",
            )
        return f"{job.name}: failed (agent_prompt_failed)", True

    # Reaching here means the gate check failed above (gate_outcome.passed was False —
    # otherwise this function already returned at the "gate passed" branch) and the fix
    # agent then resolved it: a "finding", not a plain "success".
    if _notify_gate(job, "finding"):
        _notify(client, f"herdr-routines: {job.name} done", sound="done")
    return f"{job.name}: done", False


# Bounds the one `git check-ref-format` call per event head ref; it reads no repo.
_REF_CHECK_TIMEOUT_S = 10


def _is_valid_branch_name(ref: str) -> bool | None:
    """Whether ``ref`` is a plain branch name git itself accepts
    (``git check-ref-format --branch``), or None when the check could not run (timeout,
    no git) — an unknown answer, never a pass and never proof the name is bad.

    Two shapes are refused before git is asked: a leading ``-``, which a positional
    argument could read as an option, and ``@{``, because ``--branch`` expands the
    ``@{-N}`` previous-branch syntax instead of rejecting it. The check reads no
    repository, so it runs the same from any working directory."""
    if not ref or ref.startswith("-") or "@{" in ref:
        return False
    try:
        proc = subprocess.run(
            ["git", "check-ref-format", "--branch", ref],
            capture_output=True,
            text=True,
            timeout=_REF_CHECK_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode == 0


def _find_existing_worktree(repo: Path, branch: str) -> Path | None:
    """Return the path of a worktree that already has *branch* checked out, or None.
    Parses `git worktree list --porcelain` (issue 036: the pipeline orchestrator
    deliberately retains its worktree on the PR branch after a run — see
    docs/pipeline/orchestrator-prompt.md G-10 — so a second `git worktree add` on
    that same branch always fails with "already used by worktree at ...")."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return None
    target = f"refs/heads/{branch}"
    current_path: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif (
            line.startswith("branch ")
            and current_path is not None
            and line[len("branch ") :] == target
        ):
            return Path(current_path)
    return None


def _worktree_reuse_check(wt_path: Path) -> tuple[bool, str]:
    """Before pointing a fix worker at a worktree we didn't create ourselves, verify
    it has no uncommitted changes — an in-progress edit there (e.g. a still-running
    orchestrator) must not be clobbered by a fix worker starting concurrently."""
    return verify.worktree_status(wt_path)


def _cleanup_worktree(repo: Path, wt_path: Path) -> None:
    try:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not remove worktree %s: %s", wt_path, e)


def _dispatch_fix_worker(
    *,
    job: Job,
    pr: PRInfo,
    reason: str,
    run_id: str,
    attempt: int,
    owner: str,
    repo: str,
    client: HerdrClient,
    failing_checks: str,
    thread_bodies: str,
) -> _WorkerResult:
    """Dispatch a single fix worker for a PR. Uses git worktree add to checkout
    the PR head branch directly (review finding B), not build_branch_name which
    creates a new auto/* branch that the agent would push to instead of the PR
    branch."""
    from herdr_routines.runner import (
        _capture_visible_tail,
        _close_run_pane,
        _prompt_with_watchdog,
        _wait_for_agent_ready,
        default_reports_dir,
        substitute_prompt,
    )

    agent_name = build_worker_agent_name(job.name, pr.number, run_id)
    pr_run_id = f"{run_id}-pr{pr.number}"
    report_path = default_reports_dir() / f"auto-fix-{run_id}-pr{pr.number}.md"

    # Ensure repo checkout before any worktree creation
    try:
        ensure_repo(job)
    except (RuntimeError, OSError) as e:
        reason = (
            "clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed"
        )
        return _WorkerFailed(reason=reason, error=str(e))

    # Create report dir
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _WorkerFailed(reason="report_dir_creation_failed", error=str(e))

    # Build prompt with real data (review finding C) and report path (review finding D)
    prompt_text = job.prompt or build_fix_prompt(
        pr_number=pr.number,
        branch=pr.head_ref,
        failing_checks=failing_checks,
        thread_bodies=thread_bodies,
        owner_repo=f"{owner}/{repo}",
        report_path=str(report_path),
    )
    prompt_text = substitute_prompt(
        prompt_text, report_path=report_path, job_name=job.name, run_id=pr_run_id
    )

    # Reuse an existing checkout of the PR's head branch instead of forcing a second
    # `worktree add` on it (issue 036), which git refuses whenever the orchestrator's
    # retained worktree (or a prior autofix attempt's) already holds that branch.
    existing_wt = _find_existing_worktree(job.repo, pr.head_ref)
    if existing_wt is not None:
        ok, detail = _worktree_reuse_check(existing_wt)
        if not ok:
            return _WorkerFailed(
                reason="worktree_reuse_not_clean",
                error=f"existing worktree {existing_wt} for {pr.head_ref}: {detail}",
                worktree_path=str(existing_wt),
            )
        wt_path = existing_wt
    else:
        # Create worktree pinned to PR head branch (review finding B)
        wt_path = Path(job.repo) / ".worktrees" / f"autofix-pr{pr.number}"
        try:
            # Remove stale worktree if it exists from a prior attempt
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(job.repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(wt_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(job.repo),
                    "worktree",
                    "add",
                    str(wt_path),
                    pr.head_ref,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
        except Exception as e:  # noqa: BLE001  # noqa: BLE001 — any worktree failure marks the PR failed
            return _WorkerFailed(reason="worktree_creation_failed", error=str(e))

    # Start agent in the worktree (review finding B)
    pane_id: str | None = None
    try:
        pane_id = client.tab_create(cwd=str(wt_path), label=agent_name)
        client.agent_start(
            name=agent_name,
            kind=job.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=job.model,
        )
    except (HerdrCliError, OSError) as e:
        if pane_id is not None:
            try:
                client.pane_close(pane_id)
            except Exception as e2:  # noqa: BLE001 — best-effort close must not mask the real error
                log.debug("error closing pane %s after start failure: %s", pane_id, e2)
        return _WorkerFailed(reason="agent_start_failed", error=str(e), pane_id=pane_id)

    # Wait for agent readiness
    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=pr_run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _WorkerFailed(
            reason="agent_not_interactive", error=last_error, pane_id=pane_id
        )

    # Deliver prompt with watchdog
    try:
        settled_status = _prompt_with_watchdog(
            client,
            job_name=agent_name,
            target=agent_name,
            text=prompt_text,
            timeout_ms=job.timeout_ms,
            markers=job.failure_markers or ("Free usage exceeded",),
            prompt_text=prompt_text,
        )
    except Exception as e:  # noqa: BLE001  # noqa: BLE001 — any prompt failure marks the PR failed
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=pr_run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _WorkerFailed(
            reason="agent_prompt_failed", error=str(e), pane_id=pane_id
        )

    # Capture tail and close pane
    _capture_visible_tail(
        client, agent_name, reports_dir=report_path.parent, run_id=pr_run_id
    )

    report_written = report_path.exists()
    report_bytes = report_path.stat().st_size if report_written else 0

    session_id: str | None = None
    try:
        session_id = client.agent_session_id(agent_name)
    except Exception as e:  # noqa: BLE001  # noqa: BLE001 — session id is best-effort reporting data
        log.debug("could not read session id for %s: %s", agent_name, e)

    _close_run_pane(client, job_name=agent_name, pane_id=pane_id)

    return _WorkerSettled(
        state="done" if settled_status in SETTLED_AGENT_STATUSES else "failed",
        pane_id=pane_id,
        report_path=str(report_path) if report_written else None,
        report_bytes=report_bytes,
        final_agent_status=settled_status,
        session_id=session_id,
        worktree_path=str(wt_path),
    )


# The post-settle gates a gated fix worker must pass to stay `done` (issue 070). Order is
# precedence: when both fail the dirty tree is the recorded reason, because it is what
# wedges every later attempt on the branch.
_FIX_WORKER_GATES: tuple[VerifyGate, ...] = ("worktree_clean", "pushed_or_replied")
# Reasons whose record carries a dirty worktree a human has to deal with.
_DIRTY_WORKTREE_REASONS = frozenset(
    {"worktree_reuse_not_clean", verify.REASON_LEFT_UNCOMMITTED}
)


def _verify_fix_worker(
    job: Job,
    *,
    gh: GhClient,
    run_id: str,
    now: datetime,
    owner: str,
    repo: str,
    pr: PRInfo,
    worker: _WorkerSettled,
    given_thread_ids: tuple[str, ...] | None,
    trusted: frozenset[str],
) -> tuple[verify.VerifyResult, ...]:
    """Run `_FIX_WORKER_GATES` for a fix worker whose agent settled. The agent's `done`
    is its word; these read the worktree and the PR."""
    return verify.run_verify_gates(
        _FIX_WORKER_GATES,
        VerifyContext(
            job=job,
            run_id=run_id,
            started_at=now,
            report_path=Path(worker.report_path or ""),
            gh=gh,
            trusted_review_authors=(),
            target=VerifyTarget(owner, repo, pr.number),
            trusted_authors=trusted,
            worktree_path=Path(worker.worktree_path) if worker.worktree_path else None,
            dispatch_head_sha=pr.head_sha,
            given_thread_ids=given_thread_ids,
        ),
    )


def _dirty_worktree_already_reported(
    history_path: Path, job_name: str, worktree_path: str
) -> bool:
    """Whether the latest record naming this worktree already reported it dirty (issue
    070), so the first detection notifies a human and later ticks stay quiet. A clean
    run there since makes the next dirty finding a new detection."""
    for record in reversed(read_job(history_path, job_name)):
        if record.extra and record.extra.get("worktree_path") == worktree_path:
            return record.extra.get("reason") in _DIRTY_WORKTREE_REASONS
    return False


def _outcome_extra(
    outcome: RunOutcome, *, target: VerifyTarget | None = None
) -> HistoryExtra:
    extra = HistoryExtra(
        agent=outcome.agent_name,
        pane_id=outcome.pane_id,
        branch=outcome.branch,
        final_agent_status=outcome.final_agent_status,
        report_written=outcome.report_written,
        report_bytes=outcome.report_bytes,
        report=outcome.report_path,
        duration_seconds=outcome.duration_seconds,
        session_id=outcome.session_id,
    )
    if outcome.reason:
        extra["reason"] = outcome.reason
    if outcome.error:
        extra["error"] = outcome.error
    if outcome.nudged:
        extra["nudged"] = True
    if outcome.resigned_commits:
        extra["resigned_commits"] = outcome.resigned_commits
    if outcome.reaped_stale_agent:
        extra["reaped_stale_agent"] = True
    if target is not None:
        extra["target"] = target.ref  # issue 073: what the selector picked
    if outcome.verify:
        extra["verify"] = [
            VerifyRecord(gate=r.gate, passed=r.passed, reason=r.reason)
            for r in outcome.verify
        ]
    return extra


def _retry_eligible(outcome: RunOutcome, job: Job) -> bool:
    """Check if this outcome is eligible for a retry attempt per the job's retry config."""
    return (
        outcome.state == "failed"
        and outcome.reason is not None
        and job.retry_on is not None
        and outcome.reason in job.retry_on
    )


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One firing of a job (issue 074): a cron occurrence the tick found due, or a manual
    `herdr-routines run`. `extra` goes into the `running` record: `scheduled_for` and
    `late_seconds` for a cron firing, `trigger: manual` for a manual one."""

    run_id: str
    extra: HistoryExtra


def _occurrence_extra(occ: Occurrence) -> HistoryExtra:
    return HistoryExtra(**occ.extra)


def _with_trigger(
    source: Occurrence | HistoryExtra, extra: HistoryExtra
) -> HistoryExtra:
    """Stamp `extra` with the occurrence's `trigger` when it is manual. A scheduled record
    gets no `trigger` key, so a cron firing writes the same records it always did."""
    origin = source.extra if isinstance(source, Occurrence) else source
    if origin.get("trigger") == TRIGGER_MANUAL:
        extra["trigger"] = TRIGGER_MANUAL
    return extra


def _append_running(
    job: Job, history_path: Path, occ: Occurrence, *, now: datetime
) -> None:
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=occ.run_id,
            extra=_occurrence_extra(occ),
        ),
    )


def dispatch_occurrence(
    job: Job,
    history_path: Path,
    occ: Occurrence,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    """Everything after "this job is due" for one occurrence, per kind (issue 074). The tick
    reaches it from each `_process_*_job` once `schedule.decide` says RUN; `herdr-routines
    run` skips that decision and calls it directly. One function, so the two cannot drift."""
    if job.kind == "pipeline":
        # A manual run's `running` record would shadow an in-flight one, and tick reconciles
        # only the latest open run, so refuse instead.
        if (
            occ.extra.get("trigger") == TRIGGER_MANUAL
            and _open_pipeline_run(history_path, job.name) is not None
        ):
            return f"{job.name}: skipped (already running)", True
        return _launch_pipeline_run(
            job,
            history_path,
            client=client,
            now=now,
            run_id=occ.run_id,
            bare_run_id=_bare_pipeline_run_id(job.name, occ.run_id),
            running_extra=_occurrence_extra(occ),
        )
    if job.kind == "audit":
        _append_running(job, history_path, occ, now=now)
        return _audit_cycle(
            job, history_path, client=client, now=now, run_id=occ.run_id
        )
    if job.kind == "gated":
        return _gated_cycle(
            job,
            history_path,
            occ,
            client=client,
            now=now,
            trusted_review_authors=trusted_review_authors,
        )
    if job.kind == "review":
        _append_running(job, history_path, occ, now=now)
        return _review_cycle(
            job,
            history_path,
            client=client,
            now=now,
            run_id=occ.run_id,
            trusted_review_authors=trusted_review_authors,
        )
    return _run_routine(
        job,
        history_path,
        occ,
        client=client,
        now=now,
        trusted_review_authors=trusted_review_authors,
    )


def _gated_cycle(
    job: Job,
    history_path: Path,
    occ: Occurrence,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...],
) -> tuple[str, bool]:
    """One occurrence of a gated job, after the schedule guards: record `running`, then
    route to the event / pr / base target. The tick and `run` both land here (issue 074)."""
    checks = job.require_checks()
    target = job.require_target()
    run_id = occ.run_id

    _append_running(job, history_path, occ, now=now)

    # github_event (issue 014) is a poll predicate: same schedule guards as every
    # gated job, but its "gate" is a diff against last-seen event IDs rather than a
    # pr/base fix path — the event itself decides, so it routes to its own target.
    for check in checks:
        if check.kind == "github_event":
            return _process_event_target(
                job,
                history_path,
                client=client,
                now=now,
                run_id=run_id,
                check=check,
                trusted_review_authors=trusted_review_authors,
            )

    if target == "pr":
        return _process_pr_target(
            job,
            history_path,
            client=client,
            now=now,
            run_id=run_id,
            trusted_review_authors=trusted_review_authors,
        )
    else:
        return _process_base_target(
            job, history_path, client=client, now=now, run_id=run_id
        )


def _run_routine(
    job: Job,
    history_path: Path,
    occ: Occurrence,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...],
) -> tuple[str, bool]:
    """One occurrence of a plain routine job: selector, `running` record, `execute_run` with
    the fallback_model and retry loop, terminal record, notification. The tick and
    `herdr-routines run` both land here (issue 074)."""
    run_id = occ.run_id
    # Target selector (issue 073): answered in code before any worktree or agent exists.
    # Both outcomes below are terminal records, so the occurrence counts as having run:
    # the next tick sees `not due`, never `missed`, and neither is retried.
    gh: RealGhClient | None = None
    target: VerifyTarget | None = None
    if job.pick is not None:
        gh = RealGhClient()
        selected = select_target(
            SelectorContext(
                job=job, gh=gh, trusted_review_authors=trusted_review_authors
            )
        )
        pre_dispatch_extra = _occurrence_extra(occ)
        if isinstance(selected, SelectorFailed):
            pre_dispatch_extra["reason"] = "selector_failed"
            pre_dispatch_extra["error"] = selected.error
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="failed",
                    run_id=run_id,
                    extra=pre_dispatch_extra,
                ),
            )
            if _notify_gate(job, "failure"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} failed",
                    body="selector_failed",
                    sound="request",
                )
            return f"{job.name}: failed (selector_failed)", True
        if selected is None:
            pre_dispatch_extra["reason"] = "no_target"
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    run_id=run_id,
                    extra=pre_dispatch_extra,
                ),
            )
            # Nothing to do is not news: only `notify_policy: always` hears about it.
            if _notify_gate(job, "progress"):
                _notify(client, f"herdr-routines: {job.name} skipped", body="no_target")
            return f"{job.name}: skipped (no_target)", False
        target = selected

    running_extra = _occurrence_extra(occ)
    if target is not None:
        running_extra["target"] = target.ref
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=run_id,
            extra=running_extra,
        ),
    )

    outcome = execute_run(
        job,
        client,
        run_id=run_id,
        trusted_review_authors=trusted_review_authors,
        gh=gh,
        target=target,
    )
    used_fallback = False
    attempt = 0  # 0 = first try

    # Per-job retry loop (issue 008): bounded synchronous retries within the same
    # tick, gated on retry_on whitelist. The fallback_model quota retry runs inside
    # each attempt so retried attempts also get the fallback treatment.
    base_run_id = run_id
    while True:
        # Fallback_model quota retry (issue 008): runs inside each attempt so a
        # retried attempt that hits quota_exhausted also gets the fallback.
        if (
            outcome.state == "failed"
            and outcome.reason == "quota_exhausted"
            and job.fallback_model
            and job.fallback_model != job.model
        ):
            used_fallback = True
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state=outcome.state,
                    run_id=run_id,
                    extra=_with_trigger(occ, _outcome_extra(outcome, target=target)),
                ),
            )
            log.info(
                "%s: primary model quota_exhausted, retrying once with fallback_model=%r",
                job.name,
                job.fallback_model,
            )
            fallback_run_id = make_run_id(f"{job.name}-fallback", now)
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="running",
                    run_id=fallback_run_id,
                    extra=_with_trigger(
                        occ,
                        HistoryExtra(reason="fallback_retry", primary_run_id=run_id),
                    ),
                ),
            )
            run_id = fallback_run_id
            outcome = execute_run(
                replace(job, model=job.fallback_model),
                client,
                run_id=run_id,
                trusted_review_authors=trusted_review_authors,
                gh=gh,
                target=target,
            )

        # Check if this outcome is eligible for a retry attempt
        if not _retry_eligible(outcome, job) or attempt >= job.retry_attempts:
            break

        # Log the failed attempt as a distinct history record with attempt metadata
        extra = _with_trigger(occ, _outcome_extra(outcome, target=target))
        extra["attempt"] = attempt
        extra["retry_eligible_reason"] = outcome.reason
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state=outcome.state,
                run_id=run_id,
                extra=extra,
            ),
        )
        attempt += 1
        run_id = f"{base_run_id}-retry{attempt}"
        log.info(
            "%s: retry attempt %d/%d after %s",
            job.name,
            attempt,
            job.retry_attempts,
            outcome.reason,
        )
        # Brief backoff (reuses PROMPT_RETRY_DELAYS_S shape: 5s, 15s, ...)
        backoff = (5.0, 15.0)[min(attempt - 1, 1)]
        time.sleep(backoff)
        # Append a running record for the retry attempt
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="running",
                run_id=run_id,
                extra=_with_trigger(
                    occ,
                    HistoryExtra(
                        attempt=attempt,
                        max_retries=job.retry_attempts,
                        retried_from_run_id=base_run_id
                        if attempt == 1
                        else f"{base_run_id}-retry{attempt - 1}",
                    ),
                ),
            ),
        )
        outcome = execute_run(
            job,
            client,
            run_id=run_id,
            trusted_review_authors=trusted_review_authors,
            gh=gh,
            target=target,
        )

    # Terminal record for final outcome (last retry or first attempt if no retries)
    final_extra = _with_trigger(occ, _outcome_extra(outcome, target=target))
    final_extra["attempt"] = attempt
    final_extra["max_retries"] = job.retry_attempts
    if attempt > 0:
        final_extra["final_attempt"] = True
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state=outcome.state,
            run_id=run_id,
            extra=final_extra,
        ),
    )

    if outcome.state == "done":
        # Both of these mean the job ultimately succeeded but something a human should know
        # about happened on the way — surface as a "finding", not a plain "success":
        #  - fallback retry ⇒ the primary model hit quota_exhausted
        #  - reaped_stale_agent ⇒ a prior run's blocked agent was force-closed (issue 051)
        notes = []
        if used_fallback:
            notes.append(f"via fallback_model={job.fallback_model}")
        if outcome.reaped_stale_agent:
            notes.append("force-closed a prior run's blocked agent (issue 051)")
        done_body = "; ".join(notes) or None
        gate = "finding" if (used_fallback or outcome.reaped_stale_agent) else "success"
        if _notify_gate(job, gate):
            _notify(
                client, f"herdr-routines: {job.name} done", body=done_body, sound="done"
            )
        return f"{job.name}: done", False

    if _notify_gate(job, "failure"):
        if outcome.reason == "blocked":
            excerpt = extract_prompt_excerpt(outcome.visible_tail or "")
            body_lines = [
                f"job={job.name}",
                f"run={run_id}",
                f"pane={outcome.pane_id or 'unknown'}",
                f"agent={outcome.agent_name or 'unknown'}",
            ]
            if excerpt:
                body_lines.append(f"prompt: {excerpt}")
            body_lines.append(
                'Reply to this message to approve — e.g. "yes" / "approve"'
            )
            _notify(
                client,
                f"herdr-routines: {job.name} blocked",
                body="\n".join(body_lines),
                sound="request",
            )
        else:
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=outcome.reason or "unknown",
                sound="request",
            )
    return f"{job.name}: failed ({outcome.reason})", True


def _process_job(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    # kind: pipeline dispatches a detached systemd-run unit and returns immediately —
    # tick must never block on the multi-hour orchestrator run (issue 026). Checked first
    # since config.py already rejects `checks:` on a pipeline job (mutually exclusive
    # dispatch paths); the order here is belt-and-braces, not load-bearing.
    if job.kind == "pipeline":
        return _process_pipeline_job(job, history_path, client=client, now=now)

    # Audit jobs (issue 057) run an audit skill/command, diff its findings against the
    # ledger, and dispatch fixes only for new/regressed findings. Schedule guards match
    # the gated path; the "gate" is the report diff, not a checks list.
    if job.kind == "audit":
        return _process_audit_job(job, history_path, client=client, now=now)

    # Gated jobs follow the same schedule guards but run gate checks + dispatch
    # instead of execute_run when their cron fires (issue 049: kind is the SSOT).
    if job.kind == "gated":
        return _process_gated_job(
            job,
            history_path,
            client=client,
            now=now,
            trusted_review_authors=trusted_review_authors,
        )

    # Review jobs (issue 055) search across repos, dispatch one read-only review worker
    # per eligible open PR, and verify each posted review with a content marker. Same
    # schedule guards as the gated path.
    if job.kind == "review":
        return _process_review_job(
            job,
            history_path,
            client=client,
            now=now,
            trusted_review_authors=trusted_review_authors,
        )

    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=job.timeout_ms, now=now
    )
    if stale is not None:
        stale_extra = HistoryExtra(reason="stale_running_record")
        reaped = _reap_agents_of_rebooted_run(
            client, job, stale, boot_epoch=system_boot_epoch()
        )
        if reaped:
            stale_extra["reaped_agents"] = reaped
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="interrupted_unknown",
                run_id=stale.run_id,
                extra=stale_extra,
            ),
        )

    if is_currently_running(history_path, job.name, timeout_ms=job.timeout_ms, now=now):
        return f"{job.name}: skipped (already running)", False

    if _live_agent_exists(client, job):
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="skipped",
                extra=HistoryExtra(reason="agent_name_live"),
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_scheduled_terminal_run(history_path, job.name)
    registered_at = first_seen_at(history_path, job.name) or now
    result = decide(
        cron=job.cron,
        timezone=job.timezone,
        catch_up_minutes=job.catch_up_minutes,
        now=now,
        last_terminal=last,
        job_registered_at=registered_at,
    )

    if result.decision == Decision.NOT_DUE:
        return f"{job.name}: not due", False

    if result.decision == Decision.MISSED:
        extra = HistoryExtra(
            reason="outside_catch_up_window", occurrence=result.occurrence.isoformat()
        )
        if result.skipped_occurrences:
            extra["skipped_occurrences"] = result.skipped_occurrences
        append(
            history_path,
            HistoryRecord(ts=now, job=job.name, state="missed", extra=extra),
        )
        if job.on_missed == "notify":
            _notify(
                client,
                f"herdr-routines: {job.name} missed",
                body="outside catch-up window",
                sound="request",
            )
        return f"{job.name}: missed", False

    # Decision.RUN
    run_id = make_run_id(job.name, result.occurrence)
    if result.skipped_occurrences:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra=HistoryExtra(
                    reason="collapsed_earlier_occurrences",
                    skipped_occurrences=result.skipped_occurrences,
                    skipped_first=result.skipped_first.isoformat()
                    if result.skipped_first
                    else None,
                    skipped_last=result.skipped_last.isoformat()
                    if result.skipped_last
                    else None,
                ),
            ),
        )

    return dispatch_occurrence(
        job,
        history_path,
        Occurrence(
            run_id,
            HistoryExtra(
                scheduled_for=result.occurrence.isoformat(),
                late_seconds=result.late_seconds,
            ),
        ),
        client=client,
        now=now,
        trusted_review_authors=trusted_review_authors,
    )


# -- kind: pipeline dispatch (issue 026) --------------------------------------------
#
# tick launches the overnight orchestrator as a detached `systemd-run --user` unit
# (scripts/pipeline-launch.sh) and returns immediately — it must never block on the
# multi-hour run while holding tick.lock. A later tick reconciles the run purely by
# reading the terminal report the orchestrator (or pipeline_watchdog.py, issue 031, if
# the orchestrator dies silently) writes — both write the same `## Outcome:` marker
# (docs/pipeline/orchestrator-prompt.md "Final report"), so tick has one reconcile
# contract regardless of which side wrote it.

# Matches the launcher script's `-p RuntimeMaxSec=...` margin (scripts/pipeline-launch.sh
# and design.md's TimeoutStartSec precedent): the systemd unit outlives the orchestrator's
# own `deadline_ms` so it can write a partial report + notify before any kill.
PIPELINE_UNIT_MARGIN_MS = 600_000

# How far past `deadline_ms` tick waits for a terminal report before declaring a silently
# dead orchestrator itself. Deliberately generous — pipeline_watchdog.py is the fast path
# (deadline + 30min grace, checked every 15min) and normally writes the report long before
# this trips; this is the slow backstop for "the watchdog timer itself isn't running"
# (the documented "Known limitation": issue 026 doesn't promise a bound tighter than this).
PIPELINE_RECONCILE_GRACE_MS = 60 * 60 * 1000  # 1h

_OUTCOME_RE = re.compile(r"^##\s*Outcome:\s*(?P<status>.+?)\s*$", re.MULTILINE)
# The reason a `skipped` outcome names itself, e.g. `skipped (no_feature)`. A marker
# with no parenthesised reason yields None rather than a guessed one.
_SKIP_REASON_RE = re.compile(r"^skipped\s*\(\s*(?P<reason>[^)]+?)\s*\)$")


def pipeline_report_path(run_id: str) -> Path:
    """Bare-timestamp run_id -> the pinned terminal-report path. Matches both the
    launcher script's `--report` argument and pipeline_watchdog._write_report's own
    convention — one filename contract regardless of which side writes it."""
    return default_reports_dir() / f"pipeline-{run_id}.md"


def _bare_pipeline_run_id(job_name: str, run_id: str) -> str:
    """`nightly-pipeline-20260904T020000Z` -> `20260904T020000Z` — the same
    remove-the-job-name-prefix idiom `runner.build_branch_name` uses. This bare timestamp
    is what the orchestrator receives as RUN_ID (herdr caps agent names at 32 chars;
    `pl-1-<full run_id>` overflows, `pl-1-<bare_ts>` fits — see the issue's RUN_ID
    contract)."""
    return run_id.removeprefix(f"{job_name}-")


def _pipeline_stage_independence_issue(bare_run_id: str) -> str | None:
    """design.md G-17 gate: reason string if this run's `state.json` `stage_sessions`
    show the per-stage fresh-session contract was faked, else None. None also when state.json is
    gone/unreadable — a human may gc that worktree (G-14), and "can't check" must not
    turn a clean run red."""
    state_path = pipeline_state_json_path(default_worktrees_root(), bare_run_id)
    if state_path is None:
        return None
    try:
        raw = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return validate_stage_sessions(raw) if isinstance(raw, dict) else None


def _pipeline_host_rebooted_mid_run(bare_run_id: str) -> bool:
    """True when the host booted after the orchestrator's last state.json write — proof
    the orchestrator (and its `systemd-run` launcher unit) is dead, so tick can fail the
    run now rather than waiting out `deadline_ms + grace`. False whenever it can't tell
    (no state.json, no /proc/stat)."""
    state_path = pipeline_state_json_path(default_worktrees_root(), bare_run_id)
    if state_path is None:
        return False
    return host_rebooted_after_state_write(state_path, boot_epoch=system_boot_epoch())


def _classify_pipeline_outcome(report_text: str) -> tuple[HistoryState, str | None]:
    """(history_state, reason) from a terminal report's first `## Outcome:` line.

    `partial (deadline exceeded)` is tolerated content-wise (the orchestrator is
    documented to wait out an in-flight stage before writing it, design.md G-7) but is
    still reported `failed` here — a run that didn't finish is not silently green.

    `skipped (…)` is its own terminal state, not a flavour of failure: `pipeline-prepare`
    writes `## Outcome: skipped (no_feature)` when the backlog is empty and the launcher
    starts no agent at all (issue 054 phase A, which closes issue 052). A healthy night
    with nothing to build must not read as a red history line — and the reconcile path
    below returns before the failure notification for it. The parenthesised text is
    returned as the reason so a *different* skip (`skipped (deadline_not_reached)`)
    records what actually happened rather than being filed under `no_feature`; the
    `skipped` early return in the caller is what keeps a reason from becoming a retry."""
    match = _OUTCOME_RE.search(report_text)
    if match is None:
        return "interrupted_unknown", "outcome_marker_missing"
    status = match.group("status").strip().lower()
    if status.startswith("ok"):
        return "done", None
    if status.startswith("skipped"):
        reason_match = _SKIP_REASON_RE.match(status)
        return "skipped", reason_match.group("reason") if reason_match else None
    if status.startswith("partial"):
        return "failed", "partial_deadline"
    if status.startswith("failed"):
        if "watchdog" in status:
            return "failed", "watchdog_killed"
        if "quota_exhausted" in status:
            return "failed", "quota_exhausted"
        if "ci_check_failed" in status:
            return "failed", "ci_check_failed"
        return "failed", "orchestrator_failed"
    return "interrupted_unknown", "outcome_marker_unrecognized"


def _open_pipeline_run(history_path: Path, job_name: str) -> HistoryRecord | None:
    """Latest 'running' record for this job with no terminal record for the same run_id
    yet — regardless of staleness. Deliberately distinct from `is_currently_running`,
    which folds a clock-based staleness check into the same predicate: the pipeline
    reconcile below needs "is there an open run" and "has it gone stale" as two separate
    questions, since staleness alone must never flip a healthy run to failed (see the
    dispatch-window comment in `_process_pipeline_job`)."""
    records = read_job(history_path, job_name)
    terminal_run_ids = {
        r.run_id for r in records if r.state in TERMINAL_STATES and r.run_id
    }
    latest_running: HistoryRecord | None = None
    for record in records:
        if record.state == "running":
            latest_running = record
    if latest_running is None or latest_running.run_id in terminal_run_ids:
        return None
    return latest_running


def launch_pipeline(
    argv: list[str], *, timeout_s: float = 30.0
) -> tuple[int, str, str]:
    """The one impure seam in the pipeline dispatch path: registers the detached
    `systemd-run --user` unit and returns as soon as it's registered — never waits for
    the orchestrator. Module-level so tests monkeypatch it by string
    (`herdr_routines.tick.launch_pipeline`), the same convention already used for
    `ensure_repo` (see the autouse fixtures in tests/test_tick.py)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return 124, "", str(e)
    return proc.returncode, proc.stdout, proc.stderr


def pipeline_deadline_epoch(job: Job, now: datetime) -> int:
    """The run's wall-clock deadline: launch time + `deadline_ms`. Computed in code and
    handed to the orchestrator verbatim (`DEADLINE_EPOCH`). It used to be computed by the
    orchestrator model itself, and a fallback model once wrote a timestamp a year in the
    past, so the watchdog reaped a live run 15 min in (2026-09-28)."""
    return int(now.timestamp()) + job.require_deadline_ms() // 1000


def _build_pipeline_launch_argv(
    job: Job, *, run_id: str, report_path: Path, unit_name: str, deadline_epoch: int
) -> list[str]:
    deadline_ms = job.require_deadline_ms()
    prompt_file = job.require_prompt_file()
    runtime_max_sec = (deadline_ms + PIPELINE_UNIT_MARGIN_MS) // 1000
    script = job.repo / "scripts" / "pipeline-launch.sh"
    argv = [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit={unit_name}",
        "-p",
        f"RuntimeMaxSec={runtime_max_sec}",
        "/bin/bash",
        str(script),
        "--run-id",
        run_id,
        "--repo-parent",
        str(job.repo),
        "--report",
        str(report_path),
        "--agent-name",
        job.agent_name,
        "--agent-kind",
        job.agent_kind,
        "--prompt-file",
        str(job.repo / prompt_file),
        "--wait-timeout-ms",
        str(deadline_ms),
        "--deadline-epoch",
        str(deadline_epoch),
    ]
    if job.model:
        argv += ["--model", job.model]
    # Same default the routine-job watchdog uses (tick.py's other call site, runner.py's
    # DEFAULT_FAILURE_MARKERS) — the launcher polls the orchestrator's visible screen for
    # this so a quota-exhaustion wedge fails fast instead of burning the full deadline.
    for marker in job.failure_markers or ("Free usage exceeded",):
        argv += ["--failure-marker", marker]
    return argv


def _process_pipeline_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    open_run = _open_pipeline_run(history_path, job.name)
    if open_run is not None:
        if _pipeline_run_is_live(client, job, history_path, now=now):
            return f"{job.name}: skipped (already running)", False

        if open_run.run_id is None:
            raise RuntimeError(f"{job.name}: open pipeline run record has no run_id")
        bare_run_id = _bare_pipeline_run_id(job.name, open_run.run_id)
        report_path = pipeline_report_path(bare_run_id)
        report_text = ""
        if report_path.exists():
            try:
                report_text = report_path.read_text()
            except OSError:
                report_text = ""

        if report_text.strip():
            state, reason = _classify_pipeline_outcome(report_text)
            # design.md G-17: a report that says "done" is only trusted if the run
            # actually ran its stages in independent sessions. A faked/incomplete
            # `stage_sessions` downgrades it to failed — the PR (if any) is
            # single-session work and needs a human, not a green checkmark.
            if state == "done":
                independence_issue = _pipeline_stage_independence_issue(bare_run_id)
                if independence_issue is not None:
                    state, reason = "failed", "stage_independence_unverified"
                    log.warning(
                        "%s: run %s reported done but %s",
                        job.name,
                        bare_run_id,
                        independence_issue,
                    )
            extra = HistoryExtra(pipeline_run_id=bare_run_id, report=str(report_path))
            if reason is not None:
                extra["reason"] = reason
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state=state,
                    run_id=open_run.run_id,
                    extra=extra,
                ),
            )
            if state == "done":
                if _notify_gate(job, "success"):
                    _notify(client, f"herdr-routines: {job.name} done", sound="done")
                return f"{job.name}: done", False

            # `skipped` is terminal and quiet: prepare already wrote the report and no
            # agent ever ran, so there is nothing to retry and nothing to page anyone
            # about (issue 052/054 phase A). Without this early return it would fall
            # through to the failure notify below — a Telegram request at 05:00 for a
            # night where the backlog was simply empty.
            if state == "skipped":
                return f"{job.name}: skipped", False

            # No run-level fallback_model relaunch, unlike the routine-job path: a quota
            # wedge already resumed the stage on `StageSpec.fallback_model` inside
            # `pipeline_run`, so a `quota_exhausted` report means that resume was not
            # possible (the session id could not be read). A relaunch could not change the stage models (they are pinned in
            # `pipeline_stages.py`, and the launcher ignores `--model`), so it only redid
            # the night from scratch on the same exhausted Zen models.
            if _notify_gate(job, "failure"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} failed",
                    body=reason or "unknown",
                    sound="request",
                )
            return f"{job.name}: {state} ({reason})", True

        # No usable report yet. This is deliberately *not* "agent gone => failed": the
        # window between systemd-run returning and the launcher script's `herdr agent
        # start` landing (workspace create + a settle sleep) looks identical from here —
        # a naive check would kill a perfectly healthy run's history record on the very
        # next tick. Only a report (handled above), a host reboot (handled here), or a
        # genuinely stale deadline (handled below) is allowed to close this run out.
        if _pipeline_host_rebooted_mid_run(bare_run_id):
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="failed",
                    run_id=open_run.run_id,
                    extra=HistoryExtra(
                        reason="host_rebooted", pipeline_run_id=bare_run_id
                    ),
                ),
            )
            if _notify_gate(job, "failure"):
                _notify(
                    client,
                    f"herdr-routines: {job.name} failed",
                    body="host rebooted mid-run",
                    sound="request",
                )
            return f"{job.name}: failed (host_rebooted)", True

        stale = find_stale_running(
            history_path,
            job.name,
            timeout_ms=(job.deadline_ms or 0) + PIPELINE_RECONCILE_GRACE_MS,
            now=now,
        )
        if stale is None:
            return f"{job.name}: in flight ({open_run.run_id})", False

        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=open_run.run_id,
                extra=HistoryExtra(reason="no_report", pipeline_run_id=bare_run_id),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="no_report",
                sound="request",
            )
        return f"{job.name}: failed (no_report)", True

    if _live_agent_exists(client, job):
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="skipped",
                extra=HistoryExtra(reason="agent_name_live"),
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_scheduled_terminal_run(history_path, job.name)
    registered_at = first_seen_at(history_path, job.name) or now
    result = decide(
        cron=job.cron,
        timezone=job.timezone,
        catch_up_minutes=job.catch_up_minutes,  # fixed PIPELINE_CATCH_UP_MINUTES, config-enforced
        now=now,
        last_terminal=last,
        job_registered_at=registered_at,
    )

    if result.decision == Decision.NOT_DUE:
        return f"{job.name}: not due", False

    if result.decision == Decision.MISSED:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra=HistoryExtra(
                    reason="outside_catch_up_window",
                    scheduled_for=result.occurrence.isoformat(),
                ),
            ),
        )
        # Unlike a routine job's ~2h default grace, PIPELINE_CATCH_UP_MINUTES is tight
        # enough that a starved night (tick busy dispatching other jobs) is a real,
        # not just theoretical, way to land here — silently losing a whole night's run
        # is worse for a nightly job than for a 5-min routine, so surface it whenever
        # on_missed asks for that (the deploy example sets on_missed: notify).
        if job.on_missed == "notify":
            _notify(
                client,
                f"herdr-routines: {job.name} missed",
                body="outside catch-up window",
                sound="request",
            )
        return f"{job.name}: missed", False

    # Decision.RUN
    return dispatch_occurrence(
        job,
        history_path,
        Occurrence(
            make_run_id(job.name, result.occurrence),
            HistoryExtra(
                scheduled_for=result.occurrence.isoformat(),
                late_seconds=result.late_seconds,
            ),
        ),
        client=client,
        now=now,
    )


def _launch_pipeline_run(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    bare_run_id: str,
    running_extra: HistoryExtra,
) -> tuple[str, bool]:
    """Sync the repo, launch the detached orchestrator unit, and record the outcome.
    `running_extra` goes into the `running` record's `extra` (scheduled_for/
    late_seconds)."""
    report_path = pipeline_report_path(bare_run_id)
    unit_name = f"{PIPELINE_UNIT_PREFIX}{bare_run_id}"
    # Recorded in the `running` record below: the watchdog reads it from history.
    deadline_epoch = pipeline_deadline_epoch(job, now)

    try:
        ensure_repo(job)
    except RuntimeError as e:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=_with_trigger(
                    running_extra,
                    HistoryExtra(reason="repo_sync_failed", error=str(e)),
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="repo_sync_failed",
                sound="request",
            )
        return f"{job.name}: failed (repo_sync_failed)", True

    argv = _build_pipeline_launch_argv(
        job,
        run_id=bare_run_id,
        report_path=report_path,
        unit_name=unit_name,
        deadline_epoch=deadline_epoch,
    )
    # Launch first, record second: writing the `running` record before confirming the
    # launch succeeded would wedge this job for the full deadline if `systemd-run` failed
    # or tick died in between (issue 026 AC #2).
    rc, _out, err = launch_pipeline(argv)
    if rc != 0:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=_with_trigger(
                    running_extra,
                    HistoryExtra(
                        reason="launch_failed", rc=rc, error=err.strip()[:2000]
                    ),
                ),
            ),
        )
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body="launch_failed",
                sound="request",
            )
        return f"{job.name}: failed (launch_failed)", True

    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=run_id,
            extra=HistoryExtra(
                **running_extra,
                pipeline_run_id=bare_run_id,
                report=str(report_path),
                unit=unit_name,
                deadline_epoch=deadline_epoch,
            ),
        ),
    )
    return f"{job.name}: dispatched ({unit_name})", False


def _notify(
    client: HerdrClient, title: str, *, body: str | None = None, sound: str = "none"
) -> None:
    """Best-effort: a notification failure (e.g. Herdr server unreachable) must not abort the
    tick — see run_tick's isolation contract above."""
    try:
        client.notification_show(title, body=body, sound=sound)
    except HerdrCliError as e:
        log.warning("%s: notification failed: %s", title, e)


def _notify_gate(job: Job, kind: str) -> bool:
    """Whether a `job.notify_policy`-governed notification of *kind* should actually be
    sent (issue 009). *kind* is one of:

    - "failure": the job's own dispatch ended in a terminal failure this tick (setup
      errors, a gate whose fix agent could not resolve it, a max-attempts cap reached).
    - "finding": the job completed without a terminal failure but surfaced something
      worth knowing about even so — a gated job's checks found something and the fix
      agent resolved it, or a plain routine needed its `fallback_model` retry.
    - "success": a clean terminal outcome with nothing notable (checks passed on the
      first try, a plain routine finished with its primary model, a pr-target gate
      enumerated nothing eligible).
    - "progress": a sub-step inside a still-in-progress job (e.g. one PR skipped
      mid-loop while a pr-target gate keeps dispatching others) — never the job's own
      once-per-tick terminal notification.

    The four policy values form a strict hierarchy, each a subset of the last:
    "on-failure" (notify only on "failure") subset of "on-finding" (adds "finding")
    subset of "terminal" (default — adds "success", i.e. every terminal outcome, which
    is exactly the pre-issue-009 behavior: one notification per run, done or failed,
    never a mid-run progress ping — see the issue's acceptance criteria: "defaulting to
    a single terminal-state notification") subset of "always" (adds "progress" too —
    the full pre-issue-009-job per-tick pinginess some jobs opted into but the issue
    says an unattended overnight run should no longer get by default).

    Deliberately excludes `on_missed`-triggered notifications: that is its own
    pre-existing per-job opt-in (config.VALID_ON_MISSED) and stays orthogonal here so
    existing jobs.d/ configs that already set `on_missed: notify` keep exactly their
    current behavior regardless of `notify_policy`."""
    if job.notify_policy == "always":
        return True
    if job.notify_policy == "terminal":
        return kind != "progress"
    if job.notify_policy == "on-finding":
        return kind in ("failure", "finding")
    if job.notify_policy == "on-failure":
        return kind == "failure"
    assert_never(job.notify_policy)


# What follows `rt-<job>` in a worker agent name: auto_fix's `build_worker_agent_name`
# (`-pr<n>` or `-pr<n>-<tail>`) and `build_gate_worker_agent_name` (`-gate-<hash>`).
_WORKER_NAME_SUFFIX_RE = re.compile(r"-pr\d+(?:-[0-9a-z]+)?|-gate-[0-9a-f]+")


def _is_job_agent(name: str, job: Job) -> bool:
    """`name` is this job's own agent or one of the per-PR / gate workers it spawned.
    Anchored on the worker suffix shapes, so job `babysit-prs` never claims an agent of a
    job named `babysit-prs-nightly`. Lowercased because herdr lowercases agent names."""
    name, agent = name.lower(), job.agent_name.lower()
    if name == agent:
        return True
    return name.startswith(agent) and bool(
        _WORKER_NAME_SUFFIX_RE.fullmatch(name.removeprefix(agent))
    )


def _reap_agents_of_rebooted_run(
    client: HerdrClient, job: Job, stale: HistoryRecord, *, boot_epoch: float | None
) -> list[str]:
    """Close the agents a run left behind when the host rebooted under it; return their
    names. herdr restores every pane's agent at boot, reporting `blocked`, so a run whose
    tick died with the host comes back as opencode processes nobody will ever prompt
    again — ~550 MB each, pinning 2.2 GB of the Pi's 4 GB on 2026-10-01. Per-PR worker
    names carry the run id, so the reap-on-name-collision path never sees them.

    Only for a run that started before the current boot: a stale run on a host that did
    not reboot may have a blocked agent a human is meant to inspect. Never for a root-mode
    job, whose pane is the ambient workspace. Best-effort: herdr errors are logged."""
    if job.workspace == "root" or boot_epoch is None:
        return []
    if stale.ts.timestamp() >= boot_epoch:
        return []
    try:
        agents = client.agent_panes_by_status()
    except HerdrCliError as e:
        log.warning("%s: could not list agents to reap: %s", job.name, e)
        return []
    # An audit run's agents carry its run id in their names, which `_is_job_agent`'s
    # suffix shapes do not cover; the stale record names the run, so match them exactly.
    audit_names: set[str] = set()
    if job.kind == "audit" and stale.run_id is not None:
        audit_names = {
            build_audit_agent_name(job.name, stale.run_id).lower(),
            build_gate_worker_agent_name(job.name, stale.run_id).lower(),
        }
    reaped: list[str] = []
    for name, (_status, pane_id) in sorted(agents.items()):
        if not (_is_job_agent(name, job) or name.lower() in audit_names):
            continue
        try:
            client.pane_close(pane_id)
        except HerdrCliError as e:
            log.warning(
                "%s: could not close pane %s of %s: %s", job.name, pane_id, name, e
            )
            continue
        reaped.append(name)
    if reaped:
        log.warning(
            "%s: run %s predates the last boot; closed its restored agents %s",
            job.name,
            stale.run_id,
            reaped,
        )
    return reaped


def _live_agent_exists(client: HerdrClient, job: Job) -> bool:
    """Cross-process safety net (docs/plan-v1.md §4): catches a live `rt-<name>` agent that
    survived a lost/rotated history.jsonl, which `is_currently_running` can't see since it
    reads only history. "Live" means actually still mid-run (agent_status in
    LIVE_AGENT_STATUSES), not merely registered — a finished agent stays registered under
    `herdr agent list` until its tab is closed, so name presence alone would make a recurring
    job's agent name "live" forever after its first run. Fails open on a HerdrCliError (e.g.
    server unreachable) since the history/flock check above remains the primary guard."""
    try:
        status = client.agent_statuses().get(job.agent_name)
    except HerdrCliError as e:
        log.warning("%s: could not query live agents, proceeding: %s", job.name, e)
        return False
    return status in LIVE_AGENT_STATUSES


def _pipeline_run_is_live(
    client: HerdrClient, job: Job, history_path: Path, *, now: datetime
) -> bool:
    """Is a pipeline run still in flight — the pipeline's cross-process overlap guard.

    Two checks, either sufficient:

    - a live `rt-<name>` agent, which is what this used to be and still is for any run
      that has not yet reached phase B's `pipeline-run`;
    - a `state.json` with no terminal report yet, enumerated by
      `pipeline_watchdog.find_inflight_runs` and *not* stalled per
      `pipeline_watchdog.is_stalled` (host rebooted since the last state write, or past
      deadline + grace with a quiet heartbeat), or, if stalled, still backed by a live
      systemd unit.

    The second is the load-bearing one now. Issue 056 removed the orchestrator agent, and
    that agent's name was what the old check looked up — so with no orchestrator there is
    no `rt-<name>` agent to find, the guard could never fire again, and a second night
    would launch on top of a live one. `find_inflight_runs` is literally "a `state.json`
    with no terminal report yet", which is the condition `_live_agent_exists` was
    approximating: it is written by the run itself, from the start of the run, and so
    exists for the whole window the guard needs to cover.

    The stalled filter is what stops this failing closed forever: a run hard-killed before
    it could write its report (SIGKILL, OOM, reboot) leaves a `state.json` with no report,
    and pipeline worktrees are never GC'd, so without it that directory would read as
    in-flight on every later night. It reuses the watchdog's own staleness rule rather
    than a second one, so "dead" means the same thing to both; the deadline comes from
    tick's recorded history, as the watchdog's does, never from the model-written copy.

    Stalled is only a guess at death, though: a resumed run gets a fresh unit and deadline
    but keeps the old recorded one, so a live, quiet run reads as stalled. As in the
    watchdog, a stalled run whose unit is active, or can't be queried, still counts as
    in flight; only an inactive unit makes it dead. Better to skip a night than to launch
    one on top of a live run.

    Fails open on a HerdrCliError (unreachable server), matching `_live_agent_exists`:
    the history/flock check above remains the primary guard."""
    if _live_agent_exists(client, job):
        return True
    try:
        boot_epoch = system_boot_epoch()
        heartbeat_dir = default_heartbeat_dir()
        in_flight = [
            run
            for run in find_inflight_runs(
                default_worktrees_root(),
                default_reports_dir(),
                recorded_pipeline_deadlines(history_path),
            )
            if not is_stalled(
                run, now=now, heartbeat_dir=heartbeat_dir, boot_epoch=boot_epoch
            )
            or pipeline_unit_active(run.run_id) is not False
        ]
    except (OSError, HerdrCliError) as e:
        log.warning("%s: could not enumerate in-flight pipeline runs: %s", job.name, e)
        return False
    if in_flight:
        log.info(
            "%s: a pipeline run is already in flight (%s)",
            job.name,
            ", ".join(run.run_id for run in in_flight),
        )
    return bool(in_flight)


def _pr_worker_is_live(client: HerdrClient, job_name: str, pr_number: int) -> bool:
    """Double-dispatch guard for the pr-target dispatch loop (review finding E): a PR
    still eligible on the next tick must not get a second fix worker while its first
    is still running. The dispatcher never creates a bare `build_pr_agent_name(...)`
    agent — every worker's real name also carries a run_id tail (issue 036c) — so this
    matches by prefix against every live agent instead of an exact name lookup, which
    could never find anything. Fails open on a HerdrCliError, same as
    `_live_agent_exists`."""
    prefix = build_pr_agent_name(job_name, pr_number)
    try:
        statuses = client.agent_statuses()
    except HerdrCliError as e:
        log.warning(
            "%s: could not query live agents for PR #%d: %s", job_name, pr_number, e
        )
        return False
    return any(
        status in LIVE_AGENT_STATUSES
        for name, status in statuses.items()
        if name.startswith(prefix)
    )


# -- kind: review dispatch (issue 055 Phase B) --------------------------------------
#
# A review job is a cross-repo analogue of the gated pr-target path: one batched search
# (review_scan), two re-review guards (content marker + per-target attempt budget), then
# a per-repo checkout, a read-only worktree, one review agent per PR, and a post-condition
# that re-reads the PR for the marker. The after-condition, not the worker's own settle
# status, is what decides `done` — an unverifiable review is never green.


def _process_review_job(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    """Schedule guards for a kind: review job (same shape as _process_gated_job), then
    run one review cycle. A tick dispatches up to `max_workers_per_tick` workers
    sequentially, so the running-record staleness window is scaled by that cap."""
    run_timeout_ms = job.timeout_ms * max(1, job.max_workers_per_tick)

    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=run_timeout_ms, now=now
    )
    if stale is not None:
        stale_extra = HistoryExtra(reason="stale_running_record")
        reaped = _reap_agents_of_rebooted_run(
            client, job, stale, boot_epoch=system_boot_epoch()
        )
        if reaped:
            stale_extra["reaped_agents"] = reaped
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="interrupted_unknown",
                run_id=stale.run_id,
                extra=stale_extra,
            ),
        )

    if is_currently_running(history_path, job.name, timeout_ms=run_timeout_ms, now=now):
        return f"{job.name}: skipped (already running)", False

    if _live_agent_exists(client, job):
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="skipped",
                extra=HistoryExtra(reason="agent_name_live"),
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_scheduled_terminal_run(history_path, job.name)
    registered_at = first_seen_at(history_path, job.name) or now
    result = decide(
        cron=job.cron,
        timezone=job.timezone,
        catch_up_minutes=job.catch_up_minutes,
        now=now,
        last_terminal=last,
        job_registered_at=registered_at,
    )

    if result.decision == Decision.NOT_DUE:
        return f"{job.name}: not due", False

    if result.decision == Decision.MISSED:
        extra = HistoryExtra(
            reason="outside_catch_up_window", occurrence=result.occurrence.isoformat()
        )
        if result.skipped_occurrences:
            extra["skipped_occurrences"] = result.skipped_occurrences
        append(
            history_path,
            HistoryRecord(ts=now, job=job.name, state="missed", extra=extra),
        )
        if job.on_missed == "notify":
            _notify(
                client,
                f"herdr-routines: {job.name} missed",
                body="outside catch-up window",
                sound="request",
            )
        return f"{job.name}: missed", False

    # Decision.RUN
    run_id = make_run_id(job.name, result.occurrence)

    if result.skipped_occurrences:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra=HistoryExtra(
                    reason="collapsed_earlier_occurrences",
                    skipped_occurrences=result.skipped_occurrences,
                    skipped_first=result.skipped_first.isoformat()
                    if result.skipped_first
                    else None,
                    skipped_last=result.skipped_last.isoformat()
                    if result.skipped_last
                    else None,
                ),
            ),
        )

    return dispatch_occurrence(
        job,
        history_path,
        Occurrence(
            run_id,
            HistoryExtra(
                scheduled_for=result.occurrence.isoformat(),
                late_seconds=result.late_seconds,
            ),
        ),
        client=client,
        now=now,
        trusted_review_authors=trusted_review_authors,
    )


def _review_terminal_failure(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    reason: str,
    error: str | None = None,
) -> tuple[str, bool]:
    """Record a whole-cycle failure (auth/search) as this tick's terminal outcome."""
    extra = HistoryExtra(
        gate="failed",
        target="pr",
        reason=reason,
        enumerated=0,
        candidates=0,
        dispatched=0,
        skipped=0,
        failed=0,
        repos_failed=0,
    )
    if error is not None:
        extra["error"] = error
    append(
        history_path,
        HistoryRecord(ts=now, job=job.name, state="failed", run_id=run_id, extra=extra),
    )
    if _notify_gate(job, "failure"):
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body=reason,
            sound="request",
        )
    return f"{job.name}: failed ({reason})", True


def _review_cycle(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    """Search, apply both re-review guards, cap, dispatch per repo, verify each review,
    and write the aggregate report + terminal history record.

    The review marker only counts when a trusted author posted it (the configured
    ``trusted_review_authors`` plus the authenticated gh user, who is the account the
    review worker posts with), both in the pre-dispatch guard and the post-condition."""
    gh = RealGhClient()

    try:
        login = gh.api_user()
    except Exception as e:  # noqa: BLE001
        return _review_terminal_failure(
            job,
            history_path,
            client=client,
            now=now,
            run_id=run_id,
            reason="gh_auth_missing",
            error=str(e),
        )

    trusted = effective_trusted_authors(trusted_review_authors, viewer_login=login)

    # Pre-dispatch classification is one shared helper (issue 066), also used by the
    # read-only `review-scan` CLI, so the two can never disagree on dispositions. Its
    # stage order is load-bearing: scan (None → this tick failed) → attempt budget (a
    # local history read, cheapest first) → marker guard (one `gh pr view` per
    # cap-window candidate — everything past the cap can never dispatch, so it never
    # costs an API call) → cap to job.max_workers_per_tick.
    classification = classify_review_candidates(
        gh,
        job,
        login=login,
        history_path=history_path,
        trusted_authors=trusted,
    )
    if classification is None:
        # Fail closed: an unknown search answer must never read as "nothing to review".
        return _review_terminal_failure(
            job,
            history_path,
            client=client,
            now=now,
            run_id=run_id,
            reason="search_failed",
        )

    # Pre-dispatch rows come from the helper; post-dispatch dispositions
    # (repo_info_failed, clone_failed, dispatched) are appended below.
    rows: list[tuple[Candidate, str]] = list(classification.rows)
    to_dispatch = classification.dispatchable

    by_repo: dict[str, list[Candidate]] = {}
    for cand in to_dispatch:
        by_repo.setdefault(cand.repo, []).append(cand)

    repo_info_cache: dict[str, dict[str, object] | None] = {}
    repos_failed = 0
    dispatched_count = 0
    any_failed = False
    for repo, cands in sorted(by_repo.items()):
        owner, _, repo_name = repo.partition("/")
        if repo not in repo_info_cache:
            repo_info_cache[repo] = gh.repo_info(owner=owner, repo=repo_name)
        info = repo_info_cache[repo]
        if info is None:
            repos_failed += 1
            any_failed = True
            for cand in cands:
                rows.append((cand, "repo_info_failed"))
            continue

        clone_url = info.get("clone_url")
        default_branch = info.get("default_branch")
        checkout = Path(job.repo) / owner / repo_name
        try:
            _ensure_checkout(
                clone_url if isinstance(clone_url, str) else None,
                checkout,
                base=default_branch if isinstance(default_branch, str) else "main",
            )
        except (RuntimeError, OSError) as e:
            # Isolate: only this repo's targets fail; other repos still dispatch.
            repos_failed += 1
            any_failed = True
            log.warning("%s: review checkout failed for %s: %s", job.name, repo, e)
            for cand in cands:
                rows.append((cand, "clone_failed"))
            continue

        for cand in cands:
            disposition = _dispatch_review_target(
                job,
                cand,
                checkout,
                gh=gh,
                client=client,
                history_path=history_path,
                now=now,
                run_id=run_id,
                trusted_authors=trusted,
            )
            rows.append((cand, disposition))
            if disposition == "dispatched":
                dispatched_count += 1
            elif disposition in REVIEW_FAILURE_DISPOSITIONS:
                any_failed = True

    failed_count = sum(1 for _, d in rows if d in REVIEW_FAILURE_DISPOSITIONS)
    skipped_count = len(rows) - dispatched_count - failed_count

    try:
        reports_dir = default_reports_dir()
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"review-{run_id}.md"
        report_path.write_text(
            render_review_report(
                job_name=job.name,
                run_id=run_id,
                now=now,
                enumerated=classification.enumerated,
                candidate_count=classification.candidate_count,
                repos_failed=repos_failed,
                rows=rows,
                truncated_modes=classification.truncated_modes,
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning("%s: could not write review report: %s", job.name, e)

    summary = (
        f"{job.name}: done (enumerated={classification.enumerated}, "
        f"candidates={classification.candidate_count}, dispatched={dispatched_count}, "
        f"skipped={skipped_count}, failed={failed_count}, "
        f"repos_failed={repos_failed})"
    )

    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="failed" if any_failed else "done",
            run_id=run_id,
            extra=HistoryExtra(
                gate="failed" if any_failed else "passed",
                target="pr",
                enumerated=classification.enumerated,
                candidates=classification.candidate_count,
                dispatched=dispatched_count,
                skipped=skipped_count,
                failed=failed_count,
                repos_failed=repos_failed,
            ),
        ),
    )

    if any_failed:
        if _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=(
                    f"{dispatched_count} dispatched, {failed_count} failed, "
                    f"{skipped_count} skipped"
                ),
                sound="request",
            )
        return summary, True

    if _notify_gate(job, "finding" if dispatched_count > 0 else "success"):
        _notify(client, f"herdr-routines: {job.name} done", sound="done")
    return summary, False


def _live_review_agent_exists(
    client: HerdrClient, job_name: str, target_key: str
) -> bool:
    """Double-dispatch guard for one review target, matching by the shared
    ``build_review_agent_prefix`` (`rt-<job>-rv<h6>`) so two ticks five minutes apart
    can never dispatch the same PR twice (issue 036c's lesson). Fails open on a
    HerdrCliError, same as `_live_agent_exists`."""
    prefix = build_review_agent_prefix(job_name, target_key).lower()
    try:
        statuses = client.agent_statuses()
    except HerdrCliError as e:
        log.warning(
            "%s: could not query live agents for %s: %s", job_name, target_key, e
        )
        return False
    return any(
        status in LIVE_AGENT_STATUSES
        for name, status in statuses.items()
        if name.lower().startswith(prefix)
    )


def _review_target_record(
    *,
    job: Job,
    cand: Candidate,
    run_id: str,
    now: datetime,
    state: HistoryState,
    attempt: int,
    agent_name: str | None,
    pane_id: str | None,
    report_path: Path | None,
    verified: bool | None,
    reason: str | None = None,
    error: str | None = None,
) -> HistoryRecord:
    """One per-target history record, shaped per issue 055's "History and report"."""
    extra = HistoryExtra(
        target_key=cand.target_key,
        repo=cand.repo,
        pr_number=cand.number,
        headRefName=cand.head_ref,
        select_mode=cand.select_mode,
        attempt=attempt,
        agent=agent_name,
        pane_id=pane_id,
        report_path=str(report_path) if report_path is not None else None,
        verified=verified,
        target="pr",
    )
    if reason is not None:
        extra["reason"] = reason
    if error is not None:
        extra["error"] = error
    return HistoryRecord(ts=now, job=job.name, state=state, run_id=run_id, extra=extra)


def _dispatch_review_target(
    job: Job,
    cand: Candidate,
    checkout: Path,
    *,
    gh: RealGhClient,
    client: HerdrClient,
    history_path: Path,
    now: datetime,
    run_id: str,
    trusted_authors: frozenset[str],
) -> str:
    """Fetch the PR head ref, create/reuse a read-only worktree, run one review agent,
    then verify a trusted author posted the marker. Always removes the worktree after
    settle.

    Returns the disposition string recorded in the aggregate report."""
    from herdr_routines.runner import (
        _capture_visible_tail,
        _close_run_pane,
        _prompt_with_watchdog,
        _wait_for_agent_ready,
        substitute_prompt,
    )

    # Real clock, not the tick's `now`: the gate compares it with GitHub's timestamps.
    dispatch_started = datetime.now(UTC)
    attempt = attempt_count_for_target(history_path, job.name, cand.target_key)
    h6 = review_target_hash(job.name, cand.target_key)
    report_path = default_reports_dir() / f"auto-review-{run_id}-{h6}.md"
    agent_name = build_review_agent_name(job.name, cand.target_key, run_id)

    if _live_review_agent_exists(client, job.name, cand.target_key):
        append(
            history_path,
            _review_target_record(
                job=job,
                cand=cand,
                run_id=run_id,
                now=now,
                state="skipped",
                attempt=attempt,
                agent_name=agent_name,
                pane_id=None,
                report_path=None,
                verified=None,
                reason="agent_live",
            ),
        )
        return "agent_live"

    wt_path = checkout / ".worktrees" / f"review-pr{cand.number}"

    # A review needs no branch, so the worktree is a detached HEAD at origin/<headRef>:
    # it never creates a local branch in the shared checkout and never contends with
    # another worktree (e.g. the orchestrator's) that has the head branch checked out.
    # Our own clean worktree is reused (moved to the fresh head); a dirty one is
    # force-removed and re-added.
    if wt_path.exists():
        reusable, why = _worktree_reuse_check(wt_path)
        if not reusable:
            log.info(
                "%s: re-adding stale review worktree %s (%s)", job.name, wt_path, why
            )
            _cleanup_worktree(checkout, wt_path)

    head = f"origin/{cand.head_ref}"
    try:
        fetch = subprocess.run(
            ["git", "-C", str(checkout), "fetch", "origin", cand.head_ref],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if fetch.returncode != 0:
            raise RuntimeError(f"git fetch failed: {fetch.stderr.strip()}")
        if wt_path.exists():
            step = "git checkout"
            argv = ["git", "-C", str(wt_path), "checkout", "--detach", head]
        else:
            step = "git worktree add"
            argv = [
                "git",
                "-C",
                str(checkout),
                "worktree",
                "add",
                "--detach",
                str(wt_path),
                head,
            ]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(f"{step} failed: {proc.stderr.strip()}")
    except Exception as e:  # noqa: BLE001
        _cleanup_worktree(checkout, wt_path)
        append(
            history_path,
            _review_target_record(
                job=job,
                cand=cand,
                run_id=run_id,
                now=now,
                state="failed",
                attempt=attempt,
                agent_name=None,
                pane_id=None,
                report_path=None,
                verified=False,
                reason="worktree_failed",
                error=str(e),
            ),
        )
        return "worktree_failed"

    prompt_text = job.prompt or build_review_prompt(
        job_name=job.name,
        target_key=cand.target_key,
        url=cand.url,
        report_path=str(report_path),
    )
    prompt_text = substitute_prompt(
        prompt_text, report_path=report_path, job_name=job.name, run_id=run_id
    )

    pane_id: str | None = None
    try:
        pane_id = client.tab_create(cwd=str(wt_path), label=agent_name)
        client.agent_start(
            name=agent_name,
            kind=job.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=job.model,
        )
    except (HerdrCliError, OSError) as e:
        if pane_id is not None:
            try:
                client.pane_close(pane_id)
            except Exception:  # noqa: BLE001, S110
                pass
        _cleanup_worktree(checkout, wt_path)
        append(
            history_path,
            _review_target_record(
                job=job,
                cand=cand,
                run_id=run_id,
                now=now,
                state="failed",
                attempt=attempt,
                agent_name=agent_name,
                pane_id=pane_id,
                report_path=None,
                verified=False,
                reason="agent_start_failed",
                error=str(e),
            ),
        )
        return "agent_start_failed"

    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        _cleanup_worktree(checkout, wt_path)
        append(
            history_path,
            _review_target_record(
                job=job,
                cand=cand,
                run_id=run_id,
                now=now,
                state="failed",
                attempt=attempt,
                agent_name=agent_name,
                pane_id=pane_id,
                report_path=None,
                verified=False,
                reason="agent_not_interactive",
                error=last_error,
            ),
        )
        return "agent_not_interactive"

    try:
        _prompt_with_watchdog(
            client,
            job_name=agent_name,
            target=agent_name,
            text=prompt_text,
            timeout_ms=job.timeout_ms,
            markers=job.failure_markers
            if job.failure_markers is not None
            else ("Free usage exceeded",),
            prompt_text=prompt_text,
        )
    except Exception as e:  # noqa: BLE001
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        _cleanup_worktree(checkout, wt_path)
        append(
            history_path,
            _review_target_record(
                job=job,
                cand=cand,
                run_id=run_id,
                now=now,
                state="failed",
                attempt=attempt,
                agent_name=agent_name,
                pane_id=pane_id,
                report_path=None,
                verified=False,
                reason="agent_prompt_failed",
                error=str(e),
            ),
        )
        return "agent_prompt_failed"

    _capture_visible_tail(
        client, agent_name, reports_dir=report_path.parent, run_id=run_id
    )
    _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
    # A review retains no branch: remove the worktree on both success and failure.
    _cleanup_worktree(checkout, wt_path)

    # Post-condition: the shared `review_posted` verify gate (issue 071) re-reads the PR
    # and requires a trusted author's marker posted since this dispatch started. Its
    # failure reasons are the dispositions this path has always recorded.
    verdict = verify.review_posted(
        VerifyContext(
            job=job,
            run_id=run_id,
            started_at=dispatch_started,
            report_path=report_path,
            gh=gh,
            trusted_review_authors=(),
            target=VerifyTarget(cand.owner, cand.repo_name, cand.number),
            trusted_authors=trusted_authors,
            marker=review_marker(job.name, cand.target_key),
        )
    )
    if verdict.passed:
        disposition, verified, reason = "dispatched", True, None
    else:
        disposition = reason = verdict.reason or "review_not_posted"
        verified = False

    append(
        history_path,
        _review_target_record(
            job=job,
            cand=cand,
            run_id=run_id,
            now=now,
            state="done" if verified else "failed",
            attempt=attempt,
            agent_name=agent_name,
            pane_id=pane_id,
            report_path=report_path,
            verified=verified,
            reason=reason,
        ),
    )
    return disposition
