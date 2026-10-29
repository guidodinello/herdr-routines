"""Acceptance tests for issue 057 Phase A: config + the record-only audit tick.

Covers criteria 1, 4, 7, 8, 10, 12, 14, 15, 16. No herdr server, no gh, no git.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from herdr_routines import tick
from herdr_routines.auto_fix import consecutive_base_gate_failures
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
    load_findings_manifest_full,
    load_ledger,
    save_ledger,
)
from herdr_routines.history import read_job
from herdr_routines.runner import substitute_prompt

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
    """Records notifications. The diff step never starts a pane itself: dispatch goes
    through `_dispatch_audit_fix`, which these tests replace with `_FixRecorder`."""

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


def _diff_step(
    job: Job, history_path: Path, *, client: Any, now: datetime, run_id: str
) -> tuple[str, bool]:
    """Phases 2-3 of an audit cycle, with phase 1 (the audit) stood in for by a manifest
    the test wrote itself."""
    return tick._diff_and_fix(
        job,
        history_path,
        client=client,
        now=now,
        run_id=run_id,
        paths=tick._AuditPaths.for_run(job.name, run_id),
    )


class _FixRecorder:
    """Stands in for `_dispatch_audit_fix`: records each dispatch, settles idle."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, job: Job, **kwargs: Any) -> Any:
        self.calls.append([f.id for f in kwargs["findings"]])
        return tick._AgentRun(final_agent_status="idle")


def _last_record(history_path: Path, job_name: str) -> Any:
    records = read_job(history_path, job_name)
    assert records, "no history records written"
    return records[-1]


# -- AC 1 ---------------------------------------------------------------------------


def test_audit_only_keys_are_ignored_off_kind(tmp_path: Path) -> None:
    # Inert outside kind: audit, so a malformed value still loads and stores the default.
    job = _load_jobs(
        tmp_path,
        {
            "name": "plain",
            "cron": "0 3 * * *",
            "repo": str(tmp_path),
            "adopt_baseline": "yes",
            "max_findings_per_dispatch": "many",
            "ledger_retention_days": 0,
        },
    ).job("plain")
    assert job is not None
    assert job.adopt_baseline is True
    assert job.max_findings_per_dispatch == 10
    assert job.ledger_retention_days == 90


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
    assert load_findings_manifest_full(tmp_path / "missing.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_findings_manifest_full(bad) is None

    _write_manifest(tmp_path, "r", [], check="c")
    version = tmp_path / "state" / "reports" / "r-findings.json"
    version.write_text(json.dumps({"version": 2, "check": "c", "findings": []}))
    assert load_findings_manifest_full(version) is None

    malformed = _write_manifest(
        tmp_path,
        "r2",
        [{"kind": "k", "severity": "high", "summary": "missing location"}],
    )
    assert load_findings_manifest_full(malformed) is None

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
    assert load_findings_manifest_full(oversize) is None

    # A valid manifest with no findings is an empty list, not None.
    empty = _write_manifest(tmp_path, "r4", [])
    empty_manifest = load_findings_manifest_full(empty)
    assert empty_manifest is not None
    assert empty_manifest.findings == ()

    # The tick records the failure and dispatches nothing.
    job = _audit_job(tmp_path)
    client = _FakeAuditClient()
    summary, failed = _diff_step(
        job,
        tmp_path / "history.jsonl",
        client=client,
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

    summary, failed = _diff_step(
        job,
        history,
        client=client,
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

    # adopt_baseline: false on a cold start dispatches immediately.
    ledger_path(job.name).unlink()
    job2 = _audit_job(tmp_path, adopt_baseline=False)
    client2 = _FakeAuditClient()
    fix = _FixRecorder()
    monkeypatch.setattr(tick, "_dispatch_audit_fix", fix)
    _summary2, failed2 = _diff_step(
        job2,
        history,
        client=client2,
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed2 is False
    rec2 = _last_record(history, job2.name)
    assert rec2.extra["gate"] == "fix_dispatched"
    assert len(rec2.extra["dispatched_ids"]) == 2
    assert len(fix.calls) == 1 and len(fix.calls[0]) == 2


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
    summary, failed = _diff_step(
        job,
        history,
        client=client,
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
    summary, failed = _diff_step(
        job,
        history,
        client=client,
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
    _summary2, failed2 = _diff_step(
        job,
        history,
        client=client,
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
    _summary, failed = _diff_step(
        job,
        history,
        client=client,
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


def test_report_and_extra_record_the_full_dispatch_set(
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
    fix = _FixRecorder()
    monkeypatch.setattr(tick, "_dispatch_audit_fix", fix)
    _summary, failed = _diff_step(
        job,
        history,
        client=client,
        now=NOW,
        run_id=RUN_ID,
    )
    assert failed is False

    rec = _last_record(history, job.name)
    new_id = finding_id("type-health", "missing-return-type", "new.php")
    assert list(rec.extra["dispatched_ids"]) == [new_id]
    assert list(rec.extra["suppressed_ids"]) == [suppressed_id]
    assert rec.extra["gate"] == "fix_dispatched"
    assert fix.calls == [[new_id]]

    # The full dispatch set is in the report too.
    report_path = Path(rec.extra["report_path"])
    report = report_path.read_text()
    assert new_id in report
    assert suppressed_id in report

    # The dispatched finding consumed one attempt, and the worker settled, so it is
    # handed off: no longer queued for another dispatch.
    ledger = load_ledger(ledger_path(job.name))
    assert ledger is not None
    assert ledger.entries[new_id].attempts == 1
    assert ledger.entries[new_id].last_dispatched_run == RUN_ID
    assert not ledger.entries[new_id].queued

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


# -- issue 067: the audit record's runtime shape (spec v2, acceptance criteria 2, 4-9) --
#
# The write-ahead ordering is specified at three layers. This module owns layer 2 (tick
# ordering); the pure budget arithmetic (layer 1) lives in
# test_findings.py::test_apply_diff_spends_budget_for_exactly_the_dispatched_ids and the
# failure side (layer 3) in
# test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget.


def _finding_payload(seq: int, severity: str = "high") -> dict[str, Any]:
    return {
        "kind": "implicit-any-return",
        "severity": severity,
        "location": f"app/F{seq:02}.php:{seq + 1}",
        "summary": f"finding {seq}",
    }


def _capped_ledger(seqs: list[int] | range) -> dict[str, LedgerEntry]:
    """Ledger entries at their attempt budget for the findings `_finding_payload(seq)`
    would produce, so every finding in a manifest is suppressed."""
    entries: dict[str, LedgerEntry] = {}
    for seq in seqs:
        payload = _finding_payload(seq)
        location = payload["location"].split(":")[0]
        entries[finding_id("type-health", payload["kind"], location)] = LedgerEntry(
            kind=payload["kind"],
            location=location,
            severity=payload["severity"],
            state="open",
            first_seen="2026-01-01T00:00:00Z",
            last_seen="2026-01-01T00:00:00Z",
            attempts=3,
        )
    return entries


def _scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    findings: list[dict[str, Any]] | None = None,
    ledger_entries: dict[str, LedgerEntry] | None = None,
    adopt: bool = True,
    max_findings: int = 10,
    fix_run: Any = None,
    manifest_json: str | None = None,
    corrupt_ledger: bool = False,
) -> Any:
    """One phases-2/3 cycle in a fresh state dir, returning the record it wrote."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state" / name))
    job = _audit_job(
        tmp_path, adopt_baseline=adopt, max_findings_per_dispatch=max_findings
    )
    if corrupt_ledger:
        path = tmp_path / "state" / name / "findings" / f"{job.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not valid json")
    if ledger_entries is not None:
        path = ledger_path(job.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        save_ledger(path, Ledger(job=job.name, entries=ledger_entries))
    if manifest_json is not None:
        reports = tmp_path / "state" / name / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        reports.joinpath(f"{RUN_ID}-findings.json").write_text(manifest_json)
    elif findings is not None:
        reports = tmp_path / "state" / name / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{RUN_ID}-findings.json").write_text(
            json.dumps({"version": 1, "check": "type-health", "findings": findings})
        )
    if fix_run is None:
        monkeypatch.setattr(tick, "_dispatch_audit_fix", _FixRecorder())
    else:
        monkeypatch.setattr(
            tick,
            "_dispatch_audit_fix",
            lambda job, **_kw: fix_run,
        )
    tick._diff_and_fix(
        job,
        tmp_path / "state" / name / "history.jsonl",
        client=_FakeAuditClient(),  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
        paths=tick._AuditPaths.for_run(job.name, RUN_ID),
    )
    records = read_job(tmp_path / "state" / name / "history.jsonl", job.name)
    assert records
    return records[-1]


def _extra_of(record: Any) -> Mapping[str, Any]:
    extra = record.extra or {}
    assert isinstance(extra, Mapping)
    return extra


def test_ledger_is_on_disk_before_the_fix_worker_is_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layer 2 (tick ordering) of the write-ahead trio: the ledger is saved *before*
    `_dispatch_audit_fix` is called. The recorder reads the ledger from disk at call
    time and requires the write-ahead state already to be present. Layer 1 (pure
    arithmetic) lives in test_findings.py and the failure side in
    test_audit_dispatch.py::test_failed_fix_dispatch_still_consumes_budget."""
    captured: dict[str, Any] = {}

    def recorder(job: Job, **kwargs: Any) -> Any:
        from_disk = load_ledger(kwargs["paths"].ledger)
        captured["ledger"] = from_disk
        captured["finding_id"] = kwargs["findings"][0].id
        captured["run_id"] = kwargs["run_id"]
        return tick._AgentRun(final_agent_status="idle")

    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    job = _audit_job(tmp_path, adopt_baseline=False)
    payload = _finding_payload(1)
    _write_manifest(tmp_path, RUN_ID, [payload])
    monkeypatch.setattr(tick, "_dispatch_audit_fix", recorder)
    tick._diff_and_fix(
        job,
        tmp_path / "history.jsonl",
        client=_FakeAuditClient(),  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
        paths=tick._AuditPaths.for_run(job.name, RUN_ID),
    )

    # The dispatch was handed a ledger that already carried this run's budget spend.
    ledger = captured["ledger"]
    assert ledger is not None
    fid = captured["finding_id"]
    entry = ledger.entries[fid]
    assert captured["run_id"] == RUN_ID
    assert entry.attempts == 1
    assert entry.last_dispatched_run == RUN_ID
    # Not handed off yet: that happens only after the worker settles.
    assert entry.queued is True


def test_audit_gate_set_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 4: the gate set is closed at five members. The tick indexes
    `_AUDIT_GATE_NOTIFY_KIND` directly, so a sixth gate value would ``KeyError`` on a
    cron fire; the anchor and the driven scenarios must both agree on the five."""
    expected = {"baseline", "passed", "fix_dispatched", "suppressed", "failed"}
    assert set(tick._AUDIT_GATE_NOTIFY_KIND) == expected

    seen: set[str] = set()

    def _add(record: Any) -> None:
        gate = _extra_of(record)["gate"]
        assert isinstance(gate, str)
        seen.add(gate)

    # baseline: cold start adopting a non-empty report.
    _add(_scenario(tmp_path, monkeypatch, "baseline", findings=[_finding_payload(1)]))
    # passed: cold start with an empty report.
    _add(_scenario(tmp_path, monkeypatch, "passed", findings=[]))
    # suppressed: everything current is at its attempt budget.
    _add(
        _scenario(
            tmp_path,
            monkeypatch,
            "suppressed",
            findings=[_finding_payload(1)],
            ledger_entries=_capped_ledger([1]),
        )
    )
    # fix_dispatched: fresh findings with dispatching on.
    _add(
        _scenario(
            tmp_path,
            monkeypatch,
            "fix_dispatched",
            findings=[_finding_payload(2)],
            adopt=False,
        )
    )
    # failed: a pre-verdict failure, like a corrupt ledger.
    _add(
        _scenario(
            tmp_path,
            monkeypatch,
            "failed",
            findings=[_finding_payload(3)],
            adopt=False,
            manifest_json="{not json",
        )
    )

    assert seen == expected


def test_audit_state_gate_pairings_match_the_design_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5: every state/gate pairing the Design table names, driven through
    `_diff_and_fix`. The two special cases are `failed` + `fix_dispatched` (the dispatch
    failed after a verdict) and `failed` + `failed` (pre-verdict failure: manifest
    invalid and ledger corrupt)."""
    pairings: set[tuple[str, str]] = set()

    def _add(record: Any) -> None:
        pairing = (
            record.state,
            _extra_of(record)["gate"],
        )
        pairings.add(pairing)

    _add(_scenario(tmp_path, monkeypatch, "baseline", findings=[_finding_payload(1)]))
    _add(_scenario(tmp_path, monkeypatch, "passed", findings=[]))
    _add(
        _scenario(
            tmp_path,
            monkeypatch,
            "suppressed",
            findings=[_finding_payload(2)],
            ledger_entries=_capped_ledger([2]),
        )
    )
    _add(
        _scenario(
            tmp_path,
            monkeypatch,
            "fix_dispatched",
            findings=[_finding_payload(4)],
            adopt=False,
        )
    )
    # Dispatch failed after the verdict landed: the record keeps gate=fix_dispatched...
    rec = _scenario(
        tmp_path,
        monkeypatch,
        "dispatch_failed",
        findings=[_finding_payload(5)],
        adopt=False,
        fix_run=tick._AgentRun(reason="agent_start_failed", error="boom"),
    )
    assert rec.state == "failed" and _extra_of(rec)["gate"] == "fix_dispatched"
    pairings.add(("failed", "fix_dispatched"))
    # ...while the pre-verdict failures are failed+failed, for both reasons.
    rec = _scenario(tmp_path, monkeypatch, "manifest_invalid", adopt=False)
    assert rec.state == "failed" and _extra_of(rec)["gate"] == "failed"
    assert _extra_of(rec)["reason"] == "findings_manifest_invalid"
    pairings.add(("failed", "failed"))
    rec = _scenario(
        tmp_path,
        monkeypatch,
        "ledger_corrupt",
        findings=[],
        manifest_json=json.dumps(
            {"version": 1, "check": "type-health", "findings": [_finding_payload(6)]}
        ),
        adopt=False,
        corrupt_ledger=True,
    )
    assert rec.state == "failed" and _extra_of(rec)["gate"] == "failed"
    assert _extra_of(rec)["reason"] == "ledger_corrupt"
    pairings.add(("failed", "failed"))

    assert pairings == {
        ("done", "baseline"),
        ("done", "passed"),
        ("done", "suppressed"),
        ("done", "fix_dispatched"),
        ("failed", "fix_dispatched"),
        ("failed", "failed"),
    }


_COMMON_KEYS = {
    "gate",
    "target",
    "manifest_path",
    "ledger_path",
    "report_path",
    "report_written",
}
_DIFF_KEYS = {
    "check",
    "duplicate_ids",
    "findings_total",
    "new",
    "regressed",
    "unchanged",
    "resolved",
    "dispatched",
    "suppressed",
    "adopted",
    "dispatched_ids",
    "suppressed_ids",
}
_DISPATCH_KEYS = {
    "gate_branch",
    "branch",
    "agent_name",
    "pane_id",
    "final_agent_status",
    "session_id",
    "fix_report_path",
    "fix_report_written",
}
_FAILURE_KEYS = {"reason", "error"}


def _assert_extra_keys(
    extra: Mapping[str, Any], *, required: set[str], allowed: set[str]
) -> None:
    seen = set(extra)
    assert required <= seen
    assert seen <= allowed


def test_audit_extra_key_sets_are_per_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6: every record carries the common keys; the diff path alone adds the
    diff keys; the dispatch path alone adds the dispatch keys; failure paths add
    `reason` (with `error` allowed); and no key that is neither required nor allowed
    appears on a path."""
    allowed_diff = _COMMON_KEYS | _DIFF_KEYS
    allowed_dispatch = allowed_diff | _DISPATCH_KEYS
    allowed_dispatch_failed = allowed_dispatch | _FAILURE_KEYS
    allowed_failure = _COMMON_KEYS | _FAILURE_KEYS

    baseline = _extra_of(
        _scenario(tmp_path, monkeypatch, "baseline", findings=[_finding_payload(1)])
    )
    _assert_extra_keys(
        baseline, required=_COMMON_KEYS | _DIFF_KEYS, allowed=allowed_diff
    )

    passed = _extra_of(_scenario(tmp_path, monkeypatch, "passed", findings=[]))
    _assert_extra_keys(passed, required=_COMMON_KEYS | _DIFF_KEYS, allowed=allowed_diff)

    suppressed = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "suppressed",
            findings=[_finding_payload(2)],
            ledger_entries=_capped_ledger([2]),
        )
    )
    _assert_extra_keys(
        suppressed, required=_COMMON_KEYS | _DIFF_KEYS, allowed=allowed_diff
    )

    dispatched = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "dispatched",
            findings=[_finding_payload(4)],
            adopt=False,
        )
    )
    _assert_extra_keys(dispatched, required=allowed_dispatch, allowed=allowed_dispatch)

    # A failed dispatch keeps the dispatch keys and adds reason (+ error).
    dispatch_failed = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "dispatch_failed",
            findings=[_finding_payload(5)],
            adopt=False,
            fix_run=tick._AgentRun(reason="agent_start_failed", error="boom"),
        )
    )
    _assert_extra_keys(
        dispatch_failed,
        required=allowed_dispatch | {"reason"},
        allowed=allowed_dispatch_failed,
    )
    assert dispatch_failed["reason"] == "agent_start_failed"
    assert dispatch_failed["error"] == "boom"

    # Pre-verdict failures carry no diff or dispatch keys.
    pre_verdict = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "manifest_invalid",
            findings=None,
            adopt=False,
        )
    )
    _assert_extra_keys(
        pre_verdict, required=_COMMON_KEYS | {"reason"}, allowed=allowed_failure
    )
    corrupt = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "ledger_corrupt",
            findings=[_finding_payload(6)],
            manifest_json="{not json",
            adopt=False,
        )
    )
    _assert_extra_keys(
        corrupt, required=_COMMON_KEYS | {"reason"}, allowed=allowed_failure
    )


def _assert_counts_agree(extra: Mapping[str, Any]) -> None:
    assert extra["findings_total"] == (
        extra["new"] + extra["regressed"] + extra["unchanged"]
    )
    assert extra["dispatched"] == len(extra["dispatched_ids"])
    assert extra["suppressed"] >= len(extra["suppressed_ids"])


def test_audit_counts_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 7: on the diff path the counts agree with the id lists."""
    # Baseline adoption: adopted == findings_total, no duplicate ids.
    baseline = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "baseline",
            findings=[_finding_payload(1), _finding_payload(2)],
        )
    )
    assert baseline["adopted"] == baseline["findings_total"] == 2
    assert baseline["dispatched"] == 0
    assert baseline["dispatched_ids"] == []
    assert baseline["duplicate_ids"] == []

    # A cap of 10 over 12 fresh findings: dispatched == len(dispatched_ids) == 10,
    # adopted == 0 (not a baseline).
    capped = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "capped_dispatch",
            findings=[_finding_payload(i) for i in range(12)],
            adopt=False,
        )
    )
    _assert_counts_agree(capped)
    assert capped["adopted"] == 0
    assert capped["dispatched"] == len(capped["dispatched_ids"]) == 10

    # A suppressed finding is counted too, without entering the dispatched budget.
    suppressed = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "suppressed",
            findings=[_finding_payload(20)],
            ledger_entries=_capped_ledger([20]),
            adopt=False,
        )
    )
    _assert_counts_agree(suppressed)
    assert suppressed["suppressed"] == len(suppressed["suppressed_ids"]) == 1
    assert suppressed["dispatched"] == 0

    # A manifest collapse puts the collapsed id in duplicate_ids and still agrees.
    collapsed = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "collapsed",
            adopt=False,
            manifest_json=json.dumps(
                {
                    "version": 1,
                    "check": "type-health",
                    "findings": [
                        {
                            "kind": "implicit-any-return",
                            "severity": "low",
                            "location": "a.php:12",
                            "summary": "first",
                        },
                        {
                            "kind": "implicit-any-return",
                            "severity": "high",
                            "location": "a.php:400",
                            "summary": "second",
                        },
                    ],
                }
            ),
        )
    )
    _assert_counts_agree(collapsed)
    assert collapsed["duplicate_ids"] == [
        finding_id("type-health", "implicit-any-return", "a.php")
    ]
    assert collapsed["findings_total"] == 1
    assert collapsed["duplicate_ids"] != []


def test_suppressed_count_is_true_while_ids_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 8: `suppressed` is the true count while `suppressed_ids` is capped at
    `tick._AUDIT_SUPPRESSED_ID_CAP` (20). `dispatched_ids` is bounded by
    `max_findings_per_dispatch`, not capped this way."""
    capped_out = _capped_ledger(range(25))
    extra = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "over_cap",
            findings=[_finding_payload(seq) for seq in range(25)],
            ledger_entries=capped_out,
            adopt=False,
        )
    )
    assert tick._AUDIT_SUPPRESSED_ID_CAP == 20
    assert extra["gate"] == "suppressed"
    assert extra["suppressed"] == 25
    assert len(extra["suppressed_ids"]) == 20
    assert extra["dispatched"] == 0
    assert extra["dispatched_ids"] == []

    # dispatched_ids is bounded by max_findings_per_dispatch (25 here), not by the cap.
    dispatched = _extra_of(
        _scenario(
            tmp_path,
            monkeypatch,
            "dispatched_uncapped",
            findings=[_finding_payload(seq) for seq in range(25)],
            adopt=False,
            max_findings=25,
        )
    )
    assert dispatched["gate"] == "fix_dispatched"
    assert dispatched["dispatched"] == len(dispatched["dispatched_ids"]) == 25


def test_audit_records_never_move_consecutive_base_gate_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 9: audit records never move
    `auto_fix.consecutive_base_gate_failures` — the ledger's
    `max_attempts_per_target` is the budget, never charged twice. Both a failed audit
    dispatch and a failed audit command leave the counter at 0."""
    history = tmp_path / "state" / "cross" / "history.jsonl"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state" / "cross"))
    job = _audit_job(tmp_path, adopt_baseline=False)

    # A failed audit dispatch (record failed + gate fix_dispatched).
    monkeypatch.setattr(
        tick,
        "_dispatch_audit_fix",
        lambda job, **_kw: tick._AgentRun(reason="agent_start_failed", error="boom"),
    )
    payload = _finding_payload(1)
    _write_manifest(tmp_path, RUN_ID, [payload])
    for _ in range(3):
        tick._diff_and_fix(
            job,
            history,
            client=_FakeAuditClient(),  # type: ignore[arg-type]
            now=NOW,
            run_id=RUN_ID,
            paths=tick._AuditPaths.for_run(job.name, RUN_ID),
        )
    assert consecutive_base_gate_failures(history, job.name) == 0

    # A failed audit command records its failure without a gate_branch, via the
    # pre-verdict failure path (_record_audit_failure).
    tick._record_audit_failure(
        job,
        history,
        client=_FakeAuditClient(),  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
        paths=tick._AuditPaths.for_run(job.name, RUN_ID),
        reason="audit_command_failed",
    )
    assert consecutive_base_gate_failures(history, job.name) == 0
