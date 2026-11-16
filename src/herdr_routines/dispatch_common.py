"""Pieces every dispatch flow shares: the occurrence record, notifications, worker
results, worktree helpers and agent-liveness probes. Imports no flow module.

Split out of `tick.py` (issue 060).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, assert_never

from logger import get_logger

from herdr_routines import verify
from herdr_routines.auto_fix import (
    build_audit_agent_name,
    build_gate_worker_agent_name,
    build_pr_agent_name,
)
from herdr_routines.config import (
    Job,
)
from herdr_routines.herdr import (
    LIVE_AGENT_STATUSES,
    SETTLED_AGENT_STATUSES,
    AgentStatus,
    HerdrClient,
    HerdrCliError,
)
from herdr_routines.history import (
    HistoryExtra,
    HistoryRecord,
    WorkerFailedReason,
    append,
    read_job,
)
from herdr_routines.runner import (
    default_reports_dir,
)

log = get_logger(__name__)
NotifyKind = Literal["progress", "success", "failure", "finding"]


@dataclass(frozen=True, slots=True)
class _AgentRun:
    """How one agent dispatched into a worktree ended. `reason` is set iff it failed."""

    reason: str | None = None
    error: str | None = None
    pane_id: str | None = None
    final_agent_status: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkerFailed:
    """A fix worker that never got an agent to settle: why, and the pane it had already
    opened, if any."""

    reason: WorkerFailedReason
    error: str | None
    pane_id: str | None = None
    # Set for `worktree_reuse_not_clean` (issue 070): the dirty checkout the refusal names.
    worktree_path: str | None = None


@dataclass(frozen=True, slots=True)
class _WorkerSettled:
    """A fix worker whose agent settled. `state` is `failed` when the settled status is not
    one of SETTLED_AGENT_STATUSES; `report_path` is set iff the agent wrote its report."""

    state: Literal["done", "failed"]
    pane_id: str
    report_path: str | None
    final_agent_status: AgentStatus | None
    session_id: str | None
    report_bytes: int = 0
    # The worktree the worker ran in (issue 070), so the settle site can check it.
    worktree_path: str | None = None
    # The PR head captured at dispatch (issue 078), so `pushed_or_replied` can tell
    # whether the worker moved it. None for issue events and when enrichment could not
    # read it (the gate then fails closed).
    dispatch_head_sha: str | None = None


_WorkerResult = _WorkerFailed | _WorkerSettled


def _add_worktree(repo: Path, wt_path: Path, base: str) -> None:
    """Force-remove then add a detached worktree at `base` — the sequence
    `_process_base_target` runs. Raises RuntimeError when `git worktree add` fails."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt_path), base],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")


def _run_agent_in_worktree(
    job: Job,
    client: HerdrClient,
    *,
    agent_name: str,
    wt_path: Path,
    prompt_text: str,
    timeout_ms: int,
    tail_id: str,
) -> _AgentRun:
    """Start `agent_name` in a pane at `wt_path`, prompt it and wait for it to settle,
    then close the pane. The same start / ready / watchdog / tail / close path
    `_process_base_target` uses. Never raises; the caller owns the worktree."""
    from herdr_routines.runner import (
        _capture_visible_tail,
        _close_run_pane,
        _prompt_with_watchdog,
        _wait_for_agent_ready,
    )

    reports_dir = default_reports_dir()
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
            _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _AgentRun(reason="agent_start_failed", error=str(e), pane_id=pane_id)

    ready, last_error = _wait_for_agent_ready(
        client, agent_name, timeout_s=job.start_timeout_ms / 1000
    )
    if not ready:
        _capture_visible_tail(
            client, agent_name, reports_dir=reports_dir, run_id=tail_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _AgentRun(
            reason="agent_not_interactive", error=last_error, pane_id=pane_id
        )

    try:
        settled_status = _prompt_with_watchdog(
            client,
            job_name=agent_name,
            target=agent_name,
            text=prompt_text,
            timeout_ms=timeout_ms,
            markers=job.failure_markers
            if job.failure_markers is not None
            else ("Free usage exceeded",),
            prompt_text=prompt_text,
        )
    except Exception as e:  # noqa: BLE001
        _capture_visible_tail(
            client, agent_name, reports_dir=reports_dir, run_id=tail_id
        )
        _close_run_pane(client, job_name=agent_name, pane_id=pane_id)
        return _AgentRun(reason="agent_prompt_failed", error=str(e), pane_id=pane_id)

    _capture_visible_tail(client, agent_name, reports_dir=reports_dir, run_id=tail_id)
    session_id: str | None = None
    try:
        session_id = client.agent_session_id(agent_name)
    except Exception as e:  # noqa: BLE001 — session id is best-effort reporting data
        log.debug("could not read session id for %s: %s", agent_name, e)
    _close_run_pane(client, job_name=agent_name, pane_id=pane_id)

    return _AgentRun(
        reason=None
        if settled_status in SETTLED_AGENT_STATUSES
        else "agent_prompt_failed",
        pane_id=pane_id,
        final_agent_status=settled_status,
        session_id=session_id,
    )


# Bounds the one `git check-ref-format` call per event head ref; it reads no repo.
_REF_CHECK_TIMEOUT_S = 10


def _is_valid_branch_name(ref: str) -> bool | None:
    """Whether ``ref`` is a plain branch name git itself accepts
    (``git check-ref-format --branch``), or None when the check could not run (timeout,
    no git) — an unknown answer, never a pass and never proof the name is bad.

    Two shapes are refused before git is asked: a leading ``-``, which a positional
    argument could read as an option, and ``@{``, because ``--branch`` expands the
    ``@{-N}`` previous-branch syntax instead of rejecting it. The check reads no
    repository, so it runs the same from any working directory."""
    if not ref or ref.startswith("-") or "@{" in ref:
        return False
    try:
        proc = subprocess.run(
            ["git", "check-ref-format", "--branch", ref],
            capture_output=True,
            text=True,
            timeout=_REF_CHECK_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.returncode == 0


def _find_existing_worktree(repo: Path, branch: str) -> Path | None:
    """Return the path of a worktree that already has *branch* checked out, or None.
    Parses `git worktree list --porcelain` (issue 036: the pipeline orchestrator
    deliberately retains its worktree on the PR branch after a run — see
    docs/pipeline/orchestrator-prompt.md G-10 — so a second `git worktree add` on
    that same branch always fails with "already used by worktree at ...")."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return None
    target = f"refs/heads/{branch}"
    current_path: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif (
            line.startswith("branch ")
            and current_path is not None
            and line[len("branch ") :] == target
        ):
            return Path(current_path)
    return None


def _worktree_reuse_check(wt_path: Path) -> tuple[bool, str]:
    """Before pointing a fix worker at a worktree we didn't create ourselves, verify
    it has no uncommitted changes — an in-progress edit there (e.g. a still-running
    orchestrator) must not be clobbered by a fix worker starting concurrently."""
    return verify.worktree_status(wt_path)


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


# Reasons whose record carries a dirty worktree a human has to deal with.
_DIRTY_WORKTREE_REASONS = frozenset(
    {"worktree_reuse_not_clean", verify.REASON_LEFT_UNCOMMITTED}
)


def _dirty_worktree_already_reported(
    history_path: Path, job_name: str, worktree_path: str
) -> bool:
    """Whether the latest record naming this worktree already reported it dirty (issue
    070), so the first detection notifies a human and later ticks stay quiet. A clean
    run there since makes the next dirty finding a new detection."""
    for record in reversed(read_job(history_path, job_name)):
        if record.extra and record.extra.get("worktree_path") == worktree_path:
            return record.extra.get("reason") in _DIRTY_WORKTREE_REASONS
    return False


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One firing of a job (issues 074, 075): whatever a trigger decided should run now. `extra`
    goes into the `running` record and always carries `trigger` (cron / event / request /
    manual), plus `scheduled_for` and `late_seconds` for a cron firing or the event's id."""

    run_id: str
    extra: HistoryExtra


def _occurrence_extra(occ: Occurrence) -> HistoryExtra:
    return HistoryExtra(**occ.extra)


def _with_trigger(
    source: Occurrence | HistoryExtra, extra: HistoryExtra
) -> HistoryExtra:
    """Stamp `extra` with the occurrence's `trigger` (issues 074, 075), so a terminal record
    says what started its run just as the `running` record does."""
    origin = source.extra if isinstance(source, Occurrence) else source
    trigger = origin.get("trigger")
    if trigger is not None:
        extra["trigger"] = trigger
    return extra


def _append_running(
    job: Job, history_path: Path, occ: Occurrence, *, now: datetime
) -> None:
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=job.name,
            state="running",
            run_id=occ.run_id,
            extra=_occurrence_extra(occ),
        ),
    )


def _notify(
    client: HerdrClient, title: str, *, body: str | None = None, sound: str = "none"
) -> None:
    """Best-effort: a notification failure (e.g. Herdr server unreachable) must not abort the
    tick — see run_tick's isolation contract above."""
    try:
        client.notification_show(title, body=body, sound=sound)
    except HerdrCliError as e:
        log.warning("%s: notification failed: %s", title, e)


def _notify_gate(job: Job, kind: NotifyKind) -> bool:
    """Whether a `job.notify_policy`-governed notification of *kind* should actually be
    sent (issue 009). *kind* is one of:

    - "failure": the job's own dispatch ended in a terminal failure this tick (setup
      errors, a gate whose fix agent could not resolve it, a max-attempts cap reached).
    - "finding": the job completed without a terminal failure but surfaced something
      worth knowing about even so — a gated job's checks found something and the fix
      agent resolved it, or a plain routine needed its `fallback_model` retry.
    - "success": a clean terminal outcome with nothing notable (checks passed on the
      first try, a plain routine finished with its primary model, a pr-target gate
      enumerated nothing eligible).
    - "progress": a sub-step inside a still-in-progress job (e.g. one PR skipped
      mid-loop while a pr-target gate keeps dispatching others) — never the job's own
      once-per-tick terminal notification.

    The four policy values form a strict hierarchy, each a subset of the last:
    "on-failure" (notify only on "failure") subset of "on-finding" (adds "finding")
    subset of "terminal" (default — adds "success", i.e. every terminal outcome, which
    is exactly the pre-issue-009 behavior: one notification per run, done or failed,
    never a mid-run progress ping — see the issue's acceptance criteria: "defaulting to
    a single terminal-state notification") subset of "always" (adds "progress" too —
    the full pre-issue-009-job per-tick pinginess some jobs opted into but the issue
    says an unattended overnight run should no longer get by default).

    Deliberately excludes `on_missed`-triggered notifications: that is its own
    pre-existing per-job opt-in (config.VALID_ON_MISSED) and stays orthogonal here so
    existing jobs.d/ configs that already set `on_missed: notify` keep exactly their
    current behavior regardless of `notify_policy`."""
    if job.notify_policy == "always":
        return True
    if job.notify_policy == "terminal":
        return kind != "progress"
    if job.notify_policy == "on-finding":
        return kind in ("failure", "finding")
    if job.notify_policy == "on-failure":
        return kind == "failure"
    assert_never(job.notify_policy)


# What follows `rt-<job>` in a worker agent name: auto_fix's `build_worker_agent_name`
# (`-pr<n>` or `-pr<n>-<tail>`) and `build_gate_worker_agent_name` (`-gate-<hash>`).
_WORKER_NAME_SUFFIX_RE = re.compile(r"-pr\d+(?:-[0-9a-z]+)?|-gate-[0-9a-f]+")


def _is_job_agent(name: str, job: Job) -> bool:
    """`name` is this job's own agent or one of the per-PR / gate workers it spawned.
    Anchored on the worker suffix shapes, so job `babysit-prs` never claims an agent of a
    job named `babysit-prs-nightly`. Lowercased because herdr lowercases agent names."""
    name, agent = name.lower(), job.agent_name.lower()
    if name == agent:
        return True
    return name.startswith(agent) and bool(
        _WORKER_NAME_SUFFIX_RE.fullmatch(name.removeprefix(agent))
    )


def _reap_agents_of_rebooted_run(
    client: HerdrClient, job: Job, stale: HistoryRecord, *, boot_epoch: float | None
) -> list[str]:
    """Close the agents a run left behind when the host rebooted under it; return their
    names. herdr restores every pane's agent at boot, reporting `blocked`, so a run whose
    tick died with the host comes back as opencode processes nobody will ever prompt
    again — ~550 MB each, pinning 2.2 GB of the Pi's 4 GB on 2026-10-01. Per-PR worker
    names carry the run id, so the reap-on-name-collision path never sees them.

    Only for a run that started before the current boot: a stale run on a host that did
    not reboot may have a blocked agent a human is meant to inspect. Never for a root-mode
    job, whose pane is the ambient workspace. Best-effort: herdr errors are logged."""
    if job.workspace == "root" or boot_epoch is None:
        return []
    if stale.ts.timestamp() >= boot_epoch:
        return []
    try:
        agents = client.agent_panes_by_status()
    except HerdrCliError as e:
        log.warning("%s: could not list agents to reap: %s", job.name, e)
        return []
    # An audit run's agents carry its run id in their names, which `_is_job_agent`'s
    # suffix shapes do not cover; the stale record names the run, so match them exactly.
    audit_names: set[str] = set()
    if job.kind == "audit" and stale.run_id is not None:
        audit_names = {
            build_audit_agent_name(job.name, stale.run_id).lower(),
            build_gate_worker_agent_name(job.name, stale.run_id).lower(),
        }
    reaped: list[str] = []
    for name, (_status, pane_id) in sorted(agents.items()):
        if not (_is_job_agent(name, job) or name.lower() in audit_names):
            continue
        try:
            client.pane_close(pane_id)
        except HerdrCliError as e:
            log.warning(
                "%s: could not close pane %s of %s: %s", job.name, pane_id, name, e
            )
            continue
        reaped.append(name)
    if reaped:
        log.warning(
            "%s: run %s predates the last boot; closed its restored agents %s",
            job.name,
            stale.run_id,
            reaped,
        )
    return reaped


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


def _pr_worker_is_live(client: HerdrClient, job_name: str, pr_number: int) -> bool:
    """Double-dispatch guard for the pr-target dispatch loop (review finding E): a PR
    still eligible on the next tick must not get a second fix worker while its first
    is still running. The dispatcher never creates a bare `build_pr_agent_name(...)`
    agent — every worker's real name also carries a run_id tail (issue 036c) — so this
    matches by prefix against every live agent instead of an exact name lookup, which
    could never find anything. Fails open on a HerdrCliError, same as
    `_live_agent_exists`."""
    prefix = build_pr_agent_name(job_name, pr_number)
    try:
        statuses = client.agent_statuses()
    except HerdrCliError as e:
        log.warning(
            "%s: could not query live agents for PR #%d: %s", job_name, pr_number, e
        )
        return False
    return any(
        status in LIVE_AGENT_STATUSES
        for name, status in statuses.items()
        if name.startswith(prefix)
    )
