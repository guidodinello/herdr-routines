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
# Does NOT sync the repo itself — that is `docs/pipeline/orchestrator-prompt.md`
# Prerequisite 1's job (`herdr-routines sync-repo`, issue 030's shipped primitive),
# which runs once inside the orchestrator's own session so there is exactly one owner
# of "is $REPO_PARENT up to date with origin/<base>".
set -u

usage() {
  cat >&2 <<'EOF'
Usage: pipeline-launch.sh --run-id ID --repo-parent PATH --report PATH --agent-name NAME
                           [--agent-kind KIND] [--model MODEL] [--prompt-file PATH]
                           [--wait-timeout-ms MS]

  --run-id           bare UTC timestamp, e.g. 20260905T020000Z (fits the pl-<N>-<run_id>
                      worker agent-name cap once "pl-N-" is prepended)
  --repo-parent      path to the parent clone the orchestrator branches its shared
                      worktree from (a plain git clone, not a herdr worktree)
  --report           absolute path this run's terminal $PIPELINE_REPORT is pinned to
  --agent-name       the orchestrator's own live agent name — MUST equal the dispatching
                      job's `rt-<name>` (Job.agent_name), or tick's `_live_agent_exists`
                      overlap guard silently never fires and a second run can launch on
                      top of this one
  --agent-kind       herdr agent kind (default: opencode)
  --model            native model flag value passed after `--` to `herdr agent start`
                      (default: opencode/muse-spark-1.2-contributor-free)
  --prompt-file      orchestrator prompt source, relative to repo-parent
                      (default: docs/pipeline/orchestrator-prompt.md)
  --wait-timeout-ms  --wait timeout passed to `herdr agent prompt` (default: 25200000,
                      i.e. 7h — must match the job's deadline_ms)
EOF
  exit 64
}

AGENT_KIND="opencode"
MODEL="opencode/muse-spark-1.2-contributor-free"
PROMPT_FILE="docs/pipeline/orchestrator-prompt.md"
WAIT_TIMEOUT_MS="25200000"
RUN_ID=""
REPO_PARENT=""
REPORT=""
AGENT_NAME=""

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
    -h|--help) usage ;;
    *) echo "pipeline-launch.sh: unknown argument: $1" >&2; usage ;;
  esac
done

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

# The trailing pane-close only runs on a normal fall-through exit. This unit is started
# with `-p RuntimeMaxSec=...` (tick.py's PIPELINE_UNIT_MARGIN_MS-padded deadline) — if
# that ever SIGTERMs this script, a plain last-line close would never fire and the leak
# this pattern exists to fix (docs/pipeline/pane-lifecycle-v2-proposal.md) comes right
# back. A trap runs on every exit path, killed or not.
WS_PANE=""
cleanup() {
  if [ -n "$WS_PANE" ]; then
    herdr pane close "$WS_PANE" >/dev/null 2>&1 || true
    echo "=== closed orchestrator pane $WS_PANE (cleanup trap) ==="
  fi
}
trap cleanup EXIT INT TERM

cd "$REPO_PARENT" || exit 1

WS_JSON=$(herdr workspace create --cwd "$REPO_PARENT" \
  --label "pipeline-$RUN_ID" --env HERDR_ENV=1 2>&1)
WS_PANE=$(printf '%s' "$WS_JSON" | jq -r '.result.root_pane.pane_id')
if [ "$WS_PANE" = "null" ] || [ -z "$WS_PANE" ]; then
  echo "workspace create failed: $WS_JSON"
  WS_PANE=""
  exit 1
fi

herdr agent start "$AGENT_NAME" --kind "$AGENT_KIND" --pane "$WS_PANE" \
  --timeout 120000 -- -m "$MODEL"
# Give the freshly started agent's shell a moment to settle before prompting it — the
# live launcher this was ported from found agent_start returning before the pane's
# prompt was ready to receive input.
sleep 5

PROMPT_FILE_TMP="/tmp/full_prompt_${RUN_ID}.md"
{
  cat "$PROMPT_FILE"
  printf '\nRUN_ID: %s\nREPO_PARENT: %s\nPIPELINE_REPORT: %s\n' "$RUN_ID" "$REPO_PARENT" "$REPORT"
} > "$PROMPT_FILE_TMP"

# --wait blocks until the orchestrator agent settles; the `cleanup` trap above closes
# $WS_PANE on every exit path from here, normal or killed, instead of only a fall-through
# (the 2026-08-24 leak this pattern fixes — see docs/pipeline/pane-lifecycle-v2-proposal.md).
herdr agent prompt "$AGENT_NAME" "$(cat "$PROMPT_FILE_TMP")" --wait --timeout "$WAIT_TIMEOUT_MS"
PROMPT_STATUS=$?
echo "=== prompted (wait exited status=$PROMPT_STATUS), run_id=$RUN_ID ws=$WS_PANE report=$REPORT ==="
