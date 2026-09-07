"""Tests for `herdr-routines gc --dry-run` (spec.md acceptance criteria).

Each test builds a real temp git repo and drives the CLI through herdr_routines.cli.main,
so the git plumbing (for-each-ref, merge-base, worktree list --porcelain) is exercised
for real rather than stubbed. The no-server test additionally spies on every subprocess
and blocks sockets/HerdrClient to prove the command is pure git + filesystem.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_routines import cli, gc


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _git_with_date(
    repo: Path, date_iso: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Same as `_git`, but with both commit dates pinned — issue 044's age gate reads
    the tip's *committer* date, which `git commit --date` alone never sets."""
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = date_iso
    env["GIT_COMMITTER_DATE"] = date_iso
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
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


@pytest.fixture(autouse=True)
def _no_gh_or_inflight_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every test gets a hermetic "no open PR, nothing in flight" default so dry-run
    tests that happen to create an `auto/pipeline-*` branch never shell out to a real
    `gh` or touch `~/.herdr` (issue 039; same hermeticity concern issue 038 fixed for
    /tmp diagnosis). Individual tests override these to exercise the retained cases."""
    monkeypatch.setattr(gc, "check_open_pr", lambda repo, branch: False)
    monkeypatch.setattr(gc, "check_inflight", lambda branch, **kwargs: False)
    # Issue 042: is_merged's batched gh lookup gets the same hermetic default — no test
    # here should shell out to a real `gh` just because it created an auto/* branch.
    # Tests exercising squash-merge detection override this explicitly.
    monkeypatch.setattr(gc, "fetch_merged_pr_heads", lambda repo: {})
    # Issue 045: every `gc` invocation that doesn't pass --worktrees-root falls back to
    # ~/.herdr/worktrees/herdr-routines for its orphan sweep — under --delete that sweep
    # unlinks/rmdir's what it finds there. Point the default at a tmp_path sandbox so no
    # test can ever touch (let alone delete from) the real directory on this machine; the
    # five orphan tests below pass --worktrees-root explicitly and are unaffected.
    monkeypatch.setattr(
        gc, "_default_pipeline_worktrees_root", lambda: tmp_path / "no-worktrees-root"
    )


def _gc(
    repo: Path, capsys: pytest.CaptureFixture[str], *extra_args: str
) -> tuple[int, str, str]:
    # --older-than 0 disables issue 044's age gate by default so pre-044 tests, which
    # build branches with no committer-date fixture and expect immediate eligibility,
    # keep testing exactly what they tested before. Age-specific tests override this
    # via extra_args (last --older-than wins — argparse's normal repeated-flag rule).
    code = cli.main(
        [
            "gc",
            "--dry-run",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--older-than",
            "0",
            *extra_args,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _rows(out: str) -> dict[str, tuple[str, str]]:
    """Parse table body into {branch: (worktree-exists, merged-into-base)}.

    Looks up column positions by header name (rather than assuming the last two
    tokens) so issue 044's AGE-DAYS column, appended after MERGED, doesn't shift what
    used to be the last two tokens on each row."""
    lines = out.splitlines()
    header = lines[0].split() if lines else []
    try:
        wt_idx = header.index("WORKTREE-EXISTS")
        merged_idx = header.index("MERGED")
    except ValueError:
        wt_idx, merged_idx = 1, 2
    result: dict[str, tuple[str, str]] = {}
    for line in lines[1:]:
        parts = line.split()
        if parts and parts[0].startswith("auto/"):
            result[parts[0]] = (parts[wt_idx], parts[merged_idx])
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


def test_gc_lists_merged_pipeline_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue 039 acceptance 1: a merged auto/pipeline-* branch with no open PR and no
    in-flight run is no longer structurally excluded — it's listed exactly like any
    other merged auto/* branch."""
    pipeline = "auto/pipeline-nightly-20260824T010000Z"
    real = "auto/real-job-20260820T000000Z"
    _git(repo, "branch", pipeline, "main")
    _git(repo, "branch", real, "main")

    code, out, err = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[pipeline] == ("no", "yes")
    assert real in out
    assert "2 branch(es) listed" in out
    assert "warning" not in err


def test_gc_retains_unmerged_pipeline_branch(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue 039 acceptance 2: an unmerged auto/pipeline-* branch is retained (the
    orchestrator's PR is presumably still open/unmerged) and stays out of the listing."""
    pipeline = "auto/pipeline-nightly-20260824T010000Z"
    wt = tmp_path / "wt-pipeline"
    _git(repo, "worktree", "add", str(wt), "-b", pipeline)
    (wt / "spec.md").write_text("wip\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "wip")

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert pipeline not in out
    assert "0 branch(es) listed" in out


def test_gc_retains_pipeline_branch_with_open_pr(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue 039 acceptance 3: a merged auto/pipeline-* branch with an open PR is still
    retained — merged-into-base alone isn't enough once a PR is open against it."""
    pipeline = "auto/pipeline-nightly-20260824T010000Z"
    _git(repo, "branch", pipeline, "main")
    monkeypatch.setattr(gc, "check_open_pr", lambda repo_, branch: branch == pipeline)

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert pipeline not in out
    assert "0 branch(es) listed" in out


def test_gc_non_pipeline_branches_unaffected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue 039 acceptance 4: non-pipeline auto/* behaviour is unchanged — merged, no
    open-PR/in-flight check ever runs against it."""
    real = "auto/real-job-20260820T000000Z"
    _git(repo, "branch", real, "main")
    checked: list[str] = []

    def spy_open_pr(repo_: Path, branch: str) -> bool:
        checked.append(branch)
        return False

    monkeypatch.setattr(gc, "check_open_pr", spy_open_pr)

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[real] == ("no", "yes")
    assert "1 branch(es) listed" in out
    assert checked == []


# ---------------------------------------------------------------------------
# Issue 042: squash-merge detection via gh, not ancestry alone
# ---------------------------------------------------------------------------


def _squash_merge(repo: Path, branch: str, filename: str) -> None:
    """Build the one topology this repo actually produces: `branch` gets its own
    commit(s), then `base` gets a *new* commit carrying the same content — landed the
    way a squash-merged PR lands, not by merging or fast-forwarding `branch` into it.
    `branch`'s own commit is deliberately never made reachable from `base`, so
    `git merge-base --is-ancestor branch base` stays false — the exact gap issue 042
    reports."""
    _git(repo, "checkout", "-b", branch)
    (repo / filename).write_text("squashed content\n")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"work on {branch}")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--squash", branch)
    _git(repo, "commit", "-m", f"squash-merge {branch} (#1)")


def test_gc_detects_squash_merged_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 1: a squash-merged auto/* branch — never an ancestor of base, because
    it lands as a brand-new commit on base with no second parent — is reported merged
    once GitHub's PR record says so."""
    branch = "auto/fix-thing-20260901T000000Z"
    _squash_merge(repo, branch, "thing.txt")
    assert not gc.is_merged(branch, "main", repo, {}), (
        "test setup bug: branch must NOT be an ancestor of base for this to test "
        "squash-merge detection rather than ordinary ancestry"
    )
    monkeypatch.setattr(gc, "fetch_merged_pr_heads", lambda repo_: {branch: None})

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[branch] == ("no", "yes")


def test_gc_lists_squash_merged_pipeline_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 2: a squash-merged auto/pipeline-* branch with no open PR is listed
    as eligible — same as any other merged auto/* branch (issue 039's retention only
    kicks in for a still-unmerged, still-open, or still-in-flight pipeline branch)."""
    pipeline = "auto/pipeline-nightly-20260901T000000Z"
    _squash_merge(repo, pipeline, "pipeline.txt")
    monkeypatch.setattr(gc, "fetch_merged_pr_heads", lambda repo_: {pipeline: None})

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[pipeline] == ("no", "yes")


def test_gc_open_pr_branch_not_merged(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 3: a branch with an open PR (never merged, so `gh pr list --state
    merged` never names it) is not reported merged — an open PR alone can't spuriously
    flip the merged column."""
    branch = "auto/wip-thing-20260901T000000Z"
    wt = repo.parent / "wt-wip-open-pr"
    _git(repo, "worktree", "add", str(wt), "-b", branch)
    (wt / "wip.txt").write_text("in review\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", f"work on {branch}")
    monkeypatch.setattr(gc, "check_open_pr", lambda repo_, b: b == branch)

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[branch] == ("yes", "no")


def test_gc_unmerged_branch_not_reported_merged(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 4: a genuinely unmerged branch — no PR at all, merged or open — is
    not reported merged. Guards against is_merged's gh-authority addition producing a
    false positive when gh legitimately has nothing to say about the branch."""
    branch = "auto/unmerged-thing-20260901T000000Z"
    wt = repo.parent / "wt-unmerged"
    _git(repo, "worktree", "add", str(wt), "-b", branch)
    (wt / "wip.txt").write_text("still in progress\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "wip")

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert _rows(out)[branch] == ("yes", "no")


_REAL_CHECK_INFLIGHT = gc.check_inflight


def test_gc_check_inflight_retains_merged_branch_with_no_terminal_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_inflight (issue 039's third OR-clause) reads state.json's own `branch`
    field under worktrees_root/auto-pipeline-*/, exactly as the orchestrator writes it
    (docs/pipeline/orchestrator-prompt.md), and treats a run as in-flight until its
    terminal report shows up — same criterion pipeline_watchdog.py uses.

    This tests the real function directly, undoing the autouse stub above."""
    monkeypatch.setattr(gc, "check_inflight", _REAL_CHECK_INFLIGHT)
    branch = "auto/pipeline-20260824T010000Z"
    worktrees_root = tmp_path / "worktrees"
    run_dir = worktrees_root / "auto-pipeline-20260824T010000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        f'{{"run_id": "20260824T010000Z", "branch": "{branch}"}}'
    )
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    assert gc.check_inflight(
        branch, worktrees_root=worktrees_root, reports_dir=reports_dir
    )

    (reports_dir / "pipeline-20260824T010000Z.md").write_text("## Outcome: ok\n")
    assert not gc.check_inflight(
        branch, worktrees_root=worktrees_root, reports_dir=reports_dir
    )


def test_gc_check_inflight_false_for_non_pipeline_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gc, "check_inflight", _REAL_CHECK_INFLIGHT)
    assert not gc.check_inflight(
        "auto/real-job-20260820T000000Z", worktrees_root=tmp_path
    )


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
    # 4 listed: merged + diverged + gone + the merged pipeline branch (issue 039: a
    # merged auto/pipeline-* branch with no open PR/in-flight run is now listed like
    # any other auto/* branch — it just never becomes a delete candidate, see the
    # delete-half tests below).
    assert "4 branch(es) listed" in out
    assert "nothing deleted" in out
    assert "eligible: 3, merged: 2, missing worktree: 2" in out
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


# ---------------------------------------------------------------------------
# Delete-half acceptance tests (spec v2)
# ---------------------------------------------------------------------------


def _gc_delete(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    *extra_args: str,
) -> tuple[int, str, str]:
    # --older-than 0 disables issue 044's age gate by default; see _gc's comment above.
    code = cli.main(
        [
            "gc",
            "--delete",
            "--yes",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--older-than",
            "0",
            *extra_args,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_gc_delete_removes_only_stale_merged_without_force(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --force, only stale branches where merged_into_base==True are deleted.
    Stale-but-unmerged (gone worktree, not merged) is skipped; non-stale branches
    (worktree exists, not merged) are not candidates at all."""
    merged = "auto/merged-20260821T000000Z"
    _git(repo, "branch", merged, "main")

    gone = "auto/gone-20260823T000000Z"
    wt_gone = tmp_path / "wt-gone"
    _git(repo, "worktree", "add", str(wt_gone), "-b", gone)
    (wt_gone / "wip.txt").write_text("wip\n")
    _git(wt_gone, "add", ".")
    _git(wt_gone, "commit", "-m", "wip on branch")
    shutil.rmtree(wt_gone)

    code, out, _ = _gc_delete(repo, capsys)

    assert code == 0
    assert f"deleted: {merged}" in out
    assert f"skipped (unmerged, needs --force): {gone}" in out
    assert "deleted: 1, skipped (unmerged): 1, failed: 0" in out
    # Merged branch is gone; gone-unmerged survives
    remaining = sorted(
        _git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
        ).stdout.splitlines()
    )
    assert merged not in remaining
    assert gone in remaining


def test_gc_delete_with_force_removes_orphaned_unmerged(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --force, both merged and orphaned-unmerged branches are deleted."""
    merged = "auto/merged-20260821T000000Z"
    _git(repo, "branch", merged, "main")

    gone = "auto/gone-20260823T000000Z"
    wt_gone = tmp_path / "wt-gone"
    _git(repo, "worktree", "add", str(wt_gone), "-b", gone)
    (wt_gone / "wip.txt").write_text("wip\n")
    _git(wt_gone, "add", ".")
    _git(wt_gone, "commit", "-m", "wip on branch")
    shutil.rmtree(wt_gone)

    code, out, _ = _gc_delete(repo, capsys, "--force")

    assert code == 0
    assert f"deleted: {merged}" in out
    assert f"deleted: {gone}" in out
    assert "skipped (unmerged, needs --force):" not in out
    assert "deleted: 2, skipped (unmerged): 0, failed: 0" in out
    remaining = sorted(
        _git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
        ).stdout.splitlines()
    )
    assert remaining == []


def test_gc_delete_refuses_without_yes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """gc --delete always requires --yes; refuses without it regardless of context."""
    branch = "auto/merged-20260821T000000Z"
    _git(repo, "branch", branch, "main")

    code = cli.main(
        [
            "gc",
            "--delete",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--older-than",
            "0",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "refusing to delete without --yes" in captured.err
    # Branch must survive
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert branch in remaining


def test_gc_delete_refuses_interactive_without_yes(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even in a TTY, gc --delete without --yes refuses (v1 requires explicit --yes)."""
    branch = "auto/merged-20260821T000000Z"
    _git(repo, "branch", branch, "main")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    code = cli.main(
        [
            "gc",
            "--delete",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--older-than",
            "0",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "refusing to delete without --yes" in captured.err
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert branch in remaining


def test_gc_delete_with_yes_succeeds_non_interactive(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --yes, non-interactive context proceeds and deletes."""
    branch = "auto/merged-20260821T000000Z"
    _git(repo, "branch", branch, "main")

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    code, out, _ = _gc_delete(repo, capsys)

    assert code == 0
    assert f"deleted: {branch}" in out
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert branch not in remaining


def test_gc_delete_is_exactly_dry_run_candidates(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The delete set must equal the dry-run eligible set filtered by the merge guard.

    Issue 043 acceptance 4: this invariant is the clearest signal that the two halves
    of `gc` share one definition of "collectable". A pipeline branch is included on
    purpose — before 043 the delete half dropped every `auto/pipeline-*` row, so this
    test held only because it never created one.
    """
    merged = "auto/merged-20260821T000000Z"
    _git(repo, "branch", merged, "main")

    # Merged, no open PR, nothing in flight (autouse fixture) — collectable, and the
    # exact case the pre-043 delete half silently refused.
    pipeline = "auto/pipeline-20260822T000000Z"
    _git(repo, "branch", pipeline, "main")

    gone = "auto/gone-20260823T000000Z"
    wt_gone = tmp_path / "wt-gone"
    _git(repo, "worktree", "add", str(wt_gone), "-b", gone)
    (wt_gone / "wip.txt").write_text("wip\n")
    _git(wt_gone, "add", ".")
    _git(wt_gone, "commit", "-m", "wip on branch")
    shutil.rmtree(wt_gone)

    # Dry-run: eligible = stale = merged or not worktree_exists
    # merged: stale=yes, gone: stale=yes (no worktree, not merged)
    dry_code, dry_out, _ = _gc(repo, capsys)
    assert dry_code == 0
    dry_eligible = {
        name for name, (wt, mg) in _rows(dry_out).items() if mg == "yes" or wt == "no"
    }

    # Delete with --force: all stale (run first since it consumes branches)
    code_f, out_f, _ = _gc_delete(repo, capsys, "--force")
    assert code_f == 0
    deleted_force = set()
    for line in out_f.splitlines():
        if line.startswith("deleted: ") and "," not in line:
            deleted_force.add(line.removeprefix("deleted: "))
    assert deleted_force == dry_eligible

    # Recreate branches for no-force run
    _git(repo, "branch", merged, "main")
    wt_gone2 = tmp_path / "wt-gone2"
    _git(repo, "worktree", "add", str(wt_gone2), "-b", gone)
    (wt_gone2 / "wip.txt").write_text("wip\n")
    _git(wt_gone2, "add", ".")
    _git(wt_gone2, "commit", "-m", "wip on branch")
    shutil.rmtree(wt_gone2)

    # Delete without --force: only merged-into-base
    code, out, _ = _gc_delete(repo, capsys)
    assert code == 0
    # Per-branch lines look like "deleted: <branch>" (no comma after branch name);
    # the summary line is "deleted: N, skipped..." (has comma).
    deleted_no_force = set()
    for line in out.splitlines():
        if line.startswith("deleted: ") and "," not in line:
            deleted_no_force.add(line.removeprefix("deleted: "))

    rows_dict = _rows(out)
    merged_branches = {name for name, (_, mg) in rows_dict.items() if mg == "yes"}
    assert deleted_no_force == merged_branches


def test_gc_delete_removes_squash_merged_branch_without_force(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue 042 changes what merged_into_base means, not the delete gate itself: a
    squash-merged non-pipeline branch is now truthfully merged, so (same as any other
    merged branch, gate unchanged) it's a no-force delete candidate."""
    branch = "auto/fix-thing-20260901T000000Z"
    _squash_merge(repo, branch, "thing.txt")
    monkeypatch.setattr(gc, "fetch_merged_pr_heads", lambda repo_: {branch: None})

    code, out, _ = _gc_delete(repo, capsys)

    assert code == 0
    assert f"deleted: {branch}" in out
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert branch not in remaining


def test_gc_delete_removes_unretained_pipeline_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 1 (issue 043): a merged pipeline branch with no open PR and nothing
    in flight is deleted like any other merged auto/* branch. Before 043 the delete
    half dropped every `auto/pipeline-*` row outright, so dry-run listed branches
    `--delete` then silently refused to touch."""
    pipeline = "auto/pipeline-nightly-20260824T010000Z"
    real = "auto/real-job-20260820T000000Z"
    _git(repo, "branch", pipeline, "main")
    _git(repo, "branch", real, "main")

    code, out, _ = _gc_delete(repo, capsys)

    assert code == 0
    assert f"deleted: {pipeline}" in out
    assert f"deleted: {real}" in out
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert pipeline not in remaining
    assert real not in remaining


def test_gc_delete_force_retains_unmerged_pipeline_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 2: --force skips the merged requirement, so this is the case that
    must not regress. `pipeline_branch_retained()` filters rows *before* candidates are
    derived, so an unmerged pipeline branch never reaches the force path."""
    pipeline = "auto/pipeline-nightly-20260824T020000Z"
    _git(repo, "checkout", "-b", pipeline)
    (repo / "unmerged.txt").write_text("not on main\n")
    _git(repo, "add", "unmerged.txt")
    _git(repo, "commit", "-m", "work not on main")
    _git(repo, "checkout", "main")

    code, out, _ = _gc_delete(repo, capsys, "--force")

    assert code == 0
    assert f"deleted: {pipeline}" not in out
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert pipeline in remaining


def test_gc_delete_retains_pipeline_branch_with_open_pr(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 3: merged, but a human still has its PR open — retained."""
    pipeline = "auto/pipeline-nightly-20260824T030000Z"
    _git(repo, "branch", pipeline, "main")
    monkeypatch.setattr(gc, "check_open_pr", lambda repo_, branch: branch == pipeline)

    code, out, _ = _gc_delete(repo, capsys, "--force")

    assert code == 0
    assert f"deleted: {pipeline}" not in out
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert pipeline in remaining


def test_gc_delete_needs_no_server(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Delete path must be pure git + filesystem — no HerdrClient, no socket."""
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

    code, out, _ = _gc_delete(repo, capsys)

    assert code == 0
    assert f"deleted: {branch}" in out
    assert commands, "gc should invoke git directly"
    assert all(cmd[0] == "git" for cmd in commands)


def test_gc_delete_branch_and_worktree_both_gone(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """After delete, both the worktree path and the branch ref are gone."""
    merged = "auto/merged-20260821T000000Z"
    _git(repo, "branch", merged, "main")
    wt_path = tmp_path / "wt-merged"
    _git(repo, "worktree", "add", str(wt_path), merged)

    code, out, _ = _gc_delete(repo, capsys)

    assert code == 0
    assert f"deleted: {merged}" in out
    # Worktree path is gone
    assert not wt_path.exists()
    # Branch ref is gone
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert merged not in remaining


def test_gc_delete_aborts_when_branch_listing_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing for-each-ref during delete must abort with a nonzero exit and zero
    deletions, not read as a clean '0 deletion(s) needed.' (spec Risks; PR #49 review)."""
    _git(repo, "branch", "auto/merged-20260821T000000Z", "main")
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

    code, out, err = _gc_delete(repo, capsys)

    assert code != 0
    assert "0 deletion(s) needed." not in out
    assert "deleted: " not in out
    assert "aborting delete with no deletions" in err
    assert "could not list auto/* branches" in err
    # No deletions happened; the surviving branch is untouched
    remaining = sorted(
        _git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
        ).stdout.splitlines()
    )
    assert remaining == ["auto/merged-20260821T000000Z"]


def test_gc_delete_lists_worktrees_once(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Delete mode must reuse the worktree mapping built for eligibility during
    removal — a single `git worktree list` per invocation (spec "Execution ordering
    per branch" step 1; PR #49 review)."""
    merged = "auto/merged-20260821T000000Z"
    _git(repo, "branch", merged, "main")
    wt_path = tmp_path / "wt"
    _git(repo, "worktree", "add", str(wt_path), "-b", "auto/wt-20260821T000000Z")

    real_run_git = gc.run_git
    worktree_list_count = 0

    def counting_run_git(
        repo_path: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        nonlocal worktree_list_count
        if args[:2] == ("worktree", "list"):
            worktree_list_count += 1
        return real_run_git(repo_path, *args)

    monkeypatch.setattr(gc, "run_git", counting_run_git)

    code, out, _ = _gc_delete(repo, capsys, "--force")

    assert code == 0
    assert worktree_list_count == 1
    assert f"deleted: {merged}" in out


# ---------------------------------------------------------------------------
# Issue 044: --older-than age threshold on the merged path
# ---------------------------------------------------------------------------


def test_gc_delete_retains_recently_merged_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 1: a branch merged moments ago (tip commit dated "now", no PR record)
    is withheld under the default 14-day threshold — called with no --older-than
    override, so the CLI's own default gate is what's under test."""
    branch = f"auto/merged-just-now-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    _git(repo, "branch", branch, "main")  # tip == main's seed commit, dated "now"

    code = cli.main(["gc", "--delete", "--yes", "--repo", str(repo), "--base", "main"])
    out = capsys.readouterr().out

    assert code == 0
    assert f"deleted: {branch}" not in out
    assert "0 deletion(s) needed." in out
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert branch in remaining

    # Design requirement (not a separate named acceptance test): the withheld branch
    # stays visible in dry-run with its real age shown, rather than silently vanishing.
    dry_code, dry_out, _ = _gc(repo, capsys, "--older-than", "14")
    assert dry_code == 0
    assert branch in dry_out
    assert "eligible: 0" in dry_out
    row_line = next(line for line in dry_out.splitlines() if line.startswith(branch))
    age_shown = float(row_line.split()[-1])
    assert age_shown < 1


def test_gc_delete_collects_branch_past_age_threshold(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 2: a branch whose tip commit is well past the default 14-day
    threshold, fast-forward merged into main, is collected."""
    branch = "auto/merged-old-20260801T000000Z"
    old_date = "2026-08-01T00:00:00+00:00"
    _git(repo, "checkout", "-b", branch)
    (repo / "old.txt").write_text("old\n")
    _git(repo, "add", "old.txt")
    _git_with_date(repo, old_date, "commit", "-m", "old work")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--ff-only", branch)

    code = cli.main(["gc", "--delete", "--yes", "--repo", str(repo), "--base", "main"])
    out = capsys.readouterr().out

    assert code == 0
    assert f"deleted: {branch}" in out
    remaining = _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
    ).stdout
    assert branch not in remaining


def test_gc_delete_age_threshold_zero_disables(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 3: --older-than 0 collects a just-merged branch regardless of age,
    preserving pre-044 behaviour for a human who has read the table."""
    branch = f"auto/merged-just-now-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    _git(repo, "branch", branch, "main")

    code = cli.main(
        [
            "gc",
            "--delete",
            "--yes",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--older-than",
            "0",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert f"deleted: {branch}" in out


def test_gc_age_prefers_merged_at_over_commit_date(repo: Path) -> None:
    """Acceptance 4: branch_age_days reads the PR's mergedAt when given one, and only
    falls back to the tip's committer date when merged_at is absent — proven by giving
    the same branch an old committer date and a recent mergedAt, and showing the two
    sources disagree by an order of magnitude."""
    branch = "auto/old-commit-recent-merge-20260801T000000Z"
    old_date = "2026-08-01T00:00:00+00:00"
    _git(repo, "checkout", "-b", branch)
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "f.txt")
    _git_with_date(repo, old_date, "commit", "-m", "old work")

    recent_merged_at = "2026-09-05T00:00:00Z"
    now = datetime.fromisoformat(recent_merged_at)

    age_with_pr_record = gc.branch_age_days(repo, branch, recent_merged_at, now=now)
    age_without_pr_record = gc.branch_age_days(repo, branch, None, now=now)

    assert age_with_pr_record < 1
    assert age_without_pr_record > 20


# ---------------------------------------------------------------------------
# Issue 045: orphaned worktree directories and dangling case-variant symlinks
# ---------------------------------------------------------------------------


def test_gc_dry_run_reports_dangling_symlink(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 1: a dangling symlink under the worktree parent dir is reported by
    dry-run, and dry-run touches nothing."""
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    dangling = worktrees_root / "auto-pipeline-20260902T050021Z"
    dangling.symlink_to(
        worktrees_root / "auto-pipeline-20260902t050021z"
    )  # gone target

    code, out, _ = _gc(repo, capsys, "--worktrees-root", str(worktrees_root))

    assert code == 0
    assert "dangling symlink" in out
    assert str(dangling) in out
    assert dangling.is_symlink()


def test_gc_delete_removes_dangling_symlink(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 2: --delete removes a dangling symlink."""
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    dangling = worktrees_root / "auto-pipeline-20260902T050021Z"
    dangling.symlink_to(worktrees_root / "auto-pipeline-20260902t050021z")

    code, out, _ = _gc_delete(repo, capsys, "--worktrees-root", str(worktrees_root))

    assert code == 0
    assert "deleted orphan" in out
    assert not dangling.is_symlink()
    assert not dangling.exists()


def test_gc_delete_removes_empty_orphan_dir(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 3: --delete removes an empty, unregistered auto-* directory."""
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    orphan_dir = worktrees_root / "auto-pipeline-20260826T031438Z"
    orphan_dir.mkdir()

    code, out, _ = _gc_delete(repo, capsys, "--worktrees-root", str(worktrees_root))

    assert code == 0
    assert "deleted orphan" in out
    assert not orphan_dir.exists()


def test_gc_delete_never_removes_nonempty_orphan(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 4: a non-empty unregistered directory is reported but never deleted
    — could be someone's manual checkout or a worktree whose registration was lost."""
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    orphan_dir = worktrees_root / "auto-pipeline-20260826T031438Z"
    orphan_dir.mkdir()
    (orphan_dir / "somebody-elses-work.txt").write_text("do not eat\n")

    code, out, _ = _gc_delete(repo, capsys, "--worktrees-root", str(worktrees_root))

    assert code == 0
    assert "left alone" in out
    assert orphan_dir.exists()
    assert (orphan_dir / "somebody-elses-work.txt").exists()


def test_gc_preserves_live_worktree_alias(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 5: a case-variant symlink whose target is still a registered worktree
    is left completely untouched — not reported, not removed."""
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    branch = "auto/pipeline-20260903T050016Z"
    real_dir = worktrees_root / "auto-pipeline-20260903t050016z"
    _git(repo, "worktree", "add", str(real_dir), "-b", branch)
    (real_dir / "wip.txt").write_text("wip\n")
    _git(real_dir, "add", ".")
    _git(
        real_dir, "commit", "-m", "wip on branch"
    )  # unmerged -> retained, stays registered
    alias = worktrees_root / "auto-pipeline-20260903T050016Z"
    alias.symlink_to(real_dir)

    code, out, _ = _gc_delete(repo, capsys, "--worktrees-root", str(worktrees_root))

    assert code == 0
    assert alias.is_symlink()
    assert alias.resolve() == real_dir.resolve()
    assert "orphan" not in out.lower()


def test_gc_delete_leaves_detached_worktree_alone(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A detached-HEAD worktree emits no `branch` line in `git worktree list
    --porcelain`, so a branch->path mapping alone can't account for it. It is still a
    registered, live worktree — the orphan sweep must not report or touch it."""
    worktrees_root = tmp_path / "worktrees"
    worktrees_root.mkdir()
    detached = worktrees_root / "auto-pipeline-20260904t010101z"
    _git(repo, "worktree", "add", "--detach", str(detached), "main")

    code, out, _ = _gc_delete(repo, capsys, "--worktrees-root", str(worktrees_root))

    assert code == 0
    assert detached.exists()
    assert "orphan" not in out.lower()


def test_gc_delete_review_tiers_present() -> None:
    """Spec v2 review notes: each numbered acceptance line contains Test:, tier, and confidence."""
    import re
    from pathlib import Path

    spec = Path("docs/pipeline/runs/20260831T050020Z/spec.md")
    if not spec.exists():
        spec = (
            Path(__file__).parent.parent / "docs/pipeline/runs/20260831T050020Z/spec.md"
        )
    text = spec.read_text() if spec.exists() else ""
    # Extract only the Acceptance criteria section (between ## Acceptance criteria and next ##).
    m = re.search(
        r"^## Acceptance criteria\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert m, "no Acceptance criteria section found"
    section = m.group(1)
    acceptance_lines = [
        line for line in section.splitlines() if re.match(r"^\d+\.\s", line)
    ]
    assert len(acceptance_lines) >= 1, "no numbered acceptance lines found"
    for line in acceptance_lines:
        lower = line.lower()
        assert "test:" in lower, f"missing Test: in acceptance line: {line[:80]}"
        assert "blocking" in lower or "non-blocking" in lower, (
            f"missing tier in acceptance line: {line[:80]}"
        )
        assert "confidence:" in lower, (
            f"missing confidence: in acceptance line: {line[:80]}"
        )


# ---------------------------------------------------------------------------
# Issue 047: the age gate must not be defeated by a branch that never commits
# ---------------------------------------------------------------------------


def test_branch_age_prefers_run_stamp_over_base_tip(repo: Path) -> None:
    """Acceptance 1: a branch cut from an old base and never committed to takes its age
    from the RUN_ID stamp in its name, not from the base commit it happens to point at.

    This is the shape that broke issue 044 in production: a review job branches, posts a
    review, commits nothing — so the tip IS the base, and the tip date describes when the
    *base* was written.
    """
    _git_with_date(
        repo, "2026-08-21T19:11:06-03:00", "commit", "--allow-empty", "-m", "old base"
    )
    branch = "auto/fitted-pr-review-20260903T120000Z"
    _git(repo, "branch", branch, "HEAD")

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    age = gc.branch_age_days(repo, branch, None, now=now)

    # 2026-09-03T12:00Z -> 2026-09-06T12:00Z is 3 days; the base tip is ~16.
    assert 2.9 < age < 3.1, f"took the base tip date instead of the run stamp: {age}"


def test_branch_ages_differ_for_same_base_commit(repo: Path) -> None:
    """Acceptance 2: two branches cut from the same commit on different days report
    different ages. Measured on the Pi, 43 of 47 branches shared one identical age."""
    _git_with_date(
        repo,
        "2026-08-21T19:11:06-03:00",
        "commit",
        "--allow-empty",
        "-m",
        "shared base",
    )
    older = "auto/fitted-pr-review-20260824T090000Z"
    newer = "auto/fitted-pr-review-20260903T120000Z"
    _git(repo, "branch", older, "HEAD")
    _git(repo, "branch", newer, "HEAD")
    assert (
        _git(repo, "rev-parse", older).stdout == _git(repo, "rev-parse", newer).stdout
    )

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    age_older = gc.branch_age_days(repo, older, None, now=now)
    age_newer = gc.branch_age_days(repo, newer, None, now=now)

    assert age_older > age_newer
    assert age_older - age_newer > 9


def test_recent_noncommit_branch_survives_age_threshold(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 3: the whole point — a recent no-commit branch is retained by the
    default threshold instead of being collected on a fabricated age."""
    _git_with_date(
        repo, "2026-08-21T19:11:06-03:00", "commit", "--allow-empty", "-m", "old base"
    )
    recent = f"auto/fitted-pr-review-{datetime.now(UTC):%Y%m%dT%H%M%S}Z"
    _git(repo, "branch", recent, "HEAD")

    code = cli.main(
        [
            "gc",
            "--delete",
            "--yes",
            "--force",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--older-than",
            "14",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert f"deleted: {recent}" not in out
    assert (
        recent
        in _git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/auto/"
        ).stdout
    )


def test_unknown_age_retains_branch(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 4: an unreadable age retains the branch. A destructive operation must
    fail closed — previously an unknown age returned +inf, which cleared any threshold."""
    _git(repo, "commit", "--allow-empty", "-m", "base")
    branch = "auto/no-stamp-here"  # no RUN_ID to parse
    _git(repo, "branch", branch, "HEAD")
    monkey = gc._tip_committer_date
    try:
        gc._tip_committer_date = lambda repo_, branch_: None  # type: ignore[assignment]
        age = gc.branch_age_days(repo, branch, None)
        assert age == float("-inf")
        row = gc.Row(
            branch=branch, worktree_exists=False, merged_into_base=True, age_days=age
        )
        assert gc.collectible(row, 14) is False
    finally:
        gc._tip_committer_date = monkey  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Issue 048: the orphan scan must follow --repo, never a hardcoded project dir
# ---------------------------------------------------------------------------


def _repo_with_worktree(repo: Path, root: Path, name: str) -> Path:
    """Register a worktree of *repo* under *root*, so root becomes its derived home."""
    root.mkdir(parents=True, exist_ok=True)
    wt = root / name
    _git(repo, "worktree", "add", str(wt), "-b", f"auto/{name}")
    return wt


def test_gc_orphan_scan_follows_target_repo(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 1: the sweep scans the directory holding *this* repo's worktrees,
    derived from its own registrations — not a constant."""
    _git(repo, "commit", "--allow-empty", "-m", "base")
    mine = tmp_path / "worktrees" / "myrepo"
    _repo_with_worktree(repo, mine, "live-20260901T000000Z")
    (mine / "auto-orphan-20260101T000000Z").symlink_to(tmp_path / "gone")

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert "auto-orphan-20260101T000000Z" in out
    assert "dangling symlink" in out


def test_gc_never_reports_other_repo_worktree_as_orphan(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance 2: a live worktree belonging to a different project is never listed.

    Reproduces the production condition precisely: the hardcoded default is pointed at
    the *other* project's directory, exactly as `_default_pipeline_worktrees_root()`
    did for every repo that was not herdr-routines. Without pinning the default this
    test passes for the wrong reason — the real default simply does not exist under
    tmp_path — so it would not have caught the bug it is named for.
    """
    _git(repo, "commit", "--allow-empty", "-m", "base")
    mine = tmp_path / "worktrees" / "myrepo"
    _repo_with_worktree(repo, mine, "live-20260901T000000Z")

    other = tmp_path / "worktrees" / "someone-else"
    other.mkdir(parents=True)
    # An empty dir and a dangling symlink are both *collectible* — under the bug these
    # would not merely be listed, they would be deleted out of another project.
    (other / "auto-pipeline-20260903t050016z").mkdir()
    (other / "auto-pipeline-19990101T000000Z").symlink_to(tmp_path / "gone")
    monkeypatch.setattr(gc, "_default_pipeline_worktrees_root", lambda: other)

    code, out, _ = _gc(repo, capsys)

    assert code == 0
    assert "someone-else" not in out
    assert "auto-pipeline-20260903t050016z" not in out
    assert "auto-pipeline-19990101T000000Z" not in out
    # And nothing in the other project was touched.
    assert (other / "auto-pipeline-20260903t050016z").is_dir()
    assert (other / "auto-pipeline-19990101T000000Z").is_symlink()


def test_gc_skips_orphan_sweep_on_unrelated_root(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 3: a root holding none of this repo's worktrees is skipped with a
    warning, not swept. Listing (or deleting) another project's entries is the bug."""
    _git(repo, "commit", "--allow-empty", "-m", "base")
    mine = tmp_path / "worktrees" / "myrepo"
    _repo_with_worktree(repo, mine, "live-20260901T000000Z")

    unrelated = tmp_path / "worktrees" / "unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "auto-victim-20260101T000000Z").symlink_to(tmp_path / "gone")

    code = cli.main(
        [
            "gc",
            "--dry-run",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--worktrees-root",
            str(unrelated),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "skipping orphan sweep" in captured.err
    assert "auto-victim-20260101T000000Z" not in captured.out
    assert (unrelated / "auto-victim-20260101T000000Z").is_symlink()


def test_gc_worktrees_root_override_respected(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 4: an explicit --worktrees-root still wins when it does hold this
    repo's worktrees — the override must survive the new derivation."""
    _git(repo, "commit", "--allow-empty", "-m", "base")
    mine = tmp_path / "worktrees" / "myrepo"
    _repo_with_worktree(repo, mine, "live-20260901T000000Z")
    (mine / "auto-orphan-20260101T000000Z").symlink_to(tmp_path / "gone")

    code = cli.main(
        [
            "gc",
            "--dry-run",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--worktrees-root",
            str(mine),
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "auto-orphan-20260101T000000Z" in out
