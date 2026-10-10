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
from datetime import datetime
from pathlib import Path
from typing import Any

from logger import get_logger

from herdr_routines.auto_fix import (
    EligiblePR,
    GateCheck,
    PRInfo,
    RealGhClient,
    attempt_count_for_gate_branch,
    attempt_count_for_pr,
    build_audit_agent_name,
    build_audit_fix_prompt,
    build_audit_prompt,
    build_base_fix_prompt,
    build_fix_prompt,
    build_gate_worker_agent_name,
    build_pr_agent_name,
    build_worker_agent_name,
    fetch_failing_checks,
    fetch_thread_bodies,
    is_eligible,
    list_open_prs,
    repo_owner_and_name,
    run_checks,
)
from herdr_routines.config import Job, RoutinesConfig
from herdr_routines.findings import (
    Finding,
    Ledger,
    apply_diff,
    diff_findings,
    dispatch_pool,
    ledger_path,
    load_findings_manifest_full,
    load_ledger,
    prune_ledger,
    save_ledger,
)
from herdr_routines.herdr import LIVE_AGENT_STATUSES, HerdrClient, HerdrCliError
from herdr_routines.history import (
    TERMINAL_STATES,
    HistoryRecord,
    append,
    find_stale_running,
    first_seen_at,
    has_ever_been_seen,
    is_currently_running,
    last_terminal_run,
    maybe_rotate,
    read_job,
)
from herdr_routines.pipeline_watchdog import (
    default_heartbeat_dir,
    default_worktrees_root,
    find_inflight_runs,
    host_rebooted_after_state_write,
    is_stalled,
    pipeline_state_json_path,
    recorded_pipeline_deadlines,
    system_boot_epoch,
    validate_stage_sessions,
)
from herdr_routines.repos import ensure_repo
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
from herdr_routines.tmp_hygiene import reap_tmp

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


def run_tick(
    config: RoutinesConfig, history_path: Path, *, client: HerdrClient, now: datetime
) -> TickOutcome:
    """Process every enabled job once. Does not raise for individual job failures; each is
    captured in its own history record so one bad job cannot prevent the rest from being
    evaluated."""
    # /tmp hygiene preamble (issue 027): best-effort reap under tick.lock, never fails
    # the tick. Runs before job dispatch so leaked agent .so files and pytest artifacts
    # are cleaned before any new agent spawn.
    try:
        reap_tmp()
    except Exception as e:  # noqa: BLE001
        log.warning("tmp hygiene reap failed (continuing): %s", e)

    summaries: list[str] = []
    any_job_failed = False
    for job in config.jobs:
        if not job.enabled:
            continue
        summary, failed = _process_job(job, history_path, client=client, now=now)
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
    assert job.audit is not None
    run_timeout_ms = job.audit.timeout_ms + job.timeout_ms

    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=run_timeout_ms, now=now
    )
    if stale is not None:
        stale_extra: dict[str, Any] = {"reason": "stale_running_record"}
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
                extra={"reason": "agent_name_live"},
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_terminal_run(history_path, job.name)
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
        assert result.occurrence is not None
        extra: dict[str, Any] = {
            "reason": "outside_catch_up_window",
            "occurrence": result.occurrence.isoformat(),
        }
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

    assert result.occurrence is not None
    run_id = make_run_id(job.name, result.occurrence)

    if result.skipped_occurrences:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra={
                    "reason": "collapsed_earlier_occurrences",
                    "skipped_occurrences": result.skipped_occurrences,
                    "skipped_first": result.skipped_first.isoformat()
                    if result.skipped_first
                    else None,
                    "skipped_last": result.skipped_last.isoformat()
                    if result.skipped_last
                    else None,
                },
            ),
        )

    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=run_id,
            extra={
                "scheduled_for": result.occurrence.isoformat(),
                "late_seconds": result.late_seconds,
            },
        ),
    )

    return _audit_cycle(job, history_path, client=client, now=now, run_id=run_id)


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
    manifest: Any,
    diff: Any,
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
    fix_report: Path
    ledger: Path

    @classmethod
    def for_run(cls, job_name: str, run_id: str) -> _AuditPaths:
        reports = default_reports_dir()
        return cls(
            manifest=reports / f"{run_id}-findings.json",
            report=reports / f"{run_id}.md",
            audit_report=reports / f"{run_id}-audit.md",
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


def _record_audit_failure(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    paths: _AuditPaths,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    if _notify_gate(job, _AUDIT_GATE_NOTIFY_KIND["failed"]):
        _notify(client, f"herdr-routines: {job.name} failed", body=reason)
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="failed",
            run_id=run_id,
            extra={
                "gate": "failed",
                "target": job.target or "base",
                "reason": reason,
                "manifest_path": str(paths.manifest),
                "ledger_path": str(paths.ledger),
                "report_path": str(paths.report),
                "report_written": False,
                **(extra or {}),
            },
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
        reason=None if settled_status in ("idle", "done") else "agent_prompt_failed",
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


def run_audit_now(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime, run_id: str
) -> tuple[str, bool]:
    """`herdr-routines run <audit job>`: one audit cycle outside the schedule, recorded
    like a cron fire. The caller holds the tick lock."""
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=run_id,
            extra={"trigger": "manual"},
        ),
    )
    return _audit_cycle(job, history_path, client=client, now=now, run_id=run_id)


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
    assert job.audit is not None

    def _fail(reason: str, extra: dict[str, Any]) -> tuple[str, bool]:
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
        return _fail(reason, {"error": str(e)})

    try:
        paths.report.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _fail("report_dir_creation_failed", {"error": str(e)})

    wt_path = Path(job.repo) / ".worktrees" / f"audit-{run_id}"
    try:
        _add_worktree(job.repo, wt_path, job.base)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return _fail("worktree_creation_failed", {"error": str(e)})

    try:
        if job.audit.command is not None:
            command = substitute_prompt(
                job.audit.command,
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
                        kind="command", command=command, timeout_ms=job.audit.timeout_ms
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
                    {"exit_code": exit_code, "audit_output_path": str(output_path)},
                )
            return None

        assert job.audit.skill is not None
        agent_name = build_audit_agent_name(job.name, run_id)
        prompt_text = job.prompt or build_audit_prompt(
            skill=job.audit.skill,
            base=job.base,
            report_path=str(paths.audit_report),
            findings_path=str(paths.manifest),
        )
        prompt_text = substitute_prompt(
            prompt_text,
            report_path=paths.audit_report,
            job_name=job.name,
            run_id=run_id,
            findings_path=paths.manifest,
        )
        run = _run_agent_in_worktree(
            job,
            client,
            agent_name=agent_name,
            wt_path=wt_path,
            prompt_text=prompt_text,
            timeout_ms=job.audit.timeout_ms,
            tail_id=f"{run_id}-audit",
        )
        if run.reason is not None:
            return _fail(
                run.reason,
                {
                    "phase": "audit",
                    "agent_name": agent_name,
                    "error": run.error,
                    "pane_id": run.pane_id,
                    "final_agent_status": run.final_agent_status,
                },
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
    assert job.audit is not None
    wt_path = Path(job.repo) / ".worktrees" / f"audit-fix-{run_id}"
    try:
        _add_worktree(job.repo, wt_path, job.base)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return _AgentRun(reason="worktree_creation_failed", error=str(e))

    try:
        recheck = (
            f"`{job.audit.command}` (it writes the findings manifest)"
            if job.audit.command is not None
            else f"run the `{job.audit.skill}` skill and read its findings"
        )
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
    assert job.audit is not None

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

    extra: dict[str, Any] = {
        "gate": gate,
        "target": job.target or "base",
        "check": check,
        "manifest_path": str(paths.manifest),
        "ledger_path": str(paths.ledger),
        "report_path": str(paths.report),
        "report_written": True,
        "duplicate_ids": list(manifest.duplicate_ids),
        "findings_total": len(manifest.findings),
        "new": len(diff.new),
        "regressed": len(diff.regressed),
        "unchanged": len(diff.unchanged),
        "resolved": len(diff.resolved),
        "dispatched": len(dispatched_ids),
        "suppressed": len(suppressed_ids),
        "adopted": adopted,
        "dispatched_ids": list(dispatched_ids),
        "suppressed_ids": list(suppressed_ids[:_AUDIT_SUPPRESSED_ID_CAP]),
    }

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
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    """Process a gated job: run checks, dispatch fix agent on failure.
    Handles both pr and base targets."""
    assert job.checks is not None
    assert job.target is not None

    # Standard guards (same as _process_job for regular jobs).
    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=job.timeout_ms, now=now
    )
    if stale is not None:
        stale_extra: dict[str, Any] = {"reason": "stale_running_record"}
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
                extra={"reason": "agent_name_live"},
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_terminal_run(history_path, job.name)
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
        assert result.occurrence is not None
        extra: dict[str, Any] = {
            "reason": "outside_catch_up_window",
            "occurrence": result.occurrence.isoformat(),
        }
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
    assert result.occurrence is not None
    run_id = make_run_id(job.name, result.occurrence)

    if result.skipped_occurrences:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra={
                    "reason": "collapsed_earlier_occurrences",
                    "skipped_occurrences": result.skipped_occurrences,
                    "skipped_first": result.skipped_first.isoformat()
                    if result.skipped_first
                    else None,
                    "skipped_last": result.skipped_last.isoformat()
                    if result.skipped_last
                    else None,
                },
            ),
        )

    # Record running state
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=run_id,
            extra={
                "scheduled_for": result.occurrence.isoformat(),
                "late_seconds": result.late_seconds,
            },
        ),
    )

    if job.target == "pr":
        return _process_pr_target(
            job, history_path, client=client, now=now, run_id=run_id
        )
    else:
        return _process_base_target(
            job, history_path, client=client, now=now, run_id=run_id
        )


def _process_pr_target(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime, run_id: str
) -> tuple[str, bool]:
    """PR-target gate: enumerate eligible PRs, dispatch fix workers per flagged PR."""
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
                extra={"reason": reason, "error": str(e)},
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
                extra={"reason": "repo_detection_failed", "error": str(e)},
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
                extra={"reason": "gh_auth_missing", "error": str(e)},
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

    open_prs = list_open_prs(
        gh, owner=owner, repo=repo_name, branch_prefix="auto/", author=author
    )

    eligible: list[EligiblePR] = []
    for pr_info in open_prs:
        elig = is_eligible(gh, owner=owner, repo=repo_name, pr=pr_info)
        if elig is not None:
            eligible.append(elig)

    eligible.sort(key=lambda e: e.pr.number)
    dispatched = eligible[: job.max_workers_per_tick]
    skipped_over_cap = len(eligible) - len(dispatched)

    any_failed = False
    dispatched_count = 0
    skipped_count = skipped_over_cap
    for elig_pr in dispatched:
        attempt = attempt_count_for_pr(history_path, job.name, elig_pr.pr.number)
        if attempt >= job.max_attempts_per_target:
            append(
                history_path,
                HistoryRecord(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    extra={
                        "reason": "max_attempts_exceeded",
                        "pr_number": elig_pr.pr.number,
                        "attempt": attempt,
                    },
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

        failing_checks = fetch_failing_checks(
            gh, owner=owner, repo=repo_name, number=elig_pr.pr.number
        )
        thread_bodies = fetch_thread_bodies(
            gh, owner=owner, repo=repo_name, number=elig_pr.pr.number
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

        agent_name = build_worker_agent_name(job.name, elig_pr.pr.number, run_id)
        extra_record: dict[str, Any] = {
            "pr_number": elig_pr.pr.number,
            "headRefName": elig_pr.pr.head_ref,
            "attempt": attempt,
            "eligible_reason": elig_pr.reason,
            "fix_worker_agent": agent_name,
            "pane_id": worker_outcome.get("pane_id"),
            "report_path": worker_outcome.get("report_path"),
            "report_written": worker_outcome.get("report_written", False),
            "final_agent_status": worker_outcome.get("final_agent_status"),
            "target": "pr",
        }
        if worker_outcome.get("reason") is not None:
            extra_record["reason"] = worker_outcome["reason"]
        if worker_outcome.get("error") is not None:
            extra_record["error"] = worker_outcome["error"]
        if worker_outcome.get("state") in ("failed", "interrupted_unknown"):
            log.warning(
                "%s: PR #%d dispatch failed: reason=%s error=%s",
                job.name,
                elig_pr.pr.number,
                worker_outcome.get("reason"),
                worker_outcome.get("error"),
            )

        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state=worker_outcome.get("state", "failed"),
                run_id=run_id,
                extra=extra_record,
            ),
        )

        if worker_outcome.get("state") in ("failed", "interrupted_unknown"):
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
            f"- **Skipped (attempts)**: {skipped_count - skipped_over_cap}",
            "",
        ]
        for elig_pr in dispatched:
            report_lines.append(f"- PR #{elig_pr.pr.number}: {elig_pr.reason}")
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
            extra={
                "gate": "passed" if not any_failed else "failed",
                "target": "pr",
                "enumerated": len(open_prs),
                "eligible": len(eligible),
                "dispatched": dispatched_count,
                "skipped": skipped_count,
            },
        ),
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

    assert job.checks is not None
    gate_branch = build_branch_name(job.name, run_id)

    attempt = attempt_count_for_gate_branch(history_path, job.name, gate_branch)
    if attempt >= job.max_attempts_per_target:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="skipped",
                run_id=run_id,
                extra={
                    "reason": "max_attempts_exceeded",
                    "gate_branch": gate_branch,
                    "attempt": attempt,
                },
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
                extra={
                    "gate": "failed",
                    "reason": reason,
                    "error": str(e),
                    "target": "base",
                },
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
                extra={
                    "gate": "failed",
                    "reason": "worktree_creation_failed",
                    "error": str(e),
                    "target": "base",
                    "gate_branch": gate_branch,
                },
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

    gate_outcome = run_checks(job.checks, cwd=str(wt_path))  # type: ignore[arg-type]

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
                extra={
                    "gate": "passed",
                    "target": "base",
                    "gate_branch": gate_branch,
                    "gate_output_path": None,
                },
            ),
        )
        if _notify_gate(job, "success"):
            _notify(client, f"herdr-routines: {job.name} done", sound="done")
        return f"{job.name}: done (gate passed)", False

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
                extra={
                    "gate": "failed",
                    "reason": "report_dir_creation_failed",
                    "error": str(e),
                    "target": "base",
                    "gate_branch": gate_branch,
                },
            ),
        )
        return f"{job.name}: failed (report_dir_creation_failed)", True

    prompt_text = job.prompt or build_base_fix_prompt(
        job_name=job.name,
        gate_output=gate_outcome.combined_output,
        base=job.base,
        report_path=str(report_path),
        checks=job.checks,  # type: ignore[arg-type]
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
                extra={
                    "gate": "failed",
                    "reason": "worktree_creation_failed",
                    "error": str(e),
                    "target": "base",
                    "gate_branch": gate_branch,
                },
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
                extra={
                    "gate": "failed",
                    "reason": "agent_start_failed",
                    "error": str(e),
                    "pane_id": pane_id,
                    "target": "base",
                    "gate_branch": gate_branch,
                },
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
                extra={
                    "gate": "failed",
                    "reason": "agent_not_interactive",
                    "error": last_error,
                    "pane_id": pane_id,
                    "target": "base",
                    "gate_branch": gate_branch,
                },
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
                extra={
                    "gate": "failed",
                    "reason": "agent_prompt_failed",
                    "error": str(e),
                    "pane_id": pane_id,
                    "target": "base",
                    "gate_branch": gate_branch,
                },
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

    state = "done" if settled_status in ("idle", "done") else "failed"
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state=state,
            run_id=run_id,
            extra={
                "gate": "failed",
                "target": "base",
                "gate_branch": gate_branch,
                "reason": "gate_failed",
                "failed_checks": gate_outcome.combined_output,
                "gate_output_path": str(gate_output_path)
                if gate_output_path.exists()
                else None,
                "branch": gate_branch,
                "pane_id": pane_id,
                "report_path": str(report_path) if report_written else None,
                "report_written": report_written,
                "final_agent_status": settled_status,
                "session_id": session_id,
            },
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
    status = subprocess.run(
        ["git", "-C", str(wt_path), "status", "--porcelain=v1"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip()}"
    if status.stdout.strip():
        return False, "worktree has uncommitted changes"
    return True, ""


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
) -> dict[str, Any]:
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
        return {
            "state": "failed",
            "reason": reason,
            "error": str(e),
        }

    # Create report dir
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {
            "state": "failed",
            "reason": "report_dir_creation_failed",
            "error": str(e),
        }

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
            return {
                "state": "failed",
                "reason": "worktree_reuse_not_clean",
                "error": f"existing worktree {existing_wt} for {pr.head_ref}: {detail}",
            }
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
            return {
                "state": "failed",
                "reason": "worktree_creation_failed",
                "error": str(e),
            }

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
        return {
            "state": "failed",
            "reason": "agent_start_failed",
            "error": str(e),
            "pane_id": pane_id,
        }

    # Wait for agent readiness
    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(
            client, agent_name, reports_dir=report_path.parent, run_id=pr_run_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return {
            "state": "failed",
            "reason": "agent_not_interactive",
            "error": last_error,
            "pane_id": pane_id,
        }

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
        return {
            "state": "failed",
            "reason": "agent_prompt_failed",
            "error": str(e),
            "pane_id": pane_id,
        }

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

    return {
        "state": "done" if settled_status in ("idle", "done") else "failed",
        "pane_id": pane_id,
        "report_path": str(report_path) if report_written else None,
        "report_written": report_written,
        "report_bytes": report_bytes,
        "final_agent_status": settled_status,
        "session_id": session_id,
    }


def _outcome_extra(outcome: RunOutcome) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "agent": outcome.agent_name,
        "pane_id": outcome.pane_id,
        "branch": outcome.branch,
        "final_agent_status": outcome.final_agent_status,
        "report_written": outcome.report_written,
        "report_bytes": outcome.report_bytes,
        "report": outcome.report_path,
        "duration_seconds": outcome.duration_seconds,
        "session_id": outcome.session_id,
    }
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
    return extra


def _retry_eligible(outcome: RunOutcome, job: Job) -> bool:
    """Check if this outcome is eligible for a retry attempt per the job's retry config."""
    return (
        outcome.state == "failed"
        and outcome.reason is not None
        and job.retry_on is not None
        and outcome.reason in job.retry_on
    )


def _process_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
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
        return _process_gated_job(job, history_path, client=client, now=now)

    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=job.timeout_ms, now=now
    )
    if stale is not None:
        stale_extra: dict[str, Any] = {"reason": "stale_running_record"}
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
                extra={"reason": "agent_name_live"},
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_terminal_run(history_path, job.name)
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
        assert result.occurrence is not None
        extra: dict[str, Any] = {
            "reason": "outside_catch_up_window",
            "occurrence": result.occurrence.isoformat(),
        }
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
    assert result.occurrence is not None
    run_id = make_run_id(job.name, result.occurrence)
    if result.skipped_occurrences:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra={
                    "reason": "collapsed_earlier_occurrences",
                    "skipped_occurrences": result.skipped_occurrences,
                    "skipped_first": result.skipped_first.isoformat()
                    if result.skipped_first
                    else None,
                    "skipped_last": result.skipped_last.isoformat()
                    if result.skipped_last
                    else None,
                },
            ),
        )

    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=run_id,
            extra={
                "scheduled_for": result.occurrence.isoformat(),
                "late_seconds": result.late_seconds,
            },
        ),
    )

    outcome = execute_run(job, client, run_id=run_id)
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
                    extra=_outcome_extra(outcome),
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
                    extra={"reason": "fallback_retry", "primary_run_id": run_id},
                ),
            )
            run_id = fallback_run_id
            outcome = execute_run(
                replace(job, model=job.fallback_model), client, run_id=run_id
            )

        # Check if this outcome is eligible for a retry attempt
        if not _retry_eligible(outcome, job) or attempt >= job.retry_attempts:
            break

        # Log the failed attempt as a distinct history record with attempt metadata
        extra = _outcome_extra(outcome)
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
                extra={
                    "attempt": attempt,
                    "max_retries": job.retry_attempts,
                    "retried_from_run_id": base_run_id
                    if attempt == 1
                    else f"{base_run_id}-retry{attempt - 1}",
                },
            ),
        )
        outcome = execute_run(job, client, run_id=run_id)

    # Terminal record for final outcome (last retry or first attempt if no retries)
    final_extra = _outcome_extra(outcome)
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
    show the 6-fresh-session contract was faked, else None. None also when state.json is
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


def _classify_pipeline_outcome(report_text: str) -> tuple[str, str | None]:
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
    assert job.deadline_ms is not None  # config.py requires this for kind: pipeline
    return int(now.timestamp()) + job.deadline_ms // 1000


def _build_pipeline_launch_argv(
    job: Job, *, run_id: str, report_path: Path, unit_name: str, deadline_epoch: int
) -> list[str]:
    assert job.deadline_ms is not None  # config.py requires this for kind: pipeline
    assert job.prompt_file is not None
    runtime_max_sec = (job.deadline_ms + PIPELINE_UNIT_MARGIN_MS) // 1000
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
        str(job.repo / job.prompt_file),
        "--wait-timeout-ms",
        str(job.deadline_ms),
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

        assert open_run.run_id is not None
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
            extra: dict[str, Any] = {
                "pipeline_run_id": bare_run_id,
                "report": str(report_path),
            }
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
                    extra={"reason": "host_rebooted", "pipeline_run_id": bare_run_id},
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
                extra={"reason": "no_report", "pipeline_run_id": bare_run_id},
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
                extra={"reason": "agent_name_live"},
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    last = last_terminal_run(history_path, job.name)
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
        assert result.occurrence is not None
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="missed",
                extra={
                    "reason": "outside_catch_up_window",
                    "scheduled_for": result.occurrence.isoformat(),
                },
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
    assert result.occurrence is not None
    run_id = make_run_id(job.name, result.occurrence)
    bare_run_id = _bare_pipeline_run_id(job.name, run_id)
    return _launch_pipeline_run(
        job,
        history_path,
        client=client,
        now=now,
        run_id=run_id,
        bare_run_id=bare_run_id,
        running_extra={
            "scheduled_for": result.occurrence.isoformat(),
            "late_seconds": result.late_seconds,
        },
    )


def _launch_pipeline_run(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    bare_run_id: str,
    running_extra: dict[str, Any],
) -> tuple[str, bool]:
    """Sync the repo, launch the detached orchestrator unit, and record the outcome.
    `running_extra` goes into the `running` record's `extra` (scheduled_for/
    late_seconds)."""
    report_path = pipeline_report_path(bare_run_id)
    unit_name = f"herdr-pipeline-{bare_run_id}"
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
                extra={"reason": "repo_sync_failed", "error": str(e)},
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
                extra={
                    "reason": "launch_failed",
                    "rc": rc,
                    "error": err.strip()[:2000],
                },
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
            extra={
                **running_extra,
                "pipeline_run_id": bare_run_id,
                "report": str(report_path),
                "unit": unit_name,
                "deadline_epoch": deadline_epoch,
            },
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
    return kind == "failure"  # "on-failure"


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
    reaped: list[str] = []
    for name, (_status, pane_id) in sorted(agents.items()):
        if not _is_job_agent(name, job):
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
      deadline + grace with a quiet heartbeat).

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
