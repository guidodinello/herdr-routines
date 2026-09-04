"""Pipeline stall watchdog (issue 031): detects an overnight pipeline whose orchestrator
died silently (no crash report, `docs/pipeline/orchestrator-prompt.md`'s documented G-4
gap) and kills the worker it left running, unattended, past the run's own deadline.

Design notes (see the issue for the full incident writeup):

- Enumerates in-flight runs from `state.json` files under each host's
  `~/.herdr/worktrees/herdr-routines/auto-pipeline-*/` (the shared worktree the
  orchestrator creates, `orchestrator-prompt.md` Prerequisite 1) that have no terminal
  report yet.
- A run is only ever touched when **both** hold: `now > deadline_epoch + grace` (30 min
  grace so this never races a live orchestrator that is itself draining an in-flight
  `--wait` and about to write its own partial report at deadline,
  `orchestrator-prompt.md` "Pipeline deadline, quota, resume, cleanup"), **and** its
  heartbeat log (`/tmp/pipeline_resume_<run_id>.log`) has not been touched recently
  either. Stage 3's own worker timeout is 90 minutes and the orchestrator is documented
  to legitimately wait out an in-flight `--wait` before it can even notice its deadline
  passed — deadline overrun alone is not evidence of a *dead* orchestrator, a stale
  heartbeat is. A missing heartbeat file counts as stale (never as healthy): `/tmp`
  clears across a reboot, which is exactly the kind of silent death this exists to catch.
- On a stall: kill any live `pl-<N>-<run_id>` agent (`herdr pane close` on its pane —
  verified empirically 2026-09-03 against a live `herdr` server: closing a pane running
  a foreground child kills that child's process group, not just the UI — see the issue
  PR description for the transcript), write a terminal report distinguishing this from
  a normal stage-6 report, and fire the same notification channel routine job failures
  already use.
- Never resumes or retries. Kill + report only.

Deliberately **not** wired through `tick.py`'s job-dispatch model: every job there
issues an agent prompt (`_process_job`/`_process_gated_job`/`_process_pipeline_job`
family); this sweep issues no prompt and spawns nothing; it is a maintenance script,
same posture as `gc.py`. Scheduled instead via its own systemd timer
(`deploy/systemd/herdr-routines-watchdog.{timer,service}`) that runs `herdr-routines
pipeline-watchdog` directly, the same "cron-triggered systemd unit runs a CLI
subcommand" shape `herdr-routines.timer` already uses for `tick`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from logger import get_logger

from herdr_routines.herdr import HerdrClient, HerdrCliError

log = get_logger(__name__)

# design's suggested margin: avoids racing a live orchestrator that is itself about to
# hit its own deadline check and write a partial report (orchestrator-prompt.md
# "Pipeline deadline, quota, resume, cleanup").
DEADLINE_GRACE_SECONDS = 30 * 60

# The heartbeat poll loop writes a line every 3-5 min while a stage is in flight
# (orchestrator-prompt.md Worker spawn template step 2); a gap several times that is
# only plausible once nothing is polling any more.
HEARTBEAT_STALE_SECONDS = 20 * 60

WORKTREE_GLOB = "auto-pipeline-*"
STATE_JSON_NAME = "state.json"


@dataclass(frozen=True, slots=True)
class InflightRun:
    """One in-flight pipeline run discovered from a `state.json` with no terminal report
    yet."""

    run_id: str
    state_path: Path
    deadline_epoch: int
    current_stage: int | None


@dataclass(frozen=True, slots=True)
class WatchdogAction:
    """What the watchdog did for one stalled run."""

    run_id: str
    killed_agents: tuple[str, ...]
    stage_killed: int | None
    report_path: Path


def default_worktrees_root(repo_name: str = "herdr-routines") -> Path:
    """`~/.herdr/worktrees/<repo_name>` — where `herdr worktree create` links the
    orchestrator's shared per-run worktree (issue 031 Design)."""
    return Path.home() / ".herdr" / "worktrees" / repo_name


def default_heartbeat_dir() -> Path:
    return Path("/tmp")


def heartbeat_log_path(heartbeat_dir: Path, run_id: str) -> Path:
    return heartbeat_dir / f"pipeline_resume_{run_id}.log"


def _candidate_report_paths(
    reports_dir: Path, run_id: str, raw_state: dict
) -> list[Path]:
    """Every path this run's terminal report could plausibly live at. The orchestrator
    records the exact path under `artifact_paths.report` (same field `ps.py`'s
    `_resolve_report_path` trusts first); the repo's own docs disagree on the mirrored
    convention between `<run_id>.md` (`orchestrator-prompt.md`, `design.md:185`) and
    `pipeline-<run_id>.md` (`design.md:344`'s real example) — check both rather than pick
    one and risk reporting on a run that already finished cleanly."""
    paths = []
    artifacts = raw_state.get("artifact_paths")
    if isinstance(artifacts, dict):
        report = artifacts.get("report")
        if isinstance(report, str) and report:
            paths.append(Path(report))
    paths.append(reports_dir / f"pipeline-{run_id}.md")
    paths.append(reports_dir / f"{run_id}.md")
    return paths


def _has_terminal_report(reports_dir: Path, run_id: str, raw_state: dict) -> bool:
    return any(
        p.exists() for p in _candidate_report_paths(reports_dir, run_id, raw_state)
    )


def _parse_state_json(state_path: Path, reports_dir: Path) -> InflightRun | None:
    try:
        raw = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("skipping unreadable %s: %s", state_path, e)
        return None
    if not isinstance(raw, dict):
        log.warning("skipping %s: top level is not an object", state_path)
        return None
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        log.warning("skipping %s: missing run_id", state_path)
        return None
    deadline_epoch = raw.get("deadline_epoch")
    if not isinstance(deadline_epoch, int) or isinstance(deadline_epoch, bool):
        log.warning("skipping %s: missing/invalid deadline_epoch", state_path)
        return None
    if _has_terminal_report(reports_dir, run_id, raw):
        return None
    current_stage = raw.get("current_stage")
    stage = (
        current_stage
        if isinstance(current_stage, int) and not isinstance(current_stage, bool)
        else None
    )
    return InflightRun(
        run_id=run_id,
        state_path=state_path,
        deadline_epoch=deadline_epoch,
        current_stage=stage,
    )


def find_inflight_runs(worktrees_root: Path, reports_dir: Path) -> list[InflightRun]:
    """Every parseable `state.json` under `worktrees_root/auto-pipeline-*/` that has no
    terminal report yet. Unreadable/malformed files are skipped with a warning
    (fail-open, matching `ps.py`'s `scan_pipeline_runs`)."""
    if not worktrees_root.exists():
        return []
    runs = []
    for state_path in sorted(worktrees_root.glob(f"{WORKTREE_GLOB}/{STATE_JSON_NAME}")):
        run = _parse_state_json(state_path, reports_dir)
        if run is not None:
            runs.append(run)
    return runs


def _heartbeat_last_line(log_path: Path) -> str | None:
    try:
        lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    except OSError:
        return None
    return lines[-1] if lines else None


def _heartbeat_is_stale(
    heartbeat_dir: Path, run_id: str, *, now: datetime, stale_seconds: int
) -> bool:
    """True when the heartbeat log is missing, empty, or hasn't been written to
    recently. A missing file counts as stale on purpose (see module docstring) —
    `/tmp` clearing across a reboot must not read as "still healthy"."""
    log_path = heartbeat_log_path(heartbeat_dir, run_id)
    try:
        mtime = log_path.stat().st_mtime
    except OSError:
        return True
    return (now.timestamp() - mtime) > stale_seconds


def is_stalled(
    run: InflightRun,
    *,
    now: datetime,
    heartbeat_dir: Path,
    grace_seconds: int = DEADLINE_GRACE_SECONDS,
    heartbeat_stale_seconds: int = HEARTBEAT_STALE_SECONDS,
) -> bool:
    """Both must hold: deadline (plus grace) has passed, and the heartbeat has gone
    quiet. Deadline overrun alone is not evidence of a dead orchestrator — one is
    documented to legitimately wait out an in-flight `--wait` before it can even
    notice its own deadline passed (see module docstring)."""
    deadline_passed = now.timestamp() > run.deadline_epoch + grace_seconds
    if not deadline_passed:
        return False
    return _heartbeat_is_stale(
        heartbeat_dir, run.run_id, now=now, stale_seconds=heartbeat_stale_seconds
    )


def _stage_from_agent_name(name: str, run_id: str) -> int | None:
    """Best-effort `N` out of a `pl-<N>-<run_id>` agent name; None if it doesn't parse
    (never fatal — the report still gets written either way)."""
    prefix = "pl-"
    suffix = f"-{run_id}".lower()
    lowered = name.lower()
    if not (lowered.startswith(prefix) and lowered.endswith(suffix)):
        return None
    middle = lowered[len(prefix) : -len(suffix)]
    return int(middle) if middle.isdigit() else None


def _write_report(
    reports_dir: Path,
    run: InflightRun,
    *,
    killed_agents: dict[str, str],
    heartbeat_last_line: str | None,
    now: datetime,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"pipeline-{run.run_id}.md"
    killed = bool(killed_agents)
    stages_killed = sorted(
        s
        for s in (_stage_from_agent_name(n, run.run_id) for n in killed_agents)
        if s is not None
    )
    # `## Outcome:` is the one contract `tick._process_pipeline_job` reconciles on,
    # regardless of whether the orchestrator itself or this watchdog wrote the terminal
    # report (docs/pipeline/orchestrator-prompt.md "Final report"). Always "failed" here —
    # a watchdog-authored report is definitionally not a clean completion.
    outcome_marker = (
        "## Outcome: failed (watchdog killed)"
        if killed
        else "## Outcome: failed (watchdog: no live worker found)"
    )
    lines = [
        f"# Pipeline run {run.run_id} — watchdog report",
        "",
        outcome_marker,
        "",
        f"watchdog_killed: {'true' if killed else 'false'}",
        f"stage_killed: {stages_killed[0] if stages_killed else 'none'}",
        f"last_known_stage: {run.current_stage if run.current_stage is not None else 'unknown'}",
        f"deadline_epoch: {run.deadline_epoch}",
        f"checked_at: {now.isoformat()}",
        f"killed_agents: {', '.join(sorted(killed_agents)) or 'none'}",
        f"heartbeat_last_line: {heartbeat_last_line or 'none found'}",
        "",
    ]
    if killed:
        lines.append(
            "The orchestrator for this run appears to have died silently (heartbeat "
            "stopped advancing, deadline exceeded) leaving the above worker(s) running "
            "unattended; the pipeline-stall watchdog killed them via `herdr pane close` "
            "and is writing this report in the orchestrator's place. This run was not "
            "resumed — a human should look at the PR (if any) and the shared worktree."
        )
    else:
        lines.append(
            "The orchestrator for this run appears to have died silently (heartbeat "
            "stopped advancing, deadline exceeded) but no live pl-<N>-<run_id> worker "
            "was found to kill — it may have already exited on its own. Writing this "
            "report so the run is not rechecked forever."
        )
    report_path.write_text("\n".join(lines) + "\n")
    return report_path


def _notify(client: HerdrClient, title: str, *, body: str | None) -> None:
    """Best-effort: a notification failure must not abort the sweep."""
    try:
        client.notification_show(title, body=body, sound="request")
    except HerdrCliError as e:
        log.warning("%s: notification failed: %s", title, e)


def run_watchdog(
    *,
    client: HerdrClient,
    worktrees_root: Path,
    reports_dir: Path,
    heartbeat_dir: Path,
    now: datetime | None = None,
) -> list[WatchdogAction]:
    """One watchdog sweep: find every stalled in-flight run, kill any live worker it
    left running, write its terminal report, and notify. Never raises — a single run's
    `herdr` failure is logged and the sweep continues with the rest (fail-open, same
    posture as `ps.py`/`gc.py`)."""
    now = now or datetime.now(UTC)
    actions: list[WatchdogAction] = []
    for run in find_inflight_runs(worktrees_root, reports_dir):
        if not is_stalled(run, now=now, heartbeat_dir=heartbeat_dir):
            continue
        killed_agents: dict[str, str] = {}
        try:
            panes = client.live_pipeline_agent_panes(run.run_id)
        except HerdrCliError as e:
            log.warning(
                "%s: could not list agents, skipping this cycle: %s", run.run_id, e
            )
            continue
        for name, pane_id in panes.items():
            try:
                client.pane_close(pane_id)
                killed_agents[name] = pane_id
            except HerdrCliError as e:
                log.warning(
                    "%s: could not close pane %s for %s: %s",
                    run.run_id,
                    pane_id,
                    name,
                    e,
                )
        heartbeat_last_line = _heartbeat_last_line(
            heartbeat_log_path(heartbeat_dir, run.run_id)
        )
        report_path = _write_report(
            reports_dir,
            run,
            killed_agents=killed_agents,
            heartbeat_last_line=heartbeat_last_line,
            now=now,
        )
        stage_killed = next(
            (
                s
                for s in (_stage_from_agent_name(n, run.run_id) for n in killed_agents)
                if s is not None
            ),
            None,
        )
        if killed_agents:
            _notify(
                client,
                f"herdr-routines: pipeline {run.run_id} stalled, worker killed",
                body=f"stage {stage_killed} agent(s) {', '.join(sorted(killed_agents))} "
                f"killed past deadline; see {report_path}",
            )
        else:
            _notify(
                client,
                f"herdr-routines: pipeline {run.run_id} stalled, no report",
                body=f"deadline exceeded, no live worker found; see {report_path}",
            )
        actions.append(
            WatchdogAction(
                run_id=run.run_id,
                killed_agents=tuple(sorted(killed_agents)),
                stage_killed=stage_killed,
                report_path=report_path,
            )
        )
    return actions
