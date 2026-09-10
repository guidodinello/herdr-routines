---
id: "051"
title: "a blocked agent from a prior run permanently wedges its recurring job (agent_name_taken); issue-refinement also calls a bare herdr-routines"
status: done
priority: high
area: routines
---

## Description

The issue-refinement job produced no PR on the night of 2026-09-08 (run
`issue-refinement-20260909T010000Z`). `history.jsonl`:

```json
{"state": "failed", "run_id": "issue-refinement-20260909T010000Z", "agent": null,
 "reason": "agent_start_failed",
 "error": "herdr server error running agent start rt-issue-refinement …:
   {\"error\":{\"code\":\"agent_name_taken\",\"message\":\"agent name rt-issue-refinement
   is already used; candidates: … pane_id=w4B:p1 … cwd=…/auto-issue-refinement-20260908t010000z
   status=Blocked\"}}"}
```

The **previous** night's run (`issue-refinement-20260908T010000Z`) did all of its
work — authored issue 049, ran the 3-pass review loop, opened PR #111, wrote its
report — and then **opencode never settled to `idle`**. It sat `Blocked` at the
end (a known opencode-TUI-doesn't-settle-after-done class; the visible screen just
shows the final "Done." summary, no permission prompt). The runner recorded the
run `failed` / `reason: blocked` and captured a `.tail.txt`, but left the agent
alive.

`herdr.settled_agent_pane()` (the runner's pre-start stale-pane reap,
`runner.py:550`) is **deliberately** scoped to `idle`/`done` only — its docstring:
*"blocked/unknown are sticky under cron and must not be reaped"*, to protect the
evidence pane (issue 033). The consequence: **`rt-issue-refinement` stayed
`Blocked` on `w4B:p1` and every subsequent nightly run fails `agent_name_taken`**
until a human runs `herdr pane close w4B:p1`. As of 2026-09-09 it had eaten two
nights and was still parked.

The evidence the no-reap rule protects is already on disk (`_capture_visible_tail`
wrote `issue-refinement-20260908T010000Z.tail.txt` when the run was recorded
failed). The live pane is redundant, but nothing ever clears it.

### Secondary: bare `herdr-routines refine-issue` (issue 050 class)

`deploy/jobs.d/issue-refinement.yaml` step 1 told the agent to run
`herdr-routines refine-issue` — bare, not `uv run herdr-routines` — the same
not-a-standalone-binary bug fixed for the orchestrator prompt in issue 050 / PR
#113. #111's agent worked around it; it stays latent otherwise. **Fixed in this
PR** (`uv run herdr-routines refine-issue`). The blocked-agent wedge above is
*not* fixed here — it needs a product decision (below).

## Design (chosen: A + B)

The no-reap-blocked rule and "recurring job must be able to start" are in tension.
Options considered: **A** reap on `agent_name_taken` + retry once; **B** keep
no-reap but surface the lost run loudly; **C** age-gate the reap. **A + B** shipped
— self-heal so the job keeps running, *and* still flag that a run was force-recovered,
since a blocked settle right after a successful PR (as here) often means a real
opencode "doesn't settle after done" bug worth a human look. C's age-gate is
unnecessary once the reap is scoped to an actual name collision.

Implementation:

- **A.** `herdr.sticky_agent_pane(name)` — the inverse filter of `settled_agent_pane`,
  returns `(pane_id, status)` only for a `blocked`/`unknown` registered agent.
  `runner._start_agent_reaping_stale_collision` calls it **only** when `agent start`
  fails `agent_name_taken` (matched on the error body's `code`), force-closes that
  pane, and retries the start exactly once. A collision with a genuinely `working`
  agent (`sticky_agent_pane` → `None`) is left alone and falls through to
  `agent_start_failed` as before. The pre-start best-effort reap
  (`settled_agent_pane`, idle/done only) is unchanged — the evidence pane for a
  clean-exit prior run still survives; only a name **collision** triggers the
  force-close, and by then the prior run is already recorded `failed` with its
  `.tail.txt` on disk.
- **B.** `RunOutcome.reaped_stale_agent` flows to the terminal history record
  (`_outcome_extra` → `extra["reaped_stale_agent"]`) and flips the terminal
  notification from `success` to `finding` (same treatment as a `fallback_model`
  retry), body: "force-closed a prior run's blocked agent (issue 051)".

## Acceptance criteria

1. `issue-refinement.yaml` invokes `herdr-routines` only via `uv run`. Test: `test_issue_refinement_job_invokes_herdr_routines_via_uv_run`
2. A blocked agent left by a prior run no longer permanently wedges the next run; the runner force-closes it and retries once. Test: `test_execute_run_reaps_blocked_agent_on_name_collision` (+ `test_execute_run_does_not_reap_a_working_agent_on_collision`)
3. A run that only started because a stale agent was reaped is surfaced (history flag + `finding` notification), not a silent success. Test: `test_history_and_notification_surface_a_reaped_stale_agent`
