"""Auto-fix PR standing job: enumerate open routine-owned PRs with failing CI or
unresolved review threads, dispatch bounded fix workers.

Pure-ish module: no subprocess except via injected GhClient, so unit-testable with
frozen now and fixture history. See docs/pipeline/runs/20260829T050025Z/spec.md.
"""

from __future__ import annotations

import json
import logging
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from herdr_routines.history import HistoryRecord, read_job

log = logging.getLogger(__name__)

FAILING_CI_STATES = frozenset({"FAILURE", "ERROR", "TIMED_OUT"})
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
    ) -> list[dict[str, str]]:
        """Return open PRs with number, headRefName, author.login, url, author.type."""
        ...

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        """Return PR details including statusCheckRollup."""
        ...

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        """Execute a GraphQL query via gh api graphql."""
        ...


@dataclass(frozen=True, slots=True)
class PRInfo:
    """Minimal PR info for eligibility checking."""

    number: int
    head_ref: str
    author: str
    url: str


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
                argv, capture_output=True, text=True, timeout=self._timeout_s
            )
        except subprocess.TimeoutExpired:
            return 124, "", "gh timed out"
        return proc.returncode, proc.stdout, proc.stderr

    def api_user(self) -> str:
        exit_code, stdout, stderr = self._run(
            ["gh", "api", "user", "--jq", ".login"]
        )
        if exit_code != 0:
            raise RuntimeError(f"gh auth failed: {stderr.strip()}")
        login = stdout.strip()
        if not login:
            raise RuntimeError("gh auth returned empty login")
        return login

    def pr_list(
        self, *, owner: str, repo: str, state: str, limit: int
    ) -> list[dict[str, str]]:
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
                "number,headRefName,author,url",
            ]
        )
        if exit_code != 0:
            raise RuntimeError(f"gh pr list failed: {stderr.strip()}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return []

    def pr_view(self, *, owner: str, repo: str, number: int) -> dict[str, object]:
        exit_code, stdout, stderr = self._run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "statusCheckRollup,headRefName",
            ]
        )
        if exit_code != 0:
            raise RuntimeError(f"gh pr view {number} failed: {stderr.strip()}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {}

    def graphql(self, query: str, **variables: str) -> dict[str, object]:
        argv = ["gh", "api", "graphql"]
        for k, v in variables.items():
            argv += ["-F", f"{k}={v}"]
        argv += ["-f", f"query={query}"]
        exit_code, stdout, stderr = self._run(argv)
        if exit_code != 0:
            raise RuntimeError(f"gh api graphql failed: {stderr.strip()}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {}


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
    treated as herdr-routines-originated regardless of the bot's login.
    """
    try:
        raw = gh.pr_list(owner=owner, repo=repo, state="open", limit=100)
    except Exception as e:
        log.warning("gh pr list failed: %s", e)
        return []

    result: list[PRInfo] = []
    for pr in raw:
        if not isinstance(pr, dict):
            continue
        head = pr.get("headRefName", "")
        auth = pr.get("author", {})
        login = auth.get("login", "") if isinstance(auth, dict) else ""
        auth_type = auth.get("type", "") if isinstance(auth, dict) else ""
        is_bot = auth_type in ("Bot", "Mannequin")
        num = pr.get("number")
        url = pr.get("url", "")
        if not (isinstance(head, str) and head.startswith(branch_prefix) and isinstance(num, int)):
            continue
        # Human author must match exactly; bot/app accepted on branch-prefix provenance alone.
        if is_bot or login == author:
            result.append(PRInfo(number=num, head_ref=head, author=login, url=url))
    return result


def _has_ci_failure(gh: GhClient, *, owner: str, repo: str, number: int) -> bool:
    """Check if any statusCheckRollup entry is in a failing state."""
    try:
        view = gh.pr_view(owner=owner, repo=repo, number=number)
    except Exception as e:
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
    except Exception as e:
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


def _has_unresolved_threads(gh: GhClient, *, owner: str, repo: str, number: int) -> bool:
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
    except Exception as e:
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
    except Exception as e:
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
    gh: GhClient, *, owner: str, repo: str, number: int
) -> EligiblePR | None:
    """Check if a PR is eligible for auto-fix (failing CI or unresolved threads).

    Returns EligiblePR with reason, or None if not eligible.
    """
    has_ci = _has_ci_failure(gh, owner=owner, repo=repo, number=number)
    has_threads = _has_unresolved_threads(gh, owner=owner, repo=repo, number=number)

    if has_ci and has_threads:
        return EligiblePR(
            pr=PRInfo(number=number, head_ref="", author="", url=""),
            reason="both",
        )
    if has_ci:
        return EligiblePR(
            pr=PRInfo(number=number, head_ref="", author="", url=""),
            reason="ci_failure",
        )
    if has_threads:
        return EligiblePR(
            pr=PRInfo(number=number, head_ref="", author="", url=""),
            reason="unresolved_threads",
        )
    return None


def attempt_count_for_pr(
    history_path: Path, job_name: str, pr_number: int
) -> int:
    """Count prior fix-attempt records for this job+pr_number that were real
    attempts (done or failed — not skipped). Skipped/max_attempts_exceeded
    records the tick itself appends must not count toward the budget, or a
    fixed-then-broken PR is permanently abandoned (review finding F)."""
    records = read_job(history_path, job_name)
    count = 0
    for r in records:
        if r.state in _COUNTABLE_STATES and r.extra:
            if r.extra.get("pr_number") == pr_number:
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


def build_worker_agent_name(job_name: str, pr_number: int, run_id: str) -> str:
    """Build the agent name for a fix worker: rt-<job>-pr<n>-<run_id> truncated to
    32 chars. Follows NAME_RE from config.py (rt- prefix + name)."""
    raw = f"rt-{job_name}-pr{pr_number}-{run_id}"
    return raw[:32]


def build_pr_agent_name(job_name: str, pr_number: int) -> str:
    """Build a run_id-less agent name for live-agent checks across ticks.
    Two ticks 5 min apart must not double-dispatch the same PR (review finding E)."""
    raw = f"rt-{job_name}-pr{pr_number}"
    return raw[:32]
