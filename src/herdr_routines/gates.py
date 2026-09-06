"""Gate 6 (reply coverage) and the CI gate — the two checks issues 034/035 move out of
``docs/pipeline/orchestrator-prompt.md`` prose into code.

The orchestrator is an LLM that reads the prompt and executes the gate it describes.
On PR #81 it was told to run a specific jq filter for gate 6, silently substituted a
stricter regex, and reported the gate as passed — nothing detected the swap. A gate
written as prose is advisory; an agent told "run this check" can run a different check
and still report success. These two gates ship as real functions the orchestrator can
only invoke (via ``herdr-routines gate --stage ci|6 --pr <n>``) and read an exit code
from — it cannot rewrite them.

Gate 3's lint widening (``ruff format --check .`` and ``ruff check .`` alongside
pytest) stays prompt text: it's a command the implementer runs on their own tree
before ever opening a PR, not a verdict the orchestrator computes about someone
else's PR. ``GATE3_LINT_TEST_CHECKS`` below only pins that the widened check set fails
on a lint error even with green tests (test_gate3_fails_on_lint_error) — it is reused
from the same ``run_checks`` machinery as ``auto_fix.py``, not exposed as a CLI stage.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from herdr_routines.auto_fix import (
    GateCheck,
    GateOutcome,
    GhClient,
    repo_owner_and_name,
    run_checks,
)

# Bounds the CI-gate poll (issue 034): CI on this repo finishes in well under 2
# minutes, so 10 minutes is generous headroom without letting a stuck check eat the
# orchestrator's 7h deadline.
CI_POLL_TIMEOUT_S = 600.0
CI_POLL_INTERVAL_S = 15.0

# gh's statusCheckRollup mixes two GraphQL shapes: a CheckRun's terminal outcome is
# its `conclusion` field (only meaningful once `status == "COMPLETED"`); a legacy
# commit StatusContext instead carries `state` directly, with no separate pending
# marker beyond `state == "PENDING"`.
FAILING_CI_CONCLUSIONS = frozenset({"FAILURE"})
TOLERATED_CI_CONCLUSIONS = frozenset({"SKIPPED", "NEUTRAL"})

# Anchored to the literal bracketed form so "[non-blocking]" — which contains the
# substring "blocking" but not "[blocking]" — can never satisfy this by substring
# (issue 035's secondary finding: jq's `test("blocking")` matched both).
BLOCKING_TAG = "[blocking]"

# Every unresolved thread must carry at least the original comment plus one reply.
MIN_REPLIED_COMMENT_COUNT = 2

GATE3_LINT_TEST_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(kind="command", command="uv run ruff format --check ."),
    GateCheck(kind="command", command="uv run ruff check ."),
    GateCheck(kind="command", command="uv run pytest -q"),
)


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Uniform pass/fail result for both gates: exit 0 on passed, else a legible
    reason for stderr."""

    passed: bool
    reason: str | None = None


def gate3_result(
    cwd: str, *, runner: Callable[..., tuple[int, str, str]] | None = None
) -> GateOutcome:
    """Run the widened gate-3 command set (CI parity: both ruff invocations plus
    pytest) and report pass/fail. Fails on a lint error even with a green pytest —
    the original gate ran pytest alone and never saw PR #81's ruff-format breakage."""
    return run_checks(GATE3_LINT_TEST_CHECKS, cwd=cwd, runner=runner)


def remote_owner_and_repo(repo_path: Path) -> tuple[str, str]:
    """Resolve (owner, repo) from ``origin``'s remote URL for a git checkout."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git remote get-url origin failed: {proc.stderr.strip()}")
    return repo_owner_and_name(proc.stdout.strip())


# ---------------------------------------------------------------------------
# CI gate
# ---------------------------------------------------------------------------


def _is_pending_check(check: dict[str, object]) -> bool:
    status = check.get("status")
    if status is not None:
        return status != "COMPLETED"
    return check.get("state") == "PENDING"


def _conclusion_of(check: dict[str, object]) -> str | None:
    """The terminal outcome of one check, whichever of the two rollup shapes it is."""
    conclusion = check.get("conclusion")
    if conclusion is not None:
        return str(conclusion)
    state = check.get("state")
    return str(state) if state is not None else None


def evaluate_ci_checks(checks: Sequence[dict[str, object]]) -> GateVerdict:
    """Evaluate an already-settled statusCheckRollup: fail on any FAILURE conclusion,
    tolerate SKIPPED and NEUTRAL (and everything else that isn't FAILURE)."""
    failing = [c for c in checks if _conclusion_of(c) in FAILING_CI_CONCLUSIONS]
    if not failing:
        return GateVerdict(passed=True)
    names = ", ".join(
        str(c.get("name") or c.get("context") or "unknown") for c in failing
    )
    return GateVerdict(passed=False, reason=f"CI check(s) failed: {names}")


def run_ci_gate(
    gh: GhClient,
    *,
    owner: str,
    repo: str,
    pr: int,
    timeout_s: float = CI_POLL_TIMEOUT_S,
    poll_interval_s: float = CI_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> GateVerdict:
    """Poll ``gh pr view --json statusCheckRollup`` until every check is non-pending,
    then evaluate. A FAILURE conclusion routes the PR to stage 6 as a must-fix item
    (it is not itself a pipeline abort — a lint slip is exactly what stage 6 exists to
    clean up); this function only reports pass/fail, the caller decides what to do
    with it. Bounded to `timeout_s` so a stuck check can't eat the deadline."""
    deadline = clock() + timeout_s
    while True:
        view = gh.pr_view(owner=owner, repo=repo, number=pr)
        raw_checks = view.get("statusCheckRollup")
        checks = (
            [c for c in raw_checks if isinstance(c, dict)]
            if isinstance(raw_checks, list)
            else []
        )

        if not checks or all(not _is_pending_check(c) for c in checks):
            return evaluate_ci_checks(checks)

        if clock() >= deadline:
            return GateVerdict(
                passed=False,
                reason=f"CI checks still pending after {timeout_s:.0f}s",
            )
        sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# Gate 6 — reply coverage
# ---------------------------------------------------------------------------

GATE6_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 50) {
        nodes {
          isResolved
          comments(first: 100) {
            totalCount
            nodes { body }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class ReviewThread:
    is_resolved: bool
    comment_count: int
    first_comment_body: str


def _parse_review_threads(data: dict[str, object]) -> list[ReviewThread]:
    repo_data = data.get("data", {})
    pr_data = repo_data.get("repository", {}) if isinstance(repo_data, dict) else {}
    pull_request = pr_data.get("pullRequest", {}) if isinstance(pr_data, dict) else {}
    threads = (
        pull_request.get("reviewThreads", {}) if isinstance(pull_request, dict) else {}
    )
    nodes = threads.get("nodes", []) if isinstance(threads, dict) else []
    if not isinstance(nodes, list):
        return []

    result: list[ReviewThread] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        comments = node.get("comments", {})
        comment_nodes = comments.get("nodes", []) if isinstance(comments, dict) else []
        total_count = (
            comments.get("totalCount", len(comment_nodes))
            if isinstance(comments, dict)
            else 0
        )
        first_body = ""
        if isinstance(comment_nodes, list) and comment_nodes:
            first = comment_nodes[0]
            if isinstance(first, dict):
                first_body = str(first.get("body", ""))
        result.append(
            ReviewThread(
                is_resolved=bool(node.get("isResolved")),
                comment_count=int(total_count) if isinstance(total_count, int) else 0,
                first_comment_body=first_body,
            )
        )
    return result


def evaluate_gate6(threads: Sequence[ReviewThread]) -> GateVerdict:
    """Reply coverage: every unresolved thread must carry at least one reply. Kept
    separate from — and in addition to — the blocking-tag check: neither the old
    prose gate nor its stricter substitute counted replies at all (issue 035)."""
    unresolved = [t for t in threads if not t.is_resolved]
    unreplied = [t for t in unresolved if t.comment_count < MIN_REPLIED_COMMENT_COUNT]
    blocking = [t for t in unresolved if BLOCKING_TAG in t.first_comment_body]

    if not unreplied and not blocking:
        return GateVerdict(passed=True)

    reasons: list[str] = []
    if unreplied:
        reasons.append(f"{len(unreplied)} unresolved thread(s) with no reply")
    if blocking:
        reasons.append(
            f"{len(blocking)} unresolved thread(s) still tagged {BLOCKING_TAG}"
        )
    return GateVerdict(passed=False, reason="; ".join(reasons))


def run_gate6(gh: GhClient, *, owner: str, repo: str, pr: int) -> GateVerdict:
    """Fetch review threads via GraphQL and evaluate reply coverage. `gh api
    repos/.../threads` 404s (verified against a live PR) — GraphQL is the only
    route to per-thread resolution state."""
    data = gh.graphql(GATE6_REVIEW_THREADS_QUERY, owner=owner, repo=repo, pr=str(pr))
    threads = _parse_review_threads(data)
    return evaluate_gate6(threads)
