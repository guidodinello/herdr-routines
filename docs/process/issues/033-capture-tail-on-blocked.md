---
id: "033"
title: "Capture a diagnostic tail on a blocked settle, not just other failure paths"
status: open
priority: medium
area: runner
---

## Description

`execute_run` (`src/herdr_routines/runner.py`) captures a screen tail
(`{run_id}.tail.txt` under the reports dir) on every other failure path —
`agent_start_failed`, `agent_not_interactive`, the prompt-wedge
(`quota_exhausted`/`agent_prompt_failed`) path, and the generic
`unsettled_status_*` path all call `_capture_visible_tail(...)` before
returning. The `settled_status == "blocked"` branch does not:

```python
if settled_status == "blocked":
    return RunOutcome(state="failed", reason="blocked", **common)
```

There is a "best-effort diagnostic tail" block just above this (runner.py,
right after the prompt-wait succeeds) that unconditionally tries
`client.agent_read(job.agent_name, lines=200)` — but it uses the plain
(non-`--source visible`) read, wrapped in a bare `except OSError: pass`.
`_capture_visible_tail`'s own docstring notes `agent_read_visible`
specifically exists because "recent-unwrapped is rejected while unsettled" —
i.e. the plain `agent_read` this best-effort block uses is exactly the read
that's expected to fail for a `blocked` (still-interactive, not fully
settled) agent, and the failure is silently swallowed. So `blocked` is the
one failure mode most likely to need a human to see *what it was blocked
on*, and it's the one path that saves nothing to look at.

**Hit on 2026-09-03**: investigating recurring `blocked` failures on
`fitted-pr-review` (Aug 30), `fitted-implementer` and `fitted-pr-review-2`
(Sep 2), found zero `.tail.txt` files for any of the three runs — confirmed
by listing the reports dir and comparing against `no_report`/`quota_exhausted`
runs from the same jobs, which all have tail files. The Pi also happened to
reboot before this investigation, which would have killed any still-open
blocked pane (a `blocked` outcome never calls `_close_run_pane`, and the
pre-run stale-pane reap explicitly excludes non-idle/done agents — a blocked
pane is intentionally left open for human resume) — so between the missing
tail file and the reboot, there was no way to recover what these three runs
were actually blocked on.

## Design (proposal)

Add a `_capture_visible_tail(client, job.agent_name, reports_dir=report_path.parent, run_id=run_id)` call in the `settled_status == "blocked"` branch, mirroring the
other failure paths exactly — same helper, same call shape. Since
`_capture_visible_tail` already uses `agent_read_visible` (the read that's
designed to work while unsettled), this should succeed where the existing
best-effort plain-`agent_read` block silently fails for this case.

Do **not** change the rest of the `blocked` branch's behavior: it should
still leave the pane open (no `_close_run_pane`, no `_capture_session_id`) —
that's the intentional "a human can resume and see what it's stuck on"
design, this issue only adds the diagnostic file so there's something to
look at even after the pane itself is gone (closed manually, or lost to a
reboot, as happened here).

Files: `src/herdr_routines/repos.py` not involved; only
`src/herdr_routines/runner.py`'s `execute_run`, plus
`tests/test_runner.py`.

## Acceptance

- A run that settles `blocked` gets a `{run_id}.tail.txt` written under the
  reports dir, same as every other failure path already does.
- The pane is still left open on a `blocked` outcome — no regression to the
  existing human-resume design (verify no `_close_run_pane`/
  `_capture_session_id` call was added to this branch).
- If the visible-tail read itself fails (`_capture_visible_tail` already
  swallows `OSError` and returns `""`), the run still terminates cleanly as
  `state="failed", reason="blocked"` exactly as today — no new failure mode
  introduced.
- Existing tests for `no_report`/`quota_exhausted`/`unsettled_status_*` tail
  capture are unaffected.

## Log

- **2026-09-03**: filed after finding zero diagnostic tails for 3 real
  `blocked` failures on `fitted-*` jobs (Aug 30, Sep 2) — the data was
  already unrecoverable (missing tail file + a Pi reboot that killed any
  still-open pane), but the code gap causing it is fixable so the *next*
  `blocked` failure leaves something to investigate. Traced to
  `_capture_visible_tail` (uses `agent_read_visible`, works while unsettled)
  vs. the existing best-effort block just above the `blocked` check (uses
  plain `agent_read`, silently fails for an unsettled/blocked agent).
