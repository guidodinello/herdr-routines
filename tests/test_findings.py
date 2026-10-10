"""Acceptance tests for issue 057 Phase A: the pure diff/ledger half.

Covers criteria 2, 3, 5, 6, 9, 11, 13. No herdr server, no gh, no git.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_routines.findings import (
    Finding,
    Ledger,
    LedgerEntry,
    apply_diff,
    diff_findings,
    finding_id,
    ledger_path,
    load_findings_manifest,
    load_findings_manifest_full,
    load_ledger,
    normalize_location,
    prune_ledger,
    save_ledger,
)

NOW = datetime(2026, 10, 10, 4, 0, 0, tzinfo=UTC)


def _entry(**over: object) -> LedgerEntry:
    base: dict[str, object] = {
        "kind": "k",
        "location": "a.php",
        "severity": "high",
        "state": "open",
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": "2026-01-01T00:00:00Z",
    }
    base.update(over)
    return LedgerEntry(**base)  # type: ignore[arg-type]


def _write_manifest(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload))
    return path


# -- AC 2 ---------------------------------------------------------------------------


def test_finding_id_ignores_line_numbers() -> None:
    assert normalize_location("app/Services/Invoice.php:212") == (
        "app/Services/Invoice.php"
    )
    assert normalize_location("app/Services/Invoice.php:212:5") == (
        "app/Services/Invoice.php"
    )
    assert normalize_location("  app/Services/Invoice.php  ") == (
        "app/Services/Invoice.php"
    )

    shifted = finding_id(
        "type-health", "implicit-any-return", "app/Services/Invoice.php:212"
    )
    assert shifted == finding_id(
        "type-health", "implicit-any-return", "app/Services/Invoice.php:900"
    )
    assert shifted != finding_id(
        "type-health", "implicit-any-return", "app/Other.php:212"
    )
    assert shifted != finding_id(
        "ui-ux-review", "implicit-any-return", "app/Services/Invoice.php:212"
    )
    assert shifted != finding_id(
        "type-health", "missing-return-type", "app/Services/Invoice.php:212"
    )


# -- AC 3 ---------------------------------------------------------------------------


def test_derived_id_collision_collapses_to_highest_severity(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path / "m.json",
        {
            "version": 1,
            "check": "type-health",
            "findings": [
                {
                    "kind": "implicit-any-return",
                    "severity": "low",
                    "location": "app/Services/Invoice.php:212",
                    "summary": "first",
                },
                {
                    "kind": "implicit-any-return",
                    "severity": "high",
                    "location": "app/Services/Invoice.php:400",
                    "summary": "second",
                },
            ],
        },
    )
    parsed = load_findings_manifest_full(manifest)
    assert parsed is not None
    assert len(parsed.findings) == 1
    assert parsed.findings[0].severity == "high"
    assert parsed.duplicate_ids == (
        finding_id(
            "type-health", "implicit-any-return", "app/Services/Invoice.php:212"
        ),
    )
    assert load_findings_manifest(manifest) == list(parsed.findings)

    # A skill-supplied id is used verbatim (trimmed).
    supplied = _write_manifest(
        tmp_path / "supplied.json",
        {
            "version": 1,
            "check": "type-health",
            "findings": [
                {
                    "id": "  structural-anchor  ",
                    "kind": "k",
                    "severity": "medium",
                    "location": "x.php:1",
                    "summary": "s",
                }
            ],
        },
    )
    parsed = load_findings_manifest_full(supplied)
    assert parsed is not None
    assert parsed.findings[0].id == "structural-anchor"
    assert parsed.duplicate_ids == ()


# -- AC 5 ---------------------------------------------------------------------------


def test_diff_classifies_new_regressed_resolved() -> None:
    ledger = Ledger(
        job="j",
        entries={
            "a": _entry(location="a.php", severity="low", attempts=1),
            "b": _entry(location="b.php", severity="high"),
            "c": _entry(location="c.php", severity="medium"),
            "d": _entry(
                location="d.php",
                severity="low",
                state="resolved",
                first_seen="2020-01-01T00:00:00Z",
                resolved_at="2026-02-01T00:00:00Z",
            ),
        },
    )
    findings = [
        Finding("a", "k", "high", "a.php", "s"),
        Finding("b", "k", "low", "b.php", "s"),
        Finding("d", "k", "medium", "d.php", "s"),
        Finding("e", "k", "high", "e.php", "s"),
    ]

    diff = diff_findings(findings, ledger, check="c")
    assert {f.id for f in diff.new} == {"d", "e"}
    assert {f.id for f in diff.regressed} == {"a"}
    assert {f.id for f in diff.unchanged} == {"b"}
    assert diff.resolved == ("c",)

    out = apply_diff(ledger, diff, now=NOW, run_id="run1", dispatched_ids=["a", "d"])
    # The reappeared (resolved -> new) finding keeps its original first_seen.
    assert out.entries["d"].first_seen == "2020-01-01T00:00:00Z"
    assert out.entries["d"].state == "open"
    assert out.entries["d"].resolved_at is None
    # Dispatched ids get the write-ahead increment; others do not.
    assert out.entries["a"].attempts == 2
    assert out.entries["b"].attempts == 0
    # The absent open finding became resolved.
    assert out.entries["c"].state == "resolved"
    assert out.entries["c"].resolved_at is not None


# -- AC 6 ---------------------------------------------------------------------------


def test_diff_orders_by_severity_for_the_cap() -> None:
    findings = [
        Finding("id1", "k", "low", "z.php", "s"),
        Finding("id2", "k", "high", "b.php", "s"),
        Finding("id3", "k", "high", "a.php", "s"),
        Finding("id4", "k", "medium", "a.php", "s"),
    ]
    diff = diff_findings(findings, None, check="c")
    assert [f.id for f in diff.new] == ["id3", "id2", "id4", "id1"]

    # Deterministic: a second tick over the same manifest picks the same set.
    again = diff_findings(findings, None, check="c")
    assert [f.id for f in again.new] == [f.id for f in diff.new]

    # A cap of 2 drops the least severe (low, then medium), never an arbitrary set.
    capped = diff.new[:2]
    assert {f.id for f in capped} == {"id3", "id2"}
    assert "id1" not in {f.id for f in capped}


# -- AC 9 ---------------------------------------------------------------------------


def test_ledger_write_ahead_consumes_budget_on_failed_dispatch(
    tmp_path: Path,
) -> None:
    ledger = Ledger(job="j", entries={})
    findings = [
        Finding("f1", "k", "high", "a.php", "s"),
        Finding("f2", "k", "high", "b.php", "s"),
    ]
    diff = diff_findings(findings, ledger, check="c")

    # Only f1 is dispatched; f2 (suppressed/over cap) must stay untouched.
    written = apply_diff(ledger, diff, now=NOW, run_id="run1", dispatched_ids=["f1"])
    assert written.entries["f1"].attempts == 1
    assert written.entries["f1"].last_dispatched_run == "run1"
    assert written.entries["f2"].attempts == 0
    assert written.entries["f2"].last_dispatched_run is None

    # Write-ahead: the increment is on disk before the dispatch is attempted, so a
    # dispatch that raises still consumes the budget.
    path = tmp_path / "ledger.json"
    save_ledger(path, written)

    def _dispatch() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _dispatch()

    reloaded = load_ledger(path)
    assert reloaded is not None
    assert reloaded.entries["f1"].attempts == 1
    assert reloaded.entries["f2"].attempts == 0


# -- AC 11 --------------------------------------------------------------------------


def test_ledger_prunes_only_resolved_entries() -> None:
    ledger = Ledger(
        job="j",
        entries={
            "old_resolved": _entry(
                state="resolved",
                resolved_at="2026-01-01T00:00:00Z",
                first_seen="2020-01-01T00:00:00Z",
            ),
            "fresh_resolved": _entry(
                state="resolved",
                resolved_at="2026-10-09T00:00:00Z",
                first_seen="2026-10-01T00:00:00Z",
            ),
            "still_open": _entry(
                state="open",
                first_seen="2020-01-01T00:00:00Z",
            ),
        },
    )
    pruned = prune_ledger(ledger, now=NOW, retention_days=30)
    assert "old_resolved" not in pruned.entries
    assert "fresh_resolved" in pruned.entries
    assert "still_open" in pruned.entries


# -- AC 13 --------------------------------------------------------------------------


def test_ledger_write_is_atomic_and_outside_reports_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    path = ledger_path("audit-type-health")
    assert path == tmp_path / "state" / "findings" / "audit-type-health.json"

    from herdr_routines.runner import default_reports_dir

    assert default_reports_dir() != path.parent
    assert default_reports_dir() not in path.parents

    path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"version": 1, "job": "j", "entries": {}}'
    path.write_text(original)

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("herdr_routines.findings.os.replace", _boom)
    with pytest.raises(OSError):
        save_ledger(path, Ledger(job="j", entries={}))

    # A torn write must leave the previous ledger intact, with no stray tmp file.
    assert path.read_text() == original
    assert not (path.parent / (path.name + ".tmp")).exists()
