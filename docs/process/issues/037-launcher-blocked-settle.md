---
id: "037"
title: "pipeline-launch.sh never inspects settle status: a blocked orchestrator looks like success for 8h"
status: open
priority: high
area: pipeline
---

## Description

On 2026-09-05 the nightly pipeline died at second zero and nothing noticed for
eight hours.

`/tmp/pipeline_launch_20260905T050000Z.log`:

```
=== launch at 2026-09-05T02:00:24-03:00, run_id=20260905T050000Z agent=rt-nightly-pipeline ===
{"id":"cli:agent:prompt","result":{"agent":{... "agent_status":"blocked" ...}}}
=== prompted (wait exited status=0), run_id=20260905T050000Z ws=w3N:p1 ... ===
=== closed orchestrator pane w3N:p1 (cleanup trap) ===
```

Three failures compound:

1. **`scripts/pipeline-launch.sh` never inspects the settle status.**
   `herdr agent prompt --wait` exits 0 for a `blocked` settle just as it does for
   `idle`/`done`. The script records `PROMPT_STATUS=$?`, logs it, and falls
   through as if the run succeeded. The orchestrator prompt's own settle mapping
   (`idle/done → success, blocked → needs-human, unknown → interrupted_unknown`)
   is documented for *workers* and never applied to the orchestrator itself.

2. **The `cleanup` EXIT trap then destroys the evidence.** It closes `$WS_PANE`
   on every exit path — which is correct for the pane-leak it was written to fix
   (`docs/pipeline/pane-lifecycle-v2-proposal.md`), but it runs *before* anything
   captures the visible screen. There is no `herdr-server.log` on the box either,
   so **why** the orchestrator was blocked is now unknowable.

3. **No report means `tick` waits out the full deadline.** With no
   `## Outcome:` line to reconcile against, `tick` logged
   `nightly-pipeline: in flight` every five minutes from 05:00 to 13:05Z before
   finally recording `failed (reason: no_report)`. Eight hours of believing a
   process that had been dead since 05:00:24.

Net result: no worktree, no `state.json`, no heartbeat log, no report, no
notification, and no diagnosable cause.

### This is issue 033's fix, on the path that didn't get it

Issue 033 ("Capture a diagnostic tail on a blocked settle") added exactly this
diagnostic — a `_capture_visible_tail(...)` call on the `blocked` branch — and
shipped in `11cfb9c`. But it landed in `src/herdr_routines/runner.py`, which
serves **routine** jobs. `kind: pipeline` dispatches through
`tick._process_pipeline_job` → `systemd-run` → `scripts/pipeline-launch.sh`, a
separate path that shares none of `runner.py`'s settle handling. The lesson of 033
never reached the launcher.

## Design (proposal)

In `scripts/pipeline-launch.sh`, between the `herdr agent prompt --wait` and the
trap firing:

1. **Read the settle status** — `herdr agent get "$AGENT_NAME" | jq -r
   '.result.agent.agent_status'` — and branch on it rather than on the exit code
   alone.
2. **On `blocked` or `unknown`, capture the screen before the pane closes**:
   `herdr agent read "$AGENT_NAME" --source visible --lines 200` appended to
   `$LOG` and written next to the report as `<run_id>.tail.txt`, mirroring
   `runner._capture_visible_tail`. Use `--source visible`; the plain read is
   rejected while unsettled, which is precisely the `blocked` case (033's
   finding).
3. **Write an `## Outcome: failed` stub report** to `$REPORT` whenever the
   orchestrator settles non-successfully and has not written one itself, with the
   settle status and a pointer to the tail file. `tick` reconciles on that marker,
   so this converts an 8-hour `no_report` timeout into a failure recorded within
   one tick.
4. **Fire `herdr notification show --sound request`** on that path, consistent
   with the deadline-exceeded branch (G-7).

Keep the trap closing the pane — the leak fix stays. The capture just has to
happen first.

Consider factoring the settle-status → outcome mapping into one place shared by
`runner.py` and the launcher, so the next fix does not have to be applied twice.

## Acceptance criteria

1. A `blocked` orchestrator settle writes a tail file before the pane is closed. Test: `test_launcher_captures_tail_on_blocked`
2. A `blocked` settle writes a report containing `## Outcome: failed`. Test: `test_launcher_writes_failed_outcome_stub`
3. `tick` reconciles that stub on the next tick instead of waiting for the deadline. Test: `test_tick_reconciles_launcher_failure_stub`
4. The pane is still closed on every exit path, including the capture path. Test: `test_launcher_closes_pane_after_capture`
