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
from collections.abc import Callable, Mapping, Sequence
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

# The complement for a *commit* self-update is about to deploy (spec
# 20260929T050000Z). `evaluate_ci_checks` above tolerates everything that is not
# FAILURE, which is right for a PR whose gate is about authoring a fix; it is wrong
# here, because a CANCELLED / TIMED_OUT / ACTION_REQUIRED / STALE runner is not
# green, and an unrecognised conclusion string is not evidence of anything. So this
# set is an allowlist, not a denylist.
ACCEPTED_COMMIT_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})

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


# ---------------------------------------------------------------------------
# Commit gate — the stricter sibling evaluate_ci_checks defers on
# ---------------------------------------------------------------------------


def _normalise_token(value: object) -> str | None:
    """Uppercase, whitespace-trimmed copy of one API token, or None if absent.

    The REST check-runs API speaks lowercase (`"completed"`, `"success"`) and the
    GraphQL rollup speaks uppercase (`"COMPLETED"`, `"SUCCESS"`). Comparing a raw
    value against the uppercase constants above therefore classifies *every* real
    REST check as pending.
    """
    if value is None:
        return None
    return str(value).strip().upper()


def _is_pending_check_normalised(check: dict[str, object]) -> bool:
    status = _normalise_token(check.get("status"))
    if status is not None:
        return status != "COMPLETED"
    return _normalise_token(check.get("state")) == "PENDING"


def _conclusion_of_normalised(check: dict[str, object]) -> str | None:
    return _normalise_token(check.get("conclusion") or check.get("state"))


def _check_name(check: dict[str, object]) -> str:
    return str(check.get("name") or check.get("context") or "unknown")


def evaluate_commit_checks(checks: Sequence[dict[str, object]]) -> GateVerdict:
    """Is this *commit* green enough to install? Case-normalised; safe for both the
    REST check-runs shape and the GraphQL rollup shape.

    Deliberately stricter than `evaluate_ci_checks`, which fails only on `FAILURE`
    and is right for a PR the orchestrator is about to fix up. A commit is
    different: the host is about to *deploy* it, so

    - an empty list defers — an unverifiable commit is not a green one. Note REST
      check-runs omit legacy commit statuses, so a statuses-only repo lands here,
      which is the correct fail-closed answer;
    - any still-pending check defers;
    - any conclusion outside {SUCCESS, SKIPPED, NEUTRAL} defers, so a CANCELLED /
      TIMED_OUT / ACTION_REQUIRED / STALE runner (or an unrecognised string from a
      runner we don't know) is not mistaken for green.

    Every failure is a *defer* (exit 0, nothing changed), never an error: the
    human merge to main is the real gate, and this only keeps the host off
    something red (spec 20260929T050000Z finding F5).
    """
    if not checks:
        return GateVerdict(passed=False, reason="no check runs reported for the commit")

    pending = [c for c in checks if _is_pending_check_normalised(c)]
    if pending:
        names = ", ".join(_check_name(c) for c in pending)
        return GateVerdict(passed=False, reason=f"check(s) still pending: {names}")

    unaccepted = [
        c
        for c in checks
        if _conclusion_of_normalised(c) not in ACCEPTED_COMMIT_CONCLUSIONS
    ]
    if unaccepted:
        names = ", ".join(
            f"{_check_name(c)} ({_conclusion_of_normalised(c)})" for c in unaccepted
        )
        return GateVerdict(passed=False, reason=f"check(s) not green: {names}")

    return GateVerdict(passed=True)


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


# ---------------------------------------------------------------------------
# Gates 1, 2, 4, 5 — issue 056 phase B, the checks the orchestrator prompt used to
# describe in prose. Same reason as gates 6/CI above: prose is advisory, and an agent
# told "run this check" can run a different one and still report success.
# ---------------------------------------------------------------------------

# Every long-running agent command goes through this wrapper so an external `timeout`
# (exit 124) is always distinguishable from the command's own non-zero exit. Copied from
# runner's `_subprocess_runner`, kept here rather than imported because gates must be
# callable without a Job and this module's whole job is to be the leaf an agent can only
# invoke.
SUBPROCESS_TIMEOUT_GRACE_S = 5.0


def _run_bounded(argv: Sequence[str], *, timeout_s: float) -> tuple[int, str, str]:
    """Run `argv` with a hard wall-clock bound; 124 on overrun, never an exception for
    the overrun itself (a `TimeoutExpired` here would abort the gate's caller instead of
    producing the verdict the pipeline acts on)."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_s + SUBPROCESS_TIMEOUT_GRACE_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(argv)}: timed out after {timeout_s:.0f}s"
    except OSError as e:
        return 127, "", f"{' '.join(argv)}: {e}"
    return proc.returncode, proc.stdout, proc.stderr


def run_gate1(
    repo_path: Path, *, runner: Callable[..., tuple[int, str, str]] | None = None
) -> GateVerdict:
    """Issue exists, is unclaimed and still open — the check that stops a night's work
    being spent on something a human closed or claimed in the meantime.

    The claim is `status: open` plus no `claimed_by:` line, matching
    `pick_feature.pick_issue` (issue 054) so this can never pass a run that pick would
    have skipped. Read with plain text matching rather than a YAML parser on purpose:
    the issue file is a template this repo controls, and a parse error in a single issue
    must fail this gate loudly rather than silently select a feature."""
    del runner  # gate 1 is pure filesystem work; no subprocess to bound
    issue_files = sorted(repo_path.glob("docs/process/issues/*.md"))
    if not issue_files:
        return GateVerdict(passed=False, reason="no issue files found")

    open_issues: list[Path] = []
    for path in issue_files:
        try:
            text = path.read_text()
        except OSError as e:
            return GateVerdict(passed=False, reason=f"could not read {path.name}: {e}")
        if not _frontmatter_has(text, "status", "open"):
            continue
        if _frontmatter_field(text, "claimed_by") is not None:
            continue
        open_issues.append(path)

    if not open_issues:
        return GateVerdict(
            passed=False,
            reason="no open unclaimed issue in docs/process/issues",
        )
    return GateVerdict(passed=True, reason=f"feature: {open_issues[0].stem}")


def _frontmatter_block(text: str) -> str | None:
    """The YAML frontmatter block's text, or None when the file has none."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    return None if end == -1 else text[3:end]


def _frontmatter_field(text: str, field: str) -> str | None:
    block = _frontmatter_block(text)
    if block is None:
        return None
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == field:
            return value.strip().strip("\"'") or None
    return None


def _frontmatter_has(text: str, field: str, value: str) -> bool:
    return _frontmatter_field(text, field) == value


def run_gate2(
    repo_path: Path, *, runner: Callable[..., tuple[int, str, str]] | None = None
) -> GateVerdict:
    """Worktree is clean, on the expected branch, synced with origin, and has no
    unpushed commits. Each of those has bitten a real run: a dirty tree means the PR
    would carry whatever was in the working directory, an unsynced base means the
    pipeline fixes last night's already-fixed problem, and an unpushed commit means the
    agent's work is only on this host until someone pushes by hand.

    Uses `git status --porcelain`, which reports untracked files too (`-uno` would let a
    stray scratch file into the PR)."""
    run = runner or _run_bounded

    def git(*args: str) -> tuple[int, str, str]:
        return run(["git", "-C", str(repo_path), *args], timeout_s=60)

    code, stdout, stderr = git("status", "--porcelain")
    if code != 0:
        return GateVerdict(passed=False, reason=f"git status failed: {stderr.strip()}")
    if stdout.strip():
        first = stdout.strip().splitlines()[0]
        return GateVerdict(passed=False, reason=f"worktree is dirty: {first}")

    code, stdout, stderr = git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return GateVerdict(
            passed=False, reason=f"git rev-parse HEAD failed: {stderr.strip()}"
        )
    branch = stdout.strip()
    if not branch.startswith("auto/pipeline-"):
        return GateVerdict(
            passed=False, reason=f"not on a pipeline branch (on {branch!r})"
        )

    code, _, stderr = git("fetch", "origin", "--quiet")
    if code != 0:
        return GateVerdict(passed=False, reason=f"git fetch failed: {stderr.strip()}")

    code, stdout, stderr = git(
        "rev-list", "--left-right", "--count", "HEAD...@{upstream}"
    )
    if code != 0:
        return GateVerdict(
            passed=False,
            reason=f"no upstream to compare against: {stderr.strip()}",
        )
    ahead, _, behind = (part.strip() for part in stdout.split()[:2]) + ("",)[:0]
    if int(ahead or 0) > 0:
        return GateVerdict(
            passed=False, reason=f"{ahead} unpushed commit(s) on {branch}"
        )
    if int(behind or 0) > 0:
        return GateVerdict(
            passed=False,
            reason=f"branch is {behind} commit(s) behind its upstream",
        )
    return GateVerdict(passed=True)


def run_gate4(gh: GhClient, *, owner: str, repo: str, branch: str) -> GateVerdict:
    """The PR stage 4 opened is live and pointed at this run's branch.

    `gh pr create` exits 0 after printing a URL, and it exits 0 for a PR that a
    same-run re-invocation has already opened — so exit code alone cannot prove a PR
    exists, and cannot prove it is the one this run means. Re-reading the branch's
    open PR and checking its head is the only check that survives both."""
    procs = gh.pr_list(owner=owner, repo=repo, state="open", limit=100)
    matches = [pr for pr in procs if str(pr.get("headRefName", "")) == branch]
    if not matches:
        return GateVerdict(passed=False, reason=f"no open PR with head {branch!r}")
    numbers = sorted(int(pr["number"]) for pr in matches if "number" in pr)
    if not numbers:
        return GateVerdict(passed=False, reason=f"open PR on {branch!r} has no number")
    return GateVerdict(passed=True, reason=f"PR #{numbers[-1]}")


def _pr_bodies(view: Mapping[str, object]) -> list[str]:
    """Every comment and review body `gh pr view --json comments,reviews` returns.

    Both surfaces, because gate 5 checks that the review *skill* ran: a reviewer that
    posted its findings as a review leaves them in `reviews`, and one that commented
    inline leaves them in `comments`."""
    bodies: list[str] = []
    for key in ("comments", "reviews"):
        entries = view.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("body"), str):
                bodies.append(entry["body"])
    return bodies


def run_gate5(gh: GhClient, *, owner: str, repo: str, pr: int) -> GateVerdict:
    """The review skill's `blocking`/`non-blocking` tier structure is present.

    Anchored on the literal bracketed ``[blocking]``, never a substring test. The prompt's
    original check was ``test("blocking")``, which ``[non-blocking]`` satisfies — the
    exact false positive issue 035 exists to kill, and the reason `BLOCKING_TAG` above is
    a bracketed constant. A review that only ever writes `[non-blocking]` has not labelled
    a blocking tier, so it is not evidence the skill's structure ran.

    Note this is deliberately *stricter* than the prose it replaces: prose asked for
    "at least one ``[blocking]`` or ``[non-blocking]`` label", which `[non-blocking]`
    alone satisfies, and so keeps the bug. See the spec's note on the prose/acceptance-
    criteria conflict."""
    view = gh.pr_view(owner=owner, repo=repo, number=pr)
    bodies = _pr_bodies(view)
    if not bodies:
        return GateVerdict(
            passed=False,
            reason=f"PR #{pr} has no review or comment to check for tier labels",
        )
    if any(BLOCKING_TAG in body for body in bodies):
        return GateVerdict(passed=True)
    return GateVerdict(
        passed=False,
        reason=(
            f"no {BLOCKING_TAG} tier label in any of PR #{pr}'s "
            f"{len(bodies)} review/comment body/bodies — the review skill's tier "
            f"structure is unproven"
        ),
    )


# CLI-visible stage numbers. Gate 3 is prose by design (see this module's docstring and
# GATE3_LINT_TEST_CHECKS), so "3" is absent on purpose: gate 3's verdict is the
# implementer's own-tree check, and Gate CI is the authoritative one for a PR.
GATE_STAGE_CHOICES: tuple[str, ...] = ("1", "2", "4", "5", "6", "ci")


def run_stage_gate(
    stage: str,
    *,
    repo_path: Path,
    gh: GhClient,
    owner: str,
    repo: str,
    pr: int | None = None,
    branch: str | None = None,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> GateVerdict:
    """Dispatch one gate by its CLI stage token. Raises `ValueError` for an unknown token
    so the CLI can exit 2 rather than reporting an unrun gate as a pass."""
    token = stage.strip().lower()
    if token == "1":
        return run_gate1(repo_path, runner=runner)
    if token == "2":
        return run_gate2(repo_path, runner=runner)
    if token == "4":
        if branch is None:
            raise ValueError("gate 4 requires the branch the PR was opened from")
        return run_gate4(gh, owner=owner, repo=repo, branch=branch)
    if token == "5":
        if pr is None:
            raise ValueError("gate 5 requires a PR number")
        return run_gate5(gh, owner=owner, repo=repo, pr=pr)
    if token == "6":
        if pr is None:
            raise ValueError("gate 6 requires a PR number")
        return run_gate6(gh, owner=owner, repo=repo, pr=pr)
    if token == "ci":
        if pr is None:
            raise ValueError("gate ci requires a PR number")
        return run_ci_gate(gh, owner=owner, repo=repo, pr=pr)
    raise ValueError(
        f"unknown gate stage {stage!r} (expected one of {', '.join(GATE_STAGE_CHOICES)})"
    )
