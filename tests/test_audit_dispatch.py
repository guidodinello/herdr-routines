"""Acceptance tests for issue 058: kind: audit phase B (run the audit, dispatch one fix
worker).

The audit and fix phases run against a real git repository, so worktree creation and
removal are the real `git worktree` commands; only herdr is faked.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from herdr_routines import tick
from herdr_routines.auto_fix import (
    build_audit_agent_name,
    build_audit_fix_prompt,
    build_gate_worker_agent_name,
)
from herdr_routines.cli import _check_systemd_timeout
from herdr_routines.config import AuditSpec, GateCheck, Job, RoutinesConfig
from herdr_routines.findings import Finding, ledger_path, load_ledger
from herdr_routines.herdr import HerdrCliError
from herdr_routines.history import HistoryRecord, append, read_job

NOW = datetime(2026, 10, 12, 4, 0, 0, tzinfo=UTC)
RUN_ID = "audit-type-health-20261012T040000Z"
BRANCH = "auto/audit-type-health-20261012T040000Z"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repo with one commit on `main`; ensure_repo stubbed (no remote)."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "a.php").write_text("<?php\n")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tick, "ensure_repo", lambda job: job.repo)
    return path


def _job(repo: Path, **over: Any) -> Job:
    job = Job(
        name="audit-type-health",
        enabled=True,
        cron="0 4 * * 1",
        repo=repo,
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
        adopt_baseline=False,
    )
    return replace(job, **over)


def _finding(i: int, severity: str = "high") -> dict[str, str]:
    return {
        "kind": "implicit-any-return",
        "severity": severity,
        "location": f"app/F{i:02}.php:{i}",
        "summary": f"finding {i}",
    }


def _manifest_path() -> Path:
    return tick.default_reports_dir() / f"{RUN_ID}-findings.json"


def _write_manifest(findings: list[dict[str, str]]) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "check": "type-health", "findings": findings})
    )


class _Agent:
    """One agent the fake herdr started: its pane cwd and the prompt it was given."""

    def __init__(self, name: str, cwd: str) -> None:
        self.name = name
        self.cwd = Path(cwd)
        self.prompt: str | None = None
        self.cwd_existed_at_prompt = False


class _FakeHerdr:
    """Enough of HerdrClient for an audit cycle. `on_prompt(agent)` runs when an agent
    is prompted (the audit agent writes its manifest there) and returns its settle
    status."""

    def __init__(
        self,
        on_prompt: Callable[[_Agent], str] | None = None,
        *,
        statuses: dict[str, str] | None = None,
        fail_start: Callable[[str], bool] | None = None,
    ) -> None:
        self._on_prompt = on_prompt or (lambda _agent: "idle")
        self._statuses = statuses or {}
        self._fail_start = fail_start or (lambda _name: False)
        self._pending_cwd: str | None = None
        self.agents: list[_Agent] = []
        self.notifications: list[str] = []

    def tab_create(self, *, cwd: str, label: str | None = None) -> str:
        self._pending_cwd = cwd
        return f"w1:p{len(self.agents) + 1}"

    def agent_start(self, *, name: str, **_kw: Any) -> None:
        if self._fail_start(name):
            raise HerdrCliError(f"could not start {name}", exit_code=1)
        assert self._pending_cwd is not None
        self.agents.append(_Agent(name, self._pending_cwd))

    def agent_interactive_ready(self, target: str) -> bool:
        return True

    def agent_prompt_wait_with_watchdog(
        self, *, target: str, text: str, timeout_ms: int, **_kw: Any
    ) -> str:
        agent = next(a for a in self.agents if a.name == target)
        agent.prompt = text
        agent.cwd_existed_at_prompt = agent.cwd.is_dir()
        return self._on_prompt(agent)

    def agent_read_visible(self, target: str, *, lines: int = 200) -> str:
        return ""

    def agent_session_id(self, name: str) -> str:
        return f"ses-{name}"

    def pane_close(self, pane_id: str) -> None:
        pass

    def agent_statuses(self) -> dict[str, str]:
        return dict(self._statuses)

    def notification_show(
        self, title: str, *, body: str | None = None, sound: str = "none"
    ) -> None:
        self.notifications.append(title)


def _is_audit_agent(name: str) -> bool:
    return name == build_audit_agent_name("audit-type-health", RUN_ID)


def _manifest_fixture(tmp_path: Path, findings: list[dict[str, str]]) -> Path:
    """A manifest on disk for an `audit.command` to copy into place."""
    path = tmp_path / "fixture-findings.json"
    path.write_text(
        json.dumps({"version": 1, "check": "type-health", "findings": findings})
    )
    return path


def _auditor(findings: list[dict[str, str]]) -> Callable[[_Agent], str]:
    """The audit agent writes the manifest; any other agent just settles."""

    def on_prompt(agent: _Agent) -> str:
        if _is_audit_agent(agent.name):
            _write_manifest(findings)
        return "idle"

    return on_prompt


def _cycle(job: Job, client: _FakeHerdr, history: Path) -> tuple[str, bool]:
    return tick._audit_cycle(
        job,
        history,
        client=client,  # type: ignore[arg-type]
        now=NOW,
        run_id=RUN_ID,
    )


def _last(history: Path) -> tuple[HistoryRecord, dict[str, Any]]:
    rec = read_job(history, "audit-type-health")[-1]
    return rec, rec.extra or {}


# -- AC 1 ---------------------------------------------------------------------------


def test_audit_command_exit_code_is_not_the_gate(repo: Path, tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    fixture = _manifest_fixture(tmp_path, [_finding(1)])
    # Exits 3 — a healthy audit that found something — after writing the manifest to
    # the path it is handed, from inside the audit worktree.
    command = f"sh -c 'test -f a.php && cp {fixture} \"$ROUTINE_FINDINGS\"; exit 3'"
    job = _job(
        repo,
        adopt_baseline=True,
        audit=AuditSpec(skill=None, command=command, timeout_ms=60_000),
    )
    summary, failed = _cycle(job, _FakeHerdr(), history)
    assert failed is False, summary
    rec, extra = _last(history)
    assert rec.state == "done"
    assert extra["gate"] == "baseline"
    assert extra["findings_total"] == 1
    assert not (repo / ".worktrees" / f"audit-{RUN_ID}").exists()

    # 127 (binary missing) is the only exit code that fails the audit.
    missing = _job(
        repo,
        audit=AuditSpec(
            skill=None, command="no-such-audit-binary-058", timeout_ms=60_000
        ),
    )
    summary, failed = _cycle(missing, _FakeHerdr(), history)
    assert failed is True
    rec, extra = _last(history)
    assert extra["reason"] == "audit_command_failed"
    assert extra["exit_code"] == 127


# -- AC 2 ---------------------------------------------------------------------------


def test_audit_skill_dispatch_uses_its_own_worktree(repo: Path, tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    audit_wt = repo / ".worktrees" / f"audit-{RUN_ID}"
    fix_wt = repo / ".worktrees" / f"audit-fix-{RUN_ID}"
    audit_wt_at_fix: list[bool] = []

    def on_prompt(agent: _Agent) -> str:
        if _is_audit_agent(agent.name):
            _write_manifest([_finding(1)])
        else:
            audit_wt_at_fix.append(audit_wt.exists())
        return "idle"

    client = _FakeHerdr(on_prompt)
    summary, failed = _cycle(_job(repo), client, history)
    assert failed is False, summary

    audit, fix = client.agents
    assert audit.name == build_audit_agent_name("audit-type-health", RUN_ID)
    assert audit.cwd == audit_wt and audit.cwd_existed_at_prompt
    assert (audit.cwd / "a.php").exists() is False  # removed afterwards
    assert audit.prompt is not None
    assert "`type-health`" in audit.prompt
    assert str(tick.default_reports_dir() / f"{RUN_ID}-audit.md") in audit.prompt
    assert str(_manifest_path()) in audit.prompt
    assert "Do NOT edit source files, commit, push" in audit.prompt

    # The audit worktree is gone before the fix phase starts, and both are gone after.
    assert fix.cwd == fix_wt and fix.cwd_existed_at_prompt
    assert audit_wt_at_fix == [False]
    assert not audit_wt.exists() and not fix_wt.exists()


# -- AC 3 ---------------------------------------------------------------------------


def test_audit_dispatches_one_fix_worker_and_budgets_it(
    repo: Path, tmp_path: Path
) -> None:
    history = tmp_path / "history.jsonl"
    findings = [_finding(i) for i in range(10)] + [
        _finding(10, "low"),
        _finding(11, "low"),
    ]
    client = _FakeHerdr(_auditor(findings))
    summary, failed = _cycle(_job(repo), client, history)
    assert failed is False, summary

    fixers = [a for a in client.agents if not _is_audit_agent(a.name)]
    assert [a.name for a in fixers] == [
        build_gate_worker_agent_name("audit-type-health", RUN_ID)
    ]
    prompt = fixers[0].prompt
    assert prompt is not None
    assert f"`{BRANCH}`" in prompt
    # Capped at 10, most severe first: the two low findings wait for the next cycle.
    assert "finding 9 " in prompt and "finding 10 " not in prompt

    _rec, extra = _last(history)
    assert extra["gate"] == "fix_dispatched"
    assert extra["gate_branch"] == BRANCH
    assert extra["dispatched"] == 10

    # Systemd budget: a skill audit waits on two agent starts (audit + fix), then
    # slop + audit.timeout_ms + timeout_ms. Command audits wait on one start.
    unit = tmp_path / "x.service"
    skill_job = _job(
        repo,
        start_timeout_ms=10_000,
        timeout_ms=100_000,
        audit=AuditSpec(skill="type-health", command=None, timeout_ms=50_000),
    )
    # 2*10 + 60 + 50 + 100 = 230, + the 300 s margin = 530
    unit.write_text("[Service]\nTimeoutStartSec=530\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=(skill_job,)), unit) == []
    unit.write_text("[Service]\nTimeoutStartSec=529\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=(skill_job,)), unit) != []

    command_job = replace(
        skill_job, audit=AuditSpec(skill=None, command="true", timeout_ms=50_000)
    )
    unit.write_text("[Service]\nTimeoutStartSec=520\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=(command_job,)), unit) == []
    unit.write_text("[Service]\nTimeoutStartSec=519\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=(command_job,)), unit) != []


# -- AC 4 ---------------------------------------------------------------------------


def test_audit_fix_prompt_carries_findings_and_recheck_command(
    repo: Path, tmp_path: Path
) -> None:
    findings = [
        Finding("rule-7:Invoice::total", "implicit-any-return", "high", "a.php:3", "x")
    ]
    prompt = build_audit_fix_prompt(
        job_name="audit-type-health",
        check="type-health",
        base="main",
        branch=BRANCH,
        report_path="/r/fix.md",
        findings=findings,
        recheck="`vendor/bin/phpstan --json > $ROUTINE_FINDINGS`",
    )
    for needle in (
        "rule-7:Invoice::total",
        "high",
        "a.php:3",
        "implicit-any-return",
        "`type-health`",
        "vendor/bin/phpstan",
        BRANCH,
        "/r/fix.md",
    ):
        assert needle in prompt, needle

    # Through the cycle: a command audit's re-check is the command itself...
    history = tmp_path / "history.jsonl"
    fixture = _manifest_fixture(tmp_path, [_finding(1)])
    command = f"sh -c 'cp {fixture} \"$ROUTINE_FINDINGS\"'"
    command_job = _job(
        repo, audit=AuditSpec(skill=None, command=command, timeout_ms=60_000)
    )
    client = _FakeHerdr()
    _cycle(command_job, client, history)
    (fixer,) = client.agents
    assert fixer.prompt is not None
    # ($ROUTINE_FINDINGS inside it resolves to this run's manifest path)
    assert f'cp {fixture} "{_manifest_path()}"' in fixer.prompt
    assert "finding 1" in fixer.prompt

    # ...and a job-level fix_prompt replaces the engine prompt, placeholders resolved.
    custom = replace(command_job, fix_prompt="Fix $ROUTINE_FINDINGS on $ROUTINE_JOB")
    _manifest_path().unlink()
    ledger_path("audit-type-health").unlink()
    client = _FakeHerdr()
    _cycle(custom, client, history)
    (fixer,) = client.agents
    assert fixer.prompt == f"Fix {_manifest_path()} on audit-type-health"


# -- AC 5 ---------------------------------------------------------------------------


def test_audit_live_agent_prefix_guard(repo: Path, tmp_path: Path) -> None:
    long_name = "a" * 24
    job = _job(repo, name=long_name)
    audit_name = build_audit_agent_name(long_name, RUN_ID)
    fix_name = build_gate_worker_agent_name(long_name, RUN_ID)
    assert len(audit_name) <= 32 and len(fix_name) <= 32
    # The 32-char cap leaves the fix worker no hash at all for a 24-char job.
    assert fix_name == f"rt-{long_name}-gate"

    other_run = "aaaaaaaaaaaaaaaaaaaaaaaa-20261005T040000Z"
    for live in (
        build_audit_agent_name(long_name, other_run),
        build_gate_worker_agent_name(long_name, other_run),
    ):
        history = tmp_path / f"history-{live}.jsonl"
        append(history, HistoryRecord(ts=NOW, job=long_name, state="registered"))
        client = _FakeHerdr(statuses={live: "working"})
        summary, _failed = tick._process_audit_job(
            job,
            history,
            client=client,  # type: ignore[arg-type]
            now=NOW,
        )
        assert summary.endswith("skipped (agent already live)"), (live, summary)
        assert (read_job(history, long_name)[-1].extra or {})["reason"] == (
            "agent_name_live"
        )
        assert client.agents == []

    # A settled agent of a past run, or another job's live agent, does not block.
    assert not tick._live_audit_agent_exists(
        _FakeHerdr(statuses={audit_name: "idle", "rt-other-gate-1234": "working"}),  # type: ignore[arg-type]
        job,
    )


# -- AC 6 ---------------------------------------------------------------------------


def test_audit_does_not_change_existing_job_dispatch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_audit(*_a: Any, **_k: Any) -> tuple[str, bool]:
        raise AssertionError("a non-audit job reached the audit path")

    monkeypatch.setattr(tick, "_process_audit_job", _no_audit)
    routed: list[str] = []

    def _gated(job: Job, *_a: Any, **_k: Any) -> tuple[str, bool]:
        routed.append(job.name)
        return "gated", False

    monkeypatch.setattr(tick, "_process_gated_job", _gated)
    history = tmp_path / "history.jsonl"
    gated = _job(
        repo,
        name="hygiene",
        kind="gated",
        audit=None,
        target="base",
        checks=(GateCheck(kind="command", command="true", timeout_ms=60_000),),
    )
    tick._process_job(gated, history, client=_FakeHerdr(), now=NOW)  # type: ignore[arg-type]
    assert routed == ["hygiene"]

    # The systemd arms of the existing kinds are untouched: a base-target gated job is
    # still start + slop + checks + timeout (30 + 60 + 60 + 100 = 250, + 300 margin).
    unit = tmp_path / "x.service"
    budgeted = replace(gated, start_timeout_ms=30_000, timeout_ms=100_000)
    unit.write_text("[Service]\nTimeoutStartSec=550\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=(budgeted,)), unit) == []
    unit.write_text("[Service]\nTimeoutStartSec=549\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=(budgeted,)), unit) != []


# -- AC 7 ---------------------------------------------------------------------------


def test_failed_fix_dispatch_still_consumes_budget(repo: Path, tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    client = _FakeHerdr(
        _auditor([_finding(1)]), fail_start=lambda name: "-gate" in name
    )
    summary, failed = _cycle(_job(repo), client, history)
    assert failed is True
    assert "agent_start_failed" in summary

    rec, extra = _last(history)
    assert rec.state == "failed"
    assert extra["gate"] == "fix_dispatched"
    assert extra["reason"] == "agent_start_failed"
    (fid,) = extra["dispatched_ids"]

    # The ledger was written before the dispatch, so the failed start still spent an
    # attempt — and the finding stays queued for the next cycle.
    ledger = load_ledger(ledger_path("audit-type-health"))
    assert ledger is not None
    assert ledger.entries[fid].attempts == 1
    assert ledger.entries[fid].last_dispatched_run == RUN_ID
    assert ledger.entries[fid].queued
    assert not (repo / ".worktrees" / f"audit-fix-{RUN_ID}").exists()


def test_inline_audit_prompt_keeps_the_engine_contract(
    repo: Path, tmp_path: Path
) -> None:
    """A job `prompt` describes the audit inline (for a repo without the skill); the
    engine still owns the output contract, and the fix worker re-runs the audit from
    the saved instructions rather than from a skill that does not exist."""
    history = tmp_path / "history.jsonl"
    job = _job(repo, prompt="Scan src/ for bare `Any` (job $ROUTINE_JOB).")
    client = _FakeHerdr(_auditor([_finding(1)]))
    summary, failed = _cycle(job, client, history)
    assert failed is False, summary

    audit, fix = client.agents
    assert audit.prompt is not None and fix.prompt is not None
    assert "Run the `type-health` audit described below" in audit.prompt
    assert "Scan src/ for bare `Any` (job audit-type-health)." in audit.prompt
    assert str(_manifest_path()) in audit.prompt
    assert "Do NOT edit source files, commit, push" in audit.prompt

    saved = tick.default_reports_dir() / f"{RUN_ID}-audit-prompt.md"
    assert saved.read_text() == audit.prompt
    assert str(saved) in fix.prompt
    assert "skill and read its findings" not in fix.prompt
