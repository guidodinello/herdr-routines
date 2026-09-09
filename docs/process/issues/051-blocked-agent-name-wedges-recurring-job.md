---
id: "051"
title: "a blocked agent from a prior run permanently wedges its recurring job (agent_name_taken); issue-refinement also calls a bare herdr-routines"
status: open
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

## Design (open — pick one)

The no-reap-blocked rule and "recurring job must be able to start" are in tension.

- **A. Reap a blocked agent's pane on `agent_name_taken`, retry start once.** The
  prior run is already recorded `failed` and its tail is on disk. Simple, keeps
  the job self-healing. Loses the live pane a human might have wanted to resume.
- **B. Keep no-reap; surface it loudly.** Digest (issue 010) / watchdog (issue
  031) reports "job X has a blocked agent parked since <ts>, N runs skipped" so a
  human clears it within a day instead of discovering it a week later.
- **C. Age-gate.** Reap a blocked agent's pane only once it is older than one
  cron interval (it is not coming back on its own by then).

Recommendation: **A + B** — self-heal so the job keeps running, *and* still
report that a run was lost, since a blocked settle after a successful PR (as here)
often means a real bug worth a human look.

## Acceptance criteria

1. `issue-refinement.yaml` invokes `herdr-routines` only via `uv run`. Test: `test_issue_refinement_job_invokes_herdr_routines_via_uv_run`
2. A blocked agent left by a prior run no longer permanently wedges the next run of a recurring job. Test: `test_blocked_agent_does_not_wedge_recurring_job`
3. A run lost to a pre-existing blocked agent is still surfaced (digest or watchdog), not silently skipped. Test: `test_lost_run_from_blocked_agent_is_reported`
