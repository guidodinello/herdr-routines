"""Target selectors (issue 073): code picks what a routine run acts on, and skips the run
when there is nothing. The behaviour tests go through `run_tick` and `cli.main`, so they
fail when the wiring is broken, not only when a selector function is."""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli, tick, verify
from herdr_routines.auto_fix import PostedBody, RealGhClient
from herdr_routines.config import (
    MULTI_TARGET_SELECTORS,
    VALID_TARGET_SELECTORS,
    Job,
    RoutinesConfig,
    load_config,
    load_config_dir,
)
from herdr_routines.history import read_job
from herdr_routines.target_selectors import (
    MULTI_TARGET_REGISTRY,
    TARGET_SELECTORS,
    SelectorContext,
    SelectorFailed,
    select_target,
)
from herdr_routines.tick import run_tick
from herdr_routines.verify import VerifyContext
from tests.test_runner import ScriptedClient
from tests.test_tick import FakeFullClient
from tests.test_tick import make_job as make_tick_job

REVIEW_BODY = "### Code review\n\nNo blocking issues.\n"
COPILOT_QUOTA_NOTICE = "Copilot was unable to review this pull request: quota reached."
PROMPT = (
    "Run /code-review on $ROUTINE_TARGET, then write your summary to $ROUTINE_REPORT"
)
REPO_URL = "https://github.com/acme/widgets.git"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("herdr_routines.runner.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)


def _pr(
    number: int,
    *,
    login: str = "alice",
    is_bot: bool = False,
    draft: bool = False,
) -> dict[str, object]:
    return {
        "number": number,
        "isDraft": draft,
        "author": {"login": login, "is_bot": is_bot},
    }


def _fresh(author: str = "me", body: str = REVIEW_BODY) -> PostedBody:
    return PostedBody(
        author=author, body=body, created_at=datetime.now(UTC) + timedelta(minutes=1)
    )


class FakeSelectorGh:
    """The slice of GhClient the selector and the review_posted gate touch. A PR's first
    `pr_review_bodies` call (the selector's) returns `before`, later ones (the gate's)
    return `after`, so a test can model "unreviewed, then the agent posted a review"."""

    def __init__(
        self,
        prs: list[dict[str, object]],
        *,
        before: dict[int, list[PostedBody] | None] | None = None,
        after: dict[int, list[PostedBody]] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.prs = prs
        self.before = before or {}
        self.after = after or {}
        self.raise_on = raise_on
        self.probed: list[tuple[str, str, int]] = []
        self.list_calls: list[tuple[str, str]] = []

    def api_user(self) -> str:
        if self.raise_on == "api_user":
            raise RuntimeError("gh auth failed")
        return "me"

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        self.list_calls.append((owner, repo))
        if self.raise_on == "pr_list":
            raise RuntimeError("gh pr list failed")
        return list(self.prs)

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[PostedBody] | None:
        first = not any(p[2] == number for p in self.probed)
        self.probed.append((owner, repo, number))
        if first:
            return self.before.get(number, [])
        return self.after.get(number, [])


class RecordingClient(FakeFullClient):
    """A herdr fake that records whether anything was spawned and what the agent was sent,
    and writes the report the agent would."""

    def __init__(self, *, report: str = "# ok\n") -> None:
        super().__init__()
        self.report = report
        self.spawned: list[str] = []
        self.prompts: list[str] = []

    def tab_create(self, *, cwd, label=None):
        self.spawned.append("tab_create")
        return super().tab_create(cwd=cwd, label=label)

    def worktree_create(self, *, cwd, branch, base, label=None):
        self.spawned.append("worktree_create")
        return super().worktree_create(cwd=cwd, branch=branch, base=base, label=label)

    def agent_start(self, **kwargs: Any) -> None:
        self.spawned.append("agent_start")
        super().agent_start(**kwargs)

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self.prompts.append(text)
        status = super().agent_prompt_wait(
            target=target, text=text, timeout_ms=timeout_ms
        )
        Path(text.rsplit(maxsplit=1)[-1]).write_text(self.report)
        return status


def _git_checkout(path: Path, remote: str = REPO_URL) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
    )
    return path


def _job(tmp_path: Path, **overrides: Any) -> Job:
    """The production shape of the fitted job: a plain `repo:` checkout (origin resolves
    the owner/name), `pick` and `review_posted`."""
    return replace(
        make_tick_job(tmp_path, repo=_git_checkout(tmp_path / "checkout")),
        prompt=PROMPT,
        pick="oldest_unreviewed_pr",
        verify=("review_posted",),
        **overrides,
    )


def _tick_twice(job: Job, client: RecordingClient, history: Path):
    """The first tick registers the job, the second runs it; returns the second's outcome."""
    config = RoutinesConfig(jobs=(job,))
    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history, client=client, now=t0)  # type: ignore[arg-type]
    return run_tick(config, history, client=client, now=t0 + timedelta(minutes=1))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("notify_policy", "notified"),
    [("terminal", False), ("on-failure", False), ("always", True)],
)
def test_selector_no_target_skips_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notify_policy: str,
    notified: bool,
) -> None:
    gh = FakeSelectorGh([_pr(5)], before={5: [_fresh()]})
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    job = _job(
        tmp_path,
        notify_policy=notify_policy,
        retry_attempts=2,
        retry_on=("no_report",),
    )
    history = tmp_path / "state" / "history.jsonl"
    client = RecordingClient()
    config = RoutinesConfig(jobs=(job,))
    t0 = datetime.now(UTC).replace(microsecond=0)
    run_tick(config, history, client=client, now=t0)  # type: ignore[arg-type]
    outcome = run_tick(config, history, client=client, now=t0 + timedelta(minutes=1))  # type: ignore[arg-type]

    assert outcome.summaries == ("a: skipped (no_target)",)
    assert outcome.any_job_failed is False
    assert client.spawned == []
    assert client.prompts == []
    terminal = read_job(history, "a")[-1]
    assert terminal.state == "skipped"
    assert terminal.run_id is not None
    assert terminal.extra is not None
    assert terminal.extra["reason"] == "no_target"
    assert [n[0] for n in client.notifications] == (
        ["herdr-routines: a skipped"] if notified else []
    )

    # The skipped record is the occurrence having run: never missed, never retried.
    later = run_tick(config, history, client=client, now=t0 + timedelta(minutes=1))  # type: ignore[arg-type]
    assert later.summaries == ("a: not due",)
    assert client.spawned == []
    assert not [r for r in read_job(history, "a") if r.state == "missed"]


@pytest.mark.parametrize(
    "report",
    ["# ok\n", "# ok\n\nTarget: acme/widgets#99\n"],
    ids=["no-line", "other-pr"],
)
def test_selector_target_reaches_prompt_history_and_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str
) -> None:
    """Code picked #7 (the draft #3 is skipped). The report neither needs to name it nor
    can redirect the gate to another PR."""
    gh = FakeSelectorGh([_pr(7), _pr(3, draft=True)], after={7: [_fresh()], 99: []})
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    history = tmp_path / "state" / "history.jsonl"
    client = RecordingClient(report=report)

    outcome = _tick_twice(_job(tmp_path), client, history)

    assert outcome.summaries == ("a: done",)
    assert len(client.prompts) == 1
    assert "acme/widgets#7" in client.prompts[0]
    assert "$ROUTINE_TARGET" not in client.prompts[0]
    records = read_job(history, "a")
    running, terminal = records[-2], records[-1]
    assert running.state == "running"
    assert running.extra is not None
    assert running.extra["target"] == "acme/widgets#7"
    assert terminal.state == "done"
    assert terminal.extra is not None
    assert terminal.extra["target"] == "acme/widgets#7"
    assert {p[2] for p in gh.probed} == {7}
    assert gh.list_calls == [("acme", "widgets")]


def test_oldest_unreviewed_pr_selection_rules(tmp_path: Path) -> None:
    prs = [
        _pr(12),  # unsorted on purpose: the lowest remaining number wins
        _pr(11, draft=True),
        _pr(10, login="dependabot[bot]", is_bot=True),
        _pr(9, login="app/dependabot", is_bot=True),
        _pr(8, login="app/renovate"),  # a GitHub App by its login alone
        _pr(7, login="deploy-helper", is_bot=True),  # a bot by gh's flag alone
        _pr(6, login="github-actions"),
        _pr(5, login="copilot-swe-agent"),
        _pr(4),  # already carries a trusted code review
        _pr(3),  # an untrusted marker and Copilot's quota notice: not a review
    ]
    gh = FakeSelectorGh(
        prs,
        before={
            4: [_fresh("me")],
            3: [
                _fresh("stranger"),
                _fresh("copilot-pull-request-reviewer", COPILOT_QUOTA_NOTICE),
            ],
        },
    )
    job = replace(
        make_tick_job(tmp_path), repository=REPO_URL, pick="oldest_unreviewed_pr"
    )
    ctx = SelectorContext(job=job, gh=gh, trusted_review_authors=())  # type: ignore[arg-type]

    picked = select_target(ctx)
    assert picked is not None
    assert not isinstance(picked, SelectorFailed)
    assert picked.ref == "acme/widgets#3"

    gh.prs = [p for p in prs if p["number"] != 3]
    again = select_target(ctx)
    assert again is not None
    assert not isinstance(again, SelectorFailed)
    assert again.ref == "acme/widgets#12"

    gh.prs = []
    assert select_target(ctx) is None


def test_selector_and_gate_share_reviewed_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = FakeSelectorGh([_pr(5)], before={5: [_fresh()]}, after={5: [_fresh()]})
    job = replace(
        make_tick_job(tmp_path), repository=REPO_URL, pick="oldest_unreviewed_pr"
    )
    selector_ctx = SelectorContext(job=job, gh=gh, trusted_review_authors=())  # type: ignore[arg-type]
    report = tmp_path / "report.md"
    report.write_text("# ok\n")
    target = verify.VerifyTarget("acme", "widgets", 5)

    def gate() -> bool:
        return verify.review_posted(
            VerifyContext(
                job=job,
                run_id="a-1",
                started_at=datetime.now(UTC),
                report_path=report,
                gh=gh,  # type: ignore[arg-type]
                trusted_review_authors=(),
                target=target,
            )
        ).passed

    # The real predicate: the PR counts as reviewed, so the selector has nothing and the
    # gate passes.
    assert select_target(selector_ctx) is None
    assert gate() is True

    # Change it once: both flip.
    monkeypatch.setattr(verify, "is_trusted_review", lambda *a, **k: False)
    assert select_target(selector_ctx) == target
    assert gate() is False


def _json_garbage_gh(monkeypatch: pytest.MonkeyPatch) -> RealGhClient:
    """The real client whose `gh pr list` exits 0 with output that is not JSON."""

    def fake_run(self: RealGhClient, argv: list[str]) -> tuple[int, str, str]:
        return (0, "me\n", "") if "user" in argv else (0, "<html>oops", "")

    monkeypatch.setattr(RealGhClient, "_run", fake_run)
    return RealGhClient()


@pytest.mark.parametrize(
    "failure",
    [
        "pr_list",
        "api_user",
        "bodies_unreadable",
        "listing_at_limit",
        "undecodable_json",
    ],
)
def test_selector_error_fails_run_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    gh: Any
    if failure == "bodies_unreadable":
        gh = FakeSelectorGh([_pr(5)], before={5: None})
    elif failure == "listing_at_limit":
        gh = FakeSelectorGh([_pr(n) for n in range(1, 101)])
    elif failure == "undecodable_json":
        gh = _json_garbage_gh(monkeypatch)
    else:
        gh = FakeSelectorGh([_pr(5)], raise_on=failure)
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    history = tmp_path / "state" / "history.jsonl"
    client = RecordingClient()

    outcome = _tick_twice(_job(tmp_path), client, history)

    assert outcome.summaries == ("a: failed (selector_failed)",)
    assert outcome.any_job_failed is True
    assert client.spawned == []
    terminal = read_job(history, "a")[-1]
    assert terminal.state == "failed"
    assert terminal.extra is not None
    assert terminal.extra["reason"] == "selector_failed"
    assert [n[0] for n in client.notifications] == ["herdr-routines: a failed"]


def _manual_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _git_checkout(tmp_path / "checkout")
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\njobs:\n  - name: a\n    cron: '0 3 * * *'\n    repo: {checkout}\n"
        f"    prompt: {PROMPT!r}\n    pick: oldest_unreviewed_pr\n"
        "    verify: [review_posted]\n"
    )
    monkeypatch.setattr(cli, "default_config_path", lambda: config_path)
    monkeypatch.setattr(cli, "default_lock_path", lambda: tmp_path / "tick.lock")


class _PromptScriptedClient(ScriptedClient):
    prompts: list[str]

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self.prompts = [*getattr(self, "prompts", []), text]
        return super().agent_prompt_wait(
            target=target, text=text, timeout_ms=timeout_ms
        )


def test_manual_run_uses_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manual_config(tmp_path, monkeypatch)
    reports = tmp_path / "state" / "reports"
    scripted = _PromptScriptedClient(
        write_report_at=reports / "a-manual.md", report_content="# ok\n"
    )
    monkeypatch.setattr(cli, "HerdrClient", lambda: scripted)
    argv = ["run", "a", "--run-id", "a-manual"]

    # No target: skipped, exit 0, nothing spawned.
    monkeypatch.setattr(
        tick, "RealGhClient", lambda: FakeSelectorGh([_pr(5)], before={5: [_fresh()]})
    )
    assert cli.main(argv) == 0
    assert scripted.calls == []

    # A selector error: failed, exit 1, nothing spawned.
    monkeypatch.setattr(
        tick, "RealGhClient", lambda: FakeSelectorGh([_pr(5)], raise_on="pr_list")
    )
    assert cli.main(argv) == 1
    assert scripted.calls == []

    # A target: it reaches the prompt and the gate, with no Target: line in the report.
    gh = FakeSelectorGh([_pr(5)], after={5: [_fresh()]})
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    assert cli.main(argv) == 0
    assert "acme/widgets#5" in scripted.prompts[0]
    assert {p[2] for p in gh.probed} == {5}


def _write_jobs_d(tmp_path: Path, files: dict[str, str]) -> Path:
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir()
    for name, content in files.items():
        (jobs_dir / name).write_text(content)
    return jobs_dir


def test_selector_config_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    routine = (
        f"name: a\ncron: '0 3 * * *'\nrepo: {repo}\nprompt: write $ROUTINE_REPORT\n"
    )

    # Unknown / non-string names.
    for i, bad in enumerate(("nope", "[oldest_unreviewed_pr]", "7")):
        root = tmp_path / f"bad{i}"
        root.mkdir()
        jobs_dir = _write_jobs_d(root, {"a.yaml": routine + f"pick: {bad}\n"})
        # The directory loader reports a bad job file in `errors` instead of raising.
        cfg = load_config_dir(jobs_dir)
        assert cfg.jobs == ()
        assert any("'pick'" in err for err in cfg.errors), bad

    # Explicit on a non-routine kind is an error.
    root = tmp_path / "explicit"
    root.mkdir()
    jobs_dir = _write_jobs_d(
        root,
        {
            "g.yaml": f"name: g\ncron: '0 3 * * *'\nrepo: {repo}\nkind: gated\n"
            "checks:\n  - command: 'true'\npick: oldest_unreviewed_pr\n"
        },
    )
    cfg = load_config_dir(jobs_dir)
    assert cfg.jobs == ()
    assert any("only supported on kind: routine" in err for err in cfg.errors)

    # `kind: review` fixes its selector, so an explicit `pick` is refused too.
    root = tmp_path / "explicit-review"
    root.mkdir()
    jobs_dir = _write_jobs_d(
        root,
        {
            "r.yaml": "name: r\ncron: '0 3 * * *'\nkind: review\nselect: [author]\n"
            "pick: oldest_unreviewed_pr\n"
        },
    )
    cfg = load_config_dir(jobs_dir)
    assert cfg.jobs == ()
    assert any("fixed by kind: review" in err for err in cfg.errors)

    # Inherited: a routine honors it, another kind keeps it aside and `validate` says so.
    root = tmp_path / "inherited"
    root.mkdir()
    jobs_dir = _write_jobs_d(
        root,
        {
            "defaults.yaml": "pick: oldest_unreviewed_pr\n",
            "g.yaml": f"name: g\ncron: '0 3 * * *'\nrepo: {repo}\nkind: gated\n"
            "checks:\n  - command: 'true'\n",
            "r.yaml": "name: r\ncron: '0 3 * * *'\nkind: review\nselect: [author]\n",
            "a.yaml": routine,
            "nopick.yaml": routine.replace("name: a", "name: nopick") + "pick: null\n",
        },
    )
    config = load_config(jobs_dir)
    by_name = {j.name: j for j in config.jobs}
    assert by_name["a"].pick == "oldest_unreviewed_pr"
    assert by_name["nopick"].pick is None
    assert by_name["g"].pick is None
    assert by_name["g"].ignored_pick == "oldest_unreviewed_pr"
    # `kind: review` fixes its own selector: the default is overridden, not ignored.
    assert by_name["r"].pick == "my_open_prs"
    assert by_name["r"].ignored_pick is None
    cli._cmd_validate(
        argparse.Namespace(config=jobs_dir, systemd_unit=tmp_path / "x.service")
    )
    err = capsys.readouterr().err
    assert "g: inherits pick from defaults but kind gated does not honor it" in err
    assert "a: inherits pick" not in err
    assert "r: inherits pick" not in err


def test_selector_registry_matches_the_config_names() -> None:
    assert set(TARGET_SELECTORS) | set(MULTI_TARGET_REGISTRY) == VALID_TARGET_SELECTORS
    assert set(MULTI_TARGET_REGISTRY) == MULTI_TARGET_SELECTORS
    assert not set(TARGET_SELECTORS) & set(MULTI_TARGET_REGISTRY)
