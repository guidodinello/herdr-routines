"""Tests for `herdr-routines sync-repo` (issue 030): the CLI entry point wrapping
`_fetch_and_fast_forward` for callers outside tick.py/runner.py's job-dispatch path,
e.g. the overnight pipeline launcher's $REPO_PARENT."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from herdr_routines import cli
from herdr_routines.pick_feature import parse_issue, run_pick_feature

ISSUE_TEMPLATE = """---
id: "001"
title: Some issue
status: {status}
priority: medium
area: cli
---

## Description

Do the thing.
"""


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


def test_done_flip_still_rides_the_pr(tmp_path: Path) -> None:
    """Acceptance criterion 4 (issue 041): the implementer's `status: done` commit,
    carried by the PR and landing atomically on merge, is untouched by this fix —
    it still fast-forwards into the parent clone like any other upstream commit."""
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    parent = tmp_path / "parent"
    subprocess.run(
        ["git", "clone", str(bare), str(parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    issues_dir = parent / "docs" / "process" / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "001-foo.md").write_text(ISSUE_TEMPLATE.format(status="open"))
    _git(parent, "config", "user.email", "test@test.com")
    _git(parent, "config", "user.name", "Test")
    _git(parent, "add", "docs")
    _git(parent, "commit", "-m", "add issue 001")
    _git(parent, "push")

    # The implementer's PR, committed and merged directly to main.
    impl = tmp_path / "impl"
    subprocess.run(
        ["git", "clone", str(bare), str(impl)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(impl, "config", "user.email", "test@test.com")
    _git(impl, "config", "user.name", "Test")
    (impl / "docs" / "process" / "issues" / "001-foo.md").write_text(
        ISSUE_TEMPLATE.format(status="done")
    )
    _git(impl, "add", "docs")
    _git(impl, "commit", "-m", "fix: close issue 001")
    _git(impl, "push")

    code = cli.main(["sync-repo", "--path", str(parent), "--base", base])
    assert code == 0
    assert parse_issue(issues_dir / "001-foo.md").status == "done"


def test_sync_repo_ff_after_pick_and_merge(tmp_path: Path) -> None:
    """Acceptance criterion 2 (issue 041): a pick with `--mark-in-progress`
    followed by the implementing PR's merge must not wedge `sync-repo` — the
    parent clone has no uncommitted edit left to collide with the incoming
    `status: done` commit."""
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    parent = tmp_path / "parent"
    subprocess.run(
        ["git", "clone", str(bare), str(parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    issues_dir = parent / "docs" / "process" / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "001-foo.md").write_text(ISSUE_TEMPLATE.format(status="open"))
    _git(parent, "config", "user.email", "test@test.com")
    _git(parent, "config", "user.name", "Test")
    _git(parent, "add", "docs")
    _git(parent, "commit", "-m", "add issue 001")
    _git(parent, "push")

    # pick-feature --mark-in-progress against the parent clone: must claim
    # out-of-tree and leave the working tree clean.
    claims_path = tmp_path / "claims.json"
    code = run_pick_feature(
        issues_dir, mark=True, out=io.StringIO(), claims_path=claims_path
    )
    assert code == 0
    status = _git(parent, "status", "--porcelain")
    assert status.stdout == ""

    # The implementing PR merges the done flip straight to main.
    impl = tmp_path / "impl"
    subprocess.run(
        ["git", "clone", str(bare), str(impl)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(impl, "config", "user.email", "test@test.com")
    _git(impl, "config", "user.name", "Test")
    (impl / "docs" / "process" / "issues" / "001-foo.md").write_text(
        ISSUE_TEMPLATE.format(status="done")
    )
    _git(impl, "add", "docs")
    _git(impl, "commit", "-m", "fix: close issue 001")
    _git(impl, "push")

    code = cli.main(["sync-repo", "--path", str(parent), "--base", base])
    assert code == 0
    assert parse_issue(issues_dir / "001-foo.md").status == "done"
