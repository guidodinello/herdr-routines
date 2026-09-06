"""Select the next open issue from docs/process/issues/ for the pipeline's stage 0.

Pure filesystem + YAML frontmatter parsing: no HerdrClient, no `herdr` binary,
mirrors gc.py's "no Herdr server required" posture so this stays usable from the
launcher before any workspace/pane exists. `gh` is fine (gc.py already calls it for
its own PR checks) — it's the Herdr *server* this stays independent of.

See docs/process/README.md for the frontmatter convention and ROADMAP.md's
"Autonomous task selection for the pipeline" for why this exists and what
promoting it beyond a manually-invoked helper is still gated on.

Issue 040: a claim (`claims.py`, issue 041) is a lease, not a permanent hold. A run
that dies before opening a PR must not orphan the issue it picked forever, so a
claim older than `LEASE_HOURS` becomes reclaimable once neither an open PR nor an
in-flight pipeline run still references it — see `reclaim_stale_claims`.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

import yaml

from herdr_routines.claims import (
    claim_issue,
    default_claims_path,
    load_claims,
    save_claims,
)

FRONTMATTER_DELIM = "---"
OPEN_STATUS = "open"
REQUIRED_FIELDS = ("id", "title", "status", "priority", "area")
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Issue 040 lease length: the pipeline's own deadline is launch + 7h
# (docs/pipeline/orchestrator-prompt.md), and pipeline_watchdog.py adds a 30min grace
# past that before treating a run as stalled. 12h is a comfortable margin past both —
# long enough that a run that is merely slow is never reclaimed mid-flight, short
# enough that a genuinely dead run does not orphan its issue for days.
LEASE_HOURS = 12.0

GH_TIMEOUT_SECONDS = 30
PIPELINE_WORKTREE_GLOB = "auto-pipeline-*"
PIPELINE_STATE_JSON_NAME = "state.json"

# PR titles/bodies in this repo carry the issue id verbatim, e.g. "(issue 041)"
# (see PR #93, #92, #90) — this is the only correlation available between a claim
# (keyed by issue id) and a PR (which is not otherwise linked to the issue). Matched
# as an int so "041" (a claim's issue id) and a differently-padded "41" in prose agree.
_ISSUE_REF_PATTERN = re.compile(r"issues?\s+(\d+)", re.IGNORECASE)


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
    issues: list[Issue],
    claimed_ids: frozenset[str] = frozenset(),
    pipeline_pr_ids: frozenset[int] | None = None,
) -> Issue | None:
    """Highest-priority, lowest-id issue with status == 'open' and not already
    claimed (issue 041: a claim no longer changes `status`, so it must be
    checked separately here); None if nothing open and unclaimed.

    `pipeline_pr_ids` (issue 028, re-scoped): issue ids already referenced by an
    open `auto/pipeline-*` PR — treated as claimed even when `claims.py` holds
    no claim for them. Compared as ints so "028" and 28 agree.
    """
    pipeline_set: frozenset[int] = pipeline_pr_ids or frozenset()

    def _excluded_by_pipeline(issue: Issue) -> bool:
        try:
            return int(issue.id) in pipeline_set
        except ValueError:
            return False  # non-numeric id can't match a pipeline PR's issue ref

    open_issues = [
        issue
        for issue in issues
        if issue.status == OPEN_STATUS
        and issue.id not in claimed_ids
        and not _excluded_by_pipeline(issue)
    ]
    if not open_issues:
        return None
    return min(open_issues, key=lambda issue: issue.sort_key)


def render_feature_idea(issue: Issue) -> str:
    """FEATURE_IDEA text for the pipeline orchestrator's stage 1 input (spec.md, orchestrator-prompt.md)."""
    return f"{issue.title} (herdr-routines issue {issue.id}, {issue.path.as_posix()}).\n\n{issue.body}"


def _default_pipeline_worktrees_root() -> Path:
    return Path.home() / ".herdr" / "worktrees" / "herdr-routines"


def _default_pipeline_reports_dir() -> Path:
    import os

    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "reports"


def open_pr_issue_ids(repo: Path) -> frozenset[int] | None:
    """Issue ids referenced by any currently-open PR's title or body, via one batched
    `gh pr list` call (flat cost regardless of how many claims need checking).

    Returns `None` on any `gh` failure — not an empty set. A claim's reclaim check
    must fail *safe*: an unknown answer means "assume a PR might still be open, keep
    the claim", never "no open PR found, release it". Getting this backwards would
    let a stale-lease reclaim silently re-pick an issue with a live PR awaiting human
    merge review.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,title,body",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    refs: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        text = f"{item.get('title', '')}\n{item.get('body', '')}"
        refs.update(int(m.group(1)) for m in _ISSUE_REF_PATTERN.finditer(text))
    return frozenset(refs)


def pipeline_open_pr_issue_ids(
    repo: Path,
    *,
    worktrees_root: Path | None = None,
) -> frozenset[int] | None:
    """Issue ids referenced by open `auto/pipeline-*` PRs, via a single batched
    `gh pr list` call (issue 028, re-scoped).

    The exclusion is unconditional and fail-open: a `gh` failure warns to stderr
    and returns ``None`` so the caller can pick anyway, rather than bricking the
    nightly run on a transient GitHub outage.

    Structural derivation is preferred where possible (``state.json`` already
    records ``feature_source`` for the run id embedded in the branch name), with
    PR title/body parsing as a fallback when no structural record exists — the
    PR body convention ("Closes issue NN — the `status: done` flip rides this PR")
    is prose and less reliable than the branch/run record, so it is only the
    fallback, not the primary. The header comment on the fallback branch says so.
    When neither structural nor prose yields an id, the PR is ignored (no guess).

    Any `gh` failure returns ``None`` (unknown), not an empty set.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "headRefName,number,title,body",
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
        print("warning: gh pr list returned unexpected shape", file=sys.stderr)
        return None

    resolved_worktrees_root = worktrees_root or _default_pipeline_worktrees_root()

    refs: set[int] = set()
    pipeline_prefix = "auto/pipeline-"
    for item in data:
        if not isinstance(item, dict):
            continue
        head = item.get("headRefName")
        if not isinstance(head, str) or not head.startswith(pipeline_prefix):
            continue  # non-pipeline open PR never excludes (028 criterion 3)

        # Structural path: headRefName -> run_id -> state.json -> feature_source -> issue id
        run_id = head.removeprefix(pipeline_prefix)
        structural_id: str | None = None
        # Try both case variants of the worktree dir (herdr lowercases the path)
        for candidate in (
            resolved_worktrees_root / f"auto-pipeline-{run_id.lower()}",
            resolved_worktrees_root / f"auto-pipeline-{run_id}",
        ):
            state_path = candidate / PIPELINE_STATE_JSON_NAME
            if not state_path.exists():
                continue
            try:
                raw = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            # If a terminal report already exists, the run is not in-flight and the
            # branch is only retained because the PR is still open — but the PR itself
            # is the signal we are already handling, so we still derive the id. The
            # "no terminal report" check only matters for the inflight set, not here.
            feature_source = raw.get("feature_source")
            if isinstance(feature_source, str) and feature_source:
                structural_id = _issue_id_from_feature_source(feature_source)
                if structural_id is not None:
                    break
        if structural_id is not None:
            try:
                refs.add(int(structural_id))
            except ValueError:
                pass
            continue

        # Fallback: PR title/body prose convention — less reliable, so only when
        # no structural record exists (see docstring).
        text = f"{item.get('title', '')}\n{item.get('body', '')}"
        refs.update(int(m.group(1)) for m in _ISSUE_REF_PATTERN.finditer(text))
    return frozenset(refs)


def _issue_id_from_feature_source(feature_source: str) -> str | None:
    """The `NNN` id prefix out of a `state.json` `feature_source` value like
    `docs/process/issues/041-pick-flip-blocks-repo-sync.md` (orchestrator-prompt.md);
    None if it doesn't look like one of ours."""
    match = re.match(r"(\d+)-", Path(feature_source).name)
    return match.group(1) if match else None


def _has_terminal_report(reports_dir: Path, run_id: str, raw_state: dict) -> bool:
    """Same three-candidate-path check as `pipeline_watchdog._has_terminal_report`
    (duplicated, not imported — see gc.py's own `PIPELINE_WORKTREE_GLOB` comment for
    why: a leaf module shouldn't pull in `herdr_routines.herdr` for a few lines of
    filesystem convention). The repo's docs disagree on `<run_id>.md` vs
    `pipeline-<run_id>.md`; checking only one would let a dead run whose report landed
    at the other path read as "still in-flight" forever, defeating this issue's fix."""
    paths = [reports_dir / f"pipeline-{run_id}.md", reports_dir / f"{run_id}.md"]
    artifacts = raw_state.get("artifact_paths")
    if isinstance(artifacts, dict):
        report = artifacts.get("report")
        if isinstance(report, str) and report:
            paths.append(Path(report))
    return any(p.exists() for p in paths)


def inflight_issue_ids(worktrees_root: Path, reports_dir: Path) -> frozenset[str]:
    """Issue ids with a currently in-flight pipeline run: a `state.json` under
    `worktrees_root/auto-pipeline-*/` whose `feature_source` names this issue, with no
    terminal report written yet."""
    if not worktrees_root.exists():
        return frozenset()
    ids: set[str] = set()
    for state_path in worktrees_root.glob(
        f"{PIPELINE_WORKTREE_GLOB}/{PIPELINE_STATE_JSON_NAME}"
    ):
        try:
            raw = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        run_id = raw.get("run_id")
        feature_source = raw.get("feature_source")
        if not isinstance(run_id, str) or not run_id:
            continue
        if not isinstance(feature_source, str) or not feature_source:
            continue
        issue_id = _issue_id_from_feature_source(feature_source)
        if issue_id is None:
            continue
        if not _has_terminal_report(reports_dir, run_id, raw):
            ids.add(issue_id)
    return frozenset(ids)


@dataclass(frozen=True, slots=True)
class ReclaimedPick:
    """One claim released by `reclaim_stale_claims` — issue 040 acceptance criterion 4
    needs this surfaced, not silently dropped."""

    issue_id: str
    claimed_at: datetime


def reclaim_stale_claims(
    claims_path: Path,
    *,
    repo: Path,
    worktrees_root: Path | None = None,
    reports_dir: Path | None = None,
    now: datetime | None = None,
    lease_hours: float = LEASE_HOURS,
) -> list[ReclaimedPick]:
    """Release every claim older than `lease_hours` that has neither an open PR nor an
    in-flight pipeline run still referencing it (issue 040).

    The dying-process-never-runs-cleanup case this exists for means reclamation must
    not depend on any cooperation from the run that claimed the issue — it is a pure
    read of `claimed_at` (the lease) plus two independent, currently-true facts (open
    PR, in-flight run), never a flag the crashed run itself would have had to set.

    A `gh` failure aborts the whole pass with no releases (see `open_pr_issue_ids`) —
    fail safe, not fail open: an unanswerable "is there a PR" is not evidence there
    isn't one.
    """
    resolved_now = now or datetime.now(UTC)
    resolved_worktrees_root = worktrees_root or _default_pipeline_worktrees_root()
    resolved_reports_dir = reports_dir or _default_pipeline_reports_dir()
    claims = load_claims(claims_path)
    stale = {
        issue_id: claim
        for issue_id, claim in claims.items()
        if resolved_now - claim.claimed_at >= timedelta(hours=lease_hours)
    }
    if not stale:
        return []
    open_pr_ids = open_pr_issue_ids(repo)
    if open_pr_ids is None:
        return []
    inflight_ids = inflight_issue_ids(resolved_worktrees_root, resolved_reports_dir)
    reclaimed: list[ReclaimedPick] = []
    for issue_id, claim in stale.items():
        if int(issue_id) in open_pr_ids or issue_id in inflight_ids:
            continue
        del claims[issue_id]
        reclaimed.append(ReclaimedPick(issue_id=issue_id, claimed_at=claim.claimed_at))
    if reclaimed:
        save_claims(claims_path, claims)
    return reclaimed


def run_pick_feature(
    issues_dir: Path,
    mark: bool = False,
    out: TextIO | None = None,
    claims_path: Path | None = None,
    repo: Path | None = None,
    worktrees_root: Path | None = None,
    reports_dir: Path | None = None,
    now: datetime | None = None,
    notify: Callable[[ReclaimedPick], None] | None = None,
) -> int:
    """Entry point behind `herdr-routines pick-feature`.

    A pick is recorded as a claim in `claims_path` (default
    `claims.default_claims_path()`), never as an edit to the issue file in
    `$REPO_PARENT` (issue 041: that edit collided with the implementing PR's own
    edit to the same line once merged). `$REPO_PARENT` stays a clean mirror of
    `origin/main`, and a claimed issue is skipped by future picks until it either
    lands as `status: done` on `main` or its claim is released (issue 040).

    With `mark=True`, stale claims are reclaimed first (see `reclaim_stale_claims`) so
    a run that died before opening a PR does not orphan its issue forever — gated on
    `mark` so a read-only pick (no `--mark-in-progress`) never mutates someone else's
    claim. Each reclaim is printed to stderr and, if `notify` is given, passed to it
    (issue 040 acceptance criterion 4: a reclaim must be visible, never silent).
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
    if mark:
        for reclaimed in reclaim_stale_claims(
            resolved_claims_path,
            repo=repo if repo is not None else Path.cwd(),
            worktrees_root=worktrees_root,
            reports_dir=reports_dir,
            now=now,
        ):
            print(
                f"reclaimed stale claim: issue {reclaimed.issue_id} "
                f"(claimed {reclaimed.claimed_at.isoformat()}, "
                "no open PR, no in-flight run)",
                file=sys.stderr,
            )
            if notify is not None:
                notify(reclaimed)
    claimed_ids = frozenset(load_claims(resolved_claims_path))
    # Issue 028 re-scoped: an issue with an open auto/pipeline-* PR is treated as
    # claimed even when claims.py holds no claim for it. Unconditional, batched,
    # fail-open (warn and pick anyway on gh failure).
    pipeline_pr_ids: frozenset[int] | None
    try:
        pipeline_pr_ids = pipeline_open_pr_issue_ids(
            repo if repo is not None else Path.cwd(),
            worktrees_root=worktrees_root,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:  # defensive: never let a PR lookup brick the pick
        print(f"warning: pipeline PR lookup failed: {exc}", file=sys.stderr)
        pipeline_pr_ids = None
    if pipeline_pr_ids is None:
        # gh failure already warned inside pipeline_open_pr_issue_ids; for any
        # other None path (e.g. no repo), ensure at least one warning so the
        # fail-open is visible. If we already warned, this is harmless noise.
        # We only warn once: check if stderr already got a warning in this call
        # is fragile, so just treat None as "unknown — pick anyway" without an
        # extra warning when the helper already warned. The helper always warns
        # on None, except when called with a non-existent repo path that still
        # returns a real gh failure shape. So here: no extra warning.
        pipeline_pr_ids = frozenset()
    picked = select_next(issues, claimed_ids, pipeline_pr_ids)
    if picked is None:
        print("no open issues", file=sys.stderr)
        return 1
    if mark:
        claim_issue(resolved_claims_path, picked.id)
    stream.write(render_feature_idea(picked) + "\n")
    return 0
