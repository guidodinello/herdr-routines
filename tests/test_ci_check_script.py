"""`scripts/ci-check.sh` is the single definition of what CI runs (issue 059)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci-check.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _run_steps() -> list[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def test_ci_check_script_matches_ci_workflow(tmp_path: Path) -> None:
    assert SCRIPT.is_file() and os.access(SCRIPT, os.X_OK)

    steps = _run_steps()
    # No job re-states a CI command: each one goes through the script, so the pipeline's
    # own check (which runs the script bare) cannot drift from what CI enforces.
    for step in steps:
        assert not any(tool in step for tool in ("ruff", "mypy", "pytest")), step
    for part in ("lint", "typecheck", "test"):
        assert f"scripts/ci-check.sh {part}" in steps

    # The bare script runs exactly CI's four commands, in order. A fake `uv` logs argv.
    log = tmp_path / "argv.log"
    fake_uv = tmp_path / "bin" / "uv"
    fake_uv.parent.mkdir()
    fake_uv.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\n')
    fake_uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_uv.parent}:{os.environ['PATH']}"}
    proc = subprocess.run(
        [str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().splitlines() == [
        "run ruff format --check .",
        "run ruff check .",
        "run mypy",
        "run pytest -q",
    ]
    bad = subprocess.run(
        [str(SCRIPT), "nope"], env=env, capture_output=True, text=True, check=False
    )
    assert bad.returncode == 2
