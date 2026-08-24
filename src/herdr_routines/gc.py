"""Read-only inventory of ``auto/*`` branches backing ``herdr-routines gc --dry-run``.

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
        print(
            f"warning: merge-check failed for {branch} vs {base}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
    return False


def branch_worktrees(repo: Path) -> dict[str, Path]:
    """Map each branch checked out in a linked worktree to that worktree's path.

    Tolerant of ordering differences across git versions: each ``branch`` line is paired
    with the most recent preceding ``worktree`` line, no strict layout assumed.
    """
    proc = run_git(repo, "worktree", "list", "--porcelain")
    mapping: dict[str, Path] = {}
    if proc.returncode != 0:
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
    root = resolve_repo_root(repo)
    if root is None:
        print(f"error: not a git repository: {repo}", file=sys.stderr)
        return 1
    resolved_base = base or detect_base(root)
    out.write(format_table(collect_rows(root, resolved_base)))
    return 0
