"""Tests for `herdr-routines self-update` (issue 053) — the runner checkout's
self-maintenance, and the gates the spec for run 20260929T050000Z puts in front of it.

Every acceptance test the spec names lives here, under the name it names. Two
properties of the suite are load-bearing and stated up front, because they are what
the spec's criterion 21 asks for:

- **Git is real.** Each test builds a temp bare repo plus a clone (the
  `_init_bare_git_repo` shape already proven in `tests/test_sync_repo.py`) and runs
  actual `git` subprocesses. Only three things are faked, each behind the seam the
  spec names: the `validate` subprocess (injected `runner=`), the CI query (injected
  `gh`) and the notification (injected `notify`).
- **A false "updated" is the failure mode this feature exists to prevent.** The
  2026-09-27 incident was a merged fix that silently never ran on the host it was
  written for, so several tests below assert the *negative* — no notification, no
  git change, exit 0 — as carefully as the positive.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import cli, self_update
from herdr_routines.config import Job, RoutinesConfig
from herdr_routines.history import HistoryRecord, append
from herdr_routines.self_update import SelfUpdateResult, run_self_update

REPO_ROOT = Path(__file__).resolve().parents[1]

# A green commit in the shape `gh api .../check-runs` actually returns: lowercase
# `status`/`conclusion` (the REST vocabulary), which is *not* the GraphQL rollup's.
GREEN_REST_CHECKS: list[dict[str, object]] = [
    {"name": "Lint Python", "status": "completed", "conclusion": "success"},
    {"name": "pytest", "status": "completed", "conclusion": "success"},
]

PASSING_VALIDATE = "ok: 2 job(s) valid\n"
NOW = datetime(2026, 9, 29, 21, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# git sandbox
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _init_bare_git_repo(path: Path) -> None:
    """Same three-step shape as tests/test_sync_repo.py — already proven in this
    suite, so this suite's git is the real thing rather than a mock."""
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


def _default_branch(bare: Path) -> str:
    return _git(bare, "symbolic-ref", "HEAD").strip().removeprefix("refs/heads/")


def _clone(bare: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", str(bare), str(dest)],
        capture_output=True,
        text=True,
        check=True,
    )


def _commit(repo: Path, rel: str, content: str, message: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


class Sandbox:
    """A bare repo, the runner checkout under test, and a second clone that pushes to
    `base` — so "upstream moved" is a real commit, not a stubbed fetch."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # Several tests want more than one independent sandbox, so the caller passes
        # a not-yet-existing subdirectory of tmp_path.
        root.mkdir(parents=True, exist_ok=True)
        self.bare = root / "bare.git"
        _init_bare_git_repo(self.bare)
        self.base = _default_branch(self.bare)
        self.checkout = root / "checkout"
        self.work = root / "work"
        _clone(self.bare, self.checkout)
        _clone(self.bare, self.work)
        for repo in (self.checkout, self.work):
            _git(repo, "config", "user.email", "test@test.com")
            _git(repo, "config", "user.name", "Test")
        self.lock_path = root / "tick.lock"
        self.history_path = root / "history.jsonl"
        self.state_path = root / "self-update.json"

    def head(self, repo: Path | None = None) -> str:
        return _git(repo or self.checkout, "rev-parse", "HEAD").strip()

    def porcelain(self, repo: Path | None = None) -> str:
        return _git(repo or self.checkout, "status", "--porcelain")

    def push(
        self, rel: str = "NEW.md", content: str = "new\n", message: str = "add"
    ) -> str:
        """Commit on the work clone and push to `base`; returns the new SHA."""
        sha = _commit(self.work, rel, content, message)
        _git(self.work, "push", "origin", f"HEAD:{self.base}")
        return sha

    def run(self, **overrides: Any) -> SelfUpdateResult:
        kwargs: dict[str, Any] = {
            "checkout": self.checkout,
            "lock_path": self.lock_path,
            "history_path": self.history_path,
            "config": RoutinesConfig(jobs=()),
            "gh": FakeGh(),
            "notify": Recorder(),
            "state_path": self.state_path,
            "base": self.base,
            "runner": FakeValidate(),
            "uv_resolver": lambda: "/usr/bin/uv",
        }
        kwargs.update(overrides)
        return run_self_update(**kwargs)


def _sandbox(root: Path, label: str) -> Sandbox:
    """A second, independent sandbox for the one test that needs several."""
    return Sandbox(root / label.replace(" ", "-"))


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# ---------------------------------------------------------------------------
# fakes — only these three seams
# ---------------------------------------------------------------------------


class FakeGh:
    """Stands in for `RealGhClient` on the `GhClient` protocol surface that
    `run_self_update` uses. Implements the whole protocol so `uv run mypy` — a
    required CI job that checks `tests/` — stays clean (spec finding F1)."""

    def __init__(
        self,
        checks: list[dict[str, object]] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        self.checks = GREEN_REST_CHECKS if checks is None else checks
        self.error = error
        self.calls: list[str] = []

    def commit_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, object]]:
        self.calls.append(sha)
        if self.error is not None:
            raise RuntimeError(self.error)
        return list(self.checks)

    def pr_review_comments(
        self, *, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        return []

    def api_user(self) -> str:
        return "testuser"

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        return []

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        return {}

    def pr_create(
        self, *, owner: str, repo: str, branch: str, title: str, body: str
    ) -> int:
        return 1

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        return {"data": {}}


class FakeValidate:
    """The out-of-process `validate` seam. Records every invocation (argv, cwd,
    timeout) so a test can assert the child really is
    `[uv, run, --frozen, herdr-routines, validate]` run in the checkout — and can
    never be reached at all (`calls == []`) where the flow must stop earlier."""

    def __init__(
        self,
        results: list[tuple[int, str]] | None = None,
        *,
        default: tuple[int, str] = (0, PASSING_VALIDATE),
        before_return: Callable[[int], None] | None = None,
        timeout_on: int | None = None,
    ) -> None:
        self.results = results or []
        self.default = default
        self.before_return = before_return
        self.timeout_on = timeout_on
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": kwargs.get("cwd"),
                "timeout": kwargs.get("timeout"),
            }
        )
        index = len(self.calls) - 1
        if self.before_return is not None:
            self.before_return(index)
        if self.timeout_on == index:
            raise subprocess.TimeoutExpired(
                cmd=argv, timeout=kwargs.get("timeout") or 0
            )
        code, out = self.results[index] if index < len(self.results) else self.default
        return subprocess.CompletedProcess(argv, code, out, "")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, title: str, body: str | None = None) -> None:
        self.calls.append((title, body))

    @property
    def body(self) -> str:
        return "\n".join(b or "" for _title, b in self.calls)

    @property
    def titles(self) -> str:
        return "\n".join(title for title, _ in self.calls)


def _pipeline_job(repo: Path, name: str = "feature-pipeline") -> Job:
    return Job(
        name=name,
        enabled=True,
        cron="0 21 * * *",
        repo=repo,
        workspace="root",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="",
        timeout_ms=60_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
        kind="pipeline",
        prompt_file="prompt.md",
        deadline_ms=3_600_000,
    )


def _open_pipeline_run_history(history_path: Path, job_name: str, run_id: str) -> None:
    """A `running` record for a kind: pipeline job with no terminal record for that
    run_id — the exact state `tick._open_pipeline_run` reports as open."""
    append(
        history_path,
        HistoryRecord(
            ts=NOW, job=job_name, state="running", run_id=run_id, extra={"run": run_id}
        ),
    )


# ---------------------------------------------------------------------------
# 1. the feature: a checkout behind origin/<base> is fast-forwarded and validated
# ---------------------------------------------------------------------------


def test_self_update_fast_forwards_and_validates(sandbox: Sandbox) -> None:
    """Criterion 1. The checkout's *real* HEAD ends up at the new SHA, `validate` ran
    out of process, and the report carries `old..new` with a commit count."""
    old = sandbox.head()
    new = sandbox.push("NEW.md", "new\n", "add NEW.md")

    notify = Recorder()
    validate = FakeValidate()
    result = sandbox.run(notify=notify, runner=validate)

    assert result.status == "updated"
    assert (result.old, result.new) == (old, new)
    assert sandbox.head() == new
    assert sandbox.porcelain() == ""
    assert f"{old}..{new}" in notify.body
    assert "1 commit" in notify.body
    assert str(sandbox.checkout) in notify.body
    # Two validate invocations: the pre-update baseline and the post-update check.
    assert len(validate.calls) == 2


# ---------------------------------------------------------------------------
# 2. a validate that fails (an import error in the new code) rolls the host back
# ---------------------------------------------------------------------------


def test_self_update_rolls_back_on_validate_failure(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 2. The parent process still has the *old* code imported, so an import
    error in the new code is data, not a crash — and the checkout goes back to `old`
    with a notification carrying the validate output. The rollback argv is
    `git reset --keep <old>` and nothing else: no `--hard`, no force."""
    old = sandbox.head()
    sandbox.push("NEW.md", "new\n", "add NEW.md")

    # Recorded from the argv the implementation actually builds (its own `_git`
    # calls), not from an argv the test writes itself — a test that builds the
    # expected argv and compares it to itself pins nothing.
    rollback_argv: list[list[str]] = []
    all_argv: list[list[str]] = []
    real_git = self_update._git

    def recording_git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
        argv = ["git", "-C", str(checkout), *args]
        all_argv.append(argv)
        if args and args[0] == "reset":
            rollback_argv.append(argv)
        return real_git(checkout, *args)

    monkeypatch.setattr(self_update, "_git", recording_git)

    notify = Recorder()
    result = sandbox.run(
        notify=notify,
        runner=FakeValidate(
            results=[(0, PASSING_VALIDATE), (1, "error: cannot import name 'boom'")]
        ),
    )

    assert result.status == "rolled_back"
    assert result.old == old
    assert sandbox.head() == old
    assert sandbox.porcelain() == ""
    assert "cannot import name 'boom'" in notify.body
    assert rollback_argv, "expected a rollback attempt"
    for argv in rollback_argv:
        assert argv[-2:] == ["--keep", old]
    for argv in all_argv:
        assert "--hard" not in argv
        assert "--force" not in argv


# ---------------------------------------------------------------------------
# 3. never swap code under an in-flight overnight run
# ---------------------------------------------------------------------------


def test_self_update_defers_while_pipeline_run_open(sandbox: Sandbox) -> None:
    """Criterion 3. A `kind: pipeline` job's real work runs in a detached
    `systemd-run --user` unit that holds no tick lock for the (hours-long) run, so the
    lock alone does not cover this: defer with no git change and exit 0."""
    config = RoutinesConfig(jobs=(_pipeline_job(sandbox.checkout),))
    _open_pipeline_run_history(
        sandbox.history_path, "feature-pipeline", "20260929T210000Z"
    )
    old = sandbox.head()
    sandbox.push("NEW.md", "new\n", "add NEW.md")

    notify = Recorder()
    validate = FakeValidate()
    gh = FakeGh()
    result = sandbox.run(config=config, notify=notify, runner=validate, gh=gh)

    assert result.status == "deferred"
    assert "feature-pipeline" in (result.reason or "")
    assert sandbox.head() == old
    assert notify.calls == []
    assert validate.calls == []
    assert gh.calls == []


# ---------------------------------------------------------------------------
# 4. a hand-edited or hand-hotfixed host is never clobbered
# ---------------------------------------------------------------------------


def test_self_update_refuses_dirty_or_diverged(tmp_path: Path) -> None:
    """Criterion 4. Four distinct refuse paths, each leaving the checkout exactly as
    it was and notifying. The detached case matters on its own: nothing would be
    tracking which commit the runner checkout is on, so a detached HEAD that happens
    to contain `origin/<base>` is indistinguishable from a stale one — and the
    branch-relative answers this command gives (the unpushed-commit warning, "already
    up to date") would be meaningless on it."""
    cases: list[tuple[str, Callable[[Sandbox], object]]] = [
        ("dirty", lambda sb: (sb.checkout / "README.md").write_text("hand-edited\n")),
        ("non-main branch", lambda sb: _git(sb.checkout, "checkout", "-b", "side")),
        ("detached HEAD", lambda sb: _git(sb.checkout, "checkout", "--detach")),
        (
            "diverged",
            lambda sb: _commit(sb.checkout, "LOCAL.md", "local\n", "hand fix"),
        ),
    ]

    for label, break_it in cases:
        sb = _sandbox(tmp_path, label)
        # Upstream has moved in every case, so "refuse" is never accidental.
        sb.push("REMOTE.md", "remote\n", "remote commit")
        break_it(sb)
        head_before, porcelain_before = sb.head(), sb.porcelain()

        notify = Recorder()
        result = sb.run(notify=notify)

        assert result.status == "refused", label
        assert sb.head() == head_before, label
        assert sb.porcelain() == porcelain_before, label
        assert len(notify.calls) == 1, label
        assert str(sb.checkout) in notify.body, label


# ---------------------------------------------------------------------------
# 5. the tick lock covers the whole fetch -> validate -> (rollback) sequence
# ---------------------------------------------------------------------------


def test_self_update_respects_tick_lock(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5. A real flock is taken on the temp lock path; the command must be a
    complete no-op — not merely "does not move HEAD", but performs no git call at
    all."""
    old = sandbox.head()
    sandbox.push("NEW.md", "new\n", "add NEW.md")

    git_calls = _record_git_calls(monkeypatch)

    with _held_flock(sandbox.lock_path):
        notify = Recorder()
        result = sandbox.run(notify=notify)

    assert result.status == "deferred"
    assert "lock" in (result.reason or "")
    assert git_calls.calls == []
    assert sandbox.head() == old
    assert notify.calls == []


# ---------------------------------------------------------------------------
# 6. deploy/ changes are reported, never applied
# ---------------------------------------------------------------------------


def test_self_update_reports_deploy_changes(sandbox: Sandbox) -> None:
    """Criterion 6. Systemd units and opencode config are host config with their own
    install steps; applying them from a timer is the one thing here that could take
    the scheduler down. The list goes in the notification and the *installed* copy is
    left alone (the checkout's own copy legitimately moves with the fast-forward)."""
    unit_rel = "deploy/systemd/herdr-routines-digest.service"
    sandbox.push(unit_rel, "# v1\n", "add deploy unit")
    new = sandbox.push(unit_rel, "# v2\n", "bump deploy unit")

    # The host's installed copy, deliberately outside the checkout.
    installed = sandbox.root / "systemd-user" / "herdr-routines-digest.service"
    installed.parent.mkdir(parents=True)
    installed.write_text("# v1\n")

    notify = Recorder()
    result = sandbox.run(notify=notify)

    assert result.status == "updated"
    assert result.new == new
    assert list(result.deploy_changes) == [unit_rel]
    assert unit_rel in notify.body
    assert installed.read_text() == "# v1\n"
    assert (sandbox.checkout / unit_rel).read_text() == "# v2\n"


# ---------------------------------------------------------------------------
# 7. never install a red commit
# ---------------------------------------------------------------------------


def test_self_update_defers_on_non_green_ci(tmp_path: Path) -> None:
    """Criterion 7. The runbook's step 0 ("never ship a red runner"), automated.
    Failing, still-pending and unqueryable all defer with exit 0 and no git change —
    fail-closed, because an unverifiable commit is not one to deploy."""
    cases: list[tuple[str, FakeGh]] = [
        (
            "failing",
            FakeGh(
                [{"name": "pytest", "status": "completed", "conclusion": "failure"}]
            ),
        ),
        (
            "pending",
            FakeGh([{"name": "pytest", "status": "in_progress", "conclusion": None}]),
        ),
        ("unqueryable", FakeGh(error="gh auth failed")),
    ]

    for label, gh in cases:
        sb = _sandbox(tmp_path, label)
        old = sb.head()
        sb.push("NEW.md", "new\n", "add NEW.md")

        notify = Recorder()
        validate = FakeValidate()
        result = sb.run(gh=gh, notify=notify, runner=validate)

        assert result.status == "deferred", label
        assert sb.head() == old, label
        assert sb.porcelain() == "", label
        assert notify.calls == [], label
        assert validate.calls == [], label


# ---------------------------------------------------------------------------
# 8. a silently-dropped job is a rollback (the config-migration hazard)
# ---------------------------------------------------------------------------


def test_self_update_rolls_back_on_job_count_drop(sandbox: Sandbox) -> None:
    """Criterion 8. A reshaped schema can make an old job entry *silently ignored*
    rather than rejected — `validate` still exits 0, it just reports fewer jobs. The
    only signal available is the count in `ok: N job(s) valid` on stdout (spec
    finding F4: `validate` emits no job names on any flag), so that is what the guard
    compares."""
    old = sandbox.head()
    sandbox.push("NEW.md", "new\n", "add NEW.md")

    notify = Recorder()
    result = sandbox.run(
        notify=notify,
        runner=FakeValidate(
            results=[(0, "ok: 3 job(s) valid\n"), (0, "ok: 2 job(s) valid\n")]
        ),
    )

    assert result.status == "rolled_back"
    assert sandbox.head() == old
    assert sandbox.porcelain() == ""
    assert "job" in notify.body


# ---------------------------------------------------------------------------
# 9. the unit pair and the docs ship with the feature
# ---------------------------------------------------------------------------


def test_self_update_units_and_docs_are_shipped() -> None:
    """Criterion 9. The timer pair is modelled on the digest unit, with a *finite*
    TimeoutStartSec large enough that systemd cannot land a SIGTERM inside the
    `git merge` window (spec finding F8), no `[Install]` on the service, and both the
    deploy README and the manual runbook pointing at the timer."""
    units = REPO_ROOT / "deploy" / "systemd"
    service_text = (units / "herdr-routines-update.service").read_text()
    timer_text = (units / "herdr-routines-update.timer").read_text()
    digest_text = (units / "herdr-routines-digest.service").read_text()

    assert "Type=oneshot" in service_text
    assert "WorkingDirectory=%h/projects/herdr-routines" in service_text
    assert "After=herdr-server.service" in service_text
    assert "Wants=herdr-server.service" in service_text
    assert "Environment=PATH=%h/.local/bin:" in service_text
    assert "ExecStart=%h/.local/bin/uv run herdr-routines self-update" in service_text
    assert "infinity" not in service_text
    timeouts = [
        line
        for line in service_text.splitlines()
        if line.startswith("TimeoutStartSec=")
    ]
    assert len(timeouts) == 1
    assert int(timeouts[0].split("=", 1)[1]) >= 1800
    # Modelled on the digest unit: same directives, same PATH — and, like it, no
    # [Install] section (only the timer is enable-able).
    for directive in ("Type=", "WorkingDirectory=", "Environment=PATH=", "After="):
        assert directive in service_text
        assert directive in digest_text
    assert "[Install]" not in service_text
    assert "[Install]" not in digest_text

    # `UTC` is pinned explicitly: an OnCalendar with no suffix is read in the
    # *system* zone, which is America/Montevideo on the Pi, so the docs' "21:30 UTC"
    # would otherwise be 21:30 local.
    assert "OnCalendar=*-*-* 21:30:00 UTC" in timer_text
    assert "Persistent=true" in timer_text
    assert "AccuracySec=5min" in timer_text
    assert "WantedBy=timers.target" in timer_text

    readme = (REPO_ROOT / "deploy" / "README.md").read_text()
    assert "herdr-routines-update.service" in readme
    assert "herdr-routines-update.timer" in readme
    assert "systemctl --user enable --now herdr-routines-update.timer" in readme

    runbook = (REPO_ROOT / "docs" / "process" / "pi-update-runbook.md").read_text()
    when_to_run = runbook.split("## When to run", 1)[1].split("\n## ", 1)[0]
    assert "herdr-routines-update.timer" in when_to_run


# ---------------------------------------------------------------------------
# 10. the new code is exercised out of process, and cannot rewrite uv.lock
# ---------------------------------------------------------------------------


def test_self_update_validates_out_of_process_with_frozen_uv(
    sandbox: Sandbox,
) -> None:
    """Criterion 10. The parent already has the old code imported, so the new import
    graph, config schema and `uv.lock` are only real if a child exercises them.
    `--frozen` is what makes that safe: it still syncs `.venv` from the committed
    lock, but `uv` can no longer rewrite a *tracked* `uv.lock` between the
    fast-forward and a rollback (spec finding F6)."""
    sandbox.push("NEW.md", "new\n", "add NEW.md")

    validate = FakeValidate()
    sandbox.run(runner=validate)

    assert len(validate.calls) == 2
    for call in validate.calls:
        assert call["argv"] == [
            "/usr/bin/uv",
            "run",
            "--frozen",
            "herdr-routines",
            "validate",
        ]
        assert call["cwd"] == sandbox.checkout
        assert call["timeout"] is not None


# ---------------------------------------------------------------------------
# 14. a fast-forward that moves nothing is "up to date", never "updated"
# ---------------------------------------------------------------------------


def test_self_update_reports_up_to_date_when_ff_is_a_noop(
    sandbox: Sandbox, caplog: pytest.LogCaptureFixture
) -> None:
    """Criterion 14 (spec finding F3). `git merge --ff-only` is a *no-op* whenever
    HEAD already contains origin/<base> — including when HEAD is strictly ahead of
    it. A host that was hot-fixed by hand *and committed* leaves exactly that state,
    so reporting `old..new (0 commits)` plus a success notification would claim an
    update that never happened. The post-ff HEAD re-read is authoritative; the
    unpushed-commit warning is logged, not notified (it would be a nightly nag)."""
    # Control: a clean checkout that is simply current.
    notify = Recorder()
    with caplog.at_level("WARNING", logger="herdr_routines.self_update"):
        result = sandbox.run(notify=notify)
    assert result.status == "up_to_date"
    assert notify.calls == []
    assert "unpushed" not in caplog.text

    # The real case: two unpushed local commits, upstream unmoved.
    for i in range(2):
        _commit(sandbox.checkout, f"HAND{i}.md", f"hand {i}\n", f"hand fix {i}")
    old = sandbox.head()
    caplog.clear()
    notify = Recorder()
    with caplog.at_level("WARNING", logger="herdr_routines.self_update"):
        result = sandbox.run(notify=notify)

    assert result.status == "up_to_date"
    assert sandbox.head() == old
    assert notify.calls == []
    assert "unpushed" in caplog.text
    assert "2 unpushed" in caplog.text
    assert str(sandbox.checkout) in caplog.text


# ---------------------------------------------------------------------------
# 15. a rejected SHA is remembered, not re-tested every night
# ---------------------------------------------------------------------------


def test_self_update_does_not_retry_a_rejected_sha(sandbox: Sandbox) -> None:
    """Criterion 15 (spec finding F2). Without this, a genuinely bad commit is
    re-fetched, re-validated, re-rolled-back and re-notified every night forever — so
    the operator cannot tell "the timer is broken" from "this commit is bad", and
    mutes it. Keyed on the SHA, so a later move of main clears it for free."""
    old = sandbox.head()
    new = sandbox.push("NEW.md", "new\n", "add NEW.md")

    first_notify = Recorder()
    first = sandbox.run(
        notify=first_notify,
        runner=FakeValidate(results=[(0, PASSING_VALIDATE), (1, "validate exited 1")]),
    )
    assert first.status == "rolled_back"
    assert sandbox.head() == old

    state = json.loads(sandbox.state_path.read_text())
    assert state["last_rejected_sha"] == new
    assert "validate" in state["reason"]

    second_notify = Recorder()
    second_validate = FakeValidate()
    second_gh = FakeGh()
    second = sandbox.run(notify=second_notify, runner=second_validate, gh=second_gh)

    assert second.status == "deferred"
    assert "already rolled back" in (second.reason or "")
    assert new[:7] in (second.reason or "")
    assert second_validate.calls == []
    assert second_gh.calls == []
    assert second_notify.calls == []
    assert sandbox.head() == old


# ---------------------------------------------------------------------------
# 16. a missing uv is refused before any git operation
# ---------------------------------------------------------------------------


def test_self_update_refuses_when_uv_is_missing(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 16 (spec finding F10). `uv` is not on PATH in non-interactive ssh —
    the runbook says so explicitly. Discovering that *after* the fast-forward would
    mean an ff-then-useless-rollback, so resolution is step 4, before the fetch."""
    old = sandbox.head()
    sandbox.push("NEW.md", "new\n", "add NEW.md")

    git_calls = _record_git_calls(monkeypatch)

    notify = Recorder()
    result = sandbox.run(notify=notify, uv_resolver=lambda: None)

    assert result.status == "refused"
    assert "uv" in (result.reason or "")
    assert sandbox.head() == old
    # A refusal is loud (R3), unlike a deferral: a host missing `uv` is a host that
    # silently stops updating, which is the failure this command exists to prevent.
    assert len(notify.calls) == 1
    assert "uv" in notify.body
    mutating = [
        args for args in git_calls.calls if args and args[0] in {"fetch", "merge"}
    ]
    assert mutating == [], f"expected no mutating git call, got {mutating}"

    # And the real resolver agrees, with no uv on PATH and none in the home dir.
    monkeypatch.setenv("PATH", str(sandbox.root / "empty-bin"))
    monkeypatch.setenv("HOME", str(sandbox.root / "empty-home"))
    assert self_update.resolve_uv() is None


# ---------------------------------------------------------------------------
# 17. a validate that hangs is a failure, not a pass
# ---------------------------------------------------------------------------


def test_self_update_treats_validate_timeout_as_failure(sandbox: Sandbox) -> None:
    """Criterion 17 (spec finding F8). `validate_subprocess` passes an explicit
    timeout, and `TimeoutExpired` is a failure — an unbounded child is exactly the
    wedge `TimeoutStartSec` exists to prevent, so it must never read as green."""
    old = sandbox.head()
    sandbox.push("NEW.md", "new\n", "add NEW.md")

    notify = Recorder()
    result = sandbox.run(notify=notify, runner=FakeValidate(timeout_on=1))

    assert result.status == "rolled_back"
    assert sandbox.head() == old
    assert sandbox.porcelain() == ""
    assert "timed out" in notify.body.lower()


# ---------------------------------------------------------------------------
# 18. a rollback that cannot land must never be silent
# ---------------------------------------------------------------------------


def test_self_update_reports_head_sha_when_rollback_fails(sandbox: Sandbox) -> None:
    """Criterion 18. `git reset --keep` *refuses* rather than discarding when a local
    edit would be overwritten (verified: rc 128, `fatal: Could not reset index
    file`). This is the one reachable path where the checkout is not back at `old`, so
    the notification has to name the SHA it is actually stuck on, read back from
    `git rev-parse HEAD` after the failed reset."""
    sandbox.push("changed.txt", "one\n", "add changed.txt")
    _git(sandbox.checkout, "fetch", "origin")
    _git(sandbox.checkout, "merge", "--ff-only", f"origin/{sandbox.base}")
    new = sandbox.push("changed.txt", "two\n", "rewrite changed.txt")

    def edit_after_ff(index: int) -> None:
        """The post-update validate child leaves a local edit to a file the fast
        forward changed — precisely what makes `--keep` refuse."""
        if index == 1:
            (sandbox.checkout / "changed.txt").write_text("hand edit\n")

    notify = Recorder()
    result = sandbox.run(
        notify=notify,
        runner=FakeValidate(
            results=[(0, PASSING_VALIDATE), (1, "error: broken new code")],
            before_return=edit_after_ff,
        ),
    )

    assert result.status == "rolled_back"
    # The checkout did NOT go back to old — and the notification says so.
    assert sandbox.head() == new
    assert new in notify.body
    assert "reset" in notify.body.lower()
    assert "changed.txt" in sandbox.porcelain()


# ---------------------------------------------------------------------------
# 19. --dry-run reports without touching the checkout
# ---------------------------------------------------------------------------


def test_self_update_dry_run_makes_no_git_change(sandbox: Sandbox) -> None:
    """Criterion 19 (spec finding F9). Steps 1-5 including the fetch and the CI query,
    because both are needed for a truthful `old..new` and deploy diff — then it stops
    before the fast-forward. Under a held tick lock a `--dry-run` reports `deferred`,
    which is correct and is said in `--help` so the manual runbook path is not
    confusing."""
    old, porcelain = sandbox.head(), sandbox.porcelain()
    new = sandbox.push("NEW.md", "new\n", "add NEW.md")

    notify = Recorder()
    validate = FakeValidate()
    gh = FakeGh()
    result = sandbox.run(dry_run=True, notify=notify, runner=validate, gh=gh)

    assert sandbox.head() == old
    assert sandbox.porcelain() == porcelain
    assert validate.calls == []
    assert notify.calls == []
    assert gh.calls == [new]
    assert "dry" in (result.reason or "").lower()
    assert result.old == old
    assert result.new == new

    # The lock applies to a dry run too.
    with _held_flock(sandbox.lock_path):
        result = sandbox.run(dry_run=True, notify=Recorder())
    assert result.status == "deferred"


# ---------------------------------------------------------------------------
# 20. --config is honoured, and a broken config is refused loudly
# ---------------------------------------------------------------------------


def test_self_update_honors_config_override_and_reports_config_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 20 (spec finding F7). `--config` is a *top-level* flag, so every
    subcommand receives `args.config`; this one resolves it like the others. It does
    not go through `_load_config_or_exit`, which turns a ConfigError into a bare
    SystemExit(1) with no notification — and a host whose config no longer loads is
    exactly what this command must report loudly. `config.errors` (the jobs.d/
    per-file soft errors) counts too: running happily on a half-broken config is the
    failure the runbook's "Config migrations" section warns about."""
    sb = Sandbox(tmp_path / "repo")
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    jobs_d = tmp_path / "jobs.d"
    jobs_d.mkdir()
    (jobs_d / "alpha.yaml").write_text(
        "cron: '0 3 * * *'\nrepo: /tmp\nagent_kind: claude\n"
        "prompt: 'write $ROUTINE_REPORT'\n"
    )
    (jobs_d / "beta.yaml").write_text(
        "cron: '0 4 * * *'\nrepo: /tmp\nagent_kind: claude\n"
        "prompt: 'write $ROUTINE_REPORT'\n"
    )

    seen: list[dict[str, Any]] = []

    def fake_run_self_update(**kwargs: Any) -> SelfUpdateResult:
        seen.append(kwargs)
        return SelfUpdateResult(status="up_to_date")

    notifications: list[tuple[str, str | None]] = []
    monkeypatch.setattr(cli, "run_self_update", fake_run_self_update)
    monkeypatch.setattr(
        cli,
        "_self_update_notify",
        lambda title, body=None: notifications.append((title, body)),
    )

    # (a) the override is honoured — the jobs come from the given jobs.d/.
    code = cli.main(
        [
            "--config",
            str(jobs_d),
            "self-update",
            "--path",
            str(sb.checkout),
            "--base",
            sb.base,
        ]
    )
    assert code == 0
    assert [job.name for job in seen[0]["config"].jobs] == ["alpha", "beta"]
    assert seen[0]["checkout"] == sb.checkout
    assert seen[0]["base"] == sb.base
    assert notifications == []

    # (b) a config that will not load at all -> refused, notified, exit 1.
    code = cli.main(
        [
            "--config",
            str(tmp_path / "missing.yaml"),
            "self-update",
            "--path",
            str(sb.checkout),
        ]
    )
    assert code == 1
    assert len(notifications) == 1
    assert "missing.yaml" in (notifications[0][1] or "")

    # (c) a jobs.d/ whose per-file errors are non-empty -> refused, notified, exit 1.
    (jobs_d / "gamma.yaml").write_text("cron: '0 5 * * *'\nnope: true\n")
    code = cli.main(
        [
            "--config",
            str(jobs_d),
            "self-update",
            "--path",
            str(sb.checkout),
            "--base",
            sb.base,
        ]
    )
    assert code == 1
    assert len(notifications) == 2
    assert "gamma.yaml" in (notifications[1][1] or "")
    assert len(seen) == 1  # never reached run_self_update


# ---------------------------------------------------------------------------
# 21. git is exercised for real; only validate / CI / notification are faked
# ---------------------------------------------------------------------------


def test_self_update_git_operations_are_real_not_mocked(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 21. Every git operation in the flow is a real subprocess against a
    real bare repo: the fetch, the fast-forward and the diff. Only the three injected
    seams are fake — and the `uv run --frozen` argv is asserted *absent* from the real
    process table, because `validate` goes through the injected runner."""
    real_run = subprocess.run
    recorded: list[list[str]] = []

    def recording_run(argv: Any, **kwargs: Any) -> Any:
        recorded.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(self_update.subprocess, "run", recording_run)

    sandbox.push("ONE.md", "one\n", "add ONE.md")
    new = sandbox.push("TWO.md", "two\n", "add TWO.md")

    notify = Recorder()
    validate = FakeValidate()
    gh = FakeGh()
    result = sandbox.run(notify=notify, runner=validate, gh=gh)

    checkout = str(sandbox.checkout)
    assert result.status == "updated"
    assert sandbox.head() == new
    assert sandbox.porcelain() == ""
    assert ["git", "-C", checkout, "fetch", "--prune", "origin"] in recorded
    # The merge targets the CI-gated SHA, not a re-resolved origin/<base>: the gate
    # is on one commit, so that is the only commit this run may deploy.
    assert ["git", "-C", checkout, "merge", "--ff-only", new] in recorded
    assert not any(
        argv[-2:] == ["--ff-only", f"origin/{sandbox.base}"] for argv in recorded
    )
    assert not any("--hard" in argv for argv in recorded)
    # The three fakes, and only those: no `uv` process was ever spawned.
    assert len(validate.calls) == 2
    assert gh.calls == [new]
    assert len(notify.calls) == 1
    assert not any(argv and argv[0] == "/usr/bin/uv" for argv in recorded)


# ---------------------------------------------------------------------------
# 22. the PR #130 review fixes: the gate, the timeout, and the rejected HEAD
# ---------------------------------------------------------------------------


def test_self_update_deploys_only_the_ci_gated_sha(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix 1 — the gate must cover the commit that actually moves HEAD. The
    fast-forward used to be `repos._fetch_and_fast_forward`, which runs a *second*
    fetch and merges whatever `origin/<base>` is at that moment, so a commit landing
    on main during the baseline validate (a full `uv run`, up to
    `VALIDATE_TIMEOUT_S`) was deployed and reported as `updated` with no `gh` call
    about it. Here main moves mid-run: the gated SHA is deployed, and the newcomer
    waits for its own night's gate."""
    gated = sandbox.push("ONE.md", "one\n", "add ONE.md")

    def land_a_commit_mid_validate(index: int) -> None:
        if index == 0:
            sandbox.push("TWO.md", "two\n", "a commit that lands after the gate")

    git_calls = _record_git_calls(monkeypatch)
    gh = FakeGh()
    notify = Recorder()
    result = sandbox.run(
        notify=notify,
        runner=FakeValidate(before_return=land_a_commit_mid_validate),
        gh=gh,
    )

    assert result.status == "updated"
    assert result.new == gated
    assert sandbox.head() == gated
    assert sandbox.porcelain() == ""
    assert gh.calls == [gated]
    assert ("merge", "--ff-only", gated) in git_calls.calls
    # One fetch, not two: the newcomer is not even in this checkout's object store.
    assert [c for c in git_calls.calls if c and c[0] == "fetch"] == [
        ("fetch", "--prune", "origin")
    ]


def test_a_git_timeout_is_a_result_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix 2 — a bounded git call that hits its bound is a *result*. Every git
    call here is bounded by `GIT_TIMEOUT_S` precisely because a degraded link is what
    the 2026-09-27 incident ran into, but `subprocess.TimeoutExpired` is not a
    `RuntimeError`, so it used to raise straight out of `run_self_update`: no
    `SelfUpdateResult`, no notification, no rollback, and `_cmd_self_update` has no
    catch-all to turn it into one. `_git` reports the bound as a failing git instead,
    so both the fetch and the merge land in the refuse path with HEAD untouched."""
    real_run = subprocess.run

    for label, slow_arg in (("fetch", "fetch"), ("merge", "merge")):
        sb = _sandbox(tmp_path, label)
        old = sb.head()
        sb.push("NEW.md", "new\n", "new commit")

        def flaky_run(argv: Any, arg: str = slow_arg, **kwargs: Any) -> Any:
            if isinstance(argv, list) and arg in argv:
                raise subprocess.TimeoutExpired(
                    cmd=argv, timeout=self_update.GIT_TIMEOUT_S
                )
            return real_run(argv, **kwargs)

        monkeypatch.setattr(self_update.subprocess, "run", flaky_run)

        notify = Recorder()
        validate = FakeValidate()
        result = sb.run(notify=notify, runner=validate)

        assert result.status == "refused", label
        assert result.exit_code == 1, label
        assert "timed out" in (result.reason or ""), label
        assert sb.head() == old, label
        assert sb.porcelain() == "", label
        assert len(notify.calls) == 1, label
        assert "timed out" in notify.body, label
        # The baseline validate runs before the merge and not at all before the fetch.
        assert len(validate.calls) == (0 if slow_arg == "fetch" else 1), label


def test_a_host_left_on_a_rejected_sha_is_refused_not_up_to_date(
    sandbox: Sandbox,
) -> None:
    """Review fix 3 — a rollback that could not be applied (`reset --keep` refused
    because of a local edit) leaves HEAD on the commit that failed validation. Once
    the operator discards that edit without moving HEAD, the `new == old`
    short-circuit used to answer `up_to_date` *before* it ever read the
    rejected-SHA file: the host ran unvalidated code and said nothing, every night,
    which is exactly what the state file exists to prevent. It refuses instead —
    loudly — and clears itself when main moves."""
    sandbox.push("changed.txt", "one\n", "add changed.txt")
    _git(sandbox.checkout, "fetch", "origin")
    _git(sandbox.checkout, "merge", "--ff-only", f"origin/{sandbox.base}")
    rejected = sandbox.push("changed.txt", "two\n", "rewrite changed.txt")

    def edit_after_ff(index: int) -> None:
        """The post-update validate child leaves a local edit to a file the fast
        forward changed — precisely what makes `reset --keep` refuse."""
        if index == 1:
            (sandbox.checkout / "changed.txt").write_text("hand edit\n")

    first_notify = Recorder()
    first = sandbox.run(
        notify=first_notify,
        runner=FakeValidate(
            results=[(0, PASSING_VALIDATE), (1, "error: broken new code")],
            before_return=edit_after_ff,
        ),
    )
    assert first.status == "rolled_back"
    assert sandbox.head() == rejected
    assert json.loads(sandbox.state_path.read_text())["last_rejected_sha"] == rejected

    # The operator throws their edit away, and HEAD stays where it is.
    _git(sandbox.checkout, "checkout", "--", "changed.txt")

    second_notify = Recorder()
    second_validate = FakeValidate()
    second_gh = FakeGh()
    second = sandbox.run(notify=second_notify, runner=second_validate, gh=second_gh)

    assert second.status == "refused"
    assert second.exit_code == 1
    assert rejected in (second.reason or "")
    assert sandbox.head() == rejected
    assert len(second_notify.calls) == 1
    assert rejected in second_notify.body
    assert second_validate.calls == []
    assert second_gh.calls == []

    # And it clears itself for free once main moves, like every other rejection.
    moved = sandbox.push("THREE.md", "three\n", "a later commit")
    third = sandbox.run(notify=Recorder(), runner=FakeValidate(), gh=FakeGh())

    assert third.status == "updated"
    assert sandbox.head() == moved


def test_the_module_docstring_lists_self_update() -> None:
    """Review fix 4 (unanchored) — `cli.py`'s module docstring carries a partial
    subcommand list, and this PR added a subcommand to the CLI without adding it
    there. Spec finding F14 is explicit that only `self-update` is in scope: the other
    seven missing subparsers are unrelated churn, so the fix is one word and a note
    that the list is partial on purpose — otherwise the next reader "completes" it."""
    docstring = (REPO_ROOT / "src" / "herdr_routines" / "cli.py").read_text()
    summary = docstring.split('"""', 2)[1]

    assert "self-update" in summary
    # Not swept: the seven subparsers F14 left out stay out.
    assert "pick-feature" not in summary
    assert "pipeline-watchdog" not in summary
    assert "deliberately partial" in summary


def test_path_default_is_the_runner_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix 5 (unanchored) — the spec's `--path` default was `Path.cwd()`; the
    shipped default is `~/projects/herdr-routines`. The deviation is the right call (a
    cwd default would update whatever directory the timer happened to start in,
    including the `repos/` clone, which is a separate lifecycle), but it was
    undocumented, which made `deploy/README.md`'s "the unit only ever updates the
    checkout it runs *in* (`WorkingDirectory`)" false as a general statement: the unit
    passes no `--path`, so the flag's default is what decides, and it merely *happens*
    to equal the unit's `WorkingDirectory`.

    This pins the behaviour, and that it survives a different cwd — which is the whole
    reason the default is not `Path.cwd()`."""
    default = Path.home() / "projects" / "herdr-routines"
    # Run from somewhere else: on the runner host (and a dev checkout at the same path)
    # the real cwd *is* the default, which would make the check below vacuous-or-false.
    monkeypatch.chdir(tmp_path)
    assert default != Path.cwd()
    parser = cli._build_parser()  # type: ignore[attr-defined]
    resolved = parser.parse_args(["self-update"])
    assert resolved.handler is cli._cmd_self_update  # type: ignore[attr-defined]
    assert resolved.path == default

    # And the deploy README says so, rather than crediting `WorkingDirectory`.
    readme = (REPO_ROOT / "deploy" / "README.md").read_text()
    assert "WorkingDirectory" in readme  # still mentioned...
    assert (
        "only ever updates the checkout it runs *in*" not in readme
    )  # ...but not as the reason
    assert "--path" in readme


# ---------------------------------------------------------------------------
# the module-level seams the tests above pin
# ---------------------------------------------------------------------------


def test_default_state_path_follows_the_plugin_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejected-SHA state file rides the same `$HERDR_PLUGIN_STATE_DIR`
    convention as `tick.default_lock_path` and `history.default_history_path`."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    assert self_update.default_state_path() == tmp_path / "state" / "self-update.json"

    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert self_update.default_state_path() == (
        tmp_path / "home" / ".local" / "state" / "herdr-routines" / "self-update.json"
    )


def test_deploy_changes_is_empty_when_nothing_under_deploy_moved(
    sandbox: Sandbox,
) -> None:
    old = sandbox.head()
    sandbox.push("NEW.md", "new\n", "add NEW.md")
    _git(sandbox.checkout, "fetch", "origin")
    _git(sandbox.checkout, "merge", "--ff-only", f"origin/{sandbox.base}")
    assert self_update.deploy_changes(sandbox.checkout, old, sandbox.head()) == []


def test_pipeline_run_open_ignores_a_settled_run(sandbox: Sandbox) -> None:
    """`_open_pipeline_run` asks "is there a run in flight" without folding in a
    staleness clock; a run whose terminal record has landed is closed."""
    config = RoutinesConfig(jobs=(_pipeline_job(sandbox.checkout),))
    append(
        sandbox.history_path,
        HistoryRecord(ts=NOW, job="feature-pipeline", state="running", run_id="r1"),
    )
    assert (
        self_update.pipeline_run_open(config, sandbox.history_path)
        == "feature-pipeline"
    )
    append(
        sandbox.history_path,
        HistoryRecord(ts=NOW, job="feature-pipeline", state="done", run_id="r1"),
    )
    assert self_update.pipeline_run_open(config, sandbox.history_path) is None
    # A config with no kind: pipeline job is never "open".
    assert (
        self_update.pipeline_run_open(RoutinesConfig(jobs=()), sandbox.history_path)
        is None
    )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


class _GitRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []


def _record_git_calls(monkeypatch: pytest.MonkeyPatch) -> _GitRecorder:
    """Record every argv `self_update._git` builds, without changing what it does."""
    recorder = _GitRecorder()
    real_git = self_update._git

    def recording_git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
        recorder.calls.append(args)
        return real_git(checkout, *args)

    monkeypatch.setattr(self_update, "_git", recording_git)
    return recorder


@contextmanager
def _held_flock(path: Path) -> Iterator[None]:
    """Take a real exclusive flock on *path* for the duration of the block, exactly
    as a concurrent `tick` would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
