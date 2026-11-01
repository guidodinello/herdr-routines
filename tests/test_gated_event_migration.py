"""Acceptance tests for issue 077: move `gated` jobs' `github_event` checks onto
`trigger.event`.

Spec: `docs/pipeline/runs/20261101T050000Z/spec.md` (v2). The two tests named by
the spec's `## Acceptance criteria` that issue 014's suite (`tests/test_github_event.py`)
does not already pin:

- `test_gated_event_check_behaviour_unchanged_after_migration` (AC 1) is the golden pin
  (issue 076's pattern): committed on the unmodified code and verified passing before any
  src change. It drives the user-facing surface — `load_config` on a jobs.d/ directory
  plus `run_tick` — through the full external behaviour of a gated job whose checks are
  one `github_event`: poll → dedup against the watermark → dispatch, over-cap deferral,
  the `max_attempts_exceeded` skip with its notification, head-ref skip vs retry,
  fail-closed poll errors, `issues:opened` first-poll baseline adoption (and an existing
  watermark preventing a spurious re-baseline), the "event clears and comes back fires
  again" watermark rule, plus the history-record shapes the consumers read
  (`gate`/`reason`/`run_id`, `last_seen_event_ids`, budget extras). Where the two
  mechanisms differ internally (cron-gated cadence vs every-tick cadence, the cron vs
  event trigger stamps, 075's quiet-poll record) the test asserts only what survives
  both: which events fire, which events skip and why, the notify hook, and the shapes
  `_last_seen_event_ids` / `attempt_count_for_event` / status+notify consumers read.
  After the migration the same test passes unmodified.

- `test_single_event_watermark_per_job` (AC 2): exactly one watermark writer per job —
  a job declaring `trigger.event` directly and one sugar-loaded from a gated
  `github_event` check each write `last_seen_event_ids` through the trigger poll alone,
  and a job spelling both cannot coexist at load (the dual-writer conflict is gone
  because the dual writer is gone, not because a validation error masks it: the sugar
  form loads clean and still owns one poll). Fails until the sugar lands.

Behaviour/record helpers come from `tests/test_github_event.py` so both mechanisms are
pinned against identical fakes and read seams.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import tick
from herdr_routines.auto_fix import GithubEvent
from herdr_routines.config import load_config
from herdr_routines.history import HistoryRecord, append, read_job
from herdr_routines.runner import RunOutcome
from herdr_routines.tick import _last_seen_event_ids, run_tick
from tests.test_github_event import (
    EVENT_LABEL,
    READ_CALLS,
    EventGhClient,
    MockSubprocess,
    RecordingHerdrClient,
    _base_calls,
    _dispatched_numbers,
    _init_repo,
    _RecordingGitSubprocess,
)

T0 = datetime(2026, 11, 1, 5, 0, 0, tzinfo=UTC)


def _pr_event_id(number: int) -> str:
    return f"evt-pr-{number}-{EVENT_LABEL}"


def _issue_event_id(number: int) -> str:
    return f"evt-issue-{number}"


class GoldenGh(EventGhClient):
    """EventGhClient whose `pr_list`/`pr_view` serve the scripted PRs in `prs` (the
    dict maps the number to per-PR overrides, e.g. a head ref the poll must reject)
    and whose `issues:opened` search serves `issues`; `fail` makes the next poll
    raise, as an auth/rate-limit error would."""

    def __init__(self) -> None:
        super().__init__(user="testuser", events=True)
        self.prs: dict[int, dict[str, Any]] = {}
        self.issues: list[int] = []
        self.fail = False

    def _maybe_fail(self, name: str) -> None:
        if self.fail:
            raise RuntimeError(f"gh api {name} failed: rate limit exceeded")

    def _pr_doc(self, number: int) -> dict[str, object]:
        doc: dict[str, object] = {
            "number": number,
            "title": f"PR {number}",
            "headRefName": f"feature-{number}",
            "author": {"login": self.user},
            "url": f"https://github.com/o/r/pull/{number}",
            "labels": [{"name": EVENT_LABEL}],
            "isCrossRepository": False,
        }
        doc.update(self.prs[number])
        return doc

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        self.calls.append("pr_list")
        self._maybe_fail("pr_list")
        return [self._pr_doc(number) for number in sorted(self.prs)]

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append(f"pr_view:{number}")
        if number not in self.prs:
            return {}
        doc = self._pr_doc(number)
        doc["statusCheckRollup"] = [{"name": "ci", "state": "SUCCESS"}]
        return doc

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self.calls.append("graphql")
        self._maybe_fail("graphql")
        nodes = [
            {
                "id": f"I_{n}",
                "number": n,
                "title": f"Issue {n}",
                "author": {"login": self.user},
                "labels": [],
            }
            for n in self.issues
        ]
        return {"data": {"search": {"nodes": nodes}}}


def _gated_event_yaml(
    name: str,
    repo: Path,
    query: str,
    *,
    cron: str = '"* * * * *"',
    match: str = "{}",
    max_attempts: int = 3,
    extra: str = "",
) -> str:
    return f"""\
name: {name}
cron: {cron}
repo: {repo}
kind: gated
max_workers_per_tick: 1
max_attempts_per_target: {max_attempts}
checks:
  - github_event:
      query: {query}
      repo: owner/repo
      match: {match}
      timeout_ms: 60000
{extra}"""


def _golden_jobs_d(tmp_path: Path, repo: Path) -> Path:
    """Three sugar-surface jobs sharing one history: the `prs:labels` fix workflow that
    every dispatch/skip/fail scenario runs through, and two `issues:opened` jobs for the
    baseline rules — one untouched (adopts the baseline) and one whose history already
    carries a watermark a poll must not replay the baseline onto."""
    jobs_d = tmp_path / "jobs.d"
    jobs_d.mkdir()
    (jobs_d / "gate-fix.yaml").write_text(
        _gated_event_yaml(
            "gate-fix", repo, "prs:labels", match=f"{{label: {EVENT_LABEL}}}"
        )
    )
    (jobs_d / "issues-fix.yaml").write_text(
        _gated_event_yaml("issues-fix", repo, "issues:opened")
    )
    (jobs_d / "issues-keep.yaml").write_text(
        _gated_event_yaml("issues-keep", repo, "issues:opened")
    )
    return jobs_d


# -- record reading helpers ------------------------------------------------------


def _records_with_event(
    history_path: Path, name: str, event_id: str
) -> list[HistoryRecord]:
    return [
        r
        for r in read_job(history_path, name)
        if r.extra and r.extra.get("event_id") == event_id
    ]


def _dispatch_records(
    history_path: Path, name: str, event_id: str
) -> list[HistoryRecord]:
    return [
        r
        for r in _records_with_event(history_path, name, event_id)
        if r.extra and "fix_worker_agent" in r.extra
    ]


def _records_with_reason(
    history_path: Path, name: str, reason: str
) -> list[HistoryRecord]:
    return [
        r
        for r in read_job(history_path, name)
        if r.extra and r.extra.get("reason") == reason
    ]


def _baseline_records(history_path: Path, name: str) -> list[HistoryRecord]:
    return [
        r
        for r in read_job(history_path, name)
        if r.extra and r.extra.get("gate") == "baseline"
    ]


# -- AC 1: the golden pin ---------------------------------------------------------


def test_gated_event_check_behaviour_unchanged_after_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1 (issue 077): the gated `github_event` behaviour and its record shapes
    move onto the trigger path unchanged. Committed on the unmodified code and
    verified passing; after the migration the same test must pass unmodified."""
    repo = _init_repo(tmp_path)
    jobs_d = _golden_jobs_d(tmp_path, repo)
    config = load_config(jobs_d)
    assert [job.name for job in config.jobs] == [
        "gate-fix",
        "issues-fix",
        "issues-keep",
    ]

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    gh = GoldenGh()
    client = RecordingHerdrClient()
    monkeypatch.setattr("herdr_routines.tick.RealGhClient", lambda: gh)
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    git = _RecordingGitSubprocess()
    monkeypatch.setattr("herdr_routines.tick.subprocess", git)

    def tick(minute: int) -> None:
        run_tick(
            config,
            history_path,
            client=client,  # type: ignore[arg-type]
            now=T0 + timedelta(minutes=minute),
        )

    tick(0)  # every job registers; nothing is polled or dispatched yet.
    for name in ("gate-fix", "issues-fix", "issues-keep"):
        assert [r.state for r in read_job(history_path, name)] == ["registered"]

    # `issues-keep` upgrades to the mechanism with the OLD path's watermark already in
    # history: the first poll must not replay the baseline onto it (no spurious adopt).
    append(
        history_path,
        HistoryRecord(
            ts=T0,
            job="issues-keep",
            state="done",
            run_id="seed-upgrade-watermark",
            extra={"last_seen_event_ids": [_issue_event_id(3)]},
        ),
    )

    # -- clean poll: read-only, no dispatch, 014-shaped gate records -----------------
    gh.issues = [3, 4]
    tick(1)
    tick(2)
    assert _dispatched_numbers(client) == [4], (
        "issues-fix must adopt the open issues as its baseline and issues-keep must "
        "fire only the new issue past the existing watermark; nothing else dispatches"
    )
    assert _base_calls(gh.calls) <= READ_CALLS, (
        f"clean tick must poll read-only, got {gh.calls}"
    )

    # issues-fix: first poll -> baseline adoption record, never a dispatch.
    [baseline] = _baseline_records(history_path, "issues-fix")
    baseline_extra = baseline.extra
    assert baseline_extra is not None
    assert baseline.state == "done"
    assert baseline.run_id, "the baseline record carries a run_id (014 shape)"
    assert baseline_extra["target"] == "issue"
    assert baseline_extra["query"] == "issues:opened"
    assert baseline_extra["last_seen_event_ids"] == [
        _issue_event_id(3),
        _issue_event_id(4),
    ]

    # issues-keep: an existing watermark prevents a spurious re-baseline...
    assert _baseline_records(history_path, "issues-keep") == []
    # ...and the one issue opened after it fires with the event's own record shape.
    [rec4] = _dispatch_records(history_path, "issues-keep", _issue_event_id(4))
    assert rec4.state == "done"
    rec4_extra = rec4.extra
    assert rec4_extra is not None
    assert rec4_extra["reason"] == "gate_failed"
    assert rec4_extra["attempt"] == 0
    assert rec4_extra["target"] == "issue"
    assert rec4_extra["fix_worker_agent"]
    assert rec4.run_id
    assert _last_seen_event_ids(history_path, "issues-keep") == [
        _issue_event_id(3),
        _issue_event_id(4),
    ], "the baseline event stays seen alongside the one that fired"

    # -- poll → dedup against the watermark → dispatch -------------------------------
    gh.prs = {7: {}}
    tick(3)
    assert _dispatched_numbers(client) == [4, 7]
    [rec7] = _dispatch_records(history_path, "gate-fix", _pr_event_id(7))
    rec7_extra = rec7.extra
    assert rec7_extra is not None
    assert rec7.state == "done"
    assert rec7_extra["reason"] == "gate_failed"
    assert rec7_extra["attempt"] == 0
    assert rec7_extra["event_number"] == 7
    assert rec7_extra["target"] == "pr"
    assert rec7_extra["fix_worker_agent"]
    assert rec7.run_id, "the per-event dispatch record carries a run_id (014 shape)"
    assert any("#7" in p for p in client.prompts), (
        "the worker prompt is seeded with the event payload"
    )
    watermark = _last_seen_event_ids(history_path, "gate-fix")
    assert watermark is not None and _pr_event_id(7) in watermark, (
        "a fired event ends up in the job's watermark"
    )

    # -- re-poll dedups against the watermark ----------------------------------------
    tick(4)
    tick(5)
    assert _dispatched_numbers(client) == [4, 7], (
        "re-polling the same event dispatches no more workers"
    )
    assert len(_dispatch_records(history_path, "gate-fix", _pr_event_id(7))) == 1
    latest = read_job(history_path, "gate-fix")[-1]
    latest_extra = latest.extra
    assert latest_extra is not None
    assert latest_extra["gate"] == "passed"
    assert latest.run_id
    assert latest_extra["query"] == "prs:labels"
    assert latest_extra["target"] == "pr"
    assert _pr_event_id(7) in (_last_seen_event_ids(history_path, "gate-fix") or [])

    # -- the event clears and comes back: the watermark catches it again --------------
    gh.prs = {}
    tick(6)
    assert _last_seen_event_ids(history_path, "gate-fix") == [], (
        "a cleared poll drops the event from the watermark"
    )
    gh.prs = {7: {}}
    tick(7)
    assert _dispatched_numbers(client) == [4, 7, 7], (
        "an event that clears and comes back fires again"
    )
    assert len(_dispatch_records(history_path, "gate-fix", _pr_event_id(7))) == 2

    # -- over-cap deferral: the second event waits a tick instead of dropping ----------
    gh.prs = {8: {}, 9: {}}
    tick(8)
    tick(9)
    numbers = _dispatched_numbers(client)
    assert numbers.count(8) == 1 and numbers.count(9) == 1, (
        f"both events fire exactly once, the second on a later tick: {numbers}"
    )
    assert _pr_event_id(9) in (_last_seen_event_ids(history_path, "gate-fix") or [])

    # -- per-event attempt budget: over-cap -> skipped + one notification ---------------
    gh.prs = {10: {}}
    for i in range(3):
        append(
            history_path,
            HistoryRecord(
                ts=T0 + timedelta(minutes=9),
                job="gate-fix",
                state="done",
                run_id=f"seed-budget-{i}",
                extra={
                    "event_id": _pr_event_id(10),
                    "pr_number": 10,
                    "reason": "gate_failed",
                    "attempt": i,
                },
            ),
        )
    tick(10)
    assert _dispatched_numbers(client) == numbers, (
        "the burnt event must not dispatch a worker"
    )
    [skip10] = _records_with_reason(history_path, "gate-fix", "max_attempts_exceeded")
    skip10_extra = skip10.extra
    assert skip10_extra is not None
    assert skip10.state == "skipped"
    assert skip10_extra["event_id"] == _pr_event_id(10)
    assert skip10_extra["attempt"] == 3, "one phase above the last real attempt"
    assert skip10.run_id
    assert any("skipped" in title.lower() for title, _, _ in client.notifications), (
        "budget exhaustion must notify"
    )
    assert _pr_event_id(10) in (_last_seen_event_ids(history_path, "gate-fix") or []), (
        "the exhausted event is marked seen"
    )
    tick(11)
    assert (
        len(_records_with_reason(history_path, "gate-fix", "max_attempts_exceeded"))
        == 1
    ), "the skipped event is not retried"
    assert _dispatched_numbers(client) == numbers

    # -- fail-closed poll errors --------------------------------------------------------
    gh.prs = {14: {}}
    gh.fail = True
    watermark_before = _last_seen_event_ids(history_path, "gate-fix")
    assert watermark_before is not None
    tick(12)
    [fail_rec] = _records_with_reason(
        history_path, "gate-fix", "github_event_poll_failed"
    )
    fail_extra = fail_rec.extra
    assert fail_extra is not None
    assert fail_rec.state == "failed"
    assert fail_extra["gate"] == "failed"
    assert fail_extra["target"] == "pr"
    assert fail_extra["query"] == "prs:labels"
    assert any(
        "failed" in title.lower() or "error" in title.lower()
        for title, _, _ in client.notifications
    ), "the gate error must be surfaced via notify"
    assert _last_seen_event_ids(history_path, "gate-fix") == watermark_before, (
        "a failed poll must carry no watermark"
    )
    gh.fail = False
    tick(13)
    assert _dispatched_numbers(client).count(14) == 1, (
        "the failed poll must never consume the event"
    )

    # -- head-ref validation: skip (no worktree) vs a retried failure --------------------
    adds_before = len(git.worktree_adds())
    gh.prs = {13: {"headRefName": "-b"}}
    tick(14)
    [rec13] = _records_with_reason(history_path, "gate-fix", "invalid_head_ref")
    rec13_extra = rec13.extra
    assert rec13_extra is not None
    assert rec13.state == "skipped"
    assert rec13_extra["event_id"] == _pr_event_id(13)
    assert rec13_extra["head_ref"] == "-b"
    assert rec13.run_id
    assert len(git.worktree_adds()) == adds_before, (
        "an invalid ref never reaches git worktree add"
    )
    tick(15)
    assert (
        len(_records_with_reason(history_path, "gate-fix", "invalid_head_ref")) == 1
    ), "the invalid ref is skipped once, then seen"

    class _BrokenCheckRef(_RecordingGitSubprocess):
        def run(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
            if argv[:2] == ["git", "check-ref-format"]:
                raise OSError("git: not found")
            return super().run(argv, *args, **kwargs)

    monkeypatch.setattr("herdr_routines.tick.subprocess", _BrokenCheckRef())
    gh.prs = {15: {}}
    tick(16)
    tick(17)
    tick(18)
    tick(19)
    failed15 = [
        r
        for r in read_job(history_path, "gate-fix")
        if r.extra
        and r.extra.get("reason") == "head_ref_check_failed"
        and r.extra.get("event_id") == _pr_event_id(15)
    ]
    assert [r.extra["attempt"] for r in failed15 if r.extra is not None] == [0, 1, 2], (
        "the check that cannot run is a retried failure until the budget taps"
    )
    assert all(r.state == "failed" and r.run_id for r in failed15)
    assert all(
        r.extra is not None and "fix_worker_agent" not in r.extra for r in failed15
    )
    [skip15] = [
        r
        for r in _records_with_reason(history_path, "gate-fix", "max_attempts_exceeded")
        if r.extra and r.extra.get("event_id") == _pr_event_id(15)
    ]
    assert skip15.extra is not None and skip15.extra["attempt"] == 3


# -- AC 2: exactly one watermark writer per job -----------------------------------


def test_single_event_watermark_per_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2 (issue 077): `last_seen_event_ids` has exactly one writer per job — the
    trigger poll, whether the job declared `trigger.event` directly or the sugar
    moved a gated `github_event` check onto it — and a job spelling both cannot
    coexist at load, so two loads/writers cannot coexist for the same job."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    monkeypatch.setattr(
        "herdr_routines.tick.RealGhClient", lambda: EventGhClient(events=False)
    )
    monkeypatch.setattr("herdr_routines.tick.ensure_repo", lambda job: job.repo)
    monkeypatch.setattr("herdr_routines.tick.subprocess", MockSubprocess())

    run_ids: list[str] = []

    def fake_execute_run(job: Any, client: Any, *, run_id: str, **_: Any) -> RunOutcome:
        run_ids.append(run_id)
        return RunOutcome(state="done", run_id=run_id)

    monkeypatch.setattr(tick, "execute_run", fake_execute_run)
    client = RecordingHerdrClient()

    def run(config: Any, minute: int) -> None:
        tick.run_tick(
            config,
            history_path,
            client=client,  # type: ignore[arg-type]
            now=T0 + timedelta(minutes=minute),
        )

    def poll_returns(events: list[GithubEvent]) -> None:
        monkeypatch.setattr(
            "herdr_routines.tick.poll_github_event", lambda gh, **kw: list(events)
        )

    def event(event_id: str, number: int) -> GithubEvent:
        return GithubEvent(
            event_id=event_id,
            number=number,
            title=f"t {number}",
            head_ref=None,
            label="go",
            payload={},
        )

    # -- vanilla form: the job declares trigger.event directly (075 semantics) --------
    vanilla_d = tmp_path / "vanilla.d"
    vanilla_d.mkdir()
    (vanilla_d / "event-only.yaml").write_text(
        f"""\
name: event-only
trigger:
  event:
    github_event:
      query: prs:labels
      repo: owner/repo
      match: {{label: go}}
repo: {repo}
prompt: noop
"""
    )
    vanilla_cfg = load_config(vanilla_d)
    [vanilla_job] = vanilla_cfg.jobs
    assert vanilla_job.trigger_event is not None, "075 keeps its direct form"

    run(vanilla_cfg, 0)  # registration
    poll_returns([event("evt-1", 1)])
    run(vanilla_cfg, 1)
    assert len(run_ids) == 1, "the first new event fires once"
    assert _last_seen_event_ids(history_path, "event-only") == ["evt-1"]

    poll_returns([event("evt-1", 1)])
    run(vanilla_cfg, 2)
    assert len(run_ids) == 1, "re-polling the same event dispatches no more"
    # Exactly one writer: the dedup only holds because this job's one watermark is kept.
    assert _last_seen_event_ids(history_path, "event-only") == ["evt-1"]

    poll_returns([event("evt-1", 1), event("evt-2", 2)])
    run(vanilla_cfg, 3)
    assert len(run_ids) == 2, "the second event fires once too"

    # -- sugar form: a gated github_event check loads onto trigger.event ---------------
    sugar_d = tmp_path / "sugar.d"
    sugar_d.mkdir()
    (sugar_d / "sugar.yaml").write_text(
        _gated_event_yaml(
            "sugar",
            repo,
            "prs:labels",
            cron="0 0 1 1 *",
            match="{label: go}",
        )
    )
    sugar_cfg = load_config(sugar_d)
    [sugar_job] = sugar_cfg.jobs
    # `load_config` normalizes it: the job loads clean, and the check's shape moves
    # to `job.trigger_event` so the trigger poll (alone) owns the job's watermark.
    assert sugar_job.trigger_event is not None, (
        "the sugar form must load onto trigger.event, not be rejected at load"
    )
    assert sugar_job.trigger_event.query == "prs:labels"
    assert sugar_job.trigger_event.repo == "owner/repo"
    assert sugar_job.trigger_event.match == {"label": "go"}

    run(sugar_cfg, 0)
    poll_returns([event("evt-sugar-1", 1)])
    run(sugar_cfg, 1)
    assert _last_seen_event_ids(history_path, "sugar") == ["evt-sugar-1"], (
        "the sugar job's own poll must write the job's watermark through the single "
        "trigger path (it was a None-sugar no-op before the migration)"
    )
    poll_returns([event("evt-sugar-1", 1)])
    run(sugar_cfg, 2)
    assert _last_seen_event_ids(history_path, "sugar") == ["evt-sugar-1"], (
        "the dedup keeps the single watermark steady across polls"
    )

    # -- the dual form: a github_event check + trigger.event cannot coexist -------------
    dual_d = tmp_path / "dual.d"
    dual_d.mkdir()
    (dual_d / "dual.yaml").write_text(
        f"""\
name: dual
trigger:
  event:
    github_event:
      query: prs:labels
      repo: owner/repo
      match: {{label: go}}
repo: {repo}
kind: gated
checks:
  - github_event:
      query: prs:labels
      repo: owner/repo
      match: {{label: go}}
      timeout_ms: 60000
"""
    )
    dual_cfg = load_config(dual_d)
    assert dual_cfg.jobs == ()
    assert dual_cfg.errors, "declaring both must be a load error"
    assert any("github_event" in e and "trigger" in e for e in dual_cfg.errors), (
        f"the conflict must name both halves: {dual_cfg.errors}"
    )
