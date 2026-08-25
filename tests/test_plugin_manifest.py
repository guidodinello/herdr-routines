"""Tests for the herdr-plugin.toml front-door manifest (spec.md acceptance criteria).

Criteria 1-4 pin the manifest itself: valid TOML, actions-only (no startup
hook/daemon), and the exact run/status action shapes. Criterion 5 pins the
HERDR_PLUGIN_CONFIG_DIR / HERDR_PLUGIN_STATE_DIR fallbacks the manifest relies
on, so a future regression cannot silently repoint plugin installs away from
the ~/.config and ~/.local/state defaults that shell/systemd invocations use.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

import herdr_routines
from herdr_routines.cli import default_log_path
from herdr_routines.config import default_config_path
from herdr_routines.history import default_history_path
from herdr_routines.runner import default_reports_dir
from herdr_routines.tick import default_lock_path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "herdr-plugin.toml"
RUN_SHIM = REPO_ROOT / "scripts" / "herdr-plugin-run.sh"
STATUS_SHIM = REPO_ROOT / "scripts" / "herdr-plugin-status.sh"
RUN_ACTION_COMMAND = ["sh", "scripts/herdr-plugin-run.sh"]
STATUS_ACTION_COMMAND = ["sh", "scripts/herdr-plugin-status.sh"]

# Manifest table names that would reintroduce a daemon or background hook —
# exactly what spec.md's actions-only contract forbids.
_DAEMON_TABLES = frozenset({"startup", "startup_hooks", "hooks", "daemon"})
_TABLE_RE = re.compile(r"^[ \t]*\[\[?([^\]]+)\]?\]", re.MULTILINE)


def _load_manifest() -> dict:
    return tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _actions_by_id(data: dict) -> dict[str, dict]:
    return {action["id"]: action for action in data["actions"]}


def _install_fake_cli(bin_dir: Path) -> Path:
    """A stand-in herdr-routines that echoes its argv and honors FAKE_RC."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "herdr-routines"
    fake.write_text('#!/bin/sh\necho "$*"\nexit ${FAKE_RC:-0}\n', encoding="utf-8")
    fake.chmod(0o755)
    return fake


def test_plugin_manifest_is_valid_toml() -> None:
    """Criterion 1: the manifest exists at repo root and parses as valid TOML."""
    data = _load_manifest()
    assert data["id"] == "herdr-routines"
    assert data["name"] == "herdr-routines"
    # Keep the manifest version in lock-step with pyproject.toml/__init__.py.
    assert data["version"] == herdr_routines.__version__
    # Required per herdr.dev/docs/plugins/; install fails without it.
    assert re.match(r"\d+\.\d+\.\d+", data["min_herdr_version"])


def test_plugin_manifest_has_no_startup_hook() -> None:
    """Criterion 2: actions only — no startup/daemon/background-hook field.

    The plugin system explicitly cannot own the schedule (docs/plan-v1.md §8.4);
    systemd stays the sole clock, so any startup/daemon entry here would be a
    regression against the documented plugin model.
    """
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    data = _load_manifest()
    offenders = [key for key in data if key.lower() in _DAEMON_TABLES]
    assert offenders == []
    for match in _TABLE_RE.finditer(raw):
        head = match.group(1).split(".")[0].strip().lower()
        assert head not in _DAEMON_TABLES, f"forbidden table [{match.group(1)}]"


def test_plugin_manifest_run_action_shape(tmp_path: Path) -> None:
    """Criterion 3: a `run` action invoking `herdr-routines run <job>`.

    Plugin v1 action commands are fixed argv with no parameter interpolation,
    so the job param flows through scripts/herdr-plugin-run.sh, which must exec
    `herdr-routines run <job>` (preserving the CLI exit code), resolve the CLI
    without assuming a full login PATH, and fail loudly when the job is missing
    rather than silently no-oping.
    """
    run = _actions_by_id(_load_manifest())["run"]
    assert run["command"] == RUN_ACTION_COMMAND

    source = RUN_SHIM.read_text(encoding="utf-8")
    assert re.search(r'^exec "\$bin" run "\$job"$', source, re.MULTILINE)
    # Resolution must not assume a full login PATH.
    assert "HERDR_ROUTINES_BIN" in source
    assert "command -v herdr-routines" in source

    fake_bin = tmp_path / "bin"
    _install_fake_cli(fake_bin)

    def run_shim(**env_extra: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": str(fake_bin), "HOME": str(tmp_path)} | env_extra
        return subprocess.run(
            ["/bin/sh", str(RUN_SHIM)], capture_output=True, text=True, check=False, env=env
        )

    # The job param reaches the CLI via the documented env var…
    forwarded = run_shim(HERDR_PLUGIN_RUN_JOB="nightly-audit")
    assert forwarded.returncode == 0
    assert forwarded.stdout.strip() == "run nightly-audit"

    # …or as argv[1]…
    positional = subprocess.run(
        ["/bin/sh", str(RUN_SHIM), "other-job"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(fake_bin), "HOME": str(tmp_path)},
    )
    assert positional.returncode == 0
    assert positional.stdout.strip() == "run other-job"

    # …and exit codes pass through unchanged so Herdr shows failures.
    failed = run_shim(HERDR_PLUGIN_RUN_JOB="nightly-audit", FAKE_RC="7")
    assert failed.returncode == 7

    # A missing job must fail loudly, never silently no-op (checked before
    # binary resolution, so this holds even with the fake CLI available).
    missing = run_shim()
    assert missing.returncode != 0
    assert "HERDR_PLUGIN_RUN_JOB" in missing.stderr


def test_plugin_manifest_status_action_shape(tmp_path: Path) -> None:
    """Criterion 4: a `status` action invoking `herdr-routines status` with no params.

    Like `run`, status goes through a thin wrapper that resolves the console
    script without assuming a full login PATH and preserves its exit code.
    """
    status = _actions_by_id(_load_manifest())["status"]
    assert status["command"] == STATUS_ACTION_COMMAND

    source = STATUS_SHIM.read_text(encoding="utf-8")
    assert re.search(r'^exec "\$bin" status$', source, re.MULTILINE)
    assert "HERDR_ROUTINES_BIN" in source
    assert "command -v herdr-routines" in source

    fake_bin = tmp_path / "bin"
    _install_fake_cli(fake_bin)

    # Found on PATH: forwards exactly one arg, exit code preserved.
    via_path = subprocess.run(
        ["/bin/sh", str(STATUS_SHIM)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(fake_bin), "HOME": str(tmp_path)},
    )
    assert via_path.returncode == 0
    assert via_path.stdout.strip() == "status"

    # HERDR_ROUTINES_BIN overrides even a PATH without the binary at all.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    override = subprocess.run(
        ["/bin/sh", str(STATUS_SHIM)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": str(empty_dir),
            "HOME": str(tmp_path),
            "HERDR_ROUTINES_BIN": str(fake_bin / "herdr-routines"),
        },
    )
    assert override.returncode == 0
    assert override.stdout.strip() == "status"

    # Unresolvable binary must fail loudly (127), never silently no-op.
    lost = subprocess.run(
        ["/bin/sh", str(STATUS_SHIM)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "", "HOME": str(tmp_path)},
    )
    assert lost.returncode == 127
    assert lost.stderr.strip()


def test_plugin_env_var_paths_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5: config/state resolution honors the plugin env vars, with the
    existing XDG-style defaults as fallback (spec.md verification list)."""
    config_dir = tmp_path / "plugin-config"
    state_dir = tmp_path / "plugin-state"

    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR", raising=False)
    home = Path.home()
    base = home / ".local" / "state" / "herdr-routines"
    assert default_config_path() == home / ".config" / "herdr-routines" / "jobs.yaml"
    assert default_history_path() == base / "history.jsonl"
    # tick.py/cli.py/runner.py follow the same HERDR_PLUGIN_STATE_DIR fallback.
    assert default_lock_path() == base / "tick.lock"
    assert default_log_path() == base / "herdr-routines.log"
    assert default_reports_dir() == base / "reports"

    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))
    assert default_config_path() == config_dir / "jobs.yaml"
    assert default_history_path() == state_dir / "history.jsonl"
    assert default_lock_path() == state_dir / "tick.lock"
    assert default_log_path() == state_dir / "herdr-routines.log"
    assert default_reports_dir() == state_dir / "reports"
