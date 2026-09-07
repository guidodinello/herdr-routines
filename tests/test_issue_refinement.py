"""Tests for the issue-refinement job (issue 029).

The five acceptance tests named in
``docs/pipeline/runs/20260907T050000Z/spec.md`` each exercise a real path:
selection + the "already refined" guard, the loop cap, frontmatter validation,
the job file through the real config validator, and pipeline independence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from herdr_routines.config import ConfigError, _build_job
from herdr_routines.issue_refinement import (
    ISSUE_REFINEMENT_CRON,
    MAX_ITERATIONS,
    PR_MARKER_PREFIX,
    is_covered,
    job_is_opencode_only,
    loop_should_continue,
    parse_parking_lot_bullets,
    run_refine_issue,
    select_next_bullet,
    validate_issue_frontmatter,
)

ROADMAP = """# ROADMAP

## Parking lot

Intro prose that must not be parsed as a bullet.

- **Switch provider/model on quota exhaustion** — `done` (PR #65). Per-job
  fallback. → [`022`](docs/process/issues/022-switch-model.md)
- **Review `@me` PRs across repos** — idea, not designed. Scan open PRs.
  Gate: the jobs refactor (issue 006). 2026-08-30 brainstorm.
- **Unify routines + pipeline** — idea, not designed. One gated-workflow engine
  spanning both scales. 2026-08-30 investigation.
- **Audit skills as gate jobs** — idea, not designed. Turn audit skills into
  scheduled report→diff jobs. Gate: 025 design merged.

House rule: deferred work gets a bullet here.
"""


def test_issue_refinement_selection_picks_oldest_uncovered() -> None:
    bullets = parse_parking_lot_bullets(ROADMAP)
    titles = [b.title for b in bullets]
    assert titles == [
        "Switch provider/model on quota exhaustion",
        "Review `@me` PRs across repos",
        "Unify routines + pipeline",
        "Audit skills as gate jobs",
    ]

    # #1 is already promoted (links an issue file); #2 and #4 are gated. So the
    # oldest *eligible* bullet is #3.
    picked = select_next_bullet(bullets, [], frozenset())
    assert picked is not None and picked.title == "Unify routines + pipeline"

    # #3 covered by an existing issue file → nothing eligible (#4 stays gated).
    assert (
        select_next_bullet(bullets, ["Unify routines + pipeline"], frozenset()) is None
    )

    # An open refinement PR for #3's slug also covers it.
    assert select_next_bullet(bullets, [], frozenset({picked.slug})) is None

    # --allow-blocked falls back to the oldest gated bullet (#2).
    blocked_pick = select_next_bullet(bullets, [], frozenset(), allow_blocked=True)
    assert blocked_pick is not None
    assert blocked_pick.title == "Review `@me` PRs across repos"


def test_issue_refinement_loop_caps_at_three() -> None:
    assert MAX_ITERATIONS == 3
    # confidence: high → consensus, stop immediately (any pass).
    assert loop_should_continue(1, "high") is False
    assert loop_should_continue(2, "High") is False
    # otherwise continue until the third completed pass.
    assert loop_should_continue(1, "medium") is True
    assert loop_should_continue(2, "low") is True
    assert loop_should_continue(3, "medium") is False


def test_issue_refinement_pr_contains_valid_issue_file() -> None:
    good = {
        "id": "999",
        "title": "Foo",
        "status": "open",
        "priority": "medium",
        "area": "cli",
    }
    assert validate_issue_frontmatter(good) == []

    assert set(validate_issue_frontmatter({"id": "999", "title": "Foo"})) >= {
        "status",
        "priority",
        "area",
    }

    # An explicit null is missing, not valid.
    assert "status" in validate_issue_frontmatter({**good, "status": None})

    # A new issue must be status: open, never done/in-progress.
    errs = validate_issue_frontmatter({**good, "status": "done"})
    assert errs and all("open" in e for e in errs)


def test_issue_refinement_job_is_opencode_only() -> None:
    job_path = (
        Path(__file__).parent.parent / "deploy" / "jobs.d" / "issue-refinement.yaml"
    )
    data = yaml.safe_load(job_path.read_text())

    assert data["cron"] == ISSUE_REFINEMENT_CRON == "0 22 * * *"
    assert job_is_opencode_only(data) is True
    assert "claude" not in data["prompt"].lower()

    # The real config validator accepts the file (schema, cron, agent_kind, …).
    job = _build_job({**data, "name": "issue-refinement"}, {}, index=0)
    assert job.agent_kind == "opencode"
    assert job.kind == "routine"
    assert job.model is not None and "claude" not in job.model.lower()

    # Negative: a Claude variant is rejected by the helper.
    assert job_is_opencode_only({"agent_kind": "claude", "model": None}) is False
    assert (
        job_is_opencode_only({"agent_kind": "opencode", "model": "anthropic/claude"})
        is False
    )
    with pytest.raises(ConfigError):
        _build_job(
            {**data, "name": "issue-refinement", "cron": "not-a-cron"}, {}, index=0
        )


def test_issue_refinement_no_pipeline_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / "docs" / "process" / "issues").mkdir(parents=True)
    (repo / "ROADMAP.md").write_text(ROADMAP)

    import herdr_routines.issue_refinement as mod

    # refinement_open_pr_slugs already filters to auto/issue-refinement-* heads, so
    # an open auto/pipeline-* PR for the same slug simply never reaches the guard
    # (029 acceptance: zero pipeline interaction). Model that as "no slugs".
    monkeypatch.setattr(mod, "refinement_open_pr_slugs", lambda *_a: frozenset())
    rc = run_refine_issue(repo)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Unify routines + pipeline" in out
    assert f"{PR_MARKER_PREFIX} unify-routines-pipeline" in out

    # A real refinement PR for that slug — the guard covers it, nothing left.
    monkeypatch.setattr(
        mod,
        "refinement_open_pr_slugs",
        lambda *_a: frozenset({"unify-routines-pipeline"}),
    )
    assert run_refine_issue(repo) == 1

    # gh failure → fail safe (exit 1, no pick), never fall through to a pick.
    monkeypatch.setattr(mod, "refinement_open_pr_slugs", lambda *_a: None)
    assert run_refine_issue(repo) == 1


def test_is_covered_matches_slug_and_issue_title() -> None:
    bullet = parse_parking_lot_bullets(ROADMAP)[2]  # "Unify routines + pipeline"
    assert is_covered(bullet, [], frozenset({bullet.slug})) is True
    assert is_covered(bullet, ["Unify routines + pipeline"], frozenset()) is True
    assert is_covered(bullet, ["Something unrelated"], frozenset()) is False
