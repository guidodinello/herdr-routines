"""Target selectors (issue 073): code decides what a routine run acts on, before any agent.

A job names a selector under `pick:`; `tick._run_routine` (the tick and `herdr-routines run`
both land there) calls `select_target` before dispatch. No target means the run is skipped
(`no_target`), a target reaches the prompt as `$ROUTINE_TARGET`, the history record and the
verify gates. A selector that cannot answer (gh failure, truncated listing, an unreadable
review thread) is `SelectorFailed`, never "no target": an error must not look like an idle
queue.

A multi-target selector (issue 076, `MULTI_TARGET_REGISTRY`) answers with a `Classification`
instead: every PR it looked at with its verdict, and the ordered ones to act on. It goes
through `select_targets`.

Every network call goes through the injected `GhClient`, which bounds each call with a
timeout. The one non-gh call is the local `git remote get-url origin` lookup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from herdr_routines import verify
from herdr_routines.auto_fix import (
    GhClient,
    _author_login,
    effective_trusted_authors,
    repo_owner_and_name,
)
from herdr_routines.config import MULTI_TARGET_SELECTORS, Job, TargetSelector
from herdr_routines.gates import remote_owner_and_repo
from herdr_routines.repos import is_repo_less
from herdr_routines.review_scan import Classification, classify_review_candidates
from herdr_routines.verify import VerifyTarget

log = logging.getLogger(__name__)

# What code picks is exactly what `review_posted` later checks.
SelectedTarget = VerifyTarget

# One page of open PRs. A listing that fills it may have more behind it (newest first), so
# the lowest unreviewed number could be missing: that is a failure, not a pick.
PR_LIST_LIMIT = 100

BOT_LOGIN_MARKERS = ("dependabot", "github-actions", "copilot")
BOT_LOGIN_SUFFIX = "[bot]"
# `gh` shows a GitHub App's login as `app/<name>` (e.g. `app/dependabot`).
APP_LOGIN_PREFIX = "app/"


@dataclass(frozen=True, slots=True)
class SelectorContext:
    job: Job
    gh: GhClient
    trusted_review_authors: tuple[str, ...]
    # The attempt budget of `my_open_prs` reads the job's own history.
    history_path: Path | None = None


@dataclass(frozen=True, slots=True)
class SelectorFailed:
    error: str
    # The `reason` the run's `failed` record carries.
    reason: str = "selector_failed"


class SelectorError(RuntimeError):
    """A selector that cannot answer, with the `reason` its `failed` record should carry
    (`gh_auth_missing` and `search_failed` are the reasons `kind: review` always recorded)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _job_owner_and_repo(job: Job) -> tuple[str, str]:
    """`repository:` when the job has one (the selector runs before `ensure_repo`, so a
    managed clone may not exist yet); otherwise the checkout's `origin`."""
    if job.repository:
        return repo_owner_and_name(job.repository)
    return remote_owner_and_repo(job.repo)


def job_own_repo(job: Job) -> str | None:
    """`owner/repo` of the job's own checkout, or None for a repo-less job (`kind: review`,
    whose `repo` is the managed base dir that per-repo checkouts land under). Raises when
    the checkout's `origin` cannot be read."""
    if is_repo_less(job):
        return None
    owner, repo = _job_owner_and_repo(job)
    return f"{owner}/{repo}"


def _is_bot_author(pr: Mapping[str, object]) -> bool:
    """A bot author by gh's own flag, by an `app/` login, or by name. A deleted ("ghost")
    author has no login and is not a bot, so its PR is still a candidate."""
    author = pr.get("author")
    if isinstance(author, dict) and author.get("is_bot") is True:
        return True
    login = _author_login(pr)
    if not isinstance(login, str):
        return False
    login = login.casefold()
    return (
        login.startswith(APP_LOGIN_PREFIX)
        or login.endswith(BOT_LOGIN_SUFFIX)
        or any(marker in login for marker in BOT_LOGIN_MARKERS)
    )


def oldest_unreviewed_pr(ctx: SelectorContext) -> SelectedTarget | None:
    """The lowest-numbered open, non-draft, non-bot PR with no trusted code review on it,
    in the job's own repo. Raises when any probe cannot answer."""
    owner, repo = _job_owner_and_repo(ctx.job)
    trusted = effective_trusted_authors(
        ctx.trusted_review_authors, viewer_login=ctx.gh.api_user()
    )
    prs = ctx.gh.pr_list(owner=owner, repo=repo, state="open", limit=PR_LIST_LIMIT)
    if len(prs) >= PR_LIST_LIMIT:
        raise RuntimeError(
            f"{owner}/{repo} has {len(prs)} open PRs, the listing limit: "
            "the oldest unreviewed one may be missing"
        )
    candidates: list[int] = []
    for pr in prs:
        number = pr.get("number")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or pr.get("isDraft") is True
            or _is_bot_author(pr)
        ):
            continue
        candidates.append(number)
    for number in sorted(candidates):
        bodies = ctx.gh.pr_review_bodies(owner=owner, repo=repo, number=number)
        if bodies is None:
            # Unverifiable: guessing "unreviewed" risks a double review, guessing
            # "reviewed" silently skips the PR.
            raise RuntimeError(
                f"could not read the review thread of {owner}/{repo}#{number}"
            )
        if not any(
            verify.is_trusted_review(
                b, marker=verify.CODE_REVIEW_HEADER, trusted=trusted
            )
            for b in bodies
        ):
            return SelectedTarget(owner, repo, number)
    return None


def my_open_prs(ctx: SelectorContext) -> Classification:
    """The open PRs the authenticated user opened and/or was asked to review, in every repo,
    through the review scan's search, skip rules and re-review guards (marker and attempt
    budget), capped to `max_targets_per_run`. Raises `SelectorError` when the answer is
    unknowable: an unknown search result must never read as "nothing to review"."""
    job = ctx.job
    if not job.select:
        raise SelectorError("selector_failed", "my_open_prs needs a 'select' list")
    if ctx.history_path is None:
        raise SelectorError(
            "selector_failed",
            "my_open_prs needs the job's history for its attempt budget",
        )
    try:
        login = ctx.gh.api_user()
    except Exception as e:
        raise SelectorError("gh_auth_missing", str(e)) from e
    classification = classify_review_candidates(
        ctx.gh,
        job,
        login=login,
        history_path=ctx.history_path,
        trusted_authors=effective_trusted_authors(
            ctx.trusted_review_authors, viewer_login=login
        ),
    )
    if classification is None:
        raise SelectorError("search_failed", "the PR search failed")
    return classification


TARGET_SELECTORS: dict[
    TargetSelector, Callable[[SelectorContext], SelectedTarget | None]
] = {
    "oldest_unreviewed_pr": oldest_unreviewed_pr,
}

MULTI_TARGET_REGISTRY: dict[
    TargetSelector, Callable[[SelectorContext], Classification]
] = {
    "my_open_prs": my_open_prs,
}


def select_target(ctx: SelectorContext) -> SelectedTarget | SelectorFailed | None:
    """Run the job's selector: a target, None for "nothing to do", or `SelectorFailed`.
    The one pre-dispatch entry point, so a scheduled tick and a manual run behave alike."""
    pick = ctx.job.pick
    if pick is None:
        return None
    if pick in MULTI_TARGET_SELECTORS:
        return SelectorFailed(f"selector {pick} returns several targets")
    try:
        return TARGET_SELECTORS[pick](ctx)
    except Exception as e:  # noqa: BLE001 — a selector that blows up has not said "nothing to do"
        log.warning("%s: selector %s failed: %s", ctx.job.name, pick, e)
        return SelectorFailed(str(e))


def select_targets(ctx: SelectorContext) -> Classification | SelectorFailed:
    """Run the job's multi-target selector: the `Classification` of what it found, or
    `SelectorFailed`. Same fail-closed rule as `select_target`."""
    pick = ctx.job.pick
    if pick is None or pick not in MULTI_TARGET_REGISTRY:
        return SelectorFailed(f"selector {pick} is not a multi-target selector")
    try:
        return MULTI_TARGET_REGISTRY[pick](ctx)
    except SelectorError as e:
        log.warning("%s: selector %s failed: %s", ctx.job.name, pick, e)
        return SelectorFailed(str(e), reason=e.reason)
    except Exception as e:  # noqa: BLE001 — a selector that blows up has not said "nothing to do"
        log.warning("%s: selector %s failed: %s", ctx.job.name, pick, e)
        return SelectorFailed(str(e))
