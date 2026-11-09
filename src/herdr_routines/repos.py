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
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from logger import get_logger

from herdr_routines.config import Job

log = get_logger(__name__)

REPO_TIMEOUT_S = 120

# Reentrancy: `_dispatch_pending` holds a `workspace: root` job's checkout lock for the
# whole dispatch, then `runner.execute_run` -> `ensure_repo` takes the same lock again.
# A per-thread set makes the inner acquisition a no-op instead of deadlocking on an
# flock this process already owns.
_CHECKOUT_LOCKS_HELD = threading.local()
_EXCLUDED_LOCK_FILES: set[str] = set()


def _checkout_lock_path(repo: Path) -> Path:
    """`<clone>/.routines-checkout.lock`, or a sibling when the checkout is a linked
    worktree (`.git` is a file there, so a file inside it would dirty `git status`)."""
    if (repo / ".git").is_dir():
        return repo / ".routines-checkout.lock"
    return repo.parent / f".{repo.name}.routines-checkout.lock"


def _exclude_lock_file(repo: Path, lock_path: Path) -> None:
    """Keep the in-checkout lock file out of `git status` (self-update refuses a dirty
    checkout). Best effort: an unwritable `.git/info` only costs us a dirty status."""
    if not (repo / ".git").is_dir():
        return
    key = str(lock_path)
    if key in _EXCLUDED_LOCK_FILES:
        return
    exclude = repo / ".git" / "info" / "exclude"
    try:
        existing = exclude.read_text() if exclude.exists() else ""
        if lock_path.name not in existing.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{lock_path.name}\n")
    except OSError as e:
        log.warning("could not add %s to %s: %s", lock_path.name, exclude, e)
    _EXCLUDED_LOCK_FILES.add(key)


@contextmanager
def checkout_lock(repo: Path, *, wait_s: float = 0.0) -> Iterator[bool]:
    """Serialize access to one checkout (issue 079 guarantee 3).

    `workspace: root` jobs run their agent directly in `repo`, so the dispatcher holds
    this for the whole dispatch (non-blocking: a concurrent job skips this tick).
    Worktree-mode jobs take it only around `ensure_repo`'s clone/fetch/fast-forward,
    with a bounded wait so a brief sync is not starved by the other job.

    Yields `False` only when `wait_s` is exhausted; reentrant within a thread so the
    root-dispatch hold and `ensure_repo`'s inner hold never deadlock.
    """
    repo = Path(repo)
    lock_path = _checkout_lock_path(repo)
    key = os.path.abspath(lock_path)
    held: set[str] = getattr(_CHECKOUT_LOCKS_HELD, "paths", set())
    _CHECKOUT_LOCKS_HELD.paths = held
    if key in held:
        yield True
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
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
                time.sleep(min(0.1, wait_s))
        _exclude_lock_file(repo, lock_path)
        held.add(key)
        try:
            yield True
        finally:
            held.discard(key)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
    can map it to a terminal outcome.
    """
    with checkout_lock(dest, wait_s=REPO_TIMEOUT_S) as acquired:
        if not acquired:
            raise RuntimeError(
                f"checkout {dest} is locked by another dispatch "
                f"(waited {REPO_TIMEOUT_S}s)"
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
