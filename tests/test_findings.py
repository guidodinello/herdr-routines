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
    dispatch_pool,
    finding_id,
    ledger_path,
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

    out = apply_diff(
        ledger,
        diff,
        now=NOW,
        run_id="run1",
        queued_ids=["a", "d", "e"],
        dispatched_ids=["a", "d"],
    )
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


def test_apply_diff_spends_budget_for_exactly_the_dispatched_ids(
    tmp_path: Path,
) -> None:
    """Layer 1 (pure, `findings.apply_diff`) of the write-ahead trio: only the budget
    arithmetic, no dispatch and no disk I/O — that layer has neither. The tick-level
    ordering (ledger on disk before the fix worker is dispatched) lives in
    test_kind_audit.py::test_ledger_is_on_disk_before_the_fix_worker_is_dispatched, and
    the failure side in test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget."""
    ledger = Ledger(
        job="j",
        entries={
            # An untouched finding with history to preserve.
            "f0": _entry(
                location="z.php",
                severity="low",
                attempts=2,
                last_dispatched_run="run0",
                queued=False,
            ),
            # One already at its attempt cap: candidate, but not dispatched here.
            "f3": _entry(
                location="c.php",
                severity="low",
                attempts=3,
                last_dispatched_run="run0",
                queued=False,
            ),
        },
    )
    findings = [
        Finding("f1", "k", "high", "a.php", "s"),
        Finding("f2", "k", "high", "b.php", "s"),
        Finding("f3", "k", "low", "c.php", "s"),
        Finding("f0", "k", "low", "z.php", "s"),
    ]
    diff = diff_findings(findings, ledger, check="c")

    # Only f1 and f2 are dispatched; f3 (at its cap) and f0 (over cap) stay untouched.
    written = apply_diff(
        ledger,
        diff,
        now=NOW,
        run_id="run1",
        queued_ids=["f1", "f2", "f3"],
        dispatched_ids=["f1", "f2"],
    )
    assert written.entries["f1"].attempts == 1
    assert written.entries["f1"].last_dispatched_run == "run1"
    assert written.entries["f1"].queued is True
    assert written.entries["f2"].attempts == 1
    assert written.entries["f2"].last_dispatched_run == "run1"
    assert written.entries["f2"].queued is True
    # Non-dispatched: attempts and their previous last_dispatched_run are kept.
    assert written.entries["f3"].attempts == 3
    assert written.entries["f3"].last_dispatched_run == "run0"
    assert written.entries["f3"].queued is True
    assert written.entries["f0"].attempts == 2
    assert written.entries["f0"].last_dispatched_run == "run0"
    # queued is preserved for ids the caller did not queue.
    assert written.entries["f0"].queued is False

    # The increment survives a save/load round trip. That the tick saves it *before*
    # dispatching is pinned through the real tick by
    # test_kind_audit.py::test_ledger_is_on_disk_before_the_fix_worker_is_dispatched.
    path = tmp_path / "ledger.json"
    save_ledger(path, written)
    reloaded = load_ledger(path)
    assert reloaded is not None
    assert reloaded.entries["f1"].attempts == 1
    assert reloaded.entries["f1"].last_dispatched_run == "run1"
    assert reloaded.entries["f1"].queued is True
    assert reloaded.entries["f2"].attempts == 1
    assert reloaded.entries["f2"].last_dispatched_run == "run1"
    assert reloaded.entries["f2"].queued is True
    assert reloaded.entries["f3"].attempts == 3
    assert reloaded.entries["f3"].last_dispatched_run == "run0"
    assert reloaded.entries["f0"].attempts == 2
    assert reloaded.entries["f0"].last_dispatched_run == "run0"
    assert reloaded.entries["f0"].queued is False


def test_unfixed_findings_stay_in_the_dispatch_pool(tmp_path: Path) -> None:
    """A finding that was capped out, or whose fix did not land, is "unchanged" on the
    next cycle — it must stay owed a fix rather than silently joining the backlog."""
    findings = [Finding(f"f{i}", "k", "high", f"{i}.php", "s") for i in range(3)]
    empty = Ledger(job="j", entries={})
    diff = diff_findings(findings, empty, check="c")
    ids = [f.id for f in findings]
    # Cycle 1: cap of 2 -> f0, f1 dispatched; f2 capped out.
    led = apply_diff(
        empty, diff, now=NOW, run_id="r1", queued_ids=ids, dispatched_ids=ids[:2]
    )
    save_ledger(tmp_path / "l.json", led)
    reloaded = load_ledger(tmp_path / "l.json")
    assert reloaded is not None and all(e.queued for e in reloaded.entries.values())
    led = reloaded

    # Cycle 2: nothing got fixed. All three are unchanged and all three are still owed.
    diff2 = diff_findings(findings, led, check="c")
    assert len(diff2.unchanged) == 3
    assert [f.id for f in dispatch_pool(diff2, led)] == ids

    # A baseline adoption records debt without queueing it.
    adopted = apply_diff(
        empty, diff, now=NOW, run_id="r1", queued_ids=[], dispatched_ids=[]
    )
    assert dispatch_pool(diff_findings(findings, adopted, check="c"), adopted) == ()

    # Resolve clears the queue; a reappearance re-queues it and keeps its attempts, so a
    # flapping fix cannot earn a fresh budget.
    gone = diff_findings(findings[1:], led, check="c")
    led = apply_diff(led, gone, now=NOW, run_id="r2", queued_ids=[], dispatched_ids=[])
    assert led.entries["f0"].state == "resolved" and not led.entries["f0"].queued
    back = diff_findings(findings, led, check="c")
    assert [f.id for f in back.new] == ["f0"]
    led = apply_diff(
        led, back, now=NOW, run_id="r3", queued_ids=["f0"], dispatched_ids=["f0"]
    )
    assert led.entries["f0"].attempts == 2
    assert led.entries["f0"].queued


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


def _ledger_payload(**entry_over: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "kind": "k",
        "location": "a.php",
        "severity": "high",
        "state": "open",
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": "2026-01-01T00:00:00Z",
    }
    entry.update(entry_over)
    return {"version": 1, "job": "j", "entries": {"f1": entry}}


def test_load_ledger_accepts_known_severity_and_state(tmp_path: Path) -> None:
    loaded = load_ledger(
        _write_manifest(
            tmp_path / "l.json", _ledger_payload(severity="low", state="resolved")
        )
    )
    assert loaded is not None
    assert loaded.entries["f1"].severity == "low"
    assert loaded.entries["f1"].state == "resolved"


@pytest.mark.parametrize("over", [{"severity": "critical"}, {"state": "closed"}])
def test_load_ledger_rejects_unknown_severity_or_state(
    tmp_path: Path, over: dict[str, object]
) -> None:
    path = _write_manifest(tmp_path / "l.json", _ledger_payload(**over))
    assert load_ledger(path) is None


def test_manifest_rejects_unknown_severity(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path / "m.json",
        {
            "version": 1,
            "check": "c",
            "findings": [
                {
                    "kind": "k",
                    "severity": "critical",
                    "location": "a.php",
                    "summary": "s",
                }
            ],
        },
    )
    assert load_findings_manifest_full(path) is None
