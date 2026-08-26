"""Tests for `herdr-routines pick-feature` (docs/process/README.md convention)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from herdr_routines.pick_feature import (
    Issue,
    IssueParseError,
    load_issues,
    mark_in_progress,
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


def test_mark_in_progress_flips_status_atomically(issues_dir: Path) -> None:
    path = _write_issue(issues_dir, "001-foo.md", id="001", status="open")
    issue = parse_issue(path)
    mark_in_progress(issue)
    reparsed = parse_issue(path)
    assert reparsed.status == "in-progress"
    # No leftover tmp file after the atomic rename.
    assert not path.with_suffix(".md.tmp").exists()


def test_mark_in_progress_ambiguous_status_line_raises(issues_dir: Path) -> None:
    path = _write_issue(issues_dir, "001-foo.md", id="001", status="open")
    # Sneak in a second identical status line inside the body to force ambiguity.
    path.write_text(path.read_text() + "\nstatus: open\n")
    issue = parse_issue(path)
    with pytest.raises(IssueParseError, match="expected exactly one"):
        mark_in_progress(issue)


def test_run_pick_feature_writes_feature_idea_for_open_issue(issues_dir: Path) -> None:
    _write_issue(issues_dir, "001-foo.md", id="001", title="Foo", status="open")
    out = io.StringIO()
    code = run_pick_feature(issues_dir, out=out)
    assert code == 0
    assert "Foo" in out.getvalue()


def test_run_pick_feature_mark_in_progress_updates_file(issues_dir: Path) -> None:
    path = _write_issue(issues_dir, "001-foo.md", id="001", status="open")
    out = io.StringIO()
    code = run_pick_feature(issues_dir, mark=True, out=out)
    assert code == 0
    assert parse_issue(path).status == "in-progress"


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
