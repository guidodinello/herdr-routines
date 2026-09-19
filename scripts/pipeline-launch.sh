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
                           [--wait-timeout-ms MS] [--failure-marker TEXT]...

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
                      for the orchestrator (default: opencode/big-pickle)
  --prompt-file      orchestrator prompt source, relative to repo-parent
                      (default: docs/pipeline/orchestrator-prompt.md)
  --wait-timeout-ms  --wait timeout passed to `herdr agent prompt` (default: 25200000,
                      i.e. 7h — must match the job's deadline_ms)
  --failure-marker   text to watch for on the orchestrator's visible screen while
                      waiting (repeatable; default: "Free usage exceeded" — same as
                      runner.py's DEFAULT_FAILURE_MARKERS). Two consecutive sightings of
                      the same marker end the run early with an outcome of "failed
                      (quota_exhausted)" instead of waiting out the full deadline.
EOF
  exit 64
}

AGENT_KIND="opencode"
MODEL="opencode/big-pickle"  # orchestrator model; tick passes job.model via --model
PROMPT_FILE="docs/pipeline/orchestrator-prompt.md"
WAIT_TIMEOUT_MS="25200000"
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
    --failure-marker) FAILURE_MARKERS+=("$2"); shift 2 ;;
    -h|--help) usage ;;
    *) echo "pipeline-launch.sh: unknown argument: $1" >&2; usage ;;
  esac
done

if [ ${#FAILURE_MARKERS[@]} -eq 0 ]; then
  FAILURE_MARKERS=("Free usage exceeded")
fi

# How often the wait-loop below polls the visible screen for a failure marker while the
# orchestrator prompt is outstanding. Mirrors runner.py's WATCHDOG_POLL_INTERVAL_S (30s);
# overridable so tests don't have to sleep for real.
POLL_INTERVAL_S="${PIPELINE_LAUNCH_POLL_INTERVAL_S:-30}"

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
#
# Run it in the background and poll the visible screen concurrently for a quota-exhaustion
# wedge (runner.py's _prompt_with_watchdog pattern, ported here): a provider's free-tier
# limit dialog (e.g. "Free usage exceeded ... retrying in 11h 59m") never changes the
# agent's herdr-visible state away from "working", so a plain blocking `--wait` would sit
# for the entire multi-hour deadline before noticing. Two consecutive sightings of the SAME
# marker (a stability gate against a transient screen tear) end the wait early.
herdr agent prompt "$AGENT_NAME" "$(cat "$PROMPT_FILE_TMP")" --wait --timeout "$WAIT_TIMEOUT_MS" &
WAIT_PID=$!

QUOTA_MARKER=""
PREV_HIT=""
while kill -0 "$WAIT_PID" 2>/dev/null; do
  # Race a fresh sleep against the still-running prompt job and reap whichever finishes
  # first with `wait -n` — a plain `kill -0`-then-`sleep` loop leaves the prompt job a
  # zombie once it exits (still visible to `kill -0` until something `wait`s it), which
  # would spin this loop forever re-sleeping on a job that already finished.
  sleep "$POLL_INTERVAL_S" &
  SLEEP_PID=$!
  wait -n 2>/dev/null || true
  if ! kill -0 "$WAIT_PID" 2>/dev/null; then
    # The prompt job finished (and was just reaped by wait -n above); drop the leftover
    # sleep rather than let it become the next iteration's zombie.
    kill "$SLEEP_PID" 2>/dev/null || true
    wait "$SLEEP_PID" 2>/dev/null || true
    break
  fi
  # Still running past a full poll interval — the sleep is what finished. Poll the screen.
  SCREEN=$(herdr agent read "$AGENT_NAME" --source visible --lines 200 2>/dev/null)
  HIT=""
  for marker in "${FAILURE_MARKERS[@]}"; do
    if [ -n "$marker" ] && printf '%s' "$SCREEN" | grep -qF "$marker"; then
      HIT="$marker"
      break
    fi
  done
  if [ -n "$HIT" ] && [ "$HIT" = "$PREV_HIT" ]; then
    QUOTA_MARKER="$HIT"
    break
  fi
  PREV_HIT="$HIT"
done

if [ -n "$QUOTA_MARKER" ]; then
  kill "$WAIT_PID" 2>/dev/null || true
  wait "$WAIT_PID" 2>/dev/null || true
  PROMPT_STATUS=124
  echo "=== quota-exhaustion marker '$QUOTA_MARKER' confirmed on two consecutive" \
    "${POLL_INTERVAL_S}s polls; ending run_id=$RUN_ID early instead of waiting out the" \
    "full deadline ==="
else
  wait "$WAIT_PID"
  PROMPT_STATUS=$?
fi
echo "=== prompted (wait exited status=$PROMPT_STATUS), run_id=$RUN_ID ws=$WS_PANE report=$REPORT ==="

# `--wait` exits 0 for a `blocked` settle exactly as it does for `idle`/`done` (issue 037) —
# the exit code alone can't tell success from a stuck orchestrator. Read the actual settle
# status instead, and on anything but idle/done capture the visible screen (033's fix,
# never reached this launcher path) and stub a failed report *before* the `cleanup` trap
# below closes the pane and destroys the only evidence of what it was stuck on.
SETTLE_JSON=$(herdr agent get "$AGENT_NAME" 2>&1)
SETTLE_STATUS=$(printf '%s' "$SETTLE_JSON" | jq -r '.result.agent.agent_status // empty' 2>/dev/null)
[ -z "$SETTLE_STATUS" ] && SETTLE_STATUS="unknown"
echo "=== settle status: $SETTLE_STATUS (quota_marker='$QUOTA_MARKER') ==="

if [ -n "$QUOTA_MARKER" ] || { [ "$SETTLE_STATUS" != "idle" ] && [ "$SETTLE_STATUS" != "done" ]; }; then
  # --source visible, not the default read: the plain read is rejected while unsettled
  # (agent_not_idle), which is precisely the blocked/unknown case here — same reasoning as
  # runner._capture_visible_tail (issue 033).
  TAIL_FILE="$(dirname "$REPORT")/${RUN_ID}.tail.txt"
  TAIL_TEXT=$(herdr agent read "$AGENT_NAME" --source visible --lines 200 2>&1)
  echo "=== captured visible tail for $AGENT_NAME (settle=$SETTLE_STATUS) ==="
  echo "$TAIL_TEXT"
  if [ -n "$TAIL_TEXT" ]; then
    printf '%s\n' "$TAIL_TEXT" > "$TAIL_FILE"
  fi

  # Only stub the report if the orchestrator hasn't already written one itself — a report
  # with real content always wins over this best-effort marker.
  if [ ! -s "$REPORT" ]; then
    if [ -n "$QUOTA_MARKER" ]; then
      {
        echo "# Pipeline run $RUN_ID — launcher stub report"
        echo
        echo "## Outcome: failed (quota_exhausted)"
        echo
        echo "settle_status: $SETTLE_STATUS"
        echo "tail: $TAIL_FILE"
        echo
        echo "pipeline-launch.sh wrote this stub: the orchestrator's visible screen showed" \
          "the failure marker '$QUOTA_MARKER' on two consecutive ${POLL_INTERVAL_S}s polls," \
          "so the run was ended early instead of waiting out the full deadline. Configure" \
          "this job's \`fallback_model\` (config.py) to have tick retry automatically with" \
          "a different model. See the tail file above for the exact screen content."
      } > "$REPORT"
    else
      {
        echo "# Pipeline run $RUN_ID — launcher stub report"
        echo
        echo "## Outcome: failed"
        echo
        echo "settle_status: $SETTLE_STATUS"
        echo "tail: $TAIL_FILE"
        echo
        echo "pipeline-launch.sh wrote this stub: the orchestrator settled '$SETTLE_STATUS'" \
          "instead of idle/done, so \`herdr agent prompt --wait\` returned without a real" \
          "report. Written before the pane was closed so tick reconciles on the next tick" \
          "instead of waiting out the full deadline (issue 037). See the tail file above" \
          "for what the orchestrator was stuck on."
      } > "$REPORT"
    fi
    echo "=== wrote failed-outcome stub report to $REPORT ==="
  fi

  herdr notification show --sound request >/dev/null 2>&1 || true
fi
