"""Tests for `herdr-routines pick-feature` (docs/process/README.md convention)."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from herdr_routines.claims import claim_issue, load_claims
from herdr_routines.pick_feature import (
    Issue,
    IssueParseError,
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
