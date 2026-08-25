# Pipeline pane lifecycle — v2 proposal (close-and-resume)

Status: proposal (2026-08-24 night), not yet built or reviewed. Written after two real
dogfood runs (`20260823T234906Z` → PR #27, `20260824T232136Z` → PR #28, and the in-progress
`20260825T000735Z`) exposed a real memory ceiling on the Pi. Read-only relative to
`design.md`/`spec.md` — this proposes an amendment, it does not change them.

## The problem, with evidence

`design.md:190-195` gives each stage a unique worker `pl-<stage>-<run_id>`, **except stage 6**,
which reuses `pl-3-<run_id>`'s live session via `herdr agent prompt pl-3-... ` — chosen
specifically to "preserve code context per G-11" (the noted alternative, a fresh `pl-6` seeded
with `git diff` + `gh pr view --comments`, was left as a fallback "if context burn becomes an
issue"). Cleanup (`design.md:159-168`, audit gap 12/G-10) closes **all** worker panes together,
but only once, at the very end of the whole run, after `$PIPELINE_REPORT` is mirrored.

The consequence, confirmed empirically tonight via `herdr agent list` mid-run: **every stage's
opencode process stays resident for the rest of the run**, whether or not anything later reuses
it. By stage 4 of the `20260825T000735Z` run there were 5 concurrent opencode processes alive
(orchestrator + `pl-1`/`pl-2`/`pl-3` all `done`-but-not-exited + the stage-4 worker), each costing
400MB–870MB RSS. By stage 5 (`big-pickle` code review), the Pi's 2GB swap was fully exhausted
(down to ~112KB free) with **zero** headroom left, though it did not tip into an OOM-kill this
time. This is not a fluke of one run: it happened on both completed dogfood runs, worse on the
second (heavier stage-5 model). It is the structural behavior of the current design on a 4GB
host, not an edge case.

Checking actual reuse across both completed runs: **only `pl-3` is ever reused** (by stage 4 and
stage 6). `pl-1`, `pl-2`, and `pl-5` are never touched again after their own gate passes. So most
of the "keep everything alive" cost buys nothing — it's not a tradeoff being made deliberately
per-worker, it's a blanket policy that happens to only pay off for one worker.

## Proposed change

Close a worker's pane as soon as its stage's gate has passed and the handoff (commit + `state.json`
update) is confirmed on disk — for every worker **except** whichever one a later stage will reuse
for context. For that one (`pl-3`, under the current stage plan), close it too, but **reopen it by
resuming its session** when the later stage actually needs it, instead of keeping it idle the whole
time in between.

This is not a new capability to build — `opencode --help` already supports it. The interactive/TUI
launch form (the same one `herdr agent start ... -- -m <model>` already uses) takes:

```
-c, --continue     continue the last session
-s, --session       session id to continue
    --fork          fork the session before continuing (requires --continue or --session)
```

`agent_session.value` (the opencode session ID) is already captured for every worker in
`history.jsonl`/run reports — nothing new needs to be recorded. So stage 6 would become:
`herdr agent start pl-6-<run_id> --kind opencode --pane <fresh_pane> -- -m <model> -s <pl-3's
session_id>` instead of `herdr agent prompt pl-3-<run_id> ...` against a pane that's been sitting
open since stage 3. Same context preservation, none of the standing memory cost.

Net effect on the memory profile: at most 2 opencode processes resident at any point (orchestrator
+ whichever single stage is active), instead of one per stage reached so far. This should keep the
run well clear of the swap ceiling hit tonight, on the same 4GB Pi, with no change to what any
individual stage actually does.

## What doesn't change

- Gate mechanics, stage prompts, and the files-as-handoff model (`spec.md`, `state.json`, commits)
  are untouched — this is purely about when a pane/process is alive, not what gates check or how
  stages hand off.
- The end-of-run cleanup step (`design.md:159-168`) still applies to whatever's still open at that
  point (in practice, just the currently-active stage + the reopened `pl-3`-resume for stage 6).
- Crash-recovery reconciliation (`design.md:144`) still works: `state.json` plus `herdr agent list`
  filtered to `pl-*` remains the resume recipe. A closed-but-resumable worker just means "not found
  live, but its session ID is in state.json/history — reopen by `-s <id>` if this stage still needs
  it" is now a normal, expected case instead of only a crash-recovery edge case.

## Open questions before building this

1. Does `herdr agent start ... -- -m <model> -s <session_id>` actually attach opencode's TUI to
   that session cleanly (vs. starting a fresh one and silently ignoring `-s`)? Untested — verify
   empirically before relying on it, same caution the original design applied to the detached
   `--wait` form (`design.md:118-119`).
2. Exact trigger for "close this worker's pane" — immediately on gate-pass, or with a short grace
   window in case the orchestrator itself needs to re-read its output? Immediate is simpler and
   matches "the files are the handoff," but worth confirming there's no reliance on reading pane
   scrollback (`agent read --source visible`) after the gate check.
3. Whether `-s`/`--fork` interacts correctly with a *different* model than the one that created the
   session (stage 6 might reuse `pl-3`'s `x-preview-f-free` session, which is fine since it's the
   same worker; this wouldn't apply to `pl-5`'s `big-pickle` session since nothing reuses it).

## Rationale for filing this now rather than later

Tonight's second dogfood run pushed swap to functionally zero with nothing else running on the Pi
concurrently — that is close to the actual promotion-gate condition (a "few real overnight runs...
without human rescue"), not a stress test. The gap this proposal closes is a better candidate for
that gate than "run it more times and hope," per `raspberrypi/roadmap.md`'s memory-budget-gate item
and `raspberrypi/troubleshooting-log.md`'s 2026-08-24 entries.
