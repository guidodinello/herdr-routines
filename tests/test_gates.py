"""Tests for gates.py: the CI gate and gate 6 (reply coverage), pinned per the
acceptance criteria in docs/process/issues/034-gate-ci-checks.md and
docs/process/issues/035-gate6-reply-coverage.md.
"""

from __future__ import annotations

from herdr_routines.gates import (
    GATE3_LINT_TEST_CHECKS,
    ReviewThread,
    evaluate_ci_checks,
    evaluate_gate6,
    gate3_result,
    run_ci_gate,
    run_gate6,
)


class FakeGhClient:
    """In-memory gh client: pr_view responses per call (queue), graphql fixed."""

    def __init__(
        self,
        *,
        pr_views: list[dict[str, object]] | None = None,
        review_threads: dict[str, object] | None = None,
    ) -> None:
        self._pr_views = list(pr_views or [])
        self._review_threads = review_threads or {"data": {}}
        self.pr_view_calls = 0
        self.graphql_calls = 0

    def api_user(self) -> str:
        return "testuser"

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        return []

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        self.pr_view_calls += 1
        if len(self._pr_views) == 1:
            return self._pr_views[0]
        return self._pr_views.pop(0)

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self.graphql_calls += 1
        return self._review_threads


def _fake_runner(exit_codes: dict[str, int]):
    def runner(argv: list[str], *, timeout_s: float) -> tuple[int, str, str]:
        command = " ".join(argv)
        for prefix, code in exit_codes.items():
            if command.startswith(prefix):
                return code, "", "" if code == 0 else f"{prefix}: failed"
        return 0, "", ""

    return runner


# ---------------------------------------------------------------------------
# Gate 3 (issue 034 acceptance criterion 1) — lint widening stays prompt text,
# but the widened check set itself is pinned here.
# ---------------------------------------------------------------------------


def test_gate3_fails_on_lint_error() -> None:
    runner = _fake_runner(
        {
            "uv run ruff format --check .": 1,
            "uv run ruff check .": 0,
            "uv run pytest -q": 0,
        }
    )
    result = gate3_result(".", runner=runner)
    assert result.passed is False


def test_gate3_passes_when_all_green() -> None:
    runner = _fake_runner(
        {
            "uv run ruff format --check .": 0,
            "uv run ruff check .": 0,
            "uv run pytest -q": 0,
        }
    )
    result = gate3_result(".", runner=runner)
    assert result.passed is True
    assert len(GATE3_LINT_TEST_CHECKS) == 3


# ---------------------------------------------------------------------------
# CI gate (issue 034 acceptance criteria 2 & 3)
# ---------------------------------------------------------------------------


def test_pipeline_ci_failure_routes_to_stage6() -> None:
    """A FAILURE conclusion fails the CI gate rather than being silently accepted —
    this is the signal the orchestrator routes into stage 6 as a must-fix item
    instead of reaching `## Outcome: ok`."""
    checks: list[dict[str, object]] = [
        {"name": "Lint Python", "status": "COMPLETED", "conclusion": "FAILURE"},
        {"name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    verdict = evaluate_ci_checks(checks)
    assert verdict.passed is False
    assert "Lint Python" in (verdict.reason or "")


def test_ci_gate_tolerates_skipped_checks() -> None:
    checks: list[dict[str, object]] = [
        {"name": "optional-job", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"name": "neutral-job", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]
    verdict = evaluate_ci_checks(checks)
    assert verdict.passed is True


def test_run_ci_gate_passes_once_settled() -> None:
    gh = FakeGhClient(
        pr_views=[
            {
                "statusCheckRollup": [
                    {"name": "pytest", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ]
            }
        ]
    )
    verdict = run_ci_gate(gh, owner="o", repo="r", pr=1)
    assert verdict.passed is True
    assert gh.pr_view_calls == 1


def test_run_ci_gate_polls_until_non_pending_then_fails() -> None:
    gh = FakeGhClient(
        pr_views=[
            {
                "statusCheckRollup": [
                    {"name": "pytest", "status": "IN_PROGRESS", "conclusion": None}
                ]
            },
            {
                "statusCheckRollup": [
                    {"name": "pytest", "status": "COMPLETED", "conclusion": "FAILURE"}
                ]
            },
        ]
    )
    sleeps: list[float] = []
    ticks = iter([0.0, 0.0, 1000.0])
    verdict = run_ci_gate(
        gh,
        owner="o",
        repo="r",
        pr=1,
        timeout_s=600.0,
        poll_interval_s=15.0,
        clock=lambda: next(ticks),
        sleep=sleeps.append,
    )
    assert verdict.passed is False
    assert sleeps == [15.0]


def test_run_ci_gate_bounds_the_poll() -> None:
    """A check stuck pending forever must not hang past the bounded poll."""
    gh = FakeGhClient(
        pr_views=[
            {
                "statusCheckRollup": [
                    {"name": "stuck", "status": "IN_PROGRESS", "conclusion": None}
                ]
            }
        ]
    )
    clock_values = iter([0.0, 700.0, 700.0])
    verdict = run_ci_gate(
        gh,
        owner="o",
        repo="r",
        pr=1,
        timeout_s=600.0,
        poll_interval_s=15.0,
        clock=lambda: next(clock_values),
        sleep=lambda _s: None,
    )
    assert verdict.passed is False
    assert "pending" in (verdict.reason or "")


# ---------------------------------------------------------------------------
# Gate 6 — reply coverage (issue 035 acceptance criteria)
# ---------------------------------------------------------------------------


def test_gate6_fails_on_unreplied_thread() -> None:
    threads = [
        ReviewThread(
            is_resolved=False, comment_count=1, first_comment_body="[non-blocking] fyi"
        )
    ]
    verdict = evaluate_gate6(threads)
    assert verdict.passed is False
    assert "no reply" in (verdict.reason or "")


def test_gate6_passes_on_replied_nonblocking() -> None:
    threads = [
        ReviewThread(
            is_resolved=False, comment_count=2, first_comment_body="[non-blocking] fyi"
        )
    ]
    verdict = evaluate_gate6(threads)
    assert verdict.passed is True


def test_blocking_tag_match_excludes_non_blocking() -> None:
    """[non-blocking] must never satisfy the [blocking] assertion by substring —
    even replied, an unresolved [blocking] thread fails; an unresolved
    [non-blocking] thread with a reply passes."""
    blocking_replied = [
        ReviewThread(
            is_resolved=False, comment_count=2, first_comment_body="[blocking] fix this"
        )
    ]
    assert evaluate_gate6(blocking_replied).passed is False

    non_blocking_replied = [
        ReviewThread(
            is_resolved=False, comment_count=2, first_comment_body="[non-blocking] fyi"
        )
    ]
    assert evaluate_gate6(non_blocking_replied).passed is True


def test_gate6_resolved_threads_are_ignored() -> None:
    threads = [
        ReviewThread(
            is_resolved=True, comment_count=1, first_comment_body="[blocking] old"
        )
    ]
    assert evaluate_gate6(threads).passed is True


def test_run_gate6_parses_graphql_response() -> None:
    gh = FakeGhClient(
        review_threads={
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "comments": {
                                        "totalCount": 1,
                                        "nodes": [
                                            {"body": "[non-blocking] du glob note"}
                                        ],
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    verdict = run_gate6(gh, owner="o", repo="r", pr=81)
    assert verdict.passed is False
    assert gh.graphql_calls == 1
