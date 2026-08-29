"""Inventory and optional deletion of ``auto/*`` branches (``herdr-routines gc``).

Pure git + filesystem: no HerdrClient, no socket, no ``herdr`` binary — the command
must stay usable with no Herdr server running (spec.md §No Herdr server required).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

BRANCH_PATTERN = "refs/heads/auto/*"
PIPELINE_PREFIX = "auto/pipeline-"
GIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Row:
    branch: str
    worktree_exists: bool
    merged_into_base: bool

    @property
    def stale(self) -> bool:
        """Eligible for cleanup: fully merged into base OR worktree dir gone."""
        return self.merged_into_base or not self.worktree_exists


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def resolve_repo_root(repo: Path) -> Path | None:
    """Working-tree top-level containing ``repo``, or None when not inside a git repo."""
    proc = run_git(repo, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return Path(root) if root else None


def list_auto_branches(repo: Path) -> list[str]:
    """Local ``auto/*`` branches minus ``auto/pipeline-*`` (G-14), sorted by name."""
    proc = run_git(repo, "for-each-ref", "--format=%(refname:short)", BRANCH_PATTERN)
    if proc.returncode != 0:
        # Keep going with an empty listing, but never let plumbing failure masquerade as
        # a clean "0 branch(es) listed" inventory.
        _warn(f"could not list auto/* branches: {proc.stderr.strip()}")
        return []
    names = (line.strip() for line in proc.stdout.splitlines())
    return sorted(n for n in names if n and not n.startswith(PIPELINE_PREFIX))


def detect_base(repo: Path) -> str:
    """Merge-check target: the repo's default branch via origin/HEAD, else ``main``."""
    proc = run_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    ref = proc.stdout.strip()
    if proc.returncode == 0 and ref.startswith("refs/remotes/"):
        return ref.removeprefix("refs/remotes/")
    return "main"


def is_merged(branch: str, base: str, repo: Path) -> bool:
    """True when the branch tip is reachable from base; unknown-ref failures warn, not raise."""
    proc = run_git(repo, "merge-base", "--is-ancestor", branch, base)
    if proc.returncode == 0:
        return True
    if proc.returncode != 1:
        _warn(f"merge-check failed for {branch} vs {base}: {proc.stderr.strip()}")
    return False


def branch_worktrees(repo: Path) -> dict[str, Path]:
    """Map each branch checked out in a linked worktree to that worktree's path.

    Tolerant of ordering differences across git versions: each ``branch`` line is paired
    with the most recent preceding ``worktree`` line, no strict layout assumed.
    """
    proc = run_git(repo, "worktree", "list", "--porcelain")
    mapping: dict[str, Path] = {}
    if proc.returncode != 0:
        # Same as list_auto_branches: degrade to "no worktrees" but say so on stderr,
        # so a plumbing failure can't silently mark every branch worktree_exists=no.
        _warn(f"could not list worktrees: {proc.stderr.strip()}")
        return mapping
    current: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/"):
            name = line.removeprefix("branch refs/heads/")
            if current is not None and name not in mapping:
                mapping[name] = current
    return mapping


def collect_rows(repo: Path, base: str) -> list[Row]:
    worktrees = branch_worktrees(repo)
    rows: list[Row] = []
    for branch in list_auto_branches(repo):
        path = worktrees.get(branch)
        rows.append(
            Row(
                branch=branch,
                worktree_exists=path is not None and path.exists(),
                merged_into_base=is_merged(branch, base, repo),
            )
        )
    return rows


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def format_table(rows: Sequence[Row]) -> str:
    """Human-readable table with the summary count line last; stable yes/no tokens."""
    width = max([len("BRANCH"), *(len(row.branch) for row in rows)])
    lines = [f"{'BRANCH':<{width}}  WORKTREE-EXISTS  MERGED-INTO-BASE"]
    lines.extend(
        f"{row.branch:<{width}}  {_yn(row.worktree_exists):<16} {_yn(row.merged_into_base)}"
        for row in rows
    )
    merged = sum(row.merged_into_base for row in rows)
    missing_wt = sum(not row.worktree_exists for row in rows)
    eligible = sum(row.stale for row in rows)
    lines.append(
        f"{len(rows)} branch(es) listed (dry-run, nothing deleted; "
        f"eligible: {eligible}, merged: {merged}, missing worktree: {missing_wt})"
    )
    return "\n".join(lines) + "\n"


def run_gc(repo: Path, base: str | None = None, out: TextIO | None = None) -> int:
    """Entry point behind ``gc --dry-run``: print the table, write and delete nothing."""
    if out is None:
        # Resolved lazily so callers that swap sys.stdout (pytest capsys) are honored.
        out = sys.stdout
    try:
        root = resolve_repo_root(repo)
        if root is None:
            print(f"error: not a git repository: {repo}", file=sys.stderr)
            return 1
        resolved_base = base or detect_base(root)
        out.write(format_table(collect_rows(root, resolved_base)))
    except subprocess.TimeoutExpired:
        # run_git's 30s cap must fail cleanly (stderr + exit), never as a traceback.
        print(
            f"error: git timed out after {GIT_TIMEOUT_SECONDS}s in {repo}",
            file=sys.stderr,
        )
        return 1
    return 0


def _remove_worktree(repo: Path, row: Row, worktrees: dict[str, Path]) -> bool:
    path = worktrees.get(row.branch)
    if path is None:
        return True
    proc = run_git(repo, "worktree", "remove", str(path), "--force")
    if proc.returncode != 0:
        _warn(
            f"worktree remove failed for {row.branch}: {proc.stderr.strip()}"
        )
        return False
    return True


def _delete_branch(repo: Path, row: Row) -> bool:
    # Always -D: we already verified merged_into_base via is_merged() against --base,
    # so git's HEAD-based safety net in -d is redundant and can fail when running
    # from a diverged worktree (e.g. auto/pipeline-*).
    proc = run_git(repo, "branch", "-D", row.branch)
    if proc.returncode != 0:
        _warn(f"branch -D failed for {row.branch}: {proc.stderr.strip()}")
        return False
    return True


def format_delete_table(rows: Sequence[Row]) -> str:
    """Table for delete mode — same columns as dry-run, different summary line."""
    width = max([len("BRANCH"), *(len(row.branch) for row in rows)])
    lines = [f"{'BRANCH':<{width}}  WORKTREE-EXISTS  MERGED-INTO-BASE"]
    lines.extend(
        f"{row.branch:<{width}}  {_yn(row.worktree_exists):<16} {_yn(row.merged_into_base)}"
        for row in rows
    )
    eligible = sum(row.stale for row in rows)
    lines.append(
        f"{len(rows)} branch(es) listed (eligible: {eligible})"
    )
    return "\n".join(lines) + "\n"


def run_gc_delete(
    repo: Path,
    base: str | None = None,
    force: bool = False,
    assume_yes: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Entry point behind ``gc --delete``: remove stale auto/* branches."""
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    try:
        root = resolve_repo_root(repo)
        if root is None:
            print(f"error: not a git repository: {repo}", file=err)
            return 1
        resolved_base = base or detect_base(root)
        rows = collect_rows(root, resolved_base)
        candidates = [r for r in rows if r.stale]

        if not candidates:
            out.write(format_delete_table(rows))
            out.write("0 deletion(s) needed.\n")
            return 0

        if not force:
            to_delete = [r for r in candidates if r.merged_into_base]
            skipped = [r for r in candidates if not r.merged_into_base]
        else:
            to_delete = candidates
            skipped = []

        if not assume_yes:
            print(
                "error: refusing to delete without --yes; "
                "--yes is required for gc --delete",
                file=err,
            )
            return 2

        out.write(format_delete_table(rows))

        worktrees = branch_worktrees(root)
        deleted: list[str] = []
        failed: list[str] = []

        for row in to_delete:
            if not _remove_worktree(root, row, worktrees):
                failed.append(f"{row.branch} (worktree remove)")
                continue
            if not _delete_branch(root, row):
                failed.append(row.branch)
                continue
            deleted.append(row.branch)
            out.write(f"deleted: {row.branch}\n")

        for row in skipped:
            out.write(f"skipped (unmerged, needs --force): {row.branch}\n")

        if failed:
            for entry in failed:
                out.write(f"failed: {entry}\n")

        summary = (
            f"deleted: {len(deleted)}, "
            f"skipped (unmerged): {len(skipped)}, "
            f"failed: {len(failed)}"
        )
        out.write(f"{summary}\n")
        return 1 if failed else 0

    except subprocess.TimeoutExpired:
        print(
            f"error: git timed out after {GIT_TIMEOUT_SECONDS}s in {repo}",
            file=err,
        )
        return 1
