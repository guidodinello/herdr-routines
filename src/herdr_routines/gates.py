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

import re
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
    repo_path: Path,
    spec: Path,
    *,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> GateVerdict:
    """Stage 1's spec exists, is non-trivial, sits on a pipeline branch, and is
    committed — `orchestrator-prompt.md` G-1 verbatim.

    The commit check is the one that has bitten: an uncommitted spec.md means stage 2
    reviews a file that is not on the branch, so the review and the implementation end
    up looking at different documents. `git log -1 -- <spec>` returning nothing is the
    proof that no commit touched it."""
    run = runner or _run_bounded

    def git(*args: str) -> tuple[int, str, str]:
        return run(["git", "-C", str(repo_path), *args], timeout_s=60)

    try:
        text = spec.read_text()
    except OSError as e:
        return GateVerdict(passed=False, reason=f"spec not readable at {spec}: {e}")
    if not text.strip():
        return GateVerdict(passed=False, reason=f"spec is empty: {spec}")
    if len(text.splitlines()) <= 2:
        return GateVerdict(
            passed=False,
            reason=f"spec is {len(text.splitlines())} line(s); gate 1 requires > 2",
        )

    code, stdout, stderr = git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return GateVerdict(
            passed=False, reason=f"git rev-parse HEAD failed: {stderr.strip()}"
        )
    branch = stdout.strip()
    if not branch.startswith("auto/pipeline-"):
        return GateVerdict(
            passed=False, reason=f"HEAD is {branch!r}, not an auto/pipeline-* branch"
        )

    try:
        rel = spec.relative_to(repo_path)
    except ValueError:
        rel = spec
    code, stdout, _stderr = git("log", "--oneline", "-1", "--", str(rel))
    if code != 0 or not stdout.strip():
        return GateVerdict(
            passed=False, reason=f"spec is not committed on {branch}: {rel}"
        )
    return GateVerdict(passed=True)


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


def run_gate2(spec: Path) -> GateVerdict:
    """Stage 2's spec v2 carries the structure the rest of the pipeline relies on — the
    `## Acceptance criteria` block stage 3 and gate 3 read, the `## Changelog`
    documenting the v1->v2 delta, and both tier words plus the confidence token.

    `-qw` for the tier words, not a substring: the prose this replaces used `rg -qw
    "blocking"` precisely because a substring test also matches "non-blocking", making
    the check pass without either tier ever being written (G-2).

    Reads the file rather than shelling out to `rg`, which is safe here precisely
    because every pattern is a fixed literal with no metacharacters — a regex built
    from spec text would not be."""
    try:
        text = spec.read_text()
    except OSError as e:
        return GateVerdict(passed=False, reason=f"spec not readable at {spec}: {e}")

    if not re.search(r"^## Acceptance criteria", text, re.MULTILINE):
        return GateVerdict(passed=False, reason="spec has no `## Acceptance criteria`")
    if not re.search(r"^## Changelog", text, re.MULTILINE):
        return GateVerdict(passed=False, reason="spec has no `## Changelog` section")
    for word in ("blocking", "non-blocking"):
        if not re.search(rf"\b{re.escape(word)}\b", text):
            return GateVerdict(
                passed=False, reason=f"spec never uses the {word!r} tier word"
            )
    if "confidence:" not in text:
        return GateVerdict(
            passed=False, reason="spec has no `confidence:` token on any criterion"
        )
    return GateVerdict(passed=True)


def run_gate4(
    gh: GhClient, *, owner: str, repo: str, branch: str, pr: int | None = None
) -> GateVerdict:
    """The PR stage 4 opened is live and pointed at this run's branch.

    `gh pr create` exits 0 after printing a URL, and it exits 0 for a PR a re-invocation
    has already opened — so its exit code alone cannot prove a PR exists, and cannot
    prove it is the one this run means. Re-reading the recorded PR and checking both its
    state and its head is the only check that survives both cases.

    The failure reason names the head that *was* observed. "no PR with head X" and "PR
    Y exists with head Z" are different bugs — a stale PR number, a wrong branch pushed,
    a leftover PR from an earlier run — and collapsing them into one message would send
    the morning triage to the wrong one."""
    if pr is None:
        return GateVerdict(
            passed=False,
            reason=f"no PR number recorded for branch {branch!r}",
        )
    try:
        view = gh.pr_view(owner=owner, repo=repo, number=pr)
    except RuntimeError as e:
        return GateVerdict(passed=False, reason=f"gh pr view {pr} failed: {e}")

    state = view.get("state")
    if state != "OPEN":
        return GateVerdict(
            passed=False,
            reason=f"PR #{pr} is {state or 'in an unknown state'}, not OPEN",
        )
    head = view.get("headRefName")
    if head != branch:
        return GateVerdict(
            passed=False,
            reason=f"PR #{pr} head is {head!r}, expected {branch!r}",
        )
    return GateVerdict(passed=True, reason=f"PR #{pr} on {branch}")


def _pr_bodies(view: Mapping[str, object]) -> list[str]:
    """Every comment and review body `gh pr view --json comments,reviews` returns.

    Conversation comments and review summaries only — NOT inline review comments, which
    `gh pr view` omits entirely (its `reviews` rows for them have empty bodies). Those
    are fetched separately in `run_gate5`."""
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
    # The `code-review` skill posts every finding as an inline comment prefixed
    # `**[blocking]**`; its review-summary body carries no tier tag at all. Without this
    # fetch the gate fails on every PR that skill reviewed (verified on PR #137).
    for comment in gh.pr_review_comments(owner=owner, repo=repo, number=pr):
        body = comment.get("body")
        if isinstance(body, str):
            bodies.append(body)
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

# The gates that read a PR, and therefore the only ones that need `--pr` and a resolved
# GitHub owner/repo. Kept next to `GATE_STAGE_CHOICES` so the CLI's argument handling and
# the dispatcher's own requirement checks are reading one list, not two that can drift.
PR_GATE_STAGES: frozenset[str] = frozenset({"4", "5", "6", "ci"})


def run_stage_gate(
    stage: str,
    *,
    repo_path: Path,
    gh: GhClient,
    owner: str,
    repo: str,
    spec: Path | None = None,
    pr: int | None = None,
    branch: str | None = None,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> GateVerdict:
    """Dispatch one gate by its CLI stage token. Raises `ValueError` for an unknown token
    or a missing requirement, so the CLI can exit 2 / the pipeline can abort rather than
    reporting an unrun gate as a pass.

    The requirements are per-gate rather than uniform on purpose: gates 1 and 2 need only
    a checkout and a spec, gate 4 only a branch, and 5/6/ci a PR. A single
    `pr: int` on the signature would force the caller to invent a PR number for gates that
    have no PR yet — which is how an unset value silently becomes gate 0's input.

    The spec's sketch was `run_stage_gate(stage, *, cwd, run_id, state, gh, owner,
    repo)`; `cwd`/`run_id`/`state` are unpacked into `repo_path`/`spec`/`pr`/`branch`
    here instead of taken as a blob, because the CLI's caller has no `state` to hand
    (that is the point of the command) and unpacking at the edge keeps each gate's real
    requirements visible at its own signature."""
    token = stage.strip().lower()
    if token == "1":
        if spec is None:
            raise ValueError("gate 1 requires the spec path")
        return run_gate1(repo_path, spec, runner=runner)
    if token == "2":
        if spec is None:
            raise ValueError("gate 2 requires the spec path")
        return run_gate2(spec)
    if token == "4":
        if branch is None:
            raise ValueError("gate 4 requires the branch the PR was opened from")
        return run_gate4(gh, owner=owner, repo=repo, branch=branch, pr=pr)
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


# ---------------------------------------------------------------------------
# Gate 3's mechanical half — existence, not greenness
# ---------------------------------------------------------------------------

# `Test: <name>` ending a line, the shape stage 2 is told to write ("each ends `Test:
# <name>`"). Not only on a line of its own: run 20261010T174659Z's stage 2 wrote every
# criterion as one line, `1. [blocking] ... — confidence: high — Test: test_x`, and an
# own-line-only pattern read that spec as naming no tests at all.
_TEST_LINE_RE = re.compile(
    r"(?:^|\s)Test:\s*`?(?P<name>[A-Za-z_][A-Za-z0-9_]*)`?\s*$", re.MULTILINE
)


def gate3_test_names(
    spec_text: str,
) -> list[str]:
    """Every acceptance-criterion test name the spec names, in order, de-duplicated.

    The names are written by stage 2 and read by stage 3, gate 3 and `pipeline_run`
    alike, so the extraction is one function rather than a `grep "Test:"` copied into
    each. Backticked names are unwrapped because both spellings are in the wild and a
    backtick left in would make the fixed-string existence check pass against a file that
    only mentions the name inside a backticked sentence."""
    names: list[str] = []
    for match in _TEST_LINE_RE.finditer(spec_text):
        name = match.group("name")
        if name not in names:
            names.append(name)
    return names


def gate3_test_names_present(
    spec_text: str,
    *,
    repo_path: Path,
) -> GateVerdict:
    """Every `Test: <name>` in the spec exists somewhere under `tests/`.

    Existence first, green second (G-2), and fixed-string only: a test name is an
    identifier, and letting the name be a regex would let `test_a.*` pass the existence
    check while matching no test. Scoped to `tests/` rather than the spec directory, for
    the same reason the prose gate is: a spec that merely repeats its own test names
    must not satisfy this. Plain Python rather than `rg`, so the verdict does not depend
    on which binaries the host happens to have on PATH (CI has no `rg`).

    Deliberately *not* the lint/pytest half of gate 3 — that stays prose (see this
    module's docstring). This function is the part that is mechanical enough to stop
    trusting a model with it."""
    names = gate3_test_names(spec_text)
    if not names:
        return GateVerdict(
            passed=False, reason="spec names no acceptance tests (`Test: <name>` lines)"
        )
    tests_dir = repo_path / "tests"
    texts = [
        path.read_text(errors="replace")
        for path in sorted(tests_dir.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    missing = [name for name in names if not any(name in text for text in texts)]
    if missing:
        return GateVerdict(
            passed=False,
            reason=f"acceptance test(s) not found under tests/: {', '.join(missing)}",
        )
    return GateVerdict(passed=True, reason=f"{len(names)} acceptance test(s) present")
