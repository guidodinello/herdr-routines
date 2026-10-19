"""Acceptance tests for issue 014: API / webhook trigger (event-triggered job type).

Maps 1:1 to the `## Acceptance criteria` in
`docs/pipeline/runs/20261013T050000Z/spec.md` (spec v2). Each test is named
exactly as the criterion's `Test:` suffix requires.

The feature does not exist yet: these tests drive the real tick path
(`run_tick` with a fake `GhClient` + recording Herdr client, `load_config`
for schema) and assert the spec-mandated behaviour — poll + diff against
`last_seen_event_ids`, one worker per new event, `event_id` dedup, per-event
retry budget, fail-closed gate errors. They fail until the `github_event`
check kind lands in `config.py` / `auto_fix.py` / `tick.py`.

Contract notes for the implementer (deliberate, from the spec):

- Polling reuses the existing `GhClient` protocol (`pr_list` / `graphql`
  shapes, read-only). The `EventGhClient` fake below serves the single
  canned event on every read seam, so the poll may use any of them.
- The canned event is PR #42 labelled `ready-for-agent`. History keys are
  `last_seen_event_ids` (list), `event_id` (containing `42`), and
  `gate` (`passed` | `failed`). Per-attempt records carry
  `reason: gate_failed` plus `event_id`.
"""

from __future__ import annotations

import re
import subprocess as _sp
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from herdr_routines.auto_fix import (
    PostedBody,
    ReviewSearch,
    build_event_fix_prompt,
    enrich_event_detail,
    event_worktree_slug,
    poll_github_event,
)
from herdr_routines.config import ConfigError, GateCheck, Job, RoutinesConfig
from herdr_routines.history import HistoryRecord, append, read_job
from herdr_routines.tick import run_tick

SPEC_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "pipeline"
    / "runs"
    / "20261013T050000Z"
    / "spec.md"
)

EVENT_NUMBER = 42
EVENT_LABEL = "ready-for-agent"
EVENT_TITLE = "Add widget exporter behind ready-for-agent"
EVENT_ID = "evt-pr-42-ready-for-agent"
EVENT_BRANCH = "auto/add-widget-exporter"

READ_CALLS = frozenset(
    {
        "api_user",
        "pr_list",
        "pr_view",
        "graphql",
        "commit_check_runs",
        "pr_review_comments",
    }
)


# -- helpers -------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    proc = _sp.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


def _init_repo(tmp_path: Path) -> Path:
    """A real git repo with `main` + origin, so branch assertions are meaningful."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    _git("remote", "add", "origin", "git@github.com:test/repo.git", cwd=repo)
    return repo


def _branches(repo: Path) -> str:
    return _git("branch", "--list", cwd=repo)


class MockSubprocess:
    """Tick's subprocess seam: git remote detection succeeds, everything else
    is a no-op success (mirrors tests/test_tick.py)."""

    class Result:
        returncode = 0
        stdout = "git@github.com:test/repo.git"
        stderr = ""

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return MockSubprocess.Result()


def _make_event_job(repo: Path, **overrides: Any) -> Job:
    job = Job(
        name="on-label-pr",
        enabled=True,
        cron="* * * * *",
        repo=repo,
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="",
        timeout_ms=600_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
        checks=(GateCheck(kind="github_event"),),
        target="pr",
        max_workers_per_tick=1,
        max_attempts_per_target=3,
        kind="gated",
    )
    if not overrides:
        return job
    from dataclasses import replace

    return replace(job, **overrides)


class EventGhClient:
    """Fake `GhClient` serving one canned `ready-for-agent` event (PR #42) on
    every read-only seam (`pr_list` carries the label, `pr_view` confirms it,
    `graphql` returns the event node). Pass `events=False` for the clean case,
    `raise_on={"pr_list", "graphql"}` for the auth/rate-limit failure case."""

    def __init__(
        self,
        *,
        user: str = "testuser",
        events: bool = True,
        raise_on: set[str] | None = None,
    ) -> None:
        self.user = user
        self.events = events
        self.raise_on = raise_on or set()
        self.calls: list[str] = []

    def _maybe_raise(self, name: str) -> None:
        self.calls.append(name)
        if name in self.raise_on:
            raise RuntimeError(f"gh api {name} failed: rate limit exceeded")

    def api_user(self) -> str:
        self.calls.append("api_user")
        if "api_user" in self.raise_on:
            raise RuntimeError("gh auth failed")
        return self.user

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        self._maybe_raise("pr_list")
        if not self.events:
            return []
        return [
            {
                "number": EVENT_NUMBER,
                "headRefName": EVENT_BRANCH,
                "author": {"login": self.user, "is_bot": False},
                "url": f"https://github.com/{owner}/{repo}/pull/{EVENT_NUMBER}",
                "labels": [{"name": EVENT_LABEL}],
                "isCrossRepository": False,
            }
        ]

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append(f"pr_view:{number}")
        if "pr_view" in self.raise_on:
            raise RuntimeError("gh pr view failed")
        if not self.events or number != EVENT_NUMBER:
            return {}
        return {
            "statusCheckRollup": [{"name": "ci", "state": "SUCCESS"}],
            "headRefName": EVENT_BRANCH,
            "title": EVENT_TITLE,
            "labels": [{"name": EVENT_LABEL}],
        }

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self._maybe_raise("graphql")
        node: dict[str, object] = {
            "id": EVENT_ID,
            "number": EVENT_NUMBER,
            "title": EVENT_TITLE,
            "labels": [EVENT_LABEL],
            "headRefName": EVENT_BRANCH,
            "author": {"login": self.user},
            "isCrossRepository": False,
        }
        nodes = [node] if self.events else []
        return {
            "data": {
                "repository": {"pullRequests": {"nodes": nodes}},
                "search": {"nodes": nodes},
                "events": nodes,
            }
        }

    def commit_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, object]]:
        self.calls.append(f"commit_check_runs:{sha}")
        return []

    def pr_review_comments(
        self, *, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        self.calls.append(f"pr_review_comments:{number}")
        return []

    def pr_create(
        self, *, owner: str, repo: str, branch: str, title: str, body: str
    ) -> int:
        self.calls.append(f"pr_create:{branch}")
        return 1

    # kind: review (issue 055) members of the GhClient protocol; never polled here.
    def search_prs(self, modes: Sequence[str], limit: int = 100) -> ReviewSearch | None:
        return ReviewSearch(prs=())

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[PostedBody] | None:
        return []

    def repo_info(self, *, owner: str, repo: str) -> dict[str, object] | None:
        return None


class CiGhClient(EventGhClient):
    """EventGhClient whose `graphql` read returns one open PR's status rollup, so
    the batched `ci:failed` poll can be exercised without any per-PR `pr_view`."""

    def __init__(self, *, ci_state: str = "FAILURE", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ci_state = ci_state

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self.calls.append("graphql")
        return {
            "data": {
                "repository": {
                    "pullRequests": {
                        "nodes": [
                            {
                                "number": EVENT_NUMBER,
                                "title": EVENT_TITLE,
                                "headRefName": EVENT_BRANCH,
                                "author": {"login": self.user},
                                "isCrossRepository": False,
                                "labels": {"nodes": [{"name": EVENT_LABEL}]},
                                "statusCheckRollup": {"state": self.ci_state},
                            }
                        ]
                    }
                }
            }
        }


class RecordingHerdrClient:
    """Superset fake HerdrClient: records spawns, prompts and notifications so
    tests can assert dispatch counts and prompt seeding. Never reports live
    agents (dedup must come from `event_id`, not the live-agent guard)."""

    def __init__(self) -> None:
        self.tab_creates: list[dict[str, Any]] = []
        self.agent_starts: list[str] = []
        self.prompts: list[str] = []
        self.notifications: list[tuple[str, str | None, str]] = []

    def tab_create(self, *, cwd: Any, label: Any = None) -> str:
        self.tab_creates.append({"cwd": str(cwd), "label": label})
        return f"w1:p{len(self.tab_creates)}"

    def agent_start(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        start_timeout_ms: int,
        model: Any = None,
        session_id: Any = None,
    ) -> None:
        self.agent_starts.append(name)

    def agent_interactive_ready(self, target: str) -> bool:
        return True

    def agent_prompt_wait(self, *, target: str, text: str, timeout_ms: int) -> str:
        self.prompts.append(text)
        return "idle"

    def agent_prompt_wait_with_watchdog(
        self,
        *,
        target: str,
        text: str,
        timeout_ms: int,
        poll_interval_s: float = 30.0,
        on_poll: Any = None,
    ) -> str:
        self.prompts.append(text)
        return "idle"

    def agent_read(self, target: str, *, lines: int = 200) -> str:
        return ""

    def agent_read_visible(self, target: str, *, lines: int = 200) -> str:
        return ""

    def agent_statuses(self) -> dict[str, str]:
        return {}

    def agent_session_id(self, name: str) -> str:
        return "sess-1"

    def pane_close(self, pane_id: str) -> None:
        pass

    def workspace_close(self, workspace_id: str) -> None:
        pass

    def settled_agent_workspace(self, name: str) -> Any:
        return None

    def settled_agent_pane(self, name: str) -> Any:
        return None

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append((title, body, sound))


def _register_then_tick(
    job: Job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh_client: EventGhClient,
    client: RecordingHerdrClient,
    *,
    at: datetime,
) -> tuple[Path, Any]:
    """Register on one tick, run the gated path on the next — the shared shape
    every tick-level test here needs."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,))
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh_client)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    run_tick(config, history_path, client=client, now=at)  # type: ignore[arg-type]
    outcome = run_tick(
        config,
        history_path,
        client=client,  # type: ignore[arg-type]
        now=at + timedelta(minutes=1),
    )
    return history_path, outcome


def _base_calls(calls: list[str]) -> set[str]:
    return {c.split(":")[0] for c in calls}


# -- AC 1: clean tick ------------------------------------------------------------


def test_github_event_clean_tick_no_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1: no new matching event -> no agent, no branch, `done` with
    `extra.gate == "passed"`, costing only read-only `gh api` polls."""
    repo = _init_repo(tmp_path)
    branches_before = _branches(repo)
    job = _make_event_job(repo)
    gh = EventGhClient(events=False)
    client = RecordingHerdrClient()

    t0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)
    history_path, _ = _register_then_tick(job, tmp_path, monkeypatch, gh, client, at=t0)

    assert client.tab_creates == [], "clean tick spawned an agent"
    assert client.agent_starts == []
    assert _branches(repo) == branches_before, "clean tick created a branch"

    records = read_job(history_path, job.name)
    terminal = records[-1]
    assert terminal.state == "done"
    assert terminal.extra is not None
    assert terminal.extra.get("gate") == "passed"
    # The diff watermark: the tick must remember what it has seen.
    assert isinstance(terminal.extra.get("last_seen_event_ids"), list)

    # Only the read-only poll ran — no listener, no tunnel, no writes.
    assert len(gh.calls) >= 1, "clean tick never polled gh"
    assert _base_calls(gh.calls) <= READ_CALLS


# -- AC 2: one new event -> one worker -------------------------------------------


def test_github_event_new_event_dispatches_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: one new matching event dispatches exactly one worker whose prompt
    is seeded with the event payload, plus a per-attempt record with
    `reason: gate_failed` and `event_id`."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo)
    gh = EventGhClient(events=True)
    client = RecordingHerdrClient()

    t0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)
    history_path, _ = _register_then_tick(job, tmp_path, monkeypatch, gh, client, at=t0)

    assert len(client.tab_creates) == 1, (
        f"expected exactly one worker, got {len(client.tab_creates)}"
    )
    assert len(client.prompts) == 1
    prompt = client.prompts[0]
    assert EVENT_TITLE in prompt, "worker prompt is not seeded with the event payload"
    assert str(EVENT_NUMBER) in prompt

    attempts = [
        r
        for r in read_job(history_path, job.name)
        if r.extra and r.extra.get("reason") == "gate_failed"
    ]
    assert len(attempts) == 1, (
        f"expected one gate_failed attempt record, got {len(attempts)}"
    )
    event_id = attempts[0].extra.get("event_id") if attempts[0].extra else None
    assert event_id is not None, "attempt record carries no event_id"
    assert str(EVENT_NUMBER) in str(event_id)


# -- AC 3: re-poll dedups --------------------------------------------------------


def test_github_event_repoll_dedups_same_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 3: re-polling the same event ID dispatches no second worker and
    records a clean pass via `event_id` dedup against `last_seen_event_ids`."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo)
    gh = EventGhClient(events=True)
    client = RecordingHerdrClient()

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,))
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    t0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    run_tick(
        config,
        history_path,
        client=client,  # type: ignore[arg-type]
        now=t0 + timedelta(minutes=1),
    )
    assert len(client.tab_creates) == 1, "first sight of the event must dispatch once"

    # Same event returned again: no second worker.
    run_tick(
        config,
        history_path,
        client=client,  # type: ignore[arg-type]
        now=t0 + timedelta(minutes=2),
    )
    assert len(client.tab_creates) == 1, "re-poll dispatched a duplicate worker"

    records = read_job(history_path, job.name)
    terminal = records[-1]
    assert terminal.state == "done"
    assert terminal.extra is not None
    assert terminal.extra.get("gate") == "passed"
    seen = terminal.extra.get("last_seen_event_ids")
    assert isinstance(seen, list) and any(str(EVENT_NUMBER) in str(e) for e in seen), (
        "clean re-poll must record the deduped event ID"
    )


# -- AC 4: budget taps out -------------------------------------------------------


def test_github_event_budget_exceeded_skips_with_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 4: `max_attempts_per_target` exceeded for an `event_id` records
    `skipped` plus a notification, with no further worker dispatch."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo)

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,))
    t0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)
    run_tick(config, history_path, client=RecordingHerdrClient(), now=t0)  # type: ignore[arg-type]

    # Burn the whole per-event retry budget with real attempt records.
    for i in range(job.max_attempts_per_target):
        append(
            history_path,
            HistoryRecord(
                ts=t0 + timedelta(minutes=i + 1),
                job=job.name,
                state="done",
                run_id=f"seed-{i}",
                extra={
                    "event_id": EVENT_ID,
                    "pr_number": EVENT_NUMBER,
                    "reason": "gate_failed",
                    "attempt": i,
                },
            ),
        )

    gh = EventGhClient(events=True)
    client = RecordingHerdrClient()
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())
    run_tick(
        config,
        history_path,
        client=client,  # type: ignore[arg-type]
        now=t0 + timedelta(minutes=60),
    )

    assert client.tab_creates == [], "over-budget event dispatched a worker"
    skipped = [r for r in read_job(history_path, job.name) if r.state == "skipped"]
    assert skipped, "over-budget event must record skipped"
    titles = [t.lower() for t, _, _ in client.notifications]
    assert any("skipped" in t for t in titles), (
        f"over-budget event must notify, got: {titles}"
    )


# -- AC 5: api failure fails closed ----------------------------------------------


def test_github_event_api_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 5: `gh api` auth/rate-limit failure fails closed as a gate error —
    never a silent green pass — surfaced like other gate errors."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo)
    gh = EventGhClient(events=True, raise_on={"pr_list", "graphql"})
    client = RecordingHerdrClient()

    t0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)
    history_path, _ = _register_then_tick(job, tmp_path, monkeypatch, gh, client, at=t0)

    assert len(gh.calls) >= 1, "tick never attempted the event poll"
    records = read_job(history_path, job.name)
    terminal = records[-1]
    assert terminal.state == "failed", (
        f"api failure must fail closed, got {terminal.state} "
        f"(silent green: {terminal.extra})"
    )
    assert terminal.extra is not None
    assert terminal.extra.get("gate") != "passed", "gate must not report passed"
    titles = [t.lower() for t, _, _ in client.notifications]
    assert any("failed" in t or "error" in t for t in titles), (
        f"gate error must be surfaced via notify, got: {titles}"
    )


# -- AC 6: config validation -----------------------------------------------------


def _event_job_yaml(
    *,
    query: str = "prs:labels",
    match: str = '{label: "ready-for-agent"}',
    extra_check: str = "",
) -> str:
    return f"""\
version: 1
jobs:
  - name: on-label-pr
    cron: "*/10 * * * *"
    repo: /repo/test
    kind: gated
    max_workers_per_tick: 1
    max_attempts_per_target: 3
    checks:
      - github_event:
          query: {query}
          repo: owner/repo
          match: {match}
          timeout_ms: 60000
{extra_check}\
"""


def test_github_event_config_validation(
    tmp_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 6: the closed `query` set and `match` shape validate with no network,
    and `github_event` mixed with `pr_health`/`command` in one job is rejected."""
    import socket

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("config validation must not touch the network")

    monkeypatch.setattr(socket, "socket", _blocked)

    from herdr_routines.config import load_config

    def _load(text: str) -> RoutinesConfig:
        tmp_config_path.write_text(text)
        return load_config(tmp_config_path)

    # The spec's sample: closed query, match shape, poll timeout.
    cfg = _load(_event_job_yaml())
    job = cfg.job("on-label-pr")
    assert job is not None
    assert job.kind == "gated"
    assert job.checks is not None and len(job.checks) == 1
    check = job.checks[0]
    assert check.kind == "github_event"
    assert getattr(check, "query", None) == "prs:labels"
    assert getattr(check, "timeout_ms", None) == 60000
    match = getattr(check, "match", None)
    assert match is not None and match.get("label") == "ready-for-agent"

    # The other two closed queries validate too.
    for query in ("issues:opened", "ci:failed"):
        cfg = _load(_event_job_yaml(query=query))
        loaded = cfg.job("on-label-pr")
        assert loaded is not None and loaded.checks is not None
        assert getattr(loaded.checks[0], "query", None) == query

    # Unknown query outside the closed set is rejected.
    with pytest.raises(ConfigError):
        _load(_event_job_yaml(query="prs:merged"))

    # Malformed match is rejected.
    with pytest.raises(ConfigError):
        _load(_event_job_yaml(match="{bogus_key: 1}"))

    # A match value must be a non-empty string: a number or "" could never match.
    for bad in ("{label: 123}", "{label: ''}", "{author: [a]}"):
        with pytest.raises(ConfigError, match="non-empty string"):
            _load(_event_job_yaml(match=bad))

    # An issue has no branch, so `match.branch` with issues:opened can never match.
    with pytest.raises(ConfigError, match="branch"):
        _load(_event_job_yaml(query="issues:opened", match="{branch: main}"))

    # `repo:` must be owner/name, not a bare name the poll cannot split.
    with pytest.raises(ConfigError, match="owner/name"):
        _load(_event_job_yaml().replace("repo: owner/repo", "repo: justaname"))

    # Mixing github_event with pr_health / command in one job is rejected.
    with pytest.raises(ConfigError, match="mix"):
        _load(
            _event_job_yaml(
                extra_check="      - pr_health:\n",
            )
        )
    with pytest.raises(ConfigError, match="mix"):
        _load(
            _event_job_yaml(
                extra_check="      - command: uv run ruff check .\n",
            )
        )


# -- AC 7: history keys, example job, timeout docs --------------------------------


def test_github_event_history_example_and_timeout_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 7: `history.jsonl` carries the new `extra` keys, the shipped
    `deploy/jobs.example.yaml` sample validates, and the cli systemd-timeout
    worst case covers poll time plus workers."""
    from herdr_routines import cli
    from herdr_routines.config import ConfigError, load_config
    from herdr_routines.history import HistoryRecord

    # --- history.jsonl round-trips the new extra keys -------------------------
    now = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)
    history_path = tmp_path / "history.jsonl"
    append(
        history_path,
        HistoryRecord(
            ts=now,
            job="on-label-pr",
            state="done",
            run_id="on-label-pr-20261013T050000Z",
            extra={
                "gate": "passed",
                "last_seen_event_ids": [EVENT_ID],
                "event_id": EVENT_ID,
            },
        ),
    )
    records = read_job(history_path, "on-label-pr")
    assert len(records) == 1
    assert records[0].extra is not None
    assert records[0].extra["gate"] == "passed"
    assert records[0].extra["last_seen_event_ids"] == [EVENT_ID]
    assert records[0].extra["event_id"] == EVENT_ID

    # --- the shipped example carries a validating github_event sample ----------
    example = Path(__file__).resolve().parent.parent / "deploy" / "jobs.example.yaml"
    example_text = example.read_text()
    assert "github_event" in example_text, (
        "deploy/jobs.example.yaml must document a github_event job"
    )
    raw = yaml.safe_load(example_text)
    assert isinstance(raw, dict) and isinstance(raw.get("jobs"), list)
    sample_jobs = [
        j
        for j in raw["jobs"]
        if isinstance(j, dict)
        and any(
            isinstance(c, dict) and "github_event" in c for c in (j.get("checks") or [])
        )
    ]
    assert sample_jobs, "example must contain a job with a github_event check"
    sample_path = tmp_path / "sample-jobs.yaml"
    sample_path.write_text(yaml.safe_dump({"version": 1, "jobs": sample_jobs}))
    try:
        sample_cfg = load_config(sample_path)
    except ConfigError as e:
        pytest.fail(f"example github_event job does not validate: {e}")
    sample_job = sample_cfg.job(sample_jobs[0]["name"])
    assert sample_job is not None and sample_job.checks is not None
    assert any(c.kind == "github_event" for c in sample_job.checks)

    # --- systemd-timeout worst case covers poll time plus workers --------------
    def _flagged(job: Job, timeout_s: int) -> bool:
        unit = tmp_path / f"unit-{timeout_s}.service"
        unit.write_text(f"[Service]\nTimeoutStartSec={timeout_s}\n")
        return bool(cli._check_systemd_timeout(RoutinesConfig(jobs=(job,)), unit))

    repo = tmp_path / "repo"
    base_job = _make_event_job(repo)
    assert _flagged(base_job, 60), "a 60s unit cannot cover an event job"
    assert not _flagged(base_job, 86_400)

    # Workers covered: some unit size must pass with 1 worker but flag with 3.
    one_worker = _make_event_job(repo, max_workers_per_tick=1)
    three_workers = _make_event_job(repo, max_workers_per_tick=3)
    assert any(
        _flagged(three_workers, t) != _flagged(one_worker, t)
        for t in (300, 600, 1200, 1800, 3600, 7200, 20000)
    ), "systemd worst case must grow with max_workers_per_tick"

    # Poll covered: some unit size must differ when the check timeout grows.
    fast_poll = _make_event_job(
        repo, checks=(GateCheck(kind="github_event", timeout_ms=60_000),)
    )
    slow_poll = _make_event_job(
        repo, checks=(GateCheck(kind="github_event", timeout_ms=600_000),)
    )
    assert any(
        _flagged(slow_poll, t) != _flagged(fast_poll, t)
        for t in (300, 600, 1200, 1800, 3600, 7200, 20000)
    ), "systemd worst case must grow with the event-poll timeout"


# -- Review notes: tiers present -------------------------------------------------


def test_github_event_review_tiers_present() -> None:
    """Review notes: every `## Acceptance criteria` line carries its `Test:`
    name, a `blocking`/`non-blocking` tier, and a `confidence:` rating."""
    text = SPEC_PATH.read_text()
    section = re.split(r"^## Acceptance criteria\s*$", text, flags=re.MULTILINE)[1]
    section = re.split(r"^## \S", section, flags=re.MULTILINE)[0]
    lines = [ln.strip() for ln in section.splitlines() if re.match(r"^\d+\.", ln)]
    assert len(lines) >= 7, f"expected >=7 criteria, got {len(lines)}"
    for line in lines:
        assert "Test: test_github_event_" in line, f"missing test name: {line}"
        assert ("blocking" in line) or ("non-blocking" in line), f"missing tier: {line}"
        assert "confidence:" in line, f"missing confidence: {line}"


# -- Review fixes: issue events, batched ci:failed, slug, prompt -----------------


def test_issue_event_enrichment_skips_pr_view() -> None:
    """`issues:opened` events carry issue numbers, so enrichment must not hit the
    PR-only `pr_view` (which would fail and quietly drop the title)."""
    gh = EventGhClient(events=True)
    check = GateCheck(kind="github_event", query="issues:opened", match={})
    events = poll_github_event(gh, owner="test", repo="repo", check=check)
    assert len(events) == 1
    event = events[0]
    assert event.is_issue is True
    assert event.title == EVENT_TITLE

    before = len(gh.calls)
    detailed = enrich_event_detail(gh, event, owner="test", repo="repo")
    assert detailed is event
    assert not any(c.startswith("pr_view") for c in gh.calls[before:]), (
        "issue enrichment must not call the PR-only pr_view endpoint"
    )


def test_ci_failed_polls_once_without_per_pr_view() -> None:
    """`ci:failed` batches the status rollup into one GraphQL read instead of a
    `pr_list` + per-PR `pr_view` N+1."""
    gh = CiGhClient(ci_state="FAILURE")
    check = GateCheck(kind="github_event", query="ci:failed", match={})
    events = poll_github_event(gh, owner="owner", repo="repo", check=check)
    assert [e.event_id for e in events] == [f"evt-ci-{EVENT_NUMBER}"]
    assert events[0].title == EVENT_TITLE
    assert gh.calls == ["graphql"], (
        f"ci:failed must be one batched read, got {gh.calls}"
    )

    # A green rollup yields no events.
    clean = CiGhClient(ci_state="SUCCESS")
    assert poll_github_event(clean, owner="owner", repo="repo", check=check) == []


def test_event_worktree_slug_is_filesystem_safe() -> None:
    """A label with spaces/slashes in the event id must not leak into the worktree
    path, and distinct ids must slug apart."""
    assert event_worktree_slug(EVENT_ID) == EVENT_ID
    dirty = "evt-pr-42-review needed/now"
    slug = event_worktree_slug(dirty)
    assert " " not in slug and "/" not in slug
    assert slug.startswith("evt-pr-42-review-needed-now")
    assert event_worktree_slug("evt-pr-42-a b") != event_worktree_slug("evt-pr-42-a-b")


def test_build_event_fix_prompt_matches_worktree_checkout() -> None:
    """The publish step must reflect the worktree `_dispatch_event_worker` prepared:
    a PR event is on its head branch (no new branch), an issue event gets a PR."""
    pr_event = poll_github_event(
        EventGhClient(events=True),
        owner="test",
        repo="repo",
        check=GateCheck(kind="github_event", query="prs:labels", match={}),
    )[0]
    prompt = build_event_fix_prompt(
        job_name="on-label-pr",
        event=pr_event,
        owner_repo="test/repo",
        report_path="/tmp/report.md",
        base="main",
    )
    assert EVENT_BRANCH in prompt
    assert "already checked out" in prompt
    assert "Create a branch" not in prompt

    issue_event = poll_github_event(
        EventGhClient(events=True),
        owner="test",
        repo="repo",
        check=GateCheck(kind="github_event", query="issues:opened", match={}),
    )[0]
    issue_prompt = build_event_fix_prompt(
        job_name="on-label-pr",
        event=issue_event,
        owner_repo="test/repo",
        report_path="/tmp/report.md",
        base="main",
    )
    assert "Create a branch" in issue_prompt
    assert "gh pr create --base main" in issue_prompt


def test_github_event_empty_match_validates(tmp_config_path: Path) -> None:
    """An empty `match: {}` is a valid mapping — `ci:failed` jobs may want every
    failing PR without label/branch/author filters."""
    from herdr_routines.config import load_config

    tmp_config_path.write_text(_event_job_yaml(match="{}"))
    cfg = load_config(tmp_config_path)
    job = cfg.job("on-label-pr")
    assert job is not None and job.checks is not None
    assert job.checks[0].match == {}


# -- dedup watermark: retry, over-cap and re-fire ----------------------------------


class ScriptedEventGh(EventGhClient):
    """EventGhClient whose `pr_list` returns whichever labelled PRs `numbers` holds
    right now, so a test can add, keep or clear events between ticks."""

    def __init__(self, numbers: list[int]) -> None:
        super().__init__(events=True)
        self.numbers = numbers

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        self._maybe_raise("pr_list")
        return [
            {
                "number": n,
                "headRefName": f"feature-{n}",
                "author": {"login": self.user},
                "url": f"https://github.com/{owner}/{repo}/pull/{n}",
                "labels": [{"name": EVENT_LABEL}],
                "isCrossRepository": False,
            }
            for n in self.numbers
        ]


class StatusHerdrClient(RecordingHerdrClient):
    """RecordingHerdrClient whose worker settles with the next status from
    `statuses` (then "idle"), to drive a failed worker."""

    def __init__(self, statuses: list[str]) -> None:
        super().__init__()
        self.statuses = statuses

    def agent_prompt_wait_with_watchdog(
        self,
        *,
        target: str,
        text: str,
        timeout_ms: int,
        poll_interval_s: float = 30.0,
        on_poll: Any = None,
    ) -> str:
        self.prompts.append(text)
        return self.statuses.pop(0) if self.statuses else "idle"


def _event_ticks(
    job: Job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh: EventGhClient,
    client: RecordingHerdrClient,
) -> Any:
    """Register the job, then return a `tick(minute)` callable for later ticks."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,))
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())
    t0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]

    def tick(minute: int) -> None:
        run_tick(
            config,
            history_path,
            client=client,  # type: ignore[arg-type]
            now=t0 + timedelta(minutes=minute),
        )

    return tick


def _dispatched_numbers(client: RecordingHerdrClient) -> list[int]:
    return [int(m.group(1)) for p in client.prompts if (m := re.search(r"#(\d+)", p))]


def test_github_event_over_cap_event_dispatches_on_a_later_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An event left over by `max_workers_per_tick` is not marked seen: the next
    tick dispatches it instead of dropping it forever."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo, max_workers_per_tick=1)
    gh = ScriptedEventGh([7, 8])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)

    tick(1)
    tick(2)
    tick(3)
    assert sorted(_dispatched_numbers(client)) == [7, 8]


def test_github_event_failed_worker_retries_until_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed worker leaves its event out of the watermark, so later ticks retry it;
    once the per-event budget is spent it is skipped (with one notify) and seen."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo, max_attempts_per_target=2)
    gh = ScriptedEventGh([7])
    client = StatusHerdrClient(["failed", "failed"])
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)

    for minute in range(1, 6):
        tick(minute)

    assert _dispatched_numbers(client) == [7, 7], "two attempts, then the budget"
    skipped = [n for n in client.notifications if "skipped" in n[0].lower()]
    assert len(skipped) == 1, "budget exhaustion notifies once, not every tick"


def test_github_event_refires_after_it_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watermark is the latest poll, not all history: an event that clears and
    comes back (CI red again, a label re-added) dispatches again."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo)
    gh = ScriptedEventGh([7])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)

    tick(1)  # fires
    tick(2)  # same event: deduped
    gh.numbers = []
    tick(3)  # cleared
    gh.numbers = [7]
    tick(4)  # back: fires again
    assert _dispatched_numbers(client) == [7, 7]


def test_real_pr_list_requests_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prs:labels` matches on `labels`, so the real `gh pr list` must ask for them
    (the fakes above always return labels, which hid their absence)."""
    from herdr_routines.auto_fix import RealGhClient

    seen: list[list[str]] = []

    def _run(self: RealGhClient, argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, "[]", ""

    monkeypatch.setattr(RealGhClient, "_run", _run)
    RealGhClient().pr_list(owner="o", repo="r", state="open", limit=10)
    fields = seen[0][seen[0].index("--json") + 1].split(",")
    assert "labels" in fields


class ScriptedIssueGh(EventGhClient):
    """EventGhClient whose `issues:opened` search returns whichever issues `numbers`
    holds right now; `fail` makes the next poll raise, as an auth error would."""

    def __init__(self, numbers: list[int]) -> None:
        super().__init__(events=True)
        self.numbers = numbers
        self.fail = False

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self._maybe_raise("graphql")
        if self.fail:
            raise RuntimeError("gh api graphql failed: rate limit exceeded")
        nodes = [
            {
                "id": f"I_{n}",
                "number": n,
                "title": f"Issue {n}",
                "author": {"login": self.user},
                "labels": [],
            }
            for n in self.numbers
        ]
        return {"data": {"search": {"nodes": nodes}}}


def _issue_job(repo: Path) -> Job:
    check = GateCheck(kind="github_event", query="issues:opened", match={})
    return _make_event_job(repo, checks=(check,), max_workers_per_tick=5)


def test_issues_opened_first_poll_adopts_open_issues_as_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling an `issues:opened` job dispatches nothing for the issues already
    open: the first poll records them as seen, and only an issue opened after
    that gets a worker (issue #164)."""
    repo = _init_repo(tmp_path)
    job = _issue_job(repo)
    gh = ScriptedIssueGh([3, 4])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)

    tick(1)
    assert client.tab_creates == [], "first poll dispatched on pre-existing issues"
    history_path = tmp_path / "state" / "history.jsonl"
    terminal = read_job(history_path, job.name)[-1]
    assert terminal.state == "done"
    assert terminal.extra is not None
    assert terminal.extra.get("gate") == "baseline"
    assert terminal.extra.get("last_seen_event_ids") == ["evt-issue-3", "evt-issue-4"]

    gh.numbers = [3, 4, 5]
    tick(2)
    assert _dispatched_numbers(client) == [5]


def test_issues_opened_failed_first_poll_does_not_count_as_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed poll records no watermark, so the next successful poll is still the
    first one and adopts the baseline rather than dispatching every open issue."""
    repo = _init_repo(tmp_path)
    job = _issue_job(repo)
    gh = ScriptedIssueGh([3, 4])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)

    gh.fail = True
    tick(1)
    gh.fail = False
    tick(2)
    assert client.tab_creates == []


def test_issues_opened_empty_baseline_still_counts_as_polled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first poll that finds no open issues is a baseline too (an empty one): the
    first issue opened after it is new and dispatches."""
    repo = _init_repo(tmp_path)
    job = _issue_job(repo)
    gh = ScriptedIssueGh([])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)

    tick(1)
    gh.numbers = [9]
    tick(2)
    assert _dispatched_numbers(client) == [9]


# -- Same-repo PRs, trusted issue authors, head ref validation --------------------


class _PrPollGh(EventGhClient):
    """Serves `prs` on both PR read seams: `pr_list` (prs:labels) and the batched
    `ci:failed` GraphQL read."""

    def __init__(self, prs: list[dict[str, object]]) -> None:
        super().__init__(events=True)
        self.prs = prs

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        self._maybe_raise("pr_list")
        return self.prs

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self._maybe_raise("graphql")
        return {"data": {"repository": {"pullRequests": {"nodes": self.prs}}}}


def _poll_pr(number: int, **fields: object) -> dict[str, object]:
    pr: dict[str, object] = {
        "number": number,
        "title": f"PR {number}",
        "headRefName": f"feature-{number}",
        "author": {"login": "testuser"},
        "labels": [{"name": EVENT_LABEL}],
        "statusCheckRollup": {"state": "FAILURE"},
    }
    pr.update(fields)
    return pr


@pytest.mark.parametrize("query", ["prs:labels", "ci:failed"])
def test_pr_events_only_come_from_same_repo_prs(query: str) -> None:
    """A fork PR's head branch would be checked out in our clone under a name the fork
    chose, so PR events only fire for PRs that say their branch is in this repo. A
    payload that does not say is treated as a fork."""
    gh = _PrPollGh(
        [
            _poll_pr(1, isCrossRepository=False),
            _poll_pr(2, isCrossRepository=True),
            _poll_pr(3),  # field missing
            _poll_pr(4, isCrossRepository=None),
        ]
    )
    check = GateCheck(kind="github_event", query=query, match={})
    events = poll_github_event(gh, owner="o", repo="r", check=check)
    assert [e.number for e in events] == [1]


def test_ci_failed_query_requests_is_cross_repository() -> None:
    gh = _PrPollGh([])
    seen: list[str] = []
    real_graphql = gh.graphql

    def _graphql(query: str, **variables: str) -> dict[str, object]:
        seen.append(query)
        return real_graphql(query, **variables)

    gh.graphql = _graphql  # type: ignore[method-assign]
    check = GateCheck(kind="github_event", query="ci:failed", match={})
    poll_github_event(gh, owner="o", repo="r", check=check)
    assert "isCrossRepository" in seen[0]


class _IssueAuthorsGh(EventGhClient):
    """`issues:opened` search whose issue N is authored by `authors[N]` (a login, None
    for a deleted account, or absent for a node with no `author` key at all)."""

    def __init__(self, authors: dict[int, str | None], **kwargs: Any) -> None:
        super().__init__(user="owner", **kwargs)
        self.authors = authors
        self.missing: set[int] = set()

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self._maybe_raise("graphql")
        nodes: list[dict[str, object]] = []
        for n, login in self.authors.items():
            node: dict[str, object] = {"number": n, "title": f"Issue {n}"}
            if n not in self.missing:
                node["author"] = None if login is None else {"login": login}
            nodes.append(node)
        return {"data": {"search": {"nodes": nodes}}}


def test_issues_opened_without_author_match_only_fires_for_trusted_authors() -> None:
    """Without `match.author`, only issues from the trusted set fire: the configured
    logins (case-insensitive) plus the gh user. Anyone else, a deleted account and a
    node with no author are all dropped."""
    gh = _IssueAuthorsGh({1: "owner", 2: "Alice", 3: "stranger", 4: None, 5: "alice"})
    gh.missing = {5}
    check = GateCheck(kind="github_event", query="issues:opened", match={})
    events = poll_github_event(
        gh, owner="o", repo="r", check=check, trusted_review_authors=("alice",)
    )
    assert [e.number for e in events] == [1, 2]
    assert "api_user" in gh.calls


def test_issues_opened_explicit_author_match_stays_exact() -> None:
    """An explicit `match.author` keeps its exact-match meaning, trusted or not, and
    needs no gh user lookup."""
    gh = _IssueAuthorsGh({1: "owner", 3: "stranger"})
    check = GateCheck(
        kind="github_event", query="issues:opened", match={"author": "stranger"}
    )
    events = poll_github_event(gh, owner="o", repo="r", check=check)
    assert [e.number for e in events] == [3]
    assert "api_user" not in gh.calls


def test_issues_opened_fails_closed_when_the_gh_user_is_unknown() -> None:
    gh = _IssueAuthorsGh({1: "owner"}, raise_on={"api_user"})
    check = GateCheck(kind="github_event", query="issues:opened", match={})
    with pytest.raises(RuntimeError):
        poll_github_event(gh, owner="o", repo="r", check=check)


def test_issues_opened_tick_uses_configured_trusted_authors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_tick` hands the config's `trusted_review_authors` to the poll."""
    repo = _init_repo(tmp_path)
    job = _issue_job(repo)
    gh = _IssueAuthorsGh({})
    client = RecordingHerdrClient()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,), trusted_review_authors=("alice",))
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())
    t0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=UTC)

    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    run_tick(  # first poll: empty baseline
        config,
        history_path,
        client=client,  # type: ignore[arg-type]
        now=t0 + timedelta(minutes=1),
    )
    gh.authors = {7: "alice", 8: "stranger"}
    run_tick(
        config,
        history_path,
        client=client,  # type: ignore[arg-type]
        now=t0 + timedelta(minutes=2),
    )
    assert _dispatched_numbers(client) == [7]


class _RecordingGitSubprocess:
    """Tick's subprocess seam: `git check-ref-format` runs for real, every other call is
    recorded and succeeds like `MockSubprocess`."""

    TimeoutExpired = _sp.TimeoutExpired

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def run(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if argv[:2] == ["git", "check-ref-format"]:
            return _sp.run(
                argv, capture_output=True, text=True, timeout=10, check=False
            )
        self.argvs.append(list(argv))
        return MockSubprocess.Result()

    def worktree_adds(self) -> list[list[str]]:
        return [a for a in self.argvs if "worktree" in a and "add" in a]


@pytest.mark.parametrize("head_ref", ["-b", "bad..ref", "@{-1}", "has space"])
def test_invalid_head_ref_is_skipped_without_a_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head_ref: str
) -> None:
    """A head ref git would not accept as a branch name never reaches `git worktree
    add`: the event is recorded as skipped (`invalid_head_ref`), marked seen, and not
    retried on the next tick."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo)
    gh = _PrPollGh([_poll_pr(7, headRefName=head_ref, isCrossRepository=False)])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)
    git = _RecordingGitSubprocess()
    monkeypatch.setattr("herdr_routines.tick.subprocess", git)

    tick(1)
    tick(2)

    assert git.worktree_adds() == []
    assert client.tab_creates == []
    history_path = tmp_path / "state" / "history.jsonl"
    skipped = [
        r
        for r in read_job(history_path, job.name)
        if r.extra and r.extra.get("reason") == "invalid_head_ref"
    ]
    assert len(skipped) == 1, "an invalid ref is skipped once, then seen"
    assert skipped[0].state == "skipped"
    assert skipped[0].extra is not None
    assert skipped[0].extra.get("event_id") == f"evt-pr-7-{EVENT_LABEL}"


def test_valid_head_ref_dispatches_with_option_terminator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed head ref still dispatches, and `--` ends git's options before the
    worktree path and the ref."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo)
    gh = _PrPollGh([_poll_pr(7, headRefName="feature/ok-7", isCrossRepository=False)])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)
    git = _RecordingGitSubprocess()
    monkeypatch.setattr("herdr_routines.tick.subprocess", git)

    tick(1)

    assert len(client.tab_creates) == 1
    (add,) = git.worktree_adds()
    assert add[-3:-2] == ["--"]
    assert add[-1] == "feature/ok-7"


def test_head_ref_check_that_cannot_run_is_a_retried_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the branch-name check itself cannot run, the event is neither dispatched nor
    written off as invalid: it is a failed attempt, retried on later ticks within the
    event's budget."""
    repo = _init_repo(tmp_path)
    job = _make_event_job(repo, max_attempts_per_target=2)
    gh = _PrPollGh([_poll_pr(7, isCrossRepository=False)])
    client = RecordingHerdrClient()
    tick = _event_ticks(job, tmp_path, monkeypatch, gh, client)

    class _NoGit(_RecordingGitSubprocess):
        def run(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
            if argv[:2] == ["git", "check-ref-format"]:
                raise OSError("git: not found")
            return super().run(argv, *args, **kwargs)

    git = _NoGit()
    monkeypatch.setattr("herdr_routines.tick.subprocess", git)

    for minute in (1, 2, 3):
        tick(minute)

    assert git.worktree_adds() == []
    assert client.tab_creates == []
    history_path = tmp_path / "state" / "history.jsonl"
    reasons = [
        r.extra.get("reason") for r in read_job(history_path, job.name) if r.extra
    ]
    assert reasons.count("head_ref_check_failed") == 2, "retried up to the budget"
    assert "invalid_head_ref" not in reasons
    assert "max_attempts_exceeded" in reasons
