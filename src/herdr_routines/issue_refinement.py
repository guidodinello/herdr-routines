"""Issue refinement job helpers (issue 029).

Product-refinement loop: pick a Parking Lot bullet, run author→reviewer
iterations (cap 3), produce a refined issue file + ROADMAP bullet as a PR.

Pure helpers here — no Herdr, no gh, no clock — so tests stay fast.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# Cap per spec: 3 iterations max.
MAX_ITERATIONS = 3

# Job constants per spec (used by tests to assert opencode-only + cron).
ISSUE_REFINEMENT_CRON = "0 22 * * *"
ISSUE_REFINEMENT_AGENT_KIND = "opencode"
ISSUE_REFINEMENT_HEAD_PREFIX = "auto/issue-refinement-"

# ROADMAP Parking Lot header — bullets under this header are candidates.
PARKING_LOT_HEADER = "## Parking lot"

# A bullet is considered blocked if it contains a blocked marker.
BLOCKED_MARKER_RE = re.compile(r"\bblocked\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParkingLotBullet:
    """One Parking Lot bullet parsed from ROADMAP.md."""

    raw: str  # full bullet text (single line)
    title: str  # first line / title portion
    is_blocked: bool


def parse_parking_lot_bullets(roadmap_text: str) -> list[ParkingLotBullet]:
    """Extract bullets under the Parking Lot section.

    Simple: find PARKING_LOT_HEADER, then collect lines starting with "- **"
    or "- " until next top-level header (## ) or EOF. This matches ROADMAP.md's
    current format and is resilient to minor formatting drift.
    """
    lines = roadmap_text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip().startswith(PARKING_LOT_HEADER):
            start = idx
            break
    if start is None:
        return []
    bullets: list[ParkingLotBullet] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("## Parking"):
            break
        if stripped.startswith("- "):
            raw = stripped[2:].strip()
            # Title is before " — " or " - " or first sentence.
            title = raw.split(" — ")[0].split(" - ")[0].strip()
            # Remove markdown bold wrappers for title comparison.
            title_clean = title.replace("**", "").strip()
            is_blocked = bool(BLOCKED_MARKER_RE.search(raw))
            bullets.append(
                ParkingLotBullet(raw=raw, title=title_clean, is_blocked=is_blocked)
            )
    return bullets


def is_covered(bullet: ParkingLotBullet, existing_issue_titles: list[str]) -> bool:
    """Skip bullets already covered by an existing issue file.

    Heuristic: if any existing issue title (lowercased) is a substring of the
    bullet's raw text (lowercased) or vice versa, consider it covered. This
    is intentionally loose — it prevents duplicate refinement, not exact
    matching. Tests pin the behavior.
    """
    bullet_lower = bullet.raw.lower()
    for t in existing_issue_titles:
        tl = t.lower()
        if tl in bullet_lower or bullet_lower in tl:
            return True
    return False


def select_next_bullet(
    bullets: list[ParkingLotBullet],
    existing_issue_titles: list[str],
    *,
    allow_blocked: bool = False,
) -> ParkingLotBullet | None:
    """Pick oldest (first) Parking Lot bullet not covered, skipping blocked unless allowed.

    Iteration order is ROADMAP order (oldest first) — matches spec's
    "oldest-gated / earliest date first". Returns None if nothing eligible.
    """
    for b in bullets:
        if not allow_blocked and b.is_blocked:
            continue
        if is_covered(b, existing_issue_titles):
            continue
        return b
    return None


def loop_should_continue(iteration: int, reviewer_confidence: str) -> bool:
    """Whether author→reviewer loop should do another iteration.

    Consensus = reviewer says confidence: high. Otherwise continue until cap.
    Iteration is 1-indexed count of completed reviewer passes.
    """
    if reviewer_confidence.strip().lower() == "high":
        return False
    return iteration < MAX_ITERATIONS


def validate_issue_frontmatter(frontmatter: Mapping[str, object]) -> list[str]:
    """Return list of missing/invalid required fields; empty means valid."""
    required = ("id", "title", "status", "priority", "area")
    missing: list[str] = []
    for key in required:
        if key not in frontmatter:
            missing.append(key)
    # status must be open for a newly refined issue.
    if frontmatter.get("status") not in (None, "open") and "status" in frontmatter:
        missing.append("status: must be 'open'")
    return missing


def job_is_opencode_only(job_config: dict[str, object]) -> bool:
    """True if job uses opencode only and not claude."""
    kind = job_config.get("agent_kind")
    model = str(job_config.get("model") or "")
    if kind != "opencode":
        return False
    return "claude" not in model.lower()


def head_is_pipeline(head: str) -> bool:
    """Whether head looks like a pipeline PR (auto/pipeline-*)."""
    return head.startswith("auto/pipeline-")


def head_is_issue_refinement(head: str) -> bool:
    """Whether head matches issue-refinement prefix (not pipeline)."""
    return head.startswith(ISSUE_REFINEMENT_HEAD_PREFIX)
