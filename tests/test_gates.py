"""Tests for gates.py: the CI gate and gate 6 (reply coverage), pinned per the
acceptance criteria in docs/process/issues/034-gate-ci-checks.md and
docs/process/issues/035-gate6-reply-coverage.md, plus `evaluate_commit_checks` — the
stricter, case-normalised gate `self-update` puts in front of a commit it is about to
deploy (spec 20260929T050000Z finding F5).
"""

from __future__ import annotations

from collections.abc import Sequence

from herdr_routines.auto_fix import GhClient, PostedBody, RealGhClient, ReviewSearch
from herdr_routines.gates import (
    BLOCKING_TAG,
    GATE3_LINT_TEST_CHECKS,
    GATE_STAGE_CHOICES,
    ReviewThread,
    evaluate_ci_checks,
    evaluate_commit_checks,
    evaluate_gate7,
    gate3_result,
    run_ci_gate,
    run_gate6,
    run_gate7,
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

    # issue 055 widened the GhClient protocol with the review-search trio; these stubs
    # keep the static (mypy) and runtime protocol-conformance checks green.
    def search_prs(self, modes: Sequence[str], limit: int = 100) -> ReviewSearch | None:
        return None

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[PostedBody] | None:
        return None

    def repo_info(self, *, owner: str, repo: str) -> dict[str, object] | None:
        return None


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


def test_pipeline_ci_failure_routes_to_stage7() -> None:
    """A FAILURE conclusion fails the CI gate rather than being silently accepted —
    this is the signal the orchestrator routes into stage 7 as a must-fix item
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
    verdict = evaluate_gate7(threads)
    assert verdict.passed is False
    assert "no reply" in (verdict.reason or "")


def test_gate6_passes_on_replied_nonblocking() -> None:
    threads = [
        ReviewThread(
            is_resolved=False, comment_count=2, first_comment_body="[non-blocking] fyi"
        )
    ]
    verdict = evaluate_gate7(threads)
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
    assert evaluate_gate7(blocking_replied).passed is False

    non_blocking_replied = [
        ReviewThread(
            is_resolved=False, comment_count=2, first_comment_body="[non-blocking] fyi"
        )
    ]
    assert evaluate_gate7(non_blocking_replied).passed is True


def test_gate6_resolved_threads_are_ignored() -> None:
    threads = [
        ReviewThread(
            is_resolved=True, comment_count=1, first_comment_body="[blocking] old"
        )
    ]
    assert evaluate_gate7(threads).passed is True


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
    verdict = run_gate7(gh, owner="o", repo="r", pr=81)
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
    verdict = run_gate6(only_non_blocking, owner="o", repo="r", pr=7)
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
    assert run_gate6(structured, owner="o", repo="r", pr=7).passed is True

    # The bare word, unbracketed, is the substring trap itself: not a tier label.
    unbracketed = FakeGhClient(
        pr_views=[{"reviews": [{"body": "this is blocking-ish, probably fine"}]}]
    )
    assert run_gate6(unbracketed, owner="o", repo="r", pr=7).passed is False

    # No review at all is not a pass either.
    assert (
        run_gate6(FakeGhClient(pr_views=[{}]), owner="o", repo="r", pr=7).passed
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
    assert run_gate6(gh, owner="o", repo="r", pr=7).passed is True


def test_gate6_ignores_tier_labels_from_untrusted_authors() -> None:
    """With a trusted set, a `[blocking]` label only counts when a trusted author wrote
    it, on both the `gh pr view` surfaces (``author.login``) and inline comments
    (REST ``user.login``). A deleted account (no login) is never trusted. Without a set
    (the standalone CLI), every author still counts, like gate 7."""
    trusted = frozenset({"owner"})
    untrusted_only = FakeGhClient(
        pr_views=[
            {
                "reviews": [
                    {"author": {"login": "drive-by"}, "body": "[blocking] fake tier"},
                    {"author": None, "body": "[blocking] ghost tier"},
                ],
                "comments": [{"author": {"login": "owner"}, "body": "no tier here"}],
            }
        ],
        inline_comments=[{"user": {"login": "drive-by"}, "body": "**[blocking]** x"}],
    )
    verdict = run_gate6(
        untrusted_only, owner="o", repo="r", pr=7, trusted_authors=trusted
    )
    assert verdict.passed is False
    assert run_gate6(untrusted_only, owner="o", repo="r", pr=7).passed is True

    trusted_inline = FakeGhClient(
        pr_views=[{"reviews": [{"author": {"login": "owner"}, "body": "summary"}]}],
        inline_comments=[{"user": {"login": "Owner"}, "body": "**[blocking]** x"}],
    )
    assert (
        run_gate6(
            trusted_inline, owner="o", repo="r", pr=7, trusted_authors=trusted
        ).passed
        is True
    )

    nobody_trusted = FakeGhClient(
        pr_views=[{"reviews": [{"author": {"login": "drive-by"}, "body": "hi"}]}]
    )
    reason = run_gate6(
        nobody_trusted, owner="o", repo="r", pr=7, trusted_authors=trusted
    ).reason
    assert reason is not None and "trusted author" in reason


# ---------------------------------------------------------------------------
# Gate 6's second evidence type — the review skill's summary phrase (issue 069,
# spec docs/pipeline/runs/20261023T050000Z/spec.md)
# ---------------------------------------------------------------------------


def test_gate6_passes_clean_review_with_only_non_blocking_notes() -> None:
    """Criterion 1 — a review that finds nothing blocking must pass. The fixture copies
    PR #211's real shape (run 20261021T050000Z): a summary review body stating the
    blocking count ("Found 0 blocking issue(s) and 4 non-blocking note(s)") plus
    4 inline `**[non-blocking]**` comments, all from a trusted author. No body holds
    the literal `[blocking]`, which is exactly why the pre-069 gate failed a clean
    review and left its 4 threads unanswered for a human to resolve."""
    gh = FakeGhClient(
        pr_views=[
            {
                "reviews": [
                    {
                        "author": {"login": "owner"},
                        "body": "Found 0 blocking issue(s) and 4 non-blocking note(s).",
                    }
                ],
            }
        ],
        inline_comments=[
            {"user": {"login": "owner"}, "body": f"**[non-blocking]** note {n}"}
            for n in range(4)
        ],
    )
    verdict = run_gate6(
        gh, owner="o", repo="r", pr=211, trusted_authors=frozenset({"owner"})
    )
    assert verdict.passed is True


def test_gate6_still_rejects_bare_non_blocking_without_summary() -> None:
    """Criterion 2 — issue 035's protection is kept. Only bare `[non-blocking]` labels
    on every surface and no `Found N blocking issue(s)` summary: neither evidence
    type, so the gate still fails — and the verdict reason still names `[blocking]`
    (the only gate-6 reason assertion in the suite keeps `BLOCKING_TAG in
    verdict.reason`)."""
    gh = FakeGhClient(
        pr_views=[{"reviews": [{"body": "[non-blocking] tidy this"}]}],
        inline_comments=[{"body": "**[non-blocking]** rename this"}],
    )
    verdict = run_gate6(gh, owner="o", repo="r", pr=7)
    assert verdict.passed is False
    assert verdict.reason is not None and BLOCKING_TAG in verdict.reason


def test_gate6_passes_zero_findings_review() -> None:
    """Criterion 3 — the zero-findings review (summary only, no tier tags anywhere)
    passes gate 6, and `evaluate_gate7` on an empty thread list passes in the same
    test: with no findings there is nothing for stage 7 to answer, so gate 6 must not
    be the step standing between a clean review and a resolved PR."""
    gh = FakeGhClient(
        pr_views=[
            {
                "reviews": [
                    {
                        "body": "Review complete. Found 0 blocking issue(s) and 0 "
                        "non-blocking note(s).",
                    }
                ],
            }
        ],
    )
    assert run_gate6(gh, owner="o", repo="r", pr=313).passed is True
    assert evaluate_gate7([]).passed is True


def test_gate6_summary_match_is_anchored_not_substring() -> None:
    """Criterion 4 — the second evidence type is a compiled anchored regex on the
    `code-review` skill's exact summary phrase, pinned as a module constant
    `BLOCKING_SUMMARY_RE` from `herdr_routines.gates` (beside `BLOCKING_TAG`), not a
    substring test: prose merely containing the word `blocking`, unbracketed or
    reworded, matches neither evidence and still fails the gate.
    (Fetched via ``getattr`` so `uv run mypy` over ``tests/`` — a required CI job —
    stays green until the constant lands, leaving pytest as the red signal.)"""
    import herdr_routines.gates as gates_module

    summary_re = getattr(gates_module, "BLOCKING_SUMMARY_RE", None)
    assert summary_re is not None, "BLOCKING_SUMMARY_RE module constant missing"
    assert summary_re.pattern == r"Found \d+ blocking issue\(s\)"
    assert summary_re.search("Found 0 blocking issue(s) and 4 non-blocking note(s).")
    assert summary_re.search("Found 1 blocking issue(s).")
    for prose in (
        "all blocking concerns cleared",
        "nothing found, no blocking notes",
        "[non-blocking] tier label only",
        "this review found nothing",
    ):
        assert summary_re.search(prose) is None, prose

    prose_only = FakeGhClient(
        pr_views=[
            {"reviews": [{"body": "all blocking concerns cleared, nothing found"}]}
        ]
    )
    assert run_gate6(prose_only, owner="o", repo="r", pr=7).passed is False


def test_gate6_ignores_summary_from_untrusted_authors() -> None:
    """Criterion 5 — trusted-author scoping is inherited for the summary evidence
    too, on both surfaces (`gh pr view` rows via ``author.login`` and REST inline
    comments via ``user.login``), mirroring
    test_gate6_ignores_tier_labels_from_untrusted_authors: a drive-by quoting the
    skill's summary line, or a deleted "ghost" author, is not evidence; without a
    set (the standalone CLI) every author still counts."""
    trusted = frozenset({"owner"})
    untrusted_summary = FakeGhClient(
        pr_views=[
            {
                "reviews": [
                    {
                        "author": {"login": "drive-by"},
                        "body": "Found 0 blocking issue(s) and 2 non-blocking note(s).",
                    },
                    {"author": None, "body": "Found 1 blocking issue(s)."},
                ],
            }
        ],
        inline_comments=[
            {"user": {"login": "drive-by"}, "body": "Found 2 blocking issue(s)."}
        ],
    )
    verdict = run_gate6(
        untrusted_summary, owner="o", repo="r", pr=7, trusted_authors=trusted
    )
    assert verdict.passed is False
    assert run_gate6(untrusted_summary, owner="o", repo="r", pr=7).passed is True

    trusted_summary = FakeGhClient(
        pr_views=[
            {
                "reviews": [
                    {
                        "author": {"login": "owner"},
                        "body": "Found 0 blocking issue(s) and 1 non-blocking note(s).",
                    }
                ],
            }
        ],
        inline_comments=[
            {"user": {"login": "Owner"}, "body": "Found 0 blocking issue(s)."}
        ],
    )
    assert (
        run_gate6(
            trusted_summary, owner="o", repo="r", pr=7, trusted_authors=trusted
        ).passed
        is True
    )


def test_gates_3_and_4_are_not_cli_stages() -> None:
    """Gates 3 and 4 are git checks `pipeline_run` runs on its own worktree (the test
    author touched only tests/, the implementer removed no named test), so `3` and `4`
    must never appear in the CLI's `--stage` choices. Every other stage keeps its own
    number as its gate token (issue 059 renumbered them 1-7)."""
    assert "3" not in GATE_STAGE_CHOICES
    assert "4" not in GATE_STAGE_CHOICES
    assert set(GATE_STAGE_CHOICES) == {"1", "2", "5", "6", "7", "ci"}


def test_evaluate_gate7_scopes_to_trusted_authors_when_given() -> None:
    threads = [
        ReviewThread(
            is_resolved=False,
            comment_count=1,
            first_comment_body="[blocking] unanswered",
            first_comment_author="drive-by",
        ),
        ReviewThread(
            is_resolved=False,
            comment_count=2,
            first_comment_body="answered",
            first_comment_author="Owner",
        ),
    ]
    # Without a trusted set every thread counts (the standalone `gate 7` CLI).
    assert evaluate_gate7(threads).passed is False
    assert evaluate_gate7(threads, trusted_authors=frozenset({"owner"})).passed is True
    # A thread whose author is unknown is never in scope of a trusted-only check.
    ghost = [ReviewThread(is_resolved=False, comment_count=1, first_comment_body="x")]
    assert evaluate_gate7(ghost, trusted_authors=frozenset({"owner"})).passed is True
