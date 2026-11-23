"""The `kind: audit` flow: run the audit agent in a worktree, diff its findings against
the last report, and dispatch a fix worker for the new ones.

Split out of `tick.py` (issue 060).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from logger import get_logger

from herdr_routines.auto_fix import (
    build_audit_agent_name,
    build_audit_fix_prompt,
    build_audit_prompt,
    build_gate_worker_agent_name,
    run_checks,
)
from herdr_routines.config import (
    GateCheck,
    Job,
    JobInvariantError,
)
from herdr_routines.dispatch_common import (
    NotifyKind,
    _add_worktree,
    _AgentRun,
    _cleanup_worktree,
    _notify,
    _notify_gate,
    _run_agent_in_worktree,
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
    HerdrClient,
    HerdrCliError,
)
from herdr_routines.history import (
    HistoryExtra,
    HistoryRecord,
    append,
)
from herdr_routines.repos import (
    CheckoutBusy,
    ensure_repo,
)
from herdr_routines.runner import (
    build_branch_name,
    default_reports_dir,
    substitute_prompt,
)

log = get_logger(__name__)


# Per-run cap on how many suppressed IDs the history record carries. Suppression is
# terminal (never re-dispatched) so the count matters more than the list, but a bounded
# sample keeps one bad night from bloating every history line.
_AUDIT_SUPPRESSED_ID_CAP = 20

AuditGate = Literal["baseline", "fix_dispatched", "suppressed", "passed", "failed"]

# gate -> notification kind (see _notify_gate's four-tier policy).
_AUDIT_GATE_NOTIFY_KIND: Final[dict[AuditGate, NotifyKind]] = {
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
    gate: AuditGate,
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
        HistoryRecord.new(
            ts=now,
            job=job.name,
            state="failed",
            run_id=run_id,
            extra=failure_extra,
        ),
    )
    return f"{job.name}: failed ({reason})", True


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
    except CheckoutBusy:
        # Contention, not a failed run (issue 079): let the dispatch turn it into a
        # `skipped`/`checkout_busy` record under the occurrence's trigger.
        raise
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
    gate: AuditGate

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
            HistoryRecord.new(
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
            HistoryRecord.new(
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
        HistoryRecord.new(
            ts=now, job=job.name, state="done", run_id=run_id, extra=extra
        ),
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
