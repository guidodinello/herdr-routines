"""Tests for `signing.resign_unsigned_branch` — re-signing a run's pushed commits that the
agent left unsigned (PR #140: `git -c commit.gpgsign=false commit`).

Real git against a real bare origin, signing with a real throwaway SSH key: what matters
is that the commits on *origin* end up carrying a signature, which a fake cannot show.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from herdr_routines.signing import ResignError, is_signed, resign_unsigned_branch

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs ssh-keygen for an SSH signing key"
)

BRANCH = "auto/issue-refinement-20261001T010000Z"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # The host's own ~/.gitconfig (signing keys, gpgsign) must not leak in.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone configured like docs/pipeline/setup.md step 2, with `main` on origin and
    BRANCH pushed carrying two commits made with signing switched off."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    key = tmp_path / "signing"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    repo = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(origin), str(repo))
    for k, v in {
        "user.email": "bot@example.com",
        "user.name": "bot",
        "gpg.format": "ssh",
        "user.signingkey": f"{key}.pub",
        "commit.gpgsign": "true",
    }.items():
        _git(repo, "config", k, v)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "init")
    _git(repo, "push", "--quiet", "origin", "HEAD:main")
    _git(repo, "switch", "--quiet", "-c", BRANCH)
    for name in ("a", "b"):
        (repo / name).write_text(f"{name}\n")
        _git(repo, "add", name)
        _git(repo, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", name)
    _git(repo, "push", "--quiet", "origin", BRANCH)
    return repo


def _origin_commits(repo: Path) -> list[str]:
    _git(repo, "fetch", "--quiet", "origin")
    return _git(repo, "rev-list", f"origin/main..origin/{BRANCH}").split()


def test_unsigned_commits_are_resigned_on_origin(clone: Path) -> None:
    before = _origin_commits(clone)
    tree_before = _git(clone, "rev-parse", f"origin/{BRANCH}^{{tree}}")
    assert not any(is_signed(clone, sha) for sha in before)

    assert resign_unsigned_branch(clone, branch=BRANCH, base="main") == 2

    after = _origin_commits(clone)
    assert len(after) == 2
    assert all(is_signed(clone, sha) for sha in after)
    # Same content, same messages — only the signatures changed.
    assert _git(clone, "rev-parse", f"origin/{BRANCH}^{{tree}}") == tree_before
    assert _git(clone, "log", "--format=%s", f"origin/main..origin/{BRANCH}") == "b\na"


def test_signed_branch_is_left_alone(clone: Path) -> None:
    resign_unsigned_branch(clone, branch=BRANCH, base="main")
    tip = _git(clone, "rev-parse", f"origin/{BRANCH}")

    assert resign_unsigned_branch(clone, branch=BRANCH, base="main") == 0
    assert _origin_commits(clone)[0] == tip


def test_clone_without_signing_config_is_a_noop(clone: Path) -> None:
    _git(clone, "config", "--unset", "commit.gpgsign")
    before = _origin_commits(clone)

    assert resign_unsigned_branch(clone, branch=BRANCH, base="main") == 0
    assert _origin_commits(clone) == before


def test_branch_never_pushed_is_a_noop(clone: Path) -> None:
    assert resign_unsigned_branch(clone, branch="auto/never-pushed", base="main") == 0


def test_branch_with_merge_commit_is_not_rewritten(clone: Path) -> None:
    _git(clone, "switch", "--quiet", "-c", "side", "main")
    (clone / "c").write_text("c\n")
    _git(clone, "add", "c")
    _git(clone, "commit", "--quiet", "-m", "c")
    _git(clone, "switch", "--quiet", BRANCH)
    _git(clone, "-c", "commit.gpgsign=false", "merge", "--quiet", "--no-edit", "side")
    _git(clone, "push", "--quiet", "origin", BRANCH)
    before = _origin_commits(clone)

    with pytest.raises(ResignError, match="merge commits"):
        resign_unsigned_branch(clone, branch=BRANCH, base="main")
    assert _origin_commits(clone) == before
