"""Tests for `herdr-routines pipeline-prepare` (issue 054 phase A) — the pre-flight the
pipeline launcher runs *before* any agent exists: sync, pick+claim, worktree+workspace,
`state.json`.

Real bare-origin git fixtures (copied from tests/test_sync_repo.py:38-67) so the sync is
a real fetch+fast-forward, and a fake HerdrClient so no `herdr` binary, server, or
workspace is needed (AC 8: the sync/pick/state paths are Herdr-free, and the two call
sites that do need it are injected).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest

from herdr_routines import cli
from herdr_routines.claims import load_claims
from herdr_routines.herdr import HerdrClient, HerdrCliError, WorktreeInfo
from herdr_routines.pipeline_prepare import (
    EXIT_FAILURE,
    EXIT_NO_FEATURE,
    EXIT_PREPARED,
    _write_state_json,
    prepare_run,
    render_prepared_values,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ISSUES_DIR = REPO_ROOT / "docs" / "process" / "issues"

ISSUE_TEMPLATE = """---
id: "{id}"
title: {title}
status: {status}
priority: medium
area: pipeline
---

## Description

Do the thing for issue {id}.
"""

RUN_ID = "20260930T050000Z"
DEADLINE_EPOCH = 1_790_597_715


# --- git fixtures (from tests/test_sync_repo.py) ------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--bare", str(path)], capture_output=True, text=True, check=True
    )
    tmp = path.parent / f".bare-init-{path.name}"
    subprocess.run(
        ["git", "clone", str(path), str(tmp)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(tmp, "config", "user.email", "test@test.com")
    _git(tmp, "config", "user.name", "Test")
    (tmp / "README.md").write_text("init\n")
    _git(tmp, "add", "README.md")
    _git(tmp, "commit", "-m", "init")
    _git(tmp, "push", "-u", "origin", "HEAD")
    shutil.rmtree(tmp)


def _detect_bare_default_branch(bare_path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(bare_path), "symbolic-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip().replace("refs/heads/", "")


class FakeHerdr:
    """A HerdrClient-shaped fake for prepare-level tests: it creates a real directory to
    stand in for the worktree (so `state.json` lands where a real run's would) and records
    every call, so a test can assert which steps ran at all.

    `raise_on` raises `HerdrCliError`, not a bare `RuntimeError`, because that is what a
    real down server raises and what `prepare_run`'s `except (HerdrCliError, OSError)`
    is there to catch — a `RuntimeError` would sail past the handler and the test would
    be asserting the wrong thing entirely."""

    def __init__(self, worktrees_root: Path) -> None:
        self.worktrees_root = worktrees_root
        self.calls: list[str] = []
        self.created_worktrees: list[Path] = []
        self.created_workspaces: list[tuple[str, str, dict[str, str]]] = []
        self.existing_workspaces: list[dict[str, Any]] = []
        self.raise_on: str | None = None

    def worktree_create_full(self, *, cwd, branch, base, label=None):
        self.calls.append("worktree_create_full")
        if self.raise_on == "worktree_create_full":
            raise HerdrCliError(
                "herdr server error running worktree create", exit_code=1
            )
        assert cwd and branch and base
        # herdr names the dir after the branch with the slash flattened, e.g.
        # auto-pipeline-<run_id> (pick_feature.PIPELINE_WORKTREE_GLOB relies on that).
        worktree = self.worktrees_root / branch.replace("/", "-")
        worktree.mkdir(parents=True, exist_ok=True)
        self.created_worktrees.append(worktree)
        return WorktreeInfo(path=str(worktree), branch=branch, root_pane_id="w1:p1")

    def workspace_list(self):
        self.calls.append("workspace_list")
        if self.raise_on == "workspace_list":
            raise HerdrCliError(
                "herdr server error running workspace list", exit_code=1
            )
        return list(self.existing_workspaces)

    def workspace_create(self, *, cwd, label=None, env=None):
        self.calls.append("workspace_create")
        if self.raise_on == "workspace_create":
            raise HerdrCliError(
                "herdr server error running workspace create", exit_code=1
            )
        assert cwd
        self.created_workspaces.append((cwd, label or "", dict(env or {})))
        return "ws1"


@pytest.fixture
def no_open_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pick's `gh pr list` exclusion, stubbed: no `gh`/network in unit tests."""
    monkeypatch.setattr(
        "herdr_routines.pick_feature.pipeline_open_pr_issue_ids",
        lambda repo, worktrees_root=None: frozenset(),
    )


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHerdr:
    """For tests that drive the **CLI**: point every default state path (claims store,
    reports dir, worktrees root) at `tmp_path`, and swap `HerdrClient` for a fake.

    Without this a CLI-level test resolves `default_claims_path()` to the real
    `~/.local/state/herdr-routines/pick-feature-claims.json` and *claims real issues on
    the developer's machine* — which is exactly what the first draft of this file did.
    Every test that reaches `cli.main` must take this fixture.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    client = FakeHerdr(tmp_path / "home" / ".herdr" / "worktrees" / "herdr-routines")
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setattr("herdr_routines.pipeline_prepare.HerdrClient", lambda: client)
    return client


def _seed_issues(repo_parent: Path, *issues: tuple[str, str, str]) -> None:
    """`(id, filename, status)` triples into `$REPO_PARENT/docs/process/issues`, committed
    and pushed so the clone a prepare syncs is the source of truth."""
    issues_root = repo_parent / "docs" / "process" / "issues"
    issues_root.mkdir(parents=True, exist_ok=True)
    for issue_id, filename, status in issues:
        (issues_root / filename).write_text(
            ISSUE_TEMPLATE.format(id=issue_id, title=f"Issue {issue_id}", status=status)
        )
    _git(repo_parent, "config", "user.email", "test@test.com")
    _git(repo_parent, "config", "user.name", "Test")
    _git(repo_parent, "add", "docs")
    _git(repo_parent, "commit", "-m", "add issues")
    _git(repo_parent, "push")


def _parent_clone(tmp_path: Path) -> tuple[Path, Path, str]:
    """A bare origin with one commit, a `parent` clone of it, and the base branch name.
    Returns (bare, parent_clone, base)."""
    bare = tmp_path / "bare.git"
    _init_bare_git_repo(bare)
    base = _detect_bare_default_branch(bare)
    parent = tmp_path / "parent"
    subprocess.run(
        ["git", "clone", str(bare), str(parent)],
        capture_output=True,
        text=True,
        check=True,
    )
    return bare, parent, base


def _push_new_commit(bare: Path, tmp_path: Path, name: str) -> None:
    """Commit + push `name` to origin from a throwaway clone — i.e. make the parent clone
    genuinely behind, so a real sync has something to fast-forward."""
    work = tmp_path / f"work-{name}"
    subprocess.run(
        ["git", "clone", str(bare), str(work)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(work, "config", "user.email", "test@test.com")
    _git(work, "config", "user.name", "Test")
    (work / name).write_text("new\n")
    _git(work, "add", name)
    _git(work, "commit", "-m", f"add {name}")
    _git(work, "push")


class _PrepareKwargs(TypedDict):
    """`prepare_run`'s keyword arguments, typed so a test that builds them is checked
    against the real signature instead of passing an untyped `**dict[str, object]`."""

    run_id: str
    repo_parent: Path
    report: Path
    deadline_epoch: int
    base: str
    client: HerdrClient | None
    claims_path: Path
    worktrees_root: Path
    reports_dir: Path


def _prepare_kwargs(
    tmp_path: Path, *, parent: Path, base: str, client: FakeHerdr | None = None
) -> _PrepareKwargs:
    return _PrepareKwargs(
        run_id=RUN_ID,
        repo_parent=parent,
        report=tmp_path / "reports" / f"pipeline-{RUN_ID}.md",
        deadline_epoch=DEADLINE_EPOCH,
        base=base,
        # The fake is duck-typed against HerdrClient's three call sites; the cast is the
        # seam prepare_run documents, not a way around a real mismatch.
        client=cast("HerdrClient", client),
        claims_path=tmp_path / "claims.json",
        worktrees_root=tmp_path / "worktrees",
        reports_dir=tmp_path / "reports",
    )


# --- AC 1: a real run is set up, in code, with the passed-in deadline -------------------


def test_pipeline_prepare_sets_up_run(tmp_path: Path, no_open_prs: None) -> None:
    """Acceptance criterion 1: an eligible issue leaves a synced `$REPO_PARENT`, a claim
    recorded out-of-tree, a worktree on `auto/pipeline-<run_id>`, and a `state.json` whose
    `deadline_epoch` is the value that was passed in and whose `feature_source` names the
    claimed file."""
    bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-pick-a-feature.md", "open"))
    # Something on origin the parent clone is behind by — a real fast-forward to prove the
    # sync step ran rather than being decoration.
    _push_new_commit(bare, tmp_path, "UPSTREAM.md")

    client = FakeHerdr(tmp_path / "worktrees")
    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "ok"
    # 1. the sync happened (the upstream commit is now in the checkout) ...
    assert (parent / "UPSTREAM.md").exists()
    # 2. ... the issue was claimed out of tree, leaving the issue file itself untouched ...
    claims = load_claims(tmp_path / "claims.json")
    assert set(claims) == {"001"}
    assert (parent / "docs/process/issues/001-pick-a-feature.md").read_text().count(
        "status: open"
    ) == 1
    # 3. ... the worktree is on the run's branch ...
    branch = f"auto/pipeline-{RUN_ID}"
    assert client.calls == [
        "worktree_create_full",
        "workspace_list",
        "workspace_create",
    ]
    assert client.created_worktrees == [
        tmp_path / "worktrees" / f"auto-pipeline-{RUN_ID}"
    ]
    assert client.created_workspaces[0][1] == f"pipeline-{RUN_ID}"
    assert client.created_workspaces[0][2] == {"HERDR_ENV": "1"}
    # 4. ... and state.json is where the rest of the system reads it from.
    state_path = client.created_worktrees[0] / "state.json"
    assert result.state_path == state_path
    import json

    state = json.loads(state_path.read_text())
    assert state["run_id"] == RUN_ID
    assert state["deadline_epoch"] == DEADLINE_EPOCH
    assert state["feature_source"] == "docs/process/issues/001-pick-a-feature.md"
    assert state["branch"] == branch
    assert state["current_stage"] == 0
    assert state["stage_sessions"] == {}
    assert state["shared_worktree"] == str(client.created_worktrees[0])
    assert state["shared_workspace"] == "ws1"
    assert state["artifact_paths"]["spec"].endswith(
        f"docs/pipeline/runs/{RUN_ID}/spec.md"
    )
    assert state["artifact_paths"]["report"].endswith(f"pipeline-{RUN_ID}.md")
    assert result.feature_source == state["feature_source"]
    assert result.issue_id == "001"
    assert result.branch == branch
    # A prepared run writes no terminal report — the orchestrator still owns that.
    assert not (tmp_path / "reports" / f"pipeline-{RUN_ID}.md").exists()


def test_pipeline_prepare_prints_resolved_values(
    tmp_path: Path,
    no_open_prs: None,
    isolated_state: FakeHerdr,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 1: the resolved values cross the shell boundary as machine-parsed `KEY=VALUE`
    lines on **stdout**, with the log noise on stderr — the launcher appends them to the
    orchestrator's prompt header, so anything else in that stream would be prompt text."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(
        parent,
        ("001", "001-pick-a-feature.md", "open"),
        ("002", "002-another.md", "open"),
    )
    client = FakeHerdr(tmp_path / "worktrees")
    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )
    assert result.outcome == "ok"

    code = cli.main(
        [
            "pipeline-prepare",
            "--run-id",
            RUN_ID,
            "--repo-parent",
            str(parent),
            "--report",
            str(tmp_path / "reports" / "cli-report.md"),
            "--deadline-epoch",
            str(DEADLINE_EPOCH),
            "--base",
            base,
        ]
    )
    captured = capsys.readouterr()
    # The CLI run has its own default claims store, so 001 is pickable again here.
    assert code == EXIT_PREPARED, captured.err

    # The second call re-picks nothing (001 is claimed by the first) — so only assert the
    # shape of the stream from the direct render, and the CLI's exit code above.
    values = dict(
        line.split("=", 1) for line in render_prepared_values(result).splitlines()
    )
    assert set(values) == {
        "FEATURE_IDEA",
        "FEATURE_SOURCE",
        "ISSUE_ID",
        "WT",
        "BRANCH",
        "SHARED_WS",
        "STATE_JSON",
    }
    assert values["FEATURE_SOURCE"] == "docs/process/issues/001-pick-a-feature.md"
    assert values["ISSUE_ID"] == "001"
    assert values["BRANCH"] == f"auto/pipeline-{RUN_ID}"
    assert values["STATE_JSON"] == str(result.state_path)
    # Multi-line values survive as one escaped line, so a multi-paragraph feature idea
    # can't break the line format the launcher appends to the prompt header. The two
    # real guarantees are the line count and the round trip; anything more here
    # (`"\n" not in values[...].splitlines()`, `count("\\n") >= 0`) is a tautology that
    # reads like a check but cannot fail.
    rendered = render_prepared_values(result)
    assert len(rendered.splitlines()) == 7
    assert "Do the thing for issue 001." in values["FEATURE_IDEA"].replace("\\n", "\n")

    # And the CLI itself put exactly that KEY=VALUE stream on stdout. The *exact* set,
    # not just "every key it emitted is allowed": the launcher parses this stream, so a
    # key silently dropped from the CLI path (SHARED_WS=, WT=) would otherwise leave the
    # orchestrator with an empty variable and no test failing.
    cli_keys = [
        line.split("=", 1)[0] for line in captured.out.splitlines() if "=" in line
    ]
    assert cli_keys == [
        "FEATURE_IDEA",
        "FEATURE_SOURCE",
        "ISSUE_ID",
        "WT",
        "BRANCH",
        "SHARED_WS",
        "STATE_JSON",
    ], f"expected all seven KEY=VALUE lines on stdout, got: {captured.out!r}"
    assert "INFO" not in captured.out and "pipeline-prepare:" not in captured.out


# --- AC 2: a night with an empty backlog costs a report and nothing else ---------------


def test_pipeline_prepare_no_feature_skips_without_agent(
    tmp_path: Path, no_open_prs: None
) -> None:
    """Acceptance criterion 2: no eligible issue ⇒ `## Outcome: skipped (no_feature)` at
    the report path, exit 3, and no agent — nothing in the Herdr client was touched."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-done.md", "done"))
    report = tmp_path / "reports" / f"pipeline-{RUN_ID}.md"
    client = FakeHerdr(tmp_path / "worktrees")

    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "no_feature"
    text = report.read_text()
    assert text.splitlines()[2] == "## Outcome: skipped (no_feature)"
    # The marker is near the top and greppable in one hit, like every other report.
    assert text.index("## Outcome:") < len(text) // 2
    assert client.calls == []
    assert result.feature_source is None and result.worktree is None
    assert (
        not (tmp_path / "reports" / f"pipeline-{RUN_ID}.md")
        .read_text()
        .count("## Outcome: ok")
    )


def test_pipeline_prepare_no_feature_creates_no_worktree(
    tmp_path: Path, no_open_prs: None
) -> None:
    """The rest of issue 052: an empty backlog must cost *zero* worktrees, workspaces and
    `state.json` — not merely zero stages. The worktree is created after the pick for
    exactly this reason."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-done.md", "done"))
    client = FakeHerdr(tmp_path / "worktrees")
    worktrees_root = tmp_path / "worktrees"

    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "no_feature"
    assert not worktrees_root.exists() or list(worktrees_root.iterdir()) == []
    assert result.state_path is None
    assert result.workspace_id is None
    # No claim either: a run that built nothing must not put an issue in the pool's
    # claimed set (that would be a claim with no run behind it).
    assert not (tmp_path / "claims.json").exists()
    # And the issue file itself is untouched — claiming is out-of-tree by design.
    issue_file = parent / "docs/process/issues/001-done.md"
    assert issue_file.read_text() == ISSUE_TEMPLATE.format(
        id="001", title="Issue 001", status="done"
    )


# --- AC 3: a sync failure is loud -------------------------------------------------------


def test_pipeline_prepare_fails_loud_on_sync_error(
    tmp_path: Path, no_open_prs: None
) -> None:
    """Acceptance criterion 3: a diverged `$REPO_PARENT` writes
    `## Outcome: failed (repo_sync_failed)` and stops before the pick — no claim, no
    worktree, no agent. Same fixture shape as test_sync_repo.py's non-fast-forward test."""
    bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-pick-a-feature.md", "open"))
    # Local commit on the checkout ...
    _git(parent, "config", "user.email", "test@test.com")
    _git(parent, "config", "user.name", "Test")
    (parent / "LOCAL.md").write_text("local\n")
    _git(parent, "add", "LOCAL.md")
    _git(parent, "commit", "-m", "local commit")
    # ... and a different one on origin: not a fast-forward.
    _push_new_commit(bare, tmp_path, "REMOTE.md")

    report = tmp_path / "reports" / f"pipeline-{RUN_ID}.md"
    client = FakeHerdr(tmp_path / "worktrees")
    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "sync_failed"
    assert "## Outcome: failed (repo_sync_failed)" in report.read_text()
    assert client.calls == []
    assert not (tmp_path / "claims.json").exists()
    assert not (tmp_path / "worktrees").exists()
    assert (parent / "LOCAL.md").exists()
    assert not (parent / "REMOTE.md").exists()


# --- AC 4: the deadline is required; the exit codes are distinguishable -----------------


def test_pipeline_prepare_requires_deadline_epoch() -> None:
    """`--deadline-epoch` has no default: a prepare invocation must never be able to invent
    a deadline (the 2026-09-28 incident). argparse's usage error is the loudest possible
    answer."""
    with pytest.raises(SystemExit):
        cli.main(["pipeline-prepare", "--run-id", RUN_ID])


def test_pipeline_prepare_exit_codes_are_distinct(
    tmp_path: Path, no_open_prs: None, isolated_state: FakeHerdr
) -> None:
    """AC 4: {0 prepared, 3 no feature, 1 sync/prepare failure, 2 usage} — and 3 is
    distinct from 1 so the launcher can tell a healthy empty-backlog night from a
    broken one (they want opposite exit statuses from the unit)."""
    _bare_ok, parent_ok, base = _parent_clone(tmp_path)
    _seed_issues(parent_ok, ("001", "001-pick-a-feature.md", "open"))

    # 0 — prepared.
    prepared = cli.main(
        [
            "pipeline-prepare",
            "--run-id",
            RUN_ID,
            "--repo-parent",
            str(parent_ok),
            "--report",
            str(tmp_path / "ok.md"),
            "--deadline-epoch",
            str(DEADLINE_EPOCH),
            "--base",
            base,
        ]
    )

    # 3 — nothing to build. A separate clone, since the first one just claimed 001.
    bare_empty, parent_empty, base_empty = _parent_clone(tmp_path / "empty")
    _seed_issues(parent_empty, ("001", "001-done.md", "done"))
    no_feature = cli.main(
        [
            "pipeline-prepare",
            "--run-id",
            RUN_ID,
            "--repo-parent",
            str(parent_empty),
            "--report",
            str(tmp_path / "empty.md"),
            "--deadline-epoch",
            str(DEADLINE_EPOCH),
            "--base",
            base_empty,
        ]
    )

    # 1 — sync failure. Diverge *this* clone's own origin: a local commit on the
    # checkout plus a different one on origin is not a fast-forward.
    _git(parent_empty, "config", "user.email", "test@test.com")
    _git(parent_empty, "config", "user.name", "Test")
    (parent_empty / "LOCAL.md").write_text("local\n")
    _git(parent_empty, "add", "LOCAL.md")
    _git(parent_empty, "commit", "-m", "local")
    _push_new_commit(bare_empty, tmp_path / "empty", "OTHER.md")
    sync_failed = cli.main(
        [
            "pipeline-prepare",
            "--run-id",
            RUN_ID,
            "--repo-parent",
            str(parent_empty),
            "--report",
            str(tmp_path / "failed.md"),
            "--deadline-epoch",
            str(DEADLINE_EPOCH),
            "--base",
            base_empty,
        ]
    )

    # 2 — argparse usage error (SystemExit carries argparse's own exit code).
    with pytest.raises(SystemExit) as usage:
        cli.main(
            [
                "pipeline-prepare",
                "--run-id",
                RUN_ID,
                "--repo-parent",
                str(parent_ok),
                "--report",
                str(tmp_path / "u.md"),
            ]
        )
    assert usage.value.code == 2

    assert (prepared, no_feature, sync_failed) == (EXIT_PREPARED, EXIT_NO_FEATURE, 1)
    assert EXIT_FAILURE == 1
    assert len({prepared, no_feature, sync_failed, 2}) == 4
    assert EXIT_NO_FEATURE != EXIT_FAILURE


# --- AC 7: state.json is written atomically ---------------------------------------------


def test_pipeline_prepare_writes_state_json_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 7 / G-9: the file only ever appears via `os.replace` of a tmpfile in the *same
    directory* (so a reader never sees a half-written `state.json`), and a failing write
    leaves no `state.json` at all — not a truncated one."""
    target = tmp_path / "wt" / "state.json"
    target.parent.mkdir()
    observed: list[tuple[str, bool]] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        observed.append(
            (
                str(Path(src).parent),
                # the destination must not exist while the tmpfile is being written:
                # that is what makes the replace the only way the file can appear
                not Path(dst).exists(),
            )
        )
        return real_replace(src, dst)

    monkeypatch.setattr("herdr_routines.pipeline_prepare.os.replace", recording_replace)
    _write_state_json(target, {"run_id": RUN_ID, "current_stage": 0})
    import json

    assert observed == [(str(target.parent), True)]
    assert json.loads(target.read_text()) == {"run_id": RUN_ID, "current_stage": 0}
    # No tmpfile left behind next to it.
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]

    def exploding_replace(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr("herdr_routines.pipeline_prepare.os.replace", exploding_replace)
    second = target.parent / "second.json"
    with pytest.raises(OSError):
        _write_state_json(second, {"run_id": RUN_ID})
    assert not second.exists()
    assert list(target.parent.iterdir()) == [target]


# --- AC 8: the sync/pick/state paths need no Herdr server -------------------------------


def test_pipeline_prepare_needs_no_herdr_server(
    tmp_path: Path, no_open_prs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 8: with `herdr` unreachable on PATH, the sync/pick/state half of prepare still
    completes, and the two HerdrClient call sites are the injected fake's — nothing shells
    out to a real `herdr` binary. This is what makes the whole step testable in CI."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-pick-a-feature.md", "open"))

    # A PATH with git (the fixture needs it) and no herdr at all.
    only_bin = tmp_path / "onlybin"
    only_bin.mkdir()
    os.symlink(shutil.which("git") or "git", only_bin / "git")
    monkeypatch.setenv("PATH", str(only_bin))
    assert shutil.which("herdr") is None

    client = FakeHerdr(tmp_path / "worktrees")
    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "ok"
    assert client.calls == [
        "worktree_create_full",
        "workspace_list",
        "workspace_create",
    ]
    assert result.state_path is not None and result.state_path.exists()

    # Same posture on the no-feature path, which must not even construct a client call.
    _bare2, parent2, base2 = _parent_clone(tmp_path / "second")
    _seed_issues(parent2, ("001", "001-done.md", "done"))
    client2 = FakeHerdr(tmp_path / "worktrees2")
    skipped = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent2, base=base2, client=client2)
    )
    assert skipped.outcome == "no_feature"
    assert client2.calls == []


# --- the shared-workspace lookup: the launcher's label collision (review blocking #2) -----


def test_ensure_shared_workspace_reuses_only_the_workspace_on_the_run_worktree(
    tmp_path: Path, no_open_prs: None
) -> None:
    """`scripts/pipeline-launch.sh` creates a *second* workspace with the identical
    `pipeline-{run_id}` label for the orchestrator, rooted at `$REPO_PARENT`. A
    label-only match returns whichever `workspace list` happens to order first, and
    every stage worker spawned into that one operates on the parent clone instead of the
    run's branch. So the lookup requires the label *and* `worktree.checkout_path` to
    match the run's worktree — the closest real key to a `cwd` (live `herdr workspace
    list` entries carry no `cwd`). This is the reuse branch, which until now no test
    exercised at all."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-pick-a-feature.md", "open"))
    worktrees_root = tmp_path / "worktrees"
    client = FakeHerdr(worktrees_root)
    label = f"pipeline-{RUN_ID}"
    # The orchestrator's workspace first in the list — exactly the ordering that made the
    # old label-only lookup return the wrong one. It is rooted at $REPO_PARENT, which is
    # itself a checkout, so herdr reports a `worktree.checkout_path` for it too: the
    # label alone genuinely cannot tell these two apart.
    client.existing_workspaces = [
        {
            "workspace_id": "ws-orchestrator",
            "label": label,
            "worktree": {"checkout_path": str(parent), "is_linked_worktree": False},
        },
        {
            "workspace_id": "ws-shared",
            "label": label,
            "worktree": {
                "checkout_path": str(worktrees_root / f"auto-pipeline-{RUN_ID}")
            },
        },
    ]

    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "ok"
    assert result.workspace_id == "ws-shared"
    # Reused, not re-created — a relaunch after a crash must not pile up workspaces.
    assert "workspace_create" not in client.calls
    assert client.created_workspaces == []


def test_ensure_shared_workspace_ignores_worktree_workspace_opened_by_herdr(
    tmp_path: Path, no_open_prs: None
) -> None:
    """The mirror trap, and the reason the match needs *both* conditions rather than
    just `checkout_path`: `worktree create` already opens a workspace on the new
    worktree and reports it as `worktree.open_workspace_id`, and that workspace carries
    herdr's default label, not ours — and not `HERDR_ENV=1`, in which every `herdr` call
    from inside an agent settles `blocked`. Matching on the path alone would hand it
    back."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-pick-a-feature.md", "open"))
    worktrees_root = tmp_path / "worktrees"
    client = FakeHerdr(worktrees_root)
    worktree_path = worktrees_root / f"auto-pipeline-{RUN_ID}"
    client.existing_workspaces = [
        {
            "workspace_id": "w65",
            "label": "auto-pipeline",
            "worktree": {"checkout_path": str(worktree_path)},
        }
    ]

    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "ok"
    # Created, and forked with HERDR_ENV=1 — never the unenv'd workspace herdr opened.
    assert result.workspace_id == "ws1"
    assert client.created_workspaces == [
        (str(worktree_path), f"pipeline-{RUN_ID}", {"HERDR_ENV": "1"})
    ]


def test_ensure_shared_workspace_creates_when_entry_has_no_worktree_key(
    tmp_path: Path, no_open_prs: None
) -> None:
    """Live `herdr workspace list` omits `worktree` entirely (not null) for a workspace
    whose cwd is not a checkout. A chained `.get()` on the missing key must not raise —
    it must fall through to create, which is the same answer a genuinely new run gets."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-pick-a-feature.md", "open"))
    client = FakeHerdr(tmp_path / "worktrees")
    client.existing_workspaces = [
        {"workspace_id": "ws-plain", "label": f"pipeline-{RUN_ID}"}
    ]

    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "ok"
    assert result.workspace_id == "ws1"
    assert "workspace_create" in client.calls


# --- herdr down: the most likely real prepare failure, finally covered (review NB #7) -----


@pytest.mark.parametrize(
    ("raise_on", "expected_marker"),
    [
        ("worktree_create_full", "## Outcome: failed (worktree_setup_failed)"),
        ("workspace_list", "## Outcome: failed (worktree_setup_failed)"),
        ("workspace_create", "## Outcome: failed (worktree_setup_failed)"),
    ],
)
def test_pipeline_prepare_reports_herdr_failure_instead_of_raising(
    tmp_path: Path, no_open_prs: None, raise_on: str, expected_marker: str
) -> None:
    """AC 3's sibling: when the herdr server is down, every step of the pre-flight's
    Herdr half raises `HerdrCliError`, and `prepare_run` must turn that into a terminal
    report plus a `prepare_failed` result — never an exception out of the function whose
    "Never raises" contract the launcher's {0, 3, 1, 2} exit-code table is built on.

    This is the coverage that was missing while the client read `result.branch`: the
    fake's `raise_on` was wired to a bare `RuntimeError`, which `except (HerdrCliError,
    OSError)` would not even have caught, so no test could have reached this handler."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-pick-a-feature.md", "open"))
    report = tmp_path / "reports" / f"pipeline-{RUN_ID}.md"
    client = FakeHerdr(tmp_path / "worktrees")
    client.raise_on = raise_on

    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
    )

    assert result.outcome == "prepare_failed"
    assert result.state_path is None and result.workspace_id is None
    assert expected_marker in report.read_text()
    # The claim already on disk is named, so the morning report can say which issue went
    # into the claimed set instead of the claim silently self-healing later.
    assert "claimed issue: 001" in report.read_text()
    assert "001" in load_claims(tmp_path / "claims.json")


def test_pipeline_prepare_never_raises_on_an_unwritable_report_path(
    tmp_path: Path, no_open_prs: None
) -> None:
    """The docstring promises a `PrepareResult` plus a report; under a report path whose
    parent is a regular file, `path.parent.mkdir(…)` raises `NotADirectoryError`, which
    escaped the function entirely. The launcher then printed "report already at $REPORT"
    for a report that did not exist and the run sat `running` until its deadline. The
    run is unrecoverable either way, so the contract is kept and the write degrades to
    log-only — the exit code is still 1, which is what the launcher branches on."""
    _bare, parent, base = _parent_clone(tmp_path)
    _seed_issues(parent, ("001", "001-done.md", "done"))
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    client = FakeHerdr(tmp_path / "worktrees")

    result = prepare_run(
        **_prepare_kwargs(tmp_path, parent=parent, base=base, client=client)
        | {"report": blocker / "reports" / f"pipeline-{RUN_ID}.md"}
    )

    assert result.outcome == "no_feature"
    assert not (blocker / "reports").exists()
    assert client.calls == []


# --- AC 14: the follow-on issue exists before 054 is closed ------------------------------


def test_phase_b_and_c_filed_as_follow_on_issue_055() -> None:
    """Acceptance criterion 14, as a doc contract: flipping 054 to `done` without filing
    the remaining phases would retire them from the backlog with only a `gate:` line
    nobody reads, leaving the feature 1/3 shipped and looking complete."""
    follow_on = ISSUES_DIR / "055-orchestrator-stage-loop-in-code.md"
    assert follow_on.exists(), "issue 055 (phases B and C) must exist"
    follow_on_text = follow_on.read_text()
    assert "status: open" in follow_on_text
    # 055 carries the phase B and C criteria (and their test names) from 054.
    for test_name in (
        "test_pipeline_run_records_real_stage_sessions",
        "test_pipeline_run_aborts_on_gate_failure",
        "test_pipeline_run_writes_heartbeat",
        "test_pipeline_run_partial_on_deadline",
        "test_pipeline_run_quota_marker_fast_fails",
        "test_pipeline_stage4_opens_pr_without_agent",
    ):
        assert test_name in follow_on_text, f"055 lost acceptance test {test_name}"
    assert "pipeline-run" in follow_on_text

    picked = (ISSUES_DIR / "054-orchestrator-mechanical-steps-to-code.md").read_text()
    assert "status: done" in picked
    assert "gate:" in picked and "055" in picked.split("gate:", 1)[1].splitlines()[0]

    superseded = (
        ISSUES_DIR / "052-pipeline-launches-with-no-feature-to-build.md"
    ).read_text()
    assert "status: done" in superseded
