"""A routine with a target selector: resolve the targets and run one agent per target,
each in its own worktree (this is how `kind: review` runs).

Split out of `tick.py` (issue 060).
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from logger import get_logger

from herdr_routines.auto_fix import (
    RealGhClient,
    attempt_count_for_target,
    build_review_agent_name,
    build_review_agent_prefix,
    review_marker,
    review_target_hash,
)
from herdr_routines.config import (
    Job,
)
from herdr_routines.dispatch_common import (
    Occurrence,
    _append_running,
    _cleanup_worktree,
    _notify,
    _notify_gate,
    _occurrence_extra,
    _with_trigger,
    _worktree_reuse_check,
)
from herdr_routines.herdr import (
    LIVE_AGENT_STATUSES,
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
    _ensure_checkout,
    default_repos_dir,
    ensure_repo,
)
from herdr_routines.review_scan import (
    DISPATCHED,
    Candidate,
    is_failure_disposition,
    render_review_report,
)
from herdr_routines.runner import (
    TargetPlacement,
    default_reports_dir,
    execute_run,
)
from herdr_routines.target_selectors import (
    SelectorContext,
    SelectorFailed,
    job_own_repo,
    select_targets,
)
from herdr_routines.verify import VerifyTarget

log = get_logger(__name__)


# -- multi-target routine dispatch (issues 055, 076) -------------------------------------
#
# A routine whose selector returns several targets (`pick: my_open_prs`, which is what a
# `kind: review` job file loads as) runs one agent per target, in turn, inside the one
# occurrence. Selection and its re-review guards (content marker, per-target attempt budget)
# are the selector's (review_scan); each target gets a checkout (the job's own, or a managed
# per-repo one), a read-only detached worktree, `execute_run` with the verify gates on its
# own PR, and its own history record. The post-run gate, not the agent's settle status,
# decides `done`: an unverifiable review is never green.


def _own_repo(job: Job) -> str | None:
    """`owner/repo` of the job's own checkout, or None when it has none (a repo-less job such
    as `kind: review`, whose `repo` is the managed base dir) or it cannot be read."""
    try:
        return job_own_repo(job)
    except (RuntimeError, OSError) as e:
        log.warning("%s: could not read the job's own repo: %s", job.name, e)
        return None


def _targets_selector_failed(
    job: Job,
    history_path: Path,
    occ: Occurrence,
    failed: SelectorFailed,
    *,
    client: HerdrClient,
    now: datetime,
) -> tuple[str, bool]:
    """The selector could not answer: a terminal `failed` record, like the single-target
    path's, so the occurrence counts as having run and is not retried."""
    extra = _occurrence_extra(occ)
    extra["reason"] = failed.reason
    extra["error"] = failed.error
    append(
        history_path,
        HistoryRecord.new(
            ts=now, job=job.name, state="failed", run_id=occ.run_id, extra=extra
        ),
    )
    if _notify_gate(job, "failure"):
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body=failed.reason,
            sound="request",
        )
    return f"{job.name}: failed ({failed.reason})", True


def _run_routine_targets(
    job: Job,
    history_path: Path,
    occ: Occurrence,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...],
) -> tuple[str, bool]:
    """One occurrence of a routine with a multi-target selector: select, run one agent per
    target (the selector's cap, `max_targets_per_run`), then write the aggregate report and
    terminal record. Dispatch order is by repo name, oldest first within a repo, so a repo is
    looked up and checked out once however many of its PRs are picked.

    Targets get exactly one attempt: no `fallback_model` and no `retry_attempts`, since a
    retry could post a second review."""
    run_id = occ.run_id
    gh = RealGhClient()
    selected = select_targets(
        SelectorContext(
            job=job,
            gh=gh,
            trusted_review_authors=trusted_review_authors,
            history_path=history_path,
        )
    )
    if isinstance(selected, SelectorFailed):
        return _targets_selector_failed(
            job, history_path, occ, selected, client=client, now=now
        )
    _append_running(job, history_path, occ, now=now)

    # Pre-dispatch rows come from the selector; post-dispatch dispositions are appended below.
    rows: list[tuple[Candidate, str]] = list(selected.rows)
    by_repo: dict[str, list[Candidate]] = {}
    for cand in selected.dispatchable:
        by_repo.setdefault(cand.repo, []).append(cand)

    own_repo = _own_repo(job)
    repo_info_cache: dict[str, dict[str, object] | None] = {}
    repos_failed = 0
    for repo, cands in sorted(by_repo.items()):
        owner, _, repo_name = repo.partition("/")
        try:
            if own_repo is not None and own_repo.casefold() == repo.casefold():
                # The job's own checkout: kept current the way a plain routine's is.
                checkout = ensure_repo(job)
            else:
                # Any other repo's targets run in its managed checkout, made on first use.
                if repo not in repo_info_cache:
                    repo_info_cache[repo] = gh.repo_info(owner=owner, repo=repo_name)
                info = repo_info_cache[repo]
                if info is None:
                    repos_failed += 1
                    rows.extend((cand, "repo_info_failed") for cand in cands)
                    continue
                clone_url = info.get("clone_url")
                default_branch = info.get("default_branch")
                checkout = _ensure_checkout(
                    clone_url if isinstance(clone_url, str) else None,
                    default_repos_dir() / owner / repo_name,
                    base=default_branch if isinstance(default_branch, str) else "main",
                )
        except (RuntimeError, OSError) as e:
            # Isolate: only this repo's targets fail; other repos still dispatch.
            repos_failed += 1
            log.warning("%s: checkout failed for %s: %s", job.name, repo, e)
            rows.extend((cand, "clone_failed") for cand in cands)
            continue

        for cand in cands:
            disposition = _run_one_target(
                job,
                cand,
                checkout,
                gh=gh,
                client=client,
                history_path=history_path,
                now=now,
                occ=occ,
                trusted_authors=selected.trusted_authors,
                trusted_review_authors=trusted_review_authors,
            )
            rows.append((cand, disposition))

    dispatched_count = sum(1 for _, d in rows if d == DISPATCHED)
    failed_count = sum(1 for _, d in rows if is_failure_disposition(d))
    skipped_count = len(rows) - dispatched_count - failed_count
    any_failed = failed_count > 0

    try:
        reports_dir = default_reports_dir()
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"review-{run_id}.md"
        report_path.write_text(
            render_review_report(
                job_name=job.name,
                run_id=run_id,
                now=now,
                enumerated=selected.enumerated,
                candidate_count=selected.candidate_count,
                repos_failed=repos_failed,
                rows=rows,
                truncated_modes=selected.truncated_modes,
            )
        )
    except Exception as e:  # noqa: BLE001
        log.warning("%s: could not write review report: %s", job.name, e)

    summary = (
        f"{job.name}: done (enumerated={selected.enumerated}, "
        f"candidates={selected.candidate_count}, dispatched={dispatched_count}, "
        f"skipped={skipped_count}, failed={failed_count}, "
        f"repos_failed={repos_failed})"
    )

    append(
        history_path,
        HistoryRecord.new(
            ts=now,
            job=job.name,
            state="failed" if any_failed else "done",
            run_id=run_id,
            extra=HistoryExtra(
                gate="failed" if any_failed else "passed",
                target="pr",
                enumerated=selected.enumerated,
                candidates=selected.candidate_count,
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
    occ: Occurrence,
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
    """One per-target history record, shaped per issue 055's "History and report". It carries
    no `trigger`: the occurrence's `running` record and this target's own do, and
    `last_scheduled_terminal_run` links terminals to them by run_id."""
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
    return HistoryRecord.new(
        ts=now, job=job.name, state=state, run_id=occ.run_id, extra=extra
    )


def _prepare_review_worktree(checkout: Path, wt_path: Path, head_ref: str) -> None:
    """Fetch the PR head and leave a detached worktree on it at `wt_path`. A review needs no
    branch, so the worktree is a detached HEAD at origin/<headRef>: it never creates a local
    branch in the shared checkout and never contends with another worktree (e.g. the
    orchestrator's) that has the head branch checked out. Our own clean worktree is reused
    (moved to the fresh head); a dirty one is force-removed and re-added. Raises on any git
    failure."""
    if wt_path.exists():
        reusable, why = _worktree_reuse_check(wt_path)
        if not reusable:
            log.info("re-adding stale review worktree %s (%s)", wt_path, why)
            _cleanup_worktree(checkout, wt_path)

    head = f"origin/{head_ref}"
    fetch = subprocess.run(
        ["git", "-C", str(checkout), "fetch", "origin", head_ref],
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
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{step} failed: {proc.stderr.strip()}")


def _run_one_target(
    job: Job,
    cand: Candidate,
    checkout: Path,
    *,
    gh: RealGhClient,
    client: HerdrClient,
    history_path: Path,
    now: datetime,
    occ: Occurrence,
    trusted_authors: frozenset[str],
    trusted_review_authors: tuple[str, ...],
) -> str:
    """Run one agent on one target: a read-only worktree in `checkout`, `execute_run` (which
    runs the verify gates on this PR), and a per-target record. Always removes the worktree
    after the agent settles. Returns the disposition recorded in the aggregate report:
    `dispatched` when the run is done, otherwise why it was skipped or failed."""
    run_id = occ.run_id
    attempt = attempt_count_for_target(history_path, job.name, cand.target_key)
    h6 = review_target_hash(job.name, cand.target_key)
    report_path = default_reports_dir() / f"auto-review-{run_id}-{h6}.md"
    agent_name = build_review_agent_name(job.name, cand.target_key, run_id)

    def record(
        state: HistoryState,
        *,
        reason: str | None = None,
        error: str | None = None,
        agent: str | None = None,
        pane_id: str | None = None,
        report: Path | None = None,
        verified: bool | None = False,
    ) -> None:
        append(
            history_path,
            _review_target_record(
                job=job,
                cand=cand,
                occ=occ,
                now=now,
                state=state,
                attempt=attempt,
                agent_name=agent,
                pane_id=pane_id,
                report_path=report,
                verified=verified,
                reason=reason,
                error=error,
            ),
        )

    if _live_review_agent_exists(client, job.name, cand.target_key):
        record("skipped", reason="agent_live", agent=agent_name, verified=None)
        return "agent_live"

    wt_path = checkout / ".worktrees" / f"review-pr{cand.number}"
    try:
        _prepare_review_worktree(checkout, wt_path, cand.head_ref)
    except Exception as e:  # noqa: BLE001
        _cleanup_worktree(checkout, wt_path)
        record("failed", reason="worktree_failed", error=str(e))
        return "worktree_failed"

    append(
        history_path,
        HistoryRecord.new(
            ts=now,
            job=job.name,
            state="running",
            run_id=run_id,
            extra=_with_trigger(
                occ,
                HistoryExtra(
                    target_key=cand.target_key,
                    repo=cand.repo,
                    pr_number=cand.number,
                    target="pr",
                ),
            ),
        ),
    )
    try:
        outcome = execute_run(
            job,
            client,
            run_id=run_id,
            trusted_review_authors=trusted_review_authors,
            gh=gh,
            target=VerifyTarget(cand.owner, cand.repo_name, cand.number),
            placement=TargetPlacement(
                workdir=wt_path,
                agent_name=agent_name,
                report_path=report_path,
                marker=review_marker(job.name, cand.target_key),
                trusted_authors=trusted_authors,
            ),
        )
    finally:
        # A review retains no branch: remove the worktree on success, failure and crash.
        _cleanup_worktree(checkout, wt_path)

    if outcome.state == "done":
        record(
            "done",
            agent=outcome.agent_name,
            pane_id=outcome.pane_id,
            report=report_path,
            verified=True,
        )
        return DISPATCHED
    failed_gate = next((r for r in outcome.verify if not r.passed), None)
    reason = outcome.reason or outcome.state
    record(
        outcome.state,
        reason=reason,
        # The run's own error, else the gate's reason (`review_not_posted`, ...).
        error=outcome.error or (failed_gate.reason if failed_gate else None),
        agent=outcome.agent_name,
        pane_id=outcome.pane_id,
        report=report_path if outcome.report_written else None,
    )
    return reason
