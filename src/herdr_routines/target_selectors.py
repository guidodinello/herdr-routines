"""Target selectors (issue 073): code decides what a routine run acts on, before any agent.

A job names a selector under `pick:`; `tick._process_job` and `cli._cmd_run` call
`select_target` before dispatch. No target means the run is skipped (`no_target`), a target
reaches the prompt as `$ROUTINE_TARGET`, the history record and the verify gates. A selector
that cannot answer (gh failure, truncated listing, an unreadable review thread) is
`SelectorFailed`, never "no target": an error must not look like an idle queue.

Every network call goes through the injected `GhClient`, which bounds each call with a
timeout. The one non-gh call is the local `git remote get-url origin` lookup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from herdr_routines import verify
from herdr_routines.auto_fix import (
    GhClient,
    _author_login,
    effective_trusted_authors,
    repo_owner_and_name,
)
from herdr_routines.config import Job, TargetSelector
from herdr_routines.gates import remote_owner_and_repo
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


@dataclass(frozen=True, slots=True)
class SelectorFailed:
    error: str


def _job_owner_and_repo(job: Job) -> tuple[str, str]:
    """`repository:` when the job has one (the selector runs before `ensure_repo`, so a
    managed clone may not exist yet); otherwise the checkout's `origin`."""
    if job.repository:
        return repo_owner_and_name(job.repository)
    return remote_owner_and_repo(job.repo)


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


TARGET_SELECTORS: dict[
    TargetSelector, Callable[[SelectorContext], SelectedTarget | None]
] = {
    "oldest_unreviewed_pr": oldest_unreviewed_pr,
}


def select_target(ctx: SelectorContext) -> SelectedTarget | SelectorFailed | None:
    """Run the job's selector: a target, None for "nothing to do", or `SelectorFailed`.
    The one pre-dispatch entry point, so a scheduled tick and a manual run behave alike."""
    pick = ctx.job.pick
    if pick is None:
        return None
    try:
        return TARGET_SELECTORS[pick](ctx)
    except Exception as e:  # noqa: BLE001 — a selector that blows up has not said "nothing to do"
        log.warning("%s: selector %s failed: %s", ctx.job.name, pick, e)
        return SelectorFailed(str(e))
