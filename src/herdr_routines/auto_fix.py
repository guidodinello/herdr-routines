"""Auto-fix PR standing job: enumerate open routine-owned PRs with failing CI or
unresolved review threads, dispatch bounded fix workers.

Pure-ish module: no subprocess except via injected GhClient, so unit-testable with
frozen now and fixture history. See docs/pipeline/runs/20260829T050025Z/spec.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import subprocess
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, assert_never

from herdr_routines.config import GateCheck, GateCheckKind
from herdr_routines.findings import Finding
from herdr_routines.history import read_job

log = logging.getLogger(__name__)

FAILING_CI_STATES = frozenset({"FAILURE", "ERROR", "TIMED_OUT"})
TIMEOUT_EXIT_CODE = 124

# The branch every pipeline PR is opened against (the pipeline's human merge gate is
# `main`; `pipeline-launch.sh` never pushed elsewhere). A module constant rather than a
# parameter because there is exactly one caller (stage 5) and one correct answer.
DEFAULT_PR_BASE = "main"
# Only count real fix attempts toward the retry budget — skipped records the tick
# appends each time a PR exceeds max_attempts must not increment the counter,
# otherwise a fixed-then-broken PR is permanently abandoned (review finding F).
_COUNTABLE_STATES = frozenset({"done", "failed"})


class GhClient(Protocol):
    """Abstracts gh CLI calls for testability."""

    def api_user(self) -> str:
        """Return the authenticated user's login."""
        ...

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        """Return open PRs with number, headRefName, author, url, labels (labels are
        what a `github_event` `prs:labels` poll matches on)."""
        ...

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        """Return PR details including statusCheckRollup."""
        ...

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        """Execute a GraphQL query via gh api graphql."""
        ...

    def commit_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, object]]:
        """Return the check runs reported for one commit (REST shape: lowercase
        ``status``/``conclusion``). Raises ``RuntimeError`` when the query fails —
        callers must treat that as "unverifiable", not as "green"."""
        ...

    def pr_review_comments(
        self, *, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        """Every inline review comment on the PR (REST ``pulls/{n}/comments``). Raises
        ``RuntimeError`` when the query fails. Gate 6 needs this because the
        ``code-review`` skill puts its ``[blocking]`` tags on inline comments, which
        ``gh pr view --json comments,reviews`` never returns."""
        ...

    def pr_create(
        self, *, owner: str, repo: str, branch: str, title: str, body: str
    ) -> int:
        """Open a PR from ``branch`` and return its number. Raises ``RuntimeError`` on
        any failure — stage 5 of the pipeline (issue 056 phase C) turns that into a
        gate-5 failure hours later, so it must never be a silent no-op."""
        ...

    def search_prs(self, modes: Sequence[str], limit: int = 100) -> ReviewSearch | None:
        """One batched ``gh api graphql`` search, one alias per selected mode. Returns
        ``None`` on any failure (fail closed — an unknown answer must never mean
        "nothing to review")."""
        ...

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[str] | None:
        """The PR's posted review bodies **and** issue comments, concatenated (one
        ``gh pr view --json comments,reviews`` call). Returns ``None`` on any failure;
        every caller treats ``None`` as *unverified*, never as "empty"."""
        ...

    def repo_info(self, *, owner: str, repo: str) -> dict[str, object] | None:
        """``gh api repos/{owner}/{repo}`` → a mapping carrying at least ``clone_url``
        and ``default_branch``. ``None`` on any failure."""
        ...


@dataclass(frozen=True, slots=True)
class PRInfo:
    """Minimal PR info for eligibility checking."""

    number: int
    head_ref: str
    author: str
    url: str


@dataclass(frozen=True, slots=True)
class ReviewPR:
    """One open PR returned by a review search, before skip/guard filtering."""

    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    head_ref: str
    author: str
    repo: str  # base repo "owner/name" (repository.nameWithOwner)
    head_repo: str  # head repo "owner/name", or "" when unknown
    mode: (
        str  # the selected mode alias that returned it ("author" | "review_requested")
    )


@dataclass(frozen=True, slots=True)
class ReviewSearch:
    """The batched search result: nodes plus the modes that hit GitHub's 100-node
    ceiling (recorded as ``search_truncated`` — phase A does not paginate)."""

    prs: tuple[ReviewPR, ...]
    truncated_modes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EligiblePR:
    """A PR confirmed eligible for auto-fix."""

    pr: PRInfo
    reason: str  # "ci_failure", "unresolved_threads", or "both"


class RealGhClient:
    """Subprocess-based gh CLI implementation."""

    def __init__(self, *, timeout_s: float = 30) -> None:
        self._timeout_s = timeout_s

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124, "", "gh timed out"
        return proc.returncode, proc.stdout, proc.stderr

    def api_user(self) -> str:
        exit_code, stdout, stderr = self._run(["gh", "api", "user", "--jq", ".login"])
        if exit_code != 0:
            raise RuntimeError(f"gh auth failed: {stderr.strip()}")
        login = stdout.strip()
        if not login:
            raise RuntimeError("gh auth returned empty login")
        return login

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, object]]:
        exit_code, stdout, stderr = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                f"{owner}/{repo}",
                "--state",
                state,
                "--limit",
                str(limit),
                "--json",
                "number,headRefName,author,url,labels,isCrossRepository",
            ]
        )
        if exit_code != 0:
            raise RuntimeError(f"gh pr list failed: {stderr.strip()}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [p for p in payload if isinstance(p, dict)]

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        exit_code, stdout, stderr = self._run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                # `state`/`url` for stage 5's gate 5 (issue 056 phase C), which has to
                # confirm the PR it just opened is the live one on the right head;
                # `comments`/`reviews` for gate 6's tier-label check. Both are additive:
                # `statusCheckRollup` is still there for the CI gate.
                "--json",
                "statusCheckRollup,headRefName,state,url,comments,reviews",
            ]
        )
        if exit_code != 0:
            raise RuntimeError(f"gh pr view {number} failed: {stderr.strip()}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def pr_review_comments(
        self, *, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        """`--paginate --slurp` so a PR with more than one page of inline comments comes
        back as one JSON array of per-page arrays, which is flattened here."""
        exit_code, stdout, stderr = self._run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{owner}/{repo}/pulls/{number}/comments",
            ]
        )
        if exit_code != 0:
            raise RuntimeError(
                f"gh api pulls/{number}/comments failed: {stderr.strip()}"
            )
        try:
            pages = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(pages, list):
            return []
        return [
            c
            for page in pages
            for c in (page if isinstance(page, list) else [])
            if isinstance(c, dict)
        ]

    def pr_create(
        self, *, owner: str, repo: str, branch: str, title: str, body: str
    ) -> int:
        """`gh pr create` and parse the PR number back out of the URL gh prints.

        `gh pr create` has no machine-readable output mode; it prints the new PR's
        URL on stdout, so the number is read from that rather than predicted (the same
        "never invent an id" rule `herdr.py` follows for every id it hands back)."""
        exit_code, stdout, stderr = self._run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                f"{owner}/{repo}",
                "--base",
                DEFAULT_PR_BASE,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        if exit_code != 0:
            raise RuntimeError(f"gh pr create failed: {stderr.strip()}")
        match = re.search(r"/pull/(\d+)", stdout)
        if match is None:
            raise RuntimeError(f"gh pr create printed no PR url: {stdout.strip()!r}")
        return int(match.group(1))

    def search_prs(self, modes: Sequence[str], limit: int = 100) -> ReviewSearch | None:
        aliases = [m for m in modes if m in REVIEW_SEARCH_QUALIFIERS]
        if not aliases:
            return ReviewSearch(prs=())
        var_decls = ", ".join(f"${m}: String!" for m in aliases)
        selections = "\n".join(
            f"{m}: search(query: ${m}, type: ISSUE, first: {limit}) "
            "{ nodes { ... on PullRequest { " + _REVIEW_NODE_FIELDS + " } } }"
            for m in aliases
        )
        query = f"query({var_decls}) {{ {selections} }}"
        try:
            data = self.graphql(
                query, **{m: REVIEW_SEARCH_QUALIFIERS[m] for m in aliases}
            )
        except Exception as e:  # noqa: BLE001 — fail closed, never "nothing to do"
            log.warning("review search failed: %s", e)
            return None
        payload = data.get("data")
        if not isinstance(payload, dict):
            return None
        prs: list[ReviewPR] = []
        truncated: list[str] = []
        for mode in aliases:
            bucket = payload.get(mode)
            if not isinstance(bucket, dict):
                return None
            nodes = bucket.get("nodes")
            if not isinstance(nodes, list):
                return None
            if len(nodes) >= limit:
                truncated.append(mode)
            for node in nodes:
                pr = _parse_review_node(node, mode)
                if pr is not None:
                    prs.append(pr)
        return ReviewSearch(prs=tuple(prs), truncated_modes=tuple(truncated))

    def pr_review_bodies(
        self, *, owner: str, repo: str, number: int
    ) -> list[str] | None:
        exit_code, stdout, _stderr = self._run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "comments,reviews",
            ]
        )
        if exit_code != 0:
            return None
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        bodies: list[str] = []
        for surface in ("reviews", "comments"):
            items = payload.get(surface, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                body = item.get("body", "")
                if isinstance(body, str) and body:
                    bodies.append(body)
        return bodies

    def repo_info(self, *, owner: str, repo: str) -> dict[str, object] | None:
        exit_code, stdout, _stderr = self._run(["gh", "api", f"repos/{owner}/{repo}"])
        if exit_code != 0:
            return None
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        argv = ["gh", "api", "graphql"]
        for k, v in variables.items():
            argv += ["-F", f"{k}={v}"]
        argv += ["-f", f"query={query}"]
        exit_code, stdout, stderr = self._run(argv)
        if exit_code != 0:
            raise RuntimeError(f"gh api graphql failed: {stderr.strip()}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def commit_check_runs(
        self, *, owner: str, repo: str, sha: str
    ) -> list[dict[str, object]]:
        """The REST check-runs endpoint, whose response is
        ``{"total_count": N, "check_runs": [...]}`` — a dict, not a list, so the
        ``check_runs`` key has to be unwrapped. The entries use the *lowercase* REST
        vocabulary (``status: "completed"``, ``conclusion: "success"``), which is
        why ``gates.evaluate_commit_checks`` normalises case rather than reusing
        ``_is_pending_check`` (spec 20260929T050000Z finding F5)."""
        exit_code, stdout, stderr = self._run(
            ["gh", "api", f"repos/{owner}/{repo}/commits/{sha}/check-runs"]
        )
        if exit_code != 0:
            raise RuntimeError(f"gh api check-runs failed: {stderr.strip()}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        raw = payload.get("check_runs", [])
        if not isinstance(raw, list):
            return []
        return [c for c in raw if isinstance(c, dict)]


def repo_owner_and_name(remote_url: str) -> tuple[str, str]:
    """Parse owner/repo from a git remote URL. Handles https and ssh forms."""
    url = remote_url.strip()
    url = url.removesuffix(".git").rstrip("/")

    if url.startswith("git@"):
        path = url.split(":", 1)[-1]
    elif "://" in url:
        path = url.split("://", 1)[-1]
        if "/" in path:
            path = path.split("/", 1)[1]
    else:
        path = url

    parts = path.split("/")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    raise ValueError(f"cannot parse owner/repo from: {remote_url}")


def list_open_prs(
    gh: GhClient, *, owner: str, repo: str, branch_prefix: str, author: str
) -> list[PRInfo]:
    """List open PRs whose headRefName starts with branch_prefix and author matches.

    Bot/app accounts are accepted when the branch prefix matches provenance
    (review finding J): a PR opened by a bot from an ``auto/*`` branch is
    treated as herdr-routines-originated regardless of the bot's login, but only
    when the head lives in this repository: a fork can name its branch anything.
    The ``login == author`` path is not fork-checked: that login is the operator's
    own authenticated account, so provenance is already proven.
    """
    try:
        raw = gh.pr_list(owner=owner, repo=repo, state="open", limit=100)
    except Exception as e:  # noqa: BLE001 — any gh failure means no eligible PRs
        log.warning("gh pr list failed: %s", e)
        return []

    result: list[PRInfo] = []
    for pr in raw:
        if not isinstance(pr, dict):
            continue
        head = pr.get("headRefName", "")
        auth = pr.get("author", {})
        login = auth.get("login", "") if isinstance(auth, dict) else ""
        # gh returns author.is_bot (not author.type) in pr list JSON — the
        # finding J bot/app acceptance below reads is_bot directly.
        is_bot = bool(auth.get("is_bot")) if isinstance(auth, dict) else False
        num = pr.get("number")
        url = pr.get("url", "")
        if not (
            isinstance(head, str)
            and head.startswith(branch_prefix)
            and isinstance(num, int)
            and isinstance(url, str)
        ):
            continue
        # Human author must match exactly; a bot/app is accepted on branch-prefix
        # provenance only from a same-repo head. `is False` fails closed when the field
        # is missing.
        if login == author or (is_bot and pr.get("isCrossRepository") is False):
            result.append(PRInfo(number=num, head_ref=head, author=login, url=url))
    return result


def _has_ci_failure(gh: GhClient, *, owner: str, repo: str, number: int) -> bool:
    """Check if any statusCheckRollup entry is in a failing state."""
    try:
        view = gh.pr_view(owner=owner, repo=repo, number=number)
    except Exception as e:  # noqa: BLE001 — any gh failure is treated as "no CI failure"
        log.warning("gh pr view %d failed: %s", number, e)
        return False

    rollup = view.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return False
    for check in rollup:
        if isinstance(check, dict) and check.get("state") in FAILING_CI_STATES:
            return True
    return False


def fetch_failing_checks(gh: GhClient, *, owner: str, repo: str, number: int) -> str:
    """Fetch failing check names and states from statusCheckRollup."""
    try:
        view = gh.pr_view(owner=owner, repo=repo, number=number)
    except Exception as e:  # noqa: BLE001 — best-effort detail fetch must not raise
        log.warning("gh pr view %d failed for failing_checks: %s", number, e)
        return "(could not fetch CI status)"

    rollup = view.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return "(no check data available)"

    failing: list[str] = []
    for check in rollup:
        if not isinstance(check, dict):
            continue
        state = check.get("state", "")
        if state in FAILING_CI_STATES:
            name = check.get("name", check.get("context", "unknown"))
            failing.append(f"  - {name}: {state}")
    return "\n".join(failing) if failing else "(no failing checks found)"


def _has_unresolved_threads(
    gh: GhClient, *, owner: str, repo: str, number: int
) -> bool:
    """Check if any review thread is unresolved (isResolved == false)."""
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 50) {
            nodes {
              isResolved
              comments(first: 1) {
                nodes {
                  body
                }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = gh.graphql(query, owner=owner, repo=repo, number=str(number))
    except Exception as e:  # noqa: BLE001 — any gh failure is treated as "no unresolved threads"
        log.warning("GraphQL reviewThreads query failed for PR %d: %s", number, e)
        return False

    pr = data.get("data", {})
    if not isinstance(pr, dict):
        return False
    repo_data = pr.get("repository", {})
    if not isinstance(repo_data, dict):
        return False
    pr_data = repo_data.get("pullRequest", {})
    if not isinstance(pr_data, dict):
        return False
    threads = pr_data.get("reviewThreads", {})
    if not isinstance(threads, dict):
        return False
    nodes = threads.get("nodes", [])
    if not isinstance(nodes, list):
        return False
    for thread in nodes:
        if not isinstance(thread, dict):
            continue
        if thread.get("isResolved") is False:
            return True
    return False


def fetch_thread_bodies(gh: GhClient, *, owner: str, repo: str, number: int) -> str:
    """Fetch unresolved review thread bodies for the prompt."""
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 50) {
            nodes {
              id
              isResolved
              comments(first: 1) {
                nodes {
                  body
                }
              }
            }
          }
        }
      }
    }
    """
    try:
        data = gh.graphql(query, owner=owner, repo=repo, number=str(number))
    except Exception as e:  # noqa: BLE001 — best-effort detail fetch must not raise
        log.warning("GraphQL reviewThreads query failed for PR %d: %s", number, e)
        return "(could not fetch review threads)"

    pr = data.get("data", {})
    if not isinstance(pr, dict):
        return "(no thread data)"
    repo_data = pr.get("repository", {})
    if not isinstance(repo_data, dict):
        return "(no thread data)"
    pr_data = repo_data.get("pullRequest", {})
    if not isinstance(pr_data, dict):
        return "(no thread data)"
    threads = pr_data.get("reviewThreads", {})
    if not isinstance(threads, dict):
        return "(no thread data)"
    nodes = threads.get("nodes", [])
    if not isinstance(nodes, list):
        return "(no thread data)"

    bodies: list[str] = []
    for thread in nodes:
        if not isinstance(thread, dict):
            continue
        if thread.get("isResolved") is not False:
            continue
        thread_id = thread.get("id", "unknown")
        comments = thread.get("comments", {})
        comment_nodes = comments.get("nodes", []) if isinstance(comments, dict) else []
        body = ""
        if comment_nodes and isinstance(comment_nodes[0], dict):
            body = comment_nodes[0].get("body", "")
        bodies.append(f"  - Thread {thread_id}: {body[:200]}")
    return "\n".join(bodies) if bodies else "(no unresolved threads)"


def is_eligible(
    gh: GhClient, *, owner: str, repo: str, pr: PRInfo
) -> EligiblePR | None:
    """Check if a PR is eligible for auto-fix (failing CI or unresolved threads).

    Returns EligiblePR carrying the real PRInfo, or None if not eligible.
    """
    has_ci = _has_ci_failure(gh, owner=owner, repo=repo, number=pr.number)
    has_threads = _has_unresolved_threads(gh, owner=owner, repo=repo, number=pr.number)

    if has_ci and has_threads:
        return EligiblePR(pr=pr, reason="both")
    if has_ci:
        return EligiblePR(pr=pr, reason="ci_failure")
    if has_threads:
        return EligiblePR(pr=pr, reason="unresolved_threads")
    return None


def attempt_count_for_pr(history_path: Path, job_name: str, pr_number: int) -> int:
    """Count prior fix-attempt records for this job+pr_number that were real
    attempts (done or failed — not skipped). Skipped/max_attempts_exceeded
    records the tick itself appends must not count toward the budget, or a
    fixed-then-broken PR is permanently abandoned (review finding F)."""
    records = read_job(history_path, job_name)
    count = 0
    for r in records:
        if (
            r.state in _COUNTABLE_STATES
            and r.extra
            and r.extra.get("pr_number") == pr_number
        ):
            count += 1
    return count


def build_fix_prompt(
    pr_number: int,
    branch: str,
    failing_checks: str,
    thread_bodies: str,
    owner_repo: str,
    report_path: str,
) -> str:
    """Build the prompt for the fix worker agent."""
    return textwrap.dedent(f"""\
        You are fixing a failing PR. Work in the checked-out branch.

        PR: #{pr_number} on {owner_repo}
        Branch: {branch}
        Report: {report_path}

        Failing CI checks:
        {failing_checks}

        Unresolved review threads:
        {thread_bodies}

        Instructions:
        1. Read the failing check output and review thread comments
        2. Fix the code to address the CI failures and review feedback
        3. Run `uv run pytest -q` to verify tests pass
        4. Run `uv run ruff check src/` to verify lint passes
        5. Commit your changes with a descriptive message
        6. Run `git push` to push the fix

        After pushing:
        - For each review thread, reply to it with a summary of what you fixed
        - Use `gh api graphql` with `resolveReviewThread` mutation to resolve
          threads you addressed, using the thread ID from the GraphQL query

        Write a summary of your findings and fixes to: {report_path}

        Do NOT modify files outside the scope of the CI failures or review comments.
        Bounded work: complete the fix and push, then stop.
    """)


def build_pr_agent_name(job_name: str, pr_number: int) -> str:
    """Build the stable per-PR agent-name prefix: rt-<job>-pr<n>, truncated to
    32 chars. Used for live-agent checks across ticks (two ticks 5 min apart must
    not double-dispatch the same PR, review finding E) — and, since
    build_worker_agent_name always keeps this as a true prefix of the full name it
    creates, callers can match live agents by prefix instead of exact equality
    (issue 036c: the previous exact-match guard could never match anything because
    the dispatcher never created a name without the run_id)."""
    raw = f"rt-{job_name}-pr{pr_number}"
    return raw[:32]


def build_worker_agent_name(job_name: str, pr_number: int, run_id: str) -> str:
    """Build the agent name for a fix worker: rt-<job>-pr<n>-<tail>, truncated to
    NAME_RE's 32-char cap (config.py). `run_id` is itself "<job_name>-<timestamp>"
    (see runner.make_run_id), so the job name is stripped from it here before use —
    otherwise it appears twice in `raw` and the naive 32-char slice used to swallow
    the whole timestamp, the only part distinguishing one attempt from the next
    (issue 036c). The `build_pr_agent_name` prefix is always kept intact; only the
    distinguishing tail is shortened (via a hash, to stay a fixed size regardless of
    the timestamp's length) when there isn't room for it verbatim."""
    prefix = build_pr_agent_name(job_name, pr_number)
    job_prefix = f"{job_name}-"
    tail = run_id.removeprefix(job_prefix)

    budget = 32 - len(prefix) - 1  # -1 for the separating "-"
    if budget <= 0:
        return prefix[:32]
    if len(tail) > budget:
        tail = hashlib.sha1(run_id.encode()).hexdigest()[:budget]
    return f"{prefix}-{tail}"


# ---------------------------------------------------------------------------
# kind: review helpers (issue 055)
# ---------------------------------------------------------------------------

# The stable HTML marker a review agent ends its posted review with, and the engine
# looks for before dispatching and again as its post-condition. Names *both* the job and
# the target key so two review jobs over one account never treat each other's reviews as
# their own (the same "own work only" shape as the attempt budget).
REVIEW_MARKER_PREFIX = "herdr-routines:review"

# The per-mode GitHub search qualifier. `first: 100` (GitHub's own ceiling) is added at
# query-build time; phase A does not paginate.
REVIEW_SEARCH_QUALIFIERS: dict[str, str] = {
    "author": "is:pr is:open archived:false draft:false author:@me",
    "review_requested": "is:pr is:open archived:false draft:false review-requested:@me",
}

_REVIEW_NODE_FIELDS = (
    "number title url createdAt updatedAt headRefName "
    "author { login } repository { nameWithOwner } headRepository { nameWithOwner }"
)


def _parse_review_node(node: object, mode: str) -> ReviewPR | None:
    """Best-effort parse of one search node; a malformed node is skipped, not fatal."""
    if not isinstance(node, dict):
        return None
    number = node.get("number")
    if not isinstance(number, int):
        return None
    author_block = node.get("author")
    login = author_block.get("login", "") if isinstance(author_block, dict) else ""
    repo_block = node.get("repository")
    repo = repo_block.get("nameWithOwner", "") if isinstance(repo_block, dict) else ""
    head_block = node.get("headRepository")
    head_repo = (
        head_block.get("nameWithOwner", "") if isinstance(head_block, dict) else ""
    )
    return ReviewPR(
        number=number,
        title=str(node.get("title", "")),
        url=str(node.get("url", "")),
        created_at=str(node.get("createdAt", "")),
        updated_at=str(node.get("updatedAt", "")),
        head_ref=str(node.get("headRefName", "")),
        author=str(login),
        repo=str(repo),
        head_repo=str(head_repo or ""),
        mode=mode,
    )


def review_marker(job_name: str, target_key: str) -> str:
    """The exact marker this job writes for this target — one function so the prompt,
    the pre-dispatch guard and the post-condition can never drift apart."""
    return f"<!-- {REVIEW_MARKER_PREFIX} job={job_name} key={target_key} -->"


def review_target_hash(job_name: str, target_key: str) -> str:
    """``h6 = sha1("job|target_key")[:6]`` — the per-(job, target) discriminator. A bare
    PR number is ambiguous across repos, so the hash keys the agent name *and* the per-PR
    report filename on the full target key."""
    return hashlib.sha1(f"{job_name}|{target_key}".encode()).hexdigest()[:6]


def build_review_agent_prefix(job_name: str, target_key: str) -> str:
    """The stable per-(job, target) agent-name prefix: ``rt-<job>-rv<h6>`` where
    ``h6 = sha1("job|target_key")[:6]``. The job-name portion is capped at 13 chars so
    the prefix is at most ``13 + 12 = 25`` chars, leaving the run tail ≥ 6 (issue 051:
    a full 24-char job name would otherwise leave no room for the tail, and two ticks
    five minutes apart would collide on `agent_name_taken`). The single function is both
    the builder's and the liveness guard's source of truth (issue 036c)."""
    return f"rt-{job_name[:13]}-rv{review_target_hash(job_name, target_key)}"


def build_review_agent_name(job_name: str, target_key: str, run_id: str) -> str:
    """The full worker agent name: the shared prefix plus a run tail, truncated to the
    32-char NAME_RE cap. The ``rv<h6>`` discriminator is never truncated; only the tail
    is hashed when it doesn't fit, exactly like ``build_worker_agent_name``."""
    prefix = build_review_agent_prefix(job_name, target_key)
    tail = run_id.removeprefix(f"{job_name}-")
    budget = 32 - len(prefix) - 1
    if budget <= 0:
        return prefix[:32]
    if len(tail) > budget:
        tail = hashlib.sha1(run_id.encode()).hexdigest()[:budget]
    return f"{prefix}-{tail}"


def build_review_prompt(
    *,
    job_name: str,
    target_key: str,
    url: str,
    report_path: str,
) -> str:
    """The engine-injected prompt for a review worker (the sibling of
    ``build_fix_prompt``; a job-level ``prompt`` overrides it). Read-only by
    construction: the agent reviews the PR and disappears with its worktree."""
    marker = review_marker(job_name, target_key)
    return textwrap.dedent(f"""\
        You are reviewing an open pull request. This is a read-only review: do not
        edit files, commit, push, create a branch, resolve threads, or otherwise change
        the PR. The worktree is removed when you finish.

        PR: {url}
        Target: {target_key}
        Report: {report_path}

        Steps:
        1. First re-read the PR's existing comments and reviews. If it already contains
           this exact marker, POST NOTHING and stop:
               {marker}
        2. Otherwise run the `code-review` skill against {url} in single-session mode.
        3. Post exactly one review as a COMMENT review (`gh pr review --comment`), never
           approve or request changes: GitHub rejects both on a PR you authored, and the
           blocking tier already carries the verdict. Tag every finding `blocking` or
           `non-blocking`.
        4. End the posted review body with this exact marker line:
               {marker}

        Write a summary to $ROUTINE_REPORT whose first line is
        `## Outcome: ok` (or `## Outcome: skipped` / `## Outcome: failed`) followed by
        the PR URL and what you did.

        Bounded work: post the single review, write the report, then stop.
    """)


# Failure reasons that still consume a review target's budget: the agent ran to settle,
# so a review may well have landed (an unread or missing marker). Re-dispatching would
# risk a duplicate review. Every other failure (worktree, agent start/prompt) happens
# before the agent could post anything and must not burn the target's only attempt.
_REVIEW_COUNTABLE_FAILURES = frozenset(
    {"review_not_posted", "review_verification_failed"}
)


def attempt_count_for_target(history_path: Path, job_name: str, target_key: str) -> int:
    """Count prior review attempts for this job+target_key: ``done`` records plus
    ``failed`` records whose agent actually ran (``_REVIEW_COUNTABLE_FAILURES``).
    Sibling of ``attempt_count_for_pr``, keyed on the full ``owner/repo#N`` so ``#7``
    in one repo is independent of ``#7`` in another. Skips and pre-agent infra failures
    must not consume budget — with the default budget of 1, a transient failure would
    otherwise exclude the PR forever."""
    records = read_job(history_path, job_name)
    count = 0
    for r in records:
        if not r.extra or r.extra.get("target_key") != target_key:
            continue
        if r.state == "done" or (
            r.state == "failed" and r.extra.get("reason") in _REVIEW_COUNTABLE_FAILURES
        ):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Unified gate model (docs/pipeline/runs/20260830T050021Z/spec.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single check execution."""

    kind: GateCheckKind
    passed: bool
    output: str = ""
    error: str | None = None
    timed_out: bool = False
    # The raw exit code of a command check (None for pr_health). An audit command reads
    # it directly: only 127 (binary missing) is a failure there, not "non-zero".
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """Aggregate result of running all checks."""

    passed: bool
    results: tuple[CheckResult, ...]
    combined_output: str = ""


def _timeout_text(value: bytes | str | None) -> str:
    """`TimeoutExpired.stdout/.stderr` are raw bytes on POSIX even under `text=True`
    (they come from `communicate()` before decoding); keep them rather than dropping."""
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode(errors="replace")


def run_checks(
    checks: tuple[GateCheck, ...],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    runner: Any | None = None,
) -> GateOutcome:
    """Run all gate checks sequentially, no short-circuit. Returns GateOutcome.

    `runner` is an injectable callable ``(argv, *, timeout_s) -> (exit_code, stdout, stderr)``
    for testability. Defaults to subprocess.run."""
    import subprocess as _subprocess

    if runner is None:

        def _default_runner(
            argv: list[str], *, timeout_s: float
        ) -> tuple[int, str, str]:
            try:
                proc = _subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    check=False,
                    cwd=cwd,
                    env=env,
                )
                return proc.returncode, proc.stdout, proc.stderr
            except _subprocess.TimeoutExpired as e:
                return (
                    TIMEOUT_EXIT_CODE,
                    _timeout_text(e.stdout),
                    _timeout_text(e.stderr),
                )
            except OSError as e:
                return 127, "", str(e)

        runner = _default_runner

    results: list[CheckResult] = []
    all_passed = True
    outputs: list[str] = []

    for check in checks:
        kind = check.kind
        if kind == "pr_health":
            results.append(CheckResult(kind="pr_health", passed=True))
        elif kind == "command":
            if check.command is None:
                # A command check without a command can't run — treat as failed
                all_passed = False
                results.append(
                    CheckResult(
                        kind=kind,
                        passed=False,
                        error="command check has no command",
                    )
                )
                continue
            timeout_s = check.timeout_ms / 1000
            argv = shlex.split(check.command)
            exit_code, stdout, stderr = runner(argv, timeout_s=timeout_s)
            timed_out = exit_code == TIMEOUT_EXIT_CODE
            passed = exit_code == 0 and not timed_out
            output = stdout + stderr
            outputs.append(f"[{check.command}] exit={exit_code}\n{output}")
            if not passed:
                all_passed = False
            results.append(
                CheckResult(
                    kind="command",
                    passed=passed,
                    output=output.strip(),
                    timed_out=timed_out,
                    error=f"exit code {exit_code}" if not passed else None,
                    exit_code=exit_code,
                )
            )
        elif kind == "github_event":
            # Polled by the tick, never run here — treat as failed
            all_passed = False
            results.append(
                CheckResult(
                    kind=kind,
                    passed=False,
                    error="github_event checks are polled by the tick, not run by run_checks",
                )
            )
        else:
            assert_never(kind)

    return GateOutcome(
        passed=all_passed,
        results=tuple(results),
        combined_output="\n".join(outputs),
    )


def build_base_fix_prompt(
    job_name: str,
    gate_output: str,
    base: str,
    report_path: str,
    checks: tuple[GateCheck, ...] | None = None,
) -> str:
    """Build the prompt for a base-target gate fix worker."""
    check_lines = ""
    if checks:
        cmds = [f"  - {c.command}" for c in checks if c.kind == "command" and c.command]
        if cmds:
            check_lines = (
                "\n        Configured gate checks (re-run these after fixing):\n"
                + "\n".join(cmds)
                + "\n"
            )
    return textwrap.dedent(f"""\
        You are fixing gate failures on the {base} branch. Work in the checked-out worktree.

        Job: {job_name}
        Base: {base}
        Report: {report_path}

        Gate output (failing checks):
        {gate_output}{check_lines}
        Instructions:
        1. Read the gate output above to understand what failed
        2. Fix the code to address the failures
        3. Re-run the configured gate checks listed above to verify they pass
        4. Commit your changes with a descriptive message
        5. Create a new branch `auto/{job_name}-<timestamp>` and push it
        6. Open a PR targeting {base} with `gh pr create --base {base}`

        Write a summary of your findings and fixes to: {report_path}

        Bounded work: complete the fix and push, then stop.
    """)


def build_gate_worker_agent_name(job_name: str, run_id: str) -> str:
    """Build agent name for a gate fix worker: rt-<job>-gate-<run_id> truncated to
    32 chars (NAME_RE cap). Use a short hash of run_id to avoid collision."""
    import hashlib

    short_hash = hashlib.sha1(run_id.encode()).hexdigest()[:8]
    raw = f"rt-{job_name}-gate-{short_hash}"
    return raw[:32]


def build_audit_agent_name(job_name: str, run_id: str) -> str:
    """Agent name for an audit agent: rt-<job>-au<hash>, the hash shortened to fit the
    32-char NAME_RE cap (2 hex chars for a 24-char job name, never zero)."""
    prefix = f"rt-{job_name}-au"
    budget = max(32 - len(prefix), 1)
    return f"{prefix}{hashlib.sha1(run_id.encode()).hexdigest()[: min(budget, 8)]}"[:32]


# The manifest shape an audit must write (issue 057, "The findings manifest"). Spelled
# out in the prompt because no audit skill emits it on its own yet.
_MANIFEST_EXAMPLE = """\
{
  "version": 1,
  "check": "<skill or tool name>",
  "findings": [
    {
      "id": "<optional stable id, e.g. a rule id + symbol>",
      "kind": "<finding category>",
      "severity": "low | medium | high",
      "location": "<path/to/file.ext:line>",
      "summary": "<one line>"
    }
  ]
}"""


def build_audit_prompt(
    *,
    skill: str,
    base: str,
    report_path: str,
    findings_path: str,
    instructions: str = "",
) -> str:
    """The prompt for an audit agent (`audit.skill`). With `instructions` (the job's
    `prompt`), the audit is described inline instead of loaded as a skill, for a repo
    that has no such skill. Either way the output contract below stays the engine's."""
    if instructions.strip():
        what = (
            f"Run the `{skill}` audit described below against this repository.\n\n"
            f"{instructions.strip()}\n\n---\n\n"
        )
    else:
        what = f"Run the `{skill}` skill against this repository. "
    return what + textwrap.dedent("""\
        You are in a fresh worktree checked out at `{base}`.

        This is a read-only audit. Do NOT edit source files, commit, push, create a
        branch, or open a PR. A separate worker fixes findings later.

        Write two files when the audit is done:

        1. A human-readable Markdown report to: {report_path}
        2. A machine-readable findings manifest to: {findings_path}
           It must be valid JSON in exactly this shape:

        {manifest}

           - `severity` must be one of low, medium, high.
           - `kind`, `location` and `summary` are required, non-empty strings.
           - List every finding that should be fixed (a fix worker acts on each entry;
             if the audit's own instructions narrow which findings qualify, follow
             them). An empty `findings` list means there is nothing to fix.
           - Write the manifest even when there are no findings.

        Bounded work: finish the audit, write both files, then stop.
    """).format(
        base=base,
        report_path=report_path,
        findings_path=findings_path,
        manifest=textwrap.indent(_MANIFEST_EXAMPLE, "   "),
    )


def build_audit_fix_prompt(
    *,
    job_name: str,
    check: str,
    base: str,
    branch: str,
    report_path: str,
    findings: Sequence[Finding],
    recheck: str,
) -> str:
    """The engine-injected prompt for an audit fix worker: build_base_fix_prompt's
    sibling, carrying the finding table instead of gate output. `recheck` is how to
    re-run the audit, so the worker can enumerate every instance behind a derived ID
    (same kind + same file collapse to one ID)."""
    table = "\n".join(
        f"| `{f.id}` | {f.severity} | {f.kind} | `{f.location}` | {f.summary} |"
        for f in findings
    )
    return textwrap.dedent("""\
        You are fixing findings from the `{check}` audit on `{base}`. Work in the
        checked-out worktree.

        Job: {job_name}
        Base: {base}
        Branch: {branch}
        Report: {report_path}

        Findings to fix (most severe first):

        | id | severity | kind | location | summary |
        |----|----------|------|----------|---------|
        {table}

        One finding can stand for several instances: a finding without a stable id is
        keyed by kind and file, not line. Re-run the audit to list every instance:
        {recheck}

        Instructions:
        1. Fix every instance of each finding above. Do not fix unrelated findings.
        2. Re-run the audit and confirm these findings are gone.
        3. Commit your changes with a descriptive message.
        4. Create the branch `{branch}` and push it.
        5. Open a PR targeting {base} with `gh pr create --base {base}`.

        Write a summary of what you fixed (and anything you could not) to: {report_path}

        Bounded work: complete the fix and push, then stop.
    """).format(
        check=check,
        base=base,
        job_name=job_name,
        branch=branch,
        report_path=report_path,
        table=table,
        recheck=recheck,
    )


def consecutive_base_gate_failures(history_path: Path, job_name: str) -> int:
    """Base-target retry budget: failed gate attempts since the last passing run.
    Keyed on job_name only (stable across runs); a passing gate resets the count.
    Reads `gate`, not `state`: a fix agent that settles `done` still left the gate failed."""
    count = 0
    for r in read_job(history_path, job_name):
        if not r.extra or r.extra.get("target") != "base":
            continue
        gate = r.extra.get("gate")
        if gate == "passed":
            count = 0
        # Records without a gate_branch (e.g. ensure_repo failures) are infra flakes,
        # not gate attempts, so they do not spend budget.
        elif gate == "failed" and r.extra.get("gate_branch") is not None:
            count += 1
    return count


# ---------------------------------------------------------------------------
# github_event check kind (issue 014, spec v2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GithubEvent:
    """One polled, dedupable GitHub event for a `github_event` check.

    `event_id` is the stable dedup key (the spec's `extra.event_id`); re-polls that
    return an ID already in `last_seen_event_ids` are clean no-ops. `payload` is the
    raw read the poll produced so the seeded prompt can quote real fields.

    `is_issue` distinguishes an `issues:opened` event (an issue number, no head ref)
    from a PR-backed event, so detail enrichment and history `target` never treat an
    issue as a PR."""

    event_id: str
    number: int
    title: str
    head_ref: str | None
    label: str | None
    payload: dict[str, Any]
    is_issue: bool = False


def _label_names(labels: object) -> list[str]:
    """Flatten the label shapes gh returns to bare names. Handles both the REST
    ``[{"name": ...}, ...]`` list and the GraphQL ``{"nodes": [{"name": ...}]}``
    wrapper, plus a bare list of strings."""
    if isinstance(labels, dict):
        labels = labels.get("nodes")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for lbl in labels:
        if isinstance(lbl, dict) and isinstance(lbl.get("name"), str):
            names.append(lbl["name"])
        elif isinstance(lbl, str):
            names.append(lbl)
    return names


def _event_matches(event: Mapping[str, object], match: Mapping[str, object]) -> bool:
    """Elevate the config-level `match` mapping to a predicate over one polled event."""
    label = match.get("label")
    if label is not None and (
        not isinstance(label, str) or label not in _label_names(event.get("labels"))
    ):
        return False
    author = match.get("author")
    if author is not None:
        auth = event.get("author", {})
        login = auth.get("login") if isinstance(auth, dict) else None
        if login != author:
            return False
    branch = match.get("branch")
    return not (branch is not None and event.get("headRefName") != branch)


def poll_github_event(
    gh: GhClient, *, owner: str, repo: str, check: GateCheck
) -> list[GithubEvent]:
    """Read-only poll for a `github_event` check (spec v2 §2).

    Always raises on a failing `gh` call (auth, rate limit, transient error) so the
    tick can fail closed as a gate error — the "unverifiable ≠ green" rule that
    `commit_check_runs` already follows. One list query plus per-event detail only;
    detail (title/head ref) is cheap REST data already present in the list/poll
    shapes. The `title` field may be left empty by queries whose list shape lacks it;
    the tick enriches new events right before dispatch."""
    query = check.query or "prs:labels"
    match = check.match or {}

    if query == "prs:labels":
        raw = gh.pr_list(owner=owner, repo=repo, state="open", limit=100)
        match_label = (
            match.get("label") if isinstance(match.get("label"), str) else None
        )
        result: list[GithubEvent] = []
        for pr in raw:
            if not isinstance(pr, dict):
                continue
            number = pr.get("number")
            if not isinstance(number, int) or not _event_matches(pr, match):
                continue
            # The dedup key is the event's *own* label (the matched one when configured,
            # else the first label the PR carries), so `evt-pr-<n>-<label>` is stable
            # and self-describing rather than depending on the match config.
            names = sorted(_label_names(pr.get("labels")))
            label = (
                match_label
                if match_label is not None
                else (names[0] if names else None)
            )
            head_ref = pr.get("headRefName")
            suffix = f"-{label}" if label else ""
            result.append(
                GithubEvent(
                    event_id=f"evt-pr-{number}{suffix}",
                    number=number,
                    title="",
                    head_ref=head_ref if isinstance(head_ref, str) else None,
                    label=label,
                    payload=pr,
                )
            )
        return result

    if query == "ci:failed":
        # One GraphQL read for every open PR plus its status rollup, rather than a
        # `pr_list` + per-PR `pr_view` N+1 (which could trip rate limits on a busy
        # repo). A failing `gh` call raises, so the tick still fails closed.
        gql = """
        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            pullRequests(first: 100, states: OPEN) {
              nodes {
                number
                title
                headRefName
                author { login }
                labels(first: 20) { nodes { name } }
                statusCheckRollup { state }
              }
            }
          }
        }
        """
        data = gh.graphql(gql, owner=owner, repo=repo)
        nodes = _graphql_nodes(data, "data", "repository", "pullRequests", "nodes")
        result = []
        for pr in nodes:
            if not isinstance(pr, dict) or not _event_matches(pr, match):
                continue
            number = pr.get("number")
            if not isinstance(number, int):
                continue
            rollup = pr.get("statusCheckRollup")
            failing = (
                isinstance(rollup, dict) and rollup.get("state") in FAILING_CI_STATES
            )
            if not failing:
                continue
            title = pr.get("title")
            head_ref = pr.get("headRefName")
            result.append(
                GithubEvent(
                    event_id=f"evt-ci-{number}",
                    number=number,
                    title=title if isinstance(title, str) else "",
                    head_ref=head_ref if isinstance(head_ref, str) else None,
                    label=None,
                    payload=pr,
                )
            )
        return result

    if query == "issues:opened":
        gql = """
        query($owner: String!, $repo: String!, $searchQuery: String!) {
          search(query: $searchQuery, type: ISSUE, first: 50) {
            nodes {
              ... on Issue {
                id number title author { login } labels(first: 10) { nodes { name } }
              }
            }
          }
        }
        """
        data = gh.graphql(
            gql,
            owner=owner,
            repo=repo,
            searchQuery=f"repo:{owner}/{repo} is:issue is:open",
        )
        nodes = _graphql_nodes(data, "data", "search", "nodes")
        issue_events: list[GithubEvent] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            number = node.get("number")
            if not isinstance(number, int):
                continue
            if not _event_matches(node, match):
                continue
            label = match.get("label") if isinstance(match.get("label"), str) else None
            title = node.get("title")
            issue_events.append(
                GithubEvent(
                    event_id=f"evt-issue-{number}",
                    number=number,
                    title=title if isinstance(title, str) else "",
                    head_ref=None,
                    label=label,
                    payload=node,
                    is_issue=True,
                )
            )
        return issue_events

    log.warning("unknown github_event query %r — nothing to poll", query)
    return []


def _graphql_nodes(data: object, *path: str) -> list[Any]:
    """Walk a nested GraphQL response down ``path`` and return the node list, or an
    empty list when any hop is missing/wrong-shaped. Keeps the poll parsers free of
    the repeated isinstance ladder."""
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return node if isinstance(node, list) else []


_SLUG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def event_worktree_slug(event_id: str) -> str:
    """A filesystem-safe slug for an event's worktree directory.

    ``event_id`` can embed a GitHub label (e.g. ``evt-pr-42-review/needed``), and
    labels may contain spaces or characters that are illegal in a path segment.
    Replace runs of unsafe characters with ``-``; when that changes the id, append a
    short hash so two distinct events can never slug to the same directory."""
    slug = _SLUG_UNSAFE_RE.sub("-", event_id).strip("-")
    if slug == event_id:
        return slug or "event"
    digest = hashlib.sha1(event_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}" if slug else digest


def enrich_event_detail(
    gh: GhClient, event: GithubEvent, *, owner: str, repo: str
) -> GithubEvent:
    """Best-effort detail fill-in (title, head ref) for a new event right before the
    prompt is built. Only called for events about to dispatch, so a clean tick never
    pays for it (spec v2 §2: "per-new-event detail only"). Never raises: an event whose
    detail cannot be read still dispatches with what the list poll already returned.

    Issue events are returned unchanged: an issue has no PR head ref, and its title
    already came from the `issues:opened` search read — `pr_view` would fail on an
    issue number, so it must not be called for one."""
    if event.is_issue:
        return event
    try:
        view = gh.pr_view(owner=owner, repo=repo, number=event.number)
    except Exception as e:  # noqa: BLE001 — detail is best-effort, never blocks dispatch
        log.warning(
            "gh pr view %d failed for new event %s: %s", event.number, event.event_id, e
        )
        return event
    if not isinstance(view, dict):
        return event
    title = view.get("title")
    head_ref = view.get("headRefName")
    return GithubEvent(
        event_id=event.event_id,
        number=event.number,
        title=title if isinstance(title, str) and title else event.title,
        head_ref=head_ref if isinstance(head_ref, str) and head_ref else event.head_ref,
        label=event.label,
        payload=event.payload,
        is_issue=event.is_issue,
    )


def attempt_count_for_event(history_path: Path, job_name: str, event_id: str) -> int:
    """Prior real fix-attempt count for one `event_id` (issue 014 retry budget keyed
    per event, mirroring the pr-number key of pr-target mode). Terminal states only —
    skipped/max_attempts records must not increment the budget, same rule as
    attempt_count_for_pr."""
    records = read_job(history_path, job_name)
    count = 0
    for r in records:
        if (
            r.state in _COUNTABLE_STATES
            and r.extra
            and r.extra.get("event_id") == event_id
        ):
            count += 1
    return count


def build_event_fix_prompt(
    job_name: str,
    event: GithubEvent,
    owner_repo: str,
    report_path: str,
    base: str,
) -> str:
    """Build the prompt for an event-triggered worker: seeded with the event payload
    (its stable id, number, title and labels) so the agent knows what fired (spec v2 §2).

    The publish step matches how `_dispatch_event_worker` prepared the worktree: a PR
    event is already checked out at its head branch (commit + push updates that PR), an
    issue event has no head branch (branch off the base and open a PR)."""
    label_line = f"Label: {event.label}\n" if event.label else ""
    if event.head_ref:
        publish_step = (
            f"4. The worktree is already checked out at the event's head branch "
            f"`{event.head_ref}`; commit your changes there and push to update the "
            "existing PR that fired this event."
        )
    else:
        publish_step = (
            f"4. Create a branch `auto/{job_name}-<timestamp>` from the current "
            f"checkout, push it, and open a PR targeting {base} with "
            f"`gh pr create --base {base}`."
        )
    return textwrap.dedent(f"""\
        You are responding to a GitHub event on {owner_repo}. Work in the checked-out worktree.

        Job: {job_name}
        Event: {event.event_id}
        PR/Issue: #{event.number}
        Title: {event.title}
        {label_line}Report: {report_path}

        Instructions:
        1. Read the event details above and make the changes they call for.
        2. Verify with `uv run pytest -q` (and `uv run ruff check src/` if the repo lints).
        3. Commit your changes with a descriptive message.
        {publish_step}

        Write a summary of what you did to: {report_path}

        Bounded work: complete the change and push, then stop.
    """)
