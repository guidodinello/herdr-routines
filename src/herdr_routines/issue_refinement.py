"""Issue refinement job — stage-0 selector (issue 029).

Product refinement: promote one ROADMAP.md *Parking Lot* idea into a buildable
``docs/process/issues/NNN-*.md`` + ROADMAP pointer, delivered as a PR for a human
to merge. The author->reviewer loop itself is prompt-driven inside the job's
opencode session (same architecture as the pipeline orchestrator — there is no
Python stage/spawn loop in this repo, see ROADMAP's "Unify routines + pipeline"
bullet). This module is the deterministic part: *which* bullet to refine and whether it
is already covered, plus the decisions the loop must make through code rather
than prompt prose (the issues 034/035 precedent) — the next issue number (fresh
``origin/main`` union every open same-repo PR), the 3-pass cap / consensus
decision, the refined issue's frontmatter and id uniqueness, and the ROADMAP
pointer keyed off the issue filename.

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
from typing import Literal, TextIO, TypeIs

import yaml

# Cap per spec: at most 3 author->reviewer passes.
MAX_ITERATIONS = 3

# Job constants per spec (asserted by tests against the real job file).
ISSUE_REFINEMENT_CRON = "0 22 * * *"
ISSUE_REFINEMENT_HEAD_PREFIX = "auto/issue-refinement-"

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

# Issue numbers live in filenames as ``docs/process/issues/NNN-<slug>.md``.
ISSUES_DIR_RELPATH = "docs/process/issues"
_ISSUE_NUMBER_RE = re.compile(r"docs/process/issues/(\d+)-")

GH_TIMEOUT_SECONDS = 30
FRONTMATTER_DELIM = "---"
REQUIRED_FRONTMATTER = ("id", "title", "status", "priority", "area")

ReviewerConfidence = Literal["high", "medium", "low"]


def is_reviewer_confidence(value: str) -> TypeIs[ReviewerConfidence]:
    """Narrow a lowercased confidence word to the closed set the regex allows."""
    return value in ("high", "medium", "low")


# The reviewer is told to reply with one 'confidence: high|medium|low' line; the
# loop check reads it off the raw reply so consensus is not the author's call.
# Line-anchored (after stripping markdown emphasis, so `**Confidence:** high`
# still counts): an echoed prompt's `'confidence: high' | ...` or a gap that
# mentions the phrase mid-sentence must never read as consensus.
_CONFIDENCE_RE = re.compile(
    r"^[\s>#-]*confidence:\s*(high|medium|low)\b", re.IGNORECASE | re.MULTILINE
)
_MARKDOWN_EMPHASIS_RE = re.compile(r"[*_`]")

# Exit codes for the code-enforced loop steps — the `gate` contract: 0 pass /
# continue, 1 fail / stop (reason on stderr), 2 usage (nothing was checked).
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


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
            fm = read_frontmatter(path.read_text())
        except (OSError, ValueError):
            continue
        title = fm.get("title")
        if isinstance(title, str):
            titles.append(title)
    return titles


def read_frontmatter(text: str) -> dict[str, object]:
    """The leading ``---``-delimited YAML mapping of an issue file.

    Raises ``ValueError`` naming what is wrong (no block, unterminated block,
    invalid YAML, not a mapping) — ``existing_issue_titles`` skips such a file,
    ``refine-issue --check-issue`` reports it.
    """
    if not text.startswith(f"{FRONTMATTER_DELIM}\n"):
        raise ValueError(
            f"no frontmatter: file does not start with '{FRONTMATTER_DELIM}'"
        )
    _, _, rest = text.partition(f"{FRONTMATTER_DELIM}\n")
    fm_text, sep, _ = rest.partition(f"\n{FRONTMATTER_DELIM}\n")
    if not sep:
        raise ValueError(f"frontmatter is never closed by a '{FRONTMATTER_DELIM}' line")
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a YAML mapping")  # noqa: TRY004 — malformed input; both callers handle ValueError
    return fm


def refinement_open_pr_slugs(repo: Path) -> frozenset[str] | None:
    """Parking-Lot slugs that already have an open ``auto/issue-refinement-*`` PR,
    via one batched ``gh pr list``.

    Returns ``None`` on any ``gh`` failure — *not* an empty set. The caller must
    fail safe: an unknown answer means "a refinement PR might already exist, do
    not pick", never "none found, pick anyway". Getting this backwards re-opens
    the same PR every night until a human closes it.

    Only PRs opened by the authenticated gh account (``--author @me``, which the
    job's own PRs are) from a branch in this repository are scanned: anyone can open
    a PR carrying the marker, and that must not keep a bullet from being refined.

    Primary correlation is the ``Refines-Parking-Lot: <slug>`` body marker the
    job writes; PR-title tokens are a fallback when the marker is absent.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--author",
                "@me",
                "--json",
                "headRefName,title,body,isCrossRepository",
            ],
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
        # Same rule as `auto_fix.is_same_repo_pr` (mirrored, not imported, to keep
        # this module off auto_fix's dependency graph): only an explicit
        # ``isCrossRepository: false`` counts, so a fork PR never covers a bullet.
        if not isinstance(item, dict) or item.get("isCrossRepository") is not False:
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


def _issue_numbers_in_paths(paths: list[str]) -> set[int]:
    """Issue numbers carried by ``docs/process/issues/NNN-*`` paths, as integers.

    Compared as integers so ``071``, ``71`` and ``0071`` are the same number —
    the collision space is the set of numbers, not the lexicographic max.
    """
    numbers: set[int] = set()
    for path in paths:
        match = _ISSUE_NUMBER_RE.search(path)
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def main_issue_numbers(repo: Path) -> frozenset[int] | None:
    """Issue numbers on *fresh-fetched* ``origin/main``, or ``None`` on failure.

    Fetches first (the worktree checkout can be stale — the 2026-10-24 collision)
    and reads the issues directory off ``origin/main`` rather than the working
    tree. Any ``git`` failure returns ``None`` so the caller can fail safe.
    """
    try:
        fetched = subprocess.run(
            ["git", "fetch", "origin", "main", "--quiet"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: git fetch origin main failed: {exc}", file=sys.stderr)
        return None
    if fetched.returncode != 0:
        print(
            f"warning: git fetch origin main failed: {fetched.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        tree = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                "origin/main",
                "--",
                f"{ISSUES_DIR_RELPATH}/",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: git ls-tree origin/main failed: {exc}", file=sys.stderr)
        return None
    if tree.returncode != 0:
        print(
            f"warning: git ls-tree origin/main failed: {tree.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    return frozenset(_issue_numbers_in_paths(tree.stdout.splitlines()))


def open_pr_issue_numbers(repo: Path) -> frozenset[int] | None:
    """Issue numbers carried by ``docs/process/issues/NNN-*`` files in open,
    same-repo PRs — *any* author, not just ``@me`` — or ``None`` on failure.

    A refinement PR (or a pipeline PR) already carries its number until it
    merges; counting only this account's PRs would miss the pipeline bot's. A
    fork PR never owns a number in this repo. ``None`` means "unknown" and the
    caller must fail safe, exactly like ``refinement_open_pr_slugs``.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "200",
                "--json",
                "headRefName,files,isCrossRepository",
            ],
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

    paths: list[str] = []
    for item in data:
        if not isinstance(item, dict) or item.get("isCrossRepository") is not False:
            continue
        files = item.get("files")
        if not isinstance(files, list):
            continue
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str):
                paths.append(path)
    return frozenset(_issue_numbers_in_paths(paths))


def next_issue_id(repo: Path) -> int | None:
    """The next free 3-digit issue number: union of fresh ``origin/main`` and every
    open same-repo PR, max + 1 (``1`` when nothing is in use).

    ``None`` when either source cannot be read — never a guessed number.
    """
    main_numbers = main_issue_numbers(repo)
    if main_numbers is None:
        return None
    pr_numbers = open_pr_issue_numbers(repo)
    if pr_numbers is None:
        return None
    return max(main_numbers | pr_numbers, default=0) + 1


def run_next_issue_id(repo: Path) -> int:
    """Entry point behind ``refine-issue --next-id``: print the next zero-padded
    id on stdout, or exit 1 with the failing source named on stderr."""
    number = next_issue_id(repo)
    if number is None:
        print(
            "error: could not compute the next issue id — refusing to guess "
            "(git origin/main or gh pr list is unavailable); will retry next run",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    print(f"{number:03d}")
    return EXIT_OK


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


def parse_reviewer_confidence(reply: str) -> ReviewerConfidence | None:
    """The first ``confidence: high|medium|low`` in a reviewer reply, lowercased;
    ``None`` when the reviewer never gave one (treated as not-consensus)."""
    match = _CONFIDENCE_RE.search(_MARKDOWN_EMPHASIS_RE.sub("", reply))
    if not match:
        return None
    value = match.group(1).lower()
    return value if is_reviewer_confidence(value) else None


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
    """True when the job runs opencode with no Claude anywhere — the author model
    *and* the quota fallback, which is the model a fallback run actually uses."""
    if job_config.get("agent_kind") != "opencode":
        return False
    models = (job_config.get("model"), job_config.get("fallback_model"))
    return not any("claude" in str(m or "").lower() for m in models)


def _id_as_number(value: object) -> int | None:
    """A frontmatter ``id`` as an integer, so ``071``/``71``/``0071`` collide."""
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def _parking_lot_blocks(roadmap_text: str) -> list[tuple[str, int, int]]:
    """``(slug, first_line, last_line)`` for each Parking Lot bullet, in file order.

    Same bullet/continuation rules as ``parse_parking_lot_bullets`` but keeps the
    original line span so a pointer can be appended in place.
    """
    lines = roadmap_text.split("\n")
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == PARKING_LOT_HEADER),
        None,
    )
    if start is None:
        return []

    blocks: list[tuple[str, int, int]] = []
    current: list[str] | None = None
    first = last = 0

    def _flush() -> None:
        nonlocal current
        if current is not None:
            blocks.append((_finalize_bullet(current).slug, first, last))
            current = None

    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("## "):
            break
        if line.startswith("- "):
            _flush()
            current = [line[2:].strip()]
            first = last = i
        elif current is not None and line.startswith("  ") and line.strip():
            current.append(line.strip())
            last = i
        elif current is not None:
            _flush()
            if blocks:
                break
    _flush()
    return blocks


def apply_roadmap_pointer(roadmap_path: Path, issue_path: Path) -> int:
    """Entry point behind ``refine-issue --roadmap-pointer``: append the issue
    pointer to its own Parking Lot bullet.

    The bullet is located by the issue *filename* slug — no guessing a paragraph
    (the 2026-10-24 bug). Idempotent: the exact pointer already on the bullet is
    exit 0 with no edit. A slug with no bullet is exit 1; an unreadable issue or
    absent/empty frontmatter ``id`` is exit 2 (nothing could be built).
    """
    match = re.fullmatch(r"(\d+)-(.+)\.md", issue_path.name)
    if not match:
        print(
            f"roadmap-pointer: {issue_path.name} is not an NNN-<slug>.md issue file",
            file=sys.stderr,
        )
        return EXIT_USAGE
    slug = match.group(2)

    try:
        issue_text = issue_path.read_text()
    except OSError as exc:
        print(f"roadmap-pointer: cannot read {issue_path}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        frontmatter = read_frontmatter(issue_text)
    except ValueError as exc:
        print(f"roadmap-pointer: {issue_path}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    raw_id = frontmatter.get("id")
    issue_id = str(raw_id).strip() if raw_id is not None else ""
    if not issue_id:
        print(
            f"roadmap-pointer: {issue_path} has no frontmatter id: cannot build "
            "the pointer",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        roadmap_text = roadmap_path.read_text()
    except OSError as exc:
        print(f"roadmap-pointer: cannot read {roadmap_path}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    block = next(
        (b for b in _parking_lot_blocks(roadmap_text) if b[0] == slug),
        None,
    )
    if block is None:
        print(
            f"roadmap-pointer: no Parking Lot bullet matches slug '{slug}' — "
            "refusing to attach the pointer to a guessed paragraph",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    _, first, last = block

    lines = roadmap_text.split("\n")
    pointer = f"→ [`{issue_id}`](docs/process/issues/{issue_path.name})"
    bullet_text = "\n".join(lines[first : last + 1])
    if pointer in bullet_text:
        return EXIT_OK

    if lines[last].startswith("  "):
        lines[last] = f"{lines[last].rstrip()} {pointer}"
    else:
        lines.insert(last + 1, f"  {pointer}")
    roadmap_path.write_text("\n".join(lines))
    return EXIT_OK


def run_check_issue(repo: Path, path: Path) -> int:
    """Entry point behind ``refine-issue --check-issue``: the job's last step
    before opening its PR (issue 029's frontmatter rule, in code rather than
    prompt prose — the issues 034/035 precedent).

    Exits 0 when the refined issue's frontmatter is valid *and* its ``id`` is
    unused on fresh ``origin/main`` or by an open PR, 1 with every problem on
    stderr when it is not, 2 when the file cannot be read. An unavailable number
    source is exit 1 ("could not verify number uniqueness") — never fail-open.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        print(f"check-issue: cannot read {path}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    frontmatter: dict[str, object] | None = None
    try:
        frontmatter = read_frontmatter(text)
        problems = validate_issue_frontmatter(frontmatter)
    except ValueError as exc:
        problems = [str(exc)]

    if not problems and frontmatter is not None:
        used = main_issue_numbers(repo)
        if used is not None:
            pr_numbers = open_pr_issue_numbers(repo)
            used = None if pr_numbers is None else frozenset(used | pr_numbers)
        if used is None:
            print(
                "check-issue failed: could not verify number uniqueness — "
                "git origin/main or gh pr list is unavailable",
                file=sys.stderr,
            )
            return EXIT_FAILURE
        own_number = _id_as_number(frontmatter.get("id"))
        if own_number is not None and own_number in used:
            problems.append(
                f"duplicate id: {frontmatter.get('id')} is already used on "
                "origin/main or by an open PR"
            )

    if not problems:
        return EXIT_OK
    print(f"check-issue failed: {path}", file=sys.stderr)
    for problem in problems:
        if problem in REQUIRED_FRONTMATTER:
            problem = f"missing or empty frontmatter key: {problem}"
        print(f"  - {problem}", file=sys.stderr)
    return EXIT_FAILURE


def run_loop_check(iteration: int, reply_path: Path) -> int:
    """Entry point behind ``refine-issue --review-pass N --reviewer-reply FILE``:
    after reviewer pass ``N`` (1-indexed), decide whether the author->reviewer
    loop runs another pass.

    The confidence is parsed from the reviewer's raw reply here, not reported by
    the author, so neither consensus nor the ``MAX_ITERATIONS`` cap is the
    author model's judgment. Exits 0 = run another pass, 1 = stop (stderr says
    ``consensus reached`` or ``capped at N``), 2 = usage error.
    """
    if iteration < 1:
        print(
            f"loop-check: --review-pass must be >= 1, got {iteration}", file=sys.stderr
        )
        return EXIT_USAGE
    try:
        reply = reply_path.read_text()
    except OSError as exc:
        print(f"loop-check: cannot read {reply_path}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    confidence = parse_reviewer_confidence(reply)
    if loop_should_continue(iteration, confidence or ""):
        print(
            f"loop-check: continue — pass {iteration}/{MAX_ITERATIONS}, reviewer "
            f"confidence: {confidence or 'not stated'}",
            file=sys.stderr,
        )
        return EXIT_OK
    if confidence == "high":
        print(
            f"loop-check: stop — consensus reached on pass {iteration}", file=sys.stderr
        )
    else:
        print(
            f"loop-check: stop — capped at {MAX_ITERATIONS} without consensus "
            f"(last reviewer confidence: {confidence or 'not stated'})",
            file=sys.stderr,
        )
    return EXIT_FAILURE


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
