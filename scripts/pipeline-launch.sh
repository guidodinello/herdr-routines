#!/bin/bash
# Overnight feature-pipeline launcher (issue 026).
#
# Runs inside a detached `systemd-run --user` unit that `tick._process_pipeline_job`
# generates for a `kind: pipeline` job — tick launches this and returns immediately
# (it holds `tick.lock` and must never block on the multi-hour orchestrator run). This
# is a parameterized port of the launcher previously hand-maintained only on the Pi at
# `~/.local/bin/pipeline-launch-nightly.sh` (the "still outstanding" gap issue 004's log
# flagged) — every value that script hardcoded is now a flag, so the script is generic
# across jobs/hosts and lives under version control.
#
# Phase B note: `--agent-name`, `--agent-kind`, `--model`, `--prompt-file` and
# `--wait-timeout-ms` remain on the flag surface because `tick._build_pipeline_launch_argv`
# still passes them, but they no longer change what this script does — the stage agents
# and their prompts are declared in `pipeline_stages.py`. They are documented as accepted
# rather than silently ignored so an operator reading `--help` isn't misled.
#
# Runs the pre-flight in code (`uv run herdr-routines pipeline-prepare`, issue 054 phase A)
# and then, only if there is something to build, hands the run to `pipeline-run` (issue
# 056 phase B). Nothing here starts an agent: sync, pick/claim, worktree+workspace and
# state.json are done before any agent exists (so a night with an empty backlog costs a
# report and nothing else, issue 052), and the stage loop that used to be an orchestrator
# agent is now a Python function.
set -u

usage() {
  cat >&2 <<'EOF'
Usage: pipeline-launch.sh --run-id ID --repo-parent PATH --report PATH --agent-name NAME
                           [--agent-kind KIND] [--model MODEL] [--prompt-file PATH]
                           [--wait-timeout-ms MS] [--deadline-epoch EPOCH]
                           [--failure-marker TEXT]...

  --run-id           bare UTC timestamp, e.g. 20260905T020000Z (fits the pl-<N>-<run_id>
                      worker agent-name cap once "pl-N-" is prepended)
  --repo-parent      path to the parent clone the pipeline's shared worktree is branched
                      from (a plain git clone, not a herdr worktree). Also the root
                      `pipeline-run` reads the stage prompts from, as
                      <repo-parent>/docs/pipeline/stages
  --report           absolute path this run's terminal report is pinned to; written by
                      pipeline-run, and stubbed here if it exits without writing one
  --deadline-epoch   the run's wall-clock deadline (unix seconds), computed by tick
                      from the launch time + deadline_ms and handed to pipeline-prepare
                      verbatim (default: now + --wait-timeout-ms, for a tick that predates
                      this flag)
  --failure-marker   text `pipeline-run` watches each stage's screen for (repeatable;
                      default: "Free usage exceeded" — same as runner.py's
                      DEFAULT_FAILURE_MARKERS). Two consecutive sightings of the same
                      marker resume that stage on its fallback model instead of waiting
                      out the stage's timeout.

  Accepted and ignored, kept only because tick still passes them (phase B):
  --agent-name, --agent-kind, --model, --prompt-file, --wait-timeout-ms
EOF
  exit 64
}

AGENT_KIND="opencode"
MODEL="opencode/big-pickle"  # orchestrator model; tick passes job.model via --model
PROMPT_FILE="docs/pipeline/orchestrator-prompt.md"
WAIT_TIMEOUT_MS="25200000"
DEADLINE_EPOCH=""
RUN_ID=""
REPO_PARENT=""
REPORT=""
AGENT_NAME=""
FAILURE_MARKERS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id) RUN_ID="$2"; shift 2 ;;
    --repo-parent) REPO_PARENT="$2"; shift 2 ;;
    --report) REPORT="$2"; shift 2 ;;
    --agent-name) AGENT_NAME="$2"; shift 2 ;;
    --agent-kind) AGENT_KIND="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
    --wait-timeout-ms) WAIT_TIMEOUT_MS="$2"; shift 2 ;;
    --deadline-epoch) DEADLINE_EPOCH="$2"; shift 2 ;;
    --failure-marker) FAILURE_MARKERS+=("$2"); shift 2 ;;
    -h|--help) usage ;;
    *) echo "pipeline-launch.sh: unknown argument: $1" >&2; usage ;;
  esac
done

# The launcher comes from the auto-synced job repo while tick runs from the runner
# checkout, so the two can briefly be on different versions: compute the deadline here
# too rather than leaving it to the orchestrator model.
if [ -z "$DEADLINE_EPOCH" ]; then
  DEADLINE_EPOCH=$(( $(date +%s) + WAIT_TIMEOUT_MS / 1000 ))
fi

if [ ${#FAILURE_MARKERS[@]} -eq 0 ]; then
  FAILURE_MARKERS=("Free usage exceeded")
fi

for required in RUN_ID REPO_PARENT REPORT AGENT_NAME; do
  if [ -z "${!required}" ]; then
    echo "pipeline-launch.sh: --${required,,} is required" >&2
    usage
  fi
done

export PATH="$HOME/.local/bin:$HOME/.opencode/bin:/usr/local/bin:/usr/bin:/bin"
LOG="/tmp/pipeline_launch_${RUN_ID}.log"
exec >>"$LOG" 2>&1
echo "=== launch at $(date -Is), run_id=$RUN_ID agent=$AGENT_NAME ==="

# No pane, so no cleanup trap: phase B deleted the workspace this used to create, and
# `pipeline-run` closes each stage pane itself.

cd "$REPO_PARENT" || exit 1

# Pre-flight in code (issue 054 phase A), BEFORE any agent exists: sync the parent clone,
# pick+claim the feature, create the shared worktree+workspace and write state.json. On
# success it prints the resolved values as KEY=VALUE on stdout, which go into the
# orchestrator's prompt header below instead of being computed by a model. `uv run` is
# required — herdr-routines is not a standalone binary on the pipeline hosts (issue 050).
#
# Exit codes: 0 prepared, 3 nothing to build (a healthy night with an empty backlog),
# 1 sync/setup failure, 2 usage. 3 is distinct from 1 on purpose so this branch can skip
# the agent quietly instead of reporting a break. Both non-zero paths already have their
# terminal report written at $REPORT (tick reconciles from there), so add nothing here.
#
# stdout only: stderr is already redirected into $LOG above, and merging it in would
# splice log text into a KEY=VALUE stream about to be appended to the prompt.
PREPARE_OUT=$(uv run herdr-routines pipeline-prepare \
  --run-id "$RUN_ID" --repo-parent "$REPO_PARENT" --report "$REPORT" \
  --deadline-epoch "$DEADLINE_EPOCH") ; PREPARE_RC=$?
if [ "$PREPARE_RC" -ne 0 ]; then
  echo "=== pipeline-prepare exited $PREPARE_RC; report already at $REPORT, no agent started ==="
  if [ "$PREPARE_RC" -eq 3 ]; then
    exit 0   # a skip is a successful skip
  fi
  exit "$PREPARE_RC"
fi
echo "=== prepared values for run_id=$RUN_ID ==="
echo "$PREPARE_OUT"

# `pipeline-run` owns the stage loop from here (issue 056 phase B): the workspace, the
# orchestrator agent, the prompt assembly, the bash marker-poll loop and the settle
# check are all gone, because each was a hand-maintained duplicate of something Python
# already does -- and the two duplicates had already drifted. The script is now a
# pre-flight wrapper with one call and a last-resort report backstop.
#
# `pipeline-prepare` emits machine-parsed KEY=VALUE lines on stdout. Everything phase B
# needs from it is already in the state file it wrote -- worktree, branch, shared
# workspace, feature source, issue id, deadline_epoch, artifact paths -- so only that
# file's path has to cross the shell boundary. Passing the rest as flags would mean
# re-deriving them here (and get `deadline_epoch` wrong, since this script and tick can
# briefly be different versions of the repo -- see the DEADLINE_EPOCH fallback above).
STATE_JSON=$(printf '%s\n' "$PREPARE_OUT" | sed -n 's/^STATE_JSON=//p' | head -n 1)
if [ -z "$STATE_JSON" ]; then
  echo "=== pipeline-prepare emitted no STATE_JSON (run_id=$RUN_ID); cannot run stages ==="
  exit 1
fi

PROMPTS_DIR="$REPO_PARENT/docs/pipeline/stages"

echo "=== dispatching pipeline-run: run_id=$RUN_ID state=$STATE_JSON prompts=$PROMPTS_DIR ==="
RUN_RC=0
# The failure markers are the launcher's only remaining opinion about the run: quota
# exhaustion is detected inside the wait loop (runner.wait_loop), so they are forwarded
# verbatim rather than scanned for here. Each marker must stay ONE argv entry: the default
# ("Free usage exceeded") contains spaces, and an unquoted `$(printf ...)` expansion
# word-split it into three, which argparse rejected with exit 2 every night from
# 2026-10-02 to 2026-10-07. The array is never empty (it defaults above), so `set -u`
# cannot bite.
MARKER_ARGS=()
for marker in "${FAILURE_MARKERS[@]}"; do
  MARKER_ARGS+=(--failure-marker "$marker")
done
uv run herdr-routines pipeline-run \
  --run-id "$RUN_ID" --state-json "$STATE_JSON" \
  --report "$REPORT" --prompts-dir "$PROMPTS_DIR" \
  "${MARKER_ARGS[@]}" || RUN_RC=$?
echo "=== pipeline-run exited $RUN_RC (run_id=$RUN_ID report=$REPORT) ==="

# Backstop. This used to be conditional on the orchestrator settling somewhere other than
# idle/done, and keyed `settle_status` and a captured screen tail into the stub. Neither
# exists any more -- there is no orchestrator to settle -- and there no longer needs to be:
# `pipeline-run` writes the terminal report itself on every path it controls, so an empty
# report now means exactly one thing, that this process died before it got there
# (RuntimeMaxSec, SIGTERM, a crash). Tick reconciles the run from that file, so without
# this stub a killed run stays "running" in history until the watchdog reaps it hours
# later. Unconditional, and it never overwrites a report with real content.
if [ ! -s "$REPORT" ]; then
  {
    echo "# Pipeline run $RUN_ID — launcher stub report"
    echo
    echo "## Outcome: failed"
    echo
    echo "pipeline_run_exit: $RUN_RC"
    echo
    echo "pipeline-launch.sh wrote this stub because \$REPORT was empty after" \
      "pipeline-run exited. pipeline-run writes the report on every path it controls, so" \
      "an empty one means the run was killed before it finished (systemd RuntimeMaxSec, a" \
      "signal, or a crash) rather than failing a check. See /tmp/pipeline_launch_${RUN_ID}.log" \
      "for the log of this launcher and the state at $STATE_JSON."
  } > "$REPORT"
  echo "=== wrote failed-outcome stub report to $REPORT ==="
  herdr notification show --sound request >/dev/null 2>&1 || true
fi

exit "$RUN_RC"
