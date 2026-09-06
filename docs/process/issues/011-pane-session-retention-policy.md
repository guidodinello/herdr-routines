---
id: "011"
title: "Pane/session retention policy"
status: open
priority: low
area: pipeline
---

## Description

Lock in when a finished run's pane/session is cleaned up. Direction
(conversation 2026-08-21/22): capture the full transcript to the run-history
log as soon as a run finishes, then close the pane. Actual cleanup timing
(immediate vs. keep-for-a-week vs. manual) is the open decision.

Partly implemented already: `execute_run` closes its own pane on every
settled terminal path, capturing the agent session id first
(`RunOutcome.session_id` in `history.jsonl`) so a human can resume-and-inspect
(PR #42). What is missing is the *transcript capture to the history log*
before that close, and a documented retention window.

## Re-refinement (2026-09-06) — what is actually left

Re-read against the code after issues 032, 037 and PR #42 shipped. The gap is
narrower than the original text, and in one respect different from it.

**Already done:**

- `execute_run` closes its own pane on every settled terminal path and captures
  `RunOutcome.session_id` first, so a human can resume-and-inspect (PR #42).
- `_capture_visible_tail()` writes `{run_id}.tail.txt` to the reports dir, and is
  called from **every failure path**: `agent_start_failed`, `agent_not_interactive`,
  the prompt-wedge (`quota_exhausted` / `agent_prompt_failed`), the generic
  `unsettled_status_*` path, and — since issue 037 — the pipeline launcher's
  `blocked` settle.

**Still missing, and this is the whole issue:**

1. **The success path captures nothing.** Every `_capture_visible_tail` call site is
   a failure branch. A run that settles `idle`/`done` writes its report, closes its
   pane, and leaves no transcript. So the run you most often want to read after the
   fact — the one that worked, whose report you want to check against what the agent
   actually did — is the one with no record beyond the report the agent chose to
   write about itself.
2. **"Transcript" vs "visible tail".** `_capture_visible_tail` reads the *visible
   screen* (`agent_read --source visible --lines N`) — the last screenful, not the
   session. For a failure that is the right thing: you want what it was stuck on. For
   a successful run it is close to useless, since the screen by then shows the tail
   end of a summary. Decide deliberately whether success-path capture means a bounded
   screen read, a fuller scrollback read, or nothing — do not just call the existing
   helper on one more branch and declare it done.
3. **No documented retention window.** The original issue's open decision (immediate
   vs keep-for-a-week vs manual) is still open, and now spans two artifact families:
   `.tail.txt` files and reports. Issue 021 (log rotation) covers pruning the reports
   directory; this issue should settle the *policy* and let 021 implement the
   mechanism, rather than the two inventing separate answers.

**Do not** widen this into "capture everything always". A 108 MB `.venv` per worktree
is already the dominant on-disk cost (see issue 044); an unbounded per-run transcript
would be a second one. Whatever is captured must be bounded, and the bound stated.

## Acceptance

- On settle, the run's visible transcript (or a bounded tail) is persisted to
  the run-history artifact before the pane is closed.
- Retention timing for the underlying agent session is documented and
  consistent between routine jobs and pipeline workers.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. G-16 / PR #42 already did
  the immediate-close half; this issue is the transcript-capture + documented
  window that was punted.
