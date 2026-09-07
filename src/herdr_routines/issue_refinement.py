"""Issue refinement job — stage-0 selector (issue 029).

Product refinement: promote one ROADMAP.md *Parking Lot* idea into a buildable
``docs/process/issues/NNN-*.md`` + ROADMAP pointer, delivered as a PR for a human
to merge. The author->reviewer loop itself is prompt-driven inside the job's
opencode session (same architecture as the pipeline orchestrator — there is no
Python stage/spawn loop in this repo, see ROADMAP's "Unify routines + pipeline"
bullet). This module is only the deterministic part: *which* bullet to refine and
whether it is already covered.

Pure filesystem + YAML + one batched ``gh`` call — no HerdrClient, no ``herdr``
binary — mirroring ``pick_feature.py``'s "no Herdr server required" posture so it
runs from the job's first step before any reviewer session exists.

The "already refined" guard (issue 029 Design): a bullet with an open
``auto/issue-refinement-*`` PR is treated as covered. The documented mechanism
("mark it picked in ROADMAP") was never buildable — the job opens a PR and never
writes to ``main`` — so the open-PR check stands in for it, the same substitution
``pick_feature`` makes for pipeline picks (issue 028). The correlation key is a
``Refines-Parking-Lot: <slug>`` marker the selector prints and the job is told to
copy verbatim into the PR body; the guard matches that marker. The title-slug
fallback (for a PR missing the marker) rarely fires — the job titles its PRs
``docs: refine issue NNN — <title>``, which does not slugify to the bullet slug —
so in practice the marker *is* the mechanism, and a marker-less refinement PR can
be re-refined. Any ``gh`` failure fails *safe* — no pick that run — never
fail-open into a duplicate PR every night of a GitHub outage.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import yaml

# Cap per spec: at most 3 author->reviewer passes.
MAX_ITERATIONS = 3

# Job constants per spec (asserted by tests against the real job file).
ISSUE_REFINEMENT_CRON = "0 22 * * *"
ISSUE_REFINEMENT_AGENT_KIND = "opencode"
ISSUE_REFINEMENT_HEAD_PREFIX = "auto/issue-refinement-"
PIPELINE_HEAD_PREFIX = "auto/pipeline-"

# ROADMAP Parking Lot header — bullets under this header are candidates.
PARKING_LOT_HEADER = "## Parking lot"

# A bullet is gated when it carries a structural marker: "Gate: <what>" (the
# ROADMAP Parking Lot convention) or a leading "blocked: <what>". Matched as
# markers, not as the bare word "blocked" — a bullet's folded multi-line body
# routinely mentions blocked panes/agents/calls in passing.
_BLOCKED_RE = re.compile(r"\bGate:|\bblocked:", re.IGNORECASE)
# A bullet is already promoted when it links a docs/process/issues/ file.
_PROMOTED_RE = re.compile(r"docs/process/issues/")

# Body marker the job writes into the PR, matched by the open-PR guard.
PR_MARKER_PREFIX = "Refines-Parking-Lot:"
_PR_MARKER_RE = re.compile(
    rf"{re.escape(PR_MARKER_PREFIX)}\s*([a-z0-9][a-z0-9-]*)", re.IGNORECASE
)

GH_TIMEOUT_SECONDS = 30
FRONTMATTER_DELIM = "---"
REQUIRED_FRONTMATTER = ("id", "title", "status", "priority", "area")


@dataclass(frozen=True, slots=True)
class ParkingLotBullet:
    """One Parking Lot bullet parsed from ROADMAP.md (continuation lines folded in)."""

    raw: str  # full bullet text, continuation lines joined with a space
    title: str  # bolded lead-in, `**`-stripped, before the em dash
    is_blocked: bool  # names an unmet gate / says "blocked"
    is_promoted: bool  # already linked to a docs/process/issues/ file

    @property
    def slug(self) -> str:
        return slugify(self.title)


def slugify(text: str) -> str:
    """Lowercase kebab slug, stable across runs — the PR-correlation key."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_parking_lot_bullets(roadmap_text: str) -> list[ParkingLotBullet]:
    """Bullets under ``## Parking lot``, in file order (oldest first).

    A bullet is ``- `` at column 0; lines indented two spaces continue it; a
    blank line or any other column-0 line ends the section (the Parking Lot is
    the last section of ROADMAP.md, followed only by a "House rule:" paragraph).
    """
    lines = roadmap_text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip() == PARKING_LOT_HEADER),
        None,
    )
    if start is None:
        return []

    bullets: list[ParkingLotBullet] = []
    current: list[str] | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            bullets.append(_finalize_bullet(current))
            current = None

    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            _flush()
            current = [line[2:].strip()]
        elif current is not None and line.startswith("  ") and line.strip():
            current.append(line.strip())
        elif current is not None:
            # blank line or a column-0 non-bullet line — end of this bullet, and
            # (once we have bullets) end of the list.
            _flush()
            if bullets:
                break
    _flush()
    return bullets


def _finalize_bullet(parts: list[str]) -> ParkingLotBullet:
    raw = " ".join(parts).strip()
    lead = re.split(r"\s+[—-]\s+", raw, maxsplit=1)[0]
    title = lead.replace("**", "").strip()
    return ParkingLotBullet(
        raw=raw,
        title=title,
        is_blocked=bool(_BLOCKED_RE.search(raw)),
        is_promoted=bool(_PROMOTED_RE.search(raw)),
    )


def existing_issue_titles(issues_dir: Path) -> list[str]:
    """Frontmatter ``title:`` of every ``docs/process/issues/*.md`` (lenient —
    a malformed file is skipped, not fatal: this is a dedup hint, not a gate)."""
    titles: list[str] = []
    for path in sorted(issues_dir.glob("*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        if not text.startswith(f"{FRONTMATTER_DELIM}\n"):
            continue
        _, _, rest = text.partition(f"{FRONTMATTER_DELIM}\n")
        fm_text, sep, _ = rest.partition(f"\n{FRONTMATTER_DELIM}\n")
        if not sep:
            continue
        try:
            fm = yaml.safe_load(fm_text)
        except yaml.YAMLError:
            continue
        if isinstance(fm, dict) and isinstance(fm.get("title"), str):
            titles.append(fm["title"])
    return titles


def refinement_open_pr_slugs(repo: Path) -> frozenset[str] | None:
    """Parking-Lot slugs that already have an open ``auto/issue-refinement-*`` PR,
    via one batched ``gh pr list``.

    Returns ``None`` on any ``gh`` failure — *not* an empty set. The caller must
    fail safe: an unknown answer means "a refinement PR might already exist, do
    not pick", never "none found, pick anyway". Getting this backwards re-opens
    the same PR every night until a human closes it.

    Primary correlation is the ``Refines-Parking-Lot: <slug>`` body marker the
    job writes; PR-title tokens are a fallback when the marker is absent.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--json", "headRefName,title,body"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: gh pr list failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"warning: gh pr list failed: {proc.stderr.strip()}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("warning: gh pr list returned invalid JSON", file=sys.stderr)
        return None
    if not isinstance(data, list):
        print("warning: gh pr list returned an unexpected shape", file=sys.stderr)
        return None

    slugs: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        head = item.get("headRefName")
        if not isinstance(head, str) or not head.startswith(
            ISSUE_REFINEMENT_HEAD_PREFIX
        ):
            continue  # a non-refinement open PR never covers a bullet
        raw_body = item.get("body")
        body = raw_body if isinstance(raw_body, str) else ""
        marker = _PR_MARKER_RE.search(body)
        if marker:
            slugs.add(marker.group(1).lower())
            continue
        raw_title = item.get("title")
        if isinstance(raw_title, str) and raw_title:
            slugs.add(slugify(raw_title))
    return frozenset(slugs)


def is_covered(
    bullet: ParkingLotBullet,
    issue_titles: list[str],
    open_pr_slugs: frozenset[str],
) -> bool:
    """True when the bullet already has an issue file or an open refinement PR."""
    if bullet.slug in open_pr_slugs:
        return True
    title_lower = bullet.title.lower()
    if any(title_lower and title_lower in slug for slug in open_pr_slugs):
        return True
    raw_lower = bullet.raw.lower()
    for candidate in issue_titles:
        cand = candidate.lower().strip()
        if cand and (cand in raw_lower or raw_lower.startswith(cand)):
            return True
    return False


def select_next_bullet(
    bullets: list[ParkingLotBullet],
    issue_titles: list[str],
    open_pr_slugs: frozenset[str] = frozenset(),
    *,
    allow_blocked: bool = False,
) -> ParkingLotBullet | None:
    """First (oldest) Parking Lot bullet that is not promoted, not covered, and
    (unless ``allow_blocked``) not gated. ``None`` if nothing is eligible."""
    for bullet in bullets:
        if bullet.is_promoted:
            continue
        if bullet.is_blocked and not allow_blocked:
            continue
        if is_covered(bullet, issue_titles, open_pr_slugs):
            continue
        return bullet
    return None


def loop_should_continue(iteration: int, reviewer_confidence: str) -> bool:
    """Whether the author->reviewer loop should run another pass.

    Consensus = reviewer reports ``confidence: high``. Otherwise continue until
    the cap. ``iteration`` is the 1-indexed count of completed reviewer passes.
    """
    if reviewer_confidence.strip().lower() == "high":
        return False
    return iteration < MAX_ITERATIONS


def validate_issue_frontmatter(frontmatter: Mapping[str, object]) -> list[str]:
    """Problems with a refined issue's frontmatter; empty list == valid.

    Every required key must be present and non-empty (an explicit ``null`` counts
    as missing), and ``status`` must be ``open`` for a newly refined issue.
    """
    errors: list[str] = []
    for key in REQUIRED_FRONTMATTER:
        value = frontmatter.get(key)
        if key not in frontmatter or value is None or value == "":
            errors.append(key)
    status = frontmatter.get("status")
    if status not in (None, "", "open") and "status" not in errors:
        errors.append("status must be 'open' for a newly refined issue")
    return errors


def job_is_opencode_only(job_config: Mapping[str, object]) -> bool:
    """True when the job runs opencode with no Claude anywhere."""
    if job_config.get("agent_kind") != "opencode":
        return False
    model = str(job_config.get("model") or "")
    return "claude" not in model.lower()


def head_is_pipeline(head: str) -> bool:
    return head.startswith(PIPELINE_HEAD_PREFIX)


def head_is_issue_refinement(head: str) -> bool:
    return head.startswith(ISSUE_REFINEMENT_HEAD_PREFIX)


def run_refine_issue(
    repo: Path,
    *,
    issues_dir: Path | None = None,
    roadmap_path: Path | None = None,
    allow_blocked: bool = False,
    out: TextIO | None = None,
) -> int:
    """Entry point behind ``herdr-routines refine-issue``.

    Prints the selected Parking Lot bullet followed by its
    ``Refines-Parking-Lot: <slug>`` marker (for the job to copy into the PR
    body), and exits 0. Exits 1 when nothing is eligible *or* when the open-PR
    guard could not be evaluated (fail safe — see ``refinement_open_pr_slugs``).
    """
    stream = out if out is not None else sys.stdout
    resolved_issues_dir = issues_dir or repo / "docs" / "process" / "issues"
    resolved_roadmap = roadmap_path or repo / "ROADMAP.md"

    try:
        roadmap_text = resolved_roadmap.read_text()
    except OSError as exc:
        print(f"error: cannot read {resolved_roadmap}: {exc}", file=sys.stderr)
        return 1

    bullets = parse_parking_lot_bullets(roadmap_text)
    if not bullets:
        print("no Parking Lot bullets found", file=sys.stderr)
        return 1

    open_pr_slugs = refinement_open_pr_slugs(repo)
    if open_pr_slugs is None:
        print(
            "error: could not list open PRs — refusing to pick (a refinement PR "
            "may already exist); will retry next run",
            file=sys.stderr,
        )
        return 1

    picked = select_next_bullet(
        bullets,
        existing_issue_titles(resolved_issues_dir),
        open_pr_slugs,
        allow_blocked=allow_blocked,
    )
    if picked is None:
        print("no Parking Lot idea to refine", file=sys.stderr)
        return 1

    stream.write(f"{picked.raw}\n\n{PR_MARKER_PREFIX} {picked.slug}\n")
    return 0
