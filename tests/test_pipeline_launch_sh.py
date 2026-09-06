"""Tests for scripts/pipeline-launch.sh (issue 037): `herdr agent prompt --wait` exits 0
for a `blocked` settle exactly as it does for `idle`/`done`, so the launcher must read the
actual settle status via `herdr agent get`, capture the visible screen and stub a failed
report *before* its `cleanup` EXIT trap closes the orchestrator pane and destroys the
evidence.

These tests run the real script against a fake `herdr` CLI (a small bash stub) placed on
PATH the same way the script builds its own PATH — under `$HOME/.local/bin` — since the
script hardcodes its own PATH rather than trusting the caller's.
"""

from __future__ import annotations

import os
import stat
import subprocess
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "pipeline-launch.sh"

# Records every invocation (one line per call, "<argv[0]> <argv[1]> ...") to
# $FAKE_HERDR_CALL_LOG so tests can assert both occurrence and ordering.
FAKE_HERDR = """#!/bin/bash
echo "$*" >> "$FAKE_HERDR_CALL_LOG"
case "$1 $2" in
  "workspace create")
    echo '{"result":{"root_pane":{"pane_id":"w1:p1"}}}'
    ;;
  "agent start")
    exit 0
    ;;
  "agent prompt")
    exit 0
    ;;
  "agent get")
    printf '{"result":{"agent":{"agent_status":"%s"}}}\\n' "${FAKE_SETTLE_STATUS:-idle}"
    ;;
  "agent read")
    printf '%s\\n' "${FAKE_TAIL_TEXT:-}"
    ;;
  "pane close")
    exit 0
    ;;
  "notification show")
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


def _run_launcher(
    tmp_path: Path,
    *,
    settle_status: str,
    tail_text: str = "",
    report_precontent: str | None = None,
) -> tuple[Path, Path]:
    """Runs the real launcher script against the fake herdr CLI above. Returns
    (report_path, call_log_path)."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    herdr_path = bin_dir / "herdr"
    herdr_path.write_text(FAKE_HERDR)
    herdr_path.chmod(
        herdr_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    repo_parent = tmp_path / "repo"
    repo_parent.mkdir()
    (repo_parent / "prompt.md").write_text("do the thing\n")

    run_id = f"T{uuid.uuid4().hex[:12]}"
    report_path = tmp_path / "reports" / f"pipeline-{run_id}.md"
    report_path.parent.mkdir(parents=True)
    if report_precontent is not None:
        report_path.write_text(report_precontent)

    call_log = tmp_path / "calls.log"
    call_log.write_text("")

    env = {
        **os.environ,
        "HOME": str(home),
        "FAKE_HERDR_CALL_LOG": str(call_log),
        "FAKE_SETTLE_STATUS": settle_status,
        "FAKE_TAIL_TEXT": tail_text,
    }

    argv = [
        "bash",
        str(LAUNCHER),
        "--run-id",
        run_id,
        "--repo-parent",
        str(repo_parent),
        "--report",
        str(report_path),
        "--agent-name",
        "rt-nightly-pipeline",
        "--prompt-file",
        "prompt.md",
        "--wait-timeout-ms",
        "1000",
    ]
    subprocess.run(argv, env=env, timeout=60, check=False)
    return report_path, call_log


def test_launcher_captures_tail_on_blocked(tmp_path: Path) -> None:
    tail_text = "orchestrator is waiting on a permission prompt it never got"
    report_path, _call_log = _run_launcher(
        tmp_path, settle_status="blocked", tail_text=tail_text
    )

    run_id = report_path.stem.removeprefix("pipeline-")
    tail_file = report_path.parent / f"{run_id}.tail.txt"
    assert tail_file.exists()
    assert tail_text in tail_file.read_text()


def test_launcher_writes_failed_outcome_stub(tmp_path: Path) -> None:
    report_path, _call_log = _run_launcher(
        tmp_path, settle_status="blocked", tail_text="stuck here"
    )

    assert report_path.exists()
    report_text = report_path.read_text()
    assert "## Outcome: failed" in report_text
    assert "blocked" in report_text
    # Points a human at the captured evidence, not just "it failed".
    assert ".tail.txt" in report_text


def test_launcher_does_not_overwrite_an_existing_report(tmp_path: Path) -> None:
    """The orchestrator's own report always wins over the launcher's best-effort stub."""
    report_path, _call_log = _run_launcher(
        tmp_path,
        settle_status="blocked",
        tail_text="stuck here",
        report_precontent="## Outcome: ok\nreal report\n",
    )

    assert report_path.read_text() == "## Outcome: ok\nreal report\n"


def test_launcher_leaves_report_and_tail_alone_on_idle_settle(tmp_path: Path) -> None:
    """The happy path (idle/done) must not fire the blocked/unknown branch at all."""
    report_path, _call_log = _run_launcher(
        tmp_path, settle_status="idle", tail_text="should never be read"
    )

    run_id = report_path.stem.removeprefix("pipeline-")
    tail_file = report_path.parent / f"{run_id}.tail.txt"
    assert not report_path.exists()
    assert not tail_file.exists()


def test_launcher_closes_pane_after_capture(tmp_path: Path) -> None:
    """The pane is still closed on every exit path (the leak fix stays) — but only after
    the tail capture, so the trap never destroys evidence before it's written."""
    _report_path, call_log = _run_launcher(
        tmp_path, settle_status="blocked", tail_text="stuck here"
    )

    lines = call_log.read_text().splitlines()
    read_calls = [i for i, line in enumerate(lines) if line.startswith("agent read")]
    close_calls = [i for i, line in enumerate(lines) if line.startswith("pane close")]
    assert read_calls, "expected a visible-tail capture call"
    assert close_calls, "expected the cleanup trap to close the pane"
    assert max(read_calls) < min(close_calls)
