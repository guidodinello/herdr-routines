"""herdr-routines CLI: tick | status | ps | scheduled | history | validate | run | gc.

See docs/plan-v1.md §5 and docs/pipeline/runs/20260825T070012Z/spec.md for the two
read-only visibility commands."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from logger import get_logger, init_logging

import herdr_routines
from herdr_routines.config import (
    ConfigError,
    RoutinesConfig,
    default_config_path,
    load_config,
)
from herdr_routines.gc import run_gc, run_gc_delete
from herdr_routines.herdr import HerdrClient
from herdr_routines.history import (
    default_history_path,
    first_seen_at,
    last_terminal_run,
    read_job,
)
from herdr_routines.pick_feature import run_pick_feature
from herdr_routines.ps import collect_ps_rows, render_ps
from herdr_routines.runner import build_dry_run_argv, execute_run, make_run_id
from herdr_routines.schedule import Decision, decide
from herdr_routines.scheduled import build_scheduled_rows, render_scheduled
from herdr_routines.tick import default_lock_path, run_tick, tick_lock

log = get_logger(__name__)


def default_log_path() -> Path:
    """$HERDR_PLUGIN_STATE_DIR/herdr-routines.log if set, else ~/.local/state/herdr-routines/.

    Same convention as history.default_history_path and tick.default_lock_path.
    """
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "herdr-routines.log"


def main(argv: list[str] | None = None) -> int:
    init_logging(log_file=default_log_path())
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def _get_version() -> str:
    """Installed distribution version (pyproject.toml is authoritative); never raises."""
    try:
        return importlib.metadata.version("herdr-routines")
    except importlib.metadata.PackageNotFoundError:
        return herdr_routines.__version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-routines")
    parser.add_argument("--config", type=Path, default=None, help="path to jobs.yaml")
    # On the top-level parser so the version action fires before the required-subcommand
    # check — --version/-V must work with no jobs.yaml and no Herdr server.
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=_get_version(),
        help="show program's version number and exit",
    )
    sub = parser.add_subparsers(required=True)

    p_tick = sub.add_parser(
        "tick", help="evaluate all jobs and run any that are due (for systemd)"
    )
    p_tick.set_defaults(handler=_cmd_tick)

    p_status = sub.add_parser(
        "status", help="one line per job: last run, and whether it's due"
    )
    p_status.set_defaults(handler=_cmd_status)

    p_ps = sub.add_parser(
        "ps",
        help="read-only: what's currently running (live agents + in-progress pipeline runs)",
    )
    p_ps.add_argument("--json", action="store_true")
    p_ps.set_defaults(handler=_cmd_ps)

    p_scheduled = sub.add_parser(
        "scheduled",
        help="read-only: every configured job with its next-fire time (disabled included)",
    )
    p_scheduled.add_argument("--json", action="store_true")
    p_scheduled.set_defaults(handler=_cmd_scheduled)

    p_history = sub.add_parser("history", help="recent runs for one job")
    p_history.add_argument("job")
    p_history.add_argument("-n", "--limit", type=int, default=20)
    p_history.add_argument("--json", action="store_true")
    p_history.set_defaults(handler=_cmd_history)

    p_validate = sub.add_parser("validate", help="check jobs.yaml for problems")
    p_validate.add_argument(
        "--systemd-unit",
        type=Path,
        default=Path("deploy/systemd/herdr-routines.service"),
        help="also check this unit's TimeoutStartSec against the largest job timeout",
    )
    p_validate.set_defaults(handler=_cmd_validate)

    p_run = sub.add_parser(
        "run", help="force one job to run now, ignoring its schedule"
    )
    p_run.add_argument("job")
    p_run.add_argument(
        "--dry-run", action="store_true", help="print the herdr argv, run nothing"
    )
    p_run.set_defaults(handler=_cmd_run)

    p_gc = sub.add_parser(
        "gc", help="inventory and optional deletion of stale auto/* branches (pure git)"
    )
    mode = p_gc.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="list only; writes and deletes nothing",
    )
    mode.add_argument(
        "--delete",
        "--prune",
        dest="delete",
        action="store_true",
        help="delete stale branches (requires --yes unless interactive)",
    )
    p_gc.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="bypass interactive guard; required for non-interactive deletion",
    )
    p_gc.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="also delete stale-but-unmerged (orphaned) branches",
    )
    p_gc.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="target repository (default: current directory)",
    )
    p_gc.add_argument(
        "--base",
        default=None,
        help="merge target for the merged-check (default: origin/HEAD, else main)",
    )
    p_gc.set_defaults(handler=_cmd_gc)

    p_pick = sub.add_parser(
        "pick-feature",
        help="pick the next open docs/process/issues/ item for the pipeline's stage 0",
    )
    p_pick.add_argument(
        "--issues-dir",
        type=Path,
        default=Path("docs/process/issues"),
        help="directory of issue files (default: docs/process/issues, relative to cwd)",
    )
    p_pick.add_argument(
        "--mark-in-progress",
        action="store_true",
        help="flip the picked issue's status to in-progress before printing it",
    )
    p_pick.set_defaults(handler=_cmd_pick_feature)

    return parser


def _load_config_or_exit(args: argparse.Namespace) -> RoutinesConfig:
    path = args.config or default_config_path()
    try:
        return load_config(path)
    except ConfigError as e:
        log.error("failed to load config %s: %s", path, e)
        raise SystemExit(1) from e


def _cmd_tick(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args)
    history_path = default_history_path()
    lock_path = default_lock_path()
    with tick_lock(lock_path) as acquired:
        if not acquired:
            # Another tick is already running — skip quietly, per docs/plan-v1.md §4.
            return 0
        now = datetime.now(UTC)
        client = HerdrClient()
        outcome = run_tick(config, history_path, client=client, now=now)
        for line in outcome.summaries:
            log.info(line)
        return 1 if outcome.any_job_failed else 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args)
    history_path = default_history_path()
    now = datetime.now(UTC)
    for job in config.jobs:
        last = last_terminal_run(history_path, job.name)
        last_desc = f"{last.state} at {last.ts.isoformat()}" if last else "never run"
        result = decide(
            cron=job.cron,
            timezone=job.timezone,
            catch_up_minutes=job.catch_up_minutes,
            now=now,
            last_terminal=last,
            job_registered_at=first_seen_at(history_path, job.name) or now,
        )
        due_desc = {
            Decision.NOT_DUE: "not due",
            Decision.RUN: "due now",
            Decision.MISSED: "missed",
        }[result.decision]
        enabled_desc = "" if job.enabled else " [disabled]"
        print(f"{job.name}{enabled_desc}: last={last_desc}; {due_desc}")
    return 0


def _cmd_ps(args: argparse.Namespace) -> int:
    # Read-only by construction: no history.append, no config write, no state.json touch
    # (spec 20260825T070012Z criterion 5). Unreachable Herdr degrades to a warning + empty
    # table with exit 0 rather than a crash.
    rows, warnings = collect_ps_rows(HerdrClient())
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.json:
        print(
            json.dumps(
                [
                    {"agent": r.agent, "status": r.status, "detail": r.detail}
                    for r in rows
                ]
            )
        )
    else:
        print(render_ps(rows))
        # One warning is enough: when collect_ps_rows already explained why the table
        # is empty (e.g. Herdr unreachable), don't also print the generic one.
        if not rows and not warnings:
            print("warning: nothing currently running", file=sys.stderr)
    return 0


def _cmd_scheduled(args: argparse.Namespace) -> int:
    # Read-only: loads jobs.yaml, reads history.jsonl, writes neither.
    config = _load_config_or_exit(args)
    rows = build_scheduled_rows(config, default_history_path(), now=datetime.now(UTC))
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": r.name,
                        "enabled": r.enabled,
                        "cron": r.cron,
                        "timezone": r.timezone,
                        "next_fire": r.next_fire.isoformat() if r.next_fire else None,
                        "last_state": r.last_state,
                        "last_run_at": (
                            r.last_run_at.isoformat() if r.last_run_at else None
                        ),
                    }
                    for r in rows
                ]
            )
        )
    else:
        print(render_scheduled(rows))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    history_path = default_history_path()
    records = read_job(history_path, args.job, limit=args.limit)
    if args.json:
        print(json.dumps([json.loads(r.to_json_line()) for r in records]))
        return 0
    for r in records:
        extra = f" {r.extra}" if r.extra else ""
        print(f"{r.ts.isoformat()} {r.job} {r.state} run_id={r.run_id}{extra}")
    return 0


# Margin added on top of the largest job's timeout_ms when checking the systemd service unit's
# TimeoutStartSec — see docs/plan-v1.md §3 on why TimeoutStartSec must never be `infinity`.
SYSTEMD_TIMEOUT_MARGIN_SECONDS = 300

_TIMEOUT_START_SEC_RE = re.compile(
    r"^TimeoutStartSec=(?P<value>\S.*?)\s*$", re.MULTILINE
)

# systemd.time(7) unit suffixes, in seconds. A bare number with no suffix means seconds.
_SYSTEMD_TIME_UNIT_SECONDS = {
    "us": 0.000001,
    "usec": 0.000001,
    "ms": 0.001,
    "msec": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
}

_TIME_SPAN_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)")


def _parse_systemd_seconds(value: str) -> float | None:
    """Parse a systemd.time(7) span (e.g. "3000", "50min", "1h 30min") into seconds, or None
    if it doesn't match the grammar we support."""
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        return float(value)

    total = 0.0
    pos = 0
    matched_any = False
    for m in _TIME_SPAN_TOKEN_RE.finditer(value):
        if m.start() != pos:
            return None
        number, unit = m.groups()
        unit = unit.lower()
        if unit not in _SYSTEMD_TIME_UNIT_SECONDS:
            return None
        total += float(number) * _SYSTEMD_TIME_UNIT_SECONDS[unit]
        matched_any = True
        pos = m.end()
    if not matched_any or pos != len(value):
        return None
    return total


def _cmd_validate(args: argparse.Namespace) -> int:
    path = args.config or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    problems = []
    warnings = []
    for job in config.jobs:
        if not job.repo.exists():
            problems.append(f"{job.name}: repo path does not exist: {job.repo}")
        elif job.workspace == "worktree" and not (job.repo / ".git").exists():
            # `workspace: worktree` calls `herdr worktree create --cwd <repo>`, which creates a
            # *new* linked worktree elsewhere (under ~/.herdr/worktrees/) from whatever `repo`
            # is — confirmed empirically against a live herdr server: a plain clone (`.git` a
            # directory) works exactly like an already-linked worktree (`.git` a file) here.
            # `repo` just needs to be a git repo at all, either shape.
            problems.append(
                f"{job.name}: repo is not a git repository (no .git): {job.repo}"
            )
        if job.enabled and "$ROUTINE_REPORT" not in job.prompt:
            # A run only settles as "done" when the report file exists and is non-empty (see
            # runner.execute_run's no_report check); the agent only writes it if the prompt
            # asks. A prompt that never mentions the placeholder can never succeed. Empty
            # prompts are diagnosed separately — they fail earlier as agent_prompt_failed
            # (config.py:87 defaults omitted prompt to ""), not no_report.
            if not job.prompt.strip():
                warnings.append(
                    f"{job.name}: prompt is empty — the run will fail before "
                    f"$ROUTINE_REPORT can be written; set a prompt that tells the "
                    f"agent to write its summary there"
                )
            else:
                warnings.append(
                    f"{job.name}: prompt never mentions $ROUTINE_REPORT — the run will always "
                    f"fail with no_report; tell the agent to write its summary there"
                )

    problems += _check_systemd_timeout(config, args.systemd_unit)

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1

    print(f"ok: {len(config.jobs)} job(s) valid")
    return 0


def _check_systemd_timeout(config: RoutinesConfig, unit_path: Path) -> list[str]:
    """If the systemd service unit exists, make sure its TimeoutStartSec covers a tick running
    every enabled job sequentially in the worst case — a unit with too small a value (or the
    tempting `infinity`) can leave a wedged tick blocking every future run forever. Silently
    skipped if the unit file isn't found, since `validate` is also usable before deployment
    exists."""
    enabled_jobs = [job for job in config.jobs if job.enabled]
    if not enabled_jobs or not unit_path.exists():
        return []

    directive_lines = [
        line
        for line in unit_path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    text = "\n".join(directive_lines)

    if re.search(r"^TimeoutStartSec=infinity\s*$", text, re.MULTILINE):
        return [
            f"{unit_path}: TimeoutStartSec=infinity is a trap — see docs/plan-v1.md §3"
        ]

    match = _TIMEOUT_START_SEC_RE.search(text)
    if match is None:
        return [f"{unit_path}: no TimeoutStartSec= line found"]

    unit_timeout_s = _parse_systemd_seconds(match.group("value"))
    if unit_timeout_s is None:
        return [
            f"{unit_path}: could not parse TimeoutStartSec={match.group('value')!r}"
        ]

    # A tick runs every due job sequentially in one service invocation, so the worst case is
    # every enabled job being due at once.
    total_job_seconds = sum(
        (job.start_timeout_ms + job.timeout_ms) / 1000 for job in enabled_jobs
    )
    required_s = total_job_seconds + SYSTEMD_TIMEOUT_MARGIN_SECONDS
    if unit_timeout_s < required_s:
        message = (
            f"{unit_path}: TimeoutStartSec={unit_timeout_s:.0f} is less than the sum of every "
            f"enabled job's start+run timeout ({total_job_seconds:.0f}s, worst case for one "
            f"tick) plus {SYSTEMD_TIMEOUT_MARGIN_SECONDS}s margin = {required_s:.0f}s — bump it"
        )
        return [message]
    return []


def _cmd_run(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args)
    job = config.job(args.job)
    if job is None:
        log.error("no such job: %s", args.job)
        return 1

    now = datetime.now(UTC)
    run_id = make_run_id(job.name, now)

    if args.dry_run:
        for command in build_dry_run_argv(job, run_id=run_id):
            print(" ".join(command))
        return 0

    log.info("%s: starting run %s", job.name, run_id)
    client = HerdrClient()
    outcome = execute_run(job, client, run_id=run_id)
    if outcome.state == "done":
        log.info("%s: %s (%s)", job.name, outcome.state, outcome.reason or "ok")
    else:
        log.error("%s: %s (%s)", job.name, outcome.state, outcome.reason or "ok")
    return 0 if outcome.state == "done" else 1


def _cmd_gc(args: argparse.Namespace) -> int:
    # No HerdrClient, no config load — pure git + filesystem (spec.md §No Herdr server).
    repo = args.repo or Path.cwd()
    if args.delete:
        return run_gc_delete(
            repo, base=args.base, force=args.force, assume_yes=args.yes
        )
    return run_gc(repo, base=args.base)


def _cmd_pick_feature(args: argparse.Namespace) -> int:
    # No HerdrClient — pure filesystem, same posture as gc (docs/process/README.md).
    return run_pick_feature(args.issues_dir, mark=args.mark_in_progress)


if __name__ == "__main__":
    raise SystemExit(main())
