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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
        """Return open PRs with number, headRefName, author, url."""
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
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {}

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
    treated as herdr-routines-originated regardless of the bot's login.
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
        # Human author must match exactly; bot/app accepted on branch-prefix provenance alone.
        if is_bot or login == author:
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
# Unified gate model (docs/pipeline/runs/20260830T050021Z/spec.md)
# ---------------------------------------------------------------------------

# _COUNTABLE_STATES is reused for gate budget (SSOT, no separate _COUNTABLE_STATES_GATE)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single check execution."""

    kind: str  # "pr_health" | "command"
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


@dataclass(frozen=True, slots=True)
class GateCheck:
    """A single check in the gate model."""

    kind: str  # "pr_health" | "command" | "github_event"
    command: str | None = None
    timeout_ms: int = 120_000
    # -- kind: github_event (issue 014) -----------------------------------------
    # Mirror of config.GateCheck's event fields so run_checks and the event poll see
    # the same shape whichever GateCheck type a caller holds.
    query: str | None = None
    repo: str | None = None
    match: dict[str, Any] | None = None


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
                stdout = e.stdout if isinstance(e.stdout, str) else ""
                stderr = e.stderr if isinstance(e.stderr, str) else ""
                return TIMEOUT_EXIT_CODE, stdout, stderr
            except OSError as e:
                return 127, "", str(e)

        runner = _default_runner

    results: list[CheckResult] = []
    all_passed = True
    outputs: list[str] = []

    for check in checks:
        if check.kind == "pr_health":
            results.append(CheckResult(kind="pr_health", passed=True))
            continue

        if check.kind == "command" and check.command is not None:
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
            continue

        # Unknown check kind — treat as failed
        all_passed = False
        results.append(
            CheckResult(
                kind=check.kind,
                passed=False,
                error=f"unknown check kind: {check.kind}",
            )
        )

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


def attempt_count_for_gate_branch(
    history_path: Path, job_name: str, gate_branch: str
) -> int:
    """Count prior terminal records for this job+gate_branch (base-target retry budget).
    Keyed on job_name only (stable across runs) so the budget persists per job."""
    records = read_job(history_path, job_name)
    count = 0
    for r in records:
        if (
            r.state in _COUNTABLE_STATES
            and r.extra
            and r.extra.get("target") == "base"
            and r.extra.get("gate_branch") is not None
        ):
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
    raw read the poll produced so the seeded prompt can quote real fields."""

    event_id: str
    number: int
    title: str
    head_ref: str | None
    label: str | None
    payload: dict[str, Any]


def _label_names(labels: Any) -> list[str]:
    """Flatten the label shapes gh returns (`[{"name": ...}, ...]`) to bare names."""
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for lbl in labels:
        if isinstance(lbl, dict) and isinstance(lbl.get("name"), str):
            names.append(lbl["name"])
    return names


def _event_matches(event: dict[str, Any], match: dict[str, Any]) -> bool:
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
        raw = gh.pr_list(owner=owner, repo=repo, state="open", limit=100)
        result = []
        for pr in raw:
            if not isinstance(pr, dict) or not _event_matches(pr, match):
                continue
            number = pr.get("number")
            if not isinstance(number, int):
                continue
            try:
                view = gh.pr_view(owner=owner, repo=repo, number=number)
            except Exception as e:  # noqa: BLE001 — a failing detail read is not a failing CI
                log.warning("gh pr view %d failed for ci:failed: %s", number, e)
                continue
            rollup = view.get("statusCheckRollup") if isinstance(view, dict) else None
            failing = False
            if isinstance(rollup, list):
                failing = any(
                    isinstance(c, dict) and c.get("state") in FAILING_CI_STATES
                    for c in rollup
                )
            if not failing:
                continue
            title = view.get("title") if isinstance(view, dict) else ""
            head_ref = view.get("headRefName") if isinstance(view, dict) else None
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
              ... on Issue { id number title labels(first: 10) { nodes { name } } }
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
        nodes: list[Any] = []
        if isinstance(data, dict):
            payload = data.get("data")
            if isinstance(payload, dict):
                search = payload.get("search")
                if isinstance(search, dict) and isinstance(search.get("nodes"), list):
                    nodes = search["nodes"]
        issue_events: list[GithubEvent] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            number = node.get("number")
            if not isinstance(number, int):
                continue
            labels = node.get("labels")
            names = (
                _label_names(labels.get("nodes")) if isinstance(labels, dict) else []
            )
            label = match.get("label") if isinstance(match.get("label"), str) else None
            if label is not None and label not in names:
                continue
            title = node.get("title")
            issue_events.append(
                GithubEvent(
                    event_id=f"evt-issue-{number}",
                    number=number,
                    title=title if isinstance(title, str) else "",
                    head_ref=None,
                    label=label,
                    payload=node,
                )
            )
        return issue_events

    log.warning("unknown github_event query %r — nothing to poll", query)
    return []


def enrich_event_detail(
    gh: GhClient, event: GithubEvent, *, owner: str, repo: str
) -> GithubEvent:
    """Best-effort detail fill-in (title, head ref) for a new event right before the
    prompt is built. Only called for events about to dispatch, so a clean tick never
    pays for it (spec v2 §2: "per-new-event detail only"). Never raises: an event whose
    detail cannot be read still dispatches with what the list poll already returned."""
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
    (its stable id, number, title and labels) so the agent knows what fired (spec v2 §2)."""
    label_line = f"Label: {event.label}\n" if event.label else ""
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
        4. Create a branch `auto/{job_name}-<timestamp>`, push it, and open a PR
           targeting {base} with `gh pr create --base {base}`.

        Write a summary of what you did to: {report_path}

        Bounded work: complete the change and push, then stop.
    """)
