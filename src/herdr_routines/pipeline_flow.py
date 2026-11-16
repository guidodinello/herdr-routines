"""The `kind: pipeline` flow: launch a pipeline run as a systemd unit, find the open run,
and classify a finished run from its report.

Split out of `tick.py` (issue 060).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from logger import get_logger

from herdr_routines.config import (
    Job,
)
from herdr_routines.dispatch_common import (
    _live_agent_exists,
    _notify,
    _notify_gate,
    _with_trigger,
)
from herdr_routines.herdr import (
    HerdrClient,
    HerdrCliError,
)
from herdr_routines.history import (
    TERMINAL_STATES,
    HistoryExtra,
    HistoryRecord,
    HistoryState,
    append,
    read_job,
)
from herdr_routines.pipeline_watchdog import (
    PIPELINE_UNIT_PREFIX,
    default_heartbeat_dir,
    default_worktrees_root,
    find_inflight_runs,
    host_rebooted_after_state_write,
    is_stalled,
    pipeline_state_json_path,
    pipeline_unit_active,
    recorded_pipeline_deadlines,
    system_boot_epoch,
    validate_stage_sessions,
)
from herdr_routines.repos import (
    CheckoutBusy,
    ensure_repo,
)
from herdr_routines.runner import (
    default_reports_dir,
)

log = get_logger(__name__)


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
    show the per-stage fresh-session contract was faked, else None. None also when state.json is
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


def _classify_pipeline_outcome(report_text: str) -> tuple[HistoryState, str | None]:
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
        if "resume_failed" in status:
            return "failed", "resume_failed"
        if "watchdog" in status:
            return "failed", "watchdog_killed"
        if "quota_exhausted" in status:
            return "failed", "quota_exhausted"
        if "ci_check_failed" in status:
            return "failed", "ci_check_failed"
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
    return int(now.timestamp()) + job.require_deadline_ms() // 1000


def _build_pipeline_launch_argv(
    job: Job, *, run_id: str, report_path: Path, unit_name: str, deadline_epoch: int
) -> list[str]:
    deadline_ms = job.require_deadline_ms()
    prompt_file = job.require_prompt_file()
    runtime_max_sec = (deadline_ms + PIPELINE_UNIT_MARGIN_MS) // 1000
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
        str(job.repo / prompt_file),
        "--wait-timeout-ms",
        str(deadline_ms),
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


def _launch_pipeline_run(
    job: Job,
    history_path: Path,
    *,
    client: HerdrClient,
    now: datetime,
    run_id: str,
    bare_run_id: str,
    running_extra: HistoryExtra,
) -> tuple[str, bool]:
    """Sync the repo, launch the detached orchestrator unit, and record the outcome.
    `running_extra` goes into the `running` record's `extra` (scheduled_for/
    late_seconds)."""
    report_path = pipeline_report_path(bare_run_id)
    unit_name = f"{PIPELINE_UNIT_PREFIX}{bare_run_id}"
    # Recorded in the `running` record below: the watchdog reads it from history.
    deadline_epoch = pipeline_deadline_epoch(job, now)

    try:
        ensure_repo(job)
    except CheckoutBusy:
        # Contention, not a failed launch (issue 079): the dispatch records it as a skip.
        raise
    except RuntimeError as e:
        append(
            history_path,
            HistoryRecord(
                ts=now,
                job=job.name,
                state="failed",
                run_id=run_id,
                extra=_with_trigger(
                    running_extra,
                    HistoryExtra(reason="repo_sync_failed", error=str(e)),
                ),
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
                extra=_with_trigger(
                    running_extra,
                    HistoryExtra(
                        reason="launch_failed", rc=rc, error=err.strip()[:2000]
                    ),
                ),
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
            extra=HistoryExtra(
                **running_extra,
                pipeline_run_id=bare_run_id,
                report=str(report_path),
                unit=unit_name,
                deadline_epoch=deadline_epoch,
            ),
        ),
    )
    return f"{job.name}: dispatched ({unit_name})", False


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
      deadline + grace with a quiet heartbeat), or, if stalled, still backed by a live
      systemd unit.

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

    Stalled is only a guess at death, though: a resumed run gets a fresh unit and deadline
    but keeps the old recorded one, so a live, quiet run reads as stalled. As in the
    watchdog, a stalled run whose unit is active, or can't be queried, still counts as
    in flight; only an inactive unit makes it dead. Better to skip a night than to launch
    one on top of a live run.

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
            or pipeline_unit_active(run.run_id) is not False
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
