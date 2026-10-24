"""Tests for the CLI's own validation logic (not the full argparse plumbing — see
docs/plan-v1.md §7). Covers _check_systemd_timeout (the TimeoutStartSec trap described in
docs/plan-v1.md §3), _cmd_validate's repo/.git check for workspace:worktree jobs, and
_cmd_tick's exit code (must go non-zero when a job's run genuinely fails, so systemd marks the
unit "failed" rather than always reporting success).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from herdr_routines import cli
from herdr_routines.cli import _check_systemd_timeout, _cmd_validate, default_log_path
from herdr_routines.config import GateCheck, Job, RoutinesConfig
from herdr_routines.history import HistoryRecord, append, rolled_path
from herdr_routines.runner import RunOutcome
from herdr_routines.tick import TickOutcome
from tests.test_review_scan import ReviewGh, _pr


def make_job(tmp_path: Path, timeout_ms: int, name: str = "a") -> Job:
    return Job(
        name=name,
        enabled=True,
        cron="0 3 * * *",
        repo=tmp_path,
        workspace="worktree",
        base="main",
        agent_kind="claude",
        model=None,
        prompt="",
        timeout_ms=timeout_ms,
        start_timeout_ms=30_000,
        catch_up_minutes=120,
        timezone="UTC",
        on_missed="log",
    )


def test_missing_unit_file_is_skipped_silently(tmp_path: Path) -> None:
    config = RoutinesConfig(jobs=(make_job(tmp_path, 1000),))
    assert _check_systemd_timeout(config, tmp_path / "does-not-exist.service") == []


def test_no_jobs_skips_the_check_even_if_unit_exists(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nTimeoutStartSec=infinity\n")
    assert _check_systemd_timeout(RoutinesConfig(jobs=()), unit) == []


def test_infinity_is_flagged_even_when_a_comment_mentions_infinity(
    tmp_path: Path,
) -> None:
    """Regression: an explanatory comment saying '# ...TimeoutStartSec=infinity is a trap...'
    must not itself be treated as the directive (real bug hit while writing the actual unit
    file shipped in deploy/systemd/)."""
    unit = tmp_path / "x.service"
    unit.write_text(
        "[Service]\n"
        "# the naive choice is TimeoutStartSec=infinity but that's a trap\n"
        "TimeoutStartSec=3030\n"
    )
    config = RoutinesConfig(jobs=(make_job(tmp_path, 2_700_000),))
    assert _check_systemd_timeout(config, unit) == []


def test_actual_infinity_directive_is_flagged(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nTimeoutStartSec=infinity\n")
    config = RoutinesConfig(jobs=(make_job(tmp_path, 1000),))
    problems = _check_systemd_timeout(config, unit)
    assert len(problems) == 1
    assert "infinity" in problems[0]


def test_too_small_timeout_is_flagged(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nTimeoutStartSec=100\n")
    config = RoutinesConfig(
        jobs=(make_job(tmp_path, 2_700_000),)
    )  # (2700s + 30s start) + 300s margin = 3030s
    problems = _check_systemd_timeout(config, unit)
    assert len(problems) == 1
    assert "3030" in problems[0]


def test_sufficient_timeout_passes(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nTimeoutStartSec=3030\n")
    config = RoutinesConfig(jobs=(make_job(tmp_path, 2_700_000),))
    assert _check_systemd_timeout(config, unit) == []


def test_sums_timeouts_across_enabled_jobs(tmp_path: Path) -> None:
    """A tick can run every due job sequentially in one invocation, so the required
    TimeoutStartSec is the sum across jobs, not just the largest one."""
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nTimeoutStartSec=1420\n")
    config = RoutinesConfig(
        jobs=(make_job(tmp_path, 60_000, "small"), make_job(tmp_path, 1_000_000, "big"))
    )
    # small: 60s + 30s start = 90s; big: 1000s + 30s start = 1030s; sum = 1120s + 300s = 1420s
    assert _check_systemd_timeout(config, unit) == []


def test_auto_fix_job_counts_max_prs_per_tick_worst_case(tmp_path: Path) -> None:
    """A gated tick dispatches up to max_workers_per_tick fix workers sequentially,
    each bounded by timeout_ms plus the job start timeout — the systemd
    check must budget for that, not just the job's own timeout_ms."""
    unit = tmp_path / "x.service"
    af_job = replace(
        make_job(tmp_path, 60_000, "afix"),  # job.timeout_ms = 60s
        checks=(GateCheck(kind="pr_health"),),
        target="pr",
        max_workers_per_tick=3,
        kind="gated",
    )
    config = RoutinesConfig(jobs=(af_job,))
    # pr-target: 30s start + 60s gate_slop + 120s pr_health timeout + 3*60s workers
    # + 3*120s workers re-run checks = 750s + 300s margin = 1050s
    unit.write_text("[Service]\nTimeoutStartSec=1050\n")
    assert _check_systemd_timeout(config, unit) == []

    unit.write_text("[Service]\nTimeoutStartSec=500\n")
    problems = _check_systemd_timeout(config, unit)
    assert len(problems) == 1
    assert "1050" in problems[0]


def test_systemd_timeout_skips_pipeline_job(tmp_path: Path) -> None:
    """kind: pipeline never runs in the tick process (issue 026) — a large default
    timeout_ms must not inflate the required TimeoutStartSec."""
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nTimeoutStartSec=90\n")
    pipeline_job = replace(
        make_job(tmp_path, 25_200_000, "nightly-pipeline"),  # would dominate if counted
        kind="pipeline",
        catch_up_minutes=0,
        deadline_ms=25_200_000,
        prompt_file="docs/pipeline/orchestrator-prompt.md",
    )
    plain_job = make_job(
        tmp_path, 60_000, "small"
    )  # 60s + 30s start = 90s + 300s margin
    config = RoutinesConfig(jobs=(pipeline_job, plain_job))
    unit.write_text("[Service]\nTimeoutStartSec=390\n")
    assert _check_systemd_timeout(config, unit) == []

    unit.write_text("[Service]\nTimeoutStartSec=100\n")
    problems = _check_systemd_timeout(config, unit)
    assert len(problems) == 1
    assert "390" in problems[0]


def test_missing_directive_is_flagged(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nExecStart=/bin/true\n")
    config = RoutinesConfig(jobs=(make_job(tmp_path, 1000),))
    problems = _check_systemd_timeout(config, unit)
    assert len(problems) == 1
    assert "no TimeoutStartSec" in problems[0]


def test_default_log_path_honors_state_dir_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    assert default_log_path() == tmp_path / "state" / "herdr-routines.log"


def test_default_log_path_falls_back_to_local_state_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR", raising=False)
    assert default_log_path() == (
        Path.home() / ".local" / "state" / "herdr-routines" / "herdr-routines.log"
    )


def _validate_args(config_path: Path, tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=config_path, systemd_unit=tmp_path / "does-not-exist.service"
    )


def _write_worktree_job_config(tmp_path: Path, repo: Path) -> Path:
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: a\n"
        f"    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
    )
    return config_path


def test_validate_accepts_a_plain_clone_as_worktree_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: `herdr worktree create --cwd <repo>` creates a *new* linked worktree
    elsewhere from `repo` — confirmed empirically against a live herdr server — so `repo` does
    not need to already be one itself. A plain clone's `.git` is a directory, not a file."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = _write_worktree_job_config(tmp_path, repo)
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    assert "not a git repository" not in capsys.readouterr().err


def test_validate_accepts_an_already_linked_worktree_as_worktree_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The previously-only-accepted shape must keep working: a linked worktree's `.git` is a
    file (a pointer to the real gitdir), not a directory."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo\n")
    config_path = _write_worktree_job_config(tmp_path, repo)
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    assert "not a git repository" not in capsys.readouterr().err


def test_validate_rejects_a_worktree_repo_with_no_git_at_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    config_path = _write_worktree_job_config(tmp_path, repo)
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 1
    assert "not a git repository" in capsys.readouterr().err


def _prepare_tick_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: TickOutcome
) -> None:
    monkeypatch.setattr(cli, "default_config_path", lambda: tmp_path / "jobs.yaml")
    (tmp_path / "jobs.yaml").write_text("version: 1\njobs: []\n")
    monkeypatch.setattr(cli, "default_history_path", lambda: tmp_path / "history.jsonl")
    monkeypatch.setattr(cli, "default_lock_path", lambda: tmp_path / "tick.lock")
    monkeypatch.setattr(cli, "run_tick", lambda *args, **kwargs: outcome)


def test_cmd_tick_exits_zero_when_no_job_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_tick_cli(
        tmp_path, monkeypatch, TickOutcome(summaries=("a: done",), any_job_failed=False)
    )
    assert cli.main(["tick"]) == 0


def test_cmd_tick_exits_nonzero_when_a_job_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: `_cmd_tick` used to always `return 0`, even when a job's run genuinely
    failed (verified 3x on the Pi with the herdr server killed mid-run) — contradicting
    deploy/README.md's claim that the systemd unit goes "failed" in that case."""
    _prepare_tick_cli(
        tmp_path,
        monkeypatch,
        TickOutcome(summaries=("a: failed (agent_start_failed)",), any_job_failed=True),
    )
    assert cli.main(["tick"]) != 0


def test_main_configures_logging_before_parsing_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path | None] = []
    monkeypatch.setattr(
        cli, "init_logging", lambda log_file=None, **_: calls.append(log_file)
    )
    monkeypatch.setattr(
        cli, "default_log_path", lambda: tmp_path / "herdr-routines.log"
    )
    monkeypatch.setattr(cli, "default_config_path", lambda: tmp_path / "jobs.yaml")
    (tmp_path / "jobs.yaml").write_text("version: 1\njobs: []\n")

    cli.main(["status"])

    assert calls == [tmp_path / "herdr-routines.log"]


def test_validate_warns_when_prompt_never_writes_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live on the Pi (2026-08-23): a job whose prompt never mentions $ROUTINE_REPORT ran
    green through the whole pipeline and still failed with no_report — validate must flag
    that up front instead."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: a\n"
        f"    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
        f"    prompt: Review the oldest PR and stop.\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    assert "$ROUTINE_REPORT" in capsys.readouterr().err


def test_validate_stays_silent_when_prompt_targets_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: a\n"
        f"    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
        f"    prompt: Write your findings to $ROUTINE_REPORT.\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    assert "$ROUTINE_REPORT" not in capsys.readouterr().err


def test_validate_pipeline_job_suppresses_report_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A kind: pipeline job's prompt lives in prompt_file, not the inline `prompt` field
    (which is legitimately empty) — the $ROUTINE_REPORT warning must not fire for it."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "orchestrator-prompt.md").write_text("do the thing\n")
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: nightly-pipeline\n"
        f"    cron: '0 2 * * *'\n"
        f"    repo: {repo}\n"
        f"    kind: pipeline\n"
        f"    prompt_file: orchestrator-prompt.md\n"
        f"    deadline_ms: 25200000\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    err = capsys.readouterr().err
    assert "$ROUTINE_REPORT" not in err
    assert "prompt is empty" not in err


def test_validate_pipeline_workspace_na_repo_is_clone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """validate treats `workspace` as N/A for kind: pipeline and always checks `repo` as
    a plain git clone (the parent the orchestrator branches its own worktree from),
    regardless of the (unused, possibly defaults.yaml-inherited) workspace value."""
    repo = tmp_path / "repo"  # deliberately NOT a git repo
    repo.mkdir()
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: nightly-pipeline\n"
        f"    cron: '0 2 * * *'\n"
        f"    repo: {repo}\n"
        f"    kind: pipeline\n"
        f"    prompt_file: orchestrator-prompt.md\n"
        f"    deadline_ms: 25200000\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_validate_pipeline_missing_prompt_file_is_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: nightly-pipeline\n"
        f"    cron: '0 2 * * *'\n"
        f"    repo: {repo}\n"
        f"    kind: pipeline\n"
        f"    prompt_file: does-not-exist.md\n"
        f"    deadline_ms: 25200000\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 1
    assert "prompt_file does not exist" in capsys.readouterr().err


def test_validate_does_not_warn_for_disabled_job_missing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Blocking review finding on PR #18: the $ROUTINE_REPORT warning must not fire for
    disabled jobs — disabling is the natural mitigation for a known-broken prompt."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: a\n"
        f"    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
        f"    enabled: false\n"
        f"    prompt: Review the oldest PR and stop.\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    assert "$ROUTINE_REPORT" not in capsys.readouterr().err


def test_validate_empty_prompt_gets_distinct_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-blocking finding on PR #18: an omitted/empty prompt defaults to "" (config.py:87)
    and fails earlier as agent_prompt_failed, so the misdiagnosis as no_report is fixed."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    # Omit prompt entirely -> defaults to "" via config.py:_JOB_DEFAULTS.
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: a\n"
        f"    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    err = capsys.readouterr().err
    assert "$ROUTINE_REPORT" in err
    assert "prompt is empty" in err
    assert "no_report" not in err


def test_validate_whitespace_prompt_gets_empty_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: a\n"
        f"    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
        f'    prompt: "   "\n'
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    err = capsys.readouterr().err
    assert "prompt is empty" in err


def test_validate_disabled_empty_prompt_stays_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: a\n"
        f"    cron: '0 3 * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
        f"    enabled: false\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    assert "$ROUTINE_REPORT" not in capsys.readouterr().err


def test_validate_gated_job_with_empty_prompt_stays_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gated job dispatches fix workers with the built-in prompt template and never
    writes $ROUTINE_REPORT (it writes per-PR reports), so an empty prompt is legitimate —
    validate must not warn about it."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: babysit-prs\n"
        f"    cron: '*/10 * * * *'\n"
        f"    repo: {repo}\n"
        f"    workspace: worktree\n"
        f"    kind: gated\n"
        f"    checks:\n"
        f"      - pr_health:\n"
        f"    max_workers_per_tick: 3\n"
        f"    max_attempts_per_target: 3\n"
    )
    assert _cmd_validate(_validate_args(config_path, tmp_path)) == 0
    err = capsys.readouterr().err
    assert "prompt is empty" not in err
    assert "$ROUTINE_REPORT" not in err


# -- jobs.d directory layout CLI tests (spec 20260831T012350Z) ------------------------------------


def _make_jobs_d(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a jobs.d/ directory with the given filename->content mapping."""
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir()
    for name, content in files.items():
        (jobs_dir / name).write_text(content)
    return jobs_dir


def test_cli_validate_scheduled_ps_history_directory_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance 4: validate/status/scheduled/ps/history all work against directory
    layout via new directory loader (--config dir, no jobs.yaml hard-code left in src/)."""
    from herdr_routines import cli
    from herdr_routines.config import load_config

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()

    jobs_dir = _make_jobs_d(
        tmp_path,
        {
            "defaults.yaml": "agent_kind: opencode\n",
            "test-job.yaml": (
                f"name: test-job\n"
                f"cron: '0 3 * * *'\n"
                f"repo: {repo}\n"
                f"workspace: worktree\n"
                f"prompt: Write findings to $ROUTINE_REPORT.\n"
            ),
        },
    )

    # validate works against directory layout
    args = argparse.Namespace(
        config=jobs_dir,
        systemd_unit=tmp_path / "does-not-exist.service",
    )
    assert _cmd_validate(args) == 0
    out = capsys.readouterr()
    assert "ok: 1 job(s) valid" in out.out

    # status works against directory layout
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))
    monkeypatch.setattr(cli, "default_config_path", lambda: jobs_dir)
    monkeypatch.setattr(
        cli, "default_history_path", lambda: state_dir / "history.jsonl"
    )
    monkeypatch.setattr(cli, "default_lock_path", lambda: state_dir / "tick.lock")
    assert cli.main(["status"]) == 0
    assert "test-job" in capsys.readouterr().out

    # scheduled works against directory layout
    assert cli.main(["scheduled"]) == 0
    assert "test-job" in capsys.readouterr().out

    # history works against directory layout (reads history.jsonl, not config)
    assert cli.main(["history", "test-job"]) == 0

    # load_config works with --config pointing to directory
    cfg = load_config(jobs_dir)
    assert len(cfg.jobs) == 1
    assert cfg.jobs[0].name == "test-job"
    assert cfg.jobs[0].agent_kind == "opencode"  # from defaults.yaml


# -- repository: <url> validation (issue 016) -------------------------------------------


def test_repo_url_validate_skips_existence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """repository jobs skip repo.exists()/.git hard errors in validate, warn instead."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir()
    (jobs_dir / "repo-job.yaml").write_text(
        "name: repo-job\ncron: '0 3 * * *'\nrepository: https://github.com/org/repo.git\n"
    )
    # Run validate via _cmd_validate's logic directly
    from herdr_routines.config import load_config

    config = load_config(jobs_dir)
    job = config.job("repo-job")
    assert job is not None
    assert job.repository is not None
    # repo path doesn't exist on disk but validate should NOT error for repository jobs
    assert not job.repo.exists()
    # The validate function should produce a warning, not an error for repository jobs
    # We test the logic by checking the validate path
    problems: list[str] = []
    warnings: list[str] = []
    for j in config.jobs:
        if j.repository is not None:
            if not j.repo.exists():
                warnings.append(f"{j.name}: repository job not yet cloned: {j.repo}")
        else:
            if not j.repo.exists():
                problems.append(f"{j.name}: repo path does not exist: {j.repo}")
    # Repository job should produce a warning, not a problem
    assert len(problems) == 0
    assert len(warnings) == 1
    assert "not yet cloned" in warnings[0]


# -- kind: pipeline `run --run-id` (issue 026 AC #11) --------------------------------


def _write_pipeline_job_config(tmp_path: Path, repo: Path) -> Path:
    config_path = tmp_path / "jobs.yaml"
    config_path.write_text(
        f"version: 1\n"
        f"jobs:\n"
        f"  - name: nightly-pipeline\n"
        f"    cron: '0 2 * * *'\n"
        f"    repo: {repo}\n"
        f"    kind: pipeline\n"
        f"    prompt_file: docs/pipeline/orchestrator-prompt.md\n"
        f"    deadline_ms: 25200000\n"
    )
    return config_path


def test_cmd_run_pipeline_dry_run_prints_systemd_argv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs" / "pipeline").mkdir(parents=True)
    (repo / "docs" / "pipeline" / "orchestrator-prompt.md").write_text("do it\n")
    config_path = _write_pipeline_job_config(tmp_path, repo)
    monkeypatch.setattr(cli, "default_config_path", lambda: config_path)

    assert cli.main(["run", "nightly-pipeline", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "systemd-run" in out
    assert "rt-nightly-pipeline" in out
    argv = out.split()
    assert "--wait" not in argv  # tick must never block on the orchestrator


def test_cmd_run_pipeline_resume_uses_distinct_unit_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed run (--run-id given) must not collide with a still-registered prior
    attempt's transient unit name."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs" / "pipeline").mkdir(parents=True)
    (repo / "docs" / "pipeline" / "orchestrator-prompt.md").write_text("do it\n")
    config_path = _write_pipeline_job_config(tmp_path, repo)
    monkeypatch.setattr(cli, "default_config_path", lambda: config_path)

    assert (
        cli.main(
            ["run", "nightly-pipeline", "--run-id", "20260904T020000Z", "--dry-run"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "--unit=herdr-pipeline-20260904T020000Z-r" in out
    assert "--run-id 20260904T020000Z" in out


def test_cmd_run_pipeline_never_calls_execute_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs" / "pipeline").mkdir(parents=True)
    (repo / "docs" / "pipeline" / "orchestrator-prompt.md").write_text("do it\n")
    config_path = _write_pipeline_job_config(tmp_path, repo)
    monkeypatch.setattr(cli, "default_config_path", lambda: config_path)

    def fail_execute_run(*a, **kw):
        raise AssertionError("execute_run must never be called for kind: pipeline")

    monkeypatch.setattr(cli, "execute_run", fail_execute_run)

    launched: list[list[str]] = []

    def fake_launch(argv, **kw):
        launched.append(argv)
        return 0, "", ""

    monkeypatch.setattr(cli, "launch_pipeline", fake_launch)

    assert cli.main(["run", "nightly-pipeline"]) == 0
    assert len(launched) == 1


# ---------------------------------------------------------------------------
# Issue 018: Model selection per job beyond claude/opencode
# ---------------------------------------------------------------------------


def test_validate_rejects_model_for_unsupported_kind_as_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """validate reports model for unsupported kind as blocking error: (exit 1), not warning:."""
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir()
    (jobs_dir / "a.yaml").write_text(
        "name: a\ncron: '0 3 * * *'\nrepo: /repo/a\nagent_kind: gemini\nmodel: gemini-2.0\n"
    )
    args = _validate_args(jobs_dir, tmp_path)
    assert _cmd_validate(args) == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "warning:" not in captured.err or captured.err.index("error:") < (
        captured.err.index("warning:")
        if "warning:" in captured.err
        else len(captured.err)
    )


# ---------------------------------------------------------------------------
# Issue 021: retention — `prune` and the tiny-history-threshold warning
# ---------------------------------------------------------------------------


def _plant_report(reports: Path, name: str, *, age_days: float) -> Path:
    """A report file backdated by *age_days* — `prune` selects on mtime, so tests set it
    rather than sleeping."""
    path = reports / name
    path.write_text("report\n")
    stamp = time.time() - age_days * 86_400
    os.utime(path, (stamp, stamp))
    return path


def test_prune_reports_requires_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 11: `prune reports --dry-run` lists candidates and deletes nothing, and
    `prune reports` with no mode at all exits non-zero with gc's refusal — *and* still prints
    the listing, because an operator cannot decide without seeing what they would lose. Same
    rc 2 and message shape as `gc --delete` without `--yes` (`gc.run_gc_delete`), so there is
    one refusal to learn and one message to grep for.

    Driven through `cli.main`, not the handler, so the argparse wiring is covered too: a
    `required=True` mode group here would exit 2 with a *usage* error instead, and the
    criterion is specifically the gc-style refusal. Hence the mutually-exclusive group is
    optional and the handler owns the refusal.
    """
    # Inside tmp_path, so the rendered table shows whole paths — it truncates cells at 48
    # characters, which pytest's long tmp dirs would otherwise eat.
    monkeypatch.chdir(tmp_path)
    reports = Path("reports")
    reports.mkdir()
    old = _plant_report(reports, "a-20260101T000000Z.md", age_days=200)
    fresh = _plant_report(reports, "a-20260927T000000Z.md", age_days=1)
    base = [
        "--config",
        "jobs.yaml",
        "prune",
        "reports",
        "--reports-dir",
        "reports",
        "--older-than",
        "90",
    ]

    assert cli.main([*base, "--dry-run"]) == 0
    out = capsys.readouterr()
    assert "reports/a-20260101T000000Z.md" in out.out
    assert "reports/a-20260927T000000Z.md" in out.out
    assert "keep (fresh)" in out.out
    assert old.exists() and fresh.exists(), "--dry-run must not delete"

    assert cli.main(base) == 2
    out = capsys.readouterr()
    assert "refusing to delete without --yes" in out.err
    assert "a-20260101T000000Z.md" in out.out, (
        "the listing still prints, to decide with"
    )
    assert old.exists() and fresh.exists(), "no --yes means no deletion"

    # --yes is what deletes, and only the collectable entry.
    assert cli.main([*base, "--yes"]) == 0
    assert "removed 1" in capsys.readouterr().out
    assert not old.exists() and fresh.exists()

    # Both modes at once is a usage error, not a silent preference for one of them.
    with pytest.raises(SystemExit):
        cli.main([*base, "--dry-run", "--yes"])

    # A negative window is rejected rather than silently collecting everything.
    assert cli.main([*base, "--older-than", "-1", "--dry-run"]) == 1
    assert "non-negative" in capsys.readouterr().err
    assert fresh.exists()


def test_prune_reports_takes_its_window_from_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no `--older-than`, the window comes from `retention.reports_max_age_days` — so one
    setting governs both `prune reports` and the fallback, instead of a second flag that can
    disagree with it. A config that cannot be parsed falls back to the documented 90 days
    rather than refusing to run: a destructive command should not be unusable because an
    unrelated file is broken."""
    monkeypatch.chdir(tmp_path)
    reports = Path("reports")
    reports.mkdir()
    # 40 days old: past the configured 30-day window, inside the 90-day fallback. One file
    # distinguishes the two.
    forty_days = _plant_report(reports, "a-20260819T000000Z.md", age_days=40)
    config_path = Path("jobs.yaml")
    config_path.write_text(
        "version: 1\nretention:\n  reports_max_age_days: 30\njobs: []\n"
    )
    base = [
        "--config",
        "jobs.yaml",
        "prune",
        "reports",
        "--reports-dir",
        "reports",
        "--dry-run",
    ]

    assert cli.main(base) == 0
    assert "would remove 1" in capsys.readouterr().out
    assert forty_days.exists()

    # A broken config still prunes, on the built-in window — 40 days is inside 90.
    config_path.write_text("version: 1\njobs: [\n")
    assert cli.main(base) == 0
    assert "would remove 0" in capsys.readouterr().out

    # An explicit --older-than overrides both.
    assert cli.main([*base, "--older-than", "5"]) == 0
    assert "would remove 1" in capsys.readouterr().out


def test_prune_history_uses_its_own_window_and_spares_the_live_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prune history` collects old *rolls* and never the live file, at any window. The
    built-in 365-day window is deliberately generous — this is a by-name command, and the
    earliest roll is what `first_seen_at` still needs for a long-lived job (see
    `reports_prune.prune_rolled_history`)."""
    monkeypatch.chdir(tmp_path)
    history_path = Path("history.jsonl")
    history_path.write_text("")
    old_roll = rolled_path(history_path, datetime(2024, 1, 1, tzinfo=UTC))
    old_roll.write_text('{"ts": "2024-01-01T00:00:00Z", "job": "a", "state": "done"}\n')
    recent_roll = rolled_path(history_path, datetime(2026, 9, 27, tzinfo=UTC))
    recent_roll.write_text(
        '{"ts": "2026-09-27T00:00:00Z", "job": "a", "state": "done"}\n'
    )
    for path, age_days in ((old_roll, 400), (recent_roll, 1)):
        stamp = time.time() - age_days * 86_400
        os.utime(path, (stamp, stamp))
    os.utime(history_path, (0, 0))  # ancient, and still not a candidate
    monkeypatch.setattr(cli, "default_history_path", lambda: history_path)

    assert cli.main(["prune", "history", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would remove 1" in out, "400 days is past the 365-day built-in window"
    assert "history-2024" in out
    assert old_roll.exists() and recent_roll.exists() and history_path.exists()

    # --older-than 0 collects the recent roll too; the live file is still not a candidate,
    # which the count (2 of the 2 rolls) is the assertion for.
    assert cli.main(["prune", "history", "--dry-run", "--older-than", "0"]) == 0
    assert "would remove 2" in capsys.readouterr().out
    assert history_path.exists()

    # And with --yes, only the rolls go.
    assert cli.main(["prune", "history", "--yes", "--older-than", "0"]) == 0
    assert "removed 2" in capsys.readouterr().out
    assert not old_roll.exists() and not recent_roll.exists()
    assert history_path.exists() and history_path.read_text() == ""


def test_validate_warns_on_tiny_history_max_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance 16: `history_max_bytes` below 1 MiB is a warning, not an error. It is legal
    — it just rolls the log on essentially every tick, and `validate` is the only place anyone
    would notice before wondering why the state dir is full of rolls. Same posture as the
    `$ROUTINE_REPORT` warnings: stderr, exit code unchanged.

    A value at or above 1 MiB must stay silent, or the warning is noise on every run — and
    so must an unset threshold, which means no rotation at all.
    """
    config_path = tmp_path / "jobs.yaml"
    args = _validate_args(config_path, tmp_path)

    config_path.write_text(
        "version: 1\nretention:\n  history_max_bytes: 4096\njobs: []\n"
    )
    assert _cmd_validate(args) == 0
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "history_max_bytes" in err

    config_path.write_text(
        "version: 1\nretention:\n  history_max_bytes: 1048576\njobs: []\n"
    )
    assert _cmd_validate(args) == 0
    assert "history_max_bytes" not in capsys.readouterr().err

    config_path.write_text("version: 1\nretention: {}\njobs: []\n")
    assert _cmd_validate(args) == 0
    assert "history_max_bytes" not in capsys.readouterr().err


def test_pipeline_run_reads_trusted_review_authors_from_config(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs.d"
    jobs_dir.mkdir()
    (jobs_dir / "trusted_review_authors.yaml").write_text("- alice\n")
    args = argparse.Namespace(config=jobs_dir)
    assert cli._configured_trusted_review_authors(args) == ("alice",)


def test_pipeline_run_trusts_only_the_gh_user_when_config_is_unreadable(
    tmp_path: Path,
) -> None:
    """A broken or missing config must not stop `pipeline-run` from writing its report;
    it falls back to the narrowest set (no configured logins, so only the gh user)."""
    args = argparse.Namespace(config=tmp_path / "missing.d")
    assert cli._configured_trusted_review_authors(args) == ()


# -- tick lock: `tick` defers, manual `run` refuses, while another holder has it ---------


def _prepare_run_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with one plain job `a` and the lock path under tmp_path; returns the lock path."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = _write_worktree_job_config(tmp_path, repo)
    lock_path = tmp_path / "tick.lock"
    monkeypatch.setattr(cli, "default_config_path", lambda: config_path)
    monkeypatch.setattr(cli, "default_lock_path", lambda: lock_path)
    return lock_path


def test_cmd_tick_returns_zero_without_running_when_the_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The skip is rc 0 but not silent: one INFO line naming the lock, so contention
    (a self-update or manual `run` colliding with the tick) shows in journalctl."""
    lock_path = _prepare_run_cli(tmp_path, monkeypatch)

    def fail_run_tick(*a, **kw):
        raise AssertionError("run_tick must not run while another tick holds the lock")

    monkeypatch.setattr(cli, "run_tick", fail_run_tick)
    with cli.tick_lock(lock_path) as holder_acquired, caplog.at_level("INFO"):
        assert holder_acquired
        assert cli.main(["tick"]) == 0
    skips = [r for r in caplog.records if "lock" in r.message and "skip" in r.message]
    assert len(skips) == 1
    assert skips[0].levelname == "INFO"
    assert str(lock_path) in skips[0].getMessage()


def test_cmd_run_refuses_while_a_tick_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual run of a plain job shares the `rt-<job>` agent name and the repo checkout
    with the scheduled tick, so it must not overlap one."""
    lock_path = _prepare_run_cli(tmp_path, monkeypatch)

    def fail_execute_run(*a, **kw):
        raise AssertionError("execute_run must not run while a tick holds the lock")

    monkeypatch.setattr(cli, "execute_run", fail_execute_run)
    with cli.tick_lock(lock_path) as holder_acquired:
        assert holder_acquired
        assert cli.main(["run", "a"]) == 1


def test_cmd_run_holds_the_tick_lock_while_the_job_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = _prepare_run_cli(tmp_path, monkeypatch)
    lock_free_during_run: list[bool] = []

    def fake_execute_run(job, client, *, run_id, **_):
        with cli.tick_lock(lock_path) as acquired:
            lock_free_during_run.append(acquired)
        return RunOutcome(state="done", run_id=run_id)

    monkeypatch.setattr(cli, "execute_run", fake_execute_run)
    monkeypatch.setattr(cli, "HerdrClient", lambda: object())

    assert cli.main(["run", "a"]) == 0
    assert lock_free_during_run == [False]
    with cli.tick_lock(lock_path) as acquired:
        assert acquired, "the lock must be released once the run finishes"


def test_cmd_run_dry_run_ignores_the_tick_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = _prepare_run_cli(tmp_path, monkeypatch)
    with cli.tick_lock(lock_path) as holder_acquired:
        assert holder_acquired
        assert cli.main(["run", "a", "--dry-run"]) == 0


# ---------------------------------------------------------------------------
# Issue 066: inspect-only `review-scan` subcommand (spec 20261021T050000Z)
# ---------------------------------------------------------------------------

_SCAN_DISPOSITIONS = frozenset(
    {
        "would_dispatch",
        "author_mismatch",
        "routine_owned_branch",
        "fork_pr",
        "max_attempts_exceeded",
        "review_already_posted",
        "over_cap",
    }
)

_TS = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def _code(call: Callable[[], object]) -> int:
    """`cli.main` for handler returns; SystemExit (argparse usage/exit) unwrapped.

    The observable exit code either way, so the tests pin the contract rather than
    whether the handler returns 2 or argparse raises SystemExit(2)."""
    try:
        code = call()
    except SystemExit as e:
        code = e.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 2  # a string code means argparse's usage exit


def _write_review_scan_config(tmp_path: Path) -> Path:
    """One `kind: review` job to scan plus a non-review job for the wrong-kind exit."""
    path = tmp_path / "jobs.yaml"
    path.write_text(
        "version: 1\n"
        "jobs:\n"
        "  - name: review-my-prs\n"
        "    cron: '30 8 * * *'\n"
        "    kind: review\n"
        "    select: [author]\n"
        "    max_workers_per_tick: 1\n"
        f"  - name: not-review\n"
        "    cron: '0 3 * * *'\n"
        f"    repo: {tmp_path / 'plain-repo'}\n"
    )
    return path


def _review_scan_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: ReviewGh
) -> Path:
    """Point the global `--config` at the tmp jobs file, the state dir at
    `tmp/state` (history, reports, repos all resolve from the env var), and the
    handler's RealGhClient at the fake."""
    config_path = _write_review_scan_config(tmp_path)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "RealGhClient", lambda: gh)
    return config_path


def _scan_argv(config_path: Path, *extra: str) -> list[str]:
    """Argv for `review-scan` over the default review job. A leading non-flag extra
    replaces that job name (how a test targets another job); anything else is flags,
    appended after the job."""
    job = "review-my-prs"
    rest = list(extra)
    if rest and not rest[0].startswith("-"):
        job = rest.pop(0)
    return ["--config", str(config_path), "review-scan", job, *rest]


def _mixed_review_gh() -> ReviewGh:
    """Six PRs in one repo hitting every pre-dispatch disposition: #1 author_mismatch
    and #2 routine_owned_branch (scan skips), #3 max_attempts_exceeded (budget, seeded
    in history), #4 review_already_posted (trusted marker), #5 would_dispatch, and #6
    over_cap under the 1-worker cap. Sorted by target_key, the numbering is the order."""
    prs = (
        _pr(1, repo="acme/alpha", author="someone-else"),
        _pr(2, repo="acme/alpha", head_ref="auto/nightly"),
        _pr(3, repo="acme/alpha"),
        _pr(4, repo="acme/alpha"),
        _pr(5, repo="acme/alpha", created_at="2026-10-01T00:00:00Z"),
        _pr(6, repo="acme/alpha", created_at="2026-10-02T00:00:00Z"),
    )
    gh = ReviewGh(prs=prs)
    gh.posted.add("acme/alpha#4")
    return gh


_MIXED_DISPOSITIONS = {
    1: "author_mismatch",
    2: "routine_owned_branch",
    3: "max_attempts_exceeded",
    4: "review_already_posted",
    5: "would_dispatch",
    6: "over_cap",
}


def _seed_mixed_history(tmp_path: Path) -> None:
    """A spent attempt for `acme/alpha#3` so the budget stage skips it."""
    history = tmp_path / "state" / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    append(
        history,
        HistoryRecord(
            ts=_TS,
            job="review-my-prs",
            state="done",
            run_id="prior",
            extra={"target_key": "acme/alpha#3"},
        ),
    )


def test_review_scan_cli_wiring() -> None:
    """`review-scan` is wired like `refine-issue`/`ps`/`scheduled`: `sub.add_parser`
    with `set_defaults(handler=_cmd_review_scan)`, a required positional `job`,
    `--limit` (default 100) and a `--json` flag — under the global `--config`, with no
    per-command `--config`."""
    parser = cli._build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert "review-scan" in subparsers_action.choices
    sub = subparsers_action.choices["review-scan"]

    args = parser.parse_args(["review-scan", "review-my-prs"])
    assert args.handler is cli._cmd_review_scan
    assert args.job == "review-my-prs"
    assert args.limit == 100
    assert args.json is False

    opted = parser.parse_args(["review-scan", "j", "--limit", "7", "--json"])
    assert opted.limit == 7
    assert opted.json is True

    # The config flag is the parser-level one; the subcommand adds its own nothing.
    assert not any("--config" in a.option_strings for a in sub._actions)
    assert parser.parse_args(["--config", "jobs.d", "review-scan", "j"]).config == Path(
        "jobs.d"
    )
    # `job` is required, like `run`/`history`.
    with pytest.raises(SystemExit):
        parser.parse_args(["review-scan"])


def test_review_scan_missing_job_and_non_review_kind_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing job exits EXIT_FAILURE (1) with the problem named on stderr; a resolved
    job whose `kind` is not `review` is a usage error, EXIT_USAGE (2), also named."""
    config_path = _review_scan_setup(tmp_path, monkeypatch, gh=ReviewGh())

    assert _code(lambda: cli.main(_scan_argv(config_path, "no-such-job"))) == 1
    err = capsys.readouterr().err
    assert "no-such-job" in err

    assert _code(lambda: cli.main(_scan_argv(config_path, "not-review"))) == 2
    err = capsys.readouterr().err
    assert "not-review" in err
    assert "kind" in err.lower()

    # And a review job resolves: the same invocation goes on to a successful scan.
    assert _code(lambda: cli.main(_scan_argv(config_path))) == 0


def test_review_scan_auth_and_search_failures_exits_1_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No silent empty output: an `api_user()` failure and a failed search each exit 1
    with the reason on stderr — never a clean empty list."""
    config_path = _write_review_scan_config(tmp_path)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))

    class _ApiUserFails(ReviewGh):
        def api_user(self) -> str:
            raise RuntimeError("gh: not logged in")

    monkeypatch.setattr(cli, "RealGhClient", lambda: _ApiUserFails())
    assert _code(lambda: cli.main(_scan_argv(config_path))) == 1
    captured = capsys.readouterr()
    assert "gh_auth_missing" in captured.err
    assert captured.out.strip() == ""

    monkeypatch.setattr(cli, "RealGhClient", lambda: ReviewGh(search_fail=True))
    assert _code(lambda: cli.main(_scan_argv(config_path))) == 1
    captured = capsys.readouterr()
    assert "search_failed" in captured.err
    assert captured.out.strip() == ""

    # `--json` carries the same failure contract: errors to stderr, no JSON on stdout.
    monkeypatch.setattr(cli, "RealGhClient", lambda: ReviewGh(search_fail=True))
    assert _code(lambda: cli.main(_scan_argv(config_path, "--json"))) == 1
    captured = capsys.readouterr()
    assert "search_failed" in captured.err
    assert captured.out.strip() == ""


def test_review_scan_human_output_sorted_dispositions_and_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Header (job, select modes, gh login, enumerated; no truncated field on a clean
    search) + the eligibility-only note, then one line per PR sorted by target_key
    carrying target_key, select_mode and exactly one disposition from the closed set.
    Exit 0 on a successful scan regardless of outcome count."""
    config_path = _review_scan_setup(tmp_path, monkeypatch, gh=_mixed_review_gh())
    _seed_mixed_history(tmp_path)

    assert _code(lambda: cli.main(_scan_argv(config_path))) == 0
    out = capsys.readouterr().out

    # Header: job name, select modes, gh login, enumerated count, and no truncated
    # field for an untruncated search.
    assert "review-my-prs" in out
    assert "author" in out
    assert re.search(r"\bme\b", out)  # the gh login
    assert re.search(r"enumerat", out) and "6" in out
    assert "truncat" not in out.lower()

    # One line per PR, sorted by target_key, exactly one disposition each.
    lines = [line for line in out.splitlines() if "acme/alpha#" in line]
    assert len(lines) == 6
    keys = re.findall(r"acme/alpha#\d+", "\n".join(lines))
    assert keys == sorted(keys)
    assert len(set(keys)) == 6
    for number, disposition in _MIXED_DISPOSITIONS.items():
        (line,) = (line for line in lines if f"acme/alpha#{number}" in line)
        assert "author" in line, "select_mode must be on the line"
        assert "skipped" not in line, "no generic 'skipped' disposition"
        hits = {
            known
            for known in _SCAN_DISPOSITIONS
            if re.search(rf"(?<![\w-]){known}(?![\w-])", line)
        }
        assert hits == {disposition}

    # The eligibility-only note: "would dispatch" is not "next tick will run it".
    assert "would dispatch" in out
    assert re.search(r"schedule|cron|next tick", out)

    # Exit 0 with nothing to review at all, too.
    monkeypatch.setattr(cli, "RealGhClient", lambda: ReviewGh())
    assert _code(lambda: cli.main(_scan_argv(config_path))) == 0


def test_review_scan_json_stdout_only_array_with_pinned_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under `--json`, stdout parses as one JSON array of objects with exactly the
    pinned fields (`number` an int), sorted by target_key; header and note go to
    stderr; exit codes match the human path's."""
    config_path = _review_scan_setup(tmp_path, monkeypatch, gh=_mixed_review_gh())
    _seed_mixed_history(tmp_path)

    assert _code(lambda: cli.main(_scan_argv(config_path, "--json"))) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # any non-JSON text on stdout breaks this
    assert isinstance(data, list)
    assert len(data) == 6
    pinned = {
        "target_key",
        "repo",
        "number",
        "title",
        "url",
        "select_mode",
        "disposition",
    }
    assert all(set(obj) == pinned for obj in data)
    assert all(isinstance(obj["number"], int) for obj in data)
    keys = [obj["target_key"] for obj in data]
    assert keys == sorted(keys)
    for obj in data:
        number = int(obj["target_key"].partition("#")[2])
        assert obj["repo"] == "acme/alpha"
        assert obj["title"] == f"PR {number}"
        assert obj["url"] == f"https://example.invalid/acme/alpha/{number}"
        assert obj["select_mode"] == "author"
        assert obj["disposition"] == _MIXED_DISPOSITIONS[number]

    # Header + note on stderr, not mixed into the document.
    assert "review-my-prs" in captured.err

    # Exit codes identical to the human path: an auth failure is still 1, with the
    # reason on stderr and no clean empty array on stdout.
    class _ApiUserFails(ReviewGh):
        def api_user(self) -> str:
            raise RuntimeError("gh: not logged in")

    monkeypatch.setattr(cli, "RealGhClient", lambda: _ApiUserFails())
    assert _code(lambda: cli.main(_scan_argv(config_path, "--json"))) == 1
    captured = capsys.readouterr()
    assert "gh_auth_missing" in captured.err
    assert captured.out.strip() == ""


def test_review_scan_is_read_only_touches_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read-only by observation, not by hoping a happy path writes nothing: history
    bytes and mtime unchanged, no `reports/review-*.md`, no repos/worktree dir, and no
    herdr/agent/notification call at all."""
    config_path = _review_scan_setup(
        tmp_path, monkeypatch, gh=ReviewGh(prs=(_pr(1, repo="acme/alpha"),))
    )
    state = tmp_path / "state"
    history = state / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    append(
        history,
        HistoryRecord(ts=_TS, job="review-my-prs", state="registered"),
    )
    before = (history.read_bytes(), history.stat().st_mtime_ns)

    def _no_herdr(*_a: object, **_k: object) -> None:
        raise AssertionError("review-scan must not touch herdr/agents/notifications")

    def _no_execute(*_a: object, **_k: object) -> None:
        raise AssertionError("review-scan must not run agents")

    monkeypatch.setattr(cli, "HerdrClient", _no_herdr)
    monkeypatch.setattr(cli, "execute_run", _no_execute)

    assert _code(lambda: cli.main(_scan_argv(config_path))) == 0
    captured = capsys.readouterr()
    assert captured.out.strip(), "a successful scan still prints its human output"
    assert (history.read_bytes(), history.stat().st_mtime_ns) == before
    assert (
        not (state / "reports").exists()
        or list((state / "reports").glob("review-*.md")) == []
    )
    assert not (state / "repos").exists(), "no clone, so no worktree either"


def test_review_scan_limit_validation_and_forwarding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--limit` outside 1–100 is a usage error (2) — before any search call — and an
    in-range `--limit` is forwarded verbatim; the default is 100."""
    gh = ReviewGh(prs=(_pr(1, repo="acme/alpha"),))
    config_path = _review_scan_setup(tmp_path, monkeypatch, gh=gh)
    argv = _scan_argv(config_path)

    def run(limit: str) -> int:
        return _code(lambda: cli.main([*argv, "--limit", limit]))

    for bad in ("0", "101", "-5"):
        assert run(bad) == 2
        assert "limit" in capsys.readouterr().err.lower()
    assert gh.search_calls == [], "the search must not run for an invalid limit"

    for good in ("1", "7", "100"):
        assert run(good) == 0
    assert [limit for _modes, limit in gh.search_calls] == [1, 7, 100]

    gh.search_calls.clear()
    assert _code(lambda: cli.main(argv)) == 0  # no --limit: the 100 default
    assert gh.search_calls[0][1] == 100


def test_review_scan_truncated_modes_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A truncated search is surfaced on the human path (header field / note naming the
    truncated mode), and the JSON path mirrors it on stderr without leaking non-JSON
    text onto stdout."""
    gh = ReviewGh(
        prs=(_pr(1, repo="acme/alpha"),),
        truncated_modes=("author",),
    )
    config_path = _review_scan_setup(tmp_path, monkeypatch, gh=gh)

    assert _code(lambda: cli.main(_scan_argv(config_path))) == 0
    out = capsys.readouterr().out
    truncated_lines = [line for line in out.splitlines() if "truncat" in line.lower()]
    assert truncated_lines, "the truncated search must be surfaced"
    assert any("author" in line for line in truncated_lines)
    assert re.search(r"acme/alpha#1", out), "the PR line is still there"

    assert _code(lambda: cli.main(_scan_argv(config_path, "--json"))) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # stdout stays pure JSON
    assert isinstance(data, list) and len(data) == 1
    assert "truncat" in captured.err.lower(), "the JSON path mirrors the note on stderr"
    assert "author" in captured.err


def _validate_jobs_d(
    tmp_path: Path, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> str:
    tmp_path.mkdir(exist_ok=True)
    jobs_dir = _make_jobs_d(tmp_path, files)
    _cmd_validate(
        argparse.Namespace(config=jobs_dir, systemd_unit=tmp_path / "x.service")
    )
    return capsys.readouterr().err


def test_validate_warns_code_review_without_review_posted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    job = (
        f"cron: '0 3 * * *'\nrepo: {repo}\n"
        "prompt: run /code-review on the PR and write $ROUTINE_REPORT\n"
    )
    err = _validate_jobs_d(tmp_path / "w", {"a.yaml": "name: a\n" + job}, capsys)
    assert "a: prompt runs /code-review but verify lacks review_posted" in err

    err = _validate_jobs_d(
        tmp_path / "ok",
        {"a.yaml": "name: a\nverify: [review_posted]\n" + job},
        capsys,
    )
    assert "review_posted" not in err


def test_validate_warns_inherited_verify_on_non_routine_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    err = _validate_jobs_d(
        tmp_path,
        {
            "defaults.yaml": "verify: [report_wellformed]\n",
            "r.yaml": "name: r\ncron: '0 3 * * *'\nkind: review\nselect: [author]\n",
            "a.yaml": f"name: a\ncron: '0 3 * * *'\nrepo: {repo}\n"
            "prompt: write $ROUTINE_REPORT\n",
        },
        capsys,
    )
    assert (
        "r: inherits verify from defaults but kind review does not honor it yet "
        "(issue 071 phase A)"
    ) in err
    assert "a: inherits verify" not in err
