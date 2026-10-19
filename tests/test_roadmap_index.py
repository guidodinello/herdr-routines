"""Guards ROADMAP.md as a pure index over ``docs/process/issues/``.

Each issue file's frontmatter (``status:``, ``priority:``, ``gate:``) is the
single source of truth. ROADMAP once repeated status in prose ("— `done`",
"`low`. `done`.") and drifted from the files, so these tests pin two things
against the real files: every issue file is linked from ROADMAP, and no ROADMAP
entry that links an issue file restates its status or priority.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROADMAP_PATH = REPO_ROOT / "ROADMAP.md"
ISSUES_DIR = REPO_ROOT / "docs" / "process" / "issues"

# A link to one specific issue file, e.g. docs/process/issues/034-gate-ci-checks.md.
_ISSUE_LINK_RE = re.compile(r"docs/process/issues/(\d{3})-[\w-]+\.md")

# Frontmatter values that must not be restated in ROADMAP prose.
_STATUS_TOKENS = ("open", "in-progress", "done", "blocked")
_PRIORITY_TOKENS = ("high", "medium", "low")
_FORBIDDEN_RE = re.compile(
    "|".join(
        [rf"`{re.escape(t)}`" for t in (*_STATUS_TOKENS, *_PRIORITY_TOKENS)]
        + [r"\bstatus:"]
    )
)

# The bolded lead-in is the item's title; titles may legitimately name a herdr
# agent state (e.g. "Approval path for `blocked` runs"), so it isn't checked.
_BOLD_TITLE_RE = re.compile(r"\*\*.*?\*\*")


def _entries(text: str) -> list[str]:
    """Split markdown into bullets and paragraphs, continuation lines folded in.

    Same shape ``issue_refinement.parse_parking_lot_bullets`` relies on: a
    bullet starts with ``- `` at column 0 and its indented lines continue it; a
    paragraph is a run of non-blank lines. Blank lines and headings end both.
    """
    entries: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        starts_new = line.startswith(("- ", "#")) or not line.strip()
        if starts_new and current:
            entries.append(" ".join(current))
            current = []
        if line.strip() and not line.startswith("#"):
            current.append(line.strip())
    if current:
        entries.append(" ".join(current))
    return entries


def test_every_issue_file_is_linked_from_roadmap() -> None:
    roadmap = ROADMAP_PATH.read_text()
    missing = sorted(
        path.name
        for path in ISSUES_DIR.glob("*.md")
        if f"docs/process/issues/{path.name}" not in roadmap
    )
    assert not missing, f"issue files not linked from ROADMAP.md: {missing}"


def test_roadmap_entries_do_not_restate_issue_status() -> None:
    offenders: list[str] = []
    for entry in _entries(ROADMAP_PATH.read_text()):
        issue_ids = _ISSUE_LINK_RE.findall(entry)
        if not issue_ids:
            continue
        hits = _FORBIDDEN_RE.findall(_BOLD_TITLE_RE.sub("", entry))
        if hits:
            offenders.append(
                f"issue(s) {', '.join(issue_ids)}: {sorted(set(hits))} in {entry[:80]!r}"
            )
    assert not offenders, (
        "ROADMAP.md restates status/priority that lives in the issue file's "
        "frontmatter — remove it:\n" + "\n".join(offenders)
    )


def test_entry_parser_folds_bullets_and_paragraphs() -> None:
    text = (
        "## Now\n\n"
        "Intro paragraph\nspanning two lines.\n\n"
        "- **A** — first\n  continued.\n"
        "- **B** — second.\n\n"
        "Trailing paragraph.\n"
    )
    assert _entries(text) == [
        "Intro paragraph spanning two lines.",
        "- **A** — first continued.",
        "- **B** — second.",
        "Trailing paragraph.",
    ]


def test_status_guard_flags_restated_status_but_not_title() -> None:
    restated = "- **Retries** — `low`. `done`. → [`008`](docs/process/issues/008-x.md)"
    title_only = "- **Approval path for `blocked` runs** — notify. → [`007`](docs/process/issues/007-x.md)"
    assert _FORBIDDEN_RE.findall(_BOLD_TITLE_RE.sub("", restated)) == [
        "`low`",
        "`done`",
    ]
    assert _FORBIDDEN_RE.findall(_BOLD_TITLE_RE.sub("", title_only)) == []
