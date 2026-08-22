"""herdr-routines CLI: tick | status | history | validate | run. See docs/plan-v1.md §5."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from logger import get_logger, init_logging

from herdr_routines.config import (
    ConfigError,
    RoutinesConfig,
    default_config_path,
    load_config,
)
from herdr_routines.herdr import HerdrClient
from herdr_routines.history import default_history_path, last_terminal_run, read_job
from herdr_routines.runner import build_dry_run_argv, execute_run, make_run_id
from herdr_routines.schedule import Decision, decide
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-routines")
    parser.add_argument("--config", type=Path, default=None, help="path to jobs.yaml")
    sub = parser.add_subparsers(required=True)

    p_tick = sub.add_parser(
        "tick", help="evaluate all jobs and run any that are due (for systemd)"
    )
    p_tick.set_defaults(handler=_cmd_tick)

    p_status = sub.add_parser(
        "status", help="one line per job: last run, next occurrence"
    )
    p_status.set_defaults(handler=_cmd_status)

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
        for line in run_tick(config, history_path, client=client, now=now):
            log.info(line)
    return 0


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
            job_registered_at=now,
        )
        due_desc = {
            Decision.NOT_DUE: "not due",
            Decision.RUN: "due now",
            Decision.MISSED: "missed",
        }[result.decision]
        enabled_desc = "" if job.enabled else " [disabled]"
        print(f"{job.name}{enabled_desc}: last={last_desc}; {due_desc}")
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

_TIMEOUT_START_SEC_RE = re.compile(r"^TimeoutStartSec=(\d+)\s*$", re.MULTILINE)


def _cmd_validate(args: argparse.Namespace) -> int:
    path = args.config or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    problems = []
    for job in config.jobs:
        if not job.repo.exists():
            problems.append(f"{job.name}: repo path does not exist: {job.repo}")
        elif job.workspace == "worktree" and not (job.repo / ".git").exists():
            problems.append(f"{job.name}: repo is not a git worktree root: {job.repo}")

    problems += _check_systemd_timeout(config, args.systemd_unit)

    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1

    print(f"ok: {len(config.jobs)} job(s) valid")
    return 0


def _check_systemd_timeout(config: RoutinesConfig, unit_path: Path) -> list[str]:
    """If the systemd service unit exists, make sure its TimeoutStartSec covers the largest
    job timeout — a unit with too small a value (or the tempting `infinity`) can leave a wedged
    tick blocking every future run forever. Silently skipped if the unit file isn't found, since
    `validate` is also usable before deployment exists."""
    if not config.jobs or not unit_path.exists():
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

    unit_timeout_s = int(match.group(1))
    max_job_timeout_s = max(job.timeout_ms for job in config.jobs) / 1000
    required_s = max_job_timeout_s + SYSTEMD_TIMEOUT_MARGIN_SECONDS
    if unit_timeout_s < required_s:
        message = (
            f"{unit_path}: TimeoutStartSec={unit_timeout_s} is less than the largest job "
            f"timeout ({max_job_timeout_s:.0f}s) plus {SYSTEMD_TIMEOUT_MARGIN_SECONDS}s margin "
            f"= {required_s:.0f}s — bump it"
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


if __name__ == "__main__":
    raise SystemExit(main())
