"""Behavior tests for tick's review and audit dispatch flows (issue 062).

`_process_review_job` and `_process_audit_job` share one schedule-guard shape (registered,
stale reap, already running, live agent, not due, missed, collapsed, due) that differs only
in the running-record staleness window, so the guards are one table over both kinds. The
cycles behind them (`_review_cycle` / `_dispatch_review_target`, `_audit_cycle` /
`_run_audit` / `_dispatch_audit_fix`) run against real local git and a fake herdr / gh,
reusing the fakes of `test_review_scan` and `test_audit_dispatch`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import runner, tick
from herdr_routines.auto_fix import (
    build_audit_agent_name,
    build_gate_worker_agent_name,
    build_review_agent_name,
    build_review_agent_prefix,
    review_target_hash,
)
from herdr_routines.config import Job
from herdr_routines.findings import ledger_path, load_ledger
from herdr_routines.herdr import HerdrCliError
from herdr_routines.history import STALE_MARGIN, HistoryRecord, append, read_job
from tests.test_audit_dispatch import _FakeHerdr, _finding, _git
from tests.test_audit_dispatch import _job as _audit_job
from tests.test_review_scan import (
    ReviewGh,
    ReviewHerdrClient,
    _mark_posted,
    _pr,
    _review_job,
    _setup_repo,
)

NOW = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)
# One cron fire (08:30 UTC) lies between REGISTERED and NOW, 30 minutes late.
REGISTERED = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
STALE_MARGIN_MS = int(STALE_MARGIN.total_seconds() * 1000)


class GuardClient:
    """The slice of HerdrClient the schedule guards touch."""

    def __init__(
        self,
        *,
        statuses: dict[str, str] | None = None,
        panes: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.statuses = statuses or {}
        self.panes = panes or {}
        self.closed: list[str] = []
        self.notifications: list[tuple[str, str | None]] = []

    def agent_statuses(self) -> dict[str, str]:
        return dict(self.statuses)

    def agent_panes_by_status(self) -> dict[str, tuple[str, str]]:
        return dict(self.panes)

    def pane_close(self, pane_id: str) -> None:
        self.closed.append(pane_id)

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append((title, body))


@dataclass(frozen=True, slots=True)
class Kind:
    """What differs between the two flows under the shared guard table."""

    name: str
    make_job: Callable[..., Job]
    process: str
    cycle: str
    # Running-record ages (ms since it started): one that is still inside this kind's
    # staleness window but past a bare `timeout_ms` (so a wrong formula reads it stale),
    # and one past the window.
    inside_age_ms: int
    stale_age_ms: int
    live_agent: Callable[[Job], str]


def _review_kind(tmp_path: Path) -> Kind:
    # window = timeout_ms (60s) * max_workers_per_tick (3) + margin
    return Kind(
        name="review",
        make_job=lambda **over: _review_job(tmp_path, max_workers_per_tick=3, **over),
        process="_process_review_job",
        cycle="_review_cycle",
        inside_age_ms=STALE_MARGIN_MS + 120_000,
        stale_age_ms=STALE_MARGIN_MS + 200_000,
        live_agent=lambda job: job.agent_name,
    )


def _audit_kind(tmp_path: Path) -> Kind:
    # window = audit.timeout_ms (1.8M) + timeout_ms (2.7M) + margin
    return Kind(
        name="audit",
        # The audit helper's weekly cron is swapped for the review job's daily one so
        # both kinds share the REGISTERED / NOW dates.
        make_job=lambda **over: _audit_job(
            tmp_path / "repo", **{"cron": "30 8 * * *", **over}
        ),
        process="_process_audit_job",
        cycle="_audit_cycle",
        inside_age_ms=STALE_MARGIN_MS + 3_000_000,
        stale_age_ms=STALE_MARGIN_MS + 4_600_000,
        live_agent=lambda job: build_audit_agent_name(job.name, "any-run"),
    )


@pytest.fixture(params=["review", "audit"])
def kind(request: pytest.FixtureRequest, tmp_path: Path) -> Kind:
    return {"review": _review_kind, "audit": _audit_kind}[request.param](tmp_path)


@pytest.fixture
def history(tmp_path: Path) -> Path:
    return tmp_path / "history.jsonl"


@pytest.fixture
def cycle_calls(kind: Kind, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the kind's cycle with a recorder of the run_ids it is handed."""
    calls: list[str] = []

    def _cycle(job: Job, history_path: Path, *, run_id: str, **_kw: Any) -> Any:
        calls.append(run_id)
        return f"{job.name}: cycle ran", False

    monkeypatch.setattr(tick, kind.cycle, _cycle)
    return calls


def _process(
    kind: Kind, job: Job, history: Path, client: GuardClient
) -> tuple[str, bool]:
    return getattr(tick, kind.process)(
        job,
        history,
        client=client,
        now=NOW,
    )


def _seed(
    history: Path,
    job: Job,
    *,
    running_age_ms: int | None = None,
    run_id: str = "run-old",
) -> None:
    append(history, HistoryRecord(ts=REGISTERED, job=job.name, state="registered"))
    if running_age_ms is not None:
        append(
            history,
            HistoryRecord(
                ts=NOW - timedelta(milliseconds=running_age_ms),
                job=job.name,
                state="running",
                run_id=run_id,
            ),
        )


def _states(history: Path, job: Job) -> list[str]:
    return [r.state for r in read_job(history, job.name)]


def test_first_sight_registers_without_dispatch(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: registered", False)
    assert _states(history, job) == ["registered"]
    assert cycle_calls == []


def test_running_record_inside_the_kinds_window_blocks_the_tick(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    _seed(history, job, running_age_ms=kind.inside_age_ms)

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: skipped (already running)", False)
    assert _states(history, job) == ["registered", "running"]
    assert cycle_calls == []


def test_stale_running_record_is_interrupted_and_nothing_is_reaped_on_a_live_boot(
    kind: Kind, history: Path, cycle_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = kind.make_job()
    _seed(history, job, running_age_ms=kind.stale_age_ms)
    # The host booted before the stale run started, so its agents are not restored ones.
    monkeypatch.setattr(
        tick, "system_boot_epoch", lambda: (REGISTERED - timedelta(days=1)).timestamp()
    )
    client = GuardClient(panes={job.agent_name: ("blocked", "w1:p1")})

    _process(kind, job, history, client)

    stale = read_job(history, job.name)[-1]
    assert stale.state == "interrupted_unknown"
    assert stale.run_id == "run-old"
    assert stale.extra is not None
    assert stale.extra["reason"] == "stale_running_record"
    assert "reaped_agents" not in stale.extra
    assert client.closed == []


def test_stale_running_record_reaps_the_agents_a_reboot_restored(
    kind: Kind, history: Path, cycle_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `workspace: root` jobs never reap (their pane is the ambient workspace).
    job = kind.make_job(workspace="worktree")
    _seed(history, job, running_age_ms=kind.stale_age_ms)
    monkeypatch.setattr(tick, "system_boot_epoch", lambda: NOW.timestamp())
    client = GuardClient(
        panes={
            job.agent_name: ("blocked", "w1:p7"),
            "rt-unrelated-job": ("blocked", "w1:p8"),
        }
    )

    _process(kind, job, history, client)

    stale = read_job(history, job.name)[-1]
    assert stale.state == "interrupted_unknown"
    assert stale.extra is not None
    assert stale.extra["reaped_agents"] == [job.agent_name]
    assert client.closed == ["w1:p7"]


def test_live_agent_skips_the_tick_and_records_why(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    _seed(history, job)
    client = GuardClient(statuses={kind.live_agent(job): "working"})

    summary, failed = _process(kind, job, history, client)

    assert (summary, failed) == (f"{job.name}: skipped (agent already live)", False)
    skipped = read_job(history, job.name)[-1]
    assert skipped.state == "skipped"
    assert skipped.extra is not None
    assert skipped.extra["reason"] == "agent_name_live"
    assert cycle_calls == []


def test_not_due_writes_nothing(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    # Registered after this morning's 08:30 fire: nothing is due yet.
    append(
        history,
        HistoryRecord(ts=NOW - timedelta(minutes=15), job=job.name, state="registered"),
    )

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: not due", False)
    assert _states(history, job) == ["registered"]
    assert cycle_calls == []


@pytest.mark.parametrize("on_missed", ["log", "notify"])
def test_missed_occurrence_is_recorded_and_notifies_only_when_asked(
    kind: Kind, history: Path, cycle_calls: list[str], on_missed: str
) -> None:
    # The 08:30 fire is 30 minutes late; a 10 minute catch-up window misses it.
    job = kind.make_job(catch_up_minutes=10, on_missed=on_missed)
    _seed(history, job)
    client = GuardClient()

    summary, failed = _process(kind, job, history, client)

    assert (summary, failed) == (f"{job.name}: missed", False)
    missed = read_job(history, job.name)[-1]
    assert missed.state == "missed"
    assert missed.extra is not None
    assert missed.extra["reason"] == "outside_catch_up_window"
    assert missed.extra["occurrence"] == "2026-10-12T08:30:00+00:00"
    assert client.notifications == (
        [(f"herdr-routines: {job.name} missed", "outside catch-up window")]
        if on_missed == "notify"
        else []
    )
    assert cycle_calls == []


def test_due_tick_records_running_and_hands_the_run_id_to_the_cycle(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    _seed(history, job)

    summary, failed = _process(kind, job, history, GuardClient())

    assert (summary, failed) == (f"{job.name}: cycle ran", False)
    running = read_job(history, job.name)[-1]
    assert running.state == "running"
    assert running.run_id is not None
    assert running.extra is not None
    assert running.extra["scheduled_for"] == "2026-10-12T08:30:00+00:00"
    assert running.extra["late_seconds"] == 1800.0
    assert cycle_calls == [running.run_id]


def test_earlier_occurrences_collapse_into_one_missed_record_before_the_run(
    kind: Kind, history: Path, cycle_calls: list[str]
) -> None:
    job = kind.make_job()
    # Three fires (Oct 10, 11, 12) since registration: the first two collapse.
    append(
        history,
        HistoryRecord(
            ts=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
            job=job.name,
            state="registered",
        ),
    )

    _process(kind, job, history, GuardClient())

    records = read_job(history, job.name)
    assert [r.state for r in records] == ["registered", "missed", "running"]
    collapsed = records[1].extra
    assert collapsed is not None
    assert collapsed["reason"] == "collapsed_earlier_occurrences"
    assert collapsed["skipped_occurrences"] == 2
    assert collapsed["skipped_first"] == "2026-10-10T08:30:00+00:00"
    assert collapsed["skipped_last"] == "2026-10-11T08:30:00+00:00"
    assert cycle_calls == [records[2].run_id]


def test_process_job_routes_kind_review_to_the_review_flow(
    tmp_path: Path, history: Path
) -> None:
    job = _review_job(tmp_path)

    summary, failed = tick._process_job(
        job,
        history,
        client=GuardClient(),  # type: ignore[arg-type]
        now=NOW,
    )

    # First sight is `_process_review_job`'s own answer; no other kind registers a review job.
    assert (summary, failed) == (f"{job.name}: registered", False)
    assert _states(history, job) == ["registered"]


# -- review cycle and per-target dispatch ----------------------------------------------


@dataclass(slots=True)
class ReviewEnv:
    job: Job
    gh: ReviewGh
    client: ReviewHerdrClient
    history: Path
    checkout: Path

    def run(self) -> tuple[str, bool]:
        return tick._process_review_job(
            self.job,
            self.history,
            client=self.client,  # type: ignore[arg-type]
            now=NOW,
        )

    @property
    def worktree_root(self) -> Path:
        return self.checkout / ".worktrees"

    def target_records(self) -> list[HistoryRecord]:
        return [
            r
            for r in read_job(self.history, self.job.name)
            if r.extra and "target_key" in r.extra
        ]

    def run_id(self) -> str:
        running = [
            r for r in read_job(self.history, self.job.name) if r.state == "running"
        ]
        assert running[-1].run_id is not None
        return running[-1].run_id


def _review_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prs: tuple[Any, ...],
    client: Callable[[ReviewGh], ReviewHerdrClient] | None = None,
    head_branch: str = "feature-x",
    **job_over: Any,
) -> ReviewEnv:
    """A due review job over a real `acme/widgets` clone, with gh and herdr faked.
    The default herdr fake plays a worker that posts its review."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    base = tmp_path / "repos"
    origin, checkout = _setup_repo(tmp_path, "acme", "widgets", head_branch, base)
    job = _review_job(tmp_path, repo=base, **job_over)
    gh = ReviewGh(
        prs=prs,
        repo_infos={
            ("acme", "widgets"): {"clone_url": str(origin), "default_branch": "main"}
        },
    )
    monkeypatch.setattr(tick, "RealGhClient", lambda: gh)
    history = tmp_path / "history.jsonl"
    append(history, HistoryRecord(ts=REGISTERED, job=job.name, state="registered"))
    return ReviewEnv(
        job=job,
        gh=gh,
        client=client(gh) if client else ReviewHerdrClient(on_prompt=_mark_posted(gh)),
        history=history,
        checkout=checkout,
    )


def _raises(exc: Exception) -> Callable[..., Any]:
    def _raise(*_a: Any, **_kw: Any) -> Any:
        raise exc

    return _raise


def test_review_flow_dispatches_the_oldest_target_and_reports_every_skip_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Oldest first: #1 is at its attempt budget, #2 already carries a trusted review
    # marker, #3 is dispatched, #4 is over the one-worker cap.
    env = _review_env(
        tmp_path,
        monkeypatch,
        prs=tuple(_pr(n, created_at=f"2026-10-0{n}T00:00:00Z") for n in (1, 2, 3, 4)),
        max_workers_per_tick=1,
    )
    env.gh.posted.add("acme/widgets#2")
    append(
        env.history,
        HistoryRecord(
            ts=REGISTERED,
            job=env.job.name,
            state="done",
            run_id="review-my-prs-20261011T083000Z",
            extra={"target_key": "acme/widgets#1"},
        ),
    )

    summary, failed = env.run()

    assert failed is False
    assert summary == (
        "review-my-prs: done (enumerated=4, candidates=4, dispatched=1, "
        "skipped=3, failed=0, repos_failed=0)"
    )
    run_id = env.run_id()
    target_done = [r for r in env.target_records() if r.run_id == run_id]
    assert len(target_done) == 1
    record = target_done[0]
    assert record.state == "done"
    assert record.extra is not None
    h6 = review_target_hash(env.job.name, "acme/widgets#3")
    assert record.extra["target_key"] == "acme/widgets#3"
    assert record.extra["pr_number"] == 3
    assert record.extra["select_mode"] == "author"
    assert record.extra["headRefName"] == "feature-x"
    assert record.extra["attempt"] == 0
    assert record.extra["verified"] is True
    assert record.extra["agent"] == build_review_agent_name(
        env.job.name, "acme/widgets#3", run_id
    )
    assert record.extra["pane_id"] == "w1:p1"
    assert record.extra["report_path"] == str(
        tick.default_reports_dir() / f"auto-review-{run_id}-{h6}.md"
    )
    aggregate = read_job(env.history, env.job.name)[-1]
    assert aggregate.state == "done"
    assert aggregate.extra is not None
    assert aggregate.extra["gate"] == "passed"
    assert aggregate.extra["target"] == "pr"
    assert (
        aggregate.extra["enumerated"],
        aggregate.extra["candidates"],
        aggregate.extra["dispatched"],
        aggregate.extra["skipped"],
        aggregate.extra["failed"],
        aggregate.extra["repos_failed"],
    ) == (4, 4, 1, 3, 0, 0)
    report = (tick.default_reports_dir() / f"review-{run_id}.md").read_text()
    for reason in ("max_attempts_exceeded", "review_already_posted", "over_cap"):
        assert reason in report
    assert env.client.started == [record.extra["agent"]]
    assert env.client.notifications == [
        ("herdr-routines: review-my-prs done", None, "done")
    ]


def test_review_flow_fails_closed_when_gh_has_no_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _review_env(tmp_path, monkeypatch, prs=(_pr(1),))
    monkeypatch.setattr(env.gh, "api_user", _raises(RuntimeError("gh: not logged in")))

    summary, failed = env.run()

    assert (summary, failed) == ("review-my-prs: failed (gh_auth_missing)", True)
    record = read_job(env.history, env.job.name)[-1]
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["reason"] == "gh_auth_missing"
    assert record.extra["error"] == "gh: not logged in"
    assert record.extra["gate"] == "failed"
    assert record.extra["dispatched"] == 0
    assert env.gh.search_calls == []
    assert env.client.notifications == [
        ("herdr-routines: review-my-prs failed", "gh_auth_missing", "request")
    ]


def test_review_flow_isolates_a_repo_whose_info_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `acme/gadgets` has no repo_info entry, so gh answers None for it.
    env = _review_env(
        tmp_path,
        monkeypatch,
        prs=(
            _pr(1, repo="acme/gadgets", created_at="2026-10-01T00:00:00Z"),
            _pr(2, created_at="2026-10-02T00:00:00Z"),
        ),
    )

    summary, failed = env.run()

    assert failed is True
    assert summary == (
        "review-my-prs: done (enumerated=2, candidates=2, dispatched=1, "
        "skipped=0, failed=1, repos_failed=1)"
    )
    # Only the readable repo's target got a per-target record.
    assert [r.extra["target_key"] for r in env.target_records() if r.extra] == [
        "acme/widgets#2"
    ]
    aggregate = read_job(env.history, env.job.name)[-1]
    assert aggregate.state == "failed"
    assert aggregate.extra is not None
    assert aggregate.extra["gate"] == "failed"
    assert aggregate.extra["repos_failed"] == 1
    report = (tick.default_reports_dir() / f"review-{env.run_id()}.md").read_text()
    assert "repo_info_failed" in report
    assert env.client.notifications == [
        (
            "herdr-routines: review-my-prs failed",
            "1 dispatched, 1 failed, 0 skipped",
            "request",
        )
    ]


def test_review_target_with_a_live_agent_is_skipped_not_redispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = build_review_agent_prefix("review-my-prs", "acme/widgets#1") + "-earlier"
    env = _review_env(
        tmp_path,
        monkeypatch,
        prs=(_pr(1),),
        client=lambda gh: ReviewHerdrClient(statuses={live: "working"}),
    )

    summary, failed = env.run()

    assert failed is False
    assert "dispatched=0, skipped=1, failed=0" in summary
    (record,) = env.target_records()
    assert record.state == "skipped"
    assert record.extra is not None
    assert record.extra["reason"] == "agent_live"
    assert record.extra["attempt"] == 0
    assert record.extra["pane_id"] is None
    assert record.extra["verified"] is None
    assert env.client.tabs == []
    assert env.client.started == []


def test_review_target_whose_head_ref_cannot_be_fetched_is_worktree_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _review_env(tmp_path, monkeypatch, prs=(_pr(1, head_ref="gone-branch"),))

    _summary, failed = env.run()

    assert failed is True
    (record,) = env.target_records()
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["reason"] == "worktree_failed"
    assert str(record.extra["error"]).startswith("git fetch failed")
    assert record.extra["verified"] is False
    assert record.extra["agent"] is None
    assert env.client.tabs == []
    assert not (env.worktree_root / "review-pr1").exists()


def test_review_target_whose_worktree_cannot_be_added_is_worktree_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _review_env(tmp_path, monkeypatch, prs=(_pr(1),))
    # A regular file where the `.worktrees` directory belongs: the fetch succeeds,
    # `git worktree add` cannot create its target.
    env.worktree_root.write_text("not a directory\n")

    _summary, failed = env.run()

    assert failed is True
    (record,) = env.target_records()
    assert record.extra is not None
    assert record.extra["reason"] == "worktree_failed"
    assert str(record.extra["error"]).startswith("git worktree add failed")
    assert env.client.tabs == []


class _TabCreateFails(ReviewHerdrClient):
    def tab_create(self, *, cwd: Any, label: Any = None) -> str:
        raise HerdrCliError("tab_create refused", exit_code=1)


class _AgentStartFails(ReviewHerdrClient):
    def agent_start(self, **_kw: Any) -> None:
        raise HerdrCliError("agent_start refused", exit_code=1)


@pytest.mark.parametrize(
    ("client_cls", "pane_id", "error"),
    [
        (_TabCreateFails, None, "tab_create refused"),
        (_AgentStartFails, "w1:p1", "agent_start refused"),
    ],
)
def test_review_agent_that_cannot_be_started_fails_and_closes_its_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client_cls: type[ReviewHerdrClient],
    pane_id: str | None,
    error: str,
) -> None:
    env = _review_env(
        tmp_path,
        monkeypatch,
        prs=(_pr(1),),
        client=lambda gh: client_cls(on_prompt=_mark_posted(gh)),
    )

    _summary, failed = env.run()

    assert failed is True
    (record,) = env.target_records()
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["reason"] == "agent_start_failed"
    assert error in str(record.extra["error"])
    assert record.extra["pane_id"] == pane_id
    assert record.extra["verified"] is False
    assert env.client.closed == ([pane_id] if pane_id else [])
    assert env.client.prompt_texts == []
    assert not (env.worktree_root / "review-pr1").exists()


def test_review_agent_that_never_gets_ready_is_recorded_as_agent_start_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The audit flow records `agent_not_interactive` for the same situation.
    monkeypatch.setattr(runner, "READY_POLL_INTERVAL_S", 0.0)
    env = _review_env(
        tmp_path,
        monkeypatch,
        prs=(_pr(1),),
        client=lambda gh: ReviewHerdrClient(ready=False),
        start_timeout_ms=10,
    )

    _summary, failed = env.run()

    assert failed is True
    (record,) = env.target_records()
    assert record.extra is not None
    assert record.extra["reason"] == "agent_start_failed"
    assert record.extra["pane_id"] == "w1:p1"
    assert env.client.closed == ["w1:p1"]
    assert env.client.prompt_texts == []
    assert not (env.worktree_root / "review-pr1").exists()


def test_review_agent_whose_prompt_fails_is_recorded_as_agent_prompt_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "PROMPT_RETRY_DELAYS_S", ())
    env = _review_env(
        tmp_path,
        monkeypatch,
        prs=(_pr(1),),
        client=lambda gh: ReviewHerdrClient(
            on_prompt=_raises(RuntimeError("agent crashed"))
        ),
    )

    _summary, failed = env.run()

    assert failed is True
    (record,) = env.target_records()
    assert record.state == "failed"
    assert record.extra is not None
    assert record.extra["reason"] == "agent_prompt_failed"
    assert "agent crashed" in str(record.extra["error"])
    assert record.extra["pane_id"] == "w1:p1"
    assert env.client.closed == ["w1:p1"]
    assert not (env.worktree_root / "review-pr1").exists()


def test_review_job_prompt_replaces_the_engine_prompt_with_placeholders_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _review_env(
        tmp_path,
        monkeypatch,
        prs=(_pr(1),),
        prompt="Review $ROUTINE_JOB run $ROUTINE_RUN_ID, write $ROUTINE_REPORT",
    )

    env.run()

    run_id = env.run_id()
    report = tick.default_reports_dir() / (
        f"auto-review-{run_id}-{review_target_hash(env.job.name, 'acme/widgets#1')}.md"
    )
    assert env.client.prompt_texts == [
        f"Review review-my-prs run {run_id}, write {report}"
    ]


# -- audit run and fix dispatch ---------------------------------------------------------

AUDIT_RUN_ID = "audit-type-health-20261012T083000Z"
AUDIT_JOB = "audit-type-health"
AUDIT_AGENT = build_audit_agent_name(AUDIT_JOB, AUDIT_RUN_ID)
FIX_AGENT = build_gate_worker_agent_name(AUDIT_JOB, AUDIT_RUN_ID)


@pytest.fixture
def state_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state))
    return state


@pytest.fixture
def audit_repo(
    tmp_path: Path, state_env: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A real one-commit git repo on `main`. `ensure_repo` is stubbed (there is no
    remote); the tests that need it to fail replace the stub."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "a.php").write_text("<?php\n")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
    )
    monkeypatch.setattr(tick, "ensure_repo", lambda job: job.repo)
    return path


def _due_audit_job(repo: Path, history: Path, **over: Any) -> Job:
    job = _audit_job(repo, **{"cron": "30 8 * * *", **over})
    append(history, HistoryRecord(ts=REGISTERED, job=job.name, state="registered"))
    return job


def _run_audit_job(job: Job, history: Path, client: _FakeHerdr) -> tuple[str, bool]:
    return tick._process_audit_job(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
    )


def _auditor(
    findings: list[dict[str, str]], *, fix_status: str = "idle"
) -> Callable[[Any], str]:
    """The audit agent writes its manifest; the fix worker settles with `fix_status`."""

    def on_prompt(agent: Any) -> str:
        if agent.name != AUDIT_AGENT:
            return fix_status
        manifest = tick.default_reports_dir() / f"{AUDIT_RUN_ID}-findings.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"version": 1, "check": "type-health", "findings": findings})
        )
        return "idle"

    return on_prompt


def _last_audit(history: Path) -> tuple[HistoryRecord, Mapping[str, Any]]:
    record = read_job(history, AUDIT_JOB)[-1]
    return record, record.extra or {}


def _worktree(repo: Path, prefix: str) -> Path:
    return repo / ".worktrees" / f"{prefix}-{AUDIT_RUN_ID}"


class _NotReady(_FakeHerdr):
    """Every agent starts but never reports interactive."""

    def agent_interactive_ready(self, target: str) -> bool:
        return False


class _FixNotReady(_FakeHerdr):
    """Only the fix worker never reports interactive."""

    def agent_interactive_ready(self, target: str) -> bool:
        return target != FIX_AGENT


@pytest.fixture
def fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the ready-poll sleep and the prompt retry backoff."""
    monkeypatch.setattr(runner, "READY_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(runner, "PROMPT_RETRY_DELAYS_S", ())


def test_audit_flow_runs_the_audit_then_one_fix_worker_in_their_own_worktrees(
    audit_repo: Path, history: Path
) -> None:
    job = _due_audit_job(audit_repo, history)
    client = _FakeHerdr(_auditor([_finding(1), _finding(2)]))

    summary, failed = _run_audit_job(job, history, client)

    assert (summary, failed) == ("audit-type-health: done (fix_dispatched)", False)
    records = read_job(history, AUDIT_JOB)
    assert [r.state for r in records] == ["registered", "running", "done"]
    assert records[1].run_id == AUDIT_RUN_ID
    assert records[1].extra is not None
    assert records[1].extra["scheduled_for"] == "2026-10-12T08:30:00+00:00"
    extra = records[2].extra
    assert extra is not None
    branch = tick.build_branch_name(AUDIT_JOB, AUDIT_RUN_ID)
    assert extra["gate"] == "fix_dispatched"
    assert extra["target"] == "base"
    assert (extra["findings_total"], extra["new"], extra["dispatched"]) == (2, 2, 2)
    assert isinstance(ids := extra["dispatched_ids"], list)
    assert len(ids) == 2
    assert extra["branch"] == extra["gate_branch"] == branch
    assert extra["agent_name"] == FIX_AGENT
    assert extra["final_agent_status"] == "idle"
    assert extra["session_id"] == f"ses-{FIX_AGENT}"
    assert extra["report_path"] == str(
        tick.default_reports_dir() / f"{AUDIT_RUN_ID}.md"
    )
    # The audit and the fix worker each ran in a worktree of their own, both gone now.
    audit, fix = client.agents
    assert (audit.name, audit.cwd) == (AUDIT_AGENT, _worktree(audit_repo, "audit"))
    assert (fix.name, fix.cwd) == (FIX_AGENT, _worktree(audit_repo, "audit-fix"))
    assert not _worktree(audit_repo, "audit").exists()
    assert not _worktree(audit_repo, "audit-fix").exists()
    # The worker settled, so its findings are handed off, not owed another dispatch.
    ledger = load_ledger(ledger_path(AUDIT_JOB))
    assert ledger is not None
    assert {(e.attempts, e.queued) for e in ledger.entries.values()} == {(1, False)}
    assert client.notifications == ["herdr-routines: audit-type-health fix_dispatched"]


@pytest.mark.parametrize(
    ("has_git_dir", "reason"),
    [(False, "clone_failed"), (True, "repo_sync_failed")],
)
def test_audit_repo_that_cannot_be_prepared_fails_before_any_agent_starts(
    tmp_path: Path,
    state_env: Path,
    history: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_git_dir: bool,
    reason: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    if has_git_dir:
        (repo / ".git").mkdir()
    monkeypatch.setattr(tick, "ensure_repo", _raises(RuntimeError("sync exploded")))
    job = _due_audit_job(repo, history)
    client = _FakeHerdr()

    summary, failed = _run_audit_job(job, history, client)

    assert (summary, failed) == (f"audit-type-health: failed ({reason})", True)
    record, extra = _last_audit(history)
    assert record.state == "failed"
    assert extra["reason"] == reason
    assert extra["error"] == "sync exploded"
    assert extra["gate"] == "failed"
    assert extra["target"] == "base"
    assert extra["report_written"] is False
    assert extra["report_path"] == str(
        tick.default_reports_dir() / f"{AUDIT_RUN_ID}.md"
    )
    assert client.agents == []
    assert client.notifications == ["herdr-routines: audit-type-health failed"]


def test_audit_whose_base_cannot_be_checked_out_fails_with_worktree_creation_failed(
    audit_repo: Path, history: Path
) -> None:
    job = _due_audit_job(audit_repo, history, base="no-such-ref")
    client = _FakeHerdr()

    summary, failed = _run_audit_job(job, history, client)

    assert (summary, failed) == (
        "audit-type-health: failed (worktree_creation_failed)",
        True,
    )
    _record, extra = _last_audit(history)
    assert extra["reason"] == "worktree_creation_failed"
    assert "git worktree add failed" in extra["error"]
    assert client.agents == []


@pytest.mark.parametrize(
    ("client_factory", "reason", "final_status", "error"),
    [
        pytest.param(
            lambda: _FakeHerdr(fail_start=lambda name: True),
            "agent_start_failed",
            None,
            "could not start",
            id="start_raises",
        ),
        pytest.param(
            lambda: _NotReady(),
            "agent_not_interactive",
            None,
            None,
            id="never_interactive",
        ),
        pytest.param(
            lambda: _FakeHerdr(_raises(RuntimeError("agent crashed"))),
            "agent_prompt_failed",
            None,
            "agent crashed",
            id="prompt_raises",
        ),
        pytest.param(
            lambda: _FakeHerdr(lambda agent: "blocked"),
            "agent_prompt_failed",
            "blocked",
            None,
            id="settles_blocked",
        ),
    ],
)
def test_audit_agent_that_fails_ends_the_run_without_a_fix_worker(
    audit_repo: Path,
    history: Path,
    fast_waits: None,
    client_factory: Callable[[], _FakeHerdr],
    reason: str,
    final_status: str | None,
    error: str | None,
) -> None:
    job = _due_audit_job(audit_repo, history, start_timeout_ms=10)
    client = client_factory()

    summary, failed = _run_audit_job(job, history, client)

    assert (summary, failed) == (f"audit-type-health: failed ({reason})", True)
    record, extra = _last_audit(history)
    assert record.state == "failed"
    assert extra["reason"] == reason
    assert extra["phase"] == "audit"
    assert extra["agent_name"] == AUDIT_AGENT
    assert extra["pane_id"] == "w1:p1"
    assert extra["final_agent_status"] == final_status
    if error is None:
        assert extra["error"] is None
    else:
        assert error in extra["error"]
    assert extra["report_written"] is False
    assert FIX_AGENT not in [a.name for a in client.agents]
    assert not _worktree(audit_repo, "audit").exists()
    assert client.notifications == ["herdr-routines: audit-type-health failed"]


def test_audit_fix_that_cannot_get_a_worktree_still_spends_the_attempt(
    audit_repo: Path, history: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_add = tick._add_worktree

    def add_worktree(repo: Path, wt_path: Path, base: str) -> None:
        if wt_path.name.startswith("audit-fix-"):
            raise RuntimeError("git worktree add failed: simulated")
        real_add(repo, wt_path, base)

    monkeypatch.setattr(tick, "_add_worktree", add_worktree)
    job = _due_audit_job(audit_repo, history)
    client = _FakeHerdr(_auditor([_finding(1)]))

    summary, failed = _run_audit_job(job, history, client)

    assert (summary, failed) == (
        "audit-type-health: failed (worktree_creation_failed)",
        True,
    )
    _record, extra = _last_audit(history)
    assert extra["gate"] == "fix_dispatched"
    assert extra["reason"] == "worktree_creation_failed"
    assert "simulated" in extra["error"]
    assert extra["pane_id"] is None
    assert extra["fix_report_written"] is False
    (fid,) = extra["dispatched_ids"]
    # The ledger is written before the dispatch, so the finding is spent an attempt and
    # stays queued for the next cycle.
    ledger = load_ledger(ledger_path(AUDIT_JOB))
    assert ledger is not None
    assert ledger.entries[fid].attempts == 1
    assert ledger.entries[fid].queued
    assert [a.name for a in client.agents] == [AUDIT_AGENT]


@pytest.mark.parametrize(
    ("client_factory", "reason", "final_status"),
    [
        pytest.param(
            lambda: _FixNotReady(_auditor([_finding(1)])),
            "agent_not_interactive",
            None,
            id="never_interactive",
        ),
        pytest.param(
            lambda: _FakeHerdr(_auditor([_finding(1)], fix_status="blocked")),
            "agent_prompt_failed",
            "blocked",
            id="settles_blocked",
        ),
    ],
)
def test_audit_fix_worker_that_does_not_settle_keeps_its_findings_queued(
    audit_repo: Path,
    history: Path,
    fast_waits: None,
    client_factory: Callable[[], _FakeHerdr],
    reason: str,
    final_status: str | None,
) -> None:
    job = _due_audit_job(audit_repo, history, start_timeout_ms=10)
    client = client_factory()

    summary, failed = _run_audit_job(job, history, client)

    assert (summary, failed) == (f"audit-type-health: failed ({reason})", True)
    record, extra = _last_audit(history)
    assert record.state == "failed"
    assert extra["gate"] == "fix_dispatched"
    assert extra["reason"] == reason
    assert extra["agent_name"] == FIX_AGENT
    assert extra["pane_id"] == "w1:p2"
    assert extra["final_agent_status"] == final_status
    assert extra["fix_report_written"] is False
    (fid,) = extra["dispatched_ids"]
    ledger = load_ledger(ledger_path(AUDIT_JOB))
    assert ledger is not None
    assert (ledger.entries[fid].attempts, ledger.entries[fid].queued) == (1, True)
    assert not _worktree(audit_repo, "audit-fix").exists()
    assert client.notifications == ["herdr-routines: audit-type-health failed"]
