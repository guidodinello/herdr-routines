"""Tests for the issue-refinement job (issue 029).

The five acceptance tests named in
``docs/pipeline/runs/20260907T050000Z/spec.md`` each exercise a real path:
selection + the "already refined" guard, the loop cap, frontmatter validation,
the job file through the real config validator, and pipeline independence.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
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


# ---------------------------------------------------------------------------
# Issue 080 — issue-number collisions. Spec v2:
# docs/pipeline/runs/20261107T050000Z/spec.md. The number the nightly job picks
# is computed in code from the union of fresh-fetched `origin/main` and every
# open same-repo PR — otherwise the run collides the way 071/072 did on
# 2026-10-24 — and `--check-issue` stops the loop when the draft's own id is
# already taken instead of validating frontmatter only.
# ---------------------------------------------------------------------------

# Fresh-fetched main: `git ls-tree -r --name-only origin/main -- docs/process/issues/`.
MAIN_LS_TREE = (
    "docs/process/issues/070-fix-worker-done-with-uncommitted-changes.md\n"
    "docs/process/issues/071-post-run-verify-gates.md\n"
)

# `gh pr list --state open --json headRefName,files,isCrossRepository`: an open
# pipeline PR — a *different* author identity than gh@me — owns 072 until it
# merges; a fork PR never owns a number in this repo.
OPEN_PR_LIST = json.dumps(
    [
        {
            "headRefName": "auto/pipeline-20261107T050000Z",
            "isCrossRepository": False,
            "files": [{"path": "docs/process/issues/072-pipeline-pick.md"}],
        },
        {
            "headRefName": "drive-by/fork",
            "isCrossRepository": True,
            "files": [{"path": "docs/process/issues/073-forked-claim.md"}],
        },
    ]
)


class _GitGhFake:
    """`subprocess.run` stub for the issue-080 sources: `git fetch`/`git ls-tree`/
    `gh pr list`, plus every failure pseudo-shape the fail-safe rules are tested
    against. Records every argv it receives so tests can also pin the exact
    command shape the numbers are computed from."""

    def __init__(
        self,
        *,
        ls_tree: str = "",
        pr_list: str = "[]",
        fail: str | None = None,
        stderr: str = "simulated outage",
    ) -> None:
        self.ls_tree = ls_tree
        self.pr_list = pr_list
        self.fail = fail
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> _FakeProc:
        self.calls.append(list(argv))

        def _res(out: str, code: int = 0) -> _FakeProc:
            return _FakeProc(out, returncode=code, stderr=self.stderr)

        if argv[:2] == ["git", "fetch"]:
            return _res("", 1 if self.fail == "fetch" else 0)
        if argv[:2] == ["git", "ls-tree"]:
            return _res(self.ls_tree, 1 if self.fail == "ls-tree" else 0)
        if argv[:1] == ["gh"]:
            return _res(self.pr_list, 1 if self.fail == "gh" else 0)
        return _res("", 0)


def _issue_repo(tmp_path: Path) -> Path:
    """A checkout-shaped repo dir: `docs/process/issues/` + ROADMAP.md."""
    repo = tmp_path / "repo"
    (repo / "docs" / "process" / "issues").mkdir(parents=True)
    (repo / "ROADMAP.md").write_text(ROADMAP)
    return repo


def _stub_subprocess(monkeypatch: pytest.MonkeyPatch, fake: _GitGhFake) -> None:
    monkeypatch.setattr("herdr_routines.issue_refinement.subprocess.run", fake)


def test_next_issue_id_skips_open_pr_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 1 (blocking): the union of fresh-fetched `origin/main` and every open
    same-repo PR — of *any* author — is what `--next-id` skips. 072 is owned only
    by the open pipeline PR, so it must not be picked even though it is on
    neither main nor the worktree checkout; the fork PR's 073 must not own a
    number here at all, so the next id is 073."""
    repo = _issue_repo(tmp_path)
    fake = _GitGhFake(ls_tree=MAIN_LS_TREE, pr_list=OPEN_PR_LIST)
    _stub_subprocess(monkeypatch, fake)

    assert _run_refine_cli(["--next-id", "--repo", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == "073"

    gh_calls = [argv for argv in fake.calls if argv[:1] == ["gh"]]
    assert len(gh_calls) == 1  # one batched `gh pr list`
    (gh_argv,) = gh_calls
    gh_text = " ".join(gh_argv)
    assert "--state" in gh_argv and gh_argv[gh_argv.index("--state") + 1] == "open"
    # `files` is required so PRs can own numbers; no author filter at all — any
    # open same-repo PR owns its number, not just this account's (@me).
    assert {"headRefName", "files", "isCrossRepository"} <= set(
        gh_argv[gh_argv.index("--json") + 1].split(",")
    )
    assert "--author" not in gh_argv and "@me" not in gh_text

    # The main part of the union comes from fresh-fetched origin/main, never the
    # checkout: a fetch and an origin/main ls-tree naming the issues dir ran in
    # the repo, before the gh snapshot.
    fetch_texts = [
        " ".join(argv) for argv in fake.calls if argv[:2] == ["git", "fetch"]
    ]
    assert fetch_texts and any("origin" in t and "main" in t for t in fetch_texts)
    ls_calls = [argv for argv in fake.calls if argv[:2] == ["git", "ls-tree"]]
    assert ls_calls
    ls_tokens = {token for argv in ls_calls for token in argv}
    assert {"-r", "--name-only", "origin/main"} <= ls_tokens
    assert any("docs/process/issues/" in token for argv in ls_calls for token in argv)
    first_git = next(i for i, argv in enumerate(fake.calls) if argv[0] == "git")
    first_gh = next(
        (i for i, argv in enumerate(fake.calls) if argv[:1] == ["gh"]), len(fake.calls)
    )
    assert first_git < first_gh


@pytest.mark.parametrize(
    ("fail", "pr_list", "needle"),
    [
        ("fetch", None, "fetch"),
        ("ls-tree", None, "ls-tree"),
        ("gh", None, "gh"),
        (None, "not json at all", "gh"),
        (None, '{"not": "a list"}', "gh"),
    ],
)
def test_next_issue_id_fails_safe_on_git_or_gh_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail: str | None,
    pr_list: str | None,
    needle: str,
) -> None:
    """AC 2 (blocking): `--next-id` fails closed. Any git fetch/ls-tree or gh pr
    list failure — including a gh answer that cannot be parsed as the PR-list
    shape — exits 1 with the reason named on stderr and prints no id. Never a
    fail-open id from an unknown answer, mirroring `refinement_open_pr_slugs`."""
    repo = _issue_repo(tmp_path)
    fake = _GitGhFake(
        ls_tree=MAIN_LS_TREE,
        pr_list=pr_list if pr_list is not None else OPEN_PR_LIST,
        fail=fail,
    )
    _stub_subprocess(monkeypatch, fake)

    assert _run_refine_cli(["--next-id", "--repo", str(repo)]) == 1

    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # no id printed on an unknown answer
    assert needle in captured.err  # the failing source is named


def test_check_issue_rejects_duplicate_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 3 (blocking): after the frontmatter checks pass, `--check-issue` rejects
    a draft whose `id:` is already used on fresh-fetched `origin/main` or by an
    open PR — the issue-029 frontmatter-only check is what passed #225 as a
    second 071. A unique draft still passes, and the duplicate problem line names
    the id, never "could not verify number uniqueness"."""
    repo = _issue_repo(tmp_path)
    draft = repo / "docs" / "process" / "issues" / "071-post-run-verify-gates.md"

    # Duplicate on fresh-fetched origin/main.
    fake = _GitGhFake(ls_tree=MAIN_LS_TREE, pr_list="[]")
    _stub_subprocess(monkeypatch, fake)
    draft.write_text(GOOD_ISSUE.replace('id: "999"', 'id: "071"'))
    assert _run_refine_cli(["--check-issue", str(draft), "--repo", str(repo)]) == 1
    err = capsys.readouterr().err
    problem_lines = [
        line for line in err.splitlines() if line.lstrip().startswith("- ")
    ]
    assert any("duplicate" in line.lower() and "071" in line for line in problem_lines)
    assert "could not verify" not in err.lower()

    # Duplicate owned only by an open PR — the other half of the union.
    fake = _GitGhFake(
        ls_tree="docs/process/issues/070-fix-worker-done.md\n",
        pr_list=json.dumps(
            [
                {
                    "headRefName": "auto/issue-refinement-20261107T220000Z",
                    "isCrossRepository": False,
                    "files": [{"path": "docs/process/issues/071-colliding-draft.md"}],
                }
            ]
        ),
    )
    _stub_subprocess(monkeypatch, fake)
    assert _run_refine_cli(["--check-issue", str(draft), "--repo", str(repo)]) == 1
    assert "071" in capsys.readouterr().err

    # A unique draft on the same sources still passes frontmatter + uniqueness.
    fake = _GitGhFake(ls_tree=MAIN_LS_TREE, pr_list="[]")
    _stub_subprocess(monkeypatch, fake)
    unique = draft.with_name("072-next.md")
    unique.write_text(GOOD_ISSUE.replace('id: "999"', 'id: "072"'))
    assert _run_refine_cli(["--check-issue", str(unique), "--repo", str(repo)]) == 0
    assert capsys.readouterr().err == ""


def test_check_issue_fails_safe_when_uniqueness_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 4 (blocking): `--check-issue` never fail-opens. When the number union
    cannot be verified at all — gh or git failure of any kind — the check exits 1
    with "could not verify number uniqueness", a message distinct from the
    duplicate-id failure; a valid-looking draft is never waved through."""
    repo = _issue_repo(tmp_path)
    draft = repo / "docs" / "process" / "issues" / "072-next.md"
    draft.write_text(GOOD_ISSUE.replace('id: "999"', 'id: "072"'))

    for fake in (
        _GitGhFake(ls_tree=MAIN_LS_TREE, pr_list="[]", fail="fetch"),
        _GitGhFake(ls_tree=MAIN_LS_TREE, pr_list="[]", fail="gh"),
        _GitGhFake(ls_tree=MAIN_LS_TREE, pr_list="not json at all"),
    ):
        _stub_subprocess(monkeypatch, fake)
        assert _run_refine_cli(["--check-issue", str(draft), "--repo", str(repo)]) == 1
        captured = capsys.readouterr()
        assert captured.out.strip() == ""
        assert "could not verify number uniqueness" in captured.err
        assert "duplicate" not in captured.err.lower()


# --- AC 5-9: the ROADMAP pointer is a code step, keyed off the issue filename ---

FOUNDRY_SLUG = "foundry-replaces-the-hand-rolled-template-pins"

PTR_FRONTMATTER = "title: Pointer target\nstatus: open\npriority: medium\narea: docs\n"

POINTER_ROADMAP = """# ROADMAP

## Parking lot

- **Foundry replaces the hand-rolled template pins** — idea, not designed. One
  data-driven template the config loader validates. 2026-11-01 brainstorm.
- **Digest rollup by weekday** — idea, not designed. The nightly digest gets a
  weekly rollup. Gate: 079's history lock.

House rule: deferred work gets a bullet here.
"""

FOLDED_ROADMAP = """# ROADMAP

## Parking lot

- **Docs housekeeping sweep** — prune stale run docs and archive them. This is a
  longer idea that deliberately folds over two continuation lines to pin the
  exact indent contract the pointer step must preserve.

House rule: deferred work gets a bullet here.
"""


def _pointer_repo(tmp_path: Path, *, name: str = "repo") -> tuple[Path, Path]:
    repo = tmp_path / name
    (repo / "docs" / "process" / "issues").mkdir(parents=True)
    roadmap = repo / "ROADMAP.md"
    roadmap.write_text(POINTER_ROADMAP)
    return repo, roadmap


def _pointer_issue(
    repo: Path,
    *,
    number: str,
    id_value: str | None,
    slug: str = FOUNDRY_SLUG,
) -> Path:
    """An issue file for `--roadmap-pointer`: `number` defines the filename (and
    so the bullet slug it must attach to); `id_value` defines (or omits) the
    frontmatter `id:` line."""
    path = repo / "docs" / "process" / "issues" / f"{number}-{slug}.md"
    id_lines = f'id: "{id_value}"\n' if id_value is not None else ""
    path.write_text(f"---\n{id_lines}{PTR_FRONTMATTER}---\n\n## Description\n")
    return path


def _run_pointer(repo: Path, roadmap: Path, issue: Path) -> int:
    return _run_refine_cli(
        [
            "--roadmap-pointer",
            str(issue),
            "--roadmap",
            str(roadmap),
            "--repo",
            str(repo),
        ]
    )


def test_roadmap_pointer_appended_to_its_bullet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 5 (blocking): `--roadmap-pointer` reads the issue frontmatter `id:`,
    locates the Parking Lot bullet whose slug matches the issue filename via
    `parse_parking_lot_bullets`, and appends `` → [`NNN`](docs/process/issues/
    NNN-<slug>.md) `` to that bullet's last line at the two-space continuation
    indent — never to the wrong bullet or paragraph (the 2026-10-24 bug)."""
    repo, roadmap = _pointer_repo(tmp_path)
    issue = _pointer_issue(repo, number="082", id_value="082")
    fake = _GitGhFake()
    _stub_subprocess(monkeypatch, fake)

    assert _run_pointer(repo, roadmap, issue) == 0

    pointer = f"→ [`082`](docs/process/issues/082-{FOUNDRY_SLUG}.md)"
    after = roadmap.read_text()
    assert f" {pointer}" in after
    # The pointer line is a bullet continuation line, not a column-0 paragraph.
    pointer_line = next(line for line in after.splitlines() if pointer in line)
    assert pointer_line.startswith("  ")
    assert pointer_line.endswith(pointer)  # appended to the bullet's last line
    # It landed on *its* bullet: the parsed Parking Lot is otherwise unchanged.
    bullets = parse_parking_lot_bullets(after)
    assert [bullet.slug for bullet in bullets] == [
        FOUNDRY_SLUG,
        "digest-rollup-by-weekday",
    ]
    assert pointer in bullets[0].raw
    assert pointer not in bullets[1].raw
    assert fake.calls == []  # filesystem-only: no git/gh on the pointer path


def test_roadmap_pointer_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 6 (non-blocking): calling `--roadmap-pointer` twice leaves ROADMAP.md
    byte-identical — the exact pointer text already present on the matching
    bullet means exit 0 with no further edit."""
    repo, roadmap = _pointer_repo(tmp_path)
    issue = _pointer_issue(repo, number="082", id_value="082")
    fake = _GitGhFake()
    _stub_subprocess(monkeypatch, fake)

    assert _run_pointer(repo, roadmap, issue) == 0
    once = roadmap.read_text()
    assert once.count(f"docs/process/issues/082-{FOUNDRY_SLUG}.md") == 1

    assert _run_pointer(repo, roadmap, issue) == 0
    assert roadmap.read_text() == once
    assert f"→ [`082`](docs/process/issues/082-{FOUNDRY_SLUG}.md)" in once
    assert fake.calls == []


def test_next_issue_id_treats_padded_ids_as_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 7 (non-blocking): `071` = `71` = `0071`. The union — and the duplicate
    gate — compare filename numbers as integers through the padded-id rule. A
    fresh main carrying `0070` plus an open PR carrying `0071` means the next id
    is 072: a string-compare implementation would return 071 and collide."""
    repo = _issue_repo(tmp_path)
    fake = _GitGhFake(
        ls_tree="docs/process/issues/0070-a.md\n",
        pr_list=json.dumps(
            [
                {
                    "headRefName": "auto/issue-refinement-20261107T220000Z",
                    "isCrossRepository": False,
                    "files": [{"path": "docs/process/issues/0071-b.md"}],
                }
            ]
        ),
    )
    _stub_subprocess(monkeypatch, fake)
    assert _run_refine_cli(["--next-id", "--repo", str(repo)]) == 0
    # ints {70, 71} → 072; string-max("0070", "0071") would wrongly say 071.
    assert capsys.readouterr().out.strip() == "072"

    # And in the --check-issue duplicate gate: `0071` collides with main's `71`.
    repo = _issue_repo(tmp_path)
    fake = _GitGhFake(ls_tree="docs/process/issues/71-c.md\n", pr_list="[]")
    _stub_subprocess(monkeypatch, fake)
    draft = repo / "docs" / "process" / "issues" / "0071-padded.md"
    draft.write_text(GOOD_ISSUE.replace('id: "999"', 'id: "0071"'))
    assert _run_refine_cli(["--check-issue", str(draft), "--repo", str(repo)]) == 1
    assert "duplicate" in capsys.readouterr().err.lower()


def test_roadmap_pointer_unknown_slug_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 8 (non-blocking): a filename slug with no matching Parking Lot bullet
    exits 1 with a reason and never edits ROADMAP.md — attaching the pointer to a
    guessed paragraph is the 2026-10-24 bug in code form."""
    repo, roadmap = _pointer_repo(tmp_path)
    issue = _pointer_issue(repo, number="083", id_value="083", slug="no-such-bullet")
    before = roadmap.read_text()
    fake = _GitGhFake()
    _stub_subprocess(monkeypatch, fake)

    assert _run_pointer(repo, roadmap, issue) == 1
    assert capsys.readouterr().err != ""  # the reason is stated, not silent
    assert roadmap.read_text() == before
    assert fake.calls == []


def test_roadmap_pointer_preserves_continuation_indent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 9 (non-blocking): the pointer never breaks the two-space continuation
    indent. A bullet that already folds over multiple lines stays parseable by
    `parse_parking_lot_bullets` after the edit, with every original fold line
    still inside the same bullet — no folded line promoted to column 0."""
    repo = tmp_path / "repo"
    (repo / "docs" / "process" / "issues").mkdir(parents=True)
    roadmap = repo / "ROADMAP.md"
    roadmap.write_text(FOLDED_ROADMAP)
    issue = _pointer_issue(
        repo, number="089", id_value="089", slug="docs-housekeeping-sweep"
    )
    fake = _GitGhFake()
    _stub_subprocess(monkeypatch, fake)

    assert len(parse_parking_lot_bullets(FOLDED_ROADMAP)) == 1

    assert _run_pointer(repo, roadmap, issue) == 0

    pointer = "→ [`089`](docs/process/issues/089-docs-housekeeping-sweep.md)"
    after = roadmap.read_text()
    pointer_line = next(line for line in after.splitlines() if pointer in line)
    assert pointer_line.startswith("  ") and not pointer_line.startswith("- ")

    bullets = parse_parking_lot_bullets(after)
    assert len(bullets) == 1
    assert pointer in bullets[0].raw
    # Fold lines still inside the same bullet — nothing was promoted to column 0.
    assert "exact indent contract" in bullets[0].raw
    assert fake.calls == []


def test_refine_issue_next_id_and_roadmap_pointer_cli_usage_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC 10 (non-blocking): the four modes are mutually exclusive at the parser
    (usage exit 2 on overlap); `--repo` feeds `run_check_issue` with `Path.cwd()`
    as the default; `--roadmap-pointer` with an empty/absent frontmatter `id:` is
    usage exit 2 — the pointer cannot be built."""
    parser = cli._build_parser()
    assert parser.parse_args(["refine-issue"]).repo == Path.cwd()

    overlaps = [
        ["--next-id", "--check-issue", "docs/process/issues/080-x.md"],
        ["--next-id", "--roadmap-pointer", "docs/process/issues/080-x.md"],
        ["--next-id", "--review-pass", "1"],
        ["--roadmap-pointer", "docs/process/issues/080-x.md", "--review-pass", "1"],
        [
            "--check-issue",
            "docs/process/issues/080-x.md",
            "--roadmap-pointer",
            "docs/process/issues/080-x.md",
        ],
    ]
    for argv in overlaps:
        with pytest.raises(SystemExit):
            parser.parse_args(["refine-issue", *argv])

    # --repo feeds run_check_issue even at its default: the uniqueness gate runs
    # against `Path.cwd()` — the worktree the job is authoring in.
    repo = _issue_repo(tmp_path)
    monkeypatch.chdir(repo)
    _stub_subprocess(monkeypatch, _GitGhFake(ls_tree=MAIN_LS_TREE, pr_list="[]"))
    draft = repo / "docs" / "process" / "issues" / "071-dup.md"
    draft.write_text(GOOD_ISSUE.replace('id: "999"', 'id: "071"'))
    assert _run_refine_cli(["--check-issue", str(draft)]) == 1
    assert "duplicate" in capsys.readouterr().err.lower()

    # The pointer step needs the issue's id: empty or absent it exits 2, and
    # ROADMAP.md is untouched each time.
    repo, roadmap = _pointer_repo(tmp_path, name="pointer-repo")
    _stub_subprocess(monkeypatch, _GitGhFake())
    empty_id = _pointer_issue(repo, number="081", id_value="")
    absent_id = _pointer_issue(repo, number="082", id_value=None)
    for bad in (empty_id, absent_id):
        before = roadmap.read_text()
        assert _run_pointer(repo, roadmap, bad) == 2
        assert roadmap.read_text() == before


BLOCKING_TESTS = (
    "test_next_issue_id_skips_open_pr_numbers",
    "test_check_issue_rejects_duplicate_id",
    "test_roadmap_pointer_appended_to_its_bullet",
)


def test_ci_green_with_blocking_tests_run_first() -> None:
    """AC 12 (meta-check): the automated half of the Verification claim. The
    three blocking tests exist in this suite and `scripts/ci-check.sh` runs the
    suite (so a green ci-check proves they pass); and TDD sequencing is pinned in
    git history — the blocking tests' commit precedes any commit introducing the
    feature. Watching them go red on a clean tree and then running ci-check is
    the implementing agent's duty, not something a test can re-assert."""
    for name in BLOCKING_TESTS:
        assert callable(globals()[name]), f"{name} must exist as a test here"

    script = Path(__file__).parent.parent / "scripts" / "ci-check.sh"
    assert "uv run pytest -q" in script.read_text()  # the suite runs under ci-check

    def first_commit_ts(pattern: str, pathspec: str) -> int | None:
        proc = subprocess.run(
            ["git", "log", "-S", pattern, "--format=%ct", "--reverse", "--", pathspec],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        lines = proc.stdout.splitlines()
        return int(lines[0]) if proc.returncode == 0 and lines else None

    tests_ts = first_commit_ts(
        "def test_next_issue_id_skips_open_pr_numbers", "tests/test_issue_refinement.py"
    )
    impl_ts = first_commit_ts(
        "def next_issue_id", "src/herdr_routines/issue_refinement.py"
    )
    if impl_ts is None:
        return  # next_issue_id not implemented yet: nothing to sequence against
    assert tests_ts is not None  # the blocking tests were committed first
    assert tests_ts <= impl_ts


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
    """The job prompt is the only caller of the code steps on the hosts: it must invoke
    every one via `uv run`, with flags the real parser accepts (issue 080: step 2
    computes the number with --next-id instead of "highest existing + 1" prose, the
    ROADMAP pointer is its own step-4 call after review settles, and step 5 keeps
    the --check-issue exit loop)."""
    prompt = yaml.safe_load(JOB_PATH.read_text())["prompt"]
    review_call = (
        "uv run herdr-routines refine-issue --review-pass N "
        "--reviewer-reply $ROUTINE_REPORT.review-N.txt"
    )
    pointer_call = (
        "uv run herdr-routines refine-issue --roadmap-pointer "
        "docs/process/issues/NNN-<slug>.md"
    )
    check_call = (
        "uv run herdr-routines refine-issue --check-issue "
        "docs/process/issues/NNN-<slug>.md"
    )
    assert "uv run herdr-routines refine-issue --next-id" in prompt
    assert review_call in prompt
    assert pointer_call in prompt
    assert check_call in prompt
    # The number is code's answer, never the snapshot of a snapshot.
    assert "highest existing" not in prompt.lower()
    # Order: selection → author → loop → pointer (after review settles) → check → PR.
    assert prompt.index(review_call) < prompt.index(pointer_call)
    assert prompt.index(pointer_call) < prompt.index(check_call)
    assert prompt.index(check_call) < prompt.index("gh pr create")
    # Step 5 stays an exit-code loop: re-run until it passes, never open the PR on
    # a failing check.
    tail = prompt[prompt.index(check_call) :]
    assert "re-run" in tail and "exits 0" in tail
    # Never a bare `refine-issue ...` — always via `uv run herdr-routines`.
    assert re.search(r"(?<!herdr-routines )refine-issue --", prompt) is None

    # All calls parse with the real CLI parser (placeholders filled in).
    parser = cli._build_parser()
    for call in (
        "uv run herdr-routines refine-issue --next-id",
        review_call.replace("N ", "1 ").replace("-N.txt", "-1.txt"),
        pointer_call,
        check_call,
    ):
        argv = call.removeprefix("uv run herdr-routines ").split()
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
