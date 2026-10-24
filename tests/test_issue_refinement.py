"""Tests for the issue-refinement job (issue 029).

The five acceptance tests named in
``docs/pipeline/runs/20260907T050000Z/spec.md`` each exercise a real path:
selection + the "already refined" guard, the loop cap, frontmatter validation,
the job file through the real config validator, and pipeline independence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from herdr_routines import cli
from herdr_routines.config import ConfigError, _build_job
from herdr_routines.issue_refinement import (
    ISSUE_REFINEMENT_CRON,
    MAX_ITERATIONS,
    PR_MARKER_PREFIX,
    is_covered,
    job_is_opencode_only,
    loop_should_continue,
    parse_parking_lot_bullets,
    parse_reviewer_confidence,
    refinement_open_pr_slugs,
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
    # A Claude quota fallback is Claude too — the model a fallback run actually uses.
    assert (
        job_is_opencode_only(
            {
                "agent_kind": "opencode",
                "model": "opencode/big-pickle",
                "fallback_model": "anthropic/claude-sonnet",
            }
        )
        is False
    )
    with pytest.raises(ConfigError):
        _build_job(
            {**data, "name": "issue-refinement", "cron": "not-a-cron"}, {}, index=0
        )


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_refinement_open_pr_slugs_filters_to_refinement_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prs = [
        # A pipeline PR carrying the marker MUST NOT count (029: zero pipeline
        # interaction — the head filter is what keeps it out).
        {
            "headRefName": "auto/pipeline-20260907T050000Z",
            "isCrossRepository": False,
            "title": "feat: unify routines + pipeline",
            "body": f"{PR_MARKER_PREFIX} unify-routines-pipeline",
        },
        # A refinement PR with the marker → slug taken from the marker.
        {
            "headRefName": "auto/issue-refinement-20260908T220000Z",
            "isCrossRepository": False,
            "title": "docs: refine issue 050 — audit skills",
            "body": f"body\n{PR_MARKER_PREFIX} audit-skills-as-gate-jobs\n",
        },
        # A refinement PR with no marker → slug falls back to the title.
        {
            "headRefName": "auto/issue-refinement-20260909T220000Z",
            "isCrossRepository": False,
            "title": "review me prs across repos",
            "body": "no marker here",
        },
        # A human PR is ignored outright.
        {
            "headRefName": "docs/hand-written",
            "isCrossRepository": False,
            "title": "x",
            "body": "x",
        },
    ]
    monkeypatch.setattr(
        "herdr_routines.issue_refinement.subprocess.run",
        lambda *a, **k: _FakeProc(json.dumps(prs)),
    )
    slugs = refinement_open_pr_slugs(Path("/repo"))
    assert slugs == frozenset(
        {"audit-skills-as-gate-jobs", "review-me-prs-across-repos"}
    )

    # Any gh failure → None (caller fails safe), never an empty set.
    monkeypatch.setattr(
        "herdr_routines.issue_refinement.subprocess.run",
        lambda *a, **k: _FakeProc("", returncode=1, stderr="boom"),
    )
    assert refinement_open_pr_slugs(Path("/repo")) is None


def test_refinement_open_pr_slugs_scans_only_own_same_repo_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only this account's PRs are listed (``--author @me``), and a fork PR — or one
    whose payload does not say it is same-repo — never covers a bullet, even when it
    carries a refinement-shaped head and the marker."""
    prs = [
        {
            "headRefName": "auto/issue-refinement-20260910T220000Z",
            "isCrossRepository": True,
            "title": "docs: refine",
            "body": f"{PR_MARKER_PREFIX} fork-slug",
        },
        {
            "headRefName": "auto/issue-refinement-20260911T220000Z",
            "title": "docs: refine",
            "body": f"{PR_MARKER_PREFIX} unlabelled-slug",
        },
        {
            "headRefName": "auto/issue-refinement-20260912T220000Z",
            "isCrossRepository": False,
            "title": "docs: refine",
            "body": f"{PR_MARKER_PREFIX} own-slug",
        },
    ]
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> _FakeProc:
        calls.append(argv)
        return _FakeProc(json.dumps(prs))

    monkeypatch.setattr("herdr_routines.issue_refinement.subprocess.run", fake_run)
    assert refinement_open_pr_slugs(Path("/repo")) == frozenset({"own-slug"})
    (argv,) = calls
    assert argv[argv.index("--author") + 1] == "@me"
    assert "isCrossRepository" in argv[argv.index("--json") + 1]


def test_issue_refinement_no_pipeline_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / "docs" / "process" / "issues").mkdir(parents=True)
    (repo / "ROADMAP.md").write_text(ROADMAP)

    import herdr_routines.issue_refinement as mod

    # No open refinement PRs → the oldest eligible bullet is picked, with its marker.
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


# --- The code-enforced loop steps, through the real argparse -> handler path ---

JOB_PATH = Path(__file__).parent.parent / "deploy" / "jobs.d" / "issue-refinement.yaml"

GOOD_ISSUE = """---
id: "999"
title: Foo
status: open
priority: medium
area: cli
---

## Description
"""


def _run_refine_cli(argv: list[str]) -> int:
    """Parse with the real CLI parser and call the handler it selects — the same
    path `uv run herdr-routines refine-issue ...` takes, minus `main`'s logging."""
    args = cli._build_parser().parse_args(["refine-issue", *argv])
    assert args.handler is cli._cmd_refine_issue
    return int(args.handler(args))


def _reply(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "review.txt"
    path.write_text(text)
    return path


def test_parse_reviewer_confidence_reads_the_raw_reply() -> None:
    assert parse_reviewer_confidence("confidence: high\nno gaps") == "high"
    assert parse_reviewer_confidence("Preamble.\nConfidence: MEDIUM — x") == "medium"
    assert parse_reviewer_confidence("looks fine to me") is None
    # Markdown emphasis around the label or the level still counts.
    assert parse_reviewer_confidence("**Confidence:** high\n- none") == "high"
    assert parse_reviewer_confidence("- confidence: *low*") == "low"
    # An echoed prompt lists every level inside quotes; only the reviewer's own
    # line-leading verdict counts, so this is medium, not a fake "high" consensus.
    echoed = (
        "Reply with one line 'confidence: high' | 'confidence: medium' | "
        "'confidence: low' followed by the gaps.\n\nconfidence: medium\n- gap"
    )
    assert parse_reviewer_confidence(echoed) == "medium"
    # A gap that mentions the phrase mid-sentence is not a verdict.
    assert (
        parse_reviewer_confidence("I'd give confidence: high if X were fixed") is None
    )


def test_parse_reviewer_confidence_on_a_real_reviewer_reply() -> None:
    """Shape captured from a real `opencode run ... > FILE` reviewer call: tool
    chatter goes to stderr, stdout is the verdict line then the gap bullets."""
    reply = (
        "confidence: medium\n\n"
        "- Criterion 1 vs 2 leaves the core rule ambiguous: is a trusted "
        "blocking-count summary sufficient on its own?\n"
        "- The exact phrase to match is never pinned down.\n"
    )
    assert parse_reviewer_confidence(reply) == "medium"


@pytest.mark.parametrize(
    ("reply", "review_pass", "rc", "stderr_has"),
    [
        # Consensus on any pass stops the loop.
        ("confidence: high\n", 1, 1, "consensus reached"),
        # Not consensus, under the cap → run another pass.
        ("confidence: medium\n- gap", 1, 0, "continue"),
        ("confidence: low\n- gap", 2, 0, "continue"),
        # A reply with no confidence line is not consensus — it burns a pass.
        ("I think it's fine", 1, 0, "not stated"),
        # Third pass without consensus → capped, whatever the author thinks.
        ("confidence: medium\n- gap", 3, 1, "capped at 3"),
        ("confidence: medium\n- gap", 4, 1, "capped at 3"),
    ],
)
def test_refine_issue_review_pass_cli_enforces_cap_and_consensus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reply: str,
    review_pass: int,
    rc: int,
    stderr_has: str,
) -> None:
    reply_path = _reply(tmp_path, reply)
    argv = ["--review-pass", str(review_pass), "--reviewer-reply", str(reply_path)]
    assert _run_refine_cli(argv) == rc
    assert stderr_has in capsys.readouterr().err


def test_refine_issue_review_pass_cli_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Usage errors are 2 (nothing was checked), never 0 — the job must not read a
    # bad call as "run another pass" or as "stop".
    assert _run_refine_cli(["--review-pass", "1"]) == 2
    assert "--reviewer-reply" in capsys.readouterr().err
    assert _run_refine_cli(["--reviewer-reply", str(tmp_path / "r.txt")]) == 2
    missing = tmp_path / "missing.txt"
    assert (
        _run_refine_cli(["--review-pass", "1", "--reviewer-reply", str(missing)]) == 2
    )
    reply_path = _reply(tmp_path, "confidence: medium")
    assert (
        _run_refine_cli(["--review-pass", "0", "--reviewer-reply", str(reply_path)])
        == 2
    )
    # The two modes are mutually exclusive at the parser.
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["refine-issue", "--check-issue", "x.md", "--review-pass", "1"]
        )


def test_refine_issue_check_issue_cli_accepts_a_valid_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    issue = tmp_path / "999-foo.md"
    issue.write_text(GOOD_ISSUE)
    assert _run_refine_cli(["--check-issue", str(issue)]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("text", "stderr_has"),
    [
        (GOOD_ISSUE.replace("status: open", "status: done"), ["status must be 'open'"]),
        (
            GOOD_ISSUE.replace("priority: medium\n", "").replace("area: cli\n", ""),
            ["missing or empty frontmatter key: priority", "area"],
        ),
        (GOOD_ISSUE.replace("status: open", "status:"), ["key: status"]),
        ("# no frontmatter\n", ["no frontmatter"]),
        ("---\nid: 1\n", ["never closed"]),
        ("---\n- a\n- b\n---\n", ["not a YAML mapping"]),
        ("---\nid: [unclosed\n---\n", ["not valid YAML"]),
    ],
)
def test_refine_issue_check_issue_cli_lists_problems(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    stderr_has: list[str],
) -> None:
    issue = tmp_path / "999-foo.md"
    issue.write_text(text)
    assert _run_refine_cli(["--check-issue", str(issue)]) == 1
    err = capsys.readouterr().err
    for needle in stderr_has:
        assert needle in err


def test_refine_issue_check_issue_cli_unreadable_file_is_usage(tmp_path: Path) -> None:
    assert _run_refine_cli(["--check-issue", str(tmp_path / "nope.md")]) == 2


def test_refine_issue_bare_still_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The new flags must not change the bare stage-0 call the prompt makes first."""
    repo = tmp_path / "repo"
    (repo / "docs" / "process" / "issues").mkdir(parents=True)
    (repo / "ROADMAP.md").write_text(ROADMAP)
    monkeypatch.setattr(
        "herdr_routines.issue_refinement.refinement_open_pr_slugs",
        lambda *_a: frozenset(),
    )
    assert _run_refine_cli(["--repo", str(repo)]) == 0
    assert f"{PR_MARKER_PREFIX} unify-routines-pipeline" in capsys.readouterr().out


def test_issue_refinement_prompt_runs_the_code_enforced_steps() -> None:
    """The job prompt is the only caller of the two checks on the hosts: it must invoke
    both, via `uv run`, with flags the real parser accepts."""
    prompt = yaml.safe_load(JOB_PATH.read_text())["prompt"]
    review_call = (
        "uv run herdr-routines refine-issue --review-pass N "
        "--reviewer-reply $ROUTINE_REPORT.review-N.txt"
    )
    check_call = (
        "uv run herdr-routines refine-issue --check-issue "
        "docs/process/issues/NNN-<slug>.md"
    )
    assert review_call in prompt
    assert check_call in prompt
    # The frontmatter check comes before the PR is opened.
    assert prompt.index(check_call) < prompt.index("gh pr create")
    # Both calls parse with the real CLI parser (placeholders filled in).
    parser = cli._build_parser()
    for call in (review_call, check_call):
        argv = (
            call.removeprefix("uv run herdr-routines ")
            .replace("N ", "1 ")
            .replace("-N.txt", "-1.txt")
            .split()
        )
        assert parser.parse_args(argv).handler is cli._cmd_refine_issue


def _validate_ns(config_path: Path, tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=config_path, systemd_unit=tmp_path / "does-not-exist.service"
    )


def _write_refinement_jobs(tmp_path: Path, **overrides: str) -> Path:
    data = yaml.safe_load(JOB_PATH.read_text())
    data["repo"] = str(tmp_path)  # a git repo, so validate raises no repo problem
    (tmp_path / ".git").mkdir(exist_ok=True)
    data.update(overrides)
    jobs_d = tmp_path / "jobs.d"
    jobs_d.mkdir()
    (jobs_d / "issue-refinement.yaml").write_text(yaml.safe_dump(data))
    return jobs_d


def test_validate_stays_silent_for_the_real_issue_refinement_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _write_refinement_jobs(tmp_path)
    assert cli._cmd_validate(_validate_ns(config, tmp_path)) == 0
    assert "not opencode-only" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_kind": "claude", "model": "sonnet"},
        {"model": "anthropic/claude-sonnet"},
        {"fallback_model": "anthropic/claude-haiku"},
    ],
)
def test_validate_warns_when_issue_refinement_job_uses_claude(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], overrides: dict[str, str]
) -> None:
    config = _write_refinement_jobs(tmp_path, **overrides)
    # A warning, not an error: exit code unchanged, same posture as the
    # $ROUTINE_REPORT checks.
    assert cli._cmd_validate(_validate_ns(config, tmp_path)) == 0
    assert "issue-refinement: runs `herdr-routines refine-issue` but is not " in (
        capsys.readouterr().err
    )
