"""Pure selection behind the ``my_open_prs`` selector (issues 055, 076).

No subprocess, no clock reads beyond the injected ``now``: everything is driven through
an injected :class:`~herdr_routines.auto_fix.GhClient`, so the whole selector is
unit-testable with a fake. The module turns one batched GraphQL search into a sorted,
de-duplicated candidate list, drops the skip-reason PRs (author mismatch, ``auto/*``
branches, forks), applies the two load-bearing re-review guards (content marker and
per-target attempt budget), caps to ``max_targets_per_run`` newest-last, and renders the
aggregate report.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from logger import get_logger

from herdr_routines.auto_fix import (
    GhClient,
    ReviewPR,
    attempt_count_for_target,
    has_trusted_marker,
    review_marker,
)
from herdr_routines.config import Job

log = get_logger(__name__)

# Target dispositions that are not failures: a verified "dispatched" and every skip (scan
# skip, attempt budget, marker, cap, a live agent). Any other disposition is the reason a
# target's run failed (a `RunOutcome.reason`, `worktree_failed`, `clone_failed`, ...), so a
# reason nobody listed here still counts as a failure instead of reading as a skip.
DISPATCHED = "dispatched"
SKIP_DISPOSITIONS = frozenset(
    {
        "author_mismatch",
        "routine_owned_branch",
        "fork_pr",
        "max_attempts_exceeded",
        "review_already_posted",
        "over_cap",
        "agent_live",
        "skipped",
    }
)


def is_failure_disposition(disposition: str) -> bool:
    return disposition != DISPATCHED and disposition not in SKIP_DISPOSITIONS


@dataclass(frozen=True, slots=True)
class Candidate:
    """One open PR that passed the scan, or was dropped with a *skip_reason*."""

    target_key: str  # "owner/repo#N"
    repo: str  # "owner/repo"
    owner: str
    repo_name: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    head_ref: str
    author: str | None
    select_mode: str  # "author" | "review_requested"
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """The selector's result. ``candidates`` are eligible and oldest-first; ``skipped``
    carry their own ``skip_reason``; ``truncated_modes`` names any mode that hit GitHub's
    100-node ceiling."""

    candidates: tuple[Candidate, ...]
    skipped: tuple[Candidate, ...]
    truncated_modes: tuple[str, ...] = ()
    enumerated: int = 0


def _candidate(pr: ReviewPR) -> Candidate:
    owner, _, repo_name = pr.repo.partition("/")
    return Candidate(
        target_key=f"{pr.repo}#{pr.number}",
        repo=pr.repo,
        owner=owner,
        repo_name=repo_name,
        number=pr.number,
        title=pr.title,
        url=pr.url,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        head_ref=pr.head_ref,
        author=pr.author,
        select_mode=pr.mode,
    )


def _skip_reason(
    pr: ReviewPR, *, login: str, include_auto_branches: bool
) -> str | None:
    """The first applicable skip reason, or None. Order is checked against the issue's
    table: author first (a stale login), then the double-review guard, then forks.

    ``author_mismatch`` applies only to the ``author`` mode: a ``review_requested`` PR is
    by definition someone else's, so its author never matches ``login``."""
    if pr.mode == "author" and pr.author != login:
        return "author_mismatch"
    if pr.head_ref.startswith("auto/") and not include_auto_branches:
        return "routine_owned_branch"
    if pr.head_repo != pr.repo:
        return "fork_pr"
    return None


def scan_review_targets(
    gh: GhClient, job: Job, *, login: str, limit: int = 100
) -> ScanOutcome | None:
    """One batched search → a ``ScanOutcome``, or ``None`` on any ``gh`` failure.

    ``None`` is the fail-closed answer: the tick records ``failed (search_failed)``,
    because an unknown answer must never read as "nothing to review"."""
    modes = list(job.select or ())
    result = gh.search_prs(modes, limit=limit)
    if result is None:
        return None

    seen: set[str] = set()
    candidates: list[Candidate] = []
    skipped: list[Candidate] = []
    enumerated = 0
    for pr in result.prs:
        if pr.number <= 0:
            continue
        cand = _candidate(pr)
        if cand.target_key in seen:
            continue
        seen.add(cand.target_key)
        enumerated += 1
        reason = _skip_reason(
            pr, login=login, include_auto_branches=job.include_auto_branches
        )
        if reason is not None:
            skipped.append(replace(cand, skip_reason=reason))
        else:
            candidates.append(cand)

    candidates.sort(key=lambda c: (c.created_at, c.updated_at, c.target_key))
    skipped.sort(key=lambda c: c.target_key)
    return ScanOutcome(
        candidates=tuple(candidates),
        skipped=tuple(skipped),
        truncated_modes=result.truncated_modes,
        enumerated=enumerated,
    )


def apply_marker_guard(
    gh: GhClient,
    job_name: str,
    candidates: Sequence[Candidate],
    *,
    trusted_authors: frozenset[str],
) -> tuple[list[Candidate], list[Candidate]]:
    """Split *candidates* into (dispatchable, skipped-by-marker).

    Guard 1: a candidate whose PR already carries **this job's** marker for its target,
    posted by a trusted author (`has_trusted_marker`), is ``review_already_posted``.
    Another job's marker is not a match (the job name is in the marker), and neither is
    a copy posted by anyone else. A body read that fails (``None``) is *unverified* — the
    candidate stays dispatchable, since the attempt budget, not this guard, bounds a
    missed marker."""
    dispatchable: list[Candidate] = []
    skipped: list[Candidate] = []
    for cand in candidates:
        bodies = gh.pr_review_bodies(
            owner=cand.owner, repo=cand.repo_name, number=cand.number
        )
        if bodies is None:
            dispatchable.append(cand)
            continue
        marker = review_marker(job_name, cand.target_key)
        if has_trusted_marker(bodies, marker, trusted_authors):
            skipped.append(replace(cand, skip_reason="review_already_posted"))
        else:
            dispatchable.append(cand)
    return dispatchable, skipped


def apply_attempt_budget(
    history_path: Path,
    job_name: str,
    candidates: Sequence[Candidate],
    *,
    max_attempts: int,
) -> tuple[list[Candidate], list[Candidate]]:
    """Guard 2: drop candidates already at the per-target attempt budget.

    Keyed on ``owner/repo#N`` (via ``attempt_count_for_target``), so ``#7`` in one repo
    never consumes ``#7``'s budget in another."""
    dispatchable: list[Candidate] = []
    skipped: list[Candidate] = []
    for cand in candidates:
        attempt = attempt_count_for_target(history_path, job_name, cand.target_key)
        if attempt >= max_attempts:
            skipped.append(replace(cand, skip_reason="max_attempts_exceeded"))
        else:
            dispatchable.append(cand)
    return dispatchable, skipped


def cap_candidates(
    candidates: Sequence[Candidate], max_workers: int
) -> tuple[list[Candidate], list[Candidate]]:
    """Keep the oldest *max_workers* candidates; the rest are ``over_cap``.

    Candidates are already oldest-first, so the cap starves the newest, never the
    backlog."""
    kept = list(candidates[:max_workers])
    over = [replace(c, skip_reason="over_cap") for c in candidates[max_workers:]]
    return kept, over


@dataclass(frozen=True, slots=True)
class Classification:
    """The pre-dispatch verdicts of the review selector, shared by ``tick`` and the
    read-only ``review-scan`` CLI (issue 066).

    ``rows`` is the tick's pre-dispatch row contract in tick order — scan skips, attempt
    budget skips, marker skips, ``over_cap`` — each carrying the stage's fallback
    disposition, so both callers report exactly the same dispositions for the same
    inputs. ``dispatchable`` is what survived every stage *and* the cap; the tick appends
    the post-dispatch dispositions to ``rows`` itself. ``enumerated`` and
    ``candidate_count`` are the post-scan, pre-budget numbers the tick writes as
    ``enumerated=`` / ``candidates=``. ``trusted_authors`` is the set the marker guard
    used, which the post-run gate must use too."""

    rows: tuple[tuple[Candidate, str], ...]
    dispatchable: tuple[Candidate, ...]
    truncated_modes: tuple[str, ...]
    enumerated: int
    candidate_count: int
    trusted_authors: frozenset[str]


def classify_review_candidates(
    gh: GhClient,
    job: Job,
    *,
    login: str,
    history_path: Path,
    trusted_authors: frozenset[str],
    limit: int = 100,
) -> Classification | None:
    """Run the whole selector pipeline once and return its pre-dispatch verdicts.

    The stage order is load-bearing and must stay: ``scan_review_targets`` (``None`` →
    ``None``, fail closed — an unknown search answer never reads as "nothing to
    review") → ``apply_attempt_budget`` (a local history read, the cheapest guard) →
    ``apply_marker_guard`` (one ``gh pr view`` per check) → ``cap_candidates`` against
    ``job.max_targets_per_run``. The routine fan-out dispatches what comes back and
    ``review-scan`` only reports it — neither re-runs a stage.

    The marker guard is spent on the cap window (the oldest ``max_targets_per_run``
    budget survivors) and nothing else: everything past the window is ``over_cap`` by
    definition and can never dispatch this tick, so it must not cost an API call. The
    cap itself still runs last, over ``marker survivors + everything past the window``,
    so the rows, the fallback strings and the newest-first starvation order are exactly
    what the tick has always produced — a marker skip inside the window just lets the
    next survivor up into the kept set."""
    outcome = scan_review_targets(gh, job, login=login, limit=limit)
    if outcome is None:
        return None

    rows: list[tuple[Candidate, str]] = [
        (cand, cand.skip_reason or "skipped") for cand in outcome.skipped
    ]

    budgeted, budget_skipped = apply_attempt_budget(
        history_path,
        job.name,
        outcome.candidates,
        max_attempts=job.max_attempts_per_target,
    )
    rows.extend(
        (cand, cand.skip_reason or "max_attempts_exceeded") for cand in budget_skipped
    )

    window = budgeted[: job.max_targets_per_run]
    checked, marker_skipped = apply_marker_guard(
        gh, job.name, window, trusted_authors=trusted_authors
    )
    rows.extend(
        (cand, cand.skip_reason or "review_already_posted") for cand in marker_skipped
    )

    to_dispatch, over_cap = cap_candidates(
        [*checked, *budgeted[job.max_targets_per_run :]],
        job.max_targets_per_run,
    )
    rows.extend((cand, cand.skip_reason or "over_cap") for cand in over_cap)

    return Classification(
        rows=tuple(rows),
        dispatchable=tuple(to_dispatch),
        truncated_modes=outcome.truncated_modes,
        enumerated=outcome.enumerated,
        candidate_count=len(outcome.candidates),
        trusted_authors=trusted_authors,
    )


def render_review_report(
    *,
    job_name: str,
    run_id: str,
    now: datetime,
    enumerated: int,
    candidate_count: int,
    repos_failed: int,
    rows: Sequence[tuple[Candidate, str]],
    truncated_modes: Sequence[str] = (),
) -> str:
    """The aggregate ``reports/review-<run_id>.md``: counts plus one line per candidate
    with its disposition. Every candidate appears — including skips and why — so the
    report is the audit trail that stands in for a dry-run subcommand in phase A."""
    dispatched = sum(1 for _, disposition in rows if disposition == DISPATCHED)
    failed = sum(1 for _, d in rows if is_failure_disposition(d))
    skipped = len(rows) - dispatched - failed
    lines = [
        f"# Review tick report: {run_id}",
        "",
        f"- **Job**: {job_name}",
        f"- **Time**: {now.isoformat()}",
        "- **Target**: pr",
        f"- **Enumerated**: {enumerated}",
        f"- **Candidates**: {candidate_count}",
        f"- **Dispatched**: {dispatched}",
        f"- **Skipped**: {skipped}",
        f"- **Failed**: {failed}",
        f"- **Repos failed**: {repos_failed}",
    ]
    if truncated_modes:
        lines.append(f"- **Search truncated**: {', '.join(truncated_modes)}")
    lines.append("")
    for cand, disposition in rows:
        lines.append(f"- {cand.target_key}: {disposition}")
    return "\n".join(lines) + "\n"
