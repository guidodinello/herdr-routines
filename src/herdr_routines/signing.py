"""Re-sign a run's pushed commits that the agent left unsigned.

The pipeline clones are configured to sign every commit (``docs/pipeline/setup.md`` step 2),
and the repo rulesets reject unsigned commits at merge. But no config can stop an agent:
``git -c commit.gpgsign=false commit`` overrides it, and on 2026-10-01 the issue-refinement
agent did exactly that, unprompted, on its first commit (PR #140 sat BLOCKED on "Commits must
have verified signatures"). So after a run, code checks the branch the agent pushed and
re-signs whatever is unsigned with the clone's own key, rather than trusting a prompt line.

Signedness is read from the commit object's ``gpgsig`` header, *not* ``git log %G?``: on the
pipeline hosts there is no ``gpg.ssh.allowedSignersFile``, so ``%G?`` reports ``N`` even for
SSH-signed commits that GitHub verifies as valid.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from logger import get_logger

log = get_logger(__name__)

GIT_TIMEOUT_S = 120


class ResignError(RuntimeError):
    """A git step of the re-sign failed; the branch on origin is left as it was."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_S,
    )


def _git_ok(repo: Path, *args: str) -> str:
    proc = _git(repo, *args)
    if proc.returncode != 0:
        raise ResignError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def is_signed(repo: Path, sha: str) -> bool:
    """True if the commit object carries a signature header (``gpgsig`` or
    ``gpgsig-sha256``), whatever the signature format."""
    raw = _git_ok(repo, "cat-file", "commit", sha)
    header = raw.split("\n\n", 1)[0]
    return any(line.startswith("gpgsig") for line in header.splitlines())


def resign_unsigned_branch(repo: Path, *, branch: str, base: str) -> int:
    """Re-sign every unsigned commit on ``origin/<branch>`` since ``origin/<base>`` and
    force-push the result. Returns how many commits were unsigned (0 = nothing done).

    A no-op when the clone is not configured to sign (``commit.gpgsign`` unset — the repo
    does not require signatures, so there is no key to sign with) or the agent never pushed
    the branch. The rewrite replays the same commits onto their own merge-base, so it cannot
    conflict; a branch containing merge commits is left alone rather than flattened. The push
    is leased on the tip just inspected, so a concurrent push wins over this rewrite.

    Raises ``ResignError`` if a git step fails."""
    if _git(repo, "config", "--get", "commit.gpgsign").stdout.strip() != "true":
        return 0
    remote_branch = f"refs/remotes/origin/{branch}"
    remote_base = f"refs/remotes/origin/{base}"
    fetch = _git(
        repo,
        "fetch",
        "--quiet",
        "origin",
        f"+refs/heads/{branch}:{remote_branch}",
        f"+refs/heads/{base}:{remote_base}",
    )
    if fetch.returncode != 0:
        # Most often: the agent never pushed this branch. Nothing on origin to fix.
        log.info("re-sign: origin has no %s (%s)", branch, fetch.stderr.strip())
        return 0

    tip = _git_ok(repo, "rev-parse", remote_branch).strip()
    shas = _git_ok(repo, "rev-list", f"{remote_base}..{tip}").split()
    unsigned = [sha for sha in shas if not is_signed(repo, sha)]
    if not unsigned:
        return 0
    if _git_ok(repo, "rev-list", "--merges", f"{remote_base}..{tip}").strip():
        raise ResignError(
            f"{branch} has {len(unsigned)} unsigned commit(s) but contains merge "
            "commits; not rewriting it"
        )

    merge_base = _git_ok(repo, "merge-base", remote_base, tip).strip()
    # A detached throwaway worktree: the run's own worktree still has `branch` checked
    # out, and a second checkout of the same branch is refused.
    with tempfile.TemporaryDirectory(prefix="herdr-resign-") as tmp:
        wt = Path(tmp) / "wt"
        _git_ok(repo, "worktree", "add", "--quiet", "--detach", str(wt), tip)
        try:
            _git_ok(wt, "rebase", "--quiet", "--force-rebase", "-S", merge_base)
            _git_ok(
                wt,
                "push",
                "--quiet",
                f"--force-with-lease=refs/heads/{branch}:{tip}",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
        finally:
            _git(repo, "worktree", "remove", "--force", str(wt))
    log.warning(
        "re-sign: %s had %d unsigned commit(s); re-signed and force-pushed",
        branch,
        len(unsigned),
    )
    return len(unsigned)
