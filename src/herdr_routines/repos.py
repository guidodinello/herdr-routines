"""Repository lifecycle helpers, shared by ``repository: <url>``-managed jobs and plain
``repo: <path>`` jobs.

Pure-subprocess: no Herdr dependency, no config writes.  ``ensure_repo`` is called at the top
of ``runner.execute_run`` and gated-dispatch paths in ``tick.py`` so every worktree/tab
creation starts from a known-good checkout — fetched+fast-forwarded to ``origin/<base>``
regardless of whether the job's checkout is managed or a pre-existing local clone.
``pipeline_prepare`` calls ``_fetch_and_fast_forward`` directly for the overnight
pipeline's ``$REPO_PARENT``, which is outside the job-dispatch path.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from logger import get_logger

from herdr_routines.config import Job

log = get_logger(__name__)

REPO_TIMEOUT_S = 120

# How long `_ensure_checkout` waits for another dispatch's brief sync of the same checkout
# (issue 079). One sync is a fetch, a rev-parse and a merge, each bounded by REPO_TIMEOUT_S,
# so this covers an ordinary holder; a `workspace: root` run holding the checkout for its
# whole dispatch outlasts it, and the sync fails as `CheckoutBusy` instead of mutating the
# tree under that run.
CHECKOUT_LOCK_WAIT_S = 2 * REPO_TIMEOUT_S
CHECKOUT_LOCK_POLL_S = 0.5


class CheckoutBusy(RuntimeError):
    """Another dispatch holds this checkout's lock (issue 079). A `RuntimeError`, so every
    caller that maps a clone/sync failure to a terminal outcome still handles it."""


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


def checkout_lock_path(checkout: Path) -> Path:
    """The per-checkout lock (issue 079), in the state dir rather than the checkout.

    Keyed by the resolved path, so a managed clone and a `repo:` path naming the same
    directory share one lock. It lives outside the tree on purpose: a lock file inside
    the checkout would be an untracked file there, which makes `git status` dirty (the
    self-update and dirty-worktree checks read it) and is one `git add -A` away from
    being committed by a `workspace: root` agent."""
    resolved = checkout.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
    return (
        default_repos_dir().parent / "locks" / f"checkout-{resolved.name}-{digest}.lock"
    )


# Locks this thread already holds, so a `workspace: root` dispatch (which holds its
# checkout for the whole run) can still call `ensure_repo` on it. flock conflicts between
# two open() calls even inside one process, so without this the nested sync would wait on
# itself. Per thread, not per process: tests serialize threads on the same lock.
_held_checkouts = threading.local()


def _held() -> dict[Path, int]:
    held: dict[Path, int] | None = getattr(_held_checkouts, "paths", None)
    if held is None:
        held = {}
        _held_checkouts.paths = held
    return held


@contextmanager
def checkout_lock(checkout: Path, *, wait_s: float = 0.0) -> Generator[bool]:
    """Exclusive flock serializing git mutations of one checkout across jobs (issue 079).

    Yields True holding the lock, or False when another holder kept it for *wait_s*
    seconds (0 = one non-blocking try). Re-entrant within a thread. Lock ordering: taken
    inside a job's dispatch lock and outside `history.lock`."""
    path = checkout_lock_path(checkout)
    held = _held()
    if path in held:
        held[path] += 1
        try:
            yield True
        finally:
            held[path] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(CHECKOUT_LOCK_POLL_S)
        held[path] = 1
        try:
            yield True
        finally:
            del held[path]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_repo_less(job: Job) -> bool:
    """Whether `job.repo` is only the managed base dir rather than a checkout (issue 076):
    what `kind: review` loads with, since each PR it picks is checked out in its own repo."""
    return job.repo == default_repos_dir()


def ensure_repo(job: Job) -> Path:
    """Ensure the job's checkout exists and is up-to-date.

    For ``repository:`` jobs:
      - clone-if-missing (atomic tmp+rename)
      - fetch + fast-forward on every subsequent run

    For plain ``repo:`` jobs (a pre-existing local clone path — the checkout
    must already exist, it is never cloned): fetch + fast-forward on every
    run, same as ``repository:`` jobs.

    ``job.repo`` is already the resolved checkout path (``config.py`` resolves
    it against ``default_repos_dir()`` for ``repository:`` jobs at load time),
    so no separate ``repos_dir`` parameter is needed here.

    Returns the checkout path on success.  Raises ``RuntimeError`` on
    clone/sync failure so callers can map it to a terminal ``RunOutcome``.

    This is the ``job``-shaped wrapper around :func:`_ensure_checkout`; a
    clone URL of ``None`` (a plain ``repo:`` path) can never trigger a clone.
    """
    return _ensure_checkout(job.repository, job.repo, base=job.base)


def _ensure_checkout(clone_url: str | None, dest: Path, *, base: str) -> Path:
    """Ensure *dest* exists and is up-to-date, cloning from *clone_url* when it
    is missing and the URL is known.

    The per-target checkout of a multi-target routine (``pick: my_open_prs``, so
    ``kind: review``) calls this directly: its clone URL and default branch are
    discovered per-repo at tick time, so there is no ``Job`` to hand it.  ``ensure_repo`` is the ``Job``-shaped
    caller and preserves the pre-refactor behavior exactly — a plain ``repo:``
    job (``clone_url is None``) is still never cloned, only synced.

    Returns *dest*.  Raises ``RuntimeError`` on clone/sync failure so callers
    can map it to a terminal outcome, and its ``CheckoutBusy`` subclass when another
    dispatch kept the checkout locked for ``CHECKOUT_LOCK_WAIT_S`` (issue 079): two jobs
    sharing a checkout must not fetch, fast-forward or clone it at the same time.
    """
    with checkout_lock(dest, wait_s=CHECKOUT_LOCK_WAIT_S) as acquired:
        if not acquired:
            raise CheckoutBusy(
                f"checkout {dest} is locked by another dispatch "
                f"(waited {CHECKOUT_LOCK_WAIT_S}s)"
            )
        if clone_url is not None and not (dest / ".git").exists():
            _clone(clone_url, dest)
        else:
            _fetch_and_fast_forward(dest, base=base)

    return dest


def _run_git(args: list[str], *, what: str) -> subprocess.CompletedProcess[str]:
    """Run git with ``REPO_TIMEOUT_S``.  A timeout surfaces as ``RuntimeError`` — the
    clone/sync failure every caller already guards — not ``TimeoutExpired``, which is a
    ``SubprocessError`` and would escape their ``except (RuntimeError, OSError)``."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=REPO_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git {what} timed out after {REPO_TIMEOUT_S}s") from e


def _clone(url: str, dest: Path) -> None:
    """Clone *url* into *dest* atomically: clone to a tmp dir then rename.
    On failure the tmp dir is removed so the next tick retries a full clone."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=dest.parent, suffix=".tmp"))
    try:
        proc = _run_git(["git", "clone", url, str(tmp_dir / "checkout")], what="clone")
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
    proc = _run_git(
        ["git", "-C", str(checkout), "fetch", "--prune", "origin"], what="fetch"
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git fetch failed")

    # Check if detached — fetch alone is sufficient for detached HEAD
    head_proc = _run_git(
        ["git", "-C", str(checkout), "rev-parse", "--abbrev-ref", "HEAD"],
        what="rev-parse",
    )
    if head_proc.stdout.strip() == "HEAD":
        return

    # Fast-forward to origin/<base>
    proc = _run_git(
        ["git", "-C", str(checkout), "merge", "--ff-only", f"origin/{base}"],
        what="merge",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"non-fast-forward merge on origin/{base}"
        )
