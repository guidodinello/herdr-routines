"""The `kind: routine` flow: run one agent occurrence, with the fallback model, and record
it. Retries are scheduled by the tick on a later occurrence (issue 088); this flow only
runs one attempt, so a tick never holds a lock while waiting for a backoff.

Split out of `tick.py` (issue 060).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from logger import get_logger

from herdr_routines.auto_fix import (
    RealGhClient,
)
from herdr_routines.config import (
    MULTI_TARGET_SELECTORS,
    Job,
)
from herdr_routines.dispatch_common import (
    NotifyKind,
    Occurrence,
    _notify,
    _notify_gate,
    _occurrence_extra,
    _with_trigger,
)
from herdr_routines.herdr import (
    HerdrClient,
)
from herdr_routines.history import (
    HistoryExtra,
    HistoryRecord,
    VerifyRecord,
    append,
)
from herdr_routines.routine_targets_flow import (
    _run_routine_targets,
)
from herdr_routines.runner import (
    RunOutcome,
    execute_run,
    extract_prompt_excerpt,
    make_run_id,
)
from herdr_routines.target_selectors import (
    SelectorContext,
    SelectorFailed,
    select_target,
)
from herdr_routines.verify import VerifyTarget

log = get_logger(__name__)


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


def _with_retry_meta(occ: Occurrence, extra: HistoryExtra) -> HistoryExtra:
    """Stamp a record with the occurrence's trigger and retry bookkeeping, if any."""
    stamped = _with_trigger(occ, extra)
    attempt = occ.extra.get("attempt")
    max_retries = occ.extra.get("max_retries")
    retried_from = occ.extra.get("retried_from_run_id")
    if isinstance(attempt, int):
        stamped["attempt"] = attempt
    if isinstance(max_retries, int):
        stamped["max_retries"] = max_retries
    if isinstance(retried_from, str):
        stamped["retried_from_run_id"] = retried_from
    return stamped


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
    the fallback_model, terminal record, notification. The tick and `herdr-routines run`
    both land here (issue 074). A selector that returns several targets (issue 076) fans out
    in `_run_routine_targets` instead."""
    if job.pick in MULTI_TARGET_SELECTORS:
        return _run_routine_targets(
            job,
            history_path,
            occ,
            client=client,
            now=now,
            trusted_review_authors=trusted_review_authors,
        )
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
                HistoryRecord.new(
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
                HistoryRecord.new(
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
        HistoryRecord.new(
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

    # fallback_model is a same-tick recovery for a quota wall, not a scheduled retry.
    if (
        outcome.state == "failed"
        and outcome.reason == "quota_exhausted"
        and job.fallback_model
        and job.fallback_model != job.model
    ):
        used_fallback = True
        append(
            history_path,
            HistoryRecord.new(
                ts=now,
                job=job.name,
                state=outcome.state,
                run_id=run_id,
                extra=_with_retry_meta(occ, _outcome_extra(outcome, target=target)),
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
            HistoryRecord.new(
                ts=now,
                job=job.name,
                state="running",
                run_id=fallback_run_id,
                extra=_with_retry_meta(
                    occ, HistoryExtra(reason="fallback_retry", primary_run_id=run_id)
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

    # Terminal record for this attempt. A tick-scheduled retry carries its attempt index
    # in the occurrence; a first run records zeros.
    final_extra = _with_retry_meta(occ, _outcome_extra(outcome, target=target))
    final_attempt = occ.extra.get("attempt")
    final_extra["attempt"] = final_attempt if isinstance(final_attempt, int) else 0
    final_extra["max_retries"] = (
        occ.extra.get("max_retries", 0)
        if isinstance(occ.extra.get("max_retries"), int)
        else 0
    )
    if final_extra["attempt"] > 0:
        final_extra["final_attempt"] = True
    append(
        history_path,
        HistoryRecord.new(
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
        gate: NotifyKind = (
            "finding" if (used_fallback or outcome.reaped_stale_agent) else "success"
        )
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
