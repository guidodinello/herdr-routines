"""Age-based pruning of the state dir: reports, and rolled history files (issue 021).

Two halves, deliberately asymmetric:

* `maybe_rotate` (history.py) is **non-destructive** — it renames, never deletes — so it
  can run automatically inside the tick. Rotation alone bounds nothing, though: the rolls
  accumulate too, so they need a sweep of their own.
* everything here **deletes**, so it is only reachable from the explicit
  `herdr-routines prune` command, gated on `--yes` exactly like `gc --delete`. Nothing in
  this module runs from a tick or a scheduled job.

Selection follows `tmp_hygiene.reap_tmp`'s proven shape: mtime-based, top level only (no
recursion), per-entry `try/except OSError` so one unreadable entry never aborts the sweep,
symlinks skipped, and a `PruneResult` counter returned for the caller to render.

Not `gc.py` (pure git, 800+ lines of branch collection) and not `tmp_hygiene.py` (/tmp
leaks only): this is state-dir retention with a different safety predicate — an in-flight
pipeline run's files are protected regardless of age.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from logger import get_logger

from herdr_routines.history import rolled_history_paths
from herdr_routines.ps import default_scan_dirs, scan_pipeline_runs

log = get_logger(__name__)

# `prune history` is opt-in and off by default, so its window is deliberately generous: it
# only ever runs when a human asked for it by name. See the caveat in `prune_rolled_history`
# before narrowing this.
DEFAULT_HISTORY_MAX_AGE_DAYS = 365

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What one sweep did (or, in dry-run mode, would do). Paths rather than bare counts so
    the caller can render the same listing `gc` prints; the `*_count` properties keep the
    one-line summary cheap to write."""

    removed: tuple[Path, ...] = ()
    kept_fresh: tuple[Path, ...] = ()
    protected: tuple[Path, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def kept_fresh_count(self) -> int:
        return len(self.kept_fresh)

    @property
    def protected_count(self) -> int:
        return len(self.protected)

    @property
    def error_count(self) -> int:
        return len(self.errors)


def protected_paths(reports_dirs: list[Path] | None = None) -> set[Path]:
    """Every path in the state dir that belongs to an **in-flight** pipeline run and must
    therefore survive a prune regardless of age.

    The predicate is reused, not reinvented: `ps.scan_pipeline_runs` finds each run's
    `state.json` and `PipelineRun.in_progress` is exactly "its final report has not been
    written yet". Pruning such a run's `state.json` would make `ps` drop the row entirely
    and let `pick_feature` re-pick the same issue; pruning a report a run still needs
    would make `pipeline_watchdog._has_terminal_report` disagree with reality the same way.
    So: protect the `state.json`, the report path `ps` resolved, and both documented report
    naming conventions (`pipeline-<run_id>.md` from design.md, `<run_id>.md` from
    orchestrator-prompt.md) beside the state file and beside its parent — the same two
    locations `ps._resolve_report_path` and `pipeline_watchdog._candidate_report_paths`
    check. Paths that don't exist are harmless; the sweep only consults them for entries
    it actually found.

    A *finished* run's report is deliberately not protected: keeping those is what would
    make the reports dir grow without bound, which is the whole point of the command.
    """
    runs = scan_pipeline_runs(
        default_scan_dirs() if reports_dirs is None else reports_dirs
    )
    protected: set[Path] = set()
    for run in runs.values():
        if not run.in_progress:
            continue
        protected.add(run.state_path)
        protected.add(run.report_path)
        for base in (run.state_path.parent, run.state_path.parent.parent):
            protected.add(base / f"pipeline-{run.run_id}.md")
            protected.add(base / f"{run.run_id}.md")
    return protected


def prune_reports(
    reports_dir: Path,
    *,
    older_than_days: int,
    dry_run: bool = False,
    protected: set[Path] | None = None,
) -> PruneResult:
    """Delete entries in *reports_dir* whose mtime is older than *older_than_days*.

    Covers the whole flat set the state dir accumulates: `{run_id}.md` per run,
    `{run_id}.tail.txt` per failure, `auto-fix-*.md` gate/worker reports, and `state.json`
    per pipeline run. Top level only — nothing here recurses, so a subdirectory is skipped
    rather than descended into (the reports dir is flat by construction; a stray directory
    is left for a human to look at).

    A missing reports dir is not an error: a host that has never run anything has nothing
    to prune, and `prune` must stay usable there.
    """
    if not reports_dir.is_dir():
        return PruneResult()
    if protected is None:
        # The same two locations `ps` scans, so a state.json dropped either in the reports
        # dir or beside history.jsonl still protects its run.
        protected = protected_paths([reports_dir, reports_dir.parent])
    return _sweep(
        sorted(reports_dir.iterdir()),
        older_than_days=older_than_days,
        dry_run=dry_run,
        protected=protected,
    )


def prune_rolled_history(
    history_path: Path, *, older_than_days: int, dry_run: bool = False
) -> PruneResult:
    """Delete rolled `history-*.jsonl` files older than *older_than_days* (mtime-based).
    The live `history.jsonl` is never a candidate.

    Opt-in and off by default, and that is a safety decision, not an oversight: deleting
    the *earliest* roll can make `first_seen_at` return None for a long-lived job, which
    re-registers it and shifts the `job_registered_at` fed into `schedule.decide`. If this
    is ever wired into a timer, exclude the most recent roll unconditionally first.
    """
    return _sweep(
        rolled_history_paths(history_path),
        older_than_days=older_than_days,
        dry_run=dry_run,
        protected=set(),
    )


def _sweep(
    entries: list[Path],
    *,
    older_than_days: int,
    dry_run: bool,
    protected: set[Path],
) -> PruneResult:
    cutoff = time.time() - older_than_days * SECONDS_PER_DAY
    removed: list[Path] = []
    kept_fresh: list[Path] = []
    is_protected: list[Path] = []
    errors: list[str] = []

    for entry in entries:
        # One guard for the whole per-entry body, not just the unlink: `is_file()` and
        # `stat()` raise OSError too, and an entry that cannot be inspected is exactly the
        # one that must not take the rest of the sweep down with it.
        try:
            if entry.is_symlink():
                # Never follow or delete a symlink: the target may be outside the state dir
                # entirely (tmp_hygiene's rule, for the same reason).
                continue
            if not entry.is_file():
                continue
            if entry in protected:
                is_protected.append(entry)
                continue
            if entry.stat().st_mtime > cutoff:
                kept_fresh.append(entry)
                continue
            if dry_run:
                removed.append(entry)
                continue
            entry.unlink()
        except OSError as e:
            errors.append(f"{entry}: {e}")
            log.warning("prune: leaving %s in place: %s", entry, e)
            continue
        removed.append(entry)

    return PruneResult(
        removed=tuple(removed),
        kept_fresh=tuple(kept_fresh),
        protected=tuple(is_protected),
        errors=tuple(errors),
    )
