"""Repository lifecycle helpers for ``repository: <url>`` jobs.

Pure-subprocess: no Herdr dependency, no config writes.  ``ensure_repo`` is called at the top
of ``runner.execute_run`` and gated-dispatch paths in ``tick.py`` so every worktree/tab
creation starts from a known-good checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from logger import get_logger

from herdr_routines.config import Job

log = get_logger(__name__)

REPO_TIMEOUT_S = 120


def default_repos_dir() -> Path:
    """Resolve the managed repos base dir, following the same
    ``HERDR_PLUGIN_STATE_DIR`` pattern as ``history.default_history_path`` and
    ``runner.default_reports_dir``."""
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "repos"


def ensure_repo(job: Job, *, repos_dir: Path | None = None) -> Path:
    """Ensure the job's checkout exists and is up-to-date.

    For ``repository:`` jobs:
      - clone-if-missing (atomic tmp+rename)
      - fetch + fast-forward on every subsequent run

    For plain ``repo:`` jobs: returns ``job.repo`` unchanged.

    ``repos_dir`` is accepted for callers that pre-resolve the managed repos
    base dir (e.g. via ``default_repos_dir``); ``job.repo`` is already the
    resolved checkout path, so it is not otherwise used here.

    Returns the checkout path on success.  Raises ``RuntimeError`` on
    clone/sync failure so callers can map it to a terminal ``RunOutcome``.
    """
    if job.repository is None:
        return job.repo

    checkout = job.repo
    if not (checkout / ".git").exists():
        _clone(job.repository, checkout)
    else:
        _fetch_and_fast_forward(checkout, base=job.base)

    return checkout


def _clone(url: str, dest: Path) -> None:
    """Clone *url* into *dest* atomically: clone to a tmp dir then rename.
    On failure the tmp dir is removed so the next tick retries a full clone."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=dest.parent, suffix=".tmp"))
    try:
        proc = subprocess.run(
            ["git", "clone", url, str(tmp_dir / "checkout")],
            capture_output=True,
            text=True,
            timeout=REPO_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.strip() or f"git clone failed (rc={proc.returncode})"
            )
        # Atomic rename into place
        os.replace(str(tmp_dir / "checkout"), str(dest))
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _fetch_and_fast_forward(checkout: Path, *, base: str = "main") -> None:
    """Fetch and fast-forward to ``origin/<base>``.  For detached HEAD (worktree
    base-target gate), fetch alone is enough."""
    # Fetch
    proc = subprocess.run(
        ["git", "-C", str(checkout), "fetch", "--prune", "origin"],
        capture_output=True,
        text=True,
        timeout=REPO_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git fetch failed")

    # Check if detached — fetch alone is sufficient for detached HEAD
    head_proc = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=REPO_TIMEOUT_S,
        check=False,
    )
    if head_proc.stdout.strip() == "HEAD":
        return

    # Fast-forward to origin/<base>
    proc = subprocess.run(
        ["git", "-C", str(checkout), "merge", "--ff-only", f"origin/{base}"],
        capture_output=True,
        text=True,
        timeout=REPO_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"non-fast-forward merge on origin/{base}"
        )
