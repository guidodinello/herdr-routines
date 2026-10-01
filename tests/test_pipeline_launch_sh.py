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
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "scripts" / "pipeline-launch.sh"

# A stand-in for `uv run herdr-routines pipeline-prepare` (issue 054 phase A) and, since
# issue 056 phase B, `uv run herdr-routines pipeline-run`. The launcher calls the real
# things on a real host; here both are stubbed and their argv is logged to the same call
# log as herdr's, so "prepare ran before the run started" and "the deadline reached
# prepare unchanged" are assertable from one ordered log.
#
# FAKE_PREPARE_RC is prepare's exit code (0 prepared, 3 no feature, 1 failed);
# FAKE_PREPARE_VALUES is the KEY=VALUE stdout the launcher parses.
# FAKE_PIPELINE_RUN_RC / FAKE_PIPELINE_RUN_WRITES_REPORT stand in for the run itself: by
# default it exits 0 *without* writing a report, which is exactly the killed-mid-run
# shape the launcher's backstop exists for.
FAKE_UV = """#!/bin/bash
echo "uv $*" >> "$FAKE_HERDR_CALL_LOG"
case "$2 $3" in
  "herdr-routines pipeline-prepare")
    printf '%s\\n' "$FAKE_PREPARE_VALUES"
    exit "${FAKE_PREPARE_RC:-0}"
    ;;
  "herdr-routines pipeline-run")
    if [ "${FAKE_PIPELINE_RUN_WRITES_REPORT:-0}" = "1" ]; then
      report=""
      prev=""
      for arg in "$@"; do
        if [ "$prev" = "--report" ]; then report="$arg"; fi
        prev="$arg"
      done
      printf '## Outcome: ok\\nreal report\\n' > "$report"
    fi
    exit "${FAKE_PIPELINE_RUN_RC:-0}"
    ;;
  *)
    echo "fake uv: unexpected command: $*" >&2
    exit 99
    ;;
esac
"""

PREPARED_VALUES = """FEATURE_IDEA=Issue 001 (herdr-routines issue 001, docs/process/issues/001-foo.md).\n\n## Description\n\nDo the thing.
FEATURE_SOURCE=docs/process/issues/001-foo.md
ISSUE_ID=001
WT=$HOME/.herdr/worktrees/herdr-routines/auto-pipeline-T000000000000
BRANCH=auto/pipeline-T000000000000
SHARED_WS=w-prepared
STATE_JSON=$HOME/.herdr/worktrees/herdr-routines/auto-pipeline-T000000000000/state.json"""

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
    deadline_epoch: str | None = None,
    prepare_rc: int = 0,
    prepare_values: str = PREPARED_VALUES,
    pipeline_run_rc: int = 0,
    pipeline_run_writes_report: bool = False,
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
    # The launcher hardcodes its own PATH ($HOME/.local/bin first), so the fake `uv` goes
    # in the same bin dir as the fake `herdr` — a real `uv run` here would invoke the
    # real subcommand against this tmp repo and fail the pre-flight for real.
    uv_path = bin_dir / "uv"
    uv_path.write_text(FAKE_UV)
    uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

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
        "FAKE_PREPARE_RC": str(prepare_rc),
        "FAKE_PREPARE_VALUES": prepare_values,
        "FAKE_PIPELINE_RUN_RC": str(pipeline_run_rc),
        "FAKE_PIPELINE_RUN_WRITES_REPORT": "1" if pipeline_run_writes_report else "0",
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
    if deadline_epoch is not None:
        argv += ["--deadline-epoch", deadline_epoch]
    subprocess.run(argv, env=env, timeout=60, check=False)
    return report_path, call_log


# -- issue 056 phase B: the launcher hands the whole stage loop to code --------------
#
# What the launcher still owns is small: run the pre-flight, honour its exit code, make
# one `pipeline-run` call, and never leave the run without a report. Everything the old
# tests asserted about a settle status, a captured screen tail and a bash marker-poll
# loop is gone with the orchestrator, and the tests below assert what replaced it.


def test_launcher_runs_pipeline_run_after_prepare(tmp_path: Path) -> None:
    """Phase A's guarantee, restated for the new successor: nothing the stage loop needs
    is produced by a model, so the pre-flight has already written `state.json` before
    `pipeline-run` is invoked."""
    _report_path, call_log = _run_launcher(tmp_path, settle_status="idle")

    lines = call_log.read_text().splitlines()
    prepare_idx = next(i for i, line in enumerate(lines) if "pipeline-prepare" in line)
    run_idx = next(i for i, line in enumerate(lines) if "pipeline-run" in line)
    assert prepare_idx < run_idx, "prepare must run before pipeline-run"
    prepare_argv = lines[prepare_idx]
    for flag in ("--run-id", "--repo-parent", "--deadline-epoch"):
        assert flag in prepare_argv


def test_launcher_passes_the_prepared_state_json_to_pipeline_run(
    tmp_path: Path,
) -> None:
    """The prepared `KEY=VALUE` block is machine-parsed, not read by a model: the one
    value that has to cross the shell boundary is the path of the state file, which
    carries worktree, branch, workspace, feature source and deadline already."""
    _report_path, call_log = _run_launcher(tmp_path, settle_status="idle")

    log = call_log.read_text()
    assert "--state-json" in log
    assert "auto-pipeline-T000000000000/state.json" in log


def test_launcher_stubs_a_failed_report_when_pipeline_run_writes_none(
    tmp_path: Path,
) -> None:
    """`pipeline-run` writes the report on every path it controls, so an empty one after
    it exits means the process was killed before it got there. Tick reconciles from that
    file, so without this stub the run stays "running" in history until the watchdog
    reaps it hours later. This used to be conditional on the orchestrator settling badly;
    with no orchestrator there is nothing to condition on."""
    report_path, call_log = _run_launcher(tmp_path, settle_status="idle")

    log = call_log.read_text()
    assert "pipeline-run" in log
    assert report_path.exists(), "an empty report after pipeline-run must be stubbed"
    report_text = report_path.read_text()
    assert "## Outcome: failed" in report_text
    assert "notification show" in log


def test_launcher_does_not_overwrite_a_real_report(tmp_path: Path) -> None:
    """A report with real content always wins over the launcher's best-effort stub."""
    report_path, call_log = _run_launcher(
        tmp_path,
        settle_status="idle",
        report_precontent="## Outcome: ok\nreal report\n",
    )

    assert report_path.read_text() == "## Outcome: ok\nreal report\n"
    assert "notification show" not in call_log.read_text()


def test_launcher_skips_pipeline_run_when_no_feature(tmp_path: Path) -> None:
    """Issue 052, still the launcher's job: an empty backlog costs no run at all.
    `pipeline-prepare` exits 3, which becomes a successful exit (0) — a healthy skip,
    not a break tick should report."""
    report_path, call_log = _run_launcher(
        tmp_path, settle_status="idle", prepare_rc=3, prepare_values=""
    )

    log = call_log.read_text()
    assert "pipeline-prepare" in log
    assert "pipeline-run" not in log
    # prepare already wrote the terminal report; the launcher must not stub over it.
    assert not report_path.exists()


def test_launcher_propagates_a_prepare_failure(tmp_path: Path) -> None:
    """A pre-flight that could not sync, claim or create the worktree must not fall
    through into a run that has nothing to work from — and must not write a second
    report over prepare's own. `pipeline-prepare` writes its terminal report on both its
    non-zero paths precisely so this script has nothing left to say about them."""
    report_path, call_log = _run_launcher(
        tmp_path, settle_status="idle", prepare_rc=1, prepare_values=""
    )

    log = call_log.read_text()
    assert "pipeline-prepare" in log
    assert "pipeline-run" not in log
    # The fake prepare writes no report of its own here; the launcher's job is to add
    # nothing, not to invent one.
    assert not report_path.exists()


def test_launcher_forwards_failure_markers_to_pipeline_run(tmp_path: Path) -> None:
    """Quota exhaustion is detected inside the shared wait loop now, so the markers are
    forwarded rather than scanned for in bash — one implementation, not two copies."""
    _report_path, call_log = _run_launcher(tmp_path, settle_status="idle")

    assert "--failure-marker Free usage exceeded" in call_log.read_text()


def test_launcher_matches_a_custom_failure_marker(tmp_path: Path) -> None:
    _report_path, call_log = _run_launcher(
        tmp_path, settle_status="idle", failure_markers=("Custom quota wall",)
    )

    log = call_log.read_text()
    assert "--failure-marker Custom quota wall" in log
    assert "Free usage exceeded" not in log


def test_launcher_hands_pipeline_run_the_tick_computed_deadline(tmp_path: Path) -> None:
    """One deadline, computed once, and it reaches prepare verbatim — the 2026-09-28
    bug was a caller recomputing it as a year in the past. `pipeline-run` then reads it
    out of `state.json` rather than taking it as a flag."""
    _, call_log = _run_launcher(
        tmp_path, settle_status="idle", deadline_epoch="1790597715"
    )

    assert "--deadline-epoch 1790597715" in call_log.read_text()


def test_launcher_computes_deadline_when_tick_passes_none(tmp_path: Path) -> None:
    """The launcher (auto-synced job repo) can be newer than tick (runner checkout), so
    it still hands over a code-computed deadline when no --deadline-epoch arrives."""
    before = int(time.time())
    _, call_log = _run_launcher(
        tmp_path, settle_status="idle", wait_timeout_ms="3600000"
    )
    after = int(time.time())

    match = re.search(r"--deadline-epoch (\d+)", call_log.read_text())
    assert match is not None
    assert before + 3600 <= int(match.group(1)) <= after + 3600


def test_launcher_runs_pipeline_run_and_stages_dir_is_complete(tmp_path: Path) -> None:
    """Phase B deletes the orchestrator agent, its workspace and the bash marker-poll
    loop (a third copy of runner.py's wait loop) from the launcher, and replaces them
    with one `pipeline-run` call. Two things have to hold together, because a missing
    one only shows up hours later: the launcher must start no agent at all, and every
    prompt file `STAGES` names must exist — a missing file would abort at stage 1,
    after the whole night had already been prepared."""
    _report_path, call_log = _run_launcher(tmp_path, settle_status="idle")

    log = call_log.read_text()
    assert "pipeline-run" in log
    # Nothing that starts a model: no orchestrator agent, no prompt, no workspace.
    assert "agent start" not in log
    assert "agent prompt" not in log
    assert "workspace create" not in log
    # The values it does need are on the flag surface, and the deadline reaches
    # `pipeline-run` as the single value tick computed.
    assert "--run-id" in log
    assert "--state-json" in log
    assert "--report" in log
    assert "--prompts-dir" in log
    assert "--failure-marker Free usage exceeded" in log

    # The workflow table and the prompt files it names cannot disagree.
    from herdr_routines.pipeline_stages import STAGES

    stages_dir = REPO_ROOT / "docs" / "pipeline" / "stages"
    assert stages_dir.is_dir()
    for spec in STAGES:
        if spec.prompt_file is None:
            assert spec.model is None
            continue
        assert (stages_dir / spec.prompt_file).is_file(), spec.prompt_file
