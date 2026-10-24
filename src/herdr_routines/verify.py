"""Post-run verify gates (issue 071): what proves a run worked, as a job field.

A routine run is `done` once its agent settles with a non-empty `$ROUTINE_REPORT`, which
is the agent's word and nothing more (fitted #839: a one-line report claimed a review that
was never posted). A job lists gates under `verify:`; `runner.execute_run` runs them after
the agent settles and fails the run on any miss. `kind: review` calls `review_posted`
directly for its per-target post-condition, so there is one implementation of it.

Every external call goes through the injected `GhClient` (which bounds each call with a
timeout). A probe that errors or times out is a failed gate, never an exception and never
a pass.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from herdr_routines.auto_fix import (
    GhClient,
    PostedBody,
    effective_trusted_authors,
    is_trusted_author,
)
from herdr_routines.config import Job, VerifyGate
from herdr_routines.gates import GateVerdict

log = logging.getLogger(__name__)

# The header the code-review skill opens its review body with, used as the marker when the
# caller has none (a routine job's agent picks its own target and has no per-target marker).
CODE_REVIEW_HEADER = "### Code review"

# Gate reasons. `kind: review` records these verbatim as its dispositions, so they are the
# same strings its post-condition used before it called the gate.
REASON_TARGET_UNDECLARED = "target_undeclared"
REASON_VERIFICATION_FAILED = "review_verification_failed"
REASON_NOT_POSTED = "review_not_posted"

# `Target: <owner>/<repo>#<n>` on a line of its own, the shape `build_review_prompt` emits.
_TARGET_RE = re.compile(
    r"^\s*Target:\s*(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+)#(?P<number>\d+)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class VerifyTarget:
    owner: str
    repo: str
    number: int

    @property
    def ref(self) -> str:
        """`<owner>/<repo>#<n>`, the form `$ROUTINE_TARGET` and `extra.target` carry."""
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass(frozen=True, slots=True)
class VerifyContext:
    """What the runner already knows when the agent has settled. There is deliberately no
    worktree path: `execute_run` never learns it (herdr owns it), and the first gate that
    needs one is issue 070's."""

    job: Job
    run_id: str
    started_at: datetime
    report_path: Path
    gh: GhClient
    trusted_review_authors: tuple[str, ...]
    # Known to code for `kind: review`; a routine's agent chooses its own, so it is parsed
    # from the report's `Target:` line when this is None.
    target: VerifyTarget | None = None
    # Precomputed trusted set; resolved from `trusted_review_authors` plus the gh viewer
    # when None.
    trusted_authors: frozenset[str] | None = None
    marker: str | None = None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    gate: str
    passed: bool
    reason: str | None


def parse_declared_target(text: str) -> VerifyTarget | None:
    """The one `Target:` line a report declares. None when there is none, it is
    malformed, or the report names two different targets (ambiguous reads as undeclared)."""
    targets = {
        VerifyTarget(m["owner"], m["repo"], int(m["number"]))
        for m in _TARGET_RE.finditer(text)
    }
    return targets.pop() if len(targets) == 1 else None


def _fail(reason: str) -> GateVerdict:
    return GateVerdict(passed=False, reason=reason)


def is_trusted_review(
    posted: PostedBody, *, marker: str, trusted: frozenset[str]
) -> bool:
    """The one definition of "this body is a review" (issue 073): a trusted author's body
    carrying the review marker. `review_posted` adds a freshness bound on top; the
    `oldest_unreviewed_pr` selector uses it as is, so "reviewed" cannot drift between the
    two."""
    return marker in posted.body and is_trusted_author(posted.author, trusted)


def review_posted(ctx: VerifyContext) -> GateVerdict:
    """Passes when the target PR carries a review or comment from a trusted author that
    was posted at or after the run's start and carries the review marker. The freshness
    check is what stops an older review satisfying a run that posted nothing."""
    target = ctx.target
    if target is None:
        try:
            target = parse_declared_target(ctx.report_path.read_text())
        except (OSError, UnicodeDecodeError):
            target = None
    if target is None:
        return _fail(REASON_TARGET_UNDECLARED)

    try:
        trusted = ctx.trusted_authors
        if trusted is None:
            trusted = effective_trusted_authors(
                ctx.trusted_review_authors, viewer_login=ctx.gh.api_user()
            )
        bodies = ctx.gh.pr_review_bodies(
            owner=target.owner, repo=target.repo, number=target.number
        )
    except Exception as e:  # noqa: BLE001 — a probe failure is a failed gate, never a pass
        log.warning("%s: review_posted probe failed: %s", ctx.job.name, e)
        return _fail(REASON_VERIFICATION_FAILED)
    if bodies is None:
        return _fail(REASON_VERIFICATION_FAILED)

    marker = ctx.marker or CODE_REVIEW_HEADER
    # GitHub stamps whole seconds, so a review posted in the run's first second would
    # otherwise compare earlier than the sub-second start.
    since = ctx.started_at.replace(microsecond=0)
    for posted in bodies:
        if (
            is_trusted_review(posted, marker=marker, trusted=trusted)
            and posted.created_at is not None
            and posted.created_at >= since
        ):
            return GateVerdict(passed=True)
    return _fail(REASON_NOT_POSTED)


def report_wellformed(ctx: VerifyContext) -> GateVerdict:
    """Fails an empty report and one with literal `\\n` escapes standing in for newlines
    (the #839 report). A cheap signal of a model that wrote the report without doing the
    work, never a substitute for a real gate."""
    try:
        text = ctx.report_path.read_text()
    except (OSError, UnicodeDecodeError):
        return _fail("report_unreadable")
    if not text.strip():
        return _fail("report_empty")
    # A report that merely discusses `\n` (a regex, say) still has real lines.
    real_lines = [line for line in text.splitlines() if line.strip()]
    if "\\n" in text and len(real_lines) <= 1:
        return _fail("report_literal_escapes")
    return GateVerdict(passed=True)


VERIFY_GATES: dict[VerifyGate, Callable[[VerifyContext], GateVerdict]] = {
    "review_posted": review_posted,
    "report_wellformed": report_wellformed,
}


def run_verify_gates(
    names: Iterable[VerifyGate], ctx: VerifyContext
) -> tuple[VerifyResult, ...]:
    """Run every listed gate (none short-circuits) and return each result in order."""
    results: list[VerifyResult] = []
    for name in names:
        try:
            verdict = VERIFY_GATES[name](ctx)
        except Exception as e:  # noqa: BLE001 — a gate that blows up has not passed
            log.warning("%s: verify gate %s raised: %s", ctx.job.name, name, e)
            verdict = _fail(f"gate_error: {e}")
        results.append(VerifyResult(name, verdict.passed, verdict.reason))
    return tuple(results)
