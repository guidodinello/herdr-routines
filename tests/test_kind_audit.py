"""Acceptance tests for issue 057 Phase A: config + the record-only audit tick.

Covers criteria 1, 4, 7, 8, 10, 12, 14, 15, 16. No herdr server, no gh, no git.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from herdr_routines import tick
from herdr_routines.config import (
    _DEFAULTS_ALLOWED_KEYS,
    VALID_JOB_KINDS,
    AuditSpec,
    ConfigError,
    Job,
    load_config,
    load_config_dir,
)
from herdr_routines.findings import (
    Ledger,
    LedgerEntry,
    finding_id,
    ledger_path,
    load_findings_manifest,
    load_ledger,
    save_ledger,
)
from herdr_routines.history import read_job
from herdr_routines.runner import substitute_prompt
from herdr_routines.tick import _audit_phase_a

NOW = datetime(2026, 10, 10, 4, 0, 0, tzinfo=UTC)
RUN_ID = "audit-type-health-20261010T040000Z"


# -- helpers -------------------------------------------------------------------


def _base_audit_job() -> dict[str, Any]:
    return {
        "name": "audit-type-health",
        "cron": "0 4 * * 1",
        "repo": "/repo/fitted",
        "workspace": "worktree",
        "base": "development",
        "kind": "audit",
        "audit": {"skill": "type-health", "timeout_ms": 1_800_000},
        "max_findings_per_dispatch": 10,
        "max_attempts_per_target": 3,
        "ledger_retention_days": 90,
        "adopt_baseline": True,
    }


def _load_jobs(tmp_path: Path, *jobs: dict[str, Any]) -> Any:
    # The legacy single-file loader is used deliberately: unlike load_config_dir it
    # *raises* ConfigError on a bad job (directory loading collects per-file errors).
    path = tmp_path / "jobs.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "jobs": list(jobs)}))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return load_config(path)


def _jobs_d(tmp_path: Path, files: dict[str, str]) -> Path:
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (jobs_dir / name).write_text(content)
    return jobs_dir


def _audit_job(tmp_path: Path, **over: Any) -> Job:
    job = Job(
        name="audit-type-health",
        enabled=True,
        cron="0 4 * * 1",
        repo=tmp_path,
        workspace="worktree",
        base="main",
        agent_kind="opencode",
        model=None,
        prompt="",
        timeout_ms=2_700_000,
        start_timeout_ms=120_000,
        catch_up_minutes=720,
        timezone="UTC",
        on_missed="log",
        kind="audit",
        audit=AuditSpec(skill="type-health", command=None, timeout_ms=1_800_000),
        notify_policy="on-finding",
        max_findings_per_dispatch=10,
        max_attempts_per_target=3,
        ledger_retention_days=90,
        adopt_baseline=True,
    )
    return replace(job, **over)


def _write_manifest(
    tmp_path: Path,
    run_id: str,
    findings: list[dict[str, Any]],
    *,
    check: str = "type-health",
) -> Path:
    reports_dir = tmp_path / "state" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{run_id}-findings.json"
    path.write_text(json.dumps({"version": 1, "check": check, "findings": findings}))
    return path


class _FakeAuditClient:
    """Records notifications; phase A must never dispatch an agent or start a pane."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str | None, str]] = []
        self.dispatch_calls = 0

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append((title, body, sound))

    def tab_create(self, *_a: Any, **_k: Any) -> str:
        self.dispatch_calls += 1
        raise AssertionError("phase A must not start a pane")

    def agent_start(self, *_a: Any, **_k: Any) -> None:
        self.dispatch_calls += 1
        raise AssertionError("phase A must not dispatch an agent")


def _last_record(history_path: Path, job_name: str) -> Any:
    records = read_job(history_path, job_name)
    assert records, "no history records written"
    return records[-1]


# -- AC 1 ---------------------------------------------------------------------------


def test_audit_job_config_validation(tmp_path: Path) -> None:
    job = _load_jobs(tmp_path, _base_audit_job()).job("audit-type-health")
    assert job is not None
    assert job.kind == "audit"
    assert job.audit == AuditSpec(
        skill="type-health", command=None, timeout_ms=1_800_000
    )
    assert job.max_findings_per_dispatch == 10
    assert job.max_attempts_per_target == 3
    assert job.ledger_retention_days == 90
    assert job.adopt_baseline is True

    def _reject(over: dict[str, Any], match: str) -> None:
        job_dict = _base_audit_job()
        job_dict.update(over)
        with pytest.raises(ConfigError, match=match):
            _load_jobs(tmp_path, job_dict)

    _reject({"audit": {"skill": "a", "command": "b"}}, "exactly one")
    _reject({"audit": {}}, "exactly one")
    _reject({"audit": {"skill": "a", "timeout_ms": 0}}, "timeout_ms")
    _reject({"max_findings_per_dispatch": 0}, "max_findings_per_dispatch")
    _reject({"max_attempts_per_target": 0}, "max_attempts_per_target")
    _reject({"ledger_retention_days": 0}, "ledger_retention_days")
    _reject({"workspace": "root"}, "worktree")
    _reject({"checks": [{"command": "true"}]}, "checks")
    _reject({"target": "pr"}, "target")
    _reject({"max_workers_per_tick": 2}, "max_workers_per_tick")

    # Explicit target: base is accepted (uniform with 025's records).
    ok = _base_audit_job()
    ok["target"] = "base"
    assert _load_jobs(tmp_path, ok).job("audit-type-health").target == "base"

    # fix_prompt is per-job and accepted there...
    ok = _base_audit_job()
    ok["fix_prompt"] = "fix it"
    assert _load_jobs(tmp_path, ok).job("audit-type-health").fix_prompt == "fix it"

    # ...but not inheritable via defaults.yaml.
    assert "fix_prompt" not in _DEFAULTS_ALLOWED_KEYS
    jobs_dir = _jobs_d(
        tmp_path,
        {
            "defaults.yaml": "fix_prompt: nope\n",
            "audit-type-health.yaml": yaml.safe_dump(_base_audit_job()),
        },
    )
    with pytest.raises(ConfigError, match="fix_prompt"):
        load_config_dir(jobs_dir)

    assert "audit" in VALID_JOB_KINDS


# -- AC 4 ---------------------------------------------------------------------------


def test_manifest_validation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    # Never [] for an unverifiable manifest.
    assert load_findings_manifest(tmp_path / "missing.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_findings_manifest(bad) is None

    _write_manifest(tmp_path, "r", [], check="c")
    version = tmp_path / "state" / "reports" / "r-findings.json"
    version.write_text(json.dumps({"version": 2, "check": "c", "findings": []}))
    assert load_findings_manifest(version) is None

    malformed = _write_manifest(
        tmp_path,
        "r2",
        [{"kind": "k", "severity": "high", "summary": "missing location"}],
    )
    assert load_findings_manifest(malformed) is None

    oversize = _write_manifest(
        tmp_path,
        "r3",
        [
            {
                "id": "x" * 129,
                "kind": "k",
                "severity": "high",
                "location": "a",
                "summary": "s",
            }
        ],
    )
    assert load_findings_manifest(oversize) is None

    # A valid manifest with no findings is an empty list, not None.
    empty = _write_manifest(tmp_path, "r4", [])
    assert load_findings_manifest(empty) == []

    # The tick records the failure and dispatches nothing.
    job = _audit_job(tmp_path)
    client = _FakeAuditClient()
    summary, failed = _audit_phase_a(
        job,
        tmp_path / "history.jsonl",
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id="audit-type-health-missing",
    )
    assert failed is True
    assert "findings_manifest_invalid" in summary
    rec = _last_record(tmp_path / "history.jsonl", job.name)
    assert rec.state == "failed"
    assert rec.extra["gate"] == "failed"
    assert rec.extra["reason"] == "findings_manifest_invalid"
    assert client.dispatch_calls == 0


# -- AC 7 ---------------------------------------------------------------------------


def test_cold_start_adopts_baseline_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    job = _audit_job(tmp_path, adopt_baseline=True)
    _write_manifest(
        tmp_path,
        RUN_ID,
        [
            {
                "kind": "implicit-any-return",
                "severity": "high",
                "location": "a.php:1",
                "summary": "s",
            },
            {
                "kind": "missing-return-type",
                "severity": "low",
                "location": "b.php:2",
                "summary": "s",
            },
        ],
    )
    client = _FakeAuditClient()
    history = tmp_path / "history.jsonl"

    summary, failed = _audit_phase_a(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed is False
    assert "baseline" in summary
    rec = _last_record(history, job.name)
    assert rec.state == "done"
    assert rec.extra["gate"] == "baseline"
    assert rec.extra["adopted"] == 2
    assert rec.extra["dispatched_ids"] == []
    assert client.dispatch_calls == 0

    ledger = load_ledger(ledger_path(job.name))
    assert ledger is not None
    assert len(ledger.entries) == 2
    assert all(e.state == "open" and e.attempts == 0 for e in ledger.entries.values())

    # adopt_baseline: false on a cold start produces a dispatch set (still no dispatch
    # in phase A — the set is recorded, not acted on).
    ledger_path(job.name).unlink()
    job2 = _audit_job(tmp_path, adopt_baseline=False)
    client2 = _FakeAuditClient()
    _summary2, failed2 = _audit_phase_a(
        job2,
        history,
        client=client2,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed2 is False
    rec2 = _last_record(history, job2.name)
    assert rec2.extra["gate"] == "fix_dispatched"
    assert len(rec2.extra["dispatched_ids"]) == 2
    assert client2.dispatch_calls == 0


# -- AC 8 ---------------------------------------------------------------------------


def test_corrupt_ledger_fails_closed_without_rebaseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    job = _audit_job(tmp_path)
    _write_manifest(
        tmp_path,
        RUN_ID,
        [{"kind": "k", "severity": "high", "location": "a.php:1", "summary": "s"}],
    )
    path = ledger_path(job.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json")

    client = _FakeAuditClient()
    history = tmp_path / "history.jsonl"
    summary, failed = _audit_phase_a(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed is True
    assert "ledger_corrupt" in summary
    assert client.dispatch_calls == 0

    # No silent re-baseline: the corrupt file is copied aside, not overwritten.
    assert path.read_text() == "{ this is not valid json"
    backups = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert len(backups) == 1

    rec = _last_record(history, job.name)
    assert rec.state == "failed"
    assert rec.extra["gate"] == "failed"
    assert rec.extra["reason"] == "ledger_corrupt"


# -- AC 10 --------------------------------------------------------------------------


def test_suppressed_finding_stops_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    job = _audit_job(tmp_path, max_attempts_per_target=3)
    fid = finding_id("type-health", "implicit-any-return", "a.php")
    _write_manifest(
        tmp_path,
        RUN_ID,
        [
            {
                "kind": "implicit-any-return",
                "severity": "high",
                "location": "a.php:1",
                "summary": "s",
            }
        ],
    )

    path = ledger_path(job.name)
    save_ledger(
        path,
        Ledger(
            job=job.name,
            entries={
                fid: LedgerEntry(
                    kind="implicit-any-return",
                    location="a.php",
                    severity="high",
                    state="open",
                    first_seen="2026-01-01T00:00:00Z",
                    last_seen="2026-01-01T00:00:00Z",
                    attempts=3,
                )
            },
        ),
    )

    client = _FakeAuditClient()
    history = tmp_path / "history.jsonl"
    summary, failed = _audit_phase_a(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )
    # Suppression is a done, never a failure.
    assert failed is False
    assert "suppressed" in summary
    rec = _last_record(history, job.name)
    assert rec.state == "done"
    assert rec.extra["gate"] == "suppressed"
    assert rec.extra["dispatched_ids"] == []
    assert list(rec.extra["suppressed_ids"]) == [fid]
    assert client.dispatch_calls == 0

    # Not re-dispatched next cycle, and the budget is not incremented.
    _summary2, failed2 = _audit_phase_a(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed2 is False
    rec2 = _last_record(history, job.name)
    assert rec2.extra["gate"] == "suppressed"
    reloaded = load_ledger(path)
    assert reloaded is not None
    assert reloaded.entries[fid].attempts == 3


# -- AC 12 --------------------------------------------------------------------------


def test_clean_audit_night_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    job = _audit_job(tmp_path, notify_policy="on-finding")
    _write_manifest(tmp_path, RUN_ID, [])

    client = _FakeAuditClient()
    history = tmp_path / "history.jsonl"
    _summary, failed = _audit_phase_a(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed is False
    rec = _last_record(history, job.name)
    assert rec.state == "done"
    assert rec.extra["gate"] == "passed"
    assert rec.extra["dispatched_ids"] == []
    assert client.dispatch_calls == 0
    assert client.notifications == []


# -- AC 14 --------------------------------------------------------------------------


def test_substitute_prompt_resolves_findings_path(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "r.md"
    findings_path = tmp_path / "reports" / "r-findings.json"
    text = substitute_prompt(
        "report=$ROUTINE_REPORT findings=$ROUTINE_FINDINGS "
        "job=$ROUTINE_JOB run=$ROUTINE_RUN_ID",
        report_path=report_path,
        job_name="audit-type-health",
        run_id="r1",
        findings_path=findings_path,
    )
    assert str(report_path) in text
    assert str(findings_path) in text
    assert "job=audit-type-health" in text
    assert "run=r1" in text

    # A prompt carrying none of them passes through unchanged.
    assert (
        substitute_prompt(
            "just a prompt", report_path=report_path, job_name="j", run_id="r1"
        )
        == "just a prompt"
    )


# -- AC 15 --------------------------------------------------------------------------


def test_shipped_audit_job_config_validates() -> None:
    example_dir = Path(__file__).resolve().parent.parent / "deploy" / "jobs.d"
    cfg = load_config_dir(example_dir)
    job = cfg.job("audit-type-health")
    assert job is not None, cfg.errors
    assert job.kind == "audit"
    assert job.enabled is False
    assert job.audit is not None
    assert (job.audit.skill is None) != (job.audit.command is None)


# -- AC 16 --------------------------------------------------------------------------


def test_phase_a_records_dispatch_set_without_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    job = _audit_job(tmp_path, max_attempts_per_target=1)

    suppressed_id = finding_id("type-health", "implicit-any-return", "old.php")
    _write_manifest(
        tmp_path,
        RUN_ID,
        [
            {
                "kind": "implicit-any-return",
                "severity": "high",
                "location": "old.php:1",
                "summary": "old",
            },
            {
                "kind": "missing-return-type",
                "severity": "high",
                "location": "new.php:1",
                "summary": "new",
            },
        ],
    )
    save_ledger(
        ledger_path(job.name),
        Ledger(
            job=job.name,
            entries={
                suppressed_id: LedgerEntry(
                    kind="implicit-any-return",
                    location="old.php",
                    severity="high",
                    state="open",
                    first_seen="2026-01-01T00:00:00Z",
                    last_seen="2026-01-01T00:00:00Z",
                    attempts=1,
                )
            },
        ),
    )

    client = _FakeAuditClient()
    history = tmp_path / "history.jsonl"
    _summary, failed = _audit_phase_a(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed is False

    rec = _last_record(history, job.name)
    new_id = finding_id("type-health", "missing-return-type", "new.php")
    assert list(rec.extra["dispatched_ids"]) == [new_id]
    assert list(rec.extra["suppressed_ids"]) == [suppressed_id]
    assert rec.extra["gate"] == "fix_dispatched"

    # The full dispatch set is in the report too.
    report_path = Path(rec.extra["report_path"])
    report = report_path.read_text()
    assert new_id in report
    assert suppressed_id in report

    # No agent, no pane, no worktree.
    assert client.dispatch_calls == 0
    assert not (tmp_path / ".worktrees").exists()

    # _process_job routes kind: audit to _process_audit_job.
    called: dict[str, bool] = {}

    def _fake_process_audit(*_a: Any, **_k: Any) -> tuple[str, bool]:
        called["hit"] = True
        return ("routed", False)

    monkeypatch.setattr(tick, "_process_audit_job", _fake_process_audit)
    summary, failed = tick._process_job(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
    )
    assert called.get("hit") is True
    assert summary == "routed"
