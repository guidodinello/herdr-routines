"""Tests for `herdr-routines pick-feature` (docs/process/README.md convention)."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_routines import pick_feature as pick_feature_module
from herdr_routines.claims import claim_issue, load_claims
from herdr_routines.pick_feature import (
    Issue,
    IssueParseError,
    ReclaimedPick,
    load_issues,
    parse_issue,
    render_feature_idea,
    run_pick_feature,
    select_next,
)

ISSUE_TEMPLATE = """---
id: "{id}"
title: {title}
status: {status}
priority: {priority}
area: cli
---

## Description

{body}
"""


def _write_issue(
    issues_dir: Path,
    filename: str,
    *,
    id: str,
    title: str = "Some issue",
    status: str = "open",
    priority: str = "medium",
    body: str = "Do the thing.",
) -> Path:
    path = issues_dir / filename
    path.write_text(
        ISSUE_TEMPLATE.format(
            id=id, title=title, status=status, priority=priority, body=body
        )
    )
    return path


@pytest.fixture
def issues_dir(tmp_path: Path) -> Path:
    d = tmp_path / "issues"
    d.mkdir()
    return d


def test_parse_issue_reads_frontmatter_and_body(issues_dir: Path) -> None:
    path = _write_issue(
        issues_dir, "001-foo.md", id="001", title="Foo", body="Fix the foo bug."
    )
    issue = parse_issue(path)
    assert issue == Issue(
        id="001",
        title="Foo",
        status="open",
        priority="medium",
        area="cli",
        path=path,
        body="## Description\n\nFix the foo bug.",
    )


def test_parse_issue_missing_delimiter_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("no frontmatter here\n")
    with pytest.raises(IssueParseError, match="missing frontmatter delimiter"):
        parse_issue(path)


def test_parse_issue_unterminated_frontmatter_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text('---\nid: "001"\n')
    with pytest.raises(IssueParseError, match="unterminated frontmatter"):
        parse_issue(path)


def test_parse_issue_missing_required_field_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text('---\nid: "001"\ntitle: Foo\n---\n\nbody\n')
    with pytest.raises(IssueParseError, match="missing required field"):
        parse_issue(path)


def test_load_issues_sorted_by_filename(issues_dir: Path) -> None:
    _write_issue(issues_dir, "002-b.md", id="002", title="B")
    _write_issue(issues_dir, "001-a.md", id="001", title="A")
    issues = load_issues(issues_dir)
    assert [i.id for i in issues] == ["001", "002"]


def test_load_issues_empty_dir_raises(tmp_path: Path) -> None:
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(IssueParseError, match="no issue files found"):
        load_issues(d)


def test_select_next_prefers_higher_priority(issues_dir: Path) -> None:
    _write_issue(issues_dir, "001-low.md", id="001", priority="low")
    _write_issue(issues_dir, "002-high.md", id="002", priority="high")
    issues = load_issues(issues_dir)
    picked = select_next(issues)
    assert picked is not None
    assert picked.id == "002"


def test_select_next_prefers_lower_id_within_same_priority(issues_dir: Path) -> None:
    _write_issue(issues_dir, "002-b.md", id="002", priority="medium")
    _write_issue(issues_dir, "001-a.md", id="001", priority="medium")
    issues = load_issues(issues_dir)
    picked = select_next(issues)
    assert picked is not None
    assert picked.id == "001"


def test_select_next_skips_non_open_status(issues_dir: Path) -> None:
    _write_issue(issues_dir, "001-done.md", id="001", status="done", priority="high")
    _write_issue(issues_dir, "002-open.md", id="002", status="open", priority="low")
    issues = load_issues(issues_dir)
    picked = select_next(issues)
    assert picked is not None
    assert picked.id == "002"


def test_select_next_returns_none_when_nothing_open(issues_dir: Path) -> None:
    _write_issue(issues_dir, "001-done.md", id="001", status="done")
    issues = load_issues(issues_dir)
    assert select_next(issues) is None


def test_render_feature_idea_includes_title_id_path_and_body(issues_dir: Path) -> None:
    path = _write_issue(
        issues_dir, "001-foo.md", id="001", title="Foo thing", body="Fix the foo bug."
    )
    issue = parse_issue(path)
    text = render_feature_idea(issue)
    assert "Foo thing" in text
    assert "issue 001" in text
    assert str(path.as_posix()) in text
    assert "Fix the foo bug." in text


def test_run_pick_feature_writes_feature_idea_for_open_issue(issues_dir: Path) -> None:
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out)
    assert code == 0
    assert "Foo" in out.getvalue()


def test_pick_feature_leaves_parent_clone_clean(tmp_path: Path) -> None:
    """Acceptance criterion 1 (issue 041): `--mark-in-progress` must not write to
    the issue file — that edit collided with the implementing PR's own edit to
    the same line and wedged `sync-repo`. The claim goes to an out-of-tree store
    instead, so a real git checkout of `docs/process/issues/` has no uncommitted
    changes after the pick (a byte-equality check on the file alone would miss an
    untracked leftover, e.g. a stray tmp file or an in-repo claims file)."""
    repo = tmp_path / "repo"
    issues_dir = repo / "docs" / "process" / "issues"
    issues_dir.mkdir(parents=True)
    _write_issue(issues_dir, "001-foo.md", id="001", status="open")
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True
    )

    out = io.StringIO()
    claims_path = tmp_path / "state" / "claims.json"
    code = run_pick_feature(issues_dir, mark=True, out=out, claims_path=claims_path)
    assert code == 0

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
    assert parse_issue(issues_dir / "001-foo.md").status == "open"


def test_claimed_issue_not_repicked(issues_dir: Path, tmp_path: Path) -> None:
    """Acceptance criterion 3: a claim (recorded out-of-tree, not as a status
    change) still keeps the next pick from re-selecting the same issue."""
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    _write_issue(issues_dir, "002-bar.md", id="002", title="Bar", status="open")
    claims_path = tmp_path / "claims.json"

    first = io.StringIO()
    code = run_pick_feature(issues_dir, mark=True, out=first, claims_path=claims_path)
    assert code == 0
    assert "Foo" in first.getvalue()

    second = io.StringIO()
    code = run_pick_feature(issues_dir, mark=True, out=second, claims_path=claims_path)
    assert code == 0
    assert "Bar" in second.getvalue()
    assert "Foo" not in second.getvalue()


def test_claimed_issue_skipped_without_marking_again(
    issues_dir: Path, tmp_path: Path
) -> None:
    """A pre-existing claim (e.g. from a previous run) is honored even on an
    unmarked pick, not just immediately after the claiming call."""
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    _write_issue(issues_dir, "002-bar.md", id="002", title="Bar", status="open")
    claims_path = tmp_path / "claims.json"
    claim_issue(claims_path, "001")

    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out, claims_path=claims_path)
    assert code == 0
    assert "Bar" in out.getvalue()
    assert load_claims(claims_path).keys() == {"001"}


def test_run_pick_feature_no_open_issues_fails(issues_dir: Path) -> None:
    _write_issue(issues_dir, "001-foo.md", id="001", status="done")
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out)
    assert code == 1
    assert out.getvalue() == ""


def test_run_pick_feature_missing_dir_fails(tmp_path: Path) -> None:
    out = io.StringIO()
    code = run_pick_feature(tmp_path / "nope", out=out)
    assert code == 1
    assert out.getvalue() == ""


# --- Issue 040: stale-lease reclamation --------------------------------------------


def _stale_now(claimed_at: datetime, *, lease_hours: float = 12.0) -> datetime:
    """A `now` comfortably past `claimed_at + lease_hours`."""
    return claimed_at + timedelta(hours=lease_hours, minutes=1)


def test_pick_feature_reclaims_stale_in_progress(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 1: a claim past its lease with no open PR and no
    in-flight run is released, and the issue is picked again."""
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    claims_path = tmp_path / "claims.json"
    claimed_at = datetime(2026, 9, 1, tzinfo=UTC)
    claim_issue(claims_path, "001", now=claimed_at)
    monkeypatch.setattr(
        pick_feature_module, "open_pr_issue_ids", lambda repo: frozenset()
    )

    out = io.StringIO()
    code = run_pick_feature(
        issues_dir,
        mark=True,
        out=out,
        claims_path=claims_path,
        repo=tmp_path,
        worktrees_root=tmp_path / "worktrees",
        now=_stale_now(claimed_at),
    )
    assert code == 0
    assert "Foo" in out.getvalue()
    # Reclaimed then immediately re-claimed by this same pick.
    assert load_claims(claims_path).keys() == {"001"}


def test_pick_feature_skips_in_progress_with_open_pr(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 2: a claim past its lease is NOT released while an open
    PR still references its issue."""
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    _write_issue(issues_dir, "002-bar.md", id="002", title="Bar", status="open")
    claims_path = tmp_path / "claims.json"
    claimed_at = datetime(2026, 9, 1, tzinfo=UTC)
    claim_issue(claims_path, "001", now=claimed_at)
    monkeypatch.setattr(
        pick_feature_module, "open_pr_issue_ids", lambda repo: frozenset({1})
    )

    out = io.StringIO()
    code = run_pick_feature(
        issues_dir,
        mark=True,
        out=out,
        claims_path=claims_path,
        repo=tmp_path,
        worktrees_root=tmp_path / "worktrees",
        now=_stale_now(claimed_at),
    )
    assert code == 0
    assert "Bar" in out.getvalue()
    assert "Foo" not in out.getvalue()
    assert load_claims(claims_path).keys() == {"001", "002"}


def test_pick_feature_skips_in_progress_inflight_run(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 3: a claim past its lease is NOT released while its issue
    belongs to an in-flight pipeline run (a `state.json` with no terminal report)."""
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    _write_issue(issues_dir, "002-bar.md", id="002", title="Bar", status="open")
    claims_path = tmp_path / "claims.json"
    claimed_at = datetime(2026, 9, 1, tzinfo=UTC)
    claim_issue(claims_path, "001", now=claimed_at)
    monkeypatch.setattr(
        pick_feature_module, "open_pr_issue_ids", lambda repo: frozenset()
    )

    worktrees_root = tmp_path / "worktrees"
    run_dir = worktrees_root / "auto-pipeline-20260901T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "20260901T000000Z",
                "feature_source": "docs/process/issues/001-foo.md",
                "branch": "auto/pipeline-20260901T000000Z",
            }
        )
    )
    reports_dir = tmp_path / "reports"

    out = io.StringIO()
    code = run_pick_feature(
        issues_dir,
        mark=True,
        out=out,
        claims_path=claims_path,
        repo=tmp_path,
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
        now=_stale_now(claimed_at),
    )
    assert code == 0
    assert "Bar" in out.getvalue()
    assert "Foo" not in out.getvalue()
    assert load_claims(claims_path).keys() == {"001", "002"}


def test_reclaimed_pick_is_surfaced(
    issues_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance criterion 4: releasing a stale claim is reported on stderr and
    handed to an injected notifier — never silent."""
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    claims_path = tmp_path / "claims.json"
    claimed_at = datetime(2026, 9, 1, tzinfo=UTC)
    claim_issue(claims_path, "001", now=claimed_at)
    monkeypatch.setattr(
        pick_feature_module, "open_pr_issue_ids", lambda repo: frozenset()
    )

    notified: list[ReclaimedPick] = []
    out = io.StringIO()
    code = run_pick_feature(
        issues_dir,
        mark=True,
        out=out,
        claims_path=claims_path,
        repo=tmp_path,
        worktrees_root=tmp_path / "worktrees",
        now=_stale_now(claimed_at),
        notify=notified.append,
    )
    assert code == 0
    assert len(notified) == 1
    assert notified[0].issue_id == "001"
    assert notified[0].claimed_at == claimed_at

    err = capsys.readouterr().err
    assert "reclaimed stale claim: issue 001" in err
    assert "no open PR, no in-flight run" in err


# --- Issue 028: pipeline open-PR exclusion (re-scoped) -----------------------


def test_pick_feature_skips_issue_with_open_pipeline_pr(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An issue with an open auto/pipeline-* PR is not picked, even when claims.py holds no claim."""
    _write_issue(
        issues_dir,
        "028-foo.md",
        id="028",
        title="Foo",
        status="open",
        priority="medium",
    )
    _write_issue(
        issues_dir,
        "029-bar.md",
        id="029",
        title="Bar",
        status="open",
        priority="medium",
    )
    claims_path = tmp_path / "claims.json"
    monkeypatch.setattr(
        pick_feature_module,
        "pipeline_open_pr_issue_ids",
        lambda repo, worktrees_root=None, reports_dir=None: frozenset({28}),
    )
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out, claims_path=claims_path, repo=tmp_path)
    assert code == 0
    assert "Bar" in out.getvalue()
    assert "Foo" not in out.getvalue()


def test_open_pr_exclusion_needs_no_flag(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusion is unconditional — no flag is required to enable it."""
    _write_issue(
        issues_dir,
        "028-foo.md",
        id="028",
        title="Foo",
        status="open",
        priority="medium",
    )
    _write_issue(
        issues_dir, "029-bar.md", id="029", title="Bar", status="open", priority="low"
    )
    claims_path = tmp_path / "claims.json"
    monkeypatch.setattr(
        pick_feature_module,
        "pipeline_open_pr_issue_ids",
        lambda repo, worktrees_root=None, reports_dir=None: frozenset({28}),
    )
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out, claims_path=claims_path, repo=tmp_path)
    assert code == 0
    assert "Bar" in out.getvalue()
    assert "Foo" not in out.getvalue()


def test_non_pipeline_pr_does_not_exclude(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-pipeline open PR never excludes an issue."""
    _write_issue(issues_dir, "028-foo.md", id="028", title="Foo", status="open")
    claims_path = tmp_path / "claims.json"
    monkeypatch.setattr(
        pick_feature_module,
        "pipeline_open_pr_issue_ids",
        lambda repo, worktrees_root=None, reports_dir=None: frozenset(),
    )
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out, claims_path=claims_path, repo=tmp_path)
    assert code == 0
    assert "Foo" in out.getvalue()


def test_pick_feature_fails_open_on_gh_error(
    issues_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A gh or git-remote failure warns and picks anyway (fail-open, exit 0)."""
    _write_issue(issues_dir, "028-foo.md", id="028", title="Foo", status="open")
    claims_path = tmp_path / "claims.json"

    def fake_fail(*args, **kwargs):
        print(
            "warning: gh pr list failed: simulated failure",
            file=__import__("sys").stderr,
        )

    monkeypatch.setattr(pick_feature_module, "pipeline_open_pr_issue_ids", fake_fail)
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out, claims_path=claims_path, repo=tmp_path)
    assert code == 0
    assert "Foo" in out.getvalue()
    err = capsys.readouterr().err
    assert "warning" in err.lower()


def test_open_pr_lookup_is_batched(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open-PR lookup is a single batched call, not one per issue."""
    for i in range(5):
        _write_issue(
            issues_dir,
            f"0{i + 1:02d}-x.md",
            id=f"0{i + 1:02d}",
            title=f"T{i}",
            status="open",
        )
    claims_path = tmp_path / "claims.json"
    call_count = 0

    def counting(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return frozenset()

    monkeypatch.setattr(pick_feature_module, "pipeline_open_pr_issue_ids", counting)
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out, claims_path=claims_path, repo=tmp_path)
    assert code == 0
    assert call_count == 1


def test_select_next_with_pipeline_ids(issues_dir: Path) -> None:
    """select_next correctly excludes pipeline PR ids alongside claimed ids."""
    _write_issue(issues_dir, "028-foo.md", id="028", priority="medium")
    _write_issue(issues_dir, "029-bar.md", id="029", priority="medium")
    issues = load_issues(issues_dir)
    picked = select_next(issues, frozenset(), frozenset({28}))
    assert picked is not None
    assert picked.id == "029"


def test_pipeline_structural_derivation(
    issues_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline PR with state.json feature_source derives issue id structurally even with empty body."""
    _write_issue(issues_dir, "028-foo.md", id="028", title="Foo", status="open")
    _write_issue(issues_dir, "029-bar.md", id="029", title="Bar", status="open")
    claims_path = tmp_path / "claims.json"
    worktrees_root = tmp_path / "worktrees"
    run_id = "20260906T050000Z"
    state_dir = worktrees_root / f"auto-pipeline-{run_id.lower()}"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "feature_source": "docs/process/issues/028-foo.md",
                "branch": f"auto/pipeline-{run_id}",
            }
        )
    )
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    fake_prs = [
        {
            "headRefName": f"auto/pipeline-{run_id}",
            "number": 1,
            "title": "feat",
            "body": "",
        }
    ]
    # Let the real pipeline_open_pr_issue_ids run against the fake gh output by mocking subprocess.run
    original_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if "pr" in cmd and "list" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(fake_prs), stderr=""
            )
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(pick_feature_module.subprocess, "run", fake_run)
    out = io.StringIO()
    code = run_pick_feature(
        issues_dir,
        out=out,
        claims_path=claims_path,
        repo=tmp_path,
        worktrees_root=worktrees_root,
        reports_dir=reports_dir,
    )
    assert code == 0
    assert "Bar" in out.getvalue()
    assert "Foo" not in out.getvalue()
