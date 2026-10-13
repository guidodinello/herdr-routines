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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

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
    outcome = run_tick(  # type: ignore[arg-type]
        config, history_path, client=client, now=at + timedelta(minutes=1)
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
    run_tick(  # type: ignore[arg-type]
        config, history_path, client=client, now=t0 + timedelta(minutes=1)
    )
    assert len(client.tab_creates) == 1, "first sight of the event must dispatch once"

    # Same event returned again: no second worker.
    run_tick(  # type: ignore[arg-type]
        config, history_path, client=client, now=t0 + timedelta(minutes=2)
    )
    assert len(client.tab_creates) == 1, "re-poll dispatched a duplicate worker"

    records = read_job(history_path, job.name)
    terminal = records[-1]
    assert terminal.state == "done"
    assert terminal.extra is not None
    assert terminal.extra.get("gate") == "passed"
    seen = terminal.extra.get("last_seen_event_ids")
    assert isinstance(seen, list) and any(
        str(EVENT_NUMBER) in str(e) for e in seen
    ), "clean re-poll must record the deduped event ID"


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
    run_tick(  # type: ignore[arg-type]
        config, history_path, client=client, now=t0 + timedelta(minutes=60)
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
    (records := read_job(history_path, "on-label-pr"))
    assert len(records) == 1
    assert records[0].extra is not None
    assert records[0].extra["gate"] == "passed"
    assert records[0].extra["last_seen_event_ids"] == [EVENT_ID]
    assert records[0].extra["event_id"] == EVENT_ID

    # --- the shipped example carries a validating github_event sample ----------
    example = (
        Path(__file__).resolve().parent.parent / "deploy" / "jobs.example.yaml"
    )
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
            isinstance(c, dict) and "github_event" in c
            for c in (j.get("checks") or [])
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
        return bool(
            cli._check_systemd_timeout(RoutinesConfig(jobs=(job,)), unit)
        )

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
        assert ("blocking" in line) or ("non-blocking" in line), (
            f"missing tier: {line}"
        )
        assert "confidence:" in line, f"missing confidence: {line}"
