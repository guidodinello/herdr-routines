"""The systemd entrypoint: acquire the tick lock, load config, decide which jobs are due, run
them, write history. This is what `herdr-routines tick` calls. See docs/plan-v1.md §3/§4.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from logger import get_logger

from herdr_routines.auto_fix import (
    EligiblePR,
    PRInfo,
    RealGhClient,
    attempt_count_for_gate_branch,
    attempt_count_for_pr,
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
    read_job,
)
from herdr_routines.repos import ensure_repo
from herdr_routines.runner import (
    RunOutcome,
    default_reports_dir,
    execute_run,
    make_run_id,
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
    return TickOutcome(summaries=tuple(summaries), any_job_failed=any_job_failed)


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
            _notify(
                client,
                f"herdr-routines: {job.name} PR #{elig_pr.pr.number} skipped",
                body="max_attempts_exceeded",
                sound="request",
            )
            skipped_count += 1
            continue

        pr_agent_name = build_pr_agent_name(job.name, elig_pr.pr.number)
        try:
            status = client.agent_statuses().get(pr_agent_name)
        except HerdrCliError:
            status = None
        if status in LIVE_AGENT_STATUSES:
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
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body=f"{dispatched_count} dispatched, {skipped_count} skipped",
            sound="request",
        )
        return summary, True

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

    try:
        tail = client.agent_read(agent_name, lines=200)
        if tail:
            (report_path.parent / f"{run_id}.tail.txt").write_text(tail)
    except OSError:
        pass

    report_written = report_path.exists()
    _report_bytes = report_path.stat().st_size if report_written else 0

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
            },
        ),
    )

    if state == "failed":
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body="agent_prompt_failed",
            sound="request",
        )
        return f"{job.name}: failed (agent_prompt_failed)", True

    _notify(client, f"herdr-routines: {job.name} done", sound="done")
    return f"{job.name}: done", False


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

    # Create worktree pinned to PR head branch (review finding B)
    wt_path = Path(job.repo) / ".worktrees" / f"autofix-pr{pr.number}"
    try:
        # Remove stale worktree if it exists from a prior attempt
        subprocess.run(
            ["git", "-C", str(job.repo), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        proc = subprocess.run(
            ["git", "-C", str(job.repo), "worktree", "add", str(wt_path), pr.head_ref],
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
    return extra


def _process_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    # kind: pipeline dispatches a detached systemd-run unit and returns immediately —
    # tick must never block on the multi-hour orchestrator run (issue 026). Checked first
    # since config.py already rejects `checks:` on a pipeline job (mutually exclusive
    # dispatch paths); the order here is belt-and-braces, not load-bearing.
    if job.kind == "pipeline":
        return _process_pipeline_job(job, history_path, client=client, now=now)

    # Gated jobs follow the same schedule guards but run gate checks + dispatch
    # instead of execute_run when their cron fires.
    if job.checks is not None:
        return _process_gated_job(job, history_path, client=client, now=now)

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
    used_fallback = False

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
        # `now` (wall-clock) rather than `result.occurrence` for the fallback's own run_id
        # (both are fine now that `build_branch_name` is injective in run_id — see its
        # docstring — but keeping `now` preserves the original PR #65 intent of the fallback
        # attempt recording when it actually ran, not the primary's scheduled occurrence).
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

    if outcome.state == "done":
        done_body = (
            f"via fallback_model={job.fallback_model}" if used_fallback else None
        )
        _notify(
            client, f"herdr-routines: {job.name} done", body=done_body, sound="done"
        )
        return f"{job.name}: done", False

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


def _classify_pipeline_outcome(report_text: str) -> tuple[str, str | None]:
    """(history_state, reason) from a terminal report's first `## Outcome:` line.

    `partial (deadline exceeded)` is tolerated content-wise (the orchestrator is
    documented to wait out an in-flight stage before writing it, design.md G-7) but is
    still reported `failed` here — a run that didn't finish is not silently green."""
    match = _OUTCOME_RE.search(report_text)
    if match is None:
        return "interrupted_unknown", "outcome_marker_missing"
    status = match.group("status").strip().lower()
    if status.startswith("ok"):
        return "done", None
    if status.startswith("partial"):
        return "failed", "partial_deadline"
    if status.startswith("failed"):
        if "watchdog" in status:
            return "failed", "watchdog_killed"
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


def _build_pipeline_launch_argv(
    job: Job, *, run_id: str, report_path: Path, unit_name: str
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
    ]
    if job.model:
        argv += ["--model", job.model]
    return argv


def _process_pipeline_job(
    job: Job, history_path: Path, *, client: HerdrClient, now: datetime
) -> tuple[str, bool]:
    if not has_ever_been_seen(history_path, job.name):
        append(history_path, HistoryRecord(ts=now, job=job.name, state="registered"))
        return f"{job.name}: registered", False

    open_run = _open_pipeline_run(history_path, job.name)
    if open_run is not None:
        if _live_agent_exists(client, job):
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
                _notify(client, f"herdr-routines: {job.name} done", sound="done")
                return f"{job.name}: done", False
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
        # next tick. Only a report (handled above) or a genuinely stale deadline
        # (handled below) is allowed to close this run out.
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
    report_path = pipeline_report_path(bare_run_id)
    unit_name = f"herdr-pipeline-{bare_run_id}"

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
        _notify(
            client,
            f"herdr-routines: {job.name} failed",
            body="repo_sync_failed",
            sound="request",
        )
        return f"{job.name}: failed (repo_sync_failed)", True

    argv = _build_pipeline_launch_argv(
        job, run_id=bare_run_id, report_path=report_path, unit_name=unit_name
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
                "scheduled_for": result.occurrence.isoformat(),
                "late_seconds": result.late_seconds,
                "pipeline_run_id": bare_run_id,
                "report": str(report_path),
                "unit": unit_name,
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
