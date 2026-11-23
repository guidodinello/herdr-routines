"""Acceptance tests for spec v2 20261123T050000Z: retries for every job kind, backed off
across ticks. One test per `## Acceptance criteria` item, named exactly as the spec
requires. The feature is not implemented yet, so these tests fail for the right reason.
"""

from __future__ import annotations

import inspect
import sys
import warnings
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import tick
from herdr_routines.config import (
    AuditSpec,
    ConfigError,
    GateCheck,
    Job,
    RoutinesConfig,
    load_config,
)
from herdr_routines.dispatch_common import Occurrence
from herdr_routines.history import (
    TRIGGER_CRON,
    TRIGGER_MANUAL,
    TRIGGER_REQUEST,
    HistoryExtra,
    HistoryRecord,
    read_job,
)
from herdr_routines.runner import RunOutcome
from herdr_routines.tick import run_tick
from tests.dispatch_modules import patch_dispatch
from tests.test_github_event import (
    EVENT_ID,
    EventGhClient,
    RecordingHerdrClient,
    _event_ticks,
)
from tests.test_github_event import (
    _init_repo as _init_event_repo,
)
from tests.test_tick import (
    FakeClient,
    FakePipelineClient,
    _init_repo_with_branch,
    make_job,
    make_pipeline_job,
)

TICK_INTERVAL = timedelta(minutes=5)
REGISTER_AT = datetime(2026, 1, 1, 4, 59, tzinfo=UTC)
CRON_AT = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def _dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    assert isinstance(value, str), f"expected a datetime or ISO string, got {value!r}"
    return datetime.fromisoformat(value)


def _states(records: list[HistoryRecord]) -> list[str]:
    return [r.state for r in records]


def _scheduled_retries(records: list[HistoryRecord]) -> list[HistoryRecord]:
    return [
        r
        for r in records
        if r.state == "scheduled" and (r.extra or {}).get("reason") == "retry_pending"
    ]


def _failed(records: list[HistoryRecord]) -> list[HistoryRecord]:
    return [r for r in records if r.state == "failed"]


def _flaky_ensure_repo(*, fail_times: int = 1) -> Callable[[Job], Path]:
    calls = {"n": 0}

    def ensure(job: Job) -> Path:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise RuntimeError("transient dns failure")
        return job.repo

    return ensure


def _always_fail_ensure_repo(job: Job) -> Path:
    raise RuntimeError("transient dns failure")


def _patch_ensure_repo(
    monkeypatch: pytest.MonkeyPatch, ensure: Callable[[Job], Path]
) -> None:
    patch_dispatch(monkeypatch, "ensure_repo", ensure)
    monkeypatch.setattr("herdr_routines.runner.ensure_repo", ensure)


# ---------------------------------------------------------------------------
# 1. Config defaults and explicit overrides
# ---------------------------------------------------------------------------


def test_retry_defaults_and_explicit_overrides(tmp_config_path: Path) -> None:
    """Criterion 1: absent retry keys resolve to one attempt over the five pre-agent
    reasons; explicit 0 opts out, explicit post-agent retry_on opts in, an explicit empty
    retry_on with attempts > 0 is a ConfigError, and directly built Jobs keep 0/None."""
    from herdr_routines.config import PRE_AGENT_RETRY_REASONS

    base = """
version: 1
jobs:
  - name: a
    cron: "0 5 * * *"
    repo: /repo/a
"""

    job = load_config(write(tmp_config_path, base)).job("a")
    assert job is not None
    assert job.retry_attempts == 1
    assert job.retry_on == tuple(sorted(PRE_AGENT_RETRY_REASONS))

    job = load_config(
        write(
            tmp_config_path,
            base.rstrip() + "\n    retry_attempts: 0\n",
        )
    ).job("a")
    assert job is not None
    assert job.retry_attempts == 0
    assert job.retry_on is None

    job = load_config(
        write(
            tmp_config_path,
            base.rstrip() + "\n    retry_on:\n      - agent_prompt_failed\n",
        )
    ).job("a")
    assert job is not None
    assert job.retry_attempts == 1
    assert job.retry_on == ("agent_prompt_failed",)

    with pytest.raises(ConfigError, match="retry_on"):
        load_config(
            write(
                tmp_config_path,
                base.rstrip() + "\n    retry_attempts: 2\n    retry_on: []\n",
            )
        )

    direct = Job(
        name="direct",
        enabled=True,
        cron="0 5 * * *",
        repo=Path("/repo/direct"),
        workspace="root",
        base="main",
        agent_kind="claude",
        model=None,
        prompt=None,
        timeout_ms=60_000,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )
    assert direct.retry_attempts == 0
    assert direct.retry_on is None


# ---------------------------------------------------------------------------
# 2. Multi-target selectors default to no retries
# ---------------------------------------------------------------------------


def test_retry_rejected_for_multi_target_selector(tmp_config_path: Path) -> None:
    """Criterion 2: pick: my_open_prs and kind: review derive retry_attempts 0; an
    explicit retry_attempts > 0 is a ConfigError naming the double-post risk; the old
    "accepted but ignored" warning is gone."""
    pick_job = """
version: 1
jobs:
  - name: pick-review
    cron: "0 5 * * *"
    repo: /repo/a
    pick: my_open_prs
    select: [author]
"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        job = load_config(write(tmp_config_path, pick_job)).job("pick-review")
    assert job is not None
    assert job.retry_attempts == 0
    assert not any("ignored" in str(w.message) for w in caught), (
        f"the old ignored-retry warning must be gone, got: {[str(w.message) for w in caught]}"
    )

    review_job = """
version: 1
jobs:
  - name: review-sugar
    cron: "0 5 * * *"
    repo: /repo/b
    kind: review
    select: [author]
"""
    job = load_config(write(tmp_config_path, review_job)).job("review-sugar")
    assert job is not None
    assert job.retry_attempts == 0

    for text in (pick_job, review_job):
        bad = text.rstrip() + "\n    retry_attempts: 1\n"
        with pytest.raises(ConfigError) as exc_info:
            load_config(write(tmp_config_path, bad))
        message = str(exc_info.value).lower()
        assert "retry" in message
        assert "review" in message or "double" in message or "multi-target" in message


# ---------------------------------------------------------------------------
# 3. Every kind retries a pre-agent failure on a later tick
# ---------------------------------------------------------------------------


def _audit_script(tmp_path: Path) -> str:
    script = tmp_path / "audit_manifest.py"
    script.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['ROUTINE_FINDINGS']).write_text(\n"
        "    json.dumps({'version': 1, 'check': 'type-health', 'findings': []})\n"
        ")\n"
    )
    return f"{sys.executable} {script}"


def _run_sync_kind_retry(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    if kind == "routine":
        job = make_job(
            tmp_path,
            name="retry-routine",
            cron="0 5 * * *",
            workspace="root",
            repo=tmp_path,
        )
        repo = tmp_path
    elif kind == "audit":
        repo = _init_repo_with_branch(tmp_path, branch="unused")
        job = make_job(
            tmp_path,
            name="retry-audit",
            cron="0 5 * * *",
            workspace="worktree",
            repo=repo,
            base="main",
            kind="audit",
            audit=AuditSpec(command=_audit_script(tmp_path), timeout_ms=60_000),
            adopt_baseline=True,
        )
    elif kind == "gated-base":
        repo = _init_repo_with_branch(tmp_path, branch="unused")
        job = make_job(
            tmp_path,
            name="retry-gated-base",
            cron="0 5 * * *",
            workspace="worktree",
            repo=repo,
            base="main",
            kind="gated",
            checks=(GateCheck(kind="command", command="true"),),
            target="base",
            max_attempts_per_target=3,
        )
    elif kind == "gated-pr":
        repo = _init_repo_with_branch(tmp_path, branch="unused")
        job = make_job(
            tmp_path,
            name="retry-gated-pr",
            cron="0 5 * * *",
            workspace="worktree",
            repo=repo,
            base="main",
            kind="gated",
            checks=(GateCheck(kind="pr_health"),),
            target="pr",
            max_workers_per_tick=1,
            max_attempts_per_target=3,
        )

        class _Gh:
            def api_user(self) -> str:
                return "me"

        monkeypatch.setattr(
            "herdr_routines.gated_pr_flow.repo_owner_and_name",
            lambda _url: ("acme", "widgets"),
        )
        monkeypatch.setattr("herdr_routines.gated_pr_flow.RealGhClient", lambda: _Gh())
        monkeypatch.setattr(
            "herdr_routines.gated_pr_flow.list_open_prs", lambda *_a, **_k: []
        )
    else:
        raise AssertionError(f"unknown kind {kind}")

    assert repo.exists()
    _patch_ensure_repo(monkeypatch, _flaky_ensure_repo(fail_times=1))
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")

    run_tick(config, history_path, client=client, now=REGISTER_AT)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT)  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    failed = _failed(records)
    assert failed, f"{kind}: first attempt did not fail: {_states(records)}"
    failed = failed[0]
    assert failed.extra is not None
    assert failed.extra.get("reason") in {"clone_failed", "repo_sync_failed"}

    scheduled = _scheduled_retries(records)
    assert scheduled, f"{kind}: no scheduled retry after a pre-agent failure"

    run_tick(
        config,
        history_path,
        client=client,
        now=CRON_AT + TICK_INTERVAL,
    )  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    retry_running = [
        r
        for r in records
        if r.state == "running" and (r.extra or {}).get("attempt") == 1
    ]
    assert retry_running, f"{kind}: the retry never dispatched: {_states(records)}"
    assert retry_running[0].extra is not None
    assert retry_running[0].extra.get("retried_from_run_id") == failed.run_id
    assert records[-1].state == "done", (
        f"{kind}: retry did not end done: {_states(records)}"
    )


def _run_pipeline_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_pipeline_job(tmp_path, cron="0 5 * * *")
    (job.repo / ".git").mkdir(parents=True, exist_ok=True)
    config = RoutinesConfig(jobs=(job,))
    client = FakePipelineClient()
    launches: list[list[str]] = []

    def fake_launch(
        argv: list[str], *, timeout_s: float = 30.0
    ) -> tuple[int, str, str]:
        launches.append(argv)
        return 0, "", ""

    patch_dispatch(monkeypatch, "launch_pipeline", fake_launch)

    run_tick(config, history_path, client=client, now=REGISTER_AT)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT)  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    assert records[-1].state == "running", _states(records)
    assert not _scheduled_retries(records), (
        "a pipeline launch that leaves `running` must schedule nothing"
    )

    running = records[-1]
    assert running.run_id is not None
    bare_run_id = running.run_id.removeprefix(f"{job.name}-")
    report_path = tmp_path / "state" / "reports" / f"pipeline-{bare_run_id}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("## Outcome: failed (repo_sync_failed)\nreason: dns\n")

    failed_at = CRON_AT + timedelta(minutes=1)
    run_tick(config, history_path, client=client, now=failed_at)  # type: ignore[arg-type]
    records = read_job(history_path, job.name)
    failed = _failed(records)
    assert failed, f"pipeline failure did not reconcile: {_states(records)}"
    assert failed[-1].extra is not None
    assert failed[-1].extra.get("reason") == "repo_sync_failed"
    scheduled = _scheduled_retries(records)
    assert scheduled, "pipeline failure did not schedule a retry"

    retry_at = failed_at + TICK_INTERVAL
    run_tick(config, history_path, client=client, now=retry_at)  # type: ignore[arg-type]
    assert len(launches) == 2, f"retry did not relaunch the pipeline: {launches}"
    records = read_job(history_path, job.name)
    retry_running = [
        r
        for r in records
        if r.state == "running" and (r.extra or {}).get("attempt") == 1
    ]
    assert retry_running, f"no retry running record: {_states(records)}"
    assert retry_running[-1].extra is not None
    assert retry_running[-1].extra.get("retried_from_run_id") == failed[-1].run_id

    retry_run_id = retry_running[-1].run_id
    assert retry_run_id is not None
    retry_bare = retry_run_id.removeprefix(f"{job.name}-")
    done_path = tmp_path / "state" / "reports" / f"pipeline-{retry_bare}.md"
    done_path.write_text("## Outcome: done\n")
    run_tick(config, history_path, client=client, now=retry_at + timedelta(minutes=1))  # type: ignore[arg-type]
    records = read_job(history_path, job.name)
    assert records[-1].state == "done", (
        f"pipeline retry did not end done: {_states(records)}"
    )


@pytest.mark.parametrize(
    "kind", ["routine", "audit", "gated-base", "gated-pr", "pipeline"]
)
def test_every_kind_retries_pre_agent_failure_on_a_later_tick(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 3: routine, audit, gated base, gated PR and pipeline each retry one
    pre-agent failure on a later tick and end done. A pipeline launch that leaves
    `running` as the last record schedules nothing."""
    if kind == "pipeline":
        _run_pipeline_retry(tmp_path, monkeypatch)
    else:
        _run_sync_kind_retry(kind, tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# 4. Event-gate jobs keep their per-event budget
# ---------------------------------------------------------------------------


def test_event_gate_job_keeps_per_event_attempt_budget(
    tmp_path: Path, tmp_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 4: a gated job whose github_event check is its trigger keeps its
    per-event attempt budget and never gets a tick-scheduled retry."""
    repo = _init_event_repo(tmp_path)
    text = f"""
version: 1
jobs:
  - name: on-label-pr
    cron: "* * * * *"
    repo: {repo}
    kind: gated
    max_workers_per_tick: 1
    max_attempts_per_target: 2
    checks:
      - github_event:
          query: prs:labels
          repo: owner/repo
          match: {{label: ready-for-agent}}
"""
    job = load_config(write(tmp_config_path, text)).job("on-label-pr")
    assert job is not None
    assert job.retry_attempts == 1
    assert job.retry_on is not None
    assert "repo_sync_failed" in job.retry_on

    gh = EventGhClient(events=True)
    client = RecordingHerdrClient()
    tick_once = _event_ticks(job, tmp_path, monkeypatch, gh, client)
    patch_dispatch(monkeypatch, "ensure_repo", _always_fail_ensure_repo)

    for minute in range(1, 6):
        tick_once(minute)

    history_path = tmp_path / "state" / "history.jsonl"
    records = read_job(history_path, job.name)
    assert not _scheduled_retries(records), (
        f"an event-gate job must not get a tick-scheduled retry: {_states(records)}"
    )
    failed = [
        r
        for r in records
        if r.state == "failed"
        and (r.extra or {}).get("event_id") == EVENT_ID
        and (r.extra or {}).get("reason") == "repo_sync_failed"
    ]
    assert len(failed) == 2, (
        f"the per-event budget must allow two attempts, got {_states(records)}"
    )
    skipped = [
        r
        for r in records
        if r.state == "skipped"
        and (r.extra or {}).get("reason") == "max_attempts_exceeded"
    ]
    assert skipped, "the per-event budget did not tap out as skipped"


# ---------------------------------------------------------------------------
# 5. Backoff is computed in ticks, not slept
# ---------------------------------------------------------------------------


def test_retry_backoff_is_one_two_four_ticks_without_sleeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5: retry_due_at is 1, 2 then 4 timer intervals after the failure, and
    neither retry.py nor the tick path calls time.sleep."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        name="backoff",
        cron="0 5 * * *",
        workspace="root",
        retry_attempts=3,
        retry_on=("clone_failed", "repo_sync_failed"),
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient()
    _patch_ensure_repo(monkeypatch, _always_fail_ensure_repo)

    run_tick(config, history_path, client=client, now=REGISTER_AT)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT + timedelta(minutes=5))  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT + timedelta(minutes=15))  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT + timedelta(minutes=35))  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    scheduled = _scheduled_retries(records)
    assert [r.extra["attempt"] for r in scheduled if r.extra] == [1, 2, 3]
    failed = _failed(records)
    assert len(failed) == 4, f"expected four failed attempts, got {_states(records)}"

    intervals = []
    for marker in scheduled:
        assert marker.extra is not None
        due = _dt(marker.extra["retry_due_at"])
        prior = [r for r in failed if r.ts <= due][-1]
        intervals.append(int((due - prior.ts).total_seconds()))
    assert intervals == [300, 600, 1200]
    assert not _scheduled_retries(records[-1:])

    import herdr_routines.retry as retry_module

    import herdr_routines.tick as tick_module
    from herdr_routines import routine_flow

    for module in (retry_module, tick_module, routine_flow):
        source = Path(module.__file__ or "").read_text()
        assert "time.sleep" not in source, f"{module.__name__} still sleeps in a tick"


# ---------------------------------------------------------------------------
# 6. Attempt metadata and no `missed`
# ---------------------------------------------------------------------------


def test_retry_records_carry_attempt_metadata_and_no_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6: the failed record carries attempt; the pending retry record carries
    attempt, max_retries, retry_of, retry_eligible_reason and retry_due_at; a tick that
    is not yet due writes no missed record."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        name="metadata",
        cron="0 5 * * *",
        workspace="root",
        retry_attempts=2,
        retry_on=("clone_failed", "repo_sync_failed"),
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient()
    _patch_ensure_repo(monkeypatch, _always_fail_ensure_repo)

    run_tick(config, history_path, client=client, now=REGISTER_AT)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT)  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    failed = _failed(records)
    assert failed
    assert failed[0].extra is not None
    assert failed[0].extra.get("attempt") == 0
    assert failed[0].extra.get("reason") == "clone_failed"

    scheduled = _scheduled_retries(records)
    assert len(scheduled) == 1
    marker = scheduled[0]
    assert marker.extra is not None
    assert marker.extra.get("attempt") == 1
    assert marker.extra.get("max_retries") == 2
    assert marker.extra.get("retry_of") == failed[0].run_id
    assert marker.extra.get("retry_eligible_reason") == "clone_failed"
    assert marker.extra.get("trigger") == TRIGGER_CRON
    assert _dt(marker.extra["retry_due_at"]) == failed[0].ts + TICK_INTERVAL

    before = len(records)
    run_tick(config, history_path, client=client, now=CRON_AT + timedelta(minutes=2))  # type: ignore[arg-type]
    records = read_job(history_path, job.name)
    assert len(records) == before, (
        f"a not-yet-due tick wrote records: {_states(records[before:])}"
    )
    assert "missed" not in _states(records)


# ---------------------------------------------------------------------------
# 7. A pending retry is dropped when the next cron occurrence comes first
# ---------------------------------------------------------------------------


def test_pending_retry_is_dropped_when_the_next_cron_occurrence_comes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 7: a pending retry is dropped, logged as a skipped record with reason
    retry_dropped, and falls through to the normal decision when the next cron
    occurrence arrives before retry_due_at."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        name="drop",
        cron="* * * * *",
        workspace="root",
        retry_attempts=2,
        retry_on=("clone_failed", "repo_sync_failed"),
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(settle_status="idle")
    _patch_ensure_repo(monkeypatch, _flaky_ensure_repo(fail_times=1))

    t0 = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=1)
    t2 = t0 + timedelta(minutes=2)
    run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    failed = _failed(read_job(history_path, job.name))
    assert failed, "first attempt did not fail"

    run_tick(config, history_path, client=client, now=t2)  # type: ignore[arg-type]
    records = read_job(history_path, job.name)
    dropped = [
        r
        for r in records
        if r.state == "skipped" and (r.extra or {}).get("reason") == "retry_dropped"
    ]
    assert len(dropped) == 1, f"pending retry was not dropped: {_states(records)}"
    assert dropped[0].extra is not None
    assert dropped[0].extra.get("retry_of") == failed[0].run_id
    assert dropped[0].extra.get("attempt") == 1
    assert dropped[0].extra.get("trigger") == TRIGGER_CRON

    assert records[-1].state == "done", (
        f"drop did not fall through to the normal run: {_states(records)}"
    )
    assert records[-1].run_id != failed[0].run_id
    assert "missed" not in _states(records)


# ---------------------------------------------------------------------------
# 8. Post-agent reasons do not retry under the defaults
# ---------------------------------------------------------------------------


def test_post_agent_reason_does_not_retry_under_defaults(
    tmp_path: Path, tmp_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 8: a post-agent reason does not retry under the defaults; only the
    explicit pre-agent set does."""
    text = f"""
version: 1
jobs:
  - name: post-agent
    cron: "0 5 * * *"
    repo: {tmp_path}
    workspace: root
    prompt: "report to $ROUTINE_REPORT"
"""
    job = load_config(write(tmp_config_path, text)).job("post-agent")
    assert job is not None
    assert job.retry_attempts == 1
    assert job.retry_on is not None
    assert "agent_prompt_failed" not in job.retry_on

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient(fail_at="agent_prompt_wait")
    _patch_ensure_repo(monkeypatch, lambda job: job.repo)

    run_tick(config, history_path, client=client, now=REGISTER_AT)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT)  # type: ignore[arg-type]
    run_tick(config, history_path, client=client, now=CRON_AT + TICK_INTERVAL)  # type: ignore[arg-type]

    records = read_job(history_path, job.name)
    failed = _failed(records)
    assert len(failed) == 1, f"a post-agent failure was retried: {_states(records)}"
    assert failed[0].extra is not None
    assert failed[0].extra.get("reason") == "agent_prompt_failed"
    assert not _scheduled_retries(records)


# ---------------------------------------------------------------------------
# 9. Manual and request runs never schedule retries
# ---------------------------------------------------------------------------


def test_manual_and_request_runs_do_not_create_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9: manual and request runs never create a scheduled retry record, and a
    retry is scheduled only for cron/event triggers."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    job = make_job(
        tmp_path,
        name="manual",
        cron="0 5 * * *",
        workspace="root",
        retry_attempts=2,
        retry_on=("clone_failed", "repo_sync_failed"),
    )
    config = RoutinesConfig(jobs=(job,))
    client = FakeClient()

    def fake_execute_run(
        job: Job, client: Any, *, run_id: str, **_kwargs: Any
    ) -> RunOutcome:
        return RunOutcome(state="failed", run_id=run_id, reason="clone_failed")

    patch_dispatch(monkeypatch, "execute_run", fake_execute_run)

    for trigger, at in (
        (TRIGGER_MANUAL, datetime(2026, 1, 1, 4, 50, tzinfo=UTC)),
        (TRIGGER_REQUEST, datetime(2026, 1, 1, 4, 51, tzinfo=UTC)),
    ):
        tick.dispatch_manual_occurrence(
            job,
            history_path,
            Occurrence(
                f"{job.name}-{trigger}",
                HistoryExtra(trigger=trigger),
            ),
            client=client,  # type: ignore[arg-type]
            now=at,
        )

    records = read_job(history_path, job.name)
    assert not _scheduled_retries(records), (
        f"manual/request runs must not schedule retries: {_states(records)}"
    )

    run_tick(config, history_path, client=client, now=CRON_AT)  # type: ignore[arg-type]
    records = read_job(history_path, job.name)
    scheduled = _scheduled_retries(records)
    assert len(scheduled) == 1, f"a cron failure did not schedule: {_states(records)}"
    assert scheduled[0].extra is not None
    assert scheduled[0].extra.get("trigger") == TRIGGER_CRON


# ---------------------------------------------------------------------------
# 10. fallback_model still runs within one tick
# ---------------------------------------------------------------------------


def test_fallback_model_still_runs_within_one_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 10: removing the synchronous retry loop leaves fallback_model intact: a
    quota failure still falls back within the same tick, and the fallback running/terminal
    records copy attempt, max_retries and retried_from_run_id from occ.extra."""
    from herdr_routines.routine_flow import _run_routine

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"
    repo = _init_repo_with_branch(tmp_path, branch="unused")
    job = make_job(
        tmp_path,
        name="fallback",
        workspace="root",
        repo=repo,
        agent_kind="opencode",
        model="primary-model",
        fallback_model="fallback-model",
    )
    client = FakeClient(settle_status="idle", quota_exhausted_for_model="primary-model")
    now = datetime(2026, 1, 1, 5, 0, tzinfo=UTC)
    occ = Occurrence(
        f"{job.name}-retry1",
        HistoryExtra(
            trigger=TRIGGER_CRON,
            attempt=1,
            max_retries=2,
            retried_from_run_id=f"{job.name}-orig",
        ),
    )

    summary, failed = _run_routine(
        job,
        history_path,
        occ,
        client=client,  # type: ignore[arg-type]
        now=now,
        trusted_review_authors=(),
    )

    assert (summary, failed) == (f"{job.name}: done", False)
    records = read_job(history_path, job.name)
    run_records = [r for r in records if r.run_id is not None]
    assert _states(run_records) == ["running", "failed", "running", "done"]
    primary_running, primary_failed, fallback_running, fallback_done = run_records
    assert primary_failed.extra is not None
    assert primary_failed.extra.get("reason") == "quota_exhausted"
    assert fallback_running.run_id != primary_running.run_id
    assert fallback_done.run_id == fallback_running.run_id

    for record in (fallback_running, fallback_done):
        assert record.extra is not None
        assert record.extra.get("attempt") == 1
        assert record.extra.get("max_retries") == 2
        assert record.extra.get("retried_from_run_id") == f"{job.name}-orig"
        assert record.ts == now


# ---------------------------------------------------------------------------
# 11. validate's timeout no longer multiplies by retry_attempts
# ---------------------------------------------------------------------------


def test_validate_timeout_no_longer_multiplies_by_retry_attempts(
    tmp_path: Path,
) -> None:
    """Criterion 11: TimeoutStartSec no longer multiplies by (1 + retry_attempts), since
    a tick runs at most one attempt per job."""
    from dataclasses import replace

    from herdr_routines.cli import _check_systemd_timeout

    job = replace(
        make_job(tmp_path, timeout_ms=60_000),
        retry_attempts=3,
        retry_on=("clone_failed",),
    )
    config = RoutinesConfig(jobs=(job,))
    unit = tmp_path / "herdr-routines.service"

    unit.write_text("[Service]\nTimeoutStartSec=390\n")
    assert _check_systemd_timeout(config, unit) == [], (
        "one attempt is 30s start + 60s run + 300s margin = 390s; retries run on "
        "later ticks and must not inflate this"
    )

    unit.write_text("[Service]\nTimeoutStartSec=389\n")
    problems = _check_systemd_timeout(config, unit)
    assert len(problems) == 1
    assert "390" in problems[0]


# ---------------------------------------------------------------------------
# 12. Docstring and guard-comment cleanup
# ---------------------------------------------------------------------------


def test_history_docstring_and_guard_comment_drop_sync_retry() -> None:
    """Criterion 12: the history.py docstring state list mentions scheduled, and
    _guard_open_run's comment no longer describes the old synchronous retry multiplier."""
    import herdr_routines.history as history_module
    from herdr_routines.tick import _guard_open_run

    assert "scheduled" in (history_module.__doc__ or "")
    guard_source = inspect.getsource(_guard_open_run)
    assert "retry_attempts" not in guard_source, (
        "the guard comment still describes the old synchronous retry multiplier"
    )


# ---------------------------------------------------------------------------
# 13. CI green meta-check
# ---------------------------------------------------------------------------


def test_ci_green_with_retry_backoff_blocking_tests_run_first() -> None:
    """Criterion 13: lint, strict mypy and the full suite are green through
    scripts/ci-check.sh, with the blocking acceptance tests defined before the
    non-blocking ones."""
    root = Path(__file__).resolve().parents[1]
    ci_text = (root / "scripts" / "ci-check.sh").read_text()
    assert "uv run mypy" in ci_text
    assert "uv run pytest -q" in ci_text
    assert "ruff check" in ci_text

    retry_py = root / "src" / "herdr_routines" / "retry.py"
    assert retry_py.is_file(), "retry.py is the home of the pure retry policy"
    retry_source = retry_py.read_text()
    for name in (
        "pending_retry",
        "retry_due_at",
        "schedule_record",
        "retry_decision",
        "retry_occurrence",
    ):
        assert f"def {name}(" in retry_source

    schedule_source = (root / "src" / "herdr_routines" / "schedule.py").read_text()
    assert "def next_occurrence_after(" in schedule_source

    source = Path(__file__).read_text()
    blocking = [
        "test_retry_defaults_and_explicit_overrides",
        "test_retry_rejected_for_multi_target_selector",
        "test_every_kind_retries_pre_agent_failure_on_a_later_tick",
        "test_event_gate_job_keeps_per_event_attempt_budget",
        "test_retry_backoff_is_one_two_four_ticks_without_sleeping",
        "test_retry_records_carry_attempt_metadata_and_no_missed",
        "test_pending_retry_is_dropped_when_the_next_cron_occurrence_comes_first",
        "test_post_agent_reason_does_not_retry_under_defaults",
        "test_manual_and_request_runs_do_not_create_retries",
        "test_fallback_model_still_runs_within_one_tick",
        "test_validate_timeout_no_longer_multiplies_by_retry_attempts",
    ]
    first_non_blocking = source.index(
        "def test_history_docstring_and_guard_comment_drop_sync_retry("
    )
    for name in blocking:
        position = source.index(f"def {name}(")
        assert position < first_non_blocking, (
            f"{name} must be defined before non-blocking tests"
        )
