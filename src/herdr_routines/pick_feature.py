"""Select the next open issue from docs/process/issues/ for the pipeline's stage 0.

Pure filesystem + YAML frontmatter parsing: no HerdrClient, no `herdr` binary,
mirrors gc.py's "no Herdr server required" posture so this stays usable from the
launcher before any workspace/pane exists.

See docs/process/README.md for the frontmatter convention and ROADMAP.md's
"Autonomous task selection for the pipeline" for why this exists and what
promoting it beyond a manually-invoked helper is still gated on.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import yaml

from herdr_routines.claims import claim_issue, default_claims_path, load_claims

FRONTMATTER_DELIM = "---"
OPEN_STATUS = "open"
REQUIRED_FIELDS = ("id", "title", "status", "priority", "area")
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class IssueParseError(Exception):
    """Raised when an issue file is missing or has malformed frontmatter."""


@dataclass(frozen=True, slots=True)
class Issue:
    id: str
    title: str
    status: str
    priority: str
    area: str
    path: Path
    body: str

    @property
    def sort_key(self) -> tuple[int, str]:
        """Highest priority first, then lowest id — unknown priorities sort last."""
        return (PRIORITY_ORDER.get(self.priority, len(PRIORITY_ORDER)), self.id)


def parse_issue(path: Path) -> Issue:
    text = path.read_text()
    if not text.startswith(f"{FRONTMATTER_DELIM}\n"):
        raise IssueParseError(f"{path}: missing frontmatter delimiter")
    _, _, rest = text.partition(f"{FRONTMATTER_DELIM}\n")
    frontmatter_text, sep, body = rest.partition(f"\n{FRONTMATTER_DELIM}\n")
    if not sep:
        raise IssueParseError(f"{path}: unterminated frontmatter")
    frontmatter = yaml.safe_load(frontmatter_text)
    if not isinstance(frontmatter, dict):
        raise IssueParseError(f"{path}: frontmatter is not a mapping")
    missing = [key for key in REQUIRED_FIELDS if key not in frontmatter]
    if missing:
        raise IssueParseError(f"{path}: missing required field(s) {missing}")
    return Issue(
        id=str(frontmatter["id"]),
        title=str(frontmatter["title"]),
        status=str(frontmatter["status"]),
        priority=str(frontmatter["priority"]),
        area=str(frontmatter["area"]),
        path=path,
        body=body.strip(),
    )


def load_issues(issues_dir: Path) -> list[Issue]:
    """All issues in issues_dir, sorted by filename. Raises if the dir has no .md files."""
    paths = sorted(issues_dir.glob("*.md"))
    if not paths:
        raise IssueParseError(f"{issues_dir}: no issue files found")
    return [parse_issue(p) for p in paths]


def select_next(
    issues: list[Issue], claimed_ids: frozenset[str] = frozenset()
) -> Issue | None:
    """Highest-priority, lowest-id issue with status == 'open' and not already
    claimed (issue 041: a claim no longer changes `status`, so it must be
    checked separately here); None if nothing open and unclaimed."""
    open_issues = [
        issue
        for issue in issues
        if issue.status == OPEN_STATUS and issue.id not in claimed_ids
    ]
    if not open_issues:
        return None
    return min(open_issues, key=lambda issue: issue.sort_key)


def render_feature_idea(issue: Issue) -> str:
    """FEATURE_IDEA text for the pipeline orchestrator's stage 1 input (spec.md, orchestrator-prompt.md)."""
    return f"{issue.title} (herdr-routines issue {issue.id}, {issue.path.as_posix()}).\n\n{issue.body}"


def run_pick_feature(
    issues_dir: Path,
    mark: bool = False,
    out: TextIO | None = None,
    claims_path: Path | None = None,
) -> int:
    """Entry point behind `herdr-routines pick-feature`.

    A pick is recorded as a claim in `claims_path` (default
    `claims.default_claims_path()`), never as an edit to the issue file in
    `$REPO_PARENT` (issue 041: that edit collided with the implementing PR's own
    edit to the same line once merged). `$REPO_PARENT` stays a clean mirror of
    `origin/main`, and a claimed issue is skipped by future picks until it either
    lands as `status: done` on `main` or its claim is released (issue 040).
    """
    # Resolved lazily so callers that swap sys.stdout (pytest capsys) are honored.
    stream: TextIO = out if out is not None else sys.stdout
    resolved_claims_path = (
        claims_path if claims_path is not None else default_claims_path()
    )
    try:
        issues = load_issues(issues_dir)
    except IssueParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    claimed_ids = frozenset(load_claims(resolved_claims_path))
    picked = select_next(issues, claimed_ids)
    if picked is None:
        print("no open issues", file=sys.stderr)
        return 1
    if mark:
        claim_issue(resolved_claims_path, picked.id)
    stream.write(render_feature_idea(picked) + "\n")
    return 0
