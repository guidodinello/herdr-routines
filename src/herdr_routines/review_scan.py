"""Pure selection for ``kind: review`` jobs (issue 055 Phase A).

No subprocess, no clock reads beyond the injected ``now``: everything is driven through
an injected :class:`~herdr_routines.auto_fix.GhClient`, so the whole selector is
unit-testable with a fake. The module turns one batched GraphQL search into a sorted,
de-duplicated candidate list, drops the skip-reason PRs (author mismatch, ``auto/*``
branches, forks), applies the two load-bearing re-review guards (content marker and
per-target attempt budget), caps to ``max_workers_per_tick`` newest-last, and renders the
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
    review_marker,
)
from herdr_routines.config import Job

log = get_logger(__name__)


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
    author: str
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
    table: author first (a stale login), then the double-review guard, then forks."""
    if pr.author != login:
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
        if not pr.repo or pr.number <= 0 or pr.head_ref == "":
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
    gh: GhClient, job_name: str, candidates: Sequence[Candidate]
) -> tuple[list[Candidate], list[Candidate]]:
    """Split *candidates* into (dispatchable, skipped-by-marker).

    Guard 1: a candidate whose PR already carries **this job's** marker for its target
    is ``review_already_posted``. Another job's marker is not a match (the job name is in
    the marker). A body read that fails (``None``) is *unverified* — the candidate stays
    dispatchable, since the attempt budget, not this guard, bounds a missed marker."""
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
        if any(marker in body for body in bodies):
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
    dispatched = sum(1 for _, disposition in rows if disposition == "dispatched")
    skipped = len(rows) - dispatched
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
        f"- **Repos failed**: {repos_failed}",
    ]
    if truncated_modes:
        lines.append(f"- **Search truncated**: {', '.join(truncated_modes)}")
    lines.append("")
    for cand, disposition in rows:
        lines.append(f"- {cand.target_key}: {disposition}")
    return "\n".join(lines) + "\n"
