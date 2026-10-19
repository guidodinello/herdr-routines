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

import fcntl
import json
import logging
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


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


def _parse_claims(path: Path) -> dict[str, Claim]:
    """Raises on a missing or malformed store; callers decide how to degrade."""
    data = json.loads(path.read_text())
    return {
        issue_id: Claim(
            issue_id=issue_id, claimed_at=datetime.fromisoformat(entry["claimed_at"])
        )
        for issue_id, entry in data.items()
    }


# A hand-edited or truncated store can fail in any of these ways.
_CORRUPT_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def load_claims(path: Path) -> dict[str, Claim]:
    """All current claims, keyed by issue id. Empty if the store doesn't exist yet or
    is corrupt (warned, left in place: only a locked writer moves it aside)."""
    try:
        return _parse_claims(path)
    except FileNotFoundError:
        return {}
    except _CORRUPT_ERRORS as exc:
        log.warning("claims store %s is corrupt, treating as empty: %s", path, exc)
        return {}


def save_claims(path: Path, claims: dict[str, Claim]) -> None:
    """Atomic write (unique tmpfile in the same dir + rename). Callers that read first
    must hold `locked_claims`; this alone does not serialise writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        issue_id: {
            "claimed_at": claim.claimed_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z")
        }
        for issue_id, claim in claims.items()
    }
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, sort_keys=True))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _quarantine(path: Path) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    os.replace(path, aside)
    log.warning(
        "claims store %s is corrupt, moved to %s and starting empty", path, aside
    )


@contextmanager
def locked_claims(path: Path) -> Generator[dict[str, Claim]]:
    """Exclusive read-modify-write of the claims store. Holds a blocking flock on a
    sibling `.lock` file for the whole block (neither `tick.lock` nor anything else
    covers the pipeline unit or manual `pick-feature` runs), yields the current claims
    to mutate in place, and saves only if they changed and the block didn't raise. A
    corrupt store is moved aside and treated as empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path.with_name(f"{path.name}.lock"), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            claims = _parse_claims(path)
        except FileNotFoundError:
            claims = {}
        except _CORRUPT_ERRORS:
            _quarantine(path)
            claims = {}
        before = dict(claims)
        yield claims
        if claims != before:
            save_claims(path, claims)
    finally:
        os.close(lock_fd)


def claim_issue(path: Path, issue_id: str, *, now: datetime | None = None) -> None:
    """Record that `issue_id` has been picked, replacing any prior claim for it."""
    with locked_claims(path) as claims:
        claims[issue_id] = Claim(issue_id=issue_id, claimed_at=now or datetime.now(UTC))


def is_claimed(path: Path, issue_id: str) -> bool:
    """True if any claim is recorded for issue_id, however old. Ignores the lease:
    pick_feature decides reclaimability against LEASE_HOURS."""
    return issue_id in load_claims(path)


def release_claim(path: Path, issue_id: str) -> None:
    """No-op if `issue_id` isn't claimed."""
    with locked_claims(path) as claims:
        claims.pop(issue_id, None)
