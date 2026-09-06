"""Out-of-tree claim store for `pick-feature --mark-in-progress` (issue 041).

Recording a pick as an uncommitted edit to the issue file in `$REPO_PARENT` collided
with the implementing PR's own edit to the same line once it merged to `main`:
`sync-repo`'s `git merge --ff-only` then refuses (modified locally + changed
upstream), and `ensure_repo` fails every subsequent job on that repo path. See
docs/process/issues/041-pick-flip-blocks-repo-sync.md.

The fix: claims live here instead, keyed by issue id with a `claimed_at`
timestamp, so `$REPO_PARENT` stays a clean mirror of `origin/main` — which is what
`sync-repo`/`ensure_repo` assume. JSON dict, not JSONL: a claim is a single
mutable fact ("is this issue claimed, since when") that gets released, not an
append-only log of events.

Shape chosen with issue 040 (lease-expiry reclamation) in mind: a `claimed_at`
timestamp per issue id, queryable and releasable (`is_claimed`, `release_claim`)
without touching any tracked file in a checkout this process doesn't own. 040
only needs to add a "claimed longer than the lease" check against `claimed_at`
and call `release_claim` — no new storage.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def default_claims_path() -> Path:
    """$HERDR_PLUGIN_STATE_DIR/pick-feature-claims.json if set, else
    ~/.local/state/herdr-routines/. Same convention as history.default_history_path."""
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "pick-feature-claims.json"


@dataclass(frozen=True, slots=True)
class Claim:
    issue_id: str
    claimed_at: datetime


def load_claims(path: Path) -> dict[str, Claim]:
    """All current claims, keyed by issue id. Empty if the store doesn't exist yet."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {
        issue_id: Claim(
            issue_id=issue_id, claimed_at=datetime.fromisoformat(entry["claimed_at"])
        )
        for issue_id, entry in data.items()
    }


def save_claims(path: Path, claims: dict[str, Claim]) -> None:
    """Atomic write (tmpfile + rename), same discipline as pick_feature.mark_in_progress
    used on the tracked file — just aimed at our own out-of-tree store now."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        issue_id: {
            "claimed_at": claim.claimed_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        }
        for issue_id, claim in claims.items()
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True))
    tmp.replace(path)


def claim_issue(path: Path, issue_id: str, *, now: datetime | None = None) -> None:
    """Record that `issue_id` has been picked, replacing any prior claim for it."""
    claims = load_claims(path)
    claims[issue_id] = Claim(issue_id=issue_id, claimed_at=now or datetime.now(UTC))
    save_claims(path, claims)


def is_claimed(path: Path, issue_id: str) -> bool:
    return issue_id in load_claims(path)


def release_claim(path: Path, issue_id: str) -> None:
    """No-op if `issue_id` isn't claimed."""
    claims = load_claims(path)
    if issue_id in claims:
        del claims[issue_id]
        save_claims(path, claims)
