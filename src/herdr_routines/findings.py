"""The pure diff/ledger half of issue 057's audit gate (Phase A).

An audit produces a report, not an exit code, so the honest converter from a
report to a boolean is a diff against the previous cycle's report. This module is
that diff: stable finding identity, a persisted per-finding ledger, and the
classification/ordering the dispatch cap rests on.

Pure by construction — no herdr, no gh, no git, no subprocess, no clock reads
(callers pass ``now``). The only I/O is the two files a caller hands it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

VALID_SEVERITIES = ("low", "medium", "high")
SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

_ID_MAX_LEN = 128
_SECONDS_PER_DAY = 86_400

# "app/x.php:212" / "app/x.php:212:5" -> drop the trailing line and column.
_TRAILING_LINE_RE = re.compile(r":\d+(?::\d+)?$")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_location(location: str) -> str:
    """Strip whitespace and a trailing ``:line`` / ``:line:col``.

    Line numbers shift under unrelated edits; keying an identity on one means the
    fixer's own edit re-arms the gate and re-opens the same PR every week."""
    return _TRAILING_LINE_RE.sub("", _WHITESPACE_RE.sub("", location))


def finding_id(check: str, kind: str, location: str) -> str:
    """The derived identity used when a finding does not supply its own ``id``.

    Deliberately coarse: same kind + same file collapse to one ID and are
    dispatched as one. Under-dispatch, never over-dispatch."""
    key = f"{check}|{kind}|{normalize_location(location)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    kind: str
    severity: str  # "low" | "medium" | "high"
    location: str
    summary: str


@dataclass(frozen=True, slots=True)
class FindingsManifest:
    """A validated findings manifest. ``duplicate_ids`` are the derived IDs that
    more than one finding collapsed onto (counted, never fatal)."""

    check: str
    findings: tuple[Finding, ...] = ()
    duplicate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FindingDiff:
    new: tuple[Finding, ...] = ()
    regressed: tuple[Finding, ...] = ()
    unchanged: tuple[Finding, ...] = ()
    resolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    kind: str
    location: str
    severity: str
    state: str  # "open" | "resolved"
    first_seen: str
    last_seen: str
    resolved_at: str | None = None
    attempts: int = 0
    last_dispatched_run: str | None = None


@dataclass(frozen=True, slots=True)
class Ledger:
    job: str
    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    version: int = 1
    updated: str | None = None
    runs: int = 0


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _order_key(finding: Finding) -> tuple[int, str, str, str]:
    # Total order: most severe first, then location/kind/id. A cap therefore drops
    # the least severe deterministically, and two ticks pick the same set.
    return (
        -SEVERITY_RANK.get(finding.severity, 0),
        finding.location,
        finding.kind,
        finding.id,
    )


# -- manifest -----------------------------------------------------------------


def _parse_finding(element: object, *, check: str) -> Finding | None:
    if not isinstance(element, dict):
        return None
    kind = element.get("kind")
    severity = element.get("severity")
    location = element.get("location")
    summary = element.get("summary")
    if not (
        isinstance(kind, str)
        and kind
        and isinstance(severity, str)
        and severity
        and isinstance(location, str)
        and location
        and isinstance(summary, str)
        and summary
    ):
        return None
    if severity not in VALID_SEVERITIES:
        return None

    supplied_id = element.get("id")
    if supplied_id is not None:
        if not isinstance(supplied_id, str):
            return None
        trimmed = supplied_id.strip()
        if not trimmed or len(trimmed) > _ID_MAX_LEN:
            return None
        fid = trimmed
    else:
        fid = finding_id(check, kind, location)

    return Finding(
        id=fid, kind=kind, severity=severity, location=location, summary=summary
    )


def load_findings_manifest_full(path: Path) -> FindingsManifest | None:
    """Validate and parse the whole manifest.

    ``None`` means unverifiable and is **never** ``[]``: a missing file, bad JSON,
    a non-dict document, ``version != 1``, a non-list ``findings``, a malformed
    element, or an empty/over-long skill-supplied ``id``. Two findings that derive
    the same ID collapse to one entry at the higher severity (counted in
    ``duplicate_ids``) rather than rejecting the whole report."""
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != 1:
        return None

    check = raw.get("check")
    if not isinstance(check, str) or not check:
        return None
    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list):
        return None

    collapsed: dict[str, Finding] = {}
    duplicate_ids: list[str] = []
    for element in findings_raw:
        finding = _parse_finding(element, check=check)
        if finding is None:
            return None
        existing = collapsed.get(finding.id)
        if existing is None:
            collapsed[finding.id] = finding
            continue
        if finding.id not in duplicate_ids:
            duplicate_ids.append(finding.id)
        if SEVERITY_RANK[finding.severity] > SEVERITY_RANK[existing.severity]:
            collapsed[finding.id] = finding

    findings = tuple(sorted(collapsed.values(), key=_order_key))
    return FindingsManifest(
        check=check, findings=findings, duplicate_ids=tuple(duplicate_ids)
    )


def load_findings_manifest(path: Path) -> list[Finding] | None:
    """``load_findings_manifest_full`` minus the duplicate bookkeeping, for callers
    that only need the finding list."""
    parsed = load_findings_manifest_full(path)
    if parsed is None:
        return None
    return list(parsed.findings)


# -- diff ---------------------------------------------------------------------


def diff_findings(
    findings: Iterable[Finding], ledger: Ledger | None, *, check: str
) -> FindingDiff:
    """Classify the current findings against the ledger (see the issue's table).

    ``check`` is carried for call-site symmetry; identity is resolved before this
    point."""
    entries = ledger.entries if ledger is not None else {}
    current = {f.id: f for f in findings}

    new: list[Finding] = []
    regressed: list[Finding] = []
    unchanged: list[Finding] = []
    for finding in current.values():
        old = entries.get(finding.id)
        if old is None or old.state == "resolved":
            # Absent -> new. Resolved -> new (the finding came back).
            new.append(finding)
        elif SEVERITY_RANK[finding.severity] > SEVERITY_RANK.get(old.severity, 0):
            regressed.append(finding)
        else:
            # Same or lower severity: a decrease is not a regression.
            unchanged.append(finding)

    resolved = [
        fid
        for fid, entry in entries.items()
        if entry.state == "open" and fid not in current
    ]
    return FindingDiff(
        new=tuple(sorted(new, key=_order_key)),
        regressed=tuple(sorted(regressed, key=_order_key)),
        unchanged=tuple(sorted(unchanged, key=_order_key)),
        resolved=tuple(sorted(resolved)),
    )


def apply_diff(
    ledger: Ledger,
    diff: FindingDiff,
    *,
    now: datetime,
    run_id: str,
    dispatched_ids: Iterable[str],
) -> Ledger:
    """Fold a diff into the ledger, consuming the budget for exactly the dispatched
    IDs (write-ahead: callers save before dispatch, so a dispatch that raises has
    still consumed its attempt). Suppressed/unchanged entries are untouched."""
    dispatched = set(dispatched_ids)
    stamp = _iso(now)
    entries = dict(ledger.entries)

    for finding in (*diff.new, *diff.regressed, *diff.unchanged):
        old = ledger.entries.get(finding.id)
        if old is None or old.state == "resolved":
            # A fresh occurrence: reset the budget, but keep first_seen so the
            # regression signal survives a resolve.
            base_attempts = 0
            first_seen = old.first_seen if old is not None else stamp
        else:
            base_attempts = old.attempts
            first_seen = old.first_seen
        attempts = base_attempts + (1 if finding.id in dispatched else 0)
        last_run = (
            run_id
            if finding.id in dispatched
            else (old.last_dispatched_run if old is not None else None)
        )
        entries[finding.id] = LedgerEntry(
            kind=finding.kind,
            location=finding.location,
            severity=finding.severity,
            state="open",
            first_seen=first_seen,
            last_seen=stamp,
            resolved_at=None,
            attempts=attempts,
            last_dispatched_run=last_run,
        )

    for fid in diff.resolved:
        old = ledger.entries.get(fid)
        if old is None:
            continue
        entries[fid] = replace(old, state="resolved", resolved_at=stamp)

    return Ledger(
        job=ledger.job,
        entries=entries,
        version=ledger.version,
        updated=stamp,
        runs=ledger.runs + 1,
    )


def prune_ledger(ledger: Ledger, *, now: datetime, retention_days: int) -> Ledger:
    """Drop only ``state: resolved`` entries older than *retention_days*. Open
    entries are never pruned — pruning them destroys the regression signal."""
    cutoff = now.astimezone(UTC).timestamp() - retention_days * _SECONDS_PER_DAY
    kept: dict[str, LedgerEntry] = {}
    for fid, entry in ledger.entries.items():
        if entry.state == "resolved" and entry.resolved_at is not None:
            resolved_ts = _parse_epoch(entry.resolved_at)
            if resolved_ts is not None and resolved_ts < cutoff:
                continue
        kept[fid] = entry
    return replace(ledger, entries=kept)


def _parse_epoch(value: str) -> float | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# -- ledger persistence --------------------------------------------------------


def default_findings_dir() -> Path:
    """``$HERDR_PLUGIN_STATE_DIR/findings`` else ``~/.local/state/herdr-routines/findings``.

    Deliberately not under ``reports_dir``: ``reports_prune.prune_reports`` deletes
    by mtime and would eat the ledger, taking the whole regression signal with it."""
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = (
        Path(plugin_dir)
        if plugin_dir
        else Path.home() / ".local" / "state" / "herdr-routines"
    )
    return base / "findings"


def ledger_path(job_name: str) -> Path:
    return default_findings_dir() / f"{job_name}.json"


def _ledger_to_dict(ledger: Ledger) -> dict[str, object]:
    return {
        "version": ledger.version,
        "job": ledger.job,
        "updated": ledger.updated,
        "runs": ledger.runs,
        "entries": {
            fid: {
                "kind": entry.kind,
                "location": entry.location,
                "severity": entry.severity,
                "state": entry.state,
                "first_seen": entry.first_seen,
                "last_seen": entry.last_seen,
                "resolved_at": entry.resolved_at,
                "attempts": entry.attempts,
                "last_dispatched_run": entry.last_dispatched_run,
            }
            for fid, entry in ledger.entries.items()
        },
    }


def _entry_from_dict(raw: object) -> LedgerEntry | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    location = raw.get("location")
    severity = raw.get("severity")
    state = raw.get("state")
    first_seen = raw.get("first_seen")
    last_seen = raw.get("last_seen")
    if not (
        isinstance(kind, str)
        and kind
        and isinstance(location, str)
        and location
        and isinstance(severity, str)
        and severity
        and isinstance(state, str)
        and state
        and isinstance(first_seen, str)
        and first_seen
        and isinstance(last_seen, str)
        and last_seen
    ):
        return None
    if severity not in VALID_SEVERITIES or state not in ("open", "resolved"):
        return None
    attempts = raw.get("attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        return None
    resolved_at = raw.get("resolved_at")
    if resolved_at is not None and not isinstance(resolved_at, str):
        return None
    last_dispatched_run = raw.get("last_dispatched_run")
    if last_dispatched_run is not None and not isinstance(last_dispatched_run, str):
        return None
    return LedgerEntry(
        kind=kind,
        location=location,
        severity=severity,
        state=state,
        first_seen=first_seen,
        last_seen=last_seen,
        resolved_at=resolved_at,
        attempts=attempts,
        last_dispatched_run=last_dispatched_run,
    )


def load_ledger(path: Path) -> Ledger | None:
    """Parse a ledger. ``None`` means corrupt, which is not the same as absent —
    callers distinguish absence with ``path.exists()`` and treat corruption as a
    hard ``failed (ledger_corrupt)`` rather than a cold start."""
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    job = raw.get("job")
    if not isinstance(job, str):
        return None
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, dict):
        return None
    entries: dict[str, LedgerEntry] = {}
    for fid, entry_raw in entries_raw.items():
        if not isinstance(fid, str):
            return None
        entry = _entry_from_dict(entry_raw)
        if entry is None:
            return None
        entries[fid] = entry
    runs = raw.get("runs", 0)
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 0:
        return None
    updated = raw.get("updated")
    if updated is not None and not isinstance(updated, str):
        return None
    return Ledger(job=job, entries=entries, version=1, updated=updated, runs=runs)


def save_ledger(path: Path, ledger: Ledger) -> None:
    """Atomic write: a ``.tmp`` sibling then ``os.replace``, so a crash mid-write
    leaves the previous ledger intact rather than a truncated SSOT."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(_ledger_to_dict(ledger), indent=2, sort_keys=True))
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
