"""The systemd entrypoint: acquire the tick lock, load config, decide which jobs are due, run
them, write history. This is what `herdr-routines tick` calls. See docs/plan-v1.md §3/§4.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from logger import get_logger

from herdr_routines.auto_fix import (
    EligiblePR,
    PRInfo,
    RealGhClient,
    attempt_count_for_pr,
    build_fix_prompt,
    build_pr_agent_name,
    build_worker_agent_name,
    fetch_failing_checks,
    fetch_thread_bodies,
    is_eligible,
    list_open_prs,
    repo_owner_and_name,
)
from herdr_routines.config import AutoFixConfig, Job, RoutinesConfig
from herdr_routines.herdr import LIVE_AGENT_STATUSES, HerdrClient, HerdrCliError
from herdr_routines.history import (
    HistoryRecord,
    append,
    find_stale_running,
    first_seen_at,
    has_ever_been_seen,
    is_currently_running,
    last_terminal_run,
    read_job,
)
from herdr_routines.runner import execute_run, make_run_id
from herdr_routines.schedule import Decision, decide

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
    summaries: list[str] = []
    any_job_failed = False
    for job in config.jobs:
        if not job.enabled:
            continue
        summary, failed = _process_job(job, history_path, client=client, now=now)
        summaries.append(summary)
        any_job_failed = any_job_failed or failed
    return TickOutcome(summaries=tuple(summaries), any_job_failed=any_job_failed)


def _process_auto_fix_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    """Process an auto-fix PR standing job: enumerate eligible PRs, check retry
    budget, dispatch bounded fix workers."""
    assert job.auto_fix is not None
    af = job.auto_fix

    # Standard guards (same as _process_job for regular jobs).
    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=job.timeout_ms, now=now
    )
    if stale is not None:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="interrupted_unknown",
                run_id=stale.run_id,
                extra={"reason": "stale_running_record"},
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

    # Decision.RUN — enumerate eligible PRs
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

    # --- Auto-fix enumeration and dispatch ---
    gh = RealGhClient()

    # Detect repo owner/name from git remote
    try:
        proc = subprocess.run(
            ["git", "-C", str(job.repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git remote failed: {proc.stderr.strip()}")
        owner, repo_name = repo_owner_and_name(proc.stdout.strip())
    except Exception as e:
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
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body="repo_detection_failed",
            sound="request",
        )
        return f"{job.name}: failed (repo_detection_failed)", True

    # Validate gh auth
    try:
        author = gh.api_user()
    except Exception as e:
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
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body="gh_auth_missing",
            sound="request",
        )
        return f"{job.name}: failed (gh_auth_missing)", True

    # Enumerate open PRs
    open_prs = list_open_prs(
        gh, owner=owner, repo=repo_name,
        branch_prefix=af.branch_prefix, author=author,
    )

    # Check eligibility for each PR
    eligible: list[EligiblePR] = []
    for pr_info in open_prs:
        elig = is_eligible(gh, owner=owner, repo=repo_name, number=pr_info.number)
        if elig is not None:
            eligible.append(
                EligiblePR(
                    pr=PRInfo(
                        number=pr_info.number,
                        head_ref=pr_info.head_ref,
                        author=pr_info.author,
                        url=pr_info.url,
                    ),
                    reason=elig.reason,
                )
            )

    # Sort oldest-first (PR number ascending) and cap
    eligible.sort(key=lambda e: e.pr.number)
    dispatched = eligible[: af.max_prs_per_tick]
    skipped_over_cap = len(eligible) - len(dispatched)

    # Check retry budget and dispatch
    any_failed = False
    dispatched_count = 0
    skipped_count = skipped_over_cap
    for elig_pr in dispatched:
        attempt = attempt_count_for_pr(history_path, job.name, elig_pr.pr.number)
        if attempt >= af.max_attempts_per_pr:
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
            _notify(
                client,
                f"herdr-routines: {job.name} PR #{elig_pr.pr.number} skipped",
                body="max_attempts_exceeded",
                sound="request",
            )
            skipped_count += 1
            continue

        # Per-PR live-agent check using run_id-less name (review finding E)
        pr_agent_name = build_pr_agent_name(job.name, elig_pr.pr.number)
        try:
            status = client.agent_statuses().get(pr_agent_name)
        except HerdrCliError:
            status = None
        if status in LIVE_AGENT_STATUSES:
            continue

        # Fetch real failing checks and thread bodies for the prompt (review finding C)
        failing_checks = fetch_failing_checks(gh, owner=owner, repo=repo_name, number=elig_pr.pr.number)
        thread_bodies = fetch_thread_bodies(gh, owner=owner, repo=repo_name, number=elig_pr.pr.number)

        # Dispatch fix worker
        worker_outcome = _dispatch_fix_worker(
            job=job,
            af=af,
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
        }

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

    # Write aggregate report file (review finding G)
    try:
        from herdr_routines.runner import default_reports_dir
        reports_dir = default_reports_dir()
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{run_id}.md"
        report_lines = [
            f"# Auto-fix tick report: {run_id}",
            "",
            f"- **Job**: {job.name}",
            f"- **Time**: {now.isoformat()}",
            f"- **Enumerated**: {len(open_prs)} open PRs with branch prefix `{af.branch_prefix}`",
            f"- **Eligible**: {len(eligible)} PRs with failing CI or unresolved threads",
            f"- **Dispatched**: {dispatched_count} fix workers",
            f"- **Skipped (over cap)**: {skipped_over_cap}",
            f"- **Skipped (max attempts)**: {skipped_count - skipped_over_cap}",
            "",
        ]
        for elig_pr in dispatched:
            report_lines.append(f"- PR #{elig_pr.pr.number}: {elig_pr.reason}")
        report_path.write_text("\n".join(report_lines) + "\n")
    except Exception as e:
        log.warning("%s: could not write aggregate report: %s", job.name, e)

    # Aggregate summary
    summary = (
        f"{job.name}: done "
        f"(enumerated={len(open_prs)}, eligible={len(eligible)}, "
        f"dispatched={dispatched_count}, skipped={skipped_count})"
    )

    # Write terminal record for the auto-fix tick itself (review finding A).
    # Without this, is_currently_running() self-blocks the job for timeout_ms+5min.
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="done" if not any_failed else "failed",
            run_id=run_id,
            extra={
                "enumerated": len(open_prs),
                "eligible": len(eligible),
                "dispatched": dispatched_count,
                "skipped": skipped_count,
            },
        ),
    )

    if any_failed:
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body=f"{dispatched_count} dispatched, {skipped_count} skipped",
            sound="request",
        )
        return summary, True

    _notify(client, f"herdr-routines: {job.name} done", sound="done")
    return summary, False


def _dispatch_fix_worker(
    *,
    job: Job,
    af: AutoFixConfig,
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

    # Create report dir
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"state": "failed", "reason": "report_dir_creation_failed", "error": str(e)}

    # Build prompt with real data (review finding C) and report path (review finding D)
    prompt_text = af.prompt or build_fix_prompt(
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

    # Create worktree pinned to PR head branch (review finding B)
    wt_path = Path(job.repo) / ".worktrees" / f"autofix-pr{pr.number}"
    try:
        # Remove stale worktree if it exists from a prior attempt
        subprocess.run(
            ["git", "-C", str(job.repo), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True, text=True, timeout=15,
        )
        proc = subprocess.run(
            ["git", "-C", str(job.repo), "worktree", "add",
             str(wt_path), pr.head_ref],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    except Exception as e:
        return {"state": "failed", "reason": "worktree_creation_failed", "error": str(e)}

    # Start agent in the worktree (review finding B)
    pane_id: str | None = None
    try:
        pane_id = client.tab_create(cwd=str(wt_path), label=agent_name)
        client.agent_start(
            name=agent_name,
            kind=af.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=af.model,
        )
    except (HerdrCliError, OSError) as e:
        if pane_id is not None:
            try:
                client.pane_close(pane_id)
            except Exception:
                pass
        return {"state": "failed", "reason": "agent_start_failed", "error": str(e), "pane_id": pane_id}

    # Wait for agent readiness
    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(client, agent_name, reports_dir=report_path.parent, run_id=pr_run_id)
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return {"state": "failed", "reason": "agent_not_interactive", "error": last_error, "pane_id": pane_id}

    # Deliver prompt with watchdog
    try:
        settled_status = _prompt_with_watchdog(
            client,
            job_name=agent_name,
            target=agent_name,
            text=prompt_text,
            timeout_ms=af.timeout_ms,
            markers=job.failure_markers or ("Free usage exceeded",),
            prompt_text=prompt_text,
        )
    except Exception as e:
        _capture_visible_tail(client, agent_name, reports_dir=report_path.parent, run_id=pr_run_id)
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return {"state": "failed", "reason": "agent_prompt_failed", "error": str(e), "pane_id": pane_id}

    # Capture tail and close pane
    try:
        tail = client.agent_read(agent_name, lines=200)
        if tail:
            (report_path.parent / f"{pr_run_id}.tail.txt").write_text(tail)
    except OSError:
        pass

    report_written = report_path.exists()
    report_bytes = report_path.stat().st_size if report_written else 0

    session_id: str | None = None
    try:
        session_id = client.agent_session_id(agent_name)
    except Exception:
        pass

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


def _process_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    # Auto-fix jobs follow the same schedule guards but run enumeration+dispatch
    # instead of execute_run when their cron fires.
    if job.auto_fix is not None:
        return _process_auto_fix_job(job, history_path, client=client, now=now)

    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    stale = find_stale_running(
        history_path, job.name, timeout_ms=job.timeout_ms, now=now
    )
    if stale is not None:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="interrupted_unknown",
                run_id=stale.run_id,
                extra={"reason": "stale_running_record"},
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

    extra = {
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

    if outcome.state == "done":
        _notify(client, f"herdr-routines: {job.name} done", sound="done")
        return f"{job.name}: done", False

    _notify(
        client,
        f"herdr-routines: {job.name} failed",
        body=outcome.reason or "unknown",
        sound="request",
    )
    return f"{job.name}: failed ({outcome.reason})", True


def _notify(
    client: HerdrClient, title: str, *, body: str | None = None, sound: str = "none"
) -> None:
    """Best-effort: a notification failure (e.g. Herdr server unreachable) must not abort the
    tick — see run_tick's isolation contract above."""
    try:
        client.notification_show(title, body=body, sound=sound)
    except HerdrCliError as e:
        log.warning("%s: notification failed: %s", title, e)


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
