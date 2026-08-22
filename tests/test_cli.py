"""Tests for the CLI's own validation logic (not the full argparse plumbing — see
docs/plan-v1.md §7). Covers _check_systemd_timeout (the TimeoutStartSec trap described in
docs/plan-v1.md §3), _cmd_validate's repo/.git check for workspace:worktree jobs, and
_cmd_tick's exit code (must go non-zero when a job's run genuinely fails, so systemd marks the
unit "failed" rather than always reporting success).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from herdr_routines import cli
from herdr_routines.cli import _check_systemd_timeout, _cmd_validate, default_log_path
from herdr_routines.config import Job, RoutinesConfig
from herdr_routines.tick import TickOutcome


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
