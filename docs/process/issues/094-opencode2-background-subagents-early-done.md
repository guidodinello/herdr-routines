---
id: "094"
title: "opencode 2 jobs finish early while background subagents run"
status: done
priority: high
area: runner
---

## Description

opencode 2 runs `subagent` calls in the background. While one runs, the main session goes
`idle`, so herdr reports the agent settled (`idle`/`done`). `wait_loop.prompt_with_watchdog`
takes that settle as the end of the run. With no report yet, `_attempt_report_nudge`
(issue 032) fires, and then verification fails.

Seen on the Pi on 2026-11-25 (opencode 2.4.1, herdr 0.9.5): the 12:00 `fitted-pr-review`
run (`fitted-pr-review-20261125T150000Z`, session `ses_decc71913ffepH4f34RfDxTq2S`) was
recorded `failed` at 12:04:45 (275 s, `final_agent_status: done`, `nudged: True`,
`verify_failed:review_posted`). The agent kept working and posted its review at 12:24:57.
Under opencode 1 the same job took 550–1690 s and ended `done`.

Timeline of the incident session (UTC, `opencode api session.message.list` and
`session.list --param parentID=…`):

- 15:03:35–15:05:02: child `ses_decc43364…` busy.
- 15:03:44: parent `idle` event. herdr settles. No report exists, so the nudge goes in
  at 15:03:59 (the `user` message).
- 15:04:50: parent `idle` again. herdr-routines records `failed` at about 15:04:45.
- 24 child sessions in all, the last ending 15:24:08. Final parent `idle` at 15:25:35.

### Live probe (Pi, opencode 2.4.1, 2026-11-25)

One throwaway session on `opencode/big-pickle` launched one background subagent; the
probe polled every 3–4 s, and the session was removed afterwards.

| UTC | `experimental.job.list` (`sessionID`, `descendants=true`) | `session.active` | parent |
|---|---|---|---|
| 23:21:29 | `[{kind: subagent, status: running}]` | parent and child | busy |
| 23:21:33–23:22:02 | still `running` (9 polls) | child only | `idle`, `outcome: succeeded` |
| 23:22:06 | `[]` | parent only | busy again |
| 23:22:21 | `[]` | `[]` | `idle` |

`session.inbox.list` was `[]` on every poll. The parent's `outcome` was already `succeeded`
at the first, premature idle, so it is not a completion signal.

Findings that shape the fix:

- opencode 2.1.4 (laptop) has no `experimental.job.list` ("Operation not found") and no
  `time.busy`. The Pi's 2.4.1 has both.
- `opencode api` starts a stopped service in the caller's process (see
  `HerdrClient._require_opencode_service`), so the poll checks `service status` first, as
  `agent_start` does. `--server <url>` is not an alternative: the Pi's service answers it
  with "requires a password" (checked 2026-11-25); a bare call authenticates itself.

## Acceptance

- An opencode run counts as finished only when herdr reports `idle`/`done` **and**, on two
  consecutive polls, `experimental.job.list` (sessionID, `descendants=true`) has no
  `running` job and `session.active` does not list the session.
- This covers every settle-wait: routine runs, the four gated/dispatch flows and pipeline
  stages (all via `wait_loop.prompt_with_watchdog`), and the report nudge.
- The extra wait is bounded by the job's existing `timeout_ms` (the nudge by
  `NUDGE_TIMEOUT_MS`) and logged. In the main wait the failure-marker scan and the pipeline
  heartbeat keep running while work is pending; the nudge's wait has no scan. A wait that
  ends at the deadline with work still running is a failure (`unsettled_status_working`;
  `subagent_wait_timeout` for a pipeline stage); a persistently unreadable state (5
  consecutive errors) falls back to the settle.
- When the signal is unavailable (service stopped, session id unknown, operation missing),
  behaviour is unchanged and a warning is logged.
- Claude and codex runs never call the opencode API.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-25**: filed; mechanism and completion signal verified live on the Pi.
- **2026-11-25**: done. `wait_loop.drain_opencode_background` runs after every opencode
  settle (main wait for routines, the four gated/dispatch flows and pipeline stages, plus the
  report nudge): herdr settled and `HerdrClient.opencode_background_busy` false on two
  consecutive polls, polled every 15 s (2 s to confirm a quiet poll), bounded by the job's
  `timeout_ms` (the nudge by `NUDGE_TIMEOUT_MS`). In the main wait the marker scan and pipeline heartbeat run
  on each poll while work is pending (not in the nudge's wait). An unreadable first poll (stopped service, opencode 2.1.4 without
  `experimental.job.list`, no session id) logs a warning and keeps the old behaviour.
  Not deployed: the Pi's next `fitted-pr-review` run is the live confirmation.
- **2026-11-26**: confirmed live on the Pi (deployed at 7c5500d). The 12:00 `fitted-pr-review`
  (`fitted-pr-review-20261126T150000Z`, session `ses_de7a0c7e5ffedEgXK8CUMZuu2M`) logged "has
  background work" at 12:02:56 and "background work finished after 647s" at 12:13:42, then ended
  `done` after 806 s with no nudge; both verify gates passed (review posted on fitted#1316).
