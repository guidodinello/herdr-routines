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


def select_next(issues: list[Issue]) -> Issue | None:
    """Highest-priority, lowest-id issue with status == 'open'; None if none open."""
    open_issues = [issue for issue in issues if issue.status == OPEN_STATUS]
    if not open_issues:
        return None
    return min(open_issues, key=lambda issue: issue.sort_key)


def render_feature_idea(issue: Issue) -> str:
    """FEATURE_IDEA text for the pipeline orchestrator's stage 1 input (spec.md, orchestrator-prompt.md)."""
    return f"{issue.title} (herdr-routines issue {issue.id}, {issue.path.as_posix()}).\n\n{issue.body}"


def mark_in_progress(issue: Issue) -> None:
    """Flip an issue's frontmatter status to in-progress. Atomic write (tmpfile + rename, G-9)."""
    text = issue.path.read_text()
    marker = f"status: {issue.status}\n"
    if text.count(marker) != 1:
        raise IssueParseError(
            f"{issue.path}: expected exactly one 'status: {issue.status}' line, "
            f"found {text.count(marker)}"
        )
    updated = text.replace(marker, "status: in-progress\n", 1)
    tmp = issue.path.with_suffix(".md.tmp")
    tmp.write_text(updated)
    tmp.replace(issue.path)


def run_pick_feature(
    issues_dir: Path, mark: bool = False, out: TextIO | None = None
) -> int:
    """Entry point behind `herdr-routines pick-feature`."""
    # Resolved lazily so callers that swap sys.stdout (pytest capsys) are honored.
    stream: TextIO = out if out is not None else sys.stdout
    try:
        issues = load_issues(issues_dir)
    except IssueParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    picked = select_next(issues)
    if picked is None:
        print("no open issues", file=sys.stderr)
        return 1
    if mark:
        mark_in_progress(picked)
    stream.write(render_feature_idea(picked) + "\n")
    return 0
