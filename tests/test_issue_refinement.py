"""Tests for issue refinement job (issue 029)."""

from __future__ import annotations

from pathlib import Path

import yaml

from herdr_routines.issue_refinement import (
    ISSUE_REFINEMENT_CRON,
    MAX_ITERATIONS,
    ParkingLotBullet,
    head_is_issue_refinement,
    head_is_pipeline,
    is_covered,
    job_is_opencode_only,
    loop_should_continue,
    parse_parking_lot_bullets,
    select_next_bullet,
    validate_issue_frontmatter,
)


def test_issue_refinement_selection_picks_oldest_uncovered() -> None:
    roadmap = """# ROADMAP
## Parking lot
- **Review @me PRs across repos** — idea, not designed. Scan open PRs.
- **Audit skills as report→diff gate jobs** — idea, not designed. Turn audit skills.
- **Release/update strategy** — blocked: needs decision.
"""
    bullets = parse_parking_lot_bullets(roadmap)
    assert len(bullets) == 3
    # First bullet not covered => picks it
    picked = select_next_bullet(bullets, [])
    assert picked is not None
    assert "Review @me PRs" in picked.raw
    # If first is covered, picks second
    picked2 = select_next_bullet(bullets, ["Review @me PRs across repos"])
    assert picked2 is not None
    assert "Audit skills" in picked2.raw
    # Blocked skipped by default
    bullets_blocked = parse_parking_lot_bullets(
        "## Parking lot\n- **Foo** — blocked: needs decision\n- **Bar** — idea\n"
    )
    picked3 = select_next_bullet(bullets_blocked, [])
    assert picked3 is not None
    assert "Bar" in picked3.raw
    # With allow_blocked, picks blocked
    picked4 = select_next_bullet(bullets_blocked, [], allow_blocked=True)
    assert picked4 is not None
    assert "Foo" in picked4.raw


def test_issue_refinement_loop_caps_at_three() -> None:
    # Consensus stops immediately
    assert loop_should_continue(1, "high") is False
    assert loop_should_continue(2, "high") is False
    # Non-high continues until cap
    assert loop_should_continue(1, "medium") is True
    assert loop_should_continue(2, "medium") is True
    assert loop_should_continue(3, "medium") is False
    assert MAX_ITERATIONS == 3
    # Case-insensitive
    assert loop_should_continue(1, "High") is False


def test_issue_refinement_pr_contains_valid_issue_file(tmp_path: Path) -> None:
    # Frontmatter validation for refined issue
    good = {
        "id": "999",
        "title": "Foo",
        "status": "open",
        "priority": "medium",
        "area": "cli",
    }
    assert validate_issue_frontmatter(good) == []
    missing = {"id": "999", "title": "Foo"}
    errs = validate_issue_frontmatter(missing)
    assert "status" in errs
    # Status must be open for new issue
    bad_status = {
        "id": "999",
        "title": "Foo",
        "status": "done",
        "priority": "medium",
        "area": "cli",
    }
    errs2 = validate_issue_frontmatter(bad_status)
    assert any("status" in e for e in errs2)
    # Head prefix check
    assert head_is_issue_refinement("auto/issue-refinement-20260907t220000z")
    assert not head_is_pipeline("auto/issue-refinement-20260907t220000z")


def test_issue_refinement_job_is_opencode_only() -> None:
    # Real job file must be opencode only, no claude, correct cron
    job_path = (
        Path(__file__).parent.parent / "deploy" / "jobs.d" / "issue-refinement.yaml"
    )
    data = yaml.safe_load(job_path.read_text())
    assert data["cron"] == ISSUE_REFINEMENT_CRON
    assert data["cron"] == "0 22 * * *"
    assert job_is_opencode_only(data) is True
    assert data["agent_kind"] == "opencode"
    assert "claude" not in str(data.get("model") or "").lower()
    # Negative: claude should fail
    assert job_is_opencode_only({"agent_kind": "claude", "model": None}) is False
    assert (
        job_is_opencode_only({"agent_kind": "opencode", "model": "claude-sonnet"})
        is False
    )


def test_issue_refinement_no_pipeline_overlap() -> None:
    # Head for this job must not be pipeline, and pipeline guard must not match it
    assert not head_is_pipeline("auto/issue-refinement-20260907t220000z")
    assert head_is_pipeline("auto/pipeline-20260907T050000Z")
    # is_covered helper sanity
    b = ParkingLotBullet(
        raw="Review @me PRs across repos — idea", title="Review", is_blocked=False
    )
    assert is_covered(b, ["Review @me PRs across repos"]) is True
    assert is_covered(b, ["Something else"]) is False
