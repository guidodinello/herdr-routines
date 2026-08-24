"""Tests for `herdr-routines gc --dry-run` (spec.md acceptance criteria).

Each test builds a real temp git repo and drives the CLI through herdr_routines.cli.main,
so the git plumbing (for-each-ref, merge-base, worktree list --porcelain) is exercised
for real rather than stubbed. The no-server test additionally spies on every subprocess
and blocks sockets/HerdrClient to prove the command is pure git + filesystem.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from herdr_routines import cli, gc


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Plain clone with one seed commit on main and a deterministic identity."""
    target = tmp_path / "repo"
    _git(tmp_path, "init", "-b", "main", "repo")
    _git(target, "config", "user.email", "test@example.com")
    _git(target, "config", "user.name", "Test")
    (target / "README.md").write_text("seed\n")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "seed")
    return target


def _gc(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(["gc", "--dry-run", "--repo", str(repo), "--base", "main"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _rows(out: str) -> dict[str, tuple[str, str]]:
    """Parse table body into {branch: (worktree-exists, merged-into-base)}."""
    result: dict[str, tuple[str, str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("auto/"):
            result[parts[0]] = (parts[-2], parts[-1])
    return result


def test_gc_dry_run_lists_merged_branches(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    branch = "auto/nightly-dep-audit-20260822T030000Z"
    _git(repo, "branch", branch, "main")  # tip == main ⇒ fully merged
    worktree = tmp_path / "wt-audit"
    _git(repo, "worktree", "add", str(worktree), branch)

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[branch] == ("yes", "yes")
    assert "1 branch(es) listed" in out


def test_gc_dry_run_lists_gone_worktrees(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Worktree dir removed behind git's back: still registered in `worktree list`,
    so exists=no — and the diverged tip keeps merged=no. The row must still be listed."""
    branch = "auto/fix-foo-20260823T011500Z"
    worktree = tmp_path / "wt-fix"
    _git(repo, "worktree", "add", str(worktree), "-b", branch)
    (worktree / "fix.txt").write_text("wip\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "wip on branch")
    shutil.rmtree(worktree)

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[branch] == ("no", "no")


def test_gc_dry_run_excludes_pipeline_branches(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pipeline = "auto/pipeline-nightly-20260824T010000Z"
    real = "auto/real-job-20260820T000000Z"
    _git(repo, "branch", pipeline, "main")
    _git(repo, "branch", real, "main")

    code, out, err = _gc(repo, capsys)

    assert code == 0
    assert pipeline not in out
    assert real in out
    assert "1 branch(es) listed" in out
    assert "warning" not in err


def test_gc_dry_run_deletes_nothing(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def snapshot() -> tuple[list[str], str, list[tuple[str, int]]]:
        branches = sorted(
            _git(
                repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"
            ).stdout.splitlines()
        )
        worktrees = _git(repo, "worktree", "list", "--porcelain").stdout
        files = sorted(
            (str(p.relative_to(repo)), p.stat().st_size if p.is_file() else -1)
            for p in repo.rglob("*")
        )
        return branches, worktrees, files

    merged = "auto/merged-20260821T000000Z"
    _git(repo, "branch", merged, "main")
    wt_merged = tmp_path / "wt-merged"
    _git(repo, "worktree", "add", str(wt_merged), merged)

    diverged = "auto/diverged-20260822T000000Z"
    wt_div = tmp_path / "wt-div"
    _git(repo, "worktree", "add", str(wt_div), "-b", diverged)
    (wt_div / "wip.txt").write_text("wip\n")
    _git(wt_div, "add", ".")
    _git(wt_div, "commit", "-m", "wip on branch")

    gone = "auto/gone-20260823T000000Z"
    wt_gone = tmp_path / "wt-gone"
    _git(repo, "worktree", "add", str(wt_gone), "-b", gone)
    (wt_gone / "wip.txt").write_text("wip\n")
    _git(wt_gone, "add", ".")
    _git(wt_gone, "commit", "-m", "wip on branch")
    shutil.rmtree(wt_gone)

    _git(repo, "branch", "auto/pipeline-run-20260824T000000Z", "main")

    before = snapshot()
    code, out, _ = _gc(repo, capsys)
    after = snapshot()

    assert code == 0
    assert before == after
    # 3 listed: merged + diverged + gone (pipeline excluded from the count too).
    assert "3 branch(es) listed" in out
    assert "nothing deleted" in out
    assert "eligible: 2, merged: 1, missing worktree: 1" in out
    assert _rows(out)[diverged] == ("yes", "no")


def test_gc_dry_run_needs_no_server(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    branch = "auto/solo-20260819T000000Z"
    _git(repo, "branch", branch, "main")

    commands: list[list[str]] = []
    real_run_git = gc.run_git

    def spy_run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        commands.append(["git", *args])
        return real_run_git(repo_path, *args)

    monkeypatch.setattr(gc, "run_git", spy_run_git)

    def no_herdr_client(*args: object, **kwargs: object) -> None:
        pytest.fail("gc constructed HerdrClient")

    monkeypatch.setattr(cli, "HerdrClient", no_herdr_client)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("gc attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[branch] == ("no", "yes")
    assert commands, "gc should invoke git directly"
    assert all(cmd[0] == "git" for cmd in commands)


def test_gc_dry_run_empty_repo_lists_zero(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _gc(repo, capsys)

    assert code == 0
    assert err == ""
    assert "BRANCH" in out
    assert "0 branch(es) listed" in out


def test_gc_outside_a_git_repo_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()

    code = cli.main(["gc", "--dry-run", "--repo", str(plain_dir)])
    captured = capsys.readouterr()

    assert code == 1
    assert "not a git repository" in captured.err
    assert captured.out == ""


def test_gc_requires_dry_run_flag(repo: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["gc", "--repo", str(repo)])
    assert excinfo.value.code != 0


def test_gc_warns_when_branch_listing_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Plumbing failure after rev-parse must stay visible on stderr, not silently read
    as a clean '0 branch(es) listed' inventory (review finding, PR #28)."""
    real_run_git = gc.run_git

    def failing_for_each_ref(
        repo_path: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        if args[:1] == ("for-each-ref",):
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128, stdout="", stderr="fatal: bad ref"
            )
        return real_run_git(repo_path, *args)

    monkeypatch.setattr(gc, "run_git", failing_for_each_ref)

    code = cli.main(["gc", "--dry-run", "--repo", str(repo), "--base", "main"])
    captured = capsys.readouterr()

    assert code == 0
    assert "0 branch(es) listed" in captured.out
    assert "warning" in captured.err
    assert "could not list auto/* branches" in captured.err
    assert "fatal: bad ref" in captured.err


def test_gc_warns_when_worktree_listing_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing `git worktree list` must not silently mark every row worktree_exists=no
    (which would count them all as eligible) without saying why on stderr."""
    real_run_git = gc.run_git

    def failing_worktree_list(
        repo_path: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("worktree", "list"):
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=128,
                stdout="",
                stderr="fatal: worktree boom",
            )
        return real_run_git(repo_path, *args)

    monkeypatch.setattr(gc, "run_git", failing_worktree_list)
    _git(repo, "branch", "auto/solo-20260819T000000Z", "main")

    code = cli.main(["gc", "--dry-run", "--repo", str(repo), "--base", "main"])
    captured = capsys.readouterr()

    assert code == 0
    assert _rows(captured.out)["auto/solo-20260819T000000Z"] == ("no", "yes")
    assert "warning" in captured.err
    assert "could not list worktrees" in captured.err


def test_gc_times_out_cleanly_without_traceback(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """subprocess.TimeoutExpired from run_git's cap must surface as clean stderr + exit 1,
    never an unhandled traceback (review finding, PR #28)."""

    def hanging(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=gc.GIT_TIMEOUT_SECONDS)

    monkeypatch.setattr(gc, "run_git", hanging)

    code = cli.main(["gc", "--dry-run", "--repo", str(repo), "--base", "main"])
    captured = capsys.readouterr()

    assert code == 1
    assert "timed out after 30s" in captured.err
    assert "Traceback" not in captured.err
