"""The `kind: gated` event-target flow: one polled GitHub event, its worker dispatch and
the verify gates on that worker's result.

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
    GhClient,
    GithubEvent,
    RealGhClient,
    attempt_count_for_event,
    build_event_fix_prompt,
    build_worker_agent_name,
    enrich_event_detail,
    event_worktree_slug,
    poll_github_event,
    repo_owner_and_name,
)
from herdr_routines.config import (
    DEFAULT_TRUSTED_REVIEW_AUTHORS,
    GateCheck,
    Job,
    VerifyGate,
)
from herdr_routines.dispatch_common import (
    _dirty_worktree_already_reported,
    _find_existing_worktree,
    _is_valid_branch_name,
    _notify,
    _notify_gate,
    _WorkerFailed,
    _WorkerResult,
    _WorkerSettled,
    _worktree_reuse_check,
)
from herdr_routines.gated_pr_flow import (
    _FIX_WORKER_GATES,
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
    HistoryState,
    append,
    read_job,
)
from herdr_routines.repos import (
    CheckoutBusy,
    ensure_repo,
)
from herdr_routines.verify import VerifyContext, VerifyTarget

log = get_logger(__name__)


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


def _event_trigger_for(job: Job) -> tuple[GateCheck | None, bool]:
    """The job's event trigger, and whether it is a gated `github_event` check moved onto
    the trigger path (issue 077).

    `load_config` sets `trigger_event` for such a job; a directly-built `Job` may still
    carry only the check, so infer it here too. The second element is True only for the
    migrated form: it keeps 014's per-event budget and skips the cron path, since the
    check *is* the gate."""
    if job.kind == "gated":
        for check in job.checks or ():
            if check.kind == "github_event":
                return (job.trigger_event or check), True
    return job.trigger_event, False


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
            HistoryRecord.new(
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
            HistoryRecord.new(
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
            HistoryRecord.new(
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
            HistoryRecord.new(
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

    dispatched_count = 0
    skipped_count = 0
    any_failed = False

    # A budget-exhausted event is reported once and spends no dispatch slot (issue 078):
    # unlike before, the first exhaustion is never added to the watermark, so it must not
    # starve the events behind it on later ticks.
    attempts = {
        e.event_id: attempt_count_for_event(history_path, job.name, e.event_id)
        for e in new_events
    }
    # One pass over the job's history for every exhausted event this tick, instead of
    # re-scanning the whole file once per budget-exhausted event (issue 078).
    exhausted_reported: set[str] = set()
    for record in read_job(history_path, job.name):
        extra = record.extra
        event_id = extra.get("event_id") if extra else None
        if (
            extra is not None
            and extra.get("reason") == "max_attempts_exceeded"
            and isinstance(event_id, str)
        ):
            exhausted_reported.add(event_id)
    candidates: list[GithubEvent] = []
    for event in new_events:
        attempt = attempts[event.event_id]
        if attempt >= job.max_attempts_per_target:
            # The budget tapped out (issue 078): the event stays out of the `handled`
            # watermark — a gated failure is never written off as seen — and is retried
            # no further because every later tick lands here. Report the exhaustion
            # once; repeating the same record and notification every tick would only
            # bury the first one.
            if event.event_id not in exhausted_reported:
                append(
                    history_path,
                    HistoryRecord.new(
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
                exhausted_reported.add(event.event_id)
                if _notify_gate(job, "failure"):
                    _notify(
                        client,
                        f"herdr-routines: {job.name} skipped",
                        body=f"max_attempts_exceeded for {event.event_id}",
                        sound="request",
                    )
            skipped_count += 1
            continue
        candidates.append(event)

    dispatchable = candidates[: job.max_workers_per_tick]
    skipped_over_cap = len(candidates) - len(dispatchable)

    for event in dispatchable:
        attempt = attempts[event.event_id]
        detailed = enrich_event_detail(gh, event, owner=owner, repo=repo_name)
        # The head ref is GitHub data that becomes a `git worktree add` argument and a
        # worktree lookup key, so it must be a well-formed branch name first. Checked
        # after enrichment, which may replace it. A malformed name stays malformed, so
        # the event is skipped and marked seen rather than retried every tick; a check
        # that could not run is a failed attempt, retried within the event's budget.
        head_ref = detailed.head_ref
        ref_ok = _is_valid_branch_name(head_ref) if head_ref else True
        if ref_ok is None:
            append(
                history_path,
                HistoryRecord.new(
                    ts=now,
                    job=job.name,
                    state="failed",
                    run_id=run_id,
                    extra=HistoryExtra(
                        reason="head_ref_check_failed",
                        event_id=event.event_id,
                        event_number=event.number,
                        attempt=attempt,
                        target=event_target,
                    ),
                ),
            )
            any_failed = True
            continue
        if not ref_ok and head_ref:
            append(
                history_path,
                HistoryRecord.new(
                    ts=now,
                    job=job.name,
                    state="skipped",
                    run_id=run_id,
                    extra=HistoryExtra(
                        reason="invalid_head_ref",
                        event_id=event.event_id,
                        event_number=event.number,
                        head_ref=head_ref,
                        target=event_target,
                    ),
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
                HistoryRecord.new(
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
                # `done` is the agent's word (issue 078, mirroring 070's PR path): prove
                # the query's required action happened before keeping it.
                verify_results = _verify_event_worker(
                    job,
                    gh=gh,
                    run_id=run_id,
                    now=now,
                    owner=owner,
                    repo=repo_name,
                    query=check.query,
                    event=detailed,
                    worker=worker_outcome,
                    trusted_review_authors=trusted_review_authors,
                )
                failed_gate = next((r for r in verify_results if not r.passed), None)
                if failed_gate is not None:
                    outcome_state = "failed"
                    outcome_reason = failed_gate.reason
                    if (
                        outcome_reason == verify.REASON_LEFT_UNCOMMITTED
                        and worker_outcome.worktree_path is not None
                    ):
                        wt = worker_outcome.worktree_path
                        if not _dirty_worktree_already_reported(
                            history_path, job.name, wt
                        ) and _notify_gate(job, "failure"):
                            _notify(
                                client,
                                f"herdr-routines: {job.name} event "
                                f"{event.event_id} blocked",
                                body=(
                                    f"uncommitted changes in {wt}; "
                                    "rescue or discard by hand"
                                ),
                                sound="request",
                            )
        else:
            # Keep the worker's own reason (issue 078): the PR path already does
            # (tick.py:_process_pr_target) instead of the old hardcoded `gate_failed`.
            outcome_reason = worker_outcome.reason
            outcome_error = worker_outcome.error

        extra_record = HistoryExtra(
            event_id=event.event_id,
            event_number=event.number,
            attempt=attempt,
            fix_worker_agent=agent_name,
            pane_id=worker_outcome.pane_id,
            report_path=outcome_report_path,
            report_written=outcome_report_path is not None,
            final_agent_status=outcome_agent_status,
            target=event_target,
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
                "%s: event %s dispatch failed: reason=%s error=%s",
                job.name,
                event.event_id,
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
        else:
            handled.add(event.event_id)
        dispatched_count += 1

    state: HistoryState = "failed" if any_failed else "done"
    append(
        history_path,
        HistoryRecord.new(
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
        worktree_path=str(wt_path),
        dispatch_head_sha=event.head_sha,
    )


# What proves an event worker's `done` per `github_event` query (issue 078). A PR-shaped
# event uses 070's gate set; an issue has no head branch, so the work shipping means a
# comment on it or a PR linked to it. The config loader's query set is closed, so a
# missing key is a programming error, not a query we can safely guess a gate set for:
# `_verify_event_worker` raises rather than silently applying another query's gates.
_EVENT_WORKER_GATES: dict[str, tuple[VerifyGate, ...]] = {
    "prs:labels": _FIX_WORKER_GATES,
    "ci:failed": _FIX_WORKER_GATES,
    "issues:opened": ("worktree_clean", "commented_or_linked"),
}


def _verify_event_worker(
    job: Job,
    *,
    gh: GhClient,
    run_id: str,
    now: datetime,
    owner: str,
    repo: str,
    query: str | None,
    event: GithubEvent,
    worker: _WorkerSettled,
    trusted_review_authors: tuple[str, ...],
) -> tuple[verify.VerifyResult, ...]:
    """Run the query's settle gates for an event worker whose agent said `done`. The
    agent's word is not proof: these read the worktree and the PR/issue (issue 078)."""
    if query is None or query not in _EVENT_WORKER_GATES:
        raise ValueError(
            f"{job.name}: no settle gates defined for github_event query {query!r}"
        )
    gates = _EVENT_WORKER_GATES[query]
    return verify.run_verify_gates(
        gates,
        VerifyContext(
            job=job,
            run_id=run_id,
            started_at=now,
            report_path=Path(worker.report_path or ""),
            gh=gh,
            trusted_review_authors=trusted_review_authors,
            target=VerifyTarget(owner, repo, event.number),
            worktree_path=Path(worker.worktree_path) if worker.worktree_path else None,
            dispatch_head_sha=worker.dispatch_head_sha,
            given_thread_ids=None,
        ),
    )
