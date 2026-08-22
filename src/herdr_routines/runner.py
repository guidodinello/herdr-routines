"""Orchestrates one job run: creates the pane, starts the agent, sends the prompt, verifies the
result, and writes the terminal history record. See docs/plan-v1.md §6 for the report-file
contract and the post-run verification rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from herdr_routines.config import Job
from herdr_routines.herdr import HerdrClient, HerdrCliError, build_agent_start_args

# Settle states that count as success for a scheduled (never-focused) run. Both are included
# because "idle" is what was empirically observed on herdr 0.8.2 for a never-focused pane, and
# "done" is kept in case SKILL.md's seen/unseen distinction applies under some other condition
# not exercised by the step-5 probe. See docs/plan-v1.md.
SUCCESS_AGENT_STATUSES = frozenset({"idle", "done"})


def default_reports_dir() -> Path:
    import os

    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "reports"


def make_run_id(job_name: str, scheduled_for: datetime) -> str:
    return f"{job_name}-{scheduled_for.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def build_branch_name(job_name: str, run_id: str) -> str:
    # run_id already encodes the timestamp, so re-use it rather than duplicating a clock read.
    return f"auto/{job_name}-{run_id.rsplit('-', 1)[-1]}"


def substitute_prompt(
    prompt_template: str, *, report_path: Path, job_name: str, run_id: str
) -> str:
    return (
        prompt_template.replace("$ROUTINE_REPORT", str(report_path))
        .replace("$ROUTINE_JOB", job_name)
        .replace("$ROUTINE_RUN_ID", run_id)
    )


@dataclass(frozen=True, slots=True)
class RunOutcome:
    state: str  # "done" | "failed" | "interrupted_unknown" (see docs/plan-v1.md §4)
    run_id: str
    reason: str | None = None
    error: str | None = None
    agent_name: str | None = None
    pane_id: str | None = None
    branch: str | None = None
    final_agent_status: str | None = None
    report_written: bool = False
    report_bytes: int = 0
    report_path: str | None = None
    duration_seconds: float | None = None


def build_dry_run_argv(job: Job, *, run_id: str) -> list[list[str]]:
    """The exact `herdr` command sequence `run --dry-run` prints, without executing anything.
    Kept in lockstep with `execute_run` below by sharing branch/prompt construction helpers."""
    report_path = default_reports_dir() / f"{run_id}.md"
    prompt = substitute_prompt(
        job.prompt, report_path=report_path, job_name=job.name, run_id=run_id
    )

    argv: list[list[str]] = []
    if job.workspace == "worktree":
        branch = build_branch_name(job.name, run_id)
        argv.append(
            [
                "herdr",
                "worktree",
                "create",
                "--cwd",
                str(job.repo),
                "--branch",
                branch,
                "--base",
                job.base,
                "--no-focus",
                "--label",
                job.name,
            ]
        )
    else:
        argv.append(
            [
                "herdr",
                "tab",
                "create",
                "--cwd",
                str(job.repo),
                "--no-focus",
                "--label",
                job.name,
            ]
        )

    argv.append(
        [
            "herdr",
            *build_agent_start_args(
                name=job.agent_name,
                kind=job.agent_kind,
                pane_id="<pane_id>",
                start_timeout_ms=job.start_timeout_ms,
                model=job.model,
            ),
        ]
    )
    argv.append(
        [
            "herdr",
            "agent",
            "prompt",
            job.agent_name,
            prompt,
            "--wait",
            "--timeout",
            str(job.timeout_ms),
        ]
    )
    return argv


def execute_run(job: Job, client: HerdrClient, *, run_id: str) -> RunOutcome:
    """Runs one job to completion (or failure) against a real or faked HerdrClient. Never
    raises — every failure mode is captured in the returned RunOutcome so `tick.py` can always
    write a terminal history record. (A `job.model` unsupported for `job.agent_kind` would raise
    `ValueError` from `agent_start`, but `config.py` already rejects that combination at load
    time, so it can't reach here.)"""
    started_at = datetime.now(UTC)
    report_path = default_reports_dir() / f"{run_id}.md"
    branch = (
        build_branch_name(job.name, run_id) if job.workspace == "worktree" else None
    )

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="report_dir_creation_failed",
            error=str(e),
            branch=branch,
        )

    prompt = substitute_prompt(
        job.prompt, report_path=report_path, job_name=job.name, run_id=run_id
    )

    try:
        if job.workspace == "worktree":
            pane_id = client.worktree_create(
                cwd=str(job.repo), branch=branch or "", base=job.base, label=job.name
            )
        else:
            pane_id = client.tab_create(cwd=str(job.repo), label=job.name)
    except (HerdrCliError, OSError) as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="pane_creation_failed",
            error=str(e),
            branch=branch,
        )

    try:
        client.agent_start(
            name=job.agent_name,
            kind=job.agent_kind,
            pane_id=pane_id,
            start_timeout_ms=job.start_timeout_ms,
            model=job.model,
        )
    except (HerdrCliError, OSError, ValueError) as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="agent_start_failed",
            error=str(e),
            pane_id=pane_id,
            branch=branch,
        )

    try:
        settled_status = client.agent_prompt_wait(
            target=job.agent_name, text=prompt, timeout_ms=job.timeout_ms
        )
    except (HerdrCliError, OSError) as e:
        return RunOutcome(
            state="failed",
            run_id=run_id,
            reason="agent_prompt_failed",
            error=str(e),
            agent_name=job.agent_name,
            pane_id=pane_id,
            branch=branch,
        )

    # Best-effort diagnostic tail — never allowed to fail the run (docs/plan-v1.md §6 layer 2).
    try:
        tail = client.agent_read(job.agent_name, lines=200)
        if tail:
            (report_path.parent / f"{run_id}.tail.txt").write_text(tail)
    except OSError:
        pass

    report_written = report_path.exists()
    report_bytes = report_path.stat().st_size if report_written else 0

    common = {
        "run_id": run_id,
        "agent_name": job.agent_name,
        "pane_id": pane_id,
        "branch": branch,
        "final_agent_status": settled_status,
        "report_written": report_written,
        "report_bytes": report_bytes,
        "report_path": str(report_path) if report_written else None,
        "duration_seconds": (datetime.now(UTC) - started_at).total_seconds(),
    }

    if settled_status == "blocked":
        return RunOutcome(state="failed", reason="blocked", **common)

    if settled_status == "unknown":
        # An unresolvable settle status maps to the same terminal state as a crashed/killed
        # tick (docs/plan-v1.md §4's state machine), not a plain "failed" — stale-run recovery
        # already treats interrupted_unknown as the "we don't know what happened" bucket.
        return RunOutcome(
            state="interrupted_unknown", reason="unsettled_status_unknown", **common
        )

    if settled_status not in SUCCESS_AGENT_STATUSES:
        # "working" here means agent_prompt_wait's --wait settled on something that isn't a
        # completion signal — treat as interrupted/unclear rather than success.
        return RunOutcome(
            state="failed", reason=f"unsettled_status_{settled_status}", **common
        )

    if not report_written or report_bytes == 0:
        # Direct response to the research repo's standing pattern that unattended scheduled
        # runs fail silently and plausibly (docs/plan-v1.md §6): a clean settle with no report,
        # or an empty one, is not "done".
        return RunOutcome(state="failed", reason="no_report", **common)

    return RunOutcome(state="done", **common)
