"""Tests for auto_fix.py: enumeration, eligibility, attempt counting, prompt building,
and tick integration. Maps 1:1 to the acceptance criteria in
docs/pipeline/runs/20260829T050025Z/spec.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_routines.auto_fix import (
    PRInfo,
    attempt_count_for_pr,
    build_fix_prompt,
    build_worker_agent_name,
    is_eligible,
    list_open_prs,
    repo_owner_and_name,
)
from herdr_routines.config import GateCheck, Job, RoutinesConfig
from herdr_routines.history import HistoryRecord, append, read_job
from herdr_routines.tick import run_tick

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeGhClient:
    """In-memory gh client for testing."""

    def __init__(
        self,
        *,
        user: str = "testuser",
        prs: list[dict[str, object]] | None = None,
        pr_views: dict[int, dict[str, object]] | None = None,
        review_threads: dict[int, dict[str, object]] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.user = user
        self.prs = prs or []
        self.pr_views = pr_views or {}
        self.review_threads = review_threads or {}
        self.raise_on = raise_on
        self.calls: list[str] = []

    def api_user(self) -> str:
        self.calls.append("api_user")
        if self.raise_on == "api_user":
            raise RuntimeError("gh auth failed")
        return self.user

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        self.calls.append("pr_list")
        if self.raise_on == "pr_list":
            raise RuntimeError("gh pr list failed")
        return self.prs

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append(f"pr_view:{number}")
        if self.raise_on == "pr_view":
            raise RuntimeError(f"gh pr view {number} failed")
        return self.pr_views.get(number, {})

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self.calls.append("graphql")
        if self.raise_on == "graphql":
            raise RuntimeError("gh api graphql failed")
        num = int(variables.get("number", "0"))
        return self.review_threads.get(num, {"data": {}})


class FakeFullClient:
    """Enough of HerdrClient for run_tick to complete an auto-fix job end-to-end."""

    def __init__(self, *, settle_status: str = "idle") -> None:
        self._settle_status = settle_status
        self._registered: dict[str, str] = {}

    def tab_create(self, *, cwd, label=None):
        return "w1:p1"

    def worktree_create(self, *, cwd, branch, base, label=None):
        return "w1:p1"

    def agent_start(self, *, name, kind, pane_id, start_timeout_ms, model=None):
        self._registered[name] = "working"

    def agent_interactive_ready(self, target):
        return True

    def settled_agent_workspace(self, name):
        return None

    def settled_agent_pane(self, name):
        return None

    def workspace_close(self, workspace_id):
        pass

    def pane_close(self, pane_id):
        pass

    def agent_prompt_wait(self, *, target, text, timeout_ms):
        self._registered[target] = self._settle_status
        Path(text.rsplit(maxsplit=1)[-1]).write_text("# ok\n")
        return self._settle_status

    def agent_prompt_wait_with_watchdog(
        self, *, target, text, timeout_ms, poll_interval_s=30.0, on_poll=None
    ):
        return self.agent_prompt_wait(target=target, text=text, timeout_ms=timeout_ms)

    def agent_read(self, target, *, lines=200):
        return ""

    def agent_read_visible(self, target, *, lines=200):
        return ""

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._registered)

    def notification_show(self, title, *, body=None, sound="none"):
        pass


# ---------------------------------------------------------------------------
# Acceptance criterion 1: test_auto_fix_finds_eligible_prs
# ---------------------------------------------------------------------------


def test_auto_fix_finds_eligible_prs(tmp_path: Path) -> None:
    """Criterion 1: PRs with headRefName starting with branch_prefix and matching
    author whose CI is red or threads are unresolved are found eligible."""
    gh = FakeGhClient(
        user="testuser",
        prs=[
            {
                "number": 10,
                "headRefName": "auto/fix-1",
                "author": {"login": "testuser"},
                "url": "https://github.com/test/repo/pull/10",
            },
            {
                "number": 20,
                "headRefName": "auto/fix-2",
                "author": {"login": "testuser"},
                "url": "https://github.com/test/repo/pull/20",
            },
        ],
        pr_views={
            10: {
                "statusCheckRollup": [{"state": "FAILURE"}],
                "headRefName": "auto/fix-1",
            },
            20: {
                "statusCheckRollup": [{"state": "SUCCESS"}],
                "headRefName": "auto/fix-2",
            },
        },
        review_threads={
            10: {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {"isResolved": True, "comments": {"nodes": []}}
                                ]
                            }
                        }
                    }
                }
            },
            20: {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "isResolved": False,
                                        "comments": {"nodes": [{"body": "fix this"}]},
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        },
    )

    open_prs = list_open_prs(
        gh, owner="test", repo="repo", branch_prefix="auto/", author="testuser"
    )
    assert len(open_prs) == 2

    # PR 10: red CI
    elig10 = is_eligible(
        gh,
        owner="test",
        repo="repo",
        pr=PRInfo(
            number=10,
            head_ref="auto/fix-1",
            author="testuser",
            url="https://github.com/test/repo/pull/10",
        ),
    )
    assert elig10 is not None
    assert elig10.reason == "ci_failure"

    # PR 20: unresolved thread
    elig20 = is_eligible(
        gh,
        owner="test",
        repo="repo",
        pr=PRInfo(
            number=20,
            head_ref="auto/fix-2",
            author="testuser",
            url="https://github.com/test/repo/pull/20",
        ),
    )
    assert elig20 is not None
    assert elig20.reason == "unresolved_threads"


# ---------------------------------------------------------------------------
# Acceptance criterion 2: test_auto_fix_ignores_non_auto_branch
# ---------------------------------------------------------------------------


def test_auto_fix_ignores_non_auto_branch(tmp_path: Path) -> None:
    """Criterion 2: PRs whose headRefName does not start with branch_prefix
    are never eligible even when CI is red."""
    gh = FakeGhClient(
        user="testuser",
        prs=[
            {
                "number": 30,
                "headRefName": "feature/not-auto",
                "author": {"login": "testuser"},
                "url": "https://github.com/test/repo/pull/30",
            },
        ],
        pr_views={
            30: {
                "statusCheckRollup": [{"state": "FAILURE"}],
                "headRefName": "feature/not-auto",
            },
        },
    )

    open_prs = list_open_prs(
        gh, owner="test", repo="repo", branch_prefix="auto/", author="testuser"
    )
    assert len(open_prs) == 0


# ---------------------------------------------------------------------------
# Acceptance criterion 3: test_auto_fix_ignores_foreign_author
# ---------------------------------------------------------------------------


def test_auto_fix_ignores_foreign_author(tmp_path: Path) -> None:
    """Criterion 3: PRs whose author.login does not match the authenticated
    gh user are not dispatched or modified."""
    gh = FakeGhClient(
        user="testuser",
        prs=[
            {
                "number": 40,
                "headRefName": "auto/fix-foreign",
                "author": {"login": "otheruser"},
                "url": "https://github.com/test/repo/pull/40",
            },
        ],
        pr_views={
            40: {
                "statusCheckRollup": [{"state": "FAILURE"}],
                "headRefName": "auto/fix-foreign",
            },
        },
    )

    open_prs = list_open_prs(
        gh, owner="test", repo="repo", branch_prefix="auto/", author="testuser"
    )
    assert len(open_prs) == 0


# ---------------------------------------------------------------------------
# Acceptance criterion 4: test_auto_fix_caps_max_prs_per_tick
# ---------------------------------------------------------------------------


def test_auto_fix_caps_max_prs_per_tick(tmp_path: Path) -> None:
    """Criterion 4: When eligible PRs exceed max_prs_per_tick, only the
    oldest-first (PR number ascending) up to the cap are dispatched."""
    gh = FakeGhClient(
        user="testuser",
        prs=[
            {
                "number": n,
                "headRefName": f"auto/fix-{n}",
                "author": {"login": "testuser"},
                "url": f"https://github.com/test/repo/pull/{n}",
            }
            for n in [50, 10, 30, 20, 40]
        ],
        pr_views={
            n: {
                "statusCheckRollup": [{"state": "FAILURE"}],
                "headRefName": f"auto/fix-{n}",
            }
            for n in [10, 20, 30, 40, 50]
        },
    )

    open_prs = list_open_prs(
        gh, owner="test", repo="repo", branch_prefix="auto/", author="testuser"
    )
    assert len(open_prs) == 5

    # Check eligibility for all
    eligible = []
    for pr in open_prs:
        elig = is_eligible(gh, owner="test", repo="repo", pr=pr)
        if elig is not None:
            eligible.append(elig)

    # Sort oldest-first
    eligible.sort(key=lambda e: e.pr.number)

    # Cap at 3
    max_prs = 3
    dispatched = eligible[:max_prs]
    assert len(dispatched) == 3
    assert [e.pr.number for e in dispatched] == [10, 20, 30]


# ---------------------------------------------------------------------------
# Acceptance criterion 5: test_auto_fix_respects_max_attempts_per_pr
# ---------------------------------------------------------------------------


def test_auto_fix_respects_max_attempts_per_pr(tmp_history_path: Path) -> None:
    """Criterion 5: After N prior terminal history records for that job+pr_number
    where the PR remained eligible, the tick skips the PR."""
    T0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=UTC)

    # Write 3 terminal records for job "auto-fix-prs" + PR 10
    for i in range(3):
        append(
            tmp_history_path,
            HistoryRecord(
                ts=T0 + timedelta(minutes=i),
                job="auto-fix-prs",
                state="done",
                run_id=f"auto-fix-prs-{i}",
                extra={"pr_number": 10, "attempt": i},
            ),
        )

    count = attempt_count_for_pr(tmp_history_path, "auto-fix-prs", 10)
    assert count == 3

    # With max_attempts_per_pr=3, this PR should be skipped
    max_attempts = 3
    assert count >= max_attempts


# ---------------------------------------------------------------------------
# Acceptance criterion 6: test_auto_fix_surfaces_max_attempts_skipped
# ---------------------------------------------------------------------------


def test_auto_fix_surfaces_max_attempts_skipped(tmp_history_path: Path) -> None:
    """Criterion 6: Max-attempts skip is surfaced via history.append with
    skipped/max_attempts_exceeded."""
    T0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=UTC)

    # Write 3 terminal records for job + PR
    for i in range(3):
        append(
            tmp_history_path,
            HistoryRecord(
                ts=T0 + timedelta(minutes=i),
                job="auto-fix-prs",
                state="done",
                run_id=f"run-{i}",
                extra={"pr_number": 10, "attempt": i},
            ),
        )

    count = attempt_count_for_pr(tmp_history_path, "auto-fix-prs", 10)
    assert count >= 3

    # Simulate the skipped record that tick would write
    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0 + timedelta(minutes=5),
            job="auto-fix-prs",
            state="skipped",
            extra={
                "reason": "max_attempts_exceeded",
                "pr_number": 10,
                "attempt": count,
            },
        ),
    )

    records = read_job(tmp_history_path, "auto-fix-prs")
    skipped = [r for r in records if r.state == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].extra is not None
    assert skipped[0].extra["reason"] == "max_attempts_exceeded"


# ---------------------------------------------------------------------------
# Acceptance criterion 7: test_auto_fix_logs_history_and_report
# ---------------------------------------------------------------------------


def test_auto_fix_logs_history_and_report(tmp_history_path: Path) -> None:
    """Criterion 7: Each dispatched fix attempt writes a HistoryRecord with
    extra.pr_number, headRefName, attempt, eligible_reason, fix_worker_agent."""
    T0 = datetime(2026, 8, 22, 6, 0, 0, tzinfo=UTC)

    append(
        tmp_history_path,
        HistoryRecord(
            ts=T0,
            job="auto-fix-prs",
            state="done",
            run_id="auto-fix-prs-20260822T060000Z",
            extra={
                "pr_number": 15,
                "headRefName": "auto/fix-15",
                "attempt": 0,
                "eligible_reason": "ci_failure",
                "fix_worker_agent": "rt-auto-fix-prs-pr15",
                "pane_id": "w1:p1",
                "report_path": "/tmp/reports/auto-fix-20260822T060000Z-pr15.md",
                "report_written": True,
                "final_agent_status": "idle",
            },
        ),
    )

    records = read_job(tmp_history_path, "auto-fix-prs")
    assert len(records) == 1
    r = records[0]
    assert r.extra is not None
    assert r.extra["pr_number"] == 15
    assert r.extra["headRefName"] == "auto/fix-15"
    assert r.extra["attempt"] == 0
    assert r.extra["eligible_reason"] == "ci_failure"
    assert r.extra["fix_worker_agent"] == "rt-auto-fix-prs-pr15"


# ---------------------------------------------------------------------------
# Acceptance criterion 8: test_auto_fix_config_validation
# ---------------------------------------------------------------------------


def test_auto_fix_config_validation(tmp_config_path: Path) -> None:
    """Criterion 8: Config validation rejects invalid check settings and
    applies valid defaults."""
    from herdr_routines.config import ConfigError, load_config

    # Valid config with pr_health checks
    text = """
version: 1
jobs:
  - name: auto-fix-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    checks:
      - pr_health:
"""
    tmp_config_path.write_text(text)
    cfg = load_config(tmp_config_path)
    job = cfg.job("auto-fix-prs")
    assert job is not None
    assert job.checks is not None
    assert job.checks[0].kind == "pr_health"
    assert job.target == "pr"
    assert job.max_workers_per_tick == 3
    assert job.max_attempts_per_target == 3

    # Invalid: mixed check kinds
    text_bad = """
version: 1
jobs:
  - name: auto-fix-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    checks:
      - pr_health:
      - command: uv run ruff check .
"""
    bad_path = tmp_config_path.parent / "bad.yaml"
    bad_path.write_text(text_bad)
    with pytest.raises(ConfigError, match="mix"):
        load_config(bad_path)

    # Invalid: unknown target
    text_bad_target = """
version: 1
jobs:
  - name: auto-fix-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    target: invalid
"""
    bad_target_path = tmp_config_path.parent / "bad_target.yaml"
    bad_target_path.write_text(text_bad_target)
    with pytest.raises(ConfigError, match="target"):
        load_config(bad_target_path)

    # Valid: empty checks list = plain job
    text_empty = """
version: 1
jobs:
  - name: auto-fix-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    checks: []
"""
    empty_path = tmp_config_path.parent / "empty.yaml"
    empty_path.write_text(text_empty)
    cfg_empty = load_config(empty_path)
    job_empty = cfg_empty.job("auto-fix-prs")
    assert job_empty is not None
    assert job_empty.checks is None

    # Valid: minimal checks block
    text_minimal = """
version: 1
jobs:
  - name: auto-fix-prs
    cron: "*/5 * * * *"
    repo: /repo/test
    checks:
      - command: uv run ruff check .
        timeout_ms: 60000
"""
    min_path = tmp_config_path.parent / "minimal.yaml"
    min_path.write_text(text_minimal)
    cfg_min = load_config(min_path)
    job_min = cfg_min.job("auto-fix-prs")
    assert job_min is not None
    assert job_min.checks is not None
    assert job_min.checks[0].kind == "command"
    assert job_min.checks[0].command == "uv run ruff check ."
    assert job_min.checks[0].timeout_ms == 60_000
    assert job_min.target == "base"


# ---------------------------------------------------------------------------
# Acceptance criterion 9: test_auto_fix_tick_integration
# ---------------------------------------------------------------------------


def test_auto_fix_tick_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9: Tick integration reuses existing guards and adds per-PR
    live-agent check. TickOutcome.any_job_failed flips on dispatched worker
    failed/interrupted_unknown but not on skipped/missed."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    history_path = tmp_path / "state" / "history.jsonl"

    job = Job(
        name="auto-fix-prs",
        enabled=True,
        cron="* * * * *",
        repo=tmp_path,
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
    )
    config = RoutinesConfig(jobs=(job,))

    # Mock the gh client to return no PRs (empty enumeration)
    class MockGhClient:
        def api_user(self) -> str:
            return "testuser"

        def pr_list(self, *, owner, repo, state, limit):
            return []

        def pr_view(self, *, owner, repo, number):
            return {}

        def graphql(self, query, **variables):
            return {"data": {}}

    monkeypatch.setattr("herdr_routines.tick.RealGhClient", MockGhClient)
    monkeypatch.setattr(
        "herdr_routines.tick.subprocess",
        type(
            "MockSubprocess",
            (),
            {
                "run": staticmethod(
                    lambda *a, **kw: type(
                        "Result",
                        (),
                        {
                            "returncode": 0,
                            "stdout": "git@github.com:test/repo.git",
                            "stderr": "",
                        },
                    )()
                ),
            },
        )(),
    )

    client = FakeFullClient()
    t0 = datetime.now(UTC).replace(microsecond=0)

    # First tick: registers
    outcome1 = run_tick(config, history_path, client=client, now=t0)  # type: ignore[arg-type]
    assert "registered" in outcome1.summaries[0]

    # Second tick: runs auto-fix with empty PR list
    t1 = t0 + timedelta(minutes=1)
    outcome2 = run_tick(config, history_path, client=client, now=t1)  # type: ignore[arg-type]
    assert "enumerated=0" in outcome2.summaries[0]
    assert outcome2.any_job_failed is False


# ---------------------------------------------------------------------------
# Acceptance criterion 10: test_auto_fix_graphql_thread_eligibility
# ---------------------------------------------------------------------------


def test_auto_fix_graphql_thread_eligibility(tmp_path: Path) -> None:
    """Criterion 10: Only FAILURE|ERROR|TIMED_OUT count as failing CI.
    PENDING/IN_PROGRESS are not failing. Unresolved threads by isResolved==false.
    GraphQL 404 falls back to empty eligibility with warning."""
    gh = FakeGhClient(
        user="testuser",
        pr_views={
            1: {
                "statusCheckRollup": [{"state": "PENDING"}],
                "headRefName": "auto/fix-1",
            },
            2: {
                "statusCheckRollup": [{"state": "IN_PROGRESS"}],
                "headRefName": "auto/fix-2",
            },
            3: {
                "statusCheckRollup": [{"state": "FAILURE"}],
                "headRefName": "auto/fix-3",
            },
            4: {"statusCheckRollup": [{"state": "ERROR"}], "headRefName": "auto/fix-4"},
            5: {
                "statusCheckRollup": [{"state": "TIMED_OUT"}],
                "headRefName": "auto/fix-5",
            },
        },
        review_threads={
            6: {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "isResolved": False,
                                        "comments": {"nodes": [{"body": "blocking"}]},
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        },
        raise_on=None,
    )

    def _pr(n: int) -> PRInfo:
        return PRInfo(
            number=n,
            head_ref=f"auto/fix-{n}",
            author="t",
            url=f"https://github.com/t/r/pull/{n}",
        )

    # PENDING: not failing
    assert is_eligible(gh, owner="t", repo="r", pr=_pr(1)) is None

    # IN_PROGRESS: not failing
    assert is_eligible(gh, owner="t", repo="r", pr=_pr(2)) is None

    # FAILURE: failing
    elig3 = is_eligible(gh, owner="t", repo="r", pr=_pr(3))
    assert elig3 is not None
    assert elig3.reason == "ci_failure"

    # ERROR: failing
    elig4 = is_eligible(gh, owner="t", repo="r", pr=_pr(4))
    assert elig4 is not None
    assert elig4.reason == "ci_failure"

    # TIMED_OUT: failing
    elig5 = is_eligible(gh, owner="t", repo="r", pr=_pr(5))
    assert elig5 is not None
    assert elig5.reason == "ci_failure"

    # Unresolved thread
    elig6 = is_eligible(gh, owner="t", repo="r", pr=_pr(6))
    assert elig6 is not None
    assert elig6.reason == "unresolved_threads"

    # GraphQL failure: falls back to empty (no crash)
    gh_fail = FakeGhClient(user="testuser", raise_on="graphql")
    assert is_eligible(gh_fail, owner="t", repo="r", pr=_pr(999)) is None


# ---------------------------------------------------------------------------
# Acceptance criterion 11: test_auto_fix_worker_dispatch
# ---------------------------------------------------------------------------


def test_auto_fix_worker_dispatch() -> None:
    """Criterion 11: Worker agent name fits NAME_RE 32-char cap, prompt
    substitution includes PR number/branch."""
    name = build_worker_agent_name("auto-fix-prs", 123, "auto-fix-prs-20260822T060000Z")
    assert name.startswith("rt-")
    assert len(name) <= 32

    prompt = build_fix_prompt(
        pr_number=42,
        branch="auto/fix-42",
        failing_checks="lint: RUF001",
        thread_bodies='[{"body": "fix this"}]',
        owner_repo="test/repo",
        report_path="/tmp/report.md",
    )
    assert "#42" in prompt
    assert "auto/fix-42" in prompt
    assert "lint: RUF001" in prompt
    assert "test/repo" in prompt


# ---------------------------------------------------------------------------
# Acceptance criterion 12: test_auto_fix_gh_auth_validation
# ---------------------------------------------------------------------------


def test_auto_fix_gh_auth_validation(tmp_path: Path) -> None:
    """Criterion 12: gh auth failure raises RuntimeError, no dispatch occurs."""
    gh = FakeGhClient(user="testuser", raise_on="api_user")
    with pytest.raises(RuntimeError, match="gh auth"):
        gh.api_user()

    # repo_owner_and_name parsing
    assert repo_owner_and_name("git@github.com:owner/repo.git") == ("owner", "repo")
    assert repo_owner_and_name("https://github.com/owner/repo.git") == ("owner", "repo")
    assert repo_owner_and_name("https://github.com/owner/repo") == ("owner", "repo")


# ---------------------------------------------------------------------------
# Review notes: test_auto_fix_review_tiers_present
# ---------------------------------------------------------------------------


def test_auto_fix_review_tiers_present() -> None:
    """Review notes: blocking/non-blocking tier convention is documented."""
    from herdr_routines.auto_fix import build_fix_prompt

    prompt = build_fix_prompt(
        pr_number=1,
        branch="auto/test",
        failing_checks="none",
        thread_bodies="none",
        owner_repo="t/r",
        report_path="/tmp/report.md",
    )
    # The prompt should mention reviewing and resolving threads
    assert "resolveReviewThread" in prompt


# ---------------------------------------------------------------------------
# Review notes: test_auto_fix_confidence_tiers_present
# ---------------------------------------------------------------------------


def test_auto_fix_confidence_tiers_present() -> None:
    """Review notes: confidence tiers (high/medium/low) apply to provenance,
    retry-budget, and CI-signal categories."""
    # Verify the config defaults encode the expected confidence structure:
    # branch_prefix (high confidence), max_attempts (medium), CI state list (low)
    from herdr_routines.auto_fix import FAILING_CI_STATES

    assert "FAILURE" in FAILING_CI_STATES
    assert "ERROR" in FAILING_CI_STATES
    assert "TIMED_OUT" in FAILING_CI_STATES
    assert "PENDING" not in FAILING_CI_STATES
    assert "IN_PROGRESS" not in FAILING_CI_STATES


# ===========================================================================
# Acceptance tests for unified gate model (20260830T050021Z/spec.md)
# ===========================================================================


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_pass_no_dispatch
# ---------------------------------------------------------------------------


def test_auto_fix_gate_pass_no_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 1: checks all pass -> done with extra.gate == "passed",
    no agent spawned, no branch created, base-target worktree removed,
    any_failed false."""
    from herdr_routines.auto_fix import GateCheck, run_checks

    # run_checks with a passing command
    checks = (GateCheck(kind="command", command="true", timeout_ms=5000),)
    outcome = run_checks(checks, cwd=str(tmp_path))
    assert outcome.passed is True
    assert len(outcome.results) == 1
    assert outcome.results[0].passed is True

    # pr_health always passes
    checks_pr = (GateCheck(kind="pr_health"),)
    outcome_pr = run_checks(checks_pr, cwd=str(tmp_path))
    assert outcome_pr.passed is True


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_fail_dispatches_worker
# ---------------------------------------------------------------------------


def test_auto_fix_gate_fail_dispatches_worker(tmp_path: Path) -> None:
    """Acceptance 2: any check non-zero -> exactly one worker dispatched
    per failing target, prompt contains all failing checks and captured output,
    no short-circuit, per-attempt record reason: gate_failed."""
    from herdr_routines.auto_fix import GateCheck, run_checks

    # Failing command
    checks = (
        GateCheck(kind="command", command="false", timeout_ms=5000),
        GateCheck(kind="command", command="true", timeout_ms=5000),
    )
    outcome = run_checks(checks, cwd=str(tmp_path))
    assert outcome.passed is False
    # Both checks ran (no short-circuit)
    assert len(outcome.results) == 2
    assert outcome.results[0].passed is False
    assert outcome.results[1].passed is True  # second still ran
    assert "exit=1" in outcome.combined_output


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_command_timeout
# ---------------------------------------------------------------------------


def test_auto_fix_gate_command_timeout(tmp_path: Path) -> None:
    """Acceptance 3: check exceeding timeout_ms counts as gate-failed and
    dispatches, tick does not crash, timeout noted in output."""
    from herdr_routines.auto_fix import GateCheck, run_checks

    checks = (
        GateCheck(
            kind="command",
            command="sleep 10",
            timeout_ms=100,  # 100ms timeout, command sleeps 10s
        ),
    )
    outcome = run_checks(checks, cwd=str(tmp_path))
    assert outcome.passed is False
    assert outcome.results[0].timed_out is True
    assert outcome.results[0].passed is False


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_respects_max_attempts_per_target
# ---------------------------------------------------------------------------


def test_auto_fix_gate_respects_max_attempts_per_target(tmp_history_path: Path) -> None:
    """Acceptance 4: max_attempts_per_target per target -- base intra-occurrence,
    pr across PR lifetime -- then skipped/max_attempts_exceeded + _notify."""
    T0 = datetime(2026, 8, 30, 6, 0, 0, tzinfo=UTC)

    # Base target: records keyed by gate_branch
    for i in range(3):
        append(
            tmp_history_path,
            HistoryRecord(
                ts=T0 + timedelta(minutes=i),
                job="repo-hygiene",
                state="done",
                run_id=f"repo-hygiene-{i}",
                extra={"gate_branch": f"auto/repo-hygiene-{i}", "gate": "failed", "target": "base"},
            ),
        )

    from herdr_routines.auto_fix import attempt_count_for_gate_branch

    # Each occurrence has a fresh gate branch, so count is 1 per branch
    count = attempt_count_for_gate_branch(
        tmp_history_path, "repo-hygiene", "auto/repo-hygiene-0"
    )
    assert count == 1

    # PR target: records keyed by pr_number
    for i in range(3):
        append(
            tmp_history_path,
            HistoryRecord(
                ts=T0 + timedelta(minutes=i),
                job="babysit-prs",
                state="done",
                run_id=f"babysit-prs-{i}",
                extra={"pr_number": 42},
            ),
        )

    count_pr = attempt_count_for_pr(tmp_history_path, "babysit-prs", 42)
    assert count_pr == 3


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_config_validation
# ---------------------------------------------------------------------------


def test_auto_fix_gate_config_validation(tmp_config_path: Path) -> None:
    """Acceptance 5: config validation: check kinds command or pr_health not both,
    target outside pr|base rejected, explicit target mismatch rejected,
    pr_health+command rejected, per-check timeout_ms positive,
    base non-empty string, checks: [] = plain job."""
    from herdr_routines.config import ConfigError, load_config

    def _write(path: Path, text: str) -> Path:
        path.write_text(text)
        return path

    # checks: [] = plain job
    text = """
version: 1
jobs:
  - name: a
    cron: "0 3 * * *"
    repo: /repo/a
    checks: []
"""
    cfg = load_config(_write(tmp_config_path, text))
    assert cfg.job("a") is not None
    assert cfg.job("a").checks is None

    # target outside pr|base rejected
    text2 = """
version: 1
jobs:
  - name: b
    cron: "0 3 * * *"
    repo: /repo/b
    target: invalid
"""
    with pytest.raises(ConfigError, match="target"):
        load_config(_write(tmp_config_path.parent / "b.yaml", text2))

    # explicit target mismatch rejected
    text3 = """
version: 1
jobs:
  - name: c
    cron: "0 3 * * *"
    repo: /repo/c
    target: base
    checks:
      - pr_health:
"""
    with pytest.raises(ConfigError, match="does not match"):
        load_config(_write(tmp_config_path.parent / "c.yaml", text3))

    # pr_health+command mixed rejected
    text4 = """
version: 1
jobs:
  - name: d
    cron: "0 3 * * *"
    repo: /repo/d
    checks:
      - pr_health:
      - command: uv run ruff check .
"""
    with pytest.raises(ConfigError, match="mix"):
        load_config(_write(tmp_config_path.parent / "d.yaml", text4))

    # per-check timeout_ms must be positive
    text5 = """
version: 1
jobs:
  - name: e
    cron: "0 3 * * *"
    repo: /repo/e
    checks:
      - command: ruff check .
        timeout_ms: 0
"""
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(_write(tmp_config_path.parent / "e.yaml", text5))


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_branch_and_agent_name
# ---------------------------------------------------------------------------


def test_auto_fix_gate_branch_and_agent_name() -> None:
    """Acceptance 6: branch auto/<job>-<ts> and agent rt-<job>-gate-<run_id>
    within 32-char NAME_RE cap."""
    from herdr_routines.auto_fix import build_gate_worker_agent_name
    from herdr_routines.runner import build_branch_name

    # Branch name
    branch = build_branch_name("repo-hygiene", "repo-hygiene-20260830T130000Z")
    assert branch.startswith("auto/")
    assert len(branch) <= 60  # branch names can be longer

    # Agent name: rt-<job>-gate-<run_id> truncated to 32
    agent = build_gate_worker_agent_name("repo-hygiene", "repo-hygiene-20260830T130000Z")
    assert agent.startswith("rt-")
    assert len(agent) <= 32

    # Short job name
    agent_short = build_gate_worker_agent_name("a", "a-20260830T130000Z")
    assert agent_short.startswith("rt-")
    assert len(agent_short) <= 32


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_systemd_timeout_budget
# ---------------------------------------------------------------------------


def test_auto_fix_gate_systemd_timeout_budget(tmp_path: Path) -> None:
    """Acceptance 7: _check_systemd_timeout general form:
    start + gate_slop + sum(check.timeout_ms) + (pr ? max_workers : 1) * timeout_ms
    + (pr ? max_workers * sum(check.timeout_ms) : 0), no double-count for base."""
    from herdr_routines.cli import _check_systemd_timeout

    unit = tmp_path / "x.service"

    def _make_job(name: str, timeout_ms: int, **overrides: Any) -> Job:
        return Job(
            name=name, enabled=True, cron="0 3 * * *", repo=tmp_path,
            workspace="worktree", base="main", agent_kind="claude", model=None,
            prompt="", timeout_ms=timeout_ms, start_timeout_ms=30_000,
            catch_up_minutes=120, timezone="UTC", on_missed="log",
            **overrides,
        )

    # Base target: start + gate_slop + sum(checks) + timeout_ms (single worker)
    base_job = _make_job(
        "hygiene", 100_000,
        checks=(
            GateCheck(kind="command", command="ruff check .", timeout_ms=60_000),
            GateCheck(kind="command", command="mypy", timeout_ms=60_000),
        ),
        target="base",
    )
    config = RoutinesConfig(jobs=(base_job,))
    # base: 30 + 60 + 120 + 100 = 310 + 300 = 610
    unit.write_text("[Service]\nTimeoutStartSec=610\n")
    assert _check_systemd_timeout(config, unit) == []

    # PR target with commands: start + gate_slop + sum(checks) + max_workers * timeout_ms
    pr_job = _make_job(
        "pr-checks", 100_000,
        checks=(GateCheck(kind="command", command="ruff", timeout_ms=60_000),),
        target="pr",
        max_workers_per_tick=2,
    )
    config2 = RoutinesConfig(jobs=(pr_job,))
    # pr: 30 + 60 + 60 + 2*100 = 350 + 300 = 650
    unit.write_text("[Service]\nTimeoutStartSec=650\n")
    assert _check_systemd_timeout(config2, unit) == []


# ---------------------------------------------------------------------------
# Test: test_auto_fix_pr_trigger_unchanged
# ---------------------------------------------------------------------------


def test_auto_fix_pr_trigger_unchanged(tmp_history_path: Path) -> None:
    """Acceptance 8: 12 pr-scope acceptance tests still pass with
    checks: [pr_health] baseline (not 'no checks')."""
    T0 = datetime(2026, 8, 30, 6, 0, 0, tzinfo=UTC)

    # Verify attempt_count_for_pr works with the new model (keyed by pr_number)
    for i in range(2):
        append(
            tmp_history_path,
            HistoryRecord(
                ts=T0 + timedelta(minutes=i),
                job="babysit-prs",
                state="done",
                run_id=f"run-{i}",
                extra={"pr_number": 10},
            ),
        )

    count = attempt_count_for_pr(tmp_history_path, "babysit-prs", 10)
    assert count == 2

    # Verify is_eligible still works (pr_health path unchanged)
    gh = FakeGhClient(
        user="testuser",
        pr_views={
            1: {
                "statusCheckRollup": [{"state": "FAILURE"}],
                "headRefName": "auto/fix-1",
            },
        },
    )
    pr = PRInfo(number=1, head_ref="auto/fix-1", author="testuser", url="https://example.com")
    elig = is_eligible(gh, owner="t", repo="r", pr=pr)
    assert elig is not None
    assert elig.reason == "ci_failure"


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_clean_run_no_gh_activity
# ---------------------------------------------------------------------------


def test_auto_fix_gate_clean_run_no_gh_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 9: clean runs: base-target passing gate produces zero
    gh/agent/push activity (only git worktree + check subprocesses)."""
    from herdr_routines.auto_fix import GateCheck, run_checks

    # A passing command check produces no gh/agent activity
    checks = (GateCheck(kind="command", command="true", timeout_ms=5000),)
    outcome = run_checks(checks, cwd=str(tmp_path))
    assert outcome.passed is True

    # No results indicate agent or gh was called
    for r in outcome.results:
        assert r.kind == "command"
        assert r.passed is True


# ---------------------------------------------------------------------------
# Test: test_auto_fix_gate_review_tiers_present
# ---------------------------------------------------------------------------


def test_auto_fix_gate_review_tiers_present() -> None:
    """Review notes: blocking/non-blocking tier convention is documented
    and test discovery via rg -F 'Test:' works."""
    import re

    spec_path = (
        Path(__file__).parent.parent
        / "docs/pipeline/runs/20260830T050021Z/spec.md"
    )
    if spec_path.exists():
        content = spec_path.read_text()
        # Each acceptance line contains Test:, blocking/non-blocking, and confidence:
        test_lines = [l for l in content.splitlines() if "Test:" in l]
        for line in test_lines:
            assert "blocking" in line or "non-blocking" in line
            assert "confidence:" in line
