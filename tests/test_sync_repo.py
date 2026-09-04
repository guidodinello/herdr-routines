"""Tests for `herdr-routines sync-repo` (issue 030): the CLI entry point wrapping
`_fetch_and_fast_forward` for callers outside tick.py/runner.py's job-dispatch path,
e.g. the overnight pipeline launcher's $REPO_PARENT."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from herdr_routines import cli


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--bare", str(path)], capture_output=True, text=True, check=True
    )
    tmp = path.parent / f".bare-init-{path.name}"
    subprocess.run(
        ["git", "clone", str(path), str(tmp)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(tmp, "config", "user.email", "test@test.com")
    _git(tmp, "config", "user.name", "Test")
    (tmp / "README.md").write_text("init\n")
    _git(tmp, "add", "README.md")
    _git(tmp, "commit", "-m", "init")
    _git(tmp, "push", "-u", "origin", "HEAD")
    import shutil

    shutil.rmtree(tmp)


def _detect_bare_default_branch(bare_path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(bare_path), "symbolic-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip().replace("refs/heads/", "")


def test_sync_repo_fetches_and_fast_forwards(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", str(bare), str(checkout)],
        capture_output=True,
        text=True,
        check=True,
    )

    # Push a new commit to bare that the local checkout doesn't have yet.
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(work)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(work, "config", "user.email", "test@test.com")
    _git(work, "config", "user.name", "Test")
    (work / "NEW.md").write_text("new\n")
    _git(work, "add", "NEW.md")
    _git(work, "commit", "-m", "add new")
    _git(work, "push")

    code = cli.main(["sync-repo", "--path", str(checkout), "--base", base])
    assert code == 0
    assert (checkout / "NEW.md").exists()


def test_sync_repo_fails_loudly_on_non_fast_forward(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", str(bare), str(checkout)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(checkout, "config", "user.email", "test@test.com")
    _git(checkout, "config", "user.name", "Test")
    (checkout / "LOCAL.md").write_text("local\n")
    _git(checkout, "add", "LOCAL.md")
    _git(checkout, "commit", "-m", "local commit")

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(work)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(work, "config", "user.email", "test@test.com")
    _git(work, "config", "user.name", "Test")
    (work / "REMOTE.md").write_text("remote\n")
    _git(work, "add", "REMOTE.md")
    _git(work, "commit", "-m", "remote commit")
    _git(work, "push")

    code = cli.main(["sync-repo", "--path", str(checkout), "--base", base])
    assert code != 0
    # Checkout left untouched, no silent proceed on the diverged branch.
    assert (checkout / "LOCAL.md").exists()
    assert not (checkout / "REMOTE.md").exists()


def test_sync_repo_requires_path() -> None:
    with pytest.raises(SystemExit):
        cli.main(["sync-repo", "--base", "main"])
