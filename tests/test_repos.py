"""Tests for repos.py: clone-if-missing, fetch+ff, failure paths, explicit repo unchanged."""

from __future__ import annotations

import io
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.config import Job
from herdr_routines.pick_feature import parse_issue, run_pick_feature
from herdr_routines.repos import ensure_repo


def make_job(tmp_path: Path, **overrides: Any) -> Job:
    job = Job(
        name="a",
        enabled=True,
        cron="0 3 * * *",
        repo=tmp_path / "checkout",
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="Write $ROUTINE_REPORT",
        timeout_ms=60_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )
    return replace(job, **overrides)


def _init_bare_git_repo(path: Path) -> None:
    """Create a bare git repo at *path* for testing clones, with an initial commit."""
    subprocess.run(
        ["git", "init", "--bare", str(path)], capture_output=True, text=True, check=True
    )
    # Create a temp work dir, init, push to bare
    tmp = path.parent / f".bare-init-{path.name}"
    subprocess.run(
        ["git", "clone", str(path), str(tmp)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.email", "test@test.com"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
        check=True,
    )
    (tmp / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "-C", str(tmp), "add", "README.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "commit", "-m", "init"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "push", "-u", "origin", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    import shutil

    shutil.rmtree(tmp)


def _detect_bare_default_branch(bare_path: Path) -> str:
    """Detect the default branch name of a bare repo."""
    proc = subprocess.run(
        ["git", "-C", str(bare_path), "symbolic-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip().replace("refs/heads/", "")


# --- Acceptance 1: test_repo_url_clone_if_missing ---


def test_repo_url_clone_if_missing(tmp_path: Path) -> None:
    """repository job with no existing checkout clones to default_repos_dir / job.name."""
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    repos_dir = tmp_path / "repos"
    checkout = repos_dir / "a"
    job = make_job(tmp_path, repository=f"file://{bare}", repo=checkout, base=base)
    result = ensure_repo(job)
    assert result == checkout
    assert (checkout / ".git").exists()


# --- Acceptance 2: test_repo_url_fetch_fast_forward ---


def test_repo_url_fetch_fast_forward(tmp_path: Path) -> None:
    """Existing managed checkout fetches and fast-forwards on subsequent runs."""
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    repos_dir = tmp_path / "repos"
    checkout = repos_dir / "a"
    job = make_job(tmp_path, repository=f"file://{bare}", repo=checkout, base=base)
    # First run: clone
    ensure_repo(job)

    # Push a new commit to bare
    tmp_work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(tmp_work)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.email", "test@test.com"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
        check=True,
    )
    (tmp_work / "NEW.md").write_text("new\n")
    subprocess.run(
        ["git", "-C", str(tmp_work), "add", "NEW.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "commit", "-m", "add new"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "push"], capture_output=True, text=True, check=True
    )

    # Second run: fetch + fast-forward
    ensure_repo(job)
    assert (checkout / "NEW.md").exists()


# --- Acceptance 3: plain repo: jobs are fetched+fast-forwarded too (issue 030) ---


def test_repo_plain_job_fetches_and_fast_forwards(tmp_path: Path) -> None:
    """A plain repo: job (no repository: field) is fetched+fast-forwarded to origin/<base>
    just like a repository:-managed job — issue 030's core behavior change."""
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
    job = make_job(tmp_path, repo=checkout, repository=None, base=base)

    # Push a new commit to bare that the local checkout doesn't have yet.
    tmp_work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(tmp_work)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.email", "test@test.com"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
        check=True,
    )
    (tmp_work / "NEW.md").write_text("new\n")
    subprocess.run(
        ["git", "-C", str(tmp_work), "add", "NEW.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "commit", "-m", "add new"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "push"], capture_output=True, text=True, check=True
    )

    result = ensure_repo(job)
    assert result == checkout
    assert (checkout / "NEW.md").exists()


def test_repo_plain_job_without_git_dir_fails_loudly(tmp_path: Path) -> None:
    """A plain repo: job never clones (only repository: jobs may), so a missing/non-git
    checkout must fail loudly rather than silently returning the path unchanged."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    job = make_job(tmp_path, repo=checkout, repository=None)
    with pytest.raises(RuntimeError):
        ensure_repo(job)


# --- Acceptance 4: test_repo_url_clone_failed_clean ---


def test_repo_url_clone_failed_clean(tmp_path: Path) -> None:
    """Clone failure raises RuntimeError, tmp dir removed, no partial checkout."""
    repos_dir = tmp_path / "repos"
    checkout = repos_dir / "a"
    job = make_job(
        tmp_path,
        repository="https://this-does-not-exist.example.com/repo.git",
        repo=checkout,
    )
    with pytest.raises(RuntimeError):
        ensure_repo(job)
    # No partial checkout left behind
    assert not checkout.exists()
    # No tmp dirs left behind
    tmp_dirs = list(repos_dir.glob("*.tmp")) if repos_dir.exists() else []
    assert tmp_dirs == []


def _raise_timeout(*args: Any, **kwargs: Any) -> None:
    raise subprocess.TimeoutExpired(cmd="git", timeout=120)


def test_fetch_timeout_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git timeout is a repo sync failure (RuntimeError), not a stray TimeoutExpired
    that the callers' `except (RuntimeError, OSError)` guards would miss."""
    monkeypatch.setattr("herdr_routines.repos.subprocess.run", _raise_timeout)
    job = make_job(tmp_path, repo=tmp_path / "checkout", repository=None)
    with pytest.raises(RuntimeError, match="timed out"):
        ensure_repo(job)


def test_clone_timeout_raises_runtime_error_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("herdr_routines.repos.subprocess.run", _raise_timeout)
    repos_dir = tmp_path / "repos"
    checkout = repos_dir / "a"
    job = make_job(tmp_path, repository="https://example.com/repo.git", repo=checkout)
    with pytest.raises(RuntimeError, match="timed out"):
        ensure_repo(job)
    assert not checkout.exists()
    assert list(repos_dir.glob("*.tmp")) == []


# --- Acceptance 5: test_repo_url_repo_sync_failed_non_fast_forward ---


def test_repo_url_repo_sync_failed_non_fast_forward(tmp_path: Path) -> None:
    """Non-fast-forward merge raises RuntimeError, checkout left untouched."""
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    repos_dir = tmp_path / "repos"
    checkout = repos_dir / "a"
    job = make_job(tmp_path, repository=f"file://{bare}", repo=checkout, base=base)
    # Clone
    ensure_repo(job)

    # Create a local commit on main (diverged from bare)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.email", "test@test.com"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
        check=True,
    )
    (checkout / "LOCAL.md").write_text("local\n")
    subprocess.run(
        ["git", "-C", str(checkout), "add", "LOCAL.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "commit", "-m", "local commit"],
        capture_output=True,
        text=True,
        check=True,
    )

    # Push a different commit to bare (diverges)
    tmp_work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(bare), str(tmp_work)],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.email", "test@test.com"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
        check=True,
    )
    (tmp_work / "REMOTE.md").write_text("remote\n")
    subprocess.run(
        ["git", "-C", str(tmp_work), "add", "REMOTE.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "commit", "-m", "remote commit"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "push"], capture_output=True, text=True, check=True
    )

    # Fetch + merge should fail (non-fast-forward or unrelated histories)
    with pytest.raises(RuntimeError):
        ensure_repo(job)

    # Local checkout left untouched (LOCAL.md still there, REMOTE.md not added)
    assert (checkout / "LOCAL.md").exists()
    assert not (checkout / "REMOTE.md").exists()


def test_repo_url_review_tiers_present() -> None:
    """Spec v2 review notes contain blocking/non-blocking and confidence tiers."""
    from pathlib import Path

    spec = Path("docs/pipeline/runs/20260902T050021Z/spec.md")
    if not spec.exists():
        spec = (
            Path(__file__).parent.parent / "docs/pipeline/runs/20260902T050021Z/spec.md"
        )
    text = spec.read_text() if spec.exists() else ""
    assert "blocking" in text.lower()
    assert "non-blocking" in text.lower()
    assert "confidence:" in text.lower()


# --- Issue 041: an out-of-tree claim never wedges the parent clone's sync ---

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

    ensure_repo(make_job(tmp_path, repo=parent, repository=None, base=base))
    assert parse_issue(issues_dir / "001-foo.md").status == "done"


def test_repo_sync_ff_after_pick_and_merge(tmp_path: Path) -> None:
    """Acceptance criterion 2 (issue 041): a pick with `--mark-in-progress`
    followed by the implementing PR's merge must not wedge the repo sync — the
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

    ensure_repo(make_job(tmp_path, repo=parent, repository=None, base=base))
    assert parse_issue(issues_dir / "001-foo.md").status == "done"
