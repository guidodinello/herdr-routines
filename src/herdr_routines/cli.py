"""herdr-routines CLI: tick | status | ps | scheduled | history | validate | run | gc |
self-update.

The list is deliberately partial — `self-update` was added here for issue 053, not as a
sweep over the other subparsers.

See docs/plan-v1.md §5 and docs/pipeline/runs/20260825T070012Z/spec.md for the two
read-only visibility commands."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from logger import get_logger, init_logging

import herdr_routines
from herdr_routines import gc, reports_prune, tick
from herdr_routines.auto_fix import RealGhClient, effective_trusted_authors
from herdr_routines.config import (
    DEFAULT_REPORTS_MAX_AGE_DAYS,
    TINY_HISTORY_MAX_BYTES,
    ConfigError,
    Job,
    RoutinesConfig,
    default_config_path,
    load_config,
)
from herdr_routines.digest import digest_now
from herdr_routines.gates import (
    GATE_STAGE_CHOICES,
    PR_GATE_STAGES,
    remote_owner_and_repo,
    run_stage_gate,
)
from herdr_routines.gc import run_gc, run_gc_delete
from herdr_routines.herdr import HerdrClient, HerdrCliError
from herdr_routines.history import (
    TRIGGER_MANUAL,
    HistoryExtra,
    default_history_path,
    first_seen_at,
    last_scheduled_terminal_run,
    last_terminal_run,
    read_job,
)
from herdr_routines.host_drift import check_host, format_findings
from herdr_routines.issue_refinement import (
    job_is_opencode_only,
    run_check_issue,
    run_loop_check,
    run_refine_issue,
)
from herdr_routines.pick_feature import ReclaimedPick, run_pick_feature
from herdr_routines.pipeline_prepare import (
    DEFAULT_ISSUES_DIR,
    EXIT_FAILURE,
    EXIT_NO_FEATURE,
    EXIT_PREPARED,
    prepare_run,
    render_prepared_values,
)
from herdr_routines.pipeline_run import EXIT_PARTIAL_OR_FAILED, run_pipeline
from herdr_routines.pipeline_watchdog import (
    PIPELINE_UNIT_PREFIX,
    default_heartbeat_dir,
    default_worktrees_root,
    run_watchdog,
)
from herdr_routines.ps import collect_ps_rows, render_ps
from herdr_routines.reports_prune import (
    PruneResult,
    prune_reports,
    prune_rolled_history,
)
from herdr_routines.review_scan import (
    Candidate,
    Classification,
    classify_review_candidates,
)
from herdr_routines.runner import (
    DEFAULT_FAILURE_MARKERS,
    build_dry_run_argv,
    default_reports_dir,
    make_run_id,
)
from herdr_routines.schedule import Decision, decide
from herdr_routines.scheduled import build_scheduled_rows, render_scheduled
from herdr_routines.self_update import default_state_path, run_self_update
from herdr_routines.table import render_table
from herdr_routines.telegram_spawn import alias_table, handle_spawn_message
from herdr_routines.tick import (
    _build_pipeline_launch_argv,
    default_lock_path,
    launch_pipeline,
    pipeline_deadline_epoch,
    pipeline_report_path,
    run_tick,
    tick_lock,
)
from herdr_routines.tmp_hygiene import reap_tmp

log = get_logger(__name__)

# Shared with `pipeline_run`'s contract (0 ok, 1 partial/failed) and argparse's own usage
# code. A gate given a missing requirement returns 2, not 1: nothing was checked, and
# filing "failed" in tick history for a typo is the wrong signal.
EXIT_OK = 0
EXIT_USAGE = 2


def _default_runner_checkout() -> Path:
    """The runner checkout — the one the systemd units run. `self-update`,
    `host-check` and `digest` all act on it by default, never on the cwd (see
    `self-update --path`)."""
    return Path.home() / "projects" / "herdr-routines"


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
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


def _get_version() -> str:
    """Installed distribution version (pyproject.toml is authoritative); never raises."""
    try:
        return importlib.metadata.version("herdr-routines")
    except importlib.metadata.PackageNotFoundError:
        return herdr_routines.__version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-routines")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to jobs.d/ directory or jobs.yaml file",
    )
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

    p_validate = sub.add_parser(
        "validate", help="check jobs.d/ or jobs.yaml for problems"
    )
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
    p_run.add_argument(
        "--run-id",
        default=None,
        help=(
            "resume with an existing run id. For a plain job this is the full "
            "<job>-<timestamp>; for kind: pipeline it is the orchestrator's bare "
            "UTC timestamp RUN_ID (design.md's same-RUN_ID relaunch path)"
        ),
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
        help="delete stale branches (requires --yes)",
    )
    p_gc.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="required for gc --delete; confirms deletion without prompt",
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
    p_gc.add_argument(
        "--older-than",
        type=int,
        default=gc.DEFAULT_OLDER_THAN_DAYS,
        dest="older_than_days",
        help=(
            "days since merge (PR mergedAt, else tip committer date) before a merged "
            "branch is collectable; 0 disables the age gate (default: "
            f"{gc.DEFAULT_OLDER_THAN_DAYS})"
        ),
    )
    p_gc.add_argument(
        "--worktrees-root",
        type=Path,
        default=None,
        help=(
            "worktree parent dir to scan for orphaned dirs/symlinks (issue 045); "
            "default: ~/.herdr/worktrees/herdr-routines"
        ),
    )
    p_gc.set_defaults(handler=_cmd_gc)

    p_prune = sub.add_parser(
        "prune",
        help=(
            "delete old state-dir artifacts (issue 021): reports by default, or rolled "
            "history files. Nothing is ever deleted without --yes"
        ),
    )
    p_prune.add_argument(
        "target",
        choices=["reports", "history"],
        help="'reports' prunes the reports dir; 'history' prunes rolled history-*.jsonl",
    )
    prune_mode = p_prune.add_mutually_exclusive_group()
    prune_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="list only; deletes nothing (the default, so the flag is for explicitness)",
    )
    prune_mode.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="required to delete; without it the listing prints and nothing is removed",
    )
    p_prune.add_argument(
        "--older-than",
        type=int,
        default=None,
        dest="older_than_days",
        help=(
            "age in days before an entry is collectable (mtime-based); reports default to "
            f"retention.reports_max_age_days (fallback {DEFAULT_REPORTS_MAX_AGE_DAYS}), "
            f"history to {reports_prune.DEFAULT_HISTORY_MAX_AGE_DAYS}"
        ),
    )
    p_prune.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help="reports directory to prune (default: $HERDR_PLUGIN_STATE_DIR/reports)",
    )
    p_prune.set_defaults(handler=_cmd_prune)

    p_gate = sub.add_parser(
        "gate",
        help=(
            "run a hard pipeline gate and read the result off its exit code "
            "(issues 034/035: the CI gate and gate 7's reply coverage used to be "
            "prose in the orchestrator prompt, now real code)"
        ),
    )
    p_gate.add_argument(
        "--stage",
        required=True,
        choices=list(GATE_STAGE_CHOICES),
        help="which gate to run: 1/2 check the run's own artifacts, 5 the recorded PR, "
        "6 its reviews, 7 reply coverage, 'ci' the PR checks",
    )
    p_gate.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number — required for stages 5, 6, 7 and 'ci', which all read a PR",
    )
    p_gate.add_argument(
        "--spec",
        type=Path,
        default=None,
        help="run spec to check — required for stages 1 and 2, which read no PR",
    )
    p_gate.add_argument(
        "--branch",
        default=None,
        help="branch the PR under test was opened from — required for stage 5, which "
        "confirms the PR is the run's own and still open",
    )
    p_gate.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="git checkout to resolve the GitHub owner/repo from (default: cwd)",
    )
    p_gate.set_defaults(handler=_cmd_gate)

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
    p_pick.add_argument(
        "--repo",
        type=Path,
        default=None,
        help=(
            "git checkout to run `gh pr list` from, for stale-claim reclamation "
            "(default: cwd; only used with --mark-in-progress)"
        ),
    )
    p_pick.set_defaults(handler=_cmd_pick_feature)

    p_tmp_hygiene = sub.add_parser(
        "tmp-hygiene",
        help="age-based /tmp cleanup for leaked agent-runtime .so files and pytest artifacts",
    )
    p_tmp_hygiene.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("/tmp"),
        help="directory to clean (default: /tmp)",
    )
    p_tmp_hygiene.add_argument(
        "--max-age",
        type=int,
        default=3600,
        help="max age in seconds for files to be reaped (default: 3600 = 1h)",
    )
    p_tmp_hygiene.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be removed without mutating",
    )
    p_tmp_hygiene.set_defaults(handler=_cmd_tmp_hygiene)

    p_prepare = sub.add_parser(
        "pipeline-prepare",
        help="pre-flight for the overnight pipeline: sync the parent clone, pick and "
        "claim the feature, create the shared worktree/workspace and write state.json "
        "— all before any agent starts (issue 054 phase A)",
    )
    p_prepare.add_argument(
        "--run-id", required=True, help="bare UTC timestamp, e.g. 20260930T050000Z"
    )
    p_prepare.add_argument(
        "--repo-parent",
        type=Path,
        required=True,
        help="parent clone to sync and branch the run's worktree from",
    )
    p_prepare.add_argument(
        "--report",
        type=Path,
        required=True,
        help="terminal-report path to write on a skip/failure (tick reconciles from it)",
    )
    p_prepare.add_argument(
        "--deadline-epoch",
        type=int,
        required=True,
        help="the run's wall-clock deadline (unix seconds), forwarded from tick by the "
        "launcher verbatim — never computed here, so no caller can invent one",
    )
    p_prepare.add_argument(
        "--base",
        default="main",
        help="branch to sync and branch the worktree from (default: main)",
    )
    p_prepare.add_argument(
        "--issues-dir",
        default=DEFAULT_ISSUES_DIR,
        help=f"issue files to pick from, relative to --repo-parent "
        f"(default: {DEFAULT_ISSUES_DIR})",
    )
    p_prepare.set_defaults(handler=_cmd_pipeline_prepare)

    p_pipeline_run = sub.add_parser(
        "pipeline-run",
        help="run the prepared pipeline's stages in order, gating each — the stage loop "
        "that used to be an orchestrator agent (issue 056 phase B)",
    )
    p_pipeline_run.add_argument(
        "--run-id", required=True, help="bare UTC timestamp, e.g. 20260930T050000Z"
    )
    p_pipeline_run.add_argument(
        "--state-json",
        type=Path,
        required=True,
        help="the state.json pipeline-prepare wrote; the run's single source of truth "
        "for worktree, branch, feature source and deadline",
    )
    p_pipeline_run.add_argument(
        "--report",
        type=Path,
        required=True,
        help="terminal-report path, written on every outcome (tick reconciles from it)",
    )
    p_pipeline_run.add_argument(
        "--prompts-dir",
        type=Path,
        required=True,
        help="directory holding the stage prompt files named in pipeline_stages.STAGES",
    )
    p_pipeline_run.add_argument(
        "--failure-marker",
        action="append",
        default=None,
        help="text to watch each stage's screen for (repeatable; default: "
        "'Free usage exceeded'). Two consecutive sightings of the same marker end the "
        "run as failed (quota_exhausted) instead of waiting out the deadline",
    )
    p_pipeline_run.set_defaults(handler=_cmd_pipeline_run)

    p_refine = sub.add_parser(
        "refine-issue",
        help=(
            "pick the next ROADMAP Parking Lot idea to refine into an issue file "
            "(issue 029, stage 0 of the issue-refinement job)"
        ),
    )
    p_refine.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="git checkout to read ROADMAP.md/issues from and run `gh pr list` in "
        "(default: cwd)",
    )
    p_refine.add_argument(
        "--issues-dir",
        type=Path,
        default=None,
        help="issue-file directory (default: <repo>/docs/process/issues)",
    )
    p_refine.add_argument(
        "--roadmap",
        type=Path,
        default=None,
        help="ROADMAP file (default: <repo>/ROADMAP.md)",
    )
    p_refine.add_argument(
        "--allow-blocked",
        action="store_true",
        help="also consider bullets that name an unmet gate (the job must then "
        "derive the missing decision itself)",
    )
    # The job's later steps, enforced in code rather than prompt prose (issues
    # 034/035 precedent): read the verdict off the exit code like `gate`.
    refine_mode = p_refine.add_mutually_exclusive_group()
    refine_mode.add_argument(
        "--check-issue",
        type=Path,
        default=None,
        metavar="PATH",
        help="instead of selecting: check a refined issue file's frontmatter "
        "(id/title/status/priority/area, status: open); exit 1 lists the problems",
    )
    refine_mode.add_argument(
        "--review-pass",
        type=int,
        default=None,
        metavar="N",
        help="instead of selecting: after reviewer pass N, decide whether the loop "
        "runs another pass (exit 0) or stops (exit 1: consensus or the 3-pass cap); "
        "requires --reviewer-reply",
    )
    p_refine.add_argument(
        "--reviewer-reply",
        type=Path,
        default=None,
        metavar="FILE",
        help="the reviewer's raw reply for --review-pass; its 'confidence:' line "
        "is parsed here, not reported by the author",
    )
    p_refine.set_defaults(handler=_cmd_refine_issue)

    p_review_scan = sub.add_parser(
        "review-scan",
        help=(
            "read-only: classify one kind: review job's PR candidates the way the next "
            "tick's selector would (would dispatch, or the exact skip reason) without "
            "dispatching anything (issue 066)"
        ),
    )
    p_review_scan.add_argument("job")
    p_review_scan.add_argument(
        "--limit",
        type=int,
        default=100,
        help="max PRs to take from the search, 1-100 (GitHub's ceiling; default 100)",
    )
    p_review_scan.add_argument(
        "--json",
        action="store_true",
        help="emit the rows as JSON (header goes to stderr)",
    )
    p_review_scan.set_defaults(handler=_cmd_review_scan)

    p_digest = sub.add_parser(
        "digest",
        help=(
            "one morning summary of every job's last terminal state and report "
            "link, read from existing history (issue 010)"
        ),
    )
    p_digest.add_argument(
        "--timezone",
        default="UTC",
        help="timezone to render timestamps in (default: UTC)",
    )
    p_digest.add_argument(
        "--notify",
        action="store_true",
        help="also post the digest as a Herdr notification (default: print only)",
    )
    p_digest.add_argument(
        "--include-disabled",
        action="store_true",
        default=False,
        help="include disabled jobs in the digest output (default: omit them)",
    )
    p_digest.add_argument(
        "--path",
        type=Path,
        default=_default_runner_checkout(),
        help=(
            "the runner checkout to run the host drift check on "
            "(default: ~/projects/herdr-routines)"
        ),
    )
    p_digest.set_defaults(handler=_cmd_digest)

    p_watchdog = sub.add_parser(
        "pipeline-watchdog",
        help=(
            "maintenance sweep: kill any pl-<N>-<run_id> worker left running past its "
            "run's deadline by a silently-dead orchestrator, and write a terminal "
            "report in its place (issue 031)"
        ),
    )
    p_watchdog.set_defaults(handler=_cmd_pipeline_watchdog)

    p_self_update = sub.add_parser(
        "self-update",
        help=(
            "fast-forward the runner checkout to origin/<base> once the new commit is "
            "green and validates this host's config, and roll back if it does not "
            "(issue 053)"
        ),
    )
    p_self_update.add_argument(
        "--path",
        type=Path,
        default=_default_runner_checkout(),
        help=(
            "the runner checkout to update (default: ~/projects/herdr-routines, NOT "
            "the cwd — a deliberate deviation from the spec, which said Path.cwd(): a "
            "cwd default would make this update whatever directory the timer happened "
            "to start in, including the ~/.local/state/herdr-routines/repos/ clone, "
            "which is a separate lifecycle)"
        ),
    )
    p_self_update.add_argument(
        "--base",
        default="main",
        help="branch to track (default: main)",
    )
    p_self_update.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "fetch, check CI and print the old..new range and deploy diff, then stop "
            "before the fast-forward; reports 'deferred' (exit 0), including when the "
            "tick lock is held"
        ),
    )
    p_self_update.set_defaults(handler=_cmd_self_update)

    p_host_check = sub.add_parser(
        "host-check",
        help=(
            "read-only: report systemd units from deploy/systemd/ that are missing, "
            "stale or not enabled on this host, and a runner checkout behind "
            "origin/<base> (exit 1 on drift; never fetches, never installs)"
        ),
    )
    p_host_check.add_argument(
        "--path",
        type=Path,
        default=_default_runner_checkout(),
        help="the runner checkout to check (default: ~/projects/herdr-routines)",
    )
    p_host_check.add_argument(
        "--base",
        default="main",
        help="branch the runner tracks (default: main)",
    )
    p_host_check.add_argument(
        "--unit-dir",
        type=Path,
        default=None,
        help="installed user unit dir (default: ~/.config/systemd/user)",
    )
    p_host_check.set_defaults(handler=_cmd_host_check)

    # Cold-start spawn from Telegram (issue 024). The inbound-message glue: the bridge
    # plugin/bot calls this with one message's text + chat_id, and the handler either
    # replies in-chat (printed to stdout, to be sent back by the caller) or drops the
    # message. It is an event handler, not a scheduler: no clock, no lock.
    p_spawn = sub.add_parser(
        "spawn-from-telegram",
        help="handle one inbound Telegram message: cold-start a mapped session on /run or /start",
    )
    p_spawn.add_argument("--text", required=True, help="the raw message text")
    p_spawn.add_argument(
        "--chat-id", required=True, help="the sender's Telegram chat id (the auth gate)"
    )
    p_spawn.set_defaults(handler=_cmd_spawn_from_telegram)

    return parser


def _load_config_or_exit(args: argparse.Namespace) -> RoutinesConfig:
    path = args.config or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as e:
        log.error("failed to load config %s: %s", path, e)
        raise SystemExit(1) from e
    if config.errors:
        for err in config.errors:
            log.error("config: %s", err)
        raise SystemExit(1)
    return config


def _cmd_tick(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args)
    history_path = default_history_path()
    lock_path = default_lock_path()
    with tick_lock(lock_path) as acquired:
        if not acquired:
            # Another tick (or a manual `run`, or self-update) holds it — skip with
            # rc 0, per docs/plan-v1.md §4. One INFO line so contention is visible in
            # journalctl: a silent skip hid three nights of self-update collisions.
            log.info("tick: skipped, tick lock %s is held", lock_path)
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
            last_terminal=last_scheduled_terminal_run(history_path, job.name),
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

    problems: list[str] = []
    warnings: list[str] = []

    # Per-file errors from directory loader (jobs.d/ layout).
    problems.extend(config.errors)

    for job in config.jobs:
        if job.kind == "review":
            # kind: review has no single checkout: `repo` is the managed base dir, and
            # per-repo checkouts are created lazily at tick time. Nothing on disk to
            # validate here.
            pass
        elif job.repository is not None:
            # Repository jobs: skip existence/.git errors, warn if not yet cloned
            if not job.repo.exists():
                warnings.append(
                    f"{job.name}: repository job not yet cloned: {job.repo}"
                )
        else:
            if not job.repo.exists():
                problems.append(f"{job.name}: repo path does not exist: {job.repo}")
            elif (job.workspace == "worktree" or job.kind == "pipeline") and not (
                job.repo / ".git"
            ).exists():
                # `workspace: worktree` calls `herdr worktree create --cwd <repo>`, which creates a
                # *new* linked worktree elsewhere (under ~/.herdr/worktrees/) from whatever `repo`
                # is — confirmed empirically against a live herdr server: a plain clone (`.git` a
                # directory) works exactly like an already-linked worktree (`.git` a file) here.
                # `repo` just needs to be a git repo at all, either shape. `workspace:` doesn't
                # apply to kind: pipeline (config.py rejects it explicitly), but its `repo` is
                # still always the plain parent clone the orchestrator branches its own worktree
                # from — checked the same way regardless of the (unused, possibly
                # defaults.yaml-inherited) `job.workspace` value.
                problems.append(
                    f"{job.name}: repo is not a git repository (no .git): {job.repo}"
                )
        if job.kind == "pipeline":
            if (
                job.prompt_file is not None
                and not (job.repo / job.prompt_file).exists()
            ):
                problems.append(
                    f"{job.name}: prompt_file does not exist: {job.repo / job.prompt_file}"
                )
        elif (
            job.enabled
            # gated, audit, and review jobs inject their own prompts when `prompt` is
            # empty; an audit's verdict is its manifest and a review's is its posted
            # review marker, neither of which is $ROUTINE_REPORT.
            and job.kind not in ("gated", "audit", "review")
            and (job.prompt is None or "$ROUTINE_REPORT" not in job.prompt)
        ):
            # A run only settles as "done" when the report file exists and is non-empty (see
            # runner.execute_run's no_report check); the agent only writes it if the prompt
            # asks. A prompt that never mentions the placeholder can never succeed. Empty
            # prompts are diagnosed separately — they fail earlier as agent_prompt_failed
            # (config.py:87 defaults omitted prompt to ""), not no_report.
            if job.prompt is None or not job.prompt.strip():
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

    # Issue 071: a `verify` the job inherited but its kind does not honor would otherwise
    # be dropped without a word; and a prompt that runs /code-review with no
    # `review_posted` gate is done on the agent's word, the #839 incident.
    for job in config.jobs:
        if job.ignored_pick:
            warnings.append(
                f"{job.name}: inherits pick from defaults but kind {job.kind} does "
                f"not honor it (issue 073)"
            )
        if job.ignored_verify:
            warnings.append(
                f"{job.name}: inherits verify from defaults but kind {job.kind} does "
                f"not honor it yet (issue 071 phase A)"
            )
        if (
            job.enabled
            and job.kind == "routine"
            and job.prompt is not None
            and "/code-review" in job.prompt
            and "review_posted" not in job.verify
        ):
            warnings.append(
                f"{job.name}: prompt runs /code-review but verify lacks review_posted — "
                f"the run is done on the agent's word (issue 071)"
            )

    # Issue 029: the issue-refinement job is opencode only, no Claude. Keyed on the
    # prompt running the refinement selector rather than on the job name, so a
    # renamed or copied refinement job is still covered. Soft, like the checks above.
    for job in config.jobs:
        if (
            job.enabled
            and job.prompt is not None
            and "herdr-routines refine-issue" in job.prompt
            and not job_is_opencode_only(
                {
                    "agent_kind": job.agent_kind,
                    "model": job.model,
                    "fallback_model": job.fallback_model,
                }
            )
        ):
            warnings.append(
                f"{job.name}: runs `herdr-routines refine-issue` but is not opencode-only "
                f"(agent_kind={job.agent_kind}, model={job.model}, "
                f"fallback_model={job.fallback_model}) — issue 029 keeps the "
                f"issue-refinement job off Claude"
            )

    # Soft, exit-code-unchanged, same posture as the $ROUTINE_REPORT checks above: a
    # threshold this small is almost certainly a typo or a test, but rotation is
    # non-destructive (it renames, never deletes) so a bad value costs file churn, not data.
    if (
        config.retention.history_max_bytes is not None
        and config.retention.history_max_bytes < TINY_HISTORY_MAX_BYTES
    ):
        warnings.append(
            f"retention.history_max_bytes is {config.retention.history_max_bytes} bytes, "
            f"below 1 MiB — history.jsonl will roll on essentially every tick; "
            f"expect a new history-<timestamp>.jsonl per tick"
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
    # every enabled job being due at once. A gated job with checks runs gate checks plus
    # dispatches up to max_workers_per_tick fix workers sequentially, each bounded by
    # timeout_ms on top of the job's start timeout. For base-target: start + gate_slop +
    # sum(check.timeout_ms) + timeout_ms (worker re-runs checks inside timeout_ms). For
    # pr-target: start + gate_slop + sum(check.timeout_ms) + max_workers * timeout_ms
    # + max_workers * sum(check.timeout_ms) (each worker may re-run all checks).
    GATE_SLOP_S = 60  # covers worktree add/remove + pr_health polling
    total_job_seconds = 0.0
    for job in enabled_jobs:
        # kind: pipeline never runs in the tick process — it launches a detached
        # systemd-run unit and returns immediately (issue 026) — so it contributes
        # nothing to tick's own worst-case sequential runtime. A default `timeout_ms`
        # would otherwise silently inflate the required TimeoutStartSec by ~30 min.
        if job.kind == "pipeline":
            continue
        # kind: audit (issue 058) runs the audit, then at most one fix worker, in
        # sequence: slop + audit.timeout_ms + timeout_ms. A skill audit is an agent, so it
        # waits on start_timeout_ms too — one start per agent.
        if job.kind == "audit":
            audit = job.require_audit()
            agent_starts = 1 if audit.command is not None else 2
            total_job_seconds += (
                agent_starts * job.start_timeout_ms / 1000
                + GATE_SLOP_S
                + audit.timeout_ms / 1000
                + job.timeout_ms / 1000
            )
            continue
        # kind: review (issue 055) dispatches up to max_workers_per_tick read-only review
        # agents sequentially in one tick, each bounded by start + run + slop.
        if job.kind == "review":
            total_job_seconds += (
                GATE_SLOP_S
                + job.max_workers_per_tick
                * (job.start_timeout_ms + job.timeout_ms)
                / 1000
            )
            continue
        if job.kind == "gated":
            gate_time_s = sum(c.timeout_ms for c in job.require_checks()) / 1000
            if job.require_target() == "base":
                total_job_seconds += (
                    job.start_timeout_ms / 1000
                    + GATE_SLOP_S
                    + gate_time_s
                    + job.timeout_ms / 1000
                )
            else:
                total_job_seconds += (
                    job.start_timeout_ms / 1000
                    + GATE_SLOP_S
                    + gate_time_s
                    + job.max_workers_per_tick * job.timeout_ms / 1000
                    + job.max_workers_per_tick * gate_time_s
                )
        else:
            # Scale by (1 + retry_attempts) so the validate check accounts for
            # the worst-case tick time when a routine job retries transient failures
            # (issue 008, spec risk 2: retry loop holds the tick lock).
            total_job_seconds += (
                (job.start_timeout_ms + job.timeout_ms) * (1 + job.retry_attempts)
            ) / 1000
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

    # A resumed pipeline run (`--run-id`) keeps the launch-only relaunch path: its history
    # run_id already has a terminal record, so a new `running` one would never be reconciled.
    if job.kind == "pipeline" and (args.dry_run or args.run_id):
        return _cmd_run_pipeline(job, args, now=now)

    run_id = args.run_id or make_run_id(job.name, now)

    if args.dry_run:
        for command in build_dry_run_argv(job, run_id=run_id):
            print(" ".join(command))
        return 0

    # A manual run shares the `rt-<job>` agent name and the repo checkout with the
    # scheduled tick, so it takes the tick lock.
    with tick_lock(default_lock_path()) as acquired:
        if not acquired:
            log.error("%s: a tick is running; try again when it finishes", job.name)
            return 1
        log.info("%s: starting run %s", job.name, run_id)
        # Same dispatch as a scheduled tick (issue 074): the schedule decision is the only
        # thing skipped. Called through the module so tests can see the call.
        summary, failed = tick.dispatch_occurrence(
            job,
            default_history_path(),
            tick.Occurrence(run_id, HistoryExtra(trigger=TRIGGER_MANUAL)),
            client=HerdrClient(),
            now=now,
            trusted_review_authors=config.trusted_review_authors,
        )
    (log.error if failed else log.info)(summary)
    return 1 if failed else 0


def _cmd_spawn_from_telegram(args: argparse.Namespace) -> int:
    """Event entrypoint for the inbound-message bridge (issue 024).

    Loads the configured allowlist + alias map, hands one message to the pure spawn
    handler, and prints the reply to stdout for the caller (bot/poller) to send back.
    Never touches the tick lock or history — a cold spawn is an event, not a scheduled
    run — and the systemd timer stays the sole scheduler."""
    config = _load_config_or_exit(args)
    spawn = config.telegram_spawn
    if spawn is None:
        log.error("telegram_spawn is not configured; nothing to spawn")
        return 1
    handle_spawn_message(
        args.text,
        args.chat_id,
        allowed_chat_id=spawn.allowed_chat_id,
        aliases=alias_table(spawn),
        client=HerdrClient(),
        reply=_print_reply,
    )
    return 0


def _print_reply(text: str) -> None:
    """Print the in-chat reply the bridge sends back; a dropped message prints nothing."""
    if text:
        print(text)


def _cmd_run_pipeline(job: Job, args: argparse.Namespace, *, now: datetime) -> int:
    """`--run-id`, for kind: pipeline, is the orchestrator's bare UTC timestamp
    (design.md's same-RUN_ID relaunch path), not the full <job>-<ts> tick uses for its
    own history key. A resumed run gets an `-r<HHMMSS>` unit-name suffix so the
    transient `systemd-run --unit=` doesn't collide with a still-registered prior
    attempt (`--collect` in build_launch_argv only unregisters a *finished* unit)."""
    bare_run_id = args.run_id or now.strftime("%Y%m%dT%H%M%SZ")
    report_path = pipeline_report_path(bare_run_id)
    unit_name = (
        f"{PIPELINE_UNIT_PREFIX}{bare_run_id}"
        if not args.run_id
        else f"{PIPELINE_UNIT_PREFIX}{bare_run_id}-r{now:%H%M%S}"
    )
    argv = _build_pipeline_launch_argv(
        job,
        run_id=bare_run_id,
        report_path=report_path,
        unit_name=unit_name,
        deadline_epoch=pipeline_deadline_epoch(job, now),
    )

    if args.dry_run:
        print(" ".join(argv))
        return 0

    log.info("%s: dispatching pipeline run %s (%s)", job.name, bare_run_id, unit_name)
    rc, _out, err = launch_pipeline(argv)
    if rc != 0:
        log.error("%s: launch_failed: %s", job.name, err.strip())
        return 1
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    """Dispatch one stage gate.

    Each gate's requirements differ, and the CLI mirrors that instead of flattening it
    into one "everything is required" surface: `--pr` and the GitHub owner/repo are
    resolved only for the stages that read a PR (5, 6, 7, ci), so gates 1 and 2 — which
    inspect the checkout and the spec — are not made to fail by a checkout with no
    origin. A missing requirement is a usage error (2), not a gate failure (1): nothing
    was checked, so reporting "failed" would put a break in history for a typo.
    """
    stage = args.stage
    if stage in PR_GATE_STAGES:
        if args.pr is None:
            print(f"gate {stage}: --pr is required for this stage", file=sys.stderr)
            return EXIT_USAGE
        try:
            owner, name = remote_owner_and_repo(args.repo or Path.cwd())
        except RuntimeError as e:
            print(f"gate {stage}: could not resolve owner/repo: {e}", file=sys.stderr)
            return EXIT_FAILURE
    else:
        if args.spec is None:
            print(f"gate {stage}: --spec is required for this stage", file=sys.stderr)
            return EXIT_USAGE
        # Gates 1 and 2 never read owner/repo; they only need a checkout and a spec.
        owner = name = ""

    try:
        verdict = run_stage_gate(
            stage,
            repo_path=args.repo or Path.cwd(),
            gh=RealGhClient(),
            owner=owner,
            repo=name,
            spec=args.spec,
            pr=args.pr,
            branch=args.branch,
        )
    except ValueError as e:
        print(f"gate {stage}: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RuntimeError as e:
        print(f"gate {stage} failed: {e}", file=sys.stderr)
        log.error("gate %s: fail (PR #%s): %s", stage, args.pr, e)
        return EXIT_FAILURE

    if verdict.passed:
        log.info("gate %s: pass (PR #%s)", stage, args.pr)
        return EXIT_OK
    print(f"gate {stage} failed: {verdict.reason}", file=sys.stderr)
    log.error("gate %s: fail (PR #%s): %s", stage, args.pr, verdict.reason)
    return EXIT_FAILURE


def _cmd_gc(args: argparse.Namespace) -> int:
    # No HerdrClient, no config load — pure git + filesystem (spec.md §No Herdr server).
    repo = args.repo or Path.cwd()
    if args.delete:
        return run_gc_delete(
            repo,
            base=args.base,
            force=args.force,
            assume_yes=args.yes,
            older_than_days=args.older_than_days,
            worktrees_root=args.worktrees_root,
        )
    return run_gc(
        repo,
        base=args.base,
        older_than_days=args.older_than_days,
        worktrees_root=args.worktrees_root,
    )


def _cmd_prune(args: argparse.Namespace) -> int:
    """`prune reports` / `prune history` — the only deletion path in the state dir, and only
    ever reached from here. Mirrors `gc --delete`'s shape exactly: `--yes` (not a required
    argument) is what turns a listing into a deletion, and the refusal without it is the same
    message and the same rc 2, so there is no second "delete" vocabulary in this CLI to learn
    or audit.

    The mode group is deliberately *not* required, for the same reason `gc`'s is not: making
    argparse reject a missing flag would exit 2 with a usage error instead of the gc-style
    refusal, and it would skip the listing an operator needs in order to decide. Without
    `--yes` the sweep still runs, in dry-run mode, purely to print that listing — which is what
    makes "deletes nothing without --yes" a property of the code path rather than of the
    argument check.
    """
    if args.target == "reports":
        reports_dir = args.reports_dir or default_reports_dir()
        older_than = args.older_than_days
        if older_than is None:
            older_than = _configured_reports_window(args)
        if older_than < 0:
            print(
                f"error: --older-than must be a non-negative number of days: {older_than}",
                file=sys.stderr,
            )
            return 1
        result = prune_reports(
            reports_dir,
            older_than_days=older_than,
            dry_run=args.dry_run or not args.yes,
        )
        what = f"reports in {reports_dir}"
    else:
        history_path = default_history_path()
        older_than = (
            args.older_than_days
            if args.older_than_days is not None
            else reports_prune.DEFAULT_HISTORY_MAX_AGE_DAYS
        )
        if older_than < 0:
            print(
                f"error: --older-than must be a non-negative number of days: {older_than}",
                file=sys.stderr,
            )
            return 1
        result = prune_rolled_history(
            history_path,
            older_than_days=older_than,
            dry_run=args.dry_run or not args.yes,
        )
        what = f"rolled history beside {history_path}"

    _render_prune_result(result, would_be=not args.yes, what=what)
    for err in result.errors:
        print(f"warning: prune: {err}", file=sys.stderr)
    if result.error_count:
        return 1
    if not args.dry_run and not args.yes:
        print(
            "error: refusing to delete without --yes; --yes is required for prune",
            file=sys.stderr,
        )
        return 2
    return 0


def _render_prune_result(result: PruneResult, *, would_be: bool, what: str) -> None:
    """One listing, three verdicts: collectable, too fresh, protected. `gc` prints a table
    for the same reason — an operator about to delete has to be able to see exactly what
    they are about to lose, and which runs were exempted and why."""
    remove_label = "would remove" if would_be else "removed"
    rows = [[remove_label, str(p)] for p in result.removed]
    rows += [["keep (fresh)", str(p)] for p in result.kept_fresh]
    rows += [["protected (in-flight pipeline run)", str(p)] for p in result.protected]
    if rows:
        print(render_table(["ACTION", "PATH"], rows))
    else:
        print("nothing to prune")
    print(
        f"{what}: {remove_label} {result.removed_count}, "
        f"kept {result.kept_fresh_count} fresh, "
        f"protected {result.protected_count}, errors {result.error_count}"
    )


def _configured_reports_window(args: argparse.Namespace) -> int:
    """`--older-than` unset: take retention.reports_max_age_days from config, falling back to
    the built-in 90 days when there is no config to read. The fallback is what keeps `prune`
    usable on a host whose config is broken or absent — a destructive command should not
    refuse to run because an unrelated file failed to parse, and 90 days is the documented
    generous default anyway."""
    try:
        config = load_config(args.config or default_config_path())
    except (ConfigError, OSError) as e:
        log.warning(
            "prune: could not read retention.reports_max_age_days (%s); using the %d-day "
            "default",
            e,
            DEFAULT_REPORTS_MAX_AGE_DAYS,
        )
        return DEFAULT_REPORTS_MAX_AGE_DAYS
    if config.errors:
        log.warning(
            "prune: ignoring config errors while reading retention.reports_max_age_days: %s",
            "; ".join(config.errors),
        )
    return config.retention.reports_max_age_days


def _cmd_pick_feature(args: argparse.Namespace) -> int:
    # No HerdrClient unless a claim actually gets reclaimed below — same "no Herdr
    # server required" posture as gc (docs/process/README.md), just not absolute:
    # pipeline_watchdog.py already proves a bare `HerdrClient()` notification works
    # fine with no workspace/pane, from its own cron-driven sweep.
    def _notify_reclaim(reclaimed: ReclaimedPick) -> None:
        try:
            HerdrClient().notification_show(
                f"herdr-routines: issue {reclaimed.issue_id} reclaimed",
                body=(
                    f"stale claim from {reclaimed.claimed_at.isoformat()} "
                    "released — no open PR, no in-flight run"
                ),
                sound="request",
            )
        except HerdrCliError as e:
            log.warning(
                "pick-feature: reclaim notification failed for issue %s: %s",
                reclaimed.issue_id,
                e,
            )

    notify: Callable[[ReclaimedPick], None] | None = (
        _notify_reclaim if args.mark_in_progress else None
    )
    return run_pick_feature(
        args.issues_dir,
        mark=args.mark_in_progress,
        repo=args.repo,
        notify=notify,
    )


def _cmd_tmp_hygiene(args: argparse.Namespace) -> int:
    result = reap_tmp(
        tmp_dir=args.tmp_dir,
        max_age_s=args.max_age,
        dry_run=args.dry_run,
    )
    label = "would remove" if args.dry_run else "removed"
    log.info(
        "tmp-hygiene: %s %d file(s), skipped %d fresh, %d errors",
        label,
        result.removed,
        result.skipped_fresh,
        result.errors,
    )
    if args.dry_run:
        print(
            f"{label}: {result.removed}, fresh (skipped): {result.skipped_fresh}, errors: {result.errors}"
        )
    else:
        print(
            f"removed: {result.removed}, fresh (skipped): {result.skipped_fresh}, errors: {result.errors}"
        )
    return 0


def _cmd_pipeline_prepare(args: argparse.Namespace) -> int:
    # The launcher branches on these exit codes and nothing else: 0 prepared, 3 no
    # feature (a healthy night with an empty backlog — distinct from 1 on purpose, so
    # the launcher can skip the agent quietly instead of reporting a break).
    result = prepare_run(
        run_id=args.run_id,
        repo_parent=args.repo_parent,
        report=args.report,
        deadline_epoch=args.deadline_epoch,
        base=args.base,
        issues_dir=args.issues_dir,
    )
    if result.outcome == "ok":
        # stdout is the machine-parsed KEY=VALUE stream; logs went to stderr, which the
        # launcher has already redirected into the run log.
        print(render_prepared_values(result))
        return EXIT_PREPARED
    if result.outcome == "no_feature":
        log.info(
            "pipeline-prepare: no feature to build for run %s; wrote %s — no agent, no "
            "worktree, no workspace, no state.json",
            args.run_id,
            args.report,
        )
        return EXIT_NO_FEATURE
    log.error(
        "pipeline-prepare: run %s did not prepare (%s); wrote %s — no agent started",
        args.run_id,
        result.outcome,
        args.report,
    )
    return EXIT_FAILURE


def _configured_trusted_review_authors(args: argparse.Namespace) -> tuple[str, ...]:
    """`trusted_review_authors` from config, or ``()`` — only the authenticated gh user
    — when the config cannot be read. `pipeline-run` runs in its own systemd unit and
    must still write its report on a broken config, and the empty list is the narrowest
    set there is: the built-in default would widen trust for an operator who configured
    a list without it."""
    path = args.config or default_config_path()
    try:
        return load_config(path).trusted_review_authors
    except (ConfigError, OSError) as e:
        log.warning(
            "pipeline-run: could not read trusted_review_authors from %s (%s); "
            "trusting only the authenticated gh user",
            path,
            e,
        )
        return ()


def _cmd_pipeline_run(args: argparse.Namespace) -> int:
    """Mirrors `pipeline-prepare`'s exit-code contract: 0 ok, 1 partial/failed, 2 usage.

    Two non-zero codes for one command is deliberate and is the contract the launcher
    branches on: a healthy night that built nothing is `prepare`'s 3 and never reaches
    here, so anything non-zero out of `pipeline-run` is a real break for tick to file.
    `run_pipeline` never raises, so this cannot return 1 by accident from an escaped
    exception — the report is written first, every path."""
    result = run_pipeline(
        run_id=args.run_id,
        state_json=args.state_json,
        report=args.report,
        prompts_dir=args.prompts_dir,
        failure_markers=tuple(args.failure_marker or DEFAULT_FAILURE_MARKERS),
        trusted_review_authors=_configured_trusted_review_authors(args),
    )
    if result.outcome == "ok":
        log.info(
            "pipeline-run: run %s finished ok; report at %s", args.run_id, args.report
        )
        return EXIT_OK
    log.error(
        "pipeline-run: run %s finished %s (%s); report at %s",
        args.run_id,
        result.outcome,
        result.reason or "no reason recorded",
        args.report,
    )
    return EXIT_PARTIAL_OR_FAILED


def _cmd_refine_issue(args: argparse.Namespace) -> int:
    if args.check_issue is not None:
        return run_check_issue(args.check_issue)
    if args.review_pass is not None:
        if args.reviewer_reply is None:
            print(
                "refine-issue: --review-pass requires --reviewer-reply",
                file=sys.stderr,
            )
            return EXIT_USAGE
        return run_loop_check(args.review_pass, args.reviewer_reply)
    if args.reviewer_reply is not None:
        print("refine-issue: --reviewer-reply requires --review-pass", file=sys.stderr)
        return EXIT_USAGE
    # Pure filesystem + one `gh pr list` — no HerdrClient, same posture as
    # pick-feature: this runs as the job's first step, before any reviewer session.
    return run_refine_issue(
        args.repo,
        issues_dir=args.issues_dir,
        roadmap_path=args.roadmap,
        allow_blocked=args.allow_blocked,
    )


# One fixed sentence for both output shapes: `review-scan` answers "would the selector
# pick this PR, and if not, why" — never "will the next tick run it", which also needs
# the cron decision and the live-agent guard (issue 066's explicit non-goals).
_REVIEW_SCAN_NOTE = (
    "note: this reports scan eligibility only — it does not evaluate the cron "
    'schedule or the live-agent guard, so "would dispatch" does not mean the next '
    "tick will run it."
)


def _cmd_review_scan(args: argparse.Namespace) -> int:
    """`herdr-routines review-scan <job>` (issue 066): run the review selector's own
    scan → attempt budget → marker guard → cap pipeline through the shared
    `classify_review_candidates` helper and report one disposition per PR, without
    dispatching anything.

    Read-only by construction — no history append, no `reports/` file, no clone, no
    worktree, no agent, no notification: the tick stays the only writer, and the CLI
    is its inspect-only mirror of the same selector. Exit codes follow the CLI's own
    vocabulary: 2 when the request itself is wrong (not a `kind: review` job, a
    `--limit` GitHub could never honour), 1 when the answer is unknowable (missing
    job, unusable gh auth, failed search — never a silent empty list), 0 on a
    successful scan however many PRs it found."""
    if not 1 <= args.limit <= 100:
        # GitHub's search ceiling is 100 nodes; a wider --limit can never be honoured,
        # so it is a usage error, rejected before any gh call runs.
        print(
            f"error: review-scan: --limit must be between 1 and 100 (got {args.limit})",
            file=sys.stderr,
        )
        return EXIT_USAGE

    config = _load_config_or_exit(args)
    job = config.job(args.job)
    if job is None:
        print(f"error: review-scan: no such job: {args.job}", file=sys.stderr)
        return EXIT_FAILURE
    if job.kind != "review":
        print(
            f"error: review-scan: {job.name} has kind {job.kind!r}, not 'review'",
            file=sys.stderr,
        )
        return EXIT_USAGE

    gh = RealGhClient()
    try:
        login = gh.api_user()
    except Exception as e:  # noqa: BLE001
        # Same fail-closed posture as `_review_cycle`: an unauthenticated gh is never
        # readable as "there is nothing to review".
        print(f"error: review-scan: gh_auth_missing: {e}", file=sys.stderr)
        return EXIT_FAILURE

    classification = classify_review_candidates(
        gh,
        job,
        login=login,
        history_path=default_history_path(),
        trusted_authors=effective_trusted_authors(
            _configured_trusted_review_authors(args), viewer_login=login
        ),
        limit=args.limit,
    )
    if classification is None:
        print("error: review-scan: search_failed", file=sys.stderr)
        return EXIT_FAILURE

    header = _review_scan_header(job, login=login, classification=classification)
    if args.json:
        # stdout carries the JSON document and nothing else, so the framing goes
        # beside it on stderr and `| jq` stays clean.
        print(header, file=sys.stderr)
        print(_REVIEW_SCAN_NOTE, file=sys.stderr)
        print(json.dumps(_review_scan_json_rows(classification)))
    else:
        print(header)
        print(_REVIEW_SCAN_NOTE)
        for cand, disposition in _review_scan_entries(classification):
            print(
                f"{cand.target_key}  select_mode={cand.select_mode}  "
                f"disposition={disposition}"
            )
    return EXIT_OK


def _review_scan_entries(
    classification: Classification,
) -> list[tuple[Candidate, str]]:
    """Every PR the scan enumerated, sorted by ``target_key`` for a stable listing,
    each with exactly one disposition: ``would_dispatch`` for the candidates that
    passed every guard and the cap, else the row's own skip reason."""
    return sorted(
        [
            *classification.rows,
            *[(cand, "would_dispatch") for cand in classification.dispatchable],
        ],
        key=lambda row: row[0].target_key,
    )


def _review_scan_header(job: Job, *, login: str, classification: Classification) -> str:
    lines = [
        f"review-scan: job={job.name}",
        f"select: {', '.join(job.select or ())}",
        f"gh login: {login}",
        f"enumerated: {classification.enumerated}",
    ]
    if classification.truncated_modes:
        # A truncated search means candidates may be missing from this very listing,
        # so it belongs in the header, not a footnote.
        lines.append(f"search truncated: {', '.join(classification.truncated_modes)}")
    return "\n".join(lines)


def _review_scan_json_rows(classification: Classification) -> list[dict[str, object]]:
    return [
        {
            "target_key": cand.target_key,
            "repo": cand.repo,
            "number": cand.number,
            "title": cand.title,
            "url": cand.url,
            "select_mode": cand.select_mode,
            "disposition": disposition,
        }
        for cand, disposition in _review_scan_entries(classification)
    ]


def _cmd_pipeline_watchdog(args: argparse.Namespace) -> int:
    # Cron-driven maintenance sweep (systemd timer, not tick.py — see pipeline_watchdog's
    # module docstring): log the summary, don't print a human table like gc/ps do.
    client = HerdrClient()
    actions = run_watchdog(
        client=client,
        worktrees_root=default_worktrees_root(),
        reports_dir=default_reports_dir(),
        heartbeat_dir=default_heartbeat_dir(),
        history_path=default_history_path(),
    )
    for action in actions:
        if action.killed_agents:
            log.warning(
                "pipeline-watchdog: %s stalled, killed %s (stage %s), report at %s",
                action.run_id,
                ", ".join(action.killed_agents),
                action.stage_killed,
                action.report_path,
            )
        else:
            log.warning(
                "pipeline-watchdog: %s stalled, no live worker found, report at %s",
                action.run_id,
                action.report_path,
            )
    if not actions:
        log.info("pipeline-watchdog: no stalled runs")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    config = _load_config_or_exit(args)
    text = digest_now(
        config,
        default_history_path(),
        default_reports_dir(),
        timezone=args.timezone,
        include_disabled=args.include_disabled,
        drift=check_host(args.path),
    )
    print(text)
    if args.notify:
        try:
            HerdrClient().notification_show("herdr-routines: daily digest", body=text)
        except HerdrCliError as e:
            log.warning("digest: notification failed: %s", e)
    return 0


def _cmd_host_check(args: argparse.Namespace) -> int:
    """`herdr-routines host-check`: gate-style — findings on stdout, exit 1 on any
    drift, 0 when clean. Report only; the fix for every finding is the install steps
    in deploy/README.md."""
    findings = check_host(args.path, base=args.base, unit_dir=args.unit_dir)
    if not findings:
        print(f"ok: no host drift ({args.path})")
        return EXIT_OK
    print(f"host drift ({args.path}):")
    for line in format_findings(findings):
        print(line)
    return EXIT_FAILURE


def _self_update_notify(title: str, body: str | None = None) -> None:
    """The notification seam for self-update. `HerdrCliError` (no server, no
    workspace, `herdr` binary gone) is logged and swallowed: a failed *notification*
    must not turn a successful update into a failed command — the outcome is already
    in the log line the handler prints and in the state file, which is what the next
    run reads. Same posture as `_cmd_digest`."""
    try:
        HerdrClient().notification_show(title, body=body)
    except HerdrCliError as e:
        log.warning("self-update: notification failed: %s", e)


def _cmd_self_update(args: argparse.Namespace) -> int:
    """`herdr-routines self-update` (issue 053).

    Deliberately not `_load_config_or_exit`: that turns a ConfigError into a bare
    SystemExit(1) with no notification and no mention of the path, and "this host's
    config no longer loads" is precisely the thing this command must say out loud —
    it is the first thing a human needs to know when the runner stops updating. Same
    for `config.errors`, the per-file soft errors from jobs.d/: running happily on a
    half-broken config is what the runbook's "Config migrations" section warns about.

    A refusal returns 1; a deferral returns 0, because "the tick lock was held" is a
    correct outcome and a non-zero exit would make the timer's failure visible.
    """
    path = args.config or default_config_path()
    try:
        config = load_config(path)
    except ConfigError as e:
        log.error("self-update: config %s failed to load: %s", path, e)
        _self_update_notify(
            f"herdr-routines: self-update refused ({args.path})",
            body=f"config failed to load: {path}\n{e}",
        )
        return 1
    if config.errors:
        for err in config.errors:
            log.error("self-update: config: %s", err)
        _self_update_notify(
            f"herdr-routines: self-update refused ({args.path})",
            body="config has errors:\n" + "\n".join(config.errors),
        )
        return 1

    result = run_self_update(
        checkout=args.path,
        base=args.base,
        dry_run=args.dry_run,
        config=config,
        gh=RealGhClient(),
        notify=_self_update_notify,
        lock_path=default_lock_path(),
        history_path=default_history_path(),
        state_path=default_state_path(),
        host_check=lambda: check_host(args.path, base=args.base),
    )
    # up_to_date and deferred are quiet by design (the handler logs them at info /
    # warning inside the module, and notifies itself for the one loud deferral, a
    # tick lock held past TICK_LOCK_WAIT_S); nothing here adds a second notification.
    log.info("self-update: %s (%s)", result.status, result.reason or "no reason")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
