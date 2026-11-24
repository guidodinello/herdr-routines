"""Age-based /tmp cleanup for leaked agent-runtime .so files, pytest artifacts, and
opencode directories. See docs/process/issues/027-tmp-hygiene.md.

Pure filesystem — no herdr/subprocess except the optional diagnosis helpers in runner.py.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Default max age in seconds (1 hour). Protects an in-flight agent whose .so was
# written this run while still reaping hourly ambient leaks (~5 MB/spawn × ~24
# spawns/day ≈ 120 MB/day).
DEFAULT_MAX_AGE_S = 3600

# Patterns anchored to tmp_dir top level only. Each entry is a (glob, is_dir) pair.
# is_dir controls whether we use rmtree (True) or unlink (False).
# Files matching .3cdc* but not .3cdc*.so are still deleted (sibling native-runtime
# leaks); is_file guard prevents deleting directories that happen to match.
_LEAK_GLOBS: list[tuple[str, bool]] = [
    (".3cdc*", False),  # .3cdc*.so and siblings — files only (is_file guard)
    ("pytest-of-*", True),  # pytest temp dirs — recursive removal
    ("opencode-*", True),  # opencode sibling dirs
]

# Shared directories whose direct children are reaped one by one; the directory itself
# stays. opencode keeps unrelated work in /tmp/opencode side by side (agents clone one
# PR checkout per review into it, ~3,500 entries each), so judging the whole directory
# as one tree let one live child keep every stale sibling, and three checkouts push it
# past _MAX_WALK_ENTRIES, which keeps it for good. The Pi's /tmp reached 95% on
# 2026-11-22 with stale checkouts there while the reap logged one error a tick.
_LEAK_CONTAINERS: tuple[str, ...] = ("opencode",)


# Upper bound on entries inspected per directory tree. A tree this large that we cannot
# fully inspect is kept, never reaped on a partial look.
_MAX_WALK_ENTRIES = 10_000


class _WalkIncomplete(Exception):
    """The tree could not be fully inspected (error or entry bound hit)."""


def _is_fresh(entry: Path, cutoff: float) -> bool:
    """True if *entry* or anything under it (symlinks not followed) was modified after
    *cutoff*. A directory's own mtime only changes when its direct children do, so a
    live writer deep in the tree leaves the top level looking old. Raises
    `_WalkIncomplete` when the tree can't be fully inspected."""
    if entry.lstat().st_mtime > cutoff:
        return True
    if not entry.is_dir():
        return False
    seen = 0

    def on_error(exc: OSError) -> None:
        raise _WalkIncomplete from exc

    for root, dirs, files in os.walk(entry, followlinks=False, onerror=on_error):
        for name in (*dirs, *files):
            seen += 1
            if seen > _MAX_WALK_ENTRIES:
                raise _WalkIncomplete
            try:
                if os.lstat(os.path.join(root, name)).st_mtime > cutoff:
                    return True
            except OSError as exc:
                raise _WalkIncomplete from exc
    return False


@dataclass(frozen=True, slots=True)
class ReapResult:
    removed: int = 0
    skipped_fresh: int = 0
    errors: int = 0


def reap_tmp(
    *,
    tmp_dir: Path = Path("/tmp"),
    max_age_s: int = DEFAULT_MAX_AGE_S,
    dry_run: bool = False,
) -> ReapResult:
    """Remove leaked files/dirs from *tmp_dir* that match known patterns and are older
    than *max_age_s* seconds. A directory's age is its newest mtime anywhere in its tree
    (bounded walk), not its own mtime: agent runs write into these dirs while the reap
    runs (pipeline runs hold no lock, and since issue 079 every other dispatch holds only
    its job lock, never the `tick.lock` the reap takes), and nested writes don't touch the
    top level. A tree that can't be fully inspected is kept and counted as an error.

    Only matches files/dirs at the top level of *tmp_dir*, plus the direct children of
    each `_LEAK_CONTAINERS` directory. Follows no symlinks.
    Per-entry try/except ensures a single failure never aborts the sweep.
    """
    now = time.time()
    cutoff = now - max_age_s
    removed = 0
    skipped_fresh = 0
    errors = 0

    for entry in _candidates(tmp_dir):
        # Symlink safety: never follow or delete symlinks. Checked before any stat
        # so a dangling or retargeted link is never judged by its target.
        if entry.is_symlink():
            continue

        try:
            fresh = _is_fresh(entry, cutoff)
        except (OSError, _WalkIncomplete):
            errors += 1
            continue

        if fresh:
            skipped_fresh += 1
            continue

        if dry_run:
            removed += 1
            continue

        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            errors += 1

    return ReapResult(removed=removed, skipped_fresh=skipped_fresh, errors=errors)


def _candidates(tmp_dir: Path) -> list[Path]:
    """Every entry the reap judges: top-level glob matches, then the direct children of
    each container. A container that is a symlink or not a directory contributes
    nothing."""
    found: list[Path] = []
    for pattern, _is_dir in _LEAK_GLOBS:
        # Top-level guard: glob should only match direct children, but belt-and-suspenders.
        found.extend(e for e in tmp_dir.glob(pattern) if e.parent == tmp_dir)
    for name in _LEAK_CONTAINERS:
        container = tmp_dir / name
        if container.is_symlink() or not container.is_dir():
            continue
        try:
            found.extend(container.iterdir())
        except OSError:
            log.warning("tmp hygiene: cannot list %s", container)
    return found
