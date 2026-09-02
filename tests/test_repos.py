"""Tests for repos.py: clone-if-missing, fetch+ff, failure paths, explicit repo unchanged."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.config import Job
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
    subprocess.run(["git", "init", "--bare", str(path)], capture_output=True, text=True, check=True)
    # Create a temp work dir, init, push to bare
    tmp = path.parent / f".bare-init-{path.name}"
    subprocess.run(["git", "clone", str(path), str(tmp)], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "test@test.com"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "Test"], capture_output=True, text=True, check=True)
    (tmp / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(tmp), "add", "README.md"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(tmp), "push", "-u", "origin", "HEAD"], capture_output=True, text=True, check=True)
    import shutil
    shutil.rmtree(tmp)


def _detect_bare_default_branch(bare_path: Path) -> str:
    """Detect the default branch name of a bare repo."""
    proc = subprocess.run(
        ["git", "-C", str(bare_path), "symbolic-ref", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip().replace("refs/heads/", "")


def _init_git_repo_with_commit(path: Path, filename: str = "README.md") -> None:
    """Create a git repo with an initial commit at *path*."""
    subprocess.run(["git", "init", str(path)], capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True,
        text=True,
        check=True,
    )
    (path / filename).write_text("# test\n")
    subprocess.run(
        ["git", "-C", str(path), "add", filename],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        capture_output=True,
        text=True,
        check=True,
    )


# --- Acceptance 1: test_repo_url_clone_if_missing ---


def test_repo_url_clone_if_missing(tmp_path: Path) -> None:
    """repository job with no existing checkout clones to default_repos_dir / job.name."""
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)

    repos_dir = tmp_path / "repos"
    checkout = repos_dir / "a"
    job = make_job(tmp_path, repository=f"file://{bare}", repo=checkout, base=base)
    result = ensure_repo(job, repos_dir=repos_dir)
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
    ensure_repo(job, repos_dir=repos_dir)

    # Push a new commit to bare
    tmp_work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(tmp_work)], capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.email", "test@test.com"],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.name", "Test"],
        capture_output=True, text=True, check=True,
    )
    (tmp_work / "NEW.md").write_text("new\n")
    subprocess.run(["git", "-C", str(tmp_work), "add", "NEW.md"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(tmp_work), "commit", "-m", "add new"], capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(tmp_work), "push"], capture_output=True, text=True, check=True)

    # Second run: fetch + fast-forward
    ensure_repo(job, repos_dir=repos_dir)
    assert (checkout / "NEW.md").exists()


# --- Acceptance 3: test_repo_url_explicit_repo_unchanged ---


def test_repo_url_explicit_repo_unchanged(tmp_path: Path) -> None:
    """Legacy repo-only jobs never enter the ensure_repo fetch path."""
    checkout = tmp_path / "checkout"
    _init_git_repo_with_commit(checkout)
    job = make_job(tmp_path, repo=checkout, repository=None)
    result = ensure_repo(job)
    assert result == checkout


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
        ensure_repo(job, repos_dir=repos_dir)
    # No partial checkout left behind
    assert not checkout.exists()
    # No tmp dirs left behind
    tmp_dirs = list(repos_dir.glob("*.tmp.*")) if repos_dir.exists() else []
    assert tmp_dirs == []


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
    ensure_repo(job, repos_dir=repos_dir)

    # Create a local commit on main (diverged from bare)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.email", "test@test.com"],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "config", "user.name", "Test"],
        capture_output=True, text=True, check=True,
    )
    (checkout / "LOCAL.md").write_text("local\n")
    subprocess.run(["git", "-C", str(checkout), "add", "LOCAL.md"], capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "commit", "-m", "local commit"],
        capture_output=True, text=True, check=True,
    )

    # Push a different commit to bare (diverges)
    tmp_work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(tmp_work)], capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.email", "test@test.com"],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_work), "config", "user.name", "Test"],
        capture_output=True, text=True, check=True,
    )
    (tmp_work / "REMOTE.md").write_text("remote\n")
    subprocess.run(["git", "-C", str(tmp_work), "add", "REMOTE.md"], capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_work), "commit", "-m", "remote commit"],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(["git", "-C", str(tmp_work), "push"], capture_output=True, text=True, check=True)

    # Fetch + merge should fail (non-fast-forward or unrelated histories)
    with pytest.raises(RuntimeError):
        ensure_repo(job, repos_dir=repos_dir)

    # Local checkout left untouched (LOCAL.md still there, REMOTE.md not added)
    assert (checkout / "LOCAL.md").exists()
    assert not (checkout / "REMOTE.md").exists()

def test_repo_url_review_tiers_present() -> None:
    """Spec v2 review notes contain blocking/non-blocking and confidence tiers."""
    from pathlib import Path
    spec = Path("docs/pipeline/runs/20260902T050021Z/spec.md")
    if not spec.exists():
        spec = Path(__file__).parent.parent / "docs/pipeline/runs/20260902T050021Z/spec.md"
    text = spec.read_text() if spec.exists() else ""
    assert "blocking" in text.lower()
    assert "non-blocking" in text.lower()
    assert "confidence:" in text.lower()
