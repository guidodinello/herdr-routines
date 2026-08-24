"""Tests for the --version / -V flag (spec.md: herdr-routines --version / -V).

Each test runs the CLI as a real subprocess so the actual argparse behavior is
exercised: the version must be printed to stdout with exit code 0 before any
subcommand dispatch, config loading, or Herds server contact.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")


def _run_cli(
    *argv: str, cwd: Path | None = None, state_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if state_dir is not None:
        # Keep logging side-effects out of the developer's real ~/.local/state.
        env["HERDR_PLUGIN_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        [sys.executable, "-m", "herdr_routines.cli", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=env,
    )


def _installed_version() -> str:
    return importlib.metadata.version("herdr-routines")


def test_cli_version_prints_version(tmp_path: Path) -> None:
    result = _run_cli("--version", state_dir=tmp_path)
    assert result.returncode == 0
    assert _SEMVER_RE.search(result.stdout)
    assert _installed_version() in result.stdout


def test_cli_version_short_flag(tmp_path: Path) -> None:
    long_result = _run_cli("--version", state_dir=tmp_path)
    short_result = _run_cli("-V", state_dir=tmp_path)
    assert short_result.returncode == 0
    assert short_result.stdout == long_result.stdout
    assert _installed_version() in short_result.stdout


def test_cli_version_no_config_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--version must work from an empty directory with a nonexistent --config and no
    jobs.yaml anywhere — it fires at parse time, before config loading."""
    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    result = _run_cli(
        "--config",
        str(tmp_path / "does-not-exist.yaml"),
        "--version",
        cwd=tmp_path,
        state_dir=tmp_path,
    )
    assert result.returncode == 0
    assert _installed_version() in result.stdout
