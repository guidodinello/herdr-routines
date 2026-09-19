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
    sleep "${FAKE_PROMPT_SLEEP_S:-0}"
    exit "${FAKE_PROMPT_EXIT_CODE:-0}"
    ;;
  "agent get")
    printf '{"result":{"agent":{"agent_status":"%s"}}}\\n' "${FAKE_SETTLE_STATUS:-idle}"
    ;;
  "agent read")
    # FAKE_TAIL_SEQUENCE (if set) is a newline-separated file of screen contents; each
    # call to "agent read" advances a counter and returns the next line, then repeats the
    # last one. Lets a test simulate the marker appearing on some polls but not others,
    # deterministically (call-count-based, not wall-clock-based). Falls back to the
    # static FAKE_TAIL_TEXT when no sequence file is set.
    if [ -n "${FAKE_TAIL_SEQUENCE:-}" ]; then
      n=$(($(cat "${FAKE_TAIL_SEQUENCE}.count" 2>/dev/null || echo 0) + 1))
      echo "$n" > "${FAKE_TAIL_SEQUENCE}.count"
      total=$(wc -l < "$FAKE_TAIL_SEQUENCE")
      idx=$n
      [ "$idx" -gt "$total" ] && idx=$total
      sed -n "${idx}p" "$FAKE_TAIL_SEQUENCE"
    else
      printf '%s\\n' "${FAKE_TAIL_TEXT:-}"
    fi
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
    prompt_sleep_s: float = 0,
    poll_interval_s: float = 30,
    failure_markers: tuple[str, ...] = (),
    tail_sequence: list[str] | None = None,
    wait_timeout_ms: str = "1000",
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
        "FAKE_PROMPT_SLEEP_S": str(prompt_sleep_s),
        "PIPELINE_LAUNCH_POLL_INTERVAL_S": str(poll_interval_s),
    }
    if tail_sequence is not None:
        sequence_path = tmp_path / "tail_sequence.txt"
        sequence_path.write_text("\n".join(tail_sequence) + "\n")
        env["FAKE_TAIL_SEQUENCE"] = str(sequence_path)

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
        wait_timeout_ms,
    ]
    for marker in failure_markers:
        argv += ["--failure-marker", marker]
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


def test_launcher_fails_fast_on_quota_exhaustion_marker(tmp_path: Path) -> None:
    """The actual bug this fixes: a provider's quota dialog (e.g. "Free usage exceeded
    ... retrying in 11h 59m") never changes the agent's herdr-visible state away from
    "working", so a plain blocking `--wait` sits for the entire --wait-timeout-ms before
    noticing. The launcher must instead end the run within a couple of poll intervals."""
    report_path, call_log = _run_launcher(
        tmp_path,
        settle_status="working",
        tail_text="Free usage exceeded, subscribe to Go [retrying in 11h 59m]",
        prompt_sleep_s=5,
        poll_interval_s=0.2,
        wait_timeout_ms="600000",
    )

    report_text = report_path.read_text()
    assert "## Outcome: failed (quota_exhausted)" in report_text
    assert "fallback_model" in report_text  # points a human/tick at the actual remedy

    read_calls = [
        line
        for line in call_log.read_text().splitlines()
        if line.startswith("agent read")
    ]
    # Two consecutive 0.2s polls confirm the marker; ending near-instantly means far
    # fewer reads than the ~25 a full 5s prompt_sleep_s at this poll interval would give.
    assert len(read_calls) <= 5


def test_launcher_requires_two_consecutive_marker_sightings(tmp_path: Path) -> None:
    """A single sighting of the marker (a transient screen tear/partial render) must not
    end the run — only two consecutive polls with the SAME marker do, mirroring
    runner.py's _prompt_with_watchdog stability gate."""
    report_path, _call_log = _run_launcher(
        tmp_path,
        settle_status="working",
        prompt_sleep_s=1.0,
        poll_interval_s=0.2,
        tail_sequence=[
            "Free usage exceeded",
            "clean screen, agent working normally",
            "clean screen, agent working normally",
            "clean screen, agent working normally",
            "clean screen, agent working normally",
        ],
    )

    report_text = report_path.read_text()
    assert "## Outcome: failed" in report_text
    assert "quota_exhausted" not in report_text


def test_launcher_matches_a_custom_failure_marker(tmp_path: Path) -> None:
    """--failure-marker (as `tick._build_pipeline_launch_argv` passes from a job's
    `failure_markers` config) is what the launcher actually watches for — not a
    hardcoded string."""
    report_path, _call_log = _run_launcher(
        tmp_path,
        settle_status="working",
        tail_text="CUSTOM_PROVIDER_QUOTA_DIALOG",
        prompt_sleep_s=5,
        poll_interval_s=0.2,
        failure_markers=("CUSTOM_PROVIDER_QUOTA_DIALOG",),
        wait_timeout_ms="600000",
    )

    assert "## Outcome: failed (quota_exhausted)" in report_path.read_text()


def test_launcher_custom_failure_marker_replaces_default(tmp_path: Path) -> None:
    """A job-supplied --failure-marker list replaces the default wholesale (matches
    job.failure_markers' documented semantics in config.py) — the default "Free usage
    exceeded" text must not match once a job supplies its own marker list."""
    report_path, _call_log = _run_launcher(
        tmp_path,
        settle_status="working",
        tail_text="Free usage exceeded, subscribe to Go",
        prompt_sleep_s=0.6,
        poll_interval_s=0.2,
        failure_markers=("SOME_OTHER_MARKER",),
    )

    report_text = report_path.read_text()
    assert "## Outcome: failed" in report_text
    assert "quota_exhausted" not in report_text
