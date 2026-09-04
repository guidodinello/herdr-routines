"""Age-based /tmp cleanup for leaked agent-runtime .so files, pytest artifacts, and
opencode directories. See docs/process/issues/027-tmp-hygiene.md.

Pure filesystem — no herdr/subprocess except the optional diagnosis helpers in runner.py.
"""

from __future__ import annotations

import logging
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
    ("opencode", True),  # opencode harness dir — recursive removal
    ("opencode-*", True),  # opencode sibling dirs
]


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
    than *max_age_s* seconds (mtime-based). Safe for concurrent use under tick.lock.

    Only touches files/dirs at the top level of *tmp_dir* (no recursion beyond one
    depth for pytest-of-*). Follows no symlinks. Per-entry try/except ensures a
    single failure never aborts the sweep.
    """
    now = time.time()
    cutoff = now - max_age_s
    removed = 0
    skipped_fresh = 0
    errors = 0

    for pattern, is_dir in _LEAK_GLOBS:
        for entry in tmp_dir.glob(pattern):
            # Top-level guard: glob should only match direct children, but belt-and-suspenders.
            if entry.parent != tmp_dir:
                continue
            try:
                st = entry.stat()
            except OSError:
                errors += 1
                continue

            # Symlink safety: never follow or delete symlinks.
            if entry.is_symlink():
                continue

            if st.st_mtime > cutoff:
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
