"""The `kind: gated` PR-target flow: run the checks on each open PR, dispatch a fix worker
for a failing one and verify that worker's result.

Split out of `tick.py` (issue 060).
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from logger import get_logger

from herdr_routines import verify
from herdr_routines.auto_fix import (
    EligiblePR,
    GhClient,
    PRInfo,
    RealGhClient,
    attempt_count_for_pr,
    build_fix_prompt,
    build_worker_agent_name,
    effective_trusted_authors,
    fetch_failing_checks,
    fetch_fix_worker_threads,
    is_eligible,
    list_open_prs,
    repo_owner_and_name,
)
from herdr_routines.config import (
    DEFAULT_TRUSTED_REVIEW_AUTHORS,
    Job,
    VerifyGate,
)
from herdr_routines.dispatch_common import (
    _dirty_worktree_already_reported,
    _find_existing_worktree,
    _notify,
    _notify_gate,
    _pr_worker_is_live,
    _WorkerFailed,
    _WorkerResult,
    _WorkerSettled,
    _worktree_reuse_check,
)
from herdr_routines.herdr import (
    SETTLED_AGENT_STATUSES,
    AgentStatus,
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
from herdr_routines.verify import VerifyContext, VerifyTarget

log = get_logger(__name__)


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
            HistoryRecord.new(
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
            HistoryRecord.new(
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
            HistoryRecord.new(
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
                HistoryRecord.new(
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
                HistoryRecord.new(
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
                HistoryRecord.new(
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
            HistoryRecord.new(
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
        HistoryRecord.new(
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
    except CheckoutBusy:
        # Contention, not a failed run (issue 079): let the dispatch turn it into a
        # `skipped`/`checkout_busy` record under the occurrence's trigger.
        raise
    except (RuntimeError, OSError) as e:
        return _WorkerFailed(
            reason=(
                "clone_failed"
                if not (job.repo / ".git").exists()
                else "repo_sync_failed"
            ),
            error=str(e),
        )

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
