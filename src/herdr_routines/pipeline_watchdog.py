"""Pipeline stall watchdog (issue 031): detects an overnight pipeline whose orchestrator
died silently (no crash report, `docs/pipeline/orchestrator-prompt.md`'s documented G-4
gap) and, where safe, resumes it once before reporting.

Design notes (see the issue for the full incident writeup):

- Enumerates in-flight runs from `state.json` files under each host's
  `~/.herdr/worktrees/herdr-routines/auto-pipeline-*/` (the shared worktree the
  orchestrator creates, `orchestrator-prompt.md` Prerequisite 1) that have no terminal
  report yet.
- A run is failed immediately, no deadline wait, when the host booted after the
  orchestrator's last `state.json` write (`system_boot_epoch` vs the file's mtime): a
  reboot kills the orchestrator *and* its `systemd-run` launcher unit outright, so the
  run is definitively over — waiting out its 7h deadline just delays the failure
  notification (real incident: 2026-09-07 run, host rebooted 16 min in, watchdog only
  reaped it 7.5h later). A rebooted run still goes through the resume path when it is
  otherwise eligible.
- Otherwise a run is only touched when **both** hold: `now > deadline_epoch + grace`
  (30 min grace so this never races a live orchestrator that is itself draining an
  in-flight `--wait` and about to write its own partial report at deadline,
  `orchestrator-prompt.md` "Pipeline deadline, quota, resume, cleanup"), **and** its
  heartbeat log (`/tmp/pipeline_resume_<run_id>.log`) has not been touched recently
  either. The implement stage's (stage 4) own worker timeout is 90 minutes and the orchestrator is documented
  to legitimately wait out an in-flight `--wait` before it can even notice its deadline
  passed — deadline overrun alone is not evidence of a *dead* orchestrator, a stale
  heartbeat is. A missing heartbeat file counts as stale (never as healthy): `/tmp`
  clears across a reboot, which is exactly the kind of silent death this exists to catch.
- Every terminal report also carries the design.md G-17 stage-independence verdict
  (`validate_stage_sessions`): a run whose `state.json` `stage_sessions` are fabricated
  placeholders, reused across stages, or missing entries did not run its stages in
  independent sessions, and any PR it produced is single-session work.
- On a stall: try **one** automatic resume before reporting when the run is resumable
  (issue 068). Eligibility needs `repo_parent`, `artifact_paths.report`, a positive
  original budget and a matching `running` history record; `resume_count` is capped at
  `MAX_PIPELINE_RESUMES`. On resume the watchdog appends a fresh `running` record
  first, closes stale worker panes (`herdr pane close` — verified empirically 2026-09-03
  against a live `herdr` server: closing a pane running a foreground child kills that
  child's process group, not just the UI), rewrites `state.json` atomically, then
  registers a resume unit. When the run is not resumable it kills any live
  `pl-<N>-<run_id>` agent, writes a terminal report distinguishing this from a normal
  stage-7 report, and fires the same notification channel routine job failures already
  use.
- At most `MAX_PIPELINE_RESUMES` automatic resumes total (tracked by `resume_count`);
  after that it falls back to kill-and-report.

Deliberately **not** wired through `tick.py`'s job-dispatch model: every job there
issues an agent prompt (`_process_job`/`_process_gated_job`/`_process_pipeline_job`
family); this sweep issues no prompt and only relaunches an already-prepared run
through its launcher, so it stays a maintenance script, same posture as `gc.py`. Scheduled instead via its own systemd timer
(`deploy/systemd/herdr-routines-watchdog.{timer,service}`) that runs `herdr-routines
pipeline-watchdog` directly, the same "cron-triggered systemd unit runs a CLI
subcommand" shape `herdr-routines.timer` already uses for `tick`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from logger import get_logger

from herdr_routines.herdr import HerdrClient, HerdrCliError
from herdr_routines.history import HistoryRecord, append, read_all
from herdr_routines.pipeline_prepare import write_state_json, write_terminal_report
from herdr_routines.pipeline_stages import STAGES, StageSpec

log = get_logger(__name__)

# design's suggested margin: avoids racing a live orchestrator that is itself about to
# hit its own deadline check and write a partial report (orchestrator-prompt.md
# "Pipeline deadline, quota, resume, cleanup").
DEADLINE_GRACE_SECONDS = 30 * 60

# The heartbeat poll loop writes a line every 3-5 min while a stage is in flight
# (orchestrator-prompt.md Worker spawn template step 2); a gap several times that is
# only plausible once nothing is polling any more.
HEARTBEAT_STALE_SECONDS = 20 * 60

# Prefix of the transient unit names tick and `herdr-routines run` launch with
# (`herdr-pipeline-<run_id>`, plus an `-r<HHMMSS>` suffix on a resume). Shared so a
# drifted copy can't match no unit, which reads as "not active".
PIPELINE_UNIT_PREFIX = "herdr-pipeline-"
PIPELINE_UNIT_GLOB = PIPELINE_UNIT_PREFIX + "{run_id}*"
SYSTEMCTL_TIMEOUT_SECONDS = 10

WORKTREE_GLOB = "auto-pipeline-*"
STATE_JSON_NAME = "state.json"

# A reboot is unambiguous proof the orchestrator is dead: nothing survives it. When the
# host booted *after* state.json was last written (its mtime = the orchestrator's last
# sign of life — it rewrites it atomically at every stage handoff), the run is over,
# regardless of how far its 7h deadline still is. The slop absorbs clock skew and the
# write-then-boot edge.
BOOT_SLOP_SECONDS = 60

# At most one automatic resume per run (issue 068): a run that dies twice stops
# burning nights and falls back to a human-visible report.
MAX_PIPELINE_RESUMES = 1

# Extra time on the resumed unit's RuntimeMaxSec beyond the recomputed budget, so the
# orchestrator's own deadline handling always wins the race with systemd's kill.
RESUME_UNIT_MARGIN_S = 10 * 60

# Real opencode session ids are `ses_` + 20+ alphanumerics, all distinct across stages
# (each stage is its own fresh session — design.md G-17). Anything shorter, containing a
# separator, matching a placeholder word, or duplicated means the per-stage session independence
# contract was faked (observed: `ses_..._fake1`, `ses_fake_1_<run_id>` — 2026-09-06/07).
_SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{20,}$")
_FABRICATED_MARKERS = (
    "fake",
    "placeholder",
    "dummy",
    "stub",
    "todo",
    "example",
    "sample",
    "mock",
)


def system_boot_epoch(proc_stat: Path = Path("/proc/stat")) -> float | None:
    """Unix time the host last booted, from `/proc/stat`'s `btime` line. None if it
    can't be read (non-Linux, unreadable) — callers treat None as "can't tell, skip
    the reboot check", never as "did not reboot"."""
    try:
        for line in proc_stat.read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def validate_stage_sessions(
    raw_state: Mapping[str, object], *, expected: Sequence[StageSpec] = STAGES
) -> str | None:
    """Reason string if `state.json`'s `stage_sessions` shows the G-17 layout contract was
    not honored; None if it looks genuine.

    A legitimate run records one real session id per stage that ran an agent. Two stages
    legitimately share: stage 5 runs no agent (no session id), and stage 7 resumes stage
    4's session on purpose — both are declared in `STAGES` rather than special-cased here,
    so `pipeline_run` (which writes the ids) and this gate (which audits them) read the
    same table and cannot disagree. Everything else — a reused session across independent
    stages, a placeholder id, a stage that never recorded one — is unprovable independence,
    and "unverifiable" is treated as "failed" here on purpose."""
    sessions = raw_state.get("stage_sessions")
    if not isinstance(sessions, dict) or not sessions:
        return "stage_sessions is missing or empty"
    values = [v for v in sessions.values() if isinstance(v, str)]
    if len(values) != len(sessions):
        return "stage_sessions has non-string entries"
    for value in values:
        low = value.lower()
        if any(marker in low for marker in _FABRICATED_MARKERS):
            return f"stage_sessions contains a placeholder id ({value!r})"
    for value in values:
        if not _SESSION_ID_RE.match(value):
            return f"stage_sessions entry is not a real session id ({value!r})"

    # Reuse is legal only where STAGES says it is: stage 7 reuses stage 4's id. Any other
    # repetition means two independent stages shared one conversation.
    reuse_allowed: dict[str, str] = {
        str(spec.reuses_stage): str(spec.stage)
        for spec in expected
        if spec.reuses_stage is not None
    }
    seen: dict[str, str] = {}
    for key, value in sessions.items():
        owner = seen.get(value)
        if owner is None:
            seen[value] = str(key)
            continue
        if reuse_allowed.get(owner) != str(key):
            return (
                f"stage_sessions reuses one session across stages {owner} and {key} "
                f"(stages not independent)"
            )

    current_stage = raw_state.get("current_stage")
    if isinstance(current_stage, int) and not isinstance(current_stage, bool):
        reached = min(current_stage, len(expected))
        # Every independent stage that ran an agent must have recorded *its own* id,
        # keyed by its stage number. A count would let a key that maps to no stage pad
        # over a stage that recorded nothing. Stage 5 (no model) records none, and a
        # reused stage shares its owner's id, so neither is required to have a key.
        missing = [
            spec.stage
            for spec in expected[:reached]
            if spec.model is not None
            and spec.reuses_stage is None
            and str(spec.stage) not in sessions
        ]
        if missing:
            return (
                f"stage_sessions has no session for stage(s) "
                f"{', '.join(map(str, missing))} but the run reached stage "
                f"{current_stage} — {len(missing)} stage(s) unverified"
            )
    return None


def pipeline_state_json_path(worktrees_root: Path, run_id: str) -> Path | None:
    """The `state.json` for `run_id`, trying both casings of the worktree dir (`herdr
    worktree create` lowercases the branch into the path, but callers pass the original
    `RUN_ID`). None if neither exists."""
    for candidate in (
        worktrees_root / f"auto-pipeline-{run_id}" / STATE_JSON_NAME,
        worktrees_root / f"auto-pipeline-{run_id.lower()}" / STATE_JSON_NAME,
    ):
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class InflightRun:
    """One in-flight pipeline run discovered from a `state.json` with no terminal report
    yet."""

    run_id: str
    state_path: Path
    deadline_epoch: int
    current_stage: int | None
    # design.md G-17 gate: reason string if this run's stage_sessions look fabricated
    # / not independent, else None. Surfaced in the watchdog's report.
    stage_sessions_issue: str | None = None


@dataclass(frozen=True, slots=True)
class WatchdogAction:
    """What the watchdog did for one stalled run."""

    run_id: str
    killed_agents: tuple[str, ...]
    stage_killed: int | None
    report_path: Path | None
    resumed: bool = False


def default_worktrees_root(repo_name: str = "herdr-routines") -> Path:
    """`~/.herdr/worktrees/<repo_name>` — where `herdr worktree create` links the
    orchestrator's shared per-run worktree (issue 031 Design)."""
    return Path.home() / ".herdr" / "worktrees" / repo_name


def default_heartbeat_dir() -> Path:
    return Path("/tmp")


def heartbeat_log_path(heartbeat_dir: Path, run_id: str) -> Path:
    return heartbeat_dir / f"pipeline_resume_{run_id}.log"


def _candidate_report_paths(
    reports_dir: Path, run_id: str, raw_state: Mapping[str, object]
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


def _has_terminal_report(
    reports_dir: Path, run_id: str, raw_state: Mapping[str, object]
) -> bool:
    return any(
        p.exists() for p in _candidate_report_paths(reports_dir, run_id, raw_state)
    )


def recorded_pipeline_deadlines(history_path: Path) -> dict[str, int]:
    """Bare pipeline run_id -> the `deadline_epoch` tick recorded in the run's `running`
    history record at launch (`tick.pipeline_deadline_epoch`). This is the authoritative
    deadline: `state.json`'s copy is written by the orchestrator model, and a fallback
    model once wrote one a year in the past, which got a live run reaped 15 min in
    (2026-09-28)."""
    deadlines: dict[str, int] = {}
    for record in read_all(history_path):
        extra = record.extra or {}
        run_id = extra.get("pipeline_run_id")
        deadline = extra.get("deadline_epoch")
        if (
            record.state == "running"
            and isinstance(run_id, str)
            and isinstance(deadline, int)
            and not isinstance(deadline, bool)
        ):
            deadlines[run_id] = deadline
    return deadlines


def _parse_state_json(
    state_path: Path, reports_dir: Path, recorded_deadlines: Mapping[str, int]
) -> InflightRun | None:
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
    recorded = recorded_deadlines.get(run_id)
    if recorded is not None:
        if deadline_epoch != recorded:
            log.warning(
                "%s: ignoring state.json deadline_epoch %r, using tick's recorded %d",
                run_id,
                deadline_epoch,
                recorded,
            )
        deadline_epoch = recorded
    elif not isinstance(deadline_epoch, int) or isinstance(deadline_epoch, bool):
        # No recorded deadline (a manual `herdr-routines run`, or a run launched before
        # tick recorded one): state.json is all there is.
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
        stage_sessions_issue=validate_stage_sessions(raw),
    )


def find_inflight_runs(
    worktrees_root: Path,
    reports_dir: Path,
    recorded_deadlines: Mapping[str, int] | None = None,
) -> list[InflightRun]:
    """Every parseable `state.json` under `worktrees_root/auto-pipeline-*/` that has no
    terminal report yet. Unreadable/malformed files are skipped with a warning
    (fail-open, matching `ps.py`'s `scan_pipeline_runs`). A run's deadline comes from
    `recorded_deadlines` (tick's history) when present, else from its `state.json`."""
    if not worktrees_root.exists():
        return []
    runs = []
    for state_path in sorted(worktrees_root.glob(f"{WORKTREE_GLOB}/{STATE_JSON_NAME}")):
        run = _parse_state_json(state_path, reports_dir, recorded_deadlines or {})
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


def host_rebooted_after_state_write(
    state_path: Path, *, boot_epoch: float | None
) -> bool:
    """True when the host booted after `state_path` was last written — the orchestrator
    (and its whole `systemd-run` launcher unit) cannot have survived that, so the run
    is dead now, no matter how far off its deadline still is. `boot_epoch` None (can't
    read /proc/stat) disables the check rather than guessing; an unreadable state.json
    likewise returns False (can't tell)."""
    if boot_epoch is None:
        return False
    try:
        state_mtime = state_path.stat().st_mtime
    except OSError:
        return False
    return boot_epoch > state_mtime + BOOT_SLOP_SECONDS


def _host_rebooted_after_last_state_write(
    run: InflightRun, *, boot_epoch: float | None
) -> bool:
    return host_rebooted_after_state_write(run.state_path, boot_epoch=boot_epoch)


def is_stalled(
    run: InflightRun,
    *,
    now: datetime,
    heartbeat_dir: Path,
    boot_epoch: float | None = None,
    grace_seconds: int = DEADLINE_GRACE_SECONDS,
    heartbeat_stale_seconds: int = HEARTBEAT_STALE_SECONDS,
) -> bool:
    """A run is stalled when the host has rebooted since the orchestrator's last
    state.json write (unambiguous — fail fast, skip the deadline wait), OR when both
    the deadline (plus grace) has passed and the heartbeat has gone quiet. Deadline
    overrun alone is not evidence of a dead orchestrator — one is documented to
    legitimately wait out an in-flight `--wait` before it can even notice its own
    deadline passed (see module docstring)."""
    if _host_rebooted_after_last_state_write(run, boot_epoch=boot_epoch):
        return True
    deadline_passed = now.timestamp() > run.deadline_epoch + grace_seconds
    if not deadline_passed:
        return False
    return _heartbeat_is_stale(
        heartbeat_dir, run.run_id, now=now, stale_seconds=heartbeat_stale_seconds
    )


def pipeline_unit_active(run_id: str) -> bool | None:
    """Whether a systemd user unit for `run_id` is still running — the orchestrator's
    own liveness, which heartbeat staleness only guesses at. The glob covers the original
    unit and a resume's `-r<HHMMSS>` unit (a resumed run keeps tick's old recorded
    deadline, so it can look long overdue while perfectly alive). `list-units` rather
    than `is-active` because it exits 0 with no output when nothing matches, keeping "no
    unit" apart from "systemctl failed". None means can't tell (no systemctl, no user
    bus, timeout); callers must not reap on it."""
    argv = [
        "systemctl",
        "--user",
        "list-units",
        "--all",
        "--plain",
        "--no-legend",
        "--state=active,activating,deactivating,reloading",
        PIPELINE_UNIT_GLOB.format(run_id=run_id),
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s: could not query systemd: %s", run_id, e)
        return None
    if result.returncode != 0:
        log.warning(
            "%s: systemctl exited %d: %s",
            run_id,
            result.returncode,
            result.stderr.strip(),
        )
        return None
    return bool(result.stdout.strip())


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
    host_rebooted: bool = False,
) -> Path:
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
    if host_rebooted:
        outcome = "failed (watchdog: host rebooted mid-run)"
    elif killed:
        outcome = "failed (watchdog killed)"
    else:
        outcome = "failed (watchdog: no live worker found)"
    lines = [
        f"watchdog_killed: {'true' if killed else 'false'}",
        f"host_rebooted_mid_run: {'true' if host_rebooted else 'false'}",
        f"stage_killed: {stages_killed[0] if stages_killed else 'none'}",
        f"last_known_stage: {run.current_stage if run.current_stage is not None else 'unknown'}",
        f"stage_sessions_issue: {run.stage_sessions_issue or 'none'}",
        f"deadline_epoch: {run.deadline_epoch}",
        f"checked_at: {now.isoformat()}",
        f"killed_agents: {', '.join(sorted(killed_agents)) or 'none'}",
        f"heartbeat_last_line: {heartbeat_last_line or 'none found'}",
        "",
    ]
    if host_rebooted:
        lines.append(
            "The host rebooted after this run's orchestrator last wrote state.json — the "
            "orchestrator and its `systemd-run` launcher unit did not survive it. Failing "
            "the run now instead of waiting out its 7h deadline. This run was not resumed "
            "— a human should look at the PR (if any) and the shared worktree, and check "
            "why the host restarted mid-run."
        )
    elif killed:
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
    if run.stage_sessions_issue is not None:
        lines.append("")
        lines.append(
            f"Stage-independence gate (design.md G-17): {run.stage_sessions_issue}. "
            "Any PR from this run is single-session work at best — review it as such."
        )
    # Atomic (tmp + rename), the same writer the orchestrator uses for this file: a torn
    # report would count as terminal to `_has_terminal_report` yet fail tick's reconcile.
    write_terminal_report(
        report_path,
        run_id=run.run_id,
        outcome=outcome,
        lines=lines,
        title="watchdog report",
    )
    return report_path


def _notify(client: HerdrClient, title: str, *, body: str | None) -> None:
    """Best-effort: a notification failure must not abort the sweep."""
    try:
        client.notification_show(title, body=body, sound="request")
    except HerdrCliError as e:
        log.warning("%s: notification failed: %s", title, e)


def launch_resume(argv: list[str], *, timeout_s: float = 30.0) -> tuple[int, str, str]:
    """Module-level seam for registering the resumed run's systemd unit (monkeypatched
    in tests like `tick.launch_pipeline`)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 124, "", str(e)
    return proc.returncode, proc.stdout, proc.stderr


def _read_state_json(path: Path) -> dict[str, object] | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


@dataclass(frozen=True, slots=True)
class _ResumePlan:
    repo_parent: Path
    report_path: Path
    matching: HistoryRecord
    budget_ms: int
    new_deadline: int
    resume_count: int
    unit_name: str
    resumed_from: str | None


def _resume_plan(
    raw_state: Mapping[str, object],
    run: InflightRun,
    recorded_history: Sequence[HistoryRecord],
    *,
    now: datetime,
) -> tuple[_ResumePlan | None, str | None]:
    """Everything a safe automatic resume needs, or why this run is not eligible.

    Never guesses: an old `state.json` without `repo_parent` (pre-issue-068) and a manual
    run without tick's `running` history record are both ineligible, not resumed with a
    default.
    """
    repo_parent = raw_state.get("repo_parent")
    if not isinstance(repo_parent, str) or not repo_parent:
        return None, "missing repo_parent in state.json"
    artifacts = raw_state.get("artifact_paths")
    report = artifacts.get("report") if isinstance(artifacts, dict) else None
    if not isinstance(report, str) or not report:
        return None, "missing artifact_paths.report in state.json"
    resume_count = raw_state.get("resume_count")
    if not isinstance(resume_count, int) or isinstance(resume_count, bool):
        resume_count = 0
    if resume_count >= MAX_PIPELINE_RESUMES:
        return None, (
            f"resume_count cap reached (resume_count={resume_count}, "
            f"MAX_PIPELINE_RESUMES={MAX_PIPELINE_RESUMES})"
        )
    matching = next(
        (
            rec
            for rec in recorded_history
            if rec.state == "running"
            and (rec.extra or {}).get("pipeline_run_id") == run.run_id
        ),
        None,
    )
    if matching is None:
        return None, "no running history record for this run"
    recorded_deadline = (matching.extra or {}).get("deadline_epoch")
    if not isinstance(recorded_deadline, int) or isinstance(recorded_deadline, bool):
        recorded_deadline = run.deadline_epoch
    budget_ms = (recorded_deadline - int(matching.ts.timestamp())) * 1000
    if budget_ms <= 0:
        return None, f"non-positive original budget ({budget_ms} ms)"
    old_unit = (matching.extra or {}).get("unit")
    return (
        _ResumePlan(
            repo_parent=Path(repo_parent),
            report_path=Path(report),
            matching=matching,
            budget_ms=budget_ms,
            new_deadline=int(now.timestamp()) + budget_ms // 1000,
            resume_count=resume_count + 1,
            unit_name=f"{PIPELINE_UNIT_PREFIX}{run.run_id}-r{now.strftime('%H%M%S')}",
            resumed_from=old_unit if isinstance(old_unit, str) else None,
        ),
        None,
    )


def _resume_argv(plan: _ResumePlan, run: InflightRun) -> list[str]:
    return [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit={plan.unit_name}",
        "-p",
        f"RuntimeMaxSec={plan.budget_ms // 1000 + RESUME_UNIT_MARGIN_S}",
        "/bin/bash",
        str(plan.repo_parent / "scripts" / "pipeline-launch.sh"),
        "--run-id",
        run.run_id,
        "--repo-parent",
        str(plan.repo_parent),
        "--report",
        str(plan.report_path),
        "--state-json",
        str(run.state_path),
        "--resume",
        "--deadline-epoch",
        str(plan.new_deadline),
        "--agent-name",
        "pipeline-resume",
        "--wait-timeout-ms",
        str(plan.budget_ms),
    ]


def _resume_run(
    client: HerdrClient,
    run: InflightRun,
    *,
    raw_state: Mapping[str, object],
    recorded_history: Sequence[HistoryRecord],
    history_path: Path,
    reports_dir: Path,
    heartbeat_dir: Path,
    host_rebooted: bool,
    now: datetime,
) -> tuple[WatchdogAction | None, str | None]:
    """Try the one allowed resume. Returns (action, reason): `(None, reason)` means not
    eligible and the caller falls through to kill-and-report."""
    plan, reason = _resume_plan(raw_state, run, recorded_history, now=now)
    if plan is None:
        return None, reason
    try:
        panes = (
            client.pipeline_agent_panes(run.run_id)
            if host_rebooted
            else client.live_pipeline_agent_panes(run.run_id)
        )
    except HerdrCliError as e:
        log.warning("%s: could not list agents, skipping this cycle: %s", run.run_id, e)
        return None, None

    append(
        history_path,
        HistoryRecord(
            ts=now,
            job=plan.matching.job,
            state="running",
            run_id=plan.matching.run_id,
            extra={
                "pipeline_run_id": run.run_id,
                "report": str(plan.report_path),
                "unit": plan.unit_name,
                "deadline_epoch": plan.new_deadline,
                "resume_count": plan.resume_count,
                "resumed_from": plan.resumed_from,
            },
        ),
    )

    killed_agents: dict[str, str] = {}
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

    state_payload = dict(raw_state)
    state_payload["deadline_epoch"] = plan.new_deadline
    state_payload["resume_count"] = plan.resume_count
    state_payload["resumed_at"] = now.isoformat()
    try:
        write_state_json(run.state_path, state_payload)
    except OSError as e:
        log.warning("%s: failed to rewrite state for resume: %s", run.run_id, e)

    stage_killed = next(
        (
            s
            for s in (_stage_from_agent_name(n, run.run_id) for n in killed_agents)
            if s is not None
        ),
        None,
    )
    rc, stdout, stderr = launch_resume(_resume_argv(plan, run))
    if rc != 0:
        heartbeat_last_line = _heartbeat_last_line(
            heartbeat_log_path(heartbeat_dir, run.run_id)
        )
        report_path = reports_dir / f"pipeline-{run.run_id}.md"
        lines = [
            "watchdog_killed: false",
            f"host_rebooted_mid_run: {'true' if host_rebooted else 'false'}",
            "stage_killed: none",
            f"last_known_stage: {run.current_stage if run.current_stage is not None else 'unknown'}",
            f"stage_sessions_issue: {run.stage_sessions_issue or 'none'}",
            f"deadline_epoch: {plan.new_deadline}",
            f"checked_at: {now.isoformat()}",
            f"killed_agents: {', '.join(sorted(killed_agents)) or 'none'}",
            f"heartbeat_last_line: {heartbeat_last_line or 'none found'}",
            f"resume_attempt_rc: {rc}",
            f"resume_attempt_stderr: {(stderr.strip() or stdout.strip()) or 'none'}",
            "",
            (
                "The run was eligible for its one automatic resume, but registering "
                "the resume unit failed. A human should restart it once the cause is "
                "fixed."
            ),
        ]
        write_terminal_report(
            report_path,
            run_id=run.run_id,
            outcome="failed (resume_failed)",
            lines=lines,
            title="watchdog report",
        )
        _notify(
            client,
            f"herdr-routines: pipeline {run.run_id} resume failed",
            body=f"resume launch failed (rc {rc}); see {report_path}",
        )
        return (
            WatchdogAction(
                run_id=run.run_id,
                killed_agents=tuple(sorted(killed_agents)),
                stage_killed=stage_killed,
                report_path=report_path,
                resumed=False,
            ),
            None,
        )

    _notify(
        client,
        f"herdr-routines: pipeline {run.run_id} stalled, resuming",
        body=f"resumed from stage {run.current_stage} (unit {plan.unit_name}); "
        f"budget {plan.budget_ms // 1000}s",
    )
    return (
        WatchdogAction(
            run_id=run.run_id,
            killed_agents=tuple(sorted(killed_agents)),
            stage_killed=stage_killed,
            report_path=None,
            resumed=True,
        ),
        None,
    )


def _reap_stalled_run(
    client: HerdrClient,
    run: InflightRun,
    *,
    reports_dir: Path,
    heartbeat_dir: Path,
    now: datetime,
    boot_epoch: float | None,
    history_path: Path | None = None,
) -> WatchdogAction | None:
    """One run's share of the sweep: None when it is healthy, spared, or can't be listed
    this cycle; otherwise the action taken."""
    if not is_stalled(run, now=now, heartbeat_dir=heartbeat_dir, boot_epoch=boot_epoch):
        return None
    # Stale heartbeat is only a guess at death. A live unit (e.g. a resumed run, or an
    # orchestrator blocked in a long call) is left alone, quietly: the watchdog fires
    # every 15 min, so a notification would just repeat, and the unit is bounded by
    # its own RuntimeMaxSec. "Can't tell" is treated the same — never reap on doubt.
    unit_active = pipeline_unit_active(run.run_id)
    if unit_active is not False:
        log.warning(
            "%s: looks stalled but %s; not reaping",
            run.run_id,
            "its systemd unit is still active"
            if unit_active
            else "systemd liveness is unknown",
        )
        return None
    host_rebooted = _host_rebooted_after_last_state_write(run, boot_epoch=boot_epoch)

    # One automatic resume attempt (issue 068) before the terminal path. A missing
    # history file means no tick-launched run can be identified, so nothing is resumable.
    raw_state = _read_state_json(run.state_path)
    recorded_history = read_all(history_path) if history_path is not None else []
    resume_reason: str | None = None
    if raw_state is not None and history_path is not None:
        resumed_action, resume_reason = _resume_run(
            client,
            run,
            raw_state=raw_state,
            recorded_history=recorded_history,
            history_path=history_path,
            reports_dir=reports_dir,
            heartbeat_dir=heartbeat_dir,
            host_rebooted=host_rebooted,
            now=now,
        )
        if resumed_action is not None:
            return resumed_action
        if resume_reason is None:
            return None
    elif raw_state is not None:
        resume_reason = "history missing, so no running record for this run"

    killed_agents: dict[str, str] = {}
    try:
        # A worker left running is closed only while `working`: a `blocked` one is
        # normally left open for a human. Not after a reboot — herdr restores every
        # pane's agent at boot as `blocked`, so a dead run's pl- agents come back as
        # resurrected processes holding ~550 MB each (2026-10-01: four of them pinned
        # 2.2 GB of the Pi's 4 GB), and nobody is coming to inspect them.
        panes = (
            client.pipeline_agent_panes(run.run_id)
            if host_rebooted
            else client.live_pipeline_agent_panes(run.run_id)
        )
    except HerdrCliError as e:
        log.warning("%s: could not list agents, skipping this cycle: %s", run.run_id, e)
        return None
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
        host_rebooted=host_rebooted,
    )
    if resume_reason is not None:
        with report_path.open("a") as handle:
            handle.write(f"\nresume_fallback_reason: {resume_reason}\n")
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
    elif host_rebooted:
        _notify(
            client,
            f"herdr-routines: pipeline {run.run_id} failed, host rebooted mid-run",
            body=f"host booted after the orchestrator's last state write; "
            f"failed early instead of waiting the deadline. See {report_path}",
        )
    else:
        _notify(
            client,
            f"herdr-routines: pipeline {run.run_id} stalled, no report",
            body=f"deadline exceeded, no live worker found; see {report_path}",
        )
    return WatchdogAction(
        run_id=run.run_id,
        killed_agents=tuple(sorted(killed_agents)),
        stage_killed=stage_killed,
        report_path=report_path,
    )


def run_watchdog(
    *,
    client: HerdrClient,
    worktrees_root: Path,
    reports_dir: Path,
    heartbeat_dir: Path,
    history_path: Path | None = None,
    now: datetime | None = None,
    boot_epoch: float | None = None,
) -> list[WatchdogAction]:
    """One watchdog sweep: find every stalled in-flight run, kill any live worker it
    left running, write its terminal report, and notify. Never raises — a single run's
    failure of any kind (`herdr`, a filesystem error writing its report) is logged and
    the sweep continues with the rest (fail-open, same posture as `ps.py`/`gc.py`)."""
    now = now or datetime.now(UTC)
    if boot_epoch is None:
        boot_epoch = system_boot_epoch()
    actions: list[WatchdogAction] = []
    recorded_deadlines = (
        recorded_pipeline_deadlines(history_path) if history_path is not None else {}
    )
    for run in find_inflight_runs(worktrees_root, reports_dir, recorded_deadlines):
        try:
            action = _reap_stalled_run(
                client,
                run,
                reports_dir=reports_dir,
                heartbeat_dir=heartbeat_dir,
                now=now,
                boot_epoch=boot_epoch,
                history_path=history_path,
            )
        except Exception:  # one run's failure, of any kind, must not abort the sweep
            log.exception("%s: watchdog failed for this run; continuing", run.run_id)
            continue
        if action is not None:
            actions.append(action)
    return actions
