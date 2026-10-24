"""Issue 070: a fix worker's `done` is its word, so the PR-target tick checks it.

Every test here drives `run_tick` against a real git repo whose PR branch is already
checked out in a retained worktree (what the pipeline orchestrator leaves behind), with a
fake gh whose PR head and review threads the fake worker can change while it "runs"."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines.auto_fix import attempt_count_for_pr
from herdr_routines.config import GateCheck, Job, RoutinesConfig
from herdr_routines.history import HistoryRecord, read_job
from herdr_routines.tick import run_tick

BRANCH = "auto/pipeline-20261021T050000Z"
SHA_BEFORE = "sha-before"
SHA_AFTER = "sha-after"
PR_NUMBER = 211


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with `main` and the PR branch, an `origin` remote naming acme/widgets, and
    the branch held by a retained worktree. Returns (repo, retained worktree)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _git("remote", "add", "origin", "git@github.com:acme/widgets.git", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    _git("branch", BRANCH, cwd=repo)
    retained = tmp_path / "orchestrator-worktree"
    _git("worktree", "add", str(retained), BRANCH, cwd=repo)
    return repo, retained


class FakeGh:
    """gh for one PR: failing CI (so it is eligible), a movable head, and review threads
    kept as plain dicts so a worker can reply to them mid-run."""

    def __init__(self, *, thread_ids: tuple[str, ...] = ()) -> None:
        self.head = SHA_BEFORE
        self.threads: dict[str, list[dict[str, Any]]] = {
            tid: [_comment("testuser", "please fix", "2026-10-21T04:00:00Z")]
            for tid in thread_ids
        }

    def reply(self, thread_id: str, *, at: datetime, author: str = "testuser") -> None:
        self.threads[thread_id].append(
            _comment(author, "done: won't fix, because ...", at.isoformat())
        )

    def api_user(self) -> str:
        return "testuser"

    def pr_list(self, *, owner, repo, state, limit):
        return [
            {
                "number": PR_NUMBER,
                "headRefName": BRANCH,
                "headRefOid": self.head,
                "author": {"login": "testuser", "is_bot": False},
                "isCrossRepository": False,
                "url": f"https://github.com/acme/widgets/pull/{PR_NUMBER}",
            }
        ]

    def pr_view(self, *, owner, repo, number):
        return {
            "statusCheckRollup": [{"name": "ci", "state": "FAILURE"}],
            "headRefOid": self.head,
        }

    def graphql(self, query, **variables):
        nodes = [
            {
                "id": tid,
                # Replying resolves the thread (the fix prompt says to), so the gate must
                # find it by id rather than among the unresolved ones.
                "isResolved": len(comments) > 1,
                "comments": {"totalCount": len(comments), "nodes": comments},
            }
            for tid, comments in self.threads.items()
        ]
        return {
            "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}
        }


def _comment(login: str, body: str, created_at: str) -> dict[str, Any]:
    return {"author": {"login": login}, "body": body, "createdAt": created_at}


class FakeWorkerClient:
    """Enough of HerdrClient to settle a fix worker "idle". `on_prompt(cwd)` is what the
    worker does to the world while it runs."""

    def __init__(self, on_prompt: Callable[[str], None] | None = None) -> None:
        self.on_prompt = on_prompt
        self.cwd: str | None = None
        self.prompts = 0
        self.notifications: list[tuple[str, str | None, str]] = []
        self._registered: dict[str, str] = {}

    def tab_create(self, *, cwd, label=None):
        self.cwd = cwd
        return "w1:p1"

    def agent_start(
        self, *, name, kind, pane_id, start_timeout_ms, model=None, session_id=None
    ):
        self._registered[name] = "working"

    def agent_interactive_ready(self, target):
        return True

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        self.prompts += 1
        if self.on_prompt is not None:
            assert self.cwd is not None
            self.on_prompt(self.cwd)
        self._registered[target] = "idle"
        return "idle"

    def agent_read_visible(self, target, *, lines=200):
        return ""

    def pane_close(self, pane_id):
        pass

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._registered)

    def notification_show(self, title, *, body=None, sound="none"):
        self.notifications.append((title, body, sound))

    def blocked_notifications(self) -> list[tuple[str, str | None, str]]:
        return [n for n in self.notifications if "blocked" in n[0]]


class World:
    """One job, one repo, one fake gh, ticked a minute at a time."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        gh: FakeGh,
        client: FakeWorkerClient,
    ) -> None:
        self.repo, self.retained = _init_repo(tmp_path)
        self.gh = gh
        self.client = client
        monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
        self.history_path = tmp_path / "state" / "history.jsonl"
        monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
        monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
        self.job = _job(self.repo)
        self.config = RoutinesConfig(jobs=(self.job,))
        self.t0 = datetime.now(UTC).replace(microsecond=0)
        run_tick(self.config, self.history_path, client=client, now=self.t0)  # type: ignore[arg-type] # registers
        self.ticks = 0

    def tick(self) -> datetime:
        self.ticks += 1
        now = self.t0 + timedelta(minutes=self.ticks)
        run_tick(self.config, self.history_path, client=self.client, now=now)  # type: ignore[arg-type]
        return now

    def pr_records(self) -> list[HistoryRecord]:
        return [
            r
            for r in read_job(self.history_path, self.job.name)
            if r.extra and r.extra.get("pr_number") == PR_NUMBER
        ]

    def last_pr_record(self) -> HistoryRecord:
        return self.pr_records()[-1]

    def tick_report(self, record: HistoryRecord) -> str:
        return (tmp_reports(self.history_path) / f"{record.run_id}.md").read_text()


def tmp_reports(history_path: Path) -> Path:
    return history_path.parent / "reports"


def _job(repo: Path) -> Job:
    return Job(
        name="babysit-prs",
        enabled=True,
        cron="* * * * *",
        repo=repo,
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="",
        timeout_ms=5_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
        checks=(GateCheck(kind="pr_health"),),
        target="pr",
        max_workers_per_tick=3,
        max_attempts_per_target=3,
        kind="gated",
    )


def _dirty(cwd: str) -> None:
    (Path(cwd) / "leftover.txt").write_text("edited, never committed\n")


def _verify_by_gate(record: HistoryRecord) -> dict[str, tuple[bool, str | None]]:
    assert record.extra is not None
    results: Any = record.extra["verify"]
    return {v["gate"]: (v["passed"], v["reason"]) for v in results}


def test_fix_worker_done_with_dirty_worktree_recorded_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 (issue 070): the #211 incident. The worker edits files in the retained worktree,
    commits nothing and pushes nothing, yet its agent settles `idle`. That is `failed`
    with the dirty tree as the reason (it outranks "pushed nothing" because it is what
    wedges the branch), and both gates are on the record."""
    world = World(
        tmp_path, monkeypatch, gh=FakeGh(), client=FakeWorkerClient(on_prompt=_dirty)
    )

    world.tick()

    record = world.last_pr_record()
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["final_agent_status"] == "idle"
    assert record.extra["reason"] == "worker_left_uncommitted_changes"
    assert _verify_by_gate(record) == {
        "worktree_clean": (False, "worker_left_uncommitted_changes"),
        "pushed_or_replied": (False, "worker_pushed_nothing"),
    }
    assert record.extra["worktree_path"] == str(world.retained)


def test_fix_worker_done_pushing_nothing_recorded_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: a clean tree is not proof of work. The PR head did not move and no thread was
    answered, so a CI-only fix that pushed nothing is `failed`."""
    world = World(tmp_path, monkeypatch, gh=FakeGh(), client=FakeWorkerClient())

    world.tick()

    record = world.last_pr_record()
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["reason"] == "worker_pushed_nothing"
    assert _verify_by_gate(record)["worktree_clean"] == (True, None)


def test_fix_worker_done_with_new_head_recorded_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = FakeGh()

    def push(cwd: str) -> None:
        gh.head = SHA_AFTER

    world = World(tmp_path, monkeypatch, gh=gh, client=FakeWorkerClient(on_prompt=push))

    world.tick()

    record = world.last_pr_record()
    assert record.state == "done"
    assert record.extra is not None
    assert "reason" not in record.extra
    assert _verify_by_gate(record) == {
        "worktree_clean": (True, None),
        "pushed_or_replied": (True, None),
    }


def test_fix_worker_reply_only_on_every_thread_recorded_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering every thread it was given ("won't fix, because ...") is a legitimate
    outcome without a push: the head is unchanged and the run is still `done`."""
    gh = FakeGh(thread_ids=("t1", "t2"))
    now = datetime.now(UTC)

    def reply_to_all(cwd: str) -> None:
        for tid in ("t1", "t2"):
            gh.reply(tid, at=now + timedelta(minutes=2))

    world = World(
        tmp_path, monkeypatch, gh=gh, client=FakeWorkerClient(on_prompt=reply_to_all)
    )

    world.tick()

    record = world.last_pr_record()
    assert gh.head == SHA_BEFORE
    assert record.state == "done"
    assert _verify_by_gate(record)["pushed_or_replied"] == (True, None)


def test_fix_worker_replied_to_some_threads_recorded_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One answered thread out of two is not "a reply on each thread it was given"."""
    gh = FakeGh(thread_ids=("t1", "t2"))
    now = datetime.now(UTC)

    def reply_to_one(cwd: str) -> None:
        gh.reply("t1", at=now + timedelta(minutes=2))

    world = World(
        tmp_path, monkeypatch, gh=gh, client=FakeWorkerClient(on_prompt=reply_to_one)
    )

    world.tick()

    record = world.last_pr_record()
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["reason"] == "worker_pushed_nothing"


def test_fix_worker_stale_reply_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply that predates the dispatch cannot satisfy a worker that wrote none."""
    gh = FakeGh(thread_ids=("t1",))
    gh.reply("t1", at=datetime.now(UTC) - timedelta(hours=1))
    # The thread is unresolved at dispatch (the fake resolves on a reply, so reopen it).
    world = World(tmp_path, monkeypatch, gh=gh, client=FakeWorkerClient())
    gh.graphql = lambda query, **variables: {  # type: ignore[method-assign]
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "t1",
                                "isResolved": False,
                                "comments": {
                                    "totalCount": 2,
                                    "nodes": gh.threads["t1"],
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    world.tick()

    record = world.last_pr_record()
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["reason"] == "worker_pushed_nothing"


def test_worktree_reuse_not_clean_does_not_consume_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a refusal that never launched an agent spends no attempt. Four ticks against a
    dirty retained worktree (budget is 3) all refuse, none reaches max_attempts_exceeded,
    no agent ever starts, and once the tree is clean the next tick dispatches at attempt 0.
    """
    client = FakeWorkerClient()
    world = World(tmp_path, monkeypatch, gh=FakeGh(), client=client)
    _dirty(str(world.retained))

    for _ in range(4):
        world.tick()

    records = world.pr_records()
    assert [(r.state, r.extra and r.extra["reason"]) for r in records] == [
        ("skipped", "worktree_reuse_not_clean")
    ] * 4
    assert client.prompts == 0
    assert attempt_count_for_pr(world.history_path, world.job.name, PR_NUMBER) == 0

    (world.retained / "leftover.txt").unlink()
    world.tick()

    last = world.last_pr_record()
    assert last.extra is not None
    assert last.extra["attempt"] == 0
    assert client.prompts == 1


def test_dirty_worktree_refusal_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: a wedged worktree always needs a human, so the first refusal notifies at the
    failure tier (the default `terminal` policy delivers it) and names the path; the next
    refusals for the same path stay silent."""
    client = FakeWorkerClient()
    world = World(tmp_path, monkeypatch, gh=FakeGh(), client=client)
    _dirty(str(world.retained))

    world.tick()
    assert len(client.blocked_notifications()) == 1
    _title, body, sound = client.blocked_notifications()[0]
    assert str(world.retained) in (body or "")
    assert sound == "request"

    world.tick()
    world.tick()
    assert len(client.blocked_notifications()) == 1


def test_dirty_worktree_after_settle_already_notified_stays_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle that left the tree dirty already notified (its body names the path), so
    the refusal on the next tick is not a second alert for the same worktree."""
    client = FakeWorkerClient(on_prompt=_dirty)
    world = World(tmp_path, monkeypatch, gh=FakeGh(), client=client)

    world.tick()
    failed = [n for n in client.notifications if n[0].endswith("failed")]
    assert len(failed) == 1
    assert str(world.retained) in (failed[0][1] or "")

    world.tick()
    assert world.last_pr_record().state == "skipped"
    assert client.blocked_notifications() == []


def test_dirty_worktree_surfaced_in_babysit_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: the tick report names the dirty worktree on the tick that found the worker's
    leftovers and on every later tick that refuses it, and nothing is discarded."""
    client = FakeWorkerClient(on_prompt=_dirty)
    world = World(tmp_path, monkeypatch, gh=FakeGh(), client=client)
    leftover = world.retained / "leftover.txt"

    world.tick()
    settle_report = world.tick_report(world.last_pr_record())
    assert "## Uncommitted changes kept for a human" in settle_report
    assert f"PR #{PR_NUMBER}: {world.retained}" in settle_report
    assert "worker_left_uncommitted_changes" in settle_report
    assert leftover.read_text() == "edited, never committed\n"

    world.tick()
    refusal_report = world.tick_report(world.last_pr_record())
    assert f"PR #{PR_NUMBER}: {world.retained}" in refusal_report
    assert "worktree_reuse_not_clean" in refusal_report
    assert leftover.read_text() == "edited, never committed\n"


def test_pr_without_head_commit_is_skipped_not_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR whose head we cannot read has nothing to measure a push against, so no agent
    starts and no attempt is spent."""
    gh = FakeGh()
    client = FakeWorkerClient()
    world = World(tmp_path, monkeypatch, gh=gh, client=client)
    original = gh.pr_list
    gh.pr_list = lambda **kw: [  # type: ignore[method-assign]
        {k: v for k, v in p.items() if k != "headRefOid"} for p in original(**kw)
    ]

    world.tick()

    record = world.last_pr_record()
    assert record.state == "skipped"
    assert record.extra is not None
    assert record.extra["reason"] == "pr_head_unknown"
    assert client.prompts == 0
    assert attempt_count_for_pr(world.history_path, world.job.name, PR_NUMBER) == 0
