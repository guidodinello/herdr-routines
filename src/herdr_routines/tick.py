"""The systemd entrypoint: load config, decide which jobs are due under the brief global tick
lock, run each under its own job dispatch lock (issue 079), write history. This is what `herdr-routines tick` calls. See docs/plan-v1.md §3/§4.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from logger import get_logger

from herdr_routines.audit_flow import (
    _audit_cycle,
    _live_audit_agent_exists,
)
from herdr_routines.auto_fix import (
    RealGhClient,
    poll_github_event,
)
from herdr_routines.config import (
    DEFAULT_TRUSTED_REVIEW_AUTHORS,
    Job,
    RoutinesConfig,
)
from herdr_routines.dispatch_common import (
    Occurrence,
    _append_running,
    _live_agent_exists,
    _notify,
    _notify_gate,
    _occurrence_extra,
    _reap_agents_of_rebooted_run,
)
from herdr_routines.gated_base_flow import (
    _process_base_target,
)
from herdr_routines.gated_event_flow import (
    _event_target,
    _event_trigger_for,
    _last_seen_event_ids,
    _process_event_target,
    _resolve_event_owner_repo,
)
from herdr_routines.gated_pr_flow import (
    _process_pr_target,
)
from herdr_routines.herdr import (
    HerdrClient,
)
from herdr_routines.history import (
    NOT_A_RUN_REASONS,
    TERMINAL_STATES,
    TRIGGER_CRON,
    TRIGGER_EVENT,
    TRIGGER_MANUAL,
    TRIGGER_REQUEST,
    HistoryExtra,
    HistoryRecord,
    append,
    find_stale_running,
    first_seen_at,
    has_ever_been_seen,
    is_currently_running,
    last_scheduled_terminal_run,
    maybe_rotate,
    read_job,
)
from herdr_routines.pipeline_flow import (
    PIPELINE_RECONCILE_GRACE_MS,
    _bare_pipeline_run_id,
    _classify_pipeline_outcome,
    _launch_pipeline_run,
    _open_pipeline_run,
    _pipeline_host_rebooted_mid_run,
    _pipeline_run_is_live,
    _pipeline_stage_independence_issue,
    pipeline_report_path,
)
from herdr_routines.pipeline_watchdog import (
    system_boot_epoch,
)
from herdr_routines.repos import (
    CheckoutBusy,
    checkout_lock,
    ensure_repo,
    is_repo_less,
)
from herdr_routines.retry import (
    RETRY_DROPPED_REASON,
    eligible_failure,
    find_pending_retry,
    pending_retry_record,
)
from herdr_routines.routine_flow import (
    _run_routine,
)
from herdr_routines.runner import (
    make_run_id,
)
from herdr_routines.schedule import Decision, decide, occurrences_since
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


def job_lock_path(history_path: Path, job_name: str) -> Path:
    """The per-job dispatch lock (issue 079): a sibling of the history file's state dir,
    so it follows the same `HERDR_PLUGIN_STATE_DIR` override the history path does."""
    return history_path.parent / "locks" / f"job-{job_name}.lock"


@contextmanager
def job_lock(history_path: Path, job_name: str) -> Generator[bool]:
    """Exclusive, non-blocking flock held for one job's *entire* synchronous dispatch.

    Replaces the tick-wide lock that used to span a run (issue 079): a long run of job A
    holds only `locks/job-A.lock`, so ticks and manual runs of job B proceed. Yields True
    when acquired and False when another dispatcher of the same job already holds it —
    the loser records `skipped` with reason `dispatch_in_flight` rather than racing the
    `rt-<job>` agent name or mutating the shared checkout."""
    path = job_lock_path(history_path, job_name)
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


@contextmanager
def hold_job_locks(
    history_path: Path, job_names: Iterable[str]
) -> Generator[list[str]]:
    """Try every named job's dispatch lock, non-blocking, and hold the ones won.

    Yields the names whose lock another process holds (a dispatch in flight). An empty
    list means this caller now holds every lock, so no routine, gated, audit, manual or
    request dispatch can be running or start until the block exits. Self-update uses this
    to keep issue 053's "never swap code under a running dispatch" guarantee now that a
    dispatch no longer holds the global tick lock (issue 079)."""
    busy: list[str] = []
    with ExitStack() as stack:
        for name in sorted(set(job_names)):
            if not stack.enter_context(job_lock(history_path, name)):
                busy.append(name)
        yield busy


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

    Closes the `running` records this tick wrote for the job, whose dispatch-time
    timestamps are at or after the tick-start `now` (issue 079), or writes a
    `run_id`-less `failed` when none was open so the occurrence is consumed rather than
    re-raising every tick. A pipeline job's open records are left alone: its detached unit
    may still be live and the pipeline watchdog owns finalising them. Best-effort: a
    history write failing here must not abort the tick either."""
    extra = HistoryExtra(reason="unexpected_error", error=repr(error))
    try:
        records = read_job(history_path, job.name)
        terminal_run_ids = {
            r.run_id for r in records if r.state in TERMINAL_STATES and r.run_id
        }
        open_run_ids = [
            r.run_id
            for r in records
            if r.state == "running"
            and r.ts >= now
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
                    HistoryRecord.new(
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
    # the tick. Runs before this tick's dispatches so leaked agent .so files and pytest
    # artifacts are cleaned before its new agent spawns. The lock does not exclude live
    # agents: other dispatches hold only their job locks (issue 079), so the reap's age
    # check (newest mtime in a tree) is what keeps a live run's files. Configured from `defaults:` only.
    tmp_hygiene = config.tmp_hygiene
    if tmp_hygiene.enabled:
        with tick_lock(default_lock_path()) as acquired:
            if not acquired:
                log.info(
                    "tmp hygiene: skipped, tick lock %s is held", default_lock_path()
                )
            else:
                try:
                    reaped = reap_tmp(
                        tmp_dir=Path(tmp_hygiene.tmp_dir),
                        max_age_s=tmp_hygiene.max_age_s,
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
    with tick_lock(default_lock_path()) as acquired:
        if not acquired:
            # Never wait on the global lock here: a tick decision phase holds it briefly,
            # but skip-and-warn keeps the end of a tick from stalling behind another one
            # (issue 079's documented lock ordering).
            log.warning(
                "history rotation skipped: tick lock %s is held", default_lock_path()
            )
        else:
            try:
                maybe_rotate(
                    history_path,
                    max_bytes=config.retention.history_max_bytes,
                    max_age_days=config.retention.history_max_age_days,
                    # Rotation names the roll after the clock at rotation time, not tick
                    # start: a tick that ran for an hour must not sort its roll before a
                    # shorter tick that started later (issue 079).
                    now=datetime.now(UTC),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("history rotation failed (continuing): %s", e)

    return TickOutcome(summaries=tuple(summaries), any_job_failed=any_job_failed)


def _skip_on_tick_lock(job: Job) -> tuple[str, bool]:
    """A decision phase that lost the global lock exits quietly, as a whole tick used to.
    The holder is another tick's decision phase (seconds, or an event job's `ensure_repo`
    and `gh` poll), housekeeping, or self-update; never a long run (issue 079)."""
    log.info("%s: skipped, tick lock %s is held", job.name, default_lock_path())
    return f"{job.name}: skipped (tick lock held)", False


def _guard_open_run(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_timeout_ms: int,
) -> tuple[str, bool] | None:
    """The open-run guards every non-pipeline decision phase runs before its triggers.

    A stale `running` record is reconciled to `interrupted_unknown`, but only while this
    tick holds the job's dispatch lock (issue 079). A held lock proves the run is alive,
    since flock dies with its process: a job dispatched late in a sequential tick looks
    older than it is (its record keeps the tick-start `ts`), so the record alone cannot tell. The lock is probed only when
    a record looks stale, so a probe rarely meets a real dispatcher.

    A run still in flight (lock held, or a fresh `running` record) skips without writing
    anything, as before issue 079: a record here would be trigger-less, so it would count
    as a scheduled terminal and consume the cron occurrence the run does not serve (issue
    074), once per tick for the whole run."""
    in_flight = f"{job.name}: skipped (already running)", False
    stale = find_stale_running(
        history_path, job.name, timeout_ms=run_timeout_ms, now=now
    )
    if stale is not None:
        with job_lock(history_path, job.name) as idle:
            if not idle:
                return in_flight
            stale_extra = HistoryExtra(reason="stale_running_record")
            reaped = _reap_agents_of_rebooted_run(
                client, job, stale, boot_epoch=system_boot_epoch()
            )
            if reaped:
                stale_extra["reaped_agents"] = reaped
            append(
                history_path,
                HistoryRecord.new(
                    ts=now,
                    job=job.name,
                    state="interrupted_unknown",
                    run_id=stale.run_id,
                    extra=stale_extra,
                ),
            )
    if is_currently_running(history_path, job.name, timeout_ms=run_timeout_ms, now=now):
        return in_flight
    return None


def _skip_if_agent_live(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    live_check: Callable[[HerdrClient, Job], bool],
) -> tuple[str, bool] | None:
    """The `agent_name_live` guard, run before the schedule decision so a live agent is
    reported as a skip even when the occurrence is not due. It runs after
    `_guard_open_run`, so it only fires for an agent that outlived its run's record, as
    before issue 079. The check is repeated inside the job-locked dispatch."""
    if not live_check(client, job):
        return None
    append(
        history_path,
        HistoryRecord.new(
            ts=now,
            job=job.name,
            state="skipped",
            extra=HistoryExtra(reason="agent_name_live"),
        ),
    )
    return f"{job.name}: skipped (agent already live)", False


def _process_audit_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    """Decide a kind: audit job under the brief global lock, then dispatch under its
    per-job lock (issues 057, 058, 079).

    The schedule guards are a gated job's; when the cron fires, `_audit_cycle` runs the
    audit, diffs its findings against the ledger, and dispatches at most one fix worker.
    One run spans the audit and the fix worker, so staleness is judged against both."""
    with tick_lock(default_lock_path()) as acquired:
        if not acquired:
            return _skip_on_tick_lock(job)
        pending = _decide_audit_job(job, history_path, client=client, now=now)
    return _finish_dispatch(pending, client=client)


def _decide_audit_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool] | _PendingDispatch:
    audit = job.require_audit()
    run_timeout_ms = audit.timeout_ms + job.timeout_ms

    if not has_ever_been_seen(history_path, job.name):
        append(
            history_path, HistoryRecord.new(ts=now, job=job.name, state="registered")
        )
        return f"{job.name}: registered", False

    open_run_skip = _guard_open_run(
        job, history_path, client=client, now=now, run_timeout_ms=run_timeout_ms
    )
    if open_run_skip is not None:
        return open_run_skip

    live_skip = _skip_if_agent_live(
        job,
        history_path,
        client=client,
        now=now,
        live_check=_live_audit_agent_exists,
    )
    if live_skip is not None:
        return live_skip

    return _fire_triggers(
        job,
        history_path,
        client=client,
        now=now,
        run_timeout_ms=run_timeout_ms,
        live_check=_live_audit_agent_exists,
    )


def _process_gated_job(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool]:
    """Decide a gated job under the brief global lock, then dispatch its checks/fix
    worker under the per-job lock (issues 049, 079)."""
    with tick_lock(default_lock_path()) as acquired:
        if not acquired:
            return _skip_on_tick_lock(job)
        pending = _decide_gated_job(
            job,
            history_path,
            client=client,
            now=now,
            trusted_review_authors=trusted_review_authors,
        )
    return _finish_dispatch(pending, client=client)


def _decide_gated_job(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> tuple[str, bool] | _PendingDispatch:
    # Standard guards (same as _process_job for regular jobs).
    if not has_ever_been_seen(history_path, job.name):
        append(
            history_path, HistoryRecord.new(ts=now, job=job.name, state="registered")
        )
        return f"{job.name}: registered", False

    open_run_skip = _guard_open_run(
        job, history_path, client=client, now=now, run_timeout_ms=job.timeout_ms
    )
    if open_run_skip is not None:
        return open_run_skip

    live_skip = _skip_if_agent_live(
        job,
        history_path,
        client=client,
        now=now,
        live_check=_live_agent_exists,
    )
    if live_skip is not None:
        return live_skip

    return _fire_triggers(
        job,
        history_path,
        client=client,
        now=now,
        trusted_review_authors=trusted_review_authors,
        run_timeout_ms=job.timeout_ms,
        live_check=_live_agent_exists,
    )


def _fire_triggers(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
    run_timeout_ms: int | None = None,
    live_check: Callable[[HerdrClient, Job], bool] | None = None,
) -> tuple[str, bool] | _PendingDispatch:
    """What the job's triggers decide this tick (issue 075), after the guards (registered,
    stale) every kind shares.

    Returns a `_PendingDispatch` when an occurrence is due: the caller has been running
    under the brief global decision lock and now releases it before the per-job dispatch
    lock is taken (issue 079).

    The cron trigger goes first: a due occurrence dispatches and the event poll waits for
    the next tick, so one tick starts at most one run per job. A cron that is not due, or
    missed, falls through to the event trigger. A job with neither fires nothing and writes
    nothing: a request-only job is never reported missed.

    A gated job whose `github_event` check *is* its gate (issue 077) takes the event path
    every tick: its cron no longer has a gate to evaluate, and the moved check keeps
    014's poll-and-fire behaviour."""
    if run_timeout_ms is None:
        run_timeout_ms = job.timeout_ms * max(1, job.max_targets_per_run)
    if live_check is None:
        live_check = _live_agent_exists
    event_trigger, event_is_gate = _event_trigger_for(job)
    cron_summary: str | None = None
    if not event_is_gate and job.cron is not None:
        # A failed run's pending retry is due work, not a missed occurrence (issue 088).
        retry_dispatch = _pending_retry_dispatch(
            job,
            history_path,
            client=client,
            now=now,
            trusted_review_authors=trusted_review_authors,
            run_timeout_ms=run_timeout_ms,
            live_check=live_check,
        )
        if retry_dispatch is not None:
            return retry_dispatch
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

        if result.decision == Decision.RUN:
            run_id = make_run_id(job.name, result.occurrence)
            # Only a job that has already finished a scheduled run has a real "since" to
            # collapse against. A never-run job's registration is a scheduler watermark,
            # not missed work, so its first run does not invent a `missed` record.
            collapsed: HistoryRecord | None = None
            if result.skipped_occurrences and last is not None:
                collapsed = HistoryRecord.new(
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
                )
            return _pending_occurrence(
                job,
                history_path,
                Occurrence(
                    run_id,
                    HistoryExtra(
                        trigger=TRIGGER_CRON,
                        scheduled_for=result.occurrence.isoformat(),
                        late_seconds=result.late_seconds,
                    ),
                ),
                client=client,
                trusted_review_authors=trusted_review_authors,
                run_timeout_ms=run_timeout_ms,
                live_check=live_check,
                before_dispatch=() if collapsed is None else (collapsed,),
            )

        if result.decision == Decision.MISSED:
            extra = HistoryExtra(
                reason="outside_catch_up_window",
                occurrence=result.occurrence.isoformat(),
            )
            if result.skipped_occurrences:
                extra["skipped_occurrences"] = result.skipped_occurrences
            append(
                history_path,
                HistoryRecord.new(ts=now, job=job.name, state="missed", extra=extra),
            )
            if job.on_missed == "notify":
                _notify(
                    client,
                    f"herdr-routines: {job.name} missed",
                    body="outside catch-up window",
                    sound="request",
                )
            cron_summary = f"{job.name}: missed"
        else:
            cron_summary = f"{job.name}: not due"

    if event_trigger is None:
        return cron_summary or f"{job.name}: not due (request only)", False

    event_result = _poll_event_trigger(
        job,
        history_path,
        client=client,
        now=now,
        trusted_review_authors=trusted_review_authors,
        run_timeout_ms=run_timeout_ms,
        live_check=live_check,
    )
    if isinstance(event_result, _PendingDispatch):
        if cron_summary is not None:
            return replace(event_result, prefix=cron_summary)
        return event_result
    event_summary, failed = event_result
    if cron_summary is not None:
        return f"{cron_summary}; {event_summary}", failed
    return event_summary, failed


# `phase` on the records `_poll_event_trigger` writes, so a poll's own history (watermark,
# failures) is told apart from the runs it started, which carry the same `trigger: event`.
_EVENT_POLL_PHASE = "event_poll"


def _last_event_poll_failed(history_path: Path, job_name: str) -> bool:
    for r in reversed(read_job(history_path, job_name)):
        if r.extra and r.extra.get("phase") == _EVENT_POLL_PHASE:
            return r.state == "failed"
    return False


def _poll_event_trigger(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...],
    run_timeout_ms: int,
    live_check: Callable[[HerdrClient, Job], bool],
) -> tuple[str, bool] | _PendingDispatch:
    """The `trigger.event` poll (issue 075): run the job once for the first new matching
    event, through the same `dispatch_occurrence` a cron firing uses.

    Dedupe is issue 014's: the polled event ids minus `_last_seen_event_ids`, the latest
    watermark, so an event that clears and comes back fires again. At most one event fires
    per tick (the rest stay out of the watermark and fire on later ticks), which keeps a
    tick's worst case at one run per job, as `validate`'s timeout budget assumes.

    The firing watermark is written once the dispatch has won the job lock and passed its
    guards, but *before* the run starts (issue 079): a skipped dispatch leaves the event to
    fire later, and a crash mid-run never re-fires it. The poll itself (`ensure_repo` and
    `gh`) runs in the decision phase under the global tick lock, so that lock is held for
    the poll's network round trips too, not only for the schedule decision. A failed run is retried by its own `retry_on` (a later tick, issue 088) / `fallback_model`, not here.
    Every record this writes without a run_id carries `trigger: event`, so none of them
    counts toward `schedule.decide`. A poll failure fails closed (never "no events") and
    pages once per outage, not every tick.

    A gated job whose `github_event` check was moved here (issue 077) keeps 014's whole
    poll path instead: per-event attempt budget, baseline adoption, head-ref validation
    and its `gate=passed/failed` record shapes. It is the same `_process_event_target`
    014 ran on the cron, now reached from the single trigger entry point."""
    event_trigger, event_is_gate = _event_trigger_for(job)
    if event_is_gate:
        if event_trigger is None:
            raise RuntimeError(f"{job.name}: no event trigger configured")
        run_id = make_run_id(job.name, now)
        return _PendingDispatch(
            job=job,
            history_path=history_path,
            run_timeout_ms=run_timeout_ms,
            live_check=live_check,
            dispatch=lambda dispatched_at: _process_event_target(
                job,
                history_path,
                client=client,
                now=dispatched_at,
                run_id=run_id,
                check=event_trigger,
                trusted_review_authors=trusted_review_authors,
            ),
            run_id=run_id,
            trigger=TRIGGER_EVENT,
        )

    check = job.trigger_event
    if check is None:
        raise RuntimeError(f"{job.name}: no event trigger configured")
    event_target = _event_target(check)
    previously_failed = _last_event_poll_failed(history_path, job.name)

    def _poll_failed(reason: str, error: str) -> tuple[str, bool]:
        append(
            history_path,
            HistoryRecord.new(
                ts=now,
                job=job.name,
                state="failed",
                extra=HistoryExtra(
                    trigger=TRIGGER_EVENT,
                    phase=_EVENT_POLL_PHASE,
                    reason=reason,
                    error=error,
                    target=event_target,
                    query=check.query,
                ),
            ),
        )
        if not previously_failed and _notify_gate(job, "failure"):
            _notify(
                client,
                f"herdr-routines: {job.name} failed",
                body=reason,
                sound="request",
            )
        return f"{job.name}: failed ({reason})", True

    # The poll runs under the global tick lock, so it never waits for the checkout: one
    # non-blocking try, held across the sync (`ensure_repo` re-enters it). A holder is
    # another job's dispatch (issue 079), e.g. a `workspace: root` run working in the
    # tree. That is not an outage: skip quietly, leave the watermark alone so no event is
    # lost, and poll again next tick.
    with checkout_lock(job.repo) as checkout_free:
        if not checkout_free:
            log.info("%s: event poll skipped, checkout %s busy", job.name, job.repo)
            return f"{job.name}: not due (checkout busy)", False
        try:
            ensure_repo(job)
        except (RuntimeError, OSError) as e:
            sync_error: Exception | None = e
        else:
            sync_error = None
    if sync_error is not None:
        return _poll_failed(
            "clone_failed" if not (job.repo / ".git").exists() else "repo_sync_failed",
            str(sync_error),
        )
    try:
        owner, repo_name = _resolve_event_owner_repo(job, check)
    except Exception as e:  # noqa: BLE001
        return _poll_failed("repo_detection_failed", str(e))
    try:
        events = poll_github_event(
            RealGhClient(),
            owner=owner,
            repo=repo_name,
            check=check,
            trusted_review_authors=trusted_review_authors,
        )
    except Exception as e:  # noqa: BLE001
        return _poll_failed("github_event_poll_failed", str(e))

    last_seen = _last_seen_event_ids(history_path, job.name)

    def _poll_record(
        watermark: list[str], reason: str, *, new: int, dispatched: int
    ) -> HistoryRecord:
        return HistoryRecord.new(
            ts=now,
            job=job.name,
            state="done",
            extra=HistoryExtra(
                trigger=TRIGGER_EVENT,
                phase=_EVENT_POLL_PHASE,
                reason=reason,
                target=event_target,
                query=check.query,
                polled=len(events),
                new=new,
                dispatched=dispatched,
                last_seen_event_ids=watermark,
            ),
        )

    if last_seen is None and check.query == "issues:opened":
        # Same first-poll rule as 014's gate: adopt the open issues as the baseline.
        append(
            history_path,
            _poll_record(
                [e.event_id for e in events], "event_baseline", new=0, dispatched=0
            ),
        )
        return f"{job.name}: baseline ({len(events)} open issues adopted)", False

    seen = set(last_seen or [])
    new_events = [e for e in events if e.event_id not in seen]
    fired = new_events[:1]
    keep = seen | {e.event_id for e in fired}
    watermark = [e.event_id for e in events if e.event_id in keep]

    # A quiet poll writes nothing, so a */5 tick adds no history line; a changed watermark
    # or the first poll after a failed one (which ends the outage) does.
    if not fired:
        if watermark != (last_seen or []) or previously_failed:
            append(
                history_path,
                _poll_record(
                    watermark, "event_poll", new=len(new_events), dispatched=0
                ),
            )
        return f"{job.name}: not due (no new events)", False

    # A firing poll's watermark lands only once the dispatch has won the job lock and
    # passed its guards (issue 079): a skipped dispatch must leave the event unseen, so a
    # later poll fires it. It still lands before the run starts, so a crash mid-run never
    # re-fires it.
    event = fired[0]
    firing_record = _poll_record(
        watermark, "event_poll", new=len(new_events), dispatched=len(fired)
    )
    return _pending_occurrence(
        job,
        history_path,
        Occurrence(
            make_run_id(job.name, now),
            HistoryExtra(
                trigger=TRIGGER_EVENT,
                event_id=event.event_id,
                event_number=event.number,
            ),
        ),
        client=client,
        trusted_review_authors=trusted_review_authors,
        run_timeout_ms=run_timeout_ms,
        live_check=live_check,
        before_dispatch=(firing_record,),
    )


# Why a dispatch that was decided did not start (issue 079). Every one but
# `already_running` writes a `skipped` record stamped with the occurrence's trigger; a
# manual or request run maps any of them to a refusal (rc 1, plus a notification for
# `request`), never to a quiet success.
DispatchRefusal = Literal[
    "dispatch_in_flight", "already_running", "agent_name_live", "checkout_busy"
]

# Triggers a person asked for. Their dispatch trusts the job lock alone for "is a run
# in flight", like issue 074's manual run did under the old global lock: a fresh `running`
# record with the lock free belongs to a dead process, and refusing a retry for it would
# strand the operator until the record goes stale.
_PERSON_TRIGGERS: Final = frozenset({TRIGGER_MANUAL, TRIGGER_REQUEST})


@dataclass(frozen=True, slots=True)
class _PendingDispatch:
    """A firing whose schedule decision ran under the brief global tick lock, now waiting
    to be dispatched under its per-job lock (issue 079). The callable is either the shared
    `dispatch_occurrence` or the gated event-target cycle, which never routed through it."""

    job: Job
    history_path: Path
    run_timeout_ms: int
    live_check: Callable[[HerdrClient, Job], bool]
    # Takes the dispatch-time clock, captured under the job lock (issue 079): a sequential
    # tick can reach this job long after its decision, so tick-start `now` is not its time.
    dispatch: Callable[[datetime], tuple[str, bool]]
    # The occurrence's run_id, so a sync that finds the checkout busy can close the
    # `running` record it already wrote instead of leaving it open.
    run_id: str
    # The occurrence's `extra.trigger`. Stamped on every skip this dispatch writes, so a
    # refused manual run is off-schedule and never consumes a cron occurrence.
    trigger: str
    prefix: str | None = None
    # Records that belong to this firing but may land only once it really dispatches: the
    # collapsed-earlier-occurrences record and a firing event poll's watermark. A skip
    # must leave the occurrence (and the event) owed, not recorded as handled.
    before_dispatch: tuple[HistoryRecord, ...] = ()
    # The occurrence's own `extra` (trigger, scheduled_for, retry attempt): what a failed
    # dispatch needs to queue its retry (issue 088). Empty for a gated event-target cycle,
    # which never retries.
    occurrence_extra: HistoryExtra = field(default_factory=lambda: HistoryExtra())


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    summary: str
    failed: bool
    refusal: DispatchRefusal | None = None


def _pending_occurrence(
    job: Job,
    history_path: Path,
    occ: Occurrence,
    *,
    client: HerdrClient,
    trusted_review_authors: tuple[str, ...],
    run_timeout_ms: int,
    live_check: Callable[[HerdrClient, Job], bool],
    before_dispatch: tuple[HistoryRecord, ...] = (),
) -> _PendingDispatch:
    return _PendingDispatch(
        job=job,
        history_path=history_path,
        run_timeout_ms=run_timeout_ms,
        live_check=live_check,
        dispatch=lambda dispatched_at: dispatch_occurrence(
            job,
            history_path,
            occ,
            client=client,
            now=dispatched_at,
            trusted_review_authors=trusted_review_authors,
        ),
        run_id=occ.run_id,
        trigger=occ.extra.get("trigger") or TRIGGER_CRON,
        before_dispatch=before_dispatch,
        occurrence_extra=occ.extra,
    )


def _pending_retry_dispatch(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...],
    run_timeout_ms: int,
    live_check: Callable[[HerdrClient, Job], bool],
) -> _PendingDispatch | None:
    """The retry this job is owed and that is due this tick, if any (issue 088).

    A retry is dropped, with a `retry_dropped` record, when the job's next scheduled
    occurrence has come in the meantime (the schedule decision then handles that
    occurrence as a fresh run), or when the job no longer has retries left. One that is not
    due yet stays pending. The retry runs as a new occurrence of the original
    `scheduled_for`, with its own run id, so it gets its own history records."""
    retry = find_pending_retry(read_job(history_path, job.name))
    if retry is None:
        return None
    drop: str | None = None
    if job.cron is None or retry.failed_attempt >= job.retry_attempts:
        drop = "the job no longer has retries left"
    elif occurrences_since(job.cron, job.timezone, retry.record.ts, now):
        drop = "the next scheduled occurrence came first"
    if drop is not None:
        log.info("%s: pending retry dropped: %s", job.name, drop)
        dropped = HistoryExtra(
            reason=RETRY_DROPPED_REASON,
            trigger=TRIGGER_CRON,
            attempt=retry.failed_attempt,
            error=drop,
        )
        if retry.failed_run_id is not None:
            dropped["retried_from_run_id"] = retry.failed_run_id
        append(
            history_path,
            HistoryRecord.new(ts=now, job=job.name, state="skipped", extra=dropped),
        )
        return None
    if now < retry.due_at:
        log.info(
            "%s: retry %d/%d pending until %s",
            job.name,
            retry.failed_attempt + 1,
            job.retry_attempts,
            retry.due_at.isoformat(),
        )
        return None
    extra = HistoryExtra(
        trigger=TRIGGER_CRON,
        attempt=retry.failed_attempt + 1,
        max_retries=job.retry_attempts,
    )
    if retry.scheduled_for is not None:
        extra["scheduled_for"] = retry.scheduled_for
    if retry.failed_run_id is not None:
        extra["retried_from_run_id"] = retry.failed_run_id
    log.info(
        "%s: retry attempt %d/%d after %s",
        job.name,
        retry.failed_attempt + 1,
        job.retry_attempts,
        (retry.record.extra or {}).get("retry_eligible_reason"),
    )
    return _pending_occurrence(
        job,
        history_path,
        Occurrence(make_run_id(job.name, now), extra),
        client=client,
        trusted_review_authors=trusted_review_authors,
        run_timeout_ms=run_timeout_ms,
        live_check=live_check,
    )


def _queue_retry(
    pending: _PendingDispatch, *, records_before: int, history_path: Path
) -> None:
    """After a dispatch, queue its retry when it ended in a failure `retry_on` lists and
    attempts are left (issue 088). Only a cron-triggered run retries: a manual or request run
    has an operator watching, and an event run is not polled again."""
    job = pending.job
    if pending.trigger != TRIGGER_CRON or job.retry_attempts <= 0:
        return
    failure = eligible_failure(read_job(history_path, job.name)[records_before:], job)
    if failure is None:
        return
    record = pending_retry_record(
        job,
        failure,
        failed_attempt=pending.occurrence_extra.get("attempt", 0),
        occurrence_extra=pending.occurrence_extra,
    )
    if record is None:
        return
    append(history_path, record)
    log.info(
        "%s: %s, retry queued for %s",
        job.name,
        (failure.extra or {}).get("reason"),
        (record.extra or {}).get("retry_due_at"),
    )


def _root_checkout_lock(job: Job) -> AbstractContextManager[bool]:
    """The checkout lock a `workspace: root` dispatch holds for its whole run (issue 079):
    its agent works *in* the checkout, so no other job may fetch, fast-forward or run
    there meanwhile. Non-blocking, since a tick dispatches its jobs one after another and
    waiting here would stall every later job behind an hour-long run. Worktree-mode jobs
    only lock the checkout around their brief sync, inside `repos._ensure_checkout`."""
    if job.workspace != "root" or job.kind == "pipeline" or is_repo_less(job):
        return nullcontext(True)
    return checkout_lock(job.repo)


def _checkout_busy_skip_recorded(
    history_path: Path, job_name: str, trigger: str
) -> bool:
    """Whether this trigger already has a `checkout_busy` skip since the job last really
    ran (issue 079). A contended checkout leaves the occurrence owed, so a root job blocked
    by another root job's run on the same checkout would otherwise write one `skipped`
    record per tick for the holder's whole run."""
    for record in reversed(read_job(history_path, job_name)):
        extra = record.extra or {}
        if record.state == "running":
            return False
        if record.state in TERMINAL_STATES:
            if extra.get("reason") not in NOT_A_RUN_REASONS:
                return False
            if (
                extra.get("reason") == "checkout_busy"
                and extra.get("trigger") == trigger
            ):
                return True
    return False


def _dispatch_pending(
    pending: _PendingDispatch, *, client: HerdrClient
) -> DispatchOutcome:
    """Take the per-job lock and dispatch one decided firing (issue 079).

    Losing the non-blocking lock is skip-not-queue: the loser records `skipped`
    (`dispatch_in_flight`) so a starved firing is visible in history, but that reason never
    consumes a cron occurrence (`history.NOT_A_RUN_REASONS`): the winner's terminal record
    does. The live-agent guard, and for scheduled firings the open-run guard, are repeated
    inside the job-locked section because the decision ran before the lock was won."""
    job = pending.job
    history_path = pending.history_path
    person = pending.trigger in _PERSON_TRIGGERS
    # Capture the clock at dispatch time, not at tick start (issue 079): a sequential tick
    # can reach this job long after its decision, and overlapping ticks may dispatch jobs
    # in a different order than they started, so every record this dispatch writes (and
    # the rotation after the tick) must use the clock as of now.
    now = datetime.now(UTC)

    def skip(
        reason: DispatchRefusal, summary: str, *, run_id: str | None = None
    ) -> DispatchOutcome:
        # A root-held checkout writes no `running` record, so suppress repeats of the same
        # trigger's skip; a sync that failed after `running` must still close that record.
        if (
            run_id is None
            and reason == "checkout_busy"
            and _checkout_busy_skip_recorded(history_path, job.name, pending.trigger)
        ):
            log.info("%s: %s (already recorded)", job.name, summary)
            return DispatchOutcome(f"{job.name}: {summary}", False, refusal=reason)
        append(
            history_path,
            HistoryRecord.new(
                ts=now,
                job=job.name,
                state="skipped",
                run_id=run_id,
                extra=HistoryExtra(reason=reason, trigger=pending.trigger),
            ),
        )
        return DispatchOutcome(f"{job.name}: {summary}", False, refusal=reason)

    with job_lock(history_path, job.name) as acquired:
        if not acquired:
            return skip("dispatch_in_flight", "skipped (dispatch in flight)")

        if (
            not person
            and job.kind != "pipeline"
            and is_currently_running(
                history_path, job.name, timeout_ms=pending.run_timeout_ms, now=now
            )
        ):
            return DispatchOutcome(
                f"{job.name}: skipped (already running)",
                False,
                refusal="already_running",
            )

        # A manual or request run of a pipeline must be refused, not launched alongside an
        # open run (and not silently reported as a failure): the pipeline branch of
        # `dispatch_occurrence` cannot express a refusal, so guard here.
        if (
            job.kind == "pipeline"
            and person
            and _open_pipeline_run(history_path, job.name) is not None
        ):
            return skip("already_running", "skipped (already running)")

        if pending.live_check(client, job):
            return skip("agent_name_live", "skipped (agent already live)")

        with _root_checkout_lock(job) as owned:
            if not owned:
                return skip("checkout_busy", "skipped (checkout busy)")
            for record in pending.before_dispatch:
                append(history_path, replace(record, ts=now))
            records_before = len(read_job(history_path, job.name))
            try:
                summary, failed = pending.dispatch(now)
            except CheckoutBusy as e:
                # The sync met another dispatch's checkout holder (issue 079). That is
                # contention, not a failed run: close any `running` record the dispatch
                # wrote (a pipeline launch writes none) and leave the occurrence owed.
                log.info("%s: dispatch skipped: %s", job.name, e)
                return skip(
                    "checkout_busy",
                    "skipped (checkout busy)",
                    run_id=None if job.kind == "pipeline" else pending.run_id,
                )
            if failed:
                _queue_retry(
                    pending, records_before=records_before, history_path=history_path
                )
        if pending.prefix is not None:
            summary = f"{pending.prefix}; {summary}"
        return DispatchOutcome(summary, failed)


def _finish_dispatch(
    pending: tuple[str, bool] | _PendingDispatch,
    *,
    client: HerdrClient,
) -> tuple[str, bool]:
    if not isinstance(pending, _PendingDispatch):
        return pending
    outcome = _dispatch_pending(pending, client=client)
    return outcome.summary, outcome.failed


def dispatch_manual_occurrence(
    job: Job,
    history_path: Path,
    occ: Occurrence,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...] = DEFAULT_TRUSTED_REVIEW_AUTHORS,
) -> DispatchOutcome:
    """Dispatch a manual or request occurrence under the job lock (issues 074, 079).

    A non-None `refusal` means nothing was dispatched; `run` maps it to rc 1 and, for a
    request, a notification. It is the same lock-and-reverify path the tick's
    `_dispatch_pending` uses, so the two entry points cannot drift."""
    run_timeout_ms = job.timeout_ms * max(1, job.max_targets_per_run)
    live_check = _live_audit_agent_exists if job.kind == "audit" else _live_agent_exists
    return _dispatch_pending(
        _pending_occurrence(
            job,
            history_path,
            occ,
            client=client,
            trusted_review_authors=trusted_review_authors,
            run_timeout_ms=run_timeout_ms,
            live_check=live_check,
        ),
        client=client,
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
        # An off-schedule run of a pipeline with an open run is refused by
        # `_dispatch_pending` (an `already_running` refusal); this entry point only ever
        # launches a firing the job-locked dispatch already cleared.
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
    route to the pr / base target. The tick and `run` both land here (issue 074). A
    `github_event` check no longer dispatches here: it is the job's trigger (issue 077),
    so the poll in `_poll_event_trigger` runs `_process_event_target` instead."""
    job.require_checks()
    target = job.require_target()
    run_id = occ.run_id

    _append_running(job, history_path, occ, now=now)

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

    # A multi-target run (issue 076) acts on up to `max_targets_per_run` targets one after
    # the other, so the running-record staleness window scales with it.
    run_timeout_ms = job.timeout_ms * max(1, job.max_targets_per_run)

    with tick_lock(default_lock_path()) as acquired:
        if not acquired:
            return _skip_on_tick_lock(job)
        pending = _decide_routine_job(
            job,
            history_path,
            client=client,
            now=now,
            trusted_review_authors=trusted_review_authors,
            run_timeout_ms=run_timeout_ms,
        )
    return _finish_dispatch(pending, client=client)


def _decide_routine_job(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    trusted_review_authors: tuple[str, ...],
    run_timeout_ms: int,
) -> tuple[str, bool] | _PendingDispatch:
    if not has_ever_been_seen(history_path, job.name):
        append(
            history_path, HistoryRecord.new(ts=now, job=job.name, state="registered")
        )
        return f"{job.name}: registered", False

    open_run_skip = _guard_open_run(
        job, history_path, client=client, now=now, run_timeout_ms=run_timeout_ms
    )
    if open_run_skip is not None:
        return open_run_skip

    live_skip = _skip_if_agent_live(
        job,
        history_path,
        client=client,
        now=now,
        live_check=_live_agent_exists,
    )
    if live_skip is not None:
        return live_skip

    return _fire_triggers(
        job,
        history_path,
        client=client,
        now=now,
        trusted_review_authors=trusted_review_authors,
        run_timeout_ms=run_timeout_ms,
        live_check=_live_agent_exists,
    )


def _process_pipeline_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    """Decide a kind: pipeline job under the brief global lock, then launch under its
    per-job lock (issues 026, 079). Reconcile records belong to the decision phase; the
    launch itself is the per-job dispatch."""
    with tick_lock(default_lock_path()) as acquired:
        if not acquired:
            return _skip_on_tick_lock(job)
        pending = _decide_pipeline_job(job, history_path, client=client, now=now)
    return _finish_dispatch(pending, client=client)


def _decide_pipeline_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool] | _PendingDispatch:
    if not has_ever_been_seen(history_path, job.name):
        append(
            history_path, HistoryRecord.new(ts=now, job=job.name, state="registered")
        )
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
                HistoryRecord.new(
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
                HistoryRecord.new(
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
            HistoryRecord.new(
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
            HistoryRecord.new(
                ts=now,
                job=job.name,
                state="skipped",
                extra=HistoryExtra(reason="agent_name_live"),
            ),
        )
        return f"{job.name}: skipped (agent already live)", False

    # Request-only (issue 075). After the reconcile block above, so a run started by `run` or
    # `/job` still gets reconciled; nothing to decide and nothing to report missed.
    if job.cron is None:
        return f"{job.name}: not due (request only)", False

    retry_dispatch = _pending_retry_dispatch(
        job,
        history_path,
        client=client,
        now=now,
        trusted_review_authors=DEFAULT_TRUSTED_REVIEW_AUTHORS,
        run_timeout_ms=job.timeout_ms,
        live_check=_live_agent_exists,
    )
    if retry_dispatch is not None:
        return retry_dispatch

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
            HistoryRecord.new(
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
    return _pending_occurrence(
        job,
        history_path,
        Occurrence(
            make_run_id(job.name, result.occurrence),
            HistoryExtra(
                trigger=TRIGGER_CRON,
                scheduled_for=result.occurrence.isoformat(),
                late_seconds=result.late_seconds,
            ),
        ),
        client=client,
        trusted_review_authors=DEFAULT_TRUSTED_REVIEW_AUTHORS,
        run_timeout_ms=job.timeout_ms,
        live_check=_live_agent_exists,
    )
