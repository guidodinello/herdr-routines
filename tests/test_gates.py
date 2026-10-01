"""Tests for gates.py: the CI gate and gate 6 (reply coverage), pinned per the
acceptance criteria in docs/process/issues/034-gate-ci-checks.md and
docs/process/issues/035-gate6-reply-coverage.md, plus `evaluate_commit_checks` — the
stricter, case-normalised gate `self-update` puts in front of a commit it is about to
deploy (spec 20260929T050000Z finding F5).
"""

from __future__ import annotations

from herdr_routines.auto_fix import GhClient, RealGhClient
from herdr_routines.gates import (
    BLOCKING_TAG,
    GATE3_LINT_TEST_CHECKS,
    GATE_STAGE_CHOICES,
    ReviewThread,
    evaluate_ci_checks,
    evaluate_commit_checks,
    evaluate_gate6,
    gate3_result,
    run_ci_gate,
    run_gate5,
    run_gate6,
)
from tests.test_auto_fix import FakeGhClient as AutoFixFakeGhClient


class FakeGhClient:
    """In-memory gh client: pr_view responses per call (queue), graphql fixed.

    `commit_check_runs` is here because `GhClient` is a `typing.Protocol` and
    `uv run mypy` — a required CI job that checks `tests/` — checks protocol
    conformance *statically*. A fake missing the method is 19 mypy errors in 2 test
    files, and a red `typecheck-python` means this feature's own "require green CI
    before updating" gate would refuse to install the commit that adds it (spec
    finding F1).
    """

    def __init__(
        self,
        *,
        pr_views: list[dict[str, object]] | None = None,
        review_threads: dict[str, object] | None = None,
        check_runs: list[dict[str, object]] | None = None,
        inline_comments: list[dict[str, object]] | None = None,
    ) -> None:
        self._inline_comments = list(inline_comments or [])
        self._pr_views = list(pr_views or [])
        self._review_threads = review_threads or {"data": {}}
        self._check_runs = list(check_runs or [])
        self.pr_view_calls = 0
        self.graphql_calls = 0
        self.check_runs_calls: list[str] = []
        self.created_prs: list[dict[str, str]] = []

    def api_user(self) -> str:
        return "testuser"

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        return []

    def pr_create(
        self, *, owner: str, repo: str, branch: str, title: str, body: str
    ) -> int:
        self.created_prs.append({"branch": branch, "title": title, "body": body})
        return 9001

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        self.pr_view_calls += 1
        if len(self._pr_views) == 1:
            return self._pr_views[0]
        return self._pr_views.pop(0)

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        self.graphql_calls += 1
        return self._review_threads

    def commit_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, object]]:
        self.check_runs_calls.append(sha)
        return list(self._check_runs)

    def pr_review_comments(
        self, *, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        return list(self._inline_comments)


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


# ---------------------------------------------------------------------------
# evaluate_commit_checks (spec 20260929T050000Z acceptance criteria 11-13)
# ---------------------------------------------------------------------------


def test_evaluate_commit_checks_accepts_rest_and_graphql_shapes() -> None:
    """Criterion 11. The commit gate reads the **REST** `check-runs` payload
    (lowercase `status`/`conclusion`) and must equally tolerate a GraphQL rollup
    (uppercase). Without case normalisation, `"completed" != "COMPLETED"` classifies
    every real REST check as pending and `self-update` defers forever — silently
    (spec finding F5)."""
    rest: list[dict[str, object]] = [
        {"name": "Lint Python", "status": "completed", "conclusion": "success"},
        {"name": "pytest", "status": "completed", "conclusion": "skipped"},
        {"name": "docs", "status": "completed", "conclusion": "neutral"},
    ]
    graphql: list[dict[str, object]] = [
        {"name": "Lint Python", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "pytest", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"name": "docs", "status": "COMPLETED", "conclusion": "NEUTRAL"},
    ]
    assert evaluate_commit_checks(rest).passed is True
    assert evaluate_commit_checks(graphql).passed is True
    # Mixed casing within one payload (and stray whitespace) is still green.
    assert (
        evaluate_commit_checks(
            [{"name": "x", "status": " Completed ", "conclusion": " Success "}]
        ).passed
        is True
    )
    # The legacy commit-status shape (state, no status) is accepted too.
    assert (
        evaluate_commit_checks([{"context": "ci", "state": "success"}]).passed is True
    )


def test_evaluate_commit_checks_defers_on_empty_pending_and_bad_conclusions() -> None:
    """Criterion 12. Three ways to fail closed, each a *defer* rather than the
    FAILURE-only semantics `evaluate_ci_checks` keeps for PRs: an empty list (a
    repo that only uses commit statuses yields nothing here — unverifiable is not
    green), anything still pending, and any conclusion outside
    {SUCCESS, SKIPPED, NEUTRAL}. For a commit about to be *deployed* a CANCELLED or
    TIMED_OUT runner is not green, which is the deliberate divergence from the PR
    gate."""
    assert evaluate_commit_checks([]).passed is False

    for status, conclusion in [
        ("in_progress", None),
        ("queued", None),
        ("COMPLETED", None),
    ]:
        verdict = evaluate_commit_checks(
            [{"name": "pytest", "status": status, "conclusion": conclusion}]
        )
        assert verdict.passed is False, (status, conclusion)

    for conclusion in [
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
        "something_new_from_a_runner_we_do_not_know",
    ]:
        verdict = evaluate_commit_checks(
            [{"name": "pytest", "status": "completed", "conclusion": conclusion}]
        )
        assert verdict.passed is False, conclusion
        assert "pytest" in (verdict.reason or "")

    # A single bad check among green ones still defers.
    assert (
        evaluate_commit_checks(
            [
                {"name": "a", "status": "completed", "conclusion": "success"},
                {"name": "b", "status": "completed", "conclusion": "cancelled"},
            ]
        ).passed
        is False
    )

    # The PR gate keeps its existing FAILURE-only semantics, unchanged.
    assert (
        evaluate_ci_checks(
            [{"name": "a", "status": "COMPLETED", "conclusion": "CANCELLED"}]
        ).passed
        is True
    )
    assert (
        evaluate_ci_checks(
            [{"name": "a", "status": "COMPLETED", "conclusion": "FAILURE"}]
        ).passed
        is False
    )


def test_gh_client_fakes_implement_commit_check_runs() -> None:
    """Criterion 13 (spec finding F1). `GhClient` is a `typing.Protocol` and the
    required `typecheck-python` CI job runs `uv run mypy` over `tests/`, so widening
    the protocol without widening the fakes that get passed to `GhClient`-annotated
    parameters makes CI red — 19 errors in 2 files, verified in a sandbox copy. That
    matters more than usual here: a red `typecheck-python` means this feature's own
    "require green CI before updating" gate defers forever and the host never
    installs the commit that adds the gate."""
    required = {name for name in dir(GhClient) if not name.startswith("_")}
    assert "commit_check_runs" in required
    for fake in (FakeGhClient, AutoFixFakeGhClient, RealGhClient):
        missing = required - {name for name in dir(fake) if not name.startswith("_")}
        assert missing == set(), f"{fake.__name__} is missing {sorted(missing)}"

    gates_fake = FakeGhClient(check_runs=[{"status": "completed"}])
    assert gates_fake.commit_check_runs(owner="o", repo="r", sha="abc") == [
        {"status": "completed"}
    ]
    assert gates_fake.check_runs_calls == ["abc"]
    assert AutoFixFakeGhClient().commit_check_runs(owner="o", repo="r", sha="abc") == []


# ---------------------------------------------------------------------------
# Gate 5 — the review skill's tier structure (issue 056 phase B, criterion 9)
# ---------------------------------------------------------------------------


def test_gate5_rejects_nonblocking_only_review() -> None:
    """The gate exists to prove the review skill's `blocking`/`non-blocking` tier
    structure is actually present. Ported verbatim from the prompt's
    `test("blocking")`, `[non-blocking]` satisfies it by substring — which is exactly
    the false positive issue 035 exists to kill, and `BLOCKING_TAG = "[blocking]"`
    exists precisely so a substring match can never stand in for the tier label.

    So the check is anchored on the literal bracketed form: a review that only ever
    writes `[non-blocking]` has not labelled a blocking tier, and must fail."""
    only_non_blocking = FakeGhClient(
        pr_views=[{"reviews": [{"body": "### non-blocking\n[non-blocking] tidy this"}]}]
    )
    verdict = run_gate5(only_non_blocking, owner="o", repo="r", pr=7)
    assert verdict.passed is False
    assert verdict.reason is not None and BLOCKING_TAG in verdict.reason

    # A real tier-structured review passes, and the check reads both surfaces
    # `gh pr view --json comments,reviews` returns.
    structured = FakeGhClient(
        pr_views=[
            {
                "comments": [{"body": "### blocking\n[blocking] gate 5 is wrong"}],
                "reviews": [{"body": "### non-blocking\n[non-blocking] rename this"}],
            }
        ]
    )
    assert run_gate5(structured, owner="o", repo="r", pr=7).passed is True

    # The bare word, unbracketed, is the substring trap itself: not a tier label.
    unbracketed = FakeGhClient(
        pr_views=[{"reviews": [{"body": "this is blocking-ish, probably fine"}]}]
    )
    assert run_gate5(unbracketed, owner="o", repo="r", pr=7).passed is False

    # No review at all is not a pass either.
    assert (
        run_gate5(FakeGhClient(pr_views=[{}]), owner="o", repo="r", pr=7).passed
        is False
    )


def test_gate5_reads_blocking_labels_from_inline_review_comments() -> None:
    """The `code-review` skill posts findings as inline comments (`**[blocking]**`) and
    a summary review body with no tier tag; `gh pr view --json comments,reviews` carries
    neither the inline bodies nor a tagged review, so the gate must fetch them itself."""
    gh = FakeGhClient(
        pr_views=[{"reviews": [{"body": "Found 1 blocking issue(s)."}, {"body": ""}]}],
        inline_comments=[{"body": "**[blocking]** this is wrong"}],
    )
    assert run_gate5(gh, owner="o", repo="r", pr=7).passed is True


def test_gate5_is_not_a_cli_stage() -> None:
    """Gate 3 stays prose by design (issue 034), so `3` must never appear in the
    CLI's `--stage` choices — its lint/pytest verdict is the implementer's own-tree
    check, with Gate CI as the authoritative one."""
    assert "3" not in GATE_STAGE_CHOICES
    assert set(GATE_STAGE_CHOICES) == {"1", "2", "4", "5", "6", "ci"}
