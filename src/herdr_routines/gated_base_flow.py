"""The `kind: gated` base-target flow: run the checks on the base branch and open a fix
PR when they fail.

Split out of `tick.py` (issue 060).
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from logger import get_logger

from herdr_routines.auto_fix import (
    build_base_fix_prompt,
    build_gate_worker_agent_name,
    consecutive_base_gate_failures,
    run_checks,
)
from herdr_routines.config import (
    Job,
)
from herdr_routines.dispatch_common import (
    _cleanup_worktree,
    _notify,
    _notify_gate,
)
from herdr_routines.herdr import (
    SETTLED_AGENT_STATUSES,
    HerdrClient,
    HerdrCliError,
)
from herdr_routines.history import (
    HistoryExtra,
    HistoryRecord,
    HistoryState,
    append,
)
from herdr_routines.repos import (
    CheckoutBusy,
    ensure_repo,
)

log = get_logger(__name__)


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
    except CheckoutBusy:
        # Contention, not a failed run (issue 079): let the dispatch turn it into a
        # `skipped`/`checkout_busy` record under the occurrence's trigger.
        raise
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
